import time
import json
import uuid
import hmac
import hashlib
from reverse_runtime.auth_manager import AuthManager
from reverse_runtime.ticket_receiver import handle_push_ticket

def generate_valid_payload(cookie_data, secret, client_id="test_client"):
    timestamp = int(time.time())
    nonce = str(uuid.uuid4())
    payload_hash = hashlib.sha256(json.dumps(cookie_data, sort_keys=True).encode("utf-8")).hexdigest()
    msg = f"{timestamp}:{nonce}:{payload_hash}"
    signature = hmac.new(secret.encode("utf-8"), msg.encode("utf-8"), hashlib.sha256).hexdigest()
    
    return {
        "cookie_data": cookie_data,
        "signature": signature,
        "timestamp": timestamp,
        "nonce": nonce,
        "payload_hash": payload_hash,
        "client_id": client_id
    }

def test_handle_push_ticket_success(tmp_path):
    manager = AuthManager(str(tmp_path), {"auth_mode": "relay_ticket", "accounts": {}})
    cookie_data = {"cookie": "SECURE_1PSID=new_psid; SECURE_1PSIDTS=new_ts;"}
    payload = generate_valid_payload(cookie_data, "secret123")
    
    success, msg = handle_push_ticket(manager, payload, "secret123")
    assert success is True

def test_handle_push_ticket_invalid_signature(tmp_path):
    manager = AuthManager(str(tmp_path), {"auth_mode": "relay_ticket", "accounts": {}})
    cookie_data = {"cookie": "SECURE_1PSID=new_psid; SECURE_1PSIDTS=new_ts;"}
    payload = generate_valid_payload(cookie_data, "wrong_secret")
    
    success, msg = handle_push_ticket(manager, payload, "secret123")
    assert success is False
    assert "signature" in msg.lower()
    assert manager.store.load_active_ticket() is None

def test_handle_push_ticket_empty_secret(tmp_path):
    manager = AuthManager(str(tmp_path), {"auth_mode": "relay_ticket", "accounts": {}})
    cookie_data = {"cookie": "SECURE_1PSID=new_psid; SECURE_1PSIDTS=new_ts;"}
    payload = generate_valid_payload(cookie_data, "")
    
    # Empty secret configured on server
    success, msg = handle_push_ticket(manager, payload, "")
    assert success is False
    assert "not configured" in msg.lower()
