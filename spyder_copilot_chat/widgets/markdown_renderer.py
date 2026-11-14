# -*- coding: utf-8 -*-
"""Markdown → Qt 富文本渲染器（0.7.0 Native Render）

重构参考 DeepSeek Harness 聊天面板的渲染栈（详见
``docs/harness-rendering-notes.md``）：

1. **块级解析与行内解析分离** —— 先按块识别（fence / 标题 / 列表 / 表格 /
   引用 / 段落），块内部才跑行内正则。旧实现是逐行正则，代码块要靠占位符
   绕开，嵌套列表、任务列表、表格对齐都做不了。
2. **代码块卡片化** —— 表头行（语言 · 行数）+ 右侧「复制」链接，
   `<a href="copilot-copy:N">` 由 MessageView 拦截，是真实可点的交互。
3. **排版 token 对齐 harness** —— 正文行高 1.62、标题与正文同级加粗（不再
   画下划线大标题）、段间距 8px、表格 5/10 内边距 + 表头底色、引用左轴 3px。
4. **高亮结果缓存** —— 按 (主题, 底色, 语言, 源码) 缓存 Pygments 输出，
   流式增量渲染时已完成的代码块零重复计算。
5. **安全** —— 所有文本先 HTML 转义再套样式，模型输出的原始 HTML 一律不执行
   （等价于 harness 的 rehype-sanitize 白名单）。
"""

import html as _html
import re

from pygments import highlight
from pygments.formatters import HtmlFormatter
from pygments.lexers import (
    get_lexer_by_name,
    guess_lexer,
    TextLexer,
)
from pygments.styles import get_style_by_name
from pygments.util import ClassNotFound

from . import theme
from . import math_render as _math
from . import diff_view as _diff

MONO_FONT = ("Consolas,'Cascadia Mono','DejaVu Sans Mono',"
             "Monaco,'Courier New',monospace")

# ── 配色 ────────────────────────────────────────────────────────────
def _style(name, fallback):
    try:
        return get_style_by_name(name)
    except Exception:
        return fallback


_DARK_STYLE = _style("monokai", "monokai")
_LIGHT_STYLE = _style("friendly", "default")
_FMT_CACHE = {}


def _formatter():
    t = theme.current_theme()
    c = theme.html_colors()
    key = (t.is_dark, c["code_bg"])
    if key not in _FMT_CACHE:
        _FMT_CACHE[key] = HtmlFormatter(
            style=_DARK_STYLE if t.is_dark else _LIGHT_STYLE,
            noclasses=True,
            style_background=c["code_bg"],
        )
    return _FMT_CACHE[key]


# ── 语言别名 ────────────────────────────────────────────────────────
LANG_ALIASES = {
    "": "text",
    "py": "python",
    "js": "javascript",
    "ts": "typescript",
    "tsx": "tsx",
    "jsx": "jsx",
    "html": "html",
    "css": "css",
    "scss": "scss",
    "json": "json",
    "xml": "xml",
    "yaml": "yaml",
    "yml": "yaml",
    "toml": "toml",
    "md": "markdown",
    "markdown": "markdown",
    "sh": "bash",
    "bash": "bash",
    "shell": "bash",
    "bat": "batch",
    "cmd": "batch",
    "ps1": "powershell",
    "powershell": "powershell",
    "sql": "sql",
    "r": "r",
    "rb": "ruby",
    "ruby": "ruby",
    "go": "go",
    "golang": "go",
    "rs": "rust",
    "rust": "rust",
    "java": "java",
    "c": "c",
    "cpp": "cpp",
    "c++": "cpp",
    "cs": "csharp",
    "csharp": "csharp",
    "swift": "swift",
    "kt": "kotlin",
    "kotlin": "kotlin",
    "php": "php",
    "dockerfile": "docker",
    "docker": "docker",
    "ini": "ini",
    "cfg": "ini",
    "conf": "ini",
    "diff": "diff",
    "patch": "diff",
    "makefile": "makefile",
    "cmake": "cmake",
    "nginx": "nginx",
    "text": "text",
}

