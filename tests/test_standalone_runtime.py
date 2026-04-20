import importlib
import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from gemini_webapi.exceptions import UsageLimitExceeded


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
        "trusted_proxies": ["127.0.0.1/32"],
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
    security_mod = _reload_module("app.security")

    runtime_config = {
        "allowlist_enabled": True,
        "allowed_client_ips": ["127.0.0.1/32", "10.0.0.0/8"],
    }
    assert security_mod.is_ip_allowed("127.0.0.1", runtime_config) is True
    assert security_mod.is_ip_allowed("10.2.3.4", runtime_config) is True
    assert security_mod.is_ip_allowed("192.168.1.2", runtime_config) is False


def test_service_key_accepts_non_allowlisted_request(monkeypatch):
    monkeypatch.setenv("GEMINI_REVERSE_CONFIG", str(Path("data/runtime_config.json").resolve()))
    security_mod = _reload_module("app.security")

    runtime_config = {
        "allowlist_enabled": True,
        "allowed_client_ips": ["10.0.0.0/8"],
        "api_keys": ["test-key-1"],
    }

    class DummyRequest:
        def __init__(self, headers):
            self.headers = headers

    assert security_mod.has_valid_service_key(DummyRequest({"x-api-key": "test-key-1"}), runtime_config) is True
    assert security_mod.has_valid_service_key(DummyRequest({"authorization": "Bearer test-key-1"}), runtime_config) is True
    assert security_mod.has_valid_service_key(DummyRequest({"x-api-key": "wrong"}), runtime_config) is False


def test_real_client_ip_only_trusts_xff_from_trusted_proxy(monkeypatch):
    monkeypatch.setenv("GEMINI_REVERSE_CONFIG", str(Path("data/runtime_config.json").resolve()))
    security_mod = _reload_module("app.security")

    trusted_request = SimpleNamespace(
        headers={"x-forwarded-for": "1.2.3.4, 10.0.0.1"},
        client=SimpleNamespace(host="127.0.0.1"),
    )
    untrusted_request = SimpleNamespace(
        headers={"x-forwarded-for": "1.2.3.4, 10.0.0.1"},
        client=SimpleNamespace(host="8.8.8.8"),
    )

    assert security_mod.get_real_client_ip(trusted_request, ["127.0.0.1/32"]) == "1.2.3.4"
    assert security_mod.get_real_client_ip(untrusted_request, ["127.0.0.1/32"]) == "8.8.8.8"


def test_admin_token_supports_bearer_and_header(monkeypatch):
    monkeypatch.setenv("GEMINI_REVERSE_CONFIG", str(Path("data/runtime_config.json").resolve()))
    security_mod = _reload_module("app.security")
    runtime_config = {"admin_token": "admin-secret"}

    assert security_mod.require_admin_token(SimpleNamespace(headers={"x-admin-token": "admin-secret"}), runtime_config) is True
    assert security_mod.require_admin_token(SimpleNamespace(headers={"authorization": "Bearer admin-secret"}), runtime_config) is True
    assert security_mod.require_admin_token(SimpleNamespace(headers={"x-admin-token": "wrong"}), runtime_config) is False


def test_start_server_reads_config(tmp_path):
    config_path = tmp_path / "runtime_config.json"
    config_path.write_text(json.dumps({"host": "0.0.0.0", "port": 18000}), encoding="utf-8")
    start_server = _reload_module("scripts.start_server")
    loaded = start_server.load_runtime_config(str(config_path))
    assert loaded["host"] == "0.0.0.0"
    assert loaded["port"] == 18000


def test_healthz_returns_ok(monkeypatch):
    monkeypatch.setenv("GEMINI_REVERSE_CONFIG", str(Path("data/runtime_config.json").resolve()))
    main_mod = _reload_module("app.main")
    response = asyncio.run(main_mod.healthz())
    body = json.loads(response.body.decode("utf-8"))
    assert response.status_code == 200
    assert body == {"status": "ok"}


def test_models_response_contract_uses_gemini_reverse_owned_by(monkeypatch):
    monkeypatch.setenv("GEMINI_REVERSE_CONFIG", str(Path("data/runtime_config.json").resolve()))
    models_mod = _reload_module("app.routers.models")

    with patch.object(
        models_mod,
        "get_runtime_services",
        return_value=SimpleNamespace(state=SimpleNamespace(active_model="gemini-3-flash")),
    ):
        response = asyncio.run(models_mod.list_models_response())

    body = json.loads(response.body.decode("utf-8"))
    assert body["object"] == "list"
    assert body["data"]
    for item in body["data"]:
        assert item["object"] == "model"
        assert item["owned_by"] == "gemini-reverse"
        assert "created" in item


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


