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

from qtpy.QtCore import QTimer, Signal
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

    seen, result = set(), []
    for p in paths:
        if p and p not in seen:
            seen.add(p)
            result.append(p)
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

    def __init__(self, tool_call, preview, parent=None):
        super().__init__(parent)
        self._tc = tool_call
        self._state = "pending"
        self._danger = preview.get("risk") == "danger"
        self._raw_html = preview["html"]
        self._html_palette = theme.html_colors()
        m = theme.current_theme().metrics
        theme.named(self, "approvalRow")
        theme.set_prop(self, "risk", "danger" if self._danger else preview["risk"])
        self.setFrameShape(QFrame.NoFrame)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(m["pad"], m["gap"], m["pad"], m["gap"])
        lay.setSpacing(max(3, m["gap"] - 2))

        head = QHBoxLayout()
        head.setSpacing(6)
        label, kind = RISK_LABELS.get(preview["risk"], RISK_LABELS["read"])
        head.addWidget(theme.badge(label, kind))
        if self._danger:
            warn = theme.badge("需单独确认", "danger")
            warn.setToolTip("不可逆或影响外部系统的操作，「全部批准」不会覆盖它")
            head.addWidget(warn)

        title = QLabel(html.escape(preview["name"]))
        theme.named(title, "toolTitle")
        head.addWidget(title)

        path_lbl = QLabel(html.escape(preview["headline"] or "(无路径)"))
        theme.named(path_lbl, "approvalPath")
        path_lbl.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        path_lbl.setToolTip(preview["path"] or preview["headline"])
        head.addWidget(path_lbl, 1)

        self._state_lbl = QLabel("待批准")
        theme.named(self._state_lbl, "stateLabel")
        theme.set_prop(self._state_lbl, "state", "pending")
        head.addWidget(self._state_lbl)

        self._toggle = theme.icon_button("chevron", "收起差异", 22)
        self._toggle.setCheckable(True)
        self._toggle.setChecked(True)
        self._toggle.clicked.connect(self._on_toggle)
        head.addWidget(self._toggle)
        lay.addLayout(head)

        self._view = QTextEdit()
        theme.named(self._view, "diffView")
        self._view.setReadOnly(True)
        self._view.setMaximumHeight(190)
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
        """
        doc = self._view.document()
        width = max(160, self._view.viewport().width() - 4)
        doc.setTextWidth(width)
        height = int(doc.size().height()) + 10
        height = min(190, max(52, height))
        if abs(self._view.height() - height) > 1:
            self._view.setFixedHeight(height)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._fit_diff_height()

    def _on_toggle(self, checked):
        self._view.setVisible(checked)
        self._toggle.setToolTip("收起差异" if checked else "展开差异")

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

    def __init__(self, tool_calls, base_dir=None, parent=None):
        super().__init__(parent)
        self._cp_id = None
        self._finished = False
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
            row = ApprovalRow(tc, build_preview(tc, base_dir), self)
            row.decided.connect(self._maybe_finish)
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
        # 危险操作必须逐项显式确认，批量按钮不覆盖它们
        for row in self._rows:
            if row.is_pending() and not row.is_dangerous():
                row._decide(True)

    def _reject_all(self):
        for row in self._rows:
            if row.is_pending():
                row._decide(False)

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