_DISPLAY = {
    "python": "Python", "javascript": "JavaScript", "typescript": "TypeScript",
    "html": "HTML", "css": "CSS", "json": "JSON", "xml": "XML",
    "yaml": "YAML", "toml": "TOML", "bash": "Bash", "shell": "Shell",
    "powershell": "PowerShell", "sql": "SQL", "r": "R", "go": "Go",
    "rust": "Rust", "java": "Java", "c": "C", "cpp": "C++", "csharp": "C#",
    "swift": "Swift", "kotlin": "Kotlin", "php": "PHP", "ruby": "Ruby",
    "markdown": "Markdown", "docker": "Dockerfile", "diff": "Diff",
    "text": "Text", "tsx": "TSX", "jsx": "JSX", "scss": "SCSS",
    "batch": "Batch", "cmake": "CMake", "makefile": "Makefile",
}


def _resolve_lang(lang_tag):
    key = (lang_tag or "").strip().lower()
    return LANG_ALIASES.get(key, key or "text")


def _detect_lang_display(lang_tag):
    key = (lang_tag or "").strip().lower()
    if not key:
        return "Code"
    return _DISPLAY.get(_resolve_lang(key), lang_tag)


# ── 语法高亮（带缓存）────────────────────────────────────────────────
_HL_CACHE = {}
_HL_LIMIT = 200


def _highlight_code(code, lang):
    """Pygments 高亮；结果按 (主题, 底色, 语言, 源码) 缓存。"""
    t = theme.current_theme()
    c = theme.html_colors()
    key = (t.is_dark, c["code_bg"], lang, code)
    cached = _HL_CACHE.get(key)
    if cached is not None:
        return cached

    lexer_name = _resolve_lang(lang)
    try:
        lexer = get_lexer_by_name(lexer_name, stripall=True)
    except ClassNotFound:
        try:
            lexer = guess_lexer(code)
        except ClassNotFound:
            lexer = TextLexer()

    try:
        out = highlight(code, lexer, _formatter())
        # Pygments 会套一层 <div class="highlight" style="background:…">。
        # Qt 富文本会把这一层的背景按文本块分别绘制，长代码块出现横向色带，
        # 所以把外层 div 摘掉，背景交给外层表格单元格统一绘制。
        out = re.sub(r'^\s*<div class="highlight"[^>]*>(.*)</div>\s*$',
                     lambda m: m.group(1), out, flags=re.S)
    except Exception:
        out = ('<pre style="margin:0;white-space:pre-wrap;">'
               '<code>%s</code></pre>' % _html.escape(code))

    if len(_HL_CACHE) > _HL_LIMIT:
        _HL_CACHE.clear()
    _HL_CACHE[key] = out
    return out


def _style_pre(html_text, size):
    """代码块折行 + 字号收敛。

    Pygments 的 <pre> 是 ``white-space: pre``，长行会把气泡撑宽；窄侧栏里
    直接溢出。这里统一改成 pre-wrap，并把字号压到正文下一档。

    另外必须把 <pre> 上的 ``background`` 与 ``line-height`` 删干净：
    Qt 对块级元素上的 ``background: transparent`` 不按"透明"处理，而是当成
    白色整块刷上去——实测代码卡片的单元格底色被盖成 #ffffff，暗色主题下
    就成了"白底 + monokai 的浅色字"，几乎看不见。``line-height`` 同理，
    Pygments 的 125% 在前，Qt 取第一个值，我们的行距设置等于没写。
    """
    extra = ("white-space:pre-wrap;word-break:break-word;"
             "font-family:%s;font-size:%dpx;line-height:1.55;"
             "margin:0;" % (MONO_FONT, size))

    def _fix(m):
        style = m.group(2) or ""
        style = re.sub(r"(?:background|background-color)\s*:[^;]*;?",
                       "", style, flags=re.I)
        style = re.sub(r"line-height\s*:[^;]*;?", "", style, flags=re.I)
        if "white-space" in style:
            return m.group(1) + style + '"'
        sep = "" if (style.endswith(";") or not style) else ";"
        return m.group(1) + style + sep + extra + '"'

    return re.sub(r'(<pre[^>]*style=")([^"]*)"', _fix, html_text)


