# -*- coding: utf-8 -*-
"""
Spyder Copilot Chat — 偏好设置页面
"""
from qtpy.QtWidgets import QVBoxLayout, QLabel, QTabWidget, QWidget
from spyder.api.preferences import PluginConfigPage


class CopilotChatConfigPage(PluginConfigPage):
    """偏好设置页面"""

    def setup_page(self):
        tabs = QTabWidget()
        self._build_overview_tab(tabs)
        self._build_providers_tab(tabs)
        self._build_agent_tab(tabs)

        layout = QVBoxLayout()
        layout.addWidget(tabs)
        self.setLayout(layout)

    def _build_overview_tab(self, tabs):
        tab = QWidget()
        layout = QVBoxLayout(tab)
        lbl = QLabel(
            "Spyder Copilot Chat v0.8.3 · Visible Range Edition\n\n"
            "将 GitHub Copilot Chat (v0.48.1) 深度集成到 Spyder 6，\n"
            "并按 VS Code Cline 的交互方式重构了 Agent 工作流。\n\n"
            "v0.8.3：红绿行现在真的会出现了——装饰先滚到改动处再挂，\n"
            "并在滚动 / 切标签页时重算可见范围（Spyder 只渲染可见行的装饰）。\n"
            "v0.8.2：编辑器内联 diff 失败不再静默——对话里直接说明原因。\n"
            "v0.8.1：Agent 每改一次代码，改动都会画成「删行红底 / 增行绿底」——\n"
            "对话里贴一份可回看的卡片，编辑器里同步内联高亮并逐处接受。\n"
            "深色主题的 diff 底色重定色相，红绿一眼可分。\n"
            "v0.8.0：Agent 不再跑到 25 轮就停下要求回复「继续」——改成软提醒\n"
            "（默认 40 轮只提示收束）＋ 硬预算（默认 200 轮，0 = 不限）＋\n"
            "自动续跑（最多 5 次），只有重复的写/执行操作才硬停。\n"
            "v0.7.9：修改直接标进代码编辑区——新增行绿底、删除行红底加删除线，\n"
            "悬停行尾浮出 ✓ / ✗ 逐处接受撤销，顶部工具条报「第几个/共几个文件」并给\n"
            "「接受全部修改 / 不接受本次修改」，Ctrl+Enter 接受全部、Ctrl+Backspace 全部不接受。\n"
            "v0.7.8：字号跟着设置走，图标全部换成矢量——任务面板、Plan/Act 与\n"
            "Agent 胶囊、发送按钮都是同一套几何语言，气泡里不再有彩色 emoji。\n"
            "v0.7.7：补丁按行标红标绿——删一行整行红、加一行整行绿，左侧双行号，\n"
            "相邻的 -/+ 块再做词级高亮；unified diff、git 文件头、Codex V4A 都认。\n"
            "v0.7.6：LaTeX 公式终于能看了——$$...$$ 居中排版成图、$...$ 跟在文字里，\n"
            "用 matplotlib 的 mathtext 引擎在本机排版，深浅主题各自取色。\n"
            "v0.7.5：Agent 选择器全中文化——默认助手 / 探索助手 / 调研助手 / 代码审查 /\n"
            "任务执行 / 方案规划 / 橡皮鸭，不再混着 Default Agent 这类英文名。\n"
            "v0.7.4：修掉「整条回复不渲染 Markdown」——工具卡片插入时末尾差一个空行，\n"
            "下一段正文粘在收尾的围栏上，围栏合不上，后面的标题 / 列表 /\n"
            "表格全被吞进代码块；围栏解析同时加了容错与自愈。\n"
            "v0.7.3：修掉 composer 里文字看不全的毛病——Agent 选择器改成「胶囊 + 菜单」，\n"
            "七项全名一次排开（不再出现 Def…ent / Co…nt），胶囊上只用短名。\n"
            "v0.7.2：AI 进入编辑区——输入时停顿即出灰字，Tab 接受、Esc 丢弃；\n"
            "Ctrl+K 可就地改写选中代码（先看 diff 再落盘）。\n"
            "v0.7.1：读取编辑器光标与选区，按「选区 / 整个函数 / 整份文件」\n"
            "附加到提问（对标 Cline），并在输入框上方显示可点的上下文胶囊。\n"
            "v0.7.0：参照 DeepSeek Harness 的渲染栈重写 Markdown 管线——\n"
            "块级解析、GFM 表格/任务列表/删除线、代码块卡片（语言 + 行数 +\n"
            "逐块复制）、流式增量渲染（回复边到边排版，帧成本 < 5ms）。\n"
            "v0.6.2：修正暗色 IDE 下变白板 / 顶栏黑条 / 气泡吃半屏 /\n"
            "代码块色带与溢出，顶栏与输入区随侧栏宽度自适应。\n"
            "v0.6.1：配色从 Spyder 自身的色板（qdarkstyle）派生，\n"
            "底板与编辑器同色、强调色用 Spyder 蓝，换肤即时跟随。\n"
            "v0.6.0：语义化亮/暗双主题、矢量图标、紧凑顶栏、\n"
            "底部统一输入区、可折叠工具痕迹。\n\n"
            "核心特性：Plan/Act 模式、内联审批 diff、检查点撤销\n\n"
            "主要功能：\n"
            "  · AI 聊天面板 — 多 Provider 支持（10 个服务商）\n"
            "  · Agent 模式 — AI 可以读写文件、搜索代码、执行命令\n"
            "  · 子 Agent 系统 — searchSubagent / executionSubagent / Fleet\n"
            "  · Git 工具集 — getChangedFiles / git_add / git_commit / 代码审查\n"
            "  · 补丁应用 (applyPatch) — 精确编辑文件\n"
            "  · Notebook 操作 — 创建/编辑 Jupyter Notebook\n"
            "  · 内联聊天 — 在编辑器中快速对话\n"
            "  · FIM 代码补全 — 内联代码补全\n"
            "  · 斜杠命令 — /fix, /explain, /tests, /plan, /review 等\n"
            "  • 📁 项目上下文 — 自动收集项目文件\n"
            "  • 🧠 记忆系统 — AI 可保存和回忆信息\n"
            "  • 📝 自定义指令 (.instructions.md) + 自定义 Agent (.agent.md)\n"
            "  • 🔌 MCP 服务器集成\n\n"
            "如何在聊天面板中配置：\n"
            "  1. 打开聊天面板（查看 → 窗格 → Copilot Chat）\n"
            "  2. 点击右上角 ⚙ 按钮\n"
            "  3. 选择 Provider 并输入 API Key\n\n"
            "详细文档请访问聊天面板中的 ⚙ 设置。"
        )
        lbl.setWordWrap(True)
        layout.addWidget(lbl)
        layout.addStretch()
        tabs.addTab(tab, "概述")

    def _build_providers_tab(self, tabs):
        tab = QWidget()
        layout = QVBoxLayout(tab)

        info = (
            "支持的 AI 服务商：\n\n"
            "🔵 GitHub Copilot\n"
            "  需要有效 GitHub Copilot 订阅。\n"
            "  在聊天面板中点击「登录 GitHub」，按提示完成设备认证。\n\n"
            "🟢 DeepSeek\n"
            "  注册：platform.deepseek.com → 获取 API Key\n"
            "  API 地址：https://api.deepseek.com（OpenAI 兼容格式）\n"
            "  模型：deepseek-flash（默认，V4.1-Flash）/ deepseek-v4-pro\n"
            "  文档：api-docs.deepseek.com/zh-cn/\n"
            "  思考模式默认关闭；开启后 temperature 不生效，且\n"
            "  Agent 工具调用需要更大的最大 Token（≥65536）。\n\n"
            "🟡 OpenAI\n"
            "  注册：platform.openai.com → 获取 API Key\n"
            "  推荐模型：gpt-4o\n\n"
            "🟣 Anthropic (Claude)\n"
            "  注册：console.anthropic.com → 获取 API Key\n"
            "  推荐模型：claude-sonnet-4-20250514\n\n"
            "🔴 Ollama（本地）\n"
            "  安装：ollama.ai → 下载运行\n"
            "  推荐模型：qwen2.5-coder\n"
            "  无需 API Key\n\n"
            "⚪ 其他：xAI Grok / Google Gemini / OpenRouter / Azure OpenAI\n"
            "  均需对应平台的 API Key，在聊天面板设置中配置。"
        )
        lbl = QLabel(info)
        lbl.setWordWrap(True)
        layout.addWidget(lbl)
        layout.addStretch()
        tabs.addTab(tab, "Provider 指南")

    def _build_agent_tab(self, tabs):
        tab = QWidget()
        layout = QVBoxLayout(tab)
        lbl = QLabel(
            "Agent 模式说明\n\n"
            "Agent 模式下 AI 可以执行以下操作：\n"
            "  • 读取/创建/编辑/删除文件\n"
            "  • 搜索文件内容\n"
            "  • 运行终端命令\n"
            "  • 获取网页内容\n"
            "  • 保存/回忆信息\n"
            "  • 子 Agent 搜索与执行（searchSubagent / executionSubagent）\n"
            "  • Fleet 并行执行模式\n"
            "  • Git 操作（查看更改、提交、分支管理）\n"
            "  • 代码审查（review_changes）\n"
            "  • 补丁应用（applyPatch）\n"
            "  • Notebook 操作（创建/编辑单元格）\n"
            "  • 任务管理（manage_todo_list）\n\n"
            "安全提示：\n"
            "  • 首次使用建议关闭「自动执行工具」\n"
            "  • AI 执行的命令会显示在聊天中\n"
            "  • 工具调用次数上限为 100 轮\n\n"
            "建议的模型：\n"
            "  • DeepSeek Chat（推荐）\n"
            "  • GPT-4o / GPT-4o-mini\n"
            "  • Claude Sonnet 4\n"
            "  • Grok-3"
        )
        lbl.setWordWrap(True)
        layout.addWidget(lbl)
        layout.addStretch()
        tabs.addTab(tab, "Agent 说明")
