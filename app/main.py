from __future__ import annotations

import asyncio
import builtins
import sys
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
import uvicorn

from .openai_adapter import make_exception_error_response, make_openai_error_response
from .routers.chat import router as chat_router
from .routers.debug import router as debug_router
from .routers.models import list_models_response as list_models, router as models_router
from .security import get_real_client_ip, has_valid_service_key, is_ip_allowed, require_admin_token
from .services.chat_service import chat_completions as chat_completions, completions as completions, embeddings as embeddings
from .services.runtime_services import attach_runtime_services, build_runtime_services, get_runtime_services

_ORIGINAL_PRINT = builtins.print


def _safe_print(*args, **kwargs):
    sep = kwargs.get("sep", " ")
    end = kwargs.get("end", "\n")
    file = kwargs.get("file", sys.stdout)
    flush = kwargs.get("flush", False)
    text = sep.join(str(arg) for arg in args)
    try:
        file.write(text + end)
    except UnicodeEncodeError:
        encoding = getattr(file, "encoding", None) or "utf-8"
        safe_text = (text + end).encode(encoding, errors="replace").decode(encoding, errors="replace")
        file.write(safe_text)
    if flush:
        file.flush()


builtins.print = _safe_print


@asynccontextmanager
async def lifespan(_: FastAPI):
    services = attach_runtime_services(app)
    runtime_config = services.runtime_config
    services.request_logger.reconfigure(str(runtime_config.get("log_dir") or "logs"))
    services.session_manager.set_db_path(str(runtime_config.get("session_db_path") or "reverse_sessions.sqlite3"))
    services.session_manager.assert_writable()
    print("\n" + "=" * 60)
    print("Gemini Reverse Standalone 服务启动中…")
    print(f"  模型: {services.state.active_model}")
    print(f"  账号池: {len(services.accounts)}")
    print(f"  端口: {runtime_config.get('port', 8000)}")
    print(f"  代理: {services.proxy or 'disabled'}")
    await services.gemini_conn.initialize()
    print("=" * 60 + "\n")
    try:
        yield
    finally:
        services.request_logger.close()
        await services.gemini_conn.close()


app = FastAPI(title="Gemini Reverse Standalone", lifespan=lifespan)


def _module_runtime_config():
    return build_runtime_services().runtime_config


def _local_error_response(exc: Exception, *, status_code: int = 500):
    return make_exception_error_response(exc, status_code=status_code)


@app.middleware("http")
async def allowlist_middleware(request: Request, call_next):
    path = request.url.path
    runtime_config = get_runtime_services(request).runtime_config
    client_ip = get_real_client_ip(request, runtime_config.get("trusted_proxies", []) or [])
    is_loopback = client_ip in {"127.0.0.1", "::1", "localhost"}
    if path in {"/healthz", "/readyz"}:
        return await call_next(request)
    if path.startswith("/v1/debug/") and not is_loopback and not require_admin_token(request, runtime_config):
        return make_openai_error_response(
            "admin token required",
            error_type="authentication_error",
            code="ADMIN_TOKEN_REQUIRED",
            status_code=401,
        )
    if is_ip_allowed(client_ip, runtime_config):
        return await call_next(request)
    if has_valid_service_key(request, runtime_config):
        return await call_next(request)
    if runtime_config.get("api_keys"):
        return make_openai_error_response(
            "client ip is not allowlisted and api key is invalid",
            error_type="authentication_error",
            code="ACCESS_DENIED",
            status_code=401,
        )
    return make_openai_error_response(
        "client ip is not allowlisted",
        error_type="authentication_error",
        code="ACCESS_DENIED",
        status_code=403,
    )


@app.get("/healthz")
async def healthz():
    return JSONResponse({"status": "ok"})


@app.get("/readyz")
async def readyz():
    services = get_runtime_services()
    return JSONResponse({
        "status": "ready" if services.gemini_conn.client else "starting",
        "client_ready": services.gemini_conn.client is not None,
        "active_model": services.state.active_model,
        "active_account": services.state.active_account,
    })


app.include_router(models_router)
app.include_router(chat_router)
if _module_runtime_config().get("debug_routes_enabled", True):
    app.include_router(debug_router)


if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    runtime_config = _module_runtime_config()
    uvicorn.run(app, host=str(runtime_config.get("host") or "127.0.0.1"), port=int(runtime_config.get("port") or 8000), reload=False)
