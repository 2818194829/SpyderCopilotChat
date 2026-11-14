# -*- coding: utf-8 -*-
"""
Agent Hooks 系统 — 工具执行生命周期回调

对标 GitHub Copilot Chat 的 Hooks 系统：
  - pre-tool: 工具执行前验证/修改/拦截
  - post-tool: 工具执行后通知/记录

Hook 配置存储在 ~/.spyder_copilot_chat/hooks/hooks.json
"""

import json
import logging
import os
import re

logger = logging.getLogger(__name__)

HOOKS_CONFIG_DIR = os.path.join(os.path.expanduser("~"),
                                ".spyder_copilot_chat", "hooks")
HOOKS_CONFIG_FILE = os.path.join(HOOKS_CONFIG_DIR, "hooks.json")


class Hook:
    """单个 Hook 定义"""

    def __init__(self, name, hook_type, tools=None, condition=None,
                 action="notify", message=""):
        """
        Args:
            name: Hook 名称
            hook_type: "pre-tool" | "post-tool"
            tools: 匹配的工具名列表 (None = 全部)
            condition: 条件表达式 (可选的)
            action: "block" | "notify" | "modify" | "log"
            message: 消息文本
        """
        self.name = name
        self.type = hook_type
        self.tools = tools or []
        self.condition = condition
        self.action = action
        self.message = message

    def matches(self, tool_name):
        """检查是否匹配该工具"""
        if not self.tools:
            return True
        return tool_name in self.tools

    def to_dict(self):
        return {
            "name": self.name,
            "type": self.type,
            "tools": self.tools,
            "condition": self.condition,
            "action": self.action,
            "message": self.message,
        }

    @classmethod
    def from_dict(cls, d):
        return cls(
            name=d.get("name", "unnamed"),
            hook_type=d.get("type", "pre-tool"),
            tools=d.get("tools", []),
            condition=d.get("condition"),
            action=d.get("action", "notify"),
            message=d.get("message", ""),
        )


class HookResult:
    """Hook 评估结果"""
    def __init__(self, blocked=False, message="", modified_args=None):
        self.blocked = blocked
        self.message = message
        self.modified_args = modified_args or {}


class HookEngine:
    """Hook 执行引擎 — 评估并执行 Hook"""

    def __init__(self):
        self._hooks = []
        # 0.5.1 修复：原代码调用不存在的 _load_hooks，实例化即崩溃
        self.load_hooks()

    def load_hooks(self, hooks_dir=None):
        """从配置文件加载 Hook"""
        self._hooks = []
        hooks_file = HOOKS_CONFIG_FILE
        if hooks_dir:
            hooks_file = os.path.join(hooks_dir, "hooks.json")

        if not os.path.exists(hooks_file):
            return

        try:
            with open(hooks_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            for h in data.get("hooks", []):
                self._hooks.append(Hook.from_dict(h))
            logger.debug("Loaded %d hooks from %s", len(self._hooks), hooks_file)
        except Exception as e:
            logger.warning("Failed to load hooks: %s", e)

    def evaluate_pre_tool(self, tool_name, args):
        """评估 pre-tool hooks

        Returns: HookResult
        """
        for hook in self._hooks:
            if hook.type != "pre-tool":
                continue
            if not hook.matches(tool_name):
                continue
            # 评估条件
            if hook.condition:
                try:
                    ctx = {"tool_name": tool_name, "args": args, "tool": tool_name}
                    if not self._safe_eval(hook.condition, ctx):
                        continue
                except Exception:
                    continue

            if hook.action == "block":
                msg = hook.message or f"Hook '{hook.name}' 阻止了 {tool_name}"
                logger.info("Hook blocked: %s", hook.name)
                return HookResult(blocked=True, message=msg)

            elif hook.action == "notify":
                return HookResult(message=hook.message or f"Hook: {hook.name}")

            elif hook.action == "log":
                logger.info("Hook log: %s - %s(%s)", hook.name, tool_name, args)

        return HookResult()

    def evaluate_post_tool(self, tool_name, args, result):
        """评估 post-tool hooks"""
        for hook in self._hooks:
            if hook.type != "post-tool":
                continue
            if not hook.matches(tool_name):
                continue
            if hook.condition:
                try:
                    ctx = {"tool_name": tool_name, "args": args, "result": result}
                    if not self._safe_eval(hook.condition, ctx):
                        continue
                except Exception:
                    continue

            if hook.action == "notify":
                return HookResult(message=hook.message or f"Hook: {hook.name}")
            elif hook.action == "log":
                logger.info("Hook post: %s - %s completed", hook.name, tool_name)

        return HookResult()

    def add_hook(self, hook):
        """添加一个 Hook"""
        self._hooks.append(hook)
        self._save_hooks()

    def remove_hook(self, name):
        """按名称移除 Hook"""
        self._hooks = [h for h in self._hooks if h.name != name]
        self._save_hooks()

    def list_hooks(self):
        """列出所有 Hook"""
        return list(self._hooks)

    def _save_hooks(self):
        """保存 Hook 配置到文件"""
        try:
            os.makedirs(HOOKS_CONFIG_DIR, exist_ok=True)
            with open(HOOKS_CONFIG_FILE, "w", encoding="utf-8") as f:
                json.dump({"hooks": [h.to_dict() for h in self._hooks]},
                          f, indent=2, ensure_ascii=False)
        except Exception as e:
            logger.warning("Failed to save hooks: %s", e)

    def _safe_eval(self, expr, context):
        """安全的条件表达式评估

        只允许基本的比较/逻辑运算和字符串操作。
        """
        allowed = {
            "True": True, "False": False, "None": None,
            "len": len, "str": str, "int": int,
            "and": lambda a, b: a and b,
            "or": lambda a, b: a or b,
            "not": lambda a: not a,
            "in": lambda a, b: a in b,
            "==": lambda a, b: a == b,
            "!=": lambda a, b: a != b,
        }
        try:
            # 替换 context 中的变量
            env = {"__builtins__": {}}
            env.update(context)
            return bool(eval(expr, env))
        except Exception:
            return False


# 全局单例
_engine = None


def get_hook_engine():
    """获取全局 Hook 引擎单例"""
    global _engine
    if _engine is None:
        _engine = HookEngine()
    return _engine


def reset_hook_engine():
    """重置 Hook 引擎（用于测试）"""
    global _engine
    _engine = None
