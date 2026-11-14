# -*- coding: utf-8 -*-
"""v0.8.15 回归：API 业务错误不再让 Spyder 弹「内部错误」窗口。

现场：选了腾讯混元 Hy4 发一条消息，服务端回「模型不可用」，IDE 直接弹出
「Spyder遇到了内部错误！」的问题报告框，报告正文是
``Chat request failed: 模型「hy4」在 腾讯混元 TokenHub 上不可用``。

那不是进程崩溃。Spyder 6 把 ``sys.stderr`` 重定向到内部控制台，
``spyder/plugins/console/widgets/console.py`` 对 error 流的每一行执行::

    if not is_warning:
        self.sig_exception_occurred.emit(dict(text=line, ...))
        -> main_widget.handle_exception() -> SpyderErrorDialog.show()

logging 的 root / lastResort handler 正好写 stderr，于是插件里一句
``logger.error(...)`` 就等价于「向 IDE 报一个未处理异常」。v0.8.13 只把
traceback 降到 debug，error 级别本身仍在弹；debug 模式下三十多处
``exc_info=True`` 还会输出 ``  File "..."`` 行重新把弹窗勾出来。

这份用例钉住四件事：
1. 复刻 Spyder 的判定，证明旧写法确实会弹、新写法不会弹；
2. ``safe_logging`` 把整个包的日志出口从 stderr 摘走，traceback 只进内存；
3. ``_emit_error`` 永不把异常抛回 Qt 事件循环；
4. TokenHub 402 的「模型服务未开通」不再被误诊成「模型名写错」。

直接跑：``python tests/test_v095.py``（也兼容 pytest）。
"""

import ast
import contextlib
import io
import json
import logging
import os
import re
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from spyder_copilot_chat import api_errors as A  # noqa: E402
from spyder_copilot_chat import safe_logging as SL  # noqa: E402

_results = []


def check(name, condition, extra=""):
    _results.append((name, bool(condition), extra))
    if not condition:
        print("FAIL  {0}  {1}".format(name, extra))


def eq(name, got, want):
    check(name, got == want, "got={0!r} want={1!r}".format(got, want))


# ---------------------------------------------------------------------------
# Spyder 内部控制台的判定，逐行照抄 console.py:243-270
# ---------------------------------------------------------------------------

def _spyder_popup_lines(text):
    """返回会被当成「未处理异常」上报的行。"""
    popped = []
    is_warning = False
    is_traceback = False
    for line in text.splitlines(True):
        if line.startswith("  File") and not line.startswith('  File "<'):
            is_traceback = True
            is_warning = False
        else:
            if (re.findall("[A-Z].*Warning", line) != []
                    or "warnings.warn" in line
                    or "WARNING" in line):
                is_warning = True
        if not is_warning:
            popped.append(line)
    return popped


def _logged(fn):
    """把一次调用的 stdout / stderr 抓成字符串。"""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        fn()
    return out.getvalue(), err.getvalue()


def _pkg_log(name="spyder_copilot_chat.probe"):
    return logging.getLogger(name)


# ---------------------------------------------------------------------------
# 1. 判定器自证：旧写法会弹、新写法不弹
# ---------------------------------------------------------------------------

def test_detector_fixture():
    old_line = ("ERROR:spyder_copilot_chat.widgets.chat_widget:"
                "Chat request failed: 模型「hy4」在 腾讯混元 TokenHub 上不可用\n")
    eq("复刻判定：旧 error 行会弹窗", len(_spyder_popup_lines(old_line)), 1)
    eq("复刻判定：空文本不弹窗", _spyder_popup_lines(""), [])
    eq("复刻判定：WARNING 行不弹窗",
       _spyder_popup_lines("WARNING [spyder_copilot_chat.x] hi\n"), [])
    tb = ("WARNING [x] failed request detail\n"
          "Traceback (most recent call last):\n"
          '  File "a.py", line 1, in <module>\n'
          "ValueError: boom\n")
    check("复刻判定：traceback 的 File 行会重新触发弹窗",
          len(_spyder_popup_lines(tb)) >= 1)


# ---------------------------------------------------------------------------
# 2. safe_logging 的隔离状态
# ---------------------------------------------------------------------------

