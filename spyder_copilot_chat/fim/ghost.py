# -*- coding: utf-8 -*-
"""
fim.ghost — 编辑器内的灰字补全（ghost text）

对标 VS Code 的 ``InlineCompletionItemProvider``，但 Qt 富文本没有
"在文档里画一段还不存在的文字"这种能力，所以这里自己做三件事：

1. **请求**：用 :class:`spyder_copilot_chat.fim.provider.CopilotFimProvider`
   的 ``request_fim_suggestion``，它在后台线程跑网络，回调排队回主线程；
2. **渲染**：在 ``CodeEditor.viewport()`` 上盖一层透明子控件，用
   ``QPainter`` 把候选文本画在光标之后。灰字**不进文档**，所以不影响
   撤销栈、语法高亮、lint 和光标位置；
3. **接受**：Tab 把文本真正插入（包在一个 ``beginEditBlock`` 里，一次
   撤销即可回到原状），Esc 丢弃。

与 VS Code 的对应关系：

======================  ============================================
VS Code                这里
======================  ============================================
provider 返回文本        ``request_fim_suggestion(text, offset, ...)``
``computeGhostText``    ``_sanitize()``：裁掉与光标后文本重复的前缀
装饰器绘制               ``_GhostOverlay.paintEvent``
``Yielding`` 让位        Spyder 补全弹窗可见时不请求
Tab / Esc               事件过滤器（先于 ``CodeEditor.keyPressEvent``）
======================  ============================================

多行候选只在**下方为空行**时才逐行画：Qt 没有办法为还没插入的文本
预留行高，硬画多行会盖住下面已有的代码。所以多行建议的第一行总是
显示的，后续行能安全展开才展开；Tab 接受的永远是完整文本。
"""

from __future__ import annotations

import logging

from qtpy.QtCore import QEvent, QObject, QRect, Qt, QTimer
from qtpy.QtGui import QColor, QFontMetrics, QPainter, QTextCursor
from qtpy.QtWidgets import QWidget

from .provider import get_fim_provider
from ..widgets import theme

logger = logging.getLogger(__name__)

#: 光标停下多久才请求。比 provider 自己的 150ms 更长一点：灰字要的是
#: "停下来了" 而不是 "每个字符"，否则一次会话会打出几十个请求。
DEBOUNCE_MS = 320

#: 送模型的前后缀窗口（字符）。灰字只需要附近上下文；整份文件既慢又贵。
PREFIX_CHARS = 3000
SUFFIX_CHARS = 800

#: 一次候选最多显示/插入的行数与字符数
MAX_LINES = 12
MAX_CHARS = 1200

#: 下方最多向上"借"多少空行来展示多行候选
MAX_BORROWED_LINES = 8


def _ghost_color():
    """灰字颜色：取主题的 text_faint，再加一点透明度。"""
    try:
        base = QColor(theme.current_theme()["text_faint"])
    except Exception:
        base = QColor("#8a8f98")
    base.setAlpha(190)
    return base


def _trim_suffix_overlap(suggestion, suffix):
    """裁掉候选开头与光标后文本重复的部分。

    模型偶尔会把光标右边的代码再抄一遍（``def foo(self):`` 已经存在，
    候选又以 ``def foo(self):`` 开头）。直接插进去就是重复代码，所以按
    最长公共前缀裁掉——这正是 VS Code ``computeGhostText`` 做的事。
    """
    if not suggestion or not suffix:
        return suggestion
    limit = min(len(suggestion), len(suffix), 80)
    for size in range(limit, 2, -1):
        if suggestion.startswith(suffix[:size]):
            rest = suggestion[size:]
            # 重叠部分以换行结尾时，说明模型把"当前这行"整行抄了一遍：
            # 剩下的内容属于下一行，必须把那个换行补回来，否则插入后会
            # 粘在上一行末尾。
            if suffix[:size].endswith("\n") and not rest.startswith("\n"):
                return "\n" + rest
            return rest
    return suggestion


