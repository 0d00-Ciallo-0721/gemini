import pytest
import os
import json
from unittest.mock import patch, MagicMock, AsyncMock
from reverse_runtime.session_bridge import resolve_runtime_config
from reverse_runtime.auth_manager import AuthManager
from reverse_runtime.auth_status import AuthStatus

@pytest.fixture
def corrupted_env(tmp_path):
    env_dir = tmp_path / "data" / "plugin_data" / "astrbot_plugin_gemini_reverse"
    env_dir.mkdir(parents=True, exist_ok=True)
    return env_dir

def test_missing_sqlite_tables_are_recreated(corrupted_env):
    manager = AuthManager(str(corrupted_env), {"auth_mode": "relay_ticket", "accounts": {}})
    # Try inserting a ticket manually to db
    import sqlite3
    db_path = str(corrupted_env / "auth_repo.sqlite")
    with sqlite3.connect(db_path) as conn:
        conn.execute("DROP TABLE active_ticket")
        conn.commit()
        
    # Re-instantiating AuthManager should trigger _init_db and re-create it gracefully
    manager2 = AuthManager(str(corrupted_env), {"auth_mode": "relay_ticket", "accounts": {}})
    
    # Try saving a new active ticket now
    manager2.store.save_active_ticket({"test": "data"})
    ticket = manager2.store.load_active_ticket()
    assert ticket["test"] == "data"
    assert ticket["schema_version"] == "1.0"

def test_stale_fallback_recovery(corrupted_env):
    manager = AuthManager(str(corrupted_env), {"auth_mode": "relay_ticket", "accounts": {}})
    # Inject a ticket with missing attributes causing structural fallback
    manager.store.save_active_ticket({"status": AuthStatus.INVALIDATED.value})
    
    view = manager.get_auth_view()
    assert view["status"] == AuthStatus.FALLBACK.value
    assert manager.fallback_active is True
