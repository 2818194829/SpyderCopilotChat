# -*- coding: utf-8 -*-
"""v0.8.13 回归：API 错误必须「说对原因 + 给对动作 + 不吓人」。

背景：用户截图那条
``API 402: {"error":{"message":"Insufficient Balance ..."}}``
被显示成「Copilot 额度已用尽，请稍后重试，或升级订阅」——DeepSeek 欠费
却让人去升 Copilot 订阅，方向完全错；同时原始 JSON 连着 traceback 打进
Spyder 内部控制台，看起来像 IDE 崩了。

这份用例钉住三件事：
1. 分类：402→余额、401→登录、429→限流、模型名错→模型、超长→上下文；
2. 措辞：DeepSeek 的 402 不能说成 Copilot，反之亦然；
3. 形态：GUI 收到的是排版好的卡片，不含裸 JSON、不含 traceback。

直接跑：``python tests/test_v093.py``（也兼容 pytest）。
"""

import json
import os
import sys
import threading
import urllib.error
import socket
from http.server import BaseHTTPRequestHandler, HTTPServer

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from spyder_copilot_chat import api_errors as A  # noqa: E402

DEEPSEEK_402 = {
    "error": {
        "message": "Insufficient Balance (request_id: d5e85940-3619-4ef5-83f5-baaafc43af6c)",
        "type": "insufficient_balance_error",
        "param": None,
        "code": "invalid_request_error",
    }
}

_results = []


def check(name, condition, extra=""):
    _results.append((name, bool(condition), extra))
    if not condition:
        print("FAIL  {0}  {1}".format(name, extra))


def eq(name, got, want):
    check(name, got == want, "got={0!r} want={1!r}".format(got, want))


# ---------------------------------------------------------------------------
# 1. 用户原始报文
# ---------------------------------------------------------------------------

def test_user_payload():
    raw = "API 402: " + json.dumps(DEEPSEEK_402)
    info = A.classify(text=raw, provider="deepseek", model="deepseek-flash")
    eq("402 分类为余额不足", info.kind, A.BALANCE)
    eq("402 状态码从文本里抠出来", info.status, 402)
    check("402 取到真正的错误类型", info.code == "insufficient_balance_error",
          info.code)
    check("402 服务端原文保留", "Insufficient Balance" in info.detail)
    check("402 不可重试", info.retryable is False)
    card = A.card(info)
    check("卡片标注 DeepSeek", "DeepSeek" in card)
    check("卡片给出充值入口", "platform.deepseek.com" in card)
    check("卡片不再提 Copilot 订阅", "Copilot" not in card)
    check("卡片不再说「升级订阅」", "升级订阅" not in card)
    check("卡片不含裸 JSON 花括号", '"error"' not in card and '{"' not in card)
    check("卡片首行带锚点", A.is_card(card))


def test_legacy_runtime_error():
    """旧路径 RuntimeError("API 402: {...}") 也要分类对。"""
    exc = RuntimeError("API 402: " + json.dumps(DEEPSEEK_402))
    info = A.classify(exc, provider="deepseek")
    eq("旧 RuntimeError 也能识别 402", info.kind, A.BALANCE)
    text = A.summary_for(exc, provider="deepseek")
    check("旧入口不再说 Copilot 额度", "Copilot" not in text, text)
    check("旧入口提到余额", "余额" in text, text)


# ---------------------------------------------------------------------------
# 2. 同码不同因：措辞必须跟着 provider 走
# ---------------------------------------------------------------------------

def test_provider_wording():
    copilot = A.classify(status=402, body='{"message":"quota exceeded"}',
                         provider="copilot", model="gpt-4o-mini")
    eq("Copilot 402 也是余额类", copilot.kind, A.BALANCE)
    card = A.card(copilot)
    check("Copilot 402 说订阅", "订阅" in card or "额度" in card, card)
    check("Copilot 402 不拿 DeepSeek 余额当理由",
          "DeepSeek 账户余额" not in card, card)

    deepseek = A.classify(status=402, body=json.dumps(DEEPSEEK_402),
                          provider="deepseek", model="deepseek-chat")
    check("DeepSeek 402 给出充值链接",
          "https://platform.deepseek.com/top_up" in A.card(deepseek))


