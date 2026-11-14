# -*- coding: utf-8 -*-
"""出图：v0.8.10 的顶部工具条（文件计数 + 两个总开关）。

必须用默认 Qt 平台：offscreen 下 Qt 拿不到系统字体，截出来全是色条。
"""
import os
import sys
import tempfile

ROOT = (r"C:\Users\Mr_Yang\Documents\deepseek-harness"
        r"\default-workspace\spyder-copilot-chat")
sys.path.insert(0, ROOT)

from qtpy.QtCore import QObject                                # noqa: E402
from qtpy.QtGui import QColor, QFont, QPalette, QTextOption    # noqa: E402
from qtpy.QtWidgets import (                                   # noqa: E402
    QApplication, QPlainTextEdit, QWidget, QVBoxLayout,
)

APP = QApplication.instance() or QApplication([])

from spyder_copilot_chat.plugin import CopilotChatPlugin       # noqa: E402
from spyder_copilot_chat.widgets import theme                  # noqa: E402

theme.set_current_theme(theme.resolve_theme("dark", "spyder",
                                            "comfortable"))

OUT = os.path.join(ROOT, "docs", "images")
os.makedirs(OUT, exist_ok=True)

OLD = '''import numpy as np
import matplotlib.pyplot as plt

AMPLITUDE = 1.0
FREQUENCY = 1.0
PHASE = 0.0
N_POINTS = 1000


def main():
    x = np.linspace(0, 2 * np.pi, N_POINTS)
    y = AMPLITUDE * np.sin(2 * np.pi * FREQUENCY * x + PHASE)

    fig, ax = plt.subplots(figsize=(8, 4.5), dpi=120)
    ax.plot(x, y, color='tab:blue', linewidth=2, label='y=sin(x)')
    ax.set_title('Title')
    plt.show()
'''

NEW = '''import numpy as np
import matplotlib.pyplot as plt

# ─── 参数设置 ───
AMPLITUDE = 1.0        # 振幅 A
FREQUENCY = 1.0        # 频率 f（周期数 / 单位长度）
PHASE = 0.0            # 初相 phi（弧度）
N_POINTS = 1000        # 采样点数


def main():
    x = np.linspace(0, 2 * np.pi, N_POINTS)
    y = AMPLITUDE * np.sin(2 * np.pi * FREQUENCY * x + PHASE)

    fig, ax = plt.subplots(figsize=(8, 4.5), dpi=120)
    ax.plot(x, y, color='tab:blue', linewidth=2, label=r'$y=\\sin(x)$')

    # 坐标轴与参考线
    ax.axhline(0, color='black', linewidth=0.8)
    ax.axvline(0, color='black', linewidth=0.8)
    ax.axhline(AMPLITUDE, color='gray', linestyle='--', linewidth=0.8,
               alpha=0.7)
    ax.axhline(-AMPLITUDE, color='gray', linestyle='--', linewidth=0.8,
               alpha=0.7)

    ax.set_title('正弦函数图像')
    plt.show()
'''


class FakeFinfo(object):
    def __init__(self, editor, path):
        self.filename = path
        self.editor = editor
        self.newly_created = False


class FakeStack(object):
    def __init__(self, pairs):
        self.data = [FakeFinfo(e, p) for p, e in pairs]
        self.current = pairs[0][0]

    def load(self, path, set_current=False):
        return None

    def set_current_filename(self, path, focus=False):
        self.current = path
        return True

    def get_index_from_filename(self, path):
        return 0

    def save(self, index, force=False):
        return True


class FakeEP(object):
    def __init__(self, stack):
        self._stack = stack

    def get_current_editorstack(self):
        return self._stack


class FakePlugin(QObject):
    review_files = CopilotChatPlugin.review_files
    _save_editor_file = CopilotChatPlugin._save_editor_file
    _ensure_review_hub = CopilotChatPlugin._ensure_review_hub
    _editor_for_path = CopilotChatPlugin._editor_for_path

    def __init__(self, stack):
        super().__init__()
        self._review_hub = None
        self._ep = FakeEP(stack)

    def get_plugin(self, name):
        return self._ep

    def get_widget(self):
        return None


def make_editor():
    ed = QPlainTextEdit()
    ed.setFrameShape(QPlainTextEdit.NoFrame)
    font = QFont("Consolas", 10)
    font.setStyleHint(QFont.Monospace)
    ed.setFont(font)
    ed.setWordWrapMode(QTextOption.NoWrap)
    pal = ed.palette()
    pal.setColor(QPalette.Base, QColor("#19232d"))
    pal.setColor(QPalette.Text, QColor("#d7dde3"))
    ed.setPalette(pal)
    ed.setStyleSheet(
        "QPlainTextEdit { background: #19232d; color: #d7dde3;"
        " selection-background-color: #2a4a6b;"
        " border: none; padding: 6px 8px; }")
    return ed


def grab(editor, path, w=760):
    editor.resize(w, 470)
    editor.show()
    for _ in range(6):
        APP.processEvents()
    editor.grab().save(path)
    editor.hide()
    print("saved", path)


# ── 场景 A：三文件复核（工具条报 2/3 + 四个按钮） ──────────────
TMP = tempfile.mkdtemp(prefix="v090_img_")
paths = []
eds = []
for i in range(3):
    p = os.path.join(TMP, ["test_sine_plot.py", "tank_battle.py",
                           "plot_utils.py"][i])
    with open(p, "w", encoding="utf-8", newline="") as fh:
        fh.write(NEW)
    paths.append(p)
    eds.append(make_editor())

stack = FakeStack(list(zip(paths, eds)))
plugin = FakePlugin(stack)
res = plugin.review_files([(p, OLD) for p in paths])
print("stage:", res)

revs = plugin._review_hub.reviews()
target = revs[1] if len(revs) > 1 else revs[0]
if target._bar is not None:
    target._bar.show()
    target._bar.refresh()
for _ in range(6):
    APP.processEvents()
grab(target.editor, os.path.join(OUT, "inline-diff-bar-files-dark.png"))
# 窄编辑区（对应用户截图里的 ~515px）：标签该截就截，按钮一个不能少
target._bar.show()
target._bar.refresh()
for _ in range(6):
    APP.processEvents()
grab(target.editor, os.path.join(OUT, "inline-diff-bar-narrow-dark.png"),
     w=515)

# ── 场景 B：单文件（只有两个总开关） ───────────────────────────
for r in list(plugin._review_hub.reviews()):
    r.clear()
TMP2 = tempfile.mkdtemp(prefix="v090_img2_")
p2 = os.path.join(TMP2, "test_sine_plot.py")
with open(p2, "w", encoding="utf-8", newline="") as fh:
    fh.write(NEW)
ed2 = make_editor()
stack2 = FakeStack([(p2, ed2)])
plugin2 = FakePlugin(stack2)
print("stage2:", plugin2.review_files([(p2, OLD)]))
rev2 = plugin2._review_hub.reviews()[0]
if rev2._bar is not None:
    rev2._bar.show()
    rev2._bar.refresh()
for _ in range(6):
    APP.processEvents()
grab(rev2.editor, os.path.join(OUT, "inline-diff-bar-single-dark.png"))

