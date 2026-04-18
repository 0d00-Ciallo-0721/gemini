# Gemini Reverse Standalone

## 启动

1. 复制 `config.example.json` 为 `data/runtime_config.json`
2. 填写 `proxy`、`admin_token` 和 `accounts`
3. 启动服务：

```bash
python scripts/start_server.py --config ./data/runtime_config.json
```

## 账号池

`accounts` 使用静态多账号池格式：

```json
{
  "1": {
    "label": "account_1",
    "cookie": "完整 Cookie 字符串",
    "SECURE_1PSID": "xxx",
    "SECURE_1PSIDTS": "xxx"
  }
}
```

推荐用：

```bash
python scripts/update_cookie.py
```

把最新 Cookie 写回 `data/runtime_config.json`。

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

## 访问控制

- 主业务接口依赖 `allowed_client_ips`
- `/v1/debug/*` 默认还要求 `admin_token`
- 反向代理后请传递 `X-Forwarded-For`

## VPS 反向代理示例

Nginx:

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
