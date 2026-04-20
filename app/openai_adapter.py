from __future__ import annotations

import json

from fastapi.responses import JSONResponse
from gemini_webapi.exceptions import UsageLimitExceeded


def make_openai_error_body(message: str, *, error_type: str, code: str) -> dict:
    return {
        "error": {
            "message": str(message),
            "type": str(error_type),
            "code": str(code),
        }
    }


def make_openai_error_response(
    message: str,
    *,
    error_type: str,
    code: str,
    status_code: int,
) -> JSONResponse:
    return JSONResponse(
        make_openai_error_body(message, error_type=error_type, code=code),
        status_code=status_code,
    )


def make_exception_error_response(exc: Exception, *, status_code: int = 500) -> JSONResponse:
    code, error_type = resolve_exception_contract(exc)
    return make_openai_error_response(
        str(exc),
        error_type=error_type,
        code=code,
        status_code=status_code,
    )


def make_embeddings_unsupported_response() -> JSONResponse:
    return make_openai_error_response(
        "embeddings is not supported by gemini-reverse",
        error_type="invalid_request_error",
        code="EMBEDDINGS_NOT_SUPPORTED",
        status_code=501,
    )


def make_openai_error_chunk(
    message: str,
    *,
    error_type: str,
    code: str,
) -> str:
    return f"data: {json.dumps(make_openai_error_body(message, error_type=error_type, code=code), ensure_ascii=False)}\n\n"


def make_exception_error_chunk(exc: Exception) -> str:
    code, error_type = resolve_exception_contract(exc)
    return make_openai_error_chunk(
        str(exc),
        error_type=error_type,
        code=code,
    )


def resolve_exception_contract(exc: Exception) -> tuple[str, str]:
    if isinstance(exc, UsageLimitExceeded):
        return "USAGE_LIMIT_EXCEEDED", "service_unavailable"
    code = str(getattr(exc, "error_type", "UNKNOWN_ERROR"))
    if code == "SESSION_DB_PERMISSION_ERROR":
        return code, "service_unavailable"
    return code, "upstream_error"


def make_usage_limit_exceeded_response(
    message: str = "all configured accounts have exhausted their usage limits",
) -> JSONResponse:
    return make_openai_error_response(
        message,
        error_type="service_unavailable",
        code="USAGE_LIMIT_EXCEEDED",
        status_code=503,
    )
