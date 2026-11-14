# -*- coding: utf-8 -*-
"""
GitHub Copilot API 客户端
封装对 https://api.githubcopilot.com 的 HTTP 调用

参考 vscode-copilot-chat 的 API 调用方式：
  - Chat Completions: POST /chat/completions
  - Responses API:   POST /responses
  - Messages API:    POST /v1/messages (Anthropic 兼容)
  - Models:          GET  /models
"""

import json
import logging
import urllib.error
import urllib.request

from .copilot_headers import (
    build_chat_headers,
    api_base,
)

logger = logging.getLogger(__name__)

# 兼容旧常量（旧代码/文档可能引用）
COPILOT_API_BASE = "https://api.githubcopilot.com"
DEFAULT_MODEL = "gpt-4o"
DEFAULT_TIMEOUT = 120

#: CAPI 响应里 ``retry-after`` 是 HTTP-date 或秒数，两者都解析
_CAPI_ERROR_HINTS = {
    400: "请求参数错误（400）；若 body 含 off_topic 请换提问方式",
    402: "Copilot 免费额度/配额已用尽（402），可稍后重试或升级订阅",
    404: "模型或端点不存在（404），请检查 model id / 端点路径",
    422: "内容被 Responsible AI 过滤（422），请改写 prompt",
    424: "上游依赖失败（424），可重试",
    429: "请求被限流（429），请按 retry-after 退避后重试",
    466: "客户端/集成 id 被 Copilot 拒绝（466）；不要伪造 dev 构建头",
    499: "服务端取消了请求（499），视为取消，不重试",
}


# ---------------------------------------------------------------------------
# 异常类型（spec §7 错误码语义总表）
# ---------------------------------------------------------------------------

class CopilotApiError(RuntimeError):
    """Copilot API 错误基类（保持 RuntimeError 兼容旧调用方）。"""

    def __init__(self, status, message, body="", headers=None):
        super().__init__(message)
        self.status = status
        self.body = body or ""
        self.headers = headers or {}

    @property
    def retry_after(self):
        """服务端 retry-after（秒）；不存在返回 None。"""
        return _parse_retry_after(self.headers)


class CopilotAuthError(CopilotApiError):
    """401/403：Copilot token 过期或无效 → 应清缓存、重新交换 token 后重试一次。"""


class CopilotQuotaError(CopilotApiError):
    """402：免费额度/配额用尽。"""


class CopilotContentFilterError(CopilotApiError):
    """422：Responsible AI 内容过滤。"""


class CopilotRateLimitError(CopilotApiError):
    """429/503：限流，按 retry-after 退避。"""


class CopilotClientRejectedError(CopilotApiError):
    """466：客户端/集成 id 不被支持。"""


class CopilotCanceledError(CopilotApiError):
    """499：服务端已取消（当取消处理，不重试）。"""


class CopilotServerError(CopilotApiError):
    """5xx：服务端错误，可退避重试。"""


class CopilotNetworkError(RuntimeError):
    """网络层错误（断网/超时/DNS/TLS），与 HTTP 业务状态码无关。"""


def _parse_retry_after(headers):
    """解析 ``retry-after``（秒数或 HTTP-date），失败返回 None。"""
    if not headers:
        return None
    try:
        raw = headers.get("retry-after") or headers.get("Retry-After")
    except AttributeError:
        return None
    if not raw:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        pass
    try:
        from email.utils import parsedate_to_datetime
        dt = parsedate_to_datetime(str(raw))
        if dt is not None:
            import time as _time
            return max(0.0, dt.timestamp() - _time.time())
    except (TypeError, ValueError, OverflowError):
        pass
    return None


def _http_error_body(exc):
    """安全读取 HTTPError 的错误体。"""
    try:
        return exc.read().decode("utf-8", errors="replace")
    except Exception:  # noqa: BLE001 - 读错误体失败不影响主错误
        return ""


def _error_message(body):
    """从错误体里尽量抽出可读 message（GitHub 错误信封 {message,...}）。"""
    if not body:
        return ""
    try:
        data = json.loads(body)
    except (json.JSONDecodeError, TypeError):
        return body[:500]
    if isinstance(data, dict):
        msg = data.get("message") or data.get("error")
        if isinstance(msg, dict):
            msg = msg.get("message") or msg.get("code")
        if msg:
            return str(msg)[:500]
    return body[:500]


def _raise_capi_error(status, body="", headers=None):
    """按 spec §7 把 HTTP 状态码映射为语义化异常。"""
    hint = _CAPI_ERROR_HINTS.get(status, "")
    detail = _error_message(body)
    msg = f"Copilot API HTTP {status}"
    if detail:
        msg += f": {detail}"
    if hint:
        msg += f"（{hint}）"
    if status in (401, 403):
        raise CopilotAuthError(status, msg, body, headers)
    if status == 402:
        raise CopilotQuotaError(status, msg, body, headers)
    if status == 422:
        raise CopilotContentFilterError(status, msg, body, headers)
    if status == 429:
        raise CopilotRateLimitError(status, msg, body, headers)
    if status == 466:
        raise CopilotClientRejectedError(status, msg, body, headers)
    if status == 499:
        raise CopilotCanceledError(status, msg, body, headers)
    if status >= 500:
        raise CopilotServerError(status, msg, body, headers)
    raise CopilotApiError(status, msg, body, headers)


def _absolute_url(path_or_url):
    """允许传相对 path（拼 api_base()）或完整 URL。"""
    if not path_or_url:
        return api_base()
    if path_or_url.startswith(("http://", "https://")):
        return path_or_url
    return f"{api_base()}{path_or_url}"


def _build_headers(copilot_token, extra_headers=None, initiator="user"):
    """构建 CAPI 请求头（向后兼容旧签名 ``_build_headers(token, extra)``）。"""
    headers = build_chat_headers(copilot_token, initiator=initiator)
    if extra_headers:
        headers.update(extra_headers)
    return headers


