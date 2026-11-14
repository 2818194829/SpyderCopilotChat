# -*- coding: utf-8 -*-
"""v0.8.17 回归：右侧消息导航条 / 悬停展开的历史清单。

现场：用户对照参考截图指出「缺少一个这个方便用户查看历史发的信息」——
长会话里想回头找自己问过的某句话，只能一直滚轮往上翻。这版在对话流
右缘加了一条常驻刻度（一条刻度 = 一条用户消息），悬停展开完整清单，
点任意一条直接滚回去并闪一下边框。

实现上三个坑被这份用例钉住：

1. **``_refresh_rail`` 跑在 QTimer 槽里**。Qt 槽里未捕获的异常不走 Python
   的 traceback，PyQt 直接 abort 进程（exit 127、无输出）。首版手改留下
   一个未定义的 ``total``，一滚动整个 Spyder 就没了。现在整段包
   try/except，且 ``total`` 必须存在。
2. **浮层不能是 ``Qt.Popup``**。Popup 会抓鼠标，视口收不到 MouseMove，
   「移开自动收起」全失效。现在是面板内子控件，且高亮复用列表而不是
   重建（重建会把长清单的滚动位置弹回顶部）。
3. **``setSelected`` 必须在 ``addItem`` 之后**。还没进 model 的 item，
   选中是空操作——首版实拍里「当前项没亮」就是这个原因。

写这份用例时还踩了一次同样的 abort：``flyout_requested`` 是零参数
信号，把它直接连到 ``list.append``（需要 1 个参数）会在信号回调里抛
TypeError，PyQt 照样 abort 且不打印 traceback。所以下面用 lambda 收，
这也算给「Qt 槽里任何异常都会直接杀死进程」再留一条实证。

跑法：``python tests/test_v097.py``（也兼容 pytest）。用 offscreen 平台，
不需要真实字体与显示服务。
"""

import os
import sys
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from qtpy.QtCore import QEvent, QPoint, Qt  # noqa: E402
from qtpy.QtGui import QMouseEvent  # noqa: E402
from qtpy.QtWidgets import QApplication, QWidget  # noqa: E402

_APP = QApplication.instance() or QApplication([])

from spyder_copilot_chat.widgets import chat_widget as CW  # noqa: E402
from spyder_copilot_chat.widgets import settings_dialog as SD  # noqa: E402
from spyder_copilot_chat.widgets import theme  # noqa: E402

_results = []


def check(name, condition, extra=""):
    _results.append((name, bool(condition), extra))
    if not condition:
        print("FAIL  {0}  {1}".format(name, extra))


def eq(name, got, want):
    check(name, got == want, "got={0!r} want={1!r}".format(got, want))


