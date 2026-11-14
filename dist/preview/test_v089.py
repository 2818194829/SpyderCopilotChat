# -*- coding: utf-8 -*-
"""v0.8.9 回归：内联复核链路在真插件对象上必须走得通。

用户截图里的那句话是

    编辑器内联未生效：文件 — staging 异常：'NoneType' object is not callable

路径是空的（聊天里那个「文件」是空 basename 的兜底文案），说明异常
发生在 review_files 的**循环之前**，跟装饰、可见范围、diff 全无关系。
真因是段名遮蔽：

    on_initialize:  self._review_hub = None       # 实例字段
    plugin 类里:    def _review_hub(self): ...    # 同名方法

实例字典优先于类，于是 ``self._review_hub()`` 实际在调用 ``None``。
v0.7.9 起内联复核**一次都没真正跑过**，每次都在这一行就断了。

覆盖：
  1) 静态守卫：全包范围内不允许「实例字段」与「方法」同名
  2) 走真正的 CopilotChatPlugin.review_files（不是 ReviewHub 单测）
  3) 成功路径：staged == 1、无 failures、幽灵行 + 装饰 + 工具条都在
  4) 登记处复用：第二次调用不再新建 ReviewHub
  5) 单个文件出问题只算它自己，不吞掉整批的其它文件
  6) 失败原因带着文件路径回到调用方（聊天里才能指名道姓）
"""
import ast
import glob
import os
import shutil
import sys
import tempfile

ROOT = (r"C:\Users\Mr_Yang\Documents\deepseek-harness"
        r"\default-workspace\spyder-copilot-chat")
sys.path.insert(0, ROOT)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from qtpy.QtWidgets import QApplication, QPlainTextEdit       # noqa: E402
from qtpy.QtCore import QObject                              # noqa: E402

from spyder_copilot_chat.plugin import CopilotChatPlugin     # noqa: E402
from spyder_copilot_chat.widgets.editor_diff import ReviewHub  # noqa: E402

APP = QApplication.instance() or QApplication([])
FAILS = []
PASSES = []


def check(name, cond, extra=""):
    if cond:
        PASSES.append(name)
    else:
        FAILS.append("%s  %s" % (name, extra))


