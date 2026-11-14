# -*- coding: utf-8 -*-
"""
把插件日志从 Spyder 的「内部错误」弹窗链路里摘出来。

Spyder 6 把 ``sys.stderr`` 重定向到内部控制台
(``spyder/plugins/console/widgets/console.py``)，而那个控制台对写进
error 流的**每一行**都会执行::

    if not is_warning:
        self.sig_exception_occurred.emit(dict(text=line, ...))
        -> main_widget.handle_exception() -> SpyderErrorDialog.show()

Python ``logging`` 的 root handler（以及无 handler 时的 lastResort）正好写
stderr，于是插件里一句 ``logger.error("Chat request failed: ...")`` 就会让
IDE 弹出「Spyder遇到了内部错误！」的报告框。用户看到的是崩溃窗口，实际
只是「模型服务没开通」这类业务错误。

v0.8.13 把 traceback 从 ``logger.exception`` 降到了 debug，红字回溯没了，
但 ``error`` 级别本身仍在弹；而且 debug 模式下包里三十多处
``logger.debug(..., exc_info=True)`` 会输出 ``  File "..."`` 行，同样会
把弹窗勾出来（Spyder 遇到 File 行会把 is_warning 重置为 False）。

逐个改日志级别迟早会漏，所以这里接管整个包的出口：给
``spyder_copilot_chat`` 顶层 logger 挂一个专用 handler 并关掉 ``propagate``，
记录就再也到不了 root 的 stderr handler。handler 自己：

* 只写 stdout，并把 levelname 统一显示成 ``WARNING`` —— 即便宿主把
  stdout 也并进 error 流，Spyder 的 ``'WARNING' in line`` 判定也会把它
  当 warning 跳过；
* 强制单行、剥离 traceback。traceback 不丢，存进内存环形缓冲，可用
  :func:`diagnostics_text` 导出；
* 全程 try/except。日志系统自己绝不能变成新的崩溃点。

想彻底静默（连 stdout 都不写）设 ``SPYDER_COPILOT_CHAT_LOG=off``；
诊断信息仍然会留在内存缓冲里。
"""

import logging
import os
import sys
import threading
import traceback
from collections import deque

_LOGGER_NAME = "spyder_copilot_chat"
_MAX_RECORDS = 200
_SAFE_LEVELNAME = "WARNING"

_lock = threading.RLock()
_records = deque(maxlen=_MAX_RECORDS)
_installed = False
_original = None          # (propagate, level)，供 uninstall() 还原
_handler = None


def _one_line(text):
    """压成单行：多行 traceback 绝不允许进入任何输出流。"""
    return " ".join(str(text).split())


class _QuietHandler(logging.Handler):
    """包内唯一出口：不写 stderr、不带 traceback、不触发 Spyder 弹窗。"""

    def __init__(self, echo=None):
        super().__init__(level=logging.DEBUG)
        if echo is None:
            echo = os.environ.get(
                "SPYDER_COPILOT_CHAT_LOG", "stdout").strip().lower() != "off"
        self._echo = bool(echo)

    def emit(self, record):
        try:
            detail = ""
            if record.exc_info:
                try:
                    detail = "".join(
                        traceback.format_exception(*record.exc_info)
                    ).rstrip()
                except Exception:
                    detail = ""
            try:
                message = record.getMessage()
            except Exception:
                message = str(getattr(record, "msg", ""))

            with _lock:
                _records.append({
                    "level": record.levelname,
                    "name": record.name,
                    "message": _one_line(message),
                    "traceback": detail,
                })

            if not self._echo:
                return
            # SAFE_LEVELNAME 是刻意的：万一宿主把 stdout 也标成 error 流，
            # Spyder 的 is_warning 判定靠 'WARNING' in line 就能跳过。
            line = "%s [%s] %s" % (
                _SAFE_LEVELNAME, record.name, _one_line(message))
            stream = getattr(sys, "stdout", None)
            if stream is not None:
                try:
                    stream.write(line + "\n")
                except Exception:
                    pass
        except Exception:
            # 日志失败不许冒泡（QThread 里的异常会被 Spyder 当成崩溃）
            pass


def install(echo=None):
    """安装隔离（幂等）。返回包级 logger。"""
    global _installed, _original, _handler
    with _lock:
        if _installed:
            return logging.getLogger(_LOGGER_NAME)
        pkg = logging.getLogger(_LOGGER_NAME)
        _original = (pkg.propagate, pkg.level)
        _handler = _QuietHandler(echo=echo)
        pkg.addHandler(_handler)
        # root 默认是 ERROR；显式放开到 WARNING，否则 warning 记录在
        # isEnabledFor 阶段就被丢掉，连我们自己的 handler 都收不到。
        pkg.setLevel(logging.WARNING)
        # 关键：断开向 root / lastResort(stderr) 的传播
        pkg.propagate = False
        _installed = True
        return pkg


def uninstall():
    """还原现场，主要给测试与热重载用。"""
    global _installed, _original, _handler
    with _lock:
        if not _installed:
            return
        pkg = logging.getLogger(_LOGGER_NAME)
        if _handler is not None:
            pkg.removeHandler(_handler)
        if _original is not None:
            pkg.propagate, pkg.level = _original
        _installed = False
        _original = None
        _handler = None


def is_installed():
    return _installed


def note(message, detail="", level="INFO"):
    """主动往诊断缓冲写一条（不经过 logging、不碰任何输出流）。"""
    with _lock:
        _records.append({
            "level": level, "name": _LOGGER_NAME,
            "message": _one_line(message), "traceback": str(detail or ""),
        })


def note_exception(exc, message="exception detail"):
    """把异常的 traceback 存进诊断缓冲，而不是打进 stderr。"""
    try:
        detail = "".join(traceback.format_exception(
            type(exc), exc, getattr(exc, "__traceback__", None))).rstrip()
    except Exception:
        detail = "%s: %s" % (type(exc).__name__, exc)
    note(message, detail, level="DEBUG")


def diagnostics():
    """返回最近的诊断记录（副本）。"""
    with _lock:
        return [dict(r) for r in _records]


def diagnostics_text(limit=50):
    """可复制的诊断文本；含但不刷屏 traceback。"""
    rows = diagnostics()[-limit:]
    out = []
    for r in rows:
        out.append("%s [%s] %s" % (r["level"], r["name"], r["message"]))
        if r["traceback"]:
            out.append(r["traceback"])
    return "\n".join(out)


def set_debug(enabled):
    """需要排障时把包内 DEBUG 也收进缓冲（默认只收 WARNING 及以上）。"""
    pkg = logging.getLogger(_LOGGER_NAME)
    pkg.setLevel(logging.DEBUG if enabled else logging.WARNING)
