# -*- coding: utf-8 -*-
"""编辑器上下文：把「光标 / 选区」翻译成模型能直接用的代码片段。

对标 Cline 的 ``getContextForCommand``（``apps/vscode/src/hosts/vscode/
commandUtils.ts``）：鼠标点击先由编辑器自己转成文本偏移，插件只读取
``Selection``；空选区时上下各扩 3 行，再解析成「项目内相对路径 + 行区间」。

Qt 侧对应 ``QTextCursor``：``blockNumber()`` / ``positionInBlock()`` /
``selectedText()``（注意 Qt 用 U+2029 分段）。
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass

#: 空选区时上下扩展的行数（与 Cline 的 CONTEXT_LINES_TO_EXPAND 一致）
EXPAND_LINES = 3

#: 单次附加上限：行数 / 字符数。超出按光标位置截断并标注
MAX_LINES = 200
MAX_FILE_LINES = 400
MAX_CHARS = 8000

_LANG_BY_EXT = {
    ".py": "python", ".pyw": "python", ".pyi": "python",
    ".js": "javascript", ".jsx": "javascript", ".mjs": "javascript",
    ".ts": "typescript", ".tsx": "tsx",
    ".json": "json", ".md": "markdown", ".rst": "rst",
    ".html": "html", ".htm": "html", ".css": "css", ".scss": "scss",
    ".c": "c", ".h": "c", ".cc": "cpp", ".cpp": "cpp", ".hpp": "cpp",
    ".java": "java", ".kt": "kotlin", ".go": "go", ".rs": "rust",
    ".sh": "bash", ".zsh": "bash", ".bat": "bat", ".ps1": "powershell",
    ".sql": "sql", ".yml": "yaml", ".yaml": "yaml", ".toml": "toml",
    ".ini": "ini", ".cfg": "ini", ".r": "r", ".jl": "julia",
    ".m": "matlab", ".ipynb": "json", ".txt": "text", ".csv": "csv",
}

_DEF_RE = re.compile(r"^(\s*)(?:async\s+def|def|class)\s+\w+")


def language_for(path):
    """按扩展名推断语言标识；未知类型回退 ``text``。"""
    return _LANG_BY_EXT.get(os.path.splitext(path or "")[1].lower(), "text")


def relative_to(path, root):
    """返回 ``path`` 相对 ``root`` 的 posix 路径；越界或无法计算时返回 None。

    对应 Cline ``mention-enricher.ts`` 里的边界检查：``path.relative`` 的
    结果以 ``..`` 开头就说明文件在项目外，直接丢弃，避免把
    ``@../../.ssh/id_rsa`` 这种路径喂给模型。
    """
    if not path or not root:
        return None
    try:
        rel = os.path.relpath(os.path.abspath(path), os.path.abspath(root))
    except (ValueError, OSError):
        return None
    if rel == os.pardir or rel.startswith(os.pardir + os.sep):
        return None
    if os.path.isabs(rel):
        return None
    return rel.replace(os.sep, "/")


@dataclass
class EditorContext:
    """一次「当前编辑器状态」快照。"""

    path: str = ""
    rel_path: str = ""
    language: str = "text"
    start_line: int = 1
    end_line: int = 1
    text: str = ""
    has_selection: bool = False
    total_lines: int = 1
    truncated: bool = False
    untitled: bool = False
    mode: str = "auto"

    @property
    def line_count(self):
        return max(1, self.end_line - self.start_line + 1)

    def location(self):
        """``42`` 或 ``42-58``。"""
        if self.start_line == self.end_line:
            return str(self.start_line)
        return f"{self.start_line}-{self.end_line}"

    def display_name(self):
        if self.untitled:
            return os.path.basename(self.path) or "<untitled>"
        return self.rel_path or os.path.basename(self.path) or "<untitled>"

    def chip_text(self):
        """输入框上方胶囊上的短标签。"""
        base = os.path.basename(self.display_name()) or "<untitled>"
        if self.mode == "file":
            return f"{base} · 全文 {self.line_count} 行"
        return f"{base}:{self.location()} · {self.line_count} 行"

    def prompt_block(self):
        """拼进用户消息的 XML 块。"""
        name = self.display_name()
        fence = "~~~" if "```" in self.text else "```"
        focus = ("选中片段" if self.has_selection
                 else "整个文件" if self.mode == "file"
                 else "光标所在代码")
        attrs = [
            f'file="{name}"',
            f'language="{self.language}"',
            f'lines="{self.location()}"',
            f'focus="{focus}"',
        ]
        if self.truncated:
            attrs.append('truncated="true"')
        if self.untitled:
            attrs.append('saved="false"')
        return (
            "<editor_context " + " ".join(attrs) + ">\n"
            f"{fence}{self.language}\n{self.text}\n{fence}\n"
            "</editor_context>"
        )


def _block_text(doc, start, end):
    """按块取文本，避免 ``toPlainText()`` 整篇复制（大文件下是 O(n)）。"""
    parts = []
    for i in range(start, end + 1):
        parts.append(doc.findBlockByNumber(i).text())
    return "\n".join(parts)


def _selection_lines(doc, start_pos, end_pos):
    """选区覆盖的行号（1-based 转 0-based block 号）。

    选区正好结束在下一行行首时，那行不算被选中。
    """
    first = doc.findBlock(start_pos).blockNumber()
    last_block = doc.findBlock(end_pos)
    last = last_block.blockNumber()
    if last > first and last_block.position() == end_pos:
        last -= 1
    return first, last


def _function_range(doc, line):
    """向上找最近的 ``def`` / ``class``，再按缩进向下找块尾。

    纯缩进启发式，不做 AST 解析：编辑器里的内容可能还没保存，
    语法也可能是半截的，``ast.parse`` 在这种情况下会直接报错。
    """
    total = doc.blockCount()
    start = None
    for i in range(line, -1, -1):
        text = doc.findBlockByNumber(i).text()
        match = _DEF_RE.match(text)
        if match:
            start = i
            indent = len(match.group(1).expandtabs(4))
            break
    if start is None:
        return (max(0, line - EXPAND_LINES), min(total - 1, line + EXPAND_LINES))

    end = start
    for i in range(start + 1, total):
        text = doc.findBlockByNumber(i).text()
        if not text.strip():
            continue
        if len(text) - len(text.lstrip()) <= indent:
            break
        end = i
    return start, max(end, start)


def capture(editor, filename, project_root=None, mode="auto", untitled=False):
    """抓取编辑器状态。

    参数
    ----
    editor : QPlainTextEdit 派生对象（Spyder ``CodeEditor``）
        只需要 ``document()`` 与 ``textCursor()``。
    filename : str
        绝对路径；非绝对路径或 ``untitled`` 视为未保存的新文件。
    project_root : str
        项目根目录，用于算相对路径并做越界校验。
    mode : str
        ``auto``（选区优先，否则光标±3 行）/ ``selection`` /
        ``function`` / ``file`` / ``off``。
    """
    if editor is None or mode == "off":
        return None
    try:
        doc = editor.document()
        cursor = editor.textCursor()
    except Exception:
        return None
    if doc is None:
        return None
    total = doc.blockCount()
    if total <= 0:
        return None

    has_selection = bool(cursor.hasSelection())
    truncated = False
    selected_text = ""

    if has_selection:
        start, end = _selection_lines(
            doc, cursor.selectionStart(), cursor.selectionEnd())
        selected_text = cursor.selectedText().replace("\u2029", "\n")
    elif mode == "file":
        start, end = 0, total - 1
        # 文件以换行结尾时 Qt 会多出一个空块，别把它算进行数
        while end > start and not doc.findBlockByNumber(end).text().strip():
            end -= 1
    elif mode == "function":
        start, end = _function_range(doc, cursor.blockNumber())
    else:
        line = cursor.blockNumber()
        start = max(0, line - EXPAND_LINES)
        end = min(total - 1, line + EXPAND_LINES)

    limit = MAX_FILE_LINES if mode == "file" else MAX_LINES
    if has_selection:
        lines = selected_text.split("\n")
        if len(lines) > limit:
            # 超长选区保留靠近光标的一端
            start += len(lines) - limit
            lines = lines[-limit:]
            truncated = True
        text = "\n".join(lines)
        end = start + len(lines) - 1
    else:
        if end - start + 1 > limit:
            if mode == "file":
                end = start + limit - 1
            else:
                # 保留光标附近的窗口，而不是截掉尾部
                start = max(start, cursor.blockNumber() - limit // 2)
                end = start + limit - 1
            truncated = True
        text = _block_text(doc, start, end)

    if len(text) > MAX_CHARS:
        cut = text.rfind("\n", 0, MAX_CHARS)
        text = text[: cut if cut > 0 else MAX_CHARS]
        truncated = True

    # 截断后按实际文本重算结束行，避免 chip 上的行数说谎
    end = min(start + text.count("\n"), total - 1)

    path = filename if (filename and os.path.isabs(filename)) else ""
    is_untitled = bool(untitled) or not path
    rel = ""
    if not is_untitled:
        rel = relative_to(path, project_root) or ""
        if not rel:
            # 项目外文件不附加（与 Cline 的 mention 边界检查一致）
            return None

    if is_untitled:
        rel = os.path.basename(filename) if filename else "<untitled>"

    return EditorContext(
        path=path,
        rel_path=rel,
        language=language_for(filename or ""),
        start_line=start + 1,
        end_line=end + 1,
        text=text,
        has_selection=has_selection,
        total_lines=total,
        truncated=truncated,
        untitled=is_untitled,
        mode=mode,
    )
