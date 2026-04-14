# Gemini Local Proxy - 架构与核心机制解析报告

## 📖 项目简介
本项目是一个高度定制化的 **Gemini 本地代理服务器 (Gemini Local Proxy)**。它的核心目标是将 Google Gemini 的 Web 端接口（通过 `gemini_webapi` 驱动）包装为标准的 **OpenAI API 兼容接口**。这使得开发者可以将廉价甚至免费的 Gemini Web 模型无缝接入到 Cline、Continue、Cursor 等基于 OpenAI 协议的 AI 编程助手中。

## 🏗️ 核心架构与模块解析

项目采用解耦的模块化设计，主要分为以下几个核心层：

### 1. Web 服务入口层 (`main.py`)
- 基于 **FastAPI** 构建，提供了标准化的 `/v1/chat/completions` 路由。
- 支持流式 (Streaming) 和非流式响应的动态转换。
- 维护了全局的应用生命周期，并在启动时完成账号验证。
- **🔥 防脑补截断机制**：在流式输出中，一旦解析器捕获到完整的工具调用，立即切断后续文本输出，防止模型在触发工具后继续生成“假想”的执行结果。

### 2. 高可用客户端层 (`api_client.py` & `config.py`)
- **多账号自动轮换 (Failover)**：`GeminiConnection` 是该代理维持高稳定性的心脏。当代理侦测到 `UsageLimitExceeded` (请求配额耗尽) 或 `AuthError` (鉴权失效) 时，会自动从 `config.py` 配置的账号池 (`ACCOUNTS`) 中无缝切换到下一个账号并重试。
- **代理支持**：支持全局网络代理 (如 Clash/V2Ray)，确保网络连通性。

### 3. Prompt 注入与上下文引擎 (`context_manager.py` & `tool_adapter.py`)
- **XML 协议注入**：为了弥补 Gemini Web 端不支持原生 Function Calling 的缺陷，代理通过在 Prompt 头部注入 `TOOL_CALLING_SYSTEM_PROMPT`，强制 AI 使用严格的 `<tool_call>` XML 标签来进行工具调用。
- **多轮历史拍平**：`context_manager.py` 将 OpenAI 复杂的多轮对话历史拍平成单次长文本“阅读理解题”，确保 AI 的上下文连贯。
- **🔥 超长工具回包截断机制**：针对读取大文件导致的 `Stream interrupted` 问题，实施了**强硬截断 + AI 行为矫正**机制：
  - 单次工具输出限制在 **4000 字符**以内。
  - 保留前 2000 和后 2000 字符，并在中间注入醒目的 `[SYSTEM WARNING]`。
  - 警告文本直接在 Prompt 级别反向教育 AI 智能体，迫使其分块读取，规避 LLM 崩盘。

### 4. 实时流解析器 (`tool_parser.py`)
- **`StreamToolDecoder`**：在 Gemini 流式吐字的过程中，通过状态机引擎和正则表达式实时监控并拦截 XML 标签。
- 动态将拦截到的 `<tool_call>` 块转换为 OpenAI 标准的 `tool_calls` JSON 数组输出到前端。

### 5. 辅助调试与状态监控 (`logger.py` & `main.py`)
- 提供结构化日志存储于 `logs/tool_calls.jsonl`。
- 暴露 `/v1/debug/last` 接口，允许开发者直接查看底层经过合并、注入系统提示词后的最终超大 Prompt。

## 💡 总结
该代理不仅仅是一个简单的接口转发工具，更是一个**智能的 Prompt 组装引擎与自愈调度系统**。它通过高明的 XML 提示词工程和坚决的长文本防爆机制，补齐了 Gemini Web 接口在自动化 Agent 场景下的能力短板。