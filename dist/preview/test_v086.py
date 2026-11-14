# -*- coding: utf-8 -*-
"""v0.8.6 回归：过程面板的计时必须停得下来。

用户反馈截图：答案已经写完了，头部还挂着「正在处理 1m38s」，秒数一直涨。

根因不是计时器坏了，是**那一轮的面板再也没人收尾**：审批通过后
``_on_approval_completed`` 另起了一条气泡，旧气泡（带着还在走表的面板）
被就地丢掉。它不属于 ``_current_bubble``，``_finalize`` 自然收不到它。

覆盖：
  1) 正常收尾：停表、收起、文案变「已完成」
  2) finish 幂等：重复调用不会把耗时越加越长
  3) 等审批时暂停：表停、文案改「等待批准」、耗时不再涨
  4) 批准后继续：从暂停处接着走，不把等待时间算进工时
  5) 审批通过另起气泡：旧面板必须在自己那一段结束时就停表
  6) 兜底扫描：任何被漏掉的面板，_finalize 都会停掉
  7) 清空会话 / 转向：不留还在走表的面板
  8) 渲染抛异常也必须停表（否则一个 Markdown 异常就能让计时永远涨）
"""
import os
import sys
import time

ROOT = (r"C:\Users\Mr_Yang\Documents\deepseek-harness"
        r"\default-workspace\spyder-copilot-chat")
sys.path.insert(0, ROOT)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from qtpy.QtWidgets import QApplication                 # noqa: E402

from spyder_copilot_chat.widgets import chat_widget     # noqa: E402
from spyder_copilot_chat.widgets.chat_widget import (   # noqa: E402
    CopilotChatPanel, ProcessPane,
)

APP = QApplication.instance() or QApplication([])
FAILS = []
PASSES = []


def check(name, cond, extra=""):
    if cond:
        PASSES.append(name)
    else:
        FAILS.append(f"{name}  {extra}")


def wait(ms):
    """让事件循环真正跑一会儿（定时器才会触发）。"""
    end = time.monotonic() + ms / 1000.0
    while time.monotonic() < end:
        APP.processEvents()
        time.sleep(0.01)


def new_panel(h=700):
    p = CopilotChatPanel()
    p.resize(460, h)
    p.show()
    APP.processEvents()
    return p


def call(name, **args):
    import json
    return {"function": {"name": name,
                         "arguments": json.dumps(args, ensure_ascii=False)}}


# ── 1) 正常收尾 ────────────────────────────────────────────────
panel = new_panel()
panel._add_msg("user", "介绍一下当前项目")
b = panel._add_msg("assistant", "")
panel._current_bubble = b
b.begin_process()
b.add_process("▸ **list_files** `.`")
wait(300)
check("运行中在走表", b._process._running and b._process._timer.isActive())
check("运行中文案", "正在处理" in b._process._btn.text(), b._process._btn.text())
panel._finalize()
wait(120)
check("收尾后停表", not b._process._running)
check("收尾后定时器已停", not b._process._timer.isActive())
check("收尾后文案变已完成", "已完成" in b._process._btn.text(),
      b._process._btn.text())
frozen = b._process.elapsed()
wait(300)
check("收尾后耗时不再增长", abs(b._process.elapsed() - frozen) < 0.05,
      f"{frozen} -> {b._process.elapsed()}")
check("收尾后自动收起", not b._process._btn.isChecked())
panel.close()
panel.deleteLater()

# ── 2) finish 幂等 ────────────────────────────────────────────
pane = ProcessPane()
pane.begin()
wait(200)
pane.finish()
first = pane.elapsed()
wait(150)
pane.finish()
pane.finish()
check("重复 finish 不叠加耗时", abs(pane.elapsed() - first) < 0.01,
      f"{first} -> {pane.elapsed()}")
check("重复 finish 后仍是停止态", not pane._running and not pane._timer.isActive())
pane.deleteLater()

# ── 3) 等审批时暂停 ───────────────────────────────────────────
panel = new_panel()
b = panel._add_msg("assistant", "")
panel._current_bubble = b
b.begin_process()
b.add_process("▸ **applyPatch** `a.py`")
wait(250)
before_pause = b._process.elapsed()
b.pause_process()
check("暂停后不再走表", not b._process._running)
check("暂停后定时器已停", not b._process._timer.isActive())
check("暂停文案", "等待批准" in b._process._btn.text(), b._process._btn.text())
wait(350)
check("暂停期间耗时不变",
      abs(b._process.elapsed() - before_pause) < 0.05,
      f"{before_pause} -> {b._process.elapsed()}")

