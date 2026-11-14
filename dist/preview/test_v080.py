# -*- coding: utf-8 -*-
"""v0.8.0 回归：预算式自主循环（Autonomy Edition）。

覆盖：
  1) _round_budget 解析：默认 / 全局覆盖 / Agent 覆盖 / 0 = 不限
  2) 25 轮不再触发中断（旧版硬墙）
  3) 用满硬预算 → 自动续跑，轮次归零、注记入队、继续执行
  4) 续跑次数用尽 → 停止并给出新的可操作文案
  5) 关闭自动续跑 → 直接停止
  6) 只读重复调用只警告，写/执行重复才停
  7) 软阈值只提醒一次
  8) 系统注记排在 tool 结果之后（消息顺序契约）
  9) 设置面板默认值
"""
import os
import sys

ROOT = r"C:\Users\Mr_Yang\Documents\deepseek-harness\default-workspace\spyder-copilot-chat"
sys.path.insert(0, ROOT)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from spyder_copilot_chat.widgets.chat_widget import CopilotChatPanel
from spyder_copilot_chat.widgets.approval_card import EXECUTE_TOOLS, WRITE_TOOLS

FAILS = []
PASSES = []


def check(name, cond, extra=""):
    if cond:
        PASSES.append(name)
        print("  PASS  %s" % name)
    else:
        FAILS.append(name)
        print("  FAIL  %s  %s" % (name, extra))


FLAG = {"v": False}


class Bubble(object):
    def __init__(self):
        self.blocks = []

    def append_block(self, text):
        self.blocks.append(text)


class Audit(object):
    def __init__(self):
        self.events = []

    def log(self, name, **kw):
        self.events.append((name, kw))


class FakePanel(object):
    """只保留 v0.8.0 预算逻辑所需的最小外壳。"""
    _round_budget = CopilotChatPanel._round_budget
    _queue_agent_note = CopilotChatPanel._queue_agent_note
    _on_tool_calls = CopilotChatPanel._on_tool_calls

    def __init__(self, cfg=None, participant_rounds=0, agent_rounds=0):
        self._config = dict(cfg or {})
        self._tool_round = 0
        self._total_tool_rounds = 0
        self._auto_continues = 0
        self._soft_warned = False
        self._repeat_warned = False
        self._pending_notes = []
        self._tool_call_history = []
        self._agent_active = True
        self._tc_handled = False
        self._current_bubble = Bubble()
        self._audit = Audit()
        self._participant_rounds = participant_rounds
        self._agent_rounds = agent_rounds
        self.executed = []
        self.finalized = 0
        self.notes = []          # 模拟落库的消息顺序
        self.approval = False

    # ── 桩 ──
    def _get_participant(self):
        if not self._participant_rounds:
            return None
        panel = self

        class P(object):
            max_tool_rounds = panel._participant_rounds
        return P()

    def _get_active_agent_config(self):
        if not self._agent_rounds:
            return None
        return {"max_tool_rounds": self._agent_rounds}

    def _needs_approval_tool_calls(self, calls):
        return self.approval

    def _request_approval(self, calls):
        self.executed.append(("approval", calls))

    def _execute_approved_tools(self, calls, card=None):
        self._tool_round += 1
        self._total_tool_rounds += 1
        self.executed.append(("exec", [c["function"]["name"] for c in calls]))

    def _finalize(self, clear_task=True):
        self.finalized += 1


def call(name, args="{}"):
    return {"id": "t1", "type": "function",
            "function": {"name": name, "arguments": args}}


def read_call(path="/tmp/a.py"):
    return call("read_file", '{"path": "%s"}' % path)


def write_call(path="/tmp/a.py"):
    return call("create_file", '{"path": "%s"}' % path)


