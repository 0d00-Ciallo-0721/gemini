# Gemini Reverse Standalone

## 启动前准备

1. 复制 `config.example.json` 为 `data/runtime_config.json`
2. 至少填写这些字段：
   - `proxy`
   - `accounts`
   - `admin_token`
   - `allowed_client_ips`
   - `api_keys`
3. 如果服务放在反向代理后面，把反代出口加到 `trusted_proxies`

## 关键配置说明

### 访问控制

- `allowed_client_ips`
  - 白名单来源直接放行
- `api_keys`
  - 不在白名单内的客户端，可以通过 `Authorization: Bearer ...` 或 `x-api-key` 访问主业务接口
- `admin_token`
  - `/v1/debug/*` 默认需要 `x-admin-token` 或 `Authorization: Bearer ...`
- `trusted_proxies`
  - 只有请求源命中这里时，服务才会信任 `X-Forwarded-For`

### 流式超时

- `stream_first_chunk_timeout_sec`
  - 首包等待超时
- `stream_idle_timeout_sec`
  - 首包之后的流式空闲超时

### 静态账号池

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

推荐用下面命令更新账号池：

```bash
python scripts/update_cookie.py
```

## 启动命令

```bash
python scripts/start_server.py --config ./data/runtime_config.json
```

## 主要接口

- `GET /v1/models`
- `POST /v1/chat/completions`
- `POST /v1/completions`
- `POST /v1/embeddings`
- `GET /healthz`
- `GET /readyz`
- `GET /v1/debug/status`
- `GET /v1/debug/network`
- `GET /v1/debug/doctor`

## 最小验收

```bash
curl http://127.0.0.1:8000/healthz
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

## 反向代理示例

```nginx
server {
    listen 443 ssl;
    server_name your-domain.example;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```
