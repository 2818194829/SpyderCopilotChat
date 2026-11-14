# -*- coding: utf-8 -*-
"""
Cline 风格的操作审批卡片（v0.5.0 / Cline Edition）

对标 VS Code Cline 的核心交互：
  * 每次写文件 / 执行命令前，先在对话流里内联展示将要发生的变化
  * 新增行绿色、删除行红色，逐条「批准 / 拒绝」，或「全部批准 / 全部拒绝」
  * 不使用模态对话框，不嵌套事件循环；审批结果通过 Qt 信号异步返回

本模块只做「预览 + 决策」，绝不写文件。真正的执行仍由 agentic_actions
完成，因此所有 diff 都是 dry-run 计算出来的。
"""

import html
import json
import os
import re as _re

from qtpy.QtCore import QSize, QTimer, Qt, Signal
from qtpy.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QPushButton, QSizePolicy,
    QTextEdit, QToolButton, QVBoxLayout, QWidget,
)

from ..copilot_client import (
    TOOL_GROUP_EDIT, TOOL_GROUP_EXECUTE, TOOL_GROUP_FLEET,
    TOOL_GROUP_GIT, TOOL_GROUP_NOTEBOOK, TOOL_GROUP_SESSION,
)
from .security import approval_required as _risk_approval_required, classify_tool
from . import theme
from . import diff_view as _diff

MAX_DIFF_LINES = 220

# 差异预览的高度窗口（v0.8.5）：上限沿用旧值，下限保证还能看出改了什么。
MAX_DIFF_HEIGHT = 190
MIN_DIFF_HEIGHT = 56
# 只够露出两三行的极限高度：再低就没有阅读价值，不如整块收起。
ABS_MIN_DIFF = 26
# 兜底估算（控件还没排版时用）；正常路径都走实测的 fixed_height()。
CARD_CHROME = 132
ROW_CHROME = 80


class ElidedLabel(QLabel):
    """尾部省略的标签，且不把文字宽度顶给父布局。

    ``QLabel`` 的 ``minimumSizeHint`` 等于整段文字宽度。一条 60 字符的绝对
    路径（``D:\\python_dev\\…\\坦克大战\\_check_levels.py``）会把审批卡的最小
    宽度顶到 500px 以上，于是整条对话流横向溢出：底部冒出横向滚动条，卡片
    右侧的「待批准」和折叠箭头被推到视野外（v0.8.4 实拍）。

    路径只需要辨认出是哪个文件，中间省略就够，文件名留在尾部。
    """

    def __init__(self, text="", parent=None):
        super().__init__(parent)
        self._full = text or ""
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.setMinimumWidth(0)
        super().setText(self._elided())

    def setText(self, text):
        self._full = text or ""
        super().setText(self._elided())

    def fullText(self):
        return self._full

    def _elided(self):
        width = max(48, self.width() - 2)
        return self.fontMetrics().elidedText(self._full, Qt.ElideMiddle, width)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        super().setText(self._elided())

    def minimumSizeHint(self):
        hint = super().minimumSizeHint()
        return QSize(0, hint.height())



# ── 需要审批 / 会改动文件系统的工具集合 ────────────────────────────────

WRITE_TOOLS = set(TOOL_GROUP_EDIT) | {
    "multi_replace_string",
    "create_notebook", "add_notebook_cell", "edit_notebook_cell",
    "delete_notebook_cell", "notebook_run_cell",
    "git_add", "git_commit", "git_create_branch",
    "resolve_conflict", "memory_save",
}

EXECUTE_TOOLS = set(TOOL_GROUP_EXECUTE) | set(TOOL_GROUP_FLEET) | {
    "run_subagent", "switch_session", "compact_conversation",
}

PROTECTED_TOOLS = WRITE_TOOLS | EXECUTE_TOOLS

RISK_LABELS = {
    "danger": ("危险", "danger"),
    "execute": ("高", "warning"),
    "write": ("写入", "write"),
    "read": ("只读", "read"),
}


def needs_approval(name, policy="writes", risk=None, args=None):
    """按风险分级判断是否需要批准（v0.5.6）。

    policy:
      "risk" / "writes" — 只读免批，写操作与危险操作需批准（推荐）
      "all"             — 所有工具调用都需批准
      "none"            — 自动执行，但 danger 级仍强制逐项确认
    """
    return _risk_approval_required(name, policy, risk=risk, args=args)


