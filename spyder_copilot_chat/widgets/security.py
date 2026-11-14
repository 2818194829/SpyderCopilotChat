# -*- coding: utf-8 -*-
"""
风险分级权限与工作区沙箱 — v0.5.6 / P1

三层风险：
  read   — 只读、无副作用：默认放行
  write  — 可逆写 / 本地命令：批量确认（现有审批卡片）
  danger — 删除、强推、外部系统、危险命令：必须逐项显式批准，
           且「全部批准」按钮不会替你批准它们

沙箱：
  * 所有文件路径必须落在项目根 / 显式允许的额外根 / 插件配置目录内
  * apply_patch / 批量替换里的路径同样校验
  * 危险命令（rm -rf、del /S、git push --force、管道执行远端脚本…）默认拦截

注意：即使配置了 auto_execute / approval_policy="none"，
danger 级动作仍然强制审批 —— 这是不可逆操作的最小安全边界。

本模块是纯 Python，可在后台线程安全调用。
"""

import json
import logging
import os
import re

logger = logging.getLogger(__name__)

CONFIG_DIR = os.path.join(os.path.expanduser("~"), ".spyder_copilot_chat")

READ = "read"
WRITE = "write"
DANGER = "danger"

# 明确的写操作（可逆，走普通审批）
WRITE_TOOL_NAMES = {
    "create_file", "edit_file", "rename_file", "create_directory",
    "apply_patch", "insert_edit", "multi_replace_string",
    "create_notebook", "add_notebook_cell", "edit_notebook_cell",
    "notebook_run_cell", "git_add", "git_commit", "git_create_branch",
    "resolve_conflict", "memory_save", "run_command", "fetch_webpage",
    "get_errors", "write_to_file", "replace_in_file",
}

# 明确的危险操作（不可逆 / 影响外部系统 / 委派执行）
DANGER_TOOL_NAMES = {
    "delete_file", "delete_notebook_cell",
    "run_fleet", "run_subagent", "execution_subagent", "search_subagent",
    "switch_session", "compact_conversation",
}

# 路径型参数键
_PATH_KEYS = (
    "filepath", "file_path", "path", "file", "filename",
    "old_path", "new_path", "source", "destination", "dest",
    "target", "directory", "dir", "folder", "notebook_path",
)
# 值可能是「路径列表」的键
_PATH_LIST_KEYS = ("files", "paths", "filepaths", "replacements")

_PATCH_PATH_RE = re.compile(r"^(?:\+\+\+|---)\s+(?:[ab]/)?(.+)$", re.MULTILINE)

# 危险命令模式（大小写不敏感）
_DANGEROUS_COMMANDS = (
    (r"\brm\s+-[rRfF]{1,3}\b.*(?:/|\*|\.\.|\\)", "递归/强制删除"),
    (r"\bdel\s+/[sSfFqQ]\b", "递归删除"),
    (r"\brmdir\s+/[sS]\b", "递归删除目录"),
    (r"\b(format|mkfs)(\.\w+)?\s+[a-zA-Z]:", "格式化磁盘"),
    (r"\b(shutdown|reboot|halt)\b", "关机/重启"),
    (r"\bgit\s+push\b.*(?:--force|\s-f\b)", "强制推送"),
    (r"\bgit\s+reset\s+--hard\b", "丢弃本地改动"),
    (r"\bgit\s+clean\s+-[fFdDxX]", "清除未跟踪文件"),
    (r"\b(?:curl|wget)\b[^\n|]*\|\s*(?:sh|bash|zsh|powershell|pwsh)\b",
     "管道执行远端脚本"),
    (r"\breg\s+delete\b|\bdiskpart\b|\bvssadmin\b", "系统级破坏性命令"),
    (r"\btaskkill\s+/[fF]\b", "强制结束进程"),
    (r":\(\)\s*\{", "fork bomb"),
    (r"\bInvoke-Expression\b|\bFromBase64String\b", "混淆脚本执行"),
    (r"\bchmod\s+-R\s+777\s+/", "全盘放开权限"),
    (r"\bdrop\s+(?:table|database)\b", "删除数据库对象"),
)
_DANGEROUS_RE = [(re.compile(p, re.IGNORECASE), why)
                 for p, why in _DANGEROUS_COMMANDS]


def parse_args(raw):
    """与 agentic_actions 一致地宽松解析参数；失败返回 None。"""
    if isinstance(raw, dict):
        return raw
    obj = None
    try:
        obj = json.loads(raw or "{}")
    except (TypeError, ValueError):
        try:
            from .agentic_actions import _parse_robust_json
            obj = _parse_robust_json(raw or "{}", "")
        except Exception:
            return None
    return obj if isinstance(obj, dict) else None


def dangerous_command(command):
    """返回命中原因字符串；未命中返回 ''。"""
    if not isinstance(command, str) or not command.strip():
        return ""
    for pattern, why in _DANGEROUS_RE:
        if pattern.search(command):
            return why
    return ""