# ── 1) 静态守卫：方法名不得与实例字段同名 ──────────────────────
collisions = []
for src in sorted(glob.glob(os.path.join(
        ROOT, "spyder_copilot_chat", "**", "*.py"), recursive=True)):
    with open(src, encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    for node in tree.body:
        if not isinstance(node, ast.ClassDef):
            continue
        methods = {n.name for n in node.body
                   if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
        attrs = set()
        for n in ast.walk(node):
            targets = []
            if isinstance(n, ast.Assign):
                targets = n.targets
            elif isinstance(n, ast.AnnAssign):
                targets = [n.target]
            for t in targets:
                if (isinstance(t, ast.Attribute)
                        and isinstance(t.value, ast.Name)
                        and t.value.id == "self"):
                    attrs.add(t.attr)
        for name in sorted(methods & attrs):
            collisions.append("%s:%s.%s" % (
                os.path.basename(src), node.name, name))

check("全包没有「实例字段遮蔽方法」", not collisions, collisions)
check("插件类里不再有 _review_hub 方法",
      "_review_hub" not in vars(CopilotChatPlugin))
check("惰性创建入口叫 _ensure_review_hub",
      callable(getattr(CopilotChatPlugin, "_ensure_review_hub", None)))

# ── 脚手架：假 EditorStack / 假 editor plugin / 假 finfo ────────
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


class FakeFinfo(object):
    def __init__(self, editor, path):
        self.filename = path
        self.editor = editor
        self.newly_created = False


class FakeStack(object):
    def __init__(self, editor, path):
        self.data = [FakeFinfo(editor, path)]
        self.current = path
        self.saved = []

    def load(self, path, set_current=False):
        return None                      # 已经打开，不该再走加载

    def set_current_filename(self, path, focus=False):
        self.current = path
        return True

    def get_index_from_filename(self, path):
        return 0

    def save(self, index, force=False):
        self.saved.append(index)
        return True


class FakeEditorPlugin(object):
    def __init__(self, stack):
        self._stack = stack

    def get_current_editorstack(self):
        return self._stack


class FakePlugin(QObject):
    """只把被测的那几个方法从真类上摘下来，其余注入假对象。"""

    review_files = CopilotChatPlugin.review_files
    _save_editor_file = CopilotChatPlugin._save_editor_file
    _ensure_review_hub = CopilotChatPlugin._ensure_review_hub

    def __init__(self, stack, patched=None):
        super().__init__()
        self._review_hub = None          # 与 on_initialize 一致
        self._stack = stack
        self._ep = FakeEditorPlugin(stack)
        self._patched = patched or {}

    def get_plugin(self, name):
        return self._ep

    def get_widget(self):
        return None

    def _editor_for_path(self, stack, path):
        # 失败注入优先，其余走真实现
        hook = self._patched.get(path)
        if hook is not None:
            return hook(stack, path)
        return CopilotChatPlugin._editor_for_path(self, stack, path)


TMP = tempfile.mkdtemp(prefix="v089_")
TARGET = os.path.join(TMP, "tank_battle.py")
with open(TARGET, "w", encoding="utf-8", newline="") as fh:
    fh.write(NEW)

editor = QPlainTextEdit()
editor.setPlainText(OLD)
editor.document().setModified(False)
STACK = FakeStack(editor, TARGET)

# ── 2) 成功路径：真 review_files ───────────────────────────────
plugin = FakePlugin(STACK)
res = plugin.review_files([(TARGET, OLD)])
check("review_files 不再抛异常", isinstance(res, dict), type(res).__name__)
check("staged == 1", res.get("staged") == 1, res)
check("没有失败项", res.get("failures") == [], res.get("failures"))

buf = editor.toPlainText()
check("文档里多出了幽灵行", buf.count("\n") > NEW.count("\n"),
      "%d vs %d" % (buf.count("\n"), NEW.count("\n")))
check("幽灵行仍是旧代码", "return a - b" in buf)
check("新行已就位", '"""两数相加。"""' in buf)
check("装饰挂了至少 2 处", len(editor.extraSelections()) >= 2,
      len(editor.extraSelections()))

hub = plugin._review_hub
check("登记处是 ReviewHub 实例", isinstance(hub, ReviewHub), type(hub).__name__)
check("登记处记下了这次复核", len(hub.reviews()) == 1, hub.reviews())

same = plugin._ensure_review_hub()
check("第二次调用复用同一个登记处", same is hub)

# ── 2b) 工具条按钮不能被挤成省略号 ─────────────────────────────
rev = hub.reviews()[0]
bar = getattr(rev, "_bar", None)
check("工具条建起来了", bar is not None)
if bar is not None:
    bar.show()
    for _ in range(3):
        APP.processEvents()
    for text, btn in (("接受修改", bar._accept), ("撤销", bar._revert)):
        need = btn.fontMetrics().horizontalAdvance(text)
        check("按钮放得下「%s」" % text, btn.width() >= need,
              "宽 %d / 需要 %d" % (btn.width(), need))
    check("工具条高度合理", 20 <= bar.height() <= 90, bar.height())

# ── 3) 单个文件失败不影响同批其它文件 ─────────────────────────
TMP2 = tempfile.mkdtemp(prefix="v089b_")
GOOD = os.path.join(TMP2, "ok.py")
BAD = os.path.join(TMP2, "boom.py")
with open(GOOD, "w", encoding="utf-8", newline="") as fh:
    fh.write(NEW)
editor2 = QPlainTextEdit()
editor2.setPlainText(OLD)
editor2.document().setModified(False)
STACK2 = FakeStack(editor2, GOOD)


def _boom(stack, path):
    raise RuntimeError("模拟编辑器取用失败")


plugin2 = FakePlugin(STACK2, patched={BAD: _boom})
res2 = plugin2.review_files([(BAD, OLD), (GOOD, OLD)])
check("坏文件不拖累好文件", res2.get("staged") == 1, res2)
paths = [p for p, _r in res2.get("failures", [])]
check("失败项带上了文件名", os.path.abspath(BAD) in
      [os.path.abspath(p) for p in paths if p], res2.get("failures"))
check("失败原因不是空话",
      all(r.strip() for _p, r in res2.get("failures", [])))

# ── 4) 打不开的文件：指名道姓地失败 ───────────────────────────
MISSING = os.path.join(TMP2, "not_here.py")
res3 = plugin2.review_files([(MISSING, "x = 1\n")])
check("找不到的文件报失败", res3.get("staged") == 0, res3)
check("失败项路径就是那个文件",
      [os.path.abspath(p) for p, _r in res3["failures"] if p]
      == [os.path.abspath(MISSING)], res3["failures"])

shutil.rmtree(TMP, ignore_errors=True)
shutil.rmtree(TMP2, ignore_errors=True)

print("")
for name in PASSES:
    print("  OK   %s" % name)
for name in FAILS:
    print("  FAIL %s" % name)
print("\n%d passed, %d failed" % (len(PASSES), len(FAILS)))
sys.exit(1 if FAILS else 0)
