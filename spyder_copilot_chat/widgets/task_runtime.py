# -*- coding: utf-8 -*-
"""
任务运行时 — v0.5.5 / P0

把「会阻塞 Qt 主线程」的工作全部集中到这里：

  * submit()            通用后台任务（QThreadPool + 排队信号回主线程）
  * execute_tool_batch()工具批量执行（含检查点快照，支持取消）
  * run_verification()  写操作后的自动验证（py_compile / 自定义命令）
  * UsageMeter          上下文占用、token 与成本估算
  * TaskStateStore      任务级持久状态与恢复

设计约束：后台线程只做纯计算 / 文件 IO / 子进程，绝不触碰任何 QWidget；
结果通过 Qt 信号以 QueuedConnection 回到 GUI 线程后再更新 UI 与消息列表。
"""

import json
import logging
import os
import re
import subprocess
import sys
import threading
import time

from qtpy.QtCore import QObject, QThreadPool, QRunnable, Qt, Signal

logger = logging.getLogger(__name__)

CONFIG_DIR = os.path.join(os.path.expanduser("~"), ".spyder_copilot_chat")
TASK_DIR = os.path.join(CONFIG_DIR, "tasks")
ACTIVE_TASK_FILE = os.path.join(TASK_DIR, "active_task.json")

# 会改动磁盘的工具：写操作之后才需要触发自动验证
WRITE_TOOLS = {
    "create_file", "edit_file", "apply_patch", "insert_edit",
    "write_to_file", "replace_in_file", "multi_replace_string",
    "delete_file", "rename_file", "create_directory",
    "create_notebook", "add_notebook_cell", "edit_notebook_cell",
    "delete_notebook_cell", "resolve_conflict", "git_add", "git_commit",
}


# ── token 估算 ────────────────────────────────────────────────────────

_CJK_RE = re.compile(r"[\u3400-\u9fff]")


def estimate_tokens(text):
    """粗估 token 数：中日韩字符按 1 token，其余按 ~3.6 字符 1 token。"""
    if not text:
        return 0
    if not isinstance(text, str):
        text = str(text)
    cjk = len(_CJK_RE.findall(text))
    return int(cjk + max(0, len(text) - cjk) / 3.6) + 1


class UsageMeter:
    """上下文 / token / 成本计量。API 返回 usage 时用精确值，否则用估算。"""

    def __init__(self, context_window=1048576, price_in=0.0, price_out=0.0):
        self.context_window = max(1000, int(context_window or 1048576))
        self.price_in = float(price_in or 0.0)     # 元 / 百万输入 token
        self.price_out = float(price_out or 0.0)   # 元 / 百万输出 token
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.turn_tokens = 0
        self._exact = False
        self._tools_cache_key = None
        self._tools_cache_val = 0

    def reset_turn(self):
        self.turn_tokens = 0

    def add_usage(self, usage):
        """合并服务端 usage（若该 provider 在流里返回了 usage 字段）。"""
        if not isinstance(usage, dict):
            return
        p = usage.get("prompt_tokens") or usage.get("input_tokens")
        c = usage.get("completion_tokens") or usage.get("output_tokens")
        if p is None and c is None:
            return
        p, c = int(p or 0), int(c or 0)
        self.prompt_tokens += p
        self.completion_tokens += c
        self.turn_tokens += p + c
        self._exact = True

    def add_text(self, text, completion=False):
        n = estimate_tokens(text)
        self.turn_tokens += n
        if completion:
            self.completion_tokens += n
        else:
            self.prompt_tokens += n

    def context_tokens(self, messages=None, system_prompt="", tools=None):
        """估算当前请求占用的上下文 token（包含工具定义与系统提示词）。"""
        if self._exact and self.prompt_tokens:
            return self.prompt_tokens
        total = estimate_tokens(system_prompt)
        for m in messages or []:
            total += estimate_tokens(m.get("content") or "")
            for tc in m.get("tool_calls") or []:
                total += estimate_tokens(
                    (tc.get("function") or {}).get("arguments") or "")
        if tools:
            # 工具定义通常几十个、序列化较贵；按对象身份缓存一次估算
            key = (id(tools), len(tools))
            if self._tools_cache_key != key:
                try:
                    self._tools_cache_val = estimate_tokens(
                        json.dumps(tools, ensure_ascii=False))
                except (TypeError, ValueError):
                    self._tools_cache_val = 0
                self._tools_cache_key = key
            total += self._tools_cache_val
        return total

    def cost(self):
        if not (self.price_in or self.price_out):
            return None
        return (self.prompt_tokens / 1_000_000.0 * self.price_in
                + self.completion_tokens / 1_000_000.0 * self.price_out)

    def summary(self, messages=None, system_prompt="", tools=None):
        ctx = self.context_tokens(messages, system_prompt, tools)
        pct = min(100.0, ctx * 100.0 / self.context_window)

        def fmt(n):
            if n >= 1_000_000:
                return f"{n / 1_000_000.0:.1f}M"
            if n >= 1000:
                return f"{n / 1000.0:.1f}k"
            return str(n)

        parts = [f"上下文 {fmt(ctx)}/{fmt(self.context_window)} · {pct:.0f}%",
                 f"本轮 {fmt(self.turn_tokens)}"]
        c = self.cost()
        if c is not None:
            parts.append(f"¥{c:.4f}")
        parts.append("精确" if self._exact else "估算")
        return "  ·  ".join(parts)


