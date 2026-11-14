# -*- coding: utf-8 -*-
"""
Copilot Chat 聊天界面 — 完整功能版

集成功能：
  - 多 Provider 聊天（DeepSeek / Copilot / OpenAI / Anthropic / Ollama 等）
  - 流式输出 + Markdown 渲染
  - Agent 模式（工具定义 + 执行）
  - 斜杠命令（/fix, /explain, /tests 等）
  - 聊天历史管理（保存 / 加载 / 清除）
  - 项目上下文附件
  - 自定义系统提示词
"""

import json
import logging
import os
import re
import time
import weakref

from qtpy.QtCore import (
    QEvent, QPoint, QRect, QRectF, Qt, Signal, QThread, QTimer, QSize,
)
from qtpy.QtGui import (
    QColor, QCursor, QFont, QKeySequence, QPainter, QPixmap, QTextCursor,
)
from qtpy.QtWidgets import (
    QAction, QApplication, QDialog, QFileDialog, QFrame, QHBoxLayout,
    QLabel, QListWidget, QListWidgetItem, QMenu, QMessageBox,
    QPlainTextEdit, QProgressBar, QPushButton, QScrollArea, QSizePolicy,
    QTextEdit, QToolButton, QToolTip, QVBoxLayout, QWidget,
)

from . import theme

from spyder.api.widgets.main_widget import PluginMainWidget

from ..auth import get_valid_copilot_token, load_tokens
from .. import api_errors, safe_logging
from ..api_errors import ApiError
from ..copilot_client import (
    AGENT_TOOLS,
    TOOL_GROUP_READ, TOOL_GROUP_SEARCH, TOOL_GROUP_PLAN,
    TOOL_GROUP_REVIEW, TOOL_GROUP_WEB,
)
from .agentic_actions import execute_tool, get_mcp_tool_definitions
from . import attachments
from .approval_card import (
    ApprovalCard, needs_approval, EXECUTE_TOOLS,
    SHELL_TOOLS, SOURCE_SUFFIXES,
)
from .audit import configure_audit, get_audit_log
from .audit_viewer import AuditViewerDialog
from .meta_tools import META_TOOL_DEFINITIONS, META_TOOL_NAMES
from .repo_index import (
    ensure_index_async, format_repo_map, index_summary, invalidate,
)
from .rules import format_rules_prompt
from .security import approval_required, classify_tool, parse_args
from .skills import skills_catalog_prompt
from .checkpoints import CheckpointStore
from .chat_history_manager import save_chat, load_chat
from .commands import BUILTIN_COMMANDS
from .custom_agents import (
    get_agents_system_prompt,
    get_agent_config, get_builtin_agents,
)
from .custom_instructions import get_effective_instructions
from .participants import (
    ParticipantManager,
    parse_participant,
)
from .project_context import collect_project_files, format_file_for_context
from .settings_dialog import (
    SettingsDialog,
    fetch_copilot_models_async,
    get_cached_copilot_models,
    get_copilot_default_model,
    get_provider_config,
    load_config,
    load_system_prompt,
    save_config,
    set_cached_copilot_models,
)
from .session_manager import SessionListDialog
from .task_runtime import (
    UsageMeter, TaskStateStore, submit, execute_tool_batch,
    run_verification, changed_files_from_calls, new_cancel_event,
    WRITE_TOOLS,
)
from .todo_panel import TodoPanel
from .todo_manager import TodoManager
# Markdown 渲染器（Pygments 语法高亮 + 块级缓存）
from .markdown_renderer import extract_code_blocks, render_markdown
# 补丁 → 行模型（对话内红绿卡片与审批卡共用同一套解析）
from . import diff_view as _diff
from .stream_renderer import StreamRenderer
# Prompts & Skills 系统
from ..prompts import get_skills_system_prompt

logger = logging.getLogger(__name__)


# Plan 模式白名单：只允许「看」和「想」，绝不暴露任何会改动磁盘/执行命令的工具。
# 采用白名单而非黑名单，新增工具默认在 Plan 模式下不可见。
PLAN_ALLOWED_TOOLS = set(
    TOOL_GROUP_READ + TOOL_GROUP_SEARCH + TOOL_GROUP_PLAN
    + TOOL_GROUP_REVIEW + TOOL_GROUP_WEB
    + [
        "read_project_structure", "find_test_files",
        "search_workspace_symbols", "view_image",
        "get_notebook_info", "notebook_get_output",
        "get_changed_files", "git_log", "detect_conflicts",
        "get_symbol_definitions", "get_code_outline",
        "find_related_symbols", "get_call_hierarchy",
        "get_symbol_at_position", "list_sessions", "memory_recall",
    ]
) | set(META_TOOL_NAMES)

# ── 目录级兜底快照（v0.8.7）────────────────────────────────────────────
# 弱模型用 run_command 写盘时，工具参数里没有可解析的文件路径。事后
# 靠 mtime 比对找出真正的改动，是唯一不依赖「模型怎么写字」的办法。
_SCAN_SKIP_DIRS = {
    ".git", ".hg", ".svn", "__pycache__", "node_modules", ".venv", "venv",
    "env", "dist", "build", ".mypy_cache", ".pytest_cache", ".ruff_cache",
    ".idea", ".vscode", ".ipynb_checkpoints", "site-packages", ".tox",
    ".eggs",
}
_SCAN_MAX_FILES = 4000
_SCAN_MAX_BYTES = 12 * 1024 * 1024
_SCAN_MAX_FILE = 512 * 1024


def _scan_sources(root):
    """扫一遍项目源文件，返回 ``{绝对路径: (mtime_ns, size)}``。

    只在「这一批工具调用可能写盘但路径算不出来」时才调用。上限是硬
    的：4000 个文件 / 12 MB / 单文件 512 KB，超了就截断 —— 宁可漏掉
    冷门文件，也不能让主线程在这里卡住。
    """
    if not root or not os.path.isdir(root):
        return {}
    out = {}
    budget = _SCAN_MAX_BYTES
    for base, dirs, names in os.walk(root):
        dirs[:] = [d for d in dirs
                   if d not in _SCAN_SKIP_DIRS and not d.startswith(".")]
        for name in names:
            if not name.lower().endswith(SOURCE_SUFFIXES):
                continue
            path = os.path.join(base, name)
            try:
                st = os.stat(path)
            except OSError:
                continue
            if st.st_size > _SCAN_MAX_FILE:
                continue
            budget -= st.st_size
            if budget < 0 or len(out) >= _SCAN_MAX_FILES:
                return out
            out[path] = (st.st_mtime_ns, st.st_size)
    return out


def _batch_may_write_blindly(tool_calls):
    """这一批里有没有「会写盘、但参数里没有文件路径」的工具。"""
    for tc in tool_calls or []:
        name = (tc.get("function") or {}).get("name", "")
        if name in SHELL_TOOLS:
            return True
    return False


def _format_api_error(exc, provider="", model=""):
    """异常 → 一行结论（状态栏 / 审计用）。

    v0.8.13 起真正的判断在 :mod:`spyder_copilot_chat.api_errors`：这里只
    保留旧函数名，避免外部调用方（脚本、旧测试）一起改。旧实现拿
    ``"api 402" in text`` 一把抓，把 DeepSeek 欠费说成「Copilot 额度已用尽，
    请升级订阅」——方向完全错，用户照着修当然修不好。
    """
    return api_errors.summary_for(exc, provider=provider, model=model)


def _brief_tool_args(raw, width=56):
    """从工具参数里挑一个"最像对象"的值，压成一行摘要。

    过程面板要的是「一行一步」（``运行校验、查看文件: editor_diff.py``），
    不是把 700 字符的 JSON 摆进去——那等于把日志搬进折叠区，展开后
    照样一屏看不懂。
    """
    try:
        data = json.loads(raw or "{}")
    except Exception:
        return (raw or "").strip().replace("\n", " ")[:width]
    if not isinstance(data, dict):
        return str(data)[:width]
    for key in ("path", "file", "files", "command", "cmd", "pattern",
                "query", "url", "name", "title"):
        value = data.get(key)
        if isinstance(value, (list, tuple)) and value:
            value = value[0]
        if isinstance(value, str) and value.strip():
            return value.strip().replace("\n", " ")[:width]
    keys = [k for k in data if not k.startswith("_")][:3]
    return ", ".join(keys)[:width]


def _format_tool_args(raw, limit=700):
    """工具参数展示：能解析就缩进成多行 JSON，过长按字符截断。

    旧实现把参数压成一行再截断，``create_file`` 的 9KB 内容会变成
    "Code · 1 行" 的一张卡片——行数没有信息量，正文还被砍在半句上。
    """
    text = (raw or "").strip()
    try:
        text = json.dumps(json.loads(text), ensure_ascii=False, indent=2)
    except Exception:
        pass
    if len(text) > limit:
        text = text[:limit].rstrip() + "\n…（已截断）"
    return text or "{}"


# 工具结果里的"界面级" emoji —— 这些不是内容，是状态标记。
# 换成单色记号或直接去掉：彩色 emoji 在 18px 字号下又大又花，和旁边
# 的矢量图标完全不是一套语言。
_UI_EMOJI = {
    "✅": "✓", "❌": "✕", "⚠️": "!", "⚠": "!",
    "📋": "", "🗒": "", "📄": "", "📁": "", "📝": "",
    "🔧": "", "🛠": "", "💾": "", "🔍": "", "🧪": "",
    "🚀": "", "⏹": "", "🔀": "", "🤔": "", "🔒": "",
}


def _polish_tool_text(text, keep_mark=True):
    """把工具结果里的 emoji 换成单色记号。

    只处理**行首**：结果正文里（代码、文件内容）出现的 emoji 一律不动，
    否则会把用户文件的内容改花。``keep_mark=False`` 时直接去掉行首标记，
    用于"前面已经有 ✓ 图标"的场合，避免同一行出现两个勾。
    """
    if not text:
        return text
    out = []
    for line in str(text).split("\n"):
        stripped = line.lstrip()
        for emoji, mark in _UI_EMOJI.items():
            if stripped.startswith(emoji):
                rest = stripped[len(emoji):].lstrip(" ：:")
                if not keep_mark:
                    mark = ""
                line = (mark + " " + rest).strip() if mark or rest else ""
                break
        out.append(line)
    return "\n".join(out)



# ===================================================================
# UI 组件
# ===================================================================

class WelcomeCard(QFrame):
    """空状态欢迎卡。

    v0.6.2：从"五行说明文字"改成"一句介绍 + 三个可直接点的起手式"。
    说明文字堆在空状态里没人读，而可点击的示例能把用户直接推进第一轮对话。
    """

    suggestion = Signal(str)

    SUGGESTIONS = (
        "解释我当前打开的代码",
        "给这个函数加上类型注解和错误处理",
        "为这个模块补一份单元测试",
    )

    def __init__(self, model_text="", parent=None):
        super().__init__(parent)
        theme.named(self, "welcomeCard")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 18, 20, 16)
        lay.setSpacing(10)

        head = QHBoxLayout()
        head.setSpacing(10)
        self._mark = QLabel()
        self._mark.setFixedSize(24, 24)
        self._mark.setScaledContents(True)
        self._mark.setPixmap(theme.accent_icon("spark").pixmap(QSize(24, 24)))
        head.addWidget(self._mark)
        title = QLabel("开始一段新的协作")
        theme.named(title, "welcomeTitle")
        head.addWidget(title)
        head.addStretch()
        lay.addLayout(head)

        body = QLabel(
            "读取并修改代码、运行命令、审阅差异——每一步都在你的监督之下。")
        theme.named(body, "welcomeBody")
        body.setWordWrap(True)
        lay.addWidget(body)

        lay.addSpacing(2)
        for text in self.SUGGESTIONS:
            btn = theme.push_button(text, "ghost")
            btn.setObjectName("suggestion")
            # 按内容宽度排布：QBushButton 默认会吃掉整行，三个起手式就变成
            # 三条横贯卡片的"横幅"
            btn.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Fixed)
            btn.setCursor(Qt.PointingHandCursor)
            btn.clicked.connect(
                lambda _c=False, t=text: self.suggestion.emit(t))
            row = QHBoxLayout()
            row.setContentsMargins(0, 0, 0, 0)
            row.addWidget(btn)
            row.addStretch()
            lay.addLayout(row)

    def refresh_theme(self):
        self._mark.setPixmap(
            theme.accent_icon("spark").pixmap(QSize(24, 24)))


class ZoomScrollArea(QScrollArea):
    """对话流滚动区：Ctrl+滚轮 = 调正文字号（对标浏览器 / VS Code）。

    普通滚轮照旧滚动；Ctrl 按住时把滚动事件吃掉，别让"放大"顺带把内容
    滚走。``zoom_requested`` 带方向（+1 放大 / -1 缩小），由面板决定档位。
    """

    zoom_requested = Signal(int)

    def wheelEvent(self, event):  # noqa: N802 - Qt 接口
        if event.modifiers() & Qt.ControlModifier:
            delta = event.angleDelta().y()
            if delta:
                self.zoom_requested.emit(1 if delta > 0 else -1)
                event.accept()
                return
        super().wheelEvent(event)


class MessageView(QTextEdit):
    """消息正文视图。

    QTextEdit 没有 ``anchorClicked`` 信号（那是 QTextBrowser 才有的），
    但代码块表头里的「复制」是 ``<a href="copilot-copy:N">``，需要真实
    可点。这里自己接管 ``mouseReleaseEvent`` / ``mouseMoveEvent``：
    悬停时给手型光标，点击时把 href 抛出去。
    """

    link_activated = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setReadOnly(True)
        self.setMouseTracking(True)
        self.viewport().setMouseTracking(True)
        self.setFrameShape(QFrame.NoFrame)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        # 默认的 WidgetWidth 模式下 QTextEdit 会在每次 resizeEvent 里自己改写
        # document 的 textWidth，外面算好的宽度（含右边距）会被覆盖掉，气泡高度
        # 也跟着算错。改成固定像素折行，排版权归调用方。
        self.setLineWrapMode(QTextEdit.FixedPixelWidth)
        self.viewport().setCursor(Qt.ArrowCursor)
        self.apply_theme_font()

    # ── 字体 ──────────────────────────────────────────────────
    def apply_theme_font(self):
        """把文档默认字体对齐主题。

        HTML 里每段都写了 font-size，但有三种东西漏网：``<br>`` 造成的空行、
        流式期间还没成形的尾块、以及某些 Qt 版本对 ``<p>`` 之外元素的处理。
        它们都会回落到 QTextDocument 的默认字体——中文 Windows 上是宋体
        9pt（约 12px），比正文小一大截，看起来就是「字号调了没用」。
        这里兜底设一次，让任何漏网文本都用正文的字号和字族。
        """
        t = theme.current_theme()
        f = QFont(self.font())
        fam = getattr(t, "font_family_name", "")
        if fam:
            f.setFamily(fam)
        f.setPixelSize(max(10, int(getattr(t, "body_font", 14))))
        self.document().setDefaultFont(f)

    def setHtml(self, html):  # noqa: N802 - Qt 接口
        # setHtml 会清空文档，默认字体不一定跟着留下；每次写完再对齐一次。
        super().setHtml(html)
        self.apply_theme_font()

    def _href_at(self, pos):
        try:
            return self.anchorAt(pos)
        except Exception:
            return ""

    def mouseMoveEvent(self, event):
        href = self._href_at(event.pos())
        want = Qt.PointingHandCursor if href else Qt.IBeamCursor
        if self.viewport().cursor().shape() != want:
            self.viewport().setCursor(want)
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        href = self._href_at(event.pos())
        if href:
            self.link_activated.emit(href)
            event.accept()
            return
        super().mouseReleaseEvent(event)


class ClickChip(QLabel):
    """可点击的胶囊标签。

    QLabel 没有 clicked 信号，而 QToolButton 即使压了 padding 也会比
    同字号的 chip 高 6~10px，混在输入区里像一块砖。这里只补一个点击事件，
    样式仍然走 ``QLabel#editorChip``。
    """

    clicked = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setCursor(Qt.PointingHandCursor)
        self.setAttribute(Qt.WA_StyledBackground, True)

    def mouseReleaseEvent(self, event):
        if (event.button() == Qt.LeftButton
                and self.rect().contains(event.pos())):
            self.clicked.emit()
        super().mouseReleaseEvent(event)


def _fmt_dur(seconds):
    """把秒数写成 ``5m1s`` / ``42s``，越界一律当 0。"""
    sec = int(max(0.0, seconds))
    mins, rest = divmod(sec, 60)
    return f"{mins}m{rest}s" if mins else f"{rest}s"


def thumb_pixmap(path, size):
    """把图片读成不超过 ``size`` 的缩略图；读不出来返回 None。"""
    try:
        pm = QPixmap(path)
    except Exception:                       # noqa: BLE001
        return None
    if pm is None or pm.isNull():
        return None
    if size.width() <= 0 or size.height() <= 0:
        return pm
    if pm.width() <= size.width() and pm.height() <= size.height():
        return pm
    return pm.scaled(size, Qt.KeepAspectRatio, Qt.SmoothTransformation)


