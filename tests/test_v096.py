# -*- coding: utf-8 -*-
"""v0.8.16 回归：真实 Key 线上验证后修掉的两个 Hy4 配置坑。

现场：用户把同一个 TokenHub Key 填进插件，报
``Chat request failed: 模型「hy4」在 腾讯混元 TokenHub 上不可用``
并弹出 Spyder 内部错误窗口。拿真实 Key 做线上探测后发现问题有两层：

1. **地址写错**。用户配置里是 ``/plan/v3``（套餐页地址），实测该路径
   ``/chat/completions`` 回 401002、``/models`` 回 404；改成 ``/v1``
   两个接口立刻 200。此外 TokenHub 的两套广州入口各自签发 Key：
   同一把 Key 在 ``tokenhub.tencentmaas.com`` 返回 200，在
   ``tokenhub.tencentcloudmaas.com`` 返回 401002。旧版把后者设成默认。

2. **误诊**。401002 的原文是 ``The API Key does not exist or signature
   verification failed``，旧 ``_MARKERS`` 里有一条过宽的
   ``"does not exist"``，于是鉴权失败被判成「模型名写错」，用户照着
   改模型名永远修不好。

这份用例钉住：真实错误体的分类、``/v1`` 自动纠正、默认地址、以及
「有 Key 时」的真实线上链路。密钥只从环境变量读，绝不写进仓库。

直接跑：``python tests/test_v096.py``（也兼容 pytest）。
带真实 Key 跑线上用例：``TOKENHUB_KEY=... TOKENHUB_RUN_LIVE=1 python tests/test_v096.py``
"""

import json
import os
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from spyder_copilot_chat import api_errors as A  # noqa: E402
from spyder_copilot_chat.widgets import settings_dialog as SD  # noqa: E402

_results = []


def check(name, condition, extra=""):
    _results.append((name, bool(condition), extra))
    if not condition:
        print("FAIL  {0}  {1}".format(name, extra))


def eq(name, got, want):
    check(name, got == want, "got={0!r} want={1!r}".format(got, want))


SHORT = "https://tokenhub.tencentmaas.com/v1"
LONG = "https://tokenhub.tencentcloudmaas.com/v1"

# ---------------------------------------------------------------------------
# 真实线上抓到的错误体（request_id 已换成占位，不含任何密钥）
# ---------------------------------------------------------------------------

BODY_401002 = json.dumps({
    "error": {
        "type": "authentication_error",
        "code": "401002",
        "message": "The API Key does not exist or signature verification "
                   "failed. Please check whether the API Key is correct. "
                   "See: https://console.cloud.tencent.com/tokenhub/apikey",
        "message_zh": "API Key 不存在或签名校验失败，请检查 API Key 是否正确",
        "source": "gateway",
        "request_id": "<redacted>",
    }}, ensure_ascii=False)

BODY_400004 = json.dumps({
    "error": {
        "type": "invalid_request_error",
        "code": "400004",
        "message": "The model or service ID hy4-not-exist-xyz does not "
                   "exist. Please check whether the service ID is correct. "
                   "Service IDs are available in the Online Inference "
                   "Service list in the console.",
        "message_zh": "请求中的模型或服务 ID 不存在，请检查服务 ID 是否正确",
    }}, ensure_ascii=False)

BODY_402_NOT_ACTIVATED = json.dumps({
    "error": {
        "type": "insufficient_quota_error",
        "code": "402003",
        "message": "model hy4 does not exist or service not activated",
    }}, ensure_ascii=False)


# ---------------------------------------------------------------------------
# 1. 真实错误体的分类
# ---------------------------------------------------------------------------

def test_401002_is_auth_not_model():
    info = A.classify(A.ApiError(401, BODY_401002,
                                provider="hunyuan", model="hy4"))
    eq("401002 归为 AUTH", info.kind, A.AUTH)
    eq("401002 status 保留", info.status, 401)
    eq("401002 标题", info.title, "API Key 失效 / 未登录（HTTP 401）")
    card = A.card(info)
    check("401002 不再报「模型不可用」", "模型不可用" not in card)
    check("401002 卡片是成品卡片", A.is_card(card))
    check("401002 原文仍在卡片里", "API Key does not exist" in card)

    # 不走 ApiError、只给 status + body 也要判对
    eq("裸 status+body 也归 AUTH",
       A.classify(status=401, body=BODY_401002,
                  provider="hunyuan", model="hy4").kind, A.AUTH)