class _GhostOverlay(QWidget):
    """覆盖在编辑器视口上的一层透明画布，只负责画灰字。"""

    def __init__(self, editor):
        super().__init__(editor.viewport())
        self._editor = editor
        self._text = ""
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WA_NoSystemBackground, True)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setFocusPolicy(Qt.NoFocus)
        self.setGeometry(editor.viewport().rect())
        self.hide()

    # -- 状态 ---------------------------------------------------------

    def set_text(self, text):
        self._text = text or ""
        if self._text:
            self.setGeometry(self._editor.viewport().rect())
            self.show()
            self.raise_()
        else:
            self.hide()
        self.update()

    def text(self):
        return self._text

    def sync_geometry(self):
        if self.parent() is not None:
            self.setGeometry(self._editor.viewport().rect())

    # -- 绘制 ---------------------------------------------------------

    def paintEvent(self, event):  # noqa: N802 - Qt 接口
        if not self._text:
            return
        editor = self._editor
        try:
            cursor = editor.textCursor()
        except RuntimeError:
            return

        painter = QPainter(self)
        try:
            painter.setClipRect(self.rect())
            painter.setFont(editor.font())
            painter.setPen(_ghost_color())
            metrics = QFontMetrics(editor.font())

            anchor = editor.cursorRect(cursor)
            if anchor.isNull():
                return

            lines = self._text.split("\n")
            block = cursor.block()
            base_x = anchor.left()
            for index, line in enumerate(lines):
                if not line:
                    continue
                if index == 0:
                    # 原样画：Tab 接受的是同一段文本，显示与落盘必须一致
                    x, y = base_x, anchor.top()
                else:
                    # 只向下占用"空行"，否则会盖住已有代码
                    target = block
                    for _ in range(index):
                        if target.isValid():
                            target = target.next()
                    if not target.isValid():
                        # 文件末尾之外没有 block，但视口里是空白，可以直接画
                        y = anchor.top() + index * metrics.lineSpacing()
                        if y + anchor.height() > self.height():
                            break
                        x = base_x - metrics.horizontalAdvance(" ") * (
                            cursor.positionInBlock())
                    elif target.text().strip() or index > MAX_BORROWED_LINES:
                        break
                    else:
                        probe = QTextCursor(target)
                        rect = editor.cursorRect(probe)
                        if rect.isNull():
                            break
                        x, y = rect.left(), rect.top()

                # 换行后的候选行没有首列缩进，这里让它们贴齐第一行
                if index > 0 and line[:1].isspace():
                    lead = len(line) - len(line.lstrip())
                    x += metrics.horizontalAdvance(" ") * lead
                    line = line.lstrip()

                width = self.width() - x - 6
                if width <= 0:
                    break
                text = metrics.elidedText(line, Qt.ElideRight, width)
                painter.drawText(
                    QRect(x, y, width, anchor.height()),
                    int(Qt.AlignLeft | Qt.AlignVCenter), text)
        finally:
            painter.end()


