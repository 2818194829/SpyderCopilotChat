# -*- coding: utf-8 -*-
"""v0.8.12 回归：输入框能贴图 / 拖图 / 选图，并且真的发给模型。

用户诉求（附截图）：输入框无法上传图片、识别图片——他要的是像主流
客户端那样，粘一张截图就出现一个小胶囊，模型能"看见"它。

覆盖：
  A. attachments 模块：magic 嗅探、落盘去重、体积上限、data URL 编码
  B. 消息 part：本地引用 → data URL 的转换与失败兜底
  C. ChatInput：粘贴图片 / 粘贴文本+图 / 拖入文件 url
  D. 面板：附件的增删去重、上限制约、多模态 content 组装
  E. 气泡：用户消息里的缩略图
  F. ChatWorker：_prepare_messages 真的把图编码了；Anthropic 格式转换
"""
import os
import sys
import tempfile

ROOT = (r"C:\Users\Mr_Yang\Documents\deepseek-harness"
        r"\default-workspace\spyder-copilot-chat")
sys.path.insert(0, ROOT)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from qtpy.QtCore import QMimeData, QSize, QUrl                 # noqa: E402
from qtpy.QtGui import QColor, QImage, QPainter                # noqa: E402
from qtpy.QtWidgets import QApplication                        # noqa: E402

APP = QApplication.instance() or QApplication([])

from spyder_copilot_chat.widgets import attachments as att     # noqa: E402
from spyder_copilot_chat.widgets import chat_widget as cw      # noqa: E402

FAILS = []
PASSES = []


def check(name, cond, extra=""):
    if cond:
        PASSES.append(name)
    else:
        FAILS.append("%s  %s" % (name, extra))


def make_png(color="#3b82f6", size=(64, 48)):
    img = QImage(size[0], size[1], QImage.Format_ARGB32)
    img.fill(QColor(color))
    p = QPainter(img)
    p.setPen(QColor("#ffffff"))
    p.drawRect(4, 4, size[0] - 9, size[1] - 9)
    p.end()
    tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
    tmp.close()
    img.save(tmp.name, "PNG")
    return tmp.name


TMP = tempfile.mkdtemp(prefix="v092_")

# ── A. attachments 模块 ────────────────────────────────────────
PNG = make_png()
with open(PNG, "rb") as fh:
    PNG_BYTES = fh.read()

check("PNG magic 嗅探", att.sniff_ext(PNG_BYTES) == ".png", att.sniff_ext(PNG_BYTES))
check("JPEG magic 嗅探", att.sniff_ext(b"\xff\xd8\xff\xe0" + b"0" * 20) == ".jpg")
check("GIF magic 嗅探", att.sniff_ext(b"GIF89a" + b"0" * 20) == ".gif")
check("WEBP magic 嗅探",
      att.sniff_ext(b"RIFF" + b"\x00" * 4 + b"WEBP" + b"0" * 8) == ".webp")
check("BMP magic 嗅探", att.sniff_ext(b"BM" + b"0" * 20) == ".bmp")
check("非图片返回 None", att.sniff_ext(b"hello world") is None)
check("空数据返回 None", att.sniff_ext(b"") is None)

p1, err1 = att.save_bytes(PNG_BYTES, "a.png")
p2, err2 = att.save_bytes(PNG_BYTES, "b.png")
check("落盘成功", p1 is not None and os.path.exists(p1), err1)
check("同内容去重（同一路径）", p1 == p2, "%s vs %s" % (p1, p2))
check("落盘文件名带 hash 与后缀", os.path.basename(p1).endswith(".png")
      and len(os.path.basename(p1)) >= 20, os.path.basename(p1))

big, big_err = att.save_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * (att.MAX_IMAGE_BYTES + 10))
check("超过体积上限被拒", big is None and "过大" in (big_err or ""), big_err)

notimg, notimg_err = att.save_bytes(b"definitely not an image")
check("非图片字节被拒", notimg is None and notimg_err, notimg_err)

