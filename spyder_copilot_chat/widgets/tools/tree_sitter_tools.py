# -*- coding: utf-8 -*-
"""
Tree-sitter AST 代码理解工具

对标 GitHub Copilot Chat 的 tree-sitter WASM 代码解析能力。
可选依赖，自动降级到正则方案。
"""

import logging
import os
import re

logger = logging.getLogger(__name__)

# ── 检测 tree-sitter 是否可用 ──

_HAS_TREE_SITTER = False
try:
    import tree_sitter
    import tree_sitter_python
    _HAS_TREE_SITTER = True
except ImportError:
    pass


class TreeSitterManager:
    """Tree-sitter 解析器管理器（单例，懒加载）"""
    _instance = None
    _parsers = {}
    _initialized = False

    @classmethod
    def get_instance(cls):
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def __init__(self):
        self._parsers = {}

    def initialize(self):
        if self._initialized:
            return _HAS_TREE_SITTER
        self._initialized = True
        if not _HAS_TREE_SITTER:
            return False
        try:
            from tree_sitter import Language, Parser
            self._python_parser = Parser()
            self._python_parser.set_language(tree_sitter_python.language())
            self._parsers["python"] = self._python_parser
            return True
        except Exception as e:
            logger.debug("Tree-sitter init failed: %s", e)
            return False

    def has_parser(self, language="python"):
        return language in self._parsers

    def get_parser(self, language="python"):
        return self._parsers.get(language)

    def parse_file(self, filepath):
        """解析文件返回 AST 树"""
        if not self.initialize():
            return None
        ext = os.path.splitext(filepath)[1].lower()
        lang_map = {".py": "python", ".js": "javascript", ".ts": "typescript"}
        lang = lang_map.get(ext, "python")
        if lang not in self._parsers:
            return None
        try:
            with open(filepath, "r", encoding="utf-8", errors="replace") as f:
                content = f.read()
            tree = self._parsers[lang].parse(bytes(content, "utf-8"))
            return tree
        except Exception as e:
            logger.debug("Parse failed for %s: %s", filepath, e)
            return None


# ── 提取符号定义（支持 tree-sitter 和正则两种模式）─

# 正则模式的符号提取（降级方案）
_PY_SYMBOL_RE = re.compile(
    r'^(?:async\s+)?(?:def|class)\s+([a-zA-Z_]\w*)\s*',
    re.MULTILINE
)
_JS_SYMBOL_RE = re.compile(
    r'^(?:export\s+)?(?:async\s+)?(?:function\s+\*?\s*|class\s+)'
    r'([a-zA-Z_$]\w*)\s*',
    re.MULTILINE
)
_TS_SYMBOL_RE = re.compile(
    r'^(?:export\s+)?(?:interface|type|enum|class|function|abstract\s+class)\s+'
    r'([a-zA-Z_$]\w*)',
    re.MULTILINE
)

_SYMBOL_RE_MAP = {
    ".py": _PY_SYMBOL_RE,
    ".js": _JS_SYMBOL_RE,
    ".jsx": _JS_SYMBOL_RE,
    ".ts": _TS_SYMBOL_RE,
    ".tsx": _TS_SYMBOL_RE,
    ".mjs": _JS_SYMBOL_RE,
    ".cjs": _JS_SYMBOL_RE,
}


def get_symbol_definitions(filepath):
    """获取文件中所有符号定义

    优先使用 tree-sitter，降级到正则。
    Returns: [{"name", "type", "line", "filepath"}, ...]
    """
    if not os.path.isfile(filepath):
        return []

    # 尝试 tree-sitter
    ts = TreeSitterManager.get_instance()
    tree = ts.parse_file(filepath) if _HAS_TREE_SITTER else None
    if tree:
        return _extract_symbols_ts(tree, filepath)

    # 降级到正则
    return _extract_symbols_regex(filepath)


def _extract_symbols_ts(tree, filepath):
    """从 tree-sitter AST 提取符号"""
    symbols = []
    try:
        root = tree.root_node
        _walk_ts_node(root, filepath, symbols)
    except Exception as e:
        logger.debug("TS symbol extraction failed: %s", e)
    return symbols


def _walk_ts_node(node, filepath, symbols, depth=0):
    """递归遍历 tree-sitter AST 节点"""
    if depth > 50:
        return
    node_type = node.type
    if node_type in ("class_definition", "function_definition",
                     "method_definition", "async_function_definition"):
        name_node = node.child_by_field_name("name")
        if name_node:
            symbols.append({
                "name": name_node.text.decode("utf-8") if isinstance(name_node.text, bytes) else str(name_node.text),
                "type": "class" if "class" in node_type else "function",
                "line": node.start_point[0] + 1,
                "filepath": filepath,
            })
    elif node_type in ("decorated_definition",):
        # 装饰器：递归到子节点找定义
        for child in node.children:
            _walk_ts_node(child, filepath, symbols, depth + 1)
    elif node_type == "assignment" and _HAS_TREE_SITTER:
        # 顶级赋值（如 CONSTANT = ...）
        try:
            left = node.child_by_field_name("left")
            if left and left.type == "identifier":
                symbols.append({
                    "name": left.text.decode("utf-8"),
                    "type": "variable",
                    "line": node.start_point[0] + 1,
                    "filepath": filepath,
                })
        except Exception:
            pass

    for child in node.children:
        _walk_ts_node(child, filepath, symbols, depth + 1)


