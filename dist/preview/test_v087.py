# -*- coding: utf-8 -*-
"""v0.8.7 回归：用 run_command 写盘时，内联 diff 也必须出现。

用户反馈（截图）：让 Agent 改 tank_battle.py / adventure_island.py，
改动确实落盘了，但**编辑器里一点红绿都没有，聊天里连一句失败提示
也没有**。审计日志给出了答案：

    event=tool_requested tools=["run_command","run_command"]

弱模型（deepseek-flash）几乎不用 edit_file，而是

    python -c "open(r'D:\\...\\tank_battle.py','w').write(...)"

这类命令的参数里没有 filepath，``affected_files`` 解析为空 ⇒ 写盘前
快照为空 ⇒ ``_stage_editor_reviews`` 直接 return。整条预览链路静默
哑火，而且因为「没有 items」连失败文案都不会出现。

覆盖：
  1) 从命令文本反推路径（引号 / 无引号 / --opt=val / 带行号 / URL 反例）
  2) changed_files_from_calls 认 run_command（只在 include_shell=True 时）
  3) 目录快照能找出「被 run_command 改过」的文件
  4) 端到端：一批 run_command 之后，items 里必须出现被改的文件
  5) 没改动的文件不会混进 items
  6) 一次改很多文件时，编辑器只摊开 8 个，py 优先
  7) stage() 不再被 isModified() 单独挡下（内容与磁盘一致就放行）
"""
import json
import os
import shutil
import sys
import tempfile

ROOT = (r"C:\Users\Mr_Yang\Documents\deepseek-harness"
        r"\default-workspace\spyder-copilot-chat")
sys.path.insert(0, ROOT)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from qtpy.QtWidgets import QApplication                 # noqa: E402

from spyder_copilot_chat.widgets.approval_card import (  # noqa: E402
    affected_files, paths_from_command,
)
from spyder_copilot_chat.widgets.task_runtime import (   # noqa: E402
    changed_files_from_calls,
)
from spyder_copilot_chat.widgets.chat_widget import (    # noqa: E402
    CopilotChatPanel, _scan_sources, _batch_may_write_blindly,
)

APP = QApplication.instance() or QApplication([])
FAILS = []
PASSES = []


def check(name, cond, extra=""):
    if cond:
        PASSES.append(name)
    else:
        FAILS.append(f"{name}  {extra}")


def call(name, **args):
    return {"function": {"name": name,
                         "arguments": json.dumps(args, ensure_ascii=False)}}


TMP = tempfile.mkdtemp(prefix="v087_")
PY = os.path.join(TMP, "tank battle.py")


def write(path, text):
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


# ── 1) 从命令反推路径 ──────────────────────────────────────────
cmd = (
    'python -c "import io,os;'
    "p=r'%s';"
    "io.open(p,'w',encoding='utf-8').write('x')\"" % PY
)
got = paths_from_command(cmd, TMP)
check("引号内带空格路径能抓到", any(os.path.normcase(os.path.abspath(p))
                                   == os.path.normcase(os.path.abspath(PY))
                                   for p in got), got)

got = paths_from_command(
    r"python edit.py --out=%s" % os.path.join(TMP, "a.py"), TMP)
check("--out=val 形式能抓到",
      any(p.lower().endswith("a.py") for p in got), got)

got = paths_from_command(
    'python script.py > "%s"' % os.path.join(TMP, "b.py"), TMP)
check("重定向能抓到", any(p.lower().endswith("b.py") for p in got), got)

got = paths_from_command(
    'type "%s:12" ' % os.path.join(TMP, "c.py"), TMP)
check("带行号写法不崩", isinstance(got, list))

got = paths_from_command(
    "pip install https://example.com/foo.py --index-url https://pypi.org",
    TMP)
check("URL 不算路径", not got, got)

got = paths_from_command("dir", TMP)
check("纯命令返回空", got == [], got)

# 命令片段不能被当成路径（open(r'D:\a.py 这种半截货）
nest = ('python -c ' + chr(34) + "open(r'" + os.path.join(TMP, "sp ace.py")
        + "'" + ", 'w')" + chr(34))
got = paths_from_command(nest, TMP)
check("带空格的嵌套路径能抓到",
      any(os.path.basename(p) == "sp ace.py" for p in got), got)
check("半截命令片段不算路径",
      all(not p.startswith("open(") and "open(r" not in p for p in got), got)

# ── 2) changed_files_from_calls 认 run_command ─────────────────
calls = [call("run_command", command=cmd)]
check("默认不把 run_command 当写盘",
      changed_files_from_calls(calls, TMP) == [])
