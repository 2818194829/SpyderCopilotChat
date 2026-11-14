# -*- coding: utf-8 -*-
"""v0.8.14 回归：腾讯混元 Hy4（TokenHub）作为一等 Provider 接入。

参考：
  产品文档  https://aistudio.tencent.com/hunyuan/doc-center
  API 用法  TokenHub /document/product/1300/78941（地域域名、Bearer、/v1/models）
  模型清单  TokenHub /document/product/1300/78934（hy4 / hy4-preview，
            1M 上下文、960k 输入 / 64k 输出、看图、Function Calling）
  深度思考  TokenHub /document/product/1300/80637（thinking、
            reasoning_effort 仅 high / no_think）
  思维保留  TokenHub /document/product/1300/81572（带 tools 时默认开启
            Preserved Thinking，必须原样回填 reasoning_content）

这份用例钉住四件事：
1. 注册表 / 配置 / 设置页：Hy4 可选，参数按官方文档给对；
2. 请求体：thinking、reasoning_effort 白名单、64K 输出上限、
   stream_options.include_usage、Bearer、多模态、工具调用、思维回填；
3. SSE：文本、推理内容、工具调用、usage、以及**流式错误帧**不能被吞；
4. 错误卡片：TokenHub 的 402 / 401 不能串成 DeepSeek 或 Copilot 的处置。

直接跑：``python tests/test_v094.py``（也兼容 pytest）。
"""

import json
import os
import sys
import threading
import urllib.error
from http.server import BaseHTTPRequestHandler, HTTPServer

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from spyder_copilot_chat import api_errors as A  # noqa: E402
from spyder_copilot_chat.widgets import settings_dialog as SD  # noqa: E402

BASE_URL = "https://tokenhub.tencentcloudmaas.com/v1"
_results = []


def check(name, condition, extra=""):
    _results.append((name, bool(condition), extra))
    if not condition:
        print("FAIL  {0}  {1}".format(name, extra))


def eq(name, got, want):
    check(name, got == want, "got={0!r} want={1!r}".format(got, want))


# ---------------------------------------------------------------------------
# 1. 注册表与配置
# ---------------------------------------------------------------------------

def test_registry():
    p = SD.PROVIDERS["hunyuan"]
    eq("标签", p["label"], "腾讯混元 Hy4（TokenHub）")
    eq("默认接入地址（广州 /v1）", p["default_url"], BASE_URL)
    eq("聊天端点", p["chat_endpoint"], "/chat/completions")
    eq("默认模型", p["default_model"], "hy4")
    check("模型清单含 hy4 与 preview",
          "hy4" in p["models"] and "hy4-preview" in p["models"])
    check("需要 API Key", p["needs_api_key"] is True and not p["needs_oauth"])
    check("支持 Agent 工具调用", p["supports_agent"] is True)
    check("支持 thinking 显式开关", p["supports_thinking"] is True)
    eq("思考强度白名单", tuple(p["reasoning_efforts"]), ("high", "no_think"))
    eq("单次输出上限", p["max_output_tokens"], 65536)
    eq("上下文窗口 1M", p["context_window"], 1048576)
    eq("输入刊例价", p["price_input_per_million"], 6.0)
    eq("输出刊例价", p["price_output_per_million"], 18.0)
    check("流末尾要 usage", p["stream_usage"] is True)
    check("文档入口是用户给的 Hy AI Studio",
          p["docs"] == "https://aistudio.tencent.com/hunyuan/doc-center")
    check("三个地域都给了备选", len(p["alt_urls"]) == 3)
    check("Key 控制台是 TokenHub", "tokenhub/apikey" in p["key_console"])
    check("Hy4 在可选列表里", "hunyuan" in SD.SELECTABLE_PROVIDER_ORDER)
    check("Hy4 不在未支持列表里", "hunyuan" not in SD.UNSUPPORTED_PROVIDERS)
    for key in SD.SELECTABLE_PROVIDER_ORDER:
        check("可选列表里的 %s 有定义" % key, key in SD.PROVIDERS)