def _request(method, path, headers, body=None, timeout=DEFAULT_TIMEOUT):
    """发送 HTTP 请求到 Copilot API，返回解析后的 JSON。

    TLS 使用 Python 默认证书校验（正式修复审计 P1-6：不再关闭 SSL 校验）。
    HTTP 错误按 §7 映射为语义化异常。
    """
    url = _absolute_url(path)
    data = json.dumps(body).encode("utf-8") if body is not None else None

    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        error_body = _http_error_body(e)
        logger.warning("Copilot API HTTP %d: %s", e.code, error_body[:500])
        _raise_capi_error(e.code, error_body, e.headers)
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        reason = getattr(e, "reason", e)
        raise CopilotNetworkError(
            f"Network error: 无法连接 Copilot API（{reason}）"
        ) from e

    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        raise CopilotApiError(
            0, f"Invalid JSON response from Copilot API: {e}", body=raw
        ) from e


def _iter_sse(resp):
    """按官方规则解析 SSE（spec §4.5）。

    * 跳过 ``:`` 开头的注释/心跳行（``: ping``）；
    * ``data:`` 后可无空格（``data:{...}``），不能强依赖 ``"data: "``；
    * 跳过 ``event:`` / ``id:`` / ``retry:`` 等非 data 行；
    * ``[DONE]`` 结束；
    * JSON 解析失败只记日志并 continue。
    """
    for raw in resp:
        if isinstance(raw, bytes):
            line = raw.decode("utf-8", errors="replace")
        else:
            line = str(raw)
        line = line.rstrip("\r\n")
        if not line or line.startswith(":"):
            continue
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if payload == "[DONE]":
            return
        if not payload:
            continue
        try:
            obj = json.loads(payload)
        except json.JSONDecodeError:
            logger.debug("SSE parse error: %s", payload[:200])
            continue
        yield obj


def _stream_request(path, headers, body, timeout=DEFAULT_TIMEOUT):
    """发送流式请求，逐块 yield 解析后的 JSON（低层，保留旧函数名）。

    响应对象一定会在 finally 里 close()（审计 P1-3：socket 泄漏）。
    """
    url = _absolute_url(path)
    data = json.dumps(body).encode("utf-8")

    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    resp = None
    try:
        try:
            resp = urllib.request.urlopen(req, timeout=timeout)
        except urllib.error.HTTPError as e:
            error_body = _http_error_body(e)
            logger.warning("Copilot API HTTP %d: %s", e.code, error_body[:500])
            _raise_capi_error(e.code, error_body, e.headers)
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            reason = getattr(e, "reason", e)
            raise CopilotNetworkError(
                f"Network error: 无法连接 Copilot API（{reason}）"
            ) from e
        yield from _iter_sse(resp)
    finally:
        if resp is not None:
            try:
                resp.close()
            except OSError:
                pass


def _messages_have_image(messages):
    """消息里是否含 image_url（带图时 CAPI 要求 Copilot-Vision-Request）。"""
    for msg in messages or []:
        content = msg.get("content") if isinstance(msg, dict) else None
        if isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and part.get("type") == "image_url":
                    return True
    return False


# ---------------------------------------------------------------------------
# Chat Completions (OpenAI 兼容)
# ---------------------------------------------------------------------------

def _finalize_tool_calls(pending):
    """把流式累加器转成标准 tool_calls 列表。"""
    result = []
    for idx in sorted(pending.keys()):
        entry = pending[idx]
        func = entry.setdefault("function", {"name": "", "arguments": ""})
        func.setdefault("name", "")
        func.setdefault("arguments", "")
        if func["arguments"] in (None, ""):
            func["arguments"] = "{}"
        entry.setdefault("id", f"call_{idx}")
        entry.setdefault("type", "function")
        result.append(entry)
    return result


def _merge_tool_call_deltas(pending, tool_calls_delta):
    """按 index 拼接流式 ``delta.tool_calls``（name/arguments 都是增量）。"""
    for tc in tool_calls_delta or []:
        if not isinstance(tc, dict):
            continue
        idx = tc.get("index", 0)
        entry = pending.setdefault(idx, {
            "id": None,
            "type": "function",
            "function": {"name": "", "arguments": ""},
        })
        if tc.get("id"):
            entry["id"] = tc["id"]
        if tc.get("type"):
            entry["type"] = tc["type"]
        func = tc.get("function") or {}
        if func.get("name"):
            entry["function"]["name"] += func["name"]
        if func.get("arguments"):
            entry["function"]["arguments"] += func["arguments"]