def test_401_steps_explain_both_pitfalls():
    info = A.classify(A.ApiError(401, BODY_401002,
                                provider="hunyuan", model="hy4"))
    card = A.card(info)
    check("401 提示 /v1 结尾", "/v1` 结尾" in card or "/v1 结尾" in card)
    check("401 提示套餐页不是接口", "套餐页" in card)
    check("401 提示两套广州入口", "两套广州入口" in card)
    check("401 提示地域不匹配", "地域" in card)
    check("401 给 Key 控制台链接", "tokenhub/apikey" in card)
    check("401 步骤不少于 4 条", len(info.steps) >= 4,
          "steps=%d" % len(info.steps))
    eq("401 不会自动重试", info.retryable, False)
    eq("401 的 model 从 exc 补全", info.model, "hy4")


def test_real_400004_is_still_model():
    info = A.classify(A.ApiError(400, BODY_400004,
                                provider="hunyuan", model="hy4-not-exist-xyz"))
    eq("真实 400004 仍归 MODEL", info.kind, A.MODEL)
    check("400004 卡片给出模型名", "hy4-not-exist-xyz" in A.card(info))
    check("400004 仍给 GET /v1/models", "/v1/models" in A.card(info))


def test_hunyuan_402_still_balance():
    info = A.classify(A.ApiError(402, BODY_402_NOT_ACTIVATED,
                                provider="hunyuan", model="hy4"))
    eq("402 未开通仍归 BALANCE", info.kind, A.BALANCE)
    check("402 卡片写未开通", "未开通" in A.card(info))


def test_status_401_beats_model_wording():
    # 哪怕错误体只写「模型不存在」，401/403 也必须是鉴权结论
    for status in (401, 403):
        info = A.classify(status=status,
                          body=json.dumps({"error": {
                              "message": "model hy4 does not exist"}}),
                          provider="hunyuan", model="hy4")
        eq("status %d 优先于措辞" % status, info.kind, A.AUTH)
    # 400 不受影响
    eq("status 400 仍按措辞判 MODEL",
       A.classify(status=400,
                  body=json.dumps({"error": {
                      "message": "model hy4 does not exist"}}),
                  provider="hunyuan", model="hy4").kind, A.MODEL)


def test_no_bare_does_not_exist_marker():
    flat = [w for words, kind in A._MARKERS if kind == A.MODEL for w in words]
    check("MODEL markers 不再含裸 does not exist",
          "does not exist" not in flat, repr(flat))
    check("MODEL markers 含精确短语",
          "model or service id" in flat or "model does not exist" in flat)


def test_classify_never_raises_on_garbage():
    for bad in (None, "", "not json", "{", b"bytes"):
        try:
            info = A.classify(status=401, body=bad, provider="hunyuan")
            check("脏输入不抛异常 %r" % (bad,), info.kind in (A.AUTH, A.UNKNOWN))
        except Exception as exc:  # noqa: BLE001
            check("脏输入不抛异常 %r" % (bad,), False, repr(exc))


# ---------------------------------------------------------------------------
# 2. 默认地址与 /v1 自动纠正
# ---------------------------------------------------------------------------

def test_default_url_is_short_alias():
    eq("hunyuan 默认地址改为短别名",
       SD.PROVIDERS["hunyuan"]["default_url"], SHORT)
    check("长域名仍在备选里", LONG in SD.PROVIDERS["hunyuan"]["alt_urls"])
    check("新加坡仍在备选里",
          "https://tokenhub-intl.tencentcloudmaas.com/v1"
          in SD.PROVIDERS["hunyuan"]["alt_urls"])
    check("硅谷仍在备选里",
          "https://tokenhub-us.tencentcloudmaas.com/v1"
          in SD.PROVIDERS["hunyuan"]["alt_urls"])
    eq("默认模型仍是 hy4", SD.PROVIDERS["hunyuan"]["default_model"], "hy4")
    check("hy4-preview 可选", "hy4-preview" in SD.PROVIDERS["hunyuan"]["models"])


