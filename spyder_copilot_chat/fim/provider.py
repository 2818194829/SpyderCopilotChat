# -*- coding: utf-8 -*-
"""Copilot FIM（Fill-In-the-Middle）补全 —— SpyderCompletionProvider 实现。

本模块修正了审计报告 P0-2 中的全部 4 个问题：

1. **响应发不出去**：显式实现 :meth:`send_response`，emit
   ``sig_response_ready``（基类是空实现）。
2. **文本永远拿不到**：``textDocument/didOpen|didChange|didClose`` 走的是
   ``send_notification``（``requires_response=False``），这里覆写它并缓存
   全文；``textDocument/completion`` 请求里没有 ``text``，改为优先从
   ``req["response_instance"].toPlainText()`` 取实时文本，缓存兜底。
3. **没有任何入口打开 FIM**：通过 ``CONF_TABS`` 暴露一个
   “Copilot FIM” 首选项页（启用开关 / 后端 / engine / 模型 / 参数）。
4. **GUI 线程同步 HTTP**：所有网络请求都在 :class:`QThreadPool` 的后台
   worker 中执行，结果通过 Qt signal 排队回主线程；GUI 线程永不阻塞。

补全协议使用官方真实 FIM 端点（见 ``reports/copilot-protocol-spec.md``
§5 / §9.4）::

    POST {proxy_base()}/v1/engines/{engine}/completions
    body: {prompt, suffix, max_tokens, temperature, top_p, n, stop, stream,
           extra: {language, next_indent, trim_by_indentation}}

响应是 OpenAI *legacy completions* 形态的 SSE，文本取
``choices[i]["text"]``（**不是** ``delta.content``）。

三种后端可配置：``copilot`` / ``ollama`` / ``openai``（``deepseek`` 作为
openai 兼容后端的别名保留，兼容旧配置）。
"""

import json
import logging
import ssl
import time
import urllib.error
import urllib.request
from itertools import count

from qtpy.QtCore import QObject, QRunnable, QThreadPool, QTimer, Signal
from qtpy.QtWidgets import QLabel, QVBoxLayout

from spyder.plugins.completion.api import (
    CompletionItemKind,
    CompletionRequestTypes,
    SpyderCompletionProvider,
)

from ..auth import (
    CopilotAuthError,
    CopilotNetworkError,
    CopilotNotLoggedInError,
    CopilotRateLimitedError,
    CopilotSubscriptionError,
    CopilotTokenInvalidError,
    clear_tokens,
    get_valid_copilot_token,
)
from ..copilot_headers import build_fim_headers, proxy_base

logger = logging.getLogger(__name__)

_COMPLETION = CompletionRequestTypes.DOCUMENT_COMPLETION
_DID_OPEN = CompletionRequestTypes.DOCUMENT_DID_OPEN
_DID_CHANGE = CompletionRequestTypes.DOCUMENT_DID_CHANGE
_DID_CLOSE = CompletionRequestTypes.DOCUMENT_DID_CLOSE

_EMPTY_LIST = {"params": []}

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

#: 官方 FIM 默认引擎（spec §5.1，@17000157 ``Koa="gpt-41-copilot"``）
FIM_ENGINE_DEFAULT = "gpt-41-copilot"

#: 当前活动的 provider 实例（供 plugin / ghost text 查询）
_INSTANCE = None

#: 默认 stop 序列（spec §5.2 的 ``x2r``）
FIM_STOP_DEFAULT = ["\n\n\n", "\n```"]
FIM_STOP_PYTHON = ["\ndef ", "\nclass ", "\nif ", "\n\n#"]

#: 全局配置（聊天面板设置）路径，仅用于 ollama / openai 兼容后端
_CONFIG_DIR = None
_CONFIG_FILE = None


def _config_file():
    """返回聊天面板全局配置路径（延迟计算，便于测试 patch HOME）。"""
    global _CONFIG_DIR, _CONFIG_FILE
    if _CONFIG_FILE is None:
        import os

        _CONFIG_DIR = os.path.join(
            os.path.expanduser("~"), ".spyder_copilot_chat"
        )
        _CONFIG_FILE = os.path.join(_CONFIG_DIR, "config.json")
    return _CONFIG_FILE


