import time
from typing import Any
from .auth_manager import AuthManager
from .auth_status import AuthStatus

async def refresh_active_ticket(auth_manager: AuthManager, client: Any) -> bool:
    """
    底层活跃票据认证刷新器（非账号切换器）。
    仅仅负责与 Google 交互来延长当前 Relay Ticket 的 TTL 及更新物理 Cookie，
    如果遇到不可恢复的 AuthError，将引发 RelayTicketExpired 交给上层 API Client 执行账号回退。
    
    返回 True 表示刷新操作实际成功闭环，返回 False 表示遇到可短暂容忍的失败或被退避。
    """
    ticket = auth_manager.store.load_active_ticket()
    if not ticket or ticket.get("status") in (AuthStatus.INVALIDATED.value, AuthStatus.EXPIRED.value):
        return False
        
    next_retry = ticket.get("next_retry_after", 0)
    if time.time() < next_retry:
        auth_manager.store.log_event("refresh_skipped", {"reason": "backoff_active", "retry_after": next_retry - time.time()})
        return False
        
    try:
        from gemini_webapi.exceptions import AuthError
        # 依赖上游探针: gemini_webapi client.init() 涵盖了获取初始票据和 RotateCookies 的底层动作
        await client.init(timeout=45)
        
        # =====================================================================
        # 成功分支 (Success)
        # =====================================================================
        
        # Update ticket refresh time
        ticket["last_refresh_time"] = time.time()
        ticket["consecutive_failures"] = 0
        ticket["next_retry_after"] = 0
        auth_manager.store.save_active_ticket(ticket)
        auth_manager.transition_state(AuthStatus.HEALTHY, "Refresh Success")
        
        # ---------------------------------------------------------------------
        # 持久化视图同步：为何要既同步 cookies_dict 又要同步 SECURE_*？
        # 1. cookies_dict: 是给 httpx 作为传输层 session 保持的散装状态。
        # 2. SECURE_1PSID 等: 是给 gemini_webapi 识别身份的主键，冷启动 config.py 时依靠它恢复主体。
        # 漏掉任何一方，都会导致“进程内活得好好的，重启一次就 AuthError”。
        # ---------------------------------------------------------------------
        if hasattr(client, "cookies") and isinstance(client.cookies, dict):
            if "cookie_data" not in ticket or not isinstance(ticket["cookie_data"], dict):
                ticket["cookie_data"] = {}
                
            if "cookies_dict" not in ticket["cookie_data"]:
                ticket["cookie_data"]["cookies_dict"] = {}
                
            for k, v in client.cookies.items():
                if k not in ("SECURE_1PSID", "SECURE_1PSIDTS", "__Secure-1PSID", "__Secure-1PSIDTS"):
                    ticket["cookie_data"]["cookies_dict"][k] = v
            
            if "__Secure-1PSID" in client.cookies:
                ticket["cookie_data"]["SECURE_1PSID"] = client.cookies["__Secure-1PSID"]
            if "__Secure-1PSIDTS" in client.cookies:
                ticket["cookie_data"]["SECURE_1PSIDTS"] = client.cookies["__Secure-1PSIDTS"]
            
        auth_manager.store.save_active_ticket(ticket)
        auth_manager.store.log_event("refresh_success", {"client_id": ticket.get("client_id")})
        return True
    
    except AuthError as e:
        # =====================================================================
        # 真实认证失效分支 (AuthError)
        # =====================================================================
        auth_manager.transition_state(AuthStatus.EXPIRED, "AuthError definitively expired active ticket.")
        from bundled_gemini.exceptions import RelayTicketExpired
        raise RelayTicketExpired("Active ticket definitively expired during refresh.") from e
        
    except Exception as e:
        # =====================================================================
        # 网络或临时故障分支 (Network/Temporary Error)
        # =====================================================================
        failures = ticket.get("consecutive_failures", 0) + 1
        ticket["consecutive_failures"] = failures
        
        # 指数退避 (Exponential Backoff): 15s, 30s, 60s, 120s... 封顶 3600s
        backoff_sec = min(15 * (2 ** (failures - 1)), 3600)
        ticket["next_retry_after"] = time.time() + backoff_sec
            
        auth_manager.store.save_active_ticket(ticket)
        auth_manager.store.log_event("refresh_failed", {
            "error": str(e), 
            "consecutive_failures": failures,
            "next_retry_after": backoff_sec
        })
        
        if failures >= 5 and ticket.get("status") != AuthStatus.STALE.value:
            auth_manager.transition_state(AuthStatus.STALE, "Consecutive network/unknown errors exceeded threshold")
            
        return False
