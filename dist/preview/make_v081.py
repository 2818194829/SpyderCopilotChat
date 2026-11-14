# -*- coding: utf-8 -*-
"""v0.8.1 预览：Agent 改完文件后，对话流里的红绿 diff 卡片。

走真实链路：``MessageBubble.append_block("```diff ...```")`` →
``render_markdown`` → ``diff_view.render_rows``。离屏 grab 出图。
"""
import os
import sys

ROOT = r"C:\Users\Mr_Yang\Documents\deepseek-harness\default-workspace\spyder-copilot-chat"
sys.path.insert(0, ROOT)
OUT = os.path.join(ROOT, "dist", "preview")

from qtpy.QtWidgets import QApplication, QVBoxLayout, QWidget

from spyder_copilot_chat.widgets import theme
from spyder_copilot_chat.widgets import diff_view as _diff
from spyder_copilot_chat.widgets.chat_widget import MessageBubble

APP = QApplication.instance() or QApplication(sys.argv)

OLD = '''def total(items, tax):
    s = 0
    for it in items:
        s = s + it.price
    return s * (1 + tax)


def clamp(v, lo, hi):
    return max(lo, min(hi, v))
'''

NEW = '''def total(items, tax=0.0):
    s = 0.0
    for it in items:
        s += it.price * it.qty
    return s * (1 + tax)


def clamp(v, lo, hi):
    if v < lo or v > hi:
        raise ValueError("out of range")
    return v
'''


def card(name, old, new):
    patch = _diff.unified(old, new, fromfile=name, tofile=name, context=3)
    return "**📄 `%s`**\n\n```diff\n%s\n```" % (name, patch)


def build(tag):
    t = theme.resolve_theme(tag)
    theme.set_current_theme(t)

    root = QWidget()
    root.setStyleSheet("background:%s;" % t["bg"])
    root.resize(700, 760)
    lay = QVBoxLayout(root)
    lay.setContentsMargins(10, 10, 10, 10)

    bubble = MessageBubble("assistant", "")
    lay.addWidget(bubble)
    lay.addStretch(1)
    return root, bubble


def fill(bubble):
    """内容必须在 show 之后再灌：气泡按自身宽度排版，宽度还没下发时
    ``_adjust`` 会退到 180px，一条消息能被撑到 3000 多像素高。"""
    bubble.append("两处逻辑都改完了：`total()` 补上 qty，`clamp()` 改成抛异常。")
    bubble.append_block(
        "🔧 **write_to_file**\n\n```json\n"
        '{"path": "cart.py", "content": "..."}\n```')
    bubble.append_block(card("cart.py", OLD, NEW))


def main():
    for tag in ("dark", "light"):
        root, bubble = build(tag)
        root.show()
        APP.processEvents()
        fill(bubble)
        APP.processEvents()
        APP.processEvents()
        bubble._adjust()
        APP.processEvents()
        out = os.path.join(OUT, "v081-%s-chat-diff.png" % tag)
        root.grab().save(out)
        print("saved", out, root.size())
        root.close()
        APP.processEvents()
    print("done")


if __name__ == "__main__":
    main()
