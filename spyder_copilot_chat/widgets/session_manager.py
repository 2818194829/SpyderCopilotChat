# -*- coding: utf-8 -*-
"""
会话管理对话框 — 增强版

对标 GitHub Copilot Chat 的 Session 管理面板:
  - 会话列表（标题、日期、消息数）
  - 多选批量删除
  - 按时间自动清理（1天/3天/5天/7天/1个月）
  - 搜索 / 加载会话
"""

import json
import logging
import os
import time
from datetime import datetime, timedelta

from qtpy.QtCore import Qt, Signal
from qtpy.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QHBoxLayout, QLabel,
    QLineEdit, QListWidget, QListWidgetItem, QMessageBox,
    QPushButton, QVBoxLayout, QWidget,
)

from .chat_history_manager import (
    list_chats, load_chat, delete_chat, search_chats,
    HISTORY_DIR,
)

logger = logging.getLogger(__name__)

# 自动清理配置
AUTO_CLEAN_CONFIG = os.path.join(
    os.path.expanduser("~"), ".spyder_copilot_chat", "auto_clean.json"
)

CLEAN_OPTIONS = [
    ("0", "关闭自动清理"),
    ("1", "1 天以前的会话"),
    ("3", "3 天以前的会话"),
    ("5", "5 天以前的会话"),
    ("7", "7 天以前的会话"),
    ("30", "1 个月以前的会话"),
]


def load_auto_clean_setting():
    """加载自动清理设置"""
    try:
        with open(AUTO_CLEAN_CONFIG, "r", encoding="utf-8") as f:
            return json.load(f).get("max_age_days", "0")
    except Exception:
        return "0"


def save_auto_clean_setting(max_age_days):
    """保存自动清理设置"""
    os.makedirs(os.path.dirname(AUTO_CLEAN_CONFIG), exist_ok=True)
    with open(AUTO_CLEAN_CONFIG, "w", encoding="utf-8") as f:
        json.dump({"max_age_days": max_age_days}, f)


def run_auto_clean(max_age_days):
    """执行自动清理，删除超过指定天数的会话"""
    if not max_age_days or max_age_days == "0":
        return 0

    try:
        max_age = int(max_age_days)
    except ValueError:
        return 0

    cutoff = time.time() - (max_age * 86400)
    deleted = 0

    if not os.path.isdir(HISTORY_DIR):
        return 0

    for fname in os.listdir(HISTORY_DIR):
        if not fname.endswith(".json"):
            continue
        filepath = os.path.join(HISTORY_DIR, fname)
        try:
            mtime = os.path.getmtime(filepath)
            if mtime < cutoff:
                os.remove(filepath)
                deleted += 1
                logger.debug("Auto-clean deleted: %s", fname)
        except OSError:
            continue

    if deleted:
        logger.info("Auto-clean: deleted %d old sessions", deleted)
    return deleted