def classify_tool(name, args=None):
    """返回 read / write / danger。未知工具按 write 保守处理。"""
    if not name:
        return DANGER
    if name.startswith("mcp_"):
        return DANGER
    if name in DANGER_TOOL_NAMES:
        return DANGER
    if name == "run_command":
        cmd = (args or {}).get("command", "")
        return DANGER if dangerous_command(cmd) else WRITE
    if name in WRITE_TOOL_NAMES:
        return WRITE
    # 元工具（只读）
    if name in {"load_skill", "list_skills", "list_project_rules",
                "search_repo_map"}:
        return READ
    # 已知只读工具前缀/集合
    if name.startswith(("read_", "get_", "list_", "find_", "search_",
                        "view_", "detect_", "review_", "notebook_get")):
        return READ
    if name in {"manage_todo_list", "get_current_file", "memory_recall",
                "text_search", "codebase_search", "list_directory",
                "git_log", "get_changed_files", "get_code_outline",
                "get_symbol_definitions", "get_call_hierarchy",
                "get_symbol_at_position", "find_related_symbols",
                "search_workspace_symbols"}:
        return READ
    return WRITE


def approve_all_allowed(risk):
    """「全部批准」是否覆盖该风险级别。"""
    return risk != DANGER


def approval_required(name, policy="writes", risk=None, args=None):
    """按策略判断是否需要审批。

    policy:
      "risk"  — 只读免批，写操作与危险操作审批（推荐）
      "writes"— 同上，保持旧命名
      "all"   — 全部审批
      "none"  — 只读/写自动执行，但 danger 仍强制审批
    """
    risk = risk or classify_tool(name, args)
    if policy == "all":
        return True
    if policy == "none":
        return risk == DANGER
    if risk == READ:
        return False
    return True


def _split_roots(value):
    if not value:
        return []
    if isinstance(value, (list, tuple)):
        return [str(v).strip() for v in value if str(v).strip()]
    return [v.strip() for v in re.split(r"[;\n]", str(value)) if v.strip()]


def allowed_roots(base_dir, config=None):
    config = config or {}
    roots = [os.path.abspath(base_dir or os.getcwd()), CONFIG_DIR]
    roots.extend(_split_roots(config.get("extra_allowed_roots", "")))
    out = []
    for r in roots:
        try:
            out.append(os.path.realpath(r))
        except OSError:
            continue
    return out


def _path_is_allowed(path, roots):
    if not path or not isinstance(path, str):
        return True
    # URL / 非路径值不校验
    low = path.lower()
    if low.startswith(("http://", "https://", "ftp://", "data:", "git@")):
        return True
    abs_path = path if os.path.isabs(path) else os.path.join(
        os.path.abspath(roots[0]), path)
    try:
        real = os.path.realpath(abs_path)
        for root in roots:
            if os.path.commonpath([real, root]) == root:
                return True
    except (ValueError, OSError):
        return False
    return False


def _collect_paths(name, args, base_dir):
    """收集工具调用里涉及的所有文件路径（用于边界校验）。"""
    paths = []
    for key in _PATH_KEYS:
        value = args.get(key)
        if isinstance(value, str) and value.strip():
            paths.append(value.strip())
    for key in _PATH_LIST_KEYS:
        value = args.get(key)
        if isinstance(value, list):
            for item in value:
                if isinstance(item, str) and item.strip():
                    paths.append(item.strip())
                elif isinstance(item, dict):
                    for k in ("filepath", "path", "file"):
                        if isinstance(item.get(k), str) and item[k].strip():
                            paths.append(item[k].strip())
    if name == "apply_patch":
        patch = args.get("patch") or ""
        for raw in _PATCH_PATH_RE.findall(patch):
            raw = raw.strip()
            if raw and raw != "/dev/null" and not raw.startswith("/dev"):
                paths.append(raw)
    return paths


def validate_tool_call(tool_call, base_dir, config=None):
    """执行前校验。返回 (allowed, reason, risk)。

    config 可含：
      sandbox_enabled (默认 True)
      allow_dangerous_commands (默认 False)
      extra_allowed_roots (默认 "")
    """
    config = config or {}
    fn = (tool_call or {}).get("function") or {}
    name = fn.get("name", "") or ""
    args = parse_args(fn.get("arguments", "{}"))
    if args is None:
        return False, "工具参数不是合法 JSON，安全策略已阻止执行", DANGER
    risk = classify_tool(name, args)

    if not config.get("sandbox_enabled", True):
        return True, "", risk

    roots = allowed_roots(base_dir, config)

    # 1) 危险命令
    if name == "run_command":
        why = dangerous_command(args.get("command", ""))
        if why and not config.get("allow_dangerous_commands", False):
            return False, f"检测到危险命令（{why}），已被沙箱拦截", DANGER

    # 2) 路径边界
    for p in _collect_paths(name, args, base_dir):
        if not _path_is_allowed(p, roots):
            return False, (
                f"路径超出工作区边界: {p}\n"
                "如需访问，请在「设置 → 治理」里把该目录加入"
                "「额外允许的根目录」，或关闭工作区沙箱。"), DANGER

    return True, "", risk