# ── 1) 预算解析 ──────────────────────────────────────────────────
print("\n[1] _round_budget 解析")
p = FakePanel()
soft, hard, auto, max_auto = p._round_budget()
check("默认硬预算 200（不再是 25）", hard == 200, hard)
check("默认软阈值 40", soft == 40, soft)
check("默认开启自动续跑", auto is True)
check("默认续跑上限 5", max_auto == 5, max_auto)

p = FakePanel({"max_tool_rounds": 0})
check("全局 0 = 不限制", p._round_budget()[1] == 0)
p = FakePanel({"max_tool_rounds": 60, "soft_tool_rounds": 30,
               "auto_continue_on_limit": False, "max_auto_continues": 2})
soft, hard, auto, max_auto = p._round_budget()
check("全局覆盖生效", (soft, hard, auto, max_auto) == (30, 60, False, 2),
      (soft, hard, auto, max_auto))
p = FakePanel({"max_tool_rounds": 200}, agent_rounds=50)
check("Agent 配置优先于全局", p._round_budget()[1] == 50, p._round_budget())
p = FakePanel({"max_tool_rounds": 200}, agent_rounds=0)
check("Agent=0 表示跟随全局", p._round_budget()[1] == 200)
p = FakePanel({"max_tool_rounds": 50, "soft_tool_rounds": 100})
check("软阈值不超过硬预算", p._round_budget()[0] == 50, p._round_budget())
p = FakePanel({"soft_tool_rounds": 0})
check("软阈值 0 = 关闭提醒", p._round_budget()[0] == 200, p._round_budget())

# ── 2) 25 轮不再是墙 ─────────────────────────────────────────────
print("\n[2] 旧版 25 轮硬墙已移除")
p = FakePanel()
p._tool_round = 25
p._on_tool_calls([read_call()])
check("第 25 轮继续执行", p.executed and p.executed[0][0] == "exec", p.executed)
check("第 25 轮不 finalize", p.finalized == 0)
check("第 25 轮无「达到本轮上限」文案",
      not any("达到本轮上限" in b for b in p._current_bubble.blocks))

# ── 3) 用满硬预算 → 自动续跑 ─────────────────────────────────────
print("\n[3] 硬预算自动续跑")
p = FakePanel({"max_tool_rounds": 5, "soft_tool_rounds": 2})
p._total_tool_rounds = 5
p._tool_round = 5
p._on_tool_calls([read_call()])
check("自动续跑计数 +1", p._auto_continues == 1, p._auto_continues)
check("段内轮次归零后执行本轮", p._tool_round == 1, p._tool_round)
check("任务未中断", p.finalized == 0)
check("注记已入队", len(p._pending_notes) == 1, p._pending_notes)
check("注记内容含「自动续期」", "自动续期" in p._pending_notes[0])
check("气泡提示自动续跑",
      any("自动续跑 1/5" in b for b in p._current_bubble.blocks),
      p._current_bubble.blocks)
names = [e[0] for e in p._audit.events]
check("审计记录 auto_continue", "auto_continue" in names, names)

# ── 4) 续跑用尽 → 停止 ──────────────────────────────────────────
print("\n[4] 续跑次数用尽后停止")
p = FakePanel({"max_tool_rounds": 5, "max_auto_continues": 2})
p._auto_continues = 2
p._tool_round = 5
p._total_tool_rounds = 15
p._on_tool_calls([read_call()])
check("停止任务", p.finalized == 1, p.finalized)
check("不再执行工具", not p.executed, p.executed)
block = " ".join(p._current_bubble.blocks)
check("文案给出预算说明", "每段 5 轮" in block, block)
check("文案指向设置项", "单段工具轮次" in block, block)
check("文案不再只说「请回复继续」", "调高" in block, block)

# ── 5) 关闭自动续跑 ─────────────────────────────────────────────
print("\n[5] 关闭自动续跑")
p = FakePanel({"max_tool_rounds": 5, "auto_continue_on_limit": False})
p._tool_round = 5
p._on_tool_calls([read_call()])
check("直接停止", p.finalized == 1)
check("不产生续跑", p._auto_continues == 0)

