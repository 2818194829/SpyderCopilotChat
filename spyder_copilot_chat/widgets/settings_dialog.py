# -*- coding: utf-8 -*-
"""
Copilot Chat 设置对话框 — 完整 Provider 注册表

参考 vscode-copilot-chat 的 languageModelChatProviders 配置:
  - copilot, anthropic, xai, gemini, openrouter, openai, azure, ollama, customoai
"""

import json
import logging
import os
import threading
import time

from qtpy.QtCore import Qt, QThread, QUrl, Signal
from qtpy.QtGui import QDesktopServices
from qtpy.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox,
    QDoubleSpinBox, QFormLayout, QFrame, QGroupBox, QHBoxLayout,
    QLabel, QLineEdit, QMessageBox, QPlainTextEdit,
    QPushButton, QScrollArea, QSpinBox, QTabWidget,
    QVBoxLayout, QWidget,
)

from ..auth import load_tokens, login, logout
from . import theme
from .audit import audit_dir
from .audit_viewer import AuditViewerDialog

logger = logging.getLogger(__name__)

# ===================================================================
# Provider 注册表 — 完整版，包含所有主流 AI 服务商
# ===================================================================

PROVIDERS = {
    # ── GitHub Copilot ──────────────────────────────────────────────
    "copilot": {
        "label": "GitHub Copilot",
        "default_url": "https://api.githubcopilot.com",
        "chat_endpoint": "/chat/completions",
        "needs_api_key": False,
        "needs_oauth": True,
        # 模型列表不再硬编码：启动后异步 GET /models 动态填充（见 ModelsWorker），
        # 拉取失败时才回退到 FALLBACK_COPILOT_MODELS。
        "models": [],
        "default_model": "",
        "supports_agent": True,
    },
    # ── DeepSeek ────────────────────────────────────────────────────
    # 依据官方文档 https://api-docs.deepseek.com/zh-cn/ （2026-11-13）：
    #   base_url (OpenAI 格式) = https://api.deepseek.com
    #   POST /chat/completions，model 仅 deepseek-flash / deepseek-v4-pro。
    #   deepseek-chat / deepseek-reasoner 已从官方模型清单移除（实测仍作为
    #   兼容别名可用，但不再对应当前的能力与价格，故不再使用）。
    "deepseek": {
        "label": "DeepSeek",
        "default_url": "https://api.deepseek.com",
        "chat_endpoint": "/chat/completions",
        "needs_api_key": True,
        "needs_oauth": False,
        "models": ["deepseek-flash", "deepseek-v4-pro"],
        "default_model": "deepseek-flash",
        "supports_agent": True,
        "supports_thinking": True,
        "reasoning_efforts": ("low", "high", "max"),
        "context_window": 1048576,
    },
    # ── 腾讯混元 Hy4（TokenHub）────────────────────────────────────
    # 产品文档入口：https://aistudio.tencent.com/hunyuan/doc-center
    # 接口契约（TokenHub 官方文档）：
    #   API Usage Instructions  /document/product/1300/78941 —— 地域域名、
    #       Authorization: Bearer、GET /v1/models、OpenAI / Anthropic 双协议
    #   Model list             /document/product/1300/78934 —— hy4 /
    #       hy4-preview，1M 上下文（960k 输入 / 64k 输出）、图片理解、
    #       Function Calling、Caching、Deep Reasoning (Preserved Thinking)
    #   Deep Reasoning         /document/product/1300/80637 —— thinking
    #       {"type":"enabled"|"disabled"}；Hy4 默认 enabled，
    #       reasoning_effort 仅 high / no_think（preview 为 none / high）
    # 地域（实测三个域名均可达，无 Key 时返回 401）：
    #   广州   https://tokenhub.tencentmaas.com/v1          ← 实测可用，默认
    #          另一套入口 https://tokenhub.tencentcloudmaas.com/v1
    #   新加坡 https://tokenhub-intl.tencentcloudmaas.com/v1
    #   硅谷   https://tokenhub-us.tencentcloudmaas.com/v1
    # v0.8.16：两套广州入口各自签发 Key，并不通用。实测同一把 Key 在短别名
    # 域名 GET /v1/models 返回 200，在长域名返回 401002「API Key 不存在」。
    # 旧版把长域名设成默认，于是新用户一填 Key 就被误报成「模型不可用」。
    # Key 在 https://console.cloud.tencent.com/tokenhub/apikey 创建，
    # 按地域签发、不通用；填错地域会直接 401。
    "hunyuan": {
        "label": "腾讯混元 Hy4（TokenHub）",
        "default_url": "https://tokenhub.tencentmaas.com/v1",
        "chat_endpoint": "/chat/completions",
        "needs_api_key": True,
        "needs_oauth": False,
        "models": ["hy4", "hy4-preview"],
        "default_model": "hy4",
        "supports_agent": True,
        "supports_thinking": True,
        "reasoning_efforts": ("high", "no_think"),
        # TokenHub 需要显式 stream_options.include_usage 才在流末尾给 usage
        "stream_usage": True,
        "max_output_tokens": 65536,
        "context_window": 1048576,
        # 官方刊例价（人民币 / 百万 tokens）：输入 6 元、输出 18 元、
        # 缓存命中 0.3 元。仅作为默认显示值，用户可在设置里改。
        "price_input_per_million": 6.0,
        "price_output_per_million": 18.0,
        "key_console": "https://console.cloud.tencent.com/tokenhub/apikey",
        "docs": "https://aistudio.tencent.com/hunyuan/doc-center",
        "alt_urls": (
            "https://tokenhub.tencentcloudmaas.com/v1",
            "https://tokenhub-intl.tencentcloudmaas.com/v1",
            "https://tokenhub-us.tencentcloudmaas.com/v1",
        ),
    },
    # ── OpenAI ──────────────────────────────────────────────────────
    "openai": {
        "label": "OpenAI",
        "default_url": "https://api.openai.com/v1",
        "chat_endpoint": "/chat/completions",
        "needs_api_key": True,
        "needs_oauth": False,
        "models": ["gpt-4o", "gpt-4o-mini", "gpt-4.1", "gpt-4.1-mini",
                    "gpt-4.1-nano", "o3", "o4-mini", "gpt-3.5-turbo"],
        "default_model": "gpt-4o",
        "supports_agent": True,
    },
    # ── Anthropic ───────────────────────────────────────────────────
    "anthropic": {
        "label": "Anthropic (Claude)",
        "default_url": "https://api.anthropic.com",
        "chat_endpoint": "/v1/messages",
        "needs_api_key": True,
        "needs_oauth": False,
        "api_key_name": "x-api-key",
        "models": ["claude-sonnet-4-20250514", "claude-sonnet-4-20250212",
                    "claude-3-5-sonnet-20241022", "claude-3-5-haiku-20241022",
                    "claude-3-opus-20240229"],
        "default_model": "claude-sonnet-4-20250514",
        "supports_agent": True,
        "anthropic_version": "2023-06-01",
    },
    # ── xAI (Grok) ─────────────────────────────────────────────────
    "xai": {
        "label": "xAI (Grok)",
        "default_url": "https://api.x.ai",
        "chat_endpoint": "/v1/chat/completions",
        "needs_api_key": True,
        "needs_oauth": False,
        "models": ["grok-3", "grok-3-mini", "grok-2", "grok-2-mini"],
        "default_model": "grok-3",
        "supports_agent": True,
    },
    # ── Google Gemini ───────────────────────────────────────────────
    # 注意：Gemini 原生端点（POST /v1beta/models/{model}:generateContent）
    # 不是 OpenAI 兼容协议，当前 ChatWorker 尚未实现对应分支。
    # 保留定义仅为兼容历史配置，UI 中已不再提供选择，见 UNSUPPORTED_PROVIDERS。
    "gemini": {
        "label": "Google Gemini",
        "default_url": "https://generativelanguage.googleapis.com",
        "chat_endpoint": "/v1beta/models",
        "needs_api_key": True,
        "needs_oauth": False,
        "models": ["gemini-2.5-flash", "gemini-2.5-pro", "gemini-2.0-flash"],
        "default_model": "gemini-2.5-flash",
        "supports_agent": True,
        "api_key_in_query": True,
    },
    # ── OpenRouter ──────────────────────────────────────────────────
    "openrouter": {
        "label": "OpenRouter",
        "default_url": "https://openrouter.ai/api",
        "chat_endpoint": "/v1/chat/completions",
        "needs_api_key": True,
        "needs_oauth": False,
        "models": ["anthropic/claude-sonnet-4", "openai/gpt-4o",
                    "deepseek/deepseek-chat", "google/gemini-2.5-flash",
                    "meta-llama/llama-4-maverick", "qwen/qwen-2.5-coder-32b"],
        "default_model": "anthropic/claude-sonnet-4",
        "supports_agent": True,
        "extra_headers": {"HTTP-Referer": "https://github.com/spyder-ide",
                           "X-Title": "Spyder Copilot Chat"},
    },
    # ── Azure OpenAI ────────────────────────────────────────────────
    "azure": {
        "label": "Azure OpenAI",
        "default_url": "",
        "chat_endpoint": "/openai/deployments/{deployment}/chat/completions",
        "needs_api_key": True,
        "needs_oauth": False,
        "models": ["gpt-4o", "gpt-4", "gpt-35-turbo"],
        "default_model": "gpt-4o",
        "supports_agent": True,
        "azure_deployment": True,
    },
    # ── Ollama (本地) ──────────────────────────────────────────────
    "ollama": {
        "label": "Ollama（本地）",
        "default_url": "http://localhost:11434",
        "chat_endpoint": "/api/chat",
        "needs_api_key": False,
        "needs_oauth": False,
        "models": ["llama3.3", "llama3.2", "qwen2.5-coder", "qwen3",
                    "deepseek-coder", "deepseek-r1", "mistral", "codellama",
                    "phi4", "gemma3"],
        "default_model": "qwen2.5-coder",
        "supports_agent": False,
    },
    # ── 自定义 OpenAI 兼容 ─────────────────────────────────────────
    "custom": {
        "label": "自定义（OpenAI 兼容）",
        "default_url": "",
        "chat_endpoint": "/v1/chat/completions",
        "needs_api_key": False,
        "needs_oauth": False,
        "models": ["custom"],
        "default_model": "custom",
        "supports_agent": True,
    },
}