qpath, qerr = att.save_qimage(QImage(PNG))
check("QImage 落盘成功", qpath is not None and os.path.exists(qpath), qerr)
check("QImage 落成 PNG", att.sniff_ext(open(qpath, "rb").read()) == ".png")

fpath, ferr = att.save_file(PNG)
check("从磁盘收图", fpath is not None and os.path.exists(fpath), ferr)
bad, bad_err = att.save_file(os.path.join(TMP, "note.txt"))
open(os.path.join(TMP, "note.txt"), "w").write("x")
bad, bad_err = att.save_file(os.path.join(TMP, "note.txt"))
check("非图片文件被拒", bad is None and bad_err, bad_err)

url, uerr = att.to_data_url(p1)
check("编码成 data URL", url.startswith("data:image/png;base64,"), (url or "")[:40])
check("编码失败有原因", att.to_data_url(os.path.join(TMP, "nope.png"))[0] is None)

part = att.part_for(p1, "Clipboard_Screenshot.png")
check("part 是 image_url 形状",
      part["type"] == "image_url" and part["image_url"]["url"].startswith(
          att.PART_PREFIX), part)
check("part 记住原始文件名", part["name"] == "Clipboard_Screenshot.png")
check("path_of 还原路径", att.path_of(part) == p1.replace("\\", "/"), att.path_of(part))
check("path_of 对外链返回 None",
      att.path_of({"type": "image_url",
                   "image_url": {"url": "https://x/y.png"}}) is None)

resolved, errs = att.resolve_parts([{"type": "text", "text": "看图"},
                                    part])
check("resolve 后不再有本地引用",
      all(att.path_of(p) is None for p in resolved))
check("resolve 后是 data URL",
      resolved[1]["image_url"]["url"].startswith("data:image/png"),
      resolved[1]["image_url"]["url"][:30])
check("resolve 剥掉 name 字段（严格网关会挑刺）", "name" not in resolved[1])
check("resolve 文本 part 原样保留", resolved[0]["text"] == "看图")
check("resolve 无错误", errs == [], errs)

lost_part = att.part_for(os.path.join(TMP, "gone.png"))
resolved2, errs2 = att.resolve_parts([lost_part])
check("图片丢失时退化成文字，不整条 400",
      errs2 and resolved2[0]["type"] == "text"
      and "失效" in resolved2[0]["text"], (errs2, resolved2))

check("字符串消息原样返回", att.resolve_parts("hello")[0] == "hello")
check("has_images 认 list", att.has_images([part]))
check("has_images 不认字符串", not att.has_images("hi"))
check("message_text 取 text part",
      att.message_text([{"type": "text", "text": "a"},
                        {"type": "text", "text": "b"}]) == "a\nb")
check("message_text 兼容字符串", att.message_text("abc") == "abc")
check("message_images 取路径", att.message_images([part]) == [p1.replace("\\", "/")])
check("vision_hint 认得出不看图的模型",
      "不支持读图" in att.vision_hint("deepseek-chat"))
check("vision_hint 对 gpt-4o 闭嘴", att.vision_hint("gpt-4o") == "")
check("vision_hint 对未知模型闭嘴", att.vision_hint("my-proxy-model") == "")
check("vision_hint 认代理前缀", "不支持读图" in att.vision_hint("deepseek/deepseek-chat"))

# ── C. ChatInput 粘贴 / 拖入 ───────────────────────────────────
inp = cw.ChatInput()
got_images = []
got_files = []
inp.image_pasted.connect(lambda img: got_images.append(img))
inp.files_dropped.connect(lambda paths: got_files.append(paths))

mime_img = QMimeData()
mime_img.setImageData(QImage(120, 80, QImage.Format_ARGB32))
inp.insertFromMimeData(mime_img)
check("粘贴纯图片 → 发射 image_pasted", len(got_images) == 1, len(got_images))
check("粘贴纯图片 → 不往输入框塞乱码", inp.toPlainText() == "",
      repr(inp.toPlainText()))