class SessionListDialog(QDialog):
    """会话列表对话框 — 支持批量管理和自动清理"""

    session_selected = Signal(str)  # chat_id
    session_deleted = Signal(str)  # chat_id

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("聊天会话管理")
        self.setMinimumSize(600, 500)
        self._chats = []
        self._setup_ui()
        self._load_sessions()

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setSpacing(8)

        # ── 搜索栏 ──
        search_row = QHBoxLayout()
        self._search_input = QLineEdit()
        self._search_input.setPlaceholderText("搜索会话...")
        self._search_input.textChanged.connect(self._on_search)
        self._search_input.setStyleSheet(
            "QLineEdit{background:#333;color:#e0e0e0;border:1px solid #555;"
            "border-radius:4px;padding:6px 10px;font-size:13px;}"
        )
        search_row.addWidget(self._search_input)

        new_btn = QPushButton("✚ 新建")
        new_btn.clicked.connect(self.accept)
        new_btn.setStyleSheet(
            "QPushButton{background:#0e639c;color:#fff;border:none;"
            "padding:6px 14px;border-radius:4px;font-size:12px;}"
            "QPushButton:hover{background:#1177bb;}"
        )
        search_row.addWidget(new_btn)
        layout.addLayout(search_row)

        # ── 会话列表（多选）─
        self._list = QListWidget()
        self._list.setSelectionMode(QListWidget.ExtendedSelection)
        self._list.setStyleSheet(
            "QListWidget{background:#1e1e1e;color:#e0e0e0;border:1px solid #333;"
            "border-radius:4px;font-size:13px;}"
            "QListWidget::item{padding:8px 12px;border-bottom:1px solid #2a2a2a;}"
            "QListWidget::item:hover{background:#2a2d3a;}"
            "QListWidget::item:selected{background:#094771;}"
        )
        self._list.itemDoubleClicked.connect(self._on_select)
        layout.addWidget(self._list, 1)

        # 选中计数
        count_row = QHBoxLayout()
        self._select_count = QLabel("未选中")
        self._select_count.setStyleSheet("color:#888;font-size:11px;")
        count_row.addWidget(self._select_count)
        count_row.addStretch()

        select_all = QPushButton("全选")
        select_all.clicked.connect(self._select_all)
        select_all.setStyleSheet(
            "QPushButton{color:#888;background:transparent;border:1px solid #555;"
            "padding:2px 10px;border-radius:3px;font-size:11px;}"
            "QPushButton:hover{color:#fff;}"
        )
        count_row.addWidget(select_all)

        deselect_all = QPushButton("取消全选")
        deselect_all.clicked.connect(self._deselect_all)
        deselect_all.setStyleSheet(select_all.styleSheet())
        count_row.addWidget(deselect_all)
        layout.addLayout(count_row)

        # 列表选择变化时更新计数
        self._list.itemSelectionChanged.connect(self._update_select_count)

        # ── 自动清理栏 ──
        clean_row = QHBoxLayout()
        clean_label = QLabel("🕐 自动清理:")
        clean_label.setStyleSheet("color:#888;font-size:12px;")
        clean_row.addWidget(clean_label)

        self._clean_combo = QComboBox()
        for value, label in CLEAN_OPTIONS:
            self._clean_combo.addItem(label, value)
        current = load_auto_clean_setting()
        for i, (val, _) in enumerate(CLEAN_OPTIONS):
            if val == current:
                self._clean_combo.setCurrentIndex(i)
                break
        self._clean_combo.currentIndexChanged.connect(self._on_clean_setting_changed)
        self._clean_combo.setStyleSheet(
            "QComboBox{background:#333;color:#e0e0e0;border:1px solid #555;"
            "border-radius:4px;padding:4px 8px;font-size:12px;}"
            "QComboBox:hover{border-color:#888;}"
            "QComboBox::drop-down{border:none;}"
        )
        clean_row.addWidget(self._clean_combo)

        clean_now = QPushButton("立即清理")
        clean_now.clicked.connect(self._clean_now)
        clean_now.setStyleSheet(
            "QPushButton{background:#444;color:#ccc;border:1px solid #666;"
            "padding:4px 12px;border-radius:4px;font-size:11px;}"
            "QPushButton:hover{background:#555;}"
        )
        clean_row.addWidget(clean_now)
        clean_row.addStretch()
        layout.addLayout(clean_row)

        # ── 操作按钮 ──
        btn_row = QHBoxLayout()

        self._load_btn = QPushButton("📂 加载会话")
        self._load_btn.clicked.connect(self._on_select)
        self._load_btn.setStyleSheet(
            "QPushButton{background:#0e639c;color:#fff;border:none;"
            "padding:8px 20px;border-radius:4px;font-size:13px;}"
            "QPushButton:hover{background:#1177bb;}"
        )
        btn_row.addWidget(self._load_btn)

        self._batch_delete_btn = QPushButton("🗑 批量删除")
        self._batch_delete_btn.clicked.connect(self._on_batch_delete)
        self._batch_delete_btn.setStyleSheet(
            "QPushButton{background:#5a1a1a;color:#e0a0a0;border:none;"
            "padding:8px 20px;border-radius:4px;font-size:13px;}"
            "QPushButton:hover{background:#8b2020;color:#fff;}"
        )
        btn_row.addWidget(self._batch_delete_btn)

        btn_row.addStretch()

        close_btn = QPushButton("关闭")
        close_btn.clicked.connect(self.reject)
        close_btn.setStyleSheet(
            "QPushButton{background:#333;color:#ccc;border:1px solid #555;"
            "padding:8px 20px;border-radius:4px;font-size:13px;}"
            "QPushButton:hover{background:#444;}"
        )
        btn_row.addWidget(close_btn)
        layout.addLayout(btn_row)

    # ── 列表操作 ──

    def _load_sessions(self):
        self._chats = list_chats(500)
        self._refresh_list()

    def _refresh_list(self):
        self._list.clear()
        for chat in self._chats:
            title = chat.get("title", "无标题")[:60]
            created = chat.get("created", "")[:16].replace("T", " ")
            count = chat.get("message_count", 0)
            display = f"{title}\n  {created}  ·  {count} 条消息"
            item = QListWidgetItem(display)
            item.setData(Qt.UserRole, chat["id"])
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Unchecked)
            self._list.addItem(item)

    def _update_select_count(self):
        count = len(self._list.selectedItems())
        total = self._list.count()
        self._select_count.setText(f"已选 {count}/{total}")

    def _select_all(self):
        self._list.selectAll()

    def _deselect_all(self):
        self._list.clearSelection()

    def _on_search(self, text):
        keyword = text.strip()
        self._chats = list_chats(500) if not keyword else search_chats(keyword)
        self._refresh_list()

    def _get_selected_ids(self):
        """获取所有选中项的 ID 列表"""
        return [item.data(Qt.UserRole) for item in self._list.selectedItems()]

    def _on_select(self):
        ids = self._get_selected_ids()
        if ids:
            self.session_selected.emit(ids[0])
            self.accept()

    def _on_batch_delete(self):
        """批量删除选中的会话"""
        ids = self._get_selected_ids()
        if not ids:
            QMessageBox.information(self, "提示", "请先选中要删除的会话")
            return

        reply = QMessageBox.question(
            self, "确认批量删除",
            f"确定要删除选中的 {len(ids)} 个会话吗？此操作不可撤销。",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return

        deleted = 0
        for cid in ids:
            if delete_chat(cid):
                self.session_deleted.emit(cid)
                deleted += 1

        self._load_sessions()
        QMessageBox.information(
            self, "完成",
            f"成功删除 {deleted}/{len(ids)} 个会话"
        )

    # ── 自动清理 ──

    def _on_clean_setting_changed(self, index):
        value = self._clean_combo.currentData()
        save_auto_clean_setting(value)
        if value != "0":
            label = self._clean_combo.currentText()
            logger.info("Auto-clean set to: %s", label)

    def _clean_now(self):
        value = self._clean_combo.currentData()
        if value == "0":
            QMessageBox.information(self, "提示", "自动清理尚未启用")
            return

        label = self._clean_combo.currentText()
        reply = QMessageBox.question(
            self, "确认清理",
            f"将删除 {label} 的所有聊天记录。\n此操作不可撤销，确认继续？",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return

        deleted = run_auto_clean(value)
        self._load_sessions()
        QMessageBox.information(
            self, "清理完成",
            f"已删除 {deleted} 个过期会话"
        )