def test_install_state():
    pkg = logging.getLogger("spyder_copilot_chat")
    check("包加载时已安装隔离", SL.is_installed() is True)
    check("propagate 已断开（不再到 root 的 stderr handler）",
          pkg.propagate is False)
    eq("包级 level 放开到 WARNING", pkg.level, logging.WARNING)
    quiet = [h for h in pkg.handlers
             if type(h).__name__ == "_QuietHandler"]
    eq("只有一个隔离 handler", len(quiet), 1)
    SL.install()
    eq("install 幂等（重复调用不加 handler）",
       len([h for h in pkg.handlers
            if type(h).__name__ == "_QuietHandler"]), 1)
    check("root logger 没被挂上隔离 handler",
          all(type(h).__name__ != "_QuietHandler"
              for h in logging.getLogger().handlers))
    check("无关包的 logger 不受影响",
          logging.getLogger("some_other_package").propagate is True)


def test_init_installs_before_submodules():
    init_src = open(os.path.join(ROOT, "spyder_copilot_chat", "__init__.py"),
                    encoding="utf-8").read()
    check("__init__.py 里主动调用安装", "_install_safe_logging()" in init_src)
    check("安装失败不会拖垮插件 import",
          "except Exception" in init_src)


def test_error_level_never_writes_stderr():
    def call():
        _pkg_log().error(
            "Chat request failed: 模型「hy4」在 腾讯混元 TokenHub 上不可用")
    out, err = _logged(call)
    eq("ERROR 级别不再写 stderr", err, "")
    check("stdout 有内容", bool(out.strip()))
    eq("stdout 恰好一行", len(out.strip().splitlines()), 1)
    check("stdout 以 WARNING 开头", out.startswith("WARNING "))
    check("stdout 里含插件名", "spyder_copilot_chat" in out)
    eq("Spyder 判定不会弹任何行", _spyder_popup_lines(out), [])
    last = SL.diagnostics()[-1]
    eq("诊断缓冲保留原始级别", last["level"], "ERROR")
    check("诊断缓冲保留原文", "模型「hy4」" in last["message"])


def test_traceback_never_reaches_stream():
    def call():
        SL.set_debug(True)
        try:
            try:
                raise ValueError("boom")
            except ValueError:
                _pkg_log().debug("failed request detail", exc_info=True)
            _pkg_log().warning("line one\nline two\nline three")
        finally:
            SL.set_debug(False)
    out, err = _logged(call)
    eq("带 traceback 的 debug 不写 stderr", err, "")
    check("输出不含 Traceback", "Traceback" not in out)
    check('输出不含  File " 行', '  File "' not in out)
    eq("Spyder 判定不会弹任何行", _spyder_popup_lines(out), [])
    check("多行消息被压成单行", "line one line two line three" in out)
    eq("多行消息只占一行输出",
       len([l for l in out.splitlines() if "line one" in l]), 1)
    rows = SL.diagnostics()
    tb_row = [r for r in rows if r["traceback"] and "ValueError" in r["traceback"]]
    check("traceback 仍留在诊断缓冲", bool(tb_row))
    text = SL.diagnostics_text()
    check("diagnostics_text 能导出 traceback", "Traceback" in text)


def test_note_exception_and_bad_record():
    try:
        raise RuntimeError("note me")
    except RuntimeError as exc:
        SL.note_exception(exc, "unit detail")
    last = SL.diagnostics()[-1]
    eq("note_exception 记录级别", last["level"], "DEBUG")
    check("note_exception 存下 traceback", "RuntimeError: note me" in last["traceback"])
    check("diagnostics_text 含该条", "unit detail" in SL.diagnostics_text())

    def bad():
        # 参数不足会让 getMessage() 抛 TypeError；日志系统必须自己兜住
        _pkg_log().warning("bad format %s %s", "only-one")
    out, err = _logged(bad)
    eq("坏 record 不写 stderr", err, "")
    eq("坏 record 不抛异常", _spyder_popup_lines(out), [])

    class BadStream:
        def write(self, text):
            raise OSError("stream closed")

    old = sys.stdout
    sys.stdout = BadStream()
    try:
        _pkg_log().warning("still alive")
        ok = True
    except Exception:
        ok = False
    finally:
        sys.stdout = old
    check("stdout 不可写时 emit 不抛", ok)


def test_echo_off_keeps_diagnostics():
    pkg = logging.getLogger("spyder_copilot_chat")
    h = [x for x in pkg.handlers
         if type(x).__name__ == "_QuietHandler"][0]
    old = h._echo
    h._echo = False
    try:
        out, err = _logged(lambda: _pkg_log().error("silent error"))
    finally:
        h._echo = old
    eq("静默模式 stdout 为空", out, "")
    eq("静默模式 stderr 为空", err, "")
    check("静默模式仍留诊断记录",
          any(r["message"] == "silent error" for r in SL.diagnostics()))


