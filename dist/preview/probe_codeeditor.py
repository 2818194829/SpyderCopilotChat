# -*- coding: utf-8 -*-
"""用真实的 Spyder CodeEditor 跑一遍 InlineReview.stage()。

比离屏假控件更接近现场：装饰走真正的 TextDecorationsManager，
文档是 CodeEditor 的 QTextDocument，工具栏挂在真实 viewport 上。
"""
import json
import os
import traceback

os.environ["QT_QPA_PLATFORM"] = "offscreen"

from qtpy.QtWidgets import QApplication          # noqa: E402
from qtpy.QtCore import Qt                       # noqa: E402

APP = QApplication([])

from spyder.plugins.editor.widgets.codeeditor.codeeditor import CodeEditor  # noqa: E402

from spyder_copilot_chat.widgets import theme          # noqa: E402
from spyder_copilot_chat.widgets.editor_diff import InlineReview  # noqa: E402

OUT = os.path.dirname(os.path.abspath(__file__))
PATH = os.path.join(os.environ.get("TEMP", OUT), "inline_probe.py")

OLD = "\n".join([
    "# probe",
    "def draw_player():",
    "    print('old')",
    "    return 1",
    "",
    "def other():",
    "    return 42",
    "",
])
NEW = "\n".join([
    "# probe",
    "def draw_player():",
    "    print('new')",
    "    print('extra')",
    "    return 2",
    "",
    "def other():",
    "    return 42",
    "",
])

result = {"steps": []}


def note(k, v):
    result[k] = v
    result["steps"].append("%s=%r" % (k, v))


def main():
    theme.set_current_theme(theme.resolve_theme("dark"))
    with open(PATH, "w", encoding="utf-8") as fh:
        fh.write(OLD)

    ed = CodeEditor()
    ed.resize(760, 420)
    ed.setAttribute(Qt.WA_DontShowOnScreen, True)
    ed.set_text(OLD)
    ed.show()
    for _ in range(4):
        APP.processEvents()
    note("doc_modified_after_set_text", ed.document().isModified())
    note("blocks", ed.document().blockCount())

    with open(PATH, "w", encoding="utf-8") as fh:
        fh.write(NEW)

    review = InlineReview(ed, PATH, OLD, saver=lambda *a: True)
    ok = review.stage()
    note("stage_ok", ok)
    note("fail_reason", review.fail_reason)
    for _ in range(6):
        APP.processEvents()
    note("deco_count", review.deco_count)
    note("fallback", review.fallback_decorations)
    note("doc_head", ed.toPlainText()[:160].replace("\n", " | "))
    note("blocks_after", ed.document().blockCount())
    note("hunks", len(review.hunks()))
    note("counts", review.counts())

    note("hasattr_decorations", hasattr(ed, "decorations"))
    if hasattr(ed, "decorations"):
        stored = ed.decorations.get("copilot_chat_inline_diff")
        note("stored", len(stored) if stored else 0)
    note("extra_selections", len(ed.extraSelections()))
    note("visible_range", ed.get_buffer_block_numbers())
    detail = []
    for sel in ed.extraSelections()[:10]:
        try:
            detail.append("bg=%s strike=%s blk=%s" % (
                sel.format.background().color().name(),
                sel.format.fontStrikeOut(),
                sel.cursor.blockNumber()))
        except Exception as exc:
            detail.append("err %s" % exc)
    note("extra_detail", detail)

    note("bar_hidden", review._bar.isHidden() if review._bar else None)
    note("stripe_hidden",
         review._stripe.isHidden() if review._stripe else None)
    note("bar_geometry", str(review._bar.geometry()) if review._bar else None)
    note("viewport", "%dx%d" % (ed.viewport().width(),
                                ed.viewport().height()))

    shot = os.path.join(OUT, "probe-codeeditor.png")
    ed.viewport().grab().save(shot)
    note("screenshot", shot)


try:
    main()
    result["ok"] = True
except Exception:
    result["traceback"] = traceback.format_exc()

with open(os.path.join(OUT, "probe_codeeditor.json"), "w",
          encoding="utf-8") as fh:
    json.dump(result, fh, ensure_ascii=False, indent=2)
print(json.dumps(result, ensure_ascii=False, indent=2))
