from __future__ import annotations

import asyncio
import hashlib
import json
import os
import tempfile
import time
import uuid

from fastapi import Request
from fastapi.responses import JSONResponse, StreamingResponse

from gemini_webapi.exceptions import UsageLimitExceeded

from ..api_client import ContextMigrationNeeded
from ..context_manager import context_manager
from ..exceptions import SessionDbPermissionError
from ..openai_adapter import (
    make_embeddings_unsupported_response,
    make_exception_error_chunk,
    make_exception_error_response,
    make_openai_error_chunk,
    make_openai_error_response,
)
from ..reverse_session import extract_reverse_session_from_messages
from ..tool_adapter import build_tool_aware_prompt
from ..tool_parser import StreamToolDecoder, parse_tool_calls
from .runtime_services import get_runtime_services

MAX_TOOL_PARSE_RETRIES = 1
NON_STREAM_TIMEOUT = 300


def _resolve_requested_model(body: dict, active_model: str) -> str:
    return str((body or {}).get("model") or "").strip() or active_model


def _resolve_session_id(messages: list[dict], reverse_session_info: dict[str, str], request: Request) -> str:
    explicit = reverse_session_info.get("session_id") or request.headers.get("X-Session-Id") or request.headers.get("X-OC-Session-Id")
    if explicit:
        return explicit
    stable_msgs = []
    for message in messages[:2]:
        content = message.get("content", "")
        if isinstance(content, list):
            content = "".join(part.get("text", "") for part in content if part.get("type") == "text")
        stable_msgs.append({"r": message.get("role"), "c": content})
    fingerprint = json.dumps(stable_msgs, ensure_ascii=False, sort_keys=True)
    return "hash_" + hashlib.md5(fingerprint.encode("utf-8")).hexdigest()[:12]


def _extract_last_text(messages: list[dict]) -> str:
    if not messages:
        return ""
    content = messages[-1].get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(part.get("text", "") for part in content if part.get("type") == "text")
    return ""


def _process_extracted_files(files_list, request_logger=None):
    if not files_list or len(files_list) <= 9:
        return files_list
    if request_logger:
        request_logger.log_info(
            f"attachment count reached {len(files_list)}; applying merge strategy",
            context="runtime",
        )
    files_to_merge = files_list[:-8]
    retained_files = files_list[-8:]
    merged_content = ""
    merged_count = 0
    for file_path in files_to_merge:
        if isinstance(file_path, str) and os.path.exists(file_path):
            try:
                with open(file_path, "r", encoding="utf-8") as handle:
                    file_data = handle.read()
                filename = os.path.basename(file_path)
                merged_content += f"\n=================================\nFILE CHUNK: {filename}\n=================================\n\n{file_data}\n\n"
                merged_count += 1
                os.remove(file_path)
            except Exception:
                try:
                    os.remove(file_path)
                except Exception:
                    pass
    if merged_content:
        fd, merged_path = tempfile.mkstemp(suffix=".txt", prefix="merged_history_chunks_")
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(merged_content)
        if request_logger:
            request_logger.log_info(
                f"merged {merged_count} history files into one chunk package",
                context="runtime",
            )
        return [merged_path] + retained_files
    return retained_files


def _cleanup_files(extracted_files: list):
    if not extracted_files:
        return
    for file_path in extracted_files:
        if isinstance(file_path, str) and os.path.exists(file_path):
            try:
                os.remove(file_path)
            except Exception:
                pass


def _gen_id() -> str:
    return f"chatcmpl-{uuid.uuid4().hex[:12]}"


def _estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4)


def _make_sse_chunk(data: dict) -> str:
    return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"


def _base_chunk(chunk_id: str, model: str) -> dict:
    return {
        "id": chunk_id,
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "system_fingerprint": "fp_gemini",
    }


def make_sync_response(text: str, prompt_text: str = "", model: str = ""):
    used_model = model
    prompt_tokens = _estimate_tokens(prompt_text)
    completion_tokens = _estimate_tokens(text)
    return JSONResponse({
        "id": _gen_id(),
        "object": "chat.completion",
        "created": int(time.time()),
        "model": used_model,
        "choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens, "total_tokens": prompt_tokens + completion_tokens},
    })


