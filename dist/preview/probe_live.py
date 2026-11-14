# -*- coding: utf-8 -*-
"""在真实 Spyder 进程里验证内联 diff —— 带界面的版本。

offscreen 平台下 Spyder 的 showMaximized 路径不完整，探针跑不到点上。
这里用默认平台，让窗口真弹出来几秒（--new-instance，不干扰用户已经
打开的实例），把整条链路走完再自己退出。
"""
import json
import os
import sys
import traceback

OUT = os.path.dirname(os.path.abspath(__file__))
RESULT = os.path.join(OUT, "probe_live.json")
SHOT = os.path.join(OUT, "probe-live.png")
TMP = os.path.join(os.environ.get("TEMP", OUT), "diff_probe_live.py")

OLD_TEXT = "\n".join(
    ["# probe file"] + ["x = %d" % i for i in range(1, 40)] + [
        "def draw_player():",
        "    print('old body')",
        "    return 1",
    ] + ["y = %d" % i for i in range(1, 20)] + [""])

NEW_TEXT = "\n".join(
    ["# probe file"] + ["x = %d" % i for i in range(1, 40)] + [
        "def draw_player():",
        "    print('new body')",
        "    print('extra line')",
        "    return 2",
    ] + ["y = %d" % i for i in range(1, 20)] + [""])

state = {"done": False, "result": None}


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
        editor_plugin = main.get_plugin(Plugins.Editor)
        if editor_plugin is None:
            print("[probe] editor plugin not ready", flush=True)
            return
        with open(TMP, "w", encoding="utf-8") as fh:
            fh.write(OLD_TEXT)
        stack = editor_plugin.get_current_editorstack()
        note("stack", type(stack).__name__ if stack else None)
        editor = stack.load(TMP, set_current=True)
        note("editor", type(editor).__name__ if editor else None)
        note("doc_modified_after_load", editor.document().isModified())

        with open(TMP, "w", encoding="utf-8") as fh:
            fh.write(NEW_TEXT)
        note("doc_modified_after_write", editor.document().isModified())

        from spyder_copilot_chat.widgets.editor_diff import ReviewHub
        note("plugin_version", __import__("spyder_copilot_chat").__version__)
        hub = ReviewHub()
        hub.last_failures = []
        ok = hub.stage(editor, TMP, OLD_TEXT, saver=lambda *a: True)
        note("stage_ok", bool(ok))
        note("hub_failures", list(hub.last_failures))

        app = QApplication.instance()
        for _ in range(8):
            app.processEvents()

        note("doc_block_count", editor.document().blockCount())
        note("buffer_block_numbers", editor.get_buffer_block_numbers())
        note("cursor_block", editor.textCursor().blockNumber())
        note("extra_selections", len(editor.extraSelections()))
        if hasattr(editor, "decorations"):
            stored = editor.decorations.get("copilot_chat_inline_diff")
            note("decorations_stored", len(stored) if stored else 0)
        detail = []
        for sel in editor.extraSelections()[:10]:
            try:
                detail.append("blk=%s bg=%s strike=%s" % (
                    sel.cursor.blockNumber(),
                    sel.format.background().color().name(),
                    sel.format.fontStrikeOut()))
            except Exception as exc:
                detail.append("err %s" % exc)
        note("extra_detail", detail)

        reviews = list(hub.reviews())
        note("active_reviews", len(reviews))
        if reviews:
            rev = reviews[0]
            note("deco_count", getattr(rev, "deco_count", -1))
            bar = getattr(rev, "_bar", None)
            stripe = getattr(rev, "_stripe", None)
            note("bar_visible", bar.isVisible() if bar else None)
            note("bar_geometry", str(bar.geometry()) if bar else None)
            note("stripe_visible", stripe.isVisible() if stripe else None)
        note("viewport", "%dx%d" % (editor.viewport().width(),
                                    editor.viewport().height()))

        app.processEvents()

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
        QTimer.singleShot(3500, lambda: do_probe(self))
        return out

    mw.MainWindow.showMaximized = patched

    # 兜底：某些启动路径不走 showMaximized
    orig_init = mw.MainWindow.__init__

    def patched_init(self, *a, **kw):
        orig_init(self, *a, **kw)
        log("constructed")
        QTimer.singleShot(12000, lambda: do_probe(self))
        QTimer.singleShot(30000, lambda: do_probe(self))

    mw.MainWindow.__init__ = patched_init

    sys.argv = [sys.argv[0], "--new-instance"]
    log("starting spyder")
    from spyder.app import start
    start.main()
    log("spyder exited")


if __name__ == "__main__":
    main_entry()