got_images.clear()
mime_both = QMimeData()
mime_both.setText("这是一段文字")
mime_both.setImageData(QImage(60, 40, QImage.Format_ARGB32))
inp.insertFromMimeData(mime_both)
check("文字+图片时优先粘贴文字", inp.toPlainText() == "这是一段文字",
      repr(inp.toPlainText()))
check("文字+图片时不抢成附件", not got_images, len(got_images))

inp.clear()
got_files.clear()
mime_url = QMimeData()
mime_url.setUrls([QUrl.fromLocalFile(PNG)])
inp.insertFromMimeData(mime_url)
check("粘贴图片文件 url → 发射 files_dropped", len(got_files) == 1, got_files)
check("files_dropped 带的是路径",
      got_files and got_files[0][0].endswith(".png"), got_files)

got_files.clear()
mime_txt_url = QMimeData()
mime_txt_url.setUrls([QUrl.fromLocalFile(os.path.join(TMP, "note.txt"))])
inp.insertFromMimeData(mime_txt_url)
check("非图片文件不被当成附件", not got_files, got_files)
# 非图片 url 还要能退化成文本（这里只要求不崩、不误报）

# ── D. 面板：附件管理 + 多模态组装 ─────────────────────────────
# 上限制约会弹 QMessageBox，测试里把它变成哑巴，否则会卡住
_orig_info = cw.QMessageBox.information
_orig_warn = cw.QMessageBox.warning
cw.QMessageBox.information = staticmethod(lambda *a, **k: None)
cw.QMessageBox.warning = staticmethod(lambda *a, **k: None)
try:
    panel = cw.CopilotChatPanel()
    panel.resize(520, 700)

    check("初始没有附件", panel._attachments == [])
    check("附件条初始隐藏", panel._attach_bar.isHidden())

    check("无附件时 content 保持字符串",
          isinstance(panel._content_with_images("你好"), str))

    panel._add_attachment(p1, "Screen.png")
    check("加入一张", len(panel._attachments) == 1, panel._attachments)
    panel._add_attachment(p1, "Screen.png")
    check("同一张图不会加两次", len(panel._attachments) == 1)
    check("附件条出现了", not panel._attach_bar.isHidden())
    check("缩略图控件已生成", panel._attach_lay.count() == 2,
          panel._attach_lay.count())

    content = panel._content_with_images("这张报错什么意思？")
    check("带图时 content 变成 part 列表", isinstance(content, list), type(content))
    check("第一个 part 是文本", content[0] == {
        "type": "text", "text": "这张报错什么意思？"}, content[0])
    check("后面跟 image_url",
          content[1]["type"] == "image_url"
          and content[1]["image_url"]["url"].startswith(att.PART_PREFIX),
          content[1])
    check("part 带原始文件名", content[1].get("name") == "Screen.png")

    panel._input.setPlainText("")
    blank = panel._content_with_images("")
    check("只有图、没有文字时不塞空 text part",
          isinstance(blank, list) and all(p["type"] == "image_url" for p in blank),
          blank)

    for i in range(10):
        panel._add_attachment(make_png("#e11d48", (24, 24)), "x%d.png" % i)
    check("最多 %d 张" % att.MAX_IMAGES,
          len(panel._attachments) == att.MAX_IMAGES, len(panel._attachments))

    victim = panel._attachments[0]["path"]
    panel._remove_attachment(victim)
    check("删掉一张", len(panel._attachments) == att.MAX_IMAGES - 1)
    check("删掉的那张不在列表里",
          all(a["path"] != victim for a in panel._attachments))
    check("缩略图数量跟着减",
          panel._attach_lay.count() == len(panel._attachments) + 1,
          panel._attach_lay.count())

    panel._clear_attachments()
    check("清空后列表为空", panel._attachments == [])
    check("清空后附件条隐藏", panel._attach_bar.isHidden())
    check("清空后缩略图也拆干净", panel._attach_lay.count() == 1,
          panel._attach_lay.count())

    # ── E. 气泡里的缩略图 ──
    bubble = panel._add_msg("user", "看下这张图")
    bubble.set_attachments([p1, qpath])
    check("气泡里挂上两张缩略图", bubble._attach_lay.count() == 3,
          bubble._attach_lay.count())
    check("气泡的图片行可见", not bubble._attach_row.isHidden())
    thumbs = [bubble._attach_lay.itemAt(i).widget()
              for i in range(bubble._attach_lay.count() - 1)]
    check("缩略图是 BubbleThumb", all(isinstance(t, cw.BubbleThumb) for t in thumbs))
    check("缩略图有 pixmap", all(t.pixmap() is not None for t in thumbs))

    # ── F. ChatWorker: 编码与格式转换 ──
    msgs = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "纯文字消息"},
        {"role": "user", "content": [{"type": "text", "text": "看图"},
                                     att.part_for(p1, "a.png")]},
    ]
    worker = cw.ChatWorker(msgs, {"provider": "custom", "api_url": "http://x"})
    prepared = worker._prepare_messages()
    check("system 插到最前", prepared[0]["role"] == "system")
    check("纯文字消息维度不变", prepared[1]["content"] == "纯文字消息")
    check("带图消息被编码成 data URL",
          prepared[2]["content"][1]["image_url"]["url"].startswith(
              "data:image/png;base64,"),
          prepared[2]["content"][1]["image_url"]["url"][:30])
    check("_prepare_messages 之后没有 spyder-file 残留",
          att.path_of(prepared[2]["content"][1]) is None)

    anthro = cw.ChatWorker._anthropic_content(prepared[2]["content"])
    check("Anthropic 转换产出 image block",
          any(b.get("type") == "image" for b in anthro), anthro)
    img_block = [b for b in anthro if b.get("type") == "image"][0]
    check("Anthropic image 用 base64 source",
          img_block["source"]["type"] == "base64"
          and img_block["source"]["media_type"] == "image/png", img_block)
    check("Anthropic 文本块还在",
          any(b.get("type") == "text" and b["text"] == "看图" for b in anthro))
    check("字符串内容转换后仍是字符串",
          cw.ChatWorker._anthropic_content("hi") == "hi")
