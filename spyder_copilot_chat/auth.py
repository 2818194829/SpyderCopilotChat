# -*- coding: utf-8 -*-
"""GitHub Copilot 认证模块。

实现 GitHub Device Authorization Flow 获取 GitHub token，再用 GitHub token
换取 Copilot API token（官方 ``github.copilot-chat`` 0.48.1 的真实契约，
见工作区 ``reports/copilot-protocol-spec.md`` §2 / §9.1）。

流程：
  1. ``POST https://github.com/login/device/code`` → device_code + user_code
  2. 用户访问 verification_uri 并输入 user_code
  3. ``POST https://github.com/login/oauth/access_token`` → 轮询直到授权
  4. ``GET https://api.github.com/copilot_internal/v2/token``
     （``Authorization: token <github_token>`` +
     ``X-GitHub-Api-Version: 2025-04-01``）→ Copilot API token
  5. 缓存到 ``~/.spyder_copilot_chat/tokens.json``（含 endpoints.api / endpoints.proxy）

刷新语义（与官方一致）：
  * ``expires_at`` 覆盖为 ``now + refresh_in + 60``（服务端值不可靠，官方即如此）；
  * 剩余有效期 < 300 秒即主动刷新；
  * 刷新失败区分「GitHub token 失效」（需重新登录）与「网络错误」（可重试）。

向后兼容：``load_tokens`` / ``get_valid_copilot_token`` / ``login`` /
``logout`` / ``clear_tokens`` / ``start_device_flow`` / ``poll_for_token`` /
``get_copilot_token`` / ``refresh_copilot_token`` 的名字与可调用签名保持不变。
"""

import json
import logging
import os
import time
import urllib.error
import urllib.parse
import urllib.request

from .copilot_headers import (
    DEFAULT_API_BASE,
    DEFAULT_PROXY_BASE,
    USER_AGENT,
    token_exchange_headers,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# OAuth / GitHub API 配置
# ---------------------------------------------------------------------------

#: Copilot 基础 scope（与官方 read:user 一致，spec §1.2）
CLIENT_ID = "01ab8ac9400c4e429b23"
#: 备选 client_id（社区已知的 Copilot 线路，必要时可切换防风控，spec §1.3）
FALLBACK_CLIENT_ID = "Iv1.b507a08c87ecfe98"
SCOPES = "read:user"

GITHUB_API_BASE = "https://api.github.com"
DEVICE_CODE_URL = "https://github.com/login/device/code"
TOKEN_URL = "https://github.com/login/oauth/access_token"
COPILOT_TOKEN_URL = f"{GITHUB_API_BASE}/copilot_internal/v2/token"
COPILOT_USER_URL = f"{GITHUB_API_BASE}/copilot_internal/user"

#: 兼容旧常量名（旧代码/第三方可能按这个名字取 CAPI 地址）
COPILOT_API_URL = DEFAULT_API_BASE

#: 提前刷新阈值：剩余 < 300s 就刷新（官方值）
REFRESH_THRESHOLD = 300
#: expires_at 的官方覆盖公式：now + refresh_in + 60
REFRESH_PADDING = 60
#: 服务端既无 refresh_in 也无 expires_at 时的保守兜底（秒）
DEFAULT_TOKEN_TTL = 1500


# ---------------------------------------------------------------------------
# 异常类型：区分「token 失效」与「网络错误」（刷新失败也能给出准确提示）
# ---------------------------------------------------------------------------

class CopilotAuthError(RuntimeError):
    """认证相关错误基类（保持 RuntimeError 兼容旧调用方的 except）。"""


class CopilotNotLoggedInError(CopilotAuthError):
    """本地没有可用凭据：需要走 device flow 登录。"""


class CopilotTokenInvalidError(CopilotAuthError):
    """GitHub token 已失效/被吊销（HTTP 401）：必须重新登录。"""


class CopilotSubscriptionError(CopilotAuthError):
    """当前账号没有 Copilot 订阅或组织未授权（HTTP 403）。"""


class CopilotRateLimitedError(CopilotAuthError):
    """GitHub API 限流（429 / API rate limit exceeded）。"""

    def __init__(self, message, retry_after=None):
        super().__init__(message)
        self.retry_after = retry_after


class CopilotNetworkError(CopilotAuthError):
    """网络层错误（断网/超时/DNS/TLS）：与 token 有效性无关，可重试。"""


#: 最近一次刷新失败的异常（None 表示上次刷新成功）；供 UI 区分展示
LAST_REFRESH_ERROR = None


def last_refresh_error():
    """返回最近一次 token 刷新失败的异常对象（无失败则为 None）。"""
    return LAST_REFRESH_ERROR


# ---------------------------------------------------------------------------
# 存储认证信息（tokens.json）
# ---------------------------------------------------------------------------

CONFIG_DIR = os.path.join(os.path.expanduser("~"), ".spyder_copilot_chat")
TOKEN_FILE = os.path.join(CONFIG_DIR, "tokens.json")


def _ensure_dir():
    """确保配置目录存在（Windows 下 chmod 尽力而为）。"""
    os.makedirs(CONFIG_DIR, exist_ok=True)
    try:  # 仅 POSIX 真正生效；Windows 上退化为只读位设置
        os.chmod(CONFIG_DIR, 0o700)
    except OSError:
        pass


def _read_token_file():
    """读取 tokens.json，失败/损坏时返回空 dict。"""
    try:
        with open(TOKEN_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, OSError, ValueError):
        return {}


def _write_token_file(data):
    """原子写入 tokens.json，并尽力收紧文件权限（P1-7）。"""
    _ensure_dir()
    tmp = TOKEN_FILE + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, TOKEN_FILE)
    finally:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass
    try:  # 尽力而为：POSIX 下 0600，Windows 只影响只读位
        os.chmod(TOKEN_FILE, 0o600)
    except OSError:
        pass
    logger.debug("Tokens saved to %s", TOKEN_FILE)


