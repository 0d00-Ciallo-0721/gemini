from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from ..services.runtime_services import get_runtime_services
from runtime.auth_status import get_auth_status_payload
from runtime.healthcheck import run_doctor
from runtime.ticket_receiver import handle_push_ticket

router = APIRouter()


@router.get("/v1/debug/last")
async def debug_last(request: Request):
    last = get_runtime_services(request).request_logger.get_last_request()
    if not last:
        return JSONResponse({"message": "No requests recorded yet"})
    return JSONResponse(last)


@router.get("/v1/debug/logs")
async def debug_logs(request: Request):
    logs = get_runtime_services(request).request_logger.get_recent_logs(30)
    return JSONResponse({"count": len(logs), "logs": logs})


@router.get("/v1/debug/status")
async def debug_status(request: Request):
    services = get_runtime_services(request)
    runtime_config = services.runtime_config
    auth_data = services.get_current_account_data() if services.state.active_account else {}
    return JSONResponse({
        "status": "running",
        "active_model": services.state.active_model,
        "proxy": services.proxy or "disabled",
        "active_account": services.state.active_account,
        "accounts_total": len(services.accounts),
        "client_ready": services.gemini_conn.client is not None,
        "has_psid": bool(auth_data.get("SECURE_1PSID") or auth_data.get("__Secure-1PSID")),
        "has_psidts": bool(auth_data.get("SECURE_1PSIDTS") or auth_data.get("__Secure-1PSIDTS")),
        "cookies_dict_count": len(auth_data.get("cookies_dict", {})),
        "last_refresh_result": getattr(services.gemini_conn, "last_refresh_result", None),
        "last_request_error": getattr(services.gemini_conn, "last_request_error", None),
        "last_request_error_type": getattr(services.gemini_conn, "last_request_error_type", None),
        "debug_routes_enabled": bool(runtime_config.get("debug_routes_enabled", True)),
    })


@router.get("/v1/debug/network")
async def debug_network(request: Request):
    services = get_runtime_services(request)
    is_client_set = bool(services.gemini_conn.client and getattr(services.gemini_conn.client, "proxy", None))
    return JSONResponse({"runtime_proxy_value": services.proxy or "", "is_proxy_configured": bool(services.proxy), "is_client_initialized_with_proxy": is_client_set, "client_proxy_value": getattr(services.gemini_conn.client, "proxy", None), "active_model": services.state.active_model, "active_account": services.state.active_account})


@router.get("/v1/debug/doctor")
async def debug_doctor(request: Request):
    services = get_runtime_services(request)
    return JSONResponse(await run_doctor(services.runtime_config, services.auth_manager))


@router.post("/v1/debug/auth/push_ticket")
async def push_ticket(request: Request):
    services = get_runtime_services(request)
    auth_manager = services.auth_manager
    if not auth_manager:
        return JSONResponse({"error": "Auth Manager not enabled"}, status_code=503)
    body = await request.json()
    runtime_config = services.runtime_config
    secret = runtime_config.get("relay_shared_secret", "change_me_to_a_random_string")
    success, msg = handle_push_ticket(auth_manager, body, secret)
    if success:
        if runtime_config.get("relay_accept_push_without_restart", True):
            services.gemini_conn.reload_runtime_config()
        return JSONResponse({"status": "success", "message": msg})
    return JSONResponse({"status": "error", "message": msg}, status_code=401)


@router.get("/v1/debug/auth/status")
async def auth_status(request: Request):
    auth_manager = get_runtime_services(request).auth_manager
    if not auth_manager:
        return JSONResponse({"error": "Auth Manager not enabled"}, status_code=503)
    return JSONResponse(get_auth_status_payload(auth_manager))
