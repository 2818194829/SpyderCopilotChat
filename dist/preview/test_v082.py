# -*- coding: utf-8 -*-
"""v0.8.2 回归：内联 diff 的失败必须可见（Diagnostics Edition）。

图一（真实 Spyder）里没有图二（离屏预览）的红绿效果，原因大概率是
某个环节静默 return 了 False，然后被上层 continue 掉——用户看到的就是
「什么都没发生」。这版把每条静默路径都改成带原因返回。

覆盖：
  1) stage() 的各条失败路径都会写 fail_reason
  2) ReviewHub.stage 失败时记进 last_failures
  3) _stage_editor_reviews 兼容 dict / int 两种 provider 返回值
  4) 失败原因会出现在对话气泡里
  5) provider 未注入时也有明确提示
  6) TextDecoration 路径（editor 带 decorations manager）真的产出装饰
  7) 装饰会立刻 flush 一次，不等 80ms 定时器
  8) 成功 staging 后 UI 不依赖 viewport Resize 才显示
"""
import os
import shutil
import sys
import tempfile

ROOT = r"C:\Users\Mr_Yang\Documents\deepseek-harness\default-workspace\spyder-copilot-chat"
sys.path.insert(0, ROOT)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from qtpy.QtWidgets import QApplication, QPlainTextEdit

from spyder_copilot_chat.widgets.chat_widget import CopilotChatPanel
from spyder_copilot_chat.widgets.editor_diff import InlineReview, ReviewHub

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
    _trim_patch = staticmethod(CopilotChatPanel._trim_patch)
    _emit_chat_diffs = CopilotChatPanel._emit_chat_diffs
    _stage_editor_reviews = CopilotChatPanel._stage_editor_reviews
    CHAT_DIFF_MAX_LINES = CopilotChatPanel.CHAT_DIFF_MAX_LINES
    CHAT_DIFF_MAX_FILES = CopilotChatPanel.CHAT_DIFF_MAX_FILES

    def __init__(self, config, provider=None):
        self._config = config
        self._review_provider = provider
        self._review_before = {}
        self._current_bubble = Bubble()

    def _get_project_root(self):
        return "."

    def _scroll_to_bottom(self):
        pass


class FakeManager(object):
    """冒充 Spyder 的 TextDecorationsManager。"""

    def __init__(self, editor):
        self.editor = editor
        self.keys = {}
        self.flushed = 0

    def add_key(self, key, decos):
        self.keys[key] = list(decos)
        return len(decos)

    def remove_key(self, key):
        self.keys.pop(key, None)

    def get(self, key, default=None):
        return self.keys.get(key, default)

    def _update(self):
        self.flushed += 1
        self.editor.setExtraSelections(list(self.keys.get("copilot_chat_inline_diff", [])))


OLD = """def total(items, tax):
    s = 0
    return s * (1 + tax)
"""

NEW = """def total(items, tax=0.0):
    s = 0.0
    return s * (1 + tax)
"""


