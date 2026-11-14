# -*- coding: utf-8 -*-
"""把 API 失败翻译成「谁的问题 + 该怎么办」。

0.8.12 及以前，所有 HTTP 失败都被拼成 ``API 402: {json}`` 一串往下传：

* 文案错——DeepSeek 账户欠费会走进 ``if "api 402"`` 分支，显示成
  「Copilot 额度已用尽，请稍后重试，或升级订阅」。用户照着去升订阅，
  当然修不好；
* 形态错——整段错误 JSON 被 ``logger.exception`` 连 traceback 一起打进
  Spyder 的内部控制台，红字一片，看起来像 IDE 崩了，其实只是账户没钱。

所以错误处理集中到这个模块：先按状态码 / 错误体 / 提供方分类，再给一句
人话结论 + 两三条真能照做的步骤。GUI 只负责把结果画出来。

设计约束（都是踩过的坑）：

* **不抛异常**——这里任何输入（空体、HTML 网关页、10MB JSON、bytes）
  都必须得到结果，绝不能因为「格式化错误」把主线程带崩；
* **不带控制字符 / 不超长**——错误体直接灌进 QTextDocument 会拖慢渲染，
  顺手把 HTML 标签清掉，避免网关页原样显示；
* **provider 决定措辞**——402 在 Copilot 是「订阅额度用完」，在 DeepSeek
  是「余额不足要充值」，两件事，不能共用一句话。
"""

from __future__ import annotations

import json
import re
import socket
import urllib.error
from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# 分类枚举（字符串常量：日志里可读，跨版本也不会因为枚举改名失效）
# ---------------------------------------------------------------------------

BALANCE = "balance"          # 402：余额 / 配额
AUTH = "auth"                # 401/403：Key 失效、没登录
RATE_LIMIT = "rate_limit"    # 429
MODEL = "model"              # 模型名不存在 / 没权限
CONTEXT = "context"          # 超出上下文窗口
REFUSED = "refused"          # 422：内容策略
TOO_LARGE = "too_large"      # 413：请求体过大
CLIENT = "client"            # 466：客户端被拒
SERVER = "server"            # 5xx
NETWORK = "network"          # DNS / 连接 / TLS
TIMEOUT = "timeout"          # 超时
CANCELED = "canceled"        # 499 / 用户取消
UNKNOWN = "unknown"

#: 卡片首行的锚点。GUI 靠它判断「这个 payload 是成品卡片还是裸文本」。
CARD_MARK = "**⚠"

#: 重试没有意义的错误：余额不够，重试一百次也还是不够。
NO_RETRY = frozenset({BALANCE, AUTH, MODEL, CONTEXT, REFUSED, TOO_LARGE,
                      CANCELED, CLIENT})

PROVIDER_LABELS = {
    "deepseek": "DeepSeek",
    "hunyuan": "腾讯混元 TokenHub",
    "copilot": "GitHub Copilot",
    "openai": "OpenAI",
    "azure": "Azure OpenAI",
    "anthropic": "Anthropic",
    "ollama": "Ollama",
    "custom": "自定义服务",
}

_TITLES = {
    BALANCE: "账户余额不足",
    AUTH: "API Key 失效 / 未登录",
    RATE_LIMIT: "请求太频繁",
    MODEL: "模型不可用",
    CONTEXT: "对话超出上下文长度",
    REFUSED: "请求被内容策略拦下",
    TOO_LARGE: "请求体太大",
    CLIENT: "客户端被服务端拒绝",
    SERVER: "服务端故障",
    NETWORK: "连不上服务器",
    TIMEOUT: "请求超时",
    CANCELED: "请求已取消",
    UNKNOWN: "请求失败",
}

# provider → (按钮文案, 链接)。只在卡片里显示成一行可点的文字。
_TOPUP = {
    "deepseek": ("充值页", "https://platform.deepseek.com/top_up"),
    "hunyuan": ("TokenHub 控制台",
                "https://console.cloud.tencent.com/tokenhub"),
    "openai": ("计费页",
               "https://platform.openai.com/settings/organization/billing/overview"),
    "anthropic": ("计费页", "https://console.anthropic.com/settings/billing"),
    "copilot": ("订阅页", "https://github.com/settings/copilot"),
}
_KEYS = {
    "deepseek": ("API Keys 页", "https://platform.deepseek.com/api_keys"),
    "hunyuan": ("TokenHub API Key",
                "https://console.cloud.tencent.com/tokenhub/apikey"),
    "openai": ("API Keys 页", "https://platform.openai.com/api-keys"),
    "anthropic": ("API Keys 页", "https://console.anthropic.com/settings/keys"),
}


