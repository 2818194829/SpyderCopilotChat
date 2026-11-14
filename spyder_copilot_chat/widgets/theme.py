# -*- coding: utf-8 -*-
"""
v0.6.1 Native Edition — 统一设计系统（配色与 IDE 同源）

调研 2026 年主流代码 Agent（Cline 3.x / Cursor 3.4 / GitHub Copilot Chat
1.110+ / Windsurf Cascade / Continue.dev）后提炼的界面规范：

  * 语义化 token：界面只引用 ``bg / surface / text / accent / danger`` 等
    语义角色，颜色本身集中在本文件，不再散落在各 widget 的内联 QSS 里。
  * **单一色源**：底色 / 正文 / 高亮来自宿主 IDE 的 QPalette（Spyder 6 用
    qdarkstyle），中性色阶由底色按 HSL 亮度派生——面板和编辑器是同一个
    主题的两层，而不是两套配色拼在一起。
  * 亮 / 暗双主题 + 自动跟随宿主；单一强调色承担所有"可交互"含义，
    默认取 Spyder 蓝。
  * 4px 间距基数、8~12px 圆角、克制阴影、矢量线性图标替代彩色 emoji。
  * 三段式布局：紧凑顶栏 / 低噪音对话流 / 底部统一 composer。
  * 工具痕迹密度可调（参考 Cursor 3.4 Compact / Balanced / Detailed）。

本模块只依赖 qtpy，不依赖 Spyder，可独立离屏测试。
"""

import re

from qtpy.QtCore import QLineF, QPointF, QRectF, Qt, QSize, Signal
from qtpy.QtGui import QPolygonF
from qtpy.QtGui import (
    QBrush, QColor, QFontMetrics, QIcon, QImage, QPainter, QPen, QPixmap,
)
from qtpy.QtWidgets import (
    QApplication, QFrame, QHBoxLayout, QLabel, QPushButton, QSizePolicy,
    QToolButton, QWidget,
)

# ── 主题 token ────────────────────────────────────────────────────────
#
# v0.6.1 Native Edition：配色不再自成一套，而是**从宿主 IDE 的底色派生**。
#
# Spyder 6 的界面主题直接用 qdarkstyle 的调色板（见
# spyder/config/appearance.py 里的 DarkPalette / LightPalette），其核心值为：
#     background_1 #19232d   background_2 #293544   background_3 #37414f
#     text_1 #dfe1e2         text_3 #acb1b6         accent_3 #1a72bb
#     accent_4 #259ae9
# 所以这里以 #19232d / #dfe1e2 为种子，用"向白/向黑混合固定比例"的方式
# 生成整套中性色阶（surface / border / scrollbar…）。好处是：面板和编辑器
# 底板天然同色，不会出现"一个偏黑、一个偏蓝灰"的拼贴感；同时把种子换成
# 任意宿主色，整块界面仍然协调（自定义主题也能跟着走）。
#
# 强调色默认取 Spyder 的蓝（#259ae9 系），不再用青碧——青绿按钮放在深蓝灰
# 的 IDE 里是最刺眼的一处。

_HOST_BG_DARK = "#19232d"      # qdarkstyle background_1
_HOST_TEXT_DARK = "#dfe1e2"    # qdarkstyle text_1
_HOST_BG_LIGHT = "#fafafa"     # qdarkstyle light background_1
_HOST_TEXT_LIGHT = "#19232d"   # qdarkstyle light text_1


def _mix(color_a, color_b, ratio):
    """把 a 按 ratio 混向 b（0 → a，1 → b），返回 #rrggbb。"""
    ca, cb = QColor(color_a), QColor(color_b)
    r = ratio
    return "#%02x%02x%02x" % (
        int(round(ca.red() * (1 - r) + cb.red() * r)),
        int(round(ca.green() * (1 - r) + cb.green() * r)),
        int(round(ca.blue() * (1 - r) + cb.blue() * r)),
    )


def _shade(color, delta_l):
    """只改 HSL 亮度，保留色相与饱和度。

    直接往白色混会让颜色一路变灰（深蓝灰会变成中性灰），而 Spyder 的
    qdarkstyle 色阶是同一色相逐级提亮；这里复刻后者，面板的十几层底色
    才像"同一个主题的不同层级"，而不是一堆近似色拼起来。
    """
    c = QColor(color)
    h, s, l, a = c.getHslF()
    if h < 0:
        h = 0.0
    l = max(0.0, min(1.0, l + delta_l))
    out = QColor()
    out.setHslF(h, s, l, a)
    return out.name()


def _tokens(name, bg, text):
    """由底色 + 正文色派生整份语义 token。"""
    dark = name == "dark"
    if dark:
        soften = lambda c, r: _mix(c, bg, r)
    else:
        soften = lambda c, r: _mix(c, "#ffffff", r)

    # 语义色取"在深蓝灰底上不刺眼"的中间饱和度，而不是纯色相高亮。
    success = "#5cb98c" if dark else "#1f7a4d"
    warning = "#dfa948" if dark else "#96650c"
    danger = "#e8736b" if dark else "#bf3b3b"
    info = "#4fa8ea" if dark else "#1a72bb"

    if dark:
        t = {
            "bg": bg,
            "surface": _shade(bg, 0.035),
            "surface_alt": _shade(bg, 0.078),
            "elevated": _shade(bg, 0.105),
            "code_bg": _shade(bg, -0.032),
            # 代码块表头比代码底再亮一档（对标 harness 的 #f7f7f7 / #2b2b2b）
            "code_header": _shade(bg, 0.082),
            "border": _shade(bg, 0.062),
            "border_strong": _shade(bg, 0.125),
            "text": text,
            "text_muted": _mix(text, bg, 0.340),
            "text_faint": _mix(text, bg, 0.545),
            "scrollbar": _shade(bg, 0.105),
            "scrollbar_hover": _shade(bg, 0.185),
        }
    else:
        t = {
            "bg": bg,
            "surface": "#ffffff",
            "surface_alt": _shade(bg, -0.055),
            "elevated": "#ffffff",
            "code_bg": _shade(bg, -0.075),
            "code_header": _shade(bg, 0.030),
            "border": _shade(bg, -0.085),
            "border_strong": _shade(bg, -0.155),
            "text": text,
            "text_muted": _mix(text, bg, 0.300),
            "text_faint": _mix(text, bg, 0.480),
            "scrollbar": _shade(bg, -0.120),
            "scrollbar_hover": _shade(bg, -0.200),
        }

    # v0.8.1：diff 色不再由 success/danger 按比例「软化」得到。
    # 混色混出来的深色档是 #243b3c / #3a3037 —— 一个是青灰、一个是紫灰，
    # 在深色代码底上几乎分不出谁红谁绿，这大概就是「标了红却看不见」的
    # 由来。这里直接给红/绿各定一组色相明确的底 + 词级加深档，取的是
    # GitHub 深色 / 浅色 diff 的观感（红偏暖、绿偏冷），并保证正文
    # （#dfe1e2 / #19232d）在上面仍够对比度。
    if dark:
        diff_add, diff_add_bg, diff_add_strong = (
            "#56d364", "#1d3428", "#2f5c42")
        diff_del, diff_del_bg, diff_del_strong = (
            "#f85149", "#3d2325", "#5f3033")
    else:
        diff_add, diff_add_bg, diff_add_strong = (
            "#1a7f37", "#dff3e5", "#a6dcb8")
        diff_del, diff_del_bg, diff_del_strong = (
            "#cf222e", "#fce3e2", "#f4b7b4")

    t.update({
        "name": name,
        "success": success,
        "success_soft": soften(success, 0.84),
        "warning": warning,
        "warning_soft": soften(warning, 0.84),
        "danger": danger,
        "danger_soft": soften(danger, 0.84),
        "info": info,
        "info_soft": soften(info, 0.84),
        "diff_add": diff_add,
        "diff_add_bg": diff_add_bg,
        # 词级高亮用的加深档：铺在 diff_add_bg 的整行底色之上，只盖住真正
        # 变了的字符。两档都必须是 token（而不是现算的色值），否则主题
        # 切换时 recolor_html 的「旧色 → 新色」映射会漏掉它。
        "diff_add_strong": diff_add_strong,
        "diff_del": diff_del,
        "diff_del_bg": diff_del_bg,
        "diff_del_strong": diff_del_strong,
        "on_accent": "#ffffff",
    })
    return t