shell_files = changed_files_from_calls(calls, TMP, include_shell=True)
check("include_shell 后能抓到",
      any(os.path.normcase(os.path.abspath(p))
          == os.path.normcase(os.path.abspath(PY)) for p in shell_files),
      shell_files)
check("affected_files 对 run_command 直接可用",
      any(p.endswith("tank battle.py")
          for p in affected_files("run_command",
                                  {"command": cmd}, TMP)))

# ── 3) 目录快照能找到 run_command 的改动 ────────────────────────
write(PY, "def draw():\n    return 1\n")
other = os.path.join(TMP, "untouched.py")
write(other, "x = 1\n")
write(os.path.join(TMP, "notes.md"), "# 笔记\n")

panel = CopilotChatPanel()
panel._config["chat_diff_mode"] = "smart"
panel._config["inline_diff_enabled"] = True
panel._review_provider = lambda items: {"staged": len(items),
                                        "failures": []}
panel._get_project_root = lambda: TMP
panel._emit_chat_diffs = lambda *a, **k: None
panel._scroll_to_bottom = lambda: None
panel._current_bubble = None

check("批次被识别为盲写",
      _batch_may_write_blindly(calls) is True)
check("普通 edit_file 不算盲写",
      _batch_may_write_blindly(
          [call("edit_file", filepath=PY, start_line=1,
                new_content="y")]) is False)

panel._snapshot_review_targets(calls, TMP)
check("快照里包含目标文件", PY in panel._review_before,
      sorted(panel._review_before)[:5])
check("盲写时留了目录快照", panel._review_scan is not None)

# 模拟 run_command 真的把文件改了（内容变了、mtime 也变）
write(PY, "def draw():\n    return 2\n    print('new')\n")
staged = []
panel._review_provider = lambda items: (staged.extend(items)
                                        or {"staged": len(items),
                                            "failures": []})
panel._stage_editor_reviews(calls)

check("run_command 的改动被送进编辑器",
      any(os.path.normcase(os.path.abspath(p))
          == os.path.normcase(os.path.abspath(PY)) for p, _t in staged),
      [p for p, _t in staged])
check("送进去的是改动前原文",
      any(old.startswith("def draw():\n    return 1") for _p, old in staged),
      staged[:1])
check("没改的文件不混进来",
      all(not p.endswith("untouched.py") and not p.endswith("notes.md")
          for p, _t in staged), [p for p, _t in staged])
check("快照用后即清",
      panel._review_before == {} and panel._review_scan is None)

# ── 4) 什么也没写时不能凭空造 diff ─────────────────────────────
panel2 = CopilotChatPanel()
panel2._config["chat_diff_mode"] = "smart"
panel2._config["inline_diff_enabled"] = True
panel2._review_provider = lambda items: {"staged": len(items),
                                         "failures": []}
panel2._get_project_root = lambda: TMP
panel2._emit_chat_diffs = lambda *a, **k: None
panel2._scroll_to_bottom = lambda: None
panel2._current_bubble = None
panel2._snapshot_review_targets([call("run_command", command="dir")], TMP)
seen = []
panel2._review_provider = lambda items: (seen.extend(items)
                                         or {"staged": len(items),
                                             "failures": []})
panel2._stage_editor_reviews([call("run_command", command="dir")])
check("没改动就没有 items", seen == [], seen)

# ── 5) 多文件时编辑器只摊开 8 个，py 优先 ──────────────────────
many = []
for i in range(12):
    p = os.path.join(TMP, "m%02d.%s" % (i, "py" if i % 2 else "md"))
    write(p, "v = %d\n" % i)
    many.append(p)
panel3 = CopilotChatPanel()
panel3._config["chat_diff_mode"] = "smart"
panel3._config["inline_diff_enabled"] = True
panel3._get_project_root = lambda: TMP
panel3._emit_chat_diffs = lambda *a, **k: None
panel3._scroll_to_bottom = lambda: None
panel3._current_bubble = None
batch = [call("run_command", command="pytest -q")]
panel3._review_provider = lambda items: {"staged": len(items),
                                         "failures": []}
panel3._snapshot_review_targets(batch, TMP)
for i, p in enumerate(many):
    write(p, "v = %d  # changed\n" % i)
opened = []
panel3._review_provider = lambda items: (opened.extend(items)
                                         or {"staged": len(items),
                                             "failures": []})
panel3._stage_editor_reviews(batch)
check("最多只开 8 个复核", len(opened) <= 8, len(opened))
check("py 全部排在其他类型前面",
      [p.lower().endswith(".py") for p, _t in opened]
      == sorted([p.lower().endswith(".py") for p, _t in opened],
                reverse=True),
      [os.path.basename(p) for p, _t in opened])

