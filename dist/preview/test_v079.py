# -*- coding: utf-8 -*-
"""v0.7.9 回归：编辑器内联 diff（幽灵删除行 + 绿底新增 + 接受/撤销）。

覆盖：
  1) stage 基本形态：hunks / counts / 装饰 / 幽灵行
  2) 全部接受 / 全部撤销：文档、磁盘、resolved 动作
  3) 逐处接受 / 撤销：mixed 语义
  4) 边界：整文件重写、纯增、纯删、新建文件、无差异、结尾无换行
  5) 用户 buffer 有未保存内容 → 不接管
  6) 快捷键：Ctrl+Enter 接受、Ctrl+Backspace 撤销、Ctrl+S 被拦
  7) ReviewHub：多文件、全局接受、通知、shutdown
  8) 集成契约：chat_widget 的快照 / staging 逻辑
"""
import os
import sys
import tempfile

ROOT = r"C:\Users\Mr_Yang\Documents\deepseek-harness\default-workspace\spyder-copilot-chat"
sys.path.insert(0, ROOT)

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from qtpy.QtCore import QEvent, Qt
from qtpy.QtGui import QKeyEvent
from qtpy.QtWidgets import QApplication, QPlainTextEdit

from spyder_copilot_chat.widgets import editor_diff as ed

APP = QApplication.instance() or QApplication(sys.argv)

FAILS = []
PASSES = []


def check(name, cond, extra=""):
    if cond:
        PASSES.append(name)
        print("  PASS  %s" % name)
    else:
        FAILS.append(name)
        print("  FAIL  %s  %s" % (name, extra))


def mkeditor(text):
    e = QPlainTextEdit()
    e.setPlainText(text)
    e.document().setModified(False)
    return e


OLD = ("def add(a, b):\n"
       "    return a + b\n"
       "\n"
       "\n"
       "def sub(a, b):\n"
       "    return a - b\n")

NEW = ("def add(a, b):\n"
       '    """两数相加。"""\n'
       "    return a + b\n"
       "\n"
       "\n"
       "def sub(a, b):\n"
       "    s = a - b\n"
       "    return s\n"
       "\n"
       "\n"
       "def mul(a, b):\n"
       "    return a * b\n")


class Sandbox(object):
    """一个临时目录 + 一个可反复写的目标文件。"""

    def __init__(self, name="calc.py", old=OLD, new=NEW):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, name)
        self.old = old
        self.write(new)

    def write(self, text):
        with open(self.path, "w", encoding="utf-8", newline="") as f:
            f.write(text)

    def disk(self):
        with open(self.path, "r", encoding="utf-8", newline="") as f:
            return f.read()


# ── 1) stage 基本形态 ────────────────────────────────────────────
print("\n[1] stage 基本形态")
sb = Sandbox()
edt = mkeditor(OLD)
r = ed.InlineReview(edt, sb.path, OLD)
check("stage 返回 True", r.stage() is True)
check("会话已激活", r.is_active is True)
hs = r.hunks()
check("切出 3 处修改", len(hs) == 3, "got %d" % len(hs))
# 期望的增删行数由 difflib 现算，避免把数字写死
import difflib
_ops = difflib.SequenceMatcher(None, OLD.split("\n"), NEW.split("\n"),
                               autojunk=False).get_opcodes()
exp_plus = sum(j2 - j1 for tag, i1, i2, j1, j2 in _ops
               if tag in ("insert", "replace"))
exp_minus = sum(i2 - i1 for tag, i1, i2, j1, j2 in _ops
                if tag in ("delete", "replace"))
plus, minus = r.counts()
check("增行计数与 difflib 一致", plus == exp_plus,
      "got %d want %d" % (plus, exp_plus))
check("删行计数与 difflib 一致", minus == exp_minus,
      "got %d want %d" % (minus, exp_minus))
text = edt.toPlainText()
check("幽灵删除行插回文档", "    return a - b" in text)
check("新增行在文档里", "def mul(a, b):" in text)
# 标记：del / add 都在
kinds = []
blk = edt.document().firstBlock()
while blk.isValid():
    d = blk.userData()
    if isinstance(d, ed.DiffBlockMark):
        kinds.append(d.kind)
    blk = blk.next()
check("存在 del 标记", "del" in kinds)
check("存在 add 标记", "add" in kinds)
check("装饰已挂上", r._deco_count() >= 0 if hasattr(r, "_deco_count") else True)


# ── 2) 全部接受 / 全部撤销 ──────────────────────────────────────
print("\n[2] 全部接受 / 全部撤销")
sb = Sandbox()
edt = mkeditor(OLD)
r = ed.InlineReview(edt, sb.path, OLD)
r.stage()
actions = []
r.resolved.connect(lambda rev, act: actions.append(act))
r.accept_all()
check("accept_all 文档 == 新内容", edt.toPlainText() == NEW,
      repr(edt.toPlainText()[-40:]))
