# -*- coding: utf-8 -*-
"""v0.8.18 回归：孤立代码围栏不再把整轮过程吞成一张 Code 卡片。

现场：用户贴出一段「Markdown 没渲染」的截图——界面上一张
``Code · 33 行`` 卡片，里面 ``#`` / ``**`` / 反引号全是原样字符，
而卡片上方两行工具轨迹却是正常的粗体。用户复制出来的纯文本里连
围栏都没有，因为消息气泡的「复制」优先提取代码块正文。

真因不是渲染器坏了，是**半截围栏**：

1. ``agentic_actions._handle_read_file`` 把文件内容包成
   ``[README.md — 94 行]\\n```\\n…\\n``` `` 返回；
2. 过程卡片只取前 3 个非空行做预览，恰好带上**开启**的 ```，
   闭合围栏还在文件末尾、早被截掉；
3. ``ProcessPane.append`` 用 ``\\n\\n`` 把每条过程拼成一份 markdown
   文档，这个没闭合的围栏一路吞到消息末尾——后面追加的标题、列表、
   检查点、工具轨迹全成了代码块正文。

修复分两层，缺一不可：

- **源头**：预览只是摘要，不该带围栏 → ``_strip_fence_lines`` 剥掉；
  只补闭合围栏的话，README 标题仍会被单独关进一张 1 行 Code 卡片。
- **兜底**：整段插入前用渲染器自己的 ``parse_blocks`` 判定末块是否
  未闭合，是就按原 marker 长度补上 → ``_balance_fences``。复用渲染器
  口径很关键：`````合适。````` 这类宽松闭合已被判 closed，凭正则数
  奇偶会误补出第二个围栏，反而把后面的正文再吞一次。

跑法：``python tests/test_v098.py``（也兼容 pytest）。offscreen 平台，
不需要真实字体与显示服务。
"""

import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from qtpy.QtWidgets import QApplication  # noqa: E402

_APP = QApplication.instance() or QApplication([])

from spyder_copilot_chat.widgets import chat_widget as CW  # noqa: E402
from spyder_copilot_chat.widgets import markdown_renderer as M  # noqa: E402

_results = []


def check(name, condition, extra=""):
    _results.append((name, bool(condition), extra))
    if not condition:
        print("FAIL  {0}  {1}".format(name, extra))


def eq(name, got, want):
    check(name, got == want, "got={0!r} want={1!r}".format(got, want))