# ---------------------------------------------------------------------------
# 错误体解析
# ---------------------------------------------------------------------------

# 「model hy4 not found」这类措辞把模型名夹在 model 和 not found 中间，
# 精确短语匹配不到。放在 _MARKERS 之后做正则兜底：余额 / 鉴权 / 未开通等
# 更具体的关键词仍然优先，避免把 402「model service is not activated」
# 又抢回「模型名写错」。
_MODEL_RE = re.compile(
    r"\bmodel\b[\w\-.\s]{0,40}?"
    r"(?:not\s+(?:found|exist|available|supported|activated)|"
    r"does\s+not\s+exist|is\s+not\s+(?:available|supported)|"
    r"unknown|unsupported|invalid)",
    re.I)

_STATUS_RE = re.compile(
    r"\b(?:api|http|status(?:\s*code)?|error\s*code)\D{0,4}(\d{3})\b", re.I)
_TAG_RE = re.compile(r"<[^>]{0,200}>")
_CTRL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
_WS_RE = re.compile(r"\s+")


def clean_text(text, limit=300):
    """把任意错误文本压成一行可读内容（去控制字符、去 HTML、截断）。"""
    try:
        out = text.decode("utf-8", "replace") if isinstance(text, bytes) else str(text or "")
    except Exception:  # noqa: BLE001 - 取字符串失败也不能抛
        return ""
    out = _CTRL_RE.sub(" ", out)
    out = _TAG_RE.sub(" ", out)
    out = _WS_RE.sub(" ", out).strip()
    if limit and len(out) > limit:
        out = out[:limit].rstrip() + "…"
    return out


def parse_status(text, default=0):
    """从 ``API 402: ...`` / ``HTTP 502`` / ``status_code=429`` 里抠状态码。"""
    try:
        match = _STATUS_RE.search(str(text or ""))
        if match:
            return int(match.group(1))
    except (TypeError, ValueError):
        pass
    return int(default or 0)


def _json_payload(text):
    """错误体里第一个能解析的 JSON 对象；解析不出来返回 ``{}``。

    网关常常在 JSON 前后塞一段 HTML 或 "upstream connect error"，所以
    不能 ``json.loads(整段)``，得从每个 ``{`` 往后试。
    """
    if not text:
        return {}
    raw = str(text)
    decoder = json.JSONDecoder()
    for match in re.finditer(r"\{", raw[:4000]):
        try:
            obj, _end = decoder.raw_decode(raw[match.start():])
        except (ValueError, TypeError):
            continue
        if isinstance(obj, dict):
            return obj
    return {}


def _walk(obj, keys, depth=0):
    """在嵌套 JSON 里按 key 找第一个非空字符串。"""
    if depth > 4:
        return ""
    if isinstance(obj, dict):
        for key in keys:
            value = obj.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        for value in obj.values():
            found = _walk(value, keys, depth + 1)
            if found:
                return found
    elif isinstance(obj, list):
        for value in obj[:5]:
            found = _walk(value, keys, depth + 1)
            if found:
                return found
    return ""


_GENERIC_CODES = frozenset({"invalid_request_error", "api_error", "error",
                            "none", "unknown", "request_error"})


def _code_of(payload):
    """挑一个「有信息量」的错误码。

    DeepSeek 的 402 长这样::

        {"error": {"code": "invalid_request_error",
                   "type": "insufficient_balance_error"}}

    ``code`` 是通用壳，``type`` 才是真原因；反过来 OpenAI 常见
    ``code=invalid_api_key``、``type=invalid_request_error``。所以取
    第一个不在通用表里的值，都没有才退回 ``code``。
    """
    candidates = [_walk(payload, ("code",)), _walk(payload, ("type",)),
                  _walk(payload, ("error_code",))]
    for value in candidates:
        if value and value.strip().lower() not in _GENERIC_CODES:
            return value
    return candidates[0] or ""


