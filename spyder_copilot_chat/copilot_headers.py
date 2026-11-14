# -*- coding: utf-8 -*-
"""GitHub Copilot HTTP 请求头与 Base URL 的唯一事实来源。

依据官方 ``github.copilot-chat`` 0.48.1 产物逆向出的规格
（见工作区 ``reports/copilot-protocol-spec.md``）：

* GitHub REST（device flow / token 交换）：``https://api.github.com``，
  ``X-GitHub-Api-Version: 2025-04-01``，``Authorization: token <github_token>``
* CAPI（Chat / Responses / Messages / Models）：``https://api.githubcopilot.com``
  （可被 token 响应的 ``endpoints.api`` 覆盖），
  ``X-GitHub-Api-Version: 2026-01-09``，``Authorization: Bearer <copilot_token>``
* Copilot Proxy（FIM 内联补全）：``https://copilot-proxy.githubusercontent.com``
  （可被 ``endpoints.proxy`` 覆盖），``X-GitHub-Api-Version: 2025-05-01``

本模块**只**负责构造请求头 / 解析 Base URL，不发任何网络请求，
不依赖 Qt，可被 ``auth`` / ``copilot_client`` / FIM Provider 共同 import。

稳定公开 API（其它模块依赖这些名字与签名，改动需同步调用方）::

    EDITOR_VERSION
    EDITOR_PLUGIN_VERSION
    COPILOT_INTEGRATION_ID
    build_chat_headers(copilot_token, initiator="user") -> dict
    build_fim_headers(copilot_token) -> dict
    api_base(github_token=None) -> str
    proxy_base() -> str
"""

import json
import logging
import os
import uuid

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 常量（官方 0.48.1 字面量，见 spec §3、§5.3、§9.2）
# ---------------------------------------------------------------------------

#: 编辑器标识，格式必须为 ``<editor>/<version>``，不能带括号说明
EDITOR_VERSION = "vscode/1.120.0"
#: 插件标识，格式必须为 ``<plugin>/<version>``
EDITOR_PLUGIN_VERSION = "copilot-chat/0.48.1"
#: 官方 prod 构建的集成 id（Code-OSS 是 code-oss；不要伪造 *-dev）
COPILOT_INTEGRATION_ID = "vscode-chat"

#: CAPI（Chat / Models / Responses）使用的 API 版本
CAPI_API_VERSION = "2026-01-09"
#: FIM（Proxy legacy /completions）使用的 API 版本（注意与 CAPI 不同）
FIM_API_VERSION = "2025-05-01"
#: GitHub REST（device flow / token 交换）使用的 API 版本
GITHUB_API_VERSION = "2025-04-01"

#: User-Agent 必须是 ``GitHubCopilotChat/<版本>`` 形态
USER_AGENT = "GitHubCopilotChat/0.48.1"

#: CAPI 默认 Base URL（token 响应的 endpoints.api 可覆盖）
DEFAULT_API_BASE = "https://api.githubcopilot.com"
#: Copilot Proxy 默认 Base URL（endpoints.proxy 可覆盖）
DEFAULT_PROXY_BASE = "https://copilot-proxy.githubusercontent.com"

#: 与 auth 模块一致的 token 存储位置（仅用于读取 endpoints；不写文件）
_TOKEN_FILE = os.path.join(
    os.path.expanduser("~"), ".spyder_copilot_chat", "tokens.json"
)

# 会话级 / 设备级标识：官方会发送，任意稳定值即可（纯遥测/归因）
_SESSION_ID = str(uuid.uuid4())
_MACHINE_ID = uuid.uuid4().hex
_DEVICE_ID = str(uuid.uuid4())


def _new_request_id():
    """每次请求一个新的 UUID（便于与响应头 x-github-request-id 对账）。"""
    return str(uuid.uuid4())


# ---------------------------------------------------------------------------
# 请求头
# ---------------------------------------------------------------------------

def build_chat_headers(copilot_token, initiator="user"):
    """构造 CAPI Chat Completions / Models 请求头。

    参数
    ----
    copilot_token : str
        Copilot API token（来自 ``/copilot_internal/v2/token`` 的 ``token`` 字段）。
        **注意**：这里是 ``Bearer``，与换取 token 时的 ``token <github_token>`` 不同。
    initiator : str
        ``"user"``（用户直接发起）或 ``"agent"``（Agent 自动续跑）；
        服务端用它做配额/防刷归因。

    返回
    ----
    dict
        Authorization / Editor-Version / Editor-Plugin-Version /
        Copilot-Integration-Id / X-GitHub-Api-Version(2026-01-09) /
        X-Request-Id / X-Initiator / OpenAI-Intent(conversation-panel) /
        User-Agent / Content-Type / Accept 等。
    """
    return {
        # ★ CAPI 用 Bearer（token 交换用 "token "，别混）
        "Authorization": f"Bearer {copilot_token}",
        "Content-Type": "application/json",
        # 同时兼容 JSON 与 SSE 流式响应
        "Accept": "application/json, text/event-stream",
        "User-Agent": USER_AGENT,
        "Editor-Version": EDITOR_VERSION,
        "Editor-Plugin-Version": EDITOR_PLUGIN_VERSION,
        "Copilot-Integration-Id": COPILOT_INTEGRATION_ID,
        "X-GitHub-Api-Version": CAPI_API_VERSION,
        "X-Request-Id": _new_request_id(),
        "X-Initiator": initiator or "user",
        "OpenAI-Intent": "conversation-panel",
        # 可省（纯遥测），但官方总发，补上更像真实客户端
        "VScode-SessionId": _SESSION_ID,
        "VScode-MachineId": _MACHINE_ID,
        "Editor-Device-Id": _DEVICE_ID,
    }


