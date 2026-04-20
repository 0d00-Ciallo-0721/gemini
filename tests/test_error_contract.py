import importlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace

from gemini_webapi.exceptions import UsageLimitExceeded


def _reload_module(name: str):
    if name in sys.modules:
        return importlib.reload(sys.modules[name])
    return importlib.import_module(name)


def test_error_contract_access_denied(monkeypatch):
    monkeypatch.setenv("GEMINI_REVERSE_CONFIG", str(Path("data/runtime_config.json").resolve()))
    main_mod = _reload_module("app.main")

    async def call_next(_request):
        return SimpleNamespace(status_code=200)

    request = SimpleNamespace(
        url=SimpleNamespace(path="/v1/models"),
        headers={"x-api-key": "wrong"},
        client=SimpleNamespace(host="8.8.8.8"),
    )

    main_mod.get_runtime_services = lambda _request=None: SimpleNamespace(
        runtime_config={
            "allowlist_enabled": True,
            "allowed_client_ips": ["10.0.0.0/8"],
            "trusted_proxies": ["127.0.0.1/32"],
            "api_keys": ["valid-key"],
            "admin_token": "admin-secret",
            "debug_loopback_bypass_enabled": True,
        }
    )
    response = __import__("asyncio").run(main_mod.allowlist_middleware(request, call_next))
    body = json.loads(response.body.decode("utf-8"))
    assert response.status_code == 401
    assert set(body["error"]) == {"message", "type", "code"}
    assert body["error"]["code"] == "ACCESS_DENIED"
    assert body["error"]["type"] == "authentication_error"


def test_error_contract_admin_token_required(monkeypatch):
    monkeypatch.setenv("GEMINI_REVERSE_CONFIG", str(Path("data/runtime_config.json").resolve()))
    main_mod = _reload_module("app.main")

    async def call_next(_request):
        return SimpleNamespace(status_code=200)

    request = SimpleNamespace(
        url=SimpleNamespace(path="/v1/debug/status"),
        headers={},
        client=SimpleNamespace(host="8.8.8.8"),
    )

    main_mod.get_runtime_services = lambda _request=None: SimpleNamespace(
        runtime_config={
            "allowlist_enabled": True,
            "allowed_client_ips": ["8.8.8.8/32"],
            "trusted_proxies": ["127.0.0.1/32"],
            "api_keys": [],
            "admin_token": "admin-secret",
            "debug_loopback_bypass_enabled": False,
        }
    )
    response = __import__("asyncio").run(main_mod.allowlist_middleware(request, call_next))
    body = json.loads(response.body.decode("utf-8"))
    assert response.status_code == 401
    assert set(body["error"]) == {"message", "type", "code"}
    assert body["error"]["code"] == "ADMIN_TOKEN_REQUIRED"
    assert body["error"]["type"] == "authentication_error"


def test_error_contract_client_not_ready(monkeypatch):
    monkeypatch.setenv("GEMINI_REVERSE_CONFIG", str(Path("data/runtime_config.json").resolve()))
    main_mod = _reload_module("app.main")
    chat_service_mod = _reload_module("app.services.chat_service")

    class DummyRequest:
        headers = {}

        async def json(self):
            return {"messages": [{"role": "user", "content": "hello"}], "model": "gemini-3-flash"}

    async def fake_process_commands(_text):
        return False, ""

    with __import__("unittest").mock.patch.object(chat_service_mod.context_manager, "process_commands", side_effect=fake_process_commands):
        with __import__("unittest").mock.patch.object(
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
            response = __import__("asyncio").run(main_mod.chat_completions(DummyRequest()))

    body = json.loads(response.body.decode("utf-8"))
    assert response.status_code == 503
    assert set(body["error"]) == {"message", "type", "code"}
    assert body["error"]["code"] == "CLIENT_NOT_READY"
    assert body["error"]["type"] == "service_unavailable"


def test_error_contract_embeddings_not_supported(monkeypatch):
    monkeypatch.setenv("GEMINI_REVERSE_CONFIG", str(Path("data/runtime_config.json").resolve()))
    main_mod = _reload_module("app.main")
    response = __import__("asyncio").run(main_mod.embeddings(None))
    body = json.loads(response.body.decode("utf-8"))
    assert response.status_code == 501
    assert set(body["error"]) == {"message", "type", "code"}
    assert body["error"]["code"] == "EMBEDDINGS_NOT_SUPPORTED"
    assert body["error"]["type"] == "invalid_request_error"


def test_error_contract_session_db_permission_error(monkeypatch):
    monkeypatch.setenv("GEMINI_REVERSE_CONFIG", str(Path("data/runtime_config.json").resolve()))
    main_mod = _reload_module("app.main")
    exc_mod = _reload_module("app.exceptions")
    response = main_mod._local_error_response(
        exc_mod.SessionDbPermissionError("session database is not writable"),
        status_code=503,
    )
    body = json.loads(response.body.decode("utf-8"))
    assert response.status_code == 503
    assert set(body["error"]) == {"message", "type", "code"}
    assert body["error"]["code"] == "SESSION_DB_PERMISSION_ERROR"
    assert body["error"]["type"] == "service_unavailable"


def test_error_contract_usage_limit_exceeded(monkeypatch):
    monkeypatch.setenv("GEMINI_REVERSE_CONFIG", str(Path("data/runtime_config.json").resolve()))
    adapter_mod = _reload_module("app.openai_adapter")
    response = adapter_mod.make_exception_error_response(UsageLimitExceeded("limit reached"), status_code=503)
    body = json.loads(response.body.decode("utf-8"))
    assert response.status_code == 503
    assert set(body["error"]) == {"message", "type", "code"}
    assert body["error"]["code"] == "USAGE_LIMIT_EXCEEDED"
    assert body["error"]["type"] == "service_unavailable"
