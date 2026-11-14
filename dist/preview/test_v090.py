# -*- coding: utf-8 -*-
"""v0.8.10 回归：顶部工具条要报「修了几个文件」，并给两个总开关。

用户原话：

    这里应该显示修了几个文件，并且新增"接受全部修改"和"不接受本次修改"
    的按钮

旧版工具条只有「+2 处修改」+ 接受/全部接受/撤销 三个按钮，三个问题：
  - 不报文件总数：多文件复核时切了几次 tab 就不知道还剩几个没决定；
  - 「全部接受」只在 global_count > 1 时出现，单文件时用户根本找不到；
  - 没有真正意义上的「全部拒绝」——「撤销」只作用于当前文件。

覆盖：
  1) 单文件：标签报「N 处修改」不加文件前缀；「本文件」按钮隐藏
  2) 多文件：标签报「i/N 文件」；「本文件」按钮出现
  3) 两个总开关的文字/可见性/连接
  4) 「不接受本次修改」→ 所有文件磁盘内容回到修改前、会话全部注销
  5) 「接受全部修改」→ 所有文件磁盘内容为修改后、幽灵行清空
  6) 窄视口：标签 elide，按钮一个都不许被压成省略号
"""
import os
import sys
import tempfile

ROOT = (r"C:\Users\Mr_Yang\Documents\deepseek-harness"
        r"\default-workspace\spyder-copilot-chat")
sys.path.insert(0, ROOT)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from qtpy.QtWidgets import QApplication, QPlainTextEdit       # noqa: E402
from qtpy.QtCore import QObject                               # noqa: E402

from spyder_copilot_chat.plugin import CopilotChatPlugin      # noqa: E402
from spyder_copilot_chat.widgets.editor_diff import (         # noqa: E402
    ReviewHub,
)

APP = QApplication.instance() or QApplication([])
FAILS = []
PASSES = []


def check(name, cond, extra=""):
    if cond:
        PASSES.append(name)
    else:
        FAILS.append("%s  %s" % (name, extra))


def pump(n=6):
    for _ in range(n):
        APP.processEvents()


# ── 脚手架 ──────────────────────────────────────────────────────
class FakeFinfo(object):
    def __init__(self, editor, path):
        self.filename = path
        self.editor = editor
        self.newly_created = False


class FakeStack(object):
    """真写盘的 EditorStack 替身：save() 把 editor 内容落到 filename。"""

    def __init__(self, pairs):
        self.data = [FakeFinfo(e, p) for p, e in pairs]
        self.current = pairs[0][0] if pairs else ""
        self.saved = []

    def load(self, path, set_current=False):
        return None

    def set_current_filename(self, path, focus=False):
        self.current = path
        return True

    def get_index_from_filename(self, path):
        target = os.path.normcase(os.path.abspath(path))
        for i, finfo in enumerate(self.data):
            if os.path.normcase(os.path.abspath(finfo.filename)) == target:
                return i
        raise ValueError(path)

    def save(self, index, force=False):
        finfo = self.data[index]
        text = finfo.editor.toPlainText()
        with open(finfo.filename, "w", encoding="utf-8", newline="") as fh:
            fh.write(text)
        finfo.editor.document().setModified(False)
        self.saved.append(index)
        return True


class FakeEditorPlugin(object):
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
        self._stack = stack
        self._ep = FakeEditorPlugin(stack)

    def get_plugin(self, name):
        return self._ep

    def get_widget(self):
        return None


OLD = ("import math\n"
       "\n"
       "\n"
       "def add(a, b):\n"
       "    return a + b\n"
       "\n"
       "\n"
       "def sub(a, b):\n"
       "    return a - b\n")
NEW = ("import math\n"
       "\n"
       "\n"
       "def add(a, b):\n"
       '    """两数相加。"""\n'
       "    return a + b\n"
       "\n"
       "\n"
       "def sub(a, b):\n"
       "    s = a - b\n"
       "    return s\n")


def make_file(tmp, name):
    path = os.path.join(tmp, name)
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write(NEW)
    editor = QPlainTextEdit()
    editor.resize(560, 320)
    editor.setPlainText(OLD)
    editor.document().setModified(False)
    return path, editor