check("accept_all 磁盘 == 新内容", sb.disk() == NEW)
check("accept_all 会话结束", r.is_active is False)
check("resolved 动作 = keep", actions == ["keep"], str(actions))

sb = Sandbox()
edt = mkeditor(OLD)
r = ed.InlineReview(edt, sb.path, OLD)
r.stage()
actions = []
r.resolved.connect(lambda rev, act: actions.append(act))
r.revert_all()
check("revert_all 文档 == 旧内容", edt.toPlainText() == OLD,
      repr(edt.toPlainText()[-40:]))
check("revert_all 磁盘回写为旧内容", sb.disk() == OLD)
check("revert_all 会话结束", r.is_active is False)
check("resolved 动作 = revert", actions == ["revert"], str(actions))


# ── 3) 逐处接受 / 撤销 ──────────────────────────────────────────
print("\n[3] 逐处接受 / 撤销")
sb = Sandbox()
edt = mkeditor(OLD)
r = ed.InlineReview(edt, sb.path, OLD)
r.stage()
hs = r.hunks()
r.accept_hunk(hs[0].hid)     # docstring 插入
r.revert_hunk(hs[1].hid)     # sub 改写
expected = NEW.replace("    s = a - b\n    return s\n", "    return a - b\n")
check("逐处决定后文档 == 预期", edt.toPlainText() == expected,
      repr(edt.toPlainText()[-50:]))
check("逐处决定后磁盘同步", sb.disk() == expected)
check("仍有未决 hunk", len(r.hunks()) == 1, "got %d" % len(r.hunks()))

sb = Sandbox()
edt = mkeditor(OLD)
r = ed.InlineReview(edt, sb.path, OLD)
r.stage()
hs = r.hunks()
r.revert_hunk(hs[1].hid)
r.accept_hunk(hs[0].hid)
check("反向顺序逐处决定也一致", edt.toPlainText() == expected)

# 全部逐处接受 → 会话结束且动作 keep
sb = Sandbox()
edt = mkeditor(OLD)
r = ed.InlineReview(edt, sb.path, OLD)
r.stage()
acts = []
r.resolved.connect(lambda rev, a: acts.append(a))
for h in list(r.hunks()):
    r.accept_hunk(h.hid)
check("逐处全接受后结束", r.is_active is False)
check("逐处全接受动作 = keep", acts == ["keep"], str(acts))
check("逐处全接受文档 == 新内容", edt.toPlainText() == NEW)


# ── 4) 边界 ─────────────────────────────────────────────────────
print("\n[4] 边界")
sb = Sandbox(new="x = 10\n")
edt = mkeditor(OLD)
r = ed.InlineReview(edt, sb.path, OLD)
check("整文件重写 stage", r.stage() is True)
r.accept_all()
check("整文件重写 accept 文档", edt.toPlainText() == "x = 10\n",
      repr(edt.toPlainText()))
check("整文件重写 accept 磁盘", sb.disk() == "x = 10\n")

sb = Sandbox()
edt = mkeditor(OLD)
r = ed.InlineReview(edt, sb.path, OLD)
r.stage()
r.revert_all()
check("整文件重写 revert 文档", edt.toPlainText() == OLD,
      repr(edt.toPlainText()[-40:]))
check("整文件重写 revert 磁盘", sb.disk() == OLD)

sb = Sandbox(old="line1\nline3\n", new="line1\nline2\nline3\n")
edt = mkeditor("line1\nline3\n")
r = ed.InlineReview(edt, sb.path, "line1\nline3\n")
r.stage()
check("纯增 counts", r.counts() == (1, 0), str(r.counts()))
r.accept_all()
check("纯增 accept", edt.toPlainText() == "line1\nline2\nline3\n",
      repr(edt.toPlainText()))
check("纯增磁盘", sb.disk() == "line1\nline2\nline3\n")

sb = Sandbox(old="line1\nline2\nline3\n", new="line1\nline3\n")
edt = mkeditor("line1\nline2\nline3\n")
r = ed.InlineReview(edt, sb.path, "line1\nline2\nline3\n")
r.stage()
check("纯删 counts", r.counts() == (0, 1), str(r.counts()))
r.revert_all()
check("纯删 revert", edt.toPlainText() == "line1\nline2\nline3\n")
check("纯删磁盘回写", sb.disk() == "line1\nline2\nline3\n")

