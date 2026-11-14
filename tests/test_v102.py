# -*- coding: utf-8 -*-
"""v0.8.23 回归：字体列表从"4 条预设"扩成"预设 + 本机已装字族"。

现场：Aa 菜单里的「字体」只有跟随系统 / 微软雅黑 / 思源黑体 / 等宽四条，
想换宋体、楷体、仿宋就得去改配置。加的时候踩到两个坑，正是这里守的东西：

1. **字体表不能靠静态调用** —— ``QFontDatabase.families()`` 在 Qt5 里是
   实例方法，直接点类名会抛 TypeError；旧写法把这个异常吞了，结果是"永远
   没有系统字体"，菜单看起来毫无变化。
2. **注册名分语言** —— 中文 Windows 只注册「微软雅黑」，预设里写的
   ``Microsoft YaHei`` 在字体表里查不到，QFont 拿到一个不存在的名字等于没设。
   所以既要有别名互认，也要有"只列装了的"。

跑法：``python tests/test_v102.py``（也兼容 pytest）。offscreen 平台。
"""

import os
import sys
import types

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from qtpy.QtWidgets import QApplication, QComboBox  # noqa: E402

_APP = QApplication.instance() or QApplication([])

from spyder_copilot_chat.widgets import theme  # noqa: E402
from spyder_copilot_chat.widgets import chat_widget as CW  # noqa: E402
from spyder_copilot_chat.widgets import settings_dialog as SD  # noqa: E402

_results = []

#: 假的字体表：只放中文注册名，模拟中文 Windows；Arial / Segoe UI 用来
#: 验证"全部字体"里剩下的那些。
FAKE_FAMILIES = ["微软雅黑", "宋体", "楷体", "Consolas", "Arial", "Segoe UI"]


def check(name, condition, extra=""):
    _results.append((name, bool(condition), extra))
    if not condition:
        print("FAIL  {0}  {1}".format(name, extra))


def eq(name, got, want):
    check(name, got == want, "got={0!r} want={1!r}".format(got, want))


def _freeze(names=None):
    """把字体表换成一组确定的名字，避免测试结果跟着本机装了什么漂移。"""
    names = list(FAKE_FAMILIES if names is None else names)
    theme._FAMILY_CACHE = sorted(names, key=lambda s: s.lower())
    theme._FAMILY_MAP = {theme._family_key(n): n for n in names}


def _thaw():
    theme._FAMILY_CACHE = []
    theme._FAMILY_MAP = {}


# ---------------------------------------------------------------------------
# 字族解析
# ---------------------------------------------------------------------------

def test_presets_keep_order_and_first_is_system():
    labels = [label for label, _v in theme.FONT_CHOICES]
    eq("预设仍是四条", len(labels), 4)
    eq("第一条是跟随系统", labels[0], "跟随系统")
    eq("跟随时写入空串", theme.FONT_CHOICES[0][1], "")


def test_english_preset_resolves_to_chinese_registration():
    _freeze()
    eq("Microsoft YaHei 落到微软雅黑",
       theme.resolve_family(("Microsoft YaHei", "PingFang SC")), "微软雅黑")
    _thaw()


def test_resolve_returns_empty_when_missing():
    _freeze(["Arial"])
    eq("没装就返回空串", theme.resolve_family(("SimSun", "宋体")), "")
    _thaw()


def test_concrete_families_only_when_installed():
    _freeze(["微软雅黑", "Consolas"])
    eq("只装预设覆盖的字体时，常见列表为空",
       [label for label, _f, _v in theme.common_font_choices()], [])
    _freeze(["微软雅黑", "Consolas", "宋体"])
    eq("装上宋体后才出现",
       [label for label, _f, _v in theme.common_font_choices()], ["宋体"])
    _thaw()


def test_common_choices_do_not_repeat_presets():
    _freeze()
    labels = [label for label, _v in theme.font_choices()]
    eq("微软雅黑只出现一次（预设那条）", labels.count("微软雅黑"), 0)
    eq("字体总数 = 预设 4 + 新装 2（宋体/楷体）", len(labels), 6)
    _thaw()


def test_other_families_excludes_what_is_already_shown():
    _freeze()
    eq("剩下的就是 Arial / Segoe UI",
       theme.other_families(), ["Arial", "Segoe UI"])
    _thaw()


def test_css_family_quoting():
    eq("普通名加单引号", theme.css_family("Microsoft YaHei"), "'Microsoft YaHei'")
    eq("两头空格去掉", theme.css_family("  宋体 "), "'宋体'")
    eq("空串还是空串", theme.css_family(""), "")
    eq("引号字符被剔除", theme.css_family("O'Brien"), "'OBrien'")


def test_theme_uses_installed_name():
    _freeze()
    t = theme.resolve_theme("dark", theme.DEFAULT_ACCENT, "comfortable",
                            13, 14, "'Microsoft YaHei','PingFang SC'")
    eq("预设栈解析到真实注册名", t.font_family_name, "微软雅黑")
    t2 = theme.resolve_theme("dark", theme.DEFAULT_ACCENT, "comfortable",
                             13, 14, "'宋体'")
    eq("单字体原样生效", t2.font_family_name, "宋体")
    t3 = theme.resolve_theme("dark", theme.DEFAULT_ACCENT, "comfortable",
                             13, 14, "'不存在的字体'")
    eq("没装时退回栈里第一个名字", t3.font_family_name, "不存在的字体")
    t4 = theme.resolve_theme("dark", theme.DEFAULT_ACCENT, "comfortable",
                             13, 14, "")
    eq("跟随系统不设 QFont 字族", t4.font_family_name, "")
    _thaw()


