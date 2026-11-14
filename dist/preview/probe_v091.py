# -*- coding: utf-8 -*-
"""v0.8.11 探针：真机复刻用户那次 linting 崩溃。

用户栈（Spyder 6.1.8）：

    lsp_mixin.py:639  data._selection()
    editor_diff.py:157 __getattr__ -> AttributeError: _selection
    → manage_lsp_handle_errors → LSPHandleError: Error when processing linting

这里不模拟：直接在真 Spyder 的 CodeEditor 上塞 _diagnostics，然后调
editor.finish_code_analysis() —— 它就是用户栈里那三层的真实入口，会走
underline_errors() → _process_code_analysis(underline=True) → data._selection()，
异常会被 manage_lsp_handle_errors 包成 LSPHandleError 抛出来。

跑的是装进 user site 的 wheel，不往 sys.path 插仓库路径。
"""
import json
import os
import sys
import time
import traceback

OUT = os.path.dirname(os.path.abspath(__file__))
RESULT = os.path.join(OUT, "probe_v091.json")
SHOT = os.path.join(OUT, "probe-v091.png")
TMPDIR = os.path.join(os.environ.get("TEMP", OUT), "diff_probe_v091")
os.makedirs(TMPDIR, exist_ok=True)

NAMES = ["lint_a.py", "lint_b.py"]
FILES = [os.path.join(TMPDIR, n) for n in NAMES]

OLD_TEXT = "\n".join(
    ["# lint probe"] + ["x = %d" % i for i in range(1, 24)] + [
        "def draw_player(surface):",
        "    pos = surface.get_rect()",
        "    return pos",
    ] + ["y = %d" % i for i in range(1, 12)] + [""])

# 新内容故意留一个 pyflakes 会报的东西：未使用的 import + 未定义名，
# 这样真 LSP 也会在这些行上产生 diagnostic
NEW_TEXT = "\n".join(
    ["# lint probe", "import os"] + ["x = %d" % i for i in range(1, 24)] + [
        "def draw_player(surface):",
        "    pos = surface.get_rect()",
        "    return pos + missing_name",
    ] + ["y = %d" % i for i in range(1, 12)] + [""])

state = {"done": False}


def read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read().replace("\r\n", "\n")


def make_diagnostics(n_lines, lines):
    """造几条 diagnostic，覆盖 diff 里的红绿行。"""
    diags = []
    for ln in lines:
        ln = max(0, min(int(ln), n_lines - 1))
        diags.append({
            "range": {
                "start": {"line": ln, "character": 0},
                "end": {"line": ln, "character": 4},
            },
            "message": "probe diagnostic on line %d" % (ln + 1),
            "severity": 2,
            "code": "E999",
            "source": "probe",
        })
    return diags


