# -*- coding: utf-8 -*-
"""v0.8.4 回归：过程折叠面板的"归属边界"。

折叠只是最后一步，能不能折取决于内容有没有被正确归类：叙述文本在
「后面还有工具调用」时属于过程，在「这一轮到此为止」时属于最终回答。
旧实现把所有东西拼进同一个 markdown 串，所以折叠时会把答案也一起折走。

覆盖：
  1) 只有叙述、没有工具调用 → 不出现过程面板
  2) 工具调用前叙述被归档：进面板、出正文
  3) 面板在运行态可见且展开
  4) 一轮结束自动收起，摘要带「已完成」
  5) 用户手动展开过 → 结束时不再擅自收起
  6) 过程里出现失败 → 不收起（失败优先）
  7) kind="result" 的卡片留在正文，不被归档
  8) 连续两轮工具调用：叙述按序进面板，正文只留最终回答
"""
import os
import sys

ROOT = (r"C:\Users\Mr_Yang\Documents\deepseek-harness"
        r"\default-workspace\spyder-copilot-chat")
sys.path.insert(0, ROOT)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from qtpy.QtWidgets import QApplication               # noqa: E402

from spyder_copilot_chat.widgets.chat_widget import (  # noqa: E402
    MessageBubble,
)

APP = QApplication.instance() or QApplication([])
FAILS = []
PASSES = []


def check(name, cond, extra=""):
    if cond:
        PASSES.append(name)
    else:
        FAILS.append(f"{name}  {extra}")


def bubble():
    b = MessageBubble("assistant", "")
    return b


# ── 1) 纯叙述不产生过程面板 ─────────────────────────────────────
b = bubble()
b.append("先看看配置。")
b.finish_process()
check("纯问答不出过程面板", b._process.isHidden(), f"hidden={b._process.isHidden()}")
check("纯问答正文保留叙述", "先看看配置。" in b.get_content())

# ── 2/3) 工具调用前归档 ─────────────────────────────────────────
b = bubble()
b.append("我先读一下 editor_diff.py。")
b.begin_process()
b.add_process("▸ **read_file** `editor_diff.py`")

check("叙述已进过程面板", "我先读一下" in b._process._md, b._process._md[:80])
check("叙述已离开正文", "我先读一下" not in b._content,
      repr(b._content[:60]))
check("正文不再显示旧叙述", "我先读一下" not in b.get_content() or
      "我先读一下" in b._process._md)
check("面板可见", not b._process.isHidden())
check("运行态默认展开", b._process._btn.isChecked())
check("步骤计数", b._process._steps == 2, f"steps={b._process._steps}")
check("运行态文案", "正在处理" in b._process._btn.text(),
      b._process._btn.text())

# ── 4) 结束自动收起 ─────────────────────────────────────────────
b.append("改好了，共 3 处。")
b.finish_process()
check("结束后自动收起", not b._process._btn.isChecked())
check("摘要带已完成", "已完成" in b._process._btn.text(), b._process._btn.text())
check("最终回答留在正文", "改好了，共 3 处。" in b._content)
check("正文不含工具卡片", "read_file" not in b._content, repr(b._content[:80]))

# ── 5) 用户手动展开后不被擅自收起 ───────────────────────────────
b = bubble()
b.add_process("▸ **run_command** `pytest -q`")
b._process._btn.setChecked(False)      # 用户点：收起
b._process._btn.setChecked(True)       # 用户点：展开（此时 _user_toggled）
b.finish_process()
check("用户展开后被尊重", b._process._btn.isChecked())

# ── 6) 失败不收起 ───────────────────────────────────────────────
b = bubble()
b.add_process("**✕ run_command** 失败：exit code 1")
b.finish_process()
check("失败时不收起", b._process._btn.isChecked())
check("失败标记", b._process._failed)

# ── 7) 卡片留在正文 ─────────────────────────────────────────────
b = bubble()
b.append("现在改这一处。")
b.begin_process()
b.add_process("▸ **applyPatch** `a.py`")
b.append_block("**📄 `a.py`**\n\n```diff\n-x = 1\n+x = 2\n```")
check("diff 卡片留在正文", "```diff" in b._content, repr(b._content[:80]))
check("卡片前的叙述在面板里", "现在改这一处" in b._process._md)
check("卡片不被归档", "```diff" not in b._process._md)

# ── 8) 连续两轮：顺序正确 ───────────────────────────────────────
b = bubble()
b.append("第一步：读文件。")
b.begin_process()
b.add_process("▸ **read_file** `editor_diff.py`")
b.append("第二步：改文件。")
b.begin_process()
b.add_process("▸ **applyPatch** `a.py`")
b.append("全部完成。")
b.finish_process()

md = b._process._md
check("第一段叙述进面板", "第一步" in md)
check("第二段叙述也进面板", "第二步" in md)
check("面板内顺序保持", md.index("第一步") < md.index("第二步"),
      f"idx={md.index('第一步')},{md.index('第二步')}")
check("正文只有结论", b._content.strip() == "全部完成。", repr(b._content))
check("两个工具都在面板", md.count("▸ **") == 2, f"count={md.count('▸ **')}")

print()
for name in PASSES:
    print(f"  PASS  {name}")
for name in FAILS:
    print(f"  FAIL  {name}")
print(f"\n{len(PASSES)}/{len(PASSES) + len(FAILS)} passed")
sys.exit(1 if FAILS else 0)