def _source(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


def _click(widget, x, y):
    ev = QMouseEvent(QEvent.MouseButtonRelease, QPoint(x, y),
                     Qt.LeftButton, Qt.LeftButton, Qt.NoModifier)
    widget.mouseReleaseEvent(ev)


def _move(widget, x, y):
    ev = QMouseEvent(QEvent.MouseMove, QPoint(x, y), Qt.NoButton,
                     Qt.NoButton, Qt.NoModifier)
    widget.mouseMoveEvent(ev)


def _fresh(n=4):
    """一个干净的面板：show 过才有真实 viewport 几何，_place_rail 才算得准。"""
    p = CW.CopilotChatPanel()
    p.resize(760, 640)
    p._config["message_nav_enabled"] = True
    p.show()
    _APP.processEvents()
    for i in range(n):
        p._add_msg("user", "第 {0} 个问题".format(i + 1))
        p._add_msg("assistant", "回答 {0}".format(i + 1))
    for _ in range(6):
        _APP.processEvents()
    p._refresh_rail()
    return p


# ---------------------------------------------------------------------------
# 静态守卫：这些是「进程直接消失」级别的回归，必须永远在
# ---------------------------------------------------------------------------

def test_static_guards():
    src = _source("spyder_copilot_chat/widgets/chat_widget.py")
    check("rail total 已定义（避免 Qt 槽内 NameError 导致 abort）",
          "total = float(max(1, self._inner.height()))" in src)
    check("_refresh_rail 整段 try/except 兜底",
          "def _refresh_rail(self):" in src
          and "self._refresh_rail_inner(rail)" in src
          and "except Exception:" in src)
    check("API 错误用 warning 而不是 error（否则 Spyder 弹崩溃窗）",
          'logger.warning("Chat request failed: %s"' in src
          and 'logger.error("Chat request failed' not in src)
    fly_src = src.split("class MessageFlyout")[1].split(
        "class CopilotChatPanel")[0]
    check("浮层是面板内子控件（带布局，未设 Popup 窗口标志）",
          "class MessageFlyout(QFrame):" in src
          and "QVBoxLayout(self)" in fly_src
          and "setWindowFlags" not in fly_src)
    check("_clear_ui 清空导航索引",
          "self._user_marks = []" in src.split("def _clear_ui")[1][:900])
    check("_add_msg 只登记用户消息",
          'if role == "user":' in src.split("def _add_msg")[1][:1400]
          and "nav_preview(content)" in src)
    check("清空索引后先 addItem 再选中",
          src.index("self._list.addItem(item)") < src.index(
              "def set_current(self, current):"))

    th = _source("spyder_copilot_chat/widgets/theme.py")
    for marker in ("QFrame#messageRail", "QFrame#messageFlyout",
                   'QFrame#userBubble[flash="true"]'):
        check("主题样式含 " + marker, marker in th)

    sd = _source("spyder_copilot_chat/widgets/settings_dialog.py")
    check("设置项：读取配置带默认值",
          '"message_nav_enabled": cfg.get("message_nav_enabled", True)' in sd)
    check("设置项：载入对话框回填",
          "self._message_nav_enabled.setChecked(" in sd
          and 'bool(self._config.get("message_nav_enabled", True))' in sd)
    check("设置项：保存时写回配置",
          'self._config["message_nav_enabled"] = ' in sd
          and "self._message_nav_enabled.isChecked()" in sd)


def test_config_default():
    with mock.patch.object(SD, "load_config", return_value={}):
        cfg = SD.get_provider_config()
    check("默认开启消息导航条", cfg.get("message_nav_enabled") is True)
    with mock.patch.object(SD, "load_config",
                           return_value={"message_nav_enabled": False}):
        cfg2 = SD.get_provider_config()
    check("用户显式关闭会保留", cfg2.get("message_nav_enabled") is False)


def test_nav_preview():
    f = CW.nav_preview
    eq("空消息占位", f(""), "（空消息）")
    eq("只取第一行", f("第一行\n第二行"), "第一行")
    eq("去标题井号", f("## 标题"), "标题")
    eq("去列表符号", f("- 列表项"), "列表项")
    eq("去行内代码", f("用 `hy4` 模型"), "用 hy4 模型")
    eq("链接只留文字", f("看[文档](https://x)吧"), "看文档吧")
    check("超长截断带省略号",
          f("很长" * 100).endswith("…") and len(f("很长" * 100)) == 49)
    parts = [{"type": "text", "text": "看看这张图"},
             {"type": "image_url", "image_url": {"url": "data:..."}}]
    eq("多模态内容带图片前缀", f(parts), "[图片] 看看这张图")


# ---------------------------------------------------------------------------
# MessageRail：等距刻度、命中、信号
# ---------------------------------------------------------------------------

def test_rail_ticks():
    r = CW.MessageRail()
    r.resize(CW.MessageRail.WIDTH, 400)
    r.set_ticks([], -1)
    check("空会话不显示导航条", not r.isVisible())
    eq("空刻度无矩形", len(r._tick_rects()), 0)
    tips = [(i / 4.0, "{0}. 第 {0} 条".format(i + 1)) for i in range(4)]
    r.set_ticks(tips, 1)
    check("有消息才显示", r.isVisible())
    rects = r._tick_rects()
    eq("刻度数 = 用户消息数", len(rects), 4)
    ys = [x.y() for x in rects]
    eq("刻度等距摆放", len(set(ys[i + 1] - ys[i] for i in range(3))), 1)
    check("当前项更高", rects[1].height() == CW.MessageRail.CUR_H
          and rects[0].height() == CW.MessageRail.TICK_H)
    check("当前项更宽", rects[1].width() >= rects[0].width())
    check("刻度不越界", all(x.y() >= 0 and x.bottom() <= r.height()
                            for x in rects))
    eq("ratio 被存下供 tooltip 用", r._ticks[2][0], 0.5)


def test_rail_hit_and_signals():
    r = CW.MessageRail()
    r.resize(CW.MessageRail.WIDTH, 400)
    r.set_ticks([(i / 4.0, "{0}. msg".format(i + 1)) for i in range(4)], 0)
    rects = r._tick_rects()
    eq("命中第 3 条", r._index_at(QPoint(7, rects[2].center().y())), 2)
    eq("远离刻度不命中", r._index_at(QPoint(7, 399)), -1)

    got = []
    r.locate.connect(got.append)
    _click(r, 7, rects[2].center().y())
    eq("点刻度发出跳转信号", got, [2])

    opened = []
    # 零参数信号不能直连 list.append——回调签名不匹配会让 PyQt abort
    r.flyout_requested.connect(lambda: opened.append(1))
    _click(r, 7, 399)
    eq("点空白改为展开清单", len(opened), 1)

    hovered = []
    r.hovered.connect(hovered.append)
    _move(r, 7, rects[1].center().y())
    check("悬停发出 hovered 信号", 1 in hovered)
    eq("悬停后 tooltip 指向该条", r.toolTip(), "2. msg")
    eq("悬停项被放大", r._tick_rects()[1].height(),
       CW.MessageRail.TICK_H + 2)


def test_rail_many_ticks_no_overlap():
    r = CW.MessageRail()
    r.resize(CW.MessageRail.WIDTH, 506)
    r.set_ticks([(i / 29.0, "m{0}".format(i)) for i in range(30)], 0)
    rects = r._tick_rects()
    eq("30 条仍有 30 个刻度", len(rects), 30)
    check("长会话刻度不叠成一根线",
          rects[1].y() - rects[0].y() >= CW.MessageRail.TICK_H + 4)
    check("最后一条仍在条内", rects[-1].bottom() <= r.height())


# ---------------------------------------------------------------------------
# MessageFlyout：装填、上限、高亮复用
# ---------------------------------------------------------------------------

def test_flyout_build_and_current():
    host = QWidget()
    f = CW.MessageFlyout(host)
    check("浮层挂在面板下、不是独立 Popup 窗口（否则会抓走鼠标）",
          f.parent() is host and not f.isWindow())
    items = [(i, "第 {0} 个问题".format(i + 1)) for i in range(3)]
    size = f.build(items, 1)
    check("建议尺寸可用", size.width() >= 240 and size.height() > 0)
    eq("行数 = 消息数", f._list.count(), 3)
    eq("首行带序号", f._list.item(0).text(), "1. 第 1 个问题")
    eq("行内 data 是消息下标", f._list.item(2).data(Qt.UserRole), 2)
    check("build 时当前行已选中", f._list.item(1).isSelected())
    check("非当前行未选中", not f._list.item(0).isSelected())

    first = f._list.item(0)
    f.set_current(2)
    check("set_current 不重建列表（长清单滚动位置不会被弹回顶部）",
          f._list.item(0) is first)
    check("高亮移到第 3 行", f._list.item(2).isSelected())
    f.set_current(-1)
    check("越界高亮清空选择", f._list.currentItem() is None)

    picked = []
    f.locate.connect(picked.append)
    f._pick(1)
    eq("点行先收起再跳转", picked, [1])
    check("跳转后浮层已隐藏", not f.isVisible())


def test_flyout_row_cap():
    f = CW.MessageFlyout()
    items = [(i, "问题 {0}".format(i + 1)) for i in range(30)]
    f.build(items, 0)
    eq("24 行 + 1 行溢出提示", f._list.count(),
       CW.MessageFlyout.MAX_ROWS + 1)
    last = f._list.item(f._list.count() - 1)
    check("提示行说明还有多少条", "还有 6 条" in last.text())
    check("提示行不可点", not (last.flags() & Qt.ItemIsEnabled))
    eq("溢出行不进勾选范围", f._list.item(
        CW.MessageFlyout.MAX_ROWS - 1).data(Qt.UserRole), 23)
    f.set_current(29)
    check("越界 set_current 不抛异常也不误选提示行",
          not last.isSelected())


# ---------------------------------------------------------------------------
# 面板集成：注册、定位、跳转、开关、清空、异常兜底
# ---------------------------------------------------------------------------

def test_panel_marks_and_placement():
    p = _fresh(4)
    try:
        eq("只登记用户消息", len(p._user_marks), 4)
        check("导航条可见", p._rail.isVisible())
        eq("刻度数 = 用户消息数", len(p._rail._ticks), 4)
        check("刻度 tooltip 是预览文本",
              "第 1 个问题" in p._rail._ticks[0][1])

        vp = p._scroll.viewport()
        vp_right = vp.mapTo(p, QPoint(vp.width(), 0)).x()
        rg = p._rail.geometry()
        check("导航条贴在视口右缘内侧（不压竖滚动条）",
              rg.right() <= vp_right + 1, "{0} vs {1}".format(
                  rg.right(), vp_right))
        check("导航条在面板内", rg.left() >= 0
              and rg.right() <= p.width() + 1
              and rg.top() >= 0 and rg.bottom() <= p.height() + 1)

        vsb = p._scroll.verticalScrollBar()
        vsb.setValue(vsb.maximum())
        p._refresh_rail()
        check("滚到底后当前高亮跟着往后移", p._rail._index >= 1,
              "index={0}".format(p._rail._index))
    finally:
        p.close()


def test_panel_flyout_and_jump():
    p = _fresh(5)
    try:
        p._show_message_flyout()
        check("清单可见", p._flyout.isVisible())
        eq("清单列出全部用户消息", p._flyout._list.count(), 5)
        fg = p._flyout.geometry()
        check("清单不越出面板", fg.left() >= 0
              and fg.right() <= p.width() + 1)
        check("清单在导航条左侧", fg.right() <= p._rail.geometry().left())

        item0 = p._flyout._list.item(0)
        p._show_message_flyout(highlight=3)
        check("同一会话重复唤出只换高亮、不重建列表",
              p._flyout._list.item(0) is item0)
        check("对应行被预选", p._flyout._list.item(3).isSelected())

        p._hide_message_flyout()
        check("收起后标记失效，下次会重建", p._flyout_count == -1)
        check("收起后不可见", not p._flyout.isVisible())

        p._flyout._pick(0)
        check("点清单条目后浮层收起", not p._flyout.isVisible())
        bubble = p._user_marks[0]["bubble"]
        eq("跳转后目标气泡闪一下", bubble.property("flash"), "true")

        vsb = p._scroll.verticalScrollBar()
        vsb.setValue(vsb.maximum())
        p._locate_user_message(1)
        check("跳到第 2 条时滚动位置明显变化",
              vsb.value() < vsb.maximum(), "{0} / {1}".format(
                  vsb.value(), vsb.maximum()))
        p._locate_user_message(99)
        check("越界下标安全忽略", True)
    finally:
        p.close()


def test_panel_toggle_clear_and_refresh_guard():
    p = _fresh(3)
    try:
        p._config["message_nav_enabled"] = False
        p._refresh_rail()
        check("设置里关掉后导航条隐藏", not p._rail.isVisible())
        eq("关掉时刻度清空", len(p._rail._ticks), 0)
        p._config["message_nav_enabled"] = True
        p._refresh_rail()
        check("重新打开后恢复", p._rail.isVisible()
              and len(p._rail._ticks) == 3)

        old = p._rail.geometry()
        p.resize(620, 520)
        _APP.processEvents()
        new = p._rail.geometry()
        check("面板缩放后导航条重新贴到右缘",
              new.right() != old.right() or new.bottom() != old.bottom(),
              "{0} -> {1}".format(old, new))

        p._clear_ui()
        p._refresh_rail()
        eq("清空会话后索引为空", len(p._user_marks), 0)
        eq("清空会话后刻度为空", len(p._rail._ticks), 0)
        check("清空会话后导航条隐藏", not p._rail.isVisible())

        # Qt 槽里抛异常 = 进程 abort。这里直接让内层炸掉，验证外层吞掉。
        with mock.patch.object(p, "_refresh_rail_inner",
                               side_effect=RuntimeError("boom")):
            p._refresh_rail()
        check("_refresh_rail 吞掉内层异常", True)
        eq("异常兜底后刻度被清空", len(p._rail._ticks), 0)
    finally:
        p.close()


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
