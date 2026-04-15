# AstrBot 插件：Gemini Reverse

`astrbot_plugin_gemini_reverse` 是一个给 AstrBot 使用的 Gemini reverse 插件。

它的目标不是接入 Gemini 官方 API，而是把 Gemini 网页端能力封装成一个独立的本地代理服务，再通过 AstrBot 已有的 `openai_chat_completion` provider 方式接入框架。

这意味着：

- 不修改 AstrBot 框架源码
- 不新增 AstrBot 内置 provider type
- 继续走 AstrBot 现有 provider 体系
- 同时兼容 AstrBot 原生对话和 AstrMai lane 会话体系

---

## 1. 插件能做什么

这个插件当前具备以下能力：

- 把 Gemini reverse 服务封装成独立 AstrBot 插件
- 自动生成并维护一个可供 AstrBot 使用的 `openai_chat_completion` provider
- 对外暴露显式 reverse provider 特征，供 AstrMai 稳定识别
- 在同一逻辑会话中复用同一个 Gemini 网页物理窗口
- 为不同任务或不同 lane 分配不同物理窗口
- 在 reverse 服务或 AstrBot 重启后恢复原窗口
- 面向个人用户的极简首次填 Cookie 认证设计（内部封装长期饭票续期机制）
- 同时保留给高级玩家的高可用多设备独立 Relay 推送接口
- 提供一套可直接使用的运维命令和调试接口

---

## 2. 适用场景

适合这些场景：

- 你已经在用 AstrBot，希望把 Gemini 网页端接入为聊天 provider
- 你同时在使用 AstrMai，希望 lane 级会话能稳定复用物理窗口
- 你不想频繁手工替换 Cookie，希望通过本地 helper 推送“长期饭票”
- 你希望 reverse 服务和 AstrBot 主体相对解耦，降低热重载影响

不适合这些场景：

- 你需要 Gemini 官方 API 的正式商用 SLA
- 你希望插件自己完成 Google 登录或自动过 2FA
- 你希望完全摆脱上游网页端变化带来的不稳定性

---

## 3. 工作原理

插件分成两层：

- 插件控制层
  - 负责读取 AstrBot 配置
  - 负责同步 provider
  - 负责管理 reverse 服务生命周期
  - 负责注入 reverse session 哨兵
  - 负责输出 `status` / `doctor`

- reverse 服务层
  - 负责对外提供 OpenAI 兼容接口
  - 负责 Gemini Cookie / relay ticket 认证
  - 负责逻辑会话与 Gemini 物理窗口绑定
  - 负责 refresh / fallback / session 恢复

一句话理解：

AstrBot 不直接碰 Gemini 网页协议，AstrBot 只连本插件拉起的本地 OpenAI 兼容代理。

---

## 4. 安装前提

请先确认：

- 你已经有可运行的 AstrBot 环境
- 你知道 AstrBot 当前使用的是哪个 Python 环境
- 你能够在该 Python 环境中安装依赖

本插件不会在导入时自动执行 `pip install`。依赖必须由你手动安装。

---

## 5. 安装步骤

### 5.1 放置插件

将本插件目录放到 AstrBot 的插件目录下，例如：

```text
<AstrBot根目录>/data/plugins/gemini/
```

确保入口文件是：

```text
main.py
```

### 5.2 安装依赖

在 AstrBot 实际运行的 Python 环境中执行：

```bash
pip install fastapi uvicorn httpx gemini_webapi
```

如果你是 Windows + 虚拟环境，通常类似：

```bash
<你的venv>/Scripts/python -m pip install fastapi uvicorn httpx gemini_webapi
```

### 5.3 启动 AstrBot

启动后，插件会尝试：

- 解析配置
- 准备运行时目录
- 在需要时拉起 reverse 服务
- 同步 `gemini_reverse` provider

---

## 6. 推荐配置方式

对于个人自用场景，你**仅需**：

1. 在控制面板的【3/3 首次认证】中，填入 `bootstrap_cookie`。
2. 启动服务。

首次使用时只需要填写一次完整 Cookie，插件会自动接管并尽量长期维持登录状态；只有当谷歌彻底判定票据失效时，才需要重新补一次。
如果你有跨机器同步需求，再启用下方的高级模式。

