# -*- coding: utf-8 -*-
"""
Spyder Copilot Chat — 主插件

将 GitHub Copilot Chat 移植到 Spyder 6 的可停靠插件。

参考架构：
  - vscode-copilot-chat (Microsoft) 的设计模式
  - Spyder AI Chat Plugin (Maciej Piecko) 的 Spyder 集成方式
"""

import logging
import os

from qtpy.QtCore import Qt, QTimer
from qtpy.QtWidgets import QApplication

from spyder.api.plugins import Plugins, SpyderDockablePlugin
from spyder.api.plugin_registration.decorators import (
    on_plugin_available,
    on_plugin_teardown,
)

from .confpage import CopilotChatConfigPage
from .widgets.chat_widget import CopilotChatWidget
from .auth import get_valid_copilot_token

logger = logging.getLogger(__name__)


class CopilotChatPlugin(SpyderDockablePlugin):
    """
    Spyder Copilot Chat 主插件

    在 Spyder 中注册一个可停靠的 AI 聊天面板，
    支持 GitHub Copilot API 和 OpenAI 兼容 API。
    """

    NAME = "copilot_chat"
    WIDGET_CLASS = CopilotChatWidget
    CONF_SECTION = "copilot_chat"
    CONF_WIDGET_CLASS = CopilotChatConfigPage
    REQUIRES = [Plugins.Preferences]
    OPTIONAL = [
        Plugins.Editor,
        Plugins.Completions,
        Plugins.IPythonConsole,
        Plugins.Projects,
    ]
    TABIFY = [Plugins.Help]

    # ── Plugin metadata ────────────────────────────────────────────────

    @staticmethod
    def get_name():
        return "Copilot Chat"

    @staticmethod
    def get_description():
        return "将 GitHub Copilot Chat 集成到 Spyder 中 — AI 编程助手"

    @classmethod
    def get_icon(cls):
        try:
            import qtawesome as qta
            return qta.icon("mdi.chat-outline")
        except Exception:
            return cls.create_icon("editcopy")

    # ── Lifecycle ──────────────────────────────────────────────────────

    def on_initialize(self):
        """插件初始化"""
        # v0.7.1：编辑器上下文轮询（只在面板可见时工作）
        self._editor_ctx_timer = None
        self._editor_sig = None
        # v0.7.2：编辑器内 AI（灰字补全 + Ctrl+K 改写），跟随当前编辑器
        self._ghost = None
        self._ghost_editor = None
        self._inline_edit = None
        # v0.7.9：编辑器内联 diff 复核（Cursor 式接受 / 撤销）
        self._review_hub = None

        # 执行自动清理过期会话
        try:
            from .widgets.session_manager import load_auto_clean_setting, run_auto_clean
            setting = load_auto_clean_setting()
            if setting and setting != "0":
                deleted = run_auto_clean(setting)
                if deleted:
                    logger.info("Auto-clean deleted %d old sessions", deleted)
        except Exception as e:
            logger.debug("Auto-clean skipped: %s", e)

        logger.debug("CopilotChatPlugin initialized")

    def on_close(self, cancellable=True):
        """插件关闭"""
        self._stop_editor_context_watch()
        self._detach_ghost()
        self._shutdown_review_hub()
        logger.debug("CopilotChatPlugin closed")
        return True

    # ── Preferences ────────────────────────────────────────────────────

    #: 保存原始 ``QSpinBox.setValue``，供插件卸载时恢复
    _spinbox_guard_original = None

    @classmethod
    def _install_spinbox_type_guard(cls):
        """让配置页的整数 spinbox 容忍浮点配置值（0.5.3）。

        Spyder 6.1.8 的 ``create_spinbox`` 只按 ``step`` 的 Python 类型选择
        控件（int → ``QSpinBox``，float → ``QDoubleSpinBox``），但
        ``load_from_conf`` 会把配置中保存的值直接传给 ``setValue``，不做
        类型校验。只要某个配置项是 float 而控件是 ``QSpinBox``（例如旧版
        留下的 ``fim_timeout = 8.0``），就会抛
        ``TypeError: setValue(self, val: int): argument 1 has unexpected
        type 'float'``，中断整个 ``create_dialog``，并连锁触发首选项的
        悬空引用崩溃。

        这里做一次幂等的保守包装：**仅当传入 float 时取整**，其余情况
        原样透传；不修改任何 Spyder 文件，也不改变 int 参数的行为。
        ``QDoubleSpinBox`` 不是 ``QSpinBox`` 的子类，因此不受影响。
        """
        try:
            from qtpy.QtWidgets import QSpinBox

            if getattr(QSpinBox, "_copilot_chat_spinbox_guard", False):
                return
            original = QSpinBox.setValue
            cls._spinbox_guard_original = original

            def guarded_set_value(self, value, _original=original):
                if isinstance(value, float):
                    value = int(round(value))
                return _original(self, value)

            QSpinBox.setValue = guarded_set_value
            QSpinBox._copilot_chat_spinbox_guard = True
            logger.debug("Config spinbox numeric type guard installed")
        except Exception as exc:
            logger.debug("Config spinbox numeric type guard skipped: %s", exc)

    @classmethod
    def _uninstall_spinbox_type_guard(cls):
        """插件卸载时恢复 ``QSpinBox.setValue``。"""
        try:
            from qtpy.QtWidgets import QSpinBox

            if not getattr(QSpinBox, "_copilot_chat_spinbox_guard", False):
                return
            if cls._spinbox_guard_original is not None:
                QSpinBox.setValue = cls._spinbox_guard_original
                cls._spinbox_guard_original = None
            QSpinBox._copilot_chat_spinbox_guard = False
        except Exception as exc:
            logger.debug("Config spinbox numeric type guard restore failed: %s",
                         exc)

    def _install_preferences_guard(self):
        """防止 Spyder 6.1.x 首选项对话框的悬空引用导致 IDE 报错。

        Spyder 的 ``ConfigDialog`` 设置了 ``WA_DeleteOnClose``，容器把实例
        存在 ``PreferencesContainer.dialog`` 里，只靠 ``finished`` 回调中的
        ``self.dialog.disconnect(); self.dialog = None`` 清理。只要该回调没
        走到最后（例如某个配置页 ``apply_changes`` 抛异常、或 PyQt5 的
        ``disconnect()`` 抛 TypeError），``dialog`` 就变成指向已销毁 C++
        对象的 Python 包装；再次打开首选项会在
        ``self.dialog.resize(*self._dialog_size)`` 抛
        ``RuntimeError: wrapped C/C++ object of type ConfigDialog has been deleted``。

        Spyder 自带环境目录对本账户只读，无法修补其源码，因此这里做两层防御：
          1. 每次打开前检测悬空引用并清空，让容器重新创建对话框；
          2. 额外挂一个 finished 回调，保证关闭时引用一定被置空。
        """
        try:
            from spyder.plugins.preferences.widgets.container import (
                PreferencesContainer,
            )
            if getattr(PreferencesContainer, "_copilot_chat_dialog_guard", False):
                return
            original = PreferencesContainer.create_dialog

            def guarded_create_dialog(container, config_pages, config_tabs,
                                      main_window):
                dlg = container.dialog
                if dlg is not None:
                    try:
                        # 访问任意 Qt 方法即可检测底层 C++ 对象是否已销毁
                        dlg.isVisible()
                    except RuntimeError:
                        container.dialog = None
                try:
                    original(container, config_pages, config_tabs, main_window)
                except Exception:
                    # 创建过程若中断，不要留下半初始化的对话框引用
                    container.dialog = None
                    raise
                try:
                    dlg = container.dialog
                    if dlg is not None and not getattr(
                            dlg, "_copilot_chat_finish_guard", False):
                        def clear_reference(*_args):
                            container.dialog = None
                        dlg.finished.connect(clear_reference)
                        dlg._copilot_chat_finish_guard = True
                except Exception:
                    logger.debug("Cannot attach dialog finish guard",
                                 exc_info=True)

            PreferencesContainer.create_dialog = guarded_create_dialog
            PreferencesContainer._copilot_chat_dialog_guard = True
            logger.debug("Preferences dialog guard installed")
        except Exception as exc:
            logger.debug("Preferences dialog guard skipped: %s", exc)

    @on_plugin_available(plugin=Plugins.Preferences)
    def on_preferences_available(self):
        """Preferences 插件就绪"""
        self._install_preferences_guard()
        self._install_spinbox_type_guard()
        self.get_plugin(Plugins.Preferences).register_plugin_preferences(self)

    @on_plugin_teardown(plugin=Plugins.Preferences)
    def on_preferences_teardown(self):
        """Preferences 插件卸载"""
        self._uninstall_spinbox_type_guard()
        self.get_plugin(Plugins.Preferences).deregister_plugin_preferences(self)

    # ── Editor plugin ──────────────────────────────────────────────────

    @on_plugin_available(plugin=Plugins.Editor)
    def on_editor_available(self):
        """编辑器插件就绪"""
        editor = self.get_plugin(Plugins.Editor)
        logger.debug("Editor plugin available")
        self._start_editor_context_watch()

    @on_plugin_teardown(plugin=Plugins.Editor)
    def on_editor_teardown(self):
        """编辑器插件卸载"""
        self._stop_editor_context_watch()
        self._detach_ghost()
        self._shutdown_review_hub()
        logger.debug("Editor plugin teardown")

    # ── IPython Console ───────────────────────────────────────────────

    @on_plugin_available(plugin=Plugins.IPythonConsole)
    def on_console_available(self):
        """控制台就绪"""
        logger.debug("IPython console available")

    @on_plugin_teardown(plugin=Plugins.IPythonConsole)
    def on_console_teardown(self):
        """控制台卸载"""
        pass

    # ── Helper methods ─────────────────────────────────────────────────

    def _get_current_editor(self):
        """获取当前活动的编辑器"""
        try:
            editor = self.get_plugin(Plugins.Editor)
            if editor:
                return editor.get_current_editor()
        except Exception:
            pass
        return None

    def _get_editor_cursor(self):
        """获取编辑器光标位置"""
        editor = self._get_current_editor()
        if editor:
            try:
                cursor = editor.get_cursor()
                return cursor
            except Exception:
                pass
        return None

    def _get_project_root(self):
        """获取项目根目录（无项目时回退到当前文件目录 / cwd）"""
        try:
            projects = self.get_plugin(Plugins.Projects)
            if projects:
                root = projects.get_active_project_path()
                if root:
                    return root
        except Exception:
            pass
        try:
            editor = self.get_plugin(Plugins.Editor)
            stack = editor.get_current_editorstack() if editor else None
            finfo = stack.get_current_finfo() if stack else None
            filename = getattr(finfo, "filename", "") or ""
            if filename and os.path.isabs(filename):
                return os.path.dirname(filename)
        except Exception:
            pass
        return os.getcwd()

    def _get_editor_widget(self):
        """获取编辑器 widget"""
        return self._get_current_editor()

    # ── v0.7.1：编辑器上下文（光标 / 选区） ──────────────────────────

    #: 光标轮询间隔。只在面板可见时读取，成本是一次 textCursor() 调用
    EDITOR_POLL_MS = 400

    def _start_editor_context_watch(self):
        """把 provider 注入面板，并启动光标轮询。

        为什么用轮询而不是信号：Spyder 的 ``sig_editor_cursor_position_changed``
        挂在具体的 ``EditorStack`` 上，拆分 / 关闭 / 切换窗口后引用会失效，
        而 ``QTextCursor`` 的读取本身只是一次 O(1) 调用。这里比较一个轻量
        签名（文件名 + 行列 + 选区 + document revision），只在真的变化时
        才通知面板，编辑器里没动静时零开销。
        """
        try:
            widget = self.get_widget()
            if widget is not None and hasattr(
                    widget, "set_editor_context_provider"):
                widget.set_editor_context_provider(
                    self.capture_editor_context)
            # v0.7.9：Agent 写盘后的编辑器内联 diff 复览
            if widget is not None and hasattr(
                    widget, "set_editor_review_provider"):
                widget.set_editor_review_provider(self.review_files)
        except Exception as exc:
            logger.debug("注入编辑器上下文 provider 失败: %s", exc)

        if getattr(self, "_editor_ctx_timer", None) is None:
            self._editor_ctx_timer = QTimer(self)
            self._editor_ctx_timer.setInterval(self.EDITOR_POLL_MS)
            self._editor_ctx_timer.timeout.connect(self._poll_editor_context)
        self._editor_ctx_timer.start()

    def _stop_editor_context_watch(self):
        timer = getattr(self, "_editor_ctx_timer", None)
        if timer is not None:
            timer.stop()

    def _editor_signature(self):
        try:
            editor = self.get_plugin(Plugins.Editor)
            stack = editor.get_current_editorstack() if editor else None
            codeeditor = stack.get_current_editor() if stack else None
            if codeeditor is None:
                return None
            cursor = codeeditor.textCursor()
            finfo = stack.get_current_finfo()
            return (
                getattr(finfo, "filename", "") or "",
                cursor.blockNumber(),
                cursor.positionInBlock(),
                cursor.selectionStart(),
                cursor.selectionEnd(),
                codeeditor.document().revision(),
            )
        except Exception:
            return None

    def _poll_editor_context(self):
        # 灰字不关心面板是否可见——面板收起来时用户照样在写代码，
        # 所以先同步 ghost 控制器，再做"面板不可见就跳过"的早退。
        self._sync_ghost()

        widget = self.get_widget()
        if widget is None or not widget.isVisible():
            return
        sig = self._editor_signature()
        if sig == getattr(self, "_editor_sig", None):
            return
        self._editor_sig = sig
        panel = getattr(widget, "_panel", None)
        if panel is not None:
            panel.refresh_editor_context()

    # ── v0.7.2：行内灰字补全 ──────────────────────────────────────────

    def _sync_ghost(self):
        """把灰字控制器挂到当前编辑器上（切换文件时重建）。"""
        try:
            editor = self._get_current_editor()
        except Exception:
            editor = None
        if editor is self._ghost_editor and (
                editor is None or self._ghost is not None):
            return
        self._detach_ghost()
        self._ghost_editor = editor
        if editor is None:
            return
        try:
            from .fim.ghost import GhostTextController

            self._ghost = GhostTextController(editor, parent=self)
            logger.debug("Ghost text attached")
        except Exception as exc:
            logger.debug("Ghost text attach failed: %s", exc)
        try:
            from .widgets.inline_edit import InlineEditController

            self._inline_edit = InlineEditController(editor, parent=self)
            logger.debug("Inline edit attached")
        except Exception as exc:
            logger.debug("Inline edit attach failed: %s", exc)

    def _detach_ghost(self):
        edit = getattr(self, "_inline_edit", None)
        self._inline_edit = None
        if edit is not None:
            try:
                edit.detach()
            except Exception:
                logger.debug("Inline edit detach failed", exc_info=True)
        ghost = getattr(self, "_ghost", None)
        self._ghost = None
        self._ghost_editor = None
        if ghost is not None:
            try:
                ghost.detach()
            except Exception:
                logger.debug("Ghost text detach failed", exc_info=True)

    def capture_editor_context(self, mode="auto"):
        """返回当前编辑器的 :class:`EditorContext`；不可用时返回 None。"""
        try:
            from .editor_context import capture

            editor = self.get_plugin(Plugins.Editor)
            stack = editor.get_current_editorstack() if editor else None
            if stack is None:
                return None
            codeeditor = stack.get_current_editor()
            finfo = stack.get_current_finfo()
            filename = getattr(finfo, "filename", "") or ""
            # Spyder 的新建文件叫 untitled0.py：没有绝对路径，也没有磁盘副本
            untitled = bool(getattr(finfo, "newly_created", False)) or not (
                filename and os.path.isabs(filename))
            return capture(codeeditor, filename, self._get_project_root(),
                           mode, untitled=untitled)
        except Exception as exc:
            logger.debug("capture_editor_context failed: %s", exc)
            return None

    # ── v0.7.9：编辑器内联 diff 复核 ───────────────────────────────

    def _review_hub(self):
        """惰性创建复核会话登记处，并把结果通知接回聊天面板。"""
        if getattr(self, "_review_hub", None) is None:
            from .widgets.editor_diff import ReviewHub

            self._review_hub = ReviewHub(self)
            widget = self.get_widget()
            if widget is not None and hasattr(
                    widget, "note_editor_resolution"):
                self._review_hub.set_notifier(widget.note_editor_resolution)
        return self._review_hub

    def _shutdown_review_hub(self):
        hub = getattr(self, "_review_hub", None)
        self._review_hub = None
        if hub is not None:
            try:
                hub.shutdown()
            except Exception:
                logger.debug("review hub shutdown failed", exc_info=True)

    def review_files(self, items):
        """Agent 写盘后：``[(path, old_text), ...]`` 送编辑器内联预览。

        返回 ``{"staged": int, "failures": [(path, reason), ...]}``。
        失败原因要一路带回聊天流：静默跳过意味着用户永远不知道
        为什么编辑器里没出现红绿行（v0.8.1 之前就是这样）。
        """
        result = {"staged": 0, "failures": []}
        if not items:
            return result
        try:
            editor_plugin = self.get_plugin(Plugins.Editor)
            stack = (editor_plugin.get_current_editorstack()
                     if editor_plugin else None)
            if stack is None:
                logger.info("review_files: no editorstack")
                result["failures"] = [(p, "当前没有打开的编辑器窗口")
                                      for p, _t in items]
                return result
            hub = self._review_hub()
            hub.last_failures = []
            last_staged = None
            for path, old_text in items:
                editor = self._editor_for_path(stack, path)
                if editor is None:
                    result["failures"].append((path, "这个文件打不开"))
                    logger.info("review_files: cannot open %s", path)
                    continue
                if hub.stage(editor, path, old_text,
                             saver=self._save_editor_file):
                    result["staged"] += 1
                    last_staged = path
            result["failures"].extend(hub.last_failures)
            # v0.8.3：把复核的文件切到前台。
            # 编辑器里没打开过的文件是 load(set_current=False) 打开的，
            # 它进了后台标签页——红绿行、顶部按钮条全都画在那个看不见
            # 的 tab 里，用户这边就是"什么都没发生"。切过去才算真的生效。
            if last_staged:
                try:
                    stack.set_current_filename(last_staged, focus=False)
                except TypeError:
                    try:
                        stack.set_current_filename(last_staged)
                    except Exception:
                        logger.debug("switch to staged file failed",
                                     exc_info=True)
                except Exception:
                    logger.debug("switch to staged file failed",
                                 exc_info=True)
        except Exception as exc:
            logger.debug("review_files failed", exc_info=True)
            result["failures"].append(("", "staging 异常：%s" % (exc,)))
        return result

    def _editor_for_path(self, stack, path):
        """找到文件对应的 CodeEditor；没打开就加载进来。"""
        target = os.path.normcase(os.path.abspath(path))

        def _find():
            for finfo in getattr(stack, "data", []):
                fn = getattr(finfo, "filename", "") or ""
                if fn and os.path.normcase(os.path.abspath(fn)) == target:
                    return getattr(finfo, "editor", None)
            return None

        editor = _find()
        if editor is not None:
            return editor
        # EditorStack.load() 本来就返回新建的 editor，直接拿它；
        # 只有老版本没有返回值时才回退到重新扫描 data。
        loaded = None
        try:
            loaded = stack.load(path, set_current=False)
        except TypeError:
            try:
                loaded = stack.load(path)
            except Exception:
                logger.debug("editor load failed: %s", path, exc_info=True)
                return None
        except Exception:
            logger.debug("editor load failed: %s", path, exc_info=True)
            return None
        if loaded is not None:
            return loaded
        return _find()

    def _save_editor_file(self, editor, path):
        """复核后落盘：走 Spyder 自己的保存管线（编码 / 行尾 / 偏好）。"""
        try:
            editor_plugin = self.get_plugin(Plugins.Editor)
            stack = (editor_plugin.get_current_editorstack()
                     if editor_plugin else None)
            if stack is None:
                return False
            try:
                index = stack.get_index_from_filename(path)
            except ValueError:
                return False
            return bool(stack.save(index, force=True))
        except Exception:
            logger.debug("save editor file failed: %s", path, exc_info=True)
            return False
