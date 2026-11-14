# -*- coding: utf-8 -*-
"""v0.8.10 探针：真插件 + 三个文件 + 新工具条（文件计数 / 两个总开关）。

验的是**装进 user site 的那份 wheel**，不是源码目录——所以这里刻意不往
sys.path 插仓库路径。上一个版本的教训是：源码单测全绿、现场全废。

关注点：
  1) 标签真的写出「i/N 文件」
  2) 「接受全部修改」「不接受本次修改」都在、都点得动
  3) 点「不接受本次修改」后三个文件的磁盘内容全部回到修改前
  4) 工具条不许超出视口（v0.8.9 之前会撑到 717px 盖住编辑器）
"""
import json
import os
import sys
import traceback

OUT = os.path.dirname(os.path.abspath(__file__))
RESULT = os.path.join(OUT, "probe_v090.json")
SHOT = os.path.join(OUT, "probe-v090.png")
SHOT_BEFORE = os.path.join(OUT, "probe-v090-staged.png")
TMPDIR = os.path.join(os.environ.get("TEMP", OUT), "diff_probe_v090")
os.makedirs(TMPDIR, exist_ok=True)

NAMES = ["probe_a.py", "probe_b.py", "probe_c.py"]
FILES = [os.path.join(TMPDIR, n) for n in NAMES]

OLD_TEXT = "\n".join(
    ["# probe file"] + ["x = %d" % i for i in range(1, 30)] + [
        "def draw_player():",
        "    print('old body')",
        "    return 1",
    ] + ["y = %d" % i for i in range(1, 15)] + [""])

NEW_TEXT = "\n".join(
    ["# probe file"] + ["x = %d" % i for i in range(1, 30)] + [
        "def draw_player():",
        "    print('new body')",
        "    print('extra line')",
        "    return 2",
    ] + ["y = %d" % i for i in range(1, 15)] + [""])

state = {"done": False, "result": None}


def read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read().replace("\r\n", "\n")