def _iter_chat_events(chunks):
    """把 CAPI SSE 原始 chunk 规范化为事件流。

    事件类型（dict，至少含 ``type``）：

    * ``{"type": "text", "text": str, "index": int}`` —— 文本增量（需累积）
    * ``{"type": "reasoning", "text": str, "index": int}`` —— 思考增量
    * ``{"type": "tool_calls", "tool_calls": [...], "finish_reason": str|None}``
    * ``{"type": "usage", "usage": {...}}`` —— usage-only 块（无 choices）
    * ``{"type": "finish", "finish_reason": str}`` —— stop/length/tool_calls/...

    流内 ``error``（HTTP 200 + error）直接抛 :class:`CopilotApiError`，
    不要当正常结束（spec §4.5 / §7 最后一行）。
    """
    pending = {}
    for obj in chunks:
        if not isinstance(obj, dict):
            continue
        error = obj.get("error")
        if error is not None and not obj.get("choices"):
            raise CopilotApiError(
                200,
                f"Copilot 流内错误: {json.dumps(error, ensure_ascii=False)[:500]}",
                body=json.dumps(obj, ensure_ascii=False),
            )

        chunk_usage = obj.get("usage")
        choices = obj.get("choices") or []

        if chunk_usage and not choices:
            # usage-only 块（stream_options.include_usage 的末块）
            yield {"type": "usage", "usage": chunk_usage}
            continue

        for ch in choices:
            idx = ch.get("index", 0)
            delta = ch.get("delta") or {}

            text = delta.get("content")
            if text:
                yield {"type": "text", "text": text, "index": idx}

            reasoning = delta.get("reasoning_text") or delta.get("cot_summary")
            if reasoning:
                yield {"type": "reasoning", "text": reasoning, "index": idx}

            tc_delta = delta.get("tool_calls")
            if tc_delta:
                _merge_tool_call_deltas(pending, tc_delta)

            legacy_fc = delta.get("function_call")
            if legacy_fc:
                entry = pending.setdefault(0, {
                    "id": "call_0",
                    "type": "function",
                    "function": {"name": "", "arguments": ""},
                })
                if legacy_fc.get("name"):
                    entry["function"]["name"] += legacy_fc["name"]
                if legacy_fc.get("arguments"):
                    entry["function"]["arguments"] += legacy_fc["arguments"]

            finish_reason = ch.get("finish_reason")
            if finish_reason:
                if pending:
                    yield {
                        "type": "tool_calls",
                        "tool_calls": _finalize_tool_calls(pending),
                        "finish_reason": finish_reason,
                    }
                    pending = {}
                yield {"type": "finish", "finish_reason": finish_reason}

        if chunk_usage and choices:
            yield {"type": "usage", "usage": chunk_usage}

    if pending:  # 没有 finish_reason 就断流：仍把已拼好的 tool_calls 交出去
        yield {"type": "tool_calls", "tool_calls": _finalize_tool_calls(pending),
               "finish_reason": None}


def chat_completion(copilot_token, messages, model=DEFAULT_MODEL,
                    temperature=0.7, max_tokens=4096, stream=True,
                    tools=None, initiator="user", base_url=None,
                    timeout=DEFAULT_TIMEOUT, extra_headers=None, **kwargs):
    """发送 Chat Completion 请求（``POST {api_base}/chat/completions``）。

    参数
    ----
    copilot_token : str
        Copilot API token（Bearer）
    messages : list[dict]
        消息列表，格式: [{"role": "user", "content": "..."}]
    model : str
        模型名称；生产代码应从 ``list_models()`` 动态获取，不要硬编码
    stream : bool
        True 时返回**事件生成器**（见 :func:`_iter_chat_events` 文档）；
        False 时返回完整响应 dict（OpenAI chat.completion 结构）。
    tools : list[dict] | None
        工具定义列表（用于 Agent 模式）
    initiator : str
        ``"user"`` / ``"agent"``（X-Initiator，服务端配额归因）
    base_url : str | None
        覆盖 CAPI Base URL（默认取 token 响应的 endpoints.api）
    extra_headers : dict | None
        额外请求头（例如模型专有的 ``requestHeaders``）
    **kwargs
        其余字段原样并入请求体（如 ``top_p`` / ``tool_choice`` /
        ``stream_options`` / ``reasoning_effort``）
    """
    headers = _build_headers(copilot_token, extra_headers, initiator=initiator)
    if _messages_have_image(messages):
        # 带图且模型支持 vision 时必须发（spec §3.1 / §4.4）
        headers["Copilot-Vision-Request"] = "true"

    body = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": bool(stream),
    }
    if tools:
        body["tools"] = tools
    body.update(kwargs)

    base = (base_url or api_base()).rstrip("/")
    path = f"{base}/chat/completions"
    if stream:
        return _iter_chat_events(_stream_request(path, headers, body,
                                                 timeout=timeout))
    return _request("POST", path, headers, body, timeout=timeout)


def stream_chat_text(copilot_token, messages, **kwargs):
    """便捷生成器：只 yield 文本增量（内部走 chat_completion 事件流）。"""
    for event in chat_completion(copilot_token, messages, stream=True, **kwargs):
        if event.get("type") == "text":
            yield event["text"]



# ---------------------------------------------------------------------------
# Responses API (Copilot 新版 API)
# ---------------------------------------------------------------------------

def _iter_responses_events(chunks):
    """把 ``/responses`` 的 SSE 事件规范化为与 chat 相同的事件字典。

    官方事件名：``response.output_text.delta`` / ``response.completed`` /
    ``response.failed`` / ``error``（spec §6.4）。只做最小可用处理，
    ``/responses`` 仍属实验路径，生产建议优先 ``/chat/completions``。
    """
    for obj in chunks:
        if not isinstance(obj, dict):
            continue
        etype = obj.get("type") or ""
        if etype in ("error", "response.failed"):
            detail = obj.get("error") or obj.get("response") or obj
            raise CopilotApiError(
                200,
                f"Responses 流内错误: {json.dumps(detail, ensure_ascii=False)[:500]}",
                body=json.dumps(obj, ensure_ascii=False),
            )
        if etype == "response.output_text.delta":
            delta = obj.get("delta")
            if delta:
                yield {"type": "text", "text": delta, "index": obj.get("output_index", 0)}
            continue
        if etype == "response.completed":
            response = obj.get("response") or {}
            usage = response.get("usage")
            if usage:
                yield {"type": "usage", "usage": usage}
            yield {"type": "finish", "finish_reason": "stop"}
            return
        # 兼容非事件式块（部分代理实现直接回 chat 形态）
        if not etype and obj.get("choices"):
            yield from _iter_chat_events([obj])


