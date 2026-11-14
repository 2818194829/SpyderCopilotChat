# -*- coding: utf-8 -*-
"""v0.8.11 回归：DiffBlockMark 必须真继承 Skyder 的 BlockUserData。

用户贴的崩溃栈：

    lsp_mixin.py:639 in _process_code_analysis
        data._selection()
    editor_diff.py:157 in __getattr__
        raise AttributeError(name)
    AttributeError: _selection
      → lsp_mixin.manage_lsp_handle_errors
      → LSPHandleError: Error when processing linting

v0.8.9 只「照抄字段」，基类仍是裸 QTextBlockUserData：字段能读，方法
（_selection）不在 MRO 里，属性查找掉进 __getattr__ 的下划线分支抛
AttributeError，Spyder 把它包成 LSPHandleError，linting 整条链路炸掉。

覆盖：
  1) 结构守卫：DiffBlockMark 是 Spyder BlockUserData 的子类
  2) _selection 在类上真存在，且 selection 为空时返回 None
  3) 复刻 Spyder 的 linting 下划线路径：设好 range 后 _selection()
     必须返回 QTextCursor，绝不抛异常
  4) mark.editor 是真编辑器（_selection 内部要用 document/textCursor）
  5) Spyder 消费点逐个走一遍：is_cell_header / document_cells /
     get_current_warnings / 行号面板 / 折行面板
  6) __getattr__ 语义：公开属性 → None；dunder → AttributeError；
     未知下划线 → 可调用兜底（不能抛，也不能是 None）
"""
import os
import sys
import tempfile

ROOT = (r"C:\Users\Mr_Yang\Documents\deepseek-harness"
        r"\default-workspace\spyder-copilot-chat")
sys.path.insert(0, ROOT)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from qtpy.QtWidgets import QApplication, QPlainTextEdit        # noqa: E402
from qtpy.QtCore import QObject                                # noqa: E402
from qtpy.QtGui import QTextCursor                             # noqa: E402

from spyder_copilot_chat.plugin import CopilotChatPlugin       # noqa: E402
from spyder_copilot_chat.widgets import editor_diff as ED      # noqa: E402
from spyder_copilot_chat.widgets.editor_diff import (          # noqa: E402
    DiffBlockMark, _SpyderBlockUserData,
)

APP = QApplication.instance() or QApplication([])
FAILS = []
PASSES = []


def check(name, cond, extra=""):
    if cond:
        PASSES.append(name)
    else:
        FAILS.append("%s  %s" % (name, extra))


def pump(n=6):
    for _ in range(n):
        APP.processEvents()


# ── 脚手架 ──────────────────────────────────────────────────────
class FakeFinfo(object):
    def __init__(self, editor, path):
        self.filename = path
        self.editor = editor
        self.newly_created = False


class FakeStack(object):
    def __init__(self, pairs):
        self.data = [FakeFinfo(e, p) for p, e in pairs]
        self.current = pairs[0][0] if pairs else ""

    def load(self, path, set_current=False):
        return None

    def set_current_filename(self, path, focus=False):
        self.current = path
        return True

    def get_index_from_filename(self, path):
        target = os.path.normcase(os.path.abspath(path))
        for i, finfo in enumerate(self.data):
            if os.path.normcase(os.path.abspath(finfo.filename)) == target:
                return i
        raise ValueError(path)

    def save(self, index, force=False):
        finfo = self.data[index]
        text = finfo.editor.toPlainText()
        with open(finfo.filename, "w", encoding="utf-8", newline="") as fh:
            fh.write(text)
        finfo.editor.document().setModified(False)
        return True


class FakeEditorPlugin(object):
    def __init__(self, stack):
        self._stack = stack

    def get_current_editorstack(self):
        return self._stack


class FakePlugin(QObject):
    review_files = CopilotChatPlugin.review_files
    _save_editor_file = CopilotChatPlugin._save_editor_file
    _ensure_review_hub = CopilotChatPlugin._ensure_review_hub
    _editor_for_path = CopilotChatPlugin._editor_for_path

    def __init__(self, stack):
        super().__init__()
        self._review_hub = None
        self._ep = FakeEditorPlugin(stack)

    def get_plugin(self, name):
        return self._ep

    def get_widget(self):
        return None


OLD = ("import math\n"
       "\n"
       "\n"
       "def add(a, b):\n"
       "    return a + b\n"
       "\n"
       "\n"
       "def sub(a, b):\n"
       "    return a - b\n")
NEW = ("import math\n"
       "\n"
       "\n"
       "def add(a, b):\n"
       '    """两数相加。"""\n'
       "    return a + b\n"
       "\n"
       "\n"
       "def sub(a, b):\n"
       "    s = a - b\n"
       "    return s\n")


def make_file(tmp, name):
    path = os.path.join(tmp, name)
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write(NEW)
    editor = QPlainTextEdit()
    editor.resize(620, 360)
    editor.setPlainText(OLD)
    editor.document().setModified(False)
    return path, editor