def parse_payload(body):
    """抽出 ``(服务端原文, 错误码)``。"""
    payload = _json_payload(body)
    code = ""
    if payload:
        code = _code_of(payload)
        message = _walk(payload, ("message", "msg", "detail", "error_description"))
        if not message:
            err = payload.get("error")
            if isinstance(err, str):
                message = err
        if not message:
            message = clean_text(body)
        return clean_text(message), clean_text(code, 80)
    return clean_text(body), ""


# ---------------------------------------------------------------------------
# 结果对象
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ApiErrorInfo:
    """一次失败的「结论 + 依据 + 动作」。"""

    kind: str = UNKNOWN
    status: int = 0
    provider: str = ""
    model: str = ""
    title: str = ""
    summary: str = ""
    detail: str = ""
    code: str = ""
    steps: tuple = field(default_factory=tuple)
    links: tuple = field(default_factory=tuple)   # ((文案, url), ...)

    @property
    def retryable(self):
        """重试有没有意义。

        余额不够 / Key 错了，重试一百次还是错；而 4xx 里除限流、超时
        之外都是「请求本身有问题」，同样不该自动重试。
        """
        if self.kind in NO_RETRY:
            return False
        if 400 <= self.status < 500 and self.status not in (408, 429):
            return False
        return True

    @property
    def provider_label(self):
        return PROVIDER_LABELS.get(self.provider, self.provider or "")


class ApiError(Exception):
    """带状态码和原始错误体的 API 异常。

    ``str(exc)`` 只给一行人话——日志、状态栏、审计都用它。原始 JSON 留在
    ``.body`` 里，需要时由 :func:`card` 决定展示多少。
    """

    def __init__(self, status=0, body="", provider="", model="", message=""):
        self.status = int(status or 0)
        self.body = body or ""
        self.provider = provider or ""
        self.model = model or ""
        self.info = classify(status=self.status, body=self.body,
                             provider=self.provider, model=self.model,
                             text=message)
        super().__init__(self.info.summary)


# ---------------------------------------------------------------------------
# 分类
# ---------------------------------------------------------------------------

_MARKERS = (
    # (关键字元组, 分类) —— 顺序有意义：越「具体」的越靠前
    (("insufficient balance", "insufficient_balance", "insufficient quota",
      "insufficient_quota", "quota exceeded", "exceeded your current quota",
      "billing", "payment required", "余额不足", "欠费", "额度已用尽",
      # TokenHub 的 402 还可能是「模型服务没开通」，不是欠费
      "model service is not activated", "service not activated",
      "not activated", "未开通", "402002", "402003"),
     BALANCE),
    (("invalid api key", "incorrect api key", "invalid_api_key",
      "unauthorized", "authentication", "api key not valid", "token expired",
      "无效的令牌", "未登录"),
     AUTH),
    (("rate limit", "rate_limit", "too many requests", "requests per",
      "频率限制"),
     RATE_LIMIT),
    (("context length", "context_length", "maximum context",
      "too many tokens", "prompt is too long", "reduce the length",
      "上下文长度"),
     CONTEXT),
    (("content filter", "content_filter", "content policy", "content_policy",
      "responsible ai", "safety system"),
     REFUSED),
    # 别再放裸的 "does not exist"：TokenHub 的鉴权失败写的是
    # "The API Key does not exist ..."，会被它误判成模型名写错。
    # "model ... does not exist" 由 _MODEL_RE 正则兜底，更精确。
    (("model not found", "model_not_found", "model not exist",
      "model does not exist", "model or service id", "no such model",
      "invalid model", "not a valid model", "unsupported model",
      "unknown model", "model is not supported", "模型不存在", "模型不可用"),
     MODEL),
)


def _kind_from_markers(blob):
    for words, kind in _MARKERS:
        for word in words:
            if word in blob:
                return kind
    if _MODEL_RE.search(blob):
        return MODEL
    return ""