def parse_tool_args(raw):
    """尽量宽松地解析工具参数（AI 偶尔会给出不合法 JSON）"""
    if isinstance(raw, dict):
        return raw
    try:
        obj = json.loads(raw or "{}")
        if isinstance(obj, dict):
            return obj
    except Exception:
        pass
    try:
        from .agentic_actions import _parse_robust_json
        obj = _parse_robust_json(raw or "{}", "")
        if isinstance(obj, dict):
            return obj
    except Exception:
        pass
    return {}


def resolve_path(path, base_dir=None):
    """与 agentic_actions._resolve_path 保持一致"""
    if not path:
        return ""
    if os.path.isabs(path):
        return os.path.normpath(path)
    if base_dir:
        return os.path.normpath(os.path.join(base_dir, path))
    return os.path.normpath(os.path.abspath(path))


def read_text(path):
    """返回 (内容, 是否存在)"""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read(), True
    except Exception:
        return "", False


def _diff_html(old, new, label):
    """两段原文 → 红删绿增的行视图（v0.7.7 起带双行号与词级高亮）。"""
    c = theme.html_colors()
    body, _adds, _dels = _diff.render_diff(
        old, new, c, 11, fromfile=label or "原文件", tofile="",
        max_lines=MAX_DIFF_LINES)
    if not body:
        return (f"<div style='color:{c['text_faint']};padding:6px;'>"
                f"（内容无变化）</div>")
    return body


def _patch_html(patch_text):
    """apply_patch 的参数本身已是补丁，直接着色，不重算差异。"""
    c = theme.html_colors()
    if not (patch_text or "").strip():
        return (f"<div style='color:{c['text_faint']};padding:6px;'>"
                f"（空补丁）</div>")
    body, _ = _diff.render_patch(patch_text, c, 11, MAX_DIFF_LINES)
    return body


def affected_files(name, args, base_dir):
    """返回该工具会改动的文件路径列表（供检查点快照使用）"""
    paths = []
    for key in ("filepath", "path", "old_path", "new_path", "source", "destination"):
        value = args.get(key)
        if isinstance(value, str) and value.strip():
            paths.append(resolve_path(value, base_dir))

    if name == "multi_replace_string":
        for item in args.get("replacements", []) or []:
            if isinstance(item, dict) and item.get("filepath"):
                paths.append(resolve_path(item["filepath"], base_dir))

    if name == "apply_patch":
        for line in (args.get("patch") or "").splitlines():
            if line.startswith("+++ ") or line.startswith("--- "):
                raw = line[4:].strip()
                if raw.startswith("b/") or raw.startswith("a/"):
                    raw = raw[2:]
                if raw and raw != "/dev/null":
                    paths.append(resolve_path(raw, base_dir))

    if name in SHELL_TOOLS:
        # v0.8.7：run_command 是「写盘大户」，但参数里没有 filepath。
        # 弱模型（deepseek-flash 一类）尤其爱用
        #   python -c "open(r'D:\...\a.py','w').write(...)"
        # 或者 echo/patch/重定向来改文件 —— 只看 filepath 字段的话，
        # 这一整批改动对内联 diff 就是隐形的：快照为空 ⇒ 不预览 ⇒
        # 编辑器里永远没有红绿，也不会有一句失败提示。
        for raw in paths_from_command(args.get("command", ""), base_dir):
            paths.append(raw)

    seen, result = set(), []
    for p in paths:
        if p and p not in seen:
            seen.add(p)
            result.append(p)
    return result


#: 通过命令行写盘的工具：路径只能从命令文本里反推
SHELL_TOOLS = {"run_command"}

#: 值得做内联预览的源文件后缀
SOURCE_SUFFIXES = (
    ".py", ".pyw", ".pyi", ".js", ".jsx", ".ts", ".tsx", ".vue", ".html",
    ".htm", ".css", ".scss", ".json", ".md", ".txt", ".rst", ".yaml",
    ".yml", ".toml", ".cfg", ".ini", ".sql", ".sh", ".bat", ".ps1", ".qss",
)


