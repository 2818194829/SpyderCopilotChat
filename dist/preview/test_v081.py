# -*- coding: utf-8 -*-
"""v0.8.1 回归：对话内红绿 diff 卡片（Chat Diff Edition）。

覆盖：
  1) 写盘前快照不再依赖编辑器 provider（chat_diff_mode 独立生效）
  2) smart 模式：编辑器全部接住 → 不重复刷卡片
  3) smart 模式：编辑器一个没接住 → 对话里补卡片
  4) always 模式：编辑器接住了也照样画
  5) off 模式：不产生任何卡片
  6) 卡片走 ```diff 围栏，能被 markdown_renderer 渲染成红/绿整行底色
  7) 超长补丁按 hunk 边界截断，并给出省略行数
  8) 文件数超限时给汇总提示
  9) 设置面板默认值与持久化
"""
import json
import os
import shutil
import sys
import tempfile

ROOT = r"C:\Users\Mr_Yang\Documents\deepseek-harness\default-workspace\spyder-copilot-chat"
sys.path.insert(0, ROOT)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from spyder_copilot_chat.widgets.chat_widget import CopilotChatPanel
from spyder_copilot_chat.widgets import diff_view as _diff
from spyder_copilot_chat.widgets import theme as _theme

FAILS = []
PASSES = []


def check(name, cond, extra=""):
    if cond:
        PASSES.append(name)
        print("  PASS  %s" % name)
    else:
        FAILS.append(name)
        print("  FAIL  %s  %s" % (name, extra))


class Bubble(object):
    def __init__(self):
        self.blocks = []

    def append_block(self, text):
        self.blocks.append(text)

    @property
    def text(self):
        return "\n\n".join(self.blocks)


class FakePanel(object):
    """只保留对话内 diff 所需的最小外壳。"""

    _trim_patch = staticmethod(CopilotChatPanel._trim_patch)
    _emit_chat_diffs = CopilotChatPanel._emit_chat_diffs
    _snapshot_review_targets = CopilotChatPanel._snapshot_review_targets
    CHAT_DIFF_MAX_LINES = CopilotChatPanel.CHAT_DIFF_MAX_LINES
    CHAT_DIFF_MAX_FILES = CopilotChatPanel.CHAT_DIFF_MAX_FILES

    def __init__(self, config, provider=None):
        self._config = config
        self._review_provider = provider
        self._review_before = {}
        self._current_bubble = Bubble()
        self._scrolled = 0

    def _get_project_root(self):
        return "."

    def _scroll_to_bottom(self):
        self._scrolled += 1


def write_call(path, content=""):
    return {"function": {"name": "write_to_file",
                         "arguments": json.dumps(
                             {"path": path, "content": content})}}


OLD = """def add(a, b):
    return a + b


def sub(a, b):
    return a - b
"""

NEW = """def add(a, b):
    return a + b


def sub(a, b):
    return a + b


def mul(a, b):
    return a * b
"""