# ===================================================================
# Provider 选择 / 降级说明
# ===================================================================
#
# Azure OpenAI / Google Gemini 的原生协议与 OpenAI 兼容协议不同：
#   - Azure 需要 api-key 头 + ?api-version= 查询参数 + deployment 路径
#   - Gemini 需要 POST /v1beta/models/{model}:generateContent?key=...
# 当前 ChatWorker 只实现了 OpenAI 兼容、Anthropic、Ollama、Copilot 四条链路，
# 因此这两个 Provider 在 UI 中不再提供选择，避免用户以为"已支持"。
# 若历史配置里保存了它们，聊天面板会明确提示「未配置」，而不是发一个必然失败的请求。
UNSUPPORTED_PROVIDERS = {
    "azure": "Azure OpenAI 暂未实现：需要 api-key 头、api-version 参数和 deployment "
             "路径，与 OpenAI 兼容协议不同。请改用「自定义（OpenAI 兼容）」并填写完整 URL。",
    "gemini": "Google Gemini 暂未实现：原生端点不是 OpenAI 兼容协议，当前版本无法直接调用。"
              "请改用支持 OpenAI 兼容协议的代理服务。",
}

# UI 下拉框中的 Provider 顺序（不含未实现的 Azure / Gemini）
SELECTABLE_PROVIDER_ORDER = [
    "copilot", "hunyuan", "deepseek", "openai", "anthropic", "xai",
    "openrouter", "ollama", "custom",
]

# ===================================================================
# Copilot 动态模型列表（GET /models）
# ===================================================================

# 仅在 /models 拉取失败（未登录/网络错误）时使用的回退模型，保证下拉框可用。
FALLBACK_COPILOT_MODELS = [
    {"id": "copilot-base", "name": "Copilot Base（默认）", "endpoints": []},
    {"id": "gpt-4o", "name": "GPT-4o", "endpoints": []},
    {"id": "gpt-4.1", "name": "GPT-4.1", "endpoints": []},
    {"id": "claude-sonnet-4", "name": "Claude Sonnet 4", "endpoints": []},
    {"id": "o3-mini", "name": "o3-mini", "endpoints": []},
]

_MODELS_CACHE = {
    "models": [],       # list[dict]: {id, name, endpoints, is_default}
    "default": "",      # 动态计算出的默认模型 id
    "fetched_at": 0.0,  # time.time()
    "error": "",        # 最近一次失败原因（中文）
}

# 持有正在运行的 QThread，防止父对象先析构导致
# "QThread: Destroyed while thread is still running"
_RUNNING_MODELS_WORKERS = set()
_RUNNING_LOGIN_WORKERS = set()


def _friendly_error(exc, action="操作"):
    """把底层异常转成可读中文提示（用于登录 / 模型列表等 UI 反馈）"""
    text = str(exc) or exc.__class__.__name__
    low = text.lower()
    if isinstance(exc, TimeoutError) or "timed out" in low or "timeout" in low:
        return f"{action}超时：网络较慢或 GitHub 无响应，请检查网络/代理后重试。"
    if "403" in text or "subscription" in low or "access denied" in low:
        return ("登录成功但无法获取 Copilot 令牌：该 GitHub 账号没有 Copilot 订阅，"
                "或组织未授权。请确认已开通 GitHub Copilot 后重试。")
    if "401" in text or "invalid or expired" in low:
        return f"{action}失败：GitHub 凭证无效或已过期，请重新登录。"
    if "device code expired" in low or "expired_token" in low:
        return "设备码已过期，请重新点击「登录 GitHub」。"
    if "denied" in low or "access_denied" in low:
        return "你已拒绝 GitHub 授权。"
    if "network" in low or "url" in low or "connection" in low:
        return f"{action}失败：网络错误，无法连接 GitHub。请检查网络或代理设置。"
    return f"{action}失败：{text}"


def get_cached_copilot_models(include_fallback=True):
    """返回缓存的 Copilot 模型列表（缺省包含回退模型）"""
    models = list(_MODELS_CACHE["models"])
    if not models and include_fallback:
        models = [dict(m) for m in FALLBACK_COPILOT_MODELS]
    return models


def get_copilot_default_model():
    """返回 Copilot 的默认模型 id

    优先使用 /models 返回的 is_chat_default，其次第一个支持
    /chat/completions 的模型；都拿不到时回退到 copilot-base。
    """
    if _MODELS_CACHE["default"]:
        return _MODELS_CACHE["default"]
    if _MODELS_CACHE["models"]:
        return _MODELS_CACHE["models"][0]["id"]
    return "copilot-base"


def set_cached_copilot_models(models):
    """保存 /models 结果并计算默认模型（models: list[dict]）"""
    cleaned = []
    for m in models or []:
        mid = (m or {}).get("id")
        if not mid:
            continue
        endpoints = m.get("endpoints") or []
        cleaned.append({
            "id": mid,
            "name": m.get("name") or mid,
            "endpoints": endpoints,
            "is_default": bool(m.get("is_default")),
        })
    _MODELS_CACHE["models"] = cleaned
    _MODELS_CACHE["fetched_at"] = time.time()
    _MODELS_CACHE["error"] = ""
    default = ""
    for m in cleaned:
        if m.get("is_default"):
            default = m["id"]
            break
    if not default:
        for m in cleaned:
            if not m["endpoints"] or "/chat/completions" in m["endpoints"]:
                default = m["id"]
                break
    if not default and cleaned:
        default = cleaned[0]["id"]
    _MODELS_CACHE["default"] = default


def _parse_models_response(data):
    """把 GET /models 的响应解析成统一结构"""
    raw = data.get("data", []) if isinstance(data, dict) else (data or [])
    models = []
    for m in raw:
        if not isinstance(m, dict):
            continue
        mid = m.get("id")
        if not mid:
            continue
        # model_picker_enabled 为 False 的是内部/隐藏模型，不展示给用户
        if m.get("model_picker_enabled") is False:
            continue
        models.append({
            "id": mid,
            "name": m.get("name") or mid,
            "endpoints": m.get("supported_endpoints") or [],
            "is_default": bool(m.get("is_chat_default")),
        })
    return models


class ModelsWorker(QThread):
    """后台拉取 Copilot 模型列表（GET /models），避免阻塞 GUI"""

    loaded = Signal(list)   # list[dict]
    failed = Signal(str)    # 中文错误信息

    def run(self):
        try:
            # token 刷新可能同步请求网络，必须在子线程里做
            from ..auth import get_valid_copilot_token

            token = get_valid_copilot_token()
            if not token:
                self.failed.emit("未登录 GitHub Copilot，无法获取模型列表。")
                return

            from ..copilot_client import list_models

            data = list_models(token)
            models = _parse_models_response(data)
            if not models:
                self.failed.emit("模型列表为空：该账号可能没有可用的 Copilot 模型。")
                return
            self.loaded.emit(models)
        except Exception as e:  # noqa: BLE001 - UI 线程不能抛异常
            logger.warning("Fetch Copilot models failed: %s", e)
            self.failed.emit(_friendly_error(e, "获取 Copilot 模型列表"))


def fetch_copilot_models_async(on_loaded, on_failed):
    """启动后台模型拉取；返回 QThread 以便调用方保持引用

    线程对象由模块级集合持有，回调在 GUI 线程执行。
    """
    worker = ModelsWorker()
    _RUNNING_MODELS_WORKERS.add(worker)
    worker.loaded.connect(on_loaded)
    worker.failed.connect(on_failed)

    def _cleanup():
        _RUNNING_MODELS_WORKERS.discard(worker)
        worker.deleteLater()

    worker.finished.connect(_cleanup)
    worker.start()
    return worker


# ===================================================================
# 设备码登录（非阻塞）
# ===================================================================


def _poll_device_token_once(device_code):
    """向 GitHub 发送一次设备码轮询请求，返回解析后的 JSON dict

    这里单独实现而不是直接调用 auth.poll_for_token()，是因为后者的
    while+sleep 循环无法被取消；由 UI 侧逐次轮询才能做到「取消即停」
    和「我已授权→立即检查」。
    """
    import urllib.error
    import urllib.parse
    import urllib.request

    from .. import auth

    body = urllib.parse.urlencode({
        "client_id": getattr(auth, "CLIENT_ID", ""),
        "device_code": device_code,
        "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
    }).encode("utf-8")
    url = getattr(auth, "TOKEN_URL",
                  "https://github.com/login/oauth/access_token")
    req = urllib.request.Request(url, data=body, headers={
        "Accept": "application/json",
        "Content-Type": "application/x-www-form-urlencoded",
        "User-Agent": "SpyderCopilotChat/0.5.6",
    })
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"HTTP {e.code}: "
                           f"{e.read().decode('utf-8', 'replace')[:300]}")
    except urllib.error.URLError as e:
        raise RuntimeError(f"Network error: {e.reason}")


class DeviceLoginWorker(QThread):
    """完整的设备码登录流程（启动设备码 → 轮询 → 换 Copilot token）

    全部 HTTP 都在子线程完成，GUI 不冻结；cancel() 后最多再等一个
    轮询间隔就会退出。
    """

    code_ready = Signal(str, str)   # user_code, verification_uri
    status = Signal(str)            # 进度提示（中文）
    success = Signal(str)           # 成功提示
    failed = Signal(str)            # 中文错误

    POLL_TIMEOUT = 300  # GitHub 设备码默认 5 分钟有效

    def __init__(self, parent=None):
        super().__init__(parent)
        self._cancelled = False
        self._wake = threading.Event()   # 用于「我已授权，立即检查」和取消唤醒

    # ── 外部控制 ──
    def cancel(self):
        self._cancelled = True
        self._wake.set()

    def request_check(self):
        """让轮询立刻再检查一次授权状态"""
        self._wake.set()

    # ── 主流程 ──
    def run(self):
        from .. import auth

        try:
            flow = auth.start_device_flow()
        except Exception as e:  # noqa: BLE001
            if not self._cancelled:
                self.failed.emit(_friendly_error(e, "获取设备码"))
            return

        if self._cancelled:
            return
        self.code_ready.emit(flow.user_code, flow.verification_uri)

        interval = max(1, int(getattr(flow, "interval", 5) or 5))
        deadline = time.time() + self.POLL_TIMEOUT
        self.status.emit("等待你在浏览器中完成授权…")

        while not self._cancelled and time.time() < deadline:
            try:
                result = _poll_device_token_once(flow.device_code)
            except Exception as e:  # noqa: BLE001 - 网络错误可重试
                if self._cancelled:
                    return
                logger.warning("Device poll error: %s", e)
                self.status.emit(_friendly_error(e, "查询授权状态"))
                self._wake.wait(interval)
                self._wake.clear()
                continue

            access_token = result.get("access_token")
            if access_token:
                self._finish_with_github_token(access_token)
                return

            error = result.get("error", "")
            if error == "authorization_pending":
                self.status.emit("等待你在浏览器中完成授权…")
            elif error == "slow_down":
                interval += 5
                self.status.emit("GitHub 要求降低轮询频率，等待中…")
            elif error == "expired_token":
                self.failed.emit("设备码已过期，请重新点击「登录 GitHub」。")
                return
            elif error == "access_denied":
                self.failed.emit("你已拒绝 GitHub 授权。")
                return
            elif error:
                self.failed.emit(f"授权失败：{error}")
                return

            # 可被 cancel()/request_check() 立即唤醒
            if self._wake.wait(interval):
                self._wake.clear()

        if not self._cancelled:
            self.failed.emit("等待授权超时（5 分钟），请重新点击「登录 GitHub」。")

    def _finish_with_github_token(self, github_token):
        """用 GitHub access_token 换取 Copilot token（同一子线程内）"""
        if self._cancelled:
            return
        from .. import auth

        self.status.emit("GitHub 授权成功，正在获取 Copilot 令牌…")
        try:
            copilot_token, _expires_at = auth.get_copilot_token(github_token)
        except Exception as e:  # noqa: BLE001
            if not self._cancelled:
                logger.warning("Exchange Copilot token failed: %s", e)
                self.failed.emit(_friendly_error(e, "换取 Copilot 令牌"))
            return
        if self._cancelled:
            return
        if not copilot_token:
            self.failed.emit("未能获取 Copilot 令牌，请确认账号已开通 GitHub Copilot。")
            return
        self.success.emit("GitHub Copilot 登录成功，令牌已保存。")