def build_fim_headers(copilot_token):
    """构造 FIM（Copilot Proxy legacy ``/completions``）请求头。

    与 Chat 头不同的是版本号（``2025-05-01``）且必须带
    ``Openai-Organization: github-copilot``。
    """
    return {
        "Authorization": f"Bearer {copilot_token}",
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
        "User-Agent": USER_AGENT,
        "Editor-Version": EDITOR_VERSION,
        "Editor-Plugin-Version": EDITOR_PLUGIN_VERSION,
        "Copilot-Integration-Id": COPILOT_INTEGRATION_ID,
        "X-GitHub-Api-Version": FIM_API_VERSION,
        # ★ FIM 必需：Openai-Organization（注意官方拼写是 Openai 不是 OpenAI）
        "Openai-Organization": "github-copilot",
        "X-Request-Id": _new_request_id(),
        "VScode-SessionId": _SESSION_ID,
        "VScode-MachineId": _MACHINE_ID,
        "OpenAI-Intent": "copilot-ghost",
    }


# ---------------------------------------------------------------------------
# Base URL 解析（endpoints.api / endpoints.proxy 覆盖）
# ---------------------------------------------------------------------------

def _normalize_base(url):
    """去掉末尾斜杠，避免拼出 ``//chat/completions``。"""
    return str(url).rstrip("/") if url else ""


def _read_endpoints():
    """读取 tokens.json 里的 ``endpoints``（不 import auth，避免循环依赖）。"""
    try:
        with open(_TOKEN_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    endpoints = data.get("endpoints")
    return endpoints if isinstance(endpoints, dict) else {}


def _stored_github_token():
    try:
        with open(_TOKEN_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data.get("github_token")
    except (FileNotFoundError, json.JSONDecodeError, OSError, ValueError):
        pass
    return None


def api_base(github_token=None):
    """返回 CAPI Base URL。

    默认 ``https://api.githubcopilot.com``；若 tokens.json 中保存了
    token 响应的 ``endpoints.api``（企业版可能不同）则优先使用。

    ``github_token`` 为可选参数：传入时，只有当它与 tokens.json 里保存的
    GitHub token 一致才采用缓存的 endpoints（避免多账号串用）；不一致时
    回退默认地址。
    """
    endpoints = _read_endpoints()
    base = endpoints.get("api")
    if not base:
        return DEFAULT_API_BASE
    if github_token is not None:
        stored = _stored_github_token()
        if stored and stored != github_token:
            logger.debug("api_base: github_token 与缓存不匹配，使用默认 CAPI 地址")
            return DEFAULT_API_BASE
    return _normalize_base(base) or DEFAULT_API_BASE


def proxy_base():
    """返回 Copilot Proxy Base URL（FIM 用）。

    默认 ``https://copilot-proxy.githubusercontent.com``；若 tokens.json 中
    保存了 ``endpoints.proxy`` 则优先使用。
    """
    endpoints = _read_endpoints()
    base = endpoints.get("proxy")
    if not base:
        return DEFAULT_PROXY_BASE
    return _normalize_base(base) or DEFAULT_PROXY_BASE


def token_exchange_headers(github_token):
    """构造换取 Copilot token 的 GitHub REST 请求头。

    官方只对 ``CopilotToken`` 请求发这两个头（+ UA/Accept），
    **不要**把 CAPI 的 Editor-* 头混进来；但 ``X-GitHub-Api-Version``
    必须是 ``2025-04-01``。
    """
    return {
        "Authorization": f"token {github_token}",
        "X-GitHub-Api-Version": GITHUB_API_VERSION,
        "Accept": "application/json",
        "User-Agent": USER_AGENT,
    }


__all__ = [
    "EDITOR_VERSION",
    "EDITOR_PLUGIN_VERSION",
    "COPILOT_INTEGRATION_ID",
    "CAPI_API_VERSION",
    "FIM_API_VERSION",
    "GITHUB_API_VERSION",
    "USER_AGENT",
    "DEFAULT_API_BASE",
    "DEFAULT_PROXY_BASE",
    "build_chat_headers",
    "build_fim_headers",
    "api_base",
    "proxy_base",
    "token_exchange_headers",
]
