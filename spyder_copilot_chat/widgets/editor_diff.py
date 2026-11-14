# -*- coding: utf-8 -*-
"""
editor_diff — 把 Agent 的写盘结果变成编辑器里的「内联 diff 复核」

对标 Cursor / GitHub Copilot Edits 的 inline diff：

- **新增行**：整行绿底（Spyder 原生 ``TextDecoration``，走
  ``editor.decorations`` 管理器，和 Spyder 自己的当前行 / 单元格高亮
  同一套机制，不会被它们的刷新冲掉）。
- **删除行**：原文插回文档，红底 + 删除线 + 红字（这就是 VSCode 家族
  说的 "phantom deleted lines"——Qt 没有 zone widget，把旧文本真实
  插回 buffer 是唯一能滚动能复制的做法；接受时删掉、拒绝时保留）。
- **词级高亮**：成对的删/增行里，只把真正变化的字符段再加一层深色。
- **按钮**：
  - 悬停到某一处修改，行尾浮出 ✓ / ✗（接受 / 撤销这一处）；
  - 视口顶部常驻一条工具栏：「接受修改」「全部接受」「撤销」；
  - 快捷键与 Cursor 一致：Ctrl+Enter 全部接受、Ctrl+Backspace 全部撤销。
- **安全**：staging 前若编辑器有未保存内容则直接放弃（不碰用户
  buffer）；复核期间拦截 Ctrl+S / Ctrl+Z（否则会把「幽灵删除行」存进
  文件或打散块标记）；一切落盘都走 Spyder 自己的 ``EditorStack.save``，
  编码 / 行尾 / 保存偏好全部沿用 IDE 设置。

文件此时已经写盘（工具是先写盘的），所以「接受」= 清掉幽灵行、
「撤销」= 把旧行恢复为正文并回写磁盘——语义上等价 Cursor 的
accept / reject，只是提交点提前了。
"""

from __future__ import annotations

import difflib
import logging
import os

from qtpy.QtCore import QObject, QEvent, Qt, QTimer, Signal
from qtpy.QtGui import QBrush, QColor, QTextBlockUserData, QTextCharFormat, QTextCursor
from qtpy.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QToolButton, QTextEdit, QVBoxLayout,
    QWidget,
)

from . import theme

logger = logging.getLogger(__name__)

try:  # Spyder 运行环境里才有；离屏测试走 ExtraSelections 降级
    from spyder.plugins.editor.api.decoration import TextDecoration
    _HAVE_SPYDER_DECORATION = True
except Exception:  # pragma: no cover
    TextDecoration = None
    _HAVE_SPYDER_DECORATION = False

#: 装饰在 ``editor.decorations`` 里占用的 key（remove_key 一键回收）
_DECO_KEY = "copilot_chat_inline_diff"


def _read_text(path):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read().replace("\r\n", "\n").replace("\r", "\n")
    except Exception:
        return None


def _block_delete(doc, block, cursor):
    """删掉一个块（连同它的换行符）。必须在 beginEditBlock 里调用。

    ``QTextCursor.LineUnderCursor`` 只清文本留下空行，随后的
    ``deleteChar`` 把空行和下一行合并；若它已经是最后一块，则改删
    前面的分隔符，保证不会给文档凭空多出一个行尾换行。
    """
    cursor.setPosition(block.position())
    cursor.setPosition(block.position() + len(block.text()),
                       QTextCursor.KeepAnchor)
    cursor.removeSelectedText()
    if block.blockNumber() < doc.blockCount() - 1:
        cursor.deleteChar()
    elif cursor.position() > 0:
        cursor.deletePreviousChar()


class DiffBlockMark(QTextBlockUserData):
    """挂在 QTextBlock.userData 上的标记：这行是删除行还是新增行。

    必须继承 ``QTextBlockUserData``，否则 Qt 绑定层不接受。

    **但光继承不够**：``QTextBlock.setUserData()`` 在 C++ 侧接管了
    所有权（块销毁时会 delete 它），而 PyQt 这边 sip 仍然认为这个
    对象归 Python —— 于是 ``setUserData(DiffBlockMark(...))`` 这种
    写法里，临时对象在表达式结束就失去最后一个 Python 引用，GC 立刻
    把 C++ 对象也 delete 掉，块里只剩一个悬空指针，等文档销毁时
    就是一次 double free，进程直接崩（表现为退出码 127）。

    解法：把每个 mark 的 Python 引用攥在 ``InlineReview._marks``
    里，等它对应的块真的从文档里消失后再放手（见 ``_prune_marks``）。
    """

    def __init__(self, kind, hunk):
        QTextBlockUserData.__init__(self)
        self.kind = kind      # "del" | "add"
        self.hunk = hunk      # hunk id（int，唯一即可）


class _Hunk(object):
    __slots__ = ("hid", "del_blocks", "add_blocks")

    def __init__(self, hid):
        self.hid = hid
        self.del_blocks = []
        self.add_blocks = []


