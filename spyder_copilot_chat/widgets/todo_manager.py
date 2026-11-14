# -*- coding: utf-8 -*-
"""
任务管理（Todo List）

对标 GitHub Copilot Chat 的 manage_todo_list 工具：
  - AI Agent 可以将复杂任务分解为多个步骤
  - 每个步骤有: id, title, status (not-started / in-progress / completed)
  - 同一时间只有一个 in-progress 任务
  - 状态变更通过信号通知 UI 更新
"""

import logging

from qtpy.QtCore import QObject, Signal

logger = logging.getLogger(__name__)

# 预设的 emoji 图标映射
STATUS_ICONS = {
    "not-started": "⬜",
    "in-progress": "🔄",
    "completed": "✅",
    "cancelled": "❌",
}


class TodoManager(QObject):
    """任务管理器 — 单例模式，全局共享"""

    # 信号：todo 列表变更时通知 UI
    todos_changed = Signal(list)

    _instance = None

    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self, parent=None):
        if self._initialized:
            return
        super().__init__(parent)
        self._todos = []
        self._initialized = True

    # ── 公共 API ──────────────────────────────────────────────────

    def get_todos(self):
        """获取当前所有任务"""
        return list(self._todos)

    def set_todos(self, todo_list):
        """批量替换任务列表（AI 调用 manage_todo_list 时使用）

        Parameters
        ----------
        todo_list : list[dict]
            每个元素包含: id (int), title (str), status (str)
        """
        # 验证并清理数据
        valid = []
        for item in todo_list:
            tid = item.get("id")
            title = item.get("title", "")
            status = item.get("status", "not-started")
            if tid is None or not title:
                continue
            if status not in STATUS_ICONS:
                status = "not-started"
            valid.append({
                "id": int(tid),
                "title": str(title),
                "status": str(status),
            })

        self._todos = valid
        self._emit_change()

    def update_todo(self, todo_id, status):
        """更新单个任务状态"""
        for t in self._todos:
            if t["id"] == todo_id:
                t["status"] = status
                self._emit_change()
                return True
        return False

    def clear(self):
        """清空所有任务"""
        self._todos = []
        self._emit_change()

    def get_summary(self):
        """获取任务摘要（用于注入系统提示词）"""
        if not self._todos:
            return ""

        total = len(self._todos)
        done = sum(1 for t in self._todos if t["status"] == "completed")
        in_progress = [t for t in self._todos if t["status"] == "in-progress"]

        lines = ["## 当前任务进度"]
        lines.append(f"总计 {total} 个任务，已完成 {done} 个")

        if in_progress:
            lines.append(f"进行中: {in_progress[0]['title']}")

        # 列出所有任务
        for t in self._todos:
            icon = STATUS_ICONS.get(t["status"], "❓")
            lines.append(f"  {icon} {t['title']}")

        return "\n".join(lines)

    def _emit_change(self):
        """发送变更通知"""
        try:
            self.todos_changed.emit(self._todos)
        except Exception as e:
            logger.debug("Todo signal error: %s", e)


# ── 工具调用处理函数 ──────────────────────────────────────────────

def handle_manage_todo_list(args, base_dir=None):
    """处理 AI 的 manage_todo_list 工具调用

    参数格式（对标 VS Code）:
    {
        "todoList": [
            {"id": 1, "title": "任务描述", "status": "not-started|in-progress|completed"},
            ...
        ]
    }
    """
    todo_list = args.get("todoList", [])

    if not todo_list:
        return "错误: todoList 为空或格式不正确"

    manager = TodoManager()
    manager.set_todos(todo_list)

    # 生成反馈
    total = len(todo_list)
    completed = sum(1 for t in todo_list if t.get("status") == "completed")
    in_progress = [t for t in todo_list if t.get("status") == "in-progress"]

    lines = [f"📋 **任务列表已更新** ({completed}/{total} 完成)"]
    for t in todo_list:
        icon = STATUS_ICONS.get(t.get("status", "not-started"), "❓")
        lines.append(f"  {icon} {t.get('title', '')}")

    if in_progress:
        lines.append(f"\n🔜 下一步: **{in_progress[0].get('title', '')}**")

    return "\n".join(lines)
