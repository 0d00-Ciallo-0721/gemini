# 流式响应链路增强与诊断误判修正

我们完成了对流式响应链路的可观测性加固，并修复了诊断命令中存在的误导性超时判定。

## 主要变更

### 1. 修正流式诊断逻辑 ([main.py](file:///c:/Users/zlj/Desktop/llm/gemini/main.py))
- **双重超时判定**：现在会区分“首个 SSE 块超时”（45秒）和“后续内容块空闲超时”（20秒）。
- **精准报错**：不再将短时间的空闲超时误报为“45秒首包未返回”。
- **返回包增强**：`/gemini_reverse api stream` 返回的 JSON 中现在包含 `first_line_received` 标记和已收到的 `lines` 预览，方便观察是否只收到了 role 声明。

### 2. 增强独立后端日志 ([bundled_gemini/main.py](file:///c:/Users/zlj/Desktop/llm/gemini/bundled_gemini/main.py))
- **首块开始追踪**：流式路由现在会打印 `📡 [Stream] 正在开启上游流式生成`。
- **内容增量监控**：每收到 10 个 chunk 或收到较大文本块时，都会打印 `📥 [Chunk #N] 收到正文增量 (M chars)`。
- **空流警报**：如果流正常结束但全程没有正文输出，会打印明显的 `❌ [Stream] 警告：上游流已结束，但全程未收到任何正文！`。

## 验证与观察建议

请按以下顺序执行测试，并在 **AstrBot 控制台** 观察实时流式日志：

### 步骤 A：验证非流式（作为基准）
执行：`/gemini_reverse api sync 你好`
> [!NOTE]
> 预期：返回 200 并伴有正文内容。

### 步骤 B：执行改进后的流式验证
执行：`/gemini_reverse api stream 你好`
> [!IMPORTANT]
> **观察点 1**：如果报错，看 JSON 中的 `error_type`。
> - `DirectAPIProbeFirstLineTimeout`：连第一行 role 都没发出来（极度拥堵/连接断开）。
> - `DirectAPIProbeIdleTimeout`：role 出来了，但正文卡在了 Google 内部。
>
> **观察点 2**：查看控制台日志。
> - 看是否有 `[Chunk #N] 收到正文增量` 打印。如果没有这些打印但流结束了，确认是否输出了 `❌ [Stream] 警告...全程未收到任何正文`。

## 代码完整性检查
- [x] `main.py` 语法检查通过。
- [x] `bundled_gemini/main.py` 语法检查通过。
- [x] 核心业务逻辑（会话恢复、附件处理、迁移环）均已完整保留。

---

如果流式测试仍然只有 role 没有正文，请将控制台中带有 `[Stream]` 字样的最新日志贴给我，我们将据此定位上游空流的具体成因。