finally:
    cw.QMessageBox.information = _orig_info
    cw.QMessageBox.warning = _orig_warn

# ── G. 历史存盘 / 搜索：带图消息的 content 是 list ─────────────
from spyder_copilot_chat.widgets import chat_history_manager as chm  # noqa: E402

_old_hist = chm.HISTORY_DIR
_old_cfg = chm.CONFIG_DIR
chm.CONFIG_DIR = tempfile.mkdtemp(prefix="v092_hist_")
chm.HISTORY_DIR = os.path.join(chm.CONFIG_DIR, "history")
try:
    check("带图消息能取到标题文本",
          chm._text_of([{"type": "text", "text": "看图"},
                        att.part_for(p1)]).startswith("[图片]"),
          chm._text_of([{"type": "text", "text": "看图"}, att.part_for(p1)]))

    hist_msgs = [
        {"role": "user", "content": [{"type": "text", "text": "这个报错什么意思"},
                                     att.part_for(p1, "a.png")]},
        {"role": "assistant", "content": "是块标记的基类继承错了"},
    ]
    chat_id = chm.save_chat(hist_msgs)
    check("带图对话能存盘", bool(chat_id), chat_id)
    loaded = chm.load_chat(chat_id)
    check("标题里带 [图片] 前缀", loaded["title"].startswith("[图片]"),
          loaded["title"])
    check("存盘的 content 仍是 part 列表",
          isinstance(loaded["messages"][0]["content"], list))
    found = chm.search_chats("报错")
    check("搜索带图的对话不崩且能命中",
          any(c.get("id") == chat_id for c in found), len(found))
    check("搜不到时返回空", chm.search_chats("不存在词xyz") == [])
finally:
    chm.HISTORY_DIR = _old_hist
    chm.CONFIG_DIR = _old_cfg

# ── 汇总 ───────────────────────────────────────────────────────
print("=" * 60)
for f in FAILS:
    print("FAIL  " + f)
print("PASS %d / %d" % (len(PASSES), len(PASSES) + len(FAILS)))
sys.exit(1 if FAILS else 0)