class ReviewStripe(QWidget):
    """贴在视口上的左侧红绿条纹。只画不收事件。

    注意不能用 ``WA_TranslucentBackground``：offscreen 平台对半透明
    控件的 grab / 合成会直接崩；这里靠 NoSystemBackground 免掉系统
    擦背景，paintEvent 只画条纹，其余像素留给下层的编辑器内容。
    """

    def __init__(self, review):
        super().__init__(review.editor.viewport())
        self._review = review
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WA_NoSystemBackground, True)

    def paintEvent(self, event):  # noqa: N802 - Qt 接口
        review = self._review
        try:
            editor = review.editor
            offset = editor.contentOffset()
            t = theme.current_theme()
            add = QColor(t["diff_add"])
            dele = QColor(t["diff_del"])
            from qtpy.QtCore import QRectF
            from qtpy.QtGui import QPainter
            painter = QPainter(self)
            try:
                painter.setRenderHint(painter.Antialiasing)
                height = self.height()
                for hunk in review.hunks():
                    for blocks, color in ((hunk.add_blocks, add),
                                          (hunk.del_blocks, dele)):
                        for block in blocks:
                            geom = editor.blockBoundingGeometry(block)
                            if not geom.isValid():
                                continue
                            y = geom.translated(offset).top()
                            h = geom.height()
                            if y + h < 0 or y > height:
                                continue
                            painter.fillRect(
                                QRectF(0.0, y + 0.5, 2.6,
                                       max(h - 1.0, 1.0)), color)
            finally:
                painter.end()
        except RuntimeError:
            pass  # 编辑器已销毁


class ReviewBar(QFrame):
    """视口顶部右侧的常驻工具栏：接受修改 / 全部接受 / 撤销。"""

    def __init__(self, review):
        super().__init__(review.editor.viewport())
        self._review = review
        self.setObjectName("inlineDiffBar")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self._apply_style()
        lay = QHBoxLayout(self)
        lay.setContentsMargins(8, 4, 8, 4)
        lay.setSpacing(6)

        self._label = QLabel("")
        self._label.setObjectName("inlineDiffLabel")
        lay.addWidget(self._label)

        self._accept = QToolButton()
        self._accept.setObjectName("diffAcceptBtn")
        self._accept.setText("接受修改")
        self._accept.setToolTip("接受本文件的修改（Ctrl+Enter）")
        self._accept.setCursor(Qt.PointingHandCursor)
        self._accept.clicked.connect(review.accept_all)
        lay.addWidget(self._accept)

        self._accept_global = QToolButton()
        self._accept_global.setObjectName("diffAcceptAllBtn")
        self._accept_global.setText("全部接受")
        self._accept_global.setToolTip("接受所有文件的全部修改")
        self._accept_global.setCursor(Qt.PointingHandCursor)
        self._accept_global.clicked.connect(
            lambda: review.request_accept_all.emit())
        lay.addWidget(self._accept_global)

        self._revert = QToolButton()
        self._revert.setObjectName("diffRevertBtn")
        self._revert.setText("撤销")
        self._revert.setToolTip("撤销本文件的修改（Ctrl+Backspace）")
        self._revert.setCursor(Qt.PointingHandCursor)
        self._revert.clicked.connect(review.revert_all)
        lay.addWidget(self._revert)

    def _apply_style(self):
        t = theme.current_theme()
        fs = max(11, t.ui_font - 2)
        self.setStyleSheet(
            "QFrame#inlineDiffBar {{"
            " background: {bg}; border: 1px solid {bd};"
            " border-radius: 8px; }}"
            "QLabel#inlineDiffLabel {{ color: {fg}; font-size: {fs}px;"
            " border: none; background: transparent; }}"
            "QToolButton {{"
            " background: {btn}; color: {fg}; border: 1px solid {bd};"
            " border-radius: 6px; padding: 2px 10px; font-size: {fs}px; }}"
            "QToolButton:hover {{ border-color: {accent}; }}"
            "QToolButton#diffAcceptBtn {{"
            " background: {fill}; color: #ffffff; border: none; }}"
            "QToolButton#diffAcceptAllBtn {{"
            " background: {addbg}; color: {addfg}; border: 1px solid {add}; }}"
            "QToolButton#diffRevertBtn:hover {{"
            " background: {delbg}; border-color: {delc}; color: {delfg}; }}".format(
                bg=t["elevated"], bd=t["border_strong"], fg=t["text"],
                btn=t["surface_alt"], accent=t["accent"],
                fill=t["accent_fill"], fs=fs,
                add=t["diff_add"], addbg=t["diff_add_bg"],
                addfg=t["diff_add"],
                delbg=t["diff_del_bg"], delfg=t["diff_del"],
                delc=t["diff_del"]))

    def set_counts(self, hunks, plus, minus, global_count):
        parts = []
        if plus:
            parts.append("+{0}".format(plus))
        if minus:
            parts.append("−{0}".format(minus))
        if hunks:
            parts.append("{0} 处修改".format(hunks))
        self._label.setText("  ".join(parts) if parts else "  已处理")
        self._accept_global.setVisible(global_count > 1)
        self.adjustSize()

    def refresh(self):
        self._apply_style()
        self.adjustSize()
        view = self._review.editor.viewport()
        self.move(max(8, view.width() - self.width() - 22), 8)


