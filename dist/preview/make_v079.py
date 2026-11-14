# -*- coding: utf-8 -*-
"""v0.7.9 预览：编辑器内联 diff 复核（幽灵删除行 + 绿底新增 + 按钮条）。

不启动 Spyder：QPlainTextEdit + InlineReview 降级渲染路径
（ExtraSelections 代替 TextDecoration），离屏 grab 出图。
"""
import os
import sys
import tempfile

ROOT = r"C:\Users\Mr_Yang\Documents\deepseek-harness\default-workspace\spyder-copilot-chat"
sys.path.insert(0, ROOT)
OUT = os.path.join(ROOT, "dist", "preview")

from qtpy.QtGui import QFont
from qtpy.QtWidgets import QApplication, QPlainTextEdit, QVBoxLayout, QWidget

from spyder_copilot_chat.widgets import theme
from spyder_copilot_chat.widgets.editor_diff import InlineReview

APP = QApplication.instance() or QApplication(sys.argv)

OLD = """import math


def total(items, tax):
    s = 0
    for it in items:
        s = s + it.price
    return s * (1 + tax)


def clamp(v, lo, hi):
    return max(lo, min(hi, v))
"""

NEW = '''import math
from dataclasses import dataclass


def total(items, tax=0.0):
    """含税总价。"""
    s = 0.0
    for it in items:
        s += it.price * it.qty
    return s * (1 + tax)


def clamp(v, lo, hi):
    if v < lo:
        return lo
    if v > hi:
        return hi
    return v


@dataclass
class Item:
    price: float
    qty: int = 1
'''


def make_editor(t):
    editor = QPlainTextEdit()
    editor.setFont(QFont("Consolas", 10))
    editor.setStyleSheet(
        "QPlainTextEdit {{ background: {bg}; color: {fg};"
        " border: 1px solid {bd};"
        " selection-background-color: {sel}; }}".format(
            bg=t["code_bg"], fg=t["text"], bd=t["border_strong"],
            sel=t["selection"]))
    return editor


def build(theme_name, filename):
    t = theme.resolve_theme(theme_name)
    theme.set_current_theme(t)
    tmp = tempfile.mkdtemp()
    path = os.path.join(tmp, "cart.py")
    with open(path, "w", encoding="utf-8") as f:
        f.write(NEW)

    root = QWidget()
    root.resize(680, 470)
    lay = QVBoxLayout(root)
    lay.setContentsMargins(0, 0, 0, 0)
    editor = make_editor(t)
    editor.setPlainText(NEW)      # 模拟"写盘后编辑器还停在旧视图"
    editor.document().setModified(False)
    lay.addWidget(editor)

    review = InlineReview(editor, path, OLD)
    assert review.stage(), "stage failed"
    # 悬停态：让 ✓ / ✗ 按钮出现在第二处修改上
    hunks = review.hunks()
    if len(hunks) > 1:
        review._hover_hunk = hunks[1]
        review._place_hunk_buttons()
    review.set_global_count(1)
    review._sync_geometry()
    return root, review


def main():
    for name, tag in (("dark", "dark"), ("light", "light")):
        root, review = build(tag, None)
        root.show()                  # 崩溃已修（fillRect 重载），可正常走 show
        APP.processEvents()
        APP.processEvents()
        review._sync_geometry()
        APP.processEvents()
        out = os.path.join(OUT, "v079-{0}-inline-diff.png".format(tag))
        root.grab().save(out)
        print("saved", out)
        root.close()
        APP.processEvents()
    print("done")


if __name__ == "__main__":
    main()