def thumb_pixmap_cover(path, size):
    """等比放大到铺满 ``size`` 再居中裁切——一排 chip 高度才整齐。"""
    try:
        pm = QPixmap(path)
    except Exception:                       # noqa: BLE001
        return None
    if pm is None or pm.isNull():
        return None
    pm = pm.scaled(size, Qt.KeepAspectRatioByExpanding,
                   Qt.SmoothTransformation)
    x = max(0, (pm.width() - size.width()) // 2)
    y = max(0, (pm.height() - size.height()) // 2)
    return pm.copy(x, y, size.width(), size.height())


def show_image_preview(parent, path):
    """点缩略图看大图。窗口按图比例缩放，最大 900x620。"""
    pm = QPixmap(path) if os.path.exists(path) else None
    if pm is None or pm.isNull():
        return
    dlg = QDialog(parent)
    dlg.setWindowTitle(os.path.basename(path))
    lay = QVBoxLayout(dlg)
    lay.setContentsMargins(8, 8, 8, 8)
    label = QLabel()
    label.setAlignment(Qt.AlignCenter)
    avail = pm.size()
    if avail.width() > 900 or avail.height() > 620:
        avail = pm.scaled(900, 620, Qt.KeepAspectRatio,
                          Qt.SmoothTransformation).size()
    label.setPixmap(pm.scaled(avail, Qt.KeepAspectRatio,
                              Qt.SmoothTransformation))
    lay.addWidget(label)
    try:
        theme.harmonize(dlg, standalone=True)
    except Exception:                       # noqa: BLE001
        pass
    dlg.exec_() if hasattr(dlg, "exec_") else dlg.exec()


class AttachThumb(QFrame):
    """输入区里的一张待发送图片：48px 缩略图 + 右上角 ✕。

    刻意不做「文件名 chip」：面板常常只有 430px 宽，六个带名字的 chip
    会立刻溢出；缩略图 48px 一排六个也就 300px，名字放进 tooltip。
    """

    removed = Signal(str)

    SIZE = 48

    def __init__(self, path, name="", parent=None):
        super().__init__(parent)
        self._path = path
        self._name = name or os.path.basename(path)
        theme.named(self, "attachThumb")
        self.setFixedSize(self.SIZE, self.SIZE)
        self.setCursor(Qt.PointingHandCursor)
        try:
            self.setToolTip("%s\n点击查看大图，✕ 移除" % self._name)
        except Exception:                   # noqa: BLE001
            pass
        self._img = QLabel(self)
        self._img.setGeometry(2, 2, self.SIZE - 4, self.SIZE - 4)
        self._img.setAlignment(Qt.AlignCenter)
        self._img.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        pm = thumb_pixmap_cover(path, QSize(self.SIZE - 4, self.SIZE - 4))
        if pm is not None:
            self._img.setPixmap(pm)
        else:
            self._img.setText("?")
        # ✕ 必须有底色：直接压在浅色截图上时，一条细白线根本看不见。
        self._close = QToolButton(self)
        self._close.setFixedSize(15, 15)
        self._close.setCursor(Qt.PointingHandCursor)
        self._close.setToolTip("移除这张图")
        self._close.setIcon(theme.make_icon("close", "#ffffff", 12))
        self._close.setIconSize(QSize(9, 9))
        self._close.setStyleSheet(
            "QToolButton { border: none; border-radius: 7px;"
            " background: rgba(12, 15, 20, 0.78); }"
            "QToolButton:hover { background: #d64545; }")
        self._close.move(self.SIZE - 18, 3)
        self._close.clicked.connect(lambda: self.removed.emit(self._path))
        self._close.raise_()

    def resizeEvent(self, event):  # noqa: N802 - Qt 接口
        super().resizeEvent(event)
        self._close.move(self.width() - 17, 1)

    def mouseReleaseEvent(self, event):  # noqa: N802 - Qt 接口
        if (event.button() == Qt.LeftButton
                and self.rect().contains(event.pos())):
            show_image_preview(self, self._path)
        super().mouseReleaseEvent(event)


class ProcessPane(QFrame):
    """一轮对话的「过程」折叠面板（v0.8.4）。

    对标 Cursor / Claude Code 的 transcript 折叠：运行中展开，让用户
    看得见它在干活；这一轮结束后自动收成一行 ``已完成 5m1s · 6 步``，
    点一下再展开。失败时不收（在 ``MessageBubble.finish_process`` 里
    判），因为把报错折进"已完成"里等于告诉用户它成功了。

    ``_user_toggled`` 是必须的：没有它，用户刚点开看细节，下一帧到达
    又会把面板收回去，交互会变得没法用。

    v0.8.6 起计时用「累计 + 本次起点」而不是「结束时间 - 开始时间」：
    等审批的时间要能停表（``pause`` / ``resume``），而单点时间戳减不出
    停下来过多久。``_live`` 是所有还没收尾的面板，收尾时统一兜底停表——
    只要有哪一个气泡被换掉时忘了收尾，它就会永远显示「正在处理」，
    时间一直往上走（v0.8.5 实拍反馈）。
    """

    _live = weakref.WeakSet()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._md = ""
        self._steps = 0
        self._running = False
        self._waiting = False
        self._run_since = None
        self._accum = 0.0
        self._user_toggled = False
        self._rendered = False
        self._silent = False
        self._failed = False

        lay = QVBoxLayout(self)
        lay.setContentsMargins(8, 4, 8, 6)
        lay.setSpacing(2)
        self.setObjectName("processPane")
        self.setFrameShape(QFrame.NoFrame)

        self._btn = QToolButton(self)
        self._btn.setCheckable(True)
        self._btn.setChecked(True)
        self._btn.setAutoRaise(True)
        self._btn.setCursor(Qt.PointingHandCursor)
        self._btn.setToolButtonStyle(Qt.ToolButtonTextOnly)
        self._btn.toggled.connect(self._on_toggled)
        lay.addWidget(self._btn, 0, Qt.AlignLeft)

        self._view = MessageView(self)
        theme.named(self._view, "messageBody")
        self._view.document().setDocumentMargin(0)
        lay.addWidget(self._view)

        self._timer = QTimer(self)
        self._timer.setInterval(1000)
        self._timer.timeout.connect(self._tick)

        self.setVisible(False)
        # 和气泡一样只能收到 sizeHint 高度，否则外层布局会把剩余空间
        # 全分给它——灰块被拉高一截，正文挤在顶部，看着像渲染错位。
        self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Maximum)
        self._style_header()
        self._refresh_label()

    # ── 外观 ──────────────────────────────────────────────────
    def _style_header(self):
        t = theme.current_theme()
        muted, strong = t["text_muted"], t["text"]
        # 字号跟界面的 ui_font 走：写死 12px 的话，界面调到 18px 时
        # 这行「已完成 12s · 3 步」还是只有 12px，和其余文字差一截。
        summary_px = max(11, int(t.ui_font) - 2)
        # 选择器限定在 #processPane 上，避免 QSS 级联把正文的 messageBody
        # 样式一起改掉（Qt 的样式表是往子控件继承的）。
        self.setStyleSheet(
            "#processPane { background: %s; border: 0.5px solid %s;"
            " border-radius: 8px; }" % (t["surface_alt"], t["border"])
        )
        self._btn.setStyleSheet(
            "QToolButton { border: 0; padding: 2px 0; background: transparent;"
            f" color: {muted}; font-size: {summary_px}px; }}"
            f"QToolButton:hover {{ color: {strong}; }}"
        )

    def _refresh_label(self):
        arrow = "\u25be" if self._btn.isChecked() else "\u25b8"
        spent = _fmt_dur(self.elapsed())
        if self._waiting:
            text = f"{arrow} 等待批准 · 已用 {spent}"
        elif self._running:
            text = f"{arrow} 正在处理 {spent}"
        elif self._steps:
            text = f"{arrow} 已完成 {spent} · {self._steps} 步"
        else:
            text = f"{arrow} 已完成 {spent}"
        self._btn.setText(text)

    def elapsed(self):
        """已消耗的秒数：停下来过多久不算进去。"""
        if self._running and self._run_since is not None:
            return self._accum + (time.monotonic() - self._run_since)
        return self._accum

    # ── 生命周期 ──────────────────────────────────────────────
    def begin(self):
        """进入运行态：显示面板、开始计时、保证展开。"""
        if not self._running:
            self._run_since = time.monotonic()
            self._running = True
            self._waiting = False
            self._timer.start()
            ProcessPane._live.add(self)
        self.setVisible(True)
        if not self._btn.isChecked() and not self._user_toggled:
            self._set_checked(True)
        self._refresh_label()

    def pause(self):
        """挂起（等用户批准）：停表，等批准的时间不算 Agent 的工时。"""
        if not self._running:
            return
        if self._run_since is not None:
            self._accum += time.monotonic() - self._run_since
            self._run_since = None
        self._running = False
        self._waiting = True
        self._timer.stop()
        self._refresh_label()

    def resume(self):
        """批准后继续：从暂停处接着走表。"""
        if self._running or not self._waiting:
            return
        self._run_since = time.monotonic()
        self._running = True
        self._waiting = False
        self._timer.start()
        ProcessPane._live.add(self)
        self._refresh_label()

    def finish(self, failed=False):
        """任务结束：停表并按策略收起（用户手动展开过就尊重用户）。"""
        if self._run_since is not None:
            self._accum += time.monotonic() - self._run_since
            self._run_since = None
        self._timer.stop()
        self._running = False
        self._waiting = False
        ProcessPane._live.discard(self)
        # 失败要记在面板上，不能只影响这一次调用：紧接着 _finalize 还会
        # 再用 failed=False 收一遍尾，不记住就会被折进「已完成」，用户
        # 以为那一轮跑成功了。
        if failed:
            self._failed = True
        keep_open = failed or self._failed or self._user_toggled
        if self._btn.isChecked() and not keep_open:
            self._set_checked(False)
        self._refresh_label()

    @classmethod
    def finish_all(cls):
        """把所有还没收尾的面板停表。

        气泡被换掉（审批后另起一条、转向、异常分支）时，旧面板就再也
        收不到 ``finish``：它停在「正在处理」，秒数一直涨。收尾入口统一
        扫一遍，比在每个换气泡的地方各补一句可靠——漏一处就复发一次。
        """
        for pane in list(cls._live):
            try:
                pane.finish()
            except RuntimeError:
                # C++ 侧已被删除（清空会话时 deleteLater）
                cls._live.discard(pane)

    def append(self, md):
        body = (md or "").strip("\n")
        if not body:
            return
        # 过程里出现过失败就不自动收起：错误被折进"已完成"里，
        # 用户会以为它成功了。
        if "✕" in body or "失败" in body:
            self._failed = True
        self._md = (self._md + "\n\n" + body) if self._md else body
        self._steps += 1
        if self._btn.isChecked():
            self._render()
        self._refresh_label()

    # ── 内部 ──────────────────────────────────────────────────
    def _set_checked(self, value):
        self._silent = True
        self._btn.setChecked(value)
        self._silent = False

    def _on_toggled(self, checked):
        if not self._silent:
            self._user_toggled = True
        self._view.setVisible(checked)
        if checked:
            self._render()
        self._refresh_label()

    def _render(self):
        self._view.setHtml(render_markdown(self._md))
        self._rendered = True
        self._fit()

    def _fit(self):
        if not self._view.isVisible():
            return
        doc = self._view.document()
        # 减去左右 margin（8+8）和圆角装饰，别让正文顶到边框上
        doc.setTextWidth(max(self.width() - 22, 180))
        self._view.setFixedHeight(int(doc.size().height()) + 4)

    def refit(self):
        """外部（气泡 _adjust）在宽度变化后重新排版。"""
        self._fit()

    def _tick(self):
        if not self._running:
            # 防御：表停了就不该再有 tick，真来了也立刻收掉
            self._timer.stop()
            return
        self._refresh_label()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._fit()

    def refresh_theme(self):
        self._style_header()
        if self._btn.isChecked():
            self._render()


class BubbleThumb(QLabel):
    """气泡里的一张图：缩略图，点开看原图。

    不能用「给 QLabel 实例赋一个 mouseReleaseEvent」的写法——PyQt 的
    虚函数派发走类型查找，实例属性不参与，点了没反应。必须真的子类化。
    """

    def __init__(self, path, parent=None):
        super().__init__(parent)
        self._path = path
        theme.named(self, "bubbleThumb")
        pm = thumb_pixmap(path, QSize(240, 120))
        if pm is not None:
            self.setPixmap(pm)
        else:
            self.setText("图片已丢失")
        self.setCursor(Qt.PointingHandCursor)
        self.setToolTip(os.path.basename(path))

    def mouseReleaseEvent(self, event):  # noqa: N802 - Qt 接口
        if (event.button() == Qt.LeftButton
                and self.rect().contains(event.pos())):
            show_image_preview(self, self._path)
        super().mouseReleaseEvent(event)


class MessageBubble(QFrame):
    """消息气泡 — v0.6.0 扁平时间线设计。

    * 用户消息：带强调色左轴的卡片，和 AI 回复形成清晰层级
    * AI 消息：无边框正文，去掉层层嵌套的视觉噪音
    * 工具/系统消息：默认折叠成一行摘要（对标 Cursor 3.4 的
      Compact / Balanced / Detailed 工具痕迹密度）
    """
    FONT_SIZE = 14
    TOOL_DENSITY = "balanced"
    copy_requested = Signal(str, str)
    insert_requested = Signal(str)
    retry_requested = Signal(str)

    @classmethod
    def set_font_size(cls, size):
        cls.FONT_SIZE = size

    @classmethod
    def set_tool_density(cls, density):
        cls.TOOL_DENSITY = density if density in (
            "compact", "balanced", "detailed") else "balanced"

    def __init__(self, role, content, parent=None):
        super().__init__(parent)
        self._role = role
        self._content = content
        # v0.5.5 P0：流式期间走纯文本增量插入，结束后才做一次全量 Markdown 渲染
        self._streaming = False
        # v0.8.4：尚未确定归属的叙述文本。工具调用前它属于"过程"，
        # 流结束时它属于"最终回答"——这条边界决定了折叠面板里装什么。
        self._say = ""
        self._collapsed = (role == "tool" and self.TOOL_DENSITY != "detailed")
        t = theme.current_theme()
        m = t.metrics
        theme.named(self, {
            "user": "userBubble", "assistant": "assistantBubble",
            "tool": "toolBubble",
        }.get(role, "assistantBubble"))

        lay = QVBoxLayout(self)
        lay.setContentsMargins(m["pad"] + 2, m["gap"], m["pad"] + 2, m["gap"])
        lay.setSpacing(max(3, m["gap"] - 2))
        self._pad = m["pad"] + 2

        # ── 角色行：矢量图标 + 角色名 + 时间 + 悬停操作 ──
        header = QHBoxLayout()
        header.setSpacing(6)
        icon_name = {"user": "chat", "assistant": "spark", "tool": "terminal"}.get(role, "chat")
        icon_color = (t["accent"] if role == "assistant"
                      else t["info"] if role == "tool" else t["text_muted"])
        self._icon_lbl = QLabel()
        self._icon_lbl.setPixmap(theme.make_icon(icon_name, icon_color).pixmap(QSize(15, 15)))
        self._icon_lbl.setFixedSize(16, 16)
        header.addWidget(self._icon_lbl)

        role_map = {"user": "You", "assistant": "Copilot", "tool": "工具输出"}
        role_lbl = QLabel(role_map.get(role, role))
        theme.named(role_lbl, "roleLabel")
        theme.set_prop(role_lbl, "role", role)
        header.addWidget(role_lbl)

        if role == "tool":
            summary = (content or "").strip().splitlines()
            summary = summary[0][:72] if summary else "执行完成"
            head = QLabel(summary)
            theme.named(head, "approvalPath")
            head.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
            header.addWidget(head, 1)
            self._toggle = theme.icon_button("chevron", "展开 / 收起工具输出", 22)
            self._toggle.clicked.connect(self._toggle_collapse)
            header.addWidget(self._toggle)
        else:
            import datetime
            stamp = QLabel(datetime.datetime.now().strftime("%H:%M"))
            theme.named(stamp, "timeLabel")
            header.addWidget(stamp)
            header.addStretch()
            actions = (("copy", "复制", lambda: self.copy_requested.emit(role, self._render_text())),
                       ("insert", "插入到编辑器", lambda: self.insert_requested.emit(self._render_text())),
                       ("retry", "重试", lambda: self.retry_requested.emit(self._render_text())))
            if role == "user":
                actions = actions[:1]
            self._action_icons = []
            for name, tip, fn in actions:
                b = theme.icon_button(name, tip, 22)
                b.clicked.connect(fn)
                header.addWidget(b)
                self._action_icons.append((b, name))
        lay.addLayout(header)

        # ── 这条消息带的图：缩略图横排，点开看原图 ──
        self._attach_row = QWidget()
        self._attach_lay = QHBoxLayout(self._attach_row)
        self._attach_lay.setContentsMargins(0, 2, 0, 0)
        self._attach_lay.setSpacing(4)
        self._attach_lay.addStretch()
        self._attach_row.setVisible(False)
        lay.addWidget(self._attach_row)

        # v0.8.4：过程折叠面板。放在正文之上——折叠态它就是一行摘要，
        # 展开态才把工具卡片和中间叙述铺开。
        self._process = ProcessPane(self)
        lay.addWidget(self._process)

        # ── 内容区 ──
        self._view = MessageView()
        theme.named(self._view, "messageBody")
        self._view.setHtml(render_markdown(content))
        self._view.document().setDocumentMargin(0)
        self._view.link_activated.connect(self._on_link)
        lay.addWidget(self._view)
        # 流式渲染状态：已提交块的 HTML 拼接 + 尾块 HTML
        self._stream = StreamRenderer()
        self._prefix = ""
        self._pending = ""
        self._flush_timer = QTimer(self)
        self._flush_timer.setSingleShot(True)
        self._flush_timer.setInterval(45)
        self._flush_timer.timeout.connect(self._flush_stream)
        QTimer.singleShot(0, self._adjust)
        if self._collapsed:
            self._view.setVisible(False)

    # ── 代码块复制 ──────────────────────────────────────────────
    def _on_link(self, href):
        if href.startswith(("http://", "https://")):
            # 错误卡片里的充值 / Key 页面要能直接点开：让用户自己找
            # platform.deepseek.com 的入口，比把 URL 抄进浏览器更靠谱
            from qtpy.QtGui import QDesktopServices
            from qtpy.QtCore import QUrl
            try:
                QDesktopServices.openUrl(QUrl(href))
            except Exception:  # noqa: BLE001 - 没有默认浏览器时不该报错
                logger.debug("open url failed: %s", href, exc_info=True)
            return
        if not href.startswith("copilot-copy:"):
            return
        try:
            index = int(href.split(":", 1)[1])
        except (ValueError, IndexError):
            return
        blocks = extract_code_blocks(self._render_text())
        if not (0 <= index < len(blocks)):
            return
        code = blocks[index][1]
        QApplication.clipboard().setText(code)
        if not self._flash_copied(href):
            try:
                QToolTip.showText(self.mapToGlobal(self.rect().center()),
                                  "代码已复制", self)
            except Exception:
                pass

    def _flash_copied(self, href):
        """把被点击的「复制」就地改成「已复制」，给一次即时反馈。"""
        try:
            doc = self._view.document()
            cur = QTextCursor(doc)
            for _ in range(doc.characterCount() - 1):
                fmt = cur.charFormat()
                if fmt.isAnchor() and fmt.anchorHref() == href:
                    sel = QTextCursor(doc)
                    sel.setPosition(cur.position())
                    sel.movePosition(QTextCursor.Right, QTextCursor.KeepAnchor, 2)
                    sel.insertText("已复制")
                    return True
                if not cur.movePosition(QTextCursor.Right):
                    break
        except Exception:
            pass
        return False

    def _toggle_collapse(self):
        self._collapsed = not self._collapsed
        self._view.setVisible(not self._collapsed)
        self._adjust()

    def _adjust(self):
        # QFrame 默认 Preferred 策略时，外层 QVBoxLayout 会把剩余高度分给气泡，
        # 一条短消息也能占掉半屏（实拍里就是这样）。Maximum 表示"只能收到
        # sizeHint 高度"，气泡就永远贴着内容。
        self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Maximum)
        try:
            self._process.refit()
        except AttributeError:
            pass
        if self._view.isHidden():
            return
        d = self._view.document()
        # 用气泡自身宽度推算正文可用宽度，而不是读子控件的 width()：
        # resizeEvent 里布局还没把新宽度下发到子控件，读到的会是上一轮的值，
        # 结果文档比视口宽 8px，代码块右边框被卷到滚动条底下。
        pad = getattr(self, "_pad", 14)
        # 再留 4px：表格/代码块的外框正好压在视口右沿时会被滚动条盖住
        avail = self.width() - 2 * pad - 4
        d.setTextWidth(max(avail, 180))
        self._view.setFixedHeight(int(d.size().height() + 8))

    def set_attachments(self, paths):
        """挂上这条消息的图片缩略图（用户消息用）。"""
        for path in list(paths or []):
            if not path:
                continue
            self._attach_lay.insertWidget(self._attach_lay.count() - 1,
                                          BubbleThumb(path, self))
        if self._attach_lay.count() > 1:
            self._attach_row.setVisible(True)
            self._adjust()

    def refresh_theme(self):
        """主题切换后重绘图标与 Markdown 内联色。"""
        t = theme.current_theme()
        color = (t["accent"] if self._role == "assistant"
                 else t["info"] if self._role == "tool" else t["text_muted"])
        name = {"user": "chat", "assistant": "spark", "tool": "terminal"}.get(
            self._role, "chat")
        self._icon_lbl.setPixmap(theme.make_icon(name, color).pixmap(QSize(15, 15)))
        for btn, icon_name in getattr(self, "_action_icons", []):
            btn.setIcon(theme.make_icon(icon_name, t["text_faint"]))
        if self._streaming:
            # 重绘流式中间态：块缓存按主题 key 失效，会重新生成
            self._stream.reset()
            self._prefix = ""
            self._pending = ""
        self._process.refresh_theme()
        self._view.setHtml(render_markdown(self._render_text()))
        # 字号/字族变了，同一份 HTML 的高度也跟着变；不重排就会留着旧高度，
        # 大字号被裁掉、小字号底部露出一大块空白。
        self._adjust()
        self._adjust()

    def is_streaming(self):
        return self._streaming

    def begin_stream(self):
        """进入流式模式。

        v0.7.0 起流式期间也渲染 Markdown：每 45ms 把新增文本合批，交给
        ``StreamRenderer`` 只重算"未完成的尾块"，已完成块的 HTML 直接复用。
        """
        if self._streaming:
            return
        self._streaming = True
        self._stream.reset()
        self._prefix = ""
        self._pending = ""
        self._view.clear()

    def end_stream(self):
        """流结束：做一次全量渲染，保证与增量结果完全一致。"""
        if not self._streaming:
            return
        self._streaming = False
        self._flush_timer.stop()
        self._pending = ""
        self.commit_say()
        self._view.setHtml(render_markdown(self._content))
        self._adjust()

    def _render_text(self):
        """正文的完整文本 = 已确定的正文 + 尚未归属的叙述。"""
        return self._content + self._say

    def append(self, text):
        self._say += text
        if self._streaming:
            self._pending += text
            if not self._flush_timer.isActive():
                self._flush_timer.start()
        else:
            self._view.setHtml(render_markdown(self._render_text()))
            self._adjust()

    def append_block(self, text, kind="result"):
        """插入一"整段"内容（工具卡片、状态行、检查点），而不是流式 delta。

        和 ``append`` 的区别在边界：流式 delta 必须原样首尾相接，而这里
        必须自己保证前后各有一个空行。否则下一段流式文本会直接粘到最后
        一行上——实测：工具卡片的收尾 ``` 撞上后续正文 "合适。" 变成
        "```合适。"，围栏再也合不上，后面整段回复（含表格、粗体）全被
        吞进代码块里，看起来就是"Markdown 完全不渲染"。

        ``kind="process"`` 的内容进折叠面板而不是正文：工具调用卡片、
        工具结果行、轮次状态行都属于"过程"，不该占着最终回答的位置。
        """
        body = text.strip("\n")
        if not body:
            return
        if kind == "process":
            self.add_process(body)
            return
        # 卡片（diff / 检查点 / 警告）是"结果"，先给待定叙述定性，
        # 再把它续在正文末尾，保证顺序和折叠面板的边界一致。
        self.commit_say()
        lead = "" if not self._content.strip() else "\n\n"
        self._content += lead + body + "\n\n"
        if self._streaming:
            self._stream.reset()
            self._prefix = ""
        self._view.setHtml(render_markdown(self._content))
        self._adjust()

    # ── v0.8.4：过程 / 正文 的归属 ─────────────────────────────
    def commit_say(self):
        """把待定叙述定性为正文（不会再被折叠）。"""
        if self._say:
            self._content += self._say
            self._say = ""

    def archive_say(self):
        """把待定叙述搬进过程面板：它后面跟着工具调用，所以不是结论。"""
        text = self._say
        if not text.strip():
            return
        self._say = ""
        if self._streaming:
            self._stream.reset()
            self._prefix = ""
        self._process.append(text)
        self._view.setHtml(render_markdown(self._content))
        self._adjust()

    def add_process(self, text):
        """直接追加一条过程内容（工具卡片 / 结果行 / 状态行）。"""
        self._process.begin()
        self._process.append(text)
        self._adjust()

    def begin_process(self):
        """工具调用开始：把此前的叙述归档，并保证面板在运行态展开。"""
        self.archive_say()
        self._process.begin()

    def pause_process(self):
        """挂起等审批：钟先停，别把用户思考的时间算成 Agent 的工时。"""
        self._process.pause()

    def resume_process(self):
        self._process.resume()

    def finish_process(self, failed=False):
        """这一轮结束：把尾部叙述定性为结论，停表并按策略收起。"""
        try:
            self.commit_say()
            self._view.setHtml(render_markdown(self._content))
            self._adjust()
        finally:
            # 停表必须发生在 finally：渲染/排版抛异常时，面板会永远停在
            # 「正在处理」，秒数一直往上走（v0.8.5 实拍的那个 bug）。
            self._process.finish(failed=failed)

    def _flush_stream(self):
        """把这一帧的增量并入文档：已提交块拼接 + 尾块覆盖。"""
        if not self._streaming:
            return
        self._pending = ""
        reset, new_blocks, tail = self._stream.update(self._render_text())
        if reset:
            self._prefix = ""
        if new_blocks:
            self._prefix += "".join(new_blocks)
        self._view.setHtml(self._prefix + tail)
        self._adjust()

    def set_content(self, text):
        self._streaming = False
        self._flush_timer.stop()
        self._pending = ""
        self._content = text
        self._say = ""
        self._stream.reset()
        self._prefix = ""
        self._view.setHtml(render_markdown(text))
        self._adjust()

    def get_content(self):
        return self._render_text()

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._adjust()

    def flash(self):
        """跳转过来时闪一下：给个 1.2 秒的强调边框，视线能立刻定位。

        走动态属性 + QSS 而不是动画：面板里已有 ``set_prop`` 这套机制，
        主题切换时只需把属性清掉，不用管动画状态机。重复跳同一条也要
        重新闪，所以先把属性清掉再设回去。
        """
        if self._role != "user":
            return
        try:
            theme.set_prop(self, "flash", "false")
            theme.set_prop(self, "flash", "true")
        except RuntimeError:
            return
        QTimer.singleShot(1200, lambda: self._clear_flash())

    def _clear_flash(self):
        try:
            theme.set_prop(self, "flash", "false")
        except RuntimeError:
            pass


class ChatInput(QPlainTextEdit):
    """Composer 输入区 — 无边框，边框由外层统一容器提供。"""
    send_requested = Signal()
    #: 剪贴板里粘进来的一张图（QImage）
    image_pasted = Signal(object)
    #: 拖进来的若干图片文件路径
    files_dropped = Signal(list)
    FONT_SIZE = 14

    @classmethod
    def set_font_size(cls, size):
        cls.FONT_SIZE = size

    def __init__(self, parent=None):
        super().__init__(parent)
        theme.named(self, "composerInput")
        self.setPlaceholderText(
            "输入消息 · / 命令 · Enter 发送 · 截图可粘贴")
        # 主流 composer 的高度随内容增长：空输入时紧凑，长提示最多到 140px。
        self.setFixedHeight(48)
        self.setFrameShape(QFrame.NoFrame)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setAcceptDrops(True)
        self.textChanged.connect(self._auto_height)

    # ── 图片粘贴 / 拖入 ────────────────────────────────────────
    @staticmethod
    def _image_paths(mime):
        paths = []
        if mime is None or not mime.hasUrls():
            return paths
        for url in mime.urls():
            if not url.isLocalFile():
                continue
            path = url.toLocalFile()
            if attachments.is_image_file(path):
                paths.append(path)
        return paths

    def insertFromMimeData(self, source):  # noqa: N802 - Qt 接口
        """粘贴：截图直接变成附件，而不是往输入框里塞一个乱码方块。

        只在「没有纯文本」时才抢图：从 Word / 网页复制带图段落时剪贴板
        同时有 TEXT 和 IMAGE，那种场景用户要的是文字。
        """
        try:
            if source.hasImage() and not source.hasText():
                image = source.imageData()
                if image is not None and hasattr(image, "toImage"):
                    image = image.toImage()
                if image is not None and not image.isNull():
                    self.image_pasted.emit(image)
                    return
            paths = self._image_paths(source)
            if paths:
                # 图片文件 URL 不看 hasText()：Qt 给 uri-list 的同时一定
                # 附一份文本（就是那个 file:/// 串），按文本判会永远落空
                self.files_dropped.emit(paths)
                return
        except Exception:                 # noqa: BLE001 - 粘贴永远不能炸
            logger.debug("paste image failed", exc_info=True)
        super().insertFromMimeData(source)

    def dragEnterEvent(self, event):  # noqa: N802 - Qt 接口
        mime = event.mimeData()
        if self._image_paths(mime) or (
                mime.hasImage() and not mime.hasText()):
            event.acceptProposedAction()
            return
        super().dragEnterEvent(event)

    def dragMoveEvent(self, event):  # noqa: N802 - Qt 接口
        if self._image_paths(event.mimeData()):
            event.acceptProposedAction()
            return
        super().dragMoveEvent(event)

    def dropEvent(self, event):  # noqa: N802 - Qt 接口
        mime = event.mimeData()
        paths = self._image_paths(mime)
        if paths:
            self.files_dropped.emit(paths)
            event.acceptProposedAction()
            return
        try:
            if mime.hasImage() and not mime.hasText():
                image = mime.imageData()
                if image is not None and hasattr(image, "toImage"):
                    image = image.toImage()
                if image is not None and not image.isNull():
                    self.image_pasted.emit(image)
                    event.acceptProposedAction()
                    return
        except Exception:                 # noqa: BLE001
            logger.debug("drop image failed", exc_info=True)
        super().dropEvent(event)

    def _auto_height(self):
        lines = max(1, self.document().blockCount())
        self.setFixedHeight(max(48, min(140, 26 + lines * 21)))

    def _composer(self):
        w = self.parentWidget()
        while w is not None and w.objectName() != "composer":
            w = w.parentWidget()
        return w

    def focusInEvent(self, event):  # noqa: N802 - Qt 接口
        super().focusInEvent(event)
        c = self._composer()
        if c is not None:
            theme.set_prop(c, "focused", "true")

    def focusOutEvent(self, event):  # noqa: N802 - Qt 接口
        super().focusOutEvent(event)
        c = self._composer()
        if c is not None:
            theme.set_prop(c, "focused", "false")

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key_Return, Qt.Key_Enter):
            if not (event.modifiers() & Qt.ShiftModifier):
                self.send_requested.emit()
                return
        super().keyPressEvent(event)


# ===================================================================
# API 调用线程
# ===================================================================