# ── 6) stage() 不被 isModified 单独挡下 ────────────────────────
try:
    from qtpy.QtWidgets import QPlainTextEdit
    from spyder.plugins.editor.utils.decoration import (
        TextDecorationsManager)
    from spyder_copilot_chat.widgets import theme
    from spyder_copilot_chat.widgets.editor_diff import InlineReview

    class FakeEditor(QPlainTextEdit):
        def __init__(self):
            super().__init__()
            self.decorations = TextDecorationsManager(self)

        def get_buffer_block_numbers(self):
            return (0, 10 ** 6)

    theme.set_current_theme(theme.resolve_theme("dark"))
    path = os.path.join(TMP, "iso.py")
    old = "a = 1\nb = 2\nc = 3\n"
    write(path, old)
    ed = FakeEditor()
    ed.resize(600, 300)
    ed.setPlainText(old)
    ed.show()
    APP.processEvents()
    # 造出「isModified 为真但内容和磁盘一致」的情形
    ed.document().setModified(True)
    write(path, "a = 1\nb = 22\nc = 3\n")
    rev = InlineReview(ed, path, old, saver=lambda *a: True)
    ok = rev.stage()
    check("内容一致时不被 isModified 挡下",
          ok is True, rev.fail_reason)
    check("装饰真的挂上了", rev.deco_count > 0, rev.deco_count)

    # 模拟「装饰被可见范围过滤光」：清空 extraSelections 再让自愈跑一次
    ed.setExtraSelections([])
    check("清空后确实没有装饰了", rev._own_selection_count() == 0)
    rev._ensure_painted()
    check("自愈后装饰回来了", rev._own_selection_count() > 0,
          rev._own_selection_count())

    # 反过来：buffer 里真有未保存修改时必须让路
    ed2 = FakeEditor()
    ed2.setPlainText(old)
    ed2.show()
    APP.processEvents()
    ed2.document().setModified(True)
    ed2.setPlainText(old + "d = 4\n")
    ed2.document().setModified(True)
    rev2 = InlineReview(ed2, path, old, saver=lambda *a: True)
    ok2 = rev2.stage()
    check("真有未保存修改时让路", ok2 is False
          and "未保存" in rev2.fail_reason, rev2.fail_reason)
except Exception as exc:                                   # noqa: BLE001
    import traceback
    traceback.print_exc()
    check("stage 相关用例执行", False, str(exc))

# ── 7) /diffdemo 自检：只碰副本，不动原文件 ────────────────────
demo_src = os.path.join(TMP, "demo_subject.py")
demo_text = "\n".join(
    ["import os", "", "def a():", "    return 1", "", "def b():",
     "    return 2", "", "def c():", "    return 3", ""])
write(demo_src, demo_text)
before_src = open(demo_src, encoding="utf-8").read()

panel4 = CopilotChatPanel()
panel4._current_editor_text = lambda: (demo_src, demo_text)
seen_items = []
panel4._review_provider = lambda items: (seen_items.extend(items)
                                         or {"staged": len(items),
                                             "failures": []})
msgs = []
panel4._add_msg = lambda role, text="": (msgs.append(text)
                                         or len(msgs))
panel4._run_inline_diff_demo()

check("自检把改动送进了编辑器",
      len(seen_items) == 1 and seen_items[0][1] == demo_text,
      [p for p, _t in seen_items])
check("自检用的是副本",
      bool(seen_items) and seen_items[0][0] != demo_src
      and "diff-demo" in seen_items[0][0], seen_items[:1])
check("原文件一个字没变",
      open(demo_src, encoding="utf-8").read() == before_src)
check("自检给了成功文案", bool(msgs) and "✓" in msgs[0], msgs[:1])
if seen_items:
    got = open(seen_items[0][0], encoding="utf-8").read()
    check("副本里确实有新增行", "# ← 新增的一行" in got)
    check("副本里确实有被修改行", "# ← 被修改的行" in got)

panel5 = CopilotChatPanel()
panel5._review_provider = None
panel5._current_editor_text = lambda: (demo_src, demo_text)
msgs2 = []
panel5._add_msg = lambda role, text="": (msgs2.append(text) or len(msgs2))
panel5._run_inline_diff_demo()
check("没通道时明确说清楚", bool(msgs2) and "✕" in msgs2[0], msgs2[:1])

shutil.rmtree(TMP, ignore_errors=True)
print("=" * 56)
for f in FAILS:
    print("FAIL", f)
print("通过 %d / %d" % (len(PASSES), len(PASSES) + len(FAILS)))
sys.exit(1 if FAILS else 0)
