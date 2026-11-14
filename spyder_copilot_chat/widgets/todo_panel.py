# -*- coding: utf-8 -*-
"""可视化任务面板

对标 GitHub Copilot Chat / Cline 的 Todo 面板：
  - 显示在聊天区域上方，是一张跟随主题的卡片
  - 实时更新任务状态
  - 折叠/展开切换
  - 顶部进度条 + 完成计数

v0.7.8 重写：旧版把 ``#1a1a2e``、``12px``、``📋/✅/🔄`` 全部写死在内联
QSS 里，既不认主题也不认字号——用户把界面字号调到 18px，这里依旧是 12px
的彩色 emoji 小字，在暗色面板里显得又小又花。现在所有视觉都来自
:mod:`theme`：

  * 字号取 ``ui_font``（标题）/ ``ui_font - 1``（条目），行高与图标尺寸随之
    缩放，不再出现"正文 18px、任务 12px"的断层；
  * 状态图标改成矢量：完成 = 实心圆钩（背景色镂空），进行中 = 细环带内点，
    待开始 = 空心圆，取消 = 叉；颜色取 success / accent / text_faint；
  * 卡片底色 / 边框 / 圆角 / 进度条颜色全部是语义 token，亮暗主题都成立。
"""

from qtpy.QtCore import QSize, Qt
from qtpy.QtGui import QFontMetrics
from qtpy.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QProgressBar, QSizePolicy,
    QToolButton, QVBoxLayout, QWidget,
)

from . import theme as _theme
from .todo_manager import TodoManager

# 语义状态 → (图标名, 颜色 token)
_STATUS_ICON = {
    "completed": ("check_circle", "success"),
    "in-progress": ("progress", "accent"),
    "not-started": ("circle", "text_faint"),
    "cancelled": ("cross", "text_faint"),
}


class _ElidedLabel(QLabel):
    """按当前宽度省略的标签（QLabel 自己只会硬裁剪）。

    标题形如「任务进度 · 射击输入与更新/碰撞」，侧栏一窄就会被裁成半个字；
    这里保留全文，paint 前按宽度重新省略，tooltip 里放完整文本。
    """

    def __init__(self, text="", parent=None):
        super().__init__(text, parent)
        self._full = text

    def setText(self, text):  # noqa: N802 - Qt 接口
        self._full = text or ""
        super().setText(self._full)
        self._relayout()

    def full_text(self):
        return self._full

    def resizeEvent(self, event):  # noqa: N802 - Qt 接口
        super().resizeEvent(event)
        self._relayout()

    def _relayout(self):
        fm = QFontMetrics(self.font())
        shown = fm.elidedText(self._full, Qt.ElideRight,
                              max(40, self.width() - 2))
        if shown != super().text():
            super().setText(shown)


