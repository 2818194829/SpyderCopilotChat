# -*- coding: utf-8 -*-
"""v0.8.3 回归：装饰的「可见范围过滤」必须在滚动之后重新计算。

真实 Spyder 里 TextDecorationsManager._update() 只把
editor.get_buffer_block_numbers()（可见行 ± 半屏）以内的装饰推给
setExtraSelections，范围外直接丢弃；而 _update 没有别的触发者，
只在 add_key 时算一次（CodeEditor.update_decorations() 在 6.1 里
无人调用）。

于是旧顺序 —— setPlainText（视图回第 1 行）→ 挂装饰 → 才滚到改动处
—— 会把所有深在文件中的 hunk 在过滤阶段丢掉。文档确实被重写了、
幽灵行也在，但一个像素的颜色都没有。

覆盖：
  1) 复现：可见范围不含 hunk 时，装饰被全部过滤掉（pushed == 0）
  2) 修复：stage() 先滚动再挂装饰，同一份 diff 立刻能看见
  3) 滚动时自动重算（scrollbar.valueChanged → _refresh_decorations）
  4) 光标移动也重算
  5) 后台 tab → 切到前台（Show / Resize）也重算
"""
import os
import sys

ROOT = (r"C:\Users\Mr_Yang\Documents\deepseek-harness"
        r"\default-workspace\spyder-copilot-chat")
sys.path.insert(0, ROOT)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from qtpy.QtCore import QTimer                       # noqa: E402
from qtpy.QtGui import QTextCursor                   # noqa: E402
from qtpy.QtWidgets import QApplication, QPlainTextEdit  # noqa: E402

from spyder_copilot_chat.widgets.editor_diff import (      # noqa: E402
    InlineReview,
)
from spyder_copilot_chat.widgets import theme               # noqa: E402

APP = QApplication.instance() or QApplication([])
FAILS = []
PASSES = []


def check(name, cond, extra=""):
    if cond:
        PASSES.append(name)
        print("  PASS  %s" % name)
    else:
        FAILS.append(name)
        print("  FAIL  %s  %s" % (name, extra))


class SpyderLikeManager(object):
    """按 Spyder 的真实语义过滤装饰，而不是无脑全部推给编辑器。"""

    def __init__(self, editor):
        self.editor = editor
        self.keys = {}
        self.pushed = []        # 最近一次真的推给 setExtraSelections 的
        self.update_calls = 0

    def add_key(self, key, decos):
        self.keys[key] = list(decos)
        self._update()
        return len(decos)

    def remove_key(self, key):
        if key in self.keys:
            del self.keys[key]
            self._update()

    def get(self, key, default=None):
        return self.keys.get(key, default)

    def update(self):
        # 真 manager 用 15ms 单次定时器；这里同步算，抖动无关结论
        self.update_calls += 1
        self._update()

    def _update(self):
        first, last = self.editor.get_buffer_block_numbers()
        visible = []
        for decos in self.keys.values():
            for deco in decos:
                nb = deco.cursor.block().blockNumber()
                if first <= nb <= last:
                    visible.append(deco)
        self.pushed = visible
        self.editor.setExtraSelections(visible)


class StubEditor(QPlainTextEdit):
    """视口跟随光标：centerCursor / setTextCursor 之后可见范围就变了。"""

    HALF = 12

    def __init__(self):
        super().__init__()
        self.decorations = SpyderLikeManager(self)
        self.resize(600, 400)

    def get_buffer_block_numbers(self):
        cur = self.textCursor().blockNumber()
        return (max(0, cur - self.HALF),
                min(self.blockCount(), cur + self.HALF))


OLD = "\n".join("line %03d" % i for i in range(300))
NEW_LINES = ["line %03d" % i for i in range(300)]
NEW_LINES[250] = "line 250 CHANGED"
NEW_LINES.insert(251, "line 250b ADDED")
NEW = "\n".join(NEW_LINES)

