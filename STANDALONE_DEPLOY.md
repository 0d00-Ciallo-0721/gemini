# Gemini Reverse Standalone 部署说明

## 启动前准备

1. 复制模板：

```bash
cp config.example.json data/runtime_config.json
```

2. 至少填写以下字段：

- `proxy`
- `accounts`
- `allowed_client_ips`
- `api_keys`
- `admin_token`

3. 如果服务部署在反向代理后面，把反代出口加入 `trusted_proxies`

## 关键配置

### 访问控制

- `allowed_client_ips`
  - 命中白名单的来源可直接访问业务接口
- `api_keys`
  - 不在白名单内的来源必须提供有效 key
- `admin_token`
  - debug 路由使用的管理令牌
- `trusted_proxies`
  - 仅在请求源命中这里时，服务才信任 `X-Forwarded-For`

### Debug 相关配置

- `debug_routes_enabled`
  - 为 `false` 时，不注册任何公开 debug 路由
- `debug_loopback_bypass_enabled`
  - 为 `true` 时，来自 `127.0.0.1` / `::1` / `localhost` 的 debug 请求可免 admin token
  - 为 `false` 时，所有 debug 请求都必须带有效 admin token

注意：

- loopback bypass 只作用于 debug 路由的 admin token 校验
- 它不绕过 allowlist / API key 主链路

### 账号池

`accounts` 使用静态多账号池格式：

```json
{
  "1": {
    "label": "account_1",
    "cookie": "PASTE_FULL_COOKIE_HERE",
    "SECURE_1PSID": "PASTE_SECURE_1PSID_HERE",
    "SECURE_1PSIDTS": "PASTE_SECURE_1PSIDTS_HERE"
  }
}
```

推荐使用：

```bash
python scripts/update_cookie.py
```

## 启动命令

```bash
python scripts/start_server.py --config ./data/runtime_config.json
```

## 正式公开接口

- `GET /healthz`
- `GET /readyz`
- `GET /v1/models`
- `POST /v1/chat/completions`
- `POST /v1/completions`
- `POST /v1/embeddings`
- `GET /v1/debug/status`
- `GET /v1/debug/network`
- `GET /v1/debug/doctor`
- `POST /v1/debug/auth/push_ticket`
- `GET /v1/debug/auth/status`

### `/v1/models` 契约

返回模型列表时，每个 model item 固定包含：

```json
{
  "id": "gemini-3-flash",
  "object": "model",
  "created": 1776271617,
  "owned_by": "gemini-reverse"
}
```

## 最小验收

```bash
curl http://127.0.0.1:8000/healthz
curl http://127.0.0.1:8000/readyz
curl http://127.0.0.1:8000/v1/models
```

非流式：

```bash
curl http://127.0.0.1:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d "{\"model\":\"gemini-3-flash\",\"messages\":[{\"role\":\"user\",\"content\":\"你好\"}]}"
```

流式：

```bash
curl http://127.0.0.1:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d "{\"model\":\"gemini-3-flash\",\"stream\":true,\"messages\":[{\"role\":\"user\",\"content\":\"你好\"}]}"
```

Debug：

```bash
curl http://127.0.0.1:8000/v1/debug/status -H "x-admin-token: YOUR_ADMIN_TOKEN"
```

如果 `debug_loopback_bypass_enabled=true`，则本机 loopback 调试请求可不带 `x-admin-token`。
