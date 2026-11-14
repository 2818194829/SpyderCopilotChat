# -*- coding: utf-8 -*-
"""
审计日志查看器 — v0.5.6 / P1

工具栏「🛡 审计」打开：按事件类型过滤最近记录、刷新、
导出 CSV、打开日志目录。只读，不改动聊天状态。
"""

import json
import os
import subprocess
import sys

from qtpy.QtWidgets import (
    QComboBox, QDialog, QFileDialog, QHBoxLayout, QLabel,
    QPlainTextEdit, QPushButton, QVBoxLayout,
)

from .audit import get_audit_log, audit_dir

EVENT_LABELS = {
    "": "全部事件",
    "task_start": "任务开始",
    "request": "模型请求",
    "tool_requested": "工具调用",
    "approval_requested": "请求审批",
    "approval_decision": "审批决定",
    "blocked": "安全拦截",
    "tool_result": "工具结果",
    "verification": "自动验证",
    "usage": "用量",
    "redirect": "中途转向",
    "cancelled": "取消",
    "task_done": "任务完成",
    "error": "错误",
}


class AuditViewerDialog(QDialog):
    def __init__(self, parent=None, limit=400):
        super().__init__(parent)
        self._limit = limit
        self.setWindowTitle("审计日志 — Spyder Copilot Chat")
        self.resize(860, 560)
        lay = QVBoxLayout(self)

        head = QHBoxLayout()
        head.addWidget(QLabel("事件类型："))
        self._filter = QComboBox()
        for key, label in EVENT_LABELS.items():
            self._filter.addItem(label, key)
        self._filter.currentIndexChanged.connect(self._refresh)
        head.addWidget(self._filter)
        head.addStretch()

        self._refresh_btn = QPushButton("刷新")
        self._refresh_btn.clicked.connect(self._refresh)
        head.addWidget(self._refresh_btn)

        self._open_btn = QPushButton("打开日志目录")
        self._open_btn.clicked.connect(self._open_dir)
        head.addWidget(self._open_btn)

        self._export_btn = QPushButton("导出 CSV")
        self._export_btn.clicked.connect(self._export)
        head.addWidget(self._export_btn)
        lay.addLayout(head)

        self._view = QPlainTextEdit()
        self._view.setReadOnly(True)
        self._view.setStyleSheet(
            "QPlainTextEdit{background:#1e1e1e;color:#ccc;"
            "font-family:Consolas,monospace;font-size:11px;border:1px solid #3c3c3c;}")
        lay.addWidget(self._view, 1)

        self._status = QLabel("")
        self._status.setStyleSheet("color:#888;font-size:10px;")
        lay.addWidget(self._status)
        self._refresh()

    def _refresh(self):
        log = get_audit_log()
        log.flush()
        events = log.read_events(limit=max(self._limit, 2000))
        wanted = self._filter.currentData() or ""
        if wanted:
            events = [e for e in events if e.get("event") == wanted]
        lines = []
        for e in events:
            detail = {k: v for k, v in e.items()
                      if k not in ("ts", "epoch", "session", "event")}
            label = EVENT_LABELS.get(e.get("event", ""), e.get("event", ""))
            raw = json.dumps(detail, ensure_ascii=False)
            if len(raw) > 400:
                raw = raw[:400] + "…"
            lines.append(f"[{e.get('ts', '')}] {label:<6} | {raw}")
        self._view.setPlainText("\n".join(lines) or "（暂无审计记录）")
        self._status.setText(
            f"共 {len(events)} 条 · 日志目录 {audit_dir()} · "
            f"启用状态：{'开' if log.enabled else '关'}")

    def _open_dir(self):
        path = audit_dir()
        try:
            os.makedirs(path, exist_ok=True)
            if sys.platform == "win32":
                os.startfile(path)
            elif sys.platform == "darwin":
                subprocess.Popen(["open", path])
            else:
                subprocess.Popen(["xdg-open", path])
        except Exception:
            pass

    def _export(self):
        path, _ = QFileDialog.getSaveFileName(
            self, "导出审计日志", "copilot-audit.csv", "CSV (*.csv)")
        if not path:
            return
        try:
            n = get_audit_log().export_csv(path, limit=10000)
            self._status.setText(f"已导出 {n} 条到 {path}")
        except Exception as exc:
            self._status.setText(f"导出失败: {exc}")