def test_static_guard_no_error_calls():
    """AST 扫描：包内不许再出现 logger.error / logger.exception 调用。

    用 AST 而不是字符串匹配，docstring 里回顾历史的 ``logger.exception``
    字样不会被误判。
    """
    bad = []
    pkg_root = os.path.join(ROOT, "spyder_copilot_chat")
    files = 0
    for dirpath, _, filenames in os.walk(pkg_root):
        for fn in filenames:
            if not fn.endswith(".py"):
                continue
            path = os.path.join(dirpath, fn)
            files += 1
            with open(path, encoding="utf-8") as f:
                tree = ast.parse(f.read(), filename=path)
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                f = node.func
                if (isinstance(f, ast.Attribute)
                        and f.attr in ("error", "exception")
                        and isinstance(f.value, ast.Name)
                        and f.value.id == "logger"):
                    bad.append((os.path.relpath(path, ROOT), node.lineno,
                                f.attr))
    check("扫描覆盖了包内全部 py 文件", files >= 50, "files=%d" % files)
    eq("包内没有 logger.error / logger.exception 调用", bad, [])


def test_all_module_loggers_are_covered():
    """所有自建 logger 都在 spyder_copilot_chat.* 名下，隔离才覆盖得到。"""
    pkg_root = os.path.join(ROOT, "spyder_copilot_chat")
    names = []
    for dirpath, _, filenames in os.walk(pkg_root):
        for fn in filenames:
            if not fn.endswith(".py"):
                continue
            rel = os.path.relpath(os.path.join(dirpath, fn), pkg_root)
            mod = rel[:-3].replace(os.sep, ".")
            if mod.endswith(".__init__"):
                mod = mod[:-9]
            names.append("spyder_copilot_chat" +
                         ("." + mod if mod and mod != "__init__" else ""))
    check("推导出的模块名全部落在隔离命名空间下",
          all(n == "spyder_copilot_chat"
              or n.startswith("spyder_copilot_chat.") for n in names))
    pkg = logging.getLogger("spyder_copilot_chat")
    check("任意后代 logger 的 effective level 由包级控制",
          logging.getLogger("spyder_copilot_chat.widgets.deep.nested")
          .getEffectiveLevel() == pkg.level)


# ---------------------------------------------------------------------------
# 3. TokenHub 402「模型服务未开通」不再被误诊成模型名写错
# ---------------------------------------------------------------------------

def test_hunyuan_402_not_misclassified():
    info = A.classify(
        status=402,
        body='{"error":{"message":"model hy4 does not exist"}}',
        provider="hunyuan", model="hy4")
    eq("402 + does not exist 归为 BALANCE", info.kind, A.BALANCE)
    check("标题点明模型服务未开通", "模型服务未开通" in info.title)
    check("结论提到控制台开通", "控制台开通" in info.summary)
    card = A.card(info)
    check("卡片给出 TokenHub 控制台链接", "[TokenHub 控制台](" in card)
    check("卡片提示先确认 hy4 已开通", "确认 hy4 已开通" in card)

    info404 = A.classify(
        status=404,
        body='{"error":{"message":"model not found"}}',
        provider="hunyuan", model="hy4")
    eq("404 model not found 仍是 MODEL", info404.kind, A.MODEL)
    check("MODEL 结论说清多半是没开通", "控制台还没开通" in info404.summary)
    steps = " ".join(info404.steps)
    check("MODEL 步骤提示开通模型服务", "已开通" in steps)
    check("MODEL 步骤提示地域一致", "地域" in steps)
    check("MODEL 步骤提示 GET /v1/models", "GET /v1/models" in steps)
    card404 = A.card(info404)
    check("MODEL 卡片可点开控制台", "[TokenHub 控制台](" in card404)
    check("MODEL 卡片可点开 Key 页", "[TokenHub API Key](" in card404)

    ds = A.classify(status=402, body='{"error":{"type":"insufficient_balance_error"}}',
                    provider="deepseek", model="deepseek-chat")
    eq("DeepSeek 402 不受影响", ds.kind, A.BALANCE)
    check("DeepSeek 402 不串成 TokenHub 文案", "未开通" not in A.card(ds))
    check("DeepSeek 402 充值链接现在会渲染",
          "[充值页](https://platform.deepseek.com/top_up)" in A.card(ds))
    dsm = A.classify(status=404, body='{"error":{"message":"model not found"}}',
                     provider="deepseek", model="deepseek-chat")
    check("DeepSeek MODEL 不串成 TokenHub 文案",
          "TokenHub 控制台" not in A.card(dsm))
    eq("status_line 仍只取首行",
       A.status_line(A.card(info404)).count("\n"), 0)

    loose = A.classify(
        status=404,
        body='{"error":{"message":"model hy4 not found"}}',
        provider="hunyuan", model="hy4")
    eq("模型名夹在中间的措辞也归 MODEL", loose.kind, A.MODEL)
    check("宽松措辞的卡片同样给开通指引", "已开通" in A.card(loose))
    activated = A.classify(
        status=402,
        body='{"error":{"message":"model service is not activated"}}',
        provider="hunyuan", model="hy4")
    eq("未开通仍优先归 BALANCE，不被 MODEL 正则抢走",
       activated.kind, A.BALANCE)