def disk(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read().replace("\r\n", "\n")


def bar_of(review):
    bar = getattr(review, "_bar", None)
    if bar is not None:
        bar.show()
        pump(3)
        bar.refresh()
        pump(2)
    return bar


# ── 1) 单文件 ───────────────────────────────────────────────────
TMP1 = tempfile.mkdtemp(prefix="v090a_")
P1, E1 = make_file(TMP1, "solo.py")
plugin = FakePlugin(FakeStack([(P1, E1)]))
res = plugin.review_files([(P1, OLD)])
check("单文件 staged == 1", res.get("staged") == 1, res)

rev1 = plugin._review_hub.reviews()[0]
bar1 = bar_of(rev1)
check("单文件：工具条存在", bar1 is not None)
if bar1 is not None:
    check("单文件：标签不报文件数", "文件" not in bar1._text, bar1._text)
    check("单文件：标签有处数", "处修改" in bar1._text, bar1._text)
    check("单文件：工具条只有两个按钮",
          len(bar1._buttons()) == 2, len(bar1._buttons()))
    check("单文件：「接受全部修改」在",
          not bar1._accept_all.isHidden()
          and bar1._accept_all.text() == "接受全部修改",
          bar1._accept_all.text())
    check("单文件：「不接受本次修改」在",
          not bar1._reject_all.isHidden()
          and bar1._reject_all.text() == "不接受本次修改",
          bar1._reject_all.text())
    for btn in (bar1._accept_all, bar1._reject_all):
        need = btn.fontMetrics().horizontalAdvance(btn.text())
        check("按钮完整显示「%s」" % btn.text(), btn.width() >= need,
              "宽 %d / 需要 %d" % (btn.width(), need))
    # 标签宽度不得把按钮挤出去
    check("整条不超出视口",
          bar1.width() <= rev1.editor.viewport().width() + 4,
          "%d vs %d" % (bar1.width(), rev1.editor.viewport().width()))

# ── 2) 多文件：i/N 进度 + 本文件按钮 ───────────────────────────
TMP2 = tempfile.mkdtemp(prefix="v090b_")
PAIRS = [make_file(TMP2, "m%d.py" % i) for i in range(3)]
stack = FakeStack(PAIRS)
plugin2 = FakePlugin(stack)
items = [(p, OLD) for p, _e in PAIRS]
res2 = plugin2.review_files(items)
check("三文件全部 staged", res2.get("staged") == 3, res2)

hub2 = plugin2._review_hub
reviews = hub2.reviews()
check("登记处有三个会话", len(reviews) == 3, len(reviews))
for i, rev in enumerate(reviews, 1):
    bar = bar_of(rev)
    if bar is None:
        check("第 %d 个工具条存在" % i, False)
        continue
    check("第 %d 个报「%d/3 文件」" % (i, i),
          bar._text.startswith("%d/3 文件" % i), bar._text)
    check("第 %d 个工具栏按钮仍是两个总开关" % i,
          len(bar._buttons()) == 2)
    check("第 %d 个全局按钮文字对" % i,
          bar._accept_all.text() == "接受全部修改"
          and bar._reject_all.text() == "不接受本次修改")

# ── 3) 「不接受本次修改」：三文件全部回到修改前 ─────────────────
mid = reviews[1]
mid_bar = getattr(mid, "_bar", None)
check("中间那个工具条还在", mid_bar is not None)
if mid_bar is not None:
    mid_bar._reject_all.click()
    pump(8)
for path, _e in PAIRS:
    check("拒绝后磁盘回到旧内容：%s" % os.path.basename(path),
          disk(path).strip("\n") == OLD.strip("\n"),
          disk(path)[:40].replace("\n", "\\n"))
check("拒绝后会话全部注销", hub2.reviews() == [], hub2.reviews())
check("拒绝后工具条已摘掉", all(r._bar is None for r in reviews))

# ── 4) 「接受全部修改」：三文件全部保留修改后 ───────────────────
TMP3 = tempfile.mkdtemp(prefix="v090c_")
PAIRS3 = [make_file(TMP3, "k%d.py" % i) for i in range(3)]
stack3 = FakeStack(PAIRS3)
plugin3 = FakePlugin(stack3)
res3 = plugin3.review_files([(p, OLD) for p, _e in PAIRS3])
check("接受组 staged == 3", res3.get("staged") == 3, res3)
revs3 = plugin3._review_hub.reviews()
bar0 = getattr(revs3[0], "_bar", None)
check("接受组工具条存在", bar0 is not None)
bar0.show()
pump(3)
bar0._accept_all.click()
pump(8)
for path, editor in PAIRS3:
    check("接受后磁盘为新内容：%s" % os.path.basename(path),
          disk(path).strip("\n") == NEW.strip("\n"),
          disk(path)[:40].replace("\n", "\\n"))
    check("接受后编辑器没有幽灵行：%s" % os.path.basename(path),
          "return a - b" not in editor.toPlainText())
check("接受后会话全部注销", plugin3._review_hub.reviews() == [])

# ── 5) 窄视口：标签让位，按钮不缩 ───────────────────────────────
TMP4 = tempfile.mkdtemp(prefix="v090d_")
P4, E4 = make_file(TMP4, "narrow.py")
E4.resize(330, 260)
plugin4 = FakePlugin(FakeStack([(P4, E4)]))
plugin4.review_files([(P4, OLD)])
rev4 = plugin4._review_hub.reviews()[0]
bar4 = bar_of(rev4)
if bar4 is not None:
    for btn in (bar4._accept_all, bar4._reject_all):
        need = btn.fontMetrics().horizontalAdvance(btn.text())
        check("窄视口下「%s」仍完整" % btn.text(), btn.width() >= need,
              "宽 %d / 需要 %d" % (btn.width(), need))
    # 两个按钮本身就要 ~233px + 标签最小 48px，这是物理下限；
    # 关键是别像 v0.8.9 那样把整条撑到 717 把编辑器盖住。
    check("窄视口下整条不至于撑破屏幕",
          bar4.width() <= 400 and bar4.width() <= E4.viewport().width() + 40,
          "%d vs %d" % (bar4.width(), E4.viewport().width()))
    check("窄视口下标签真的被截断",
          bar4._label.text() != bar4._text and bar4._text not in bar4._label.text(),
          "%r vs %r" % (bar4._label.text(), bar4._text))
    check("窄视口下标签仍保留文件语义",
          bar4._text.endswith("处修改") or "已处理" in bar4._text,
          bar4._text)

# ── 6) 界面字号调到 19 时：按钮按字宽长，但工具条字号封顶 ───────
# 真机 (v0.8.10 首轮) 的坑：QToolButton 没设 icon 时 sizeHint 只报 60px，
# 17px 字号下六个汉字被压成「接受全部…」。这里把界面字号顶到 19 复现。
from spyder_copilot_chat.widgets import theme as _theme           # noqa: E402

_old_theme = _theme.current_theme()
_big = _theme.Theme(dict(_old_theme.tokens),
                    accent_key=_old_theme.accent_key, ui_font=19)
_theme.set_current_theme(_big)
try:
    TMP5 = tempfile.mkdtemp(prefix="v090e_")
    P5, E5 = make_file(TMP5, "bigfont.py")
    E5.resize(760, 320)
    plugin5 = FakePlugin(FakeStack([(P5, E5)]))
    plugin5.review_files([(P5, OLD)])
    rev5 = plugin5._review_hub.reviews()[0]
    bar5 = bar_of(rev5)
    check("大字号：工具条建起来了", bar5 is not None)
    if bar5 is not None:
        for btn in (bar5._accept_all, bar5._reject_all):
            need = btn.fontMetrics().horizontalAdvance(btn.text()) + 20
            check("大字号下「%s」按字宽给够" % btn.text(),
                  btn.width() >= need,
                  "宽 %d / 需要 %d" % (btn.width(), need))
        check("工具条字号封顶 14px",
              bar5._label.font().pixelSize() <= 15,
              bar5._label.font().pixelSize())
        check("大字号下整条仍在视口内",
              bar5.width() <= max(240, E5.viewport().width() - 44) + 4,
              "%d vs %d" % (bar5.width(), E5.viewport().width()))
finally:
    _theme.set_current_theme(_old_theme)

# ── 汇总 ───────────────────────────────────────────────────────
print("=" * 60)
for f in FAILS:
    print("FAIL  " + f)
print("PASS %d / %d" % (len(PASSES), len(PASSES) + len(FAILS)))
sys.exit(1 if FAILS else 0)