def _kind_from_status(status, blob):
    if status == 402:
        return BALANCE
    if status in (401, 403):
        return AUTH
    if status == 429:
        return RATE_LIMIT
    if status == 413:
        return TOO_LARGE
    if status == 466:
        return CLIENT
    if status == 499:
        return CANCELED
    if status in (408, 504):
        return TIMEOUT
    if status >= 500:
        return SERVER
    # 400 / 404 / 422 只在不认识错误体时落到最后，避免把
    # "model not found" 说成「请求被拒绝」。
    if status in (400, 404, 422):
        return UNKNOWN
    return ""


def _kind_from_exc(exc):
    name = type(exc).__name__.lower()
    if isinstance(exc, (urllib.error.URLError, OSError, socket.timeout,
                        TimeoutError)) and not isinstance(exc, urllib.error.HTTPError):
        text = str(getattr(exc, "reason", exc)).lower()
        if isinstance(exc, (socket.timeout, TimeoutError)) or "timed out" in text:
            return TIMEOUT
        return NETWORK
    table = {
        "copilotquotaerror": BALANCE,
        "copilotauth": AUTH,
        "copilotauthorizationerror": AUTH,
        "copilotcontentfiltererror": REFUSED,
        "copilotratelimiterror": RATE_LIMIT,
        "copilotclientrejectederror": CLIENT,
        "copilotcancelederror": CANCELED,
        "copilotnetworkerror": NETWORK,
        "copilotsubscriptionerror": AUTH,
    }
    for key, kind in table.items():
        if key in name:
            return kind
    if "timeout" in name:
        return TIMEOUT
    if "serror" in name or "servererror" in name:
        return SERVER
    return ""


def classify(exc=None, *, text="", status=0, body="", provider="", model=""):
    """把任意失败（异常或文本）归一成 :class:`ApiErrorInfo`。

    永不抛异常：哪怕 ``exc`` 是个连 ``__str__`` 都坏的怪对象，也要返回
    ``kind=UNKNOWN`` 的结果，否则错误处理自己就成了新的崩溃点。
    """
    try:
        if exc is not None:
            status = int(getattr(exc, "status", 0) or status or 0)
            body = getattr(exc, "body", "") or body
            if not text:
                text = clean_text(exc, 600)
            if not provider:
                provider = str(getattr(exc, "provider", "") or "")
            if not model:
                model = str(getattr(exc, "model", "") or "")
        status = int(status or parse_status(text) or 0)
        detail, code = parse_payload(body or text)
        # v0.8.16：分类必须看得到完整错误体。旧实现只拼 message + code，
        # 于是 TokenHub 401002 的 "The API Key does not exist ..." 被
        # 「does not exist」判成模型名写错，而同一个 JSON 里的
        # "authentication_error" 恰好被 parse_payload 丢掉了。
        raw_body = clean_text(body or "", 4000)
        blob = f"{code} {detail} {text} {raw_body}".lower()

        kind = (_kind_from_exc(exc) if exc is not None else "") \
            or _kind_from_markers(blob) \
            or _kind_from_status(status, blob) \
            or UNKNOWN
        # 401/403 是网关给出的鉴权结论，措辞再像「模型不存在」也不能覆盖。
        # TokenHub 的 401002（API Key 不存在 / 签名校验失败）就是靠这条兜住。
        if status in (401, 403) and kind not in (AUTH, BALANCE, CANCELED):
            kind = AUTH
        # TokenHub 用 402 表达「模型服务未开通 / 额度不足」，但错误体常写成
        # "model hy4 does not exist" / "model not found"，会被 MODEL markers
        # 抢先判成「模型名写错」——用户照着改名字永远修不好。402 语义优先。
        if provider == "hunyuan" and status == 402 and kind == MODEL:
            kind = BALANCE
        return _build(kind, status, provider, model, detail, code)
    except Exception:  # noqa: BLE001 - 兜底：分类失败就报原文
        return ApiErrorInfo(kind=UNKNOWN, status=int(status or 0),
                            provider=provider or "", model=model or "",
                            title=_TITLES[UNKNOWN], summary="请求失败",
                            detail=clean_text(text or exc), steps=())