def _save_tokens(github_token, copilot_token, expires_at,
                 capi_base=None, proxy_base=None, sku=None):
    """保存 token 到本地文件（保留 github_token / copilot_token / expires_at，
    并新增/更新 endpoints）。

    与旧版兼容：前三个位置参数含义完全不变；新增参数均有默认值。
    """
    data = _read_token_file()
    data["github_token"] = github_token
    data["copilot_token"] = copilot_token
    data["expires_at"] = int(expires_at or 0)

    endpoints = data.get("endpoints")
    if not isinstance(endpoints, dict):
        endpoints = {}
    if capi_base:
        endpoints["api"] = capi_base
    if proxy_base:
        endpoints["proxy"] = proxy_base
    if endpoints:
        data["endpoints"] = endpoints
    if sku:
        data["sku"] = sku

    _write_token_file(data)


def load_tokens_full():
    """返回 tokens.json 的完整字典（含 endpoints / sku），无文件时返回 {}。"""
    return _read_token_file()


def load_tokens():
    """从本地文件加载 token，返回 ``(github_token, copilot_token, expires_at)``。

    保持旧版三元组签名；需要 endpoints / sku 请用 :func:`load_tokens_full`。
    """
    data = _read_token_file()
    try:
        expires_at = float(data.get("expires_at") or 0)
    except (TypeError, ValueError):
        expires_at = 0.0
    return data.get("github_token"), data.get("copilot_token"), expires_at


def clear_tokens():
    """清除保存的 token（保留文件本身？不保留，直接删除）。"""
    global LAST_REFRESH_ERROR
    LAST_REFRESH_ERROR = None
    try:
        if os.path.exists(TOKEN_FILE):
            os.remove(TOKEN_FILE)
            logger.debug("Tokens cleared")
    except OSError as e:
        logger.warning("Failed to clear tokens: %s", e)


def invalidate_copilot_token():
    """丢弃缓存的 Copilot token，但**保留 GitHub token**。

    对应官方 ``resetCopilotToken(status)`` 的语义（spec §7）：CAPI 返回
    401/403 时清掉 Copilot token，下次 :func:`get_valid_copilot_token` 会用
    保存的 GitHub token 立刻重新交换并重试。若连 GitHub token 也失效，
    再调用 :func:`logout` / :func:`clear_tokens` 要求用户重新登录。
    """
    data = _read_token_file()
    if not data:
        return
    data["copilot_token"] = ""
    data["expires_at"] = 0
    try:
        _write_token_file(data)
        logger.debug("Cached Copilot token invalidated (GitHub token kept)")
    except OSError as e:
        logger.warning("Failed to invalidate Copilot token: %s", e)


