# -*- coding: utf-8 -*-
"""diff 视图：把补丁画成「红删绿增 + 双行号」的代码卡片（v0.7.7 / Diff Edition）

Cline / Roo Code 那一套的 Qt 版实现。整件事分三层：

  1. 表示层 —— 输入可能是 unified diff（``@@ -12,7 +12,8 @@``）、git 的
     ``--- a/x`` + ``+++ b/x``、或 Codex 的 V4A（``*** Update File: x`` 后面
     跟 ``@@`` 与 ``+``/``-`` 行）。这里把它们归一到同一种「行模型」。
  2. 计算层 —— 只有拿到 old/new 两段原文时才需要算 diff（difflib），
     算完仍然喂给同一个行模型；顺带对相邻的 ``-``/``+`` 块做词级细化。
  3. 渲染层 —— 每行三列：旧行号 / 新行号 / 标记 / 正文，整行底色由 <td> 画。

三条硬约束（都是这个项目里踩出来的）：

  * Qt 富文本里 inline ``<span>`` 的背景只包住文字，铺不满整行 —— 所以
    一行必须是一个 ``<td>``，整行底色交给单元格去画。
  * Qt 会把块级 ``background: transparent`` 当成白色整块刷上去，所以每
    一行都给显式色值，绝不写 transparent。
  * V4A 补丁没有绝对行号，此时宁可不显示行号，也不伪造 —— 只有见到
    ``@@ -a,b +c,d @@`` 或明确的 ``*** Add File`` 才开始计数。
"""

import difflib
import html
import re

MONO = "Consolas, 'DejaVu Sans Mono', 'Courier New', monospace"

_HUNK_RE = re.compile(r"^@@\s+-(\d+)(?:,(\d+))?\s+\+(\d+)(?:,(\d+))?\s+@@")
_V4A_FILE_RE = re.compile(
    r"^\*\*\*\s+(Update|Add|Delete)\s+File:\s*(.+?)\s*$")
_V4A_MOVE_RE = re.compile(r"^\*\*\*\s+Move\s+to:\s*(.+?)\s*$")
_NOISE_RE = re.compile(r"^(index |new file mode|deleted file mode|old mode|"
                       r"similarity index|rename (from|to) |Binary files )")
_GIT_HEADER_RE = re.compile(r"^diff --git a/(.+?) b/(.+)$")

# 超过这个长度就不做词级细化：收益低，还拖慢流式重绘
_INTRA_MAX_CHARS = 240
_MAX_PAIRS = 400


def _esc(text):
    return html.escape(text, quote=False)


def _bump(n):
    return None if n is None else n + 1


def looks_like_diff(text):
    """没有语言标注时，判断一段代码块是不是补丁。

    只在出现真凭实据时才认：``@@`` 块头、``*** Begin Patch``，或同时
    出现 ``--- `` 与 ``+++ ``。普通代码里 ``+``/``-`` 行首太常见，不能
    只按这个猜。
    """
    body = (text or "").lstrip("\n")
    if not body.strip():
        return False
    head = body.split("\n", 60)
    if body.startswith("*** Begin Patch") or body.startswith("*** Update File:"):
        return True
    has_hunk = any(line.startswith("@@") for line in head)
    has_pair = (any(line.startswith("--- ") for line in head)
                and any(line.startswith("+++ ") for line in head))
    return has_hunk or has_pair


