"""探针：按用户真实配置搭一份对话面板，量出各层的实际字号。

目的：区分「字号设置没生效」和「生效了但 16px 仍然偏小」。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

from qtpy.QtWidgets import QApplication, QTextEdit  # noqa: E402
from qtpy.QtGui import QFontMetrics  # noqa: E402

from spyder_copilot_chat.widgets import theme  # noqa: E402
from spyder_copilot_chat.widgets.chat_widget import CopilotChatPanel  # noqa: E402


def main():
    app = QApplication.instance() or QApplication(sys.argv)
    panel = CopilotChatPanel()
    panel.resize(860, 940)

    cfg = panel._config
    print("config  font_size=%r ui_font_size=%r ui_theme=%r density=%r"
          % (cfg.get("font_size"), cfg.get("ui_font_size"),
             cfg.get("ui_theme"), cfg.get("ui_density")))

    t = theme.current_theme()
    print("theme   dark=%r ui_font=%r body_font=%r" %
          (t.is_dark, t.ui_font, t.body_font))
    print("root qss 里 chatRoot 的 font-size:",
          [ln.strip() for ln in panel.styleSheet().splitlines()
           if "font-size" in ln and "chatRoot" in ln][:2])
    print("实际字体  panel.font().pointSizeF() =", panel.font().pointSizeF(),
          "pixelSize =", panel.font().pixelSize())

    # 造一段和截图同构的内容：检查点 + 工具行
    para = ("检查点 `20261114-113530-d82fb`（可用工具栏 ↩ 撤销本步）\n\n"
            "… run_command `powershell -NoProfile -Command \"Remove-Item "
            "-LiteralPath 'adventure_island.py','art.py','calculator.py' "
            "-Force\"`\n\nexit code: 0\n\n"
            "✕ run_command 失败：⛔ 安全策略阻止：检测到危险命令（递归删除），"
            "已被沙箱拦截")
    bubble = panel._add_msg("assistant", para)
    bubble.set_process_text(para) if hasattr(bubble, "set_process_text") else None

    views = panel.findChildren(QTextEdit)
    print("QTextEdit 数量:", len(views))
    for v in views[:4]:
        doc = v.document()
        f = doc.defaultFont()
        fm = QFontMetrics(f)
        print("  view: defaultFamily=%r docDefault=%r pt=%s px=%s "
              "height=%s blockCount=%s"
              % (doc.defaultFont().family(), f.family(), f.pointSize(),
                 f.pixelSize(), fm.height(), doc.blockCount()))
        # 文档里第一块的实际字号（HTML 里写了 font-size:16px）
        blk = doc.begin()
        it = blk.begin()
        while not it.atEnd():
            cf = it.fragment().charFormat().font()
            if cf.pixelSize() > 0 or cf.pointSizeF() > 0:
                print("    frag font: px=%s pt=%s family=%r"
                      % (cf.pixelSize(), cf.pointSizeF(), cf.family()))
                break
            it += 1

    out = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "probe-font.png")
    panel.grab().save(out)
    print("saved", out)


if __name__ == "__main__":
    main()