# ---------------------------------------------------------------------------
# 3. 其它状态码
# ---------------------------------------------------------------------------

def test_other_kinds():
    auth = A.classify(status=401, body='{"error":{"message":"Authentication Fails, '
                                       'Your api key is invalid"}}',
                      provider="deepseek")
    eq("401 → auth", auth.kind, A.AUTH)
    check("401 提示重填 Key", "API Key" in A.card(auth))
    check("401 不可重试", auth.retryable is False)

    limited = A.classify(status=429, body='{"error":{"message":"Rate limit reached"}}',
                         provider="openai")
    eq("429 → 限流", limited.kind, A.RATE_LIMIT)
    check("429 可重试", limited.retryable is True)

    missing = A.classify(status=400,
                         body='{"error":{"message":"Model Not Exist",'
                              '"type":"invalid_request_error"}}',
                         provider="deepseek", model="deepseek-x")
    eq("模型名错 → model", missing.kind, A.MODEL)
    check("模型名错带上模型名", "deepseek-x" in A.summary(missing))
    check("模型名错不自动重试", missing.retryable is False)

    long_ctx = A.classify(status=400, body='{"error":{"message":"This model\'s '
                                            'maximum context length is 65536 tokens"}}',
                          provider="deepseek")
    eq("超长 → context", long_ctx.kind, A.CONTEXT)

    refused = A.classify(status=422, body='{"error":{"message":"content filter triggered"}}',
                         provider="openai")
    eq("422 内容策略 → refused", refused.kind, A.REFUSED)

    server = A.classify(status=503, body="upstream connect error")
    eq("503 → server", server.kind, A.SERVER)
    check("5xx 可重试", server.retryable is True)

    eq("网关 HTML 500 仍是 server",
       A.classify(status=500, body="<html><body>502 Bad Gateway</body></html>").kind,
       A.SERVER)


def test_transport_errors():
    timeouts = A.classify(exc=urllib.error.URLError(socket.timeout("timed out")))
    eq("超时异常", timeouts.kind, A.TIMEOUT)
    refused = A.classify(exc=urllib.error.URLError("Connection refused"))
    eq("连接被拒", refused.kind, A.NETWORK)
    check("网络错误可重试", refused.retryable is True)


# ---------------------------------------------------------------------------
# 4. 脏输入：不能因为「格式化错误」把界面带崩
# ---------------------------------------------------------------------------

def test_dirty_input():
    giant = A.classify(status=400, body="x" * 100000)
    check("超长错误体被截断", len(giant.detail) <= 320, len(giant.detail))
    card = A.card(giant)
    check("超长卡片单行不失控",
          all(len(line) <= 400 for line in card.splitlines()),
          max(len(l) for l in card.splitlines()))

    tags = A.classify(status=502, body="<html><script>alert(1)</script>bad</html>")
    check("HTML 网关页被清掉", "<script" not in tags.detail, tags.detail)
    check("HTML 网关页保留可读文本", "bad" in tags.detail, tags.detail)

    empty = A.classify(status=500, body="")
    eq("空错误体也不炸", empty.kind, A.SERVER)
    check("空错误体卡片仍成立", A.card(empty).startswith(A.CARD_MARK))

    chaos = A.classify(text="\x00\x01garbage\x1f")
    check("控制字符被清掉", "\x00" not in chaos.detail)


def test_api_error_exception():
    exc = A.ApiError(402, json.dumps(DEEPSEEK_402), provider="deepseek",
                     model="deepseek-flash")
    eq("异常带状态码", exc.status, 402)
    text = str(exc)
    check("异常 str 是一行人话", "\n" not in text and "{" not in text, text)
    check("异常 str 提到余额", "余额" in text, text)