# ── 任务级持久状态 ────────────────────────────────────────────────────

class TaskStateStore:
    """保存/恢复未完成任务。文件写入由调用方放到后台线程执行。"""

    @staticmethod
    def path():
        return ACTIVE_TASK_FILE

    @staticmethod
    def save(state):
        try:
            os.makedirs(TASK_DIR, exist_ok=True)
            data = dict(state)
            data["updated_at"] = time.time()
            data["version"] = 1
            tmp = ACTIVE_TASK_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
            os.replace(tmp, ACTIVE_TASK_FILE)
            return True
        except Exception as exc:
            logger.debug("保存任务状态失败: %s", exc)
            return False

    @staticmethod
    def load():
        try:
            with open(ACTIVE_TASK_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict) and data.get("status") in (
                    "running", "interrupted"):
                return data
        except (OSError, ValueError):
            pass
        return None

    @staticmethod
    def clear():
        try:
            if os.path.isfile(ACTIVE_TASK_FILE):
                os.remove(ACTIVE_TASK_FILE)
        except OSError:
            pass

    @staticmethod
    def resumable():
        data = TaskStateStore.load()
        return bool(data and data.get("messages"))


# ── 后台任务 ──────────────────────────────────────────────────────────

class _TaskSignals(QObject):
    finished = Signal(object)


class _BackgroundTask(QRunnable):
    def __init__(self, fn, signals):
        super().__init__()
        self._fn = fn
        self._signals = signals

    def run(self):
        try:
            payload = self._fn()
        except Exception as exc:
            logger.warning("后台任务失败: %s", exc)
            payload = {"__error__": str(exc)}
        try:
            self._signals.finished.emit(payload)
        except RuntimeError:
            pass


def submit(fn, on_done, owner):
    """在全局线程池执行 fn，结果以 QueuedConnection 回到主线程。

    owner 必须持有 ``_bg_refs`` 列表；保留引用防止 QRunnable / signals
    在任务完成前被 Python GC 回收导致信号丢失。
    """
    signals = _TaskSignals()
    signals.finished.connect(on_done, Qt.QueuedConnection)
    task = _BackgroundTask(fn, signals)
    refs = getattr(owner, "_bg_refs", None)
    if refs is not None:
        refs.append((task, signals))
        if len(refs) > 24:
            del refs[:-24]
    QThreadPool.globalInstance().start(task)
    return task


# ── 工具批量执行（后台）────────────────────────────────────────────────

