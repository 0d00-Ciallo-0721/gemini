import time
from typing import Any, Dict
from .ticket_store import TicketStore
from .auth_status import AuthStatus

class AuthManager:
    def __init__(self, data_dir: str, runtime_config: Dict[str, Any]):
        self.store = TicketStore(data_dir)
        self.runtime_config = runtime_config
        self.auth_mode = runtime_config.get("auth_mode", "relay_ticket")
        self.manual_accounts = runtime_config.get("accounts", {})
        self.relay_ticket_ttl = int(runtime_config.get("relay_ticket_ttl_sec", 172800))
        self.fallback_active = False

    # =====================================================================
    # 阶段 1：核心状态机流转 (State Transition)
    # =====================================================================
    def transition_state(self, new_state: AuthStatus, reason: str = ""):
        """
        统一的端到端状态机迁移入口。
        控制规则：
        - HEALTHY -> STALE / EXPIRED / INVALIDATED
        - RECOVERING -> HEALTHY (刷新成功) 或 STALE (刷新彻底失败)
        
        触发链路：
        进入 EXPIRED / INVALIDATED / STALE 状态将自动拉起 set_fallback_state(True)。
        进入 HEALTHY / RECOVERING 状态将自动闭合隔离，切回主账号 (set_fallback_state(False))。
        """
        ticket = self.store.load_active_ticket()
        if not ticket:
            return
            
        old_state = ticket.get("status", "unknown")
        if old_state == new_state.value:
            return
            
        ticket["status"] = new_state.value
        self.store.save_active_ticket(ticket)
        self.store.log_event("state_transition", {
            "from": old_state,
            "to": new_state.value,
            "reason": reason
        })
        
        # Trigger fallback automatically if entering critical unrecoverable states
        if new_state in (AuthStatus.EXPIRED, AuthStatus.INVALIDATED, AuthStatus.STALE):
            self.set_fallback_state(True)
        elif new_state in (AuthStatus.HEALTHY, AuthStatus.RECOVERING):
            self.set_fallback_state(False)

    # =====================================================================
    # 阶段 2：数据面兜底管控 (Fallback Control)
    # =====================================================================
    def set_fallback_state(self, is_fallback: bool):
        self.fallback_active = is_fallback
        if is_fallback:
            self.store.log_event("fallback_triggered", {})

    # =====================================================================
    # 阶段 3：内部数据校验 (State Evaluation)
    # =====================================================================
    def _check_and_update_ttl(self, ticket: Dict[str, Any]) -> bool:
        """检查长期饭票是否已超期 (TTL)。如果超期，自动压入过期状态。"""
        last_refresh = ticket.get("last_refresh_time", ticket.get("push_time", 0))
        if time.time() - last_refresh > self.relay_ticket_ttl:
            self.transition_state(AuthStatus.EXPIRED, "TTL Expired: Longstanding inactivity")
            return True
        return False

    # =====================================================================
    # 阶段 4：外部展示视图输出 (View Generation)
    # =====================================================================
    def get_auth_view(self) -> Dict[str, Any]:
        """
        供服务配置桥接及 Doctor 面板查看的认证态只读视图。
        它是一个快照，仅进行被动的 TTL 检查，不是核心引擎主动变更点。
        """
        view = {
            "auth_mode": self.auth_mode,
            "active_ticket": None,
            "fallback_accounts": self.manual_accounts,
            "status": AuthStatus.HEALTHY.value
        }
        
        if self.auth_mode == "manual_cookie_pool":
            return view
            
        ticket = self.store.load_active_ticket()
        if not ticket:
            self.fallback_active = True
            view["status"] = AuthStatus.FALLBACK.value
            return view
            
        current_status = ticket.get("status")
        if current_status in (AuthStatus.INVALIDATED.value, AuthStatus.EXPIRED.value, AuthStatus.STALE.value):
            self.fallback_active = True
            view["status"] = AuthStatus.FALLBACK.value
            return view
            
        if self._check_and_update_ttl(ticket):
            view["status"] = AuthStatus.EXPIRED.value
            return view
            
        self.fallback_active = False
        view["status"] = current_status or AuthStatus.HEALTHY.value
        view["active_ticket"] = ticket
        return view