# ── 6) 重复检测分级 ─────────────────────────────────────────────
print("\n[6] 重复调用分级处理")
p = FakePanel()
for _ in range(3):
    p._on_tool_calls([read_call("/same.py")])
check("只读重复不中断", p.finalized == 0, p.finalized)
check("只读重复只警告一次",
      sum(1 for n in p._pending_notes if "只读调用重复" in n) == 1,
      p._pending_notes)

p = FakePanel()
for _ in range(3):
    p._on_tool_calls([write_call("/same.py")])
check("写操作重复则停止", p.finalized == 1, p.finalized)
check("写重复文案区分「写/执行」",
      any("写/执行操作" in b for b in p._current_bubble.blocks),
      p._current_bubble.blocks)

p = FakePanel()
for _ in range(3):
    p._on_tool_calls([call("run_command", '{"cmd": "pytest"}')])
check("run_command 重复也停止", p.finalized == 1)
check("run_command 属于执行类", "run_command" in EXECUTE_TOOLS)

# ── 7) 软阈值只提醒一次 ─────────────────────────────────────────
print("\n[7] 软阈值提醒")
p = FakePanel({"max_tool_rounds": 100, "soft_tool_rounds": 3})
p._tool_round = 3
p._on_tool_calls([read_call()])
tips = [b for b in p._current_bubble.blocks if "提醒模型收束" in b]
check("触发一次提醒", len(tips) == 1, p._current_bubble.blocks)
check("提醒不中断任务", p.finalized == 0)
p._tool_round = 4
p._on_tool_calls([read_call("/b.py")])
tips = [b for b in p._current_bubble.blocks if "提醒模型收束" in b]
check("不重复刷提醒", len(tips) == 1, len(tips))

# ── 8) 消息顺序契约 ─────────────────────────────────────────────
print("\n[8] 注记位置：tool 结果之后")
p = FakePanel()
p._tool_round = 40
p._queue_agent_note("[系统] A")
p._queue_agent_note("[系统] B")
fake_messages = [{"role": "assistant", "tool_calls": []},
                 {"role": "tool", "content": "ok"}]
for note in p._pending_notes:
    fake_messages.append({"role": "user", "content": note})
p._pending_notes = []
check("注记追加在 tool 之后",
      [m["role"] for m in fake_messages] == ["assistant", "tool", "user", "user"],
      [m["role"] for m in fake_messages])
check("提示语不冒充用户发言", fake_messages[2]["content"].startswith("[系统]"))

# ── 9) 设置默认值 ───────────────────────────────────────────────
print("\n[9] 设置对话框默认值")
try:
    from qtpy.QtWidgets import QApplication
    app = QApplication.instance() or QApplication(sys.argv)
    from spyder_copilot_chat.widgets.settings_dialog import SettingsDialog
    dlg = SettingsDialog()
    check("默认单段轮次 200", dlg._max_tool_rounds.value() == 200,
          dlg._max_tool_rounds.value())
    check("默认软阈值 40", dlg._soft_tool_rounds.value() == 40,
          dlg._soft_tool_rounds.value())
    check("默认开启自动续跑", dlg._auto_continue.isChecked() is True)
    check("默认续跑 5 次", dlg._max_auto_continues.value() == 5,
          dlg._max_auto_continues.value())
    check("0 显示为「不限制」",
          dlg._max_tool_rounds.specialValueText() == "不限制")
except Exception as exc:  # pragma: no cover
    check("设置对话框可实例化", False, repr(exc))

# ── 汇总 ───────────────────────────────────────────────────────
print("\n" + "=" * 58)
print("PASS %d / %d" % (len(PASSES), len(PASSES) + len(FAILS)))
if FAILS:
    print("FAILED:")
    for f in FAILS:
        print("  -", f)
    sys.exit(1)
print("ALL GREEN")