def get_endpoints(data=None):
    """从 tokens 数据解析 ``(capi_base, proxy_base)``，缺省回退官方默认值。"""
    data = data if isinstance(data, dict) else _read_token_file()
    endpoints = data.get("endpoints")
    if not isinstance(endpoints, dict):
        endpoints = {}
    capi = str(endpoints.get("api") or DEFAULT_API_BASE).rstrip("/")
    proxy = str(endpoints.get("proxy") or DEFAULT_PROXY_BASE).rstrip("/")
    return capi, proxy


# ---------------------------------------------------------------------------
# 纯函数：token 响应解析 / 有效期判定（便于单元测试，不触网）
# ---------------------------------------------------------------------------

def parse_token_response(data, now=None):
    """解析 ``/copilot_internal/v2/token`` 的 JSON 响应。

    官方做法（spec §2.4）：把 ``expires_at`` 覆盖为 ``now + refresh_in + 60``；
    缺少 ``refresh_in`` 时才使用服务端 ``expires_at``。

    返回::

        {"token": str, "expires_at": int(秒), "refresh_in": float,
         "sku": str|None, "endpoints": {"api": str, "proxy": str}}

    响应缺 ``token`` 时抛 :class:`CopilotAuthError`。
    """
    if not isinstance(data, dict):
        raise CopilotAuthError("token 交换响应不是 JSON 对象")
    token = data.get("token")
    if not token or not isinstance(token, str):
        raise CopilotAuthError("token 交换响应缺少 token 字段")

    now = time.time() if now is None else float(now)
    try:
        refresh_in = float(data.get("refresh_in") or 0)
    except (TypeError, ValueError):
        refresh_in = 0.0
    try:
        server_expires = float(data.get("expires_at") or 0)
    except (TypeError, ValueError):
        server_expires = 0.0

    if refresh_in > 0:
        # ★ 官方覆盖公式，服务端 expires_at 不直接用
        expires_at = int(now + refresh_in + REFRESH_PADDING)
    elif server_expires > 0:
        expires_at = int(server_expires)
    else:
        logger.warning("token 响应缺少 expires_at/refresh_in，使用兜底 TTL")
        expires_at = int(now + DEFAULT_TOKEN_TTL)

    endpoints = data.get("endpoints")
    if not isinstance(endpoints, dict):
        endpoints = {}
    capi = str(endpoints.get("api") or DEFAULT_API_BASE).rstrip("/")
    proxy = str(endpoints.get("proxy") or DEFAULT_PROXY_BASE).rstrip("/")

    return {
        "token": token,
        "expires_at": expires_at,
        "refresh_in": refresh_in,
        "sku": data.get("sku"),
        "endpoints": {"api": capi, "proxy": proxy},
    }


def should_use_cached_token(expires_at, now=None):
    """剩余有效期 >= 300s 才复用缓存；否则主动刷新（官方阈值）。"""
    now = time.time() if now is None else float(now)
    try:
        expires_at = float(expires_at or 0)
    except (TypeError, ValueError):
        return False
    return expires_at > 0 and (expires_at - now) >= REFRESH_THRESHOLD


# ---------------------------------------------------------------------------
# HTTP 小工具
# ---------------------------------------------------------------------------

def _parse_retry_after(headers):
    """解析 retry-after（秒数或 HTTP-date），失败返回 None。"""
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
            return max(0.0, dt.timestamp() - time.time())
    except (TypeError, ValueError, OverflowError):
        pass
    return None


def _short_body(body, limit=500):
    body = (body or "").strip()
    return body[:limit]


def _token_error(status, body, headers=None):
    """把 token 交换的 HTTP 错误映射为语义化异常（spec §2.5）。"""
    retry_after = _parse_retry_after(headers)
    if status == 401:
        return CopilotTokenInvalidError(
            "GitHub token 无效或已过期，请重新登录 GitHub Copilot"
        )
    if body and "API rate limit exceeded" in body:
        return CopilotRateLimitedError(
            "GitHub API 触发限流，请稍后重试", retry_after=retry_after
        )
    if status == 403:
        return CopilotSubscriptionError(
            "当前 GitHub 账号没有可用的 Copilot 订阅（或组织未授权 Copilot）；"
            "请确认订阅/组织授权后重新登录"
        )
    if status == 429:
        return CopilotRateLimitedError(
            "GitHub API 触发限流，请稍后重试", retry_after=retry_after
        )
    if status >= 500:
        return CopilotAuthError(
            f"GitHub API 服务端错误 HTTP {status}: {_short_body(body)}"
        )
    return CopilotAuthError(
        f"换取 Copilot token 失败：HTTP {status}: {_short_body(body)}"
    )


