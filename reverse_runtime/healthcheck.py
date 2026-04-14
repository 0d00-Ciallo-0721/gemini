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
            models_resp = await client.get(f"{base}/v1/models")
            result["status_code"] = models_resp.status_code
            result["models_ok"] = models_resp.status_code == 200
            debug_resp = await client.get(f"{base}/v1/debug/status")
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


async def run_doctor(runtime_config: dict[str, Any]) -> dict[str, Any]:
    return {
        "service": await probe_reverse_service(
            runtime_config["host"],
            int(runtime_config["port"]),
            int(runtime_config["healthcheck_interval_sec"]),
        ),
        "session_db": check_session_db(runtime_config["session_db_path"]),
        "accounts": check_accounts(runtime_config["accounts"]),
    }
