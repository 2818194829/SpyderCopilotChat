# -*- coding: utf-8 -*-
"""v0.8.9 探针：走**真插件对象**的 review_files（不是 ReviewHub 单测）。

v0.8.8 及之前的探针都直接 new 一个 ReviewHub 来验证渲染，恰好绕过了
插件里 ``self._review_hub = None`` 遮蔽同名方法的那个 bug —— 于是探针
全绿、用户那边一次都没生效。这个探针从 ``main.get_plugin(...)`` 拿真
插件，调它自己的 ``review_files``，并把 staged / failures / 装饰 / 工具
条全部写进 JSON。
"""
import json
import os
import sys
import traceback

OUT = os.path.dirname(os.path.abspath(__file__))
RESULT = os.path.join(OUT, "probe_v089.json")
SHOT = os.path.join(OUT, "probe-v089.png")
TMP = os.path.join(os.environ.get("TEMP", OUT), "diff_probe_v089.py")

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

        # 1) 先确认插件对象真的在
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
            result["traceback"] = "插件未加载，无法验证 review_files"
            return finish()
        note("plugin_attr_review_hub", repr(getattr(plugin, "_review_hub", "?")))
        note("has_ensure_hub",
             callable(getattr(plugin, "_ensure_review_hub", None)))

        # 2) 准备文件：先落旧内容并加载进编辑器，再写新内容
        editor_plugin = main.get_plugin(Plugins.Editor)
        stack = editor_plugin.get_current_editorstack()
        with open(TMP, "w", encoding="utf-8", newline="") as fh:
            fh.write(OLD_TEXT)
        editor = stack.load(TMP, set_current=True)
        # Spyder 6 的 load() 返回 FileInfo，老版本直接返回 editor
        if editor is not None and not hasattr(editor, "document"):
            editor = getattr(editor, "editor", None)
        note("editor", type(editor).__name__ if editor else None)
        with open(TMP, "w", encoding="utf-8", newline="") as fh:
            fh.write(NEW_TEXT)

        # 3) 走真正的入口
        note("items_in", 1)
        res = plugin.review_files([(TMP, OLD_TEXT)])
        note("review_result", res)

        app = QApplication.instance()
        for _ in range(10):
            app.processEvents()

        note("doc_block_count", editor.document().blockCount())
        note("extra_selections", len(editor.extraSelections()))
        if hasattr(editor, "decorations"):
            stored = editor.decorations.get("copilot_chat_inline_diff")
            note("decorations_stored", len(stored) if stored else 0)
        detail = []
        for sel in editor.extraSelections()[:8]:
            try:
                detail.append("blk=%s bg=%s strike=%s" % (
                    sel.cursor.blockNumber(),
                    sel.format.background().color().name(),
                    sel.format.fontStrikeOut()))
            except Exception as exc:
                detail.append("err %s" % exc)
        note("extra_detail", detail)

        hub = getattr(plugin, "_review_hub", None)
        note("hub_type", type(hub).__name__ if hub is not None else None)
        if hub is not None and hasattr(hub, "reviews"):
            reviews = list(hub.reviews())
            note("active_reviews", len(reviews))
            if reviews:
                rev = reviews[0]
                note("deco_count", getattr(rev, "deco_count", -1))
                bar = getattr(rev, "_bar", None)
                note("bar_visible", bar.isVisible() if bar else None)
                note("bar_geometry", str(bar.geometry()) if bar else None)

        # 4) 第二轮：登记处必须复用，不再新建
        hub_before = id(hub) if hub is not None else None
        plugin.review_files([(TMP, OLD_TEXT)])
        hub_after = id(getattr(plugin, "_review_hub", None))
        note("hub_reused", hub_before == hub_after)
        result["ok"] = bool(res.get("staged")) and not res.get("failures")

        app.processEvents()

        def shot():
            try:
                hub2 = getattr(plugin, "_review_hub", None)
                revs = list(hub2.reviews()) if hub2 is not None else []
                if revs:
                    rev = revs[0]
                    bar = getattr(rev, "_bar", None)
                    stripe = getattr(rev, "_stripe", None)
                    note("bar_visible_late", bar.isVisible() if bar else None)
                    note("bar_hidden_late", bar.isHidden() if bar else None)
                    note("stripe_visible_late",
                         stripe.isVisible() if stripe else None)
                    note("editor_visible", editor.isVisible())
                    note("viewport_visible", editor.viewport().isVisible())
                    note("viewport_size", "%dx%d" % (
                        editor.viewport().width(), editor.viewport().height()))
                editor.grab().save(SHOT.replace("probe-v089", "probe-v089-editor"))
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
        QTimer.singleShot(4000, lambda: do_probe(self))
        return out

    mw.MainWindow.showMaximized = patched

    orig_init = mw.MainWindow.__init__

    def patched_init(self, *a, **kw):
        orig_init(self, *a, **kw)
        log("constructed")
        QTimer.singleShot(14000, lambda: do_probe(self))
        QTimer.singleShot(26000, lambda: do_probe(self))

    mw.MainWindow.__init__ = patched_init

    sys.argv = [sys.argv[0], "--new-instance"]
    log("starting spyder")
    from spyder.app import start
    start.main()
    log("spyder exited")


if __name__ == "__main__":
    main_entry()