def _looks_like_source(fragment):
    """一个片段像不像可编辑的源文件路径（而不是 URL / 版本号 / 模块名）。"""
    text = (fragment or "").strip().strip("\"'`()[]{}<>,;")
    if not text or len(text) > 320:
        return False
    if "://" in text or text.startswith("www."):
        return False
    # 路径里不会出现引号 / 括号 / 等号：出现说明这段是命令片段而不是
    # 路径（``open(r'D:\a.py`` 这种半截货），放进去只会污染快照
    if any(ch in text for ch in "\"'`()="):
        return False
    low = text.lower()
    if not low.endswith(SOURCE_SUFFIXES):
        # 允许 "a.py:12" 这种带行号的写法
        head = low.split(":", 1)[0]
        if not head.endswith(SOURCE_SUFFIXES):
            return False
        text = text[:len(head)]
    return True


def paths_from_command(command, base_dir=None):
    """从一条 shell 命令里反推它可能写到的源文件。

    只做"尽量多抓"，宁可多也不能漏：多抓的交由后面「内容是否真的
    变了」再筛一遍，零成本；漏抓则整批改动都对内联 diff 隐形。
    """
    if not command or not isinstance(command, str):
        return []
    found = []
    # 1) 引号包起来的片段最可靠（Windows 路径几乎总带空格）。
    #    双引号和单引号必须**各扫各的**：python -c "…open(r'D:\a b.py')…"
    #    这种嵌套写法里，双引号一匹配就会把里面的单引号整段吃掉，
    #    用 alternation 写成一个正则就只能抓到外层那串代码。
    for pattern in (r'"([^"]{2,320})"', r"'([^']{2,320})'"):
        for match in _re.finditer(pattern, command):
            frag = match.group(1)
            if _looks_like_source(frag):
                found.append(frag)
    # 2) 无引号的 token
    for token in _re.split(r"[\s,;|&<>]+", command):
        if _looks_like_source(token):
            found.append(token)
    # 3) --file=xxx / -o=xxx 之类的赋值形式
    for match in _re.finditer(r"[-\w]+[=]\s*([^\s\"']{2,320})", command):
        if _looks_like_source(match.group(1)):
            found.append(match.group(1))

    result = []
    for frag in found:
        text = frag.strip().strip("\"'`()[]{}<>,;")
        if text.lower().endswith(SOURCE_SUFFIXES) is False:
            text = text.split(":", 1)[0]
        try:
            path = resolve_path(text, base_dir)
        except Exception:
            continue
        if path and path not in result:
            result.append(path)
    return result


def build_preview(tool_call, base_dir=None):
    """dry-run：只读取文件、在内存中计算新内容，绝不落盘。

    返回 dict: name / path / risk / headline / html / files
    """
    c = theme.html_colors()
    fn = tool_call.get("function", {}) or {}
    name = fn.get("name", "?") or "?"
    args = parse_tool_args(fn.get("arguments", "{}"))
    files = affected_files(name, args, base_dir)
    path = files[0] if files else ""

    level = classify_tool(name, args)
    if level == "danger":
        risk = "danger"
    elif name in EXECUTE_TOOLS or name.startswith("mcp_"):
        risk = "execute"
    elif level == "write" or name in WRITE_TOOLS:
        risk = "write"
    else:
        risk = "read"

    headline = path or args.get("command", "") or name
    body = ""

    try:
        if name == "create_file":
            old, _ = read_text(path)
            body = _diff_html(old, args.get("content", ""), path)

        elif name == "edit_file":
            old, _ = read_text(path)
            lines = old.splitlines(keepends=True)
            start = max(1, int(args.get("start_line", 1) or 1))
            end = int(args.get("end_line", -1) or -1)
            if end < 0 or end > len(lines):
                end = len(lines)
            new_content = args.get("new_content", "")
            new_lines = new_content.split("\n")
            if lines and lines[-1].endswith("\n"):
                new_lines = [l + "\n" for l in new_lines]
            else:
                new_lines = [l + "\n" for l in new_lines[:-1]] + [new_lines[-1]]
            body = _diff_html(old, "".join(lines[:start - 1] + new_lines + lines[end:]), path)

        elif name in ("insert_edit", "multi_replace_string"):
            if not path and name == "multi_replace_string":
                items = args.get("replacements", []) or []
                if items and isinstance(items[0], dict):
                    path = resolve_path(items[0].get("filepath", ""), base_dir)
            old, _ = read_text(path)
            new = old
            if name == "insert_edit":
                new = old.replace(args.get("old_string", ""),
                                  args.get("new_string", ""), 1)
            else:
                for item in args.get("replacements", []) or []:
                    if isinstance(item, dict):
                        new = new.replace(item.get("old_string", ""),
                                          item.get("new_string", ""), 1)
            body = _diff_html(old, new, path)

        elif name == "delete_file":
            old, _ = read_text(path)
            body = _diff_html(old, "", path)

        elif name == "apply_patch":
            body = _patch_html(args.get("patch", ""))
            for line in (args.get("patch") or "").splitlines():
                if line.startswith(("*** Update File:", "*** Add File:",
                                    "*** Delete File:")):
                    headline = line.split(":", 1)[-1].strip()
                    break

        elif name == "run_command":
            cmd = args.get("command", "")
            headline = cmd
            body = (f"<pre style='font-family:Consolas,monospace;font-size:11px;"
                    f"color:{c['warning']};margin:0;white-space:pre-wrap;'>$ "
                    + html.escape(cmd) + "</pre>")

        else:
            text = json.dumps(args, ensure_ascii=False, indent=2)
            body = (f"<pre style='font-family:Consolas,monospace;font-size:11px;"
                    f"color:{c['text']};margin:0;white-space:pre-wrap;'>"
                    + html.escape(text[:4000]) + "</pre>")
    except Exception as exc:  # 预览失败不能阻断审批
        body = (f"<div style='color:{c['warning']};padding:6px;'>"
                f"预览生成失败：{html.escape(str(exc)[:200])}</div>")

    return {
        "name": name,
        "path": path,
        "risk": risk,
        "headline": headline[:160],
        "html": body,
        "files": files,
    }