def test_session_manager_configures_busy_timeout(tmp_path):
    session_mod = _reload_module("app.session_manager")
    manager = session_mod.SessionManager(str(tmp_path / "reverse_sessions.sqlite3"))
    with manager._connect() as conn:
        busy_timeout = conn.execute("PRAGMA busy_timeout;").fetchone()[0]
    assert busy_timeout == 5000


def test_session_manager_records_restore_reason_and_status(tmp_path):
    session_mod = _reload_module("app.session_manager")
    manager = session_mod.SessionManager(str(tmp_path / "reverse_sessions.sqlite3"))
    manager.create_or_reset_session(
        "s1",
        None,
        model="gemini-3-flash",
        status="active",
        last_restore_reason="seed",
        last_restore_at=123.0,
    )
    record = manager.get_session("s1")
    record["chat_metadata"] = ["history"]
    manager._upsert_record(record)
    manager._active_sessions.pop("s1", None)

    class DummyChat:
        def __init__(self, metadata):
            self.metadata = metadata

    class DummyClient:
        def start_chat(self, metadata=None, model=""):
            return DummyChat(metadata if metadata is not None else [])

    chat, restored = manager.get_or_restore_chat_session("s1", DummyClient(), model="gemini-3-flash")
    loaded = manager.get_session("s1")
    assert restored is True
    assert chat.metadata == ["history"]
    assert loaded["status"] == "restored"
    assert loaded["last_restore_reason"] == "metadata_restored"
    assert loaded["last_restore_at"] is not None


def test_session_manager_records_fallback_recreate_reason(tmp_path):
    session_mod = _reload_module("app.session_manager")
    manager = session_mod.SessionManager(str(tmp_path / "reverse_sessions.sqlite3"))
    manager.create_or_reset_session("s2", None, model="gemini-3-flash", status="active")
    record = manager.get_session("s2")
    record["chat_metadata"] = ["bad"]
    manager._upsert_record(record)
    manager._active_sessions.pop("s2", None)

    class DummyChat:
        def __init__(self):
            self.metadata = []

    class DummyClient:
        def start_chat(self, metadata=None, model=""):
            if metadata is not None:
                raise RuntimeError("restore broken")
            return DummyChat()

    _, restored = manager.get_or_restore_chat_session("s2", DummyClient(), model="gemini-3-flash")
    loaded = manager.get_session("s2")
    assert restored is False
    assert loaded["status"] == "recreated"
    assert loaded["last_restore_reason"] == "restore_failed_fallback"
    assert loaded["last_error_type"] == "RuntimeError"


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
    assert body["error"]["type"] == "service_unavailable"
    assert body["error"]["code"] == "SESSION_DB_PERMISSION_ERROR"
    assert "readonly database" in body["error"]["message"]


def test_allowlist_middleware_returns_openai_error_for_invalid_key(monkeypatch):
    monkeypatch.setenv("GEMINI_REVERSE_CONFIG", str(Path("data/runtime_config.json").resolve()))
    main_mod = _reload_module("app.main")

    async def call_next(_request):
        return SimpleNamespace(status_code=200)

    request = SimpleNamespace(
        url=SimpleNamespace(path="/v1/models"),
        headers={"x-api-key": "wrong"},
        client=SimpleNamespace(host="8.8.8.8"),
    )

    with patch.object(
        main_mod,
        "get_runtime_services",
        return_value=SimpleNamespace(
            runtime_config={
                "allowlist_enabled": True,
                "allowed_client_ips": ["10.0.0.0/8"],
                "trusted_proxies": ["127.0.0.1/32"],
                "api_keys": ["valid-key"],
                "admin_token": "admin-secret",
            }
        ),
    ):
        response = asyncio.run(main_mod.allowlist_middleware(request, call_next))

    assert response.status_code == 401
    body = json.loads(response.body.decode("utf-8"))
    assert body["error"]["type"] == "authentication_error"
    assert body["error"]["code"] == "ACCESS_DENIED"