class ChatWorker(QThread):
    chunk = Signal(str)
    done = Signal(str)
    error = Signal(str)
    tool_calls = Signal(list)
    usage = Signal(dict)   # v0.5.5：服务端返回的精确 token 用量（若有）

    def __init__(self, messages, config, system_prompt="", tools=None,
                 parent=None, generation=0):
        super().__init__(parent)
        self.messages = messages
        self.cfg = config
        self.sp = system_prompt
        self._tools = tools if tools is not None else AGENT_TOOLS
        self.generation = generation
        self._cancel = False
        self._resp = None       # 当前 HTTP 响应（用于取消时立即 close）
        self._gen = None        # 当前 SSE 生成器（copilot_client 路径）
        # 流式 tool_calls 累加器: {index: {id, type, function: {name, arguments}}}
        self._pending_tc = {}
        self._tc_emitted = False  # 本轮流式是否已发出 tool_calls
        self._reasoning_content = ""  # DeepSeek thinking 模式推理内容
        # v0.5.5 P0：worker 端合并 SSE delta，避免每个 token 都往 GUI 线程
        # 排一个信号（跨线程信号过多同样会挤满主线程事件队列）。
        self._chunk_buf = []
        self._chunk_len = 0
        self._chunk_last = 0.0

    def _emit_chunk(self, text):
        """按 30ms / 256 字符批量发出，降低跨线程信号频率。"""
        if not text:
            return
        self._chunk_buf.append(text)
        self._chunk_len += len(text)
        now = time.monotonic()
        if (now - self._chunk_last >= 0.030
                or self._chunk_len >= 256):
            self._flush_chunks()

    def _flush_chunks(self):
        if not self._chunk_buf:
            return
        self.chunk.emit("".join(self._chunk_buf))
        self._chunk_buf = []
        self._chunk_len = 0
        self._chunk_last = time.monotonic()

    def _emit_usage(self, payload):
        if isinstance(payload, dict) and payload:
            self.usage.emit(payload)

    # ── 取消 / 资源清理 ────────────────────────────────────────────
    def cancel(self):
        """请求取消：关闭底层连接，让阻塞中的流式读取立刻返回"""
        self._cancel = True
        resp = self._resp
        self._resp = None
        if resp is not None:
            try:
                resp.close()      # 打断 for line in resp
            except Exception:
                pass
        gen = self._gen
        self._gen = None
        if gen is not None:
            try:
                gen.close()       # 打断 copilot_client 的生成器
            except Exception:
                pass

    def _close_resp(self):
        """确保 SSE 响应被关闭（P1-3：避免 socket 泄漏）"""
        resp = self._resp
        self._resp = None
        if resp is not None:
            try:
                resp.close()
            except Exception:
                pass

    def run(self):
        try:
            p = self.cfg["provider"]
            if p == "copilot": self._copilot()
            elif p == "anthropic": self._anthropic()
            elif p == "ollama": self._ollama()
            else: self._openai_compat()
        except Exception as e:
            # 兜底：任何一条提供方分支漏了 try，也不能让异常逃出线程把 IDE 带崩
            self._emit_error(e)
        finally:
            self._close_resp()

    def _prepare_messages(self):
        """发请求前的最后一道加工：把本地图片引用换成 data URL。

        这是所有提供方（OpenAI 兼容 / Copilot / Anthropic）唯一的公共
        咽喉，所以编码只写在这里。历史里存的是 ``spyder-file://<path>``
        引用，几十字节；真正的 base64 每次请求现算，会话文件不会被撑爆。
        """
        msgs = []
        for m in self.messages:
            msg = dict(m) if isinstance(m, dict) else m
            if isinstance(msg, dict) and isinstance(msg.get("content"), list):
                resolved, errors = attachments.resolve_parts(msg["content"])
                for err in errors:
                    logger.warning("image attachment: %s", err)
                msg["content"] = resolved
            msgs.append(msg)
        if self.sp:
            msgs.insert(0, {"role": "system", "content": self.sp})
        return msgs

    def _request(self, url, body, headers):
        import urllib.request
        import urllib.error
        data = json.dumps(body).encode()
        req = urllib.request.Request(url, data=data, headers=headers, method="POST")
        try:
            # 使用默认 TLS 校验（不关闭证书验证）
            self._resp = urllib.request.urlopen(req, timeout=120)
            return self._resp
        except urllib.error.HTTPError as e:
            try:
                error_body = e.read().decode("utf-8", errors="replace")[:4000]
            except Exception:  # noqa: BLE001 - 连接已被对端掐断
                error_body = ""
            # 抛结构化异常：状态码 + 原始错误体一起带走，交给 GUI 分类展示。
            # 绝不在这里拼字符串——拼完就没法区分「DeepSeek 欠费」和
            # 「Copilot 额度用完」，两者处置方式完全不同。
            raise ApiError(e.code, error_body,
                           provider=self.cfg.get("provider", ""),
                           model=self.cfg.get("model", "")) from None

    def _flush_tool_calls(self):
        """发出累加完成的 tool_calls 并清空累加器"""
        self._flush_chunks()
        if not self._pending_tc:
            return
        # 按 index 排序，转为列表
        result = []
        for idx in sorted(self._pending_tc.keys()):
            tc = self._pending_tc[idx]
            # 补全必要字段
            tc.setdefault("id", f"call_{idx}")
            tc.setdefault("type", "function")
            tc.setdefault("function", {"name": "", "arguments": "{}"})
            result.append(tc)
        self._pending_tc = {}
        if result:
            self._tc_emitted = True
            self.tool_calls.emit(result)

    def _accumulate_tool_calls(self, tool_calls_delta):
        """累加流式 tool_calls 片段"""
        for tc in tool_calls_delta:
            idx = tc.get("index", 0)
            if idx not in self._pending_tc:
                self._pending_tc[idx] = {}
            entry = self._pending_tc[idx]

            # id / type 只在第一个 chunk 出现
            if "id" in tc:
                entry["id"] = tc["id"]
            if "type" in tc:
                entry["type"] = tc["type"]

            # function 可能嵌套携带
            func = tc.get("function", {})
            if func:
                if "function" not in entry:
                    entry["function"] = {}
                if "name" in func:
                    entry["function"]["name"] = func["name"]
                if "arguments" in func:
                    entry["function"]["arguments"] = (
                        entry["function"].get("arguments", "") + func["arguments"]
                    )

    def _openai_compat(self):
        url = f"{self.cfg['api_url']}{self.cfg.get('chat_endpoint', '/chat/completions')}"
        msgs = self._prepare_messages()
        agent = self.cfg.get("agent_mode", True) and self.cfg.get("supports_agent", False)
        provider = self.cfg.get("provider", "")
        # v0.8.14：能力字段由 settings_dialog.PROVIDERS 随配置带进来，
        # 这里只按能力发请求，不再按 provider 名硬编码。旧的 cfg（测试、
        # 历史任务快照）没带字段时，按名字兜底。
        thinking_capable = bool(self.cfg.get("supports_thinking", False)) or \
            provider in ("deepseek", "hunyuan")
        thinking = str(self.cfg.get("thinking_mode", "disabled")).lower() == "enabled"
        effort = str(self.cfg.get("reasoning_effort", "high")).lower()
        allowed = tuple(self.cfg.get("reasoning_efforts") or ())
        max_tokens = int(self.cfg.get("max_tokens", 8192) or 8192)
        max_output = int(self.cfg.get("max_output_tokens", 0) or 0)
        body = {
            "model": self.cfg.get("model", "deepseek-flash"),
            "messages": msgs,
            "temperature": self.cfg.get("temperature", 0.0),
            "max_tokens": max_tokens,
            "stream": True,
        }
        # DeepSeek / 腾讯混元 Hy4 的深度思考默认开启，必须用 thinking
        # 显式控制。关闭时 temperature 才生效，工具调用也无需回传
        # reasoning_content（开启却缺回传时 API 会返回 400）。
        if thinking_capable:
            # Hy4 的 reasoning_effort 只认 high / no_think，preview 认
            # none / high；把别家的 low / max 填进去会直接 400。
            # no_think / none 本身就是「别思考」，等价于关掉 thinking，
            # 不跟 thinking=enabled 一起发。
            want_off = effort in ("no_think", "none")
            if thinking and not want_off:
                body["thinking"] = {"type": "enabled"}
                if allowed:
                    if effort not in allowed or want_off:
                        effort = "high"
                elif effort not in ("low", "high", "max"):
                    # 旧 cfg 没带白名单：沿用 DeepSeek 的合法档位
                    effort = "high"
                body["reasoning_effort"] = effort
                # 思考链本身要吃掉大量输出预算，抬到官方默认 64K
                body["max_tokens"] = max(max_tokens, 65536)
            else:
                body["thinking"] = {"type": "disabled"}
            if max_output:
                # Hy4 单次输出上限 64K；UI 里最大 Token 可填到 393216，
                # 不截断会收到 400
                body["max_tokens"] = min(int(body["max_tokens"]), max_output)
        if self.cfg.get("stream_usage"):
            # TokenHub 必须显式要，否则流末尾不带 usage（成本统计一直是 0）
            body["stream_options"] = {"include_usage": True}
        if agent and provider not in ("ollama",):
            # Hy4 带 tools 时服务端默认开启 Preserved Thinking，要求把
            # 上一轮 assistant 的 reasoning_content 原样回填；面板在
            # _on_done / _on_tool_calls 里已经这么做了。
            body["tools"] = self._tools

        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.cfg.get('api_key', '')}",
        }
        if self.cfg.get("extra_headers"):
            headers.update(self.cfg["extra_headers"])

        try:
            resp = self._request(url, body, headers)
            full = ""
            has_tc = False
            for line in resp:
                if self._cancel: return
                l = line.decode("utf-8","replace").strip()
                if not l or not l.startswith("data: "): continue
                p = l[6:]
                if p == "[DONE]":
                    # 流结束，发出累积的工具调用
                    self._flush_tool_calls()
                    break
                try:
                    d = json.loads(p)
                except Exception:
                    continue
                # v0.8.14：TokenHub 的流式错误放在 SSE error frame 里
                # （HTTP 已经 200），旧代码一句 except: continue 会把它
                # 整个吞掉，界面只看到「回答空了」。业务码是六位数字，
                # 前三位对应 HTTP 状态（402xxx / 401xxx / 429xxx）。
                if isinstance(d, dict) and d.get("error"):
                    err = d["error"] if isinstance(d["error"], dict) else \
                        {"message": str(d["error"])}
                    biz = str(err.get("code") or "")
                    err_status = (int(biz[:3]) if len(biz) == 6
                                  and biz[:3].isdigit() else 0)
                    raise ApiError(
                        err_status,
                        json.dumps(err, ensure_ascii=False)[:4000],
                        provider=provider,
                        model=self.cfg.get("model", "")) from None
                try:
                    choices = d.get("choices", [{}])
                    delta = choices[0].get("delta", {}) if choices else {}
                    # OpenAI 兼容流末尾可能带 usage；TokenHub 要显式
                    # stream_options.include_usage 才给
                    if d.get("usage"):
                        self._emit_usage(d["usage"])

                    # DeepSeek / Hy4 的推理链：官方字段 reasoning_content，
                    # 个别网关写成 reasoning，两个都认。
                    rc = delta.get("reasoning_content") or delta.get("reasoning")
                    if rc:
                        self._reasoning_content += rc

                    # 累积 tool_calls 片段
                    tc_delta = delta.get("tool_calls")
                    if tc_delta:
                        self._accumulate_tool_calls(tc_delta)
                        has_tc = True

                    # 普通文本内容
                    c = delta.get("content", "")
                    if c:
                        # 如果之前累积了 tool_calls 但还没发出，发出它们
                        if has_tc and self._pending_tc:
                            self._flush_tool_calls()
                        full += c
                        self._emit_chunk(c)
                except Exception:
                    continue

            self._flush_chunks()
            # 如果本流中已发出 tool_calls，由 _on_tool_calls 接管后续，不再触发 done
            if not self._tc_emitted:
                self.done.emit(full)
        except Exception as e:
            self._emit_error(e)
        finally:
            self._close_resp()

    def _emit_error(self, exc):
        """把异常归成「结论 + 步骤」卡片后 emit（取消时不提示）。

        这里只发**成品卡片**（Markdown）：GUI 线程拿到就能画，不再做任何
        判断——判断放在线程里，界面就不会因为文案问题抛异常。

        日志约定（v0.8.15）：Spyder 会把 stderr 的每一行当成未处理异常
        弹「内部错误」窗口，所以包级日志已由 ``safe_logging`` 接管、不再
        进 stderr。这里仍只用 ``warning``：即便有人改了包级日志配置，
        warning 也不会被判成崩溃。traceback 走 ``note_exception`` 存进
        内存诊断缓冲——``  File "..."`` 行一旦进输出流就会重新勾出弹窗。
        """
        if self._cancel:
            return
        try:
            provider = self.cfg.get("provider", "")
            model = self.cfg.get("model", "")
            info = api_errors.classify(exc, provider=provider, model=model)
            logger.warning("Chat request failed: %s", api_errors.summary(info))
            if not info.retryable:
                logger.info("（%s 类错误不会自动重试，等用户处理）", info.kind)
            safe_logging.note_exception(exc, "Chat request failed detail")
            payload = api_errors.card(info)
        except Exception:  # noqa: BLE001 - 错误处理不许成为新的崩溃点
            try:
                payload = api_errors.card_for(
                    text=str(exc),
                    provider=self.cfg.get("provider", ""),
                    model=self.cfg.get("model", ""))
            except Exception:
                payload = api_errors.card_for(
                    text="请求失败，错误卡片渲染也失败，请查看诊断日志。")
        try:
            self.error.emit(payload)
        except Exception:
            pass

    def _copilot(self):
        """Copilot 聊天：统一走 copilot_client.chat_completion()

        这样请求头（Editor-Version / Copilot-Integration-Id / X-GitHub-Api-Version
        等）、Base URL 和 SSE 解析只有一份实现，避免 chat_widget 里再复制一套。
        """
        if self._cancel:
            return
        token = get_valid_copilot_token()
        if not token:
            self._emit_error(ApiError(
                401, "", provider="copilot", model=self.cfg.get("model", ""),
                message="未登录 GitHub Copilot：请先在设置里登录 GitHub。"))
            return

        from ..copilot_client import chat_completion

        model = self.cfg.get("model") or get_copilot_default_model()
        tools = self._tools if self.cfg.get("agent_mode") else None
        full = ""
        has_tc = False
        try:
            self._gen = chat_completion(
                token,
                self._prepare_messages(),
                model=model,
                temperature=self.cfg.get("temperature", 0.7),
                max_tokens=self.cfg.get("max_tokens", 4096),
                stream=True,
                tools=tools,
            )
            for event in self._gen:
                if self._cancel:
                    return
                # 兼容两种返回：SSE dict 事件 / 纯文本增量
                if isinstance(event, str):
                    full += event
                    self._emit_chunk(event)
                    continue
                if not isinstance(event, dict):
                    continue
                if event.get("error") and not event.get("choices"):
                    raise RuntimeError(f"Copilot 流内错误: {event['error']}")
                choices = event.get("choices") or []
                if not choices:
                    continue   # usage-only / copilot_references 等块
                delta = choices[0].get("delta") or {}
                tool_calls_delta = delta.get("tool_calls")
                if tool_calls_delta:
                    self._accumulate_tool_calls(tool_calls_delta)
                    has_tc = True
                content = delta.get("content")
                if content:
                    if has_tc and self._pending_tc:
                        self._flush_tool_calls()
                    full += content
                    self._emit_chunk(content)
                if choices[0].get("finish_reason"):
                    break
                if event.get("usage"):
                    self._emit_usage(event["usage"])

            self._flush_tool_calls()      # 流结束时补发未完成的工具调用
            self._flush_chunks()
            if not self._tc_emitted:
                self.done.emit(full)
        except Exception as e:
            self._emit_error(e)
        finally:
            self._gen = None
            self._close_resp()

    @staticmethod
    def _openai_to_anthropic_messages(openai_msgs):
        """将 OpenAI 格式的消息列表转为 Anthropic 格式"""
        anthro = []
        for m in openai_msgs:
            role = m["role"]
            if role == "system":
                continue  # system 在顶层参数中
            if role == "tool":
                # tool_result → user with tool_result content block
                anthro.append({
                    "role": "user",
                    "content": [{
                        "type": "tool_result",
                        "tool_use_id": m["tool_call_id"],
                        "content": m.get("content", ""),
                    }]
                })
            elif role == "assistant":
                content = m.get("content") or ""
                # 如果有 tool_calls，构建 content 块数组
                tc = m.get("tool_calls")
                if tc:
                    blocks = []
                    if content:
                        blocks.append({"type": "text", "text": content})
                    for t in tc:
                        fn = t.get("function", {})
                        try:
                            inp = json.loads(fn.get("arguments", "{}"))
                        except json.JSONDecodeError:
                            inp = {}
                        blocks.append({
                            "type": "tool_use",
                            "id": t.get("id", ""),
                            "name": fn.get("name", ""),
                            "input": inp,
                        })
                    anthro.append({"role": "assistant", "content": blocks})
                else:
                    anthro.append({"role": "assistant", "content": content})
            else:
                anthro.append({
                    "role": role,
                    "content": self._anthropic_content(m.get("content", "")),
                })
        return anthro

    @staticmethod
    def _anthropic_content(content):
        """OpenAI 多模态 part → Anthropic content block。

        纯字符串原样返回（老消息、工具结果都是字符串）；带图的转成
        ``{"type": "image", "source": {...}}``，否则 Anthropic 会因为
        不认识 ``image_url`` 直接 400。
        """
        if not isinstance(content, list):
            return content
        blocks = []
        for part in content:
            if not isinstance(part, dict):
                continue
            kind = part.get("type")
            if kind == "text":
                blocks.append({"type": "text", "text": part.get("text", "")})
            elif kind == "image_url":
                url = (part.get("image_url") or {}).get("url", "")
                if not isinstance(url, str) or not url:
                    continue
                if url.startswith("data:"):
                    head, _, payload = url.partition(",")
                    mime = head[5:].split(";")[0] or "image/png"
                    blocks.append({"type": "image", "source": {
                        "type": "base64", "media_type": mime,
                        "data": payload}})
                else:
                    blocks.append({"type": "image", "source": {
                        "type": "url", "url": url}})
        return blocks or ""

    @staticmethod
    def _anthropic_tools_from_openai(openai_tools):
        """将 OpenAI 格式的工具定义转为 Anthropic 格式"""
        result = []
        for t in openai_tools:
            fn = t.get("function", {})
            props = fn.get("parameters", {}).get("properties", {})
            required = fn.get("parameters", {}).get("required", [])
            # 转换为 Anthropic input_schema
            result.append({
                "name": fn.get("name", ""),
                "description": fn.get("description", ""),
                "input_schema": {
                    "type": "object",
                    "properties": props,
                    "required": required,
                }
            })
        return result

    def _anthropic(self):
        msgs = self._prepare_messages()
        url = f"{self.cfg['api_url']}/v1/messages"
        anthro_msgs = self._openai_to_anthropic_messages(msgs)
        sys_prompt = next((m["content"] for m in msgs if m["role"] == "system"), "")

        body = {
            "model": self.cfg.get("model", "claude-sonnet-4-20250514"),
            "messages": anthro_msgs,
            "max_tokens": self.cfg.get("max_tokens", 4096),
            "temperature": self.cfg.get("temperature", 0.7),
            "stream": True,
        }
        if sys_prompt:
            body["system"] = sys_prompt

        # Agent 模式下添加工具
        agent = self.cfg.get("agent_mode", True)
        if agent:
            body["tools"] = self._anthropic_tools_from_openai(self._tools)

        headers = {
            "Content-Type": "application/json",
            "x-api-key": self.cfg.get("api_key", ""),
            "anthropic-version": "2023-06-01",
        }
        try:
            resp = self._request(url, body, headers)
            full = ""
            # Anthropic 流式事件跟踪
            # content_blocks: index → {type, id, name, input_json_fragments[]}
            content_blocks = {}
            block_stopped = {}  # index → True when content_block_stop received
            pending_tc = []     # accumulated tool calls ready to emit

            for line in resp:
                if self._cancel:
                    return
                l = line.decode("utf-8", "replace").strip()
                if not l or not l.startswith("data: "):
                    continue
                p = l[6:]
                if p == "[DONE]":
                    break
                try:
                    d = json.loads(p)
                except json.JSONDecodeError:
                    continue

                evt = d.get("type", "")

                if evt == "content_block_start":
                    idx = d.get("index", 0)
                    block = d.get("content_block", {})
                    bt = block.get("type", "")
                    if bt == "tool_use":
                        content_blocks[idx] = {
                            "type": "tool_use",
                            "id": block.get("id", ""),
                            "name": block.get("name", ""),
                            "input_fragments": [],
                        }
                    elif bt == "text":
                        content_blocks[idx] = {
                            "type": "text",
                            "text": block.get("text", ""),
                        }
                        t = block.get("text", "")
                        if t:
                            full += t
                            self._emit_chunk(t)

                elif evt == "content_block_delta":
                    idx = d.get("index", 0)
                    delta = d.get("delta", {})
                    dt = delta.get("type", "")
                    if dt == "text_delta":
                        t = delta.get("text", "")
                        if t:
                            full += t
                            self._emit_chunk(t)
                    elif dt == "input_json_delta":
                        frag = delta.get("partial_json", "")
                        if idx in content_blocks:
                            content_blocks[idx]["input_fragments"].append(frag)

                elif evt == "content_block_stop":
                    idx = d.get("index", 0)
                    block_stopped[idx] = True
                    cb = content_blocks.get(idx)
                    if cb and cb["type"] == "tool_use":
                        # 组装完整 input JSON
                        raw = "".join(cb.get("input_fragments", []))
                        try:
                            json.loads(raw)  # 验证是有效 JSON
                        except json.JSONDecodeError:
                            raw = "{}"
                        pending_tc.append({
                            "id": cb["id"],
                            "type": "function",
                            "function": {
                                "name": cb["name"],
                                "arguments": raw,
                            }
                        })

                elif evt == "message_delta":
                    delta = d.get("delta", {})
                    stop_reason = delta.get("stop_reason", "")
                    if stop_reason == "tool_use" and pending_tc:
                        # 发出累积的工具调用
                        self._flush_tool_calls_anthropic(pending_tc)
                        pending_tc = []
                    elif stop_reason == "end_turn":
                        pass

                elif evt == "message_stop":
                    break

            # 如果有剩余工具调用尚未发出
            if pending_tc:
                self._flush_tool_calls_anthropic(pending_tc)
            self._flush_chunks()
            if not self._tc_emitted:
                self.done.emit(full)

        except Exception as e:
            self._emit_error(e)
        finally:
            self._close_resp()

    def _flush_tool_calls_anthropic(self, tc_list):
        """发出 Anthropic 格式的工具调用（已转换为 OpenAI 兼容格式）"""
        self._flush_chunks()
        if not tc_list:
            return
        self._tc_emitted = True
        # 直接使用已组装好的工具调用列表
        self.tool_calls.emit(tc_list)

    def _ollama(self):
        url = f"{self.cfg.get('api_url','http://localhost:11434')}/api/chat"
        msgs = self._prepare_messages()
        body = {
            "model": self.cfg.get("model", "qwen2.5-coder"),
            "messages": msgs,
            "stream": True,
            "options": {"temperature": self.cfg.get("temperature", 0.7)},
        }
        headers = {"Content-Type": "application/json"}
        try:
            resp = self._request(url, body, headers)
            full = ""
            for line in resp:
                if self._cancel: return
                l = line.decode("utf-8","replace").strip()
                if not l: continue
                try:
                    d = json.loads(l)
                    if d.get("done"): break
                    c = d.get("message",{}).get("content","")
                    if c: full += c; self._emit_chunk(c)
                    if d.get("usage"):
                        self._emit_usage(d["usage"])
                except: continue
            self._flush_chunks()
            self.done.emit(full)
        except Exception as e:
            self._emit_error(e)
        finally:
            self._close_resp()


# ===================================================================
# 命令下拉框
# ===================================================================

class CommandDropdown(QFrame):
    selected = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowFlags(Qt.Popup | Qt.FramelessWindowHint)
        theme.named(self, "popupFrame")
        theme.style_popup(self)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self._list = QListWidget()
        theme.named(self._list, "popupList")
        self._list.itemClicked.connect(lambda i: self.selected.emit(i.data(Qt.UserRole)))
        self._list.itemActivated.connect(lambda i: self.selected.emit(i.data(Qt.UserRole)))
        lay.addWidget(self._list)

    def show_commands(self, commands, pos):
        self._list.clear()
        for cmd in commands:
            item = QListWidgetItem(f"/{cmd['name']}  —  {cmd.get('description','')}")
            item.setData(Qt.UserRole, cmd["name"])
            self._list.addItem(item)
        self.adjustSize()
        self.move(pos)
        self.show()


# ===================================================================
# 参与者选择下拉框
# ===================================================================


class ParticipantDropdown(QFrame):
    """@参与者自动补全下拉框"""
    selected = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowFlags(Qt.Popup | Qt.FramelessWindowHint)
        theme.named(self, "popupFrame")
        theme.style_popup(self)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self._list = QListWidget()
        theme.named(self._list, "popupList")
        self._list.itemClicked.connect(lambda i: self.selected.emit(i.data(Qt.UserRole)))
        self._list.itemActivated.connect(lambda i: self.selected.emit(i.data(Qt.UserRole)))
        lay.addWidget(self._list)

    def show_participants(self, participants, pos):
        self._list.clear()
        for p in participants:
            icon = getattr(p, 'icon', '@')
            item = QListWidgetItem(f"{icon}{p.name}  —  {p.description}")
            item.setData(Qt.UserRole, p.name)
            self._list.addItem(item)
        self.adjustSize()
        self.move(pos)
        self.show()


# ===================================================================
# 消息导航（v0.8.17）：右侧边栏 + 悬停列出「你发过的每条消息」
# ===================================================================

def nav_preview(content, limit=48):
    """把一条用户消息压成列表里的一行。

    多模态消息（``content`` 是 part 列表）与纯文本都要能吃：
    先把首行压平（Markdown 标题、代码围栏、多余空白都去掉），再按字符
    数截断——中英混排下 ``len`` 比像素更省事，配 ``ElideRight`` 收尾。
    """
    text = ""
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        parts = [p.get("text", "") for p in content
                 if isinstance(p, dict) and p.get("type") == "text"]
        has_img = any(isinstance(p, dict) and p.get("type") == "image_url"
                      for p in content)
        text = " ".join(t for t in parts if t)
        if has_img:
            text = ("[图片] " + text).strip()
    # 去掉 Markdown 装饰，让列表读起来像"你说过的话"而不是源码
    text = re.sub(r"^\s*#{1,6}\s*", "", text.strip())
    text = re.sub(r"^\s*[-*+]\s+", "", text)
    text = re.sub(r"^\s*```\w*\s*$", "", text, flags=re.M)
    text = re.sub(r"`{1,3}([^`]*)`{1,3}", r"\1", text)
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)   # 链接留文字
    text = text.replace("\r\n", "\n").split("\n")[0].strip()
    text = re.sub(r"\s+", " ", text)
    if len(text) > limit:
        text = text[:limit].rstrip() + "…"
    return text or "（空消息）"