def _get_json(url, headers, timeout=30):
    """GET 一个 JSON 响应；HTTP/网络/JSON 错误都转成语义化异常。"""
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", errors="replace")
        except Exception:  # noqa: BLE001 - 读错误体失败不影响主错误
            pass
        raise _token_error(e.code, body, e.headers) from e
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        reason = getattr(e, "reason", e)
        raise CopilotNetworkError(
            f"网络错误：无法连接 {url}（{reason}）；请检查网络/代理后重试"
        ) from e
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        raise CopilotAuthError(
            f"token 响应不是合法 JSON: {_short_body(raw, 200)}"
        ) from e


def _post_json(url, data, headers=None):
    """发送 form POST 请求并返回 JSON（device flow 用）。"""
    if headers is None:
        headers = {}
    body = urllib.parse.urlencode(data).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers={
        "Accept": "application/json",
        "Content-Type": "application/x-www-form-urlencoded",
        "User-Agent": USER_AGENT,
        **headers,
    })
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        error_body = e.read().decode("utf-8", errors="replace")
        logger.warning("HTTP %d: %s", e.code, error_body)
        raise CopilotAuthError(
            f"GitHub OAuth HTTP {e.code}: {_short_body(error_body)}"
        ) from e
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        reason = getattr(e, "reason", e)
        raise CopilotNetworkError(
            f"网络错误：无法连接 {url}（{reason}）"
        ) from e
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        raise CopilotAuthError("GitHub OAuth 响应不是合法 JSON") from e


# ---------------------------------------------------------------------------
# Device Authorization Flow
# ---------------------------------------------------------------------------

class DeviceFlowResult:
    """Device flow 的结果。"""

    def __init__(self, device_code, user_code, verification_uri, interval=5):
        self.device_code = device_code
        self.user_code = user_code
        self.verification_uri = verification_uri
        self.interval = interval


def start_device_flow():
    """Step 1: 启动 Device Authorization Flow，返回用户需要访问的 URI 和 code。"""
    data = {
        "client_id": CLIENT_ID,
        "scope": SCOPES,
    }
    result = _post_json(DEVICE_CODE_URL, data)
    if not result.get("device_code") or not result.get("user_code"):
        raise CopilotAuthError(
            "GitHub device flow 响应异常（缺少 device_code/user_code）"
        )
    logger.debug("Device flow started: %s", result.get("user_code"))
    return DeviceFlowResult(
        device_code=result["device_code"],
        user_code=result["user_code"],
        verification_uri=result.get(
            "verification_uri", "https://github.com/login/device"
        ),
        interval=result.get("interval", 5),
    )


def poll_for_token(device_code, interval=5, timeout=300, cancel_event=None):
    """Step 2: 轮询等待用户授权，返回 GitHub access_token。

    参数
    ----
    cancel_event : threading.Event | None
        可选取消信号（供 GUI worker 使用）；置位后抛
        :class:`CopilotAuthError` 中止轮询，而不是干等 timeout。
    """
    data = {
        "client_id": CLIENT_ID,
        "device_code": device_code,
        "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
    }
    start = time.time()
    while time.time() - start < timeout:
        if cancel_event is not None and cancel_event.is_set():
            raise CopilotAuthError("登录已取消")
        result = _post_json(TOKEN_URL, data)
        if "access_token" in result:
            return result["access_token"]
        error = result.get("error", "")
        if error == "authorization_pending":
            time.sleep(interval)
            continue
        elif error == "slow_down":
            interval += 5
            time.sleep(interval)
            continue
        elif error == "expired_token":
            raise CopilotAuthError("设备码已过期，请重新登录")
        elif error == "access_denied":
            raise CopilotAuthError("用户拒绝了授权")
        else:
            raise CopilotAuthError(f"GitHub OAuth 未知错误：{error}")
    raise CopilotAuthError("等待授权超时，请重新登录")


# ---------------------------------------------------------------------------
# GitHub token → Copilot token 交换
# ---------------------------------------------------------------------------

def fetch_copilot_token(github_token, timeout=30):
    """GET ``/copilot_internal/v2/token`` 并返回原始 JSON（不落盘）。

    官方只要求 ``Authorization: token <github_token>`` +
    ``X-GitHub-Api-Version: 2025-04-01``（+ UA/Accept），见 spec §2.2。
    """
    if not github_token:
        raise CopilotNotLoggedInError("缺少 GitHub token，请先登录")
    return _get_json(
        COPILOT_TOKEN_URL,
        token_exchange_headers(github_token),
        timeout=timeout,
    )