def test_debug_loopback_bypass_can_skip_admin_token(monkeypatch):
    monkeypatch.setenv("GEMINI_REVERSE_CONFIG", str(Path("data/runtime_config.json").resolve()))
    main_mod = _reload_module("app.main")

    async def call_next(_request):
        return SimpleNamespace(status_code=200)

    request = SimpleNamespace(
        url=SimpleNamespace(path="/v1/debug/status"),
        headers={},
        client=SimpleNamespace(host="127.0.0.1"),
    )

    with patch.object(
        main_mod,
        "get_runtime_services",
        return_value=SimpleNamespace(
                runtime_config={
                    "allowlist_enabled": True,
                    "allowed_client_ips": ["127.0.0.1/32"],
                    "trusted_proxies": ["127.0.0.1/32"],
                    "api_keys": [],
                    "admin_token": "admin-secret",
                    "debug_loopback_bypass_enabled": True,
                }
            ),
        ):
        response = asyncio.run(main_mod.allowlist_middleware(request, call_next))

    assert response.status_code == 200


def test_debug_loopback_requires_admin_token_when_bypass_disabled(monkeypatch):
    monkeypatch.setenv("GEMINI_REVERSE_CONFIG", str(Path("data/runtime_config.json").resolve()))
    main_mod = _reload_module("app.main")

    async def call_next(_request):
        return SimpleNamespace(status_code=200)

    request = SimpleNamespace(
        url=SimpleNamespace(path="/v1/debug/status"),
        headers={},
        client=SimpleNamespace(host="127.0.0.1"),
    )

    with patch.object(
        main_mod,
        "get_runtime_services",
        return_value=SimpleNamespace(
            runtime_config={
                "allowlist_enabled": True,
                "allowed_client_ips": ["127.0.0.1/32"],
                "trusted_proxies": ["127.0.0.1/32"],
                "api_keys": [],
                "admin_token": "admin-secret",
                "debug_loopback_bypass_enabled": False,
            }
        ),
    ):
        response = asyncio.run(main_mod.allowlist_middleware(request, call_next))

    assert response.status_code == 401
    body = json.loads(response.body.decode("utf-8"))
    assert body["error"]["code"] == "ADMIN_TOKEN_REQUIRED"


def test_chat_completions_returns_openai_error_when_client_not_ready(monkeypatch):
    monkeypatch.setenv("GEMINI_REVERSE_CONFIG", str(Path("data/runtime_config.json").resolve()))
    main_mod = _reload_module("app.main")
    chat_service_mod = _reload_module("app.services.chat_service")

    class DummyRequest:
        headers = {}

        async def json(self):
            return {"messages": [{"role": "user", "content": "hello"}], "model": "gemini-3-flash"}

    async def fake_process_commands(_text):
        return False, ""

    with patch.object(chat_service_mod.context_manager, "process_commands", side_effect=fake_process_commands):
        with patch.object(
            chat_service_mod,
            "get_runtime_services",
            return_value=SimpleNamespace(
                gemini_conn=SimpleNamespace(client=None),
                session_manager=SimpleNamespace(),
                request_logger=SimpleNamespace(),
                state=SimpleNamespace(active_model="gemini-3-flash", active_account="1"),
                proxy="",
            ),
        ):
            response = asyncio.run(main_mod.chat_completions(DummyRequest()))

    assert response.status_code == 503
    body = json.loads(response.body.decode("utf-8"))
    assert body["error"]["type"] == "service_unavailable"
    assert body["error"]["code"] == "CLIENT_NOT_READY"


def test_embeddings_returns_openai_unsupported_error(monkeypatch):
    monkeypatch.setenv("GEMINI_REVERSE_CONFIG", str(Path("data/runtime_config.json").resolve()))
    main_mod = _reload_module("app.main")

    response = asyncio.run(main_mod.embeddings(None))

    assert response.status_code == 501
    body = json.loads(response.body.decode("utf-8"))
    assert body["error"]["type"] == "invalid_request_error"
    assert body["error"]["code"] == "EMBEDDINGS_NOT_SUPPORTED"


def test_debug_routes_can_be_disabled(tmp_path, monkeypatch):
    config_path = tmp_path / "runtime_config.json"
    payload = {
        "host": "127.0.0.1",
        "port": 8000,
        "model": "gemini-3-flash",
        "accounts": {},
        "debug_routes_enabled": False,
    }
    config_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setenv("GEMINI_REVERSE_CONFIG", str(config_path))

    _reload_module("app.config")
    main_mod = _reload_module("app.main")
    routes = {route.path for route in main_mod.app.routes}
    assert "/v1/debug/status" not in routes
    assert "/v1/debug/doctor" not in routes
    assert "/v1/debug/auth/status" not in routes
    assert "/v1/debug/auth/push_ticket" not in routes
    assert "/v1/debug/last" not in routes
    assert "/v1/debug/logs" not in routes


