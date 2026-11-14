# -*- coding: utf-8 -*-
"""
内联聊天 (Inline Chat) 面板

对标 GitHub Copilot Chat 的 Ctrl+I 内联聊天功能：
  在编辑器内直接与 AI 对话，针对特定代码进行修改

功能:
  - 在 Spyder 编辑器中打开浮动聊天窗口
  - 选中代码后自动作为上下文
  - 支持斜杠命令
  - 一键应用 AI 的修改建议
"""

import logging
import os

from qtpy.QtCore import Qt, Signal
from qtpy.QtWidgets import (
    QDialog, QHBoxLayout, QLabel, QPlainTextEdit, QPushButton,
    QScrollArea, QTextEdit, QToolButton, QVBoxLayout, QWidget,
)

from . import theme
from .markdown_renderer import render_markdown

logger = logging.getLogger(__name__)


class InlineChatDialog(QDialog):
    """内联聊天对话框 — 浮动在编辑器上方"""

    def __init__(self, editor_widget, parent=None):
        super().__init__(parent)
        self._editor = editor_widget
        self._setup_ui()
        self._load_selection()

        # 窗口设置
        self.setWindowFlags(
            Qt.Dialog | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint
        )
        theme.named(self, "standaloneRoot")
        theme.harmonize(self, standalone=True)

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(4)

        # ── 标题栏 ──
        title_bar = QWidget()
        title_lay = QHBoxLayout(title_bar)
        title_lay.setContentsMargins(0, 0, 0, 0)

        title = QLabel("内联聊天")
        title.setStyleSheet("color:#e0e0e0;font-weight:bold;font-size:12px;")
        title_lay.addWidget(title)
        title_lay.addStretch()

        close_btn = QToolButton()
        close_btn.setText("✕")
        close_btn.clicked.connect(self.reject)
        close_btn.setStyleSheet(
            "QToolButton{color:#888;border:none;font-size:12px;}"
            "QToolButton:hover{color:#fff;}")
        title_lay.addWidget(close_btn)

        layout.addWidget(title_bar)

        # ── 选中代码预览 ──
        self._selection_label = QLabel()
        self._selection_label.setStyleSheet(
            "color:#6a9955;font-size:11px;padding:2px 4px;"
            "background:#1e1e1e;border-radius:2px;")
        self._selection_label.setWordWrap(True)
        self._selection_label.setVisible(False)
        layout.addWidget(self._selection_label)

        # ── 输入框 ──
        self._input = QPlainTextEdit()
        self._input.setPlaceholderText("输入问题或指令... (Enter 发送, Shift+Enter 换行)")
        self._input.setMaximumHeight(100)
        self._input.setStyleSheet(
            "QPlainTextEdit{background:#1e1e1e;color:#e0e0e0;border:1px solid #444;"
            "border-radius:3px;padding:6px;font-size:12px;"
            "selection-background-color:#264f78;}"
        )
        layout.addWidget(self._input)

        # ── 回复区 ──
        self._response = QTextEdit()
        self._response.setReadOnly(True)
        self._response.setMaximumHeight(200)
        self._response.setStyleSheet(
            "QTextEdit{background:#1e1e1e;color:#e0e0e0;border:1px solid #333;"
            "border-radius:3px;padding:6px;font-size:12px;}")
        self._response.setVisible(False)
        layout.addWidget(self._response)

        # ── 按钮区 ──
        btn_bar = QHBoxLayout()
        btn_bar.setContentsMargins(0, 4, 0, 0)

        self._send_btn = QPushButton("发送 (Enter)")
        self._send_btn.clicked.connect(self._send)
        self._send_btn.setStyleSheet(
            "QPushButton{background:#0e639c;color:#fff;border:none;"
            "padding:4px 12px;border-radius:3px;font-size:11px;}"
            "QPushButton:hover{background:#1177bb;}"
            "QPushButton:disabled{background:#333;color:#666;}")
        btn_bar.addWidget(self._send_btn)

        self._apply_btn = QPushButton("📝 应用到编辑器")
        self._apply_btn.clicked.connect(self._apply_to_editor)
        self._apply_btn.setVisible(False)
        self._apply_btn.setStyleSheet(
            "QPushButton{background:#2d6a2d;color:#fff;border:none;"
            "padding:4px 12px;border-radius:3px;font-size:11px;}"
            "QPushButton:hover{background:#3a8a3a;}")
        btn_bar.addWidget(self._apply_btn)

        btn_bar.addStretch()

        self._cancel_btn = QPushButton("取消 (Esc)")
        self._cancel_btn.clicked.connect(self.reject)
        self._cancel_btn.setStyleSheet(
            "QPushButton{background:#4a4a4a;color:#e0e0e0;border:none;"
            "padding:4px 12px;border-radius:3px;font-size:11px;}"
            "QPushButton:hover{background:#5a5a5a;}")
        btn_bar.addWidget(self._cancel_btn)

        layout.addLayout(btn_bar)

    def _load_selection(self):
        """加载编辑器选中文本"""
        try:
            if self._editor and hasattr(self._editor, 'get_selected_text'):
                text = self._editor.get_selected_text()
            elif self._editor and hasattr(self._editor, 'selectedText'):
                text = self._editor.selectedText()
            else:
                text = ""

            if text:
                self._selected_text = text
                preview = text[:200].replace("\n", " ").strip()
                self._selection_label.setText(f"📎 选中: {preview}...")
                self._selection_label.setVisible(True)
            else:
                self._selected_text = ""
                self._selection_label.setVisible(False)
        except Exception:
            self._selected_text = ""
            self._selection_label.setVisible(False)

    def _send(self):
        """发送消息"""
        text = self._input.toPlainText().strip()
        if not text:
            return

        # 构建带选中代码的询问
        full_text = text
        if self._selected_text:
            full_text = f"{text}\n\n```\n{self._selected_text}\n```"

        self._send_btn.setEnabled(False)
        self._input.setEnabled(False)
        self._response.setVisible(True)
        self._response.setHtml(
            render_markdown(f"**你**: {text}\n\n---\n\n🤔 **AI 正在思考...\n")
        )

        # 使用简单的模拟响应
        # 实际使用中，可以通过信号发送到主聊天面板处理
        from ..copilot_client import chat_completion
        from ..auth import get_valid_copilot_token
        from .settings_dialog import get_provider_config

        config = get_provider_config()
        provider = config.get("provider", "deepseek")

        # 发射信号给主面板处理
        self._send_to_main_chat(text)

    def _send_to_main_chat(self, text):
        """将消息发送到主聊天面板"""
        parent = self.parent()
        while parent:
            if hasattr(parent, '_panel') and hasattr(parent._panel, '_send'):
                parent._panel._input.setPlainText(text)
                parent._panel._send()
                self.accept()
                return
            if hasattr(parent, '_on_inline_chat'):
                parent._on_inline_chat(text, self._selected_text)
                self.accept()
                return
            parent = parent.parent()

        # 如果找不到主面板，显示提示
        self._response.setHtml(
            render_markdown("⚠️ 无法连接到聊天面板，请在主面板中输入。")
        )
        self._send_btn.setEnabled(True)
        self._input.setEnabled(True)

    def _apply_to_editor(self):
        """将 AI 回复中的代码应用到编辑器"""
        content = self._response.toPlainText()
        # 提取代码块
        import re
        code_blocks = re.findall(r'```(?:\w+)?\n(.*?)```', content, re.DOTALL)
        if code_blocks:
            code = code_blocks[0]
        else:
            code = content

        try:
            if self._editor:
                if hasattr(self._editor, 'insert_text'):
                    self._editor.insert_text(self._editor.get_cursor(), code)
                elif hasattr(self._editor, 'insertPlainText'):
                    self._editor.insertPlainText(code)
                self.accept()
        except Exception as e:
            logger.debug("Apply to editor failed: %s", e)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Escape:
            self.reject()
        elif event.key() in (Qt.Key_Return, Qt.Key_Enter):
            if not (event.modifiers() & Qt.ShiftModifier):
                self._send()
                return
        super().keyPressEvent(event)

    @classmethod
    def open_inline_chat(cls, editor_widget, parent_widget):
        """在编辑器位置打开内联聊天"""
        dialog = cls(editor_widget, parent_widget)
        dialog.resize(500, 300)
        # 在父窗口中心显示
        if parent_widget:
            parent_rect = parent_widget.geometry()
            x = parent_rect.x() + (parent_rect.width() - 500) // 2
            y = parent_rect.y() + (parent_rect.height() - 300) // 3
            dialog.move(x, y)
        dialog.exec_()