def do_probe(main):
    if state["done"]:
        return
    from qtpy.QtWidgets import QApplication
    from qtpy.QtCore import QTimer
    from spyder.api.plugins import Plugins

    result = {"ok": False, "steps": []}

    def note(key, value):
        result[key] = value
        result["steps"].append("%s=%r" % (key, value))

    def finish():
        state["done"] = True
        with open(RESULT, "w", encoding="utf-8") as fh:
            json.dump(result, fh, ensure_ascii=False, indent=2)
        state["result"] = result
        QApplication.instance().quit()

    try:
        import spyder_copilot_chat as pkg
        from spyder_copilot_chat.plugin import CopilotChatPlugin

        note("plugin_version", pkg.__version__)
        note("plugin_path", os.path.dirname(pkg.__file__))

        plugin = None
        for key in (getattr(CopilotChatPlugin, "NAME", "copilot_chat"),
                    "copilot_chat"):
            try:
                plugin = main.get_plugin(key)
            except Exception:
                plugin = None
            if plugin is not None:
                note("plugin_key", key)
                break
        note("plugin", type(plugin).__name__ if plugin else None)
        if plugin is None:
            result["traceback"] = "插件未加载"
            return finish()

        # 1) 三个文件：先落旧内容、加载进编辑器，再写新内容（模拟 agent 已写盘）
        editor_plugin = main.get_plugin(Plugins.Editor)
        stack = editor_plugin.get_current_editorstack()
        for path in FILES:
            with open(path, "w", encoding="utf-8", newline="") as fh:
                fh.write(OLD_TEXT)
            ed = stack.load(path, set_current=False)
            if ed is not None and not hasattr(ed, "document"):
                ed = getattr(ed, "editor", None)
            with open(path, "w", encoding="utf-8", newline="") as fh:
                fh.write(NEW_TEXT)

        res = plugin.review_files([(p, OLD_TEXT) for p in FILES])
        note("review_result", res)
        note("disk_is_new_before", [read(p) == NEW_TEXT for p in FILES])

        app = QApplication.instance()
        for _ in range(12):
            app.processEvents()

        hub = getattr(plugin, "_review_hub", None)
        reviews = list(hub.reviews()) if hub is not None else []
        note("active_reviews", len(reviews))

        bars = []
        for rev in reviews:
            bar = getattr(rev, "_bar", None)
            if bar is None:
                bars.append({"file": os.path.basename(rev.path),
                             "bar": None})
                continue
            info = {
                "file": os.path.basename(rev.path),
                "text": bar._text,
                "accept_all": bar._accept_all.text(),
                "reject_all": bar._reject_all.text(),
                "accept_hidden": bar._accept_all.isHidden(),
                "reject_hidden": bar._reject_all.isHidden(),
                "accept_w": bar._accept_all.width(),
                "reject_w": bar._reject_all.width(),
                "bar_w": bar.width(),
                "viewport_w": rev.editor.viewport().width(),
            }
            bars.append(info)
        note("bars", bars)
        # 决策前先留一张：此刻三个文件都还挂着红绿行和工具条
        import time

        def wait(ms):
            end = time.time() + ms / 1000.0
            while time.time() < end:
                app.processEvents()
                time.sleep(0.01)

        wait(1500)
        try:
            main.grab().save(SHOT_BEFORE)
            note("screenshot_staged", SHOT_BEFORE)
        except Exception as exc:
            note("screenshot_staged_error", repr(exc))
        # 布局落定后的二次量测：字号 / sizeHint / minimumWidth 到底是多大
        late = []
        for rev in reviews:
            bar = getattr(rev, "_bar", None)
            if bar is None:
                continue
            bar.refresh()
            wait(150)
            late.append({
                "text": bar._text,
                "label_text": bar._label.text(),
                "label_w": bar._label.width(),
                "bar_w": bar.width(),
                "bar_h": bar.height(),
                "vp_w": rev.editor.viewport().width(),
                "font_px": bar._label.font().pixelSize(),
                "btn": [{
                    "t": b.text(),
                    "w": b.width(),
                    "min": b.minimumWidth(),
                    "sh": b.sizeHint().width(),
                    "hidden": b.isHidden(),
                } for b in bar._buttons()],
            })
        note("bars_late", late)
        texts = [b["text"] for b in bars if b.get("text")]
        result["ok"] = (bool(res.get("staged")) and len(texts) == 3
                        and all(t.startswith("%d/3" % (i + 1))
                                for i, t in enumerate(texts)))

        # 2) 点「不接受本次修改」
        if reviews:
            rev = reviews[0]
            bar = getattr(rev, "_bar", None)
            if bar is not None:
                bar._reject_all.click()
                for _ in range(12):
                    app.processEvents()
        note("active_reviews_after_reject",
             len(hub.reviews()) if hub is not None else -1)
        note("disk_back_to_old",
             [read(p).strip("\n") == OLD_TEXT.strip("\n") for p in FILES])

        # 3) 单文件再来一次，确认标签不再带「文件」，点接受全部
        solo = FILES[0]
        with open(solo, "w", encoding="utf-8", newline="") as fh:
            fh.write(OLD_TEXT)
        ed = stack.load(solo, set_current=True)
        if ed is not None and not hasattr(ed, "document"):
            ed = getattr(ed, "editor", None)
        with open(solo, "w", encoding="utf-8", newline="") as fh:
            fh.write(NEW_TEXT)
        res2 = plugin.review_files([(solo, OLD_TEXT)])
        note("review_result_solo", res2)
        for _ in range(12):
            app.processEvents()
        revs2 = list(hub.reviews()) if hub is not None else []
        note("solo_reviews", len(revs2))
        if revs2:
            bar2 = getattr(revs2[0], "_bar", None)
            if bar2 is not None:
                note("solo_bar_text", bar2._text)
                note("solo_bar_buttons", len(bar2._buttons()))
                note("solo_bar_geom", str(bar2.geometry()))
                note("solo_viewport_w", revs2[0].editor.viewport().width())
                bar2._accept_all.click()
                for _ in range(12):
                    app.processEvents()
        note("disk_new_after_accept", read(solo) == NEW_TEXT)
        note("active_reviews_after_accept",
             len(hub.reviews()) if hub is not None else -1)

        def shot():
            try:
                main.grab().save(SHOT)
                note("screenshot", SHOT)
            finally:
                finish()

        QTimer.singleShot(900, shot)
    except Exception:
        result["traceback"] = traceback.format_exc()
        finish()


def main_entry():
    import spyder.app.mainwindow as mw
    from qtpy.QtCore import QTimer

    def log(msg):
        print("[probe] " + msg, flush=True)

    original = mw.MainWindow.showMaximized

    def patched(self, *args, **kwargs):
        out = original(self, *args, **kwargs)
        log("showMaximized")
        QTimer.singleShot(4500, lambda: do_probe(self))
        return out

    mw.MainWindow.showMaximized = patched

    orig_init = mw.MainWindow.__init__

    def patched_init(self, *a, **kw):
        orig_init(self, *a, **kw)
        log("constructed")
        QTimer.singleShot(15000, lambda: do_probe(self))
        QTimer.singleShot(27000, lambda: do_probe(self))

    mw.MainWindow.__init__ = patched_init

    sys.argv = [sys.argv[0], "--new-instance"]
    log("starting spyder")
    from spyder.app import start
    start.main()
    log("spyder exited")


if __name__ == "__main__":
    main_entry()