def test_debug_public_routes_are_scoped_when_enabled(monkeypatch):
    monkeypatch.setenv("GEMINI_REVERSE_CONFIG", str(Path("data/runtime_config.json").resolve()))
    _reload_module("app.config")
    main_mod = _reload_module("app.main")
    routes = {route.path for route in main_mod.app.routes}
    assert "/v1/debug/status" in routes
    assert "/v1/debug/network" in routes
    assert "/v1/debug/doctor" in routes
    assert "/v1/debug/auth/status" in routes
    assert "/v1/debug/auth/push_ticket" in routes
    assert "/v1/debug/last" not in routes
    assert "/v1/debug/logs" not in routes


def test_chat_completions_success_non_stream(monkeypatch):
    monkeypatch.setenv("GEMINI_REVERSE_CONFIG", str(Path("data/runtime_config.json").resolve()))
    chat_service_mod = _reload_module("app.services.chat_service")

    class DummyRequest:
        headers = {}

        async def json(self):
            return {"messages": [{"role": "user", "content": "hello"}], "model": "gemini-3-flash"}

    async def fake_process_commands(_text):
        return False, ""

    services = SimpleNamespace(
        gemini_conn=SimpleNamespace(client=object()),
        session_manager=SimpleNamespace(
            session_lock=lambda _sid: __import__("contextlib").nullcontext(),
            get_session=lambda _sid: {},
            get_or_restore_chat_session=lambda *args, **kwargs: (SimpleNamespace(metadata=[]), False),
            persist_live_session=lambda *args, **kwargs: None,
        ),
        request_logger=SimpleNamespace(
            log_request=lambda *args, **kwargs: None,
            log_error=lambda *args, **kwargs: None,
            log_info=lambda *args, **kwargs: None,
            log_stream_event=lambda *args, **kwargs: None,
        ),
        state=SimpleNamespace(active_model="gemini-3-flash", active_account="1"),
        proxy="",
    )

    with patch.object(chat_service_mod.context_manager, "process_commands", side_effect=fake_process_commands):
        with patch.object(chat_service_mod.context_manager, "build_stateless_prompt", return_value=("hello", [])):
            with patch.object(chat_service_mod, "_generate_with_retry", return_value=("hello from gemini", None)):
                with patch.object(chat_service_mod, "get_runtime_services", return_value=services):
                    response = asyncio.run(chat_service_mod.chat_completions(DummyRequest()))

    body = json.loads(response.body.decode("utf-8"))
    assert response.status_code == 200
    assert body["object"] == "chat.completion"
    assert body["choices"][0]["message"]["content"] == "hello from gemini"


def test_chat_completions_usage_limit_returns_openai_error(monkeypatch):
    monkeypatch.setenv("GEMINI_REVERSE_CONFIG", str(Path("data/runtime_config.json").resolve()))
    chat_service_mod = _reload_module("app.services.chat_service")

    class DummyRequest:
        headers = {}

        async def json(self):
            return {"messages": [{"role": "user", "content": "hello"}], "model": "gemini-3-flash"}

    async def fake_process_commands(_text):
        return False, ""

    services = SimpleNamespace(
        gemini_conn=SimpleNamespace(client=object()),
        session_manager=SimpleNamespace(
            session_lock=lambda _sid: __import__("contextlib").nullcontext(),
            get_session=lambda _sid: {},
            get_or_restore_chat_session=lambda *args, **kwargs: (SimpleNamespace(metadata=[]), False),
        ),
        request_logger=SimpleNamespace(
            log_request=lambda *args, **kwargs: None,
            log_error=lambda *args, **kwargs: None,
            log_info=lambda *args, **kwargs: None,
            log_stream_event=lambda *args, **kwargs: None,
        ),
        state=SimpleNamespace(active_model="gemini-3-flash", active_account="1"),
        proxy="",
    )

    with patch.object(chat_service_mod.context_manager, "process_commands", side_effect=fake_process_commands):
        with patch.object(chat_service_mod.context_manager, "build_stateless_prompt", return_value=("hello", [])):
            with patch.object(chat_service_mod, "_generate_with_retry", side_effect=UsageLimitExceeded("limit reached")):
                with patch.object(chat_service_mod, "get_runtime_services", return_value=services):
                    response = asyncio.run(chat_service_mod.chat_completions(DummyRequest()))

    body = json.loads(response.body.decode("utf-8"))
    assert response.status_code == 503
    assert body["error"]["type"] == "service_unavailable"
    assert body["error"]["code"] == "USAGE_LIMIT_EXCEEDED"


