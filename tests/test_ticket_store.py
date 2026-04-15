import os
from reverse_runtime.ticket_store import TicketStore

def test_ticket_store_basic(tmp_path):
    store = TicketStore(str(tmp_path))
    ticket = {"client_id": "test", "cookie_data": {"SECURE_1PSID": "xyz"}}
    
    # Test saving and loading
    store.save_active_ticket(ticket)
    loaded = store.load_active_ticket()
    assert loaded is not None
    assert loaded["client_id"] == "test"
    assert "SECURE_1PSID" in loaded["cookie_data"]
    
    # Test invalidation
    store.invalidate_ticket()
    loaded2 = store.load_active_ticket()
    assert loaded2 is not None
    assert loaded2["status"] == "invalidated"

def test_ticket_store_history(tmp_path):
    store = TicketStore(str(tmp_path))
    store.log_event("init", {"foo": "bar", "secret": "hide_this"})
    log_file = tmp_path / "logs" / "auth_history.jsonl"
    
    assert log_file.exists()
    content = log_file.read_text()
    assert "init" in content
    assert "foo" in content
