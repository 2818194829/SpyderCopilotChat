# -*- coding: utf-8 -*-
"""
inline_edit — 编辑器里的「就地改写」（Ctrl+K）

对标 Trae / Cursor 的行内对话，但 Qt 没有 zone widget，也没有可以叠加在
文档上的 diff 层，所以这里用「浮动输入条 + 应用前先看 diff」的方式落地：

- ``Ctrl+K``：光标处弹出输入条。有选区就用选区，没选区时如果光标在
  ``def`` / ``class`` 行上就取整个块（对齐 "给这个函数加注释" 的直觉），
  否则只取当前行。
- 输入要求（默认占位符就是「为这段代码添加注释」）→ 后台线程请求模型，
  **不阻塞 GUI**。
- 回来先渲染 unified diff；点「应用」才写进文档（一个编辑块，Ctrl+Z 可
  整段撤销），点「取消」则文档一个字符都没动过。

为什么不直接就地替换再让用户拒绝：那样文档会先被改脏，用户一旦在预览
期间输入，撤销栈就会互相踩。先看 diff 再落盘，是 Qt 侧代价最低、也最
不容易丢数据的做法。
"""

from __future__ import annotations

import json
import logging
import re
import urllib.error
import urllib.request

from qtpy.QtCore import QEvent, QObject, QThread, Qt, Signal
from qtpy.QtGui import QTextCursor
from qtpy.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QLineEdit, QTextEdit, QToolButton,
    QVBoxLayout,
)

from . import theme
from .approval_card import _diff_html

logger = logging.getLogger(__name__)

#: 一次最多送多少行给模型（超过就截断，避免误选整份文件把成本打飞）
MAX_SOURCE_LINES = 220

#: 请求超时（秒）
REQUEST_TIMEOUT = 60

_SYSTEM_PROMPT = (
    "你是严谨的代码编辑助手。按用户的要求修改给定代码。\n"
    "只输出修改后的完整代码，不要解释，不要使用 Markdown 代码围栏，"
    "不要添加「修改后：」这类前缀。保持原有的缩进、引号和代码风格。"
)


def _clean_reply(text):
    """去掉模型偶尔加上的围栏和客套话。"""
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    fence = re.search(r"```[a-zA-Z0-9_+-]*\n(.*?)```", text, re.DOTALL)
    if fence:
        text = fence.group(1)
    text = re.sub(r"^(修改后的?代码|代码如下|Here[^\n]*)\s*[:：]?\s*\n", "",
                  text, flags=re.IGNORECASE)
    return text.strip("\n")


def _request_edit(instruction, code, language):
    """同步请求（只在后台线程里调用）。返回 ``(text, error)``。"""
    from .settings_dialog import get_provider_config

    cfg = get_provider_config()
    messages = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content":
            "要求：{0}\n\n代码（{1}）：\n{2}".format(
                instruction, language or "text", code)},
    ]

    try:
        if cfg.get("provider") == "copilot":
            from ..auth import get_valid_copilot_token
            from ..copilot_client import chat_completion

            token = get_valid_copilot_token()
            if not token:
                return "", "未登录 GitHub Copilot"
            result = chat_completion(
                token, messages, model=cfg.get("model"),
                temperature=0.0, max_tokens=2048)
            return (result.get("content") or ""), ""

        api_url = (cfg.get("api_url") or "").rstrip("/")
        endpoint = cfg.get("chat_endpoint") or "/chat/completions"
        if not api_url:
            return "", "未配置 API 地址"
        body = {
            "model": cfg.get("model"),
            "messages": messages,
            "temperature": 0.0,
            "max_tokens": 2048,
            "stream": False,
        }
        if cfg.get("provider") == "deepseek" and \
                cfg.get("thinking_mode", "disabled") == "disabled":
            # DeepSeek：不显式关掉思考，返回会被 reasoning_content 吃掉
            body["thinking"] = {"type": "disabled"}
        request = urllib.request.Request(
            "{0}{1}".format(api_url, endpoint),
            data=json.dumps(body).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer {0}".format(cfg.get("api_key", "")),
            },
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
        choice = (data.get("choices") or [{}])[0]
        return ((choice.get("message") or {}).get("content") or ""), ""
    except urllib.error.HTTPError as exc:
        return "", "HTTP {0}".format(exc.code)
    except (urllib.error.URLError, TimeoutError, OSError):
        return "", "网络不可用"
    except Exception as exc:  # pragma: no cover - 防御
        logger.debug("inline edit request failed", exc_info=True)
        return "", str(exc)[:120] or "请求失败"