def test_request_logger_defaults_to_metadata_only(monkeypatch, tmp_path):
    logger_mod = _reload_module("app.logger")
    logger = logger_mod.RequestLogger(str(tmp_path))

    with patch.object(logger_mod, "get_runtime_config_snapshot", return_value={"debug_payload_logging": False}):
        logger.log_request(
            [{"role": "user", "content": "secret __Secure-1PSID=abc"}],
            [],
            "secret __Secure-1PSID=abc",
            False,
            "gemini-3-flash",
            "1",
        )
        logger.log_parse_result("authorization: Bearer token123", False, [])

    last = logger.get_last_request()
    assert "prompt_text" not in last
    assert "raw_output" not in last
    assert "prompt_hash" in last


def test_request_logger_sanitizes_payloads_when_enabled(monkeypatch, tmp_path):
    logger_mod = _reload_module("app.logger")
    logger = logger_mod.RequestLogger(str(tmp_path))

    with patch.object(logger_mod, "get_runtime_config_snapshot", return_value={"debug_payload_logging": True}):
        logger.log_request(
            [{"role": "user", "content": "secret __Secure-1PSID=abc"}],
            [],
            "secret __Secure-1PSID=abc",
            False,
            "gemini-3-flash",
            "1",
        )
        logger.log_parse_result("authorization: Bearer token123", False, [])

    last = logger.get_last_request()
    assert "***" in last["prompt_text"]
    assert "***" in last["raw_output"]
    assert "abc" not in last["prompt_text"]
    assert "token123" not in last["raw_output"]


def test_attach_runtime_services_binds_runtime_config_providers(monkeypatch):
    monkeypatch.setenv("GEMINI_REVERSE_CONFIG", str(Path("data/runtime_config.json").resolve()))
    runtime_mod = _reload_module("app.services.runtime_services")
    logger_mod = _reload_module("app.logger")
    session_mod = _reload_module("app.session_manager")

    app = SimpleNamespace()
    services = runtime_mod.attach_runtime_services(app)

    assert logger_mod.get_runtime_config_snapshot() == services.runtime_config
    assert session_mod.get_runtime_config_snapshot() == services.runtime_config


def test_readyz_reads_runtime_services(monkeypatch):
    monkeypatch.setenv("GEMINI_REVERSE_CONFIG", str(Path("data/runtime_config.json").resolve()))
    main_mod = _reload_module("app.main")

    with patch.object(
        main_mod,
        "get_runtime_services",
        return_value=SimpleNamespace(
            gemini_conn=SimpleNamespace(client=object()),
            state=SimpleNamespace(active_model="gemini-3-flash", active_account="1"),
        ),
    ):
        response = asyncio.run(main_mod.readyz())

    body = json.loads(response.body.decode("utf-8"))
    assert body["status"] == "ready"
    assert body["active_model"] == "gemini-3-flash"
    assert body["active_account"] == "1"


def test_debug_status_uses_runtime_services(monkeypatch):
    monkeypatch.setenv("GEMINI_REVERSE_CONFIG", str(Path("data/runtime_config.json").resolve()))
    debug_mod = _reload_module("app.routers.debug")

    request = SimpleNamespace()
    with patch.object(
        debug_mod,
        "get_runtime_services",
        return_value=SimpleNamespace(
            gemini_conn=SimpleNamespace(client=object(), last_refresh_result=None, last_request_error=None, last_request_error_type=None),
            state=SimpleNamespace(active_model="gemini-3-flash", active_account="1"),
            proxy="socks5://127.0.0.1:40000",
            accounts={"1": {"SECURE_1PSID": "x", "SECURE_1PSIDTS": "y", "cookies_dict": {"a": "b"}}},
            runtime_config={"debug_routes_enabled": True},
            get_current_account_data=lambda: {"SECURE_1PSID": "x", "SECURE_1PSIDTS": "y", "cookies_dict": {"a": "b"}},
        ),
    ):
        response = asyncio.run(debug_mod.debug_status(request))

    body = json.loads(response.body.decode("utf-8"))
    assert body["active_model"] == "gemini-3-flash"
    assert body["proxy"] == "socks5://127.0.0.1:40000"
    assert body["accounts_total"] == 1


