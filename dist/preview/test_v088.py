# -*- coding: utf-8 -*-
"""v0.8.8 回归：字号太小 / 调不动。

用户反馈（截图）：对话里所有文字都小得费眼，而且「怎么调都没反应」。

根因不是一个而是三个叠在一起：

1. ``_render_block`` 的段落分支没写 ``font-size``——普通段落、引用、列表
   全都回落到 QTextDocument 的默认字体。中文 Windows 上那是**宋体 9pt
   （约 12px）**，比设定的正文小一大截。于是「正文字体大小」调到 32 也没用：
   只有标题 / 代码块（它们写了字号）会变，正文纹丝不动。
2. ``get_provider_config()`` 没有把 ui_theme / ui_accent / ui_density /
   tool_density 放进返回的 dict，``_apply_theme`` 里 ``.get()`` 永远拿默认值
   ——「外观」页选了浅色、紧凑，保存后毫无反应。
3. 入口太深：字号设置埋在「设置 → 外观」里，用户找不到，也不支持滚轮/快捷键。

覆盖：
  1) 段落 / 引用 / 列表的 HTML 必须带 font-size
  2) 消息视图的文档默认字体 = 正文字号（不再掉到 9pt 宋体）
  3) 外观四项能穿透 get_provider_config
  4) 字体族写进 QSS、也写进文档默认字体
  5) Aa 菜单的档位 / 勾选 / 界面字号
  6) Ctrl+= / Ctrl+- / Ctrl+0 快捷键确实挂上了
  7) 对话流 Ctrl+滚轮 = 字号（普通滚轮不动字号）
  8) 调字号会落盘，并且已存在的消息立刻按新字号重排
"""
import json
import os
import sys
import tempfile

ROOT = (r"C:\Users\Mr_Yang\Documents\deepseek-harness"
        r"\default-workspace\spyder-copilot-chat")
sys.path.insert(0, ROOT)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from qtpy.QtCore import Qt                                    # noqa: E402
from qtpy.QtWidgets import QApplication, QTextEdit            # noqa: E402

from spyder_copilot_chat.widgets import settings_dialog as sd  # noqa: E402
from spyder_copilot_chat.widgets import theme                  # noqa: E402
from spyder_copilot_chat.widgets import markdown_renderer as mr  # noqa: E402
from spyder_copilot_chat.widgets.chat_widget import (          # noqa: E402
    CopilotChatPanel, MessageView, ZoomScrollArea,
)

APP = QApplication.instance() or QApplication([])
FAILS = []
PASSES = []


def check(name, cond, extra=""):
    if cond:
        PASSES.append(name)
    else:
        FAILS.append("%s  %s" % (name, extra))


# ── 把配置指到临时目录：绝不能动用户真实的 config.json ──────────────
CFG_DIR = tempfile.mkdtemp(prefix="v088_cfg_")
sd.CONFIG_DIR = CFG_DIR
sd.CONFIG_FILE = os.path.join(CFG_DIR, "config.json")
BASE = {
    "provider": "deepseek",
    "api_key": "sk-test",
    "model": "deepseek-flash",
    "font_size": 16,
    "ui_font_size": 18,
    "ui_theme": "light",
    "ui_accent": "indigo",
    "ui_density": "compact",
    "tool_density": "compact",
    "font_family": "",
    "auto_execute": False,
    "approval_policy": "writes",
}
with open(sd.CONFIG_FILE, "w", encoding="utf-8") as fh:
    json.dump(BASE, fh, ensure_ascii=False)


def read_cfg():
    with open(sd.CONFIG_FILE, "r", encoding="utf-8") as fh:
        return json.load(fh)


# ── 1) 外观四项必须穿透 get_provider_config ─────────────────────────
cfg = sd.get_provider_config()
for key, want in (("ui_theme", "light"), ("ui_accent", "indigo"),
                  ("ui_density", "compact"), ("tool_density", "compact"),
                  ("font_size", 16), ("ui_font_size", 18)):
    check("config 穿透 %s" % key, cfg.get(key) == want,
          "got %r want %r" % (cfg.get(key), want))

# ── 2) 段落 / 引用 / 列表的 HTML 必须带字号 ─────────────────────────
theme.set_current_theme(theme.resolve_theme(
    "light", "indigo", "compact", 18, 16, ""))
para = mr.render_markdown("这是一段正文。")
check("段落带 font-size:16px", "font-size:16px" in para, para[:160])
lst = mr.render_markdown("- 第一项\n- 第二项")
check("列表带 font-size:16px", "font-size:16px" in lst, lst[:160])
quote = mr.render_markdown("> 引用一行")
check("引用带 font-size:16px", "font-size:16px" in quote, quote[:160])
head = mr.render_markdown("## 小标题")
check("标题字号大于正文", "font-size:17px" in head, head[:120])

# 字号变化必须真的改变 HTML（否则「调了没用」还会复发）
theme.set_current_theme(theme.resolve_theme(
    "light", "indigo", "compact", 20, 24, ""))
big = mr.render_markdown("这是一段正文。")
check("正文字号 24 时 HTML 跟着变", "font-size:24px" in big, big[:160])
theme.set_current_theme(theme.resolve_theme(
    "light", "indigo", "compact", 18, 16, ""))

# ── 3) 文档默认字体：不许再掉到 9pt 宋体 ────────────────────────────
view = MessageView()
view.setHtml(mr.render_markdown("一段话"))
doc_font = view.document().defaultFont()
check("文档默认字号 = 正文字号", doc_font.pixelSize() == 16,
      "pixelSize=%s pointSize=%s family=%s"
      % (doc_font.pixelSize(), doc_font.pointSizeF(), doc_font.family()))
