import os
import json
import pytest
from unittest.mock import patch
from bundled_gemini.config import _load_runtime_config, reload_runtime_config, get_current_account_data, state, ACCOUNTS

def test_load_runtime_config_with_auth_runtime(tmp_path):
    # Setup mock runtime config dir
    config_file = tmp_path / "runtime_config.json"
    auth_file = tmp_path / "auth_runtime.json"
    
    config_file.write_text(json.dumps({
        "auth_mode": "relay_ticket",
        "accounts": {}
    }))
    
    auth_file.write_text(json.dumps({
        "status": "healthy",
        "client_id": "test",
        "cookie_data": {
            "SECURE_1PSID": "123",
            "SECURE_1PSIDTS": "456",
            "cookies_dict": {"foo": "bar"}
        }
    }))
    
    with patch.dict(os.environ, {"ASTRBOT_GEMINI_REVERSE_CONFIG": str(config_file)}):
        config = _load_runtime_config()
        
        # Ensure active_ticket is successfully picked up from auth_runtime.json
        assert "active_ticket" in config
        assert config["active_ticket"]["client_id"] == "test"
        
        # Then, if we apply it, it should load relay_active
        import bundled_gemini.config
        bundled_gemini.config.apply_runtime_config(config)
        
        assert "relay_active" in bundled_gemini.config.ACCOUNTS
        assert bundled_gemini.config.state.active_account == "relay_active"
