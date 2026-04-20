# gemini-reverse

`gemini-reverse` 是一个独立部署的 OpenAI 兼容网关服务。

它对外暴露标准风格的模型与对话接口，对内复用 Gemini 网页端 reverse 能力完成模型调用，适合部署在美国 VPS 或其他可稳定访问 Gemini 的环境中，供业务服务、本地电脑或其他系统通过统一 HTTP API 接入。

## 核心特性

- OpenAI 兼容接口
  - `GET /v1/models`
  - `POST /v1/chat/completions`
  - `POST /v1/completions`
  - `POST /v1/embeddings`
- 支持同步与流式对话
- 支持静态多账号池与自动切换
- 支持逻辑会话复用与持久化
- 支持 IP 白名单、API Key、Admin Token
- 支持 debug/doctor 运维接口
- 对关键错误返回统一的 OpenAI 风格错误对象

## 快速启动

1. 复制配置模板：

```bash
cp config.example.json data/runtime_config.json
```

2. 至少填写这些字段：

- `proxy`
- `accounts`
- `allowed_client_ips`
- `api_keys`
- `admin_token`

3. 启动服务：

```bash
python scripts/start_server.py --config ./data/runtime_config.json
```

4. 验证接口：

```bash
curl http://127.0.0.1:8000/healthz
curl http://127.0.0.1:8000/v1/models
```

## 配置说明

主要配置文件：

```text
data/runtime_config.json
```

关键字段：

- `host`
- `port`
- `model`
- `proxy`
- `session_db_path`
- `log_dir`
- `debug_routes_enabled`
- `debug_loopback_bypass_enabled`
- `allowlist_enabled`
- `allowed_client_ips`
- `trusted_proxies`
- `api_keys`
- `admin_token`
- `stream_first_chunk_timeout_sec`
- `stream_idle_timeout_sec`
- `accounts`

### 访问控制

- 白名单来源命中 `allowed_client_ips` 时可直接访问主业务接口
- 非白名单来源需要有效 `api_keys`
- `trusted_proxies` 仅用于可信反代场景下解析 `X-Forwarded-For`

### Debug 鉴权

debug 路由是否注册由 `debug_routes_enabled` 控制。

`debug_loopback_bypass_enabled` 控制 loopback 是否可绕过 admin token：

- `true`
  - `127.0.0.1` / `::1` / `localhost` 来源访问 debug 路由时，可免 `admin_token`
- `false`
  - 所有 debug 请求都必须携带有效 `admin_token`

注意：

- loopback bypass 只作用于 debug 路由的 admin token 校验
- 它不影响主业务接口的 allowlist / API key 逻辑

## 主要接口

### `GET /healthz`

存活检查。

### `GET /readyz`

就绪检查，返回当前客户端初始化状态、活跃模型和账号。

### `GET /v1/models`

返回模型列表。正式契约中每个 model item 的 `owned_by` 固定为：

```json
"owned_by": "gemini-reverse"
```

### `POST /v1/chat/completions`

主聊天接口，支持同步与流式。

### `POST /v1/completions`

兼容 completion 风格请求。

### `POST /v1/embeddings`

当前固定返回：

- `501`
- `EMBEDDINGS_NOT_SUPPORTED`

## 鉴权说明

### API Key

支持两种方式：

```http
Authorization: Bearer <api-key>
```

或：

```http
x-api-key: <api-key>
```

### Admin Token

debug 路由支持两种方式：

```http
x-admin-token: <admin-token>
```

或：

```http
Authorization: Bearer <admin-token>
```

## Debug / 运维接口

正式公开并承诺维护的 debug 路由只有以下 5 个：

- `GET /v1/debug/status`
- `GET /v1/debug/network`
- `GET /v1/debug/doctor`
- `POST /v1/debug/auth/push_ticket`
- `GET /v1/debug/auth/status`

内部接口，不作为正式公开契约维护：

- `GET /v1/debug/last`
- `GET /v1/debug/logs`

## 错误响应

非流式错误统一返回：

```json
{
  "error": {
    "message": "...",
    "type": "...",
    "code": "..."
  }
}
```

当前已固定覆盖的关键错误码包括：

- `ACCESS_DENIED`
- `ADMIN_TOKEN_REQUIRED`
- `CLIENT_NOT_READY`
- `EMBEDDINGS_NOT_SUPPORTED`
- `SESSION_DB_PERMISSION_ERROR`
- `USAGE_LIMIT_EXCEEDED`

## AstrBot 接入

仓库保留了 AstrBot 相关能力与历史兼容逻辑，但当前主线定位已经是 standalone 服务。

如果你要在 AstrBot 中接入，推荐方式是：

- 将 `gemini-reverse` 作为独立 OpenAI 兼容服务部署
- 再由 AstrBot 通过 OpenAI 风格 provider 指向该服务

也就是说，AstrBot 现在是接入场景之一，而不是仓库的主叙事。