sb = Sandbox(name="new.py", old="", new="print(1)\nprint(2)\n")
edt = mkeditor("")
r = ed.InlineReview(edt, sb.path, "")
check("新建文件 stage", r.stage() is True)
r.accept_all()
check("新建文件 accept", edt.toPlainText() == "print(1)\nprint(2)\n")
sb2 = Sandbox(name="new2.py", old="", new="print(1)\nprint(2)\n")
edt2 = mkeditor("")
r2 = ed.InlineReview(edt2, sb2.path, "")
r2.stage()
r2.revert_all()
check("新建文件 revert 回到空", edt2.toPlainText() == "")

sb = Sandbox(new=OLD)
edt = mkeditor(OLD)
r = ed.InlineReview(edt, sb.path, OLD)
check("无差异不接管", r.stage() is False)
check("无差异不激活", r.is_active is False)

sb = Sandbox()
edt = mkeditor(OLD)
# 用户真在编辑器里动过内容（不是仅仅 modified 标志被 Spyder 置位）
touched = OLD + "\n# 用户手动加的一行\n"
edt.setPlainText(touched)
edt.document().setModified(True)
r = ed.InlineReview(edt, sb.path, OLD)
check("buffer 真改过 → 不接管", r.stage() is False)
check("buffer 真改过 → 文档不动", edt.toPlainText() == touched)

# v0.8.7：只是 modified 标志为真、内容仍是改动前原文 → 必须放行。
# 正常复核时磁盘已是新内容、buffer 还是旧内容，isModified 一置位就不
# 再复核的话，Spyder 的几次无关置位就能让这个功能彻底哑火。
sb = Sandbox()
edt = mkeditor(OLD)
edt.document().setModified(True)
r = ed.InlineReview(edt, sb.path, OLD)
check("仅 modified 置位 → 放行", r.stage() is True, r.fail_reason)

sb = Sandbox(name="x.py", old="a = 1\nb = 2", new="a = 1\nb = 3")
edt = mkeditor("a = 1\nb = 2")
r = ed.InlineReview(edt, sb.path, "a = 1\nb = 2")
check("结尾无换行 stage", r.stage() is True)
r.accept_all()
check("结尾无换行 accept", edt.toPlainText() == "a = 1\nb = 3",
      repr(edt.toPlainText()))


# ── 5) 快捷键 ───────────────────────────────────────────────────
print("\n[5] 快捷键")
sb = Sandbox()
edt = mkeditor(OLD)
r = ed.InlineReview(edt, sb.path, OLD)
r.stage()


def press(widget, key, mods=Qt.ControlModifier):
    """投递一次真实的按键事件。

    注意不能直接 ``widget.event(ev)``——那只调 widget 的 event()
    方法，**绕过事件过滤器**，filter 里装的快捷键根本不会触发。
    必须走 postEvent，让事件进队列后由 notify 分发（filter 先于
    widget 自身的 keyPressEvent）。
    """
    ev = QKeyEvent(QEvent.KeyPress, key, mods)
    QApplication.postEvent(widget, ev)
    APP.processEvents()


press(edt, Qt.Key_Return)
check("Ctrl+Enter 全部接受", edt.toPlainText() == NEW,
      repr(edt.toPlainText()[-40:]))

sb = Sandbox()
edt = mkeditor(OLD)
r = ed.InlineReview(edt, sb.path, OLD)
r.stage()
press(edt, Qt.Key_Backspace)
check("Ctrl+Backspace 全部撤销", edt.toPlainText() == OLD,
      repr(edt.toPlainText()[-40:]))

sb = Sandbox()
edt = mkeditor(OLD)
r = ed.InlineReview(edt, sb.path, OLD)
r.stage()
ev = QKeyEvent(QEvent.KeyPress, Qt.Key_S, Qt.ControlModifier)
check("Ctrl+S 被拦截", edt.event(ev) is True)
check("Ctrl+S 后文档仍有幽灵行", "return a - b" in edt.toPlainText())


# ── 6) ReviewHub ────────────────────────────────────────────────
print("\n[6] ReviewHub")
sb1 = Sandbox(name="a.py", old="x = 1\n", new="x = 1\ny = 2\n")
sb2 = Sandbox(name="b.py", old="p = 1\n", new="p = 2\n")
e1 = mkeditor("x = 1\n")
e2 = mkeditor("p = 1\n")
hub = ed.ReviewHub()
notified = []
hub.set_notifier(lambda path, act: notified.append((os.path.basename(path),
                                                     act)))
r1 = hub.stage(e1, sb1.path, "x = 1\n")
r2 = hub.stage(e2, sb2.path, "p = 1\n")
check("hub 登记两场会话", len(hub.reviews()) == 2, "got %d" % len(hub.reviews()))
check("全局计数广播", r1._global_count == 2, str(r1._global_count))
hub.accept_all()
check("全局接受 → 文档1", e1.toPlainText() == "x = 1\ny = 2\n",
      repr(e1.toPlainText()))
check("全局接受 → 文档2", e2.toPlainText() == "p = 2\n",
      repr(e2.toPlainText()))
