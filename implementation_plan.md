# 实施计划：Gemini Reverse 失败追踪与可观测性升级 (Tracking & Observability)

本实施计划根据“失败追踪开发方案”的指示制定，核心目的是将原本黑盒的“Gemini Proxy Error”细化裂变，提供结构化的异常归类、代理环境溯源以及多维度的诊断探针，并在最终反馈给前端前形成结构化分类留底。

---

## 📋 实施细节 (Implementation Details)

### 阶段一：补齐运行态可观测信息 (Observability Enhancements)
1. **完善代理进程启动输出**
   - **目标对象**: `bundled_gemini/main.py` 中的 `lifespan` 生命周期钩子。
   - **执行内容**: 拓展当前启动的 `print` 仪表盘，利用 `config.py` 中的 `ACCOUNTS` 态与核心字段判空函数，输出脱敏后的全量认证信息：`active_model`, `proxy` 的值，确认是否含有关键字段 `SECURE_1PSID` / `SECURE_1PSIDTS` 及其附带 cookie 数量。

2. **扩充 `/v1/debug/status` 诊断能力**
   - **目标对象**: `bundled_gemini/main.py`
   - **执行内容**: 在原有的诊断返回 payload 外，追加暴露内部请求过程的“最近异常”（`last_request_error`, `last_request_error_type`）与核心验证态等布尔值 (`has_psid` / `cookies_dict_count` 等)，以实现无需查阅本机日志亦可快速确诊目标。

---

### 阶段二：异常确性与分类重构 (Error Classification & Logging)
3. **增强并在底层 API 层抽象错误类型**
   - **目标对象**: `bundled_gemini/api_client.py` 及自定义错误库定义（按需引入分类 Enum）。
   - **执行内容**: 抓取来自开源库或本地抛出的异常字符标识，在 `generate_with_failover` 和 `stream_with_failover` 捕捉段实现拦截分类器匹配，将其标准化抛出特定的 `ProxyException` 子类，并赋予如 `MODEL_NOT_SUPPORTED` / `AUTH_INVALID` / `NETWORK_OR_PROXY_ERROR` / `GOOGLE_SILENT_ABORT` 错误码。

4. **主服务降级反馈分类封装**
   - **目标对象**: `bundled_gemini/main.py` 中的 `chat_completions` 同异步异常捕获底口。
   - **执行内容**: 结构化捕获以上重定义错误，向 `SSE_Data` 推送附带更通俗诊断结果的 `[Gemini Proxy Error: AUTH_INVALID]` 甚至附送修复建议供用户参考，彻底杜绝混淆。通过 `request_logger` 结构化落库审计字段。

---

### 阶段三：代理穿透确定探针 (Proxy Traceability)
5. **代理传参声明化**
   - **目标对象**: `bundled_gemini/api_client.py` 中的 `__init__` 与 `initialize`。
   - **执行内容**: 初始化 `GeminiClient` 前直接以脱敏形态（含 Host & Port & Protocol）或者 `proxy=disabled` 打印代理传参凭证到标准流中，供工单自证排查。
   
6. **网络环回诊断探针 `/v1/debug/network`**
   - **目标对象**: `bundled_gemini/main.py`
   - **执行内容**: 新增 `GET` 端点，允许人工主动触发：检查并呈现 `runtime_config['proxy']` 值并验证是否已向全局 `PROXIES` 变量赋值并有效投射至连接池中；视复杂度情况或可简单附带 HTTP 探测结果。

---

### 阶段四：收束模型名校验漏洞 (Strict Model Whitelisting)
7. **硬化可用模型集边界**
   - **目标对象**: `bundled_gemini/main.py` 中的 `@app.get("/v1/models")` 接口。
   - **执行内容**: （方案A）不再反射返回用户在配置乱填的模型名，改为固化白名单。例如写死 `gemini-3.1-pro`, `gemini-3.0-pro`, `gemini-2.0-flash`、`gemini-2.5-flash` 等官方确认识别范围的节点集，限制插件前端（如 AstrBot 控制台）对无效模型的下拉切换空间。

---

### 阶段五：测试防线固化 (E2E Tracing Coverage)
8. **自动化测试**
   - **执行内容**: 新增 `test_error_tracking.py` 补充：
     1. 测试 `Proxy=""` 与存在实际合法赋值下，`api_client` 是否能如实透传状态；
     2. 发送伪造错误码（如包含 `"Unknown model"` 之类的 `Mock`），检查能否正确转化 `MODEL_NOT_SUPPORTED`；
     3. 发送带有 `"silently aborted"` 的错误请求查验分类能否命中 `GOOGLE_SILENT_ABORT`。

---

## ❓ 确认反馈 (Feedback Requested)
设计已针对“异常无头公案”问题闭环，请查看上面计划提及的文件落点是否符合你的需求？特别是第四个阶段——强制固定一个内置大模型白名单作为 API 模型枚举。如果确认符合预期的话，我将进入 P-E-V 执行环节。