DARK = _tokens("dark", _HOST_BG_DARK, _HOST_TEXT_DARK)
LIGHT = _tokens("light", _HOST_BG_LIGHT, _HOST_TEXT_LIGHT)

# 强调色预设：(显示名, 暗色值, 亮色值, 暗色填充值, 亮色填充值)
# 第 4/5 项是"实心按钮"用的填充色——它要比文字用的强调色更深，
# 才能保证白字在上面达到 4.5:1 对比度。
ACCENTS = {
    "spyder": ("Spyder 蓝 · 跟随 IDE", "#4fa8ea", "#1a72bb", "#1a72bb", "#1a72bb"),
    "aqua":   ("青碧",                 "#4fd1c5", "#0d8f80", "#0f766e", "#0d7a6d"),
    "indigo": ("靛蓝",                 "#8b9bff", "#4f46e5", "#4a44c4", "#4338ca"),
    "violet": ("紫罗兰",               "#c792ea", "#7c3aed", "#6b30c9", "#6d28d9"),
    "amber":  ("琥珀",                 "#e0a94a", "#96650c", "#8a5c0c", "#855a0b"),
    "graphite": ("石墨（中性灰）",      "#9fb0c0", "#4a5a68", "#465560", "#43525e"),
}

DEFAULT_ACCENT = "spyder"

THEME_CHOICES = [
    ("跟随 IDE（自动）", "auto"),
    ("深色", "dark"),
    ("浅色", "light"),
]

DENSITY_CHOICES = [
    ("紧凑", "compact"),
    ("舒适（推荐）", "comfortable"),
    ("宽松", "spacious"),
]

#: 正文字族。留空 = 跟随系统（QSS 里的默认栈）。
FONT_CHOICES = [
    ("跟随系统", ""),
    ("微软雅黑 / 苹方", "'Microsoft YaHei','PingFang SC'"),
    ("思源黑体 / Noto", "'Source Han Sans SC','Noto Sans CJK SC'"),
    ("等宽（Consolas）", "'Cascadia Code','Consolas',monospace"),
]

#: 正文字号档位：面板里的 Aa 菜单和滚轮缩放都按这张表走。
FONT_STEPS = [12, 13, 14, 15, 16, 18, 20, 22, 24, 28, 32, 36]

TOOL_DENSITY_CHOICES = [
    ("Compact — 只留结果", "compact"),
    ("Balanced — 关键步骤（推荐）", "balanced"),
    ("Detailed — 完整轨迹", "detailed"),
]

_DENSITY = {
    "compact":     {"pad": 6, "gap": 4, "radius": 8,  "header": 34},
    "comfortable": {"pad": 9, "gap": 7, "radius": 10, "header": 38},
    "spacious":    {"pad": 13, "gap": 10, "radius": 12, "header": 44},
}

_FONT_UI = "'Segoe UI','Microsoft YaHei','PingFang SC',sans-serif"
_FONT_MONO = "'Cascadia Code','Consolas','JetBrains Mono',monospace"


class Theme(object):
    """一次解析出的完整主题；属性访问 token，也支持 ``t['key']``。"""

    def __init__(self, tokens, accent_key=DEFAULT_ACCENT, density="comfortable",
                 ui_font=13, body_font=14, accent_hex=None, font_family=""):
        self._t = dict(tokens)
        dark = tokens["name"] == "dark"
        if accent_key not in ACCENTS:
            accent_key = DEFAULT_ACCENT
        label, dark_hex, light_hex, dark_fill, light_fill = ACCENTS[accent_key]
        accent = dark_hex if dark else light_hex
        fill = dark_fill if dark else light_fill
        if accent_hex:
            # 宿主 IDE 给了自定义高亮色：强调色跟着它，填充用原始高亮色。
            accent = accent_hex
            fill = (accent_hex if contrast("#ffffff", accent_hex) >= 4.5
                    else _mix(accent_hex, "#000000", 0.30))
        self._t["accent"] = accent
        self._t["accent_fill"] = fill
        # 半透明强调底色：直接把强调色压到底色里，避免手调一串近似色。
        self._t["accent_soft"] = _mix(accent, tokens["bg"], 0.82 if dark else 0.86)
        self._t["selection"] = _mix(tokens["bg"], accent, 0.34 if dark else 0.24)
        self._t["accent_key"] = accent_key
        self._t["accent_label"] = label
        self.accent_key = accent_key
        self.density = density if density in _DENSITY else "comfortable"
        self.metrics = dict(_DENSITY[self.density])
        self.ui_font = int(ui_font)
        self.body_font = int(body_font)
        # 字体族：QSS 用整串（带引号和回退栈），QFont 用第一个可用名字。
        self.font_family = (font_family or "").strip() or _FONT_UI
        first = self.font_family.split(",")[0].strip().strip("'\"")
        self.font_family_name = first if first != _FONT_UI else ""

    def __getitem__(self, key):
        return self._t[key]

    def get(self, key, default=None):
        return self._t.get(key, default)

    @property
    def is_dark(self):
        return self._t["name"] == "dark"

    @property
    def tokens(self):
        return dict(self._t)


def _luminance(qcolor):
    """WCAG 相对亮度。"""
    def lin(v):
        v = v / 255.0
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4
    return (0.2126 * lin(qcolor.red()) + 0.7152 * lin(qcolor.green())
            + 0.0722 * lin(qcolor.blue()))


def contrast(color_a, color_b):
    """两色的对比度，1.0 ~ 21.0。"""
    la, lb = _luminance(QColor(color_a)), _luminance(QColor(color_b))
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


def _readable(color, bg, target=4.5, dark=True):
    """把一个颜色朝白（暗底）或黑（亮底）推，直到在 bg 上可读。"""
    c = QColor(color)
    for _ in range(10):
        if contrast(c.name(), bg) >= target:
            break
        c = QColor(_mix(c.name(), "#ffffff" if dark else "#000000", 0.12))
    return c.name()


def _spyder_interface():
    """用 Spyder 自己的判定得到 "dark" / "light"，取不到返回 None。

    这是 0.6.1 最大的坑：Spyder 6 把 qdarkstyle 的**样式表**挂在主窗口上，
    并没有设置 QApplication 的调色板——``palette().Window`` 始终是 Qt 默认的
    浅灰 ``#efefef``。于是按调色板亮度判断的结果永远是「亮色」，面板在暗色
    IDE 里变成一整块白板，而未命名的控件又被宿主样式表刷成 ``#19232d``，
    看起来就是「白底里嵌了一条黑条」。

    正确做法是问 Spyder 自己：``spyder.config.gui.is_dark_interface()``
    （它综合 ``appearance/ui_theme`` 与编辑器配色方案得出答案）。
    """
    try:
        from spyder.config.gui import is_dark_interface
        return "dark" if is_dark_interface() else "light"
    except Exception:
        pass
    try:
        from spyder.config.manager import CONF
        ui_theme = CONF.get("appearance", "ui_theme")
        if ui_theme in ("dark", "light"):
            return ui_theme
        if ui_theme == "automatic":
            from spyder.config.gui import is_dark_font_color
            scheme = CONF.get("appearance", "selected")
            return "dark" if not is_dark_font_color(scheme) else "light"
    except Exception:
        pass
    return None


def _qdarkstyle_palette(dark):
    """取 qdarkstyle 的调色板类（Spyder 界面主题的真实来源）。"""
    try:
        if dark:
            from qdarkstyle.dark.palette import DarkPalette
            return DarkPalette
        from qdarkstyle.light.palette import LightPalette
        return LightPalette
    except Exception:
        return None


