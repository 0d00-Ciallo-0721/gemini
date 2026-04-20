from __future__ import annotations

from fastapi import APIRouter, Request

from ..services.chat_service import chat_completions, completions, embeddings

router = APIRouter()


@router.post("/v1/chat/completions")
async def chat_completions_route(request: Request):
    return await chat_completions(request)


@router.post("/v1/completions")
async def completions_route(request: Request):
    return await completions(request)


@router.post("/v1/embeddings")
async def embeddings_route(request: Request):
    return await embeddings(request)