# ── 4) 批准后继续 ────────────────────────────────────────────
b.resume_process()
check("恢复后继续走表", b._process._running and b._process._timer.isActive())
wait(250)
check("恢复后耗时从暂停处往后走",
      b._process.elapsed() > before_pause + 0.15,
      f"{before_pause} -> {b._process.elapsed()}")
check("等待的 350ms 没被算进去",
      b._process.elapsed() < before_pause + 0.6,
      f"elapsed={b._process.elapsed()}")
panel._finalize()
wait(60)
panel.close()
panel.deleteLater()

# ── 5) 审批通过另起气泡：旧面板必须停 ────────────────────────
panel = new_panel()
# 用户本机配置可能开了 auto_execute，会把卡片一插上就自动批准；
# 这里固定成「写操作必须人工确认」，并把工具执行换成空实现，
# 免得测试真的去调 API。
panel._config["auto_execute"] = False
panel._config["approval_policy"] = "writes"
panel._execute_approved_tools = lambda *a, **k: None
b = panel._add_msg("assistant", "")
panel._current_bubble = b
b.begin_process()
b.add_process("▸ **applyPatch** `a.py`")
panel._request_approval([call("edit_file", path="a.py", new_content="x=1\n")])
wait(200)
check("挂起时面板是暂停态", b._process._waiting, b._process._btn.text())
check("挂起时文案提示等待批准", "等待批准" in b._process._btn.text(),
      b._process._btn.text())

# 用户批准：走真实的 _on_approval_completed
panel._on_approval_completed([call("edit_file", path="a.py", new_content="x=1\n")])
wait(150)
check("换气泡后旧面板已停表", not b._process._running,
      b._process._btn.text())
check("换气泡后旧面板定时器已停", not b._process._timer.isActive())
check("换气泡后旧面板显示已完成", "已完成" in b._process._btn.text(),
      b._process._btn.text())
new_bubble = panel._current_bubble
check("新气泡是另一个对象", new_bubble is not b)
panel._finalize()
wait(60)
panel.close()
panel.deleteLater()

# ── 6) 兜底扫描：被漏掉的面板 ────────────────────────────────
panel = new_panel()
a1 = panel._add_msg("assistant", "")
panel._current_bubble = a1
a1.begin_process()
a1.add_process("▸ **read_file** `a.py`")
# 模拟某个分支换了气泡却没给旧面板收尾
a2 = panel._add_msg("assistant", "")
panel._current_bubble = a2
a2.begin_process()
a2.add_process("▸ **write_file** `b.py`")
wait(150)
check("孤儿面板确实还在走表", a1._process._running)
panel._finalize()
wait(80)
check("_finalize 会收掉所有孤儿面板", not a1._process._running,
      a1._process._btn.text())
check("孤儿面板定时器已停", not a1._process._timer.isActive())
check("当前气泡也停表", not a2._process._running)
check("兜底集合已清空",
      all(not p._running for p in list(ProcessPane._live)),
      f"{len(list(ProcessPane._live))} left")
panel.close()
panel.deleteLater()

# ── 7) 清空会话 / 转向 ──────────────────────────────────────
panel = new_panel()
b = panel._add_msg("assistant", "")
panel._current_bubble = b
b.begin_process()
b.add_process("▸ **read_file** `a.py`")
wait(120)
pane_ref = b._process
panel._clear_ui()
wait(80)
check("清空会话后不留走表面板", not pane_ref._running,
      pane_ref._btn.text())

panel = new_panel()
b = panel._add_msg("assistant", "")
panel._current_bubble = b
b.begin_process()
b.add_process("▸ **read_file** `a.py`")
wait(120)
panel._redirect = None  # 打断真实 worker 依赖：只验证旧面板停表
b.end_stream()
b.finish_process()
check("转向后旧面板停表", not b._process._running, b._process._btn.text())
panel.close()
panel.deleteLater()

# ── 8) 渲染异常也必须停表 ───────────────────────────────────
panel = new_panel()
b = panel._add_msg("assistant", "")
panel._current_bubble = b
b.begin_process()
b.add_process("▸ **read_file** `a.py`")
wait(120)

_orig_adjust = chat_widget.MessageBubble._adjust
chat_widget.MessageBubble._adjust = lambda self: (_ for _ in ()).throw(
    RuntimeError("boom"))
try:
    b.finish_process()
except RuntimeError:
    pass
finally:
    chat_widget.MessageBubble._adjust = _orig_adjust
check("渲染抛异常也停表", not b._process._running, b._process._btn.text())
check("渲染抛异常后定时器已停", not b._process._timer.isActive())
panel.close()
panel.deleteLater()

print()
for name in PASSES:
    print(f"  PASS  {name}")
for name in FAILS:
    print(f"  FAIL  {name}")
print(f"\n{len(PASSES)}/{len(PASSES) + len(FAILS)} passed")
sys.exit(1 if FAILS else 0)