class MessageRail(QFrame):
    """对话流右侧的细导航条：每一道刻度 = 一条用户消息。

    对标浏览器阅读进度条 / 聊天应用的右侧消息索引。常驻显示当前位置，
    鼠标移上去（或点击）弹出完整清单。刻度**等距摆放**而不是按消息在
    文档里的真实比例——按真实比例摆的话，长问答里几条用户消息会挤在
    顶部叠成一坨，既点不中也看不清。

    自绘而不是堆一堆小 QWidget：几十条消息就是几十个控件，滚动时逐个
    重排既慢又容易在 deleteLater 期间摸到半死对象。一个 paintEvent 全画完。
    """

    locate = Signal(int)      # 请求滚动到第 index 条用户消息
    hovered = Signal(int)     # 悬停到某条（-1 = 离开）
    flyout_requested = Signal()

    WIDTH = 14
    TICK_H = 4
    TICK_W = 8
    CUR_H = 14
    CUR_W = 12

    def __init__(self, parent=None):
        super().__init__(parent)
        theme.named(self, "messageRail")
        self.setFixedWidth(self.WIDTH)
        self.setCursor(Qt.PointingHandCursor)
        self.setMouseTracking(True)
        self.setAttribute(Qt.WA_Hover, True)
        #: [提示文本] —— 只存 tip，位置由 _tick_rects 现算
        self._ticks = []
        self._index = -1        # 当前视口最靠上的一条用户消息
        self._hover = -1
        self._hover_bg = False
        self.setVisible(False)

    def set_ticks(self, ticks, current=-1):
        """ticks: ``[(ratio, tooltip), ...]``；current 为高亮项下标。"""
        self._ticks = list(ticks or [])
        self._index = int(current)
        if self._hover >= len(self._ticks):
            self._hover = -1
        self.setVisible(bool(self._ticks))
        self.update()

    def _tick_rects(self):
        """每道刻度居中对齐在左缘，当前项更高更宽。

        等距排布：间距按可用高度和条数算，但不小于 ``TICK_H + 4``，
        超过就让它溢出——宁可让最后几条压到边界，也不要把 20 条消息
        压进 40px 里变成一根实线。
        """
        n = len(self._ticks)
        if n <= 0:
            return []
        h = max(1, self.height())
        step = self.TICK_H + 4
        if n > 1:
            # 尽量铺满，但间距不小于 8px
            step = max(step, min(28, (h - self.CUR_H) // max(1, n - 1)))
        total = self.CUR_H + step * (n - 1)
        start = max(2, (h - total) // 2)
        rects = []
        for i in range(n):
            y = start + step * i
            if i == self._index:
                w, hh = self.CUR_W, self.CUR_H
            elif i == self._hover:
                w, hh = self.TICK_W + 2, self.TICK_H + 2
            else:
                w, hh = self.TICK_W, self.TICK_H
            rects.append(QRect((self.WIDTH - w) // 2, int(y), w, hh))
        return rects

    def paintEvent(self, _event):  # noqa: N802 - Qt 接口
        t = theme.current_theme()
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setPen(Qt.NoPen)
        rects = self._tick_rects()
        if not rects:
            p.end()
            return
        # 悬停时先铺一层极淡的轨道，让"这里可以点"这件事被发现
        if self._hover_bg or self._hover >= 0:
            p.setBrush(QColor(t["surface_alt"]))
            p.drawRoundedRect(
                QRectF(self.rect()).adjusted(1.5, 0, -1.5, 0), 7, 7)
        for i, r in enumerate(rects):
            if i == self._index:
                p.setBrush(QColor(t["accent"]))
            elif i == self._hover:
                p.setBrush(QColor(t["text"]))
            else:
                p.setBrush(QColor(t["text_muted"]))
            p.drawRoundedRect(QRectF(r), r.width() / 2.0, r.width() / 2.0)
        p.end()

    def _index_at(self, pos):
        rects = self._tick_rects()
        if not rects:
            return -1
        best, best_d = -1, 1e9
        for i, r in enumerate(rects):
            d = abs(pos.y() - r.center().y())
            if d < best_d:
                best, best_d = i, d
        return best if best_d <= 16 else -1

    def enterEvent(self, event):  # noqa: N802 - Qt 接口
        self._hover_bg = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event):  # noqa: N802 - Qt 接口
        self._hover_bg = False
        self._hover = -1
        self.setToolTip("")
        self.hovered.emit(-1)
        self.update()
        super().leaveEvent(event)

    def mouseMoveEvent(self, event):  # noqa: N802 - Qt 接口
        i = self._index_at(event.pos())
        if i != self._hover:
            self._hover = i
            self.hovered.emit(i)
            self.update()
        if 0 <= i < len(self._ticks):
            self.setToolTip(self._ticks[i][1])
        else:
            self.setToolTip("")
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):  # noqa: N802 - Qt 接口
        if event.button() != Qt.LeftButton:
            return
        i = self._index_at(event.pos())
        if i >= 0:
            self.locate.emit(i)
        # 点空白处 = 打开清单，给"我只想看看发过什么"的用户一条路
        else:
            self.flyout_requested.emit()
        super().mouseReleaseEvent(event)


class MessageFlyout(QFrame):
    """悬停展开的完整清单：一条用户消息一行，点一下跳过去。

    做成**面板内的子控件**而不是 ``Qt.Popup`` 独立窗口：Popup 一出现就
    抓走鼠标，视口再也收不到 ``MouseMove``，"鼠标移开自动收起"这类交互
    全部失效，只能靠点击关闭。子控件还顺带满足了另一个需求——它要盖在
    对话流之上、且不该被滚动区裁剪，位置交给面板算。
    """

    locate = Signal(int)

    MAX_ROWS = 24
    ROW_PAD = 7

    def __init__(self, parent=None):
        super().__init__(parent)
        theme.named(self, "messageFlyout")
        self.setVisible(False)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(4, 4, 4, 4)
        self._list = QListWidget()
        theme.named(self._list, "popupList")
        self._list.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self._list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._list.itemClicked.connect(
            lambda it: self._pick(it.data(Qt.UserRole)))
        self._list.itemActivated.connect(
            lambda it: self._pick(it.data(Qt.UserRole)))
        lay.addWidget(self._list)

    def _pick(self, index):
        if not isinstance(index, int):
            return
        # 先收起再发信号：跳转过程中清单留在原地会挡住目标消息
        self.setVisible(False)
        self.locate.emit(index)

    def build(self, items, current=-1):
        """装填清单，返回建议尺寸。``items: [(序号, 预览文本)]``。"""
        self._list.clear()
        fm = self._list.fontMetrics()
        widest = 200
        for index, text in items[:self.MAX_ROWS]:
            item = QListWidgetItem("%d. %s" % (index + 1, text))
            item.setData(Qt.UserRole, index)
            item.setToolTip(text)
            item.setSizeHint(QSize(0, fm.height() + self.ROW_PAD * 2))
            self._list.addItem(item)
            widest = max(widest, fm.horizontalAdvance(text))
        if len(items) > self.MAX_ROWS:
            more = QListWidgetItem("… 还有 %d 条，用左侧刻度继续找"
                                   % (len(items) - self.MAX_ROWS))
            more.setFlags(Qt.NoItemFlags)
            self._list.addItem(more)
        self.set_current(current)
        width = max(240, min(460, widest + 64))
        rows = min(len(items), self.MAX_ROWS) + (
            1 if len(items) > self.MAX_ROWS else 0)
        height = min(430, rows * (fm.height() + self.ROW_PAD * 2) + 12)
        return QSize(width, height)

    def set_current(self, current):
        """只移动选中行，不重建列表。

        鼠标掠过刻度会连续换高亮；每次都 clear + addItem 的话，列表的
        滚动位置会被重置回顶部，长会话里刚翻到底又被弹回去。
        另外：必须先 addItem 再谈选中——还没进 model 的 item，
        ``setSelected`` / ``setCurrentItem`` 都是空操作（v0.8.17 首版实拍
        里"当前项"没亮就是这个原因）。
        """
        if not (0 <= current < self._list.count()):
            self._list.clearSelection()
            self._list.setCurrentItem(None)
            return
        item = self._list.item(current)
        if item is None or not (item.flags() & Qt.ItemIsEnabled):
            return
        self._list.setCurrentItem(item)
        item.setSelected(True)
        self._list.scrollToItem(item, QListWidget.EnsureVisible)

    def locate_tip(self, text):
        self._list.setToolTip(text)


# ===================================================================
# 聊天面板
# ===================================================================

class CopilotChatPanel(QWidget):
    UI_FONT_SIZE = 13
    MAX_TOOL_ROUNDS = 40  # Agent 工具调用最多保留的轮次（消息裁剪用）

    @classmethod
    def set_ui_font_size(cls, size):
        cls.UI_FONT_SIZE = size

    def __init__(self, parent=None):
        super().__init__(parent)
        self._messages = []
        #: 待发送的图片附件：[{"path": ..., "name": ...}]
        self._attachments = []
        self._config = get_provider_config()
        self._system_prompt = load_system_prompt()
        self._worker = None
        self._workers = []          # 保留运行中的 worker 引用（P1-2）
        self._req_gen = 0           # 请求代号：旧 worker 的信号一律丢弃
        # ── Cline Edition：Plan/Act 模式、异步审批、检查点 ──
        # 注意：0.4.1 开发分支里这里曾 connect 到不存在的 _flush_stream，
        # 会在面板实例化时抛 AttributeError，现改为显式状态。
        self._mode = self._config.get("default_mode", "act")
        if self._mode not in ("plan", "act"):
            self._mode = "act"
        self._awaiting_approval = False
        self._pending_card = None
        self._last_card = None
        self._checkpoints = CheckpointStore()
        self._current_bubble = None
        self._context_files = []
        self._cmd_dropdown = None
        self._participant_dropdown = None
        self._tool_round = 0
        self._agent_active = False
        self._tc_handled = False
        self._code_review_panel = None
        self._tool_call_history = []
        # ── v0.8.0 Autonomy Edition：预算式自主循环 ──
        # 主流 Agent 不会在固定步数处把人踢出循环，而是「软提醒 → 硬预算
        # → 自动续跑」，只在真正死循环（重复写同一操作）时停下。
        self._soft_warned = False        # 软阈值提醒是否已发过（只发一次）
        self._repeat_warned = False      # 只读重复调用的警告是否已发过
        self._auto_continues = 0         # 本任务已自动续跑次数
        self._round_had_progress = False  # 上一轮是否产生了实际改动
        self._stall_rounds = 0           # 连续「只读无进展」轮数
        self._total_tool_rounds = 0      # 本任务累计轮次（跨自动续跑）
        self._pending_notes = []         # 待注入上下文的系统注记
        self._models_worker = None
        self._settings_dialog = None
        # ── v0.5.5 P0：非阻塞渲染 / 后台工具 / 任务状态 / 用量 ──
        self._chunk_buffer = []
        self._chunk_timer = QTimer(self)
        self._chunk_timer.setSingleShot(True)
        self._chunk_timer.setInterval(
            max(10, int(self._config.get("stream_throttle_ms", 30))))
        self._chunk_timer.timeout.connect(self._flush_chunks)
        self._scroll_pending = False
        self._bg_refs = []                     # 防止 QRunnable / signals 被 GC
        self._cancel_event = new_cancel_event()
        self._usage = UsageMeter(
            context_window=self._config.get("context_window", 1048576),
            price_in=self._config.get("price_input_per_million", 0.0),
            price_out=self._config.get("price_output_per_million", 0.0),
        )
        self._usage_ticks = 0
        self._verify_attempts = 0
        self._task_store = TaskStateStore()
        self._busy = False
        self._task_started_at = 0.0
        self._pending_tool_calls = []
        self._pending_card_for_tools = None
        # ── v0.5.6 P1：规则 / Skills / 权限 / 审计 / 仓库索引 / 分模型 ──
        self._audit = configure_audit(
            bool(self._config.get("audit_enabled", True)))
        self._current_model = self._config.get("model", "")
        self._index_warming = False
        # Agent & Participant 状态
        self._active_agent = None  # 当前选中的内置 Agent
        self._active_participant = "agent"  # @participant
        self._pm = ParticipantManager.get_instance()
        self._pm.participant_changed.connect(self._on_participant_changed)
        # ── v0.7.1 编辑器上下文（光标 / 选区） ──
        # 面板不直接碰 Spyder 编辑器：由插件注入一个 provider，
        # 面板只负责在光标停下后渲染胶囊、发送时拼进消息。
        self._editor_mode = str(
            self._config.get("editor_context_mode", "auto"))
        if self._editor_mode not in (
                "auto", "selection", "function", "file", "off"):
            self._editor_mode = "auto"
        self._editor_ctx_fn = None
        self._editor_ctx = None
        # v0.7.9：编辑器内联 diff 复核（Cursor 式接受 / 撤销）
        self._review_provider = None   # 插件注入：items -> None
        self._review_before = {}       # {path: 写盘前内容}
        self._review_scan = None       # 目录级兜底快照（见 _scan_sources）
        self._editor_timer = QTimer(self)
        self._editor_timer.setSingleShot(True)
        self._editor_timer.setInterval(250)
        self._editor_timer.timeout.connect(self._refresh_editor_chip)
        self._apply_font_size()
        self._setup_ui()
        self._apply_font_size_to_ui()
        self._install_font_shortcuts()
        self._add_welcome()
        self._refresh_usage_ui()
        # 启动后异步拉取 Copilot 模型列表（不阻塞 UI）
        QTimer.singleShot(0, self._start_models_refresh_if_needed)
        QTimer.singleShot(0, self._check_resumable_task)
        if self._config.get("repo_index_enabled", True):
            QTimer.singleShot(400, self._warm_repo_index)

    def _start_models_refresh_if_needed(self):
        """按需后台刷新 Copilot 模型列表。

        0.5.1 修复：0.5.0 基于的开发分支只留下这个调用却没有实现，
        导致 CopilotChatPanel 在 Spyder 中实例化时直接抛 AttributeError，
        插件面板无法创建。这里补上安全实现：仅 Copilot provider 才发起请求，
        任何异常都只记录日志，绝不影响面板加载。
        """
        try:
            if self._config.get("provider", "deepseek") != "copilot":
                return
            worker = self._models_worker
            if worker is not None and worker.isRunning():
                return
            self._models_worker = fetch_copilot_models_async(
                self._on_models_loaded, self._on_models_failed)
        except Exception as exc:
            logger.debug("跳过 Copilot 模型列表刷新: %s", exc)

    def _on_models_loaded(self, models):
        """模型列表刷新成功：只更新缓存，不触碰 UI 控件。"""
        self._models_worker = None
        try:
            set_cached_copilot_models(models)
        except Exception as exc:
            logger.debug("缓存 Copilot 模型列表失败: %s", exc)

    def _on_models_failed(self, message):
        """模型列表刷新失败：静默回退到内置默认列表。"""
        self._models_worker = None
        logger.debug("获取 Copilot 模型列表失败: %s", message)

    def _apply_font_size(self):
        fs = self._config.get("font_size", 14)
        uifs = self._config.get("ui_font_size", 13)
        MessageBubble.set_font_size(fs)
        ChatInput.set_font_size(fs)
        self.set_ui_font_size(uifs)

    # ── 字号 / 字体（v0.8.8）──────────────────────────────────
    #
    # 为什么要有这一层：设置页里的两个 spinbox 太深了，用户根本找不到，
    # 反馈就变成"字号太小、还不能调"。这里给出三条随手可用的入口：
    # 顶栏 Aa 菜单、Ctrl+= / Ctrl+- / Ctrl+0、对话流里 Ctrl+滚轮。
    # 三者改的是同一份配置（config.json 的 font_size / ui_font_size /
    # font_family），改完立即重绘并落盘。

    def _body_font_size(self):
        try:
            return int(self._config.get("font_size", 14))
        except (TypeError, ValueError):
            return 14

    def _ui_font_size(self):
        try:
            return int(self._config.get("ui_font_size", 13))
        except (TypeError, ValueError):
            return 13

    def _persist_font(self, **changes):
        """写回 config.json。

        注意不能直接 ``save_config(self._config)``：``_config`` 是
        ``get_provider_config()`` 派生出来的字典，里面有 label / chat_endpoint
        / supports_agent 这类只读字段，写回去会把配置文件弄脏。
        """
        for key, value in changes.items():
            self._config[key] = value
        try:
            cfg = load_config()
            cfg.update(changes)
            save_config(cfg)
        except Exception as exc:
            logger.debug("保存字号设置失败: %s", exc)

    def _apply_font_now(self):
        """字号变了：重算 QSS + 让每条已存在的消息按新字号重排。"""
        self._apply_font_size()
        self._apply_theme()

    def _set_body_font(self, size):
        size = max(9, min(max(theme.FONT_STEPS), int(size)))
        if size == self._body_font_size():
            return
        self._persist_font(font_size=size)
        self._apply_font_now()
        self._set_status(f"正文字号 {size}px（Ctrl+= / Ctrl+- 继续调整）")

    def _bump_body_font(self, direction):
        """按档位表走：滚轮一格 = 一档，不会 1px 1px 磨。"""
        cur = self._body_font_size()
        steps = theme.FONT_STEPS
        if direction > 0:
            nxt = next((s for s in steps if s > cur), steps[-1])
        else:
            nxt = next((s for s in reversed(steps) if s < cur), steps[0])
        self._set_body_font(nxt)

    def _set_ui_font(self, size):
        size = max(9, min(32, int(size)))
        if size == self._ui_font_size():
            return
        self._persist_font(ui_font_size=size)
        self._apply_font_now()
        self._set_status(f"界面字号 {size}px")

    def _bump_ui_font(self, direction):
        self._set_ui_font(self._ui_font_size() + (1 if direction > 0 else -1))

    def _set_font_family(self, family):
        self._persist_font(font_family=family or "")
        self._apply_font_now()
        label = next((lbl for lbl, val in theme.FONT_CHOICES
                      if val == (family or "")), family or "跟随系统")
        self._set_status(f"字体：{label}")

    def _reset_fonts(self):
        self._persist_font(font_size=14, ui_font_size=13, font_family="")
        self._apply_font_now()
        self._set_status("字号与字体已恢复默认")

    def _popup_font_menu(self, *_args):
        """顶栏 Aa：重建菜单再弹（档位与勾选状态每次都现算）。"""
        menu = getattr(self, "_font_menu", None)
        if menu is None:
            return
        menu.clear()
        t = theme.current_theme()
        icon = lambda name: theme.make_icon(name, t["text_muted"])  # noqa: E731

        head = menu.addAction(f"正文字号 {self._body_font_size()} px")
        head.setEnabled(False)
        bigger = menu.addAction(icon("plus"), "放大\tCtrl+=")
        bigger.triggered.connect(lambda: self._bump_body_font(1))
        smaller = menu.addAction(icon("minus"), "缩小\tCtrl+-")
        smaller.triggered.connect(lambda: self._bump_body_font(-1))

        menu.addSeparator()
        for step in theme.FONT_STEPS:
            act = menu.addAction(f"{step} px")
            act.setCheckable(True)
            act.setChecked(step == self._body_font_size())
            act.triggered.connect(
                lambda _c=False, s=step: self._set_body_font(s))

        menu.addSeparator()
        ui_head = menu.addAction(f"界面字号 {self._ui_font_size()} px")
        ui_head.setEnabled(False)
        ui_up = menu.addAction("界面放大")
        ui_up.triggered.connect(lambda: self._bump_ui_font(1))
        ui_dn = menu.addAction("界面缩小")
        ui_dn.triggered.connect(lambda: self._bump_ui_font(-1))

        menu.addSeparator()
        sub = menu.addMenu("字体")
        theme.style_popup(sub, t)
        cur_family = str(self._config.get("font_family", "") or "")
        for label, value in theme.FONT_CHOICES:
            act = sub.addAction(label)
            act.setCheckable(True)
            act.setChecked(value == cur_family)
            act.triggered.connect(
                lambda _c=False, v=value: self._set_font_family(v))

        menu.addSeparator()
        reset = menu.addAction("恢复默认（正文 14 / 界面 13）")
        reset.triggered.connect(self._reset_fonts)

        btn = getattr(self, "_font_btn", None)
        if btn is not None:
            menu.popup(btn.mapToGlobal(QPoint(0, btn.height() + 4)))
        else:
            menu.popup(QCursor.pos())

    def _install_font_shortcuts(self):
        """Ctrl+= / Ctrl+- / Ctrl+0。

        用 QAction + WidgetWithChildrenShortcut 而不是 QShortcut：焦点在
        composer（QPlainTextEdit）里时快捷键也要生效，只有挂成"子控件范围"
        才收得到。
        """
        self._font_actions = []
        bindings = (
            (("Ctrl+=", "Ctrl++", "Ctrl+Shift+="), lambda: self._bump_body_font(1)),
            (("Ctrl+-", "Ctrl+Shift+-"), lambda: self._bump_body_font(-1)),
            (("Ctrl+0",), self._reset_fonts),
        )
        for sequences, handler in bindings:
            act = QAction(self)
            act.setShortcutContext(Qt.WidgetWithChildrenShortcut)
            act.setShortcuts([QKeySequence(s) for s in sequences])
            act.triggered.connect(handler)
            self.addAction(act)
            self._font_actions.append(act)


    def _apply_theme(self):
        """v0.6.0：把主题 / 强调色 / 密度 / 字号 / 工具痕迹密度一次装到整棵控件树。

        旧实现把颜色硬编码在 40 多处 ``setStyleSheet`` 里，且只有暗色。
        现在所有视觉都来自 :mod:`theme` 的语义 token，切换即时生效。
        """
        t = theme.resolve_theme(
            str(self._config.get("ui_theme", "auto")),
            str(self._config.get("ui_accent", theme.DEFAULT_ACCENT)),
            str(self._config.get("ui_density", "comfortable")),
            int(self._config.get("ui_font_size", 13)),
            int(self._config.get("font_size", 14)),
            str(self._config.get("font_family", "")),
        )
        theme.apply_theme(self, t)
        MessageBubble.set_tool_density(
            str(self._config.get("tool_density", "balanced")))

        # 图标是带色位图缓存，换主题后要重新取色
        for btn, name in getattr(self, "_icon_buttons", []):
            btn.setIcon(theme.make_icon(name, t["text_muted"]))
        self._send_btn.setIcon(theme.make_icon("arrow_up", t["on_accent"], 16))
        self._cancel_btn.setIcon(theme.make_icon("stop", t["danger"]))
        # 胶囊里的图标/箭头/字号都是算出来的，换主题要整颗重画
        for pill_w in (getattr(self, "_mode_btn", None),
                       getattr(self, "_agent_btn", None)):
            if hasattr(pill_w, "refresh_theme"):
                pill_w.refresh_theme()
        self._refresh_mode_button()
        self._refresh_policy_chip()
        # 字号变了，胶囊高度/文案要跟着重算；Agent 菜单是独立窗口，重装样式
        if getattr(self, "_agent_btn", None) is not None:
            self._refresh_agent_pill()
            theme.style_popup(self._agent_menu, t)

        # Markdown / diff 用内联 HTML 色，不继承 QSS，必须重绘
        lay = getattr(self, "_inner_lay", None)
        if lay is not None:
            for i in range(lay.count()):
                w = lay.itemAt(i).widget()
                if isinstance(w, (MessageBubble, WelcomeCard)):
                    w.refresh_theme()
                elif isinstance(w, ApprovalCard):
                    w.refresh_theme()

        # 菜单 / 弹出下拉是独立窗口，不继承面板 QSS
        for popup in (getattr(self, "_menu", None),
                      getattr(self, "_font_menu", None),
                      getattr(self, "_mode_menu", None),
                      getattr(self, "_agent_menu", None),
                      getattr(self, "_editor_menu", None),
                      self._cmd_dropdown, self._participant_dropdown):
            if popup is not None:
                try:
                    theme.style_popup(popup, t)
                except RuntimeError:
                    pass
        # 次要面板仍带旧版内联 QSS，按当前主题统一重着色；
        # 任务面板已是主题原生控件，直接让它重画图标与字号
        todo = getattr(self, "_todo_panel", None)
        if todo is not None and hasattr(todo, "refresh_theme"):
            try:
                todo.refresh_theme(t)
            except RuntimeError:
                pass
        for extra in (getattr(self, "_code_review_panel", None),):
            if extra is not None:
                try:
                    theme.harmonize(extra, t)
                except RuntimeError:
                    pass
        # 导航条的刻度是自绘的，主题变了要重画一遍
        rail = getattr(self, "_rail", None)
        if rail is not None:
            try:
                rail.update()
            except RuntimeError:
                pass
        flyout = getattr(self, "_flyout", None)
        if flyout is not None:
            try:
                theme.style_popup(flyout, t)
            except RuntimeError:
                pass
        return t

    def _apply_font_size_to_ui(self):
        """兼容入口：字号与样式现在统一由 _apply_theme 处理。"""
        self._apply_theme()

    def _refresh_policy_chip(self):
        """composer 里的权限徽章：让"当前有多自主"一眼可见。"""
        chip_w = getattr(self, "_policy_chip", None)
        if chip_w is None:
            return
        policy = str(self._config.get("approval_policy", "risk"))
        text = {
            "risk": "按风险", "writes": "写操作",
            "all": "逐项", "none": "自动",
        }.get(policy, "按风险")
        if self._config.get("auto_execute", False):
            text = "自动"
        chip_w.setText(text)
        chip_w.setToolTip(
            "审批策略：%s\n危险操作始终需要逐项确认" % {
                "risk": "按风险审批", "writes": "写操作审批",
                "all": "逐项审批", "none": "自动执行",
            }.get(policy, policy))
        theme.set_prop(chip_w, "kind", "danger" if policy == "none" else "neutral")

    def _setup_ui(self):
        t = theme.current_theme()
        m = t.metrics
        theme.named(self, "chatRoot")
        self._icon_buttons = []
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # ── 顶栏：品牌 + 模型 chip + 新对话 + 溢出菜单 ──
        # 旧版把 10 个文字/emoji 按钮全塞在一行，窄侧边栏里会挤成一团。
        # 次要动作收进单一溢出菜单，只留最高频的"新对话"常驻。
        header = QWidget()
        theme.named(header, "chatHeader")
        h = QHBoxLayout(header)
        h.setContentsMargins(m["pad"] + 4, 3, m["pad"] + 2, 3)
        h.setSpacing(8)

        # 品牌图标：固定尺寸，绝不能让它吃掉顶栏的剩余空间
        # （0.6.1 的 bug：这个没起名字的 QLabel 被布局拉伸到 300px 宽，
        #  又被宿主 qdarkstyle 样式表刷成 #19232d，于是顶栏出现一条黑条）
        mark = QLabel()
        theme.named(mark, "brandMark")
        mark.setFixedSize(20, 20)
        mark.setScaledContents(True)
        mark.setPixmap(theme.accent_icon("spark", t).pixmap(QSize(20, 20)))
        h.addWidget(mark)
        # 产品名在 dock 标签上已经有了，顶栏再写一遍既冗余又占宽
        self._title = None

        # 剩余空间交给弹簧，而不是某个控件——顶栏各元素按内容宽度排列
        h.addStretch(1)

        self._provider_label = QLabel(self._provider_text())
        theme.named(self._provider_label, "modelChip")
        self._provider_label.setSizePolicy(
            QSizePolicy.Maximum, QSizePolicy.Fixed)
        self._provider_label.setMaximumWidth(320)
        self._provider_label.setToolTip(self._provider_text())
        h.addWidget(self._provider_label)

        # ── 字号入口（v0.8.8）──
        # 用户找不到「设置 → 外观」里那个 spinbox，反馈就是「字号太小还不能
        # 调」。把入口放到顶栏：一个 Aa，点开就是放大 / 缩小 / 预设 / 字体。
        self._font_btn = theme.icon_button(
            "font",
            "字号与字体（Ctrl+= 放大 · Ctrl+- 缩小 · Ctrl+0 还原 · Ctrl+滚轮）",
            28)
        self._font_btn.clicked.connect(self._popup_font_menu)
        self._icon_buttons.append((self._font_btn, "font"))
        h.addWidget(self._font_btn)
        self._font_menu = QMenu(self)
        theme.named(self._font_menu, "overflowMenu")
        theme.style_popup(self._font_menu, t)

        self._new_chat_btn = theme.icon_button("plus", "新建对话（清空当前会话）", 28)
        self._new_chat_btn.clicked.connect(self._clear_chat)
        self._icon_buttons.append((self._new_chat_btn, "plus"))
        h.addWidget(self._new_chat_btn)

        self._more_btn = theme.icon_button("more", "更多操作", 28)
        self._icon_buttons.append((self._more_btn, "more"))
        self._menu = QMenu(self)
        theme.named(self._menu, "overflowMenu")
        theme.style_popup(self._menu, t)
        # 不 setMenu：QToolButton 会因此多画一个菜单箭头，在窄侧栏里白占
        # 20~30px。改成点击后自己弹，按钮就只剩图标本身。
        self._more_btn.clicked.connect(self._popup_overflow_menu)
        h.addWidget(self._more_btn)
        layout.addWidget(header)

        # ── 隐藏的动作按钮：保留既有的信号连接，动作收进溢出菜单 ──
        def action_button(name, tip, fn):
            b = theme.icon_button(name, tip, 24)
            b.clicked.connect(fn)
            return b

        self._ctx_btn = action_button("clip", "添加项目文件", self._attach_context)
        self._history_btn = action_button("clock", "聊天历史", self._show_history)
        self._review_btn = action_button("diff", "代码审查", self._toggle_code_review)
        self._inline_btn = action_button("chat", "内联聊天", self._open_inline_chat)
        self._audit_btn = action_button(
            "shield", "查看结构化审计日志（工具轨迹 / 审批 / 用量 / 拦截）",
            self._open_audit_viewer)
        self._index_btn = action_button(
            "map", "重建仓库结构索引（目录树 + 顶层符号）",
            self._rebuild_repo_index)
        self._undo_btn = action_button(
            "undo", "撤销上一步 AI 的文件更改（检查点）",
            self._undo_last_checkpoint)
        self._settings_btn = action_button(
            "settings", "设置", lambda: self._open_settings())

        def add_menu(text, icon_name, btn, checkable=False):
            act = self._menu.addAction(
                theme.make_icon(icon_name, t["text_muted"]), text,
                lambda _c=False, b=btn: b.click())
            act.setCheckable(checkable)
            return act

        add_menu("添加上下文文件", "clip", self._ctx_btn)
        add_menu("聊天历史", "clock", self._history_btn)
        add_menu("代码审查", "diff", self._review_btn, checkable=True)
        self._review_action = self._menu.actions()[-1]
        add_menu("内联聊天", "chat", self._inline_btn)
        self._menu.addSeparator()
        add_menu("撤销上一步更改", "undo", self._undo_btn)
        add_menu("审计日志", "shield", self._audit_btn)
        add_menu("重建仓库索引", "map", self._index_btn)
        self._menu.addSeparator()
        add_menu("设置", "settings", self._settings_btn)
        self._appearance_action = self._menu.addAction(
            theme.make_icon("spark", t["text_muted"]), "外观与主题",
            lambda: self._open_settings("外观"))
        self._menu.addAction(
            theme.make_icon("font", t["text_muted"]), "字号与字体…",
            self._popup_font_menu)

        # ── Plan / Act：下拉式模式选择器（放进底部 composer） ──
        # v0.7.8：胶囊换成 theme.pill（图标 + 文案 + 矢量箭头）。旧的
        # QToolButton 只能 "Act ▾"，那个 ▾ 是字符，字号一改就贴边/失焦。
        self._mode_btn = theme.pill("Act", "bolt", "act")
        self._mode_btn.clicked.connect(self._popup_mode_menu)
        self._mode_menu = QMenu(self._mode_btn)
        theme.named(self._mode_menu, "overflowMenu")
        theme.style_popup(self._mode_menu, t)
        self._plan_action = self._mode_menu.addAction(
            theme.make_icon("eye", t["info"]), "Plan · 只读规划",
            lambda: self._set_mode("plan"))
        self._act_action = self._mode_menu.addAction(
            theme.make_icon("terminal", t["accent"]), "Act · 可执行更改",
            lambda: self._set_mode("act"))
        self._refresh_mode_button()

        # ── Agent 选择器（放进底部 composer） ──
        # 用「胶囊 + QMenu」而不是 QComboBox：QComboBox 的弹出列表是一个独立
        # 窗口，宽度只按控件自身宽度算，行高也不认 QSS 的 ::item padding——
        # 在 78px 宽的 composer 里，七个条目会被中部省略成 Def…ent / Co…nt，
        # 行与行之间还会互相压字（v0.7.2 实拍）。QMenu 的尺寸由内容决定，
        # 永远不会缩到读不全；这也和左边 Plan/Act 胶囊是同一套交互。
        self._agent_btn = theme.pill("默认", "chat", "plain")
        self._agent_btn.clicked.connect(lambda: self._open_agent_menu())
        self._agent_menu = QMenu(self._agent_btn)
        theme.named(self._agent_menu, "overflowMenu")
        theme.style_popup(self._agent_menu, t)
        self._agent_menu.setToolTipsVisible(True)
        self._populate_agent_menu()

        # ── Todo 面板 ──
        self._todo_panel = TodoPanel()
        layout.addWidget(self._todo_panel)

        # ── 对话流 ──
        self._scroll = ZoomScrollArea()
        theme.named(self._scroll, "stream")
        self._scroll.zoom_requested.connect(self._bump_body_font)
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.NoFrame)
        # 对话流永远不横向滚动：一条长路径就能把内容顶宽，底部那条横向
        # 滚动条会吃掉视图高度、把审批按钮挤到折叠线以下（v0.8.4 实拍）。
        # 内容侧已经做了省略（ElidedLabel），这里直接禁掉。
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._inner = QWidget()
        theme.named(self._inner, "streamInner")
        self._inner_lay = QVBoxLayout(self._inner)
        self._inner_lay.setAlignment(Qt.AlignTop)
        self._inner_lay.setSpacing(m["gap"])
        self._inner_lay.setContentsMargins(
            m["pad"], m["pad"], m["pad"], m["pad"])
        self._inner_lay.addStretch()
        self._scroll.setWidget(self._inner)
        layout.addWidget(self._scroll, 1)

        # ── 消息导航条（v0.8.17）──
        # 悬浮在对话流右缘：列出发过的每条消息，点一下跳过去。
        # 做成面板的子控件而不是塞进布局——塞进去会永久吃掉 10px 行宽，
        # 而且在窄侧栏里会把正文挤得更窄。定位交给 _place_rail()。
        self._rail = MessageRail(self)
        self._rail.locate.connect(self._locate_user_message)
        self._rail.flyout_requested.connect(self._show_message_flyout)
        self._rail.hovered.connect(self._on_rail_hovered)
        self._rail.raise_()
        self._flyout = MessageFlyout(self)
        self._flyout.locate.connect(self._locate_user_message)
        #: 浮层当前装的是第几条 / 一共几条——用来判断"要不要重建列表"
        self._flyout_count = -1
        self._flyout_highlight = -2
        #: [{"bubble": ..., "text": ...}] —— 只装用户消息，供导航条与清单用
        self._user_marks = []
        self._rail_timer = QTimer(self)
        self._rail_timer.setSingleShot(True)
        self._rail_timer.setInterval(120)
        self._rail_timer.timeout.connect(self._refresh_rail)
        self._scroll.verticalScrollBar().valueChanged.connect(
            lambda _v: self._rail_timer.start())
        # 悬停整个对话流右缘也能唤出清单，不用精确命中那 10px
        self._scroll.viewport().installEventFilter(self)
        self._scroll.installEventFilter(self)

        # ── 底部 dock：状态 → 统一 composer → 用量条 ──
        dock = QWidget()
        theme.named(dock, "composerDock")
        dock_lay = QVBoxLayout(dock)
        dock_lay.setContentsMargins(m["pad"], m["gap"], m["pad"], m["gap"] + 2)
        dock_lay.setSpacing(max(2, m["gap"] - 2))

        self._status_label = QLabel("")
        theme.named(self._status_label, "statusLabel")
        self._status_label.setVisible(False)
        dock_lay.addWidget(self._status_label)

        # ── 审批快捷条（v0.8.5）──
        # 审批卡的按钮长在卡片底部。卡片比视图高的时候（一个 200 行的 diff
        # 就够），「批准 / 拒绝」会落到折叠线以下——用户看到「等待批准」，
        # 却找不到按钮，得先把对话流滚下去。这条固定在输入框上方，挂起期间
        # 才出现，滚到哪里都在。
        self._approval_bar = theme.card("approvalBar")
        bar_lay = QHBoxLayout(self._approval_bar)
        bar_lay.setContentsMargins(m["pad"], 3, m["pad"], 3)
        bar_lay.setSpacing(6)
        self._approval_bar_text = QLabel("")
        theme.named(self._approval_bar_text, "approvalBarText")
        self._approval_bar_text.setSizePolicy(
            QSizePolicy.Ignored, QSizePolicy.Preferred)
        self._approval_bar_text.setMinimumWidth(0)
        bar_lay.addWidget(self._approval_bar_text, 1)
        self._bar_locate = theme.push_button("查看", "ghost")
        self._bar_locate.setToolTip("滚到审批卡片，逐项查看差异")
        self._bar_locate.clicked.connect(self._reveal_pending_card)
        bar_lay.addWidget(self._bar_locate)
        self._bar_reject = theme.push_button("全部拒绝", "danger")
        self._bar_reject.clicked.connect(self._bar_reject_all)
        bar_lay.addWidget(self._bar_reject)
        self._bar_approve = theme.push_button("全部批准", "primary")
        self._bar_approve.clicked.connect(self._bar_approve_all)
        bar_lay.addWidget(self._bar_approve)
        self._approval_bar.setVisible(False)
        dock_lay.addWidget(self._approval_bar)

        self._thinking_bar = QProgressBar()
        theme.named(self._thinking_bar, "thinkingBar")
        self._thinking_bar.setRange(0, 0)
        self._thinking_bar.setTextVisible(False)
        self._thinking_bar.setFixedHeight(2)
        self._thinking_bar.setVisible(False)
        dock_lay.addWidget(self._thinking_bar)

        # 统一容器：输入框、附件、模式/模型/权限控制、发送按钮都在同一张卡片里
        composer = theme.card("composer")
        theme.set_prop(composer, "focused", "false")
        c_lay = QVBoxLayout(composer)
        c_lay.setContentsMargins(
            m["pad"] + 2, m["gap"] + 1, m["pad"] + 2, m["gap"] + 1)
        c_lay.setSpacing(max(3, m["gap"] - 2))

        self._ctx_bar = QWidget()
        self._ctx_bar.setVisible(False)
        ctx_lay = QHBoxLayout(self._ctx_bar)
        ctx_lay.setContentsMargins(0, 0, 0, 0)
        ctx_lay.setSpacing(4)
        self._ctx_label = theme.chip("")
        ctx_lay.addWidget(self._ctx_label)
        clear_ctx = theme.icon_button("close", "清除上下文", 20)
        clear_ctx.clicked.connect(self._clear_context)
        ctx_lay.addWidget(clear_ctx)
        ctx_lay.addStretch()
        c_lay.addWidget(self._ctx_bar)

        # ── v0.7.1：当前光标 / 选区（对标 Cline 的 editor context） ──
        self._editor_bar = QWidget()
        self._editor_bar.setVisible(False)
        e_lay = QHBoxLayout(self._editor_bar)
        e_lay.setContentsMargins(0, 0, 0, 0)
        e_lay.setSpacing(4)
        self._editor_chip = ClickChip()
        theme.named(self._editor_chip, "editorChip")
        self._editor_chip.setSizePolicy(
            QSizePolicy.Maximum, QSizePolicy.Preferred)
        self._editor_chip.setMinimumWidth(0)
        self._editor_chip.clicked.connect(self._popup_editor_menu)
        e_lay.addWidget(self._editor_chip)
        e_lay.addStretch()
        c_lay.addWidget(self._editor_bar)

        # ── 图片附件条：粘贴 / 拖入 / 选文件后的缩略图就在这儿 ──
        self._attach_bar = QWidget()
        self._attach_bar.setVisible(False)
        a_lay = QHBoxLayout(self._attach_bar)
        a_lay.setContentsMargins(0, 0, 0, 0)
        a_lay.setSpacing(4)
        a_lay.addStretch()
        self._attach_lay = a_lay
        c_lay.addWidget(self._attach_bar)

        self._editor_menu = QMenu(self)
        theme.named(self._editor_menu, "overflowMenu")
        self._editor_actions = []
        for label, mode, tip in (
                ("跟随光标（选区优先）", "auto", "选中就带选区，没选中就带光标上下 3 行"),
                ("仅选中内容", "selection", "只带选中的那几行"),
                ("整个函数 / 类", "function", "按缩进向上找定义、向下找块尾"),
                ("整份文件", "file", "最多 400 行，超出部分截断"),
                ("不附加", "off", "关闭编辑器上下文，只发你输入的文字")):
            act = self._editor_menu.addAction(label)
            act.setCheckable(True)
            act.setToolTip(tip)
            act.triggered.connect(
                lambda _checked=False, m=mode: self._set_editor_mode(m))
            self._editor_actions.append((act, mode))

        self._input = ChatInput()
        self._input.send_requested.connect(self._send)
        self._input.textChanged.connect(self._on_input_changed)
        self._input.image_pasted.connect(self._on_image_pasted)
        self._input.files_dropped.connect(self._on_image_files)
        c_lay.addWidget(self._input)

        controls = QWidget()
        theme.named(controls, "composerControls")
        ctl = QHBoxLayout(controls)
        ctl.setContentsMargins(0, 0, 0, 0)
        ctl.setSpacing(6)
        self._attach_btn = theme.icon_button(
            "image", "添加图片（截图可直接 Ctrl+V 粘贴，或拖进来）", 26)
        self._attach_btn.clicked.connect(self._pick_images)
        ctl.addWidget(self._attach_btn)
        ctl.addWidget(self._mode_btn)
        ctl.addWidget(self._agent_btn)
        self._policy_chip = theme.chip("按风险审批")
        self._policy_chip.setSizePolicy(
            QSizePolicy.Maximum, QSizePolicy.Preferred)
        ctl.addWidget(self._policy_chip)
        ctl.addStretch()

        self._cancel_btn = theme.push_button("取消", "danger")
        self._cancel_btn.setIcon(theme.make_icon("stop", t["danger"]))
        self._cancel_btn.clicked.connect(self._cancel_request)
        self._cancel_btn.setVisible(False)
        ctl.addWidget(self._cancel_btn)

        self._send_btn = theme.push_button("发送", "primary")
        theme.named(self._send_btn, "sendButton")
        self._send_btn.setIcon(theme.make_icon("arrow_up", t["on_accent"], 16))
        self._send_btn.setIconSize(QSize(15, 15))
        self._send_btn.clicked.connect(self._send)
        ctl.addWidget(self._send_btn)
        c_lay.addWidget(controls)
        dock_lay.addWidget(composer)

        # 状态条：上下文占用 / token / 费用 / 可恢复任务
        meter = QWidget()
        theme.named(meter, "composerMeter")
        meter_lay = QHBoxLayout(meter)
        meter_lay.setContentsMargins(2, 0, 6, 0)
        meter_lay.setSpacing(7)
        self._usage_label = QLabel("")
        theme.named(self._usage_label, "usageLabel")
        # QLabel 的最小宽度默认等于整段文字宽度，16px 字号下这一段有 600px，
        # 会把整个面板顶到 800px 宽、在 Spyder 侧栏里直接溢出。
        self._usage_label.setSizePolicy(
            QSizePolicy.Ignored, QSizePolicy.Preferred)
        self._usage_label.setMinimumWidth(0)
        self._usage_text = ""
        meter_lay.addWidget(self._usage_label, 1)
        self._ctx_progress = QProgressBar()
        theme.named(self._ctx_progress, "ctxMeter")
        self._ctx_progress.setRange(0, 100)
        self._ctx_progress.setTextVisible(False)
        self._ctx_progress.setFixedWidth(76)
        self._ctx_progress.setFixedHeight(5)
        meter_lay.addWidget(self._ctx_progress)
        self._resume_btn = theme.push_button("恢复任务", "subtle")
        theme.named(self._resume_btn, "linkButton")
        self._resume_btn.setCursor(Qt.PointingHandCursor)
        self._resume_btn.setToolTip("载入上次中断的任务并继续")
        self._resume_btn.clicked.connect(self._resume_task)
        self._resume_btn.setVisible(False)
        meter_lay.addWidget(self._resume_btn)
        dock_lay.addWidget(meter)
        layout.addWidget(dock)

    def _provider_text(self):
        p = self._config.get("provider", "deepseek")
        label = self._config.get("label", "未配置")
        m = self._current_model or self._config.get("model", "")
        if self._config.get("mode_models_enabled", False):
            return f"{label}  ·  {m}  ·  {self._mode.upper()}"
        return f"{label}  ·  {m}"

    def _popup_overflow_menu(self):
        self._menu.popup(self._more_btn.mapToGlobal(
            QPoint(self._more_btn.width() - self._menu.sizeHint().width(),
                   self._more_btn.height() + 4)))

    def _popup_mode_menu(self):
        self._mode_menu.popup(self._mode_btn.mapToGlobal(
            QPoint(0, self._mode_btn.height() + 4)))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._sync_header()
        self._sync_editor_chip()
        self._place_rail()
        # 面板变矮时，待决卡片要重新分空间；否则放大窗口后留下的差异区
        # 高度会把按钮重新顶出去。
        if getattr(self, "_pending_card", None) is not None:
            QTimer.singleShot(0, self._fit_approval_card)

    def _sync_header(self):
        """顶栏自适应。

        侧边栏宽度在 Spyder 里跨度很大（300 ~ 1200px）。模型胶囊必须
        "按内容宽度"排布并随宽度省略，而不是吃掉所有剩余空间——旧版
        把一个 Expanding 标签塞进顶栏，宽面板下它被拉成一根长条，看起来
        像一根多余的色块。
        """
        label = getattr(self, "_provider_label", None)
        if label is None:
            return
        try:
            full = self._provider_text()
        except Exception:
            return
        w = self.width()
        label.setToolTip(full)
        if w < 260:
            label.setVisible(False)
            return
        # 窄面板：把权限徽章、标题这些"可省略信息"先撤掉，保证输入区不被挤爆
        policy_chip = getattr(self, "_policy_chip", None)
        if policy_chip is not None:
            policy_chip.setVisible(w >= 400)
        # 窄面板下把上下文进度条让给文字，否则状态行会被挤成半截
        bar = getattr(self, "_ctx_progress", None)
        if bar is not None:
            bar.setVisible(w >= 470)
        agent_pill = getattr(self, "_agent_btn", None)
        if agent_pill is not None:
            # 胶囊的宽度按"当前 Agent 名字的实际长度"给：硬压到 120px 时
            # "Rubber Duck ▾" 会变成 "Rubb…ck"——又是同一种"读不全"。
            # 面板窄到连 330px 都没有时才回落到固定上限让 Qt 省略。
            want = agent_pill.sizeHint().width()
            agent_pill.setMaximumWidth(want if w >= 330 else min(want, 96))
        label.setVisible(True)
        fm = label.fontMetrics()
        # 0.7.8：原来上限 300px，在 900px 宽的面板里也会把 "deepseek-flash"
        # 砍成 "deepseek-flas"。改成按实际剩余空间算，宽面板下不再截断。
        limit = max(96, min(360, w - 150))
        text = fm.elidedText(full, Qt.ElideMiddle, limit)
        if text != label.text():
            label.setText(text)
        self._sync_meter()
        self._sync_column(w)

    def _sync_column(self, width):
        """宽面板下把对话列收窄到可读行宽（≈860px）并居中。

        否则在 1000px 宽的侧栏里，正文一行会横跨整个面板，长句很难读，
        也会让"对话"看起来像一块没有边界的画布。
        """
        lay = getattr(self, "_inner_lay", None)
        if lay is None:
            return
        # 兜底：面板从窄变宽时，QScrollArea 偶尔不重排内容区，导致
        # 欢迎卡停留在旧宽度上（实拍里卡片只有 350px，右边一大片空白）。
        scroll = getattr(self, "_scroll", None)
        if scroll is not None and self._inner.width() != scroll.viewport().width():
            self._inner.resize(scroll.viewport().width(), self._inner.height())
        pad = self._theme_metrics_pad()
        side = max(pad, (width - 860) // 2)
        end = lay.contentsMargins()
        if abs(end.left() - side) > 1:
            lay.setContentsMargins(side, end.top(), side, end.bottom())

    def _theme_metrics_pad(self):
        try:
            return int(self._inner_lay.contentsMargins().top())
        except Exception:
            return 12

    def _sync_meter(self):
        """用量行按可用宽度省略——它是最容易被字号撑爆的一行。"""
        label = getattr(self, "_usage_label", None)
        if label is None:
            return
        full = getattr(self, "_usage_text", "") or ""
        width = label.width() if label.width() > 40 else max(
            80, self.width() - 220)
        elided = label.fontMetrics().elidedText(
            full, Qt.ElideRight, max(60, width - 2))
        if elided != label.text():
            label.setText(elided)
        label.setToolTip(full)

    # ── Cline 风格：Plan / Act ──────────────────────────────────────

    def _refresh_mode_button(self):
        """模式胶囊：颜色/文案/菜单勾选都由语义属性驱动。"""
        plan = self._mode == "plan"
        self._mode_btn.setText("Plan" if plan else "Act")
        self._mode_btn.set_icon("eye" if plan else "bolt")
        self._mode_btn.set_tone("plan" if plan else "act")
        self._mode_btn.setToolTip(
            "Plan：只读分析、讨论方案，不修改文件 / 不执行命令\n"
            "Act：可提出文件更改与命令，但每一步都需你批准\n"
            "点击展开菜单切换模式")
        if getattr(self, "_plan_action", None) is not None:
            self._plan_action.setChecked(plan)
            self._act_action.setChecked(not plan)

    def _set_mode(self, mode):
        """由模式菜单调用；与旧的单击切换行为保持同一套副作用。"""
        if mode not in ("plan", "act") or mode == self._mode:
            return
        self._mode = mode
        self._refresh_mode_button()
        self._sync_header()
        self._audit.log("mode_changed", mode=self._mode,
                        model=self._effective_model())
        self._set_status(
            "Plan 模式：AI 只能读取/搜索，不会修改文件或执行命令"
            if self._mode == "plan" else
            "Act 模式：写操作会先展示差异并等待批准")
        if self._current_bubble:
            self._current_bubble.append_block(
                f"*已切换到 **{self._mode.upper()}** 模式*")

    def _on_mode_clicked(self, _checked=False):
        """兼容旧调用：单击直接在 Plan / Act 之间切换。"""
        self._set_mode("act" if self._mode == "plan" else "plan")

    def _mode_system_prompt(self):
        if self._mode == "plan":
            return (
                "## 运行模式：PLAN（仅规划）\n\n"
                "当前处于 Plan 模式，**严禁**创建、修改、删除任何文件，"
                "也严禁运行任何命令。你只能读取、搜索、分析代码并与用户讨论方案。\n"
                "- 需要多步骤任务时，用 manage_todo_list 列出清晰的执行计划\n"
                "- 方案确认后，明确提示用户「切换到 Act 模式后我再执行」\n"
                "- 不要在 Plan 模式下声称文件已被修改\n"
            )
        return (
            "## 运行模式：ACT（执行）\n\n"
            "你提出的每一次文件写入与命令执行，都会先以差异（diff）形式"
            "展示给用户并等待逐项批准。\n"
            "- 用户可能只批准其中一部分操作；被拒绝的操作视为**未发生**，"
            "不要假定它已生效，也不要重复提交同一操作\n"
            "- 优先用 insert_edit / apply_patch 做小范围精确修改，避免整文件重写\n"
            "- 每次工具调用都应当有明确、单一的目的\n"
        )

    # ── Agent 选择 ──

    _DEFAULT_AGENT = {
        "name": "default",
        "display_name": "默认助手",
        "short_name": "默认",
        "description": "面板默认人格：不叠加任何预设提示词",
    }

    def _agent_entries(self):
        return [self._DEFAULT_AGENT] + list(get_builtin_agents())

    def _populate_agent_menu(self):
        """菜单里用全名（QMenu 按内容排版，不怕长），胶囊上才用短名。"""
        menu = getattr(self, "_agent_menu", None)
        if menu is None:
            return
        menu.clear()
        self._agent_actions = []
        active = self._active_agent or "default"
        for a in self._agent_entries():
            name = a["name"]
            full = a.get("display_name") or name
            desc = (a.get("description") or "").strip()
            act = menu.addAction(full)
            act.setCheckable(True)
            act.setChecked(name == active)
            act.setToolTip(f"{full}\n{desc}" if desc else full)
            act.triggered.connect(
                lambda _checked=False, n=name: self._select_agent(n))
            self._agent_actions.append((act, name))
        self._refresh_agent_pill()

    def _select_agent(self, name):
        """菜单项被点中：切换当前 Agent（'default' 表示不附加预设人格）。"""
        self._active_agent = None if name == "default" else name
        for act, n in getattr(self, "_agent_actions", []):
            act.setChecked(n == name)
        self._refresh_agent_pill()
        # 名字长度变了，胶囊的宽度上限跟着重算（否则换了长名还是会被省略）
        try:
            self._sync_header()
        except Exception:  # pragma: no cover - 面板尚未完成布局
            pass

    def _refresh_agent_pill(self):
        """胶囊文案 = 当前 Agent 的短名；完整名称与说明进 tooltip。"""
        btn = getattr(self, "_agent_btn", None)
        if btn is None:
            return
        active = self._active_agent or "default"
        entry = self._DEFAULT_AGENT
        for a in self._agent_entries():
            if a["name"] == active:
                entry = a
                break
        label = self._agent_short_label(entry)
        full = entry.get("display_name") or label
        desc = (entry.get("description") or "").strip()
        btn.setText(label)
        btn.setToolTip(
            f"{full}\n{desc}\n\n点击切换助手" if desc else f"{full}\n\n点击切换助手")
        self._align_composer_row()

    def _align_composer_row(self):
        """把 composer 这一行的控件拉成同一高度。

        旧版 Plan/Act 胶囊、Agent 选择器、权限徽章、发送按钮分别是 26 / 22 /
        32px，排在一起像没对齐（0.7.x 实拍里右边那颗按钮明显高一截）。
        高度直接由字号推：``ui_font + 12``，12px 字号 → 24px，18px 字号 →
        30px，任何字号下四件控件都是一条直线。
        """
        widgets = [
            w for w in (getattr(self, "_mode_btn", None),
                        getattr(self, "_agent_btn", None),
                        getattr(self, "_policy_chip", None))
            if w is not None
        ]
        if not widgets:
            return
        height = max(max(w.sizeHint().height() for w in widgets),
                     int(theme.current_theme().ui_font) + 12, 24)
        for w in widgets:
            w.setFixedHeight(height)
        for btn in (getattr(self, "_send_btn", None),
                    getattr(self, "_cancel_btn", None)):
            if btn is not None:
                btn.setFixedHeight(height)

    def _open_agent_menu(self, anchor=None):
        """把 Agent 菜单弹在胶囊上方——composer 贴的是面板底部。"""
        btn = getattr(self, "_agent_btn", None)
        menu = getattr(self, "_agent_menu", None)
        if btn is None or menu is None:
            return
        menu.ensurePolished()
        hint = menu.sizeHint()
        pos = (anchor or btn).mapToGlobal(QPoint(0, 0))
        menu.popup(QPoint(pos.x(), pos.y() - hint.height() - 4))

    @staticmethod
    def _agent_short_label(agent):
        """胶囊上的短名：优先取 short_name，否则去掉 " Agent" 后缀。

        内置 Agent 名称已中文化（探索 / 调研 / 审查 / 任务 / 规划 / 橡皮鸭），
        短名在定义里显式给出，不再靠后缀推断。
        """
        short = str(agent.get("short_name") or "").strip()
        if short:
            return short
        label = str(agent.get("display_name") or agent.get("name") or "").strip()
        for suffix in (" Agent", " agent"):
            if label.endswith(suffix):
                label = label[: -len(suffix)]
                break
        return label or "默认"

    def _get_active_agent_config(self):
        """获取当前激活的 Agent 配置"""
        if not self._active_agent:
            return None
        return get_agent_config(self._active_agent)

    def _on_participant_changed(self, name):
        """参与者变更回调"""
        self._active_participant = name

    def _get_participant(self):
        """获取当前参与者"""
        return self._pm.get(self._active_participant)

    def _add_welcome(self):
        """v0.6.0：结构化空状态卡片，不再用一整段 Markdown 当欢迎语。"""
        model = self._config.get("model", "") or "未配置"
        card = WelcomeCard(model, self._inner)
        card.suggestion.connect(self._use_suggestion)
        self._inner_lay.insertWidget(self._inner_lay.count() - 1, card)
        self._welcome = card
        self._scroll_to_bottom()

    def _use_suggestion(self, text):
        """点击起手式：填进输入框并聚焦，用户可改完再发送。"""
        self._input.setPlainText(text)
        self._input.setFocus()
        cursor = self._input.textCursor()
        cursor.movePosition(QTextCursor.End)
        self._input.setTextCursor(cursor)

    def _scroll_to_bottom(self):
        """合并滚动请求：流式期间不重复排定时器（v0.5.5 P0）。"""
        if self._scroll_pending:
            return
        self._scroll_pending = True
        QTimer.singleShot(10, self._do_scroll_to_bottom)

    def _do_scroll_to_bottom(self):
        """实际执行滚动的函数"""
        self._scroll_pending = False
        vsb = self._scroll.verticalScrollBar()
        if vsb:
            vsb.setValue(vsb.maximum())

    # ── 消息导航（v0.8.17）─────────────────────────────────────────

    def _place_rail(self):
        """把导航条贴在**视口**右缘（竖滚动条左边）。

        按 scroll.geometry() 贴的话会正好压在滚动条上：滚动条 9px、导航条
        14px，两者叠在一起就是"拖不动滚动条、也点不准刻度"。用视口坐标
        算，刻度落在内容右侧的留白里，两者互不打扰。
        """
        rail = getattr(self, "_rail", None)
        scroll = getattr(self, "_scroll", None)
        if rail is None or scroll is None:
            return
        if not self._config.get("message_nav_enabled", True):
            rail.setVisible(False)
            return
        try:
            vp = scroll.viewport()
            tl = vp.mapTo(scroll, QPoint(0, 0))
            w = vp.width()
            h = vp.height()
            base = scroll.mapTo(self, QPoint(0, 0))
        except RuntimeError:
            return
        top = base.y() + tl.y()
        right = base.x() + tl.x() + w
        rail.setGeometry(right - MessageRail.WIDTH - 2,
                         top + 10, MessageRail.WIDTH, max(80, h - 20))
        rail.raise_()

    def _refresh_rail(self):
        """重建刻度：位置取消息气泡在内容里的相对位置。

        真实位置只用来决定"当前高亮第几条"；刻度本身等距摆放，这样
        20 条消息就是 20 个可点目标，不会在长会话里叠成一坨。
        """
        rail = getattr(self, "_rail", None)
        if rail is None:
            return
        # 整个刷新包一层：这个函数跑在 QTimer 槽里，未捕获异常会让 Qt
        # 直接 abort（exit 127、连 traceback 都没有），导航条出问题不该
        # 把整个聊天面板带走。
        try:
            self._refresh_rail_inner(rail)
        except Exception:  # noqa: BLE001
            logger.debug("消息导航条刷新失败", exc_info=True)
            try:
                rail.set_ticks([], -1)
            except RuntimeError:
                pass

    def _refresh_rail_inner(self, rail):
        marks = [m for m in self._user_marks if self._alive(m.get("bubble"))]
        self._user_marks = marks
        # 设置里关掉导航条时彻底不显示；开启时至少有一条消息才露面
        # （空会话里挂一根"没有刻度"的竖线只会让人以为界面坏了）。
        if not marks or not self._config.get("message_nav_enabled", True):
            rail.set_ticks([], -1)
            return
        viewport = self._scroll.viewport()
        total = float(max(1, self._inner.height()))
        ticks = []
        for i, mark in enumerate(marks):
            bubble = mark["bubble"]
            try:
                top = bubble.mapTo(self._inner, QPoint(0, 0)).y()
            except RuntimeError:
                top = 0
            ticks.append((max(0.0, min(1.0, top / total)),
                          "%d. %s" % (i + 1, mark.get("text") or "（空消息）")))
        # 当前项 = 视口里最靠上的那条用户消息（它的底边还没滚出视口顶）
        current = len(marks) - 1
        for i, mark in enumerate(marks):
            try:
                bottom = mark["bubble"].mapTo(
                    viewport, QPoint(0, mark["bubble"].height())).y()
            except RuntimeError:
                continue
            if bottom >= 0:
                current = i
                break
        rail.set_ticks(ticks, current)
        self._place_rail()

    def _locate_user_message(self, index):
        """滚到第 index 条用户消息，并让它闪一下边框，视线好跟上。"""
        marks = [m for m in self._user_marks if self._alive(m.get("bubble"))]
        if not (0 <= index < len(marks)):
            return
        bubble = marks[index]["bubble"]
        try:
            vsb = self._scroll.verticalScrollBar()
            y = bubble.mapTo(self._inner, QPoint(0, 0)).y()
            # 目标位置让气泡落在视口上方 1/4 处，而不是贴顶——贴顶会让
            # 用户分不清"这是第一条"还是"已经滚到顶了"
            want = max(0, y - self._scroll.viewport().height() // 4)
            vsb.setValue(min(want, vsb.maximum()))
            if hasattr(bubble, "flash"):
                bubble.flash()
        except RuntimeError:
            return

    def _show_message_flyout(self, highlight=None):
        """在导航条左侧弹出完整清单（面板内浮层）。

        ``highlight`` 给定时预选中那一行——鼠标掠过刻度时，清单里对应
        的那条正好亮着，指哪儿看哪儿。
        """
        marks = [m for m in self._user_marks if self._alive(m.get("bubble"))]
        if not marks:
            return
        target = highlight if (highlight is not None
                               and 0 <= highlight < len(marks)) else -1
        # 鼠标掠过刻度时这个函数会被高频调用：条数没变就只挪高亮，
        # 不重建列表（重建会把列表的滚动位置重置回顶部）。
        if self._flyout.isVisible() and self._flyout_count == len(marks):
            self._flyout.set_current(target)
            self._flyout_highlight = target
            return
        self._flyout_count = len(marks)
        self._flyout_highlight = target
        if target >= 0:
            current = highlight
        else:
            current = -1
            try:
                viewport = self._scroll.viewport()
                for i, mark in enumerate(marks):
                    if mark["bubble"].mapTo(
                            viewport,
                            QPoint(0, mark["bubble"].height())).y() >= 0:
                        current = i
                        break
            except RuntimeError:
                current = -1
        items = [(i, mark.get("text") or "（空消息）")
                 for i, mark in enumerate(marks)]
        size = self._flyout.build(items, current)
        # 贴着导航条左缘、与它顶端对齐；面板放不下时往下收
        try:
            rg = self._rail.geometry()
            vg = self._scroll.geometry()
        except RuntimeError:
            return
        x = rg.left() - size.width() - 6
        y = max(vg.top() + 4, rg.top() - 4)
        h = min(size.height(), max(120, vg.height() - 10))
        self._flyout.setGeometry(max(4, x), y, size.width(), h)
        self._flyout.setVisible(True)
        self._flyout.raise_()

    def _on_rail_hovered(self, index):
        """鼠标掠过刻度时就展开清单，并把对应行选中。

        比"必须精准悬停 10px 竖条才弹"好得多：刻度本身就是导航条主体的
        视觉锚点，用户往那儿一动鼠标，意图已经很明确了。移出则收起。
        """
        if index < 0:
            if not self._over_flyout(QCursor.pos()):
                self._hide_message_flyout()
            return
        self._show_message_flyout(highlight=index)

    def _hide_message_flyout(self):
        flyout = getattr(self, "_flyout", None)
        if flyout is not None and flyout.isVisible():
            flyout.setVisible(False)
        # 收起后下次必须重建，否则"同样 5 条、同样高亮"会被当成无需刷新
        self._flyout_count = -1
        self._flyout_highlight = -2

    @staticmethod
    def _alive(widget):
        """控件还没被 deleteLater 收走吗。

        Qt 删除是排队的，``_clear_ui()`` 之后指针仍在但 C++ 对象已失效，
        直接摸它抛 RuntimeError。这里做一次廉价探活，把导航条从这类
        半死状态里摘出来。
        """
        if widget is None:
            return False
        try:
            widget.isVisible()
        except RuntimeError:
            return False
        return True

    def _over_flyout(self, global_pos):
        flyout = getattr(self, "_flyout", None)
        if flyout is None or not flyout.isVisible():
            return False
        try:
            return flyout.rect().contains(flyout.mapFromGlobal(global_pos))
        except RuntimeError:
            return False

    def _maybe_hide_flyout(self, global_pos):
        """鼠标离开「右缘 + 清单」这一整块时才收起。

        清单和触发区是两块区域：只按视口坐标判断的话，鼠标往清单上一移
        就被当成"离开"了，清单会在指针底下闪掉。
        """
        if not self._flyout.isVisible():
            return
        if self._over_flyout(global_pos):
            return
        x = self._scroll.viewport().mapFromGlobal(global_pos).x()
        if x < self._scroll.viewport().width() - 70:
            self._hide_message_flyout()

    def eventFilter(self, obj, event):  # noqa: N802 - Qt 接口
        """对话流右缘也能唤出清单，但主要入口是导航条本身。

        导航条会盖住视口右侧 14px，鼠标在那儿时事件由导航条接管；
        这里只兜底"贴着右缘但没命中刻度"的移动，并且要求面板够宽——
        窄侧栏里正文本身就贴着右缘，随便动一下鼠标就弹清单会很吵。
        """
        try:
            if obj is getattr(self, "_scroll", None):
                # 审批条出现 / 用量条换行都会改滚动区几何，而面板尺寸没变，
                # 不会走 resizeEvent——导航条就得靠这条跟上。
                if event.type() == QEvent.Resize:
                    self._place_rail()
                return super().eventFilter(obj, event)
            if obj is getattr(self, "_scroll", None).viewport():
                etype = event.type()
                if etype == QEvent.Enter:
                    self._rail_timer.start()
                elif etype == QEvent.MouseMove:
                    gp = event.globalPos()
                    if self._over_flyout(gp):
                        return super().eventFilter(obj, event)
                    x = event.pos().x()
                    if (self.width() >= 420
                            and x >= self._scroll.viewport().width() - 40
                            and len(self._user_marks) >= 2):
                        if not self._flyout.isVisible():
                            self._show_message_flyout()
                    else:
                        self._maybe_hide_flyout(gp)
                elif etype in (QEvent.Leave, QEvent.Wheel):
                    self._rail_timer.start()
                    if etype == QEvent.Leave:
                        self._hide_message_flyout()
        except (RuntimeError, AttributeError):
            pass
        return super().eventFilter(obj, event)

    def _set_status(self, text):
        """设置 AI 状态提示（输入框上方）"""
        if text:
            self._status_label.setText(text)
            self._status_label.setVisible(True)
        else:
            self._status_label.setVisible(False)

    # ── 审批快捷条（v0.8.5）────────────────────────────────────────

    def _set_awaiting_approval(self, flag):
        """挂起状态与快捷条的唯一开关入口。

        以前这个布尔量在五处被直接赋值，快捷条一加就会漏掉某条退出路径
        （取消、转向、新会话、面板卸载），结果卡片已经作废、条还挂着。
        """
        self._awaiting_approval = bool(flag)
        bar = getattr(self, "_approval_bar", None)
        if bar is None:
            return
        if not flag:
            bar.setVisible(False)
            return
        self._refresh_approval_bar()

    def _refresh_approval_bar(self):
        """按剩余待决项刷新快捷条；没有待决项就自己消失。"""
        bar = getattr(self, "_approval_bar", None)
        card = self._pending_card
        if bar is None or not self._awaiting_approval or card is None:
            if bar is not None:
                bar.setVisible(False)
            return
        pending, danger = card.pending_stats()
        if pending <= 0:
            bar.setVisible(False)
            return
        safe = pending - danger
        if danger and safe:
            text = f"⏸ 待批准 {pending} 项 · 其中 {danger} 项危险操作需逐项确认"
        elif danger:
            text = f"⚠ 还有 {danger} 项危险操作：看差异后逐项批准"
        else:
            text = f"⏸ 待批准 {pending} 项操作"
        self._approval_bar_text.setText(text)
        theme.set_prop(self._approval_bar_text,
                       "state", "danger" if danger else "pending")
        self._bar_locate.setText("查看差异" if danger and not safe else "查看")
        self._bar_approve.setVisible(safe > 0)
        self._bar_approve.setText(
            "全部批准" if safe == pending else f"批准 {safe} 项")
        bar.setVisible(True)

    def _bar_approve_all(self):
        card = self._pending_card
        if card is None:
            return
        card.approve_safe()

    def _bar_reject_all(self):
        card = self._pending_card
        if card is None:
            return
        card.reject_all()

    def _fit_approval_card(self):
        """把待决卡片压进当前视图高度，让卡内按钮也在视野内。"""
        card = self._pending_card
        if card is None:
            return
        vp = self._scroll.viewport()
        height = vp.height()
        if height <= 40:
            return
        pad = theme.current_theme().metrics["pad"]
        # 留 6px 余量：按钮贴着视图下沿时，差 1~2px 就会被裁掉一条边，
        # 看着像"按钮坏了"，点起来也不稳。
        card.fit_to_height(max(120, height - 6),
                           max(180, vp.width() - 2 * pad))

    def _settle_approval_card(self):
        """布局稳定后重算一次：适配高度 + 滚到待决操作。"""
        self._fit_approval_card()
        self._reveal_pending_card()

    def _reveal_pending_card(self):
        """把待决操作滚进视野，优先第一条「按钮还没露全」的。

        固定滚到卡片顶部是不够的：卡片比视图高的时，第一条的按钮看得见、
        第二条的看不见，点「查看」画面不动，看起来像按钮坏了。

        这里直接算滚动条数值，不用 ``ensureWidgetVisible``——后者依赖控件
        几何已经生效，审批卡刚插进布局时几何还是旧的，调用会静默什么都不
        做（v0.8.5 实拍：卡片在 261px 处、视图 679px，滚条仍停在 0）。
        """
        card = self._pending_card
        if card is None:
            return
        vp = self._scroll.viewport()
        sb = self._scroll.verticalScrollBar()
        top, bottom = sb.value(), sb.value() + vp.height()
        target = None
        for row in card.rows():
            if not row.is_pending():
                continue
            y = row.mapTo(self._inner, QPoint(0, 0)).y()
            if y < top or y + row.height() > bottom - 8:
                target = row
                break
        widget = target or card
        y = widget.mapTo(self._inner, QPoint(0, 0)).y()
        height = widget.height()
        if height > vp.height() - 16:
            # 比视图还高：保证按钮那一端（行底部）在视野里
            value = y + height - vp.height() + 12
        else:
            value = y - 8
        sb.setValue(int(max(0, min(sb.maximum(), value))))

    def _add_msg(self, role, content):
        b = MessageBubble(role, content)
        b.copy_requested.connect(self._on_copy_content)
        b.insert_requested.connect(self._on_insert_content)
        b.retry_requested.connect(self._on_retry_content)
        self._inner_lay.insertWidget(self._inner_lay.count() - 1, b)
        # 首条消息进来后欢迎卡就该让位：旧实现只在 _clear_ui 里"保留 index 0"，
        # 于是整段对话上方永远顶着一张空状态卡片。
        welcome = getattr(self, "_welcome", None)
        if role == "user" and welcome is not None:
            welcome.setVisible(False)
        # v0.8.17：用户消息要进右侧导航索引。存的是预览文本而不是原文，
        # 免得长文把提示气泡撑爆；位置每次刷新时现算。
        if role == "user":
            marks = getattr(self, "_user_marks", None)
            if marks is not None:
                marks.append({"bubble": b,
                              "text": nav_preview(content)})
                QTimer.singleShot(0, self._refresh_rail)
        self._scroll_to_bottom()
        return b

    def _on_copy_content(self, role, content):
        """复制消息内容到剪贴板"""
        import html
        # 尝试提取代码块内容优先
        code_blocks = re.findall(r'```(?:\w+)?\n(.*?)```', content, re.DOTALL)
        if code_blocks:
            text_to_copy = "\n\n".join(code_blocks)
        else:
            text_to_copy = content
        clipboard = QApplication.clipboard()
        clipboard.setText(text_to_copy)

    def _on_insert_content(self, content):
        """将内容插入到编辑器中"""
        # 提取第一个代码块
        code_blocks = re.findall(r'```(?:\w+)?\n(.*?)```', content, re.DOTALL)
        code = code_blocks[0] if code_blocks else content
        try:
            w = self.window()
            if w and hasattr(w, 'get_plugin'):
                editor = w.get_plugin('editor')
                if editor and hasattr(editor, 'get_current_editor'):
                    ed = editor.get_current_editor()
                    if ed:
                        cursor = ed.get_cursor()
                        ed.insert_text(cursor, code)
                        return
        except Exception as e:
            logger.debug("Insert to editor failed: %s", e)

    def _on_retry_content(self, content):
        """重新生成回应"""
        # 找到最后一条用户消息
        last_user_msg = None
        last_user_idx = -1
        for i, m in enumerate(self._messages):
            if m["role"] == "user":
                last_user_msg = m
                last_user_idx = i

        if last_user_msg is None:
            return

        # 删除该用户消息之后的所有消息
        self._messages = self._messages[:last_user_idx + 1]

        # 清空界面中的气泡
        self._clear_ui()

        # 重建消息气泡
        for m in self._messages:
            content = m.get("content")
            text = attachments.message_text(content)
            images = attachments.message_images(content)
            if m.get("role") == "user" and images:
                bubble = self._add_msg("user", text)
                bubble.set_attachments(images)
            else:
                self._add_msg(m.get("role", "assistant"), text)

        # 重新发送最后一条用户消息（图片跟着回去，否则重试就丢图了）
        last_content = last_user_msg.get("content")
        self._attachments = [
            {"path": p, "name": os.path.basename(p)}
            for p in attachments.message_images(last_content)]
        self._refresh_attach_bar()
        self._input.setPlainText(attachments.message_text(last_content))
        self._send()

    def _clear_ui(self):
        """清空界面上的所有气泡（含欢迎卡，由调用方决定是否重建）。"""
        # 先停表再删控件：deleteLater 是延后执行的，中间这几帧定时器
        # 还在跑，会对着要销毁的控件刷新文案。
        ProcessPane.finish_all()
        while self._inner_lay.count() > 1:
            i = self._inner_lay.takeAt(0)
            if i.widget():
                i.widget().deleteLater()
        self._welcome = None
        self._current_bubble = None
        # 导航索引里的气泡这时已经被 takeAt 摘走，留着指针只会在下一次
        # _refresh_rail 里摸到半死的 C++ 对象。
        self._user_marks = []
        rail = getattr(self, "_rail", None)
        if rail is not None:
            rail.set_ticks([], -1)
        flyout = getattr(self, "_flyout", None)
        if flyout is not None and flyout.isVisible():
            flyout.hide()
        # 审批卡也在这批被删掉的控件里：快捷条不能再指着一张不存在的卡，
        # 否则点一下就是 RuntimeError（v0.8.5）。
        self._set_awaiting_approval(False)
        self._pending_card = None

    # ── 斜杠命令 ──

    def _on_input_changed(self):
        text = self._input.toPlainText()
        cursor_pos = self._input.textCursor().position()
        text_before = text[:cursor_pos]

        # 检测 @ 参与者输入
        at_index = text_before.rfind("@")
        show_participant = False
        if at_index >= 0:
            char_before = text_before[at_index - 1] if at_index > 0 else ""
            if char_before in ("", " ", "\n", "\t"):
                partial = text_before[at_index + 1:]
                # 只在输入 @ 后没有空格时触发
                if " " not in partial:
                    self._show_participant_dropdown(partial)
                    show_participant = True

        if not show_participant and self._participant_dropdown:
            self._participant_dropdown.hide()

        # 原有的 / 命令检测
        if text == "/":
            self._show_cmd_dropdown()
        elif not text.startswith("/") and self._cmd_dropdown:
            self._cmd_dropdown.hide()

    # ── 图片附件（粘贴 / 拖入 / 选文件） ─────────────────────────
    def _pick_images(self):
        paths, _filter = QFileDialog.getOpenFileNames(
            self, "选择要发给模型的图片", "",
            "图片 (*.png *.jpg *.jpeg *.gif *.webp *.bmp);;所有文件 (*)")
        if paths:
            self._on_image_files(paths)

    def _on_image_pasted(self, image):
        """剪贴板里的截图：直接落盘成 PNG，再挂到附件条上。"""
        path, err = attachments.save_qimage(image, "clipboard.png")
        if err:
            QMessageBox.warning(self, "无法添加图片", err)
            return
        self._add_attachment(path, "剪贴板截图")

    def _on_image_files(self, paths):
        errors = []
        for src in paths:
            path, err = attachments.save_file(src)
            if err:
                errors.append("%s：%s" % (os.path.basename(str(src)), err))
                continue
            self._add_attachment(path, os.path.basename(str(src)))
        if errors:
            QMessageBox.warning(self, "有图片没能加上",
                                "\n".join(errors[:5]))

    def _add_attachment(self, path, name=""):
        if any(a["path"] == path for a in self._attachments):
            return                              # 同一张图贴两次就一张
        if len(self._attachments) >= attachments.MAX_IMAGES:
            QMessageBox.information(
                self, "图片数量已达上限",
                "一条消息最多带 %d 张图片。" % attachments.MAX_IMAGES)
            return
        self._attachments.append({"path": path,
                                  "name": name or os.path.basename(path)})
        self._refresh_attach_bar()

    def _remove_attachment(self, path):
        self._attachments = [a for a in self._attachments
                             if a["path"] != path]
        self._refresh_attach_bar()

    def _clear_attachments(self):
        self._attachments = []
        self._refresh_attach_bar()

    def _refresh_attach_bar(self):
        lay = self._attach_lay
        while lay.count() > 1:              # 末尾的 stretch 必须留着
            item = lay.takeAt(0)
            w = item.widget()
            if w is not None:
                w.setParent(None)
                w.deleteLater()
        for att in self._attachments:
            thumb = AttachThumb(att["path"], att["name"])
            thumb.removed.connect(self._remove_attachment)
            lay.insertWidget(lay.count() - 1, thumb)
        self._attach_bar.setVisible(bool(self._attachments))
        self._attach_btn.setToolTip(
            "添加图片（截图可直接 Ctrl+V 粘贴，或拖进来）"
            + ("　已附 %d 张" % len(self._attachments)
               if self._attachments else ""))

    def _content_with_images(self, prompt_text):
        """没有图时原样返回字符串；有图就升级成多模态 part 列表。

        保持字符串形态很重要：不带图的老消息、工具结果、system 提示全都
        是纯字符串，一旦无条件改成 list，``m["content"]`` 的所有使用点
        都得跟着改。
        """
        if not self._attachments:
            return prompt_text
        parts = []
        if (prompt_text or "").strip():
            parts.append({"type": "text", "text": prompt_text})
        for att in self._attachments:
            parts.append(attachments.part_for(att["path"], att["name"]))
        return parts

    def _show_cmd_dropdown(self):
        if self._cmd_dropdown is None:
            self._cmd_dropdown = CommandDropdown(self)
            self._cmd_dropdown.selected.connect(self._on_cmd_selected)
        cursor_rect = self._input.cursorRect()
        global_pos = self._input.mapToGlobal(cursor_rect.bottomLeft())
        self._cmd_dropdown.show_commands(BUILTIN_COMMANDS, global_pos)

    def _on_cmd_selected(self, name):
        self._cmd_dropdown.hide()
        # 处理 action 类命令
        for cmd in BUILTIN_COMMANDS:
            if cmd["name"] == name:
                if cmd.get("action") == "clear":
                    self._clear_chat()
                    return
                if cmd.get("action") == "history":
                    self._show_history()
                    return
                if cmd.get("action") == "diffdemo":
                    self._run_inline_diff_demo()
                    return
                prompt = cmd.get("prompt", "")
                if prompt:
                    # 获取编辑器选中文本
                    editor_text = self._get_editor_selection() or ""
                    if editor_text:
                        self._input.setPlainText(f"{prompt}\n\n```\n{editor_text}\n```")
                    else:
                        self._input.setPlainText(prompt + "\n")
                    self._send()
                return

    # ── @参与者选择 ──

    def _show_participant_dropdown(self, partial):
        """显示参与者自动补全下拉框"""
        if self._participant_dropdown is None:
            self._participant_dropdown = ParticipantDropdown(self)
            self._participant_dropdown.selected.connect(self._on_participant_selected)
        participants = self._pm.list_participants()
        matched = [p for p in participants if partial.lower() in p.name.lower()]
        if not matched:
            self._participant_dropdown.hide()
            return
        cursor_rect = self._input.cursorRect()
        global_pos = self._input.mapToGlobal(cursor_rect.bottomLeft())
        self._participant_dropdown.show_participants(matched, global_pos)

    def _on_participant_selected(self, name):
        """参与者选择回调"""
        self._participant_dropdown.hide()
        text = self._input.toPlainText()
        cursor = self._input.textCursor()
        pos = cursor.position()
        # 找到最后一个 @ 的位置
        at_pos = text.rfind("@", 0, pos)
        if at_pos >= 0:
            # 替换 @xxx 为 @name
            new_text = text[:at_pos] + f"@{name} " + text[pos:]
            self._input.setPlainText(new_text)
            # 移动光标到末尾
            c = self._input.textCursor()
            c.movePosition(c.MoveOperation.End)
            self._input.setTextCursor(c)

    def _get_editor_selection(self):
        """获取 Spyder 编辑器中选中的文本"""
        try:
            w = self.window()
            if w and hasattr(w, 'get_plugin'):
                editor = w.get_plugin('editor')
                if editor:
                    # Spyder 6 API: get_current_editor()
                    ed = editor.get_current_editor()
                    if ed:
                        # 尝试多种获取选中文本的方式
                        if hasattr(ed, 'get_selected_text'):
                            return ed.get_selected_text()
                        if hasattr(ed, 'getSelection'):
                            return ed.getSelection()
                        if hasattr(ed, 'selectedText'):
                            return ed.selectedText()
        except Exception:
            pass
        return ""

    def _get_editor_diagnostics(self, filepath=None):
        """获取编辑器中的诊断信息（错误/警告）"""
        try:
            w = self.window()
            if w and hasattr(w, 'get_plugin'):
                editor = w.get_plugin('editor')
                if editor:
                    ed = editor.get_current_editor()
                    if ed and hasattr(ed, 'get_diagnostics'):
                        return ed.get_diagnostics()
        except Exception:
            pass
        return []

    # ── 上下文管理 ──

    def _attach_context(self):
        """添加项目上下文"""
        try:
            from spyder.api.plugins import Plugins
            w = self.window()
            if not w or not hasattr(w, 'get_plugin'):
                return
            projects = w.get_plugin(Plugins.Projects)
            if not projects:
                QMessageBox.information(self, "提示", "请先打开一个项目")
                return
            root = projects.get_active_project_path()
            if not root:
                QMessageBox.information(self, "提示", "请先打开一个项目")
                return
            files = collect_project_files(root, max_files=15)
            if not files:
                QMessageBox.information(self, "提示", "项目中没有找到合适的文件")
                return
            self._context_files = files
            self._ctx_bar.setVisible(True)
            self._ctx_label.setText(f"{len(files)} 个文件 · 上下文")
        except Exception as e:
            logger.warning("Attach context failed: %s", e)

    def _clear_context(self):
        self._context_files = []
        self._ctx_bar.setVisible(False)

    # ── v0.7.1：编辑器上下文（光标 / 选区） ──────────────────────────

    def set_editor_context_provider(self, provider):
        """由插件注入：``provider(mode) -> EditorContext | None``。

        面板不 import Spyder 编辑器 API，避免在插件卸载 / 编辑器被替换时
        持有已销毁的 C++ 对象。
        """
        self._editor_ctx_fn = provider
        self.refresh_editor_context()

    def refresh_editor_context(self):
        """插件在光标停下后调用；250ms 单次防抖，避免拖动选区时反复解析。"""
        if self._editor_ctx_fn is None:
            return
        self._editor_timer.start()

    def _refresh_editor_chip(self):
        ctx = None
        if self._editor_ctx_fn is not None and self._editor_mode != "off":
            try:
                ctx = self._editor_ctx_fn(self._editor_mode)
            except Exception as exc:
                logger.debug("读取编辑器上下文失败: %s", exc)
        self._editor_ctx = ctx
        bar = getattr(self, "_editor_bar", None)
        if bar is None:
            return
        if ctx is None:
            bar.setVisible(False)
            return
        bar.setVisible(True)
        self._editor_chip_full = ctx.chip_text()
        self._sync_editor_chip()
        tip = [
            f"文件：{ctx.display_name()}",
            f"行：{ctx.location()} / 共 {ctx.total_lines} 行",
            f"语言：{ctx.language}",
        ]
        if ctx.has_selection:
            tip.append("内容：当前选区")
        elif ctx.mode == "file":
            tip.append("内容：整份文件")
        else:
            tip.append("内容：光标所在代码（上下各 3 行）")
        if ctx.truncated:
            tip.append("已按上限截断")
        if ctx.untitled:
            tip.append("该文件尚未保存，仅发送编辑器里的内容")
        tip.append("点击切换附加方式")
        self._editor_chip.setToolTip("\n".join(tip))

    def _sync_editor_chip(self):
        """按当前面板宽度省略胶囊文本（长路径不能把输入区顶宽）。"""
        chip = getattr(self, "_editor_chip", None)
        full = getattr(self, "_editor_chip_full", "")
        if chip is None or not full:
            return
        # 胶囊独占一行，可以用满面板宽度——只有超长路径才需要省略，
        # 而不是给一个固定小预算（大字号下 250px 连正常路径都放不下）
        budget = max(120, self.width() - 40)
        text = chip.fontMetrics().elidedText(full, Qt.ElideMiddle, budget)
        if text != chip.text():
            chip.setText(text)

    def _popup_editor_menu(self):
        menu = getattr(self, "_editor_menu", None)
        if menu is None:
            return
        for act, mode in self._editor_actions:
            act.setChecked(mode == self._editor_mode)
        chip = self._editor_chip
        menu.popup(chip.mapToGlobal(QPoint(0, chip.height() + 4)))

    def _set_editor_mode(self, mode):
        if mode not in ("auto", "selection", "function", "file", "off"):
            return
        if mode == self._editor_mode:
            return
        self._editor_mode = mode
        self._config["editor_context_mode"] = mode
        try:
            cfg = load_config()
            cfg["editor_context_mode"] = mode
            save_config(cfg)
        except Exception as exc:
            logger.debug("保存编辑器上下文模式失败: %s", exc)
        self._refresh_editor_chip()
        self._set_status(
            "编辑器上下文已关闭" if mode == "off"
            else f"编辑器附加方式：{dict((m, l) for l, m, _t in self._editor_actions)[mode]}")

    def _compose_user_prompt(self, text):
        """把编辑器上下文拼到用户消息前面（只进模型，不进气泡）。"""
        ctx = getattr(self, "_editor_ctx", None)
        if ctx is None or self._editor_mode == "off":
            return text
        try:
            block = ctx.prompt_block()
        except Exception as exc:
            logger.debug("编辑器上下文序列化失败: %s", exc)
            return text
        return f"{block}\n\n{text}" if block else text

    # ── 新增：代码审查 ──

    def _toggle_code_review(self):
        """切换代码审查面板的显示"""
        if self._code_review_panel is None:
            from .code_review_widget import CodeReviewPanel
            self._code_review_panel = CodeReviewPanel(self)
            theme.harmonize(self._code_review_panel)
            self._code_review_panel.set_base_dir(self._get_project_root())
            # 插入到 todo 面板和滚动区之间
            idx = self.layout().indexOf(self._scroll)
            if idx >= 0:
                self.layout().insertWidget(idx, self._code_review_panel)
            self._code_review_panel.refresh()
            self._review_action.setChecked(True)
        else:
            if self._code_review_panel.isVisible():
                self._code_review_panel.hide()
                self._review_action.setChecked(False)
            else:
                self._code_review_panel.set_base_dir(self._get_project_root())
                self._code_review_panel.refresh()
                self._code_review_panel.show()
                self._review_action.setChecked(True)

    # ── 新增：内联聊天 ──

    def _open_inline_chat(self):
        """打开内联聊天对话框"""
        try:
            # 获取当前编辑器
            editor = None
            w = self.window()
            if w and hasattr(w, 'get_plugin'):
                ed_plugin = w.get_plugin('editor')
                if ed_plugin and hasattr(ed_plugin, 'get_current_editor'):
                    editor = ed_plugin.get_current_editor()

            if editor:
                from .inline_chat import InlineChatDialog
                InlineChatDialog.open_inline_chat(editor, self)
            else:
                QMessageBox.information(self, "提示",
                    "请在编辑器中打开一个文件后使用内联聊天。\n\n"
                    "内联聊天可以直接针对选中的代码进行对话和修改。")
        except Exception as e:
            logger.debug("Inline chat failed: %s", e)

    # ── 聊天历史 ──

    def _show_history(self):
        """显示会话管理对话框"""
        dlg = SessionListDialog(self)
        theme.harmonize(dlg, standalone=True)
        dlg.session_selected.connect(self._load_history)
        dlg.session_deleted.connect(lambda cid: logger.debug("Session deleted: %s", cid))
        dlg.exec_()

    def _load_history(self, chat_id):
        data = load_chat(chat_id)
        if data:
            self._clear_chat()
            for m in data.get("messages", []):
                content = m.get("content")
                text = attachments.message_text(content)
                images = attachments.message_images(content)
                if m.get("role") == "user" and images:
                    bubble = self._add_msg("user", text)
                    bubble.set_attachments(images)
                else:
                    self._add_msg(m.get("role", "assistant"), text)
            self._messages = data.get("messages", [])

    # ── 设置 ──

    def _open_settings(self, tab=None):
        dlg = SettingsDialog(self)
        theme.named(dlg, "standaloneRoot")
        theme.style_popup(dlg)
        if tab:
            try:
                dlg.show_tab(tab)
            except Exception:
                pass
        dlg.config_changed.connect(self._on_config_changed)
        dlg.exec_()

    def _on_config_changed(self):
        self._config = get_provider_config()
        self._system_prompt = load_system_prompt()
        self._apply_font_size()
        # v0.6.0：外观设置（主题 / 强调色 / 密度 / 工具痕迹）即时生效
        self._apply_theme()
        self._sync_header()
        # v0.5.5：设置页改动立即作用于运行时
        self._usage.context_window = int(
            self._config.get("context_window", 1048576))
        self._usage.price_in = float(
            self._config.get("price_input_per_million", 0.0) or 0)
        self._usage.price_out = float(
            self._config.get("price_output_per_million", 0.0) or 0)
        self._chunk_timer.setInterval(
            max(10, int(self._config.get("stream_throttle_ms", 30))))
        self._audit = configure_audit(
            bool(self._config.get("audit_enabled", True)))
        self._current_model = self._effective_model()
        self._sync_header()
        if self._config.get("repo_index_enabled", True):
            QTimer.singleShot(200, self._warm_repo_index)
        self._refresh_usage_ui()
        # v0.8.17：设置里关掉导航条要立刻收起来，不用等下次发消息
        rail = getattr(self, "_rail", None)
        if rail is not None:
            if not self._config.get("message_nav_enabled", True):
                rail.setVisible(False)
                self._hide_message_flyout()
            else:
                self._refresh_rail()

    # ── v0.5.6 P1：审计查看器 / 仓库索引 ──────────────────────────────

    def _warm_repo_index(self):
        """启动后或配置变更后后台生成索引；主线程不做任何遍历。"""
        if self._index_warming or not self._config.get(
                "repo_index_enabled", True):
            return
        self._index_warming = True
        ensure_index_async(self._get_project_root(),
                           self._on_index_ready, self)

    def _on_index_ready(self, payload):
        self._index_warming = False
        root = self._get_project_root()
        files = (payload or {}).get("files", []) if isinstance(payload, dict) else []
        self._index_btn.setToolTip(
            "仓库结构索引：" + index_summary(root) + "（点击重建）")
        self._audit.log("repo_index", root=root, files=len(files),
                        truncated=bool((payload or {}).get("truncated"))
                        if isinstance(payload, dict) else False)
        if getattr(self, "_index_show_status", False):
            self._index_show_status = False
            self._set_status(f"🗺 仓库索引已更新（{len(files)} 个文件）")
            QTimer.singleShot(2500, lambda: self._set_status(""))

    def _rebuild_repo_index(self):
        root = self._get_project_root()
        invalidate(root)
        self._index_show_status = True
        self._set_status("🗺 正在后台重建仓库结构索引…")
        self._index_warming = True
        ensure_index_async(root, self._on_index_ready, self)

    def _open_audit_viewer(self):
        dlg = AuditViewerDialog(self)
        theme.harmonize(dlg, standalone=True)
        dlg.exec_()

    # ── 清空 ──

    def _clear_chat(self):
        # v0.5.5：新对话要同时终止旧任务并清除可恢复状态
        if self._worker is not None:
            try:
                self._worker.cancel()
            except Exception:
                pass
        self._cancel_event.set()
        self._flush_chunks()
        self._save_task_state("idle")
        self._resume_btn.setVisible(False)
        self._busy = False
        # 保存当前对话（后台写盘）
        if self._messages:
            snapshot = list(self._messages)
            submit(lambda: save_chat(snapshot), lambda _p: None, self)
        self._clear_ui()
        self._messages = []
        self._current_bubble = None
        try:
            self._clear_attachments()
        except AttributeError:      # UI 还没建好（构造期调用）
            self._attachments = []
        self._set_awaiting_approval(False)
        self._pending_card = None
        self._last_card = None
        # 清除 Todo 列表
        TodoManager().clear()
        self._add_welcome()

    # ── 发送消息 ──

    def _send(self):
        text = self._input.toPlainText().strip()
        # 只发图不写字也是合法的一条消息
        if not text and not self._attachments:
            return

        # v0.5.5 P0：运行中输入新指令 = 中断当前任务并就地转向
        if (self._busy or self._awaiting_approval
                or (self._worker is not None and self._worker.isRunning())):
            return self._redirect(text)

        # /diffdemo：不发给模型，直接在编辑器里演示一次内联 diff。
        # 用户反复遇到「改了文件但编辑器里没有红绿」时，这条命令能
        # 一步分清是「渲染没生效」还是「触发链路没走到」。
        if text.split()[0].lower() == "/diffdemo":
            self._input.clear()
            self._run_inline_diff_demo()
            return

        config = get_provider_config()
        self._config = config
        # v0.5.5：即便设置对话框未发出信号，发送时也同步最新运行时参数
        self._usage.context_window = int(config.get("context_window", 1048576))
        self._usage.price_in = float(
            config.get("price_input_per_million", 0.0) or 0)
        self._usage.price_out = float(
            config.get("price_output_per_million", 0.0) or 0)
        self._chunk_timer.setInterval(
            max(10, int(config.get("stream_throttle_ms", 30))))
        api_key = config.get("api_key", "")
        provider = config["provider"]

        # ── 配置检查：不在 GUI 线程做任何网络请求 ──
        reason = config.get("unsupported_reason", "")
        if reason:
            QMessageBox.warning(self, "暂不支持", reason)
            return
        if provider == "copilot":
            # 只读本地 token 文件（轻量），真正的刷新在 ChatWorker 线程里做
            try:
                github_token = load_tokens()[0]
            except Exception:
                github_token = None
            if not github_token:
                QMessageBox.warning(
                    self, "未登录",
                    "请先在设置（⚙）中登录 GitHub Copilot。")
                return
            if not config.get("model"):
                config["model"] = get_copilot_default_model()
                self._config = config
        elif config.get("needs_api_key") and not api_key:
            QMessageBox.warning(self, "缺少 API Key",
                f"请在设置中配置 {config.get('label', '')} 的 API Key")
            return
        elif provider == "custom" and not config.get("api_url"):
            QMessageBox.warning(self, "未配置 API URL",
                "「自定义（OpenAI 兼容）」需要填写完整的 API URL。")
            return

        # ── 解析 @participant 前缀 ──
        participant_name, cleaned_text = parse_participant(text)
        if participant_name:
            text = cleaned_text
            participant = self._pm.get(participant_name)
            if not participant:
                QMessageBox.warning(self, "未知参与者",
                    f"未知的参与者 @{participant_name}")
                return
            self._active_participant = participant_name
        else:
            participant = self._get_participant()

        if not text:
            return

        self._input.clear()
        # v0.5.5 P0：运行期间输入框保持可用，允许随时输入新指令转向
        self._input.setEnabled(True)
        self._send_btn.setEnabled(True)
        self._send_btn.setText("转向")
        self._usage.reset_turn()
        self._verify_attempts = 0
        self._task_started_at = time.time()

        # 添加上下文文件
        if self._context_files:
            ctx_text = "\n\n项目上下文文件:\n"
            for f in self._context_files:
                ctx_text += format_file_for_context(f)
            text += ctx_text

        # 添加用户消息（气泡只显示原文，编辑器上下文只进模型消息）
        # 发送瞬间再取一次：防止"刚选完文字就回车"用到上一帧的上下文
        self._refresh_editor_chip()
        prompt_text = self._compose_user_prompt(text)
        content = self._content_with_images(prompt_text)
        image_paths = [a["path"] for a in self._attachments]
        bubble = self._add_msg("user", text)
        if image_paths:
            bubble.set_attachments(image_paths)
        self._messages.append({"role": "user", "content": content})
        self._clear_attachments()
        self._current_bubble = self._add_msg("assistant", "")
        if image_paths:
            hint = attachments.vision_hint(self._effective_model())
            if hint:
                self._current_bubble.append_block(hint)

        # 记录参与者信息
        participant_info = participant.to_dict() if participant else {}
        self._messages.append({
            "role": "system",
            "content": f"[参与者: {participant_info.get('display_name', '@agent')}]"
        })

        # 启动 AI 请求
        self._tool_round = 0
        self._total_tool_rounds = 0
        self._auto_continues = 0
        self._soft_warned = False
        self._repeat_warned = False
        self._stall_rounds = 0
        self._pending_notes = []
        self._tool_call_history = []
        self._audit.log(
            "task_start", mode=self._mode,
            model=self._effective_model(), provider=provider,
            text_chars=len(text), approval_policy=self._approval_policy())
        self._start_worker(participant=participant)

    def _cancel_request(self):
        """v0.5.5：取消当前任务并保留可恢复的任务快照。"""
        self._audit.log("cancelled", round=self._tool_round)
        if self._worker is not None:
            self._worker.cancel()
        self._cancel_event.set()
        self._flush_chunks()
        if self._current_bubble:
            self._current_bubble.append_block("⏹ **已取消**")
            self._current_bubble.end_stream()
        self._cancel_btn.setVisible(False)
        self._cancel_btn.setEnabled(False)
        self._finalize(clear_task=False)
        self._save_task_state("interrupted")
        self._set_status("⏹ 已停止；点「恢复任务」可从中断处继续")

    def _effective_model(self):
        """v0.5.6 P1：按 Plan / Act 模式选择实际使用的模型。"""
        cfg = self._config
        if cfg.get("mode_models_enabled", False):
            chosen = str(cfg.get(
                "model_plan" if self._mode == "plan" else "model_act", ""
            ) or "").strip()
            if chosen:
                return chosen
        return cfg.get("model", "")

    def _start_worker(self, participant=None):
        """启动 ChatWorker，连接所有信号，并构建增强系统提示词

        Args:
            participant: 当前参与者对象（可选），用于过滤工具和系统提示词
        """
        # 断开旧 worker 的信号
        if self._worker is not None:
            try:
                self._worker.chunk.disconnect()
                self._worker.done.disconnect()
                self._worker.error.disconnect()
                self._worker.tool_calls.disconnect()
                self._worker.usage.disconnect()
            except (TypeError, RuntimeError):
                pass

        # 确定参与者（优先使用传入的，否则取当前激活的）
        if participant is None:
            participant = self._get_participant()

        # 构建增强的系统提示词
        enhanced_sp = self._build_enhanced_system_prompt(participant=participant)

        # v0.5.6 P1：分模型按模式覆盖；用副本，不污染全局配置
        config = dict(self._config)
        config["model"] = self._effective_model()
        self._current_model = config["model"]
        self._sync_header()
        # 构建增强工具列表（内置 + MCP + P1 元工具）
        enhanced_tools = self._build_agent_tools(participant=participant)

        self._audit.log(
            "request", round=self._tool_round + 1,
            model=config["model"], mode=self._mode,
            provider=config.get("provider", ""),
            tools=len(enhanced_tools),
            system_prompt_chars=len(enhanced_sp),
            messages=len(self._messages))

        self._worker = ChatWorker(
            self._messages, config,
            system_prompt=enhanced_sp,
            tools=enhanced_tools
        )
        self._worker.chunk.connect(self._on_chunk)
        self._worker.done.connect(self._on_done)
        self._worker.error.connect(self._on_error)
        self._worker.tool_calls.connect(self._on_tool_calls)
        self._worker.usage.connect(self._on_usage)
        self._worker.finished.connect(self._on_worker_finished)
        self._cancel_event = new_cancel_event()
        self._busy = True
        self._cancel_btn.setVisible(True)
        self._cancel_btn.setEnabled(True)
        self._thinking_bar.setVisible(True)
        self._set_status(
            f"🤔 AI 思考中…（第 {self._tool_round + 1} 轮 · 可直接输入新指令转向）")
        self._save_task_state("running")
        self._worker.start()

    def _build_enhanced_system_prompt(self, participant=None):
        """构建增强的系统提示词，整合所有自定义配置

        内容包括:
          0. 参与者系统提示词（如果是特定参与者）
          1. 任务管理规范
          2. 用户定义的系统提示词 (settings_dialog)
          3. 可用技能摘要 (prompts/skills)
          4. 自定义指令 (project/user/file instructions)
          5. 自定义 Agent 摘要 (agents/)
          6. Plan / Act 运行模式约束（Cline Edition）
          7. v0.5.6 P1：项目规则文件 / Skills 目录 / 仓库结构索引
        """
        parts = [self._mode_system_prompt()]
        cfg = self._config
        project_dir = self._get_project_root()

        # 0. 参与者系统提示词优先
        if participant and participant.system_prompt:
            parts.append(participant.system_prompt)

        # 1. 行为规范 — 对标 GitHub Copilot Chat
        todo_guide = (
            "## 行为规范\n\n"
            "你是 AI 编程助手，请遵循以下规则：\n\n"
            "**工具使用原则：**\n"
            "- 对于多步骤任务，使用 manage_todo_list 创建计划并跟踪进度\n"
            "- 完成一个子任务后立即更新状态，同一时间只进行一个任务\n"
            "- **避免重复调用同一工具** — 如果连续 2 次调用同一工具无进展，请尝试其他方法或报告结果\n"
            "- **不要无意义循环** — 如果某个操作失败，最多重试 1 次，然后报告错误\n"
            "- read_file 一次性读取整个文件，不要分段读取\n\n"
            "**回复原则：**\n"
            "- 完成任务后给出简洁的总结，不要在没有新信息时持续调用工具\n"
            "- 如果用户的问题很简单（不需要代码修改），直接回答，不要调用工具\n"
            "- 在 15 轮工具调用后，必须给出当前进度的总结\n\n"
            "**代码规范：**\n"
            "- 使用 create_file 创建文件，使用 insert_edit 进行小范围修改\n"
            "- 不要用 python -c 内联大段代码，先写入文件再执行\n"
            "- JSON 参数中的双引号必须转义为 \\\"，换行用 \\n 表示"
        )
        parts.append(todo_guide)

        # 2. 用户系统提示词
        user_sp = self._system_prompt
        if user_sp:
            parts.append(user_sp)

        # 3. v0.5.6：渐进式 Skills 目录（仅一行描述常驻，正文按需加载）
        skills_sp = skills_catalog_prompt(
            project_dir, enabled=bool(cfg.get("skills_enabled", True)))
        if skills_sp:
            parts.append(skills_sp)

        # 4. 自定义指令
        try:
            current_file = None
            w = self.window()
            if w and hasattr(w, 'get_plugin'):
                editor = w.get_plugin('editor')
                if editor and hasattr(editor, 'get_current_editor'):
                    ed = editor.get_current_editor()
                    if ed and hasattr(ed, 'filename'):
                        current_file = ed.filename
        except Exception:
            current_file = None

        instructions = get_effective_instructions(
            file_path=current_file,
            project_dir=project_dir
        )
        if instructions:
            parts.append("## 项目自定义指令\n" + instructions)

        # 4b. v0.5.6 P1：多来源规则文件（AGENTS.md / CLAUDE.md /
        #     .clinerules / .spydercopilot/rules/*.md / 用户级规则）
        rules_sp = format_rules_prompt(
            project_dir, current_file,
            budget=cfg.get("rules_budget", 12000),
            enabled=bool(cfg.get("rules_enabled", True)))
        if rules_sp:
            parts.append(rules_sp)

        # 4c. v0.5.6 P1：仓库结构索引（只读缓存，主线程零遍历）
        repo_sp = format_repo_map(
            project_dir,
            budget=cfg.get("repo_index_budget", 6000),
            enabled=bool(cfg.get("repo_index_enabled", True)))
        if repo_sp:
            parts.append(repo_sp)

        # 5. 自定义 Agent 摘要
        agents_sp = get_agents_system_prompt()
        if agents_sp:
            parts.append(agents_sp)

        # 6. 当前 Todo 进度（仅在有任务时显示）
        todo_summary = TodoManager().get_summary()
        if todo_summary:
            parts.append(todo_summary)

        return "\n\n".join(parts) if parts else ""

    def _build_agent_tools(self, participant=None):
        """构建完整的工具列表（内置 + MCP）

        Args:
            participant: 参与者对象，用于按 allowed_tools 过滤工具
        """
        # 先获取 Agent 配置的工具过滤（如果有选中的内置 Agent）
        agent_config = self._get_active_agent_config()
        allowed_tools = None

        if participant and participant.allowed_tools is not None:
            allowed_tools = set(participant.allowed_tools)
        elif agent_config and agent_config.get("allowed_tools") is not None:
            allowed_tools = set(agent_config["allowed_tools"])

        if allowed_tools:
            tools = [
                t for t in AGENT_TOOLS
                if t.get("function", {}).get("name") in allowed_tools
            ]
        else:
            tools = list(AGENT_TOOLS)

        try:
            mcp_tools = get_mcp_tool_definitions()
            if mcp_tools:
                for t in mcp_tools:
                    fn_name = t.get("function", {}).get("name", "")
                    if allowed_tools is None or fn_name in allowed_tools:
                        tools.append({
                            "type": t.get("type", "function"),
                            "function": t.get("function", {}),
                        })
        except Exception as e:
            logger.debug("Failed to get MCP tools: %s", e)

        # v0.5.6 P1：上下文元工具（只读，Plan 模式也放行）
        if self._config.get("agent_mode", True):
            known = {t.get("function", {}).get("name") for t in tools}
            for t in META_TOOL_DEFINITIONS:
                name = t["function"]["name"]
                if name in known:
                    continue
                if allowed_tools is not None and name not in allowed_tools:
                    continue
                tools.append(t)

        # Plan 模式：只暴露只读/规划工具（含 MCP 工具一并过滤）
        if getattr(self, "_mode", "act") == "plan":
            tools = [
                t for t in tools
                if t.get("function", {}).get("name") in PLAN_ALLOWED_TOOLS
            ]
        return tools

    def _on_worker_finished(self):
        """Worker 线程结束（安全网：确保 UI 恢复可用）"""
        self._cancel_btn.setVisible(False)
        self._thinking_bar.setVisible(False)
        # 审批挂起期间不能恢复输入框，否则用户会在卡片未决时发出新请求
        if self._awaiting_approval:
            self._set_status("⏸ 等待批准：用输入框上方的按钮快速处理，"
                             "或到卡片里看差异逐项确认")
            return
        self._set_status("")
        # 安全网：如果 _on_done/_on_error 未正常调用 _finalize，
        # 这里确保输入框恢复可用
        if not self._input.isEnabled():
            self._agent_active = False
            self._tc_handled = False
            self._input.setEnabled(True)
            self._send_btn.setEnabled(True)
            self._input.setFocus()

    def _on_chunk(self, text):
        """v0.5.5 P0：只入缓冲，真正的 UI 更新由 30ms 定时器批量执行。"""
        self._chunk_buffer.append(text)
        if not self._chunk_timer.isActive():
            self._chunk_timer.start()
        self._set_status("📝 AI 正在回复…（可直接输入新指令转向）")
        self._usage.add_text(text, completion=True)
        self._usage_ticks += 1
        if self._usage_ticks >= 10:
            self._usage_ticks = 0
            self._refresh_usage_ui()

    def _flush_chunks(self):
        """把节流窗口内的 delta 合成一次增量插入，避免 O(n²) 全量重排。"""
        self._chunk_timer.stop()
        if not self._chunk_buffer:
            return
        text = "".join(self._chunk_buffer)
        self._chunk_buffer = []
        bubble = self._current_bubble
        if bubble is not None:
            if not bubble.is_streaming():
                bubble.begin_stream()
            bubble.append(text)
        self._scroll_to_bottom()

    def _on_usage(self, usage):
        self._usage.add_usage(usage)
        self._audit.log("usage", usage=usage,
                        cost=self._usage.cost(),
                        prompt_tokens=self._usage.prompt_tokens,
                        completion_tokens=self._usage.completion_tokens)
        self._refresh_usage_ui()

    def _on_done(self, response):
        # 如果本轮有 tool_calls 正在处理中，跳过（由 _on_tool_calls 接管）
        if self._agent_active and self._tc_handled:
            return
        # 收到最终文本响应 — 无论之前 agent 状态如何，都要恢复正常 UI
        self._agent_active = False
        self._tc_handled = False
        self._flush_chunks()
        if response:
            msg = {"role": "assistant", "content": response}
            rc = self._worker._reasoning_content if self._worker else ""
            if rc:
                msg["reasoning_content"] = rc
            self._messages.append(msg)
            # v0.5.5：写历史文件放到后台，避免长会话阻塞 GUI 线程
            snapshot = list(self._messages)
            submit(lambda: save_chat(snapshot), lambda _p: None, self)
        self._audit.log(
            "task_done", rounds=self._total_tool_rounds,
            segments=self._auto_continues + 1,
            response_chars=len(response or ""),
            elapsed_s=round(time.time() - self._task_started_at, 2)
            if self._task_started_at else 0,
            prompt_tokens=self._usage.prompt_tokens,
            completion_tokens=self._usage.completion_tokens,
            cost=self._usage.cost())
        self._refresh_usage_ui()
        self._finalize()

    def _on_error(self, msg):
        """画错误卡片。整段包在 try 里：错误处理自己不许把界面带崩。

        v0.8.13 之前这里固定拼三句「检查 API Key / 网络 / 配置」，不管
        真实原因是什么——402 欠费也让人去查 Key。现在 payload 是线程算好
        的卡片（结论 + 服务端原文 + 分步处置），界面只负责显示。
        """
        try:
            payload = str(msg or "").strip()
            if not payload:
                return
            if not api_errors.is_card(payload):
                # 兼容旧调用方 / 第三方直接 emit 裸文本
                payload = api_errors.card_for(
                    text=payload, provider=self._config.get("provider", ""),
                    model=self._config.get("model", ""))
            self._audit.log("error", message=payload[:600])
            self._flush_chunks()
            bubble = self._current_bubble
            if bubble is not None:
                # 已经吐了半截回答就别覆盖——那段内容往往正是用户要的
                if (bubble.get_content() or "").strip():
                    bubble.commit_say()   # 半截正文先定性，卡片才不会插到它前面
                    bubble.append_block(payload)
                else:
                    bubble.set_content(payload)
                # 不管哪一支都要标失败：过程面板里可能记着「跑到哪一步」，
                # 收尾时折成「已完成」会让人以为那一轮成功了。
                bubble.finish_process(failed=True)
            self._set_status(f"✕ {api_errors.status_line(payload)}")
        except Exception:  # noqa: BLE001 - 画卡片失败也必须回到可用状态
            logger.debug("error card rendering failed", exc_info=True)
            try:
                self._set_status("✕ 请求失败（详情见日志）")
            except Exception:  # noqa: BLE001
                pass
        finally:
            self._finalize()

    # ── Agent 工具调用 ──

    # ── Cline 风格审批流程（替代 0.4.1 的批量 QMessageBox）──────────

    def _approval_policy(self):
        """当前审批策略，兼容旧配置的 auto_execute 开关（v0.5.6 加 risk）。"""
        if self._config.get("auto_execute", False):
            return "none"
        policy = self._config.get("approval_policy", "writes")
        return policy if policy in ("risk", "writes", "all", "none") else "writes"

    def _needs_approval_tool_calls(self, tool_calls):
        """v0.5.6：按风险分级判断是否需要挂起 Agent 循环等待审批。"""
        policy = self._approval_policy()
        for tc in tool_calls:
            fn = tc.get("function") or {}
            name = fn.get("name", "")
            args = parse_args(fn.get("arguments", "{}")) or {}
            risk = classify_tool(name, args)
            # Plan 模式工具已被白名单限制为只读，不必打断对话
            if (self._mode == "plan" and policy != "all"
                    and risk != "danger"):
                continue
            if approval_required(name, policy, risk=risk, args=args):
                return True
        return False

    def _request_approval(self, tool_calls):
        """把审批卡片插入对话流，并显式挂起 Agent 循环。

        不使用模态对话框 / 嵌套事件循环，避免与 QThread 信号重入冲突。
        """
        self._agent_active = True
        self._tc_handled = True
        self._cancel_btn.setVisible(False)
        self._thinking_bar.setVisible(False)
        self._set_status(f"⏸ 等待批准 {len(tool_calls)} 项操作…")
        # 挂起期间 Agent 什么都没干，钟必须停：否则「正在处理 3m12s」
        # 会把用户看 diff 的时间算成模型思考时间。
        if self._current_bubble:
            self._current_bubble.pause_process()
        self._audit.log(
            "approval_requested", policy=self._approval_policy(),
            tools=[(tc.get("function") or {}).get("name", "") for tc in tool_calls],
            risks=[classify_tool((tc.get("function") or {}).get("name", ""),
                                 parse_args((tc.get("function") or {}).get(
                                     "arguments", "{}")) or {})
                   for tc in tool_calls])

        card = ApprovalCard(tool_calls, self._get_project_root())
        card.decided.connect(self._on_approval_completed)
        card.undo_requested.connect(self._undo_checkpoint)
        self._inner_lay.insertWidget(self._inner_lay.count() - 1, card)
        self._pending_card = card
        self._last_card = card
        card.changed.connect(self._refresh_approval_bar)
        self._set_awaiting_approval(True)
        # 卡片构造时还不知道视图多高；等布局跑完再分配差异区高度，
        # 否则「批准」按钮会被长 diff 顶到折叠线以下。滚动同理：几何在
        # 下一轮事件循环才生效，晚一点再来一次。
        self._fit_approval_card()
        for delay in (0, 60, 200):
            QTimer.singleShot(delay, self._settle_approval_card)
        # v0.5.6：只读（以及策略允许自动执行的写操作）不打扰用户，
        # 卡片里只留下真正需要人工确认的项；延迟到下一个事件循环执行。
        policy = self._approval_policy()

        def _auto(tc):
            fn = tc.get("function") or {}
            name = fn.get("name", "")
            args = parse_args(fn.get("arguments", "{}")) or {}
            risk = classify_tool(name, args)
            if self._mode == "plan" and risk != "danger" and policy != "all":
                return True
            return not approval_required(name, policy, risk=risk, args=args)

        card.auto_approve(_auto)

    def _on_approval_completed(self, approved_calls):
        """用户逐项（或批量）作出决定后恢复 Agent 循环"""
        self._set_awaiting_approval(False)
        self._pending_card = None
        self._audit.log(
            "approval_decision",
            approved=[(tc.get("function") or {}).get("name", "")
                      for tc in approved_calls],
            approved_count=len(approved_calls))
        if not approved_calls:
            self._set_status("")
            self._add_msg(
                "assistant",
                "⏸ 操作未获批准，已停止。请补充说明或直接给出新指令。")
            self._finalize()
            return
        # 这一段（提请求 → 等批准）到此为止：后面另起气泡写答案。
        # 旧面板必须先停表，否则它会永远停在「正在处理」——审批挂起时
        # 本来就是暂停态，不收尾就没人再来收（v0.8.5 实拍反馈）。
        if self._current_bubble:
            self._current_bubble.finish_process()
        # 执行结果与后续 AI 回复排在审批卡片之后，形成 Cline 式时间线
        self._current_bubble = self._add_msg("assistant", "")
        self._execute_approved_tools(approved_calls, card=self._last_card)

    def _undo_last_checkpoint(self):
        cp_id = self._checkpoints.last_id()
        if not cp_id:
            self._set_status("没有可撤销的检查点")
            return
        self._undo_checkpoint(cp_id)

    def _undo_checkpoint(self, cp_id):
        if not cp_id:
            return
        ok, notes = self._checkpoints.restore(cp_id)
        self._add_msg(
            "assistant",
            f"↩ **已撤销本步更改**（检查点 `{cp_id}`）\n\n"
            + "\n".join(notes))
        self._set_status(f"↩ 已恢复 {ok} 个文件到操作前状态")

    def _round_budget(self):
        """v0.8.0：解析本轮的工具轮次预算。

        返回 ``(soft, hard, auto_continue, max_auto)``，其中 ``hard <= 0``
        表示完全不限制轮次。优先级：Agent 自身配置 > 全局设置 > 默认值。

        这里刻意区分「软阈值」和「硬预算」：软阈值只往上下文里塞一句
        「尽快收束」，让模型自己决定何时停；硬预算才是真正的兜底。
        主流 Agent（Claude Code / Cursor / Cline）都是这个结构。
        """
        cfg = self._config or {}
        hard = 0
        participant = self._get_participant()
        agent_cfg = self._get_active_agent_config() or {}
        for raw in (getattr(participant, "max_tool_rounds", 0),
                    agent_cfg.get("max_tool_rounds", 0)):
            try:
                value = int(raw or 0)
            except (TypeError, ValueError):
                value = 0
            if value > 0:      # 0 = 跟随全局，不再覆盖
                hard = value
                break
        if hard <= 0:
            try:
                hard = int(cfg.get("max_tool_rounds", 200))
            except (TypeError, ValueError):
                hard = 200
        try:
            soft = int(cfg.get("soft_tool_rounds", 40) or 0)
        except (TypeError, ValueError):
            soft = 40
        if soft <= 0 or (hard > 0 and soft > hard):
            soft = hard if hard > 0 else 40
        auto = bool(cfg.get("auto_continue_on_limit", True))
        try:
            max_auto = int(cfg.get("max_auto_continues", 5) or 0)
        except (TypeError, ValueError):
            max_auto = 5
        return soft, hard, auto, max(0, max_auto)

    def _queue_agent_note(self, text):
        """把一条系统注记挂起，等本轮工具结果落库后再追加。

        必须放在 ``tool`` 消息之后，否则会破坏
        ``assistant(tool_calls) → tool`` 的紧邻约束。
        """
        self._pending_notes.append(text)

    def _on_tool_calls(self, tool_calls):
        """处理 AI 发出的工具调用

        v0.8.0 Autonomy Edition：把「到 25 轮就踢回给用户」改成
        「软提醒 → 硬预算 → 自动续跑」，只在真正的死循环处硬停。
        """
        soft, hard, auto, max_auto = self._round_budget()
        repeat_limit = 3

        names = [(tc.get("function") or {}).get("name", "?")
                 for tc in tool_calls]
        harmful = any(n in WRITE_TOOLS or n in EXECUTE_TOOLS for n in names)

        # ── 检测重复工具调用 ──
        call_sig = [
            (tc.get("function", {}).get("name", "?"),
             tc.get("function", {}).get("arguments", "{}")[:200])
            for tc in tool_calls
        ]
        self._tool_call_history.append(tuple(call_sig))
        if len(self._tool_call_history) > 5:
            self._tool_call_history.pop(0)

        # 检查最近 5 轮是否有 3 次以上相同签名
        if len(self._tool_call_history) >= repeat_limit:
            last = self._tool_call_history[-1]
            repeat_count = sum(1 for h in self._tool_call_history if h == last)
            if repeat_count >= repeat_limit:
                # 写/执行类原地重放 = 真死循环，必须停；只读类可能只是
                # 换个角度重试，注入一条警告让模型自己换招即可。
                if harmful:
                    self._agent_active = False
                    if self._current_bubble:
                        self._current_bubble.append_block(
                            f"**注意**：检测到重复的写/执行操作，同一指令已执行 "
                            f"{repeat_count} 次，为避免反复覆盖文件已停止。"
                            f"\n\n请描述你的需求或回复「继续」以继续。"
                        )
                    self._finalize()
                    return
                if not self._repeat_warned:
                    self._repeat_warned = True
                    self._queue_agent_note(
                        "[系统] 检测到相同参数的只读调用重复出现。"
                        "不要重复调用同一工具，换一种检索方式或直接给出结论。")

        # ── 硬预算：自动续跑，而不是要求用户敲「继续」 ──
        if hard > 0 and self._tool_round >= hard:
            if auto and self._auto_continues < max_auto:
                self._auto_continues += 1
                self._tool_round = 0
                self._soft_warned = False
                n, m = self._auto_continues, max_auto
                if self._current_bubble:
                    self._current_bubble.append_block(
                        f"🔄 **自动续跑 {n}/{m}** — 已用满 {hard} 轮预算，"
                        "继续执行（不需要回复「继续」）", kind="process")
                self._queue_agent_note(
                    "[系统] 工具预算已自动续期，任务状态未丢失。"
                    "请接着完成剩余工作，不要复述已完成的步骤；"
                    "若任务已完成，直接给出最终总结。")
                self._audit.log(
                    "auto_continue", segment=n, budget=hard,
                    total_rounds=self._total_tool_rounds)
            else:
                self._agent_active = False
                if self._current_bubble:
                    self._current_bubble.append_block(
                        f"**注意**：Agent 已完成 {self._total_tool_rounds} 轮操作，"
                        f"用满本任务预算（每段 {hard} 轮）。\n\n"
                        f"可在 设置 → Agent 中调高「单段工具轮次」、"
                        f"放宽「自动续跑次数」，或直接回复「继续」。"
                    )
                self._finalize()
                return

        # ── 软阈值：只提醒模型收束，不打断 ──
        if self._tool_round >= soft and not self._soft_warned:
            self._soft_warned = True
            remaining = (hard - self._tool_round) if hard > 0 else 0
            tail = (f"剩余约 {remaining} 轮" if hard > 0 else "轮次不设上限")
            self._queue_agent_note(
                f"[系统] 已执行 {self._tool_round} 轮工具调用（{tail}）。"
                "请收束当前工作：优先完成必要的写入与验证，"
                "避免继续做探索性读取。")
            if self._current_bubble:
                self._current_bubble.append_block(
                    f"🟡 已用 {self._tool_round} 轮，已提醒模型收束"
                    f"（{tail}，不会中断）", kind="process")

        # ── Cline 风格：写操作先展示 diff、逐项批准，显式挂起 Agent 循环 ──
        if self._needs_approval_tool_calls(tool_calls):
            self._request_approval(tool_calls)
            return

        self._execute_approved_tools(tool_calls)

    def _execute_approved_tools(self, tool_calls, card=None):
        """v0.5.5 P0：把工具执行 + 检查点快照移到线程池，GUI 线程零阻塞。

        assistant(tool_calls) 与 tool 结果仍严格一一对应，只写入本批次
        实际执行过的调用，避免部分批准时触发 OpenAI 协议错误。
        """
        self._agent_active = True
        self._tc_handled = True
        self._tool_round += 1
        self._total_tool_rounds += 1
        func_names = [tc.get("function", {}).get("name", "?") for tc in tool_calls]
        self._round_had_progress = any(
            n in WRITE_TOOLS or n in EXECUTE_TOOLS for n in func_names)
        self._stall_rounds = 0 if self._round_had_progress else self._stall_rounds + 1
        self._set_status(
            f"🔧 第{self._total_tool_rounds}轮（后台执行）: {', '.join(func_names)}")

        tool_blocks = []
        for tc in tool_calls:
            fn = tc.get("function", {}).get("name", "?")
            args_raw = tc.get("function", {}).get("arguments", "{}")
            hint = _brief_tool_args(args_raw)
            # 过程面板里一步一行；完整参数在审批卡 / 审计里还能查。
            # 用 ▸ 而不是 🔧：几何字符和面板头部的箭头同一套语言，18px 下
            # 也不会像彩色 emoji 那样抢眼。
            tool_blocks.append(f"▸ **{fn}**" + (f" `{hint}`" if hint else ""))

        if self._current_bubble:
            # v0.8.4：工具卡片进折叠面板，不占最终回答的位置
            self._current_bubble.begin_process()
            self._current_bubble.append_block(
                "\n\n".join(tool_blocks), kind="process")

        base_dir = self._get_project_root()
        self._pending_tool_calls = list(tool_calls)
        self._pending_card_for_tools = card
        self._snapshot_review_targets(tool_calls, base_dir)

        snapshot = None
        if self._config.get("checkpoints_enabled", True):
            snapshot = self._checkpoints.snapshot
        cancel_event = self._cancel_event
        cfg = dict(self._config)
        audit = self._audit
        self._audit.log(
            "tool_requested", round=self._tool_round,
            tools=func_names,
            risks=[classify_tool(
                (tc.get("function") or {}).get("name", ""),
                parse_args((tc.get("function") or {}).get("arguments", "{}")) or {})
                for tc in tool_calls],
            sandbox=bool(cfg.get("sandbox_enabled", True)))
        submit(
            lambda: execute_tool_batch(tool_calls, base_dir,
                                       snapshot=snapshot,
                                       cancel_event=cancel_event,
                                       config=cfg, audit=audit),
            self._on_tools_finished, self)
        self._save_task_state("running")

    def _on_tools_finished(self, payload):
        """后台工具批次完成：在主线程更新 UI / 消息，并决定是否自动验证。"""
        if not isinstance(payload, dict):
            return
        if payload.get("__error__"):
            self._add_msg("assistant",
                          f"✕ 工具批次执行异常：{payload['__error__']}")
            self._finalize()
            return
        if payload.get("cancelled") or self._cancel_event.is_set():
            self._add_msg("assistant", "⏹ 工具执行已取消")
            self._finalize(clear_task=False)
            self._save_task_state("interrupted")
            return

        tool_calls = self._pending_tool_calls or []
        card = self._pending_card_for_tools
        results = payload.get("results", [])
        tool_results = []
        last_result_summary = ""

        for item in results:
            tc = item.get("call") or {}
            fn = (tc.get("function") or {}).get("name", "?")
            tool_id = item.get("tool_id", "")
            result = item.get("result", "") or ""
            error = item.get("error", "") or ""

            if error:
                tool_results.append({
                    "role": "tool", "tool_call_id": tool_id,
                    "content": f"错误: {error}"
                })
                last_result_summary = f"✕ {fn} 失败"
                if self._current_bubble:
                    self._current_bubble.append_block(
                        f"**✕ {fn}** 失败：{error[:200]}")
                continue

            tool_results.append({
                "role": "tool", "tool_call_id": tool_id, "content": result
            })
            rlines = result.split("\n")
            if result.startswith("✅") or result.startswith("❌"):
                # 结果自带状态记号：这里已经有 icon 了，去掉重复的那个
                result_preview = _polish_tool_text(
                    result[:200], keep_mark=False)
            elif result.startswith("{"):
                result_preview = "(返回结构化数据)"
            else:
                preview_lines = [l for l in rlines if l.strip()][:3]
                result_preview = "\n".join(preview_lines)[:300]

            if self._current_bubble:
                icon = "✓" if "错误" not in result[:20] else "!"
                self._current_bubble.append_block(
                    f"**{icon} {fn}** → {_polish_tool_text(result_preview, keep_mark=False)}",
                    kind="process")

            if result.startswith('{"action": "resolve_conflict"'):
                try:
                    conflict_data = json.loads(result)
                    resolution_text = conflict_data.get("resolution_prompt", "")
                    if resolution_text:
                        self._messages.append(
                            {"role": "user", "content": resolution_text})
                        if self._current_bubble:
                            self._current_bubble.append_block(
                                "**正在解决冲突…**", kind="process")
                except (json.JSONDecodeError, KeyError) as e:
                    logger.debug("Conflict resolution parsing error: %s", e)

            last_result_summary = (
                f"✓ {fn} — {rlines[0][:60] if rlines else '完成'}")

        cp_id = payload.get("checkpoint")
        if cp_id:
            try:
                self._checkpoints.remember(cp_id)
            except AttributeError:
                self._checkpoints._last_id = cp_id
            if card is not None:
                card.set_checkpoint(cp_id)
            elif self._current_bubble:
                self._current_bubble.append_block(
                    f"检查点 `{cp_id}`（可用工具栏 ↩ 撤销本步）", kind="process")

        self._set_status(last_result_summary)
        # v0.7.9：写盘改动送编辑器做内联 diff 复核（Cursor 式）
        self._stage_editor_reviews(tool_calls)

        # 协议：assistant(tool_calls) + 一一对应的 tool 结果
        executed_calls = [i["call"] for i in results]
        rc = self._worker._reasoning_content if self._worker else ""
        assistant_tc_msg = {
            "role": "assistant", "content": None,
            "tool_calls": executed_calls,
        }
        if rc:
            assistant_tc_msg["reasoning_content"] = rc
        self._messages.append(assistant_tc_msg)
        self._messages.extend(tool_results)
        # v0.8.0：软提醒 / 自动续跑的系统注记必须排在 tool 结果之后，
        # 否则会破坏 assistant(tool_calls) → tool 的紧邻约束。
        self._flush_agent_notes()
        self._prune_tool_messages()
        self._save_task_state("running")

        if (self._config.get("verify_on_write", True)
                and self._verify_attempts < int(
                    self._config.get("verify_max_attempts", 2))
                and any((tc.get("function") or {}).get("name") in WRITE_TOOLS
                        for tc in tool_calls)):
            files = changed_files_from_calls(tool_calls,
                                             self._get_project_root())
            if files:
                self._set_status("🧪 自动验证中…（后台执行）")
                cfg = dict(self._config)
                root = self._get_project_root()
                submit(lambda: run_verification(files, cfg, root),
                       self._on_verification_finished, self)
                return
        self._continue_after_tools()

    # ── v0.7.9：编辑器内联 diff 复核 ───────────────────────────────

    def set_editor_review_provider(self, provider):
        """由插件注入：``provider([(path, old_text), ...]) -> int``。

        Agent 写盘之后，把改动送进编辑器做内联预览（红删除线 + 绿底
        + 接受 / 撤销按钮）。面板本身不碰 Spyder 编辑器 API。
        """
        self._review_provider = provider

    def _snapshot_review_targets(self, tool_calls, base_dir):
        """写盘前快照涉及文件的内容（主线程，submit 之前）。

        v0.8.1 起这份快照不只服务编辑器内联：对话里的红绿卡片同样需要
        「改动前的原文」。Agent 一旦落盘，被删掉的那几行就没有任何地方
        能查到了 —— 所以判断条件要和两个渲染出口分别对齐，不能只挂在
        provider 上，否则没有编辑器（或内联被关掉）时卡片就成了无源之水。

        v0.8.7：多了一层「目录级兜底」。弱模型常用 ``run_command``
        （``python -c "open(...,'w').write(...)"`` / 重定向 / echo）写盘，
        参数里根本没有 ``filepath``，逐工具解析必然落空 —— 于是快照为空、
        不预览、编辑器里永远没有红绿，连一句失败提示都没有。这里在批量
        执行前扫一遍项目源文件的 mtime，事后比对出真正被改的文件，
        不依赖模型用什么手段写盘。
        """
        self._review_before = {}
        self._review_scan = None
        want_editor = bool(self._review_provider) and bool(
            self._config.get("inline_diff_enabled", True))
        want_chat = self._config.get("chat_diff_mode", "smart") != "off"
        if not (want_editor or want_chat):
            return
        try:
            files = changed_files_from_calls(tool_calls, base_dir,
                                            include_shell=True)
        except Exception:
            logger.debug("review targets resolve failed", exc_info=True)
            return
        for path in files:
            try:
                with open(path, "r", encoding="utf-8",
                          errors="replace") as fh:
                    self._review_before[path] = fh.read()
            except OSError:
                self._review_before[path] = None   # 原本不存在（新建）

        # 这一批里有「会写盘但路径算不出来」的工具：留一份目录快照兜底
        if _batch_may_write_blindly(tool_calls):
            try:
                scan = _scan_sources(base_dir)
            except Exception:
                logger.debug("source scan failed", exc_info=True)
                return
            self._review_scan = scan
            for path in scan:
                if path in self._review_before:
                    continue
                try:
                    with open(path, "r", encoding="utf-8",
                              errors="replace") as fh:
                        self._review_before[path] = fh.read()
                except OSError:
                    self._review_before[path] = None

    # ── v0.8.7：内联 diff 自检 ─────────────────────────────────────

    def _current_editor_text(self):
        """当前 Spyder 编辑器里的 (路径, 全文)。没有编辑器时返回 (None, "")。"""
        try:
            win = self.window()
            if not (win and hasattr(win, "get_plugin")):
                return None, ""
            plugin = win.get_plugin("editor")
            if plugin is None:
                return None, ""
            try:
                path = plugin.get_current_filename() or ""
            except Exception:
                path = ""
            editor = plugin.get_current_editor()
            text = editor.toPlainText() if editor is not None else ""
            return (path or None), (text or "")
        except Exception:
            logger.debug("current editor text failed", exc_info=True)
            return None, ""

    @staticmethod
    def _demo_text(text):
        """在当前文件内容上造几处假改动：改一行、加一行、删一行。"""
        lines = text.split("\n")
        if len(lines) < 5:
            return text + ("\n# ↓ 内联 diff 自检：这一行是新增的（绿色）\n")
        idx = max(1, len(lines) // 3)
        while idx < len(lines) - 2 and not lines[idx].strip():
            idx += 1
        out = list(lines)
        out[idx] = out[idx] + "   # ← 被修改的行"
        out.insert(idx + 1, "# ← 新增的一行（绿色底）")
        drop = idx + 2
        while drop < len(out) - 1 and not out[drop].strip():
            drop += 1
        if drop < len(out) - 1:
            del out[drop]
        return "\n".join(out)

    def _run_inline_diff_demo(self):
        """对当前文件做一份**副本**并预览，绝不碰用户的文件。"""
        provider = self._review_provider
        if provider is None:
            self._add_msg(
                "assistant",
                "✕ 编辑器内联预览没有接上（插件没有把预览通道注入面板）。"
                "请重启 Spyder 后再试；若仍然这样，请把这句话发给我。")
            return
        path, text = self._current_editor_text()
        if not text.strip():
            self._add_msg("assistant",
                          "请先在编辑器里打开一个文件，再执行 `/diffdemo`。")
            return
        try:
            demo = self._write_demo_copy(path, text)
        except Exception as exc:                              # noqa: BLE001
            logger.debug("write demo copy failed", exc_info=True)
            self._add_msg("assistant", f"✕ 写自检副本失败：{exc}")
            return
        try:
            outcome = provider([(demo, text)])
        except Exception as exc:                              # noqa: BLE001
            logger.debug("demo stage failed", exc_info=True)
            self._add_msg("assistant", f"✕ 自检失败：{exc}")
            return
        staged = 0
        reasons = []
        if isinstance(outcome, dict):
            staged = int(outcome.get("staged") or 0)
            reasons = [r for _p, r in (outcome.get("failures") or [])]
        else:
            staged = int(outcome or 0)
        name = os.path.basename(demo)
        if staged:
            self._add_msg(
                "assistant",
                f"✓ 内联 diff 自检已生效：已在编辑器里打开 `{name}`，"
                "应当看到红色删除线、绿色新增行，顶部有「接受全部修改 / "
                "不接受本次修改」，"
                "鼠标移到改动处还有 ✓ / ✕。\n\n"
                "这是**副本**，你的原文件没有被改动；看完直接关掉这个标签页即可。"
                "如果这里仍然看不到颜色，说明渲染被别的插件覆盖了，"
                "请把 Spyder 的日志发我。")
        else:
            reason = reasons[0] if reasons else "未知原因"
            self._add_msg(
                "assistant",
                f"✕ 自检没能画出来：{reason}。"
                "这句是定位问题最关键的线索，请把它发给我。")

    @staticmethod
    def _write_demo_copy(path, text):
        """把当前内容写成一份带假改动的临时副本，返回副本路径。"""
        import tempfile

        base = os.path.basename(path or "demo.py")
        stem, ext = os.path.splitext(base)
        if not ext:
            ext = ".py"
        folder = os.path.join(tempfile.gettempdir(), "copilot_inline_demo")
        os.makedirs(folder, exist_ok=True)
        demo = os.path.join(folder, "diff-demo-" + (stem or "demo") + ext)
        with open(demo, "w", encoding="utf-8") as fh:
            fh.write(CopilotChatPanel._demo_text(text))
        return demo

    def _changed_by_scan(self, base_dir, scan):
        """比对目录快照，返回「确实被改过」的路径集合。
        ``scan`` 必须由调用方传进来：``_stage_editor_reviews`` 开头就把
        ``self._review_scan`` 取走并清空了，从 self 上再读一次只会拿到
        None，于是每次都判定「什么都没改」。
        """
        if not scan:
            return set()
        try:
            now = _scan_sources(base_dir)
        except Exception:
            return set()
        changed = set()
        for path, sig in now.items():
            if scan.get(path) != sig:
                changed.add(path)
        for path in scan:                 # 被删掉的也算改动
            if path not in now:
                changed.add(path)
        return changed

    def _stage_editor_reviews(self, tool_calls):
        """工具批次结束后：改动送编辑器内联，并在对话里留一份红绿卡片。

        两条出口刻意分开：编辑器内联是「在真实 file buffer 里改」，方便
        逐处接受；对话卡片是「一份可回看的记录」，不受编辑器是否打开、
        装饰是否被主题刷掉的影响。任一出口失败都不该让另一个哑火。
        """
        before = getattr(self, "_review_before", None) or {}
        self._review_before = {}
        scan = getattr(self, "_review_scan", None)
        self._review_scan = None
        if not before:
            return
        root = self._get_project_root()
        if scan is not None:
            # 有目录快照就以它为准：run_command 写盘的文件名只能事后算出
            targets = [p for p in self._changed_by_scan(root, scan)
                       if p in before]
        else:
            try:
                files = changed_files_from_calls(
                    tool_calls, root, include_shell=True)
            except Exception:
                files = list(before)
            targets = [p for p in files if p in before]
        items = []
        for path in targets:
            try:
                with open(path, "r", encoding="utf-8",
                          errors="replace") as fh:
                    new_text = fh.read()
            except OSError:
                continue        # 文件被删掉：没有可预览的内容
            if before[path] != new_text:
                items.append((path, before[path] or ""))
        if not items:
            return
        # 一次开八张 tab 已经够闹了：python 文件优先，其余只进对话卡片
        limit = getattr(self, "MAX_EDITOR_REVIEWS", 8)
        if len(items) > limit:
            order = sorted(
                range(len(items)),
                key=lambda i: (0 if items[i][0].lower().endswith(
                    (".py", ".pyw")) else 1, i))
            items = [items[i] for i in order[:limit]]

        staged = 0
        failures = []
        provider = self._review_provider
        if provider and self._config.get("inline_diff_enabled", True):
            try:
                outcome = provider(items)
            except Exception as exc:
                logger.debug("editor review stage failed", exc_info=True)
                failures = [("", "staging 抛异常：%s" % (exc,))]
            else:
                if isinstance(outcome, dict):
                    staged = int(outcome.get("staged") or 0)
                    failures = list(outcome.get("failures") or [])
                else:                      # 老签名：直接返回成功数
                    staged = int(outcome or 0)
        elif not provider:
            failures = [(p, "插件没有接入编辑器") for p, _t in items]

        # v0.8.1：编辑器没接住的（文件没打开、非 Spyder 环境、装饰被覆盖），
        # 在对话里补一张红绿卡片 —— 「删行标红 / 增行标绿」任何情况下可见。
        failed_paths = {os.path.abspath(p) for p, _r in failures if p}
        if staged and failed_paths:
            self._emit_chat_diffs(
                [it for it in items if os.path.abspath(it[0]) in failed_paths],
                staged=0)
        else:
            self._emit_chat_diffs(items, staged)

        if not self._current_bubble:
            return
        if staged:
            names = "、".join(os.path.basename(p) for p, _t in items[:3])
            more = "" if len(items) <= 3 else f" 等 {len(items)} 个文件"
            count_note = (f"（共 {len(items)} 个文件）" if len(items) > 1 else "")
            self._current_bubble.append_block(
                f"已在编辑器中高亮 {names}{more}{count_note} 的修改："
                "顶部工具条写着「第几个/共几个」，按钮为「接受全部修改 / "
                "不接受本次修改」；多文件时另有「接受本文件 / 不接受本文件」，"
                "悬停改动处还有逐处 ✓ / ✕"
                "（Ctrl+Enter 全部接受，Ctrl+Backspace 全部不接受）")
        if failures and not staged:
            # 静默跳过是最难查的失败：把原因直接摆到对话里。
            # 路径为空表示整批级失败（拿不到编辑器 / 登记处建不起来），
            # 这时别硬凑一个「文件」前缀，直接给原因更有用。
            def _fmt(item):
                p, r = item
                name = os.path.basename(p)
                return "%s — %s" % (name, r) if name else r

            reason = "；".join(_fmt(it) for it in failures[:2])
            if len(failures) > 2:
                reason += "（共 %d 处）" % len(failures)
            self._current_bubble.append_block(
                f"_编辑器内联未生效：{reason}。改动已在上面用红绿行显示。_")
        self._scroll_to_bottom()

    # 对话内 diff 卡片的规模上限：超过就折叠/截断，别让一条消息把
    # 滚动条拉到几千像素（Qt 富文本里每个 <tr> 都是一个真实的表格行，
    # 全量铺开会明显拖慢重绘）。
    CHAT_DIFF_MAX_LINES = 90
    CHAT_DIFF_MAX_FILES = 6
    #: 一次工具批次最多在编辑器里铺开几个文件的复核（其余只进对话卡片）
    MAX_EDITOR_REVIEWS = 8

    def _emit_chat_diffs(self, items, staged=0):
        """把改动画成「红删绿增」贴在对话里（Cline / Roo Code 的形态）。

        走的是和审批卡完全相同的管线：两段原文 → unified diff → 行模型
        → 每个 ``<tr>`` 一整行底色。``smart`` 模式下如果编辑器已经把每个
        文件都画出来了就不再重复，避免同一处改动画两遍。
        """
        mode = self._config.get("chat_diff_mode", "smart")
        if mode == "off" or self._current_bubble is None:
            return
        if mode == "smart" and staged >= len(items):
            return

        shown = 0
        for path, old_text in items[:self.CHAT_DIFF_MAX_FILES]:
            try:
                with open(path, "r", encoding="utf-8",
                          errors="replace") as fh:
                    new_text = fh.read()
            except OSError:
                continue
            name = os.path.basename(path) or path
            patch = _diff.unified(old_text, new_text, fromfile=name,
                                  tofile=name, context=3)
            if not patch.strip():
                continue
            body, cut, hidden = self._trim_patch(
                patch, self.CHAT_DIFF_MAX_LINES)
            tail = ""
            if cut:
                tail = (f"\n\n_… 另有 {hidden} 行改动未展开"
                        f"（卡片最多画 {self.CHAT_DIFF_MAX_LINES} 行）_")
            self._current_bubble.append_block(
                f"**📄 `{name}`**\n\n```diff\n{body}\n```{tail}")
            shown += 1

        if len(items) > shown:
            self._current_bubble.append_block(
                f"_另有 {len(items) - shown} 个文件的改动未展示_")
        if shown:
            self._scroll_to_bottom()

    @staticmethod
    def _trim_patch(patch, limit):
        """把补丁裁到 ``limit`` 行以内；返回 (文本, 是否截断, 省略行数)。"""
        lines = patch.split("\n")
        if len(lines) <= limit:
            return patch, False, 0
        # 优先在 hunk 边界收尾：越过 limit 后遇到下一个 @@ 就在那里切开，
        # 比拦腰砍断一个 hunk 看着完整得多。
        cut_at = limit
        for index in range(limit, len(lines)):
            if lines[index].startswith("@@"):
                cut_at = index
                break
        return "\n".join(lines[:cut_at]), True, len(lines) - cut_at

    def note_editor_resolution(self, path, action):
        """编辑器侧复核结束后回写通知；撤销时给模型留一条事实消息。"""
        name = os.path.basename(path)
        if action == "revert":
            text = f"↩ 已撤销 **{name}** 的修改，文件恢复到修改前。"
            self._messages.append({
                "role": "user",
                "content": f"[编辑器操作] 用户撤销了对 {path} 的修改，"
                           "文件已恢复到修改前的内容。请勿在后续假设该修改仍存在。",
            })
        elif action == "mixed":
            text = f"✏ **{name}**：已按预览部分接受、部分撤销。"
        else:
            text = f"✓ 已接受 **{name}** 的修改。"
        try:
            bubble = self._current_bubble or self._add_msg("assistant", "")
            bubble.append_block(text)
        except Exception:
            logger.debug("resolution note failed", exc_info=True)

    def _on_verification_finished(self, payload):
        """把验证结果回灌给模型：失败时给出可复现的错误，让下一轮自行修复。"""
        data = payload if isinstance(payload, dict) else {}
        if data.get("__error__"):
            self._add_msg("assistant",
                          f"🧪 验证未执行: {data['__error__']}")
            self._continue_after_tools()
            return
        self._verify_attempts += 1
        self._audit.log(
            "verification", attempt=self._verify_attempts,
            ok=bool(data.get("ok")), skipped=bool(data.get("skipped")),
            command=data.get("command", ""),
            output=(data.get("output") or "")[:800])
        if data.get("skipped"):
            line = "**自动验证**：本次改动无可自动验证的 Python 文件，已跳过"
            detail = ""
        else:
            ok = bool(data.get("ok"))
            line = ("**✓ 自动验证通过**" if ok else "**✕ 自动验证未通过**")
            out = (data.get("output") or "").strip()
            detail = f"\n```\n{out[:1200]}\n```" if out else ""
        if self._current_bubble:
            self._current_bubble.append_block(line + detail)

        verdict = (
            "[自动验证结果] " + ("通过" if data.get("ok") else "未通过")
            + f"\n命令: {data.get('command') or '（跳过）'}\n"
            + (data.get("output") or "（无输出）")[:1500]
            + "\n\n若未通过，请根据上述错误修复代码再继续；"
              "若已通过，请直接给出本次改动的简要总结。"
        )
        self._messages.append({"role": "user", "content": verdict})
        self._set_status(line.replace("**", ""))
        self._continue_after_tools()

    def _flush_agent_notes(self):
        """把排队的系统注记写进消息列表（幂等，可在任意续跑入口调用）。"""
        if not self._pending_notes:
            return
        for note in self._pending_notes:
            self._messages.append({"role": "user", "content": note})
        self._pending_notes = []

    def _continue_after_tools(self):
        self._flush_agent_notes()
        old_bubble = self._current_bubble
        self._start_worker()
        # 保持同一个气泡，让 AI 连续回复
        self._current_bubble = old_bubble

    def _prune_tool_messages(self):
        """压缩消息列表，防止工具调用轮次过多导致消息膨胀

        关键约束：OpenAI API 要求每个 'tool' 角色消息必须紧跟在
        一个包含 'tool_calls' 的 'assistant' 消息之后。
        因此必须以 assistant(含tool_calls) 作为轮次边界，保持
        assistant + tool_results 作为不可分割的原子组。
        """
        # 定位第一条 assistant 消息
        first_assistant_idx = None
        for i, m in enumerate(self._messages):
            if m["role"] == "assistant":
                first_assistant_idx = i
                break
        if first_assistant_idx is None:
            return

        # 按 assistant(含tool_calls) → tool(s) 分组
        tail = self._messages[first_assistant_idx:]
        rounds = []
        current_round = []

        for m in tail:
            # 遇到新的 assistant 消息（含 tool_calls），开始新的一轮
            if m["role"] == "assistant" and m.get("tool_calls"):
                if current_round:
                    rounds.append(current_round)
                current_round = [m]
            else:
                current_round.append(m)

        if current_round:
            rounds.append(current_round)

        # 轮次未超限则无需裁剪
        if len(rounds) <= self.MAX_TOOL_ROUNDS:
            return

        # 保留前 1 轮 + 最近 (MAX_TOOL_ROUNDS-1) 轮
        keep = rounds[:1] + rounds[-(self.MAX_TOOL_ROUNDS - 1):]
        preserved = self._messages[:first_assistant_idx]
        for r in keep:
            preserved.extend(r)
        self._messages = preserved
        logger.debug("Pruned tool messages: %d rounds → %d rounds kept",
                     len(rounds), len(keep))

    def _format_tool_result_display(self, tool_name, result):
        """格式化工具执行结果的气泡显示

        - read_file: 只显示文件名和行数，完整内容已发送给 AI
        - 其他工具: 显示前 3000 字符摘要
        """
        max_preview = 3000

        if tool_name == "read_file":
            # read_file 结果格式: [filename — N 行]\n```\n...content...\n```
            lines = result.split("\n")
            header = lines[0] if lines else "读取文件"
            return f"\n**✓ {tool_name}** {header}\n*(完整内容已发送给 AI)*"

        elif tool_name in ("search_files", "text_search", "list_directory",
                           "codebase_search"):
            # 搜索结果可能很长，显示更多
            if len(result) <= max_preview * 2:
                return f"\n**✓ {tool_name}** 结果:\n```\n{result}\n```"
            else:
                return (f"\n**✓ {tool_name}** 结果:\n```\n{result[:max_preview]}\n```"
                        f"\n*(共 {len(result)} 字符，完整结果已发送给 AI)*")

        elif tool_name in ("create_file", "edit_file", "create_directory",
                           "rename_file", "delete_file"):
            # 操作类工具，结果通常很短
            return "\n" + _polish_tool_text(result)

        elif tool_name == "manage_todo_list":
            return "\n" + _polish_tool_text(result)

        else:
            # 其他工具默认显示摘要
            if len(result) <= max_preview:
                return f"\n**✓ {tool_name}** 结果:\n```\n{result}\n```"
            else:
                return (f"\n**✓ {tool_name}** 结果:\n```\n{result[:max_preview]}\n```"
                        f"\n*(共 {len(result)} 字符，完整结果已发送给 AI)*")

    def _get_project_root(self):
        """获取项目根目录作为工具操作的基准目录"""
        try:
            from spyder.api.plugins import Plugins
            w = self.window()
            if w and hasattr(w, 'get_plugin'):
                projects = w.get_plugin(Plugins.Projects)
                if projects and hasattr(projects, 'get_active_project_path'):
                    root = projects.get_active_project_path()
                    if root:
                        return root
        except Exception:
            pass
        # 回退到当前文件所在目录
        try:
            from spyder.plugins.editor.api import Editor
            w = self.window()
            if w and hasattr(w, 'get_plugin'):
                editor = w.get_plugin('editor')
                if editor and hasattr(editor, 'get_current_editor'):
                    ed = editor.get_current_editor()
                    if ed and hasattr(ed, 'filename') and ed.filename:
                        return os.path.dirname(ed.filename)
        except Exception:
            pass
        return os.getcwd()

    # ── v0.5.5 P0：用量、任务快照、恢复、转向、验证 ──────────────────

    def _refresh_usage_ui(self):
        try:
            ctx = self._usage.context_tokens(
                self._messages, self._system_prompt, AGENT_TOOLS)
            self._usage_text = self._usage.summary(
                self._messages, self._system_prompt, AGENT_TOOLS)
            self._sync_meter()
            pct = min(100, int(ctx * 100 / self._usage.context_window))
            self._ctx_progress.setValue(pct)
            self._ctx_progress.setToolTip(f"上下文使用率约 {pct}%")
        except Exception as exc:
            logger.debug("更新用量条失败: %s", exc)

    def _save_task_state(self, status):
        """任务快照写盘放到后台线程，主线程零阻塞。"""
        if not self._config.get("task_persistence", True):
            return
        if status == "idle":
            submit(TaskStateStore.clear, lambda _p: None, self)
            return
        state = {
            "status": status,
            "mode": self._mode,
            "tool_round": self._tool_round,
            "messages": [dict(m) for m in self._messages],
            "usage": {
                "prompt_tokens": self._usage.prompt_tokens,
                "completion_tokens": self._usage.completion_tokens,
            },
            "project_root": self._get_project_root(),
        }
        submit(lambda: TaskStateStore.save(state), lambda _p: None, self)

    def _check_resumable_task(self):
        try:
            self._resume_btn.setVisible(TaskStateStore.resumable())
        except Exception:
            pass

    def _resume_task(self):
        data = TaskStateStore.load()
        if not data or not data.get("messages"):
            self._resume_btn.setVisible(False)
            return
        self._messages = list(data["messages"])
        self._tool_round = int(data.get("tool_round", 0))
        self._soft_warned = False
        self._repeat_warned = False
        self._auto_continues = 0
        self._stall_rounds = 0
        self._pending_notes = []
        self._add_msg(
            "assistant",
            "♻️ **已恢复上次中断的任务**\n\n"
            "上下文与已完成的工具结果已载入，正在继续…")
        self._messages.append({
            "role": "user",
            "content": "[系统] 会话从中断处恢复，请根据已有上下文继续完成未竟的任务。",
        })
        self._current_bubble = self._add_msg("assistant", "")
        self._resume_btn.setVisible(False)
        self._busy = True
        self._send_btn.setText("转向")
        self._usage.reset_turn()
        self._verify_attempts = 0
        self._start_worker()

    def _redirect(self, text):
        """运行中收到新指令：丢弃旧响应 / 审批，就地带上下文转向。"""
        self._audit.log("redirect", text_chars=len(text),
                        from_round=self._tool_round)
        self._input.clear()
        if self._worker is not None:
            try:
                self._worker.cancel()
            except Exception:
                pass
        self._cancel_event.set()
        self._flush_chunks()
        if self._current_bubble:
            self._current_bubble.end_stream()
            # 转向会另起气泡，旧面板没人再收尾 —— 就地停表
            self._current_bubble.finish_process()
        self._set_awaiting_approval(False)
        self._pending_card = None
        self._agent_active = False
        self._tc_handled = False
        self._tool_round = 0
        self._total_tool_rounds = 0
        self._auto_continues = 0
        self._soft_warned = False
        self._repeat_warned = False
        self._stall_rounds = 0
        self._pending_notes = []
        self._tool_call_history = []
        self._verify_attempts = 0
        self._usage.reset_turn()
        image_paths = [a["path"] for a in self._attachments]
        bubble = self._add_msg("user", text)
        if image_paths:
            bubble.set_attachments(image_paths)
        self._refresh_editor_chip()
        content = self._content_with_images(self._compose_user_prompt(text))
        self._messages.append({"role": "user", "content": content})
        self._clear_attachments()
        self._current_bubble = self._add_msg("assistant", "")
        self._busy = True
        self._send_btn.setText("转向")
        self._start_worker()

    def _finalize(self, clear_task=True):
        self._flush_chunks()
        if self._current_bubble:
            self._current_bubble.end_stream()
            # v0.8.4：这一轮结束，过程面板停表并按策略收起
            self._current_bubble.finish_process()
        # 兜底：本轮凡是被换掉 / 漏收尾的面板，一律停表。少了这一步，
        # 任何一个「另起气泡」的分支都会留下一个永远在计时的面板。
        ProcessPane.finish_all()
        self._current_bubble = None
        self._agent_active = False
        self._tc_handled = False
        self._set_awaiting_approval(False)
        self._pending_card = None
        self._pending_notes = []
        self._busy = False
        self._input.setEnabled(True)
        self._send_btn.setEnabled(True)
        self._send_btn.setText("发送")
        self._input.setFocus()
        self._set_status("")
        self._refresh_usage_ui()
        if clear_task:
            self._save_task_state("idle")
            self._resume_btn.setVisible(False)
        else:
            self._resume_btn.setVisible(True)

    def update_config(self, config):
        self._config = config

    def clear(self):
        self._clear_chat()


# ===================================================================
# 主 Widget（Spyder 6 必需）
# ===================================================================

class CopilotChatWidget(PluginMainWidget):
    def __init__(self, name, plugin, parent=None):
        super().__init__(name, plugin, parent)
        self._panel = CopilotChatPanel(self)
        self.set_content_widget(self._panel)

    def setup(self, options=None):
        pass

    def update_actions(self):
        pass

    def get_title(self):
        return "Copilot Chat"

    def get_name(self):
        return self._name

    def _open_settings(self):
        self._panel._open_settings()

    def set_editor_cursor_fn(self, fn):
        pass

    def set_editor_context_provider(self, provider):
        """v0.7.1：把插件侧的编辑器读取函数转交给面板。"""
        self._panel.set_editor_context_provider(provider)

    def set_editor_review_provider(self, provider):
        """v0.7.9：把插件侧的编辑器内联 diff 预览函数转交给面板。"""
        self._panel.set_editor_review_provider(provider)

    def note_editor_resolution(self, path, action):
        """v0.7.9：编辑器复核结果回写（由插件 hub 调用）。"""
        self._panel.note_editor_resolution(path, action)

    def set_project_fns(self, get_root_fn, get_editor_fn):
        pass

    def is_project_context_enabled(self):
        return False