def make_tool_call_response(result, prompt_text: str = "", model: str = ""):
    used_model = model
    tool_calls_data = []
    for tool_call in result.tool_calls:
        tool_calls_data.append({
            "id": tool_call.id,
            "type": "function",
            "function": {"name": tool_call.name, "arguments": tool_call.arguments},
        })
    message = {"role": "assistant", "content": result.text or None}
    if tool_calls_data:
        message["tool_calls"] = tool_calls_data
    raw_text = (result.text or "") + "".join(tool_call.arguments for tool_call in result.tool_calls)
    prompt_tokens = _estimate_tokens(prompt_text)
    completion_tokens = _estimate_tokens(raw_text)
    return JSONResponse({
        "id": _gen_id(),
        "object": "chat.completion",
        "created": int(time.time()),
        "model": used_model,
        "choices": [{"index": 0, "message": message, "finish_reason": "tool_calls" if tool_calls_data else "stop"}],
        "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens, "total_tokens": prompt_tokens + completion_tokens},
    })


def make_sse_role_chunk(chunk_id: str, model: str) -> str:
    data = _base_chunk(chunk_id, model)
    data["choices"] = [{"index": 0, "delta": {"role": "assistant", "content": ""}, "finish_reason": None}]
    return _make_sse_chunk(data)


def make_sse_text_delta(text: str, chunk_id: str, model: str) -> str:
    data = _base_chunk(chunk_id, model)
    data["choices"] = [{"index": 0, "delta": {"content": text}, "finish_reason": None}]
    return _make_sse_chunk(data)


def make_sse_tool_call_delta(tool_call, index: int, chunk_id: str, model: str) -> str:
    data = _base_chunk(chunk_id, model)
    data["choices"] = [{
        "index": 0,
        "delta": {"tool_calls": [{"index": index, "id": tool_call.id, "type": "function", "function": {"name": tool_call.name, "arguments": tool_call.arguments}}]},
        "finish_reason": None,
    }]
    return _make_sse_chunk(data)


def make_sse_done(finish_reason: str, chunk_id: str, model: str) -> str:
    data = _base_chunk(chunk_id, model)
    data["choices"] = [{"index": 0, "delta": {}, "finish_reason": finish_reason}]
    return _make_sse_chunk(data)


def _normalize_proxy_error_text(err_str: str) -> str:
    text = (err_str or "").strip()
    for prefix in ("格式错", "格式错误"):
        if text.startswith(prefix):
            text = text[len(prefix):].lstrip(" :：")
    return text or "Unknown error"


def _error_type_of(exc: Exception) -> str:
    return str(getattr(exc, "error_type", "UNKNOWN_ERROR"))


async def _generate_with_retry(
    prompt_text: str,
    tools: list,
    extracted_files: list,
    has_tools: bool,
    requested_model: str,
    chat_session=None,
    *,
    active_gemini_conn,
    active_request_logger,
):
    for attempt in range(MAX_TOOL_PARSE_RETRIES + 1):
        try:
            response = await asyncio.wait_for(
                active_gemini_conn.generate_with_failover(
                    prompt_text,
                    model=requested_model,
                    files=extracted_files if extracted_files else None,
                    stream=False,
                    chat=chat_session,
                ),
                timeout=NON_STREAM_TIMEOUT,
            )
        except asyncio.TimeoutError:
            active_request_logger.log_error(f"Non-stream timeout ({NON_STREAM_TIMEOUT}s)", "sync")
            raise TimeoutError(f"Gemini 响应超时 ({NON_STREAM_TIMEOUT}s)")
        raw_text = response.text
        active_request_logger.log_parse_result(raw_text, False, [], mode=f"batch_attempt_{attempt}")
        if not has_tools:
            return raw_text, None
        result = parse_tool_calls(raw_text)
        active_request_logger.log_parse_result(raw_text, result.has_calls, [call.name for call in result.tool_calls], mode=f"batch_attempt_{attempt}")
        if result.has_calls:
            return raw_text, result
        if attempt < MAX_TOOL_PARSE_RETRIES:
            tool_names = [tool.get("function", tool).get("name", "") for tool in tools]
            if any(name in raw_text for name in tool_names if name):
                continue
        return raw_text, result
    return raw_text, result