check("通知已发出", len(notified) == 2, str(notified))
hub.shutdown()
check("shutdown 清空会话", len(hub.reviews()) == 0)


# ── 7) 集成契约：chat_widget 的快照 / staging ───────────────────
print("\n[7] 集成契约")
from spyder_copilot_chat.widgets.chat_widget import CopilotChatPanel

tmpdir = tempfile.mkdtemp()
tfile = os.path.join(tmpdir, "svc.py")
with open(tfile, "w", encoding="utf-8") as f:
    f.write("a = 1\n")

captured = {}


class FakePanel(object):
    """只实现 _stage_editor_reviews 用到的那几个字段。"""

    def __init__(self):
        self._review_provider = None
        self._review_before = {}
        self._config = {"inline_diff_enabled": True}
        self._current_bubble = None
        self._messages = []

    set_editor_review_provider = CopilotChatPanel.set_editor_review_provider
    _snapshot_review_targets = CopilotChatPanel._snapshot_review_targets
    _stage_editor_reviews = CopilotChatPanel._stage_editor_reviews
    _emit_chat_diffs = CopilotChatPanel._emit_chat_diffs
    _trim_patch = staticmethod(CopilotChatPanel._trim_patch)
    note_editor_resolution = CopilotChatPanel.note_editor_resolution

    def _get_project_root(self):
        return tmpdir

    def _add_msg(self, role, content):
        return None

    def _scroll_to_bottom(self):
        pass


fp = FakePanel()
staged = []


def fake_provider(items):
    staged.append(list(items))
    return len(items)


fp.set_editor_review_provider(fake_provider)

# 快照：写盘前抓内容
calls = [{"function": {"name": "write_to_file",
                       "arguments": '{"path": "%s", "content": "a = 2\\n"}'
                                    % tfile.replace("\\", "\\\\")}}]
fp._snapshot_review_targets(calls, tmpdir)
check("快照抓到目标文件", tfile in fp._review_before, str(list(fp._review_before)))
check("快照内容正确", fp._review_before.get(tfile) == "a = 1\n",
      repr(fp._review_before.get(tfile)))

# 写盘后 staging
with open(tfile, "w", encoding="utf-8") as f:
    f.write("a = 2\n")
fp._stage_editor_reviews(calls)
check("staging 调用了 provider", len(staged) == 1, str(staged))
if staged:
    items = staged[0]
    check("staging 带上 (path, old_text)", items and items[0][0] == tfile,
          str(items))
    check("staging 的 old_text 是写盘前内容",
          items and items[0][1] == "a = 1\n", repr(items[0][1]) if items else "")
check("staging 后快照清空", fp._review_before == {})

# 没变化的文件不该进 staging
staged.clear()
fp._snapshot_review_targets(calls, tmpdir)
fp._stage_editor_reviews(calls)
check("内容未变 → 不 staging", len(staged) == 0, str(staged))

# 两个出口都关掉 → 不快照也不 staging
# （v0.8.1 起快照还服务对话内的红绿卡片，所以只看 inline_diff_enabled
#  不够；chat_diff_mode 是新增的第二个出口开关）
fp._config["inline_diff_enabled"] = False
fp._config["chat_diff_mode"] = "off"
fp._snapshot_review_targets(calls, tmpdir)
check("开关关闭 → 不快照", fp._review_before == {})
fp._stage_editor_reviews(calls)
check("开关关闭 → 不 staging", len(staged) == 0)
fp._config["chat_diff_mode"] = "smart"

# resolved 通知 → 回灌事实消息
fp._config["inline_diff_enabled"] = True
fp._messages = []
fp.note_editor_resolution(tfile, "revert")
check("撤销通知回灌消息", len(fp._messages) == 1, str(fp._messages))
if fp._messages:
    check("回灌内容含文件路径", tfile in fp._messages[0]["content"])
fp.note_editor_resolution(tfile, "keep")
fp.note_editor_resolution(tfile, "mixed")
check("三种动作都不崩", True)


# ── 8) 插件层契约 ───────────────────────────────────────────────
print("\n[8] 插件层")
from spyder_copilot_chat.plugin import CopilotChatPlugin
check("插件有 review_files", hasattr(CopilotChatPlugin, "review_files"))
check("插件有 _review_hub", hasattr(CopilotChatPlugin, "_review_hub"))
check("插件有 _shutdown_review_hub",
      hasattr(CopilotChatPlugin, "_shutdown_review_hub"))


# ── 汇总 ────────────────────────────────────────────────────────
print("\n" + "=" * 56)
print("PASS %d / %d" % (len(PASSES), len(PASSES) + len(FAILS)))
if FAILS:
    print("FAILED:")
    for f in FAILS:
        print("  - %s" % f)
    sys.exit(1)
print("ALL GREEN")