TMPDIR = os.path.join(os.environ.get("TEMP", "."), "v083_probe")
os.makedirs(TMPDIR, exist_ok=True)
TARGET = os.path.join(TMPDIR, "far_hunk.py")


def write(text):
    with open(TARGET, "w", encoding="utf-8") as fh:
        fh.write(text)


def pump(n=4):
    for _ in range(n):
        APP.processEvents()


theme.set_current_theme(theme.resolve_theme("dark"))
write(OLD)
write(NEW)

print("[1] 复现旧顺序的问题：装饰在可见范围外被丢弃")
ed = StubEditor()
ed.setPlainText(OLD)
pump()
rev = InlineReview(ed, TARGET, OLD)
rev.stage()
pump(6)
# 把视图挪回文件开头，再算一次装饰 —— 这正是旧顺序里
# 「setPlainText 之后视图在第 1 行，然后才挂装饰」的时刻
ed.moveCursor(QTextCursor.Start)
pump()
rev._apply_decorations()
pump()
total = len(ed.decorations.get("copilot_chat_inline_diff") or [])
check("装饰对象本身是为整篇算的 (>=3)", total >= 3, "total=%d" % total)
check("但可见范围不含 hunk → 一条都没推给编辑器",
      len(ed.decorations.pushed) == 0,
      "pushed=%d" % len(ed.decorations.pushed))
rev.clear()
pump()

print("[2] 修复后的 stage()：先滚动再挂装饰")
ed2 = StubEditor()
ed2.setPlainText(OLD)
ed2.moveCursor(QTextCursor.Start)
pump()
rev2 = InlineReview(ed2, TARGET, OLD)
ok = rev2.stage()
pump(6)
check("stage 成功", ok, rev2.fail_reason)
check("光标被移到第一处修改", 245 <= ed2.textCursor().blockNumber() <= 260,
      "cursor=%d" % ed2.textCursor().blockNumber())
check("可见范围内的装饰已推给编辑器（红绿行真的会出现）",
      len(ed2.decorations.pushed) >= 2,
      "pushed=%d" % len(ed2.decorations.pushed))
bgs = [d.format.background().color().name() for d in ed2.decorations.pushed]
check("推到编辑器的装饰里既有红也有绿",
      len(set(bgs)) >= 2, "bgs=%s" % sorted(set(bgs))[:6])
check("幽灵删除行确实插进了文档",
      "line 250\n" in ed2.toPlainText() or "line 250" in ed2.toPlainText())
check("新增行确实进了文档", "line 250b ADDED" in ed2.toPlainText())

print("[3] 滚离改动区 → 回来后自动重算")
before = ed2.decorations.update_calls
ed2.moveCursor(QTextCursor.Start)
pump()
check("光标回顶部触发重算", ed2.decorations.update_calls > before,
      "%d -> %d" % (before, ed2.decorations.update_calls))
check("顶部的装饰里没有 hunk 行", len(ed2.decorations.pushed) == 0,
      "pushed=%d" % len(ed2.decorations.pushed))
ed2.verticalScrollBar().valueChanged.emit(120)
rev2._scroll_to_first_hunk()
pump()
check("再滚回改动处又能看到颜色", len(ed2.decorations.pushed) >= 2,
      "pushed=%d" % len(ed2.decorations.pushed))

print("[4] 切 tab（Show / Resize）也会重算")
calls = ed2.decorations.update_calls
rev2._refresh_decorations()
pump()
check("_refresh_decorations 能触发重算",
      ed2.decorations.update_calls > calls)

print("[5] 装饰为空时不炸")
rev_empty = InlineReview(ed2, TARGET, NEW)
rev_empty._active = True
rev_empty._refresh_decorations()
check("没有 hunk 时 refresh 是安全的", True)

rev2.clear()
rev.clear()
pump()

print("\n%d passed, %d failed" % (len(PASSES), len(FAILS)))
if FAILS:
    print("FAILED:", FAILS)
sys.exit(1 if FAILS else 0)