def _load_global_config():
    """读取全局 ``~/.spyder_copilot_chat/config.json``（失败返回 {}）。"""
    try:
        with open(_config_file(), "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, OSError, ValueError):
        return {}


def _safe_int(value, default):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _safe_float(value, default):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _stops_for(language):
    """按语言返回 FIM stop 序列（spec §5.2）。"""
    lang = (language or "").lower()
    if lang in ("python", "py", "pyw"):
        return list(FIM_STOP_PYTHON)
    if lang in ("markdown", "md"):
        return ["\n\n\n"]
    return list(FIM_STOP_DEFAULT)


def _estimate_next_indent(prefix):
    """估算光标所在行的下一行缩进（空格数，tab 按 4 展开）。"""
    try:
        line = prefix.rsplit("\n", 1)[-1]
        indent = line[: len(line) - len(line.lstrip())]
        return len(indent.expandtabs(4))
    except Exception:  # pragma: no cover - 纯防御
        return 0


def _http_error_kind(status):
    """把 HTTP 状态码映射为内部错误语义（spec §5.5）。"""
    if status in (401, 403):
        return "auth"
    if status == 402:
        return "quota"
    if status == 429:
        return "rate_limit"
    if status == 466:
        return "client_rejected"
    if status == 499:
        return "cancelled"
    return "http"


def _choice_text(payload):
    """从 legacy completions 响应块里取 ``choices[0].text``。"""
    try:
        choices = payload.get("choices") or []
        for choice in choices:
            if choice.get("index", 0) == 0:
                return choice.get("text") or ""
        if choices:
            return choices[0].get("text") or ""
    except (AttributeError, TypeError, IndexError):
        pass
    return ""


def _choice_finished(payload):
    try:
        for choice in payload.get("choices") or []:
            if choice.get("finish_reason"):
                return True
    except (AttributeError, TypeError):
        pass
    return False


def _read_completion_response(resp, max_bytes=2_000_000):
    """解析 Copilot FIM 的 SSE 响应（兼容非流式 JSON）。

    官方为 SSE，每块形如::

        data: {"choices":[{"index":0,"text":"def fo","finish_reason":null}]}

    文本取 ``choices[i].text``；``finish_reason`` 非空或 ``[DONE]`` 结束。
    """
    parts = []
    other_lines = []
    saw_sse = False
    total = 0
    try:
        for raw in resp:
            total += len(raw)
            if total > max_bytes:
                break
            line = raw.decode("utf-8", "replace").strip()
            if not line:
                continue
            if line.startswith("data:"):
                saw_sse = True
                payload = line[5:].strip()
                if payload == "[DONE]":
                    break
                try:
                    obj = json.loads(payload)
                except json.JSONDecodeError:
                    continue
                text = _choice_text(obj)
                if text:
                    parts.append(text)
                if _choice_finished(obj):
                    break
            else:
                other_lines.append(line)
    except (OSError, ValueError) as e:
        logger.debug("FIM SSE read failed: %s", e)

    if parts:
        return "".join(parts).rstrip()

    # 兜底：某些代理会忽略 stream=True，直接返回一个完整 JSON
    if not saw_sse and other_lines:
        try:
            obj = json.loads("\n".join(other_lines))
        except json.JSONDecodeError:
            return ""
        return _choice_text(obj).rstrip()
    return ""


# ---------------------------------------------------------------------------
# 网络请求（全部在后台线程执行，禁止在 GUI 线程调用）
# ---------------------------------------------------------------------------


def _fetch_copilot_fim(cfg, prefix, suffix, language):
    """调用官方 Copilot Proxy legacy ``/completions``（spec §9.4）。"""
    try:
        token = get_valid_copilot_token(raise_on_error=True)
    except CopilotNotLoggedInError:
        return "", "auth"
    except CopilotTokenInvalidError:
        return "", "auth"
    except CopilotSubscriptionError:
        return "", "subscription"
    except CopilotRateLimitedError:
        return "", "rate_limit"
    except CopilotNetworkError:
        return "", "network"
    except CopilotAuthError:
        return "", "auth"
    except Exception:  # pragma: no cover - 防御
        logger.debug("Copilot token lookup failed", exc_info=True)
        return "", "auth"

    if not token:
        return "", "auth"

    engine = cfg.get("engine") or FIM_ENGINE_DEFAULT
    body = {
        "prompt": prefix,
        "suffix": suffix,
        "max_tokens": cfg["max_tokens"],
        "temperature": cfg["temperature"],
        "top_p": 1,
        "n": 1,
        "stop": _stops_for(language),
        "stream": True,
        "extra": {
            "language": language or "",
            "next_indent": _estimate_next_indent(prefix),
            "trim_by_indentation": True,
        },
    }

    url = "{0}/v1/engines/{1}/completions".format(proxy_base(), engine)
    try:
        headers = build_fim_headers(token)
        request = urllib.request.Request(
            url, data=json.dumps(body).encode("utf-8"),
            headers=headers, method="POST",
        )
        with urllib.request.urlopen(
            request, timeout=cfg["timeout"]
        ) as resp:
            return _read_completion_response(resp), ""
    except urllib.error.HTTPError as e:
        kind = _http_error_kind(e.code)
        try:
            body_text = e.read().decode("utf-8", "replace")[:500]
        except Exception:
            body_text = ""
        logger.debug("Copilot FIM HTTP %s (%s): %s", e.code, kind, body_text)
        return "", kind
    except (urllib.error.URLError, TimeoutError, ssl.SSLError, OSError) as e:
        logger.debug("Copilot FIM network error: %s", e)
        return "", "network"
    except Exception:
        logger.debug("Copilot FIM unexpected error", exc_info=True)
        return "", "error"


def _fetch_ollama_fim(cfg, prefix, suffix, language):
    """调用 Ollama 的 FIM（``/api/generate`` 带 suffix）。"""
    gc = _load_global_config()
    api_url = str(gc.get("api_url") or "http://localhost:11434").rstrip("/")
    if api_url.endswith("/v1"):
        api_url = api_url[:-3].rstrip("/")
    model = cfg.get("model") or gc.get("model") or "qwen2.5-coder"
    body = {
        "model": model,
        "prompt": prefix,
        "suffix": suffix,
        "options": {
            "temperature": cfg["temperature"],
            "num_predict": cfg["max_tokens"],
        },
        "stream": False,
    }
    try:
        request = urllib.request.Request(
            "{0}/api/generate".format(api_url),
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(
            request, timeout=cfg["timeout"]
        ) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
        return (data.get("response") or "").rstrip(), ""
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        logger.debug("Ollama FIM network error: %s", e)
        return "", "network"
    except Exception:
        logger.debug("Ollama FIM failed", exc_info=True)
        return "", "error"


_OPENAI_DEFAULT_URLS = {
    "openai": "https://api.openai.com",
    "deepseek": "https://api.deepseek.com",
}


def _fetch_openai_fim(cfg, prefix, suffix, language):
    """OpenAI 兼容后端的 FIM（用“填空”prompt 走 chat/completions）。"""
    gc = _load_global_config()
    gc_provider = str(gc.get("provider") or "").lower()
    api_url = str(gc.get("api_url") or "").rstrip("/")
    if not api_url:
        api_url = _OPENAI_DEFAULT_URLS.get(
            gc_provider, "https://api.openai.com"
        )
    api_key = gc.get("api_key") or ""
    model = cfg.get("model") or gc.get("model") or "gpt-4o-mini"
    prompt = (
        "Complete the following {lang} code. Output ONLY the code that "
        "belongs between the two markers, with no explanation and no "
        "duplicated context.\n\n"
        "--- code before cursor ---\n{prefix}\n"
        "--- code after cursor ---\n{suffix}\n"
        "--- completion ---\n"
    ).format(lang=language or "", prefix=prefix, suffix=suffix)
    body = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": cfg["max_tokens"],
        "temperature": cfg["temperature"],
        "stream": False,
    }
    # DeepSeek 官方文档：FIM 补全仅非思考模式支持，且思考模式默认开启，
    # 不显式关闭会让补全内容被 reasoning_content 吃掉。
    if gc_provider == "deepseek":
        body["thinking"] = {"type": "disabled"}
    headers = {
        "Content-Type": "application/json",
        "Authorization": "Bearer {0}".format(api_key),
    }
    try:
        request = urllib.request.Request(
            "{0}/chat/completions".format(api_url),
            data=json.dumps(body).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        with urllib.request.urlopen(
            request, timeout=cfg["timeout"]
        ) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
        choices = data.get("choices") or [{}]
        content = (choices[0].get("message") or {}).get("content") or ""
        return content.rstrip(), ""
    except urllib.error.HTTPError as e:
        kind = _http_error_kind(e.code)
        logger.debug("OpenAI-compatible FIM HTTP %s (%s)", e.code, kind)
        return "", kind
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        logger.debug("OpenAI-compatible FIM network error: %s", e)
        return "", "network"
    except Exception:
        logger.debug("OpenAI-compatible FIM failed", exc_info=True)
        return "", "error"


def _fetch_completion(cfg, prefix, suffix, language):
    """分派到具体后端，返回 ``(text, error_kind)``。

    该函数**只会在后台线程**被调用，可以安全地做阻塞网络请求。
    """
    backend = (cfg.get("provider") or "copilot").lower()
    if backend == "copilot":
        return _fetch_copilot_fim(cfg, prefix, suffix, language)
    if backend == "ollama":
        return _fetch_ollama_fim(cfg, prefix, suffix, language)
    if backend in ("openai", "deepseek"):
        return _fetch_openai_fim(cfg, prefix, suffix, language)
    logger.debug("Unknown FIM backend %r, falling back to copilot", backend)
    return _fetch_copilot_fim(cfg, prefix, suffix, language)


# ---------------------------------------------------------------------------
# 后台 worker（QThreadPool / QRunnable）
# ---------------------------------------------------------------------------


class _FimWorkerSignals(QObject):
    """worker -> 主线程的结果通道（跨线程 emit 由 Qt 自动排队）。"""

    completed = Signal(int, str, str)  # (token, text, error_kind)


_FIM_SIGNALS = None


def _fim_worker_signals():
    """全局单例 signals：provider 关闭时 worker 仍可安全 emit。

    QRunnable 不是 QObject，不能自带 signal；这里用一个进程级 QObject 作为
    中转。该对象生命周期与进程一致，避免 provider 被销毁后 worker emit 崩溃
    （Qt 会因对象已删除而抛 RuntimeError）。
    """
    global _FIM_SIGNALS
    if _FIM_SIGNALS is None:
        _FIM_SIGNALS = _FimWorkerSignals()
    return _FIM_SIGNALS


class _FimWorker(QRunnable):
    """在后台线程执行一次 FIM 请求。"""

    def __init__(self, token, cfg, prefix, suffix, language, deadline):
        super().__init__()
        self.setAutoDelete(True)
        self._token = token
        self._cfg = dict(cfg)
        self._prefix = prefix
        self._suffix = suffix
        self._language = language
        self._deadline = deadline

    def run(self):  # noqa: D401 - Qt 接口
        text = ""
        error = ""
        try:
            if time.monotonic() <= self._deadline:
                text, error = _fetch_completion(
                    self._cfg, self._prefix, self._suffix, self._language
                )
            else:
                error = "cancelled"
        except Exception:  # pragma: no cover - 防御
            logger.debug("FIM worker crashed", exc_info=True)
            text, error = "", "error"
        try:
            _fim_worker_signals().completed.emit(
                self._token, text or "", error or ""
            )
        except RuntimeError:  # pragma: no cover - 解释器退出
            pass


# ---------------------------------------------------------------------------
# 配置页（让用户能真正打开 FIM，修复 P0-2c）
# ---------------------------------------------------------------------------

try:  # pragma: no cover - 依赖 Spyder 版本
    from spyder.api.preferences import SpyderPreferencesTab

    class CopilotFimConfigTab(SpyderPreferencesTab):
        """Preferences > Completion and linting > Copilot FIM。"""

        TITLE = "Copilot FIM"

        def __init__(self, parent):
            super().__init__(parent)
            layout = QVBoxLayout(self)
            info = QLabel(
                "GitHub Copilot 风格的行内补全（ghost text）。\n"
                "启用后，在编辑器里打字停顿约 320ms 就会在光标后出现灰字；\n"
                "Tab 接受（Ctrl+Z 可整段撤销），Esc 丢弃。灰字不在文档里，\n"
                "所以不会被 lint 检查到，也不会挪动光标。\n"
                "编辑器里按 Ctrl+K 可就地改写选中代码（先看 diff 再落盘）。\n"
                "Copilot 后端使用官方 FIM 端点，需要先登录 GitHub Copilot。\n"
                "DeepSeek（官方文档）：FIM 模型用 deepseek-flash，仅非思考模式\n"
                "支持；官方原生 FIM 端点是 https://api.deepseek.com/beta，\n"
                "最大补全 4K。本插件用 /chat/completions 的填空提示词实现，\n"
                "并会自动带上 thinking=disabled。"
            )
            info.setWordWrap(True)
            layout.addWidget(info)
            layout.addWidget(self.create_checkbox(
                "启用 FIM 行内补全（Tab 灰字）", "fim_enabled", default=True,
                tip="关闭时不会发出任何补全请求",
            ))
            layout.addWidget(self.create_checkbox(
                "同时在 Ctrl+Space 弹窗里显示", "fim_popup_enabled",
                default=False,
                tip="默认关闭：灰字已经够用，弹窗里再混进 FIM 候选会"
                    "和 LSP 的符号补全抢排序",
            ))
            layout.addWidget(self.create_combobox(
                "补全后端",
                [("跟随聊天面板", "auto"),
                 ("GitHub Copilot", "copilot"),
                 ("Ollama（本地）", "ollama"),
                 ("OpenAI 兼容 API", "openai")],
                "fim_provider", default="auto",
            ))
            layout.addWidget(self.create_lineedit(
                "Copilot engine（仅 copilot 后端）",
                "fim_engine", default=FIM_ENGINE_DEFAULT,
                tip="官方默认 gpt-41-copilot，可改为其他 Copilot FIM engine",
            ))
            layout.addWidget(self.create_lineedit(
                "模型（ollama / openai 后端）",
                "fim_model", default="",
                tip="DeepSeek 官方 FIM 模型为 deepseek-flash，且仅非思考模式支持",
            ))
            layout.addWidget(self.create_spinbox(
                "最大 tokens", "", "fim_max_tokens",
                default=160, min_=32, max_=4096, step=10,
            ))
            layout.addWidget(self.create_spinbox(
                "温度", "", "fim_temperature",
                default=0.0, min_=0, max_=2, step=0.1,
            ))
            layout.addWidget(self.create_spinbox(
                "防抖（毫秒）", "", "fim_debounce_ms",
                default=150, min_=0, max_=2000, step=10,
            ))
            # 0.5.3 修复：fim_timeout 在 CONF_DEFAULTS 和用户 spyder.ini 中
            # 保存的是 float（8.0）。Spyder 的 create_spinbox 只按 step 的
            # Python 类型选择控件：step 为 int 时生成 QSpinBox，其
            # setValue() 拒绝 float，导致 load_from_conf 抛 TypeError 并中断
            # 整个首选项对话框的创建。这里统一用 float 参数，使控件为
            # QDoubleSpinBox，与存储类型一致。
            layout.addWidget(self.create_spinbox(
                "请求超时（秒）", "", "fim_timeout",
                default=6.0, min_=1.0, max_=30.0, step=0.5,
            ))
            layout.addStretch(1)

    _CONF_TABS = [CopilotFimConfigTab]
except Exception:  # pragma: no cover - 老版本 Spyder
    logger.debug("CopilotFimConfigTab unavailable", exc_info=True)
    _CONF_TABS = []


# ---------------------------------------------------------------------------
# Provider
# ---------------------------------------------------------------------------

_STATUS_MESSAGES = {
    "auth": "Copilot 登录已失效，请在 Copilot Chat 设置中重新登录 GitHub",
    "quota": "Copilot 免费补全额度已用尽（HTTP 402）",
    "rate_limit": "Copilot 补全请求过于频繁，请稍后再试（HTTP 429）",
    "client_rejected": "Copilot 拒绝了当前客户端（HTTP 466），请检查协议头",
    "subscription": "当前 GitHub 账号未开通 Copilot 补全服务",
    "network": "无法连接 Copilot，请检查网络或代理设置",
    "config": "FIM 后端未配置 API 地址 / API Key",
}


class CopilotFimProvider(SpyderCompletionProvider):
    """FIM 补全 Provider（Copilot / Ollama / OpenAI 兼容）。"""

    COMPLETION_PROVIDER_NAME = "copilot_fim_provider"
    DEFAULT_ORDER = 2
    SLOW = True
    CONF_SECTION = "copilot_chat"
    CONF_VERSION = "1.1.0"
    CONF_DEFAULTS = [
        ("fim_enabled", True),
        ("fim_popup_enabled", False),  # 是否同时喂 Spyder 的补全弹窗
        ("fim_provider", "auto"),   # auto / copilot / ollama / openai
        ("fim_engine", FIM_ENGINE_DEFAULT),
        ("fim_model", ""),          # 空 = 用聊天面板的模型
        ("fim_max_tokens", 160),    # 灰字最多两三行，长了对体验是负收益
        ("fim_temperature", 0.0),   # 补全要确定性
        ("fim_context_before", 3000),
        ("fim_context_after", 800),
        ("fim_debounce_ms", 150),
        ("fim_timeout", 6.0),
    ]
    CONF_TABS = _CONF_TABS

    #: 供插件 / ghost text 控制器订阅的错误提示（主线程发）
    sig_fim_status = Signal(str)

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    def __init__(self, parent, config):
        super().__init__(parent, config)
        self.started = False
        self._document_texts = {}
        self._debounce_timer = QTimer(self)
        self._debounce_timer.setSingleShot(True)
        self._debounce_timer.timeout.connect(self._fire_pending)
        self._pending = None

        # 后台线程池（不占用全局池，避免和 Spyder 其它组件互相拖累）
        self._threadpool = QThreadPool(self)
        self._threadpool.setMaxThreadCount(4)

        # token -> ("request", req_id) | ("ghost", callback)
        self._inflight = {}
        self._next_token = count(1)

        # 冷却 / 熔断状态（主线程读写）
        self._quota_until = 0.0
        self._rate_limit_until = 0.0
        self._client_rejected = False
        self._last_auth_clear = 0.0
        self._last_status = ""
        self._last_status_time = 0.0

        global _INSTANCE
        _INSTANCE = self

        self._signals = _fim_worker_signals()
        self._signals.completed.connect(self._on_worker_finished)

    def get_name(self):
        return "Copilot FIM Completion"

    def start(self):
        self.started = True
        self.sig_provider_ready.emit(self.COMPLETION_PROVIDER_NAME)

    def shutdown(self):
        self.started = False
        self._pending = None
        self._debounce_timer.stop()
        self._inflight.clear()
        try:
            self._threadpool.clear()
            self._threadpool.waitForDone(1000)
        except RuntimeError:  # pragma: no cover - 对象已删除
            pass

    def start_completion_services_for_language(self, language):
        return True

    # ------------------------------------------------------------------
    # SpyderCompletionProvider API
    # ------------------------------------------------------------------

    def send_response(self, response, resp_id):
        """把响应真正发回 Spyder（基类是空实现，见审计 P0-2a）。"""
        try:
            self.sig_response_ready.emit(
                self.COMPLETION_PROVIDER_NAME, resp_id, response
            )
        except RuntimeError:  # pragma: no cover - 解释器退出
            pass

    def send_notification(self, language, notification_type, notification):
        """缓存 didOpen / didChange 文本；didClose 清理。

        这些通知 ``requires_response=False``，Spyder 走 ``send_notification``
        （见审计 P0-2b），绝不能写在 ``send_request`` 里。
        """
        try:
            if not isinstance(notification, dict):
                return
            filename = notification.get("file")
            if notification_type in (_DID_OPEN, _DID_CHANGE):
                if filename:
                    self._document_texts[filename] = notification.get(
                        "text", ""
                    ) or ""
            elif notification_type == _DID_CLOSE:
                if filename:
                    self._document_texts.pop(filename, None)
        except Exception:  # pragma: no cover - 防御
            logger.debug("FIM notification failed", exc_info=True)

    def send_request(self, language, req_type, req, req_id=None):
        try:
            if isinstance(req, dict):
                req["language"] = language
            if req_type == _COMPLETION:
                self._handle_completion(language, req, req_id)
            elif req_type == _DID_OPEN:
                self._cache_text(req)
                self.send_response(_EMPTY_LIST, req_id)
            elif req_type == _DID_CHANGE:
                self._cache_text(req)
                self.send_response(_EMPTY_LIST, req_id)
            elif req_type == _DID_CLOSE:
                try:
                    self._document_texts.pop(req.get("file"), None)
                except AttributeError:
                    pass
                self.send_response(_EMPTY_LIST, req_id)
            else:
                self.send_response(_EMPTY_LIST, req_id)
        except Exception:  # pragma: no cover - 防御
            logger.debug("FIM send_request failed", exc_info=True)
            self.send_response(_EMPTY_LIST, req_id)

    def broadcast_notification(self, notification_type, notification):
        self.send_notification(
            (notification or {}).get("language", ""),
            notification_type,
            notification,
        )

    # ------------------------------------------------------------------
    # 配置读取
    # ------------------------------------------------------------------

    def _conf(self, key, default=None):
        try:
            value = self.get_conf(key, default)
        except Exception:
            logger.debug("FIM get_conf(%r) failed", key, exc_info=True)
            return default
        return default if value is None else value

    def _popup_enabled(self):
        try:
            return bool(self._conf("fim_popup_enabled", False))
        except Exception:
            return False

    def is_fim_enabled(self):
        try:
            return bool(self._conf("fim_enabled", True))
        except Exception:
            return False

    def _read_config(self):
        provider = str(self._conf("fim_provider", "auto")).lower()
        if provider in ("copilot", "github", "github-copilot"):
            provider = "copilot"
        elif provider == "deepseek":
            # 旧配置里的 deepseek 是 OpenAI 兼容后端
            provider = "openai"
        elif provider not in ("ollama", "openai"):
            provider = "auto"

        if provider == "auto":
            # 跟着聊天面板走：聊天用 Copilot 就复用 Copilot 通道，
            # 否则用同一份 base_url / api_key / model 走 OpenAI 兼容接口。
            # 这样用户只需要配一次模型，灰字不会因为"没单独配 FIM"而哑掉。
            global_provider = str(
                _load_global_config().get("provider") or "").lower()
            provider = "copilot" if global_provider == "copilot" else "openai"
        return {
            "provider": provider,
            "engine": str(
                self._conf("fim_engine", FIM_ENGINE_DEFAULT)
                or FIM_ENGINE_DEFAULT
            ),
            "model": str(self._conf("fim_model", "") or ""),
            "max_tokens": _safe_int(self._conf("fim_max_tokens", 160), 160),
            "temperature": _safe_float(
                self._conf("fim_temperature", 0.0), 0.0
            ),
            "context_before": max(
                200, _safe_int(self._conf("fim_context_before", 3000), 3000)
            ),
            "context_after": max(
                50, _safe_int(self._conf("fim_context_after", 800), 800)
            ),
            "timeout": max(1.0, _safe_float(self._conf("fim_timeout", 6.0), 6.0)),
        }

    def _debounce_ms(self):
        value = _safe_int(self._conf("fim_debounce_ms", 150), 150)
        return max(0, min(value, 5000))

    # ------------------------------------------------------------------
    # 文本来源（修复 P0-2b）
    # ------------------------------------------------------------------

    def _cache_text(self, req):
        try:
            filename = req.get("file")
            if filename:
                self._document_texts[filename] = req.get("text", "") or ""
        except AttributeError:
            pass

    def _current_text(self, req):
        """优先取编辑器实时文本（未保存修改也能拿到），缓存兜底。"""
        try:
            instance = req.get("response_instance")
        except AttributeError:
            instance = None
        if instance is not None:
            try:
                text = instance.toPlainText()
                if isinstance(text, str):
                    return text
            except RuntimeError:
                pass
            except Exception:
                logger.debug("response_instance.toPlainText failed",
                             exc_info=True)
        try:
            return self._document_texts.get(req.get("file") or "", "")
        except AttributeError:
            return ""

    # ------------------------------------------------------------------
    # 冷却 / 熔断
    # ------------------------------------------------------------------

    def _block_reason(self):
        now = time.time()
        if self._client_rejected:
            return "client_rejected"
        if now < self._quota_until:
            return "quota"
        if now < self._rate_limit_until:
            return "rate_limit"
        return ""

    def _handle_error(self, error):
        if not error:
            return
        now = time.time()
        message = _STATUS_MESSAGES.get(error) or (
            "Copilot FIM 请求失败（{0}）".format(error)
        )
        if error == "auth":
            if now - self._last_auth_clear > 15:
                self._last_auth_clear = now
                try:
                    clear_tokens()
                except Exception:  # pragma: no cover - 防御
                    logger.debug("clear_tokens failed", exc_info=True)
                logger.warning(
                    "Copilot FIM: token 失效，已清除缓存，请重新登录"
                )
        elif error == "quota":
            self._quota_until = now + 300
            logger.warning("Copilot FIM: 免费补全额度已用尽（402）")
        elif error == "rate_limit":
            self._rate_limit_until = now + 10
            logger.info("Copilot FIM: 限流，冷却 10 秒（429）")
        elif error == "client_rejected":
            self._client_rejected = True
            logger.error("Copilot FIM: 客户端被拒绝（466），已熔断 FIM")
        elif error in ("cancelled",):
            return
        else:
            logger.debug("Copilot FIM error: %s", error)

        # 同一个提示 30 秒内不重复弹，避免每次按键都刷屏
        if (
            message != self._last_status
            or now - self._last_status_time > 30
        ):
            self._last_status = message
            self._last_status_time = now
            if message:
                try:
                    self.sig_fim_status.emit(message)
                except RuntimeError:  # pragma: no cover
                    pass

    # ------------------------------------------------------------------
    # 补全请求（Spyder 弹窗路径）
    # ------------------------------------------------------------------

    def _handle_completion(self, language, req, req_id):
        # 灰字（request_fim_suggestion）与弹窗（EXT 补全请求）是两条路径：
        # 默认只开灰字。否则用户一开启，Ctrl+Space 的弹窗里就会混进
        # "FIM 候选"，和 LSP 的符号补全抢排序，反而更难选。
        if not self.is_fim_enabled() or not self._popup_enabled():
            self.send_response(_EMPTY_LIST, req_id)
            return
        blocked = self._block_reason()
        if blocked:
            self._handle_error(blocked)
            self.send_response(_EMPTY_LIST, req_id)
            return

        text = self._current_text(req)
        if not text:
            self.send_response(_EMPTY_LIST, req_id)
            return

        # 多次按键只保留最后一次请求；防止旧结果乱序覆盖
        self._pending = (language, req, req_id)
        self._debounce_timer.start(self._debounce_ms())

    def _fire_pending(self):
        pending = self._pending
        if pending is None:
            return
        self._pending = None
        language, req, req_id = pending
        self._do_completion(language, req, req_id)

    def _do_completion(self, language, req, req_id):
        try:
            text = self._current_text(req)
            offset = _safe_int(req.get("offset", len(text)), len(text))
            offset = max(0, min(offset, len(text)))
            cfg = self._read_config()
            self._start_worker(
                cfg, text, offset, language, ("request", req_id)
            )
        except Exception:
            logger.debug("FIM completion dispatch failed", exc_info=True)
            self.send_response(_EMPTY_LIST, req_id)

    # ------------------------------------------------------------------
    # Ghost text 路径（由 fim/ghost.py 调用）
    # ------------------------------------------------------------------

    def request_fim_suggestion(self, text, offset, language, callback):
        """为 ghost text 异步请求一次补全。

        参数
        ----
        text : str
            编辑器全文。
        offset : int
            光标在全文中的绝对位置。
        language : str
            语言标识（可为空）。
        callback : callable
            ``callback(text, error)``，**在主线程**被调用；``error`` 为错误
            语义字符串（空表示成功）。

        返回
        ----
        bool
            True 表示已排队；False 表示未启用 / 被冷却 / 参数非法。
        """
        if not self.is_fim_enabled():
            return False
        if not isinstance(text, str) or not text:
            return False
        if not callable(callback):
            return False
        blocked = self._block_reason()
        if blocked:
            self._handle_error(blocked)
            return False
        try:
            offset = int(offset)
        except (TypeError, ValueError):
            return False
        offset = max(0, min(offset, len(text)))
        cfg = self._read_config()
        try:
            self._start_worker(
                cfg, text, offset, language, ("ghost", callback)
            )
        except Exception:
            logger.debug("FIM ghost dispatch failed", exc_info=True)
            return False
        return True

    # ------------------------------------------------------------------
    # Worker 调度
    # ------------------------------------------------------------------

    def _start_worker(self, cfg, text, offset, language, target):
        before = cfg["context_before"]
        after = cfg["context_after"]
        prefix = text[max(0, offset - before):offset]
        suffix = text[offset:offset + after]
        token = next(self._next_token)
        self._inflight[token] = target
        deadline = time.monotonic() + max(1.0, cfg["timeout"]) + 2.0
        self._threadpool.start(
            _FimWorker(token, cfg, prefix, suffix, language or "", deadline)
        )

    def _on_worker_finished(self, token, text, error):
        """主线程槽：worker 结果处理（发送响应 / 触发回调）。"""
        target = self._inflight.pop(token, None)
        if target is None:
            # 已被取消 / provider 已关闭
            return
        if error:
            self._handle_error(error)

        kind, payload = target
        if kind == "request":
            item_list = []
            if text and not error:
                item = self._build_completion_item(text)
                if item is not None:
                    item_list.append(item)
            self.send_response({"params": item_list}, payload)
            return

        try:
            payload(text if not error else "", error or "")
        except Exception:  # pragma: no cover - 防御
            logger.debug("FIM ghost callback failed", exc_info=True)

    @staticmethod
    def _build_completion_item(text):
        try:
            first_line = (text.strip().splitlines() or [""])[0][:60]
            label = first_line or "Copilot FIM"
            return {
                "kind": CompletionItemKind.SNIPPET,
                "insertText": text,
                "label": label,
                "sortText": "0",
                "filterText": label,
                "documentation": "",
                "provider": "Copilot FIM",
            }
        except Exception:  # pragma: no cover - 防御
            return None


# ---------------------------------------------------------------------------
# 模块级便捷函数（plugin / ghost 用）
# ---------------------------------------------------------------------------


def get_fim_provider():
    """返回当前活动的 FIM provider 实例（没有则 None）。"""
    return _INSTANCE
