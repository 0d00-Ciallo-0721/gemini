import time
import hmac
import hashlib
import json
from typing import Any, Dict, Tuple
try:
    from scripts.update_cookie import standardize_cookie_payload
except ImportError:
    try:
        from ..update_cookie import standardize_cookie_payload
    except ImportError:
        from update_cookie import standardize_cookie_payload
from .auth_manager import AuthManager
from .auth_status import AuthStatus

def verify_signature(auth_manager: AuthManager, payload: Dict[str, Any], secret: str, signature: str) -> Tuple[bool, str]:
    """
    Relay Push 安全验签协议：防重放与防篡改
    
    验证模型下：
    - timestamp: 必须在前后 5 分钟窗口内，防大跨度重放
    - nonce: 落盘防重放，生命周期 5 分钟
    - payload_hash: cookie_data 排序转 JSON 后的 SHA256，防数据中途篡改
    - signature: hmac-sha256(secret, timestamp:nonce:payload_hash)
    """
    if not secret:
        return False, "Relay shared secret is not configured on the server"
        
    nonce = payload.get("nonce")
    if not nonce:
        return False, "Missing nonce"
        
    timestamp = payload.get("timestamp")
    if not timestamp or not isinstance(timestamp, (int, float)):
        return False, "Invalid or missing timestamp"
        
    # Check time window (5 minutes drift allowed)
    if abs(time.time() - timestamp) > 300:
        return False, "Timestamp expired or out of sync"
        
    if auth_manager and auth_manager.store.is_nonce_used(nonce):
        return False, "Replay attack detected (nonce reused)"
        
    payload_hash = payload.get("payload_hash", "")
    expected_hash = hashlib.sha256(json.dumps(payload.get("cookie_data"), sort_keys=True).encode("utf-8")).hexdigest()
    if payload_hash != expected_hash:
        return False, "Payload hash mismatch (data tampered)"
        
    msg = f"{timestamp}:{nonce}:{payload_hash}"
    expected_sig = hmac.new(secret.encode("utf-8"), msg.encode("utf-8"), hashlib.sha256).hexdigest()
    
    if not hmac.compare_digest(signature, expected_sig):
        return False, "HMAC signature mismatch"
        
    # Valid, add nonce to DB
    if auth_manager:
        auth_manager.store.mark_nonce_used(nonce, expire_seconds=300)
        
    return True, ""

def handle_push_ticket(auth_manager: AuthManager, payload: Dict[str, Any], secret: str) -> Tuple[bool, str]:
    sig_valid, sig_msg = verify_signature(auth_manager, payload, secret, payload.get("signature", ""))
    if not sig_valid:
        auth_manager.store.log_event("push_rejected", {"reason": "invalid_signature", "detail": sig_msg})
        return False, sig_msg
        
    cookie_raw = payload.get("cookie_data") or payload.get("cookie_payload")
    cookie_data = standardize_cookie_payload(cookie_raw)
    if not cookie_data:
        auth_manager.store.log_event("push_rejected", {"reason": "invalid_cookie_format"})
        return False, "Invalid cookie format"
        
    client_id = payload.get("client_id", "unknown")
    
    primary_id = ""
    if isinstance(auth_manager.runtime_config, dict):
        primary_id = auth_manager.runtime_config.get("relay_primary_client_id", "")
    
    # 严格门控：仅接收指定受信任主设备的票据上传，防止未授权多源并发污染
    if primary_id and client_id != primary_id:
        auth_manager.store.log_event("push_rejected", {"reason": "client_id_mismatch"})
        return False, f"Client ID mismatch. Expected: {primary_id}"
    
    ticket = {
        "client_id": client_id,
        "push_time": time.time(),
        "last_refresh_time": time.time(),
        "status": AuthStatus.HEALTHY.value,
        "consecutive_failures": 0,
        "next_retry_after": 0,
        "cookie_data": cookie_data
    }
    
    auth_manager.store.save_active_ticket(ticket)
    auth_manager.store.log_event("push_accepted", {"client_id": client_id})
    auth_manager.set_fallback_state(False)
    
    return True, "Ticket accepted"