def _build(kind, status, provider, model, detail, code):
    title = _TITLES.get(kind, _TITLES[UNKNOWN])
    # TokenHub 的 402 还可能是「模型服务没开通」，不能只说余额
    if kind == BALANCE and provider == "hunyuan":
        title = "账户余额不足 / 模型服务未开通"
    if status:
        title = f"{title}（HTTP {status}）"
    label = PROVIDER_LABELS.get(provider, provider or "服务端")
    return ApiErrorInfo(
        kind=kind, status=status, provider=provider or "", model=model or "",
        title=title, summary=_summary(kind, label, model, status,
                                      named=bool(provider), provider=provider),
        detail=detail, code=code,
        steps=tuple(_steps(kind, provider, label, model)),
        links=tuple(_links(kind, provider)),
    )


def _summary(kind, label, model, status, named=True, provider=""):
    if kind == BALANCE:
        if provider == "copilot":
            return "Copilot 的订阅额度已用完，服务端拒绝了这次请求"
        if provider == "hunyuan":
            return (f"{label} 账户余额 / 额度不足，或这个模型服务还没在"
                    "控制台开通，服务端直接拒绝了")
        return f"{label} 账户余额不足以支付这次请求，服务端直接拒绝了"
    if kind == AUTH:
        if label == "GitHub Copilot":
            return "还没登录 GitHub Copilot，或者登录已经失效"
        return f"{label} 拒绝了这次请求：API Key 无效、过期，或者没有该模型的权限"
    if kind == RATE_LIMIT:
        return f"{label} 判定请求过快，已限流"
    if kind == MODEL:
        if provider == "hunyuan":
            return (f"模型「{model or '未指定'}」在 {label} 上不可用："
                    "多半是控制台还没开通这个模型服务，或 Key 与地址的地域不一致")
        return f"模型「{model or '未指定'}」在 {label} 上不可用"
    if kind == CONTEXT:
        return "这轮对话（含工具输出、附件）超过了模型的上下文窗口"
    if kind == REFUSED:
        return "服务端的内容策略拦下了这次请求"
    if kind == TOO_LARGE:
        return "请求体超过服务端允许的大小"
    if kind == CLIENT:
        return "服务端不认这个客户端的协议头"
    if kind == SERVER:
        return f"{label} 服务端内部出错" if named else "服务端内部出错"
    if kind == NETWORK:
        return f"连不上 {label}：断网、代理或证书问题" if named else \
            "连不上服务器：断网、代理或证书问题"
    if kind == TIMEOUT:
        return f"{label} 一直没有响应，请求超时" if named else \
            "服务器一直没有响应，请求超时"
    if kind == CANCELED:
        return "请求已取消"
    return "请求失败，服务端没有给出可识别的原因"


def _md_link(label, url):
    """``[文字](地址)``：错误卡片里的链接要短。

    直接写裸 URL 会把 ``https://`` 和域名拆到两行（实测在 380px 宽的
    气泡里就是这样），既难读又不像人给的指引。
    """
    return f"[{label}]({url})" if url else label