def get_copilot_token_ex(github_token):
    """换取 Copilot token，解析并写入 tokens.json，返回解析后的 dict。

    与 :func:`get_copilot_token` 的区别是返回 dict（含 endpoints / sku），
    便于调用方按 ``endpoints.api`` / ``endpoints.proxy`` 选 Base URL。
    """
    raw = fetch_copilot_token(github_token)
    parsed = parse_token_response(raw)
    # 持久化 token、覆盖后的 expires_at 以及 endpoints.api / endpoints.proxy
    _save_tokens(
        github_token,
        parsed["token"],
        parsed["expires_at"],
        capi_base=parsed["endpoints"]["api"],
        proxy_base=parsed["endpoints"]["proxy"],
        sku=parsed.get("sku"),
    )
    logger.debug(
        "Copilot token obtained, expires_at=%s sku=%s api=%s",
        parsed["expires_at"], parsed.get("sku"),
        parsed["endpoints"]["api"],
    )
    return parsed


def get_copilot_token(github_token):
    """Step 3: 用 GitHub access_token 换取 Copilot API token。

    返回 ``(copilot_token, expires_at)``（保持旧版二元组签名）。

    异常（均继承 RuntimeError，旧 ``except Exception`` 调用方仍可工作）：
      * :class:`CopilotTokenInvalidError` —— GitHub token 失效（401）
      * :class:`CopilotSubscriptionError` —— 无 Copilot 订阅（403）
      * :class:`CopilotRateLimitedError`  —— 限流
      * :class:`CopilotNetworkError`      —— 网络错误
    """
    parsed = get_copilot_token_ex(github_token)
    return parsed["token"], parsed["expires_at"]


def fetch_copilot_user(github_token, timeout=30):
    """GET ``/copilot_internal/user``：返回账号套餐/配额信息（用于 UI 展示）。

    失败时抛与 token 交换同类的语义化异常；不写任何文件。
    """
    if not github_token:
        raise CopilotNotLoggedInError("缺少 GitHub token，请先登录")
    return _get_json(
        COPILOT_USER_URL,
        token_exchange_headers(github_token),
        timeout=timeout,
    )


# ---------------------------------------------------------------------------
# 刷新 / 便捷函数
# ---------------------------------------------------------------------------

def _refresh_from_github(github_token):
    """内部：刷新并返回 ``(token, expires_at, endpoints_dict)``。"""
    global LAST_REFRESH_ERROR
    try:
        parsed = get_copilot_token_ex(github_token)
    except Exception as e:  # noqa: BLE001 - 统一记录后原样上抛
        LAST_REFRESH_ERROR = e
        logger.warning("Token refresh failed: %s", e)
        raise
    LAST_REFRESH_ERROR = None
    return parsed["token"], parsed["expires_at"], parsed["endpoints"]


def refresh_copilot_token(raise_on_error=False):
    """尝试用保存的 GitHub token 刷新 Copilot token。

    返回 ``(copilot_token|None, expires_at)``；
    ``raise_on_error=True`` 时把语义化异常（token 失效 / 网络错误）原样抛出，
    便于 UI 区分「请重新登录」与「网络问题，稍后重试」。
    """
    global LAST_REFRESH_ERROR

    github_token, _, _ = load_tokens()
    if not github_token:
        err = CopilotNotLoggedInError("未登录 GitHub Copilot")
        LAST_REFRESH_ERROR = err
        if raise_on_error:
            raise err
        return None, 0
    try:
        token, expires_at, _endpoints = _refresh_from_github(github_token)
        return token, expires_at
    except Exception:
        if raise_on_error:
            raise
        return None, 0


def get_valid_copilot_token(raise_on_error=False):
    """获取有效的 Copilot token，必要时自动刷新。

    返回 token 字符串；未登录/刷新失败返回 ``None``（保持旧行为）。
    ``raise_on_error=True`` 时抛出 :class:`CopilotAuthError` 子类，UI 可据此给出
    准确提示（例如 ``CopilotTokenInvalidError`` → 请重新登录，
    ``CopilotNetworkError`` → 网络问题）。

    注意：token 剩余有效期 < 300s 时会**同步**请求 GitHub（耗时最多 30s），
    在 GUI 线程调用请改为放到 worker 线程（见 P1-1）。
    """
    token, _expires_at, _capi, _proxy = get_valid_copilot_token_ex(
        raise_on_error=raise_on_error
    )
    return token