class GhostTextController(QObject):
    """把一个编辑器接到 FIM provider 上，并管理灰字的生命周期。"""

    def __init__(self, editor, parent=None):
        super().__init__(parent)
        self._editor = editor
        self._overlay = _GhostOverlay(editor)
        self._text = ""
        self._generation = 0
        self._inserting = False
        # 上一次请求的 (offset, 文本长度)：内容没变就不重复请求
        self._last_request = None

        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._request)

        self._install_hooks()

    # -- 装配 ---------------------------------------------------------

    def _install_hooks(self):
        editor = self._editor
        editor.textChanged.connect(self._on_text_changed)
        try:
            editor.sig_cursor_position_changed.connect(self._on_cursor_moved)
        except Exception:
            logger.debug("ghost: sig_cursor_position_changed unavailable")
        for bar in (editor.verticalScrollBar(), editor.horizontalScrollBar()):
            bar.valueChanged.connect(self._on_scrolled)
        editor.installEventFilter(self)
        editor.viewport().installEventFilter(self)

    def detach(self):
        """编辑器和 provider 断链（编辑器关闭 / 插件卸载时调用）。"""
        self._timer.stop()
        self._text = ""
        self._generation += 1
        try:
            self._editor.removeEventFilter(self)
            self._editor.viewport().removeEventFilter(self)
            self._editor.textChanged.disconnect(self._on_text_changed)
            self._editor.sig_cursor_position_changed.disconnect(
                self._on_cursor_moved)
        except Exception:
            logger.debug("ghost: detach partially failed", exc_info=True)
        try:
            self._overlay.setParent(None)
            self._overlay.deleteLater()
        except RuntimeError:
            pass

    # -- 事件 ---------------------------------------------------------

    def eventFilter(self, obj, event):  # noqa: N802 - Qt 接口
        etype = event.type()
        if obj is self._editor:
            if etype == QEvent.KeyPress:
                return self._on_key_press(event)
            if etype == QEvent.FocusOut:
                self.clear()
        elif obj is self._editor.viewport():
            if etype == QEvent.Resize:
                self._overlay.sync_geometry()
        return super().eventFilter(obj, event)

    def _on_key_press(self, event):
        if not self._text:
            return False
        key = event.key()
        if key == Qt.Key_Tab:
            self.accept()
            return True
        if key == Qt.Key_Escape:
            self.clear()
            return True
        if key in (Qt.Key_Backspace, Qt.Key_Delete, Qt.Key_Return,
                   Qt.Key_Enter, Qt.Key_Left, Qt.Key_Right, Qt.Key_Up,
                   Qt.Key_Down):
            self.clear()
        return False

    def _on_text_changed(self):
        if self._inserting:
            return
        self.clear()
        self._schedule()

    def _on_cursor_moved(self, *_args):
        if self._inserting:
            return
        self.clear()
        self._schedule()

    def _on_scrolled(self, *_args):
        if self._text:
            self._overlay.update()

    # -- 请求 ---------------------------------------------------------

    def _enabled(self):
        provider = get_fim_provider()
        if provider is None:
            return False
        try:
            return bool(provider.is_fim_enabled())
        except Exception:
            return False

    def _editor_ready(self):
        editor = self._editor
        try:
            if not editor.isVisible() or not editor.hasFocus():
                return False
            if editor.isReadOnly():
                return False
            if editor.textCursor().hasSelection():
                return False
            # 让位给 Spyder 自带的补全弹窗（对标 VS Code 的 Yielding）
            checker = getattr(editor, "is_completion_widget_visible", None)
            if callable(checker) and checker():
                return False
        except RuntimeError:
            return False
        return True

    def _schedule(self):
        if not self._enabled() or not self._editor_ready():
            return
        self._timer.start(DEBOUNCE_MS)

    def _request(self):
        if not self._enabled() or not self._editor_ready():
            return
        provider = get_fim_provider()
        try:
            text = self._editor.toPlainText()
            offset = self._editor.textCursor().position()
        except RuntimeError:
            return
        if not text:
            return

        # 只把窗口内的文本送模型：provider 会再按配置切一次前缀/后缀
        start = max(0, offset - PREFIX_CHARS)
        payload = text[start:offset] + text[offset:offset + SUFFIX_CHARS]
        local_offset = offset - start
        suffix = text[offset:offset + SUFFIX_CHARS]

        key = (local_offset, hash(payload))
        if key == self._last_request:
            return
        self._last_request = key

        self._generation += 1
        generation = self._generation

        def on_result(result, error, _gen=generation, _suffix=suffix):
            if _gen != self._generation:
                return  # 已被更新的输入取代
            if error or not result:
                return
            cleaned = self._sanitize(result, _suffix)
            if cleaned:
                self._show(cleaned, _gen)

        try:
            queued = provider.request_fim_suggestion(
                payload, local_offset, self._language(), on_result)
        except Exception:
            logger.debug("ghost: request failed", exc_info=True)
            return
        if not queued:
            logger.debug("ghost: provider declined request")

    def _language(self):
        try:
            plugin = self._editor.window().get_plugin("editor")
            stack = plugin.get_current_editorstack()
            return (stack.get_current_finfo().language or "").lower()
        except Exception:
            return ""

    # -- 结果处理 -----------------------------------------------------

    @staticmethod
    def _sanitize(text, suffix):
        if not text:
            return ""
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        text = _trim_suffix_overlap(text, suffix)
        # 只去掉**结尾**空行。开头的换行是模型在说"这段代码另起一行"，
        # 删掉它会把新行内容贴到当前行尾部（"if not batch:break"）。
        text = text.rstrip("\n")
        if not text.strip():
            return ""
        lines = text.split("\n")[:MAX_LINES]
        text = "\n".join(lines)
        if len(text) > MAX_CHARS:
            text = text[:MAX_CHARS]
        return text

    def _show(self, text, generation):
        if generation != self._generation:
            return
        self._text = text
        self._overlay.set_text(text)

    # -- 接受 / 丢弃 ---------------------------------------------------

    def accept(self):
        text = self._text
        if not text:
            return False
        self.clear()
        editor = self._editor
        self._inserting = True
        try:
            cursor = editor.textCursor()
            # 一次编辑块：Ctrl+Z 能整段撤销，而不是一行一行退
            cursor.beginEditBlock()
            cursor.insertText(text)
            cursor.endEditBlock()
            editor.setTextCursor(cursor)
        except RuntimeError:
            return False
        finally:
            self._inserting = False
            self._last_request = None
        return True

    def clear(self):
        if not self._text and not self._overlay.isVisible():
            return
        self._text = ""
        self._generation += 1
        # 清掉去重键：用户按 Esc 后又回到同一位置，应该重新请求
        self._last_request = None
        try:
            self._overlay.set_text("")
        except RuntimeError:
            pass

    def refresh_theme(self):
        if self._text:
            self._overlay.update()

    def has_suggestion(self):
        return bool(self._text)