def _steps(kind, provider, label, model):
    if kind == BALANCE:
        if provider == "copilot":
            return (
                "Copilot 的额度按订阅周期给：等到下个周期，或在 GitHub 上升级订阅",
                "也可以换一条路：设置 → 模型里把提供方改成 "
                + _md_link("DeepSeek", "https://platform.deepseek.com")
                + " / OpenAI / 本地 Ollama",
                "这类错误重试没有用，插件不会自动重试，免得白等",
            )
        if provider == "deepseek":
            return (
                "DeepSeek 按量计费，余额耗尽就返回 402：到 "
                + _md_link("DeepSeek 充值页", "https://platform.deepseek.com/top_up")
                + " 充值，页面右上角能查余额",
                "充值到账后不用重开对话：点这条消息右上角的「重试」，上下文还在",
                "暂时不充：设置 → 模型里换成本地 Ollama（免费），或换一家有余额的 API",
            )
        if provider == "hunyuan":
            return (
                "TokenHub 的 402 有两种：账户余额 / 额度不足，或者这个模型服务"
                "还没在控制台开通。先到 "
                + _md_link("TokenHub 控制台",
                           "https://console.cloud.tencent.com/tokenhub")
                + " 确认 hy4 已开通，再查余额",
                "开通、充值到账后点这条消息右上角的「重试」，不用重开对话",
                "仍然 402：确认 API Key 与接入地址属于同一地域——广州 / 新加坡 / "
                "硅谷的 Key 不通用，填错会直接被拒",
            )
        topup = _TOPUP.get(provider)
        return (
            f"到 {label} 控制台确认余额 / 额度"
            + (f"（{_md_link('计费页', topup[1])}）" if topup else "")
            + "，充上钱再发一次",
            "充值到账后点这条消息右上角的「重试」即可，不用重说一遍",
            "暂时不充：设置 → 模型里换提供方（本地 Ollama 不花钱）",
        )
    if kind == AUTH:
        if provider == "copilot":
            return (
                "设置 → 账号里重新登录 GitHub，确认账号仍有 Copilot 权限",
                "退出后重启 Spyder 再试，令牌缓存需要重新交换",
            )
        link = _KEYS.get(provider)
        first = (f"设置 → 模型里重新填 {label} 的 API Key（检查有没有多余空格、"
                 f"前缀是否完整）")
        second = (f"Key 在 {_md_link('API Keys 页', link[1])} 生成；改完点「测试连接」确认能通"
                  if link else "到服务商控制台核对 Key 是否被删除 / 轮换")
        third = ("公司网络若做了 TLS 拦截，也会表现成 401，先换手机热点对比")
        if provider == "hunyuan":
            # 实测：同一个 Key 在短别名域名 200、在长域名 401002「API Key
            # 不存在」。两套入口各自签发 Key，只改模型名永远修不好。
            return (
                first, second,
                "接口地址必须以 **`/v1` 结尾**："
                "`https://tokenhub.tencentmaas.com/v1`（默认）或"
                "`https://tokenhub.tencentcloudmaas.com/v1`。"
                "套餐页 `/plan/...` 之类不是接口地址，填进去同样报 401002",
                "TokenHub 有**两套广州入口**且各自签发 Key，不通用；"
                "Key 没填错却仍报 401002 就换另一套域名再点「测试连接」"
                "（新加坡 / 硅谷是另一组地域，地域不匹配同样报 401）",
            )
        return (first, second, third)
    if kind == RATE_LIMIT:
        return (
            "等 5–10 秒再发；连续触发时降低工具调用轮次或换模型",
            "开启思考模式 + 长上下文时单次占用更高，更容易撞上限流",
        )
    if kind == MODEL:
        if provider == "hunyuan":
            return (
                "先到 TokenHub 控制台确认「hy4 / hy4-preview」模型服务**已开通**："
                "没开通时服务端会直接报「模型不存在」，改模型名是修不好的",
                "确认 API 地址与 Key 属于同一地域——广州 / 新加坡 / 硅谷各自"
                "签发 Key，地域不匹配常把「没权限」报成模型不可用",
                "用 GET /v1/models 看这个地域真正上线的模型名，再照抄进设置里",
            )
        known = {
            "deepseek": "DeepSeek 当前可用：deepseek-flash、deepseek-v4-pro",
        }
        return (
            f"设置 → 模型里填 {label} 确实提供的模型名（大小写、版本号都要一致）",
            known.get(provider, "别把别家的模型名填到另一家服务商上"),
            "改成自定义 / 代理地址时，确认它转发的模型列表里有这个模型",
        )
    if kind == CONTEXT:
        return (
            "用 /compact 压缩历史，或新开一个对话（旧对话在历史面板里）",
            "关掉项目上下文自动注入、少贴整份日志，能省很多 token",
            "工具输出过长时先落盘，只看需要的片段",
        )
    if kind == REFUSED:
        return (
            "改写提问，别让模型处理被策略禁止的内容",
            "带图请求被拦时先去掉图片，确认是不是附件触发的",
        )
    if kind == TOO_LARGE:
        return (
            "减少一次附带的图片 / 文件数量（当前上限 6 张，单张 8MB）",
            "长截图先压缩或裁剪再贴",
        )
    if kind == CLIENT:
        return (
            "升级插件到最新版本：Copilot 的协议头会变，旧版本会被直接拒",
            "仍失败请把这条错误和插件版本号一起反馈",
        )
    if kind == SERVER:
        return (
            "等 1–2 分钟再试；服务商状态页能看到是否在故障",
            "持续 5xx 时换模型或换提供方，别反复重试同一个节点",
        )
    if kind == NETWORK:
        domain = ("https://tokenhub.tencentcloudmaas.com"
                  if provider == "hunyuan" else "https://api.deepseek.com")
        return (
            f"确认能打开服务商域名（如 {domain}），再检查系统代理",
            "Spyder 默认走系统代理；公司网络的 TLS 拦截会表现为连接被重置",
            "本地 Ollama：确认 ollama serve 在跑、端口 11434 通",
        )
    if kind == TIMEOUT:
        return (
            "先重试一次；仍超时把 max_tokens 调小，或换更快的模型",
            "长上下文首包慢属正常，超过 60 秒还没动静才算超时",
        )
    if kind == CANCELED:
        return ()
    return (
        "核对设置里的 API 地址、模型名有没有写错（末尾别多 /）",
        "仍失败请把这条错误原文发给插件作者，带上复现步骤",
    )


