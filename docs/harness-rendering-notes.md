# DeepSeek Harness 聊天面板渲染栈（调研记录）

来源：`D:\Program Files\WorkBuddy\resources\app.asar` → `renderer/assets/markdown-renderer-*.js` / `.css`（v5.11.2）

## 1. 解析管线（unified 生态，双阶段）

```
remarkPlugins = [
  remark-gfm,
  remark-breaks,
  [remark-math, {singleDollarTextMath: true}],
  [remark-linkify-it, {linkify}],      // 裸 URL 自动成链
  [remark-sensitive-protection, ...],  // 敏感串脱敏
  remark-code-language,                // 归一化 ```lang
]

rehypePlugins = [
  [rehype-sanitize, schema],           // 白名单：标签/属性/protocol 三级
  rehype-fix-autolink-boundary,
  rehype-code-block,
  rehype-inline-code,
  [rehype-katex, {strict:false, throwOnError:false, errorColor:'#cc0000'}]  // $..$ / $$..$$
  [rehype-highlight, {languages}]      // lowlight(highlight.js) 语法高亮
]
```

- 代码块不走默认 `<pre>`，而是被 `components.pre` 覆盖成 **CodeBlock 组件**。
- 渲染错误由 `MarkdownErrorBoundary` 兜底：出错降级为 `<pre>` 纯文本，不炸整棵树。
- Mermaid：流式期间显示源码（`complete=false` → `code` 态），完成后才切 `diagram` 态并懒加载渲染；支持代码/图切换、缩放、下载。
- LaTeX 整篇文档（`\documentclass`）走独立 `renderToString(displayMode)` 分支。

## 2. 代码块组件结构

```
div.cb-markdown-pre-wrapper
  div.cb-markdown-pre-container      # 1px border / radius 16 / overflow hidden
    div.cb-markdown-pre__header      # h36 / 底色 #f7f7f7 / padding 6-16
      strong.cb-markdown-pre__title  # 语言名，或 文件名 + L12-L30
      CodeBlockActions               # 复制(icon-only) / 应用 / 下载 / 分隔线
    pre.cb-markdown-pre > code.hljs  # padding 12/16, max-height 60vh, overflow auto
```

- 复制按钮点击后换 check 图标（成功色 #28b894），24px 高、6px 圆角、hover 有 4% 黑底。
- 代码块带 `meta`（```py:12-30:path/to/a.py）时，标题变成可点击文件名，点击回调打开编辑器并定位行。

## 3. 排版 token（`.cb-markdown`）

| 维度 | 值 |
|---|---|
| 正文 | 13px / 21px（行高 1.615） |
| 标题 | h1~h6 **与正文同字号**，仅 600 字重，段前后用小间距区分 |
| 段间距 | `--markdown-content-gap: 12px`，列表行距 4px |
| 列表缩进 | `padding-left: 2em`，`li>p/ul/ol` 清零避免双倍 |
| 表格 | 1px 外框 + 圆角 16、`border-spacing:0`、th/td `padding: 8px 16px`、表头 12px |
| 行内代码 | `background:#ffffff1a`、radius 3、左右 4px |
| 链接 | 无下划线，hover 变深 + 下划线 |
| 引用 | 左 4px 竖条 + 16px 内缩 |
| 代码块 | border `#ebebeb`、radius 16、header 36px |

另有 `data-md-theme=loose|reasoning` 两套排版档位（宽松阅读体 / 推理小字），通过 CSS 变量整体切换。

## 4. 性能与流式

- `MarkdownPreComponent` 用 `React.memo` 深度比较 `code/language/meta`，流式追加时**已完成代码块不重新高亮**。
- `MarkdownRendererComponent` 也 memo，比较 `children/complete/theme`。
- Mermaid 按 `hashContent(code)` 缓存渲染结果与折叠状态。

## 5. Qt 端可移植性判断

| harness 技术 | Qt 富文本可否移植 |
|---|---|
| 块级解析 + 行内解析分离 | ✅ 直接可做（当前实现是逐行正则） |
| 代码块卡片（header + 语言 + 复制） | ✅ 用 table 两行实现，复制用 `<a href>` + `anchorAt` |
| 排版 token（13/21、标题同级、8/16 表格） | ✅ 内联样式 |
| 块级渲染缓存 + 增量追加 | ✅ 缓存 HTML + `QTextCursor.insertHtml` |
| KaTeX / Mermaid | ❌ 无渲染引擎，降级为源码块 |
| radius 16 圆角 | ❌ Qt rich text 不支持 border-radius |
| QTextEdit 默认 WidgetWidth 折行 | ⚠️ 它会在每次 resize 时改写 document.textWidth，必须换 FixedPixelWidth |
| rehype-sanitize 白名单 | ✅ 等价做法：先 escape 再渲染，禁 raw HTML |
