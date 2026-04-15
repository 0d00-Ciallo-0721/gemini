import json
import os
from copy import deepcopy

try:
    from ..update_cookie import normalize_cookie_accounts
except ImportError:  # pragma: no cover - local tests may import bundled_gemini as top-level package
    from update_cookie import normalize_cookie_accounts


# =====================================================================
# 内部服务状态装载器 (Service Config Loader)
# 此文件专供 Uvicorn 独立后端服务进程使用，
# 负责从磁盘装载 runtime_config 并初始化 AuthManager。
# =====================================================================

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
    "stream_first_chunk_timeout_sec": 45,
    "stream_idle_timeout_sec": 45,
}
# 全局挂载的 AuthManager 单例实例
AUTH_MANAGER = None

def _load_runtime_config():
    """从本地通信桥读取物理运行时大盘配置及持久化的认证态"""
    # 1. 基础载入
    config = deepcopy(DEFAULT_RUNTIME_CONFIG)
    config_path = os.environ.get("ASTRBOT_GEMINI_REVERSE_CONFIG", "")
    if config_path and os.path.exists(config_path):
        with open(config_path, encoding="utf-8") as fp:
            loaded = json.load(fp)
        config.update(loaded or {})
        
    # 2. 从原始兜底账号字典中规范化池数据
    config["accounts"] = normalize_cookie_accounts(config.get("accounts", {})) if isinstance(config.get("accounts", {}), list) else config.get("accounts", {})
    
    # 3. 拦截激活票据，作为 relay_ticket 强模式的热覆盖
    global AUTH_MANAGER
    try:
        from reverse_runtime.auth_manager import AuthManager
        if config_path:
            AUTH_MANAGER = AuthManager(config.get("plugin_data_dir") or os.path.dirname(config_path), config)
            if getattr(AUTH_MANAGER, "auth_mode", "relay_ticket") == "relay_ticket":
                ticket = AUTH_MANAGER.store.load_active_ticket()
                if ticket and ticket.get("status") == "healthy":
                    config["active_ticket"] = ticket
    except Exception:
        pass
        
    return config


class AppState:
    def __init__(self):
        self.active_account = ""
        self.active_model = DEFAULT_RUNTIME_CONFIG["model"]

# == 服务进程全局运行时环境变量 ==
state = AppState()
RUNTIME_CONFIG = {}
PROXIES = DEFAULT_RUNTIME_CONFIG["proxy"]
ACCOUNTS = {}

def apply_runtime_config(config):
    """
    接收结构化大盘配置并注入到服务全局环境变量。
    核心规则：强制把长效票据组装名为 relay_active 并占据第一顺位，把静态兜底作为备胎合并进入 ACCOUNTS。
    """
    global RUNTIME_CONFIG, PROXIES, ACCOUNTS
    RUNTIME_CONFIG = deepcopy(config)
    PROXIES = str(RUNTIME_CONFIG.get("proxy") or DEFAULT_RUNTIME_CONFIG["proxy"])
    
    active_ticket = RUNTIME_CONFIG.get("active_ticket")
    accounts_source = RUNTIME_CONFIG.get("fallback_accounts", RUNTIME_CONFIG.get("accounts", {}))
    
    parsed_accounts = normalize_cookie_accounts(accounts_source) if isinstance(accounts_source, list) else accounts_source
    ACCOUNTS = {}
    
    if active_ticket and active_ticket.get("cookie_data"):
        ACCOUNTS["relay_active"] = active_ticket["cookie_data"]
        
    for k, v in parsed_accounts.items():
        if str(k) != "relay_active":
            ACCOUNTS[str(k)] = v
            
    state.active_model = str(RUNTIME_CONFIG.get("model") or DEFAULT_RUNTIME_CONFIG["model"])
    state.active_account = "relay_active" if "relay_active" in ACCOUNTS else next(iter(ACCOUNTS.keys()), "")

def get_runtime_config():
    return deepcopy(RUNTIME_CONFIG)

def reload_runtime_config():
    apply_runtime_config(_load_runtime_config())


def get_current_credentials():
    if not state.active_account or state.active_account not in ACCOUNTS:
        raise RuntimeError("No Gemini accounts configured")
    acc = ACCOUNTS[state.active_account]
    return acc.get("SECURE_1PSID", ""), acc.get("SECURE_1PSIDTS", "")


def get_current_account_data():
    if not state.active_account or state.active_account not in ACCOUNTS:
        raise RuntimeError("No Gemini accounts configured")
    return deepcopy(ACCOUNTS[state.active_account])


apply_runtime_config(_load_runtime_config())