def main():
    tmp = tempfile.mkdtemp(prefix="v081_")
    try:
        path = os.path.join(tmp, "mathx.py")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(OLD)

        # ── 1) 快照解耦 provider ──
        print("[1] 写盘前快照与 provider 解耦")
        p = FakePanel({"chat_diff_mode": "always", "inline_diff_enabled": True})
        p._snapshot_review_targets([write_call(path)], tmp)
        check("chat_diff_mode=always 且无 provider 时仍快照",
              p._review_before.get(path) == OLD)

        p2 = FakePanel({"chat_diff_mode": "off", "inline_diff_enabled": True})
        p2._snapshot_review_targets([write_call(path)], tmp)
        check("chat_diff_mode=off 且无 provider 时不快照",
              p2._review_before == {})

        # 写入新内容
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(NEW)

        # ── 2) smart：编辑器全接住 → 不重复 ──
        print("[2] smart 模式去重")
        p = FakePanel({"chat_diff_mode": "smart"})
        p._emit_chat_diffs([(path, OLD)], staged=1)
        check("编辑器全部接住时对话不重复画卡片", p._current_bubble.blocks == [])

        # ── 3) smart：编辑器没接住 → 补卡片 ──
        print("[3] smart 模式兜底")
        p = FakePanel({"chat_diff_mode": "smart"})
        p._emit_chat_diffs([(path, OLD)], staged=0)
        text = p._current_bubble.text
        check("编辑器没接住时对话补卡片", "```diff" in text)
        check("卡片标题带文件名", "mathx.py" in text)
        check("卡片含删除行", "\n-    return a - b" in text)
        check("卡片含新增行", "\n+def mul(a, b):" in text)
        check("补卡片后会滚动到底部", p._scrolled >= 1)

        # 4) always
        print("[4] always 模式")
        p = FakePanel({"chat_diff_mode": "always"})
        p._emit_chat_diffs([(path, OLD)], staged=1)
        check("always 模式编辑器接住也照画",
              "```diff" in p._current_bubble.text)

        # 5) off
        print("[5] off 模式")
        p = FakePanel({"chat_diff_mode": "off"})
        p._emit_chat_diffs([(path, OLD)], staged=0)
        check("off 模式不产生卡片", p._current_bubble.blocks == [])

        # 6) 真的渲染成红绿
        print("[6] 围栏 → 红绿整行底色")
        patch = _diff.unified(OLD, NEW, fromfile="mathx.py",
                              tofile="mathx.py", context=3)
        pal = _theme.html_colors()
        html, adds, dels = _diff.render_diff(OLD, NEW, pal, 13)
        check("行模型统计正确 (%d+/%d-)" % (adds, dels), adds == 5 and dels == 1)
        check("新增行用绿底", pal["diff_add_bg"] in html)
        check("删除行用红底", pal["diff_del_bg"] in html)
        check("补丁带 @@ 块头", "@@" in patch)
        check("删行整行 <td> 上底色（不是 inline span）",
              'background-color:%s' % pal["diff_del_bg"] in html)

        # 7) 截断
        print("[7] 超长补丁截断")
        long_old = "\n".join("x%d = %d" % (i, i) for i in range(400))
        long_new = "\n".join("x%d = %d" % (i, i + 1) for i in range(400))
        long_patch = _diff.unified(long_old, long_new)
        body, cut, hidden = CopilotChatPanel._trim_patch(long_patch,
                                                        90)
        check("超长补丁被截断", cut and hidden > 0)
        check("截断后行数受限 (%d)" % len(body.split("\n")),
              len(body.split("\n")) <= 90)
        short_body, short_cut, _ = CopilotChatPanel._trim_patch(
            patch, 90)
        check("短补丁不截断", not short_cut and short_body == patch)

        # 8) 多文件
        print("[8] 多文件汇总")
        items = []
        for i in range(8):
            fp = os.path.join(tmp, "f%d.py" % i)
            with open(fp, "w", encoding="utf-8") as fh:
                fh.write("a = 1\n")
            items.append((fp, "a = 2\n"))
        p = FakePanel({"chat_diff_mode": "always"})
        p._emit_chat_diffs(items, staged=0)
        check("只展开前 6 个文件",
              p._current_bubble.text.count("```diff") == 6)
        check("其余文件有汇总提示",
              "另有 2 个文件" in p._current_bubble.text)

        # 9) 设置项
        print("[9] 设置面板")
        from spyder_copilot_chat.widgets.settings_dialog import (
            get_provider_config)
        cfg = get_provider_config()
        check("默认 chat_diff_mode=smart",
              cfg.get("chat_diff_mode") == "smart")

    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print("\n%d passed, %d failed" % (len(PASSES), len(FAILS)))
    return 1 if FAILS else 0


if __name__ == "__main__":
    from qtpy.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    sys.exit(main())