def test_provider_config():
    old = SD.load_config
    try:
        SD.load_config = lambda: {"provider": "hunyuan"}
        cfg = SD.get_provider_config()
        eq("provider", cfg["provider"], "hunyuan")
        eq("api_url", cfg["api_url"], BASE_URL)
        eq("chat_endpoint", cfg["chat_endpoint"], "/chat/completions")
        eq("model", cfg["model"], "hy4")
        eq("上下文窗口", cfg["context_window"], 1048576)
        eq("输入单价", cfg["price_input_per_million"], 6.0)
        eq("输出单价", cfg["price_output_per_million"], 18.0)
        eq("思考能力透传", cfg["supports_thinking"], True)
        eq("思考强度透传", cfg["reasoning_efforts"], ["high", "no_think"])
        eq("usage 开关透传", cfg["stream_usage"], True)
        eq("输出上限透传", cfg["max_output_tokens"], 65536)
        eq("文档透传", cfg["provider_docs"],
           "https://aistudio.tencent.com/hunyuan/doc-center")
        check("Agent 可用", cfg["supports_agent"] is True)
        check("需要 Key", cfg["needs_api_key"] is True)

        # DeepSeek 不能因为这次改动被改坏
        SD.load_config = lambda: {"provider": "deepseek"}
        d = SD.get_provider_config()
        eq("DeepSeek 上下文窗口不变", d["context_window"], 1048576)
        eq("DeepSeek 思考强度不变", d["reasoning_efforts"],
           ["low", "high", "max"])
        eq("DeepSeek 不强迫 include_usage", d["stream_usage"], False)
        eq("DeepSeek 没有 64K 截断", d["max_output_tokens"], 0)

        # 用户显式填过的值优先，不能被 Provider 默认覆盖
        SD.load_config = lambda: {"provider": "hunyuan",
                                  "context_window": 32000,
                                  "price_input_per_million": 1.5}
        u = SD.get_provider_config()
        eq("尊重用户设置的上下文", u["context_window"], 32000)
        eq("尊重用户设置的单价", u["price_input_per_million"], 1.5)
    finally:
        SD.load_config = old


# ---------------------------------------------------------------------------
# 2. 设置页（Qt）
# ---------------------------------------------------------------------------

def test_settings_ui():
    try:
        from qtpy.QtWidgets import QApplication
        from spyder_copilot_chat.widgets import theme
    except Exception as exc:  # pragma: no cover
        print("SKIP settings ui (no Qt): {0}".format(exc))
        return
    app = QApplication.instance() or QApplication([])
    theme.set_current_theme(theme.resolve_theme("dark"))
    old = SD.load_config
    try:
        SD.load_config = lambda: {"provider": "hunyuan", "model": "hy4",
                                  "api_key": "sk-test"}
        dlg = SD.SettingsDialog()
        combo = dlg._provider_combo
        idx = combo.findData("hunyuan")
        check("下拉里有 Hy4", idx >= 0)
        combo.setCurrentIndex(idx)
        eq("切到 Hy4 自动填 API 地址", dlg._api_url_edit.text(), BASE_URL)
        check("API Key 输入框可用", dlg._api_key_edit.isEnabled())
        check("思考模式可用", dlg._thinking_combo.isEnabled())
        eq("模型列表",
            [dlg._model_combo.itemText(i)
             for i in range(dlg._model_combo.count())],
            ["hy4", "hy4-preview"])
        eq("Hy4 思考强度只给合法档位",
            [dlg._effort_combo.itemData(i)
             for i in range(dlg._effort_combo.count())],
            ["high", "no_think"])
        check("API 地址提示写了地域与 Key 不能混用",
              "地域" in dlg._api_url_edit.toolTip()
              and "tokenhub/apikey" in dlg._api_url_edit.toolTip())
        dlg._fill_effort_combo("hunyuan", "low")
        eq("非法档位 low 回落到 high",
           dlg._effort_combo.currentData(), "high")

        combo.setCurrentIndex(combo.findData("deepseek"))
        eq("DeepSeek 仍是 low/high/max",
            [dlg._effort_combo.itemData(i)
             for i in range(dlg._effort_combo.count())],
            ["low", "high", "max"])
        combo.setCurrentIndex(combo.findData("openai"))
        check("OpenAI 不显示思考开关可用",
              not dlg._thinking_combo.isEnabled())
        dlg.close()
    finally:
        SD.load_config = old
        del app


