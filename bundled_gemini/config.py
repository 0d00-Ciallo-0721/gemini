import json
import os
from copy import deepcopy

try:
    from ..update_cookie import normalize_cookie_accounts
except ImportError:  # pragma: no cover - local tests may import bundled_gemini as top-level package
    from update_cookie import normalize_cookie_accounts


DEFAULT_RUNTIME_CONFIG = {
    "host": "127.0.0.1",
    "port": 8000,
    "model": "gemini-3.1-pro",
    "provider_id": "gemini_reverse",
    "provider_name": "Gemini Reverse",
    "session_db_path": "",
    "log_dir": "logs",
    "debug_mode": False,
    "accounts": {},
    "proxy": "",
}
def _load_runtime_config():
    config = deepcopy(DEFAULT_RUNTIME_CONFIG)
    config_path = os.environ.get("ASTRBOT_GEMINI_REVERSE_CONFIG", "")
    if config_path and os.path.exists(config_path):
        with open(config_path, encoding="utf-8") as fp:
            loaded = json.load(fp)
        config.update(loaded or {})
    config["accounts"] = normalize_cookie_accounts(config.get("accounts", {})) if isinstance(config.get("accounts", {}), list) else config.get("accounts", {})
    return config


class AppState:
    def __init__(self):
        self.active_account = ""
        self.active_model = DEFAULT_RUNTIME_CONFIG["model"]


state = AppState()
RUNTIME_CONFIG = {}
PROXIES = DEFAULT_RUNTIME_CONFIG["proxy"]
ACCOUNTS = {}


def apply_runtime_config(config):
    global RUNTIME_CONFIG, PROXIES, ACCOUNTS
    RUNTIME_CONFIG = deepcopy(config)
    PROXIES = str(RUNTIME_CONFIG.get("proxy") or DEFAULT_RUNTIME_CONFIG["proxy"])
    accounts = RUNTIME_CONFIG.get("accounts", {})
    ACCOUNTS = normalize_cookie_accounts(accounts) if isinstance(accounts, list) else accounts
    state.active_model = str(RUNTIME_CONFIG.get("model") or DEFAULT_RUNTIME_CONFIG["model"])
    state.active_account = next(iter(ACCOUNTS.keys()), "")


def get_runtime_config():
    return deepcopy(RUNTIME_CONFIG)


def get_current_credentials():
    if not state.active_account or state.active_account not in ACCOUNTS:
        raise RuntimeError("No Gemini accounts configured")
    acc = ACCOUNTS[state.active_account]
    return acc["SECURE_1PSID"], acc["SECURE_1PSIDTS"]


apply_runtime_config(_load_runtime_config())