def _source(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


def _kinds(text):
    return [b["kind"] for b in M.parse_blocks(text)]


def _html_flags(text):
    html = M.render_markdown(text)
    return {
        "code": "copilot-copy:" in html,
        "h1": "font-weight:600" in html,
        "bold": "<b>" in html,
        "list": "<ul" in html or "<li" in html,
    }


# 现场等价数据：read_file(README.md) 的返回 + 后续过程条目。
README_HEAD = """[README.md — 94 行]
```
# 超级玛丽（Super Mario Bros. 像素复刻）

经典 FC 横版过关游戏，纯 `pygame` 实现，**无需任何图片 / 音频素材**。

```bash
pip install pygame
```
"""

STEPS_AFTER = [
    "**✓ get_code_outline** → **mario.py** 的大纲:\n"
    "- 🔧 **get_font** (function, 行 94)\n"
    "- 🔧 **draw_text** (function, 行 112)",
    "检查点 `20261114-211836-5e7057`（可用工具栏 ↩ 撤销本步）",
    "读取项目文档和代码结构。",
    "▸ **run_command** `ls -la; wc -l mario.py`",
    "**✓ run_command** → $ ls -la; wc -l mario.py\nexit code: 255",
]


def _preview(text, limit=300):
    """复刻 chat_widget 里「前 3 个非空行」的预览逻辑。"""
    lines = [l for l in text.split("\n") if l.strip()][:3]
    return "\n".join(lines)[:limit]


def test_static_wiring():
    cw = _source("spyder_copilot_chat/widgets/chat_widget.py")
    md = _source("spyder_copilot_chat/widgets/markdown_renderer.py")
    check("预览摘要处调用了 _strip_fence_lines",
          "_strip_fence_lines(" in cw.split("preview_lines =")[1][:400]
          if "preview_lines =" in cw else False)
    check("整段插入兜底 _balance_fences 已定义",
          "def _balance_fences(text):" in cw)
    pane = cw.split("class ProcessPane(QFrame):")[1].split(
        "class MessageBubble(")[0] if "class MessageBubble(" in cw else cw
    check("ProcessPane.append 做围栏自平衡",
          "def append(self, md):" in pane
          and "_balance_fences(" in pane.split("def append(self, md):")[1][:500])
    bubble = cw.split("class MessageBubble(")[1].split(
        "class CopilotChatPanel(")[0] if "class CopilotChatPanel(" in cw else ""
    check("append_block 非 process 分支做围栏自平衡",
          "def append_block(self, text" in bubble
          and "_balance_fences(body)" in bubble)
    check("流式 append 不经过自平衡（不能破坏 delta 拼接）",
          "def append(self, text):" in bubble
          and "_balance_fences" not in bubble.split(
              "def append(self, text):")[1].split("def append_block")[0])
    check("parse_blocks 的 code 块带出 marker",
          '"marker": marker' in md)
    check("渲染器缓存签名不含 marker（加字段不影响缓存键）",
          'block.get("marker")' not in md.split("def _signature(")[1].split(
              "def render_blocks")[0])


def test_strip_fence_lines():
    f = CW._strip_fence_lines
    src = ("[README.md — 94 行]\n```\n# 标题\n```python\nx = 1\n"
           "~~~\n    ~~~\n正文 `inline`\n")
    got = f(src)
    check("剥掉三反引号围栏", "```" not in got)
    check("剥掉带语言标注的围栏", "```python" not in got)
    check("剥掉波浪线围栏", "~~~" not in got)
    check("剥掉带缩进的围栏", "    ~~~" not in got)
    check("保留文件头", "[README.md — 94 行]" in got)
    check("保留标题行", "# 标题" in got)
    check("保留行内代码", "`inline`" in got)
    check("保留正文", "正文" in got)
    eq("空文本返回空串", f(""), "")
    eq("None 返回空串", f(None), "")
    eq("纯围栏行返回空", f("```"), "")


def test_parse_blocks_marker():
    # marker 只含围栏字符本身，语言标注在单独的 lang 字段里
    for text, want in (("```\nx\n", "```"),
                       ("````python\nx\n", "````"),
                       ("~~~\nx\n", "~~~")):
        b = M.parse_blocks(text)[-1]
        check("未闭合围栏块 kind=code / {0}".format(want),
              b["kind"] == "code" and b["marker"] == want)
        check("未闭合围栏块 closed=False / {0}".format(want),
              b["closed"] is False)
    closed = M.parse_blocks("```\nx\n```")[-1]
    check("完整围栏块 closed=True", closed["closed"] is True)
    check("完整围栏块仍带 marker", closed["marker"] == "```")


def test_balance_fences_units():
    f = CW._balance_fences
    plain = "一段普通正文\n**粗体**"
    eq("纯文本一字不改", f(plain), plain)
    full = "**📄 a.py**\n\n```diff\n-old\n+new\n```"
    eq("完整 diff 卡片一字不改", f(full), full)
    eq("空文本不补围栏", f(""), "")

    for text, marker in (("```\nprint(1)\n", "```"),
                         ("````python\n```\nx\n", "````python"),
                         ("~~~\nx\n", "~~~")):
        out = f(text)
        check("补齐闭合围栏 / {0}".format(marker),
              out.rstrip("\n").endswith(marker[:3])
              and M.parse_blocks(out)[-1]["closed"] is True)

    # 渲染器的宽松闭合：```合适。 已 closed，绝不能再补第二个围栏
    loose = "```\nx\n```合适。\n**后面还有正文**"
    eq("宽松闭合不再补围栏", f(loose), loose)
    flags = _html_flags(f(loose) + "\n\n- 列表项")
    check("宽松闭合后列表正常渲染", flags["list"] and flags["bold"])

    # 四反引号包住三反引号：外面已闭合，不该误判成半截围栏
    nested = "````\n```\ncode\n```\n````"
    eq("嵌套围栏不误补", f(nested), nested)


def test_real_scenario_reproduced_and_fixed():
    """现场复现：修复前整段只剩一张未闭合 Code 卡片。"""
    preview = _preview(README_HEAD)
    check("预览里确实只剩一个开启围栏",
          preview.count("```") == 1)
    raw = "\n\n".join(
        ["▸ **get_code_outline** `filepath, max_depth`",
         "**✓ read_file** → " + preview] + STEPS_AFTER)
    blocks = M.parse_blocks(raw)
    check("修复前末块是 code", blocks[-1]["kind"] == "code")
    check("修复前围栏未闭合", blocks[-1]["closed"] is False)
    check("修复前代码块首行就是 README 标题",
          blocks[-1]["code"].split("\n")[0].startswith("# 超级玛丽"))
    before = _html_flags(raw)
    check("修复前出现 Code 卡片", before["code"])
    check("修复前标题没渲染", not before["h1"])
    check("修复前列表没渲染", not before["list"])

    # 修复后：源头剥围栏 + 每条插入前自平衡
    cleaned = CW._strip_fence_lines(_preview(README_HEAD))
    parts = ["▸ **get_code_outline** `filepath, max_depth`",
             "**✓ read_file** → " + cleaned] + STEPS_AFTER
    fixed = "\n\n".join(CW._balance_fences(p) for p in parts)
    kinds = _kinds(fixed)
    after = _html_flags(fixed)
    check("修复后不再有 Code 卡片", not after["code"])
    check("修复后标题渲染成标题", after["h1"])
    check("修复后粗体渲染", after["bold"])
    check("修复后列表渲染", after["list"])
    check("修复后含 heading 块", "heading" in kinds)
    check("修复后含 list 块", "list" in kinds)
    check("修复后末块是段落而非 code", kinds[-1] != "code")
    check("修复后检查点行仍在正文里", "20261114-211836-5e7057" in fixed)

    # 只做兜底、不剥围栏：后续能救回来，但标题仍被关进 1 行小卡片
    only_balance = "\n\n".join(
        [parts[0], "**✓ read_file** → " + CW._balance_fences(preview)]
        + STEPS_AFTER)
    ob = _html_flags(only_balance)
    check("仅兜底时后续列表恢复", ob["list"])
    check("仅兜底时仍残留小 Code 卡片（证明源头修复必要）", ob["code"])


def test_process_pane_integration():
    pane = CW.ProcessPane()
    try:
        pane.begin()
        pane.append("▸ **read_file** `filepath`")
        # 真实链路：预览在拼进气泡前已经剥掉围栏
        pane.append("**✓ read_file** → "
                    + CW._strip_fence_lines(_preview(README_HEAD)))
        for step in STEPS_AFTER:
            pane.append(step)
        html = M.render_markdown(pane._md)
        check("过程面板渲染后没有 Code 卡片", "copilot-copy:" not in html)
        check("过程面板渲染出粗体", "<b>" in html)
        check("过程面板渲染出列表", "<ul" in html or "<li" in html)
        eq("过程面板步骤数", pane._steps, 2 + len(STEPS_AFTER))
        check("过程面板末块不是未闭合 code",
              M.parse_blocks(pane._md)[-1]["kind"] != "code")
    finally:
        pane.finish()
        pane.close()

    # 兜底层单独验：就算上游忘了剥，也不能污染后续步骤
    raw = CW.ProcessPane()
    try:
        raw.begin()
        raw.append("**✓ read_file** → " + _preview(README_HEAD))
        raw.append("**后续步骤**\n\n- 列表项")
        blocks = M.parse_blocks(raw._md)
        codes = [b for b in blocks if b["kind"] == "code"]
        check("兜底后代码块已闭合", bool(codes) and codes[0]["closed"] is True)
        check("兜底后代码块只有 1 行（不再吞后续）",
              bool(codes) and len(codes[0]["code"].split("\n")) == 1)
        check("兜底后后续列表正常", "<ul" in M.render_markdown(raw._md)
              or "<li" in M.render_markdown(raw._md))
    finally:
        raw.finish()
        raw.close()


def test_message_bubble_append_block():
    b = CW.MessageBubble("assistant", "")
    try:
        b.append_block("**✓ read_file** → "
                       + CW._strip_fence_lines(_preview(README_HEAD)))
        b.append_block("## 结论\n\n- 第一条\n- 第二条")
        content = b.get_content()
        html = M.render_markdown(content)
        check("气泡正文不再被吞成 Code 卡片", "copilot-copy:" not in html)
        check("气泡正文列表渲染", "<ul" in html or "<li" in html)
        check("气泡正文含结论标题", "结论" in html)
        check("气泡末块不是未闭合 code",
              M.parse_blocks(content)[-1]["kind"] != "code")
        # process 分支走 ProcessPane，也要自平衡
        b.begin_process()
        b.append_block("**✓ run_command** → " + _preview(README_HEAD),
                       kind="process")
        b.append_block("**后续步骤** 正常显示", kind="process")
        check("过程面板步骤已记录", b._process._steps >= 2)
    finally:
        b.close()


def test_long_transcript_and_edges():
    parts = ["▸ **step {0}** 完成".format(i) for i in range(30)]
    parts.append("**✓ read_file** → "
                 + CW._strip_fence_lines(_preview(README_HEAD)))
    parts.append("**最后一步**：收尾")
    fixed = "\n\n".join(CW._balance_fences(p) for p in parts)
    flags = _html_flags(fixed)
    check("30+ 条过程后仍无 Code 卡片", not flags["code"])
    check("30+ 条过程后粗体正常", flags["bold"])
    check("最后一步没被吞", "收尾" in fixed)
    check("长过程末块是段落", _kinds(fixed)[-1] != "code")

    eq("只有开启围栏也能补", CW._balance_fences("```").strip(), "```\n```")
    check("波浪围栏补的是波浪",
          CW._balance_fences("~~~\nx").rstrip().endswith("~~~"))


def test_copy_button_still_prefers_code():
    """复制按钮继续优先提取代码块正文——用户截图里看不到围栏就是它。"""
    cw = _source("spyder_copilot_chat/widgets/chat_widget.py")
    seg = cw.split("def _on_copy_content(")[1].split("def _on_insert_content(")[0]
    check("复制仍用代码块提取正则", "re.findall" in seg and "```" in seg)
    check("没有代码块时回退整段正文", "text_to_copy = content" in seg)


def main():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
            except Exception as exc:  # noqa: BLE001
                check(name + " 未抛异常", False, repr(exc))
    passed = sum(1 for _, ok, _ in _results if ok)
    total = len(_results)
    failures = [(n, e) for n, ok, e in _results if not ok]
    print("\n{0}/{1} passed".format(passed, total))
    if failures:
        print("failures:")
        for name, extra in failures:
            print("  -", name, extra)
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