def test_normalize_plan_url():
    cases = [
        ("https://tokenhub.tencentmaas.com/plan/v3", SHORT),
        ("https://tokenhub.tencentmaas.com/v3", SHORT),
        ("https://tokenhub.tencentcloudmaas.com/plan/v3", LONG),
        ("https://tokenhub-intl.tencentcloudmaas.com/plan/v3",
         "https://tokenhub-intl.tencentcloudmaas.com/v1"),
        ("https://tokenhub-us.tencentcloudmaas.com/plan/v3",
         "https://tokenhub-us.tencentcloudmaas.com/v1"),
        ("https://tokenhub.tencentmaas.com/v1?x=1", SHORT),
    ]
    for raw, want in cases:
        cfg = {"provider": "hunyuan", "api_url": raw, "model": "hy4"}
        eq("纠正 %s" % raw, SD.normalize_hunyuan_url(cfg)["api_url"], want)
        eq("纠正不改原 dict %s" % raw, cfg["api_url"], raw)


def test_normalize_keeps_good_and_custom():
    cases = [
        {"provider": "hunyuan", "api_url": SHORT},
        {"provider": "hunyuan", "api_url": LONG},
        {"provider": "hunyuan", "api_url": SHORT + "/"},
        {"provider": "hunyuan", "api_url": "https://my-gw.example.com/plan/v3"},
        {"provider": "hunyuan", "api_url": "http://127.0.0.1:8765/v1"},
        {"provider": "deepseek", "api_url": "https://tokenhub.tencentmaas.com/plan/v3"},
        {"provider": "hunyuan", "api_url": ""},
        {"provider": "hunyuan"},
        {},
        None,
        [],
    ]
    for i, cfg in enumerate(cases):
        want = (cfg or {}).get("api_url") if isinstance(cfg, dict) else cfg
        got = SD.normalize_hunyuan_url(cfg)
        if isinstance(cfg, dict):
            eq("不该改动的用例 %d" % i, got.get("api_url"), want)
        else:
            eq("非 dict 原样返回 %d" % i, got, want)


def test_load_config_normalizes():
    tmp = tempfile.mkdtemp(prefix="scpc_v096_")
    path = os.path.join(tmp, "config.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"provider": "hunyuan",
                   "api_url": "https://tokenhub.tencentmaas.com/plan/v3",
                   "model": "hy4"}, f)
    old = SD.CONFIG_FILE
    SD.CONFIG_FILE = path
    try:
        cfg = SD.load_config()
        eq("load_config 纠正 /plan/v3", cfg["api_url"], SHORT)
        eq("load_config 保留 model", cfg["model"], "hy4")
        pcfg = SD.get_provider_config()
        eq("get_provider_config 也拿到纠正后的地址",
           pcfg.get("api_url"), SHORT)
    finally:
        SD.CONFIG_FILE = old
    # 只存了 provider、没存 api_url（切到新 Provider 但还没点保存）
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"provider": "hunyuan", "model": "hy4"}, f)
    SD.CONFIG_FILE = path
    try:
        eq("未存 api_url 时给 Hy4 默认地址",
           SD.get_provider_config().get("api_url"), SHORT)
    finally:
        SD.CONFIG_FILE = old
    # 完全没有配置文件时，默认 provider 是 DeepSeek，不该串成 Hy4
    SD.CONFIG_FILE = os.path.join(tmp, "missing.json")
    try:
        eq("空配置仍默认 DeepSeek",
           SD.get_provider_config().get("provider"), "deepseek")
    finally:
        SD.CONFIG_FILE = old


def test_fim_default_url_matches():
    from spyder_copilot_chat.fim import provider as FIM
    eq("FIM auto 后端默认地址同步", FIM._OPENAI_DEFAULT_URLS["hunyuan"], SHORT)


# ---------------------------------------------------------------------------
# 3. 静态守卫：文案 / 代码里必须留着这些提示
# ---------------------------------------------------------------------------

def _read(rel):
    with open(os.path.join(ROOT, rel), "r", encoding="utf-8") as f:
        return f.read()


def test_docs_mention_both_pitfalls():
    readme = _read("README.md")
    check("README 说明默认短别名", "广州（默认，短别名）" in readme)
    check("README 说明两套入口 Key 不通用", "各自签发 Key" in readme)
    check("README 说明 /v1 结尾", "以 `/v1` 结尾" in readme)
    check("README 说明 401002 误诊已修", "401002" in readme)
    conf = _read("spyder_copilot_chat/confpage.py")
    check("偏好页给短别名", SHORT in conf)
    check("偏好页提示 /v1", "必须以 /v1 结尾" in conf)
    ae = _read("spyder_copilot_chat/api_errors.py")
    check("分类里有 401 优先规则", "status in (401, 403)" in ae)
    check("分类里 blob 纳入完整 body", "{raw_body}" in ae)