# ── 行内渲染 ────────────────────────────────────────────────────────
_STASH_RE = re.compile("\x00(\\d+)\x00")
_CODE_SPAN_RE = re.compile(r"`([^`\n]+)`")
_MD_LINK_RE = re.compile(r"\[([^\]\n]+)\]\(([^)\s]+)\)")
_BARE_URL_RE = re.compile(r"(?<![\w/\"'=])(https?://[^\s<>\"'\x00]+)")
_BOLD_RE = re.compile(r"\*\*(?=\S)(.+?)\*\*")
_STRIKE_RE = re.compile(r"~~(?=\S)(.+?)~~")
_ITALIC_RE = re.compile(r"(?<![\w*])\*(?=\S)([^*\n]+?)\*(?![\w*])")

# ── 数学公式 ────────────────────────────────────────────────────────
# 行内 $...$ 首尾都要求"非空白、非 $"，避免把 "$100 和 $200" 这种金额当公式。
_MATH_INLINE_RE = re.compile(
    r"(?<!\\)\$(?!\$)([^\s$](?:[^$\n]{0,297}[^\s$])?)(?<!\\)\$(?!\$)")
# 同一行里写开的 $$...$$（块级公式的正规形式由 parse_blocks 处理）
_MATH_ROW_RE = re.compile(r"(?<!\\)\$\$(.+?)(?<!\\)\$\$", re.S)

MATH_INLINE_MAX_WIDTH = 320
MATH_BLOCK_MAX_WIDTH = 460
# mathtext 的字面高度比同号中文小一截（数学字体的 em 与字形比例不同），
# 行内 +1、块级 +4 才能在正文旁边看起来"一样大、还略突出"
MATH_INLINE_BUMP = 1
MATH_BLOCK_BUMP = 4


def _stash_math(text, size, pal, keep, display=False):
    """把公式换成图片占位符。

    必须在 HTML 转义**之前**做：``\\frac{a}{b}``、``x < y`` 里的字符一旦被
    转义成实体，mathtext 就不认了。渲染失败时原样返回，占位符之外的部分
    仍会正常转义，用户看到的是公式源码而不是空白。
    """
    pattern = _MATH_ROW_RE if display else _MATH_INLINE_RE
    bump = MATH_BLOCK_BUMP if display else MATH_INLINE_BUMP

    def _sub(m):
        snippet = _math.img_html(
            m.group(1),
            size + bump,
            pal["text"],
            display=display,
            max_width=MATH_BLOCK_MAX_WIDTH if display else MATH_INLINE_MAX_WIDTH,
        )
        return m.group(0) if snippet is None else keep(snippet)

    return pattern.sub(_sub, text)


def _inline(raw):
    """单行 Markdown → 内联 HTML（先摘公式/代码，再转义，最后套样式）。"""
    pal = theme.html_colors()
    size = max(11, theme.current_theme().body_font)
    if not raw:
        return ""
    stash = []

    def keep(fragment):
        stash.append(fragment)
        return "\x00%d\x00" % (len(stash) - 1)

    # 0) 公式：先摘出来，别让转义和强调正则碰到 LaTeX 源码
    text = _stash_math(raw, size, pal, keep, display=True)
    text = _stash_math(text, size, pal, keep)
    text = _html.escape(text, quote=False)
    if not text:
        return ""

    # 1) 行内代码：先摘出来，避免内部被后面的强调/链接正则改写
    text = _CODE_SPAN_RE.sub(
        lambda m: keep(
            '<span style="background-color:%s;color:%s;'
            'font-family:%s;font-size:%dpx;">&nbsp;%s&nbsp;</span>'
            % (pal["surface_alt"], pal["text"], MONO_FONT,
               max(10, size - 1), m.group(1))),
        text)

    # 2) 显式链接 [文本](地址)
    text = _MD_LINK_RE.sub(
        lambda m: keep('<a href="%s" style="color:%s;text-decoration:none;">%s</a>'
                       % (m.group(2), pal["info"], m.group(1))),
        text)

    # 3) 裸链接自动成链（harness 的 remark-linkify-it 等价能力）
    text = _BARE_URL_RE.sub(
        lambda m: keep('<a href="%s" style="color:%s;text-decoration:none;">%s</a>'
                       % (m.group(1), pal["info"], m.group(1))),
        text)

    # 4) 强调
    text = _BOLD_RE.sub(r"<b>\1</b>", text)
    text = _STRIKE_RE.sub(
        lambda m: keep('<span style="text-decoration:line-through;'
                       'color:%s;">%s</span>' % (pal["text_muted"], m.group(1))),
        text)
    text = _ITALIC_RE.sub(r"<i>\1</i>", text)

    return _STASH_RE.sub(lambda m: stash[int(m.group(1))], text)