async def chat_completions(request: Request):
    services = get_runtime_services(request)
    active_gemini_conn = services.gemini_conn
    active_session_manager = services.session_manager
    active_request_logger = services.request_logger
    active_state = services.state
    active_proxy = services.proxy
    body = await request.json()
    messages = body.get("messages", [])
    tools = body.get("tools", [])
    tool_choice = body.get("tool_choice")
    stream = body.get("stream", False)
    requested_model = _resolve_requested_model(body, active_state.active_model)
    if not messages:
        return make_openai_error_response(
            "No messages provided",
            error_type="invalid_request_error",
            code="INVALID_MESSAGES",
            status_code=400,
        )

    reverse_session_info, messages = extract_reverse_session_from_messages(messages)
    is_cmd, cmd_reply = await context_manager.process_commands(_extract_last_text(messages))
    if is_cmd:
        if stream:
            async def cmd_generator():
                chunk_id = _gen_id()
                yield make_sse_role_chunk(chunk_id, requested_model)
                yield make_sse_text_delta(cmd_reply, chunk_id, requested_model)
                yield make_sse_done("stop", chunk_id, requested_model)
                yield "data: [DONE]\n\n"
            return StreamingResponse(cmd_generator(), media_type="text/event-stream")
        return make_sync_response(cmd_reply, model=requested_model)

    if not active_gemini_conn.client:
        return make_openai_error_response(
            "client is not ready",
            error_type="service_unavailable",
            code="CLIENT_NOT_READY",
            status_code=503,
        )

    agent_type = request.headers.get("X-Agent-Type") or request.headers.get("X-OC-Agent-Type", "Main")
    parent_session_id = request.headers.get("X-Parent-Session-Id") or request.headers.get("X-OC-Parent-Id") or reverse_session_info.get("parent_session_id", "")
    session_id = _resolve_session_id(messages, reverse_session_info, request)
    with active_session_manager.session_lock(session_id):
        existing_session = active_session_manager.get_session(session_id)
        start_index = int((existing_session or {}).get("last_msg_idx") or 0)
        try:
            chat_session, restored = active_session_manager.get_or_restore_chat_session(
                session_id,
                active_gemini_conn.client,
                model=requested_model,
                parent_session_id=parent_session_id,
                agent_type=agent_type,
            )
        except SessionDbPermissionError as exc:
            active_request_logger.log_error(
                f"[{exc.error_type}] {str(exc)}\n[db: {active_session_manager.db_path}]",
                "session",
            )
            return make_exception_error_response(exc, status_code=503)
    active_request_logger.log_info(
        f"session restored={restored} session_id={session_id} model={requested_model}",
        context="session",
    )

    has_tools = bool(tools) and tool_choice != "none"
    if has_tools:
        prompt_text, extracted_files = build_tool_aware_prompt(messages, tools, tool_choice=tool_choice, max_prompt_chars=40000, start_index=start_index)
    else:
        prompt_text, extracted_files = context_manager.build_stateless_prompt(messages)
    extracted_files = _process_extracted_files(extracted_files, active_request_logger)
    active_request_logger.log_request(messages, tools, prompt_text, has_tools=has_tools, model=requested_model, account=active_state.active_account)

    if stream:
        async def event_generator():
            nonlocal prompt_text, extracted_files, chat_session
            chunk_id = _gen_id()
            decoder = StreamToolDecoder() if has_tools else None
            tool_call_index = 0
            has_yielded_tool = False
            total_text_chars = 0
            chunks_count = 0
            yield make_sse_role_chunk(chunk_id, requested_model)
            try:
                while True:
                    try:
                        stream_gen = active_gemini_conn.stream_with_failover(prompt_text, model=requested_model, files=extracted_files if extracted_files else None, chat=chat_session)
                        async for chunk in stream_gen:
                            chunks_count += 1
                            if not chunk.text_delta:
                                continue
                            total_text_chars += len(chunk.text_delta)
                            if decoder:
                                events = decoder.push(chunk.text_delta)
                                for event in events:
                                    if event.kind == "text_delta" and event.text and not has_yielded_tool:
                                        yield make_sse_text_delta(event.text, chunk_id, requested_model)
                                    elif event.kind == "tool_call_finalized" and event.tool_call:
                                        has_yielded_tool = True
                                        active_request_logger.log_stream_event("tool_call_finalized", tool_name=event.tool_call.name)
                                        yield make_sse_tool_call_delta(event.tool_call, tool_call_index, chunk_id, requested_model)
                                        tool_call_index += 1
                            else:
                                yield make_sse_text_delta(chunk.text_delta, chunk_id, requested_model)
                        if decoder:
                            for event in decoder.flush():
                                if event.kind == "text_delta" and event.text and not has_yielded_tool:
                                    yield make_sse_text_delta(event.text, chunk_id, requested_model)
                                    total_text_chars += len(event.text)
                                elif event.kind == "tool_call_finalized" and event.tool_call:
                                    has_yielded_tool = True
                                    active_request_logger.log_stream_event("tool_call_finalized", tool_name=event.tool_call.name)
                                    yield make_sse_tool_call_delta(event.tool_call, tool_call_index, chunk_id, requested_model)
                                    tool_call_index += 1
                        if total_text_chars == 0 and not has_yielded_tool:
                            active_request_logger.log_error(
                                f"stream finished without content or tool calls (chunks={chunks_count})",
                                "stream",
                            )
                        else:
                            active_request_logger.log_info(
                                f"stream finished chunks={chunks_count} chars={total_text_chars}",
                                context="stream",
                            )
                        finish_reason = "tool_calls" if (decoder and decoder.had_calls) else "stop"
                        yield make_sse_done(finish_reason, chunk_id, requested_model)
                        with active_session_manager.session_lock(session_id):
                            active_session_manager.persist_live_session(
                                session_id,
                                chat_session,
                                last_msg_idx=len(messages),
                                model=requested_model,
                                parent_session_id=parent_session_id,
                                agent_type=agent_type,
                            )
                        break
                    except ContextMigrationNeeded as exc:
                        active_request_logger.log_info(
                            f"context migration detected during stream: {exc}; rebuilding session for model={requested_model}",
                            context="session",
                        )
                        _cleanup_files(extracted_files)
                        chat_session = active_gemini_conn.client.start_chat(model=requested_model) if active_gemini_conn.client else None
                        with active_session_manager.session_lock(session_id):
                            active_session_manager.create_or_reset_session(
                                session_id,
                                chat_session,
                                parent_session_id=parent_session_id,
                                model=requested_model,
                                agent_type=agent_type,
                            )
                        prompt_text, new_ext_files = build_tool_aware_prompt(messages, tools, tool_choice=tool_choice, max_prompt_chars=40000, start_index=0)
                        extracted_files = _process_extracted_files(new_ext_files, active_request_logger)
                        continue
            except Exception as exc:
                err_str = _normalize_proxy_error_text(str(exc) or repr(exc))
                error_type = _error_type_of(exc)
                active_request_logger.log_error(
                    f"[{error_type}] {err_str}\n[模型: {requested_model} | 代理: {active_proxy or 'disabled'}]",
                    "stream",
                )
                yield make_openai_error_chunk(
                    err_str,
                    error_type="upstream_error",
                    code=error_type,
                )
            finally:
                _cleanup_files(extracted_files)
                yield "data: [DONE]\n\n"
        return StreamingResponse(event_generator(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"})

    try:
        while True:
            try:
                raw_text, result = await _generate_with_retry(
                    prompt_text,
                    tools,
                    extracted_files,
                    has_tools,
                    requested_model,
                    chat_session=chat_session,
                    active_gemini_conn=active_gemini_conn,
                    active_request_logger=active_request_logger,
                )
                with active_session_manager.session_lock(session_id):
                    active_session_manager.persist_live_session(
                        session_id,
                        chat_session,
                        last_msg_idx=len(messages),
                        model=requested_model,
                        parent_session_id=parent_session_id,
                        agent_type=agent_type,
                    )
                if has_tools and result and result.has_calls:
                    return make_tool_call_response(result, prompt_text, model=requested_model)
                return make_sync_response(raw_text, prompt_text, model=requested_model)
            except ContextMigrationNeeded as exc:
                active_request_logger.log_info(
                    f"context migration detected during sync: {exc}; rebuilding session for model={requested_model}",
                    context="session",
                )
                _cleanup_files(extracted_files)
                chat_session = active_gemini_conn.client.start_chat(model=requested_model) if active_gemini_conn.client else None
                with active_session_manager.session_lock(session_id):
                    active_session_manager.create_or_reset_session(
                        session_id,
                        chat_session,
                        parent_session_id=parent_session_id,
                        model=requested_model,
                        agent_type=agent_type,
                    )
                prompt_text, new_ext_files = build_tool_aware_prompt(messages, tools, tool_choice=tool_choice, max_prompt_chars=40000, start_index=0)
                extracted_files = _process_extracted_files(new_ext_files, active_request_logger)
                continue
            except UsageLimitExceeded:
                active_request_logger.log_error("所有账号额度耗尽", "sync")
                return make_sync_response("所有账号额度已耗尽，请稍后再试。", model=requested_model)
            except SessionDbPermissionError as exc:
                active_request_logger.log_error(
                    f"[{exc.error_type}] {str(exc)}\n[db: {active_session_manager.db_path}]",
                    "sync",
                )
                return make_exception_error_response(exc, status_code=503)
            except Exception as exc:
                error_type = _error_type_of(exc)
                active_request_logger.log_error(
                    f"[{error_type}] {str(exc)}\n[模型: {requested_model} | 代理: {active_proxy or 'disabled'}]",
                    "sync",
                )
                return make_exception_error_response(exc, status_code=500)
    finally:
        _cleanup_files(extracted_files)


async def completions(request: Request):
    services = get_runtime_services(request)
    active_gemini_conn = services.gemini_conn
    active_request_logger = services.request_logger
    active_state = services.state
    body = await request.json()
    prompt = body.get("prompt", "")
    stream = body.get("stream", False)
    requested_model = _resolve_requested_model(body, active_state.active_model)
    if not prompt:
        return make_openai_error_response(
            "No prompt provided",
            error_type="invalid_request_error",
            code="INVALID_PROMPT",
            status_code=400,
        )
    if not active_gemini_conn.client:
        return make_openai_error_response(
            "client is not ready",
            error_type="service_unavailable",
            code="CLIENT_NOT_READY",
            status_code=503,
        )
    if stream:
        async def gen():
            chunk_id = _gen_id()
            try:
                stream_gen = active_gemini_conn.stream_with_failover(prompt, model=requested_model)
                async for chunk in stream_gen:
                    if chunk.text_delta:
                        yield _make_sse_chunk({"id": chunk_id, "object": "text_completion", "choices": [{"text": chunk.text_delta, "index": 0, "finish_reason": None}]})
                yield _make_sse_chunk({"id": chunk_id, "object": "text_completion", "choices": [{"text": "", "index": 0, "finish_reason": "stop"}]})
                yield "data: [DONE]\n\n"
            except Exception as exc:
                active_request_logger.log_error(
                    f"[{_error_type_of(exc)}] {str(exc)}\n[模型: {requested_model}]",
                    "stream",
                )
                yield make_exception_error_chunk(exc)
                yield "data: [DONE]\n\n"
        return StreamingResponse(gen(), media_type="text/event-stream")
    try:
        response = await active_gemini_conn.generate_with_failover(prompt, model=requested_model, stream=False)
        return JSONResponse({"id": _gen_id(), "object": "text_completion", "created": int(time.time()), "model": requested_model, "choices": [{"text": response.text, "index": 0, "finish_reason": "stop"}]})
    except Exception as exc:
        active_request_logger.log_error(
            f"[{_error_type_of(exc)}] {str(exc)}\n[模型: {requested_model}]",
            "sync",
        )
        return make_exception_error_response(exc, status_code=500)


async def embeddings(request: Request):
    return make_embeddings_unsupported_response()