class DeviceLoginDialog(QDialog):
    """非模态设备码登录对话框

    - 显示 user_code / verification_uri
    - 「复制代码」「打开浏览器」「我已授权」「取消」
    - 轮询在 QThread 中执行，UI 不冻结，可随时取消
    """

    login_succeeded = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("登录 GitHub Copilot")
        self.setModal(False)
        self.setMinimumWidth(480)
        self._worker = None
        self._user_code = ""
        self._verification_uri = "https://github.com/login/device"
        self._build_ui()

    # ── UI ──────────────────────────────────────────────────────────
    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setSpacing(8)

        intro = QLabel(
            "1. 点击「打开浏览器」访问 GitHub 并输入下面的设备代码\n"
            "2. 在浏览器中点击授权\n"
            "3. 本窗口会自动检测授权结果，成功后关闭即可"
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)

        self._code_label = QLabel("—")
        self._code_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self._code_label.setAlignment(Qt.AlignCenter)
        self._code_label.setStyleSheet(
            "QLabel{font-family:Consolas,'Courier New',monospace;"
            "font-size:24px;font-weight:bold;letter-spacing:3px;"
            "color:#e0e0e0;background:#1e1e1e;border:1px solid #3c3c3c;"
            "border-radius:4px;padding:10px;}")
        layout.addWidget(self._code_label)

        self._uri_label = QLabel(self._verification_uri)
        self._uri_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self._uri_label.setAlignment(Qt.AlignCenter)
        self._uri_label.setStyleSheet("color:#3794ff;")
        layout.addWidget(self._uri_label)

        btn_row = QHBoxLayout()
        self._copy_btn = QPushButton("复制代码")
        self._copy_btn.clicked.connect(self._copy_code)
        self._open_btn = QPushButton("打开浏览器")
        self._open_btn.clicked.connect(self._open_browser)
        self._done_btn = QPushButton("我已授权")
        self._done_btn.clicked.connect(self._on_already_authorized)
        for b in (self._copy_btn, self._open_btn, self._done_btn):
            b.setEnabled(False)
            btn_row.addWidget(b)
        self._button_row = btn_row
        layout.addLayout(btn_row)

        self._status_label = QLabel("正在向 GitHub 申请设备码…")
        self._status_label.setWordWrap(True)
        self._status_label.setStyleSheet("color:#888;")
        layout.addWidget(self._status_label)

        bottom = QHBoxLayout()
        bottom.addStretch()
        self._cancel_btn = QPushButton("取消")
        self._cancel_btn.clicked.connect(self._on_cancel)
        bottom.addWidget(self._cancel_btn)
        layout.addLayout(bottom)

    # ── 生命周期 ────────────────────────────────────────────────────
    def start(self):
        """开始（重新开始）登录流程"""
        if self._worker is not None and self._worker.isRunning():
            return
        self._user_code = ""
        self._code_label.setText("—")
        self._status_label.setText("正在向 GitHub 申请设备码…")
        self._status_label.setStyleSheet("color:#888;")
        self._copy_btn.setEnabled(False)
        self._open_btn.setEnabled(False)
        self._done_btn.setEnabled(False)
        self._cancel_btn.setEnabled(True)

        worker = DeviceLoginWorker()
        _RUNNING_LOGIN_WORKERS.add(worker)
        worker.code_ready.connect(self._on_code_ready)
        worker.status.connect(self._on_status)
        worker.success.connect(self._on_success)
        worker.failed.connect(self._on_failed)
        worker.finished.connect(lambda: self._release_worker(worker))
        self._worker = worker
        worker.start()

    def _release_worker(self, worker):
        _RUNNING_LOGIN_WORKERS.discard(worker)
        worker.deleteLater()
        if self._worker is worker:
            self._worker = None

    def closeEvent(self, event):  # noqa: N802 - Qt 接口
        self._stop_worker()
        super().closeEvent(event)

    def reject(self):
        self._stop_worker()
        super().reject()

    def _stop_worker(self):
        worker = self._worker
        self._worker = None
        if worker is None:
            return
        try:
            worker.cancel()
        except RuntimeError:
            pass

    # ── 槽 ──────────────────────────────────────────────────────────
    def _on_code_ready(self, user_code, verification_uri):
        self._user_code = user_code or ""
        self._verification_uri = verification_uri or "https://github.com/login/device"
        self._code_label.setText(self._user_code or "—")
        self._uri_label.setText(self._verification_uri)
        self._copy_btn.setEnabled(bool(self._user_code))
        self._open_btn.setEnabled(True)
        self._done_btn.setEnabled(True)
        self._status_label.setText("等待你在浏览器中完成授权…（自动检测中）")

    def _on_status(self, text):
        self._status_label.setText(text)

    def _on_success(self, message):
        self._status_label.setText(f"✅ {message}")
        self._status_label.setStyleSheet("color:#4caf50;")
        self._copy_btn.setEnabled(False)
        self._open_btn.setEnabled(False)
        self._done_btn.setEnabled(False)
        self._cancel_btn.setText("关闭")
        QMessageBox.information(self, "登录成功", message)
        self.login_succeeded.emit()
        self.accept()

    def _on_failed(self, message):
        self._status_label.setText(f"❌ {message}")
        self._status_label.setStyleSheet("color:#e5534b;")
        self._done_btn.setEnabled(False)
        # 失败后允许直接重试：按钮文案改为「重新登录」
        self._cancel_btn.setText("关闭")
        retry = getattr(self, "_retry_btn", None)
        if retry is None:
            self._retry_btn = QPushButton("重新登录")
            self._retry_btn.clicked.connect(self._retry)
            self._button_row.addWidget(self._retry_btn)
        self._retry_btn.setVisible(True)

    def _retry(self):
        self._retry_btn.setVisible(False)
        self._cancel_btn.setText("取消")
        self.start()

    def _copy_code(self):
        if not self._user_code:
            return
        QApplication.clipboard().setText(self._user_code)
        self._status_label.setText("已复制设备代码，请粘贴到 GitHub 页面。")

    def _open_browser(self):
        QDesktopServices.openUrl(QUrl(self._verification_uri))

    def _on_already_authorized(self):
        self._status_label.setText("正在验证授权…")
        if self._worker is not None and self._worker.isRunning():
            self._worker.request_check()

    def _on_cancel(self):
        self._stop_worker()
        self.close()


# ===================================================================
# 配置管理
# ===================================================================

CONFIG_DIR = os.path.join(os.path.expanduser("~"), ".spyder_copilot_chat")
CONFIG_FILE = os.path.join(CONFIG_DIR, "config.json")
SYSTEM_PROMPT_FILE = os.path.join(CONFIG_DIR, "system_prompt.md")


def _ensure_dir():
    os.makedirs(CONFIG_DIR, exist_ok=True)


#: v0.8.16：TokenHub 的 chat 接口必须以 /v1 结尾。旧版默认长域名、
#: 以及从控制台套餐页复制来的 /plan/v3，都会把请求发到错误路径，
#: 网关回 401002「API Key 不存在」，又被旧分类误报成「模型不可用」。
_OFFICIAL_TOKENHUB_HOSTS = frozenset({
    "tokenhub.tencentmaas.com",
    "tokenhub.tencentcloudmaas.com",
    "tokenhub-intl.tencentcloudmaas.com",
    "tokenhub-us.tencentcloudmaas.com",
})


def normalize_hunyuan_url(cfg):
    """把写坏的官方 TokenHub 地址纠正成 ``scheme://host/v1``。

    只改内存副本、不写文件（用户点保存时才落盘）；host 不在官方列表里的
    自定义代理 / 网关地址一律原样保留，避免误伤。
    """
    if not isinstance(cfg, dict) or cfg.get("provider") != "hunyuan":
        return cfg
    url = str(cfg.get("api_url") or "")
    if not url or "://" not in url:
        return cfg
    try:
        from urllib.parse import urlsplit
        parts = urlsplit(url)
    except Exception:
        return cfg
    if parts.hostname not in _OFFICIAL_TOKENHUB_HOSTS:
        return cfg
    if parts.path.rstrip("/") == "/v1" and not parts.query:
        return cfg
    fixed = "%s://%s/v1" % (parts.scheme, parts.netloc)
    cfg = dict(cfg)
    cfg["api_url"] = fixed
    logger.warning("TokenHub API 地址已纠正：%s → %s", url, fixed)
    return cfg


def load_config():
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            cfg = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    return normalize_hunyuan_url(cfg)


def save_config(config):
    _ensure_dir()
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2, ensure_ascii=False)