class InlineReview(QObject):
    """绑定一个编辑器 + 一个文件的内联 diff 复核会话。"""

    #: 会话结束（用户已作出决定并清理现场）
    resolved = Signal(object, str)     # (review, "keep"|"revert"|"mixed")
    #: 用户点了「全部接受」，由插件广播到所有会话
    request_accept_all = Signal()

    def __init__(self, editor, path, old_text, saver=None, parent=None):
        super().__init__(parent)
        self.editor = editor
        self.path = os.path.abspath(path)
        self._old_text = old_text or ""
        self._saver = saver            # callable(editor, path) -> bool
        self._active = False
        self._suspend = False          # staging 期间不响应 contentsChange
        self._bar = None
        self._stripe = None
        self._ok_btn = None
        self._no_btn = None
        self._deco_timer = QTimer(self)
        self._deco_timer.setSingleShot(True)
        self._deco_timer.setInterval(80)
        self._deco_timer.timeout.connect(self._apply_decorations)
        # 攥住所有还活着的 DiffBlockMark 的 Python 引用（见类注释）
        self._marks = set()
        #: staging 失败的原因（成功时为空）。调用方要把它带进聊天流——
        #  「什么都没发生」是最难查的失败，必须留一句话给用户。
        self.fail_reason = ""
        #: 最近一次实际挂上的装饰条数 / 是否走了降级路径（诊断用）
        self.deco_count = 0
        self.fallback_decorations = False

    # ── 现场搭建 ─────────────────────────────────────────────────────

    def stage(self):
        """把 diff 落进编辑器。返回 True 表示预览已生效。

        落块方式：先在内存里把「上下文 + 幽灵删除行 + 新增行」拼成
        最终行序列，``setPlainText`` 一次性写入，再按最终行号逐块打
        userData 标记。**不要**边插入边标记——``QTextBlock`` 的身份
        在插入换行拆块时会漂移，标记会挂到错的行上。
        """
        editor = self.editor
        self.fail_reason = ""
        try:
            doc = editor.document()
        except RuntimeError:
            self.fail_reason = "编辑器对象已失效"
            return False
        # 用户 buffer 里有未保存内容：不碰，回退到纯审批卡流程。
        # 判据不能是「buffer 与磁盘不同」—— 正常复核时磁盘已经是新内容、
        # buffer 还是旧内容，两者必然不同。真正要判的是「buffer 是否还
        # 停在改动前的原文上」：停着说明 modified 只是 Spyder 的内部误报
        # （reload / 编码切换 / 语法补全都会置位），可以放心覆盖。
        try:
            buffer_text = doc.toPlainText()
        except (RuntimeError, AttributeError):
            buffer_text = None
        if doc.isModified():
            same_as_snapshot = (buffer_text is not None
                                and buffer_text.strip("\r\n")
                                == self._old_text.strip("\r\n"))
            if not same_as_snapshot:
                self.fail_reason = "编辑器里还有未保存的修改，暂不干扰"
                return False

        new_text = _read_text(self.path)
        if new_text is None:
            self.fail_reason = "读不到磁盘上的新内容"
            return False
        old_lines = self._old_text.split("\n")
        new_lines = new_text.split("\n")

        self._suspend = True
        try:
            rows = []          # [(kind, text, hid)]，kind ∈ ctx/del/add
            hid = 0
            prev_tag = None
            for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(
                    None, old_lines, new_lines, autojunk=False
                    ).get_opcodes():
                if tag == "equal":
                    rows.extend(("ctx", line, None)
                                for line in old_lines[i1:i2])
                    prev_tag = None
                else:
                    if prev_tag != tag:      # equal 之后 / 变更类型切换
                        hid += 1
                    rows.extend(("del", line, hid)
                                for line in old_lines[i1:i2])
                    rows.extend(("add", line, hid)
                                for line in new_lines[j1:j2])
                    prev_tag = tag
            if not hid:
                self._suspend = False
                self.fail_reason = "新旧内容一致，没有可预览的差异"
                return False

            # setPlainText 会把整个文档重建、所有 QTextBlock 销毁。
            # 上一轮 review 挂在编辑器上的装饰里还留着指向那些旧 block
            # 的 QTextCursor —— 不先摘掉的话，它们就成了悬空引用，
            # 下一次渲染时在 C++ 层直接崩。必须先清装饰再改文档。
            self._clear_decorations()
            editor.setPlainText("\n".join(text for _k, text, _h in rows))
            for number, (kind, _text, h) in enumerate(rows):
                if kind != "ctx":
                    mark = DiffBlockMark(kind, h)
                    doc.findBlockByNumber(number).setUserData(mark)
                    # 必须攥住 Python 引用，否则 mark 会在下一轮 GC 里
                    # 被析构，块里留下悬空指针（见 DiffBlockMark 注释）
                    self._marks.add(mark)
            self._prune_marks()
            doc.setModified(False)
        except Exception as exc:
            logger.debug("inline diff stage failed", exc_info=True)
            self._suspend = False
            self.fail_reason = "写入文档时出错：%s" % (exc,)
            return False

        self._suspend = False
        self._active = True
        self._install_ui()
        # v0.8.3：先把视图滚到第一处修改，再挂装饰。
        #
        # 顺序不能反。Spyder 的 TextDecorationsManager._update() 只把
        # **当前可见行范围**（get_buffer_block_numbers，上下各多半屏）
        # 的装饰推给 setExtraSelections，范围外的直接丢弃；而且这个
        # _update 没有任何外部触发者（update_decorations() 在 6.1 里
        # 无人调用），只在 add_key 时算一次。
        #
        # 旧顺序是：setPlainText（视图回到第 1 行）→ 挂装饰（可见范围
        # = 文件开头）→ 才滚到改动处 ⇒ 改动在 200 行开外时，全部装饰
        # 在过滤阶段就被丢掉，滚过去也是一片空白。这就是「文档确实被
        # 重写了、幽灵行也在，但一点颜色都没有」的成因。
        self._scroll_to_first_hunk()
        self._apply_decorations()
        # 布局落定后可见范围还会再变一次，补一拍刷新
        QTimer.singleShot(0, self._reveal_ui)
        QTimer.singleShot(0, self._refresh_decorations)
        # 切前台 tab / 布局落定都发生在 stage 返回之后：等这一拍过去再
        # 核对一次装饰到底有没有进 setExtraSelections，没进去就自救一遍。
        QTimer.singleShot(350, self._ensure_painted)
        return True

    def _own_selection_count(self):
        """当前 setExtraSelections 里有几条落在本次改动的行上。

        不能用 ``kind`` 认亲：``extraSelections()`` 返回的是 C++ 侧的
        拷贝，PyQt 会把每条重新包成裸 ``ExtraSelection``，我们挂在
        ``TextDecoration`` 上的自定义属性到这一步已经丢了。只能按
        光标位置比对。
        """
        try:
            selections = self.editor.extraSelections()
        except (RuntimeError, AttributeError):
            return 0
        positions = set()
        try:
            for hunk in self.hunks():
                for block in hunk.add_blocks + hunk.del_blocks:
                    if block.isValid():
                        positions.add(block.position())
        except (RuntimeError, AttributeError):
            return 0
        if not positions:
            return 0
        count = 0
        for sel in selections:
            try:
                if sel.cursor.selectionStart() in positions:
                    count += 1
            except RuntimeError:
                continue
        return count

    def _ensure_painted(self):
        """装饰被可见范围过滤掉时自救一次。

        Spyder 的 manager 只保留 ``get_buffer_block_numbers()`` 范围内的
        装饰。staging 结束的那一刻视图可能还挂在后台 tab 上（切前台是
        插件在 stage 之后才做的），范围算出来是空的，装饰就被丢光了。
        这里重滚一次再重挂，把「有幽灵行、有按钮条、就是没颜色」这种
        最难查的失败就地兜住。
        """
        if not self._active or not self.deco_count:
            return
        if self._own_selection_count():
            return
        self._scroll_to_first_hunk()
        self._apply_decorations()
        left = self._own_selection_count()
        if left:
            logger.info("inline diff repainted after stage: %s", self.path)
            return
        try:
            visible = self.editor.get_buffer_block_numbers()
        except (RuntimeError, AttributeError):
            visible = None
        logger.warning(
            "inline diff decorations still hidden (visible range %r, "
            "%d decorations, path=%s)", visible, self.deco_count, self.path)


    def _refresh_decorations(self, *_args):
        """按当前可见范围重算装饰（滚动 / 光标移动时都要重算）。

        装饰的过滤发生在 manager 里，它只保留可见块；滚动之后不重新
        推一次，换到眼前的改动就是没有底色的。
        """
        if not self._active:
            return
        try:
            if TextDecoration is not None and hasattr(self.editor,
                                                      "decorations"):
                self.editor.decorations.update()
            else:
                self._apply_decorations()
        except RuntimeError:
            pass
        except Exception:
            logger.debug("decoration refresh failed", exc_info=True)

    def _scroll_to_first_hunk(self):
        """把视图滚到第一处修改。

        staging 之后文档被整体重写，视图却停在原来的滚动位置；改动如果
        在文件另一头，用户看到的还是一屏普通代码，自然会以为没生效。
        """
        if not self._active:
            return
        try:
            for hunk in self.hunks():
                first = (hunk.del_blocks or hunk.add_blocks or [None])[0]
                if first is not None and first.isValid():
                    cursor = QTextCursor(first)
                    self.editor.setTextCursor(cursor)
                    # centerCursor 比 ensureCursorVisible 更好：后者只保证
                    # 「露一个边」，改动会贴在视口最上/最下一行，而装饰
                    # 的可见范围是「可见行 ± 半屏」，贴边时容易被下一位
                    # 的过滤挤掉。居中则一定落在范围里。
                    try:
                        self.editor.centerCursor()
                    except AttributeError:
                        self.editor.ensureCursorVisible()
                    return
        except (RuntimeError, AttributeError):
            pass

    def _install_ui(self):
        editor = self.editor
        view = editor.viewport()
        self._bar = ReviewBar(self)
        self._stripe = ReviewStripe(self)
        self._stripe.resize(view.width(), view.height())
        # 视口此刻可能还没完成首次布局（离屏 / 刚 open 的文件），立刻
        # show 会在布局竞态里崩掉。等 viewport 收到第一个 Resize（布局
        # 落定）再亮相；在此之前先隐藏挂着。

        # 悬停某一处修改时浮出的 ✓ / ✗
        self._ok_btn = QToolButton(view)
        self._ok_btn.setText("✓")
        self._ok_btn.setToolTip("接受这一处修改")
        self._ok_btn.setCursor(Qt.PointingHandCursor)
        self._ok_btn.setObjectName("hunkOkBtn")
        self._no_btn = QToolButton(view)
        self._no_btn.setText("✕")
        self._no_btn.setToolTip("撤销这一处修改")
        self._no_btn.setCursor(Qt.PointingHandCursor)
        self._no_btn.setObjectName("hunkNoBtn")
        for btn, cb in ((self._ok_btn, self._on_hunk_ok),
                        (self._no_btn, self._on_hunk_no)):
            btn.clicked.connect(cb)
            btn.setStyleSheet(
                "QToolButton {{ background: {bg}; color: {fg};"
                " border: 1px solid {bd}; border-radius: 4px;"
                " font-size: {fs}px; font-weight: 600; padding: 0 4px; }}"
                "QToolButton:hover {{ background: {bg2}; }}".format(
                    bg=theme.current_theme()["elevated"],
                    bg2=(theme.current_theme()["diff_add_bg"]
                         if btn is self._ok_btn
                         else theme.current_theme()["diff_del_bg"]),
                    fg=(theme.current_theme()["diff_add"]
                        if btn is self._ok_btn
                        else theme.current_theme()["diff_del"]),
                    bd=theme.current_theme()["border_strong"],
                    fs=max(10, theme.current_theme().ui_font - 2)))
            btn.hide()

        editor.installEventFilter(self)
        view.installEventFilter(self)
        try:
            bar = editor.verticalScrollBar()
            bar.valueChanged.connect(self._sync_geometry)
            # 滚动 = 换了一批可见行 ⇒ 必须让 manager 重新过滤一次装饰，
            # 否则新滚到眼前的改动没有底色（见 stage() 里的说明）。
            bar.valueChanged.connect(self._refresh_decorations)
        except Exception:
            pass
        try:
            editor.cursorPositionChanged.connect(self._refresh_decorations)
        except Exception:
            pass
        try:
            doc = editor.document()
            doc.contentsChange.connect(self._on_contents_change)
            doc.documentLayout().documentSizeChanged.connect(
                self._sync_geometry)
        except Exception:
            pass
        self._sync_geometry()

    def _reveal_ui(self):
        """viewport 首次 Resize（布局落定）后让工具栏与条纹亮相。"""
        if not self._active:
            return
        try:
            self._bar.show()
            self._stripe.show()
        except RuntimeError:
            pass

    # ── 查询 ────────────────────────────────────────────────────────

    @property
    def is_active(self):
        return self._active

    def _detach_marks(self):
        """把文档里还挂着的 mark 摘干净。

        mark 的 C++ 所有权在 C++ 侧（块析构时释放），PyQt 却认为它归
        Python —— review 被回收时 Python 侧会把它 delete 掉，块里就剩
        一个悬空指针，等文档销毁时就是一次 double free。这里先把
        关联摘掉（``setUserData(None)``，由 C++ 侧释放），再放开
        Python 引用，两边就对齐了。
        """
        if not self._marks:
            return
        try:
            doc = self.editor.document()
            block = doc.firstBlock()
            while block.isValid():
                data = block.userData()
                if isinstance(data, DiffBlockMark):
                    block.setUserData(None)
                block = block.next()
        except (RuntimeError, AttributeError):
            pass
        self._marks = set()

    def _detach_filters(self):
        """把自己从编辑器的 event filter 名单里摘掉。

        Qt 的 ``QObject::~QObject()`` 在析构时会遍历 event filters 并
        回调 ``removeEventFilter(this)``。若 filter（也就是本 review）
        已经被 Python 回收，那次回调就是访问野指针 —— 进程直接崩。
        所以 review 析构时必须自己先摘，名单里就不会有死掉的指针。
        """
        try:
            editor = self.editor
            editor.removeEventFilter(self)
            editor.viewport().removeEventFilter(self)
        except (RuntimeError, AttributeError):
            pass

    def __del__(self):
        # 回收前的最后一道闸：先摘 filter（否则编辑器析构时回调野指针），
        # 再把还挂在文档上的 mark 摘掉。
        try:
            self._detach_filters()
        except Exception:
            pass
        try:
            self._detach_marks()
        except Exception:
            pass
        # 必须把析构链交回 sip：QObject 的 __del__ 负责释放 C++ 侧
        # 对象，覆盖掉不调用 super 只会泄漏，不会报错——更难查。
        try:
            super().__del__()
        except Exception:
            pass

    def _prune_marks(self):
        """重新收集还活着的 mark 的 Python 引用。

        只保留仍挂在文档某块上的 mark；那些块已经被删掉的 mark，
        其 C++ 对象早已随块一起释放，此刻放开 Python 引用是安全的
        （sip 会识别出对象已由 C++ 侧销毁，不会二次 delete）。
        """
        alive = set()
        try:
            doc = self.editor.document()
            block = doc.firstBlock()
        except RuntimeError:
            return
        while block.isValid():
            data = block.userData()
            if isinstance(data, DiffBlockMark):
                alive.add(data)
            block = block.next()
        self._marks = alive

    def hunks(self):
        """按文档顺序返回当前仍标记着的 hunk。"""
        hunks = []
        index = {}
        try:
            doc = self.editor.document()
            block = doc.firstBlock()
        except RuntimeError:
            return hunks
        while block.isValid():
            data = block.userData()
            if isinstance(data, DiffBlockMark):
                hunk = index.get(data.hunk)
                if hunk is None:
                    hunk = _Hunk(data.hunk)
                    index[data.hunk] = hunk
                    hunks.append(hunk)
                if data.kind == "del":
                    hunk.del_blocks.append(block)
                else:
                    hunk.add_blocks.append(block)
            block = block.next()
        return hunks

    def counts(self):
        plus = minus = 0
        for hunk in self.hunks():
            plus += len(hunk.add_blocks)
            minus += len(hunk.del_blocks)
        return plus, minus

    def resolved_text(self):
        try:
            return self.editor.document().toPlainText()
        except RuntimeError:
            return None

    # ── 决定 ────────────────────────────────────────────────────────

    def accept_all(self):
        """接受全部：删掉幽灵删除行，保留新增行。"""
        self._resolve(None, "del")

    def revert_all(self):
        """撤销全部：删掉新增行，把删除行恢复为正文。"""
        self._resolve(None, "add")

    def _on_hunk_ok(self):
        hunk = self._hover_hunk
        if hunk is not None:
            self.accept_hunk(hunk.hid)

    def _on_hunk_no(self):
        hunk = self._hover_hunk
        if hunk is not None:
            self.revert_hunk(hunk.hid)

    def accept_hunk(self, hid):
        self._resolve(hid, "del")

    def revert_hunk(self, hid):
        self._resolve(hid, "add")

    def _resolve(self, hid, remove_kind):
        """统一的决定入口。

        ``remove_kind`` 的块被整块删除、``keep_kind`` 的块还原为普通
        正文。两个关键点：

        - 要删的块**一次倒序**删（倒序保证 index 不漂移），绝不能逐
          hunk 循环——删掉前面的块后，后面 hunk 的 ``QTextBlock``
          对象按 index 引用会指到别的行上；
        - 清 keep 标记必须在删除**之后重扫**文档再做，因为删除让旧
          块对象整体失效，凭旧引用清会把别的行洗掉。
        """
        if not self._active:
            return
        hunks = self.hunks()
        if hid is not None:
            hunks = [h for h in hunks if h.hid == hid]
        if not hunks:
            if hid is None:
                self._finish()
            return
        ids = set(h.hid for h in hunks)
        doc = self.editor.document()

        blocks = []
        for hunk in hunks:
            blocks.extend(hunk.del_blocks if remove_kind == "del"
                          else hunk.add_blocks)
        cursor = QTextCursor(doc)
        cursor.beginEditBlock()
        try:
            self._remove_blocks(blocks, cursor, remove_kind)
        finally:
            cursor.endEditBlock()

        # 块合并会让 userData 归属漂移（删掉一半、另一半被吞并时，
        # 残留标记可能落到保留块上），所以这里按 hunk id 一网打尽：
        # 该 hunk 的决定已经执行，任何残留标记都是合并伪影，一律清除。
        block = doc.firstBlock()
        while block.isValid():
            data = block.userData()
            if isinstance(data, DiffBlockMark) and data.hunk in ids:
                block.setUserData(None)
            block = block.next()

        self._hover_hunk = None
        self._apply_decorations()
        if not self.hunks():
            self._finish()
        elif hid is not None:
            # 部分决定也是一次"已应用"的决定：磁盘必须跟着走，否则
            # 编辑器里的内容和盘上的内容对不上，而 Ctrl+S 又被拦着，
            # 用户既看不到差异提示、也存不下来。
            self._save()
            self._sync_geometry()

    def _find_hunk(self, hid):
        for hunk in self.hunks():
            if hunk.hid == hid:
                return hunk
        return None

    # ── 收尾 ────────────────────────────────────────────────────────

    def _finish(self):
        """按最终内容与旧/新文本的关系判定动作，必要时回写磁盘。"""
        action = "keep"
        text = self.resolved_text()
        new_text = _read_text(self.path)
        if text is not None:
            if new_text is not None and text == new_text:
                action = "keep"
            elif text == self._old_text:
                action = "revert"
            else:
                action = "mixed"
            if new_text is not None and text != new_text:
                self._save()
        try:
            self.editor.document().setModified(False)
        except (RuntimeError, AttributeError):
            pass
        self.clear()
        try:
            self.resolved.emit(self, action)
        except RuntimeError:
            pass

    def _save(self):
        """落盘：优先走 Spyder 的保存管线（编码 / 行尾 / 偏好全沿用）。"""
        if self._saver is not None:
            try:
                if self._saver(self.editor, self.path):
                    return
            except Exception:
                logger.debug("inline diff saver failed", exc_info=True)
        try:
            text = self.editor.document().toPlainText()
            tmp = self.path + ".copilot-tmp"
            with open(tmp, "w", encoding="utf-8", newline="") as f:
                f.write(text)
            os.replace(tmp, self.path)
        except Exception:
            logger.debug("inline diff fallback write failed", exc_info=True)

    def clear(self):
        """只清 UI 与标记，不动文档内容（外部接管时使用）。"""
        self._active = False
        self._clear_decorations()
        for widget in (self._ok_btn, self._no_btn, self._bar, self._stripe):
            if widget is not None:
                try:
                    # **不要 deleteLater**：它把销毁排进事件队列，而
                    # 下面紧接着把 Python 引用置空，PyQt 会当场析构
                    # C++ 对象 —— 队列里那一笔稍后再执行就是对野指针
                    # 调用，进程直接崩。交回 Python 引用、由 GC 统一
                    # 析构才是安全的那条路。
                    widget.hide()
                except RuntimeError:
                    pass
        self._ok_btn = self._no_btn = self._bar = self._stripe = None
        try:
            self.editor.document().contentsChange.disconnect(
                self._on_contents_change)
        except Exception:
            pass
        try:
            self.editor.verticalScrollBar().valueChanged.disconnect(
                self._sync_geometry)
        except Exception:
            pass
        try:
            self.editor.removeEventFilter(self)
            self.editor.viewport().removeEventFilter(self)
        except Exception:
            pass

    # ── 装饰 ────────────────────────────────────────────────────────

    def _clear_decorations(self):
        """摘掉本会话挂在编辑器上的所有装饰。

        装饰里的 ``QTextCursor`` 引用着具体的 ``QTextBlock``，文档一
        重建这些 block 就没了。**任何改文档的动作之前都必须先调它**，
        否则留下一批悬空 cursor，下次渲染就崩。
        """
        editor = self.editor
        try:
            if hasattr(editor, "decorations") and TextDecoration is not None:
                editor.decorations.remove_key(_DECO_KEY)
            else:
                editor.setExtraSelections([])
        except Exception:
            logger.debug("inline diff clear decorations failed",
                         exc_info=True)

    def _on_contents_change(self, *_args):
        if self._suspend or not self._active:
            return
        self._deco_timer.start()

    def _apply_decorations(self):
        """重建全部行级 / 词级装饰（每次都从当前文档现算，天然抗漂移）。"""
        if not self._active:
            return
        t = theme.current_theme()
        dark = t.is_dark
        # v0.8.1：底色直接用主题里那两档精调过的 diff 色，而不是拿
        # diff_add / diff_del 按低 alpha 铺 —— 后者在深色编辑器底上
        # （#19232d 一类的深蓝灰）叠出来是脏青和脏紫，肉眼分不出红绿。
        # 留一点透明度是为了让 Spyder 自己的编辑器背景透上来，不至于
        # 和周围代码割裂成两块硬色块。
        add_bg = QColor(t["diff_add_bg"])
        add_bg.setAlpha(232 if dark else 226)
        del_bg = QColor(t["diff_del_bg"])
        del_bg.setAlpha(232 if dark else 226)
        strong_add = QColor(t["diff_add_strong"])
        strong_del = QColor(t["diff_del_strong"])
        del_fg = QColor(t["diff_del"])

        decos = []
        editor = self.editor
        try:
            doc = editor.document()
        except RuntimeError:
            return

        def _deco(block, start=None, length=None):
            if (TextDecoration is not None
                    and hasattr(editor, "decorations")):
                deco = TextDecoration(block, full_width=length is None,
                                      draw_order=6, kind=_DECO_KEY)
            else:  # 离屏 / 非 Spyder 环境降级
                deco = _FallbackSelection(block)
            if length is not None:
                deco.cursor.setPosition(block.position() + start)
                deco.cursor.setPosition(block.position() + start + length,
                                        QTextCursor.KeepAnchor)
            else:
                deco.cursor.setPosition(block.position())
                deco.cursor.setPosition(
                    block.position() + len(block.text()),
                    QTextCursor.KeepAnchor)
            return deco

        for hunk in self.hunks():
            for block in hunk.add_blocks:
                deco = _deco(block)
                deco.format.setBackground(QBrush(add_bg))
                decos.append(deco)
            for block in hunk.del_blocks:
                deco = _deco(block)
                deco.format.setBackground(QBrush(del_bg))
                deco.format.setForeground(QBrush(del_fg))
                deco.format.setFontStrikeOut(True)
                decos.append(deco)
            # 词级：成对的删/增行里只加深真正变化的字符段
            for del_blk, add_blk in zip(hunk.del_blocks, hunk.add_blocks):
                a, b = del_blk.text(), add_blk.text()
                if not a or not b or a == b:
                    continue
                matcher = difflib.SequenceMatcher(None, a, b,
                                                  autojunk=False)
                for tag, x1, x2, _y1, _y2 in matcher.get_opcodes():
                    if tag == "equal" or x1 == x2:
                        continue
                    deco = _deco(del_blk, x1, x2 - x1)
                    deco.format.setBackground(QBrush(strong_del))
                    decos.append(deco)
                for tag, _x1, _x2, y1, y2 in matcher.get_opcodes():
                    if tag == "equal" or y1 == y2:
                        continue
                    deco = _deco(add_blk, y1, y2 - y1)
                    deco.format.setBackground(QBrush(strong_add))
                    decos.append(deco)

        self.deco_count = len(decos)
        try:
            if hasattr(editor, "decorations") and TextDecoration is not None:
                manager = editor.decorations
                manager.add_key(_DECO_KEY, decos)
                # add_key 只是把刷新排进 QTimer；staging 刚结束，用户正
                # 盯着屏幕等着看，没必要再等那 80ms 的队列——直接催一次。
                try:
                    manager._update()
                except Exception:
                    logger.debug("decoration flush failed", exc_info=True)
            else:
                self.fallback_decorations = True
                editor.setExtraSelections(decos)
        except Exception:
            logger.debug("inline diff decorations failed", exc_info=True)
        self._sync_geometry()

    # ── 几何 / 交互 ──────────────────────────────────────────────────

    _hover_hunk = None

    def _sync_geometry(self):
        if not self._active:
            return
        try:
            view = self.editor.viewport()
        except RuntimeError:
            return
        if self._stripe is not None:
            self._stripe.resize(view.width(), view.height())
            self._stripe.update()
        if self._bar is not None:
            self._bar.refresh()
            plus, minus = self.counts()
            self._bar.set_counts(len(self.hunks()), plus, minus,
                                 self._global_count)
        self._place_hunk_buttons()

    _global_count = 1

    def set_global_count(self, count):
        self._global_count = max(1, int(count))
        if self._bar is not None:
            plus, minus = self.counts()
            self._bar.set_counts(len(self.hunks()), plus, minus,
                                 self._global_count)

    def _place_hunk_buttons(self):
        if self._ok_btn is None:
            return
        hunk = self._hover_hunk
        if hunk is None:
            self._ok_btn.hide()
            self._no_btn.hide()
            return
        try:
            view = self.editor.viewport()
            block = (hunk.del_blocks or hunk.add_blocks)[0]
            rect = self.editor.cursorRect(QTextCursor(block))
            y = rect.top() - 2
            x = view.width() - 96
            if -20 < y < view.height() - 20 and block.isValid():
                for i, btn in enumerate((self._ok_btn, self._no_btn)):
                    btn.move(x + i * 44, y)
                    btn.show()
                    btn.raise_()
            else:
                self._ok_btn.hide()
                self._no_btn.hide()
        except RuntimeError:
            pass

    # ── 文档手术 ────────────────────────────────────────────────────

    def _remove_blocks(self, blocks, cursor, kind=None):
        """从文档里删掉一组块（倒序）。

        ``QTextBlock`` 对象按 index 引用，删除 / 合并会让后面的对象
        失效（``isValid()`` 变 False，被静默跳过——这就是「整文件重
        写」场景多出一行的原因）。因此这里改用**字符 position 快
        照**：倒序删除时更靠前的 position 永不受影响，每删一块都用
        ``findBlock`` 现场重定位，再用 userData 的 kind 验证没有认
        错行。
        """
        try:
            doc = self.editor.document()
        except RuntimeError:
            return
        positions = sorted({b.position() for b in blocks if b.isValid()},
                           reverse=True)
        for pos in positions:
            block = doc.findBlock(pos)
            if not block.isValid():
                continue
            data = block.userData()
            if (kind is not None and not (isinstance(data, DiffBlockMark)
                                          and data.kind == kind)):
                continue    # 已被合并吞掉 / 认错行，宁可漏删不可误删
            _block_delete(doc, block, cursor)

    def _hover_check(self, event):
        try:
            view = self.editor.viewport()
            offset = self.editor.contentOffset()
            y = event.pos().y()
            found = None
            for hunk in self.hunks():
                for block in hunk.del_blocks + hunk.add_blocks:
                    geom = self.editor.blockBoundingGeometry(block)
                    if not geom.isValid():
                        continue
                    top = geom.translated(offset).top()
                    if top <= y <= top + geom.height():
                        found = hunk
                        break
                if found is not None:
                    break
        except RuntimeError:
            found = None
        self._hover_hunk = found
        self._place_hunk_buttons()

    # ── 事件过滤 ────────────────────────────────────────────────────

    def eventFilter(self, obj, event):  # noqa: N802 - Qt 接口
        try:
            if self._active:
                if obj is self.editor and event.type() == QEvent.KeyPress:
                    key = event.key()
                    mods = event.modifiers()
                    if key == Qt.Key_S and mods & Qt.ControlModifier:
                        return True   # 幽灵行还在，禁止存盘
                    if key == Qt.Key_Z and mods & Qt.ControlModifier:
                        return True   # 撤销会打散块标记
                    if (key in (Qt.Key_Return, Qt.Key_Enter) and
                            mods & Qt.ControlModifier):
                        self.accept_all()
                        return True
                    if (key == Qt.Key_Backspace and
                            mods & Qt.ControlModifier):
                        self.revert_all()
                        return True
                elif obj is self.editor.viewport():
                    etype = event.type()
                    if etype == QEvent.MouseMove:
                        self._hover_check(event)
                    elif etype == QEvent.Leave:
                        self._hover_hunk = None
                        self._place_hunk_buttons()
                    elif etype == QEvent.Resize:
                        # Resize 回调里正处于布局中途，此刻 show 子控件
                        # 会在布局重入时崩掉（离屏 grab 必崩）——推迟到
                        # 事件循环的下一拍再亮相
                        QTimer.singleShot(0, self._reveal_ui)
                        QTimer.singleShot(0, self._refresh_decorations)
                        self._sync_geometry()
                    elif etype == QEvent.Show:
                        # 切到这个 tab / 从后台变前台：可见范围整体换了，
                        # 而且后台时 centerCursor 可能没生效，重新定位一次
                        QTimer.singleShot(0, self._reveal_ui)
                        QTimer.singleShot(0, self._scroll_to_first_hunk)
                        QTimer.singleShot(0, self._refresh_decorations)
        except RuntimeError:
            pass
        return super().eventFilter(obj, event)


