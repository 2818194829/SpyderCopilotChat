# -*- coding: utf-8 -*-
"""
并行会话管理器 + 对话压缩

对标 GitHub Copilot Chat 的:
  - 并行 Agent 会话 (Multiple sessions)
  - compactConversation (对话压缩)
  - runSubagent (独立子 Agent 会话启动)
"""

import json
import logging
import os
import time
import uuid
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

CONFIG_DIR = os.path.join(os.path.expanduser("~"), ".spyder_copilot_chat")
SESSIONS_DIR = os.path.join(CONFIG_DIR, "sessions")

os.makedirs(SESSIONS_DIR, exist_ok=True)


# ══════════════════════════════════════════════════════════════════════
# 并行会话管理器
# ══════════════════════════════════════════════════════════════════════

class ParallelSessionManager:
    """管理多个并行的 Agent 会话

    对标 VS Code 的 Session Management: 可同时运行多个 Agent 会话，
    在 Session 面板中查看状态、切换、恢复。
    """

    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._sessions = {}
            cls._instance._active_id = None
        return cls._instance

    def create_session(self, title=None, system_prompt=""):
        """创建新会话"""
        sid = str(uuid.uuid4())[:8]
        session = {
            "id": sid,
            "title": title or f"会话 {sid}",
            "created": datetime.now().isoformat(),
            "updated": datetime.now().isoformat(),
            "messages": [],
            "system_prompt": system_prompt,
            "status": "idle",  # idle / running / error / done
            "tool_rounds": 0,
        }
        self._sessions[sid] = session
        self.save_session(sid)
        return session

    def get_session(self, sid):
        """获取会话"""
        return self._sessions.get(sid)

    def get_active(self):
        """获取当前活动会话"""
        return self._sessions.get(self._active_id)

    def set_active(self, sid):
        """设置活动会话"""
        self._active_id = sid

    def add_message(self, sid, message):
        """向会话添加消息"""
        s = self._sessions.get(sid)
        if s:
            s["messages"].append(message)
            s["updated"] = datetime.now().isoformat()
            self.save_session(sid)

    def update_status(self, sid, status):
        """更新会话状态"""
        s = self._sessions.get(sid)
        if s:
            s["status"] = status
            s["updated"] = datetime.now().isoformat()

    def list_sessions(self):
        """列出所有会话"""
        results = []
        for sid, s in self._sessions.items():
            results.append({
                "id": sid,
                "title": s["title"],
                "created": s["created"],
                "status": s["status"],
                "message_count": len(s["messages"]),
                "tool_rounds": s["tool_rounds"],
            })
        # 按创建时间倒序
        results.sort(key=lambda x: x["created"], reverse=True)
        return results

    def delete_session(self, sid):
        """删除会话"""
        if sid in self._sessions:
            del self._sessions[sid]
        # 删除磁盘文件
        fpath = os.path.join(SESSIONS_DIR, f"{sid}.json")
        if os.path.exists(fpath):
            os.remove(fpath)
        if self._active_id == sid:
            self._active_id = None

    def rename_session(self, sid, new_title):
        """重命名会话"""
        s = self._sessions.get(sid)
        if s:
            s["title"] = new_title
            self.save_session(sid)

    def save_session(self, sid):
        """保存会话到磁盘"""
        s = self._sessions.get(sid)
        if s:
            fpath = os.path.join(SESSIONS_DIR, f"{sid}.json")
            with open(fpath, "w", encoding="utf-8") as f:
                json.dump(s, f, indent=2, ensure_ascii=False, default=str)

    def load_sessions(self):
        """从磁盘加载所有会话"""
        if not os.path.isdir(SESSIONS_DIR):
            return
        for fname in os.listdir(SESSIONS_DIR):
            if not fname.endswith(".json"):
                continue
            try:
                with open(os.path.join(SESSIONS_DIR, fname), "r", encoding="utf-8") as f:
                    s = json.load(f)
                self._sessions[s["id"]] = s
            except Exception:
                continue

    def compact_session(self, sid):
        """压缩会话 — 保留系统提示 + 最后 N 轮对话"""
        s = self._sessions.get(sid)
        if not s:
            return "会话不存在"

        messages = s["messages"]
        if len(messages) <= 10:
            return "消息较少，无需压缩"

        # 保留: 前2条消息 + 工具调用相关的轮次 + 最近5对 user/assistant
        preserved = []
        # 前2条
        preserved.extend(messages[:2])

        # 保留所有 tool_calls 相关轮次
        tool_rounds = []
        current_round = []
        for m in messages:
            if m["role"] == "assistant" and m.get("tool_calls"):
                if current_round:
                    tool_rounds.append(current_round)
                current_round = [m]
            elif m["role"] == "tool":
                current_round.append(m)
            elif current_round:
                tool_rounds.append(current_round)
                current_round = []
        if current_round:
            tool_rounds.append(current_round)

        # 保留最后 3 轮工具调用
        for tr in tool_rounds[-3:]:
            preserved.extend(tr)

        # 保留最后 5 对非工具的 user/assistant
        recent = []
        for m in reversed(messages):
            if m["role"] in ("user", "assistant") and not m.get("tool_calls"):
                recent.append(m)
                if len(recent) >= 10:
                    break
        preserved.extend(reversed(recent))

        # 去重
        seen = set()
        deduped = []
        for m in preserved:
            key = (m["role"], str(m.get("content", ""))[:50])
            if key not in seen:
                seen.add(key)
                deduped.append(m)

        # 添加压缩摘要
        summary = {
            "role": "system",
            "content": (f"[对话已压缩] 原始消息数: {len(messages)}，"
                        f"压缩后: {len(deduped)}。"
                        f"保留了关键的工具调用上下文和最近的对话。")
        }
        deduped.insert(0, summary)

        s["messages"] = deduped
        s["updated"] = datetime.now().isoformat()
        self.save_session(sid)

        return (f"✅ 会话已压缩\n"
                f"   {len(messages)} 条消息 → {len(deduped)} 条\n"
                f"   保留了工具调用上下文和最近对话")

    def get_session_summary(self):
        """获取所有会话的摘要（用于系统提示词注入）"""
        sessions = self.list_sessions()
        if not sessions:
            return ""

        lines = ["## 当前会话\n"]
        active = self._sessions.get(self._active_id)
        if active:
            lines.append(f"- **活动会话**: {active['title']} ({active['id']})")
            lines.append(f"  消息数: {len(active['messages'])}, 状态: {active['status']}")

        other = [s for s in sessions if s["id"] != self._active_id]
        if other:
            lines.append(f"\n其他会话 ({len(other)} 个):")
            for s in other[:5]:
                lines.append(f"  - {s['title']} — {s['message_count']} 条消息")

        return "\n".join(lines)


# ══════════════════════════════════════════════════════════════════════
# 全局单例
# ══════════════════════════════════════════════════════════════════════

def get_session_manager():
    """获取全局会话管理器"""
    mgr = ParallelSessionManager()
    mgr.load_sessions()
    return mgr