def detect_dark():
    """判断宿主 IDE 当前是亮色还是暗色。"""
    mode = _spyder_interface()
    if mode is not None:
        return mode == "dark"
    try:
        app = QApplication.instance()
        if app is None:
            return True
        # 宿主可能只是挂了 qdarkstyle 样式表（不设调色板），从样式表里认。
        css = (app.styleSheet() or "").lower()
        if _HOST_BG_DARK in css and _HOST_BG_LIGHT not in css:
            return True
        if _HOST_BG_LIGHT in css and _HOST_BG_DARK not in css:
            return False
        from qtpy.QtGui import QPalette
        c = app.palette().color(QPalette.Window)
        return _luminance(c) < 0.22
    except Exception:
        return True


def host_seed():
    """读取宿主 IDE 的底色 / 正文色 / 高亮色，作为配色种子。

    在 Spyder 里优先读 qdarkstyle 调色板（那才是界面主题的真实来源），
    而不是 QApplication 的 palette——后者在 Spyder 里根本没被改过。
    """
    mode = _spyder_interface()
    if mode is not None:
        pal = _qdarkstyle_palette(mode == "dark")
        if pal is not None:
            try:
                return (
                    str(pal.COLOR_BACKGROUND_1).lower(),
                    str(pal.COLOR_TEXT_1).lower(),
                    str(pal.COLOR_ACCENT_3).lower(),
                )
            except Exception:
                pass
        return (_HOST_BG_DARK, _HOST_TEXT_DARK, "#1a72bb") if mode == "dark" \
            else (_HOST_BG_LIGHT, _HOST_TEXT_LIGHT, "#1a72bb")
    try:
        app = QApplication.instance()
        if app is None:
            return None
        from qtpy.QtGui import QPalette
        pal = app.palette()
        bg = pal.color(QPalette.Window)
        if not bg.isValid():
            return None
        base = pal.color(QPalette.Base)
        text = pal.color(QPalette.Text)
        # 内容区底板（编辑器底色）通常就是对话流该有的底色，优先采用。
        if (base.isValid() and contrast(base.name(), bg.name()) < 1.4
                and contrast(text.name(), base.name()) >= 4.5):
            bg = base
        if text.isValid() and contrast(text.name(), bg.name()) < 4.5:
            text = None
        highlight = pal.color(QPalette.Highlight)
        if not highlight.isValid():
            highlight = None
        return bg.name(), (text.name() if text else None), (
            highlight.name() if highlight else None)
    except Exception:
        return None


def _hue_gap(color_a, color_b):
    """两色的色相角距离（0~180）。"""
    ha, hb = QColor(color_a).hueF() * 360, QColor(color_b).hueF() * 360
    if ha < 0 or hb < 0:
        return 0.0
    d = abs(ha - hb) % 360
    return min(d, 360 - d)


def resolve_theme(preference="auto", accent=DEFAULT_ACCENT, density="comfortable",
                  ui_font=13, body_font=14, font_family=""):
    """把用户偏好解析成一个 Theme 实例。

    preference="auto" 时完整跟随宿主：底色取 IDE 底板，正文取 IDE 文字色，
    中性色阶由底色重新派生——换 Spyder 主题，面板跟着换，不会突兀。
    """
    dark = detect_dark() if preference == "auto" else (preference != "light")
    tokens = DARK if dark else LIGHT
    accent_hex = None
    if preference == "auto":
        seed = host_seed()
        if seed:
            bg, text, highlight = seed
            base_tokens = DARK if dark else LIGHT
            if contrast(bg, base_tokens["bg"]) > 1.02 or text:
                tokens = _tokens("dark" if dark else "light",
                                 bg, text or base_tokens["text"])
            # 用户显式选了别的强调色就尊重选择；只有"跟随 IDE"时才看高亮色。
            if highlight and accent == DEFAULT_ACCENT:
                preset = ACCENTS[DEFAULT_ACCENT][1 if dark else 2]
                if (_hue_gap(highlight, preset) > 25
                        and QColor(highlight).saturationF() >= 0.18):
                    # 宿主用了非常规色相（自定义主题），跟着它，并推到可读。
                    accent_hex = _readable(highlight, bg, 4.5, dark)
    return Theme(tokens, accent, density, ui_font, body_font,
                 accent_hex=accent_hex, font_family=font_family)


# 当前主题（模块级单例）：供 HTML 渲染器 / diff 预览读取颜色。
_CURRENT = resolve_theme()


def current_theme():
    return _CURRENT


def html_colors():
    """给内联 HTML（QTextEdit 不继承 QSS）用的语义色字典。"""
    return _CURRENT.tokens


def set_current_theme(theme):
    global _CURRENT
    _CURRENT = theme


def recolor_html(html_text, old_palette, new_palette=None):
    """把已生成的内联 HTML 从旧主题色映射到新主题色。

    QTextEdit 里的 rich text 不继承 QSS，主题切换后需要重绘。
    直接对颜色字面量做替换，比重新跑一遍 dry-run diff 更安全
    （文件在执行后可能已改变，重算会显示错误的差异）。
    """
    new = new_palette or html_colors()
    pairs = []
    for key, val in old_palette.items():
        nv = new.get(key)
        if isinstance(val, str) and isinstance(nv, str) and val != nv:
            pairs.append((val, nv))
    # 长色值优先，避免 #fff 之类的短串误伤 6 位十六进制
    pairs.sort(key=lambda p: len(p[0]), reverse=True)
    # 两趟替换：先落成占位符再换新色。一趟替换会「接力」——深色主题的
    # 背景 #19232d 换成 #fafafa 之后，浅色主题的正文色 #19232d 又会命中
    # 同一条规则，把刚换好的背景再改回去。
    staged = []
    for index, (old, new_val) in enumerate(pairs):
        token = "\x01%d\x01" % index
        if old in html_text:
            html_text = html_text.replace(old, token)
            staged.append((token, new_val))
    for token, new_val in staged:
        html_text = html_text.replace(token, new_val)
    return html_text


# ── 矢量线性图标 ──────────────────────────────────────────────────────
# 24×24 坐标系；每个图标是一组绘制命令，统一 1.7px 圆头描边。
# 用 3× 超采样位图缓存，不依赖 QtSvg / qtawesome / 系统 emoji 字体。

