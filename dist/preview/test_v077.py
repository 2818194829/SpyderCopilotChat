# -*- coding: utf-8 -*-
"""v0.7.7 diff 视图回归：解析 / 行号 / 词级高亮 / 渲染结构。"""
import io
import os
import sys

sys.path.insert(0, r"C:\Users\Mr_Yang\Documents\deepseek-harness\default-workspace\spyder-copilot-chat")

from spyder_copilot_chat.widgets import diff_view as dv
from spyder_copilot_chat.widgets import theme

FAIL = []
def check(name, cond, extra=""):
    print(("  ok  " if cond else "  FAIL") + "  " + name + (("  -> " + str(extra)) if extra and not cond else ""))
    if not cond:
        FAIL.append(name)

pal = theme.DARK

print("== 1. unified diff 解析 ==")
uni = """--- a/calc.py
+++ b/calc.py
@@ -12,7 +12,8 @@ def total(items):
     s = 0
     for i in items:
-        s += i.price
+        s += i.price * i.qty
+        s = round(s, 2)
     return s
"""
doc = dv.parse(uni)
kinds = [r["kind"] for r in doc["lines"]]
check("文件行 + hunk + 6 行正文", kinds == ["file", "hunk", "ctx", "ctx", "del", "add", "add", "ctx"], kinds)
check("adds/dels 计数", (doc["adds"], doc["dels"]) == (2, 1), (doc["adds"], doc["dels"]))
ctx0 = doc["lines"][2]
check("上下文行号 12/12", (ctx0["old"], ctx0["new"]) == (12, 12), (ctx0["old"], ctx0["new"]))
delrow = doc["lines"][4]
check("删除行只占旧号 14", (delrow["old"], delrow["new"]) == (14, None), (delrow["old"], delrow["new"]))
addrow = doc["lines"][5]
check("新增行只占新号 14", (addrow["old"], addrow["new"]) == (None, 14), (addrow["old"], addrow["new"]))
tail = doc["lines"][7]
check("尾部上下文 15/16", (tail["old"], tail["new"]) == (15, 16), (tail["old"], tail["new"]))
check("删除行没有多余区间（只加不改）", delrow["spans"] is None, delrow["spans"])
check("新增行词级只盖 * i.qty",
      addrow["spans"] == [(len("        s += i.price"), len("        s += i.price * i.qty"))],
      addrow["spans"])

print("== 2. 多文件补丁：第二个 --- 不能当删除行 ==")
multi = """--- a/a.py
+++ b/a.py
@@ -1,2 +1,2 @@
-x = 1
+x = 2
 y = 3
--- a/b.py
+++ b/b.py
@@ -5,1 +5,2 @@
 z = 1
+w = 2
"""
doc = dv.parse(multi)
kinds = [r["kind"] for r in doc["lines"]]
check("两个文件行", kinds.count("file") == 2, kinds)
check("没有把 --- 解析成 del", doc["dels"] == 1, doc["dels"])
files = [r["text"] for r in doc["lines"] if r["kind"] == "file"]
check("文件名去掉了 b/ 前缀", files == ["a.py", "b.py"], files)

print("== 3. V4A（Codex apply_patch）==")
v4a = """*** Begin Patch
*** Update File: src/app.py
@@ def greet():
-print("Hi")
+print("Hello")
*** Add File: notes.txt
+one
+two
*** Delete File: obsolete.txt
-gone
*** End Patch
"""
doc = dv.parse(v4a)
kinds = [r["kind"] for r in doc["lines"]]
check("三处文件操作", [r["text"][:2] for r in doc["lines"] if r["kind"] == "file"] == ["修改", "新建", "删除"], kinds)
u = [r for r in doc["lines"] if r["kind"] == "del"][0]
check("Update 无行号（不伪造）", u["old"] is None and u["new"] is None, (u["old"], u["new"]))
adds = [r for r in doc["lines"] if r["kind"] == "add"]
check("Add File 从 1 开始编号", [a["new"] for a in adds] == [None, 1, 2], [a["new"] for a in adds])
check("Delete File 旧号从 1 开始", [r for r in doc["lines"] if r["kind"] == "del"][-1]["old"] == 1)

print("== 4. 识别与容错 ==")
check("@@ 补丁被认出", dv.looks_like_diff("@@ -1 +1 @@\n-a\n+b"))
check("V4A 被认出", dv.looks_like_diff("*** Begin Patch\n*** End Patch"))
check("普通代码不被误判", not dv.looks_like_diff("def f():\n    return 1 - 2\n"))
check("CRLF + 尾随空行", dv.parse("@@ -1 +1 @@\r\n-a\r\n+b\r\n\r\n")["adds"] == 1)
noise = dv.parse("--- a/x\n+++ b/x\n@@ -1 +1 @@\n-a\n+b\n\\ No newline at end of file\n")
check("No newline 噪声被跳过", not any("No newline" in r["text"] for r in noise["lines"]))
check("/dev/null 不画文件行", [r["kind"] for r in dv.parse("--- /dev/null\n+++ b/n.py\n@@ -0,0 +1 @@\n+x\n")["lines"]].count("file") == 1)

print("== 5. 渲染结构 ==")
html_body, trunc = dv.render_rows(doc, pal, 12)
check("是表格", html_body.startswith("<table") and html_body.endswith("</table>"))
check("每行一个 td（整行底色）", html_body.count("<tr>") == len(doc["lines"]))
check("行底色用显式色值", pal["diff_add_bg"] in html_body and pal["code_bg"] in html_body)
check("绝不出现 transparent", "transparent" not in html_body.lower())
check("标记列有 + / -", ">+</td>" in html_body and ">-</td>" in html_body)
check("词级高亮是真的 span", 'background-color:%s' % pal["diff_add_strong"] in html_body)
check("未截断", trunc == 0)

big = "\n".join("+line %d" % i for i in range(600))
body, trunc = dv.render_rows(dv.parse(big), pal, 12, max_lines=400)
check("超长折叠", trunc == 200 and "已折叠" in body, (trunc,))

print("== 6. old/new → 补丁 ==")
patch, a, d = dv.render_diff("a\nb\nc\nd\ne\n", "a\nB\nc\nd\ne\n", pal, 12)
check("改动统计", (a, d) == (1, 1), (a, d))
check("有行号", ">2</td>" in patch)
check("无变化返回空", dv.render_diff("same\n", "same\n", pal, 12)[0] == "")
_old = "\n".join("line%d" % i for i in range(40)) + "\n"
_patch = dv.unified(_old, _old.replace("line20", "CHANGED"))
_body = [l for l in _patch.split("\n")
         if l[:1] in (" ", "+", "-") and not l.startswith(("+++", "---"))]
check("上下文默认 3 行：3+1+1+3", len(_body) == 8, len(_body))

print("== 7. 主题 token 齐全 ==")
for key in ("diff_add", "diff_add_bg", "diff_add_strong", "diff_del", "diff_del_bg", "diff_del_strong"):
    check("DARK/LIGHT 都有 " + key, key in theme.DARK and key in theme.LIGHT)
check("深浅两套的 strong 不同", theme.DARK["diff_add_strong"] != theme.LIGHT["diff_add_strong"])
check("strong 比 bg 更实（颜色距离更大）",
      abs(int(theme.DARK["diff_add_strong"][1:3], 16) - int(theme.DARK["diff_add_bg"][1:3], 16)) > 10)

print()
if FAIL:
    print("FAILED %d: %s" % (len(FAIL), FAIL))
    sys.exit(1)
print("ALL PASS")
