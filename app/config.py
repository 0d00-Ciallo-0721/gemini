from __future__ import annotations

import json
import os
from copy import deepcopy
from pathlib import Path
from typing import Any

try:
    from scripts.update_cookie import normalize_cookie_accounts
except ImportError:
    from update_cookie import normalize_cookie_accounts


DEFAULT_RUNTIME_CONFIG = {
    "host": "127.0.0.1",
    "port": 8000,
    "model": "gemini-3-flash",
    "provider_id": "gemini_reverse",
    "provider_name": "Gemini Reverse",
    "session_db_path": "",
    "log_dir": "",
    "debug_mode": False,
    "debug_routes_enabled": True,
    "debug_payload_logging": False,
    "accounts": {},
    "proxy": "",
    "healthcheck_interval_sec": 5,
    "stream_first_chunk_timeout_sec": 45,
    "stream_idle_timeout_sec": 45,
    "allowlist_enabled": True,
    "allowed_client_ips": ["127.0.0.1/32"],
    "trusted_proxies": ["127.0.0.1/32", "::1/128"],
    "api_keys": [],
    "admin_token": "change_me",
    "auth_mode": "manual_cookie_pool",
    "relay_shared_secret": "change_me_to_a_random_string",
    "relay_primary_client_id": "my_desktop",
    "relay_ticket_ttl_sec": 172800,
    "relay_refresh_interval_sec": 3600,
    "relay_accept_push_without_restart": True,
}

CONFIG_ENV_VAR = "GEMINI_REVERSE_CONFIG"
DEFAULT_CONFIG_PATH = Path("data") / "runtime_config.json"

AUTH_MANAGER = None


def _default_data_dir(config_path: Path) -> Path:
    if config_path.name == "runtime_config.json" and config_path.parent.name:
        return config_path.parent
    return config_path.parent


def _load_runtime_config() -> dict[str, Any]:
    config = deepcopy(DEFAULT_RUNTIME_CONFIG)
    config_path = Path(os.environ.get(CONFIG_ENV_VAR) or DEFAULT_CONFIG_PATH)
    if config_path.exists():
        loaded = json.loads(config_path.read_text(encoding="utf-8") or "{}")
        config.update(loaded or {})

    data_dir = Path(config.get("data_dir") or _default_data_dir(config_path))
    data_dir.mkdir(parents=True, exist_ok=True)
    logs_dir = data_dir / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)

    accounts_value = config.get("accounts", {})
    normalized_accounts = (
        normalize_cookie_accounts(accounts_value)
        if isinstance(accounts_value, list)
        else accounts_value
    )

    config["data_dir"] = str(data_dir)
    config["runtime_config_path"] = str(config_path)
    config["session_db_path"] = str(config.get("session_db_path") or data_dir / "reverse_sessions.sqlite3")
    config["log_dir"] = str(config.get("log_dir") or logs_dir)
    config["accounts"] = normalized_accounts or {}

    global AUTH_MANAGER
    AUTH_MANAGER = None
    try:
        from runtime.auth_manager import AuthManager

        AUTH_MANAGER = AuthManager(str(data_dir), config)
    except Exception:
        AUTH_MANAGER = None

    return config


class AppState:
    def __init__(self) -> None:
        self.active_account = ""
        self.active_model = DEFAULT_RUNTIME_CONFIG["model"]


state = AppState()
RUNTIME_CONFIG: dict[str, Any] = {}
PROXIES = DEFAULT_RUNTIME_CONFIG["proxy"]
ACCOUNTS: dict[str, dict[str, str]] = {}


def apply_runtime_config(config: dict[str, Any]) -> None:
    global RUNTIME_CONFIG, PROXIES, ACCOUNTS

    RUNTIME_CONFIG = deepcopy(config)
    PROXIES = str(RUNTIME_CONFIG.get("proxy") or DEFAULT_RUNTIME_CONFIG["proxy"])

    accounts_source = RUNTIME_CONFIG.get("accounts", {})
    parsed_accounts = (
        normalize_cookie_accounts(accounts_source)
        if isinstance(accounts_source, list)
        else dict(accounts_source or {})
    )

    ACCOUNTS = {str(k): v for k, v in parsed_accounts.items()}
    state.active_model = str(RUNTIME_CONFIG.get("model") or DEFAULT_RUNTIME_CONFIG["model"])
    state.active_account = str(RUNTIME_CONFIG.get("active_account") or next(iter(ACCOUNTS.keys()), ""))


def get_runtime_config() -> dict[str, Any]:
    return deepcopy(RUNTIME_CONFIG)


def reload_runtime_config() -> None:
    apply_runtime_config(_load_runtime_config())


def get_current_credentials() -> tuple[str, str]:
    if not state.active_account or state.active_account not in ACCOUNTS:
        raise RuntimeError("No Gemini accounts configured")
    acc = ACCOUNTS[state.active_account]
    return acc.get("SECURE_1PSID", ""), acc.get("SECURE_1PSIDTS", "")


def get_current_account_data() -> dict[str, Any]:
    if not state.active_account or state.active_account not in ACCOUNTS:
        raise RuntimeError("No Gemini accounts configured")
    return deepcopy(ACCOUNTS[state.active_account])


apply_runtime_config(_load_runtime_config())