def test_status_line():
    card = A.card(A.classify(status=402, body=json.dumps(DEEPSEEK_402),
                             provider="deepseek"))
    line = A.status_line(card)
    check("状态栏文案不提 ** 记号", "**" not in line and "⚠" not in line, line)
    check("状态栏文案不超长", len(line) <= 48, len(line))


# ---------------------------------------------------------------------------
# 5. 端到端：真的发一次 402，看线程发什么
# ---------------------------------------------------------------------------

class _Handler(BaseHTTPRequestHandler):
    body = json.dumps(DEEPSEEK_402).encode("utf-8")
    status = 402

    def do_POST(self):  # noqa: N802 - http.server 约定
        length = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(length)
        self.send_response(self.status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(self.body)))
        self.end_headers()
        self.wfile.write(self.body)

    def log_message(self, *args):  # 静默
        pass


def test_worker_end_to_end():
    try:
        from qtpy.QtWidgets import QApplication
    except Exception as exc:  # pragma: no cover
        print("SKIP worker test (no qtpy): {0}".format(exc))
        return

    app = QApplication.instance() or QApplication([])
    server = HTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    port = server.server_address[1]

    from spyder_copilot_chat.widgets.chat_widget import ChatWorker

    cfg = {
        "provider": "deepseek",
        "api_url": "http://127.0.0.1:{0}".format(port),
        "chat_endpoint": "/chat/completions",
        "api_key": "sk-test",
        "model": "deepseek-flash",
        "temperature": 0.0,
        "max_tokens": 16,
        "agent_mode": False,
        "supports_agent": False,
        "thinking_mode": "disabled",
    }
    worker = ChatWorker([{"role": "user", "content": "hi"}], cfg)
    seen = {}
    worker.error.connect(lambda payload: seen.setdefault("error", payload))
    worker.run()          # 同步跑，线程体本身不依赖事件循环
    server.shutdown()

    payload = seen.get("error", "")
    check("线程发出了错误", bool(payload))
    check("发出来的是卡片", A.is_card(payload), payload[:80])
    check("卡片说余额", "余额" in payload)
    check("卡片没有裸 traceback", "Traceback" not in payload)
    check("卡片没有 Copilot 误诊", "Copilot 额度" not in payload)

    # 直接调 _request 也要能拿到结构化异常
    server2 = HTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=server2.serve_forever, daemon=True).start()
    cfg2 = dict(cfg, api_url="http://127.0.0.1:{0}".format(server2.server_address[1]))
    w2 = ChatWorker([], cfg2)
    try:
        w2._request(cfg2["api_url"] + "/chat/completions", {}, {})
        check("_request 抛 ApiError", False, "没有抛异常")
    except A.ApiError as exc:
        eq("_request 保留状态码", exc.status, 402)
        check("_request 保留错误体", "Insufficient Balance" in exc.body)
    finally:
        server2.shutdown()
    del app


# ---------------------------------------------------------------------------
# 6. 渲染：卡片进 Markdown 渲染器不能翻车
# ---------------------------------------------------------------------------

def test_markdown_render():
    try:
        from spyder_copilot_chat.widgets import theme
        from spyder_copilot_chat.widgets.markdown_renderer import render_markdown
    except Exception as exc:  # pragma: no cover
        print("SKIP render test: {0}".format(exc))
        return
    theme.set_current_theme(theme.resolve_theme("dark"))
    card = A.card(A.classify(status=402, body=json.dumps(DEEPSEEK_402),
                             provider="deepseek", model="deepseek-flash"))
    html = render_markdown(card)
    check("渲染出充值链接", "platform.deepseek.com" in html)
    check("渲染结果含粗体标题", "<b>" in html)
    check("渲染不吞掉「怎么办」", "怎么办" in html)


def test_render_safety_unknown_theme():
    """主题没初始化也不能炸：卡片渲染是错误路径上的最后一步。"""
    from spyder_copilot_chat.widgets.markdown_renderer import render_markdown
    try:
        html = render_markdown(A.card(A.classify(status=401, text="x")))
        check("未知主题下仍能渲染", bool(html))
    except Exception as exc:  # noqa: BLE001
        check("未知主题下仍能渲染", False, repr(exc))


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