def _links(kind, provider):
    if kind == BALANCE and provider in _TOPUP:
        return (_TOPUP[provider],)
    if kind == MODEL and provider == "hunyuan":
        # 「模型不可用」在 TokenHub 多半要回控制台开通，不是改名字
        return (_TOPUP["hunyuan"], _KEYS["hunyuan"])
    if kind == AUTH and provider == "copilot":
        return (("GitHub 登录", "https://github.com/login"),)
    if kind == AUTH and provider in _KEYS:
        return (_KEYS[provider],)
    return ()


# ---------------------------------------------------------------------------
# 渲染
# ---------------------------------------------------------------------------

def summary(info):
    """一行结论：状态栏、审计日志、异常 ``str()`` 都用它。"""
    return info.summary or _TITLES.get(info.kind, _TITLES[UNKNOWN])


def card(info, show_detail=True):
    """整张错误卡片（Markdown）。

    结构刻意固定：结论 → 上下文（提供方 · 模型）→ 服务端原文引用 →
    分步处置 → 页脚。用户扫一眼就知道「是不是我能修的」。
    """
    lines = [f"{CARD_MARK} {info.title}**"]
    meta = " · ".join(x for x in (info.provider_label, info.model) if x)
    if meta:
        lines.append(meta)
    lines.append("")
    lines.append(summary(info))
    if show_detail and info.detail:
        lines.append("")
        lines.append(f"> {info.detail}")
    if info.steps:
        lines.append("")
        lines.append("**怎么办**")
        for index, step in enumerate(info.steps, 1):
            lines.append(f"{index}. {step}")
    # v0.8.15：links 字段此前一直没被渲染，充值页 / Key 页 / 控制台入口
    # 全是死数据。这里补成一行短链接，气泡里能直接点开。
    if info.links:
        lines.append("")
        lines.append(" · ".join(
            _md_link(label, url) for label, url in info.links if label))
    tail = f"HTTP {info.status}" if info.status else info.kind
    if info.code:
        tail += f" · {info.code}"
    if not info.retryable:
        tail += " · 不会自动重试"
    lines.append("")
    lines.append(f"*{tail}*")
    return "\n".join(lines)


def card_for(exc=None, *, text="", status=0, body="", provider="", model=""):
    """异常 / 文本 → 卡片。GUI 的 ``error`` 信号就发这个。"""
    return card(classify(exc, text=text, status=status, body=body,
                         provider=provider, model=model))


def summary_for(exc=None, *, text="", status=0, body="", provider="", model=""):
    """异常 / 文本 → 一行结论（保留旧 ``_format_api_error`` 的用途）。"""
    return summary(classify(exc, text=text, status=status, body=body,
                            provider=provider, model=model))


def is_card(text):
    """payload 是不是已经渲染好的卡片。"""
    return str(text or "").lstrip().startswith(CARD_MARK)


def status_line(text, limit=48):
    """从卡片 / 裸文本里取一行纯文字，给状态栏用。"""
    first = (str(text or "").strip().splitlines() or [""])[0]
    first = first.replace(CARD_MARK, "").replace("**", "")
    first = first.replace("`", "").replace("#", "").strip(" ：:")
    return first[:limit]
