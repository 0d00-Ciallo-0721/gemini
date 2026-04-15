import sys
import json
import time
import uuid
import hmac
import hashlib
try:
    import httpx
except ImportError:
    print("Please install httpx: pip install httpx")
    sys.exit(1)

from update_cookie import parse_cookie_string, standardize_cookie_payload

def push_to_plugin(cookie_string, host="127.0.0.1", port=8000, secret="change_me_to_a_random_string", client_id="my_desktop"):
    payload = standardize_cookie_payload(cookie_string)
    
    if not payload or "SECURE_1PSID" not in payload:
        print("❌ Invalid Cookie! Missing SECURE_1PSID.")
        return
        
    timestamp = int(time.time())
    nonce = str(uuid.uuid4())
    payload_hash = hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()
    
    msg = f"{timestamp}:{nonce}:{payload_hash}"
    signature = hmac.new(secret.encode("utf-8"), msg.encode("utf-8"), hashlib.sha256).hexdigest()
    
    url = f"http://{host}:{port}/v1/debug/auth/push_ticket"
    
    req_body = {
        "cookie_data": payload,
        "timestamp": timestamp,
        "nonce": nonce,
        "payload_hash": payload_hash,
        "signature": signature,
        "client_id": client_id,
        "source": "cli_relay_helper",
        "ttl": 3600 * 24 * 7 
    }
    
    print(f"Pushing to {url} ...")
    try:
        r = httpx.post(url, json=req_body, timeout=10)
        print(f"Status Code: {r.status_code}")
        print(f"Response: {r.text}")
    except Exception as e:
        print(f"❌ Failed to push cookie: {e}")

if __name__ == "__main__":
    print("Gemini Relay Ticket Push Helper")
    print("================================")
    cookie_str = sys.argv[1] if len(sys.argv) > 1 else input("Enter full Gemini Cookie String (must contain __Secure-1PSID): ")
    
    if not cookie_str.strip():
        print("No cookie provided.")
        sys.exit(1)
        
    port_input = input("Enter Plugin HTTP Port [8000]: ")
    port = int(port_input) if port_input.strip() else 8000
    
    secret_input = input("Enter Relay Shared Secret [change_me_to_a_random_string]: ")
    secret = secret_input if secret_input.strip() else "change_me_to_a_random_string"
    
    client_id_input = input("Enter Client ID [my_desktop]: ")
    client_id = client_id_input if client_id_input.strip() else "my_desktop"
    
    push_to_plugin(cookie_str, port=port, secret=secret, client_id=client_id)