# ---------------------------------------------------------------------------
# 4. ChatWorker._emit_error 永不冒泡，且真实 402 全程不弹窗
# ---------------------------------------------------------------------------

def _make_worker(cfg=None):
    from qtpy.QtWidgets import QApplication
    from spyder_copilot_chat.widgets.chat_widget import ChatWorker
    app = QApplication.instance() or QApplication([])
    base = {"provider": "hunyuan", "model": "hy4", "api_url": "",
            "api_key": "", "chat_endpoint": "/chat/completions"}
    base.update(cfg or {})
    worker = ChatWorker([{"role": "user", "content": "hi"}], base)
    return app, worker


def test_emit_error_never_raises():
    _, worker = _make_worker()
    seen = {}
    worker.error.connect(lambda p: seen.setdefault("error", p))

    exc = A.ApiError(
        402, '{"error":{"message":"model hy4 does not exist"}}',
        provider="hunyuan", model="hy4")
    out, err = _logged(lambda: worker._emit_error(exc))
    eq("worker 报错不写 stderr", err, "")
    eq("worker 报错不会弹窗", _spyder_popup_lines(out), [])
    check("error 信号收到卡片", A.is_card(seen.get("error", "")))
    check("卡片说清是模型服务未开通", "模型服务未开通" in seen.get("error", ""))

    class Bad:
        def __str__(self):
            raise RuntimeError("bad __str__")

    seen.clear()
    out, err = _logged(lambda: worker._emit_error(Bad()))
    eq("怪异常对象不写 stderr", err, "")
    eq("怪异常对象不会弹窗", _spyder_popup_lines(out), [])
    check("怪异常对象仍发出卡片", A.is_card(seen.get("error", "")))
    check("卡片内容不为空", len(seen.get("error", "")) > 20)

    seen.clear()
    worker._cancel = True
    out, err = _logged(lambda: worker._emit_error(exc))
    eq("取消时不发错误卡片", seen.get("error"), None)
    eq("取消时不写 stderr", err, "")


def test_end_to_end_402_no_popup():
    """本地 mock TokenHub 返回 402，走完整 _request -> run -> error 信号。"""
    payload = json.dumps(
        {"error": {"message": "model hy4 does not exist"}}
    ).encode("utf-8")

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            length = int(self.headers.get("Content-Length") or 0)
            self.rfile.read(length)
            self.send_response(402)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    port = server.server_address[1]
    _, worker = _make_worker({
        "api_url": "http://127.0.0.1:{0}/v1".format(port),
        "api_key": "sk-test", "max_tokens": 1024,
        "thinking_mode": "disabled", "supports_thinking": True,
        "reasoning_effort": "high", "reasoning_efforts": ["high", "no_think"],
        "stream_usage": True, "max_output_tokens": 65536,
        "agent_mode": False, "supports_agent": True,
    })
    seen = {}
    worker.error.connect(lambda p: seen.setdefault("error", p))
    try:
        out, err = _logged(worker.run)
    finally:
        server.shutdown()
        server.server_close()
    eq("真实 HTTP 402 全程不写 stderr", err, "")
    eq("真实 HTTP 402 全程不弹窗", _spyder_popup_lines(out), [])
    check("真实 HTTP 402 发的是错误卡片", A.is_card(seen.get("error", "")))
    check("真实 HTTP 402 归因为未开通 / 余额",
          "模型服务未开通" in seen.get("error", ""))


def main():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
            except Exception as exc:  # noqa: BLE001
                check(name + " 未抛异常", False, repr(exc))
    passed = sum(1 for _, ok, _ in _results if ok)
    total = len(_results)
    failures = [(n, e) for n, ok, e in _results if not ok]
    print("\n{0}/{1} passed".format(passed, total))
    if failures:
        print("failures:")
        for name, extra in failures:
            print("  -", name, extra)
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