# ── 块级解析 ────────────────────────────────────────────────────────
_FENCE_OPEN_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})[ \t]*([\w+#.\-]*)[ \t]*$")
_HEADING_RE = re.compile(r"^ {0,3}(#{1,6})\s+(.*?)\s*#*\s*$")
_HR_RE = re.compile(r"^ {0,3}([-*_])(?:\s*\1){2,}\s*$")
_LIST_RE = re.compile(r"^(\s*)([-*+]|\d+[.)])\s+(.*)$")
_TABLE_SEP_RE = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)+\|?\s*$")
_TASK_RE = re.compile(r"^\[( |x|X)\]\s+(.*)$")


def _fence_close(line, marker):
    stripped = line.strip()
    return (len(stripped) >= len(marker)
            and set(stripped) == {marker[0]})


# 围栏行后面粘着的这一小段若是纯 ASCII token，更可能是语言标注
# （模型把示例代码写进代码块时常见），此时按 CommonMark 处理：不算闭合。
_LANG_TOKEN_RE = re.compile(r"[A-Za-z0-9_+#.\-]{1,20}")


def _fence_break(line, marker):
    """这一行是否结束围栏；返回 ``(是否结束, 粘在围栏后的正文)``。

    CommonMark 要求闭合围栏"整行只有反引号"。但拼接出来的文本经常把围栏
    和下一句话粘在一行：工具卡片收尾的 ``` 后面紧跟正文 delta，就成了
    "```合适。"。严格实现会让这个围栏一路吞到消息末尾——后面的标题、
    表格、粗体全部变成代码块的正文，用户看到的就是"Markdown 不渲染"。
    所以放宽：行首有足够长的同一字符就闭合，多余的正文补回文档流继续解析。
    """
    stripped = line.strip()
    mark = marker[0]
    if not stripped or stripped[0] != mark:
        return False, ""
    run = len(stripped) - len(stripped.lstrip(mark))
    if run < len(marker):
        return False, ""
    extra = stripped[run:].strip()
    if extra and _LANG_TOKEN_RE.fullmatch(extra):
        return False, ""
    return True, extra


def _starts_block(lines, i):
    line = lines[i]
    if not line.strip():
        return True
    if _FENCE_OPEN_RE.match(line) or _HEADING_RE.match(line):
        return True
    if _HR_RE.match(line) or _LIST_RE.match(line):
        return True
    if line.lstrip().startswith(">"):
        return True
    if line.strip().startswith("$$"):
        return True
    if "|" in line and i + 1 < len(lines) and _TABLE_SEP_RE.match(lines[i + 1]):
        return True
    return False


def _split_table_row(line):
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|"):
        s = s[:-1]
    return [c.strip() for c in s.split("|")]


def parse_blocks(text):
    """把 Markdown 文本切成块。

    每块是 dict：``kind`` / ``closed`` 以及各类型的载荷。
    ``closed`` 表示这一块之后还有内容（已被空行或新块终结），流式渲染
    只允许把 closed 的块"提交"进文档，最后一块始终可重绘。
    """
    lines = text.split("\n")
    n = len(lines)
    blocks = []
    i = 0
    while i < n:
        line = lines[i]

        if not line.strip():
            i += 1
            continue

        # 块级公式 $$...$$（可独占一行，也可跨行；流式时未闭合的算尾块）
        if line.strip().startswith("$$"):
            stripped = line.strip()
            inner = stripped[2:]
            if inner.endswith("$$") and len(inner) >= 2:
                blocks.append({"kind": "math", "tex": inner[:-2].strip(),
                               "closed": i + 1 < n})
                i += 1
                continue
            j = i + 1
            body = [inner] if inner.strip() else []
            closed = False
            while j < n:
                seg = lines[j].strip()
                if seg.endswith("$$"):
                    body.append(seg[:-2])
                    closed = True
                    j += 1
                    break
                body.append(lines[j])
                j += 1
            blocks.append({"kind": "math", "tex": "\n".join(body).strip(),
                           "closed": closed})
            i = j
            continue

        # 围栏代码块（未闭合时视为"进行中"，整块留作尾部重绘）
        m = _FENCE_OPEN_RE.match(line)
        if m:
            marker, lang = m.group(1), (m.group(2) or "")
            j = i + 1
            body = []
            closed = False
            while j < n:
                hit, extra = _fence_break(lines[j], marker)
                if hit:
                    closed = True
                    j += 1
                    if extra:
                        # 粘在围栏后的正文塞回文档流，交给后面的块继续解析
                        lines.insert(j, extra)
                        n = len(lines)
                    break
                body.append(lines[j])
                j += 1
            # marker 随块带出：整段插入前的围栏自平衡要按同样的长度补闭合，
            # 不能凭空猜 ``` ——四反引号围住三反引号时长度不对就合不上。
            blocks.append({"kind": "code", "lang": lang, "marker": marker,
                           "code": "\n".join(body), "closed": closed})
            i = j
            continue

        # 标题
        m = _HEADING_RE.match(line)
        if m:
            blocks.append({"kind": "heading", "level": len(m.group(1)),
                           "text": m.group(2), "closed": i + 1 < n})
            i += 1
            continue

        # 分隔线
        if _HR_RE.match(line) and not _LIST_RE.match(line):
            blocks.append({"kind": "hr", "closed": i + 1 < n})
            i += 1
            continue

        # 表格
        if ("|" in line and i + 1 < n
                and _TABLE_SEP_RE.match(lines[i + 1])):
            head = _split_table_row(line)
            aligns = []
            for cell in _split_table_row(lines[i + 1]):
                left, right = cell.startswith(":"), cell.endswith(":")
                aligns.append("center" if left and right
                              else "right" if right else
                              "center" if left else "left")
            j = i + 2
            rows = []
            while j < n and lines[j].strip() and "|" in lines[j]:
                rows.append(_split_table_row(lines[j]))
                j += 1
            blocks.append({"kind": "table", "head": head, "rows": rows,
                           "aligns": aligns, "closed": j < n})
            i = j
            continue

        # 引用
        if line.lstrip().startswith(">"):
            j = i
            body = []
            while j < n and lines[j].lstrip().startswith(">"):
                body.append(re.sub(r"^\s*>\s?", "", lines[j]))
                j += 1
            blocks.append({"kind": "quote", "lines": body, "closed": j < n})
            i = j
            continue

        # 列表（含嵌套与任务列表）
        if _LIST_RE.match(line):
            j = i
            items = []
            while j < n:
                lm = _LIST_RE.match(lines[j])
                if not lm:
                    if not lines[j].strip():
                        break
                    # 续行：并入上一条
                    if items:
                        items[-1]["text"] += " " + lines[j].strip()
                        j += 1
                        continue
                    break
                indent = len(lm.group(1).replace("\t", "    "))
                marker = lm.group(2)
                text = lm.group(3)
                task = None
                tm = _TASK_RE.match(text)
                if tm:
                    task = tm.group(1).lower() == "x"
                    text = tm.group(2)
                items.append({
                    "level": indent // 2,
                    "ordered": marker[0].isdigit(),
                    "text": text,
                    "task": task,
                })
                j += 1
            blocks.append({"kind": "list", "items": items, "closed": j < n})
            i = j
            continue

        # 段落
        j = i
        body = []
        while j < n and lines[j].strip() and not _starts_block(lines, j):
            body.append(lines[j].strip())
            j += 1
        if not body:                      # 兜底，避免死循环
            body = [line.strip()]
            j = i + 1
        blocks.append({"kind": "paragraph", "lines": body, "closed": j < n})
        i = j

    return blocks


# ── 块级渲染 ────────────────────────────────────────────────────────
def _para_gap(size):
    return max(6, int(round(size * 0.58)))


def _render_list_items(nodes, pal, size):
    if not nodes:
        return ""
    tag = "ol" if nodes[0]["ordered"] else "ul"
    # 字号写在列表容器上，<li> 继承；不写会掉回文档默认字体（同段落）
    out = [f'<{tag} style="margin:2px 0 {_para_gap(size)}px 0;'
           f'font-size:{size}px;line-height:1.62;">']
    for nd in nodes:
        body = _inline(nd["text"])
        if nd.get("task") is not None:
            # 不用 ☑ / ☐：这两码位在中文等宽字体里经常缺字，渲染成豆腐块。
            # 等宽 [x] / [ ] 在任意字体下都能稳定出字，也更像代码里的勾选。
            box = "[x]" if nd["task"] else "[ ]"
            color = pal["success"] if nd["task"] else pal["text_faint"]
            body = (f'<span style="color:{color};font-family:{MONO_FONT};'
                    f'font-size:{max(10, size - 1)}px;">{box}</span>&nbsp;{body}')
        out.append(f'<li style="margin:1px 0;">{body}'
                   f'{_render_list_items(nd["children"], pal, size)}</li>')
    out.append(f"</{tag}>")
    return "".join(out)


def _render_list(block, pal, size):
    root = {"level": -1, "children": []}
    stack = [root]
    for item in block["items"]:
        node = dict(item)
        node["children"] = []
        while len(stack) > 1 and stack[-1]["level"] >= node["level"]:
            stack.pop()
        stack[-1]["children"].append(node)
        stack.append(node)
    return _render_list_items(root["children"], pal, size)


def _render_table(block, pal, size):
    border = pal["border"]
    cell = (f"padding:5px 10px;border:1px solid {border};"
            f"font-size:{max(11, size - 1)}px;line-height:1.5;")
    out = ['<table width="100%" cellspacing="0" cellpadding="0" '
           'style="border-collapse:collapse;margin:4px 0 10px 0;">']
    head_cells = []
    for idx, cell_text in enumerate(block["head"]):
        align = block["aligns"][idx] if idx < len(block["aligns"]) else "left"
        head_cells.append(
            f'<th style="{cell}background:{pal["surface_alt"]};'
            f'text-align:{align};font-weight:600;">{_inline(cell_text)}</th>')
    out.append("<tr>%s</tr>" % "".join(head_cells))
    for row in block["rows"]:
        cells = []
        for idx, cell_text in enumerate(row):
            align = block["aligns"][idx] if idx < len(block["aligns"]) else "left"
            cells.append(f'<td style="{cell}text-align:{align};">'
                         f'{_inline(cell_text)}</td>')
        out.append("<tr>%s</tr>" % "".join(cells))
    out.append("</table>")
    return "".join(out)


DIFF_MAX_LINES = 400


def _render_code(block, pal, size, index):
    lang = block.get("lang") or ""
    code = block.get("code") or ""
    # ```diff / ```patch 一定走差异视图；没标语言的补丁（模型常直接甩
    # @@ 块）靠 looks_like_diff 认，认不出来就还是普通代码块。
    is_diff = (_resolve_lang(lang) == "diff"
               or (not lang.strip() and _diff.looks_like_diff(code)))

    lines = code.count("\n") + (1 if code else 0)
    small = max(10, size - 3)
    head_style = (f'background:{pal["code_header"]};'
                  f'border-bottom:1px solid {pal["border"]};'
                  f'padding:3px 9px;font-size:{small}px;')
    copy = (f'<a href="copilot-copy:{index}" style="color:{pal["text_faint"]};'
            f'text-decoration:none;">复制</a>')

    if is_diff:
        doc = _diff.parse(code)
        label = _detect_lang_display(lang) if lang.strip() else "Diff"
        counts = ""
        if doc["adds"] or doc["dels"]:
            counts = ('&nbsp;&nbsp;<span style="color:%s;">+%d</span>'
                      '&nbsp;<span style="color:%s;">−%d</span>'
                      % (pal["diff_add"], doc["adds"],
                         pal["diff_del"], doc["dels"]))
        # 行数按"实际画出来的行"算：---/+++ 与 V4A 的 Begin/End 都被
        # 吃掉了，用原文行数会虚高。
        shown = len(doc["lines"])
        if shown:
            counts += "&nbsp;&nbsp;·&nbsp;&nbsp;%d 行" % shown
        # 行直接摊在卡片表格里（4 列：旧号/新号/标记/正文）。套一层内表格
        # 的话 Qt 不会把 width="100%" 传导下去，整行底色会缩到内容宽度，
        # 看起来像荧光笔划过而不是"这一行被标红"。
        rows, _ = _diff.render_rows(doc, pal, max(11, size - 3),
                                    DIFF_MAX_LINES, wrap=False)
        return (
            f'<table width="100%" cellspacing="0" cellpadding="0" '
            f'style="border:1px solid {pal["border"]};margin:6px 0 10px 0;">'
            f'<tr><td colspan="4" style="{head_style}'
            f'color:{pal["text_faint"]};">{label}{counts}</td>'
            f'<td width="46" align="right" style="{head_style}'
            f'padding-right:6px;">{copy}</td></tr>'
            f'{rows}'
            f'</table>')

    label = _detect_lang_display(lang)
    if lines:
        label = "%s&nbsp;&nbsp;·&nbsp;&nbsp;%d 行" % (label, lines)
    body = _style_pre(_highlight_code(code, lang), max(11, size - 3))
    header = (
        f'<tr><td style="{head_style}color:{pal["text_faint"]};">{label}</td>'
        f'<td align="right" style="{head_style}">{copy}</td></tr>')

    return (
        f'<table width="100%" cellspacing="0" cellpadding="0" '
        f'style="border:1px solid {pal["border"]};margin:6px 0 10px 0;">'
        f'{header}'
        f'<tr><td colspan="2" style="background:{pal["code_bg"]};'
        f'padding:7px 9px;">{body}</td></tr>'
        f'</table>')


def _render_math(block, pal, size):
    """块级公式：居中一张图；排不出来就退回等宽源码（不要留白）。"""
    tex = (block.get("tex") or "").strip()
    snippet = _math.img_html(tex, size + MATH_BLOCK_BUMP, pal["text"],
                             display=True, max_width=MATH_BLOCK_MAX_WIDTH)
    if snippet is None:
        return (f'<p align="center" style="margin:4px 0 10px 0;'
                f'font-family:{MONO_FONT};font-size:{max(11, size - 2)}px;'
                f'color:{pal["text_muted"]};">'
                f'{_html.escape(tex)}</p>')
    return (f'<p align="center" style="margin:6px 0 10px 0;">'
            f'{snippet}</p>')


def _render_block(block, pal, size, state):
    kind = block["kind"]

    if kind == "code":
        html = _render_code(block, pal, size, state["code"])
        state["code"] += 1
        return html

    if kind == "heading":
        level = block["level"]
        bump = 2 if level == 1 else 1 if level == 2 else 0
        top = 14 if level == 1 else 11 if level == 2 else 8
        return (f'<p style="margin:{top}px 0 3px 0;font-size:{size + bump}px;'
                f'font-weight:600;line-height:1.35;">'
                f'{_inline(block["text"])}</p>')

    if kind == "math":
        return _render_math(block, pal, size)

    if kind == "hr":
        # <hr> 在 Qt 里要占满一整行；用 1px 字号的表格单元格才画得出一条细线
        return ('<table width="100%" cellspacing="0" cellpadding="0" '
                'style="margin:10px 0;"><tr>'
                f'<td height="1" style="background:{pal["border"]};'
                'font-size:1px;line-height:1px;">&nbsp;</td>'
                '</tr></table>')

    if kind == "table":
        return _render_table(block, pal, size)

    if kind == "quote":
        body = "<br>".join(_inline(l) for l in block["lines"])
        # 引用做成"左侧竖条 + 一层浅底"的 callout：只画竖条在暗色下几乎看不见
        return ('<table width="100%" cellspacing="0" cellpadding="0" '
                'style="margin:4px 0 10px 0;"><tr>'
                f'<td width="3" style="background:{pal["border_strong"]};"></td>'
                f'<td style="padding:5px 10px;background:{pal["surface"]};'
                f'color:{pal["text_muted"]};font-size:{size}px;">{body}</td>'
                '</tr></table>')

    if kind == "list":
        return _render_list(block, pal, size)

    body = "<br>".join(_inline(l) for l in block["lines"])
    # font-size 必须写：不写就回落到 QTextDocument 的默认字体（中文 Windows
    # 上是宋体 9pt），比正文小一大截，而且「正文字体大小」怎么调都调不动——
    # 这正是「字号太小还不能调整」的全部原因。
    return (f'<p style="margin:0 0 {_para_gap(size)}px 0;'
            f'font-size:{size}px;line-height:1.62;">{body}</p>')


# ── 块级 HTML 缓存 ───────────────────────────────────────────────────
# 流式渲染每一帧都会重新走一遍 parse_blocks，但已完成的块内容不会再变。
# 按 (块签名, 主题, 代码块序号) 缓存 HTML，长回复的帧成本从 O(全文) 降到
# O(尾块)，与 harness 里 React.memo 包住 MarkdownPreComponent 是同一个思路。
_BLOCK_CACHE = {}
_BLOCK_LIMIT = 300


def _theme_key():
    t = theme.current_theme()
    c = theme.html_colors()
    return (t.is_dark, t.body_font, c["code_bg"], c["surface_alt"],
            c["accent"], c["border"])


def _signature(block, code_index):
    kind = block["kind"]
    if kind == "code":
        return (kind, block.get("lang"), block.get("code"), code_index)
    if kind == "math":
        return (kind, block.get("tex"))
    if kind in ("paragraph", "quote"):
        return (kind, tuple(block["lines"]))
    if kind == "heading":
        return (kind, block["level"], block["text"])
    if kind == "list":
        return (kind, tuple((i["level"], i["ordered"], i["text"], i["task"])
                            for i in block["items"]))
    if kind == "table":
        return (kind, tuple(block["head"]),
                tuple(tuple(r) for r in block["rows"]),
                tuple(block["aligns"]))
    return (kind,)


def render_blocks(text):
    """返回 [(html, closed), …]，供流式增量渲染使用。"""
    pal = theme.html_colors()
    size = max(11, theme.current_theme().body_font)
    tkey = _theme_key()
    state = {"code": 0}
    _math.reset_count()
    out = []
    for block in parse_blocks(text):
        key = (_signature(block, state["code"]), tkey)
        html = _BLOCK_CACHE.get(key)
        if html is None:
            try:
                html = _render_block(block, pal, size, state)
            except Exception:
                # 单块失败不能拖垮整条消息（对标 harness 的 ErrorBoundary）
                fallback = _html.escape(
                    block.get("code") or block.get("tex")
                    or "\n".join(block.get("lines", [])))
                html = (f'<p style="margin:0 0 {_para_gap(size)}px 0;'
                        f'font-family:{MONO_FONT};font-size:{size - 2}px;'
                        f'white-space:pre-wrap;">{fallback}</p>')
                if block["kind"] == "code":
                    state["code"] += 1
            if len(_BLOCK_CACHE) > _BLOCK_LIMIT:
                _BLOCK_CACHE.clear()
            _BLOCK_CACHE[key] = html
        elif block["kind"] == "code":
            state["code"] += 1
        out.append((html, block["closed"]))
    return out


def render_markdown(text):
    """Markdown → QTextEdit 可用 HTML。"""
    if not text:
        return ""
    try:
        return "".join(html for html, _closed in render_blocks(text))
    except Exception:
        return ('<pre style="white-space:pre-wrap;font-family:%s;">%s</pre>'
                % (MONO_FONT, _html.escape(text)))


def extract_code_blocks(text):
    """提取所有围栏代码块，返回 [(language, code), …]。"""
    return [(b.get("lang") or "text", b["code"])
            for b in parse_blocks(text) if b["kind"] == "code"]