# ── 1) 结构守卫：基类必须是 Spyder 的 BlockUserData ─────────────
check("Spyder 环境里能拿到 BlockUserData", _SpyderBlockUserData is not None)
if _SpyderBlockUserData is not None:
    check("DiffBlockMark 继承的是 Spyder BlockUserData",
          issubclass(DiffBlockMark, _SpyderBlockUserData),
          "MRO: %s" % ([c.__name__ for c in DiffBlockMark.__mro__],))
    check("_selection 在 MRO 里（不是掉进 __getattr__ 才算有）",
          any("_selection" in getattr(k, "__dict__", {})
              for k in DiffBlockMark.__mro__),
          "_selection 只可能来自基类")
    check("ED._MarkBase 就是 Spyder BlockUserData",
          ED._MarkBase is _SpyderBlockUserData)

check("DiffBlockMark 仍是 QTextBlockUserData 的后代（Qt 硬要求）",
      issubclass(DiffBlockMark, ED.QTextBlockUserData))

# ── 2) _selection 存在性 + 空 range 语义 ────────────────────────
TMP = tempfile.mkdtemp(prefix="v091a_")
PATH, EDITOR = make_file(TMP, "lint.py")

mark = DiffBlockMark("add", 1, EDITOR)
check("mark._selection 是可调用对象", callable(getattr(mark, "_selection", None)),
      type(getattr(mark, "_selection", None)))
check("selection 为空时 _selection() 返回 None", mark._selection() is None,
      repr(mark._selection()))
check("mark.editor 就是真编辑器", mark.editor is EDITOR,
      repr(mark.editor))

# ── 3) 复刻 lsp_mixin._process_code_analysis 的下划线路径 ───────
# 源码（Spyder 6.1.8, lsp_mixin.py:621-641）：
#     data = block.userData()
#     data.selection_start = start
#     data.selection_end = end
#     self.highlight_selection("code_analysis_underline",
#                              data._selection(), underline_color=...)
doc = EDITOR.document()
block = doc.findBlockByNumber(2)          # 第 3 行：一个空行
data = DiffBlockMark("del", 2, EDITOR)
block.setUserData(data)
data.selection_start = {"line": 2, "character": 0}
data.selection_end = {"line": 2, "character": 4}

crashed = None
try:
    sel = block.userData()._selection()
except Exception as exc:                  # noqa: BLE001 - 就是要抓崩溃栈
    crashed = "%s: %s" % (type(exc).__name__, exc)
    sel = None
check("linting 下划线路径不再抛 AttributeError", crashed is None, crashed or "")
check("_selection() 返回 QTextCursor", isinstance(sel, QTextCursor),
      type(sel))

# 换到有内容的行，确认真的选中了字符
block2 = doc.findBlockByNumber(4)         # "    return a + b"
data2 = DiffBlockMark("add", 3, EDITOR)
block2.setUserData(data2)
data2.selection_start = {"line": 4, "character": 4}
data2.selection_end = {"line": 4, "character": 10}
sel2 = data2._selection()
check("选中的是这一行的字符区间",
      isinstance(sel2, QTextCursor) and sel2.selectedText() == "return",
      repr(sel2.selectedText() if isinstance(sel2, QTextCursor) else sel2))

# 文档换行后（Spyder 会在 setPlainText 之后重跑 linting），旧 mark 的
# 行号可能越界——Qt 对越界返回无效 block，但绝不能炸
data2.selection_start = {"line": 999, "character": 0}
data2.selection_end = {"line": 999, "character": 3}
out_of_range = None
try:
    data2._selection()
except Exception as exc:                  # noqa: BLE001
    out_of_range = repr(exc)
check("行号越界时 _selection() 也不抛", out_of_range is None, out_of_range or "")

# ── 4) 走真插件：mark 上必须挂着真编辑器 ───────────────────────
TMP2 = tempfile.mkdtemp(prefix="v091b_")
P2, E2 = make_file(TMP2, "staged.py")
plugin = FakePlugin(FakeStack([(P2, E2)]))
res = plugin.review_files([(P2, OLD)])
check("staging 成功", res.get("staged") == 1, res)
review = plugin._review_hub.reviews()[0]

marks = []
blk = E2.document().firstBlock()
while blk.isValid():
    d = blk.userData()
    if isinstance(d, DiffBlockMark):
        marks.append(d)
    blk = blk.next()
check("文档里挂上了 mark", len(marks) >= 1, len(marks))
check("每个 mark 都指向真编辑器",
      all(m.editor is E2 for m in marks),
      [(m.kind, repr(m.editor)) for m in marks][:3])
check("mark 的 _selection 在真文档上能算",
      all(callable(m._selection) and m._selection() is None for m in marks))