class _FallbackSelection(QTextEdit.ExtraSelection):
    """非 Spyder 环境的 TextDecoration 替身。

    **必须**真的继承 ``QTextEdit.ExtraSelection``：PyQt5 的
    ``setExtraSelections`` 对鸭子类型的宽容度是假的——传自定义对象
    不一定当场抛 TypeError，坏引用会一直躺到渲染（grab / paint）
    才在 C++ 层崩掉，且没有任何回溯。
    """

    def __init__(self, block):
        QTextEdit.ExtraSelection.__init__(self)
        self.cursor = QTextCursor(block)
        self.format = QTextCharFormat()


class ReviewHub(QObject):
    """插件侧的会话登记处：多文件预览、全局「全部接受」。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._reviews = {}     # path -> InlineReview
        self._notifier = None  # callable(path, action)
        #: 最近一次批量 stage 里失败的文件：[(path, reason), ...]
        self.last_failures = []

    def set_notifier(self, fn):
        self._notifier = fn

    def reviews(self):
        return [r for r in self._reviews.values() if r.is_active]

    def stage(self, editor, path, old_text, saver=None):
        """对某个文件开一场复核；失败返回 None 并记下原因。

        「什么都没发生」是最难查的失败，所以失败不是静默返回 None：
        原因写进 ``last_failures``，由插件带回聊天流给用户看。
        """
        path = os.path.abspath(path)
        old = self._reviews.pop(path, None)
        if old is not None:
            try:
                old.clear()
            except Exception:
                logger.debug("inline diff clear failed", exc_info=True)
        review = InlineReview(editor, path, old_text, saver=saver,
                              parent=self)
        if not review.stage():
            self.last_failures.append(
                (path, review.fail_reason or "未知原因"))
            logger.info("inline diff skipped %s: %s",
                        path, review.fail_reason)
            return None
        review.resolved.connect(self._on_resolved)
        review.request_accept_all.connect(self.accept_all)
        self._reviews[path] = review
        self._broadcast_count()
        return review

    def accept_all(self):
        for review in list(self.reviews()):
            try:
                review.accept_all()
            except Exception:
                logger.debug("accept_all failed", exc_info=True)

    def _on_resolved(self, review, action):
        self._reviews.pop(review.path, None)
        if self._notifier is not None:
            try:
                self._notifier(review.path, action)
            except Exception:
                logger.debug("review notify failed", exc_info=True)
        self._broadcast_count()

    def _broadcast_count(self):
        count = len(self.reviews())
        for review in self.reviews():
            review.set_global_count(count)

    def shutdown(self):
        for review in list(self._reviews.values()):
            try:
                review.clear()
            except Exception:
                pass
        self._reviews.clear()
