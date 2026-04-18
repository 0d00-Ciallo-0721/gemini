from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Any

import httpx


async def probe_reverse_service(host: str, port: int, timeout_sec: int = 5) -> dict[str, Any]:
    base = f"http://{host}:{port}"
    result = {
        "base_url": base,
        "models_ok": False,
        "debug_status_ok": False,
        "status_code": None,
        "error": "",
    }
    timeout = httpx.Timeout(float(timeout_sec))
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            from app.config import get_runtime_config

            admin_token = str(get_runtime_config().get("admin_token") or "").strip()
            headers = {"x-admin-token": admin_token} if admin_token else {}
            models_resp = await client.get(f"{base}/v1/models")
            result["status_code"] = models_resp.status_code
            result["models_ok"] = models_resp.status_code == 200
            debug_resp = await client.get(f"{base}/v1/debug/status", headers=headers)
            result["debug_status_ok"] = debug_resp.status_code == 200
    except Exception as exc:
        result["error"] = str(exc)
    return result


def check_session_db(session_db_path: str) -> dict[str, Any]:
    path = Path(session_db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    result = {
        "path": str(path),
        "exists": path.exists(),
        "writable": False,
        "error": "",
    }
    try:
        with sqlite3.connect(str(path)) as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS plugin_probe (id INTEGER PRIMARY KEY)")
            conn.commit()
        result["writable"] = os.access(path, os.W_OK)
        result["exists"] = path.exists()
    except Exception as exc:
        result["error"] = str(exc)
    return result


def check_accounts(accounts: dict[str, dict[str, str]]) -> dict[str, Any]:
    valid = []
    invalid = []
    for account_id, account in (accounts or {}).items():
        if account.get("SECURE_1PSID") and account.get("SECURE_1PSIDTS"):
            valid.append(account.get("label") or account_id)
        else:
            invalid.append(account.get("label") or account_id)
    return {
        "total": len(accounts or {}),
        "valid": valid,
        "invalid": invalid,
        "ok": bool(valid),
    }


def check_auth(runtime_config: dict[str, Any], auth_manager: Any = None) -> dict[str, Any]:
    auth_mode = runtime_config.get("auth_mode", "relay_ticket")
    result = {
        "auth_mode": auth_mode,
        "is_healthy": False,
        "details": {}
    }
    
    if auth_manager:
        view = auth_manager.get_auth_view()
        result["details"] = view
        result["is_healthy"] = view.get("status") in ("healthy", "fallback")
        
        # Read recent 10 auth events
        recent_events = []
        try:
            from .auth_status import AuthStatus
            if auth_manager.store.auth_history_path.exists():
                with open(auth_manager.store.auth_history_path, "r", encoding="utf-8") as f:
                    lines = f.readlines()
                    for line in lines[-10:]:
                        if line.strip():
                            import json
                            recent_events.append(json.loads(line))
        except Exception:
            pass
        result["recent_events"] = recent_events

        # Cookie 卫生检查：检测 GOOGLE_ABUSE_EXEMPTION 高危标记
        try:
            ticket = auth_manager.store.load_active_ticket()
            if ticket:
                cookie_data = ticket.get("cookie_data", {})
                ck_str = str(cookie_data.get("cookie", "") or cookie_data.get("raw_cookie", ""))
                ck_dict = cookie_data.get("cookies_dict", {}) or {}
                has_abuse = "GOOGLE_ABUSE_EXEMPTION" in ck_str or "GOOGLE_ABUSE_EXEMPTION" in ck_dict
                result["cookie_hygiene"] = {
                    "clean": not has_abuse,
                    "warning": (
                        "Cookie 包含 GOOGLE_ABUSE_EXEMPTION！当前代理 IP 被 Google 标记为高风险，"
                        "生成请求可能被静默丢弃 (GOOGLE_SILENT_ABORT)。"
                        "请更换代理节点后重新抓取纯净 Cookie。"
                    ) if has_abuse else ""
                }
        except Exception:
            pass
    else:
        active = runtime_config.get("active_ticket")
        result["details"] = {"active_ticket_exists": bool(active), "auth_status": runtime_config.get("auth_status", "unknown")}
        result["is_healthy"] = bool(active) or (auth_mode == "manual_cookie_pool")
        
    return result


async def run_doctor(runtime_config: dict[str, Any], auth_manager: Any = None) -> dict[str, Any]:
    """
    运维诊断探针入口。
    返回的结构树严格按以下层级分组：
    - service: 本地 Reverse 代理进程探活状态（包含监听地址与可用探测）
    - upstream: 上游 gemini.google.com 的网络可达性与证书有效性
    - session_db: 本地物理映射表文件读写权限健康度
    - accounts: 静态兜底账号数据的可用性审计
    - auth: Relay 长期饭票的核心状态扫描机及最近 10 次相关刷新落盘事件
    """
    from .upstream_probe import probe_gemini_upstream
    
    return {
        "service": await probe_reverse_service(
            runtime_config["host"],
            int(runtime_config["port"]),
            int(runtime_config["healthcheck_interval_sec"]),
        ),
        "upstream": await probe_gemini_upstream(),
        "session_db": check_session_db(runtime_config["session_db_path"]),
        "accounts": check_accounts(runtime_config.get("fallback_accounts", runtime_config.get("accounts", {}))),
        "auth": check_auth(runtime_config, auth_manager),
    }
