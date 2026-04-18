from enum import Enum

class AuthStatus(str, Enum):
    HEALTHY = "healthy"
    STALE = "stale"
    EXPIRED = "expired"
    FALLBACK = "fallback"
    INVALIDATED = "invalidated"
    RECOVERING = "recovering"
    
    def __str__(self):
        return self.value


def get_auth_status_payload(auth_manager):
    if not auth_manager:
        return {
            "auth_mode": "manual_cookie_pool",
            "status": "disabled",
            "active_ticket": None,
            "fallback_accounts": {},
        }
    if hasattr(auth_manager, "get_auth_view"):
        return auth_manager.get_auth_view()
    return {
        "auth_mode": getattr(auth_manager, "runtime_config", {}).get("auth_mode", "manual_cookie_pool"),
        "status": "unknown",
    }