def parse(text):
    """把补丁文本解析成行模型。

    返回 dict::

        {"lines": [{"kind", "old", "new", "text", "marker", "spans"}],
         "adds": int, "dels": int}
    """
    raw = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    rows = raw.split("\n")
    while rows and rows[-1].strip() == "":
        rows.pop()

    lines = []
    old_no = new_no = None
    counting = False          # 行号是否可信
    skip_plus = False         # diff --git 已经画过文件行
    adds = dels = 0

    def push(kind, number_old, number_new, body, marker=""):
        lines.append({"kind": kind, "old": number_old, "new": number_new,
                      "text": body, "marker": marker, "spans": None})

    def next_is_new_file(i):
        """``--- a/x`` 后面跟 ``+++ b/x`` 才是文件头。

        不能只看「是否已经出现过 @@」—— 一个补丁里可以有多个文件，
        第二个文件的 ``--- `` 也会出现在 @@ 之后。反过来，hunk 里被删
        掉的一行以 ``-- `` 开头时会渲染成 ``--- ``，但它下一行不会是
        ``+++ ``，所以这个判断是安全的。
        """
        return i + 1 < len(rows) and rows[i + 1].startswith("+++ ")

    index = 0
    total = len(rows)
    while index < total:
        line = rows[index].rstrip("\r")
        index += 1

        if line.startswith("\\ No newline"):
            continue

        if line.startswith("***"):
            if line.startswith(("*** Begin Patch", "*** End Patch")):
                continue
            match = _V4A_FILE_RE.match(line)
            if match:
                action, path = match.group(1), match.group(2)
                old_no = new_no = None
                counting = False
                if action == "Add":
                    new_no, counting = 1, True
                elif action == "Delete":
                    old_no, counting = 1, True
                push("file", None, None,
                     "%s %s" % ({"Update": "修改", "Add": "新建",
                                 "Delete": "删除"}[action], path))
                continue
            move = _V4A_MOVE_RE.match(line)
            if move:
                push("file", None, None, "重命名 → %s" % move.group(1))
                continue
            push("meta", None, None, line)
            continue

        if line.startswith("diff --git "):
            old_no = new_no = None
            counting = False
            match = _GIT_HEADER_RE.match(line)
            push("file", None, None,
                 match.group(2) if match else line[len("diff --git "):].strip())
            # 文件行已经由 diff --git 画过，紧跟着的 +++ 不要再画一遍
            skip_plus = True
            continue

        if _NOISE_RE.match(line):
            push("meta", None, None, line)
            continue

        if line.startswith("--- ") and next_is_new_file(index - 1):
            # 旧文件名行；紧跟着的 +++ 会画成唯一的文件行
            old_no = new_no = None
            counting = False
            continue

        if line.startswith("+++ ") and skip_plus:
            skip_plus = False
            continue

        if line.startswith("+++ ") and index >= 2 and rows[index - 2].startswith("--- "):
            path = line[4:].strip()
            if path.startswith("b/"):
                path = path[2:]
            if path != "/dev/null":
                push("file", None, None, path)
            continue

        if line.startswith("@@"):
            match = _HUNK_RE.match(line)
            if match:
                old_no = int(match.group(1))
                new_no = int(match.group(3))
                counting = True
            push("hunk", None, None, line)
            continue

        head = line[:1]
        if head == "+":
            push("add", None, new_no, line[1:], "+")
            adds += 1
            new_no = _bump(new_no)
            continue
        if head == "-":
            push("del", old_no, None, line[1:], "-")
            dels += 1
            old_no = _bump(old_no)
            continue

        body = line[1:] if head == " " else line
        push("ctx", old_no, new_no, body, " ")
        old_no = _bump(old_no)
        new_no = _bump(new_no)

    if not counting:
        for row in lines:
            if row["kind"] in ("add", "del", "ctx"):
                row["old"] = row["new"] = None

    _refine(lines)
    return {"lines": lines, "adds": adds, "dels": dels}


def _refine(lines):
    """相邻的 -/+ 块做词级细化：只给真正变的片段加深底色。

    GitHub / VS Code 都是这么干的 —— 否则改一个字符，整行都是重色，
    眼睛还得自己找差异。
    """
    pairs = 0
    index = 0
    total = len(lines)
    while index < total and pairs < _MAX_PAIRS:
        if lines[index]["kind"] != "del":
            index += 1
            continue
        del_start = index
        while index < total and lines[index]["kind"] == "del":
            index += 1
        del_end = index
        add_start = index
        while index < total and lines[index]["kind"] == "add":
            index += 1
        add_end = index

        for offset in range(min(del_end - del_start, add_end - add_start)):
            left = lines[del_start + offset]
            right = lines[add_start + offset]
            spans_left, spans_right = _intra_spans(left["text"], right["text"])
            if spans_left or spans_right:
                left["spans"] = spans_left
                right["spans"] = spans_right
                pairs += 1


def _intra_spans(old, new):
    """返回 (旧行高亮区间, 新行高亮区间)，按字符算。"""
    if not old or not new:
        return None, None
    if len(old) > _INTRA_MAX_CHARS or len(new) > _INTRA_MAX_CHARS:
        return None, None
    matcher = difflib.SequenceMatcher(None, old, new, autojunk=False)
    if matcher.ratio() < 0.30:
        return None, None          # 基本重写，整行高亮就够了

    left, right = [], []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag in ("replace", "delete") and i2 > i1:
            left.append((i1, i2))
        if tag in ("replace", "insert") and j2 > j1:
            right.append((j1, j2))
    return left or None, right or None


def _marked(text, spans, colour):
    if not spans:
        return _esc(text)
    out = []
    cursor = 0
    for start, end in spans:
        if start < cursor:
            continue
        out.append(_esc(text[cursor:start]))
        out.append('<span style="background-color:%s;">%s</span>'
                   % (colour, _esc(text[start:end])))
        cursor = end
    out.append(_esc(text[cursor:]))
    return "".join(out)