def get_provider_config():
    """获取当前完整配置"""
    cfg = load_config()
    pname = cfg.get("provider", "deepseek")
    provider = PROVIDERS.get(pname, PROVIDERS["deepseek"])

    endpoint = provider.get("chat_endpoint", "/v1/chat/completions")
    # Azure OpenAI: 替换 {deployment} 占位符（该 Provider 目前不可选，仅兼容旧配置）
    if provider.get("azure_deployment"):
        dep = cfg.get("deployment_name", "")
        if dep:
            endpoint = endpoint.replace("{deployment}", dep)

    # 模型默认值：
    #   - Copilot 使用 /models 动态结果（缓存为空时回退 copilot-base），
    #     不再硬编码 gpt-4o；
    #   - 其它 Provider 使用各自注册表里的 default_model。
    if pname == "copilot":
        model = cfg.get("model") or get_copilot_default_model()
    else:
        model = cfg.get("model") or provider.get("default_model", "")

    api_url = cfg.get("api_url") or provider.get("default_url", "")
    # Azure 旧配置：api_url 为空但填了资源名时自动拼出 URL
    if pname == "azure" and not api_url and cfg.get("azure_resource"):
        api_url = f"https://{cfg['azure_resource']}.openai.azure.com"

    return {
        "provider": pname,
        "label": provider["label"],
        "api_url": (api_url or "").rstrip("/"),
        "chat_endpoint": endpoint,
        "api_key": cfg.get("api_key", ""),
        "model": model,
        # DeepSeek 官方文档：temperature 默认 1.0，代码生成/数学解题推荐 0.0；
        # 非思考模式默认输出 8K，官方上限 384K（393216）。
        "temperature": cfg.get("temperature", 0.0),
        "max_tokens": cfg.get("max_tokens", 8192),
        "top_p": cfg.get("top_p", 1.0),
        # 思考模式默认关闭：关闭时 temperature 生效，工具调用无需回传
        # reasoning_content，FIM 补全也可用。开启时按官方 high 强度。
        "thinking_mode": cfg.get("thinking_mode", "disabled"),
        "reasoning_effort": cfg.get("reasoning_effort", "high"),
        "agent_mode": cfg.get("agent_mode", True),
        "auto_execute": cfg.get("auto_execute", False),
        "supports_agent": provider["supports_agent"],
        # v0.8.14：把「这一家该怎么发请求」随配置带给 ChatWorker，
        # worker 里不再写第二份 provider 分支；测试也能直接构造 cfg。
        "supports_thinking": provider.get("supports_thinking", False),
        "reasoning_efforts": list(provider.get("reasoning_efforts") or ()),
        "stream_usage": provider.get("stream_usage", False),
        "max_output_tokens": provider.get("max_output_tokens", 0),
        "provider_docs": provider.get("docs", ""),
        "needs_oauth": provider.get("needs_oauth", False),
        "needs_api_key": provider.get("needs_api_key", False),
        "unsupported_reason": UNSUPPORTED_PROVIDERS.get(pname, ""),
        "deployment_name": cfg.get("deployment_name", ""),
        "azure_resource": cfg.get("azure_resource", ""),
        "font_size": cfg.get("font_size", 14),
        "ui_font_size": cfg.get("ui_font_size", 13),
        # v0.8.8：外观四项原先没进这份 dict，面板里 get() 永远拿到默认值——
        # 于是在「外观」页选了浅色 / 紧凑，保存后一点反应都没有。
        "ui_theme": cfg.get("ui_theme", "auto"),
        "ui_accent": cfg.get("ui_accent", theme.DEFAULT_ACCENT),
        "ui_density": cfg.get("ui_density", "comfortable"),
        "tool_density": cfg.get("tool_density", "balanced"),
        "font_family": cfg.get("font_family", ""),
        # Cline Edition (v0.5.6)
        "default_mode": cfg.get("default_mode", "act"),
        "approval_policy": cfg.get("approval_policy", "writes"),
        "checkpoints_enabled": cfg.get("checkpoints_enabled", True),
        "inline_diff_enabled": cfg.get("inline_diff_enabled", True),
        # v0.8.1：写作完成后在聊天流里也铺一遍红绿行
        "chat_diff_mode": cfg.get("chat_diff_mode", "smart"),
        # v0.8.0：预算式自主循环
        "max_tool_rounds": cfg.get("max_tool_rounds", 200),
        "soft_tool_rounds": cfg.get("soft_tool_rounds", 40),
        "auto_continue_on_limit": cfg.get("auto_continue_on_limit", True),
        "max_auto_continues": cfg.get("max_auto_continues", 5),
        # v0.5.6 P0：性能 / 验证 / 任务状态 / 用量
        "stream_throttle_ms": cfg.get("stream_throttle_ms", 30),
        "verify_on_write": cfg.get("verify_on_write", True),
        "verify_command": cfg.get("verify_command", ""),
        "verify_timeout": cfg.get("verify_timeout", 30),
        "verify_max_attempts": cfg.get("verify_max_attempts", 2),
        # 上下文窗口 / 单价跟着 Provider 走：Hy4 是 1M 上下文、
        # 6 元 / 18 元（每百万 tokens）。用户显式改过的值优先。
        "context_window": cfg.get(
            "context_window", provider.get("context_window", 128000)),
        "price_input_per_million": cfg.get(
            "price_input_per_million",
            provider.get("price_input_per_million", 0.0)),
        "price_output_per_million": cfg.get(
            "price_output_per_million",
            provider.get("price_output_per_million", 0.0)),
        "task_persistence": cfg.get("task_persistence", True),
        # v0.5.6 P1：规则 / Skills / 安全 / 审计 / 索引 / 分模型
        "rules_enabled": cfg.get("rules_enabled", True),
        "rules_budget": cfg.get("rules_budget", 12000),
        "skills_enabled": cfg.get("skills_enabled", True),
        "audit_enabled": cfg.get("audit_enabled", True),
        "sandbox_enabled": cfg.get("sandbox_enabled", True),
        "allow_dangerous_commands": cfg.get("allow_dangerous_commands", False),
        "extra_allowed_roots": cfg.get("extra_allowed_roots", ""),
        "repo_index_enabled": cfg.get("repo_index_enabled", True),
        "repo_index_budget": cfg.get("repo_index_budget", 6000),
        "mode_models_enabled": cfg.get("mode_models_enabled", False),
        "model_plan": cfg.get("model_plan", ""),
        "model_act": cfg.get("model_act", ""),
    }


