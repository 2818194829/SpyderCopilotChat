# -*- coding: utf-8 -*-
"""v0.8.5 回归：审批按钮"够得着"。

用户反馈「批准按钮有些时候按不到」。截图里能看清两种状态：
  * 卡片比对话流视图高，批准/拒绝落在折叠线以下，只剩状态行说「请到卡片中确认」
  * 卡片把内容顶得比视图宽，底部多出一条横向滚动条，进一步压掉高度

根因有三层，缺一层都还会复发：
  1) 差异区高度是常数（190），不随视图高度变化 → 卡片装不下
  2) 长路径把卡片最小宽度顶到视图外 → 横向溢出
  3) 唯一的操作入口长在卡片底部，只有滚到那里才点得到

覆盖：
  1) ElidedLabel 不再把文字宽度顶给布局，但保留完整文本与 tooltip
  2) 卡片按视图高度分配差异区，单条 / 多条都在预算内
  3) pending_stats / approve_safe / reject_all 的语义（危险项不被批量带走）
  4) 快捷条：待决时出现、文案随危险项变化、清空后自己消失
  5) 快捷条按钮真的能驱动卡片（不是只改 UI）
  6) 五条退出路径都收起快捷条（新会话 / 收尾 / 取消 / 转向）
  7) 对话流禁掉横向滚动
"""
import os
import sys
import tempfile

ROOT = (r"C:\Users\Mr_Yang\Documents\deepseek-harness"
        r"\default-workspace\spyder-copilot-chat")
sys.path.insert(0, ROOT)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from qtpy.QtCore import Qt                              # noqa: E402
from qtpy.QtWidgets import QApplication                 # noqa: E402

from spyder_copilot_chat.widgets.approval_card import (  # noqa: E402
    ApprovalCard, ElidedLabel, MAX_DIFF_HEIGHT, MIN_DIFF_HEIGHT,
)
from spyder_copilot_chat.widgets.chat_widget import (   # noqa: E402
    CopilotChatPanel,
)

APP = QApplication.instance() or QApplication([])
FAILS = []
PASSES = []


def check(name, cond, extra=""):
    if cond:
        PASSES.append(name)
    else:
        FAILS.append(f"{name}  {extra}")


# ── 1) 长路径不再顶宽布局 ───────────────────────────────────────
LONG = (r"D:\python_dev\pythonProject\我的开发\test\01 坦克大战"
        r"\_check_levels.py")
lbl = ElidedLabel("")
lbl.resize(180, 20)
lbl.setText(LONG)
check("省略标签最小宽度为 0", lbl.minimumSizeHint().width() == 0,
      f"min={lbl.minimumSizeHint().width()}")
check("省略标签保留完整文本", lbl.fullText() == LONG)
check("显示文本被省略", lbl.text() != LONG and lbl.text() != "", lbl.text()[:60])

# ── 构造工具调用 ────────────────────────────────────────────────
tmpdir = tempfile.mkdtemp()
target = os.path.join(tmpdir, "_check_levels.py")
with open(target, "w", encoding="utf-8") as f:
    f.write("\n".join("print(%d)" % i for i in range(1, 220)))


def call(name, **args):
    import json
    return {"function": {"name": name, "arguments": json.dumps(args)}}


SAFE = call("edit_file", path=target, start_line=1, end_line=2,
            new_content="print('patched')")
DANGER = call("delete_file", path=target)
CMD = call("run_command", command="python -m pytest -q")

card = ApprovalCard([SAFE])
check("单条卡片：待决计数", card.pending_stats() == (1, 0),
      f"{card.pending_stats()}")
check("安全项批准按钮可用", card._all_ok.isEnabled())

# ── 2) 视图高度分配差异区 ──────────────────────────────────────
def planned(c):
    """卡片按当前配置排完版之后的总高（布局自己的算术）。"""
    c.layout().activate()
    return c.fixed_height() + sum(r.sizeHint().height() for r in c.rows())


card.resize(420, card.height())
card.fit_to_height(320, 420)
check("单条卡片装进 320px",
      planned(card) <= 322, f"planned={planned(card)} diff={card.rows()[0].max_diff()}")
check("差异区不低于下限", card.rows()[0].max_diff() >= MIN_DIFF_HEIGHT,
      f"max_diff={card.rows()[0].max_diff()}")
check("差异区不高于上限", card.rows()[0].max_diff() <= MAX_DIFF_HEIGHT)
check("空间够时不用收起差异", not card.rows()[0].is_compact())

card.fit_to_height(1200, 420)
check("视图高时差异区用满上限",
      card.rows()[0].max_diff() == MAX_DIFF_HEIGHT,
      f"max_diff={card.rows()[0].max_diff()}")

# 两条操作挤在 320px 里：差异放不下就自动收起，按钮必须留下
c2 = ApprovalCard([SAFE, DANGER])
c2.resize(420, c2.height())
c2.fit_to_height(320, 320 and 420)
check("放不下时自动收起差异",
      all(r.is_compact() for r in c2.rows()),
      f"compact={[r.is_compact() for r in c2.rows()]}")