class _EditWorker(QThread):
    """后台请求线程：模型慢不能卡住编辑器。"""

    done = Signal(str, str)  # (text, error)

    def __init__(self, instruction, code, language, parent=None):
        super().__init__(parent)
        self._instruction = instruction
        self._code = code
        self._language = language

    def run(self):
        text, error = _request_edit(self._instruction, self._code,
                                    self._language)
        try:
            self.done.emit(text or "", error or "")
        except RuntimeError:  # pragma: no cover - 解释器退出
            pass


class InlineEditPopup(QFrame):
    """贴在光标下方的输入条；拿到结果后原地长成 diff 预览。"""

    def __init__(self, editor, controller):
        super().__init__(editor.viewport())
        self._editor = editor
        self._controller = controller
        self._diff_text = ""
        self._old_text = ""
        self._instruction = ""
        self.setObjectName("inlineEditCard")
        # 自绘背景 + 无系统背景：否则浮条是"透"的，下面的代码会穿出来
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setAutoFillBackground(False)
        self._apply_style()
        self._build_ui()
        self.hide()

    # -- 外观 ---------------------------------------------------------

    def _apply_style(self):
        t = theme.current_theme()
        self.setStyleSheet(
            "QFrame#inlineEditCard {{"
            " background: {bg}; border: 1px solid {bd};"
            " border-radius: 8px; }}"
            "QLabel#inlineEditHint {{ color: {hint}; font-size: 11px; }}"
            "QLineEdit#inlineEditInput {{"
            " background: {field}; color: {fg}; border: 1px solid {bd};"
            " border-radius: 6px; padding: 5px 8px; font-size: {fs}px; }}"
            "QLineEdit#inlineEditInput:focus {{ border-color: {accent}; }}"
            "QTextEdit#inlineEditDiff {{"
            " background: {code}; color: {fg}; border: 1px solid {bd};"
            " border-radius: 6px; font-family: Consolas, monospace;"
            " font-size: 11px; }}"
            "QPushButton {{"
            " background: {btn}; color: {fg}; border: 1px solid {bd};"
            " border-radius: 6px; padding: 4px 12px; font-size: 12px; }}"
            "QPushButton:hover {{ border-color: {accent}; }}"
            "QPushButton[kind=\"primary\"] {{"
            " background: {accent}; color: #ffffff; border: none; }}"
            "QPushButton[kind=\"primary\"]:hover {{ background: {accent_h}; }}"
            "QScrollBar:vertical {{ background: transparent; width: 8px;"
            " margin: 0; }}"
            "QScrollBar::handle:vertical {{ background: {bd};"
            " border-radius: 4px; min-height: 20px; }}"
            "QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; }}".format(
                bg=t["elevated"], bd=t["border_strong"], fg=t["text"],
                hint=t["text_faint"], field=t["surface_alt"],
                accent=t["accent"], code=t["code_bg"],
                btn=t["surface_alt"], accent_h=t["accent_soft"],
                fs=max(11, theme.current_theme().get("ui_font", 12))))

    def _build_ui(self):
        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 8, 10, 8)
        lay.setSpacing(6)

        row = QHBoxLayout()
        row.setSpacing(6)
        self._input = QLineEdit()
        self._input.setObjectName("inlineEditInput")
        self._input.setPlaceholderText("为这段代码添加注释…（Enter 生成，Esc 关闭）")
        self._input.returnPressed.connect(self._submit)
        row.addWidget(self._input, 1)

        self._send = theme.push_button("生成", "primary")
        self._send.clicked.connect(self._submit)
        row.addWidget(self._send)

        close = QToolButton()
        close.setText("\u00d7")
        close.setCursor(Qt.PointingHandCursor)
        close.clicked.connect(self._controller.cancel)
        close.setStyleSheet(
            "QToolButton{{border:none;background:transparent;color:{0};"
            "font-size:15px;padding:0 4px;}}".format(
                theme.current_theme()["text_faint"]))
        row.addWidget(close)
        lay.addLayout(row)

        self._hint = QLabel("")
        self._hint.setObjectName("inlineEditHint")
        self._hint.hide()
        lay.addWidget(self._hint)

        self._diff = QTextEdit()
        self._diff.setObjectName("inlineEditDiff")
        self._diff.setReadOnly(True)
        self._diff.setVisible(False)
        self._diff.setMinimumHeight(120)
        lay.addWidget(self._diff)

        actions = QHBoxLayout()
        actions.setSpacing(6)
        self._apply_btn = theme.push_button("应用", "primary")
        self._apply_btn.clicked.connect(self._controller.apply)
        self._apply_btn.hide()
        actions.addWidget(self._apply_btn)
        self._discard_btn = theme.push_button("取消", "ghost")
        self._discard_btn.clicked.connect(self._controller.cancel)
        self._discard_btn.hide()
        actions.addWidget(self._discard_btn)
        actions.addStretch()
        lay.addLayout(actions)

    # -- 状态 ---------------------------------------------------------

    def start(self, anchor_rect, width, instruction=""):
        """显示输入条并定位到光标下方。"""
        self._instruction = instruction
        self._apply_style()
        self._diff.hide()
        self._apply_btn.hide()
        self._discard_btn.hide()
        self._hint.hide()
        self._send.show()
        self._input.setEnabled(True)
        self._input.clear()
        self.resize(width, 46)
        self._place(anchor_rect, width, 46)
        self.show()
        self.raise_()
        self._input.setFocus(Qt.OtherFocusReason)

    def _place(self, anchor, width, height):
        view = self._editor.viewport()
        x = max(8, min(anchor.left(), view.width() - width - 8))
        y = anchor.bottom() + 6
        if y + height > view.height() - 8:
            y = max(8, anchor.top() - height - 6)
        self.move(x, y)
        self.resize(width, height)

    def set_busy(self, busy):
        self._input.setEnabled(not busy)
        self._send.setEnabled(not busy)
        if busy:
            self._hint.setText("正在生成…")
            self._hint.show()
        else:
            self._hint.hide()

    def show_diff(self, old_text, new_text):
        """把结果渲染成 diff，并让浮条长高到能看全。"""
        self._old_text = old_text
        self._diff_text = new_text
        try:
            self._diff.setHtml(_diff_html(old_text, new_text, "当前代码"))
        except Exception:
            self._diff.setPlainText(new_text)
        self._diff.setVisible(True)
        self._apply_btn.show()
        self._discard_btn.show()
        self._send.hide()
        self._input.setEnabled(False)
        if not self._input.text():
            self._input.setText(self._instruction or "")
        self._hint.setText("回车或点「应用」写入编辑器，Esc 取消")
        self._hint.show()

        view = self._editor.viewport()
        width = self.width()
        rows = min(16, max(4, len(new_text.splitlines()) + 2))
        height = 46 + 26 + rows * 17
        height = min(height, max(200, view.height() - 40))
        anchor = self._editor.cursorRect()
        self._place(anchor, width, height)

    def show_error(self, message):
        self._hint.setText("失败：{0}".format(message))
        self._hint.show()
        self.set_busy(False)

    def keyPressEvent(self, event):  # noqa: N802 - Qt 接口
        if event.key() == Qt.Key_Escape:
            self._controller.cancel()
            return
        super().keyPressEvent(event)

    # -- 交互 ---------------------------------------------------------

    def _submit(self):
        if not self._input.isEnabled():
            return
        text = self._input.text().strip() or "为这段代码添加注释"
        self._controller.request(text)


