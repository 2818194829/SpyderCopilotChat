# -*- coding: utf-8 -*-
"""在真实 Spyder 进程里验证 inline diff 的装饰路径。

不模拟、不打桩：用 Spyder 自己的 EditorStack 打开一个真实文件，把磁盘
内容改掉，然后调用插件的 ReviewHub.stage()，看
  1. stage() 是否成功
  2. extraSelections 里到底有没有我们的装饰
  3. 工具栏 / 竖纹是否真的可见
并把编辑器截图存下来。
"""
import json
import os
import sys
import traceback

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

OUT = os.path.dirname(os.path.abspath(__file__))
RESULT = os.path.join(OUT, "probe_real_spyder.json")
SHOT = os.path.join(OUT, "probe-real-spyder.png")
TMP = os.path.join(os.environ.get("TEMP", OUT), "diff_probe_target.py")

OLD_TEXT = "\n".join([
    "# probe file",
    "def draw_player():",
    "    print('old body')",
    "    return 1",
    "",
    "def other():",
    "    return 42",
    "",
])

NEW_TEXT = "\n".join([
    "# probe file",
    "def draw_player():",
    "    print('new body')",
    "    print('extra line')",
    "    return 2",
    "",
    "def other():",
    "    return 42",
    "",
])

state = {"done": False}


def do_probe(main):
    from qtpy.QtWidgets import QApplication
    from spyder.api.plugins import Plugins

    result = {"ok": False, "steps": []}

    def note(key, value):
        result[key] = value
        result["steps"].append("%s=%r" % (key, value))

    try:
        # 1. 造一个真实文件，用 Spyder 自己的栈打开
        with open(TMP, "w", encoding="utf-8") as fh:
            fh.write(OLD_TEXT)
        editor_plugin = main.get_plugin(Plugins.Editor)
        note("editor_plugin", type(editor_plugin).__name__)
        stack = editor_plugin.get_current_editorstack()
        note("stack", type(stack).__name__ if stack else None)
        editor = stack.load(TMP, set_current=True)
        if editor is None:
            editor = stack.get_current_editor()
        note("editor", type(editor).__name__ if editor else None)
        note("editor_text_head", (editor.toPlainText() or "")[:40])
        note("doc_modified_after_load", editor.document().isModified())

        # 2. 模拟 Agent 落盘
        with open(TMP, "w", encoding="utf-8") as fh:
            fh.write(NEW_TEXT)
        note("doc_modified_after_disk_write", editor.document().isModified())

        # 3. 走插件真实的 stage 路径
        from spyder_copilot_chat.widgets.editor_diff import ReviewHub
        note("plugin_version", __import__(
            "spyder_copilot_chat").__version__)
        hub = ReviewHub()
        hub.last_failures = []
        ok = hub.stage(editor, TMP, OLD_TEXT, saver=lambda *a: True)
        note("stage_ok", bool(ok))
        note("hub_failures", list(hub.last_failures))

        app = QApplication.instance()
        for _ in range(5):
            app.processEvents()

        # 4. 装饰到底有没有挂上
        note("doc_block_count", editor.document().blockCount())
        note("doc_head", (editor.toPlainText() or "")[:120])
        note("hasattr_decorations", hasattr(editor, "decorations"))
        if hasattr(editor, "decorations"):
            key = "copilot_chat_inline_diff"
            stored = editor.decorations.get(key)
            note("decorations_stored", len(stored) if stored else 0)
            note("decorations_keys", list(
                getattr(editor.decorations, "_decorations", {}).keys()))
        note("extra_selections", len(editor.extraSelections()))
        note("buffer_block_numbers", editor.get_buffer_block_numbers())
        notes = []
        for sel in editor.extraSelections()[:8]:
            try:
                bg = sel.format.background().color().name()
                fg = sel.format.foreground().color().name()
                strike = sel.format.fontStrikeOut()
                blk = sel.cursor.blockNumber()
                notes.append("%s bg=%s fg=%s strike=%s blk=%s"
                             % (type(sel).__name__, bg, fg, strike, blk))
            except Exception as exc:
                notes.append("sel err %s" % exc)
        note("extra_selection_detail", notes)

        # 5. UI 是否可见
        reviews = list(hub.reviews())
        note("active_reviews", len(reviews))
        if reviews:
            rev = reviews[0]
            note("deco_count", getattr(rev, "deco_count", -1))
            note("fallback_decorations",
                 getattr(rev, "fallback_decorations", None))
            bar = getattr(rev, "_bar", None)
            stripe = getattr(rev, "_stripe", None)
            note("bar_isHidden", bar.isHidden() if bar else None)
            note("bar_visible", bar.isVisible() if bar else None)
            note("bar_geometry", str(bar.geometry()) if bar else None)
            note("stripe_isHidden", stripe.isHidden() if stripe else None)
            note("viewport_size", "%dx%d" % (
                editor.viewport().width(), editor.viewport().height()))

        # 6. 截图
        editor.viewport().grab().save(SHOT)
        note("screenshot", SHOT)
        result["ok"] = True
    except Exception:
        result["traceback"] = traceback.format_exc()
    finally:
        with open(RESULT, "w", encoding="utf-8") as fh:
            json.dump(result, fh, ensure_ascii=False, indent=2)
        state["done"] = True
        QApplication.instance().quit()


def main_entry():
    import spyder.app.mainwindow as mw
    from qtpy.QtCore import QTimer

    def log(msg):
        print("[probe] " + msg, flush=True)

    original = mw.MainWindow.showMaximized

    def patched(self, *args, **kwargs):
        out = original(self, *args, **kwargs)
        log("showMaximized called")
        if not state["done"]:
            QTimer.singleShot(2500, lambda: do_probe(self))
        return out

    mw.MainWindow.showMaximized = patched

    original_init = mw.MainWindow.__init__

    def patched_init(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        log("mainwindow constructed")

    mw.MainWindow.__init__ = patched_init

    # 用户此刻已经开着一个 Spyder：单实例守卫会直接退出，必须新实例。
    sys.argv = [sys.argv[0], "--new-instance"]
    log("starting spyder")
    from spyder.app import start
    start.main()
    log("spyder exited")


if __name__ == "__main__":
    main_entry()