def execute_tool_batch(tool_calls, base_dir, snapshot=None, cancel_event=None,
                       config=None, audit=None):
    """执行一批已批准的工具调用，返回纯数据供主线程消费。

    v0.5.6 P1：执行前跑风险分级 + 工作区沙箱校验，拦截结果作为一条
    tool error 回灌给模型；P1 元工具（load_skill / search_repo_map…）
    在这里分发，同样在后台线程完成。audit 由调用方注入，保持本函数
    无全局副作用、便于测试。
    """
    from .agentic_actions import execute_tool
    from .meta_tools import META_TOOL_NAMES, handle_meta_tool
    from .security import parse_args, validate_tool_call

    cfg = config or {}
    cp_id = None
    if snapshot is not None:
        try:
            cp_id = snapshot(tool_calls, base_dir)
        except Exception as exc:
            logger.debug("检查点快照失败: %s", exc)

    results = []
    for tc in tool_calls:
        if cancel_event is not None and cancel_event.is_set():
            break
        fn = tc.get("function") or {}
        name = fn.get("name", "") or ""
        tool_id = tc.get("id", "")
        started = time.time()

        allowed, reason, risk = validate_tool_call(tc, base_dir, cfg)
        if not allowed:
            if audit is not None:
                audit.log("blocked", tool=name, risk=risk, reason=reason)
            results.append({
                "call": tc, "tool_id": tool_id, "result": "",
                "error": f"🛡 安全策略阻止: {reason}",
                "risk": risk, "blocked": True,
            })
            continue

        if name in META_TOOL_NAMES:
            try:
                args = parse_args(fn.get("arguments", "{}")) or {}
                result = handle_meta_tool(name, args, base_dir, cfg)
                error = ""
            except Exception as exc:
                logger.warning("元工具执行失败: %s", exc)
                result, error = "", f"元工具 {name} 执行失败: {exc}"
        else:
            try:
                tool_id, result, error = execute_tool(tc, base_dir=base_dir)
            except Exception as exc:
                logger.warning("工具执行异常: %s", exc)
                result, error = "", f"工具执行异常: {exc}"

        if audit is not None:
            audit.log("tool_result", tool=name, risk=risk,
                      ok=not bool(error), elapsed_ms=int(
                          (time.time() - started) * 1000),
                      result_chars=len(result or ""),
                      error=(error or "")[:300])
        results.append({
            "call": tc,
            "tool_id": tool_id,
            "result": result or "",
            "error": error or "",
            "risk": risk,
            "blocked": False,
        })
    return {
        "results": results,
        "checkpoint": cp_id,
        "cancelled": bool(cancel_event and cancel_event.is_set()),
    }


# ── 自动验证（后台）────────────────────────────────────────────────────

def changed_files_from_calls(tool_calls, base_dir, include_shell=False):
    """从工具调用参数里取出受影响的文件路径。

    ``include_shell=True`` 时连 run_command 也解析：弱模型常拿它写文件
    （python -c "open(...,'w')"），不解析就等于这批改动对内联 diff 隐形。
    """
    from .approval_card import affected_files, parse_tool_args, SHELL_TOOLS

    paths = []
    for tc in tool_calls or []:
        fn = tc.get("function", {}) or {}
        name = fn.get("name", "")
        if name not in WRITE_TOOLS and not (include_shell
                                            and name in SHELL_TOOLS):
            continue
        args = parse_tool_args(fn.get("arguments", "{}"))
        for path in affected_files(name, args, base_dir):
            if path and path not in paths:
                paths.append(path)
    return paths


def build_verify_command(files, config):
    """构造验证命令；返回 None 表示当前改动没有可自动验证的文件。"""
    custom = (config or {}).get("verify_command", "").strip()
    py_files = [p for p in files if p.lower().endswith(".py")]
    if custom:
        cmd = custom.replace("{files}", " ".join(f'"{p}"' for p in files))
        return cmd
    if not py_files:
        return None
    py = sys.executable or "python"
    return f'"{py}" -m py_compile ' + " ".join(f'"{p}"' for p in py_files[:20])


def run_verification(files, config, base_dir):
    """运行验证命令并返回结构化结果（在后台线程调用）。"""
    cmd = build_verify_command(files, config)
    if not cmd:
        return {"skipped": True, "ok": True, "output": "", "command": ""}
    try:
        proc = subprocess.run(
            cmd, shell=True, cwd=base_dir or os.getcwd(),
            capture_output=True, encoding="utf-8", errors="replace",
            timeout=int((config or {}).get("verify_timeout", 30)),
            creationflags=(subprocess.CREATE_NO_WINDOW
                           if sys.platform == "win32" else 0),
        )
        output = ((proc.stdout or "") + (proc.stderr or "")).strip()
        return {
            "skipped": False,
            "ok": proc.returncode == 0,
            "returncode": proc.returncode,
            "output": output[:2500],
            "command": cmd,
        }
    except subprocess.TimeoutExpired:
        return {"skipped": False, "ok": False,
                "output": "验证命令超时", "command": cmd}
    except Exception as exc:
        return {"skipped": False, "ok": False,
                "output": f"验证执行失败: {exc}", "command": cmd}


def new_cancel_event():
    return threading.Event()