def get_valid_copilot_token_ex(raise_on_error=False):
    """获取 ``(copilot_token, expires_at, capi_base, proxy_base)``。

    供 copilot_client / FIM Provider 按 token 响应里的
    ``endpoints.api`` / ``endpoints.proxy`` 选择 Base URL（spec §3.2）。
    失败时（``raise_on_error=False``）返回
    ``(None, 0, DEFAULT_API_BASE, DEFAULT_PROXY_BASE)``。
    """
    try:
        data = _read_token_file()
        github_token = data.get("github_token")
        copilot_token = data.get("copilot_token")
        try:
            expires_at = float(data.get("expires_at") or 0)
        except (TypeError, ValueError):
            expires_at = 0.0

        if not github_token and not copilot_token:
            raise CopilotNotLoggedInError(
                "未登录 GitHub Copilot，请先在设置中完成 GitHub 登录"
            )

        if copilot_token and should_use_cached_token(expires_at):
            capi, proxy = get_endpoints(data)
            return copilot_token, expires_at, capi, proxy

        # token 缺失/即将过期：用保存的 GitHub token 主动刷新
        if not github_token:
            raise CopilotNotLoggedInError(
                "GitHub token 缺失（Copilot token 已过期），请重新登录"
            )
        token, new_expires_at, endpoints = _refresh_from_github(github_token)
        return token, new_expires_at, endpoints["api"], endpoints["proxy"]
    except Exception as e:  # noqa: BLE001 - 统一按 raise_on_error 处理
        if not isinstance(e, CopilotAuthError):
            e = CopilotAuthError(str(e))
        if raise_on_error:
            raise e
        logger.warning("No valid Copilot token: %s", e)
        return None, 0, DEFAULT_API_BASE, DEFAULT_PROXY_BASE


# ---------------------------------------------------------------------------
# 登录 / 登出
# ---------------------------------------------------------------------------

def login():
    """完整登录流程：启动 device flow → 返回 ``(user_code, verification_uri, poll_func)``。

    ``poll_func()`` 阻塞直到用户授权并换取 Copilot token，返回 Copilot token。
    在 GUI 线程调用会卡住，必须放到 worker 线程（P1-1）。
    """
    flow = start_device_flow()

    def _poll(cancel_event=None):
        github_token = poll_for_token(
            flow.device_code, flow.interval, cancel_event=cancel_event
        )
        # 先保存 GitHub token：即使换取 Copilot token 失败（如 403）也保留凭据，
        # 订阅恢复后可直接自动刷新，不必重新走 device flow。
        _save_tokens(github_token, "", 0)
        parsed = get_copilot_token_ex(github_token)
        return parsed["token"]

    return flow.user_code, flow.verification_uri, _poll


def logout():
    """登出：清除保存的 token。"""
    clear_tokens()


__all__ = [
    "CLIENT_ID",
    "FALLBACK_CLIENT_ID",
    "SCOPES",
    "GITHUB_API_BASE",
    "DEVICE_CODE_URL",
    "TOKEN_URL",
    "COPILOT_TOKEN_URL",
    "COPILOT_USER_URL",
    "COPILOT_API_URL",
    "DEFAULT_API_BASE",
    "DEFAULT_PROXY_BASE",
    "CONFIG_DIR",
    "TOKEN_FILE",
    "REFRESH_THRESHOLD",
    "REFRESH_PADDING",
    "CopilotAuthError",
    "CopilotNotLoggedInError",
    "CopilotTokenInvalidError",
    "CopilotSubscriptionError",
    "CopilotRateLimitedError",
    "CopilotNetworkError",
    "LAST_REFRESH_ERROR",
    "last_refresh_error",
    "DeviceFlowResult",
    "load_tokens",
    "load_tokens_full",
    "get_endpoints",
    "clear_tokens",
    "invalidate_copilot_token",
    "parse_token_response",
    "should_use_cached_token",
    "start_device_flow",
    "poll_for_token",
    "fetch_copilot_token",
    "get_copilot_token",
    "get_copilot_token_ex",
    "fetch_copilot_user",
    "refresh_copilot_token",
    "get_valid_copilot_token",
    "get_valid_copilot_token_ex",
    "login",
    "logout",
]