# ── 单条操作的卡片行 ──────────────────────────────────────────────────

class ApprovalRow(QFrame):
    """一个工具调用 = 一行审批（含可折叠 diff 预览）"""

    decided = Signal(bool)

    def __init__(self, tool_call, preview, parent=None, max_diff=MAX_DIFF_HEIGHT):
        super().__init__(parent)
        self._tc = tool_call
        self._state = "pending"
        self._danger = preview.get("risk") == "danger"
        self._raw_html = preview["html"]
        self._html_palette = theme.html_colors()
        self._max_diff = int(max_diff or MAX_DIFF_HEIGHT)
        m = theme.current_theme().metrics
        theme.named(self, "approvalRow")
        theme.set_prop(self, "risk", "danger" if self._danger else preview["risk"])
        self.setFrameShape(QFrame.NoFrame)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(m["pad"], m["gap"], m["pad"], m["gap"])
        lay.setSpacing(max(3, m["gap"] - 2))

        head = QHBoxLayout()
        head.setSpacing(6)
        self._head_widgets = []
        label, kind = RISK_LABELS.get(preview["risk"], RISK_LABELS["read"])
        badge = theme.badge(label, kind)
        head.addWidget(badge)
        self._head_widgets.append(badge)
        if self._danger:
            warn = theme.badge("需单独确认", "danger")
            warn.setToolTip("不可逆或影响外部系统的操作，「全部批准」不会覆盖它")
            head.addWidget(warn)
            self._head_widgets.append(warn)

        title = QLabel(html.escape(preview["name"]))
        theme.named(title, "toolTitle")
        head.addWidget(title)
        self._head_widgets.append(title)

        path_lbl = ElidedLabel(preview["headline"] or "(无路径)")
        theme.named(path_lbl, "approvalPath")
        path_lbl.setToolTip(preview["path"] or preview["headline"])
        head.addWidget(path_lbl, 1)
        self._head_widgets.append(path_lbl)

        self._state_lbl = QLabel("待批准")
        theme.named(self._state_lbl, "stateLabel")
        theme.set_prop(self._state_lbl, "state", "pending")
        head.addWidget(self._state_lbl)
        self._head_widgets.append(self._state_lbl)

        self._toggle = theme.icon_button("chevron", "收起差异", 22)
        self._toggle.setCheckable(True)
        self._toggle.setChecked(True)
        self._toggle.clicked.connect(self._on_toggle)
        head.addWidget(self._toggle)
        self._head_widgets.append(self._toggle)
        self._user_choice = None      # 用户自己点过折叠箭头后就不再自动收放
        lay.addLayout(head)

        self._view = QTextEdit()
        theme.named(self._view, "diffView")
        self._view.setReadOnly(True)
        self._view.setMaximumHeight(self._max_diff)
        self._view.setFrameShape(QFrame.NoFrame)
        self._view.setHtml(self._raw_html)
        self._fit_diff_height()
        lay.addWidget(self._view)
        # 构造时还没排版，viewport 宽度不可信；等一次事件循环再校准高度。
        QTimer.singleShot(0, self._fit_diff_height)

        btns = QHBoxLayout()
        btns.addStretch()
        self._ok_btn = theme.push_button("批准", "primary")
        self._no_btn = theme.push_button("拒绝", "danger")
        self._ok_btn.clicked.connect(lambda: self._decide(True))
        self._no_btn.clicked.connect(lambda: self._decide(False))
        btns.addWidget(self._no_btn)
        btns.addWidget(self._ok_btn)
        lay.addLayout(btns)

    def _fit_diff_height(self):
        """差异区高度随内容自适应，避免短 diff 下面挂一大块空白。

        旧实现用 ``行数 × 16`` 估算，跟字号 / 自动换行脱节：字号调大后
        会切掉内容，调小则底部空一截。这里直接问文档要真实高度。

        上限不是常数，而是由 ``ApprovalCard.fit_to_height()`` 按当前视图
        高度算出来的——卡片比视图高时，批准按钮会落到折叠线以下。
        """
        doc = self._view.document()
        width = max(160, self._view.viewport().width() - 4)
        doc.setTextWidth(width)
        height = int(doc.size().height()) + 10
        floor = min(MIN_DIFF_HEIGHT, self._max_diff)
        height = min(self._max_diff, max(floor, height))
        if abs(self._view.height() - height) > 1:
            self._view.setFixedHeight(height)

    def fixed_height(self):
        """除差异区以外的固定高度。

        直接问布局要：``sizeHint`` 减去差异区本身的高度。手写
        ``边距 + 徽章行 + 按钮行 + 间距`` 会和 QVBoxLayout 的实际排布差
        4~8px，而 8px 正好够把按钮顶出视野——这个函数存在的理由就是
        别再用估算。
        """
        shown = 0 if self._view.isHidden() else self._view.height()
        return max(0, self.sizeHint().height() - shown)

    def set_max_diff(self, value):
        """审批卡按视图高度重新分配差异区高度（v0.8.5）。"""
        value = int(value or MAX_DIFF_HEIGHT)
        if value == self._max_diff:
            return
        self._max_diff = value
        self._view.setMaximumHeight(value)
        self._fit_diff_height()

    def max_diff(self):
        return self._max_diff

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._fit_diff_height()

    def _on_toggle(self, checked):
        self._user_choice = bool(checked)
        self._view.setVisible(checked)
        self._toggle.setToolTip("收起差异" if checked else "展开差异")
        self._toggle.setIcon(theme.make_icon(
            "chevron" if checked else "chevron_right",
            theme.current_theme()["text_muted"]))

    def set_compact(self, compact):
        """空间不够时自动收起差异，把位置让给批准/拒绝。

        按钮够不着比少看几行代码严重得多：差异一次点击就能重新展开，
        而一个被挤出视野的按钮会让整个 Agent 卡住等一个点不到的决定。
        用户自己点过箭头就不再干预。
        """
        compact = bool(compact)
        if self._user_choice is not None:
            return
        if compact == self._view.isHidden():
            return
        self._toggle.setChecked(not compact)
        self._on_toggle(not compact)
        self._user_choice = None      # 自动收放不算用户选择

    def is_compact(self):
        return self._view.isHidden()

    def _decide(self, approved):
        if self._state != "pending":
            return
        self._state = "approved" if approved else "rejected"
        self._ok_btn.setEnabled(False)
        self._no_btn.setEnabled(False)
        self._state_lbl.setText("已批准" if approved else "已拒绝")
        theme.set_prop(self._state_lbl, "state", self._state)
        self.decided.emit(approved)

    def auto_approve(self):
        """v0.5.6：低风险项自动放行，不占用用户的审批注意力。"""
        if self._state != "pending":
            return
        self._state = "approved"
        self._ok_btn.setEnabled(False)
        self._no_btn.setEnabled(False)
        self._state_lbl.setText("自动放行")
        theme.set_prop(self._state_lbl, "state", "done")
        self.decided.emit(True)

    def refresh_theme(self):
        """主题切换：重映射 diff 的内联颜色，不重新计算差异。"""
        self._view.setHtml(
            theme.recolor_html(self._raw_html, self._html_palette))
        self._fit_diff_height()
        self._html_palette = theme.html_colors()
        self._toggle.setIcon(
            theme.make_icon("chevron", theme.current_theme()["text_muted"]))

    def tool_call(self):
        return self._tc

    def is_pending(self):
        return self._state == "pending"

    def is_approved(self):
        return self._state == "approved"

    def is_dangerous(self):
        return self._danger


