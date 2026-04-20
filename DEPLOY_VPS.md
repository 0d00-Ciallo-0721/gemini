# Gemini Reverse VPS 部署指南

本文档面向 standalone 场景：将 `gemini-reverse` 作为一个独立的 OpenAI 兼容服务部署到 VPS，对外提供统一 HTTP 接口。

## 1. 推荐目录

```bash
/opt/gemini-reverse
```

推荐创建专用用户：

```bash
sudo useradd -r -s /bin/bash -m gemini
sudo mkdir -p /opt/gemini-reverse
sudo chown -R gemini:gemini /opt/gemini-reverse
```

## 2. 上传项目

```bash
rsync -avz ./ user@your-vps:/opt/gemini-reverse/
```

## 3. 安装运行环境

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip nginx
cd /opt/gemini-reverse
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
```

## 4. 准备配置

```bash
cp config.example.json data/runtime_config.json
```

建议起步配置：

```json
{
  "host": "127.0.0.1",
  "port": 8000,
  "model": "gemini-3-flash",
  "proxy": "socks5://127.0.0.1:40000",
  "debug_routes_enabled": true,
  "debug_loopback_bypass_enabled": true,
  "allowlist_enabled": true,
  "allowed_client_ips": [
    "127.0.0.1/32",
    "YOUR_BUSINESS_SERVER_IP/32"
  ],
  "trusted_proxies": [
    "127.0.0.1/32",
    "::1/128"
  ],
  "api_keys": [
    "REPLACE_WITH_A_LONG_RANDOM_API_KEY"
  ],
  "admin_token": "REPLACE_WITH_A_LONG_RANDOM_ADMIN_TOKEN",
  "active_account": "1",
  "accounts": {
    "1": {
      "label": "account_1",
      "cookie": "PASTE_FULL_COOKIE_HERE",
      "SECURE_1PSID": "PASTE_SECURE_1PSID_HERE",
      "SECURE_1PSIDTS": "PASTE_SECURE_1PSIDTS_HERE"
    }
  }
}
```

### 访问控制建议

- 业务服务器公网 IP：加入 `allowed_client_ips`
- 本地电脑或临时客户端：通过 `api_keys` 访问
- debug 路由：使用 `admin_token`

### Debug 鉴权说明

- `debug_routes_enabled=false`
  - 不注册公开 debug 路由
- `debug_routes_enabled=true`
  - `debug_loopback_bypass_enabled=true`
    - loopback 来源的 debug 请求可免 admin token
  - `debug_loopback_bypass_enabled=false`
    - 所有 debug 请求都必须带有效 admin token

注意：loopback bypass 只影响 debug 的 admin token，不影响 allowlist / API key 主链路。

## 5. 更新 Cookie

```bash
cd /opt/gemini-reverse
source .venv/bin/activate
python scripts/update_cookie.py
```

写回目标：

```bash
data/runtime_config.json
```

## 6. 直接启动验证

```bash
cd /opt/gemini-reverse
source .venv/bin/activate
python scripts/start_server.py --config ./data/runtime_config.json
```

本机验收：

```bash
curl http://127.0.0.1:8000/healthz
curl http://127.0.0.1:8000/readyz
curl http://127.0.0.1:8000/v1/models
curl http://127.0.0.1:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"gemini-3-flash","messages":[{"role":"user","content":"你好"}]}'
curl http://127.0.0.1:8000/v1/debug/status -H "x-admin-token: YOUR_ADMIN_TOKEN"
```

### `/v1/models` 契约

`/v1/models` 中每个 model item 的 `owned_by` 固定为：

```json
"owned_by": "gemini-reverse"
```

## 7. 配置 systemd

```bash
sudo cp deploy/systemd/gemini-reverse.service /etc/systemd/system/gemini-reverse.service
```

如目录、用户或配置路径不同，先修改 service 文件中的：

- `User`
- `Group`
- `WorkingDirectory`
- `PROJECT_DIR`
- `VENV_DIR`
- `CONFIG_PATH`

然后启用：

```bash
sudo systemctl daemon-reload
sudo systemctl enable gemini-reverse
sudo systemctl start gemini-reverse
sudo systemctl status gemini-reverse
```

查看日志：

```bash
journalctl -u gemini-reverse -f
```

## 8. 配置 Nginx

```bash
sudo cp deploy/nginx/gemini-reverse.conf /etc/nginx/sites-available/gemini-reverse.conf
sudo ln -s /etc/nginx/sites-available/gemini-reverse.conf /etc/nginx/sites-enabled/gemini-reverse.conf
sudo nginx -t
sudo systemctl reload nginx
```

至少确认：

- `server_name`
- 证书路径
- 反代目标是 `127.0.0.1:8000`
- `X-Forwarded-For` 已透传

## 9. 正式公开 debug 接口

仅以下 5 个 debug 路由作为正式公开能力维护：

- `GET /v1/debug/status`
- `GET /v1/debug/network`
- `GET /v1/debug/doctor`
- `POST /v1/debug/auth/push_ticket`
- `GET /v1/debug/auth/status`

`/v1/debug/last` 与 `/v1/debug/logs` 不作为正式公开接口维护。

## 10. 常见问题

### `403 client ip is not allowlisted`

说明请求来源不在 `allowed_client_ips`，且未提供有效 `api_keys`。

### `/v1/debug/status` 返回 `401`

说明当前请求不满足 debug 鉴权规则：

- 非 loopback，且缺少有效 `admin_token`
- 或 `debug_loopback_bypass_enabled=false`，但未提供 `admin_token`

### `ConnectError` / `ReadTimeout`

优先检查：

1. VPS 上的代理是否真的可用
2. `proxy` 配置是否正确
3. `SECURE_1PSID` / `SECURE_1PSIDTS` 是否有效
4. 是否先从 `gemini-3-flash` 开始验证