def main():
    tmp = tempfile.mkdtemp(prefix="v082_")
    try:
        path = os.path.join(tmp, "cart.py")

        # ── 1) 失败原因 ──
        print("[1] stage 失败要带原因")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(NEW)
        ed = QPlainTextEdit()
        ed.setPlainText(NEW)
        ed.document().setModified(True)     # 模拟用户有未保存改动
        r = InlineReview(ed, path, OLD)
        ok = r.stage()
        check("有未保存改动时拒绝 staging", ok is False)
        check("并且给出原因", "未保存" in r.fail_reason, r.fail_reason)

        ed.document().setModified(False)
        r2 = InlineReview(ed, path, NEW)   # 新旧一致
        ok2 = r2.stage()
        check("内容一致时不 staging", ok2 is False)
        check("原因提到没有差异", "差异" in r2.fail_reason, r2.fail_reason)

        # ── 2) ReviewHub 记录失败 ──
        print("[2] ReviewHub 失败登记")
        hub = ReviewHub()
        got = hub.stage(ed, path, NEW)
        check("失败返回 None", got is None)
        check("失败进 last_failures", len(hub.last_failures) == 1,
              str(hub.last_failures))

        # ── 6/7) TextDecoration 路径 ──
        print("[6] TextDecoration 路径真的产出装饰")
        ed2 = QPlainTextEdit()
        ed2.setPlainText(NEW)
        mgr = FakeManager(ed2)
        ed2.decorations = mgr              # 让它走 Spyder 那条路
        r3 = InlineReview(ed2, path, OLD)
        ok3 = r3.stage()
        check("staging 成功", ok3 is True, r3.fail_reason)
        check("产出装饰条数 > 0（实际 %d）" % r3.deco_count,
              r3.deco_count > 0)
        check("走的是 TextDecoration 而非降级",
              r3.fallback_decorations is False)
        check("装饰已挂到 manager 上",
              len(mgr.keys.get("copilot_chat_inline_diff", [])) == r3.deco_count)
        check("立即 flush 过一次（不等 80ms 定时器）", mgr.flushed >= 1)
        check("装饰挂在了真实 extraSelections 上",
              len(ed2.extraSelections()) == r3.deco_count)

        # 撤销：装饰应被摘干净
        r3.revert_all()
        check("撤销后装饰清空", not mgr.keys.get("copilot_chat_inline_diff"))

        # ── 3/4/5) 聊天流兼容与提示 ──
        print("[3] provider 返回值兼容")
        p = FakePanel({"chat_diff_mode": "smart"}, provider=lambda items: 1)
        p._review_before = {path: OLD}
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(NEW)
        p._stage_editor_reviews([{"function": {
            "name": "edit_file",
            "arguments": '{"path": "%s"}' % path.replace("\\", "/")}}])
        check("int 返回值不再当成 dict 崩掉", True)
        check("成功时不留失败提示",
              "未生效" not in p._current_bubble.text)

        print("[4] 失败原因进气泡")
        p2 = FakePanel({"chat_diff_mode": "smart"},
                       provider=lambda items: {
                           "staged": 0,
                           "failures": [(path, "编辑器里还有未保存的修改")]})
        p2._review_before = {path: OLD}
        p2._stage_editor_reviews([{"function": {
            "name": "edit_file",
            "arguments": '{"path": "%s"}' % path.replace("\\", "/")}}])
        text = p2._current_bubble.text
        check("气泡里出现失败原因", "未保存" in text, text[:120])
        check("同时仍然画了红绿卡片", "```diff" in text)

        print("[5] provider 未注入")
        p3 = FakePanel({"chat_diff_mode": "smart"}, provider=None)
        p3._review_before = {path: OLD}
        p3._stage_editor_reviews([{"function": {
            "name": "edit_file",
            "arguments": '{"path": "%s"}' % path.replace("\\", "/")}}])
        text3 = p3._current_bubble.text
        check("提示插件没接入编辑器", "没有接入编辑器" in text3, text3[:120])
        check("仍然画了红绿卡片", "```diff" in text3)

        # ── 8) UI 不依赖 Resize ──
        print("[8] 工具栏不靠 viewport Resize 才出现")
        ed3 = QPlainTextEdit()
        ed3.setPlainText(NEW)
        r4 = InlineReview(ed3, path, OLD)
        check("staging 成功", r4.stage() is True)
        APP.processEvents()
        APP.processEvents()
        check("工具栏已显示", r4._bar is not None and not r4._bar.isHidden())
        check("左侧竖纹已显示",
              r4._stripe is not None and not r4._stripe.isHidden())

    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print("\n%d passed, %d failed" % (len(PASSES), len(FAILS)))
    return 1 if FAILS else 0


if __name__ == "__main__":
    APP = QApplication.instance() or QApplication([])
    sys.exit(main())