_ICONS = {
    "plus":      [("line", (12, 5, 12, 19)), ("line", (5, 12, 19, 12))],
    "clock":     [("circle", (12, 12, 8)), ("poly", [(12, 7), (12, 12), (16, 14)])],
    "clip":      [("poly", [(9, 14), (7, 17), (7, 19), (17, 19), (17, 9),
                             (15, 7), (9, 7), (9, 15), (15, 15)])],
    "diff":      [("line", (4, 8, 20, 8)), ("line", (4, 16, 20, 16)),
                  ("line", (8, 5, 8, 11)), ("line", (14, 13, 20, 13))],
    "chat":      [("poly", [(4, 6), (20, 6), (20, 16), (10, 16), (5, 20),
                             (5, 16), (4, 16), (4, 6)]),
                  ("line", (8, 11, 16, 11))],
    "shield":    [("poly", [(12, 3), (19, 6), (19, 12), (12, 21), (5, 12),
                             (5, 6), (12, 3)]), ("poly", [(9, 12), (11, 14), (15, 10)])],
    "map":       [("poly", [(5, 4), (10, 4), (10, 20), (5, 20), (5, 4)]),
                  ("line", (10, 9, 14, 9)), ("line", (10, 15, 19, 15)),
                  ("poly", [(14, 6), (19, 6), (19, 12), (14, 12), (14, 6)])],
    "settings":  [("circle", (12, 12, 3)),
                  ("poly", [(12, 3), (12, 6), (12, 18), (12, 21)]),
                  ("poly", [(3, 12), (6, 12), (18, 12), (21, 12)]),
                  ("poly", [(5.6, 5.6), (7.7, 7.7), (16.3, 16.3), (18.4, 18.4)]),
                  ("poly", [(18.4, 5.6), (16.3, 7.7), (7.7, 16.3), (5.6, 18.4)])],
    "more":      [("dot", (6, 12, 1.5)), ("dot", (12, 12, 1.5)), ("dot", (18, 12, 1.5))],
    # 字母 A：字号入口。比放大镜/齿轮更像"字体"，也不和设置按钮撞脸。
    "font":      [("poly", [(6, 19), (12, 5), (18, 19)]),
                  ("line", (8.4, 14, 15.6, 14))],
    "send":      [("poly", [(5, 12), (19, 5), (15, 19), (12, 14), (5, 12)]),
                  ("line", (12, 14, 19, 5))],
    "stop":      [("rect", (6, 6, 12, 12, 2))],
    "chevron":   [("poly", [(7, 10), (12, 15), (17, 10)])],
    "copy":      [("rect", (9, 9, 11, 11, 2)),
                  ("poly", [(15, 9), (15, 6), (6, 6), (6, 15), (9, 15)])],
    "insert":    [("line", (12, 4, 12, 20)), ("poly", [(8, 15), (12, 19), (16, 15)])],
    "retry":     [("poly", [(19, 10), (19, 6), (15, 6)]),
                  ("arc", (5, 5, 14, 14, 40, 280))],
    "check":     [("poly", [(5, 13), (10, 18), (19, 7)])],
    "cross":     [("line", (6, 6, 18, 18)), ("line", (18, 6, 6, 18))],
    "undo":      [("poly", [(9, 8), (5, 12), (9, 16)]),
                  ("arc", (8, 6, 12, 12, 145, 270))],
    "spark":     [("poly", [(12, 3), (14, 10), (21, 12), (14, 14),
                             (12, 21), (10, 14), (3, 12), (10, 10), (12, 3)])],
    "folder":    [("poly", [(3, 7), (9, 7), (11, 9), (21, 9),
                             (21, 19), (3, 19), (3, 7)])],
    "close":     [("line", (6, 6, 18, 18)), ("line", (18, 6, 6, 18))],
    "eye":       [("poly", [(3, 12), (8, 7), (16, 7), (21, 12),
                             (16, 17), (8, 17), (3, 12)]),
                  ("circle", (12, 12, 2.5))],
    "terminal":  [("rect", (3, 5, 18, 14, 2)),
                  ("poly", [(7, 10), (10, 12), (7, 14)]),
                  ("line", (12, 15, 16, 15))],
    "file":      [("poly", [(6, 3), (14, 3), (18, 7), (18, 21),
                             (6, 21), (6, 3)]),
                  ("poly", [(14, 3), (14, 7), (18, 7)])],
    "alert":     [("poly", [(12, 4), (21, 19), (3, 19), (12, 4)]),
                  ("line", (12, 10, 12, 14)), ("dot", (12, 16.5, 1))],
    "info":      [("circle", (12, 12, 9)), ("line", (12, 11, 12, 16)),
                  ("dot", (12, 8, 1))],
    # ── v0.7.8：任务面板 / 胶囊用的几何图标 ──
    # 下拉箭头：比字符 "▾" 稳定（字符在不同字体里粗细、基线都不一样，
    # 字号一调就顶边或离字太远）
    "chevron_right": [("poly", [(10, 7), (15, 12), (10, 17)])],
    "chevron_up": [("poly", [(7, 14), (12, 9), (17, 14)])],
    "arrow_up":  [("line", (12, 19, 12, 5)),
                  ("poly", [(6, 11), (12, 5), (18, 11)])],
    # 任务清单：三条带圆头的横线 + 行首小点
    "list":      [("dot", (5, 7, 1.1)), ("line", (9, 7, 20, 7)),
                  ("dot", (5, 12, 1.1)), ("line", (9, 12, 20, 12)),
                  ("dot", (5, 17, 1.1)), ("line", (9, 17, 16, 17))],
    "circle":    [("circle", (12, 12, 9))],
    # 完成态：实心圆 + 背景色的钩（Cline / GitHub 的 check-badge 形态）。
    # 双色要两个画笔，见 make_icon 的 hole 参数。
    "check_circle": [("fill_circle", (12, 12, 9)),
                     ("poly_hole", [(7.9, 12.3), (10.8, 15.2), (16.1, 9.3)])],
    # 进行中：3/4 圆环 + 内点 —— 缺口让它读起来像"转到一半"，而不是"未开始"。
    # 描边加粗到 2.4：完成态是实心块，同粗的细环在旁边会显得"变小了"
    "progress":  [("arc", (3.0, 3.0, 18.0, 18.0, 90, -270), 2.6),
                  ("fill_circle", (12, 12, 4.2))],
    "minus":     [("line", (5, 12, 19, 12))],
    # 闪电：Act 模式的标记（terminal 在 15px 下全是细节，糊成一团小方块）
    "bolt":      [("poly", [(13, 3), (6, 13), (11.5, 13), (10.5, 21),
                             (18, 10.5), (12.5, 10.5), (13, 3)])],
    "save":      [("poly", [(6, 4), (16, 4), (20, 8), (20, 20), (4, 20),
                             (4, 4), (6, 4)]),
                  ("poly", [(8, 4), (8, 9), (15, 9), (15, 4)]),
                  ("poly", [(8, 20), (8, 13), (16, 13), (16, 20)])],
}

_ICON_CACHE = {}
_SUPERSAMPLE = 3


def make_icon(name, color=None, size=24, hole=None):
    """返回一个单色线性图标；按 (name, color, hole) 缓存。

    ``hole`` 是"挖空"色：``check_circle`` 这类实心徽章需要在实心圆上画一个
    底色钩子。传 None 时用当前主题的底色，所以在任何背景上都像镂空。
    """
    t = current_theme()
    color = color or t["text_muted"]
    hole = hole or t["bg"]
    key = (name, color, size, hole)
    if key in _ICON_CACHE:
        return _ICON_CACHE[key]
    s = _SUPERSAMPLE
    img = QImage(24 * s, 24 * s, QImage.Format_ARGB32)
    img.fill(QColor(0, 0, 0, 0))
    painter = QPainter(img)
    painter.setRenderHint(QPainter.Antialiasing, True)
    painter.scale(s, s)
    pen = QPen(QColor(color))
    pen.setWidthF(1.7)
    pen.setCapStyle(Qt.RoundCap)
    pen.setJoinStyle(Qt.RoundJoin)
    hole_pen = QPen(QColor(hole))
    hole_pen.setWidthF(2.1)
    hole_pen.setCapStyle(Qt.RoundCap)
    hole_pen.setJoinStyle(Qt.RoundJoin)
    brush = QBrush(QColor(color))

    def _ellipse(cx, cy, r):
        return QRectF(float(cx - r), float(cy - r),
                      float(r * 2), float(r * 2))

    for command in _ICONS.get(name, []):
        # 命令可带第三个元素 = 该笔画的线宽，缺省用 1.7
        kind, data = command[0], command[1]
        if len(command) > 2:
            wide = QPen(pen)
            wide.setWidthF(float(command[2]))
            painter.setPen(wide)
        if kind == "line":
            painter.setPen(pen if len(command) <= 2 else wide)
            x1, y1, x2, y2 = data
            painter.drawLine(QLineF(float(x1), float(y1),
                                    float(x2), float(y2)))
        elif kind == "poly":
            painter.setPen(pen if len(command) <= 2 else wide)
            painter.drawPolyline(QPolygonF([QPointF(x, y) for x, y in data]))
        elif kind == "poly_hole":
            painter.setPen(hole_pen)
            painter.drawPolyline(QPolygonF([QPointF(x, y) for x, y in data]))
        elif kind == "fill_circle":
            cx, cy, r = data
            painter.setPen(Qt.NoPen)
            painter.setBrush(brush)
            painter.drawEllipse(_ellipse(cx, cy, r))
        elif kind in ("circle", "dot"):
            painter.setPen(pen if len(command) <= 2 else wide)
            painter.setBrush(Qt.NoBrush)
            cx, cy, r = data
            painter.drawEllipse(_ellipse(cx, cy, r))
        elif kind == "rect":
            painter.setPen(pen if len(command) <= 2 else wide)
            painter.setBrush(Qt.NoBrush)
            x, y, w, h, r = data
            painter.drawRoundedRect(QRectF(float(x), float(y),
                                           float(w), float(h)),
                                    float(r), float(r))
        elif kind == "arc":
            painter.setPen(pen if len(command) <= 2 else wide)
            painter.setBrush(Qt.NoBrush)
            x, y, w, h, start, span = data
            painter.drawArc(QRectF(float(x), float(y),
                                   float(w), float(h)),
                            int(start * 16), int(span * 16))
    painter.end()
    pix = QPixmap.fromImage(img)
    pix.setDevicePixelRatio(s)
    icon = QIcon(pix)
    _ICON_CACHE[key] = icon
    return icon