## 7. 配置项说明

下面按插件配置分组说明。

### 7.1 【1/3 基础服务】

`managed_service`

- 是否由插件托管 reverse 服务进程
- 推荐：`true`
- 开启后，插件会自动拉起、停止和检查本地服务

`host`

- reverse 服务监听地址
- 默认：`127.0.0.1`

`port`

- reverse 服务监听端口
- 默认：`8000`

`proxy`

- 访问 Gemini 网页服务使用的网络代理
- 支持 HTTP 和 SOCKS5

---

### 7.2 【2/3 平台接入】

`provider_name`

- AstrBot 面板里显示的 provider 名称

`provider_id`

- 一般无需修改。如果要并行开多个引擎实例，则可修改保证唯一。

`model`

- 默认大模型版本，例如：`gemini-3.1-pro`

---

### 7.3 【3/3 首次认证】

`bootstrap_cookie`

- **核心项：** 首次使用时，请粘贴一段完整的 Gemini / Google 浏览器 Cookie。
- 只有当前票据被上游判定彻底失效无法补救时，才需要这里重新手工填写。

---

### 7.4 【高级验证选项】

这类选项默认不可见或作为高级选项提供，只有你需要无缝跨机器热更新长效票据时才需介入。

`relay_shared_secret` 和 `relay_primary_client_id` 控制允许安全地接收来自你本机的 `relay_push.py` 将最新票据打过来。

### 7.5 【高级兜底】备用 Cookie 池

`accounts`

- 可选。仅在主票据失效且自动续期雪崩时，作为最后的防线使用。
- 普通用户可以直接留空。

## 8. 使用模式

### 8.1 个人极简模式（推荐）

这是默认和推荐的方式。

步骤：

1. 打开浏览器登录 Gemini 获取 F12 抓到的完整请求 Cookie。
2. 填入插件配置 `bootstrap_cookie`，保存并重启。
3. 接下来你就不用管了！插件会在后台负责巡检和接管！

### 8.2 高级推送模式 (Relay)

适合进阶用户或存在本地 helper 常驻抓包进程的机器。

步骤：

1. 配置里检查设好 `高级：外部票据推送密钥`（`relay_shared_secret`）。
2. 在浏览器所在的宿主机上使用 `relay_push.py`。
3. 每次抓到新 Cookie 或发生自动更新时，使用脚本打向当前 AstrBot 及其插件服务端口。
4. 服务端接收后立刻热更新，不中断当前回答。

## 9. 如何使用 relay_push.py

`relay_push.py` 是本地 helper 脚本。

它的作用是：

- 从你本地准备好的 Cookie 输入中构造 relay payload
- 带上：
  - `timestamp`
  - `nonce`
  - `payload_hash`
  - `signature`
- 向服务端推送最新 ticket

使用前请先确认：

- 服务端插件已启动
- `relay_shared_secret` 已配置
- `relay_primary_client_id` 与 helper 使用的一致

典型使用思路：

```bash
python relay_push.py
```

或按你本地实际实现传参。

推送成功后，理论上不需要重启 AstrBot。

---

## 10. Provider 行为说明

本插件不会新增 AstrBot 内置 provider type。

它会继续生成一个：

```text
type = openai_chat_completion
```

的 provider，并补上 reverse 特征字段，例如：

- `reverse_provider=gemini_web`
- `reverse_plugin=astrbot_plugin_gemini_reverse`
- `reverse_kind=gemini_web`
- `gemini_reverse=true`
- `supports_reverse_session=true`
- `reverse_session_via=system_prompt`

这能让 AstrMai 稳定识别，而不是再靠启发式判断。

### 关于 provider 是否会重复创建

当前逻辑会优先按 `provider_id` 查询已有 provider。

- 如果找到已有 provider：直接复用
- 只有找不到时才会创建

所以在 `provider_id` 不变、AstrBot `provider_manager` 正常工作的前提下，热重载不应重复生成新的 provider。

---

## 11. 会话与物理窗口行为

当前插件实现了以下行为：

- 同一逻辑会话复用同一个 Gemini 物理窗口
- 不同任务或不同 lane 使用不同物理窗口
- reverse 服务重启后，从持久化记录恢复原窗口

