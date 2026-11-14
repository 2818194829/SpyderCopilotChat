# -*- coding: utf-8 -*-
"""
代码审查面板

对标 GitHub Copilot Chat 的 Code Review 功能：
  - 审查未暂存/已暂存的更改
  - 逐文件查看 diff，支持 Apply/Discard
  - 每个 hunk 可独立应用
"""

import logging
import os

from qtpy.QtCore import Qt, Signal
from qtpy.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QListWidget, QListWidgetItem,
    QMessageBox, QSplitter, QTextEdit, QToolButton, QVBoxLayout, QWidget,
)

from .markdown_renderer import render_markdown
from .tools.git_tools import review_changes, get_file_diff, is_git_repo

logger = logging.getLogger(__name__)


class CodeReviewPanel(QWidget):
    """代码审查面板"""
    changes_applied = Signal(list)     # list of result strings
    changes_discarded = Signal(list)   # list of file paths

    def __init__(self, parent=None):
        super().__init__(parent)
        self._base_dir = None
        self._review_data = None
        self._current_file_index = 0
        self._setup_ui()

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # ── 标题栏 ──
        title_bar = QWidget()
        title_bar.setStyleSheet("background:#1e1e1e;border-bottom:1px solid #333;")
        title_lay = QHBoxLayout(title_bar)
        title_lay.setContentsMargins(8, 4, 8, 4)

        title_label = QLabel("📋 代码审查")
        title_label.setStyleSheet("color:#e0e0e0;font-weight:bold;font-size:13px;")
        title_lay.addWidget(title_label)
        title_lay.addStretch()

        btn_base = (
            "QToolButton{color:#888;border:1px solid #444;border-radius:3px;"
            "padding:2px 8px;font-size:11px;}"
            "QToolButton:hover{color:#fff;border-color:#888;}"
            "QToolButton:disabled{color:#555;border-color:#333;}")

        self._refresh_btn = QToolButton()
        self._refresh_btn.setText("🔄 刷新")
        self._refresh_btn.clicked.connect(self.refresh)
        self._refresh_btn.setStyleSheet(btn_base)
        title_lay.addWidget(self._refresh_btn)

        self._staged_btn = QToolButton()
        self._staged_btn.setText("📌 已暂存")
        self._staged_btn.setCheckable(True)
        self._staged_btn.clicked.connect(lambda: self.refresh(staged=True))
        self._staged_btn.setStyleSheet(btn_base)
        title_lay.addWidget(self._staged_btn)

        # ── Apply/Discard 按钮 ──
        self._apply_all_btn = QToolButton()
        self._apply_all_btn.setText("✅ 应用所有")
        self._apply_all_btn.clicked.connect(self._apply_all_changes)
        self._apply_all_btn.setStyleSheet(btn_base)
        self._apply_all_btn.setVisible(False)
        title_lay.addWidget(self._apply_all_btn)

        self._discard_btn = QToolButton()
        self._discard_btn.setText("✕ 丢弃文件")
        self._discard_btn.clicked.connect(self._discard_file_changes)
        self._discard_btn.setStyleSheet(btn_base)
        self._discard_btn.setVisible(False)
        title_lay.addWidget(self._discard_btn)

        close_btn = QToolButton()
        close_btn.setText("✕")
        close_btn.clicked.connect(self.hide)
        close_btn.setStyleSheet(
            "QToolButton{color:#888;border:none;font-size:14px;}"
            "QToolButton:hover{color:#fff;}")
        title_lay.addWidget(close_btn)

        layout.addWidget(title_bar)

        # ── 主区域 ──
        splitter = QSplitter(Qt.Horizontal)

        # 左侧文件列表
        self._file_list = QListWidget()
        self._file_list.setStyleSheet(
            "QListWidget{background:#252526;color:#e0e0e0;border:none;font-size:12px;}"
            "QListWidget::item{padding:6px 10px;border-bottom:1px solid #333;}"
            "QListWidget::item:selected{background:#094771;}")
        self._file_list.currentRowChanged.connect(self._on_file_selected)
        splitter.addWidget(self._file_list)

        # 右侧 diff 视图
        self._diff_view = QTextEdit()
        self._diff_view.setReadOnly(True)
        self._diff_view.setStyleSheet(
            "QTextEdit{background:#1e1e1e;color:#e0e0e0;border:none;"
            "font-family:'Consolas','Courier New',monospace;font-size:12px;}")
        splitter.addWidget(self._diff_view)

        splitter.setSizes([200, 600])
        layout.addWidget(splitter, 1)

        # ── 底部状态栏 ──
        self._status_bar = QLabel("点击「刷新」开始审查")
        self._status_bar.setStyleSheet(
            "color:#888;font-size:11px;padding:4px 8px;"
            "background:#1e1e1e;border-top:1px solid #333;")
        layout.addWidget(self._status_bar)

    def set_base_dir(self, path):
        """设置项目根目录"""
        self._base_dir = path

    def refresh(self, staged=False):
        """刷新审查结果"""
        base_dir = self._base_dir or os.getcwd()

        if not is_git_repo(base_dir):
            self._status_bar.setText("⚠️ 当前目录不是 Git 仓库")
            self._file_list.clear()
            self._diff_view.clear()
            self._apply_all_btn.setVisible(False)
            self._discard_btn.setVisible(False)
            return

        self._review_data = review_changes(base_dir, staged=staged)
        self._current_file_index = 0
        self._populate_file_list()

        if self._review_data["files"]:
            self._show_diff(0)
            self._status_bar.setText(
                f"{self._review_data['summary']}  "
                f"共 {len(self._review_data['files'])} 个文件"
            )
            self._apply_all_btn.setVisible(True)
            self._discard_btn.setVisible(True)
        else:
            self._status_bar.setText("✅ 无代码更改需要审查")
            self._apply_all_btn.setVisible(False)
            self._discard_btn.setVisible(False)

    def _populate_file_list(self):
        """填充文件列表"""
        self._file_list.blockSignals(True)
        self._file_list.clear()

        if not self._review_data or not self._review_data["files"]:
            self._file_list.blockSignals(False)
            return

        status_icons = {
            "M": "📝", "A": "🆕", "D": "🗑", "R": "🔀",
            "?": "❓", "": "📝",
        }

        for f in self._review_data["files"]:
            icon = status_icons.get(f["status"], "📄")
            item = QListWidgetItem(f"{icon}  {f['path']}")
            self._file_list.addItem(item)

        self._file_list.blockSignals(False)

    def _on_file_selected(self, index):
        """文件选择变更"""
        if index >= 0:
            self._show_diff(index)

    def _show_diff(self, index):
        """显示指定文件的 diff"""
        if not self._review_data or index >= len(self._review_data["files"]):
            return

        file_info = self._review_data["files"][index]
        base_dir = self._base_dir or os.getcwd()
        filepath = os.path.join(base_dir, file_info["path"])

        diff = get_file_diff(filepath, base_dir)

        if diff:
            # 语法高亮 diff，解析 hunks 添加操作按钮
            html = "<pre style='font-family:Consolas,monospace;font-size:12px;'>"
            current_hunk = []
            in_hunk = False

            for line in diff.split("\n"):
                if line.startswith("@@"):
                    # 上一个 hunk 结束，输出操作按钮
                    if in_hunk and current_hunk:
                        html += self._hunk_apply_button(current_hunk, file_info["path"])
                        current_hunk = []
                    in_hunk = True
                    html += f"<span style='color:#569cd6'>{line}</span>\n"
                elif line.startswith("+"):
                    html += f"<span style='color:#6a9955'>{line}</span>\n"
                    if in_hunk:
                        current_hunk.append(line)
                elif line.startswith("-"):
                    html += f"<span style='color:#f14c4c'>{line}</span>\n"
                    if in_hunk:
                        current_hunk.append(line)
                else:
                    if in_hunk and current_hunk:
                        current_hunk.append(line)
                    html += f"{line}\n"

            # 最后一个 hunk
            if in_hunk and current_hunk:
                html += self._hunk_apply_button(current_hunk, file_info["path"])

            html += "</pre>"
            self._diff_view.setHtml(html)
        else:
            self._diff_view.setPlainText("(无更改)")

    def _hunk_apply_button(self, hunk_lines, filepath):
        """生成单个 hunk 的 Apply 按钮 HTML"""
        hunk_text = "\n".join(hunk_lines)
        # 使用一个可点击的链接样式的按钮
        return (
            f"<br><a href='apply_hunk:{filepath}' "
            f"style='color:#4ec9b0;text-decoration:none;font-size:11px;"
            f"background:#2d2d2d;padding:1px 8px;border:1px solid #4ec9b0;"
            f"border-radius:3px;' "
            f"onclick='window.location.href=\"apply_hunk:{filepath}\"'>"
            f"✅ Apply this hunk</a><br>"
        )

    # ── 应用/丢弃操作 ──

    def _apply_all_changes(self):
        """应用所有审查的更改"""
        if not self._review_data or not self._review_data["files"]:
            return

        from .tools.apply_patch import apply_patch
        from .tools.git_tools import get_file_diff

        results = []
        for file_info in self._review_data["files"]:
            filepath = os.path.join(self._base_dir or os.getcwd(), file_info["path"])
            diff = get_file_diff(filepath, self._base_dir)
            if diff:
                try:
                    result = apply_patch(diff, self._base_dir)
                    results.append(f"{file_info['path']}: {result}")
                except Exception as e:
                    results.append(f"{file_info['path']}: ❌ 应用失败 - {e}")

        summary = "\n".join(results)
        self._status_bar.setText(f"✅ 已应用 {len(results)} 个文件")
        self.changes_applied.emit(results)
        QMessageBox.information(self, "应用结果", summary)

    def _discard_file_changes(self):
        """丢弃当前选中的文件的更改 (git checkout)"""
        current_idx = self._file_list.currentRow()
        if current_idx < 0:
            return
        file_info = self._review_data["files"][current_idx]
        filepath = file_info["path"]

        reply = QMessageBox.question(
            self, "确认丢弃",
            f"确定要丢弃 **{filepath}** 的更改吗？\n此操作不可撤销。",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        )
        if reply == QMessageBox.Yes:
            from .tools.git_tools import _run_git
            _run_git(["checkout", "--", filepath], self._base_dir)
            self._status_bar.setText(f"🗑 已丢弃: {filepath}")
            self.changes_discarded.emit([filepath])
            self.refresh()

    def navigate_next(self):
        """导航到下一条建议"""
        if self._file_list.count() > 0:
            next_idx = min(self._current_file_index + 1, self._file_list.count() - 1)
            self._current_file_index = next_idx
            self._file_list.setCurrentRow(next_idx)

    def navigate_previous(self):
        """导航到上一条建议"""
        if self._file_list.count() > 0:
            prev_idx = max(self._current_file_index - 1, 0)
            self._current_file_index = prev_idx
            self._file_list.setCurrentRow(prev_idx)