def _number_cell(value, pal, size, tint, first=False):
    shown = "" if value is None else str(value)
    return ('<td width="30" align="right" style="background-color:%s;'
            'color:%s;font-family:%s;font-size:%dpx;padding:1px 5px 1px %dpx;">'
            '%s</td>' % (tint, pal["text_faint"], MONO,
                         max(10, size - 1), 8 if first else 0, shown or "&nbsp;"))


def render_rows(doc, pal, size, max_lines=400, wrap=True):
    """行模型 → HTML 行。

    ``wrap=False`` 只返回 ``<tr>`` 片段，交给调用方嵌进自己的卡片表格 ——
    Qt 里嵌套表格的 ``width="100%"`` 不可靠，整行底色会缩到内容宽度，
    所以代码卡片那种场景必须把行直接摊在外层表格里。
    """
    lines = doc["lines"]
    shown = lines[:max_lines]
    truncated = max(0, len(lines) - max_lines)

    out = []
    if wrap:
        out.append('<table width="100%%" cellspacing="0" cellpadding="0" '
                   'style="border-collapse:collapse;font-family:%s;'
                   'font-size:%dpx;">' % (MONO, size))

    for row in shown:
        kind = row["kind"]
        if kind in ("file", "meta", "hunk"):
            colour = {"file": pal["info"], "meta": pal["text_faint"],
                      "hunk": pal["info"]}.get(kind, pal["text_faint"])
            weight = "font-weight:600;" if kind == "file" else ""
            out.append(
                '<tr><td colspan="5" style="background-color:%s;color:%s;'
                'font-family:%s;font-size:%dpx;padding:%s;%s">%s</td></tr>'
                % (pal["code_header"] if kind == "file" else pal["code_bg"],
                   colour, MONO, max(10, size - 1),
                   "3px 8px" if kind == "file" else "2px 8px",
                   weight, _esc(row["text"])))
            continue

        tint = {"add": pal["diff_add_bg"], "del": pal["diff_del_bg"]}.get(
            kind, pal["code_bg"])
        strong = {"add": pal["diff_add_strong"],
                  "del": pal["diff_del_strong"]}.get(kind, pal["diff_add_strong"])
        mark_colour = {"add": pal["diff_add"], "del": pal["diff_del"]}.get(
            kind, pal["text_faint"])

        body = _marked(row["text"], row["spans"], strong) or "&nbsp;"
        out.append(
            '<tr>'
            + _number_cell(row["old"], pal, size, tint, first=True)
            + _number_cell(row["new"], pal, size, tint)
            + '<td width="12" align="center" style="background-color:%s;'
              'color:%s;font-family:%s;font-size:%dpx;padding:1px 0;">%s</td>'
              % (tint, mark_colour, MONO, size, row["marker"] or "&nbsp;")
            + '<td style="background-color:%s;color:%s;font-family:%s;'
              'font-size:%dpx;padding:1px 8px 1px 2px;white-space:pre-wrap;'
              'word-break:break-word;">%s</td>'
              # 第 5 列只给表头的「复制」占位，正文行留空但同样上底色，
              # 否则每条色带右端会缺一小截。
              '<td width="46" style="background-color:%s;"></td></tr>'
              % (tint, pal["text"], MONO, size, body, tint))

    if truncated:
        out.append(
            '<tr><td colspan="5" style="background-color:%s;color:%s;'
            'font-family:%s;font-size:%dpx;padding:3px 8px;">'
            '… 还有 %d 行，已折叠 …</td></tr>'
            % (pal["code_bg"], pal["text_faint"], MONO,
               max(10, size - 1), truncated))

    if wrap:
        out.append("</table>")
    return "".join(out), truncated


def stats(doc):
    return doc["adds"], doc["dels"]


def render_patch(text, pal, size, max_lines=400):
    """补丁文本 → HTML 表格。"""
    return render_rows(parse(text), pal, size, max_lines)


def unified(old, new, fromfile="原文件", tofile="修改后", context=3):
    """两段原文 → unified diff 文本（带 @@ 块头，行号才是真的）。"""
    return "\n".join(difflib.unified_diff(
        (old or "").splitlines(), (new or "").splitlines(),
        fromfile=fromfile or "原文件", tofile=tofile or "修改后",
        lineterm="", n=context))


def render_diff(old, new, pal, size, fromfile="原文件", tofile="修改后",
                max_lines=400, context=3):
    """两段原文 → 红绿行。内容没变时返回空串，交给调用方决定怎么提示。"""
    patch = unified(old, new, fromfile, tofile, context)
    if not patch.strip():
        return "", 0, 0
    doc = parse(patch)
    html_text, _ = render_rows(doc, pal, size, max_lines)
    return html_text, doc["adds"], doc["dels"]