# ── 组件工厂 ──────────────────────────────────────────────────────────

def set_prop(widget, name, value):
    """设置 Qt 动态属性并重新 polish，让 QSS 属性选择器立即生效。"""
    widget.setProperty(name, value)
    widget.style().unpolish(widget)
    widget.style().polish(widget)
    return widget


def polish(widget):
    widget.style().unpolish(widget)
    widget.style().polish(widget)
    return widget


def named(widget, object_name):
    widget.setObjectName(object_name)
    return widget


def icon_button(name, tooltip="", size=28, object_name="iconBtn"):
    btn = QToolButton()
    named(btn, object_name)
    btn.setIcon(make_icon(name))
    btn.setIconSize(QSize(int(size * 0.62), int(size * 0.62)))
    btn.setFixedSize(size, size)
    btn.setToolTip(tooltip)
    btn.setCursor(Qt.PointingHandCursor)
    btn.setAutoRaise(True)
    return btn


class PillButton(QFrame):
    """图标 + 文案 + 展开箭头组成的现代胶囊按钮。

    为什么不用 QToolButton：一个 QToolButton 只能挂一个图标，想要"左图标 +
    右箭头"就只能往文案后面拼字符 ``▾``（v0.7.5~0.7.7 的做法）。那个字符
    在不同字体里粗细、基线都不一样：Windows 的 Segoe UI 里它偏上，CJK 字体
    里它偏下，字号一调（用户现在用 18px）就贴着胶囊上边，看起来像没对齐的
    方块。这里改成三个 QLabel 拼装——图标、文字、箭头各自定尺寸，间距和
    颜色都按主题算，hover 时整颗胶囊一起变色。

    交互与 QToolButton 对齐：``clicked`` 无参信号、可聚焦、空格/回车可触发，
    外部照旧 ``btn.clicked.connect(...)``、``theme.set_prop(btn, "tone", ...)``。
    """

    clicked = Signal()

    def __init__(self, text="", icon_name=None, tone="plain", parent=None):
        super().__init__(parent)
        named(self, "pill")
        self.setProperty("tone", tone)
        self._text = text or ""
        self._icon_name = icon_name
        self._tone = tone
        self._elided = self._text
        self.setCursor(Qt.PointingHandCursor)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Fixed)

        t = current_theme()
        self._gap = max(4, (t.ui_font + 2) // 4)      # 14px 时 = 4，18px 时 = 5
        self._icon_size = max(13, t.ui_font - 3)      # 18px 字号 → 15px 图标
        self._chev_size = max(11, t.ui_font - 6)

        lay = QHBoxLayout(self)
        lay.setContentsMargins(max(7, t.ui_font // 2), 0,
                               max(5, t.ui_font // 3), 0)
        lay.setSpacing(self._gap)
        lay.setSizeConstraint(QHBoxLayout.SetMinimumSize)

        self._icon = QLabel(self)
        self._icon.setFixedSize(self._icon_size, self._icon_size)
        self._icon.setStyleSheet("background: transparent; border: none;")
        self._label = QLabel(self._text, self)
        self._label.setStyleSheet("background: transparent; border: none;")
        self._chev = QLabel(self)
        self._chev.setFixedSize(self._chev_size, self._chev_size)
        self._chev.setStyleSheet("background: transparent; border: none;")
        for child in (self._icon, self._label, self._chev):
            child.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        lay.addWidget(self._icon)
        lay.addWidget(self._label)
        lay.addWidget(self._chev)

        self._style_children()

    # ── 主题 ──
    def _palette(self):
        """(图标色, 文字色, 箭头色, 字重) —— 按 tone 取。"""
        t = current_theme()
        tone = self._tone
        if tone == "act":
            return t["accent"], t["accent"], t["accent"], 600
        if tone == "plan":
            return t["info"], t["info"], t["info"], 600
        return t["text_muted"], t["text_muted"], t["text_faint"], 500

    def _style_children(self):
        t = current_theme()
        icon_color, text_color, chev_color, weight = self._palette()
        size = max(11, t.ui_font - 1)
        self._label.setStyleSheet(
            "background: transparent; border: none;"
            f"color: {text_color}; font-size: {size}px;"
            f"font-weight: {weight};")
        if self._icon_name:
            pix = make_icon(self._icon_name, icon_color,
                            self._icon_size).pixmap(
                                QSize(self._icon_size, self._icon_size))
            self._icon.setPixmap(pix)
            self._icon.setVisible(True)
        else:
            self._icon.setVisible(False)
        self._chev.setPixmap(make_icon(
            "chevron", chev_color, self._chev_size).pixmap(
                QSize(self._chev_size, self._chev_size)))
        self._apply_elide()

    def refresh_theme(self):
        """字号 / 配色改了之后重算图标与文字，保持胶囊高度随之变化。"""
        t = current_theme()
        self._gap = max(4, (t.ui_font + 2) // 4)
        self._icon_size = max(13, t.ui_font - 3)
        self._chev_size = max(11, t.ui_font - 6)
        self._icon.setFixedSize(self._icon_size, self._icon_size)
        self._chev.setFixedSize(self._chev_size, self._chev_size)
        lay = self.layout()
        lay.setSpacing(self._gap)
        lay.setContentsMargins(max(7, t.ui_font // 2), 0,
                               max(5, t.ui_font // 3), 0)
        self._style_children()
        self.updateGeometry()

    # ── 内容 ──
    def text(self):
        return self._text

    def setText(self, text):
        self._text = text or ""
        self._apply_elide()

    def set_icon(self, name):
        self._icon_name = name
        self._style_children()

    def set_tone(self, tone):
        self._tone = tone
        set_prop(self, "tone", tone)
        self._style_children()

    def _apply_elide(self):
        """宽度被压窄时按当前可用宽度省略，而不是让文字糊出边界。"""
        avail = (self.width() - self._icon_size - self._chev_size
                 - self._gap * 2
                 - self.layout().contentsMargins().left()
                 - self.layout().contentsMargins().right())
        if avail <= 20 or self.width() <= 0:
            self._label.setText(self._text)
            return
        shown = QFontMetrics(self._label.font()).elidedText(
            self._text, Qt.ElideRight, avail)
        if shown != self._label.text():
            self._label.setText(shown)
            self._elided = shown

    def resizeEvent(self, event):  # noqa: N802 - Qt 接口
        super().resizeEvent(event)
        self._apply_elide()

    # ── 交互 ──
    def mouseReleaseEvent(self, event):  # noqa: N802 - Qt 接口
        if event.button() == Qt.LeftButton and self.rect().contains(
                event.pos()):
            self.clicked.emit()
        super().mouseReleaseEvent(event)

    def keyPressEvent(self, event):  # noqa: N802 - Qt 接口
        if event.key() in (Qt.Key_Space, Qt.Key_Return, Qt.Key_Enter):
            self.clicked.emit()
            event.accept()
            return
        super().keyPressEvent(event)


def pill(text="", icon_name=None, tone="plain"):
    return PillButton(text, icon_name, tone)


def push_button(text, kind="ghost"):
    btn = QPushButton(text)
    set_prop(btn, "kind", kind)
    btn.setCursor(Qt.PointingHandCursor)
    return btn


def badge(text, kind="neutral"):
    lbl = QLabel(text)
    named(lbl, "badge")
    set_prop(lbl, "kind", kind)
    return lbl


def chip(text, icon_name=None):
    """文本胶囊标签。icon_name 预留给未来带图标的 chip。"""
    lbl = QLabel(text)
    named(lbl, "chip")
    return lbl


def muted_label(text, name="mutedLabel"):
    lbl = QLabel(text)
    named(lbl, name)
    return lbl


def card(object_name="card"):
    f = QFrame()
    named(f, object_name)
    f.setFrameShape(QFrame.NoFrame)
    return f


# ── QSS ───────────────────────────────────────────────────────────────

def build_qss(t=None, include_root=True):
    """生成整套 QSS。所有颜色来自语义 token，字号 / 间距来自设置。"""
    t = t or current_theme()
    m = t.metrics
    ui = t.ui_font
    body = t.body_font
    xs = max(9, ui - 2)
    sm = max(10, ui - 1)
    radius = m["radius"]
    pad = m["pad"]
    # 胶囊半径：Qt 不会把 999px 夹到"高度的一半"——半径大于控件高度一半时
    # 它直接按直角画（13px 半径套在 18px 高的 chip 上就成了方块）。所以
    # 这里用字号推控件高度，再取一半，保证任何字号下都是真正的胶囊。
    pill = max(6, (xs + 4) // 2)
    pill_tall = max(7, (sm + 8) // 2)

    return f"""
/* ── 宿主样式表隔离 ───────────────────────────────────────
   宿主（Spyder）把 qdarkstyle 的一整套样式表挂在主窗口上，其中
   ``QWidget {{ background-color: #19232d }}``、``QLabel {{ … }}``、
   ``QMenu::item {{ … }}`` 都会漏进本面板：没起名字的 QLabel 会凭空
   变成一条深色矩形，菜单项会莫名变成深色药丸。

   下面的"归零"规则放在最前面：同优先级下后写的规则胜出，所以后面
   所有具名规则（QLabel#modelChip 等）依旧生效，只是把宿主那层默认值
   挡在门外。 */
QWidget {{ background: transparent; }}
QLabel {{
    background: transparent;
    color: {t['text']};
    border: none;
}}
QMenu::item {{ background: transparent; }}
QMenu::separator {{ background: {t['border']}; }}

/* ── 容器 ─────────────────────────────────────────────── */
QWidget#chatRoot, QWidget#standaloneRoot {{
    background: {t['bg']};
    color: {t['text']};
    font-family: {t.font_family};
    font-size: {ui}px;
}}
QWidget#chatHeader {{
    background: {t['surface']};
    border-bottom: 1px solid {t['border']};
}}
QWidget#composerDock {{
    background: {t['bg']};
    border-top: 1px solid {t['border']};
}}
QWidget#streamInner {{ background: {t['bg']}; }}
QScrollArea#stream {{ background: {t['bg']}; border: none; }}
QScrollArea#stream > QWidget > QWidget {{ background: transparent; }}

/* ── 品牌 / 顶栏 ──────────────────────────────────────── */
QLabel#brandMark {{
    color: {t['accent']};
    font-size: {ui + 4}px;
    font-weight: 700;
    background: transparent;
    padding: 0 2px;
}}
QLabel#brandTitle {{
    color: {t['text']};
    font-size: {ui + 1}px;
    font-weight: 600;
    background: transparent;
}}
QLabel#brandSub, QLabel#mutedLabel {{
    color: {t['text_faint']};
    font-size: {xs}px;
    background: transparent;
}}
QLabel#modelChip {{
    color: {t['text_muted']};
    background: {t['surface_alt']};
    border: 1px solid {t['border']};
    border-radius: {pill}px;
    padding: 2px 10px;
    font-size: {xs}px;
}}
QLabel#modelChip[active="true"] {{
    color: {t['accent']};
    border-color: {t['accent']};
}}
/* ── 胶囊（模式 / Agent 选择器）────────────────────────────
   v0.7.8：改由 QFrame + 三个 QLabel 拼装（见 PillButton），箭头是真图标，
   不再是 "▾" 字符。tone 决定配色，与旧 set_prop(.., "mode", ..) 语义一致。 */
QFrame#pill {{
    background: {t['surface_alt']};
    border: 1px solid {t['border']};
    border-radius: {pill_tall}px;
}}
QFrame#pill:hover {{
    background: {t['elevated']};
    border-color: {t['border_strong']};
}}
QFrame#pill:focus {{ border-color: {t['accent']}; }}
QFrame#pill[tone="act"] {{
    background: {t['accent_soft']};
    border-color: {t['accent']};
}}
QFrame#pill[tone="plan"] {{
    background: {t['info_soft']};
    border-color: {t['info']};
}}

/* ── 任务面板（v0.7.8：完全交给主题，不再内联深色 QSS）──── */
QWidget#todoDock, QWidget#todoItems {{ background: transparent; }}
QFrame#todoPanel {{
    background: {t['surface']};
    border: 1px solid {t['border']};
    border-radius: {radius}px;
}}
QLabel#todoTitle {{
    color: {t['text']};
    font-size: {ui}px;
    font-weight: 600;
    background: transparent;
}}
QLabel#todoCount {{
    color: {t['text_muted']};
    background: {t['surface_alt']};
    border: 1px solid {t['border']};
    border-radius: {pill}px;
    padding: 1px 8px;
    font-size: {xs}px;
    font-weight: 600;
}}
QLabel#todoItem {{
    color: {t['text_muted']};
    font-size: {max(11, ui - 1)}px;
    background: transparent;
}}
QLabel#todoItem[status="in-progress"] {{
    color: {t['text']};
    font-weight: 600;
}}
QLabel#todoItem[status="completed"] {{ color: {t['text_muted']}; }}
QLabel#todoItem[status="cancelled"] {{
    color: {t['text_faint']};
    text-decoration: line-through;
}}
QProgressBar#todoBar {{
    background: {t['border_strong']};
    border: none;
    border-radius: 2px;
}}
QProgressBar#todoBar::chunk {{
    background: {t['accent']};
    border-radius: 2px;
}}

/* ── 通用按钮 / 图标 ──────────────────────────────────── */
QToolButton#iconBtn {{
    background: transparent;
    border: none;
    border-radius: 7px;
    padding: 0;
}}
QToolButton#iconBtn:hover {{ background: {t['surface_alt']}; }}
QToolButton#iconBtn:pressed {{ background: {t['elevated']}; }}
QToolButton#iconBtn[active="true"] {{
    background: {t['accent_soft']};
    border: 1px solid {t['accent']};
}}
QPushButton {{
    font-family: {t.font_family};
    font-size: {sm}px;
    border-radius: {max(5, radius - 3)}px;
    padding: 5px 12px;
    background: {t['surface_alt']};
    color: {t['text']};
    border: 1px solid {t['border']};
}}
QPushButton:hover {{ border-color: {t['border_strong']}; }}
QPushButton[kind="primary"] {{
    background: {t['accent_fill']};
    color: {t['on_accent']};
    border: none;
    font-weight: 600;
    padding: 6px 16px;
}}
QPushButton[kind="primary"]:hover {{ background: {t['accent']}; }}
QPushButton[kind="primary"]:disabled {{
    background: {t['surface_alt']}; color: {t['text_faint']};
}}
/* v0.7.8：发送按钮做成胶囊 + 箭头图标，和左边的模式/Agent 胶囊同一套圆角，
   高度也由字号推出来，字号调到 18px 时不会变成一颗又扁又宽的小砖头。 */
QPushButton#sendButton {{
    border-radius: {pill_tall}px;
    padding: 0 15px;
    min-height: {max(26, ui + 12)}px;
}}
QPushButton[kind="ghost"] {{
    background: transparent; border: 1px solid {t['border']};
    color: {t['text_muted']};
}}
QPushButton[kind="ghost"]:hover {{
    color: {t['text']}; border-color: {t['border_strong']};
}}
QPushButton[kind="subtle"] {{
    background: {t['surface_alt']}; border: 1px solid transparent;
    color: {t['text_muted']};
}}
QPushButton[kind="danger"] {{
    background: {t['danger_soft']}; color: {t['danger']};
    border: 1px solid {t['danger']};
}}
QPushButton[kind="success"] {{
    background: {t['success_soft']}; color: {t['success']};
    border: 1px solid {t['success']};
}}
QPushButton:disabled {{ color: {t['text_faint']}; }}
/* 空状态里的"起手式"：安静的一行，hover 才亮起来 */
QPushButton#suggestion {{
    background: transparent;
    color: {t['text_muted']};
    border: 1px solid {t['border']};
    border-radius: {max(5, radius - 3)}px;
    padding: 5px 12px;
    font-size: {sm}px;
    text-align: left;
}}
QPushButton#suggestion:hover {{
    color: {t['accent']};
    border-color: {t['accent']};
    background: {t['accent_soft']};
}}
/* 用量行里的"恢复任务"：必须和 12px 的小字同高，否则会把整条状态行撑起来，
   在面板底部糊成一块灰砖（0.6.1 的实拍就是这样）。 */
QPushButton#linkButton {{
    background: transparent;
    color: {t['accent']};
    border: 1px solid {t['border']};
    border-radius: {pill}px;
    padding: 0 8px;
    font-size: {xs}px;
    font-weight: 500;
}}
QPushButton#linkButton:hover {{
    border-color: {t['accent']};
    background: {t['accent_soft']};
}}

/* ── 徽章 / 标签 / 提示 ───────────────────────────────── */
QLabel#badge {{
    border-radius: {pill}px;
    padding: 1px 8px;
    font-size: {xs}px;
    font-weight: 600;
    background: {t['surface_alt']};
    color: {t['text_muted']};
    border: 1px solid {t['border']};
}}
QLabel#badge[kind="danger"] {{
    background: {t['danger_soft']}; color: {t['danger']};
    border-color: {t['danger']};
}}
QLabel#badge[kind="warning"], QLabel#badge[kind="execute"] {{
    background: {t['warning_soft']}; color: {t['warning']};
    border-color: {t['warning']};
}}
QLabel#badge[kind="write"] {{
    background: {t['info_soft']}; color: {t['info']};
    border-color: {t['info']};
}}
QLabel#badge[kind="success"], QLabel#badge[kind="read"] {{
    background: {t['success_soft']}; color: {t['success']};
    border-color: {t['success']};
}}
QLabel#chip {{
    color: {t['text_muted']};
    background: {t['surface_alt']};
    border: 1px solid {t['border']};
    border-radius: {pill}px;
    padding: 1px 9px;
    font-size: {xs}px;
}}
/* 编辑器上下文胶囊：可点击切换附加方式 */
QLabel#editorChip {{
    color: {t['text_muted']};
    background: {t['surface_alt']};
    border: 1px solid {t['border']};
    border-radius: {pill}px;
    padding: 1px 9px;
    font-size: {xs}px;
}}
QLabel#editorChip:hover {{
    color: {t['text']};
    border-color: {t['border_strong']};
    background: {t['elevated']};
}}
QFrame#card, QFrame#welcomeCard {{
    background: {t['surface']};
    border: 1px solid {t['border']};
    border-radius: {radius}px;
}}
QFrame#welcomeCard {{ background: {t['surface']}; }}
QLabel#welcomeTitle {{
    color: {t['text']}; font-size: {body + 3}px; font-weight: 600;
    background: transparent;
}}
QLabel#welcomeBody {{
    color: {t['text_muted']}; font-size: {sm}px; background: transparent;
}}
QLabel#sectionLabel {{
    color: {t['text_faint']}; font-size: {xs}px; font-weight: 600;
    background: transparent;
}}

/* ── 消息气泡 ─────────────────────────────────────────── */
QFrame#userBubble {{
    background: {t['surface_alt']};
    border: 1px solid {t['border']};
    border-left: 3px solid {t['accent']};
    border-radius: {radius}px;
}}
QFrame#assistantBubble {{ background: transparent; border: none; }}
QFrame#toolBubble {{
    background: {t['surface']};
    border: 1px solid {t['border']};
    border-radius: {max(6, radius - 2)}px;
}}
QLabel#roleLabel {{
    color: {t['text_muted']};
    font-size: {xs}px;
    font-weight: 600;
    background: transparent;
}}
QLabel#roleLabel[role="assistant"] {{ color: {t['accent']}; }}
QLabel#timeLabel {{
    color: {t['text_faint']}; font-size: {xs}px; background: transparent;
}}
QTextEdit#messageBody {{
    background: transparent;
    border: none;
    color: {t['text']};
    font-family: {t.font_family};
    font-size: {body}px;
    selection-background-color: {t['selection']};
    padding: 0;
}}
QTextEdit#diffView {{
    background: {t['code_bg']};
    border: 1px solid {t['border']};
    border-radius: {max(5, radius - 4)}px;
    color: {t['text']};
    font-family: {_FONT_MONO};
    font-size: {max(10, ui - 1)}px;
    padding: 4px;
}}

/* ── 审批卡 ───────────────────────────────────────────── */
QFrame#approvalCard {{
    background: {t['surface']};
    border: 1px solid {t['border_strong']};
    border-left: 3px solid {t['accent']};
    border-radius: {radius}px;
}}
QFrame#approvalCard[state="done"] {{ border-color: {t['border']}; }}
QFrame#approvalRow {{
    background: {t['surface_alt']};
    border: 1px solid {t['border']};
    border-radius: {max(6, radius - 3)}px;
}}
QFrame#approvalRow[risk="danger"] {{ border-color: {t['danger']}; }}
QLabel#toolTitle {{
    color: {t['text']}; font-size: {sm}px; font-weight: 600;
    background: transparent;
}}
QLabel#approvalTitle {{
    color: {t['accent']}; font-size: {sm + 1}px; font-weight: 600;
    background: transparent;
}}
QLabel#approvalTitle[state="done"] {{ color: {t['text_muted']}; }}
QLabel#approvalPath {{
    color: {t['text_faint']}; font-size: {xs}px;
    font-family: {_FONT_MONO}; background: transparent;
}}
QLabel#stateLabel {{ color: {t['warning']}; font-size: {xs}px; background: transparent; }}
QLabel#stateLabel[state="approved"] {{ color: {t['success']}; }}
QLabel#stateLabel[state="rejected"] {{ color: {t['danger']}; }}
QLabel#stateLabel[state="done"] {{ color: {t['text_faint']}; }}
QLabel#hintLabel {{
    color: {t['text_faint']}; font-size: {xs}px; background: transparent;
}}
/* 审批快捷条（v0.8.5）：固定在输入框上方，滚到哪里都在。 */
QFrame#approvalBar {{
    background: {t['warning_soft']};
    border: 1px solid {t['warning']};
    border-radius: {max(6, radius - 2)}px;
}}
QLabel#approvalBarText {{
    color: {t['warning']}; font-size: {sm}px; font-weight: 600;
    background: transparent;
}}
QLabel#approvalBarText[state="danger"] {{ color: {t['danger']}; }}

/* ── Composer ─────────────────────────────────────────── */
QFrame#composer {{
    background: {t['surface']};
    border: 1px solid {t['border_strong']};
    border-radius: {radius + 2}px;
}}
QFrame#composer[focused="true"] {{ border-color: {t['accent']}; }}
QPlainTextEdit#composerInput {{
    background: transparent;
    border: none;
    color: {t['text']};
    font-family: {t.font_family};
    font-size: {body}px;
    selection-background-color: {t['selection']};
    padding: 0;
}}
QWidget#composerControls {{ background: transparent; }}
QWidget#composerMeter {{ background: transparent; }}
QComboBox#modelPicker, QComboBox#agentPicker {{
    background: {t['surface_alt']};
    color: {t['text_muted']};
    border: 1px solid {t['border']};
    border-radius: {max(5, radius - 3)}px;
    padding: 2px 8px;
    font-size: {xs}px;
}}
QComboBox#modelPicker:hover, QComboBox#agentPicker:hover {{
    border-color: {t['border_strong']};
}}
QComboBox::drop-down {{ border: none; width: 14px; }}
QComboBox QAbstractItemView {{
    background: {t['elevated']};
    color: {t['text']};
    border: 1px solid {t['border']};
    selection-background-color: {t['accent_soft']};
    selection-color: {t['text']};
    font-size: {sm}px;
    outline: none;
}}
QProgressBar#ctxMeter {{
    background: {t['surface_alt']};
    border: none;
    border-radius: 3px;
}}
QProgressBar#ctxMeter::chunk {{
    background: {t['accent']};
    border-radius: 3px;
}}
QProgressBar#thinkingBar {{ background: transparent; border: none; }}
QProgressBar#thinkingBar::chunk {{ background: {t['accent']}; }}
QLabel#statusLabel {{
    color: {t['accent']}; font-size: {xs}px; background: transparent;
}}
QLabel#usageLabel {{
    color: {t['text_faint']}; font-size: {xs}px; background: transparent;
}}

/* ── 下拉 / 弹出列表 ──────────────────────────────────── */
QFrame#popupFrame {{
    background: {t['elevated']};
    border: 1px solid {t['border_strong']};
    border-radius: {radius}px;
}}
QListWidget#popupList {{
    background: transparent;
    color: {t['text']};
    border: none;
    font-size: {sm}px;
    outline: none;
}}
QListWidget#popupList::item {{ padding: 6px 12px; border-radius: 5px; }}
QListWidget#popupList::item:selected {{
    background: {t['accent_soft']}; color: {t['text']};
}}
QMenu, QMenu#overflowMenu {{
    background: {t['elevated']};
    color: {t['text']};
    border: 1px solid {t['border_strong']};
    border-radius: {max(7, radius - 2)}px;
    padding: 4px;
    font-size: {sm}px;
}}
QMenu::item, QMenu#overflowMenu::item {{
    background: transparent;
    color: {t['text']};
    /* 左侧 30px 留给图标，否则图标和文字会贴在一起 */
    padding: 6px 20px 6px 30px;
    border-radius: {max(5, radius - 4)}px;
    margin: 0 2px;
}}
QMenu::item:selected, QMenu#overflowMenu::item:selected {{
    background: {t['accent_soft']};
    color: {t['accent']};
}}
QMenu::item:disabled {{ color: {t['text_faint']}; }}
QMenu::separator, QMenu#overflowMenu::separator {{
    height: 1px;
    background: {t['border']};
    margin: 4px 10px;
}}
QMenu::icon, QMenu#overflowMenu::icon {{
    left: 10px;
    width: 16px;
    height: 16px;
}}

/* ── 滚动条 ───────────────────────────────────────────── */
QScrollBar:vertical {{
    background: transparent; width: 9px; margin: 0 2px 0 0;
}}
QScrollBar::handle:vertical {{
    background: {t['scrollbar']};
    border-radius: 4px; min-height: 32px;
}}
QScrollBar::handle:vertical:hover {{ background: {t['scrollbar_hover']}; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{ background: transparent; }}
QScrollBar:horizontal {{
    background: transparent; height: 9px; margin: 2px 0 0 0;
}}
QScrollBar::handle:horizontal {{
    background: {t['scrollbar']}; border-radius: 4px; min-width: 32px;
}}

/* ── 输入控件（设置页 / 对话框） ──────────────────────── */
QLineEdit, QSpinBox, QDoubleSpinBox, QTextEdit, QPlainTextEdit {{
    background: {t['surface']};
    color: {t['text']};
    border: 1px solid {t['border']};
    border-radius: {max(5, radius - 3)}px;
    padding: 4px 8px;
    selection-background-color: {t['selection']};
    font-family: {t.font_family};
    font-size: {sm}px;
}}
QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus {{
    border-color: {t['accent']};
}}
QTabWidget::pane {{
    border: 1px solid {t['border']};
    border-radius: {radius}px;
    background: {t['surface']};
}}
QTabBar::tab {{
    background: transparent;
    color: {t['text_muted']};
    padding: 7px 15px;
    border-bottom: 2px solid transparent;
    font-size: {sm}px;
}}
QTabBar::tab:selected {{ color: {t['accent']}; border-bottom-color: {t['accent']}; }}
QCheckBox, QRadioButton, QGroupBox {{ color: {t['text']}; font-size: {sm}px; }}
QGroupBox {{
    border: 1px solid {t['border']};
    border-radius: {radius}px;
    margin-top: 14px;
    padding-top: 8px;
}}
QGroupBox::title {{ color: {t['text_muted']}; subcontrol-origin: margin; left: 10px; }}
QToolTip {{
    background: {t['elevated']};
    color: {t['text']};
    border: 1px solid {t['border_strong']};
    border-radius: 5px;
    padding: 4px 7px;
    font-size: {xs}px;
}}
"""


def apply_theme(root, theme=None, standalone=False):
    """把整套 QSS 装到 root（及其所有子控件）上，并登记为当前主题。"""
    t = theme or resolve_theme()
    set_current_theme(t)
    if not root.objectName():
        root.setObjectName("standaloneRoot" if standalone else "chatRoot")
    root.setStyleSheet(build_qss(t))
    polish(root)
    return t


def style_popup(widget, theme=None):
    """弹出窗口 / QMenu 不继承父级 QSS，需单独安装同一套样式。"""
    t = theme or current_theme()
    widget.setStyleSheet(build_qss(t))
    polish(widget)
    return t


# ── 遗留面板适配 ─────────────────────────────────────────────────────
# Todo / 代码审查 / 会话历史 / 内联聊天 / 审计查看器等次要面板仍带着
# v0.4 时代的 VS Code Dark+ 内联 QSS。逐个改写成本高且易回归，这里用
# 颜色映射把它们统一到当前主题：布局与状态语义全部保留，只换色。

_LEGACY_STYLE_MAP = {
    "#1e1e1e": "bg", "#252526": "surface", "#20232a": "surface",
    "#2d2d2d": "surface_alt", "#2d2d30": "surface_alt",
    "#1a1a1a": "code_bg", "#1a1a2e": "info_soft",
    "#3c3c3c": "border_strong", "#333": "border_strong",
    "#383838": "border_strong", "#444": "border_strong",
    "#555": "border_strong", "#5a5a5a": "border_strong",
    "#3a3a3a": "surface_alt", "#424242": "scrollbar",
    "#0e639c": "accent_soft", "#264f78": "selection",
    "#cccccc": "text", "#ddd": "text", "#e0e0e0": "text",
    "#d4d4d4": "text", "#d0d0d0": "text", "#ccc": "text",
    "#fff": "text", "#ffffff": "text",
    "#888": "text_faint", "#8a8a8a": "text_faint",
    "#999": "text_muted", "#a0a0a0": "text_muted",
    "#6a8": "text_muted", "#777": "text_faint",
    "#007acc": "accent", "#3794ff": "accent", "#4fc1ff": "accent",
    "#569cd6": "info", "#4ec9b0": "info", "#ce9178": "accent",
    "#e5a04a": "warning", "#e5c07b": "warning", "#d7a600": "warning",
    "#f14c4c": "danger", "#e5534b": "danger", "#d73a49": "danger",
    "#6a9955": "success", "#4caf50": "success", "#4a3a20": "warning_soft",
    "#6b542f": "warning", "#0e7a3d": "success_soft", "#5a2d2d": "danger_soft",
}

_HEX_RE = re.compile(r"#(?:[0-9a-fA-F]{6}|[0-9a-fA-F]{3})\b")


def recolor_stylesheet(stylesheet, t=None):
    """把一段遗留内联 QSS 里的 VS Code 色值映射到当前语义 token。"""
    t = t or current_theme()

    def _sub(match):
        key = _LEGACY_STYLE_MAP.get(match.group(0).lower())
        return t[key] if key else match.group(0)

    return _HEX_RE.sub(_sub, stylesheet)


def harmonize(widget, t=None, standalone=False):
    """让一个遗留面板/对话框跟随当前主题。

    standalone=True 时额外把整套 QSS 装到该窗口（独立窗口不继承父级 QSS）。
    """
    t = t or current_theme()
    for w in [widget] + widget.findChildren(QWidget):
        ss = w.styleSheet()
        if ss:
            w.setStyleSheet(recolor_stylesheet(ss, t))
    if standalone:
        if not widget.objectName():
            widget.setObjectName("standaloneRoot")
        own = widget.styleSheet()
        widget.setStyleSheet(build_qss(t) + "\n" + own)
        polish(widget)
    return t


def accent_icon(name, theme=None):
    return make_icon(name, (theme or current_theme())["accent"])


def semantic_icon(name, kind="muted", theme=None):
    t = theme or current_theme()
    keys = {
        "muted": "text_muted", "accent": "accent", "danger": "danger",
        "success": "success", "warning": "warning", "info": "info",
    }
    return make_icon(name, t[keys.get(kind, "text_muted")])