这依赖：

- reverse session 哨兵注入
- `reverse_sessions.sqlite3`
- Gemini metadata 恢复链路

这部分已经兼容：

- AstrBot 原生对话
- AstrMai lane 会话体系

---

## 12. 常用命令

插件支持这些命令：

`/gemini_reverse status`

- 查看当前运行状态
- 包括服务、端口、provider、auth 状态等

`/gemini_reverse doctor`

- 深度诊断
- 检查服务、认证、存储、上游可达性、最近事件

`/gemini_reverse start`

- 手动启动 reverse 服务

`/gemini_reverse stop`

- 手动停止 reverse 服务

`/gemini_reverse restart`

- 重启 reverse 服务

`/gemini_reverse provider_profile`

- 输出当前 provider profile
- 适合联调 AstrMai 识别逻辑时使用

---

## 13. 运行期文件说明

插件运行后，重要文件通常在：

```text
data/plugin_data/astrbot_plugin_gemini_reverse/
```

常见文件包括：

`runtime_config.json`

- reverse 服务实际读取的运行时配置

`auth_repo.sqlite`

- relay ticket 认证仓库

`reverse_sessions.sqlite3`

- 逻辑会话与 Gemini 物理窗口映射

`logs/`

- 请求、认证、运行时相关日志

---

## 14. 排障建议

如果插件无法正常工作，建议按顺序检查：

### 1. 先看 doctor

```text
/gemini_reverse doctor
```

重点看：

- 服务是否 running
- auth 是否 healthy
- upstream 是否可达
- 最近 events 有没有连续失败

### 2. 检查依赖

确认当前 Python 环境中已安装：

```bash
pip install fastapi uvicorn httpx gemini_webapi
```

### 3. 检查 relay 配置

确认：

- `relay_shared_secret` 已改成真实值
- `relay_primary_client_id` 与 helper 一致
- helper 推送签名正确

### 4. 检查 Cookie 是否过期

如果频繁出现 `AuthError`：

- 优先怀疑 Cookie 过期或失效
- 然后检查代理/IP 环境
- 再看 fallback 是否已经生效

### 5. 检查端口冲突

如果服务起不来：

- 看 `port` 是否被别的进程占用

---

## 15. 安全建议

请务必注意：

- 不要把真实 Cookie、`relay_shared_secret` 提交到仓库
- 不要把 `relay_shared_secret` 保持默认值
- 不要把服务监听在公网可直接访问的地址，除非你明确做了额外防护
- 不要让不可信设备拥有主 `client_id`

---

## 16. 已知限制

当前已知限制包括：

- 这是 reverse 方案，不是官方 Gemini API
- 稳定性仍然会受上游网页端和 `gemini_webapi` 变化影响
- 某些 Windows 控制台环境下，个别带 emoji 的日志仍可能出现编码噪音
- AstrBot 当前 `@register` 使用会有 deprecated warning，暂不影响使用，但后续需要迁移

---

## 17. 推荐上线方式

推荐这样用：

1. 先在测试机或灰度环境部署
2. 首次填写 `bootstrap_cookie` 后跑一段时间
3. 观察：
   - doctor 输出
   - auth 状态稳定性
   - session 恢复是否正常
   - 是否存在需要依靠高级备用池 (fallback) 或外部推送 (relay) 的跨设备需要
4. 稳定后再进入正式长期运行

---

## 18. 当前验收状态

当前版本已经完成：

- 功能开发
- 工业化能力收口
- 规范化文档整理

并通过本地回归：

```text
pytest -q
27 passed
```

---

## 19. 相关文档

如果你需要更偏维护者或运维视角的信息，请继续查看：

- [C:/Users/zlj/Desktop/llm/gemini/OPERATIONS.md](C:/Users/zlj/Desktop/llm/gemini/OPERATIONS.md)
- [C:/Users/zlj/Desktop/llm/gemini/implementation_plan.md](C:/Users/zlj/Desktop/llm/gemini/implementation_plan.md)
- [C:/Users/zlj/Desktop/llm/gemini/task.md](C:/Users/zlj/Desktop/llm/gemini/task.md)

这些文档分别对应：

- 运维排障
- 架构与实现说明
- 当前任务与演进记录