# ---------------------------------------------------------------------------
# 3. 本地 TokenHub 模拟：请求体 + SSE
# ---------------------------------------------------------------------------

class _Handler(BaseHTTPRequestHandler):
    frames = []
    last_body = {}
    last_headers = {}
    last_path = ""
    force_status = 200
    plain_json = False

    def do_POST(self):  # noqa: N802 - http.server 约定
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length)
        try:
            _Handler.last_body = json.loads(raw.decode("utf-8"))
        except Exception:
            _Handler.last_body = {}
        _Handler.last_headers = {k.lower(): v
                                 for k, v in self.headers.items()}
        _Handler.last_path = self.path
        if _Handler.force_status != 200 or _Handler.plain_json:
            data = json.dumps(_Handler.frames[0] if _Handler.plain_json
                              else _Handler.frames).encode("utf-8")
            self.send_response(_Handler.force_status)
            self.send_header("Content-Type", "application/json")
        else:
            chunks = []
            for frame in _Handler.frames:
                chunks.append("data: " + json.dumps(frame, ensure_ascii=False)
                              + "\n\n")
            chunks.append("data: [DONE]\n\n")
            data = "".join(chunks).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):  # 静默
        pass


def _cfg(port, **over):
    cfg = {
        "provider": "hunyuan",
        "api_url": "http://127.0.0.1:{0}/v1".format(port),
        "chat_endpoint": "/chat/completions",
        "api_key": "sk-test-tokenhub",
        "model": "hy4",
        "temperature": 0.0,
        "max_tokens": 8192,
        "agent_mode": False,
        "supports_agent": True,
        "thinking_mode": "disabled",
        "reasoning_effort": "high",
        "supports_thinking": True,
        "reasoning_efforts": ["high", "no_think"],
        "stream_usage": True,
        "max_output_tokens": 65536,
    }
    cfg.update(over)
    return cfg


def _run(frames, messages=None, **over):
    from spyder_copilot_chat.widgets.chat_widget import ChatWorker
    try:
        from qtpy.QtWidgets import QApplication
    except Exception as exc:  # pragma: no cover
        print("SKIP worker test (no Qt): {0}".format(exc))
        return {}, {}, {}, None
    app = QApplication.instance() or QApplication([])
    _Handler.frames = frames
    _Handler.last_body = {}
    server = HTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    cfg = _cfg(server.server_address[1], **over)
    worker = ChatWorker(messages or [{"role": "user", "content": "hi"}], cfg)
    seen = {}
    worker.error.connect(lambda p: seen.setdefault("error", p))
    worker.done.connect(lambda p: seen.setdefault("done", p))
    worker.tool_calls.connect(lambda p: seen.setdefault("tool_calls", p))
    worker.usage.connect(lambda p: seen.setdefault("usage", p))
    try:
        worker.run()
    finally:
        server.shutdown()
        server.server_close()
    return seen, dict(_Handler.last_body), dict(_Handler.last_headers), worker


_TEXT_FRAMES = [
    {"choices": [{"delta": {"reasoning_content": "先想一下"}}]},
    {"choices": [{"delta": {"content": "这是"}}]},
    {"choices": [{"delta": {"content": "答案"}}]},
    {"usage": {"prompt_tokens": 12, "completion_tokens": 4}},
]


