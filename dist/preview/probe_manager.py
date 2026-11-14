# -*- coding: utf-8 -*-
"""用**真实的** TextDecorationsManager 验证内联 diff 的颜色链路。

CodeEditor 在离屏环境里会直接崩进程（QSyntaxHighlighter 依赖），所以
这里换一个 PlainTextEdit 子类，但补上 manager 需要的那两个方法，
并把真正的 spyder TextDecorationsManager 挂上去。装饰过滤规则因此
与现场完全一致：first..last 可见范围之外的一律丢弃。
"""
import json
import os
import sys
import traceback

os.environ["QT_QPA_PLATFORM"] = "offscreen"

from qtpy.QtWidgets import QApplication, QPlainTextEdit   # noqa: E402
from qtpy.QtGui import QTextCursor                        # noqa: E402

APP = QApplication([])

from spyder.plugins.editor.utils.decoration import (      # noqa: E402
    TextDecorationsManager)

from spyder_copilot_chat.widgets import theme             # noqa: E402
from spyder_copilot_chat.widgets.editor_diff import InlineReview  # noqa

OUT = os.path.dirname(os.path.abspath(__file__))
PATH = os.path.join(os.environ.get("TEMP", OUT), "manager_probe.py")

OLD = "\n".join(["# head"] + ["pass"] * 60 + [
    "def target():",
    "    print('old')",
    "    return 1",
] + ["pass"] * 40 + [""])
NEW = "\n".join(["# head"] + ["pass"] * 60 + [
    "def target():",
    "    print('new')",
    "    print('extra')",
    "    return 2",
] + ["pass"] * 40 + [""])


class FakeEditor(QPlainTextEdit):
    """只有 CodeEditor 那一处 manager 依赖的行为。"""

    def __init__(self):
        super().__init__()
        self.decorations = TextDecorationsManager(self)
        self._visible = (0, 10 ** 6)
        self.calls = 0

    def get_buffer_block_numbers(self):
        return self._visible

    def set_text(self, text):
        self.setPlainText(text)


result = {"steps": []}


def note(k, v):
    result[k] = v
    result["steps"].append("%s=%r" % (k, v))


def main():
    theme.set_current_theme(theme.resolve_theme("dark"))
    with open(PATH, "w", encoding="utf-8") as fh:
        fh.write(OLD)

    ed = FakeEditor()
    ed.resize(760, 420)
    ed.set_text(OLD)
    ed.show()
    APP.processEvents()
    note("doc_modified", ed.document().isModified())

    with open(PATH, "w", encoding="utf-8") as fh:
        fh.write(NEW)

    rev = InlineReview(ed, PATH, OLD, saver=lambda *a: True)
    note("stage_ok", rev.stage())
    note("fail_reason", rev.fail_reason)
    note("hunks", len(rev.hunks()))
    note("deco_count", rev.deco_count)
    note("fallback", rev.fallback_decorations)
    note("cursor_block_after_stage", ed.textCursor().blockNumber())

    # 视图滚到的是第一处修改 → 可见范围应包含它
    note("visible_range", ed.get_buffer_block_numbers())
    note("stored", len(ed.decorations.get("copilot_chat_inline_diff") or []))

    for _ in range(6):
        APP.processEvents()
    sels = ed.extraSelections()
    note("extra_selections", len(sels))
    detail = []
    for s in sels[:12]:
        try:
            detail.append("blk=%s bg=%s strike=%s" % (
                s.cursor.blockNumber(),
                s.format.background().color().name(),
                s.format.fontStrikeOut()))
        except Exception as exc:
            detail.append("err %s" % exc)
    note("extra_detail", detail)

    # 关键实验：把可见范围人为收窄到文件头（模拟"视图停在第一行"），
    # 看 manager 会不会把改动处的装饰全部丢掉。
    ed._visible = (0, 20)
    ed.decorations._update()
    note("extra_after_narrow", len(ed.extraSelections()))
    ed._visible = (0, 10 ** 6)
    ed.decorations._update()
    note("extra_after_restore", len(ed.extraSelections()))

    shot = os.path.join(OUT, "probe-manager.png")
    ed.viewport().grab().save(shot)
    note("screenshot", shot)


try:
    main()
    result["ok"] = True
except Exception:
    result["traceback"] = traceback.format_exc()

with open(os.path.join(OUT, "probe_manager.json"), "w", encoding="utf-8") as fh:
    json.dump(result, fh, ensure_ascii=False, indent=2)
print(json.dumps(result, ensure_ascii=False, indent=2))