def responses_api(copilot_token, input_data, model=DEFAULT_MODEL,
                  instructions=None, tools=None, stream=True,
                  base_url=None, timeout=DEFAULT_TIMEOUT,
                  extra_headers=None, **kwargs):
    """使用 Copilot Responses API（新实验性路径 ``POST /responses``）。

    参数
    ----
    input_data : str | list
        输入内容
    instructions : str | None
        系统指令
    tools : list | None
        工具列表（注意：``/responses`` 的工具结构与 chat 的
        ``{"type":"function","function":...}`` 不同，调用方需自行转换）

    返回事件生成器 / 响应 dict，语义同 :func:`chat_completion`。

    .. note::
       该端点不是本项目的生产路径；是否走 ``/responses`` 应由
       :func:`pick_endpoint` 按模型 ``supported_endpoints`` 决定。
    """
    headers = _build_headers(copilot_token, extra_headers, initiator="user")

    body = {
        "model": model,
        "input": input_data,
        "stream": bool(stream),
        # /responses 默认不落盘存储（避免 previous_response_id 状态耦合）
        "store": False,
    }
    if instructions:
        body["instructions"] = instructions
    if tools:
        body["tools"] = tools
    body.update(kwargs)

    base = (base_url or api_base()).rstrip("/")
    path = f"{base}/responses"
    if stream:
        return _iter_responses_events(_stream_request(path, headers, body,
                                                      timeout=timeout))
    return _request("POST", path, headers, body, timeout=timeout)


# ---------------------------------------------------------------------------
# Models（spec §6）
# ---------------------------------------------------------------------------

def list_models_raw(copilot_token, base_url=None, timeout=30):
    """GET ``/models``，返回原始 JSON（保留 ``data[]`` 全部字段）。"""
    headers = _build_headers(copilot_token)
    base = (base_url or api_base()).rstrip("/")
    return _request("GET", f"{base}/models", headers, timeout=timeout)


def parse_models_response(data):
    """把 ``/models`` 响应解析为简化 dict 列表（spec §6.2）。

    每项包含::

        id, name, vendor, family, type, capabilities,
        max_context_window_tokens, max_prompt_tokens, max_output_tokens,
        supports, supported_endpoints, is_chat_default, is_chat_fallback,
        model_picker_enabled, billing, request_headers

    真实可用模型清单以该接口为准，不要硬编码 gpt-4o 之类（审计 C11）。
    """
    models = []
    raw_items = data.get("data") if isinstance(data, dict) else None
    for item in raw_items or []:
        if not isinstance(item, dict):
            continue
        caps = item.get("capabilities")
        if not isinstance(caps, dict):
            caps = {}
        limits = caps.get("limits")
        if not isinstance(limits, dict):
            limits = {}
        supports = caps.get("supports")
        if not isinstance(supports, dict):
            supports = {}
        request_headers = item.get("requestHeaders")
        if not isinstance(request_headers, dict):
            request_headers = {}
        models.append({
            "id": item.get("id"),
            "name": item.get("name") or item.get("id"),
            "vendor": item.get("vendor"),
            "family": caps.get("family") or item.get("family"),
            "type": caps.get("type") or "chat",
            "capabilities": caps,
            "max_context_window_tokens": limits.get("max_context_window_tokens"),
            "max_prompt_tokens": limits.get("max_prompt_tokens"),
            "max_output_tokens": limits.get("max_output_tokens"),
            "supports": supports,
            "supported_endpoints": item.get("supported_endpoints") or [],
            "is_chat_default": bool(item.get("is_chat_default")),
            "is_chat_fallback": bool(item.get("is_chat_fallback")),
            "model_picker_enabled": bool(item.get("model_picker_enabled", True)),
            "billing": item.get("billing"),
            "request_headers": request_headers,
        })
    return models


def list_models(copilot_token):
    """获取可用模型列表（简化 dict 列表，见 :func:`parse_models_response`）。

    旧版返回原始 JSON 对象；现按审计 C12 解析 ``data[]``。
    需要原始响应请用 :func:`list_models_raw`。
    """
    return parse_models_response(list_models_raw(copilot_token))


def pick_endpoint(model_meta):
    """按模型 ``supported_endpoints`` 选择端点，返回 ``(path, kind)``。

    优先 ``/chat/completions``（本项目已完整实现），其次 ``/responses``，
    最后回退 chat（spec §6.3 / §9.2）。
    """
    meta = model_meta or {}
    endpoints = meta.get("supported_endpoints") or []
    if "/chat/completions" in endpoints:
        return "/chat/completions", "chat"
    if "/responses" in endpoints:
        return "/responses", "responses"
    if "/v1/messages" in endpoints:
        return "/v1/messages", "messages"
    return "/chat/completions", "chat"


def select_default_model(models):
    """从 :func:`list_models` 结果挑一个默认模型 id。

    规则：``is_chat_default`` 优先 → 可在 picker 显示且支持 chat 端点 →
    第一个 chat 模型 → None。
    """
    models = [m for m in (models or []) if m.get("id")]
    for m in models:
        if m.get("is_chat_default"):
            return m["id"]
    for m in models:
        if m.get("model_picker_enabled") and "/chat/completions" in (
            m.get("supported_endpoints") or []
        ):
            return m["id"]
    for m in models:
        if m.get("type") in (None, "chat"):
            return m["id"]
    return models[0]["id"] if models else None


# ---------------------------------------------------------------------------
# 工具定义 — 参考 vscode-copilot-chat 的工具系统
# ---------------------------------------------------------------------------