check("文档默认字体不是宋体 9pt",
      doc_font.pointSizeF() <= 0 or doc_font.pointSizeF() >= 10,
      "pt=%s px=%s family=%s" % (doc_font.pointSizeF(),
                                 doc_font.pixelSize(), doc_font.family()))

blk = view.document().begin()
it = blk.begin()
frag_px = -1
while not it.atEnd():
    cf = it.fragment().charFormat().font()
    if cf.pixelSize() > 0:
        frag_px = cf.pixelSize()
        break
    it += 1
check("首段字符格式 = 16px", frag_px == 16, frag_px)

# ── 4) 面板：字号入口 / 落盘 / 已存在的消息立即重排 ────────────────
panel = CopilotChatPanel()
panel.resize(760, 900)
check("顶栏有 Aa 按钮", getattr(panel, "_font_btn", None) is not None)
check("对话流是 Ctrl+滚轮区域",
      isinstance(panel._scroll, ZoomScrollArea),
      type(panel._scroll).__name__)

bubble = panel._add_msg("assistant", "第一段\n\n第二段")
views = [v for v in bubble.findChildren(QTextEdit) if v.isVisible()
         or v.parent() is bubble]

# 初始 16px
check("初始正文 16px", panel._body_font_size() == 16,
      panel._body_font_size())

panel._bump_body_font(1)
check("放大到下一档 18", panel._body_font_size() == 18,
      panel._body_font_size())
check("落盘 font_size=18", read_cfg().get("font_size") == 18,
      read_cfg().get("font_size"))
check("配置文件没被派生字段污染",
      "chat_endpoint" not in read_cfg() and "label" not in read_cfg(),
      sorted(read_cfg())[:8])

body_view = bubble._view
px = -1
blk = body_view.document().begin()
it = blk.begin()
while not it.atEnd():
    cf = it.fragment().charFormat().font()
    if cf.pixelSize() > 0:
        px = cf.pixelSize()
        break
    it += 1
check("老消息按新字号重排（18px）", px == 18, px)

panel._bump_body_font(-1)
check("缩小回到 16", panel._body_font_size() == 16,
      panel._body_font_size())

# 档位表边界：最小 / 最大
for _ in range(20):
    panel._bump_body_font(-1)
check("下限是档位表最小值", panel._body_font_size() == min(theme.FONT_STEPS),
      panel._body_font_size())
for _ in range(20):
    panel._bump_body_font(1)
check("上限是档位表最大值", panel._body_font_size() == max(theme.FONT_STEPS),
      panel._body_font_size())

# 界面字号夹取
panel._set_ui_font(99)
check("界面字号上限 32", panel._ui_font_size() == 32, panel._ui_font_size())
panel._set_ui_font(1)
check("界面字号下限 9", panel._ui_font_size() == 9, panel._ui_font_size())

# ── 5) 字体族：进 QSS，也进文档默认字体 ─────────────────────────────
panel._set_font_family(theme.FONT_CHOICES[-1][1])   # 等宽 Consolas
fam = theme.current_theme().font_family
check("QSS 用上所选字族", "Consolas" in panel.styleSheet(),
      [ln for ln in panel.styleSheet().splitlines() if "font-family" in ln][:1])
check("主题记下字族", "Consolas" in fam, fam)
check("字族落盘", read_cfg().get("font_family") == theme.FONT_CHOICES[-1][1],
      read_cfg().get("font_family"))

# ── 6) 快捷键 ───────────────────────────────────────────────────────
seqs = []
for act in getattr(panel, "_font_actions", []):
    seqs.extend([s.toString() for s in act.shortcuts()])
check("挂了 Ctrl+=", any("Ctrl+=" in s for s in seqs), seqs)
check("挂了 Ctrl+-", any("Ctrl+-" in s for s in seqs), seqs)
check("挂了 Ctrl+0", any(s == "Ctrl+0" for s in seqs), seqs)
check("快捷键在子控件范围生效",
      all(a.shortcutContext() == Qt.WidgetWithChildrenShortcut
          for a in panel._font_actions))

# ── 7) Ctrl+滚轮 = 字号 ─────────────────────────────────────────────
panel._reset_fonts()
before = panel._body_font_size()
panel._scroll.zoom_requested.emit(1)
check("滚轮放大一档", panel._body_font_size() > before,
      "%s -> %s" % (before, panel._body_font_size()))
panel._scroll.zoom_requested.emit(-1)
check("滚轮缩小一档", panel._body_font_size() == before,
      panel._body_font_size())

# ── 8) Aa 菜单：档位 + 勾选 + 恢复默认 ─────────────────────────────
panel._set_body_font(20)
panel._popup_font_menu()
labels = [a.text() for a in panel._font_menu.actions()]
check("菜单列出全部档位",
      all(("%d px" % s) in labels for s in theme.FONT_STEPS), labels)
checked = [a.text() for a in panel._font_menu.actions()
           if a.isCheckable() and a.isChecked()]
check("当前档位被勾选", checked == ["20 px"], checked)
check("菜单有字体子菜单",
      any(a.menu() is not None for a in panel._font_menu.actions()), labels)
panel._font_menu.hide()

panel._reset_fonts()
check("恢复默认 14/13", (panel._body_font_size(), panel._ui_font_size())
      == (14, 13), (panel._body_font_size(), panel._ui_font_size()))
check("默认值落盘", (read_cfg().get("font_size"),
                     read_cfg().get("ui_font_size"),
                     read_cfg().get("font_family")) == (14, 13, ""),
      read_cfg())

# ── 收尾 ───────────────────────────────────────────────────────────
print("=" * 60)
for f in FAILS:
    print("FAIL", f)
print("v0.8.8: %d/%d 通过" % (len(PASSES), len(PASSES) + len(FAILS)))
sys.exit(1 if FAILS else 0)