def load_system_prompt():
    try:
        with open(SYSTEM_PROMPT_FILE, "r", encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        return "你是一个 AI 编程助手，帮助用户编写和调试代码。"


def save_system_prompt(text):
    _ensure_dir()
    with open(SYSTEM_PROMPT_FILE, "w", encoding="utf-8") as f:
        f.write(text)


# ===================================================================
# 设置对话框
# ===================================================================

class SettingsDialog(QDialog):
    """完整设置对话框"""

    config_changed = Signal()
    auth_changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Copilot Chat 设置")
        self.setMinimumWidth(600)
        self.setMinimumHeight(550)
        self._config = load_config()
        self._login_dialog = None
        self._models_worker = None
        self._models_manual_request = False
        self._setup_ui()
        self._load_from_config()
        # 对话框是独立窗口，不继承聊天面板的 QSS
        theme.named(self, "standaloneRoot")
        theme.style_popup(self)

    def show_tab(self, name):
        """供聊天面板的「外观与主题」菜单项直接跳到指定页。"""
        for i in range(self._tabs.count()):
            if self._tabs.tabText(i) == name:
                self._tabs.setCurrentIndex(i)
                return True
        return False

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        self._tabs = QTabWidget()
        layout.addWidget(self._tabs)
        self._build_provider_tab()
        self._build_agent_tab()
        self._build_governance_tab()
        self._build_appearance_tab()
        self._build_prompt_tab()
        self._build_about_tab()

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def closeEvent(self, event):  # noqa: N802 - Qt 接口
        """关闭设置页时一并停掉登录窗口的后台轮询"""
        dlg = self._login_dialog
        if dlg is not None:
            try:
                dlg._stop_worker()
                dlg.close()
            except RuntimeError:
                pass
            self._login_dialog = None
        super().closeEvent(event)

    # ── Provider tab ────────────────────────────────────────────────

    def _build_provider_tab(self):
        tab = QWidget()
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        inner = QWidget()
        form = QFormLayout(inner)
        self._provider_form = form  # 用于控制行显隐

        self._provider_combo = QComboBox()
        for key in SELECTABLE_PROVIDER_ORDER:
            if key in PROVIDERS:
                self._provider_combo.addItem(PROVIDERS[key]["label"], key)
        self._provider_combo.currentIndexChanged.connect(self._on_provider_changed)
        form.addRow("AI 服务商:", self._provider_combo)

        self._api_url_edit = QLineEdit()
        self._api_url_edit.setPlaceholderText("API 地址")
        form.addRow("API URL:", self._api_url_edit)

        self._api_key_edit = QLineEdit()
        self._api_key_edit.setPlaceholderText("API Key")
        self._api_key_edit.setEchoMode(QLineEdit.Password)
        form.addRow("API Key:", self._api_key_edit)

        # GitHub Copilot 认证
        auth_row = QHBoxLayout()
        self._login_btn = QPushButton("登录 GitHub")
        self._login_btn.clicked.connect(self._on_login)
        self._logout_btn = QPushButton("退出")
        self._logout_btn.clicked.connect(self._on_logout)
        self._auth_status = QLabel("未登录")
        auth_row.addWidget(self._login_btn)
        auth_row.addWidget(self._logout_btn)
        auth_row.addWidget(self._auth_status)
        auth_row.addStretch()
        self._auth_label = QLabel("认证:")
        # 显式拿住标签：非 Copilot 时控件隐藏，标签也得跟着隐藏，
        # 否则设置页会留一行空的「认证:」。
        form.addRow(self._auth_label, auth_row)

        # 模型：Copilot 走 /models 动态列表；其它 Provider 用注册表里的静态列表
        model_row = QHBoxLayout()
        self._model_combo = QComboBox()
        self._model_combo.setEditable(True)
        # 全局 QSS 已清掉内嵌 QLineEdit 的 padding（见 theme.build_qss），
        # 但 combo 自身 sizeHint 只有 22px，扣掉上下边距后内嵌输入框
        # 仅剩 12px，13px 的字下半截仍会被切。给足高度。
        self._model_combo.setMinimumHeight(30)
        self._refresh_models_btn = QPushButton("刷新列表")
        self._refresh_models_btn.setToolTip("重新从 GitHub Copilot 获取模型列表")
        self._refresh_models_btn.clicked.connect(
            lambda: self._refresh_models_async(manual=True))
        self._refresh_models_btn.setVisible(False)
        model_row.addWidget(self._model_combo, 1)
        model_row.addWidget(self._refresh_models_btn)
        form.addRow("模型:", model_row)

        self._model_status = QLabel("")
        self._model_status.setWordWrap(True)
        self._model_status.setStyleSheet("color:#888;font-size:11px;")
        self._model_status.setVisible(False)
        form.addRow("", self._model_status)

        self._temp_spin = QDoubleSpinBox()
        self._temp_spin.setRange(0.0, 2.0)
        self._temp_spin.setSingleStep(0.1)
        form.addRow("温度 (Temperature):", self._temp_spin)

        self._top_p_spin = QDoubleSpinBox()
        self._top_p_spin.setRange(0.0, 1.0)
        self._top_p_spin.setSingleStep(0.05)
        form.addRow("Top-p:", self._top_p_spin)

        self._max_tokens_spin = QSpinBox()
        # 官方上限 384K = 393216 tokens（1M 上下文）
        self._max_tokens_spin.setRange(1, 393216)
        self._max_tokens_spin.setSingleStep(256)
        form.addRow("最大 Token:", self._max_tokens_spin)

        # DeepSeek / 腾讯混元 Hy4 的深度思考默认开启，必须用 thinking
        # 字段显式控制；其余 Provider 没有这个字段，控件会禁用。
        self._thinking_combo = QComboBox()
        self._thinking_combo.addItem("关闭（推荐）", "disabled")
        self._thinking_combo.addItem("开启", "enabled")
        self._thinking_combo.setToolTip(
            "仅 DeepSeek / 腾讯混元 Hy4 有效。关闭时 temperature 生效，工具调用与 "
            "FIM 补全更稳定；开启时 temperature 不生效，输出预算自动提升。"
            "Hy4 也可用「思考强度 → 不思考 (no_think)」单独关掉。")
        form.addRow("思考模式:", self._thinking_combo)

        self._effort_combo = QComboBox()
        self._EFFORT_LABELS = {
            "low": "低 (low)",
            "high": "高 (high，官方默认)",
            "max": "最高 (max)",
            "none": "不思考 (none)",
            "no_think": "不思考 (no_think，Hy4)",
        }
        self._fill_effort_combo("deepseek", "high")
        self._effort_combo.setToolTip("仅思考模式开启时生效；不同 Provider 允许的取值不同")
        form.addRow("思考强度:", self._effort_combo)
        self._thinking_combo.currentIndexChanged.connect(
            self._on_thinking_changed)

        scroll.setWidget(inner)
        tab_layout = QVBoxLayout(tab)
        tab_layout.addWidget(scroll)
        self._tabs.addTab(tab, "Provider")

    # ── Agent tab ───────────────────────────────────────────────────

    def _build_agent_tab(self):
        tab = QWidget()
        layout = QVBoxLayout(tab)

        self._agent_enabled = QCheckBox("启用 Agent 模式（AI 可调用工具）")
        layout.addWidget(self._agent_enabled)

        self._auto_execute = QCheckBox(
            "自动执行工具（跳过普通审批；危险操作仍需逐项确认）")
        layout.addWidget(self._auto_execute)

        # ── Cline Edition：模式 / 审批 / 检查点 ──
        cline = QGroupBox("Cline 风格工作流（v0.5.6）")
        form = QFormLayout(cline)

        self._default_mode = QComboBox()
        self._default_mode.addItem("⚡ Act — 可提出更改，需逐步批准", "act")
        self._default_mode.addItem("📋 Plan — 只读规划，不改文件", "plan")
        form.addRow("默认模式（下次启动生效）：", self._default_mode)

        self._approval_policy = QComboBox()
        self._approval_policy.addItem(
            "按风险分级：只读免批 · 写操作审批 · 危险操作逐项确认（推荐）", "risk")
        self._approval_policy.addItem(
            "仅写文件 / 执行命令需批准（v0.5.6 旧行为）", "writes")
        self._approval_policy.addItem("所有工具调用都需批准", "all")
        self._approval_policy.addItem("全部自动执行（危险操作除外）", "none")
        form.addRow("审批策略：", self._approval_policy)

        self._checkpoints_enabled = QCheckBox(
            "启用检查点（每步文件更改可用 ↩ 撤销）")
        self._checkpoints_enabled.setChecked(True)
        form.addRow("检查点：", self._checkpoints_enabled)

        self._inline_diff_enabled = QCheckBox(
            "写盘后在编辑器里高亮红/绿修改，并提供「接受 / 撤销」按钮")
        self._inline_diff_enabled.setChecked(True)
        form.addRow("编辑器内联 diff：", self._inline_diff_enabled)

        self._chat_diff_mode = QComboBox()
        self._chat_diff_mode.addItem(
            "智能：编辑器已高亮就不重复，否则在对话里铺红绿行（推荐）", "smart")
        self._chat_diff_mode.addItem(
            "总是：对话里也画一份红绿 diff 卡片", "always")
        self._chat_diff_mode.addItem("关闭：只在对话里提示改了哪些文件", "off")
        self._chat_diff_mode.setToolTip(
            "Agent 每写一次文件，改动都会解析成「红删绿增」的行模型。\n"
            "编辑器内联（Cursor 式）看当前文件，对话内卡片（Cline 式）\n"
            "留一份可回看的记录——文件没在编辑器里打开时也能看到。")
        form.addRow("对话内 diff：", self._chat_diff_mode)
        layout.addWidget(cline)

        # ── v0.8.0 Autonomy Edition：预算式自主循环 ──
        autonomy = QGroupBox("自主循环预算（v0.8.0）")
        aform = QFormLayout(autonomy)

        self._max_tool_rounds = QSpinBox()
        self._max_tool_rounds.setRange(0, 2000)
        self._max_tool_rounds.setSingleStep(50)
        self._max_tool_rounds.setSpecialValueText("不限制")
        self._max_tool_rounds.setToolTip(
            "单段自动执行最多多少轮工具调用。0 = 完全不限制。\n"
            "默认 200，对齐 Cline 的 maxRequestsPerTask（50）与\n"
            "Claude Code 的宽松步数策略。")
        aform.addRow("单段工具轮次（0 = 不限）：", self._max_tool_rounds)

        self._soft_tool_rounds = QSpinBox()
        self._soft_tool_rounds.setRange(0, 2000)
        self._soft_tool_rounds.setSingleStep(10)
        self._soft_tool_rounds.setSpecialValueText("关闭提醒")
        self._soft_tool_rounds.setToolTip(
            "跑到这个轮数时，只在上下文里提醒模型「尽快收束」，"
            "不会中断任务。")
        aform.addRow("收束提醒阈值：", self._soft_tool_rounds)

        self._auto_continue = QCheckBox(
            "用满预算后自动续跑（无需手动回复「继续」）")
        self._auto_continue.setChecked(True)
        aform.addRow("自动续跑：", self._auto_continue)

        self._max_auto_continues = QSpinBox()
        self._max_auto_continues.setRange(0, 50)
        aform.addRow("最多自动续跑次数：", self._max_auto_continues)
        layout.addWidget(autonomy)

        # ── v0.5.6 P0：性能 / 验证 / 状态 / 用量 ──
        perf = QGroupBox("性能与验证（v0.5.6）")
        pform = QFormLayout(perf)

        self._stream_throttle = QSpinBox()
        self._stream_throttle.setRange(10, 200)
        self._stream_throttle.setSingleStep(5)
        self._stream_throttle.setSuffix(" ms")
        pform.addRow("流式渲染节流间隔：", self._stream_throttle)

        self._verify_on_write = QCheckBox(
            "写文件后自动验证并把失败结果回灌给模型")
        pform.addRow("验证闭环：", self._verify_on_write)

        self._verify_command = QLineEdit()
        self._verify_command.setPlaceholderText(
            "留空 = 自动对改动的 .py 执行 py_compile；"
            "可填 pytest -q 等，{files} 会替换为改动文件")
        pform.addRow("验证命令：", self._verify_command)

        self._verify_max = QSpinBox()
        self._verify_max.setRange(0, 5)
        pform.addRow("单任务最多验证次数：", self._verify_max)

        self._context_window = QSpinBox()
        self._context_window.setRange(1000, 2000000)
        self._context_window.setSingleStep(1000)
        pform.addRow("上下文窗口（token）：", self._context_window)

        self._price_in = QDoubleSpinBox()
        self._price_in.setRange(0.0, 100000.0)
        self._price_in.setDecimals(2)
        self._price_in.setSuffix(" 元/百万 tok")
        pform.addRow("输入单价（0 = 不显示费用）：", self._price_in)

        self._price_out = QDoubleSpinBox()
        self._price_out.setRange(0.0, 100000.0)
        self._price_out.setDecimals(2)
        self._price_out.setSuffix(" 元/百万 tok")
        pform.addRow("输出单价（0 = 不显示费用）：", self._price_out)

        self._task_persistence = QCheckBox(
            "保存任务快照（重启 Spyder 后可点「恢复任务」继续）")
        pform.addRow("任务持久化：", self._task_persistence)
        layout.addWidget(perf)

        note = QLabel(
            "Cline 风格：\n"
            "  • Plan 模式只暴露只读工具，AI 只能分析、讨论方案\n"
            "  • Act 模式下每次写文件前展示 diff（绿增红删），逐项批准\n"
            "  • 拒绝的操作不会落盘，AI 会停下来等你补充指示\n"
            "  • 执行前自动保存检查点，可在审批卡片或工具栏 ↩ 撤销\n\n"
            "注意：需要模型支持 function calling。\n"
            "DeepSeek 官方模型 deepseek-flash / deepseek-v4-pro 均支持；"
            "也可使用 gpt-4o、claude 等模型。"
        )
        note.setWordWrap(True)
        note.setStyleSheet("color: #888; padding: 10px;")
        layout.addWidget(note)
        layout.addStretch()
        self._tabs.addTab(tab, "Agent")

    # ── Governance tab（v0.5.6 P1）──────────────────────────────────

    def _build_governance_tab(self):
        tab = QWidget()
        outer = QVBoxLayout(tab)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        inner = QWidget()
        layout = QVBoxLayout(inner)

        # 1) 上下文治理
        ctx = QGroupBox("上下文治理（v0.5.6）")
        f = QFormLayout(ctx)
        self._rules_enabled = QCheckBox(
            "自动加载规则文件（AGENTS.md / CLAUDE.md / .clinerules / "
            ".spydercopilot/rules/*.md / 用户级规则）")
        f.addRow("项目规则：", self._rules_enabled)
        self._rules_budget = QSpinBox()
        self._rules_budget.setRange(1000, 60000)
        self._rules_budget.setSingleStep(1000)
        self._rules_budget.setSuffix(" 字符")
        f.addRow("规则注入预算：", self._rules_budget)
        self._skills_enabled = QCheckBox(
            "启用渐进式 Skills（目录常驻上下文，命中后用 load_skill 加载正文）")
        f.addRow("Skills：", self._skills_enabled)
        self._repo_index_enabled = QCheckBox(
            "生成并注入仓库结构索引（目录树 + 顶层符号，后台构建）")
        f.addRow("仓库地图：", self._repo_index_enabled)
        self._repo_index_budget = QSpinBox()
        self._repo_index_budget.setRange(500, 40000)
        self._repo_index_budget.setSingleStep(500)
        self._repo_index_budget.setSuffix(" 字符")
        f.addRow("地图预算：", self._repo_index_budget)
        layout.addWidget(ctx)

        # 2) 安全与审计
        sec = QGroupBox("风险分级权限 · 沙箱 · 审计（v0.5.6）")
        s = QFormLayout(sec)
        self._sandbox_enabled = QCheckBox(
            "工作区沙箱：文件路径不得越出项目根 / 额外允许目录")
        s.addRow("路径边界：", self._sandbox_enabled)
        self._extra_allowed_roots = QLineEdit()
        self._extra_allowed_roots.setPlaceholderText(
            "例如 D:\\Work\\shared; D:\\Data（多个用英文分号分隔）")
        s.addRow("额外允许目录：", self._extra_allowed_roots)
        self._allow_dangerous_commands = QCheckBox(
            "允许危险命令（rm -rf / del /S / git push --force / 管道执行远端脚本）")
        s.addRow("危险命令：", self._allow_dangerous_commands)
        self._audit_enabled = QCheckBox(
            "记录结构化审计日志（工具轨迹 / 审批 / 验证 / 用量 / 拦截）")
        s.addRow("审计日志：", self._audit_enabled)
        self._audit_view_btn = QPushButton("打开审计日志查看器…")
        self._audit_view_btn.clicked.connect(self._open_audit_from_settings)
        s.addRow("查看：", self._audit_view_btn)
        layout.addWidget(sec)

        # 3) Plan / Act 分模型
        models = QGroupBox("Plan / Act 分模型（v0.5.6）")
        m = QFormLayout(models)
        self._mode_models_enabled = QCheckBox(
            "按运行模式选用不同模型（Plan 用强推理、Act 用快模型）")
        m.addRow("启用：", self._mode_models_enabled)
        self._model_plan = QLineEdit()
        self._model_plan.setPlaceholderText(
            "留空 = 用 Provider 里的全局模型，例如 deepseek-v4-pro")
        m.addRow("Plan 模型：", self._model_plan)
        self._model_act = QLineEdit()
        self._model_act.setPlaceholderText(
            "留空 = 用 Provider 里的全局模型，例如 deepseek-flash")
        m.addRow("Act 模型：", self._model_act)
        layout.addWidget(models)

        note = QLabel(
            "风险分级：只读工具免批；写文件 / 本地命令走审批卡片的 diff 预览；"
            "删除、委派执行、MCP、危险命令标记为 ☠ 危险，必须逐项点「✓ 批准」，"
            "「全部批准安全项」不会覆盖它们。\n\n"
            "即使勾选「自动执行工具」，危险操作仍会强制逐项确认 —— "
            "这是不可逆动作的最小安全边界。\n\n"
            f"审计日志目录：{audit_dir()}（每天一个 .jsonl 文件，自动脱敏 API Key）。\n"
            "Skills 可放在 ~/.spyder_copilot_chat/skills/<name>/SKILL.md 或 "
            "<项目>/.spydercopilot/skills/；规则可放 <项目>/.spydercopilot/rules/*.md，"
            "用 frontmatter 的 globs: 指定适用文件。"
        )
        note.setWordWrap(True)
        note.setStyleSheet("color:#888;padding:10px;")
        layout.addWidget(note)
        layout.addStretch()

        scroll.setWidget(inner)
        outer.addWidget(scroll)
        self._tabs.addTab(tab, "治理")

    def _open_audit_from_settings(self):
        AuditViewerDialog(self).exec_()

    # ── System Prompt tab ───────────────────────────────────────────

    def _build_prompt_tab(self):
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.addWidget(QLabel("自定义系统提示词（影响 AI 的行为风格）："))
        self._system_prompt = QPlainTextEdit()
        self._system_prompt.setMinimumHeight(200)
        self._system_prompt.setPlaceholderText(
            "你是一个 AI 编程助手，帮助用户编写和调试代码..."
        )
        layout.addWidget(self._system_prompt)

        reset_btn = QPushButton("恢复默认")
        reset_btn.clicked.connect(self._reset_prompt)
        layout.addWidget(reset_btn)
        self._tabs.addTab(tab, "系统提示词")

    def _reset_prompt(self):
        self._system_prompt.setPlainText("你是一个 AI 编程助手，帮助用户编写和调试代码。")

    # ── Appearance tab (v0.6.0) ─────────────────────────────────────

    def _build_appearance_tab(self):
        tab = QWidget()
        lay = QVBoxLayout(tab)
        lay.setContentsMargins(16, 16, 16, 16)
        lay.setSpacing(12)

        intro = QLabel(
            "界面采用语义化设计系统：亮 / 暗双主题、单一强调色、矢量线性图标、"
            "紧凑顶栏与底部统一输入区。主题、强调色和字号保存后即时重绘，"
            "无需重启 Spyder。")
        theme.named(intro, "welcomeBody")
        intro.setWordWrap(True)
        lay.addWidget(intro)

        form = QFormLayout()
        form.setSpacing(10)

        self._ui_theme = QComboBox()
        for label, value in theme.THEME_CHOICES:
            self._ui_theme.addItem(label, value)
        form.addRow("主题:", self._ui_theme)

        self._ui_accent = QComboBox()
        for key, values in theme.ACCENTS.items():
            self._ui_accent.addItem(values[0], key)
        form.addRow("强调色:", self._ui_accent)

        self._ui_density = QComboBox()
        for label, value in theme.DENSITY_CHOICES:
            self._ui_density.addItem(label, value)
        form.addRow("界面密度:", self._ui_density)

        self._tool_density = QComboBox()
        for label, value in theme.TOOL_DENSITY_CHOICES:
            self._tool_density.addItem(label, value)
        self._tool_density.setToolTip(
            "对标 Cursor 3.4：控制对话流里工具输出的默认展开程度")
        form.addRow("工具痕迹密度:", self._tool_density)

        self._font_size_spin = QSpinBox()
        # 上限跟着 theme.FONT_STEPS 走：面板里的 Aa 菜单能把字号推到 36，
        # 设置页却卡在 32 的话，从菜单调上去再进设置页会被"看不见地"夹回来。
        self._font_size_spin.setRange(9, max(theme.FONT_STEPS))
        self._font_size_spin.setSuffix(" px")
        self._font_size_spin.setToolTip(
            "对话正文的字号。面板顶栏的 Aa 按钮和 Ctrl+滚轮调的是同一项，"
            "改完立即生效并记住。")
        form.addRow("正文字体大小:", self._font_size_spin)

        self._ui_font_size_spin = QSpinBox()
        self._ui_font_size_spin.setRange(9, 32)
        self._ui_font_size_spin.setSuffix(" px")
        self._ui_font_size_spin.setToolTip(
            "顶栏、按钮、状态行等界面元素的字号。")
        form.addRow("界面字体大小:", self._ui_font_size_spin)

        self._font_family = QComboBox()
        for label, value in theme.FONT_CHOICES:
            self._font_family.addItem(label, value)
        self._font_family.setToolTip(
            "正文与界面使用的字体族。代码块始终用等宽字体，不受这里影响。")
        form.addRow("字体:", self._font_family)
        lay.addLayout(form)

        tip = QLabel(
            "调研参考：Cline 3.x 的 Plan/Act 与逐项审批、Cursor 3.4 的"
            "工具痕迹密度、VS Code 1.110 的底部会话控制与可折叠终端输出、"
            "Windsurf 的单强调色深色系统、Continue 的语义主题 token。")
        theme.named(tip, "mutedLabel")
        tip.setWordWrap(True)
        lay.addWidget(tip)
        lay.addStretch()
        self._tabs.addTab(tab, "外观")

    # ── About tab ───────────────────────────────────────────────────

    def _build_about_tab(self):
        tab = QWidget()
        layout = QVBoxLayout(tab)
        lbl = QLabel(
            "Spyder Copilot Chat v0.8.8 · Readable Edition\n\n"
            "将 GitHub Copilot Chat 移植到 Spyder 6，并加入 Cline 式工作流：\n"
            "Plan / Act 双模式、内联审批卡片 + diff 预览、检查点一键撤销。\n\n"
            "v0.8.8 修掉「字号太小还调不动」：Markdown 的段落 / 引用 / 列表\n"
            "原来没写 font-size，回落到宋体 9pt，所以正文字号怎么调都没反应；\n"
            "现在正文严格等于 font_size，顶栏 Aa、Ctrl+= / Ctrl+- / Ctrl+0、\n"
            "Ctrl+滚轮都能调，外观页的主题 / 密度也真正生效了。\n"
            "v0.7.8 让字号与图标跟上设置：任务面板改用 ui_font 并把状态换成矢量\n"
            "图标，Plan/Act 与 Agent 选择器改成「图标 + 文案 + 箭头」的胶囊，\n"
            "发送按钮同高同圆角，气泡里的彩色 emoji 换成单色记号。\n"
            "v0.7.7 把 diff 画成了真正的行视图：删除行整行标红、新增行整行标绿，\n"
            "左侧双行号（旧/新）+ +/- 标记，相邻的 -/+ 块再做词级高亮，\n"
            "只把真正改动的字符加深一档。支持 unified diff、git 的 ---/+++ 文件头\n"
            "和 Codex 的 V4A 补丁；没有 @@ 行号时不显示行号，不编造数字。\n"
            "v0.7.6 支持 LaTeX 公式：块级 $$...$$ 居中成图、行内 $...$ 跟随文字，\n"
            "用 matplotlib 的 mathtext 在本机排版（2 倍采样、透明底、跟随主题色），\n"
            "结果按公式缓存，流式渲染不重复排版；排不出来的公式退回等宽源码。\n"
            "v0.7.5 把 Agent 选择器整体中文化：默认助手 / 探索助手 / 调研助手 / 代码审查 /\n"
            "任务执行 / 方案规划 / 橡皮鸭，胶囊与菜单都不再出现英文名，\n"
            "各 Agent 的系统提示词也改成中文自称。\n"
            "v0.7.4 修掉工具卡片之后的 Markdown 失效：卡片插进气泡时末尾少一个空行，\n"
            "下一段正文粘到收尾的 ``` 上，围栏合不上，后面的标题/列表/表格全成了\n"
            "代码块正文。现在整段内容统一走 append_block()，围栏解析也做了容错。\n"
            "v0.7.3 修掉 Agent 选择器的显示问题：改成「胶囊 + 菜单」，七个全名一次排开\n"
            "（不再被中部省略成 Def…ent / Co…nt），胶囊上只用短名，也不再和\n"
            "权限徽章叠在一起。\n"
            "v0.7.2 把 AI 搬进编辑区：Tab 灰字补全（灰字不进文档、不进撤销栈），\n"
            "Ctrl+K 就地改写，先看 diff 再决定是否落盘。\n"
            "v0.7.1 接入编辑器上下文（对标 Cline）：读取当前光标/选区，\n"
            "按选区、整个函数或整份文件附加到提问；发送时注入 editor_context 块，\n"
            "气泡里只显示你输入的文字。\n"
            "v0.7.0 重写渲染管线（对标 DeepSeek Harness 聊天面板）：\n"
            "块级 Markdown 解析、GFM 表格/任务列表/删除线/裸链、代码块卡片\n"
            "（语言 · 行数 + 逐块复制）、流式增量渲染与块级高亮缓存。\n"
            "v0.6.2 修掉暗色 IDE 下变白板、顶栏黑条、气泡吃半屏、\n"
            "代码块色带与横向溢出，面板最小宽度降到 358px。\n"
            "v0.6.1 把配色改为与 IDE 同源：底色、正文与高亮直接读 Spyder 的\n"
            "调色板（qdarkstyle），中性色阶由底色派生，强调色用 Spyder 蓝；\n"
            "v0.6.0 按 2026 年主流代码 Agent 重做界面：语义化亮/暗双主题、\n"
            "矢量线性图标、紧凑顶栏、底部统一 composer、可折叠工具痕迹。\n\n"
            "支持的 Provider：\n"
            "  • GitHub Copilot（设备码登录 + /models 动态模型）\n"
            "  • 腾讯混元 Hy4（TokenHub：hy4 / hy4-preview，1M 上下文、看图、工具调用）\n"
            "  • DeepSeek（deepseek-flash / deepseek-v4-pro，按官方文档配置）\n"
            "  • OpenAI • Anthropic Claude • xAI Grok\n"
            "  • OpenRouter • Ollama（本地）• 自定义 OpenAI 兼容\n\n"
            "暂未支持：Azure OpenAI、Google Gemini（原生协议非 OpenAI 兼容）。\n\n"
            "基于 vscode-copilot-chat (Microsoft) 的设计移植。\n"
            "GitHub Copilot 是 GitHub, Inc. 的商标。"
        )
        lbl.setWordWrap(True)
        layout.addWidget(lbl)
        layout.addStretch()
        self._tabs.addTab(tab, "关于")

    # ── Provider change handler ─────────────────────────────────────

    def _on_provider_changed(self, idx):
        key = self._provider_combo.currentData()
        p = PROVIDERS.get(key, PROVIDERS["deepseek"])

        self._api_url_edit.setText(p.get("default_url", ""))
        self._api_url_edit.setEnabled(key != "copilot")
        # TokenHub 的 Key 按地域签发：切地域必须同时换 Key，写进 tooltip
        # 比写在 README 里更容易在出错时看到。
        alt_urls = p.get("alt_urls") or ()
        if alt_urls:
            hint = "API Key 按地域签发，不能跨地域调用；换地址记得换 Key。"
            if key == "hunyuan":
                hint = (
                    "两套广州入口各自签发 Key，不通用。默认短别名地址："
                    "tokenhub.tencentmaas.com。\n"
                    "若报 401002「API Key 不存在」，换下面另一套长域名试试；"
                    "新加坡 / 硅谷是另一组地域，Key 还要配对应 -intl / -us 地址。")
            self._api_url_edit.setToolTip(
                hint + "\n可选接入地址：\n  " + "\n  ".join(alt_urls)
                + "\nKey 创建：" + p.get("key_console", ""))
        else:
            self._api_url_edit.setToolTip("")

        needs_key = p.get("needs_api_key", False) and key not in ("copilot", "ollama")
        self._api_key_edit.setEnabled(needs_key)
        self._api_key_edit.setPlaceholderText(
            "无需 API Key" if not needs_key else "输入 API Key"
        )

        is_cp = key == "copilot"
        self._refresh_models_btn.setVisible(is_cp)
        self._model_status.setVisible(is_cp)
        # 思考模式只对声明了 supports_thinking 的 Provider 开放
        # （DeepSeek / 腾讯混元 Hy4）。
        if hasattr(self, "_thinking_combo"):
            self._thinking_combo.setEnabled(p.get("supports_thinking", False))
            self._fill_effort_combo(
                key, self._config.get("reasoning_effort", "high"))
            self._on_thinking_changed()

        self._model_combo.clear()
        if is_cp:
            preferred = self._config.get("model", "") or get_copilot_default_model()
            self._fill_model_combo(get_cached_copilot_models(), preferred)
        else:
            self._fill_model_combo(
                [{"id": m} for m in p.get("models", [])],
                p.get("default_model", ""))

        self._login_btn.setVisible(is_cp)
        self._logout_btn.setVisible(is_cp)
        self._auth_status.setVisible(is_cp)
        self._auth_label.setVisible(is_cp)
        if is_cp:
            self._update_auth_status()
            # 缓存为空时后台拉取一次，避免打开设置页就卡住
            if not _MODELS_CACHE["models"]:
                self._refresh_models_async(manual=False)

    def _on_thinking_changed(self, idx=None):
        """思考强度仅在支持 thinking 的 Provider + 思考模式开启时可编辑。"""
        if not hasattr(self, "_effort_combo"):
            return
        key = self._provider_combo.currentData()
        p = PROVIDERS.get(key, {})
        enabled = (p.get("supports_thinking", False)
                   and self._thinking_combo.currentData() == "enabled")
        self._effort_combo.setEnabled(enabled)

    def _fill_effort_combo(self, provider, preferred="high"):
        """按 Provider 允许的取值重建思考强度下拉。

        DeepSeek 是 low / high / max，Hy4 只有 high / no_think
        （preview 是 none / high）。把别家不认识的档位塞进请求体，
        TokenHub 会直接 400，所以下拉里根本不该出现。
        """
        if not hasattr(self, "_effort_combo"):
            return
        p = PROVIDERS.get(provider, {})
        values = list(p.get("reasoning_efforts") or ("low", "high", "max"))
        self._effort_combo.blockSignals(True)
        self._effort_combo.clear()
        for value in values:
            self._effort_combo.addItem(
                self._EFFORT_LABELS.get(value, value), value)
        idx = self._effort_combo.findData(preferred)
        if idx < 0:
            idx = self._effort_combo.findData("high")
        self._effort_combo.setCurrentIndex(max(idx, 0))
        self._effort_combo.blockSignals(False)

    def _fill_model_combo(self, models, preferred=""):
        """填充模型下拉框（保留用户当前输入）"""
        ids = [m.get("id") for m in models if m.get("id")]
        self._model_combo.blockSignals(True)
        self._model_combo.clear()
        self._model_combo.addItems(ids)
        text = preferred if preferred else (ids[0] if ids else "")
        if text:
            self._model_combo.setCurrentText(text)
        self._model_combo.blockSignals(False)

    # ── 动态模型列表 ────────────────────────────────────────────────

    def _refresh_models_async(self, manual=False):
        """后台刷新 Copilot 模型列表（不阻塞 GUI）"""
        if self._models_worker is not None and self._models_worker.isRunning():
            return
        self._models_manual_request = manual
        self._model_status.setVisible(True)
        self._model_status.setStyleSheet("color:#888;font-size:11px;")
        self._model_status.setText("正在获取 Copilot 模型列表…")
        self._models_worker = fetch_copilot_models_async(
            self._on_models_loaded, self._on_models_failed)

    def _on_models_loaded(self, models):
        self._models_worker = None
        set_cached_copilot_models(models)
        if self._provider_combo.currentData() == "copilot":
            current = self._model_combo.currentText().strip()
            ids = [m["id"] for m in models]
            if current not in ids:
                current = get_copilot_default_model()
            self._fill_model_combo(models, current)
        self._model_status.setVisible(True)
        self._model_status.setStyleSheet("color:#4caf50;font-size:11px;")
        self._model_status.setText(
            f"✅ 已加载 {len(models)} 个 Copilot 模型（来自 GET /models）")
        self.config_changed.emit()

    def _on_models_failed(self, message):
        self._models_worker = None
        _MODELS_CACHE["error"] = message
        if self._provider_combo.currentData() == "copilot":
            # 回退到少量默认模型，保证下拉框仍可用
            current = self._model_combo.currentText().strip() or get_copilot_default_model()
            self._fill_model_combo(get_cached_copilot_models(), current)
            self._model_status.setVisible(True)
            self._model_status.setStyleSheet("color:#d7a600;font-size:11px;")
            self._model_status.setText(f"⚠️ {message}（已回退到默认模型列表）")
        if self._models_manual_request:
            QMessageBox.warning(self, "获取模型列表失败", message)
        self._models_manual_request = False

    def refresh_models_ui(self):
        """外部（登录成功/面板加载完成）调用：刷新模型下拉框"""
        if self._provider_combo.currentData() != "copilot":
            return
        current = self._model_combo.currentText().strip() or get_copilot_default_model()
        self._fill_model_combo(get_cached_copilot_models(), current)

    # ── 登录 / 登出 ─────────────────────────────────────────────────

    def _update_auth_status(self):
        """轻量检查登录状态（只读本地 token 文件，不做网络请求）"""
        try:
            github_token, copilot_token, expires_at = load_tokens()
        except Exception:  # noqa: BLE001 - 读取失败按未登录处理
            github_token, copilot_token, expires_at = None, None, 0
        if not github_token:
            text, style = "❌ 未登录", "color:#e5534b;"
        elif copilot_token and (not expires_at or time.time() < expires_at - 300):
            text, style = "✅ 已登录 GitHub Copilot", "color:#4caf50;"
        elif expires_at and time.time() >= expires_at:
            text, style = "⚠️ Copilot 令牌已过期，发消息时自动刷新", "color:#d7a600;"
        else:
            text, style = "✅ 已授权 GitHub", "color:#4caf50;"
        self._auth_status.setText(text)
        self._auth_status.setStyleSheet(style)

    def _on_login(self):
        """打开非模态设备码登录窗口（不阻塞设置对话框/GUI）"""
        if self._login_dialog is None:
            self._login_dialog = DeviceLoginDialog(self)
            self._login_dialog.login_succeeded.connect(self._on_login_succeeded)
        self._login_dialog.start()
        self._login_dialog.show()
        self._login_dialog.raise_()
        self._login_dialog.activateWindow()

    def _on_login_succeeded(self):
        self._update_auth_status()
        # 登录成功后立即刷新模型列表
        self._refresh_models_async(manual=False)
        self.auth_changed.emit()

    def _on_logout(self):
        logout()
        self._update_auth_status()
        _MODELS_CACHE["models"] = []
        _MODELS_CACHE["default"] = ""
        if self._provider_combo.currentData() == "copilot":
            self._fill_model_combo(get_cached_copilot_models(),
                                   get_copilot_default_model())
            self._model_status.setVisible(True)
            self._model_status.setStyleSheet("color:#888;font-size:11px;")
            self._model_status.setText("已退出登录，当前使用回退模型列表。")
        self.auth_changed.emit()

    # ── Load / Save ─────────────────────────────────────────────────

    def _load_from_config(self):
        pname = self._config.get("provider", "deepseek")
        idx = self._provider_combo.findData(pname)
        if idx >= 0:
            self._provider_combo.setCurrentIndex(idx)
        # 配置里没存 api_url 时（尤其刚切到新 Provider），显示该 Provider
        # 的默认地址。以前这里直接写空串，把 _on_provider_changed 填好的
        # 默认 URL 又抹掉，用户看到的是空白输入框。
        self._api_url_edit.setText(
            self._config.get("api_url", "")
            or PROVIDERS.get(pname, PROVIDERS["deepseek"]).get("default_url", ""))
        self._api_key_edit.setText(self._config.get("api_key", ""))
        # 模型：配置里没有时用动态/静态默认值
        if pname == "copilot":
            model = self._config.get("model", "") or get_copilot_default_model()
        else:
            model = (self._config.get("model", "")
                     or PROVIDERS.get(pname, PROVIDERS["deepseek"])["default_model"])
        self._model_combo.setCurrentText(model)
        self._temp_spin.setValue(self._config.get("temperature", 0.0))
        self._top_p_spin.setValue(self._config.get("top_p", 1.0))
        self._max_tokens_spin.setValue(self._config.get("max_tokens", 8192))
        thinking_idx = self._thinking_combo.findData(
            self._config.get("thinking_mode", "disabled"))
        if thinking_idx >= 0:
            self._thinking_combo.setCurrentIndex(thinking_idx)
        effort_idx = self._effort_combo.findData(
            self._config.get("reasoning_effort", "high"))
        if effort_idx >= 0:
            self._effort_combo.setCurrentIndex(effort_idx)
        self._on_thinking_changed()
        self._font_size_spin.setValue(self._config.get("font_size", 14))
        self._ui_font_size_spin.setValue(self._config.get("ui_font_size", 13))
        # v0.6.0：外观页
        for combo, key, default in (
                (self._ui_theme, "ui_theme", "auto"),
                (self._ui_accent, "ui_accent", theme.DEFAULT_ACCENT),
                (self._ui_density, "ui_density", "comfortable"),
                (self._tool_density, "tool_density", "balanced"),
                (self._font_family, "font_family", "")):
            idx = combo.findData(self._config.get(key, default))
            if idx >= 0:
                combo.setCurrentIndex(idx)
        self._agent_enabled.setChecked(self._config.get("agent_mode", True))
        self._auto_execute.setChecked(self._config.get("auto_execute", False))
        mode_idx = self._default_mode.findData(
            self._config.get("default_mode", "act"))
        if mode_idx >= 0:
            self._default_mode.setCurrentIndex(mode_idx)
        policy_idx = self._approval_policy.findData(
            self._config.get("approval_policy", "writes"))
        if policy_idx >= 0:
            self._approval_policy.setCurrentIndex(policy_idx)
        self._checkpoints_enabled.setChecked(
            self._config.get("checkpoints_enabled", True))
        self._inline_diff_enabled.setChecked(
            bool(self._config.get("inline_diff_enabled", True)))
        diff_idx = self._chat_diff_mode.findData(
            self._config.get("chat_diff_mode", "smart"))
        if diff_idx >= 0:
            self._chat_diff_mode.setCurrentIndex(diff_idx)
        # v0.8.0：自主循环预算
        self._max_tool_rounds.setValue(
            int(self._config.get("max_tool_rounds", 200)))
        self._soft_tool_rounds.setValue(
            int(self._config.get("soft_tool_rounds", 40)))
        self._auto_continue.setChecked(
            bool(self._config.get("auto_continue_on_limit", True)))
        self._max_auto_continues.setValue(
            int(self._config.get("max_auto_continues", 5)))
        self._stream_throttle.setValue(
            int(self._config.get("stream_throttle_ms", 30)))
        self._verify_on_write.setChecked(
            bool(self._config.get("verify_on_write", True)))
        self._verify_command.setText(
            str(self._config.get("verify_command", "")))
        self._verify_max.setValue(
            int(self._config.get("verify_max_attempts", 2)))
        self._context_window.setValue(
            int(self._config.get("context_window", 1048576)))
        self._price_in.setValue(
            float(self._config.get("price_input_per_million", 0.0) or 0))
        self._price_out.setValue(
            float(self._config.get("price_output_per_million", 0.0) or 0))
        self._task_persistence.setChecked(
            bool(self._config.get("task_persistence", True)))
        # v0.5.6 P1：治理页
        self._rules_enabled.setChecked(
            bool(self._config.get("rules_enabled", True)))
        self._rules_budget.setValue(int(self._config.get("rules_budget", 12000)))
        self._skills_enabled.setChecked(
            bool(self._config.get("skills_enabled", True)))
        self._repo_index_enabled.setChecked(
            bool(self._config.get("repo_index_enabled", True)))
        self._repo_index_budget.setValue(
            int(self._config.get("repo_index_budget", 6000)))
        self._sandbox_enabled.setChecked(
            bool(self._config.get("sandbox_enabled", True)))
        self._extra_allowed_roots.setText(
            str(self._config.get("extra_allowed_roots", "")))
        self._allow_dangerous_commands.setChecked(
            bool(self._config.get("allow_dangerous_commands", False)))
        self._audit_enabled.setChecked(
            bool(self._config.get("audit_enabled", True)))
        self._mode_models_enabled.setChecked(
            bool(self._config.get("mode_models_enabled", False)))
        self._model_plan.setText(str(self._config.get("model_plan", "")))
        self._model_act.setText(str(self._config.get("model_act", "")))
        self._system_prompt.setPlainText(
            self._config.get("system_prompt", "") or load_system_prompt())
        self._update_auth_status()

    def _on_accept(self):
        provider = self._provider_combo.currentData()
        self._config["provider"] = provider
        self._config["api_url"] = self._api_url_edit.text().strip()
        key = self._api_key_edit.text().strip()
        if key:
            self._config["api_key"] = key
        model = self._model_combo.currentText().strip()
        if not model:
            model = (get_copilot_default_model() if provider == "copilot"
                     else PROVIDERS.get(provider, {}).get("default_model", ""))
        self._config["model"] = model
        self._config["temperature"] = self._temp_spin.value()
        self._config["top_p"] = self._top_p_spin.value()
        self._config["max_tokens"] = self._max_tokens_spin.value()
        self._config["thinking_mode"] = self._thinking_combo.currentData()
        self._config["reasoning_effort"] = self._effort_combo.currentData()
        self._config["font_size"] = self._font_size_spin.value()
        self._config["ui_font_size"] = self._ui_font_size_spin.value()
        # v0.6.0：外观页
        self._config["ui_theme"] = self._ui_theme.currentData()
        self._config["ui_accent"] = self._ui_accent.currentData()
        self._config["ui_density"] = self._ui_density.currentData()
        self._config["tool_density"] = self._tool_density.currentData()
        self._config["font_family"] = self._font_family.currentData() or ""
        self._config["agent_mode"] = self._agent_enabled.isChecked()
        self._config["auto_execute"] = self._auto_execute.isChecked()
        self._config["default_mode"] = self._default_mode.currentData()
        self._config["approval_policy"] = self._approval_policy.currentData()
        self._config["checkpoints_enabled"] = (
            self._checkpoints_enabled.isChecked())
        self._config["inline_diff_enabled"] = (
            self._inline_diff_enabled.isChecked())
        self._config["chat_diff_mode"] = self._chat_diff_mode.currentData()
        # v0.8.0：自主循环预算
        self._config["max_tool_rounds"] = self._max_tool_rounds.value()
        self._config["soft_tool_rounds"] = self._soft_tool_rounds.value()
        self._config["auto_continue_on_limit"] = (
            self._auto_continue.isChecked())
        self._config["max_auto_continues"] = self._max_auto_continues.value()
        self._config["stream_throttle_ms"] = self._stream_throttle.value()
        self._config["verify_on_write"] = self._verify_on_write.isChecked()
        self._config["verify_command"] = self._verify_command.text().strip()
        self._config["verify_max_attempts"] = self._verify_max.value()
        self._config["context_window"] = self._context_window.value()
        self._config["price_input_per_million"] = self._price_in.value()
        self._config["price_output_per_million"] = self._price_out.value()
        self._config["task_persistence"] = (
            self._task_persistence.isChecked())
        # v0.5.6 P1：治理页
        self._config["rules_enabled"] = self._rules_enabled.isChecked()
        self._config["rules_budget"] = self._rules_budget.value()
        self._config["skills_enabled"] = self._skills_enabled.isChecked()
        self._config["repo_index_enabled"] = self._repo_index_enabled.isChecked()
        self._config["repo_index_budget"] = self._repo_index_budget.value()
        self._config["sandbox_enabled"] = self._sandbox_enabled.isChecked()
        self._config["extra_allowed_roots"] = (
            self._extra_allowed_roots.text().strip())
        self._config["allow_dangerous_commands"] = (
            self._allow_dangerous_commands.isChecked())
        self._config["audit_enabled"] = self._audit_enabled.isChecked()
        self._config["mode_models_enabled"] = (
            self._mode_models_enabled.isChecked())
        self._config["model_plan"] = self._model_plan.text().strip()
        self._config["model_act"] = self._model_act.text().strip()
        sp = self._system_prompt.toPlainText().strip()
        if sp:
            self._config["system_prompt"] = sp
            save_system_prompt(sp)
        save_config(self._config)
        self.config_changed.emit()
        self.accept()