def do_probe(main):
    if state["done"]:
        return
    from qtpy.QtWidgets import QApplication
    from qtpy.QtCore import QTimer
    from qtpy.QtGui import QTextCursor
    from spyder.api.plugins import Plugins

    result = {"ok": False, "steps": []}

    def note(key, value):
        result[key] = value
        result["steps"].append("%s=%r" % (key, value))

    def finish():
        state["done"] = True
        with open(RESULT, "w", encoding="utf-8") as fh:
            json.dump(result, fh, ensure_ascii=False, indent=2)
        QApplication.instance().quit()

    app = QApplication.instance()

    def wait(ms):
        end = time.time() + ms / 1000.0
        while time.time() < end:
            app.processEvents()
            time.sleep(0.01)

    try:
        import spyder_copilot_chat as pkg
        from spyder_copilot_chat.plugin import CopilotChatPlugin
        from spyder_copilot_chat.widgets import editor_diff as ED

        note("plugin_version", pkg.__version__)
        note("plugin_path", os.path.dirname(pkg.__file__))
        note("spyder_blockuserdata",
             getattr(ED._SpyderBlockUserData, "__module__", None))
        note("mark_bases",
             [c.__name__ for c in ED.DiffBlockMark.__mro__])

        plugin = None
        for key in (getattr(CopilotChatPlugin, "NAME", "copilot_chat"),
                    "copilot_chat"):
            try:
                plugin = main.get_plugin(key)
            except Exception:
                plugin = None
            if plugin is not None:
                break
        if plugin is None:
            result["traceback"] = "插件未加载"
            return finish()

        editor_plugin = main.get_plugin(Plugins.Editor)
        stack = editor_plugin.get_current_editorstack()
        editors = {}
        for path in FILES:
            with open(path, "w", encoding="utf-8", newline="") as fh:
                fh.write(OLD_TEXT)
            ed = stack.load(path, set_current=False)
            if ed is not None and not hasattr(ed, "document"):
                ed = getattr(ed, "editor", None)
            editors[path] = ed
            with open(path, "w", encoding="utf-8", newline="") as fh:
                fh.write(NEW_TEXT)

        res = plugin.review_files([(p, OLD_TEXT) for p in FILES])
        note("review_result", res)
        wait(600)

        hub = getattr(plugin, "_review_hub", None)
        reviews = list(hub.reviews()) if hub is not None else []
        note("active_reviews", len(reviews))

        # ── 核心：真编辑器 + 真 diagnostics + 真 finish_code_analysis ──
        lint = []
        for rev in reviews:
            ed = rev.editor
            name = os.path.basename(rev.path)
            info = {"file": name}
            doc = ed.document()

            marked = []
            block = doc.firstBlock()
            while block.isValid():
                data = block.userData()
                if isinstance(data, ED.DiffBlockMark):
                    marked.append((block.blockNumber(), data))
                block = block.next()
            info["marked_blocks"] = len(marked)
            if marked:
                info["mark_type_is_spyder_bud"] = (
                    type(marked[0][1]).__mro__[1].__name__)
                info["mark_editor_is_editor"] = marked[0][1].editor is ed

            # 1) 直接问 _selection（用户栈里挂掉的那一句）
            sel_ok = True
            sel_err = None
            try:
                for num, data in marked[:3]:
                    data.selection_start = {"line": num, "character": 0}
                    data.selection_end = {"line": num, "character": 3}
                    sel = data._selection()
                    if not isinstance(sel, QTextCursor):
                        sel_ok = False
                        sel_err = "返回 %r" % (type(sel),)
            except Exception as exc:          # noqa: BLE001
                sel_ok = False
                sel_err = "%s: %s" % (type(exc).__name__, exc)
            info["selection_ok"] = sel_ok
            info["selection_error"] = sel_err

            # 2) 真正走一遍 Spyder 的 finish_code_analysis
            lines = [num for num, _d in marked]
            if not lines:
                lines = [3, 5, 8]
            ed._diagnostics = make_diagnostics(doc.blockCount(), lines)
            info["diagnostics"] = len(ed._diagnostics)
            info["underline_enabled"] = bool(
                getattr(ed, "underline_errors_enabled", True))
            if not getattr(ed, "underline_errors_enabled", True):
                ed.underline_errors_enabled = True

            boom = None
            try:
                ed.finish_code_analysis()     # ← 用户栈的入口
            except Exception as exc:          # noqa: BLE001
                boom = "%s: %s" % (type(exc).__name__, exc)
            info["finish_code_analysis_error"] = boom
            # LSPHandleError 是我们最关心的那个名字
            info["lsp_handle_error"] = bool(
                boom and "LSPHandleError" in boom)

            # 3) 另一半（线程里跑的那半）：往 data.code_analysis 里塞东西
            boom2 = None
            try:
                ed._process_code_analysis(underline=False)
            except Exception as exc:          # noqa: BLE001
                boom2 = "%s: %s" % (type(exc).__name__, exc)
            info["process_no_underline_error"] = boom2

            # 4) 走完 linting，hunk 结构不能坏（isinstance 还得认）
            try:
                info["hunks_after_lint"] = len(rev.hunks())
            except Exception as exc:          # noqa: BLE001
                info["hunks_after_lint"] = "ERR %r" % (exc,)

            # 5) 面板侧消费点
            panel_err = None
            try:
                ed.blockuserdata_list()
                ed.outlineexplorer_data_list()
                ed.get_current_warnings()
            except Exception as exc:          # noqa: BLE001
                panel_err = "%s: %s" % (type(exc).__name__, exc)
            info["panel_error"] = panel_err

            # 6) 下划线真的画上去了吗
            try:
                sels = ed.get_extra_selections("code_analysis_underline")
                info["underline_selections"] = len(sels)
            except Exception as exc:          # noqa: BLE001
                info["underline_selections"] = "ERR %r" % (exc,)

            lint.append(info)

        note("lint", lint)
        wait(400)
        try:
            main.grab().save(SHOT)
            note("screenshot", SHOT)
        except Exception as exc:              # noqa: BLE001
            note("screenshot_error", repr(exc))

        clean = [i for i in lint if not i.get("finish_code_analysis_error")
                 and not i.get("selection_error")
                 and not i.get("panel_error")
                 and not i.get("process_no_underline_error")]
        result["ok"] = (bool(res.get("staged")) and len(lint) == len(FILES)
                        and len(clean) == len(lint))
        note("clean_editors", len(clean))

        # ── 收尾 1：接受全部（它是全局的，点一次两个文件都落盘） ──
        if reviews:
            bar = getattr(reviews[0], "_bar", None)
            if bar is not None:
                bar._accept_all.click()
                wait(300)
        note("disk_all_new_after_accept",
             [read(p) == NEW_TEXT for p in FILES])
        note("reviews_left",
             len(hub.reviews()) if hub is not None else -1)

        # ── 收尾 2：重新 stage 一轮，这次点「不接受本次修改」 ──
        for path in FILES:
            with open(path, "w", encoding="utf-8", newline="") as fh:
                fh.write(OLD_TEXT)
            stack.load(path, set_current=False)
            with open(path, "w", encoding="utf-8", newline="") as fh:
                fh.write(NEW_TEXT)
        res2 = plugin.review_files([(p, OLD_TEXT) for p in FILES])
        note("review_result_round2", res2)
        wait(400)
        revs2 = list(hub.reviews()) if hub is not None else []
        note("round2_reviews", len(revs2))
        if revs2:
            bar = getattr(revs2[0], "_bar", None)
            if bar is not None:
                bar._reject_all.click()
                wait(300)
        note("disk_all_old_after_reject",
             [read(p) == OLD_TEXT for p in FILES])
        note("reviews_final",
             len(hub.reviews()) if hub is not None else -1)

        QTimer.singleShot(500, finish)
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
        QTimer.singleShot(28000, lambda: do_probe(self))

    mw.MainWindow.__init__ = patched_init

    sys.argv = [sys.argv[0], "--new-instance"]
    log("starting spyder")
    from spyder.app import start
    start.main()
    log("spyder exited")


if __name__ == "__main__":
    main_entry()