def test_chat_stream_returns_error_chunk_not_assistant_text(monkeypatch):
    monkeypatch.setenv("GEMINI_REVERSE_CONFIG", str(Path("data/runtime_config.json").resolve()))
    chat_service_mod = _reload_module("app.services.chat_service")
    exc_mod = _reload_module("app.exceptions")

    class DummyRequest:
        headers = {}

        async def json(self):
            return {
                "messages": [{"role": "user", "content": "hello"}],
                "model": "gemini-3-flash",
                "stream": True,
            }

    async def fake_process_commands(_text):
        return False, ""

    async def consume_streaming_response(response):
        chunks = []
        async for chunk in response.body_iterator:
            chunks.append(chunk.decode("utf-8") if isinstance(chunk, bytes) else chunk)
        return "".join(chunks)

    def failing_stream(*_args, **_kwargs):
        async def gen():
            raise exc_mod.UpstreamQueueTimeoutError("queue timeout")
            yield  # pragma: no cover
        return gen()

    services = SimpleNamespace(
        gemini_conn=SimpleNamespace(client=object(), stream_with_failover=failing_stream),
        session_manager=SimpleNamespace(
            session_lock=lambda _sid: __import__("contextlib").nullcontext(),
            get_session=lambda _sid: {},
            get_or_restore_chat_session=lambda *args, **kwargs: (SimpleNamespace(), False),
        ),
        request_logger=SimpleNamespace(
            log_request=lambda *args, **kwargs: None,
            log_error=lambda *args, **kwargs: None,
            log_info=lambda *args, **kwargs: None,
            log_stream_event=lambda *args, **kwargs: None,
        ),
        state=SimpleNamespace(active_model="gemini-3-flash", active_account="1"),
        proxy="",
    )

    with patch.object(chat_service_mod.context_manager, "process_commands", side_effect=fake_process_commands):
        with patch.object(chat_service_mod.context_manager, "build_stateless_prompt", return_value=("hello", [])):
            with patch.object(chat_service_mod, "get_runtime_services", return_value=services):
                response = asyncio.run(chat_service_mod.chat_completions(DummyRequest()))

    payload = asyncio.run(consume_streaming_response(response))
    assert '"error"' in payload
    assert "UPSTREAM_QUEUE_TIMEOUT" in payload
    assert "[Gemini Proxy Error" not in payload


def test_chat_stream_success_returns_sse_chunks(monkeypatch):
    monkeypatch.setenv("GEMINI_REVERSE_CONFIG", str(Path("data/runtime_config.json").resolve()))
    chat_service_mod = _reload_module("app.services.chat_service")

    class DummyRequest:
        headers = {}

        async def json(self):
            return {
                "messages": [{"role": "user", "content": "hello"}],
                "model": "gemini-3-flash",
                "stream": True,
            }

    async def fake_process_commands(_text):
        return False, ""

    async def consume_streaming_response(response):
        chunks = []
        async for chunk in response.body_iterator:
            chunks.append(chunk.decode("utf-8") if isinstance(chunk, bytes) else chunk)
        return "".join(chunks)

    def good_stream(*_args, **_kwargs):
        async def gen():
            yield SimpleNamespace(text_delta="hello")
            yield SimpleNamespace(text_delta=" world")
        return gen()

    services = SimpleNamespace(
        gemini_conn=SimpleNamespace(client=object(), stream_with_failover=good_stream),
        session_manager=SimpleNamespace(
            session_lock=lambda _sid: __import__("contextlib").nullcontext(),
            get_session=lambda _sid: {},
            get_or_restore_chat_session=lambda *args, **kwargs: (SimpleNamespace(metadata=[]), False),
            persist_live_session=lambda *args, **kwargs: None,
        ),
        request_logger=SimpleNamespace(
            log_request=lambda *args, **kwargs: None,
            log_error=lambda *args, **kwargs: None,
            log_info=lambda *args, **kwargs: None,
            log_stream_event=lambda *args, **kwargs: None,
        ),
        state=SimpleNamespace(active_model="gemini-3-flash", active_account="1"),
        proxy="",
    )

    with patch.object(chat_service_mod.context_manager, "process_commands", side_effect=fake_process_commands):
        with patch.object(chat_service_mod.context_manager, "build_stateless_prompt", return_value=("hello", [])):
            with patch.object(chat_service_mod, "get_runtime_services", return_value=services):
                response = asyncio.run(chat_service_mod.chat_completions(DummyRequest()))

    payload = asyncio.run(consume_streaming_response(response))
    assert '"object": "chat.completion.chunk"' in payload
    assert "hello" in payload
    assert "data: [DONE]" in payload
