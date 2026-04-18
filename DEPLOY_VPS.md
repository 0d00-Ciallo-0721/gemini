# Gemini Reverse VPS 部署指南

本文档面向 Linux 美国 VPS，目标是把当前项目作为一个独立的 OpenAI 兼容服务运行，并允许外部服务器通过你的美国 VPS 访问。

## 1. 推荐目录

建议部署到：

```bash
/opt/gemini-reverse
```

建议创建专用用户：

```bash
sudo useradd -r -s /bin/bash -m gemini
sudo mkdir -p /opt/gemini-reverse
sudo chown -R gemini:gemini /opt/gemini-reverse
```

## 2. 上传项目

把当前项目整体上传到 VPS：

```bash
rsync -avz ./ user@your-vps:/opt/gemini-reverse/
```

或先上传压缩包再解压。

## 3. 安装运行环境

以下命令以 Ubuntu/Debian 为例：

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip nginx
cd /opt/gemini-reverse
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
```

## 4. 准备运行配置

复制示例配置：

```bash
cp config.example.json data/runtime_config.json
```

你至少要改这些字段：

- `host`
- `port`
- `proxy`
- `admin_token`
- `api_keys`
- `allowed_client_ips`
- `accounts`

推荐起步配置：

```json
{
  "host": "127.0.0.1",
  "port": 8000,
  "model": "gemini-3-flash",
  "proxy": "http://127.0.0.1:7897",
  "allowlist_enabled": true,
  "allowed_client_ips": [
    "127.0.0.1/32",
    "你的业务服务器IP/32"
  ],
  "api_keys": [
    "给本地电脑或其他非白名单客户端使用的调用 key"
  ],
  "admin_token": "换成强随机字符串",
  "active_account": "1",
  "accounts": {
    "1": {
      "label": "account_1",
      "cookie": "完整Cookie",
      "SECURE_1PSID": "xxx",
      "SECURE_1PSIDTS": "xxx"
    }
  }
}
```

## 5. 更新 Cookie

如果你只想在 VPS 上更新静态账号池：

```bash
cd /opt/gemini-reverse
source .venv/bin/activate
python scripts/update_cookie.py
```

写回目标是：

```bash
data/runtime_config.json
```

## 6. 直接启动测试

先不要急着上 systemd，先手动启动一次：

```bash
cd /opt/gemini-reverse
source .venv/bin/activate
python scripts/start_server.py --config ./data/runtime_config.json
```

本机验收：

```bash
curl http://127.0.0.1:8000/healthz
curl http://127.0.0.1:8000/v1/models
```

非流式：

```bash
curl http://127.0.0.1:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"gemini-3-flash","messages":[{"role":"user","content":"你好"}]}'
```

流式：

```bash
curl http://127.0.0.1:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"gemini-3-flash","stream":true,"messages":[{"role":"user","content":"你好"}]}'
```

运维探针：

```bash
curl http://127.0.0.1:8000/v1/debug/status -H "x-admin-token: 你的admin_token"
curl http://127.0.0.1:8000/v1/debug/doctor -H "x-admin-token: 你的admin_token"
```

## 7. 配置 systemd

把模板文件复制到 systemd：

```bash
sudo cp deploy/systemd/gemini-reverse.service /etc/systemd/system/gemini-reverse.service
```

如果你的目录或用户不是默认值，先改这个文件里的：

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

日志查看：

```bash
journalctl -u gemini-reverse -f
```

## 8. 配置 Nginx

复制模板：

```bash
sudo cp deploy/nginx/gemini-reverse.conf /etc/nginx/sites-available/gemini-reverse.conf
```

改掉：

- `server_name`
- 证书路径

启用：

```bash
sudo ln -s /etc/nginx/sites-available/gemini-reverse.conf /etc/nginx/sites-enabled/gemini-reverse.conf
sudo nginx -t
sudo systemctl reload nginx
```

## 9. 外部服务器如何访问

你的外部服务器不要直接访问 Gemini，只访问你的美国 VPS：

```bash
https://your-domain.example/v1/chat/completions
```

如果启用了白名单，记得把外部服务器出口 IP 加到：

```json
"allowed_client_ips"
```

如果来源 IP 不在白名单里，也可以通过 API key 访问。主业务接口支持：

```http
Authorization: Bearer YOUR_API_KEY
```

或：

```http
x-api-key: YOUR_API_KEY
```

推荐策略：

- 业务服务器出口 IP：加入 `allowed_client_ips`，免 key
- 你本地电脑或其它临时客户端：不加白名单，改用 `api_keys`
- 调试接口：继续使用 `x-admin-token`

## 10. 推荐上线顺序

1. VPS 本机跑通 `/healthz`
2. VPS 本机跑通 `/v1/models`
3. VPS 本机跑通 sync chat
4. VPS 本机跑通 stream chat
5. Nginx 反代
6. 业务服务器加白名单后远程访问

## 11. 常见问题

### `403 client ip is not allowlisted`

说明来源 IP 不在：

```json
"allowed_client_ips"
```

### `/v1/debug/status` 返回 `401`

说明没带：

```http
x-admin-token: your_token
```

### `ConnectError` 或 `ReadTimeout`

优先检查：

1. VPS 上代理是否真可用
2. `proxy` 配置是否正确
3. 当前账号的 `SECURE_1PSID` / `SECURE_1PSIDTS` 是否有效
4. 模型是否设置为 `gemini-3-flash`
