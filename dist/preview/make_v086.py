# -*- coding: utf-8 -*-
"""v0.8.6：过程面板的三种时钟状态实拍。

同一段过程连续拍三张：
  1) 正在处理（走表）
  2) 等待批准（表停，文案改口径）
  3) 已完成（收成一行，秒数定住）

必须用 windows 平台插件：offscreen 没有字体库，中文全渲染成色块。
"""
import os
import sys
import time

ROOT = (r"C:\Users\Mr_Yang\Documents\deepseek-harness"
        r"\default-workspace\spyder-copilot-chat")
sys.path.insert(0, ROOT)
os.environ.pop("QT_QPA_PLATFORM", None)
OUT = os.path.join(ROOT, "docs", "images")

from qtpy.QtTest import QTest                 # noqa: E402
from qtpy.QtWidgets import QApplication        # noqa: E402

from spyder_copilot_chat.widgets import theme  # noqa: E402
from spyder_copilot_chat.widgets.chat_widget import CopilotChatPanel  # noqa: E402

APP = QApplication.instance() or QApplication(sys.argv)

ASK = "先确认这个目录里到底有几个关卡定义，再动手改。"
STEPS = [
    "▸ **list_files** `我的开发/test/01 坦克大战`",
    "✓ **list_files** → 6 个文件，关卡定义在 `levels.py`",
    "▸ **read_file** `levels.py`",
    "✓ **read_file** → 118 行，第 42–67 行是重复的构建循环",
]
TAIL = "看到了：同一段 `build_level(spec)` 循环被抄了三遍，直接抽成一个函数就行。"


def build(palette=None):
    panel = CopilotChatPanel()
    if palette is not None:
        cfg = dict(panel._config)
        cfg["ui_theme"] = "light" if palette == theme.LIGHT else "dark"
        panel._config = cfg
        panel._apply_theme()
    panel.resize(470, 620)
    panel.show()
    APP.processEvents()
    panel._add_msg("user", "把那段重复的 for 循环删掉。")
    b = panel._add_msg("assistant", "")
    panel._current_bubble = b
    b.append(ASK)
    b.begin_process()
    for step in STEPS:
        b.add_process(step)
    QTest.qWait(120)
    return panel, b


def shoot(name, panel):
    APP.processEvents()
    panel.grab().save(os.path.join(OUT, name))
    print("%-30s %dx%d" % (name, panel.width(), panel.height()))


# ── 1) 走表 ──────────────────────────────────────────────────
panel, b = build(theme.LIGHT)
# 让秒数看起来像真跑过一阵：把累计值垫到 82 秒
b._process._accum = 82.0
b._process._run_since = time.monotonic()
b._process._refresh_label()
QTest.qWait(1200)
shoot("process-running-light.png", panel)
print("    label:", b._process._btn.text())

# ── 2) 等批准：表停 ─────────────────────────────────────────
b.pause_process()
QTest.qWait(400)
shoot("process-waiting-light.png", panel)
print("    label:", b._process._btn.text())

# ── 3) 收尾：定住 ───────────────────────────────────────────
b.resume_process()
b.append(TAIL)
b.finish_process()
QTest.qWait(300)
panel._inner.request_layout() if hasattr(panel._inner, "request_layout") else None
QTest.qWait(200)
panel.grab().save(os.path.join(OUT, "process-finished-light.png"))
print("%-30s %dx%d" % ("process-finished-light.png",
                       panel.width(), panel.height()))
print("    label:", b._process._btn.text())

panel.close()
print("done")
