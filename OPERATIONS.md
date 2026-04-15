# Gemini Reverse — 运维与排障手册

本文档供开发者和运维环境工程师使用，它覆盖了文件目录、数据溯源及异常灾备与排障标准动作。

---

## 📂 数据与存储源头 (Source of Truth)

所有的状态信息不会污染主存储区，被严格集中锁定在 AstrBot 框架挂载的数据专区内。
**绝对真理落盘目录：** `data/plugin_data/astrbot_plugin_gemini_reverse/`

| 物理文件 | 职责特征与风险边界 |
|:---:|:---|
| `runtime_config.json` | 专供底层独立 Uvicorn (Service Manager) 读取的单向渲染配置。绝不允许运维人员手改此文件。如果在面板执行新配置，该文件会自动覆写。 |
| `auth_repo.sqlite` | 核心认证票据库。存储了由外部脚本推送进来的热活跃令牌与防重放游标 (Nonce)。由强锁保证事务安全，不可裸读裸写。 |
| `reverse_sessions.sqlite3` | 负责记忆 AstrBot 上层发送源与 Google Base 网页会话 UUID 的绑定矩阵。如果该文件损毁，将导致上文联系遗失，但系统不崩溃。 |

---

## 🗃️ 审计日志切片设计 (Logging)

日志统一落于 `logs/` 子目录下。为保障安全，所有的 Cookie与 1PSID 都被截断至长度小于10的安全遮罩模式：

- `logs/tool_calls.jsonl`: AstrBot 发出的提示词外貌。
- `logs/request.jsonl`: 发送至内联 API 大模型的纯流量负载元数据统计。
- `logs/auth.jsonl`: Relay 会话降级、更新成功/失败告警（**【排查认证优先看此文件】**）。
- `logs/runtime.jsonl`: 后端自举端口是否争抢，热自愈事件记录。

---

## 🩺 探伤大盘: `/gemini_reverse doctor`

这不只是一个命令，这是本系统的体检核心！向机器人下达此指令，将获得如下维度的探针结果：

### 1. **`service` 节点**
- 检测与 Uvicorn 绑定的环回子进程的网络活态。
- 检测如果 `models_ok: false`，说明虽然端口起来了，但引擎启动崩溃，请查看后台终端或 `runtime.jsonl` 日志寻找 `Traceback`。

### 2. **`upstream` 节点**
- 这是基于代理侧直连 `https://gemini.google.com/app` 做的一组 HTTP Head 轻量探测请求。
- 如遇 `upstream_healthy: false` 代表当前寄宿宿主网络遭到阻断，此为绝对前置阻断错误。请处理梯子/网关节点，否则后面的全部功能皆无意义。

### 3. **`auth` 节点**
- `is_healthy` 将直观展示你的票号资源池子有没有弹尽粮绝。
- 如果底下的 `recent_events` 大量重现 `fallback_triggered` 或 `TTL Expired` 等错误流，意味着系统的免疫自动轮换正在无休止挣扎，必须尽快外置人工干预推送底层最新 Cookie。

---

## 💣 灾难回退标准动作 (Recovery Playbook)

### 故障 1：插件反复启动无法抢占端口 / Uvicorn 脱缰
**症状表象**：更新代码或频繁点重载以后，由于操作系统层面的异常阻断，进程未被消灭。
**干预路线**：
1. 立刻对机器人进行指令中止： `/gemini_reverse stop`。
2. 查找是否有脱管的残留进程：`ps -ef | grep uvicorn` 或在被占用主机上手工处理进程。
3. 执行 `/gemini_reverse start` 重启，或在 WebUI 重载 AstrBot。

### 故障 2：被 Google 源端识别，全系统陷入 `AuthError` 链式自旋崩溃
**症状表象**：请求完全吐不出来，控制台红海成灾，日志大面积弹出 `[AuthError definitively expired active ticket.]`
**干预路线**：
1. *切忌立刻盲目改配置或暴力关进程！* 你当前的 IP 已经被拉入灰名单或饭票过期老化。
2. 更换梯点节点并在你这端浏览器清理一下 Cookie。
3. 从新 IP 的新浏览器里登录小号拿到高纯净度 Cookie，打开本插件附带的 `relay_push.py`，执行一次远端硬性推送！
4. 系统接收验证后会利用热态恢复功能**直接接管业务断点**，而无需宕机影响机器人在线体验。
