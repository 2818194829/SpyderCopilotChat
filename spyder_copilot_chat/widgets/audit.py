# -*- coding: utf-8 -*-
"""
结构化审计日志 — v0.5.6 / P1

按 JSONL 逐行记录一次任务的完整轨迹：
  task_start / request / tool_requested / approval_requested /
  approval_decision / blocked / tool_result / verification /
  usage / redirect / cancelled / task_done / error

设计要点：
  * 写入在独立守护线程 + 队列里完成，GUI 线程零阻塞
  * 自动脱敏 API Key / token / password；长文本截断
  * 每天一个文件 ~/.spyder_copilot_chat/audit/YYYY-MM-DD.jsonl
  * 纯 Python 核心（AuditLog），Qt 查看器在 audit_viewer.py
"""

import json
import logging
import os
import queue
import threading
import time
import uuid
from datetime import datetime

logger = logging.getLogger(__name__)

CONFIG_DIR = os.path.join(os.path.expanduser("~"), ".spyder_copilot_chat")
AUDIT_DIR = os.path.join(CONFIG_DIR, "audit")

_SENSITIVE = ("api_key", "apikey", "authorization", "password", "secret",
              "access_token", "refresh_token", "github_token", "credential")
_MAX_STR = 2000


def _scrub(value, key=""):
    """递归脱敏 + 截断。"""
    if isinstance(value, dict):
        return {k: _scrub(v, str(k).lower()) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_scrub(v, key) for v in value[:50]]
    if any(s in (key or "").lower() for s in _SENSITIVE):
        return "***"
    if isinstance(value, str):
        return value[:_MAX_STR]
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return str(value)[:_MAX_STR]


class AuditLog:
    """异步 JSONL 审计日志。"""

    def __init__(self, root=None, enabled=True):
        self.dir = root or AUDIT_DIR
        self.enabled = bool(enabled)
        self.session = uuid.uuid4().hex[:10]
        self._queue = queue.Queue()
        self._closed = False
        self._lock = threading.Lock()
        self._thread = threading.Thread(
            target=self._run, name="copilot-audit", daemon=True)
        self._thread.start()

    def configure(self, enabled):
        self.enabled = bool(enabled)

    def log(self, event, **fields):
        if not self.enabled or self._closed:
            return
        record = {
            "ts": datetime.now().isoformat(timespec="milliseconds"),
            "epoch": round(time.time(), 3),
            "session": self.session,
            "event": str(event),
        }
        record.update(_scrub(fields))
        try:
            self._queue.put_nowait(record)
        except Exception:
            pass

    def _run(self):
        while True:
            item = self._queue.get()
            try:
                if item is None:
                    break
                self._write(item)
            except Exception as exc:
                logger.debug("审计日志写入失败: %s", exc)
            finally:
                self._queue.task_done()

    def _write(self, record):
        try:
            os.makedirs(self.dir, exist_ok=True)
        except OSError:
            return
        day = (record.get("ts") or "")[:10] or datetime.now().strftime("%Y-%m-%d")
        path = os.path.join(self.dir, day + ".jsonl")
        line = json.dumps(record, ensure_ascii=False, default=str)
        with self._lock:
            with open(path, "a", encoding="utf-8") as f:
                f.write(line + "\n")

    def flush(self, timeout=2.0):
        """等待队列排空（测试 / 导出前调用）。"""
        try:
            self._queue.join()
        except Exception:
            pass

    def close(self):
        if self._closed:
            return
        self.flush()
        self._closed = True
        self._queue.put(None)

    def files(self):
        try:
            return sorted(
                os.path.join(self.dir, f)
                for f in os.listdir(self.dir) if f.endswith(".jsonl"))
        except OSError:
            return []

    def read_events(self, limit=200, date=None):
        """读取最新 limit 条事件（按时间升序返回）。"""
        paths = ([os.path.join(self.dir, date + ".jsonl")]
                 if date else self.files())
        events = []
        for path in paths:
            if not os.path.isfile(path):
                continue
            try:
                with open(path, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            events.append(json.loads(line))
                        except ValueError:
                            continue
            except OSError:
                continue
        return events[-max(1, int(limit)):]

    def export_csv(self, path, limit=10000):
        """导出为 CSV（时间 / 会话 / 事件 / 详情 JSON）。"""
        import csv
        events = self.read_events(limit=limit)
        with open(path, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f)
            w.writerow(["ts", "session", "event", "detail"])
            for e in events:
                detail = {k: v for k, v in e.items()
                          if k not in ("ts", "epoch", "session", "event")}
                w.writerow([e.get("ts", ""), e.get("session", ""),
                            e.get("event", ""),
                            json.dumps(detail, ensure_ascii=False)])
        return len(events)


_GLOBAL = None
_GLOBAL_LOCK = threading.Lock()


def get_audit_log():
    """进程级单例。"""
    global _GLOBAL
    with _GLOBAL_LOCK:
        if _GLOBAL is None:
            _GLOBAL = AuditLog()
        return _GLOBAL


def configure_audit(enabled=True):
    get_audit_log().configure(enabled)
    return get_audit_log()


def audit_dir():
    return AUDIT_DIR