# ── 整批审批卡片 ──────────────────────────────────────────────────────

class ApprovalCard(QFrame):
    """插入对话流中的审批区块（Cline 风格）"""

    decided = Signal(object)       # list[dict]：被批准的工具调用
    undo_requested = Signal(str)   # checkpoint id
    changed = Signal()             # 任一行的状态变化（供固定快捷条刷新计数）

    def __init__(self, tool_calls, base_dir=None, parent=None,
                 max_diff=MAX_DIFF_HEIGHT):
        super().__init__(parent)
        self._cp_id = None
        self._finished = False
        self._max_diff = int(max_diff or MAX_DIFF_HEIGHT)
        m = theme.current_theme().metrics
        theme.named(self, "approvalCard")
        theme.set_prop(self, "state", "pending")
        self.setFrameShape(QFrame.NoFrame)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(m["pad"] + 2, m["gap"] + 1,
                               m["pad"] + 2, m["gap"] + 1)
        lay.setSpacing(m["gap"])

        self._title = QLabel(f"<b>待批准 · {len(tool_calls)} 项操作</b>")
        theme.named(self._title, "approvalTitle")
        lay.addWidget(self._title)

        self._rows = []
        for tc in tool_calls:
            row = ApprovalRow(tc, build_preview(tc, base_dir), self,
                              max_diff=self._max_diff)
            row.decided.connect(self._on_row_decided)
            lay.addWidget(row)
            self._rows.append(row)

        has_danger = any(r.is_dangerous() for r in self._rows)
        hint_text = ("先查看差异，再逐项批准。被拒绝的操作不会写入磁盘，"
                     "AI 会等待你的下一步指示。")
        if has_danger:
            hint_text += ("\n含危险操作（删除 / 委派执行 / 危险命令）："
                          "必须逐项点「批准」，批量按钮不会替你确认。")
        hint = QLabel(hint_text)
        theme.named(hint, "hintLabel")
        hint.setWordWrap(True)
        self._hint = hint
        lay.insertWidget(1, hint)

        footer = QHBoxLayout()
        self._all_ok = theme.push_button(
            "全部批准安全项" if has_danger else "全部批准", "ghost")
        self._all_no = theme.push_button("全部拒绝", "ghost")
        self._all_ok.clicked.connect(self._approve_all)
        self._all_no.clicked.connect(self._reject_all)
        footer.addWidget(self._all_no)
        footer.addWidget(self._all_ok)
        footer.addStretch()

        self._undo_btn = theme.push_button("撤销本步更改", "subtle")
        self._undo_btn.setVisible(False)
        self._undo_btn.clicked.connect(self._on_undo)
        footer.addWidget(self._undo_btn)
        lay.addLayout(footer)

    def auto_approve(self, predicate):
        """按 predicate(tool_call) 自动放行不需要人工确认的项。

        延迟到下一个事件循环再执行，确保调用方已连接 decided 信号，
        也避免审批卡还插进布局时就重入 Agent 循环。
        """
        def _apply():
            for row in self._rows:
                if not row.is_pending():
                    continue
                try:
                    if predicate(row.tool_call()):
                        row.auto_approve()
                except Exception:
                    pass
        QTimer.singleShot(0, _apply)

    def _approve_all(self):
        self.approve_safe()

    def _reject_all(self):
        self.reject_all()

    def approve_safe(self):
        """批准所有非危险项。危险操作必须逐项显式确认，批量不覆盖它们。"""
        for row in self._rows:
            if row.is_pending() and not row.is_dangerous():
                row._decide(True)

    def reject_all(self):
        """拒绝所有未决项。拒绝永远可以批量——它是可逆的那一侧。"""
        for row in self._rows:
            if row.is_pending():
                row._decide(False)

    # ── 给固定快捷条用的查询接口（v0.8.5）─────────────────────────

    def rows(self):
        return list(self._rows)

    def pending_stats(self):
        """返回 (待决数, 其中危险数)。"""
        pending = [r for r in self._rows if r.is_pending()]
        return len(pending), sum(1 for r in pending if r.is_dangerous())

    def first_pending_row(self):
        for row in self._rows:
            if row.is_pending():
                return row
        return None

    def fixed_height(self):
        """卡片里除各条差异区以外的开销：标题、提示、批量按钮、边距、间距。

        提示文案带 ``wordWrap``，在 460px 宽的面板里会折成三行；常数估
        104px 差了近 40px，正好够把底部按钮顶出视野。所以这里用实测的
        ``_hint.height()``（前提是先按最终宽度排过一次版）。
        """
        m = theme.current_theme().metrics
        hint = self._hint.height() if self._hint.height() > 1 \
            else self._hint.sizeHint().height()
        footer = max(self._all_ok.sizeHint().height(),
                     self._all_no.sizeHint().height())
        # 标题、提示、N 条操作、底部按钮 → N+3 个条目，N+2 个间距
        return (2 * (m["gap"] + 1) + self._title.sizeHint().height()
                + hint + footer + (len(self._rows) + 2) * m["gap"])

    def fit_to_height(self, available, width=None):
        """按可视高度给每条操作的差异区分配空间。

        卡片比视图高的时候，底部的「批准 / 拒绝」会落到折叠线以下：用户
        看到「等待批准」，却怎么也点不到按钮（v0.8.4 实拍，一个 200 行的
        diff 就够触发）。

        分三档让位，顺序是「先压差异，再折差异，最后才让卡片超出视图」：
          1. 还有富余 → 差异区按内容高度（上限 ``_max_diff``）
          2. 只够几行 → 压到能露出的行数
          3. 连最小差异都放不下 → 收起差异，只留徽章行与按钮
        按钮够不着比少看几行代码严重：差异一次点击就能重新展开，而一个
        被挤出视野的按钮会让 Agent 卡在一个点不到的决定上。
        """
        available = int(available or 0)
        if available <= 0 or not self._rows:
            return
        if width and width > 150:
            # 先按最终宽度排一次版，否则量到的是默认宽度下的折行高度
            self.resize(int(width), self.height())
            lay = self.layout()
            if lay is not None:
                lay.activate()

        fixed_card = self.fixed_height()
        row_fixed = self._rows[0].fixed_height()
        per_row = (available - fixed_card) // len(self._rows)
        room = per_row - row_fixed

        if room >= MIN_DIFF_HEIGHT:
            diff = int(min(self._max_diff, room))
            compact = False
        elif room >= ABS_MIN_DIFF:
            diff = int(room)
            compact = False
        else:
            diff = MIN_DIFF_HEIGHT
            compact = True

        for row in self._rows:
            row.set_compact(compact)
            if not compact:
                row.set_max_diff(diff)

    def _on_row_decided(self, approved):
        self.changed.emit()
        self._maybe_finish(approved)

    def _maybe_finish(self, _approved=None):
        if self._finished or any(r.is_pending() for r in self._rows):
            return
        self._finished = True
        approved = [r.tool_call() for r in self._rows if r.is_approved()]
        self._all_ok.setEnabled(False)
        self._all_no.setEnabled(False)
        self._title.setText(
            f"<b>已批准 {len(approved)} 项 / 拒绝 "
            f"{len(self._rows) - len(approved)} 项</b>")
        theme.set_prop(self._title, "state", "done")
        theme.set_prop(self, "state", "done")
        self.decided.emit(approved)

    def refresh_theme(self):
        """主题切换后由聊天面板统一调用。"""
        for row in self._rows:
            row.refresh_theme()
        state = "done" if self._finished else "pending"
        theme.set_prop(self, "state", state)
        theme.set_prop(self._title, "state", state)

    def set_checkpoint(self, cp_id):
        """执行完成后显示「撤销本步」按钮"""
        self._cp_id = cp_id
        if cp_id:
            self._undo_btn.setVisible(True)
            self._undo_btn.setEnabled(True)

    def _on_undo(self):
        if not self._cp_id:
            return
        self.undo_requested.emit(self._cp_id)
        self._undo_btn.setEnabled(False)
        self._undo_btn.setText("已撤销")