class TodoPanel(QFrame):
    """可折叠的任务进度面板"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._theme = _theme.current_theme()
        self._collapsed = False
        self._todos = []
        self._setup_ui()
        self._connect_todo_manager()

    # ── 构建 ──
    def _setup_ui(self):
        t = self._theme
        m = t.metrics
        self.setVisible(False)
        # 注意：这里绝不能自设 setStyleSheet("background: transparent")。
        # Qt 会把父控件上"没有选择器"的声明一并套到所有子控件上，
        # 结果是里面的 todoPanel 卡片也变成透明（v0.7.8 第一版实拍：
        # 整张卡片只剩 1px 边框，看着像没有背景）。用具名规则代替。
        _theme.named(self, "todoDock")
        # 本控件是**透明容器**：真正带底色/边框的是里面那张 todoPanel 卡片。
        # 这样卡片能左右留出与对话流一致的边距，收起时容器整体高度归零，
        # 不会在对话流上方留一条空白。
        outer = QVBoxLayout(self)
        outer.setContentsMargins(m["pad"], max(4, m["gap"] - 2), m["pad"], 0)
        outer.setSpacing(0)

        card = QFrame()
        _theme.named(card, "todoPanel")
        outer.addWidget(card)

        root = QVBoxLayout(card)
        root.setContentsMargins(m["pad"] + 2, max(4, m["gap"] - 1),
                                m["pad"], max(4, m["gap"] - 2))
        root.setSpacing(max(2, m["gap"] - 3))

        # ── 标题行：折叠箭头 + 图标 + 标题 + 计数 + 关闭 ──
        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        header.setSpacing(max(5, t.ui_font // 3))

        self._toggle_btn = QToolButton()
        _theme.named(self._toggle_btn, "iconBtn")
        self._toggle_btn.setFixedSize(20, 20)
        self._toggle_btn.setIconSize(QSize(14, 14))
        self._toggle_btn.setCursor(Qt.PointingHandCursor)
        self._toggle_btn.setAutoRaise(True)
        self._toggle_btn.clicked.connect(self._toggle)
        header.addWidget(self._toggle_btn)

        self._head_icon = QLabel()
        side = max(14, t.ui_font - 2)
        self._head_icon.setFixedSize(side, side)
        self._head_icon.setStyleSheet("background: transparent; border: none;")
        header.addWidget(self._head_icon)

        self._title_label = _ElidedLabel("任务进度")
        _theme.named(self._title_label, "todoTitle")
        self._title_label.setSizePolicy(
            QSizePolicy.Ignored, QSizePolicy.Preferred)
        self._title_label.setMinimumWidth(0)
        header.addWidget(self._title_label, 1)

        self._progress_label = QLabel("0/0")
        _theme.named(self._progress_label, "todoCount")
        header.addWidget(self._progress_label)

        self._close_btn = QToolButton()
        _theme.named(self._close_btn, "iconBtn")
        self._close_btn.setFixedSize(20, 20)
        self._close_btn.setIconSize(QSize(12, 12))
        self._close_btn.setCursor(Qt.PointingHandCursor)
        self._close_btn.setAutoRaise(True)
        self._close_btn.setToolTip("隐藏任务面板")
        self._close_btn.clicked.connect(self.hide)
        header.addWidget(self._close_btn)

        root.addLayout(header)

        # ── 进度条：4px 细线，跟着强调色 ──
        self._progress_bar = QProgressBar()
        _theme.named(self._progress_bar, "todoBar")
        self._progress_bar.setMaximum(100)
        self._progress_bar.setValue(0)
        self._progress_bar.setTextVisible(False)
        self._progress_bar.setFixedHeight(4)
        root.addWidget(self._progress_bar)

        # ── 任务列表容器 ──
        self._items_widget = QWidget()
        _theme.named(self._items_widget, "todoItems")
        self._items_layout = QVBoxLayout(self._items_widget)
        self._items_layout.setContentsMargins(0, 0, 0, 0)
        self._items_layout.setSpacing(max(2, t.metrics["gap"] - 2))
        root.addWidget(self._items_widget)

        self.refresh_theme(t)

    def _connect_todo_manager(self):
        """连接到全局 TodoManager 的信号"""
        manager = TodoManager()
        manager.todos_changed.connect(self._on_todos_changed)

    # ── 主题 ──
    def refresh_theme(self, t=None):
        """换主题 / 改字号后重画图标与间距。

        图标是带色位图，缓存键里含颜色与尺寸，所以换主题必须重新取。
        """
        t = t or _theme.current_theme()
        self._theme = t
        self._head_icon.setPixmap(
            _theme.make_icon("list", t["accent"],
                             max(14, t.ui_font - 2)).pixmap(
                                 QSize(max(14, t.ui_font - 2),
                                       max(14, t.ui_font - 2))))
        self._close_btn.setIcon(_theme.make_icon("close", t["text_faint"], 12))
        self._toggle_btn.setIcon(_theme.make_icon(
            "chevron_right" if self._collapsed else "chevron",
            t["text_faint"], 14))
        self._rebuild_items(self._todos)
        _theme.polish(self)

    # ── 数据 ──
    def _on_todos_changed(self, todos):
        """Todo 列表变更时刷新 UI"""
        self._todos = list(todos or [])
        if not self._todos:
            # 先把残留的行删掉再隐藏：否则面板再次出现时会先闪一帧旧任务
            self._rebuild_items([])
            self.setVisible(False)
            return

        self.setVisible(True)

        total = len(self._todos)
        done = sum(1 for x in self._todos if x["status"] == "completed")
        in_progress = [x for x in self._todos if x["status"] == "in-progress"]

        # 标题：进行中的那一条跟在后面，过长时由 _ElidedLabel 省略
        suffix = " · %s" % in_progress[0]["title"] if in_progress else ""
        full_title = "任务进度" + suffix
        self._title_label.setText(full_title)
        self._title_label.setToolTip(full_title)

        self._progress_label.setText("%d/%d" % (done, total))
        percent = int(done / total * 100) if total else 0
        self._progress_bar.setValue(percent)

        self._rebuild_items(self._todos)

    def _rebuild_items(self, todos):
        """重建任务列表项"""
        while self._items_layout.count():
            item = self._items_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.setParent(None)
                w.deleteLater()

        t = self._theme
        side = max(14, t.ui_font - 2)
        row_h = max(20, t.ui_font + 8)

        for todo in todos or []:
            status = todo.get("status", "not-started")
            icon_name, token = _STATUS_ICON.get(status,
                                                ("circle", "text_faint"))
            hole = t["surface"]      # 面板底色，实心钩子才能"镂空"
            icon = _theme.make_icon(icon_name, t[token], side, hole=hole)

            row = QWidget()
            row.setStyleSheet("background: transparent;")
            lay = QHBoxLayout(row)
            lay.setContentsMargins(max(4, t.ui_font // 4), 0, 2, 0)
            lay.setSpacing(max(6, t.ui_font // 3))

            mark = QLabel()
            mark.setFixedSize(side, side)
            mark.setPixmap(icon.pixmap(QSize(side, side)))
            mark.setStyleSheet("background: transparent; border: none;")
            lay.addWidget(mark)

            text = QLabel(str(todo.get("title", "")))
            _theme.named(text, "todoItem")
            _theme.set_prop(text, "status", status)
            text.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
            text.setMinimumWidth(0)
            text.setFixedHeight(row_h)
            text.setToolTip(str(todo.get("title", "")))
            lay.addWidget(text, 1)

            self._items_layout.addWidget(row)

        self._items_widget.setVisible(not self._collapsed)

    # ── 折叠 ──
    def _toggle(self):
        """折叠/展开切换"""
        self._collapsed = not self._collapsed
        self._items_widget.setVisible(not self._collapsed)
        self._progress_bar.setVisible(not self._collapsed)
        t = self._theme
        self._toggle_btn.setIcon(_theme.make_icon(
            "chevron_right" if self._collapsed else "chevron",
            t["text_faint"], 14))