check("收起后按钮仍在视野内",
      planned(c2) <= 322, f"planned={planned(c2)}")

# 用户自己展开后，重新分配不能把他按回去
c2.rows()[0]._toggle.setChecked(True)
c2.rows()[0]._on_toggle(True)
c2.fit_to_height(320, 420)
check("用户展开后不再被自动收起", not c2.rows()[0].is_compact(),
      f"compact={c2.rows()[0].is_compact()}")

# 三条操作挤在 360px 里：装不下也要保证每条按钮可见（只牺牲底部批量按钮）
c3 = ApprovalCard([SAFE, CMD, call("create_file", path=target + ".new",
                                   content="x = 1\n")])
c3.resize(420, c3.height())
c3.fit_to_height(360, 420)
check("多条目放不下时全部收起",
      all(r.is_compact() for r in c3.rows()),
      f"compact={[r.is_compact() for r in c3.rows()]}")
check("多条目仍保留可读差异区",
      all(r.max_diff() >= MIN_DIFF_HEIGHT for r in c3.rows()),
      f"{[r.max_diff() for r in c3.rows()]}")

# ── 3) 危险项不被批量带走 ─────────────────────────────────────
mixed = ApprovalCard([SAFE, DANGER])
check("危险项被识别", mixed.pending_stats() == (2, 1), f"{mixed.pending_stats()}")
mixed.approve_safe()
check("批量只批准安全项", mixed.pending_stats() == (1, 1),
      f"{mixed.pending_stats()}")
check("危险项仍在等确认",
      mixed.first_pending_row() is not None
      and mixed.first_pending_row().is_dangerous())
mixed.reject_all()
check("批量拒绝清空待决", mixed.pending_stats() == (0, 0),
      f"{mixed.pending_stats()}")

# ── 4/5) 面板快捷条 ───────────────────────────────────────────
panel = CopilotChatPanel()
panel.resize(460, 520)
panel.show()
APP.processEvents()

check("初始不显示快捷条", panel._approval_bar.isHidden())

pending_card = ApprovalCard([SAFE])
panel._pending_card = pending_card
panel._set_awaiting_approval(True)
APP.processEvents()
check("挂起时快捷条出现", not panel._approval_bar.isHidden())
check("文案带数量", "待批准 1 项" in panel._approval_bar_text.text(),
      panel._approval_bar_text.text())
check("快照：批准按钮可见", not panel._bar_approve.isHidden())

# 点快捷条的「全部批准」，卡片真的要变成已决
pending_card.changed.connect(panel._refresh_approval_bar)
panel._bar_approve.click()
APP.processEvents()
check("快捷条能驱动卡片", pending_card.pending_stats() == (0, 0),
      f"{pending_card.pending_stats()}")
check("待决清空后快捷条收起", panel._approval_bar.isHidden())

# 只有危险项时：不给一键批准，只留「查看差异」
danger_card = ApprovalCard([DANGER])
panel._pending_card = danger_card
panel._set_awaiting_approval(True)
APP.processEvents()
check("仅危险项时隐藏批量批准", panel._bar_approve.isHidden())
check("仅危险项时提示逐项批准",
      "危险" in panel._approval_bar_text.text(),
      panel._approval_bar_text.text())
check("仅危险项时按钮改为查看差异",
      panel._bar_locate.text() == "查看差异", panel._bar_locate.text())
check("危险时文字换成危险色",
      panel._approval_bar_text.property("state") == "danger",
      str(panel._approval_bar_text.property("state")))

panel._bar_reject_all()
APP.processEvents()
check("快捷条拒绝全部生效", danger_card.pending_stats() == (0, 0),
      f"{danger_card.pending_stats()}")

# ── 6) 退出路径都收起 ─────────────────────────────────────────
card2 = ApprovalCard([SAFE])
panel._pending_card = card2
panel._set_awaiting_approval(True)
panel._finalize()
APP.processEvents()
check("收尾后快捷条收起", panel._approval_bar.isHidden())
check("收尾后挂起标记清理", panel._awaiting_approval is False)

panel._pending_card = ApprovalCard([SAFE])
panel._set_awaiting_approval(True)
panel._clear_ui()
APP.processEvents()
check("清空会话后快捷条收起", panel._approval_bar.isHidden())

# ── 7) 对话流不横向滚动 ───────────────────────────────────────
check("对话流禁用横向滚动条",
      panel._scroll.horizontalScrollBarPolicy() == Qt.ScrollBarAlwaysOff,
      str(panel._scroll.horizontalScrollBarPolicy()))
check("长路径不会撑宽卡片",
      ApprovalCard([SAFE]).minimumSizeHint().width() < 420,
      f"minW={ApprovalCard([SAFE]).minimumSizeHint().width()}")

print()
for name in PASSES:
    print(f"  PASS  {name}")
for name in FAILS:
    print(f"  FAIL  {name}")
print(f"\n{len(PASSES)}/{len(PASSES) + len(FAILS)} passed")
sys.exit(1 if FAILS else 0)