def _extract_symbols_regex(filepath):
    """使用正则提取符号（降级方案）"""
    symbols = []
    ext = os.path.splitext(filepath)[1].lower()
    pattern = _SYMBOL_RE_MAP.get(ext)
    if not pattern:
        return symbols

    try:
        with open(filepath, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
        for m in pattern.finditer(content):
            line_num = content[:m.start()].count("\n") + 1
            name = m.group(1)
            # 判断类型
            line = content[:m.end()].rsplit("\n", 1)[-1] if "\n" in content[:m.end()] else content[:m.end()]
            sym_type = "class" if "class" in line else "function"
            symbols.append({
                "name": name,
                "type": sym_type,
                "line": line_num,
                "filepath": filepath,
            })
    except Exception as e:
        logger.debug("Regex symbol extraction failed for %s: %s", filepath, e)

    return symbols


def get_code_outline(filepath, max_depth=3):
    """获取代码大纲（层次化结构）

    Returns: [{"name", "type", "line", "children": [...]}, ...]
    """
    symbols = get_symbol_definitions(filepath)
    # 构建层次结构（基于缩进/行号推断缩进）
    outline = []
    stack = [{"children": outline, "depth": -1}]
    for sym in symbols:
        # 根据行号推断层级（简单方案：连续行号差）
        depth = 0  # 默认顶层
        try:
            with open(filepath, "r", encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
            if sym["line"] <= len(lines):
                line_text = lines[sym["line"] - 1]
                depth = len(line_text) - len(line_text.lstrip())
                depth = min(depth // 4, max_depth)
        except Exception:
            pass

        entry = {"name": sym["name"], "type": sym["type"],
                 "line": sym["line"], "children": []}

        # 找到正确的父节点插入
        while stack and stack[-1]["depth"] >= depth:
            stack.pop()
        if stack:
            stack[-1]["children"].append(entry)
        else:
            outline.append(entry)
        stack.append({"children": entry["children"], "depth": depth})

    return outline


def find_related_symbols(query, base_dir, max_results=20):
    """跨文件搜索符号

    在项目文件中搜索匹配的符号定义。
    优先 tree-sitter，降级正则。
    """
    results = []
    search_exts = {".py", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs"}

    for root, dirs, files in os.walk(base_dir):
        # 跳过常见排除目录
        dirs[:] = [d for d in dirs if not d.startswith(".")
                   and d not in ("node_modules", "__pycache__", "venv",
                                 ".venv", "env", "dist", "build")]
        for f in files:
            ext = os.path.splitext(f)[1].lower()
            if ext not in search_exts:
                continue
            filepath = os.path.join(root, f)
            try:
                symbols = get_symbol_definitions(filepath)
                for sym in symbols:
                    if query.lower() in sym["name"].lower():
                        results.append(sym)
                        if len(results) >= max_results:
                            return results
            except Exception:
                continue

    return results[:max_results]


def get_call_hierarchy(filepath, line, column=0):
    """获取函数调用层次

    简单实现：用正则查找调用者和被调用函数。
    """
    if not os.path.isfile(filepath):
        return {"calls": [], "called_by": []}

    try:
        with open(filepath, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
        lines = content.split("\n")
    except Exception as e:
        return {"calls": [], "called_by": [], "error": str(e)}

    # 获取当前行的函数名
    if line <= 0 or line > len(lines):
        return {"calls": [], "called_by": []}

    # 向上查找最近的函数定义
    current_func = None
    for i in range(line - 1, -1, -1):
        m = _PY_SYMBOL_RE.search(lines[i])
        if m:
            current_func = m.group(1)
            break

    if not current_func:
        return {"calls": [], "called_by": []}

    # 查找此函数体内调用的其他函数
    calls = set()
    in_func = False
    func_indent = None
    for i, l in enumerate(lines):
        if current_func in l and re.match(r'^\s*(?:async\s+)?def\s+', l):
            in_func = True
            func_indent = len(l) - len(l.lstrip())
            continue
        if in_func:
            stripped = l.strip()
            indent = len(l) - len(l.lstrip())
            if stripped and indent <= func_indent:
                in_func = False
            elif stripped:
                # 查找函数调用模式
                for call_m in re.finditer(r'([a-zA-Z_]\w*)\s*\(', stripped):
                    call_name = call_m.group(1)
                    if call_name not in ("if", "for", "while", "with", "not",
                                         "and", "or", "in", "is", "assert",
                                         "return", "yield", "raise", "del",
                                         "print", "len", "range", "type",
                                         "isinstance", "hasattr", "getattr",
                                         "setattr", "str", "int", "float",
                                         "list", "dict", "set", "tuple",
                                         "open", "super", "self", "cls"):
                        calls.add(call_name)

    # 查找调用此函数的地方
    called_by = set()
    for i, l in enumerate(lines):
        if f"{current_func}(" in l:
            # 不是定义本身
            if not re.match(r'^\s*(?:async\s+)?def\s+', l):
                # 向上找所属函数
                for j in range(i - 1, -1, -1):
                    m = _PY_SYMBOL_RE.search(lines[j])
                    if m:
                        called_by.add(m.group(1))
                        break

    return {
        "name": current_func,
        "calls": sorted(calls),
        "called_by": sorted(called_by),
    }