def test_request_body_default():
    seen, body, headers, worker = _run(_TEXT_FRAMES)
    eq("回答完整", seen.get("done"), "这是答案")
    eq("Bearer 鉴权", headers.get("authorization"),
       "Bearer sk-test-tokenhub")
    eq("请求路径", _Handler.last_path, "/v1/chat/completions")
    eq("model", body.get("model"), "hy4")
    eq("默认关掉深度思考", body.get("thinking"), {"type": "disabled"})
    check("不传 DeepSeek 的思考强度", "reasoning_effort" not in body)
    eq("TokenHub 要显式 include_usage", body.get("stream_options"),
       {"include_usage": True})
    eq("流式开关", body.get("stream"), True)
    eq("推理内容被收下", worker._reasoning_content, "先想一下")
    eq("usage 有发信号", seen.get("usage", {}).get("prompt_tokens"), 12)
    check("max_tokens 不超过 64K", body.get("max_tokens") <= 65536)


def test_thinking_modes():
    _, body, _, _ = _run(_TEXT_FRAMES, thinking_mode="enabled")
    eq("开启 thinking", body.get("thinking"), {"type": "enabled"})
    eq("合法强度 high 保留", body.get("reasoning_effort"), "high")
    eq("思考时抬到 64K", body.get("max_tokens"), 65536)

    _, body, _, _ = _run(_TEXT_FRAMES, thinking_mode="enabled",
                         reasoning_effort="low")
    eq("Hy4 不认识的 low 回落 high", body.get("reasoning_effort"), "high")

    _, body, _, _ = _run(_TEXT_FRAMES, thinking_mode="enabled",
                         reasoning_effort="no_think")
    eq("no_think 等价关掉思考", body.get("thinking"), {"type": "disabled"})
    check("no_think 不跟 enabled 一起发",
          "reasoning_effort" not in body)

    _, body, _, _ = _run(_TEXT_FRAMES, max_tokens=393216)
    eq("超大的 max_tokens 截到 64K", body.get("max_tokens"), 65536)


