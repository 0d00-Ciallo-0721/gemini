from __future__ import annotations

import time

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from ..services.runtime_services import get_runtime_services

router = APIRouter()


async def list_models_response(request: Request | None = None):
    services = get_runtime_services(request)
    active_model = services.state.active_model
    try:
        from gemini_webapi.constants import Model

        models = [value.value[0] for value in Model if value.value[0] and value.value[0] != "unspecified"]
    except Exception:
        models = [active_model]
    if active_model not in models:
        models.append(active_model)
    return JSONResponse({"object": "list", "data": [{"id": model_id, "object": "model", "created": int(time.time()), "owned_by": "google/gemini_webapi"} for model_id in models]})


@router.get("/v1/models")
async def list_models(request: Request):
    return await list_models_response(request)