def test_no_error_level_logging_calls():
    # 沿用 v0.8.15 的守卫：包内业务日志一律 warning 以下，
    # 否则 Spyder 会把 stderr 的 ERROR 行当成未处理异常弹窗。
    import ast
    bad = []
    for dirpath, _, files in os.walk(os.path.join(ROOT, "spyder_copilot_chat")):
        if "__pycache__" in dirpath:
            continue
        for name in files:
            if not name.endswith(".py"):
                continue
            full = os.path.join(dirpath, name)
            with open(full, "r", encoding="utf-8") as f:
                tree = ast.parse(f.read())
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                fn = node.func
                if (isinstance(fn, ast.Attribute)
                        and fn.attr in ("error", "exception")
                        and isinstance(fn.value, ast.Name)
                        and fn.value.id == "logger"):
                    bad.append((os.path.relpath(full, ROOT), node.lineno))
    eq("包内没有 logger.error / logger.exception 调用", bad, [])


# ---------------------------------------------------------------------------
# 4. 可选：真实 Key 线上链路（只在显式给 Key 时跑，密钥不入库）
# ---------------------------------------------------------------------------

def test_live_hy4():
    key = os.environ.get("TOKENHUB_KEY", "").strip()
    if not key or os.environ.get("TOKENHUB_RUN_LIVE") != "1":
        print("SKIP  live Hy4（未给 TOKENHUB_KEY / TOKENHUB_RUN_LIVE）")
        return
    import requests
    h = {"Authorization": "Bearer " + key, "Content-Type": "application/json"}
    r = requests.get(SHORT + "/models", headers=h, timeout=20)
    eq("线上 /v1/models 返回 200", r.status_code, 200)
    ids = [m.get("id") for m in r.json().get("data", [])]
    check("线上模型列表含 hy4", "hy4" in ids)

    p = {"model": "hy4", "stream": False, "max_tokens": 24,
         "messages": [{"role": "user", "content": "只回复：OK"}]}
    r = requests.post(SHORT + "/chat/completions", headers=h,
                      json=p, timeout=60)
    eq("线上 chat 返回 200", r.status_code, 200)
    d = r.json()
    check("线上 chat 有 choices", bool(d.get("choices")))
    check("线上 usage 含 reasoning_tokens",
          "reasoning_tokens" in json.dumps(d.get("usage", {})))

    # 插件自己的 ChatWorker 也走一遍真实网络
    from qtpy.QtWidgets import QApplication
    from spyder_copilot_chat.widgets.chat_widget import ChatWorker
    QApplication.instance() or QApplication([])
    cfg = {
        "provider": "hunyuan", "api_url": SHORT,
        "chat_endpoint": "/chat/completions", "api_key": key,
        "model": "hy4", "temperature": 0.0, "max_tokens": 256,
        "agent_mode": False, "supports_agent": True,
        "thinking_mode": "disabled", "reasoning_effort": "no_think",
        "supports_thinking": True, "reasoning_efforts": ["high", "no_think"],
        "stream_usage": True, "max_output_tokens": 65536,
    }
    w = ChatWorker([{"role": "user", "content": "只回复两个字：收到"}], cfg)
    seen = {}
    w.done.connect(lambda p: seen.setdefault("done", p))
    w.error.connect(lambda p: seen.setdefault("error", p))
    w.usage.connect(lambda p: seen.setdefault("usage", p))
    w.run()
    check("ChatWorker 真实链路出 done", "done" in seen, repr(seen)[:200])
    check("ChatWorker 真实链路没出错误卡片", "error" not in seen)
    check("ChatWorker 中文回答无乱码",
          isinstance(seen.get("done"), str) and len(seen["done"]) > 0)
    check("ChatWorker 拿到 usage", "usage" in seen)


def main():
    import logging
    # 规范化会故意打一条 warning 提示用户；批量用例里把它静音，
    # 免得几十行同样的提示淹没测试结果。
    old_level = SD.logger.level
    SD.logger.setLevel(logging.CRITICAL)
    try:
        for name, fn in sorted(globals().items()):
            if name.startswith("test_") and callable(fn):
                try:
                    fn()
                except Exception as exc:  # noqa: BLE001
                    check(name + " 未抛异常", False, repr(exc))
    finally:
        SD.logger.setLevel(old_level)
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