class InlineEditController(QObject):
    """把一个编辑器接到 Ctrl+K 上。"""

    def __init__(self, editor, parent=None):
        super().__init__(parent)
        self._editor = editor
        self._popup = InlineEditPopup(editor, self)
        self._worker = None
        self._range = None      # (start_block, end_block)
        self._source = ""
        editor.installEventFilter(self)
        editor.viewport().installEventFilter(self)
        # 浮条是钉在视口坐标上的，编辑器一滚动就会飘到错的行上。
        # 还没出 diff 时直接收起；已经在看 diff 时保持不动（用户可能在
        # 上下滚动对照代码，收掉反而更糟）。
        editor.verticalScrollBar().valueChanged.connect(self._on_scrolled)
        # 浮条是钉在视口坐标上的，编辑器一滚动就会飘到错的行上。
        # 还没出 diff 时直接收起；已经在看 diff 时保持不动（用户可能在
        # 上下滚动对照代码，收掉反而更糟）。
        editor.verticalScrollBar().valueChanged.connect(self._on_scrolled)

    # -- 生命周期 -----------------------------------------------------

    def detach(self):
        try:
            self._editor.removeEventFilter(self)
            self._editor.viewport().removeEventFilter(self)
        except Exception:
            logger.debug("inline edit detach failed", exc_info=True)
        if self._worker is not None:
            self._worker.wait(1500)
            self._worker = None
        try:
            self._popup.setParent(None)
            self._popup.deleteLater()
        except RuntimeError:
            pass

    def _on_scrolled(self, *_args):
        try:
            if self._popup.isVisible() and not self._popup._diff_text:
                self.cancel()
        except RuntimeError:
            pass

    def _on_scrolled(self, *_args):
        try:
            if self._popup.isVisible() and not self._popup._diff_text:
                self.cancel()
        except RuntimeError:
            pass

    # -- 事件 ---------------------------------------------------------

    def eventFilter(self, obj, event):  # noqa: N802 - Qt 接口
        if obj is self._editor and event.type() == QEvent.KeyPress:
            key = event.key()
            mods = event.modifiers()
            if (key == Qt.Key_K and mods == Qt.ControlModifier):
                self.open()
                return True
            if key == Qt.Key_Escape and self._popup.isVisible():
                self.cancel()
                return True
        elif obj is self._editor.viewport() and \
                event.type() == QEvent.Resize and self._popup.isVisible():
            self._popup.hide()
        return super().eventFilter(obj, event)

    # -- 目标范围 -----------------------------------------------------

    def _target_range(self):
        """返回 ``(cursor, source_text, start_block, end_block)`` 或 None。"""
        editor = self._editor
        try:
            cursor = editor.textCursor()
        except RuntimeError:
            return None
        if cursor.hasSelection():
            start = cursor.selectionStart()
            end = cursor.selectionEnd()
            doc_cursor = QTextCursor(cursor)
            doc_cursor.setPosition(start)
            first = doc_cursor.blockNumber()
            doc_cursor.setPosition(end)
            last = doc_cursor.blockNumber()
            return start, end, first, last, cursor.selectedText().replace(
                "\u2029", "\n")

        block = cursor.block()
        text = block.text()
        # 光标在 def / class 行上：整块取，别只改一行签名
        if re.match(r"\s*(async\s+def|def|class)\s", text):
            span = self._block_span()
            if span:
                first, last = span
                start = editor.document().findBlockByNumber(first).position()
                end_block = editor.document().findBlockByNumber(last)
                end = end_block.position() + len(end_block.text())
                doc_cursor = QTextCursor(editor.document())
                doc_cursor.setPosition(start)
                doc_cursor.setPosition(end, QTextCursor.KeepAnchor)
                return start, end, first, last, doc_cursor.selectedText().replace(
                    "\u2029", "\n")
        start = block.position()
        end = start + len(text)
        return start, end, block.blockNumber(), block.blockNumber(), text

    def _block_span(self):
        """用 editor_context 的缩进扫描找出当前函数/类的起止行。"""
        try:
            from ..editor_context import capture

            ctx = capture(self._editor, "<inline>", None, "function")
            if ctx is not None and ctx.end_line > ctx.start_line:
                # EditorContext 的行号是 1-based（面板胶囊要给人看），
                # blockNumber 是 0-based，这里必须换算
                return ctx.start_line - 1, ctx.end_line - 1
        except Exception:
            logger.debug("block span detection failed", exc_info=True)
        return None

    # -- 流程 ---------------------------------------------------------

    def open(self):
        target = self._target_range()
        if target is None:
            return
        start, end, first, last, source = target
        if not source.strip():
            return
        lines = source.split("\n")
        if len(lines) > MAX_SOURCE_LINES:
            lines = lines[:MAX_SOURCE_LINES]
            source = "\n".join(lines)
            last = min(last, first + MAX_SOURCE_LINES - 1)
        self._range = (start, end, first, last)
        self._source = source

        view = self._editor.viewport()
        width = max(260, min(560, view.width() - 24))
        self._popup.start(self._editor.cursorRect(), width)

    def request(self, instruction):
        if self._range is None:
            return
        self._instruction = instruction
        self._popup._instruction = instruction
        self._popup.set_busy(True)
        if self._worker is not None:
            self._worker.wait(200)
        self._worker = _EditWorker(instruction, self._source,
                                   self._language(), self)
        self._worker.done.connect(self._on_done)
        self._worker.start()

    def _language(self):
        try:
            plugin = self._editor.window().get_plugin("editor")
            stack = plugin.get_current_editorstack()
            return (stack.get_current_finfo().language or "").lower()
        except Exception:
            return ""

    def _on_done(self, text, error):
        if error:
            self._popup.show_error(error)
            return
        cleaned = _clean_reply(text)
        if not cleaned.strip():
            self._popup.show_error("模型没有返回代码")
            return
        self._popup.set_busy(False)
        self._popup.show_diff(self._source, cleaned)

    def apply(self):
        if self._range is None or not self._popup._diff_text:
            return
        start, end, _first, _last = self._range
        text = self._popup._diff_text
        try:
            cursor = QTextCursor(self._editor.document())
            cursor.setPosition(start)
            cursor.setPosition(end, QTextCursor.KeepAnchor)
            cursor.beginEditBlock()
            cursor.insertText(text)
            cursor.endEditBlock()
            self._editor.setTextCursor(cursor)
        except RuntimeError:
            return
        self._close()

    def cancel(self):
        self._close()

    def _close(self):
        self._range = None
        self._source = ""
        try:
            self._popup.hide()
        except RuntimeError:
            pass
        try:
            self._editor.setFocus(Qt.OtherFocusReason)
        except RuntimeError:
            pass