# Agent 模式下可用的工具
AGENT_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "读取指定文件的内容",
            "parameters": {
                "type": "object",
                "properties": {
                    "filepath": {
                        "type": "string",
                        "description": "文件的绝对路径"
                    }
                },
                "required": ["filepath"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "search_files",
            "description": "按 glob 模式搜索文件",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {
                        "type": "string",
                        "description": "搜索模式，如 **/*.py"
                    },
                    "base_dir": {
                        "type": "string",
                        "description": "搜索的根目录"
                    }
                },
                "required": ["pattern"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "text_search",
            "description": "在文件中搜索文本",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "要搜索的文本"
                    },
                    "pattern": {
                        "type": "string",
                        "description": "文件过滤模式，如 *.py"
                    }
                },
                "required": ["query"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "list_directory",
            "description": "列出目录内容",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "目录路径"
                    }
                },
                "required": ["path"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "create_file",
            "description": "创建新文件（推荐用于写入代码）。内容中的引号需用 \\\" 转义，换行用 \\n 表示。",
            "parameters": {
                "type": "object",
                "properties": {
                    "filepath": {
                        "type": "string",
                        "description": "文件绝对路径"
                    },
                    "content": {
                        "type": "string",
                        "description": "文件内容。内嵌双引号请用 \\\" 转义，换行用 \\n 表示。"
                    }
                },
                "required": ["filepath", "content"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "run_command",
            "description": "在终端运行命令。重要：参数中的引号必须用反斜杠转义（\\\"），换行用\\n表示。如需在命令中嵌入代码，请使用文件写入方式（create_file），不要用 python -c 内联大段代码。",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "要运行的命令。内嵌引号请用 \\\" 转义，不要使用未转义的双引号。"
                    }
                },
                "required": ["command"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_errors",
            "description": "获取编辑器的编译/语法错误",
            "parameters": {
                "type": "object",
                "properties": {
                    "filepath": {
                        "type": "string",
                        "description": "文件路径（可选）"
                    }
                }
            }
        }
    },
    # ── 新增工具 ────────────────────────────────────────────────────
    {
        "type": "function",
        "function": {
            "name": "edit_file",
            "description": "编辑已有文件的特定行范围，替换为新的内容",
            "parameters": {
                "type": "object",
                "properties": {
                    "filepath": {
                        "type": "string",
                        "description": "文件的绝对路径"
                    },
                    "start_line": {
                        "type": "integer",
                        "description": "要替换的起始行号（从 1 开始）"
                    },
                    "end_line": {
                        "type": "integer",
                        "description": "要替换的结束行号（包含），-1 表示到文件末尾"
                    },
                    "new_content": {
                        "type": "string",
                        "description": "替换后的新内容"
                    }
                },
                "required": ["filepath", "start_line", "new_content"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "rename_file",
            "description": "重命名或移动文件",
            "parameters": {
                "type": "object",
                "properties": {
                    "source": {
                        "type": "string",
                        "description": "源文件绝对路径"
                    },
                    "destination": {
                        "type": "string",
                        "description": "目标文件绝对路径"
                    }
                },
                "required": ["source", "destination"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "fetch_webpage",
            "description": "获取网页内容，用于阅读文档或在线信息",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "网页 URL"
                    },
                    "max_chars": {
                        "type": "integer",
                        "description": "最大获取字符数，默认 5000"
                    }
                },
                "required": ["url"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "memory_save",
            "description": "保存一条信息到长期记忆，AI 可以在后续对话中回忆",
            "parameters": {
                "type": "object",
                "properties": {
                    "key": {
                        "type": "string",
                        "description": "记忆的键名，如 'user_preferences', 'project_config'"
                    },
                    "value": {
                        "type": "string",
                        "description": "记忆的内容"
                    },
                    "namespace": {
                        "type": "string",
                        "description": "记忆的命名空间，如 'user', 'project', 'global'"
                    }
                },
                "required": ["key", "value"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "memory_recall",
            "description": "从长期记忆中回忆之前保存的信息",
            "parameters": {
                "type": "object",
                "properties": {
                    "key": {
                        "type": "string",
                        "description": "要回忆的记忆键名，留空则列出所有记忆"
                    },
                    "namespace": {
                        "type": "string",
                        "description": "记忆的命名空间"
                    }
                },
                "required": []
            }
        }
    },
    # ── 目录和文件管理 ───────────────────────────────────────────
    {
        "type": "function",
        "function": {
            "name": "create_directory",
            "description": "创建目录（递归创建，类似 mkdir -p）",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "要创建的目录路径"
                    }
                },
                "required": ["path"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "delete_file",
            "description": "删除指定的文件",
            "parameters": {
                "type": "object",
                "properties": {
                    "filepath": {
                        "type": "string",
                        "description": "要删除的文件路径"
                    }
                },
                "required": ["filepath"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_current_file",
            "description": "获取当前编辑器中打开的文件信息（路径、语言、内容摘要）",
            "parameters": {
                "type": "object",
                "properties": {
                    "include_content": {
                        "type": "boolean",
                        "description": "是否包含文件内容，默认 true"
                    }
                }
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "codebase_search",
            "description": "在整个代码库中语义搜索相关代码和文档（等同于 VS Code 的 semantic_search）",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "自然语言搜索查询，描述你要查找的内容"
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "最大返回结果数，默认 20"
                    }
                },
                "required": ["query"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "manage_todo_list",
            "description": "管理结构化任务列表。对于任何多步骤任务必须使用此工具：工作前先创建完整计划，每完成一步立即更新状态。使用非常频繁（VERY frequently）。规则：1)复杂任务必须先规划 2)同一时间只能有一个in-progress 3)完成即标记completed 4)每次状态变更都调用此工具更新全量列表。",
            "parameters": {
                "type": "object",
                "properties": {
                    "todoList": {
                        "type": "array",
                        "description": "完整的任务列表",
                        "items": {
                            "type": "object",
                            "properties": {
                                "id": {
                                    "type": "integer",
                                    "description": "任务唯一标识符，从 1 开始递增"
                                },
                                "title": {
                                    "type": "string",
                                    "description": "简洁的任务描述（3-7个词）"
                                },
                                "status": {
                                    "type": "string",
                                    "enum": ["not-started", "in-progress", "completed"],
                                    "description": "任务状态: not-started=未开始, in-progress=进行中, completed=已完成"
                                }
                            },
                            "required": ["id", "title", "status"]
                        }
                    }
                },
                "required": ["todoList"]
            }
        }
    },
]

# ── 新增工具定义：子 Agent ──────────────────────────────────────

NEW_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "search_subagent",
            "description": "启动以搜索为中心的迭代子 Agent，在工作区中查找相关代码。适用于复杂的搜索任务，会综合分析文件名、内容和符号定义。",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "自然语言搜索查询"
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "最大返回结果数，默认 30"
                    }
                },
                "required": ["query"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "execution_subagent",
            "description": "启动以执行为中心的子 Agent，运行一个或多个终端命令并返回结果。适用于需要独立执行环境的长时间命令。",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "要执行的命令"
                    },
                    "timeout": {
                        "type": "integer",
                        "description": "超时秒数，默认 120"
                    }
                },
                "required": ["command"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "run_fleet",
            "description": "Fleet 模式：并行执行多个子 Agent 任务（搜索/执行），提高效率。适用于需要同时处理多个独立任务时。",
            "parameters": {
                "type": "object",
                "properties": {
                    "tasks": {
                        "type": "array",
                        "description": "任务列表",
                        "items": {
                            "type": "object",
                            "properties": {
                                "type": {
                                    "type": "string",
                                    "enum": ["search", "execution"],
                                    "description": "任务类型: search=搜索, execution=执行命令"
                                },
                                "query": {
                                    "type": "string",
                                    "description": "搜索查询（type=search 时必填）"
                                },
                                "command": {
                                    "type": "string",
                                    "description": "要执行的命令（type=execution 时必填）"
                                },
                                "description": {
                                    "type": "string",
                                    "description": "任务描述"
                                }
                            },
                            "required": ["type", "description"]
                        }
                    },
                    "max_concurrent": {
                        "type": "integer",
                        "description": "最大并行数，默认 3"
                    }
                },
                "required": ["tasks"]
            }
        }
    },
    # ── 新增工具定义：Git 操作 ────────────────────────────────────
    {
        "type": "function",
        "function": {
            "name": "get_changed_files",
            "description": "获取工作区中已更改文件的列表和 diff 统计信息（对标 VS Code 的 getChangedFiles）",
            "parameters": {
                "type": "object",
                "properties": {}
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "git_log",
            "description": "获取 Git 提交历史",
            "parameters": {
                "type": "object",
                "properties": {
                    "max_count": {
                        "type": "integer",
                        "description": "最大提交数，默认 20"
                    }
                }
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "git_add",
            "description": "暂存文件到 Git 暂存区",
            "parameters": {
                "type": "object",
                "properties": {
                    "files": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "要暂存的文件路径列表"
                    }
                },
                "required": ["files"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "git_commit",
            "description": "提交 Git 暂存区的更改",
            "parameters": {
                "type": "object",
                "properties": {
                    "message": {
                        "type": "string",
                        "description": "提交信息"
                    }
                },
                "required": ["message"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "git_create_branch",
            "description": "创建并切换到新的 Git 分支",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "新分支名称"
                    }
                },
                "required": ["name"]
            }
        }
    },
    # ── 新增工具定义：代码审查 ────────────────────────────────────
    {
        "type": "function",
        "function": {
            "name": "review_changes",
            "description": "审查代码更改（对标 VS Code 的 Code Review）。获取工作区中所有更改文件的列表和 diff，用于 AI 进行代码审查。",
            "parameters": {
                "type": "object",
                "properties": {
                    "staged": {
                        "type": "boolean",
                        "description": "是否审查已暂存的更改，默认 false（审查未暂存更改）"
                    }
                }
            }
        }
    },
    # ── 新增工具定义：编辑工具 ────────────────────────────────────
    {
        "type": "function",
        "function": {
            "name": "apply_patch",
            "description": "应用统一 diff 格式的补丁到文件（对标 VS Code 的 applyPatch）。适用于精确编辑文件、替代大段代码替换。",
            "parameters": {
                "type": "object",
                "properties": {
                    "patch": {
                        "type": "string",
                        "description": "统一 diff 格式的补丁内容"
                    }
                },
                "required": ["patch"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "insert_edit",
            "description": "精确的字符串替换编辑（对标 VS Code 的 replaceString）。old_string 必须与文件中的内容完全一致且唯一匹配。",
            "parameters": {
                "type": "object",
                "properties": {
                    "filepath": {
                        "type": "string",
                        "description": "文件路径"
                    },
                    "old_string": {
                        "type": "string",
                        "description": "要被替换的精确文本（必须唯一匹配）"
                    },
                    "new_string": {
                        "type": "string",
                        "description": "替换后的新文本"
                    }
                },
                "required": ["filepath", "old_string", "new_string"]
            }
        }
    },
    # ── 新增工具定义：Notebook 操作 ──────────────────────────────
    {
        "type": "function",
        "function": {
            "name": "create_notebook",
            "description": "创建新的 Jupyter Notebook 文件（对标 VS Code 的 newJupyterNotebook）",
            "parameters": {
                "type": "object",
                "properties": {
                    "filepath": {
                        "type": "string",
                        "description": "文件路径（自动添加 .ipynb 后缀）"
                    },
                    "cells": {
                        "type": "array",
                        "description": "初始单元格列表（可选）",
                        "items": {
                            "type": "object",
                            "properties": {
                                "cell_type": {
                                    "type": "string",
                                    "enum": ["code", "markdown"],
                                    "description": "单元格类型"
                                },
                                "source": {
                                    "type": "string",
                                    "description": "单元格源代码"
                                }
                            }
                        }
                    }
                },
                "required": ["filepath"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_notebook_info",
            "description": "获取 Jupyter Notebook 的摘要信息（对标 VS Code 的 getNotebookSummary）",
            "parameters": {
                "type": "object",
                "properties": {
                    "filepath": {
                        "type": "string",
                        "description": "Notebook 文件路径"
                    }
                },
                "required": ["filepath"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "add_notebook_cell",
            "description": "向 Jupyter Notebook 添加新单元格（对标 VS Code 的 editNotebook insert）",
            "parameters": {
                "type": "object",
                "properties": {
                    "filepath": {
                        "type": "string",
                        "description": "Notebook 文件路径"
                    },
                    "cell_type": {
                        "type": "string",
                        "enum": ["code", "markdown"],
                        "description": "单元格类型，默认 code"
                    },
                    "source": {
                        "type": "string",
                        "description": "单元格源代码"
                    },
                    "index": {
                        "type": "integer",
                        "description": "插入位置（0-based），默认追加到末尾"
                    }
                },
                "required": ["filepath", "source"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "edit_notebook_cell",
            "description": "编辑 Jupyter Notebook 中指定单元格的内容（对标 VS Code 的 editNotebook edit）",
            "parameters": {
                "type": "object",
                "properties": {
                    "filepath": {
                        "type": "string",
                        "description": "Notebook 文件路径"
                    },
                    "cell_index": {
                        "type": "integer",
                        "description": "单元格索引（0-based）"
                    },
                    "source": {
                        "type": "string",
                        "description": "新的源代码"
                    }
                },
                "required": ["filepath", "cell_index", "source"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "delete_notebook_cell",
            "description": "删除 Jupyter Notebook 中指定索引的单元格（对标 VS Code 的 editNotebook delete）",
            "parameters": {
                "type": "object",
                "properties": {
                    "filepath": {
                        "type": "string",
                        "description": "Notebook 文件路径"
                    },
                    "cell_index": {
                        "type": "integer",
                        "description": "要删除的单元格索引（0-based）"
                    }
                },
                "required": ["filepath", "cell_index"]
            }
        }
    },
]

# 合并所有工具
AGENT_TOOLS.extend(NEW_TOOLS)

# ── 工具分组（Tool Sets）— 对标 VS Code Copilot Chat ─────────────

TOOL_GROUP_READ = ["read_file", "get_errors", "memory_recall", "get_current_file"]
TOOL_GROUP_SEARCH = ["search_files", "text_search", "list_directory", "codebase_search", "search_subagent"]
TOOL_GROUP_EDIT = ["create_file", "edit_file", "rename_file", "delete_file", "create_directory", "apply_patch", "insert_edit"]
TOOL_GROUP_EXECUTE = ["run_command", "execution_subagent"]
TOOL_GROUP_WEB = ["fetch_webpage"]
TOOL_GROUP_MEMORY = ["memory_save", "memory_recall"]
TOOL_GROUP_PLAN = ["manage_todo_list"]
TOOL_GROUP_GIT = ["get_changed_files", "git_log", "git_add", "git_commit", "git_create_branch"]
TOOL_GROUP_REVIEW = ["review_changes"]
TOOL_GROUP_NOTEBOOK = ["create_notebook", "get_notebook_info", "add_notebook_cell", "edit_notebook_cell", "delete_notebook_cell", "notebook_run_cell", "notebook_get_output"]
TOOL_GROUP_FLEET = ["run_fleet", "search_subagent", "execution_subagent"]
TOOL_GROUP_EDIT2 = ["multi_replace_string", "read_project_structure", "find_test_files", "search_workspace_symbols", "view_image"]
TOOL_GROUP_SESSION = ["run_subagent", "compact_conversation", "list_sessions", "switch_session"]

# ── 参与者工具分组 — 对标 GitHub Copilot Chat @participants ──
TOOL_GROUP_WORKSPACE = TOOL_GROUP_READ + TOOL_GROUP_SEARCH + ["read_project_structure", "find_test_files", "search_workspace_symbols", "view_image"]
TOOL_GROUP_TERMINAL = TOOL_GROUP_READ + ["run_command", "execution_subagent", "text_search"]
TOOL_GROUP_SPYDER = TOOL_GROUP_READ + ["get_current_file"]

# ══════════════════════════════════════════════════════════════════════
# v0.4.1 新增工具定义
# ══════════════════════════════════════════════════════════════════════

V411_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "multi_replace_string",
            "description": "在多个文件中执行精确的字符串替换（对标 VS Code multiReplaceString）。每个替换 old_string 必须唯一匹配。",
            "parameters": {
                "type": "object",
                "properties": {
                    "replacements": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "filepath": {"type": "string", "description": "文件路径"},
                                "old_string": {"type": "string", "description": "要替换的精确文本"},
                                "new_string": {"type": "string", "description": "替换后的文本"}
                            },
                            "required": ["filepath", "old_string", "new_string"]
                        }
                    }
                },
                "required": ["replacements"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "read_project_structure",
            "description": "读取项目目录结构树（对标 VS Code readProjectStructure）",
            "parameters": {
                "type": "object",
                "properties": {
                    "max_depth": {"type": "integer", "description": "最大深度，默认 4"}
                }
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "find_test_files",
            "description": "查找与源文件对应的测试文件（对标 VS Code findTestFiles）",
            "parameters": {
                "type": "object",
                "properties": {
                    "filepath": {"type": "string", "description": "源文件路径（可选）"}
                }
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "search_workspace_symbols",
            "description": "按名称搜索工作区符号（函数/类/接口）（对标 VS Code searchWorkspaceSymbols）",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "符号名称"},
                    "max_results": {"type": "integer", "description": "最大结果数，默认 50"}
                },
                "required": ["query"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "view_image",
            "description": "查看图像文件元数据（对标 VS Code viewImage）",
            "parameters": {
                "type": "object",
                "properties": {
                    "filepath": {"type": "string", "description": "图像文件路径"}
                },
                "required": ["filepath"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "notebook_run_cell",
            "description": "运行 Notebook 代码单元格（对标 VS Code runNotebookCell）",
            "parameters": {
                "type": "object",
                "properties": {
                    "filepath": {"type": "string", "description": "Notebook 路径"},
                    "cell_index": {"type": "integer", "description": "单元格索引"}
                },
                "required": ["filepath", "cell_index"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "notebook_get_output",
            "description": "获取 Notebook 单元格执行输出（对标 VS Code getNotebookCellOutput）",
            "parameters": {
                "type": "object",
                "properties": {
                    "filepath": {"type": "string", "description": "Notebook 路径"},
                    "cell_index": {"type": "integer", "description": "单元格索引"}
                },
                "required": ["filepath", "cell_index"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "run_subagent",
            "description": "创建独立子 Agent 会话用于并行处理（对标 VS Code runSubagent）",
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "子 Agent 标题"},
                    "prompt": {"type": "string", "description": "初始任务描述"}
                },
                "required": ["title"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "compact_conversation",
            "description": "压缩对话历史释放上下文（对标 VS Code compactConversation）",
            "parameters": {
                "type": "object",
                "properties": {
                    "session_id": {"type": "string", "description": "会话 ID（可选）"}
                }
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "list_sessions",
            "description": "列出所有并行会话及状态",
            "parameters": {"type": "object", "properties": {}}
        }
    },
    {
        "type": "function",
        "function": {
            "name": "switch_session",
            "description": "切换到另一个 Agent 会话",
            "parameters": {
                "type": "object",
                "properties": {
                    "session_id": {"type": "string", "description": "目标会话 ID"}
                },
                "required": ["session_id"]
            }
        }
    },
]

AGENT_TOOLS.extend(V411_TOOLS)

# ══════════════════════════════════════════════════════════════════════
# Tree-sitter AST 代码理解工具（可选）
# ══════════════════════════════════════════════════════════════════════

TREE_SITTER_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_symbol_definitions",
            "description": "获取文件中的所有符号定义（类、函数、方法），带 AST 感知的类型信息和行号。对标 VS Code 的 Go to Symbol。",
            "parameters": {
                "type": "object",
                "properties": {
                    "filepath": {"type": "string", "description": "文件路径"}
                },
                "required": ["filepath"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_code_outline",
            "description": "获取代码的层次化大纲，显示类、方法、函数的嵌套结构。对标 VS Code 的 Code Outline。",
            "parameters": {
                "type": "object",
                "properties": {
                    "filepath": {"type": "string", "description": "文件路径"},
                    "max_depth": {"type": "integer", "description": "最大嵌套深度，默认 3"}
                },
                "required": ["filepath"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "find_related_symbols",
            "description": "在代码库中搜索匹配名称的符号（类、函数、变量）。比 text_search 更精确，只返回定义。",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "符号名称搜索关键词"},
                    "max_results": {"type": "integer", "description": "最大结果数，默认 20"}
                },
                "required": ["query"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_call_hierarchy",
            "description": "获取函数的调用关系：它调用了哪些函数，被哪些函数调用。对标 VS Code 的 Call Hierarchy。",
            "parameters": {
                "type": "object",
                "properties": {
                    "filepath": {"type": "string", "description": "文件路径"},
                    "line": {"type": "integer", "description": "函数所在行号"}
                },
                "required": ["filepath", "line"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_symbol_at_position",
            "description": "获取指定位置的符号信息。对标 VS Code 的 Go to Definition。",
            "parameters": {
                "type": "object",
                "properties": {
                    "filepath": {"type": "string", "description": "文件路径"},
                    "line": {"type": "integer", "description": "行号"}
                },
                "required": ["filepath", "line"]
            }
        }
    },
]

AGENT_TOOLS.extend(TREE_SITTER_TOOLS)

# Tree-sitter 工具分组
TOOL_GROUP_TREESITTER = [
    "get_symbol_definitions", "get_code_outline", "find_related_symbols",
    "get_call_hierarchy", "get_symbol_at_position",
]

# ══════════════════════════════════════════════════════════════════════
# Git 冲突解决工具
# ══════════════════════════════════════════════════════════════════════

GIT_CONFLICT_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "detect_conflicts",
            "description": "扫描工作区或指定文件中的未解决的 Git 合并冲突标记（<<<<<<<, =======, >>>>>>>）。对标 VS Code 的 Conflict Detection。",
            "parameters": {
                "type": "object",
                "properties": {
                    "filepath": {
                        "type": "string",
                        "description": "可选：指定文件路径进行扫描，留空则扫描整个项目"
                    }
                }
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "resolve_conflict",
            "description": "用 AI 智能解决检测到的合并冲突。提供文件路径和冲突索引。",
            "parameters": {
                "type": "object",
                "properties": {
                    "filepath": {"type": "string", "description": "包含冲突的文件路径"},
                    "conflict_index": {
                        "type": "integer",
                        "description": "要解决的冲突索引（0-based），默认 0"
                    },
                    "context_hint": {
                        "type": "string",
                        "description": "可选的上下文提示，说明冲突变更的目的"
                    }
                },
                "required": ["filepath"]
            }
        }
    },
]

AGENT_TOOLS.extend(GIT_CONFLICT_TOOLS)

TOOL_GROUP_GIT_CONFLICT = ["detect_conflicts", "resolve_conflict"]

# 按阶段推荐使用的工具组合
TOOLS_INITIAL = TOOL_GROUP_READ + TOOL_GROUP_SEARCH  # 初始探索阶段
TOOLS_FULL = AGENT_TOOLS  # 完整工具集