def test_agent_tools_and_preserved_thinking():
    msgs = [
        {"role": "user", "content": "读一下 a.py"},
        {"role": "assistant", "content": None,
         "reasoning_content": "上一轮的推理草稿",
         "tool_calls": [{"id": "c0", "type": "function",
                         "function": {"name": "read_file",
                                      "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "c0", "content": "print(1)"},
    ]
    _, body, _, _ = _run(_TEXT_FRAMES, messages=msgs,
                         agent_mode=True, supports_agent=True)
    check("Agent 模式带 tools", isinstance(body.get("tools"), list)
          and len(body["tools"]) > 0)
    sent = body["messages"]
    check("历史 assistant 的 reasoning_content 原样回填",
          any(m.get("reasoning_content") == "上一轮的推理草稿"
              for m in sent if isinstance(m, dict)),
          str(sent)[:200])


def test_multimodal_message():
    """Hy4 支持图片理解：OpenAI image_url 格式要原样过到请求体。"""
    msgs = [{"role": "user", "content": [
        {"type": "text", "text": "这张图是什么"},
        {"type": "image_url",
         "image_url": {"url": "data:image/png;base64,AAAA"}},
    ]}]
    _, body, _, _ = _run(_TEXT_FRAMES, messages=msgs)
    parts = body["messages"][0]["content"]
    check("文本段保留", parts[0].get("type") == "text")
    check("图片段保留 OpenAI 格式",
          parts[1].get("type") == "image_url"
          and parts[1]["image_url"]["url"].startswith("data:image/png"))


def test_sse_tool_calls():
    frames = [
        {"choices": [{"delta": {"reasoning_content": "得先读文件",
                                "tool_calls": [
                                    {"index": 0, "id": "call_0",
                                     "type": "function",
                                     "function": {"name": "read_file",
                                                  "arguments": "{\"path\":"}}]}}]},
        {"choices": [{"delta": {"tool_calls": [
            {"index": 0, "function": {"arguments": "\"a.py\"}"}}]}}]},
    ]
    seen, _, _, worker = _run(frames, agent_mode=True)
    calls = seen.get("tool_calls") or []
    eq("工具调用发出 1 条", len(calls), 1)
    if calls:
        eq("工具名", calls[0]["function"]["name"], "read_file")
        eq("参数拼接完整", calls[0]["function"]["arguments"],
           '{"path":"a.py"}')
    check("有工具调用时不发 done", "done" not in seen)
    eq("推理内容仍被记录", worker._reasoning_content, "得先读文件")


def test_sse_reasoning_alias():
    frames = [{"choices": [{"delta": {"reasoning": "别名推理",
                                      "content": "ok"}}]}]
    seen, _, _, worker = _run(frames)
    eq("普通回答", seen.get("done"), "ok")
    eq("reasoning 别名也收", worker._reasoning_content, "别名推理")


def test_sse_error_frame_not_swallowed():
    """TokenHub 的流式错误在 SSE frame 里，HTTP 是 200。"""
    frames = [{"error": {
        "code": "402003",
        "type": "gateway_error",
        "message": "model service is not activated or insufficient quota",
    }}]
    seen, _, _, _ = _run(frames)
    payload = seen.get("error", "")
    check("流式错误变成错误卡片", A.is_card(payload), payload[:120])
    check("卡片说开通 / 余额", "开通" in payload or "余额" in payload)
    check("卡片指向 TokenHub 控制台",
          "console.cloud.tencent.com" in payload)
    check("流式错误不会被当成空回答", "done" not in seen)
    check("卡片不带裸 JSON", '"error"' not in payload)


def test_legacy_deepseek_cfg():
    """旧配置没带能力字段时仍按 DeepSeek 老行为走。"""
    from spyder_copilot_chat.widgets.chat_widget import ChatWorker
    try:
        from qtpy.QtWidgets import QApplication
    except Exception as exc:  # pragma: no cover
        print("SKIP legacy cfg (no Qt): {0}".format(exc))
        return
    app = QApplication.instance() or QApplication([])
    _Handler.frames = _TEXT_FRAMES
    server = HTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    cfg = {
        "provider": "deepseek",
        "api_url": "http://127.0.0.1:{0}".format(server.server_address[1]),
        "chat_endpoint": "/chat/completions",
        "api_key": "sk-ds",
        "model": "deepseek-flash",
        "max_tokens": 16,
        "temperature": 0.0,
        "agent_mode": False,
        "supports_agent": False,
        "thinking_mode": "enabled",
        "reasoning_effort": "low",
    }
    try:
        ChatWorker([{"role": "user", "content": "hi"}], cfg).run()
    finally:
        server.shutdown()
        server.server_close()
    body = _Handler.last_body
    eq("DeepSeek 仍开 thinking", body.get("thinking"), {"type": "enabled"})
    eq("DeepSeek 的 low 合法", body.get("reasoning_effort"), "low")
    eq("DeepSeek 思考时抬到 64K", body.get("max_tokens"), 65536)
    check("DeepSeek 不强制 include_usage", "stream_options" not in body)
    del app


# ---------------------------------------------------------------------------
# 4. 错误卡片：TokenHub 的 402 / 401 要说自己的处置
# ---------------------------------------------------------------------------

def test_hunyuan_error_cards():
    info = A.classify(
        status=402,
        body=json.dumps({"error": {
            "code": "402003", "type": "gateway_error",
            "message": "model service is not activated or insufficient quota"}}),
        provider="hunyuan", model="hy4")
    eq("402 归为余额 / 未开通", info.kind, A.BALANCE)
    eq("提供方名称", info.provider_label, "腾讯混元 TokenHub")
    card = A.card(info)
    check("卡片说开通模型服务", "开通" in card, card[:160])
    check("卡片给 TokenHub 控制台",
          "console.cloud.tencent.com/tokenhub" in card)
    check("卡片提醒 Key 地域", "地域" in card)
    check("不串成 Copilot 订阅", "升级订阅" not in card)
    check("不串成 DeepSeek 充值页", "platform.deepseek.com" not in card)
    check("不自动重试", info.retryable is False)

    auth = A.classify(status=401,
                      body=json.dumps({"error": {"code": "401002",
                                                 "message": "invalid api key"}}),
                      provider="hunyuan", model="hy4")
    eq("401 归为 Key 问题", auth.kind, A.AUTH)
    card = A.card(auth)
    check("401 提醒地域不匹配", "地域" in card)
    check("401 给 Key 控制台", "tokenhub/apikey" in card)

    model = A.classify(status=400,
                       body=json.dumps({"error": {"message": "model not found"}}),
                       provider="hunyuan", model="hy4-x")
    eq("模型名错归类", model.kind, A.MODEL)
    check("提示用 hy4 / hy4-preview", "hy4-preview" in A.card(model))
    check("不再拿 DeepSeek 的模型名举例",
          "deepseek-chat" not in A.card(model))

    net = A.classify(exc=urllib.error.URLError("connection refused"),
                     provider="hunyuan")
    eq("断网归类", net.kind, A.NETWORK)
    check("网络错误给的是 TokenHub 域名",
          "tokenhub.tencentcloudmaas.com" in A.card(net))

    # 别家不能被这次改动带偏
    ds = A.classify(status=402,
                    body=json.dumps({"error": {
                        "type": "insufficient_balance_error",
                        "message": "Insufficient Balance"}}),
                    provider="deepseek", model="deepseek-flash")
    check("DeepSeek 402 仍给充值页",
          "platform.deepseek.com/top_up" in A.card(ds))


def test_fim_does_not_hit_openai():
    from spyder_copilot_chat.fim import provider as F
    eq("FIM 默认 URL 指向 TokenHub",
       F._OPENAI_DEFAULT_URLS.get("hunyuan"), BASE_URL)
    msg = F._status_message("quota", "hunyuan")
    check("FIM 402 说 TokenHub", "TokenHub" in msg)
    check("FIM 402 不串 Copilot", "Copilot" not in msg)

    # 真的发一次 FIM 补全：检查 URL / 模型 / 关思考
    old = F._load_global_config
    _Handler.frames = [{"choices": [{"message": {"content": "  pass  "}}]}]
    _Handler.force_status = 200
    _Handler.plain_json = True
    server = HTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        F._load_global_config = lambda: {
            "provider": "hunyuan",
            "api_url": "http://127.0.0.1:{0}/v1".format(
                server.server_address[1]),
            "api_key": "sk-test", "model": "hy4",
        }
        text, err = F._fetch_openai_fim(
            {"max_tokens": 128, "temperature": 0.0, "timeout": 5},
            "def f():\n", "\n", "python")
        # 现有实现只 rstrip（保留模型给的前导缩进），别在这里改行为
        eq("FIM 返回补全文本", text, "  pass")
        eq("FIM 无错误", err, "")
        eq("FIM 走 chat/completions", _Handler.last_path,
           "/v1/chat/completions")
        eq("FIM 用 hy4", _Handler.last_body.get("model"), "hy4")
        eq("FIM 关掉深度思考", _Handler.last_body.get("thinking"),
           {"type": "disabled"})
    finally:
        F._load_global_config = old
        _Handler.force_status = 200
        _Handler.plain_json = False
        server.shutdown()
        server.server_close()


def test_vision_hint():
    from spyder_copilot_chat.widgets.attachments import vision_hint
    eq("hy4 是看图模型，不该报警", vision_hint("hy4"), "")
    eq("hy4-preview 同样", vision_hint("hy4-preview"), "")
    check("老的纯文本模型仍会报警",
          "不支持读图" in vision_hint("deepseek-chat"))


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
