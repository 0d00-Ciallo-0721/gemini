import importlib
import json
import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest


def _reload_module(name: str):
    if name in sys.modules:
        return importlib.reload(sys.modules[name])
    return importlib.import_module(name)


def test_standalone_config_loads_static_accounts(tmp_path, monkeypatch):
    config_path = tmp_path / "runtime_config.json"
    payload = {
        "host": "0.0.0.0",
        "port": 9000,
        "model": "gemini-3-flash",
        "proxy": "http://127.0.0.1:7897",
        "allowlist_enabled": True,
        "allowed_client_ips": ["10.0.0.0/8"],
        "accounts": {
            "1": {
                "label": "account_1",
                "cookie": "x",
                "SECURE_1PSID": "psid",
                "SECURE_1PSIDTS": "psidts"
            }
        }
    }
    config_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setenv("GEMINI_REVERSE_CONFIG", str(config_path))

    config_mod = _reload_module("app.config")
    assert config_mod.get_runtime_config()["port"] == 9000
    assert config_mod.PROXIES == "http://127.0.0.1:7897"
    assert config_mod.state.active_account == "1"
    assert config_mod.get_current_credentials() == ("psid", "psidts")


def test_allowlist_accepts_exact_ip_and_cidr(monkeypatch):
    monkeypatch.setenv("GEMINI_REVERSE_CONFIG", str(Path("data/runtime_config.json").resolve()))
    main_mod = _reload_module("app.main")

    runtime_config = {
        "allowlist_enabled": True,
        "allowed_client_ips": ["127.0.0.1/32", "10.0.0.0/8"],
    }
    assert main_mod._is_ip_allowed("127.0.0.1", runtime_config) is True
    assert main_mod._is_ip_allowed("10.2.3.4", runtime_config) is True
    assert main_mod._is_ip_allowed("192.168.1.2", runtime_config) is False


def test_service_key_accepts_non_allowlisted_request(monkeypatch):
    monkeypatch.setenv("GEMINI_REVERSE_CONFIG", str(Path("data/runtime_config.json").resolve()))
    main_mod = _reload_module("app.main")

    runtime_config = {
        "allowlist_enabled": True,
        "allowed_client_ips": ["10.0.0.0/8"],
        "api_keys": ["test-key-1"],
    }

    class DummyRequest:
        def __init__(self, headers):
            self.headers = headers

    assert main_mod._has_valid_service_key(DummyRequest({"x-api-key": "test-key-1"}), runtime_config) is True
    assert main_mod._has_valid_service_key(DummyRequest({"authorization": "Bearer test-key-1"}), runtime_config) is True
    assert main_mod._has_valid_service_key(DummyRequest({"x-api-key": "wrong"}), runtime_config) is False


def test_start_server_reads_config(tmp_path):
    config_path = tmp_path / "runtime_config.json"
    config_path.write_text(json.dumps({"host": "0.0.0.0", "port": 18000}), encoding="utf-8")
    start_server = _reload_module("scripts.start_server")
    loaded = start_server.load_runtime_config(str(config_path))
    assert loaded["host"] == "0.0.0.0"
    assert loaded["port"] == 18000


def test_session_manager_translates_readonly_sqlite_error(monkeypatch):
    session_mod = _reload_module("app.session_manager")
    manager = session_mod.SessionManager(":memory:")

    class DummyConn:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def execute(self, *_args, **_kwargs):
            raise session_mod.sqlite3.OperationalError("attempt to write a readonly database")

        def commit(self):
            return None

    with patch.object(manager, "_connect", return_value=DummyConn()):
        with patch.object(manager, "get_session", return_value=None):
            with pytest.raises(Exception) as caught:
                manager.create_or_reset_session("s1", object())
    assert caught.value.error_type == "SESSION_DB_PERMISSION_ERROR"
    assert "readonly database" in str(caught.value)


def test_local_error_response_uses_structured_session_db_error(monkeypatch):
    monkeypatch.setenv("GEMINI_REVERSE_CONFIG", str(Path("data/runtime_config.json").resolve()))
    main_mod = _reload_module("app.main")
    exc_mod = _reload_module("app.exceptions")

    response = main_mod._local_error_response(
        exc_mod.SessionDbPermissionError("session database is not writable: attempt to write a readonly database"),
        status_code=503,
    )

    assert response.status_code == 503
    body = json.loads(response.body.decode("utf-8"))
    assert body["error_type"] == "SESSION_DB_PERMISSION_ERROR"
    assert "readonly database" in body["error"]
