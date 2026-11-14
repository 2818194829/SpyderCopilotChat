# -*- coding: utf-8 -*-
"""v0.8.5：短视图下的审批界面实拍（快捷条 + 卡片自动压缩）。

刻意把面板压到 470x620——用户侧栏就是这个高度，卡片的「批准 / 拒绝」
在这个尺寸下会被 200 行 diff 顶到折叠线以下，这是这张图要证明的事。

必须用 windows 平台插件：offscreen 没有字体库，中文全渲染成色块。
"""
import json
import os
import sys

ROOT = (r"C:\Users\Mr_Yang\Documents\deepseek-harness"
        r"\default-workspace\spyder-copilot-chat")
sys.path.insert(0, ROOT)
os.environ.pop("QT_QPA_PLATFORM", None)
OUT = os.path.join(ROOT, "docs", "images")
TMP = os.path.join(ROOT, "dist", "preview", "tmp_v085")

from qtpy.QtTest import QTest                 # noqa: E402
from qtpy.QtWidgets import QApplication        # noqa: E402

from spyder_copilot_chat.widgets import theme  # noqa: E402
from spyder_copilot_chat.widgets.approval_card import ApprovalCard  # noqa: E402
from spyder_copilot_chat.widgets.chat_widget import CopilotChatPanel  # noqa: E402

APP = QApplication.instance() or QApplication(sys.argv)

os.makedirs(TMP, exist_ok=True)
LONG_DIR = os.path.join(TMP, "我的开发", "test", "01 坦克大战")
os.makedirs(LONG_DIR, exist_ok=True)
LONG_PATH = os.path.join(LONG_DIR, "_check_levels.py")
with open(LONG_PATH, "w", encoding="utf-8") as f:
    f.write("\n".join(
        "    lv = m.Level(spec)   # %03d" % i for i in range(1, 210)) + "\n")


def call(name, **args):
    return {"function": {"name": name,
                         "arguments": json.dumps(args, ensure_ascii=False)}}


SAFE = call("edit_file", path=LONG_PATH, start_line=8, end_line=20,
            new_content="    for spec in m.LEVELS:\n"
                        "        build_level(spec)\n")
DANGER = call("delete_file", path=LONG_PATH)


def build(palette=None, size=(470, 620)):
    panel = CopilotChatPanel()
    if palette is not None:
        cfg = dict(panel._config)
        cfg["ui_theme"] = "light" if palette == theme.LIGHT else "dark"
        panel._config = cfg
        panel._apply_theme()
    panel.resize(*size)
    panel.show()
    APP.processEvents()

    panel._add_msg("user", "把 `_check_levels.py` 里那段重复的 for 循环删掉。")
    bubble = panel._add_msg("assistant", "")
    bubble.begin_process()
    bubble.add_process("▸ **read_file** `_check_levels.py`")
    bubble.add_process("**✓ read_file** → 读到 209 行，第 8-20 行是重复构建逻辑")
    bubble.append("看清楚了：这段循环把 `m.LEVELS` 又遍历了一遍。要改的行已经标出来，"
                  "确认后我直接写。")
    bubble.finish_process()
    APP.processEvents()

    card = ApprovalCard([SAFE, DANGER], TMP)
    panel._inner_lay.insertWidget(panel._inner_lay.count() - 1, card)
    panel._pending_card = card
    card.changed.connect(panel._refresh_approval_bar)
    panel._set_awaiting_approval(True)
    # 布局要几轮事件循环才稳定：适配高度和滚动都放在稳定之后
    QTest.qWait(260)
    panel._settle_approval_card()
    QTest.qWait(80)
    return panel, card


def shoot(name, palette=None, size=(470, 620)):
    panel, card = build(palette, size)
    panel._settle_approval_card()
    QTest.qWait(60)
    path = os.path.join(OUT, name)
    panel.grab().save(path)
    pending, danger = card.pending_stats()
    print("%-28s %dx%d  pending=%d danger=%d  bar=%s"
          % (name, panel.width(), panel.height(), pending, danger,
             not panel._approval_bar.isHidden()))
    panel.close()
    panel.deleteLater()


# 短面板：卡片装不下，靠快捷条接住操作
shoot("approval-bar-dark.png", size=(470, 620))
shoot("approval-bar-light.png", palette=theme.LIGHT, size=(470, 620))
# 高面板：卡片自己就装得下，两条操作 + 底部批量按钮全在视野内
shoot("approval-card-fits-dark.png", size=(470, 900))
shoot("approval-card-fits-light.png", palette=theme.LIGHT, size=(470, 900))
print("done")