# ---------------------------------------------------------------------------
# Aa 菜单
# ---------------------------------------------------------------------------

def _panel():
    p = CW.CopilotChatPanel()
    p.resize(760, 640)
    return p


def _font_submenu(menu):
    for act in menu.actions():
        if act.text() == "字体" and act.menu() is not None:
            return act.menu()
    return None


def test_menu_lists_new_fonts():
    _freeze()
    p = _panel()
    menu = p._build_font_menu()
    sub = _font_submenu(menu)
    check("有字体子菜单", sub is not None)
    if sub is not None:
        texts = [a.text() for a in sub.actions() if a.text()]
        check("列出宋体", "宋体" in texts, texts)
        check("列出楷体", "楷体" in texts, texts)
        more = [a for a in sub.actions() if a.menu() is not None]
        check("有全部字体二级菜单", bool(more), texts)
        if more:
            check("二级菜单数量写在标题上",
                  "全部字体（2）" in more[0].text(), more[0].text())
            names = [a.text() for a in more[0].menu().actions()]
            eq("二级菜单放剩下的字族", names, ["Arial", "Segoe UI"])
    p.deleteLater()
    _thaw()


def test_menu_checks_current_font():
    _freeze()
    p = _panel()
    p._config["font_family"] = "'宋体'"
    menu = p._build_font_menu()
    sub = _font_submenu(menu)
    checked = [a.text() for a in (sub.actions() if sub else []) if a.isChecked()]
    eq("当前字体被勾上", checked, ["宋体"])
    p.deleteLater()
    _thaw()


def test_menu_items_preview_own_font():
    _freeze()
    p = _panel()
    menu = p._build_font_menu()
    sub = _font_submenu(menu)
    fonts = {}
    for act in (sub.actions() if sub else []):
        if act.text() in ("宋体", "楷体"):
            fonts[act.text()] = act.font().family()
    eq("条目用自己的字族渲染（宋体）", fonts.get("宋体"), "宋体")
    eq("条目用自己的字族渲染（楷体）", fonts.get("楷体"), "楷体")
    p.deleteLater()
    _thaw()


def test_menu_select_persists_and_applies():
    _freeze()
    p = _panel()
    saved, applied = {}, []
    p._persist_font = lambda **kw: saved.update(kw)
    p._apply_font_now = lambda: applied.append(True)
    menu = p._build_font_menu()
    sub = _font_submenu(menu)
    target = next(a for a in sub.actions() if a.text() == "宋体")
    target.trigger()
    eq("写进配置的是带引号的字族", saved.get("font_family"), "'宋体'")
    eq("立即重绘一次", len(applied), 1)
    p.deleteLater()
    _thaw()


def test_menu_survives_empty_font_table():
    """没有字体服务（离屏/精简环境）时不能崩，也不该出现空的分组。"""
    theme._FAMILY_CACHE = []
    theme._FAMILY_MAP = {}
    p = _panel()
    menu = p._build_font_menu()
    sub = _font_submenu(menu)
    check("仍然有字体子菜单", sub is not None)
    if sub is not None:
        texts = [a.text() for a in sub.actions() if a.text()]
        eq("只有四条预设", len(texts), 4)
        check("没有空标题的二级菜单",
              not any(a.menu() is not None for a in sub.actions()))
    p.deleteLater()
    _thaw()


# ---------------------------------------------------------------------------
# 设置页
# ---------------------------------------------------------------------------

def test_settings_combo_uses_shared_choices():
    src = open(os.path.join(ROOT, "spyder_copilot_chat", "widgets",
                            "settings_dialog.py"), encoding="utf-8").read()
    check("设置页用共享的字体列表", "theme.font_choices()" in src)
    check("设置页不再只用四条预设", "for label, value in theme.FONT_CHOICES" not in src)


def test_settings_keeps_unknown_current_value():
    combo = QComboBox()
    for label, value in theme.FONT_CHOICES:
        combo.addItem(label, value)
    stub = types.SimpleNamespace(_font_family=combo)
    SD.SettingsDialog._select_font_family(stub, "'已卸载的字体'")
    eq("补一条当前项", combo.currentData(), "'已卸载的字体'")
    check("标签带（当前）", "（当前）" in combo.currentText(), combo.currentText())

    stub2 = types.SimpleNamespace(_font_family=combo)
    SD.SettingsDialog._select_font_family(stub2, "")
    eq("已知值正常选中", combo.currentIndex(), 0)


def main():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
            except Exception as exc:  # noqa: BLE001
                check(name + " 未抛异常", False, repr(exc))
    passed = sum(1 for _, ok, _ in _results if ok)
    total = len(_results)
    failures = [(n, e) for n, ok, e in _results if not ok]
    print("\n{0}/{1} passed".format(passed, total))
    if failures:
        print("failures:")
        for name, extra in failures:
            print("  -", name, extra)
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