# 复刻一整轮 linting：遍历每个带 mark 的块，按 Spyder 的写法设 range
# 再取下划线选区。这正是用户崩溃的那次调用。
boom = []
blk = E2.document().firstBlock()
n_lint = 0
while blk.isValid():
    d = blk.userData()
    if isinstance(d, DiffBlockMark):
        try:
            num = blk.blockNumber()
            d.selection_start = {"line": num, "character": 0}
            d.selection_end = {"line": num, "character": 2}
            sel = d._selection()
            if not isinstance(sel, QTextCursor):
                boom.append("第 %d 行没返回 cursor" % num)
            n_lint += 1
        except Exception as exc:          # noqa: BLE001
            boom.append("第 %d 行 %s: %s"
                        % (blk.blockNumber(), type(exc).__name__, exc))
    blk = blk.next()
check("带 mark 的块逐个走 linting 全程无异常", not boom, boom[:3])
check("确实复刻了若干次（>0）", n_lint > 0, n_lint)

# linting 的「收消息」分支会往 data.code_analysis 里追加
# （lsp_mixin.py:649），然后 setUserData 回去 —— 不能因此丢掉 kind/hunk
blk = E2.document().firstBlock()
target = None
while blk.isValid():
    if isinstance(blk.userData(), DiffBlockMark):
        target = blk
        break
    blk = blk.next()
target.userData().code_analysis.append(
    ("pyflakes", "F401", 2, "'math' imported but unused"))
check("追加 code_analysis 后 kind 仍在",
      target.userData().kind in ("add", "del"), target.userData().kind)
check("追加后仍被认成 DiffBlockMark（hunks 不会瞎）",
      len(review.hunks()) > 0, len(review.hunks()))

# ── 5) Spyder 各消费点走一遍 ───────────────────────────────────
try:
    from spyder.plugins.outlineexplorer.api import (
        is_cell_header, document_cells,
    )
    has_oe = True
except Exception:                          # noqa: BLE001
    has_oe = False
check("能导入 outlineexplorer API", has_oe)
if has_oe:
    oe_boom = []
    blk = E2.document().firstBlock()
    while blk.isValid():
        try:
            is_cell_header(blk)
            list(document_cells(blk, forward=True))
        except Exception as exc:          # noqa: BLE001
            oe_boom.append("%s: %s" % (type(exc).__name__, exc))
        blk = blk.next()
    check("is_cell_header / document_cells 全程无异常", not oe_boom,
          oe_boom[:2])

# get_current_warnings：data and data.code_analysis
try:
    warnings_boom = None
    blk = E2.document().firstBlock()
    while blk.isValid():
        d = blk.userData()
        if d and d.code_analysis:
            [w[-1] for w in d.code_analysis]
        blk = blk.next()
except Exception as exc:                   # noqa: BLE001
    warnings_boom = repr(exc)
check("get_current_warnings 的写法不炸", warnings_boom is None,
      warnings_boom or "")

# 行号面板 / 折行面板 / 调试面板都直接读这些字段
need = ("oedata", "code_analysis", "breakpoint", "bookmarks", "todo",
        "import_statement", "color", "selection_start", "selection_end",
        "breakpoint_condition")
missing = [k for k in need if not hasattr(marks[0], k)]
check("Spyder 消费的字段一个不缺", not missing, missing)
check("oedata 为 None（对面板等于「这行什么都不是」）",
      marks[0].oedata is None)
check("code_analysis 是可 append 的 list",
      isinstance(marks[0].code_analysis, list))
check("breakpoint 为假", not marks[0].breakpoint)

# ── 6) __getattr__ 语义 ────────────────────────────────────────
probe = DiffBlockMark("add", 9, EDITOR)
check("未知公开属性 → None", probe.some_future_field is None,
      repr(probe.some_future_field))
try:
    object.__getattribute__(probe, "__deepcopy__")
    dunder_raised = False
except AttributeError:
    dunder_raised = True
check("dunder 仍抛 AttributeError（sip / copy 靠这个判「没有」）",
      dunder_raised)

unknown_priv = probe._no_such_private_helper
check("未知下划线属性 → 可调用兜底（既不抛，也不是 None）",
      callable(unknown_priv), type(unknown_priv))
check("该兜底被调用时返回 None", unknown_priv(1, kw=2) is None)

# ── 7) 静态守卫：不许再退回裸 QTextBlockUserData ────────────────
import ast                                    # noqa: E402

src = open(os.path.join(ROOT, "spyder_copilot_chat", "widgets",
                        "editor_diff.py"), encoding="utf-8").read()
tree = ast.parse(src)
bases = None
for node in ast.walk(tree):
    if isinstance(node, ast.ClassDef) and node.name == "DiffBlockMark":
        bases = [getattr(b, "id", getattr(b, "attr", "?"))
                 for b in node.bases]
check("源码里 DiffBlockMark 的基类是 _MarkBase",
      bases == ["_MarkBase"], bases)

# ── 汇总 ───────────────────────────────────────────────────────
print("=" * 60)
for f in FAILS:
    print("FAIL  " + f)
print("PASS %d / %d" % (len(PASSES), len(PASSES) + len(FAILS)))
sys.exit(1 if FAILS else 0)
