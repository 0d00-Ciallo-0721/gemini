from __future__ import annotations

import asyncio
import builtins
import hashlib
import ipaddress
import json
import os
import sys
import time
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
import uvicorn

from gemini_webapi.exceptions import UsageLimitExceeded

from .api_client import ContextMigrationNeeded, gemini_conn
from .config import ACCOUNTS, AUTH_MANAGER, PROXIES, get_current_account_data, get_runtime_config, reload_runtime_config, state
from .context_manager import context_manager
from .exceptions import ProxyException, SessionDbPermissionError
from .logger import request_logger
from .reverse_session import extract_reverse_session_from_messages
from .session_manager import session_manager
from .tool_adapter import build_tool_aware_prompt
from .tool_parser import StreamToolDecoder, parse_tool_calls
from runtime.auth_status import get_auth_status_payload
from runtime.healthcheck import run_doctor
from runtime.ticket_receiver import handle_push_ticket

_ORIGINAL_PRINT = builtins.print
MAX_TOOL_PARSE_RETRIES = 1
STREAM_CHUNK_TIMEOUT = 180
NON_STREAM_TIMEOUT = 300


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
    runtime_config = get_runtime_config()
    request_logger.reconfigure(str(runtime_config.get("log_dir") or "logs"))
    session_manager.set_db_path(str(runtime_config.get("session_db_path") or "reverse_sessions.sqlite3"))
    session_manager.assert_writable()
    print("\n" + "=" * 60)
    print("Gemini Reverse Standalone 服务启动中…")
    print(f"  模型: {state.active_model}")
    print(f"  账号池: {len(ACCOUNTS)}")
    print(f"  端口: {runtime_config.get('port', 8000)}")
    print(f"  代理: {PROXIES or 'disabled'}")
    await gemini_conn.initialize()
    print("=" * 60 + "\n")
    try:
        yield
    finally:
        request_logger.close()
        await gemini_conn.close()


app = FastAPI(title="Gemini Reverse Standalone", lifespan=lifespan)


def _error_type_of(exc: Exception) -> str:
    return str(getattr(exc, "error_type", "UNKNOWN_ERROR"))


def _local_error_response(exc: Exception, *, status_code: int = 500):
    return JSONResponse({"error": str(exc), "error_type": _error_type_of(exc)}, status_code=status_code)


def _get_request_client_ip(request: Request) -> str:
    forwarded_for = request.headers.get("x-forwarded-for", "")
    if forwarded_for:
        return forwarded_for.split(",")[0].strip()
    return (request.client.host if request.client else "") or ""


def _is_ip_allowed(client_ip: str, runtime_config: dict) -> bool:
    if not runtime_config.get("allowlist_enabled", True):
        return True
    if not client_ip:
        return False
    try:
        ip_obj = ipaddress.ip_address(client_ip)
    except ValueError:
        return False
    for raw in runtime_config.get("allowed_client_ips", []) or []:
        candidate = str(raw or "").strip()
        if not candidate:
            continue
        try:
            if "/" in candidate and ip_obj in ipaddress.ip_network(candidate, strict=False):
                return True
            if "/" not in candidate and ip_obj == ipaddress.ip_address(candidate):
                return True
        except ValueError:
            continue
    return False


def _require_admin_token(request: Request) -> bool:
    expected = str(get_runtime_config().get("admin_token") or "").strip()
    if not expected:
        return True
    supplied = (request.headers.get("x-admin-token") or request.headers.get("x-api-key") or "").strip()
    if not supplied:
        auth_header = request.headers.get("authorization", "")
        if auth_header.lower().startswith("bearer "):
            supplied = auth_header[7:].strip()
    return supplied == expected


def _extract_bearer_or_key(request: Request) -> str:
    supplied = (request.headers.get("x-api-key") or "").strip()
    if supplied:
        return supplied
    auth_header = request.headers.get("authorization", "")
    if auth_header.lower().startswith("bearer "):
        return auth_header[7:].strip()
    return ""


def _has_valid_service_key(request: Request, runtime_config: dict) -> bool:
    configured = runtime_config.get("api_keys", []) or []
    accepted = {str(item).strip() for item in configured if str(item).strip()}
    if not accepted:
        return False
    supplied = _extract_bearer_or_key(request)
    return bool(supplied and supplied in accepted)


@app.middleware("http")
async def allowlist_middleware(request: Request, call_next):
    path = request.url.path
    runtime_config = get_runtime_config()
    client_ip = _get_request_client_ip(request)
    is_loopback = client_ip in {"127.0.0.1", "::1", "localhost"}
    if path in {"/healthz", "/readyz"}:
        return await call_next(request)
    if path.startswith("/v1/debug/") and not is_loopback and not _require_admin_token(request):
        return JSONResponse({"error": "admin token required"}, status_code=401)
    if _is_ip_allowed(client_ip, runtime_config):
        return await call_next(request)
    if _has_valid_service_key(request, runtime_config):
        return await call_next(request)
    if runtime_config.get("api_keys"):
        return JSONResponse({"error": "client ip is not allowlisted and api key is invalid"}, status_code=401)
    return JSONResponse({"error": "client ip is not allowlisted"}, status_code=403)


@app.get("/healthz")
async def healthz():
    return JSONResponse({"status": "ok"})


@app.get("/readyz")
async def readyz():
    return JSONResponse({
        "status": "ready" if gemini_conn.client else "starting",
        "client_ready": gemini_conn.client is not None,
        "active_model": state.active_model,
        "active_account": state.active_account,
    })


def _resolve_requested_model(body: dict) -> str:
    return str((body or {}).get("model") or "").strip() or state.active_model


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


def _process_extracted_files(files_list):
    if not files_list or len(files_list) <= 9:
        return files_list
    print(f"附件数量达到 {len(files_list)}，执行合并分块策略…")
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
        import tempfile
        fd, merged_path = tempfile.mkstemp(suffix=".txt", prefix="merged_history_chunks_")
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(merged_content)
        print(f"已将 {merged_count} 个历史文件合并为一个分块包。")
        return [merged_path] + retained_files
    return retained_files


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


def make_sync_response(text: str, prompt_text: str = "", model: str | None = None):
    used_model = model or state.active_model
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


def make_tool_call_response(result, prompt_text: str = "", model: str | None = None):
    used_model = model or state.active_model
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
            text = text[len(prefix):].lstrip(" :：-")
    return text or "Unknown error"


async def _generate_with_retry(prompt_text: str, tools: list, extracted_files: list, has_tools: bool, requested_model: str, chat_session=None):
    for attempt in range(MAX_TOOL_PARSE_RETRIES + 1):
        try:
            response = await asyncio.wait_for(
                gemini_conn.generate_with_failover(
                    prompt_text,
                    model=requested_model,
                    files=extracted_files if extracted_files else None,
                    stream=False,
                    chat=chat_session,
                ),
                timeout=NON_STREAM_TIMEOUT,
            )
        except asyncio.TimeoutError:
            request_logger.log_error(f"Non-stream timeout ({NON_STREAM_TIMEOUT}s)", "sync")
            raise TimeoutError(f"Gemini 响应超时 ({NON_STREAM_TIMEOUT}s)")
        raw_text = response.text
        request_logger.log_parse_result(raw_text, False, [], mode=f"batch_attempt_{attempt}")
        if not has_tools:
            return raw_text, None
        result = parse_tool_calls(raw_text)
        request_logger.log_parse_result(raw_text, result.has_calls, [call.name for call in result.tool_calls], mode=f"batch_attempt_{attempt}")
        if result.has_calls:
            return raw_text, result
        if attempt < MAX_TOOL_PARSE_RETRIES:
            tool_names = [tool.get("function", tool).get("name", "") for tool in tools]
            if any(name in raw_text for name in tool_names if name):
                continue
        return raw_text, result
    return raw_text, result


@app.get("/v1/models")
async def list_models():
    try:
        from gemini_webapi.constants import Model
        models = [value.value[0] for value in Model if value.value[0] and value.value[0] != "unspecified"]
    except Exception:
        models = [state.active_model]
    if state.active_model not in models:
        models.append(state.active_model)
    return JSONResponse({"object": "list", "data": [{"id": model_id, "object": "model", "created": int(time.time()), "owned_by": "google/gemini_webapi"} for model_id in models]})


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    body = await request.json()
    messages = body.get("messages", [])
    tools = body.get("tools", [])
    tool_choice = body.get("tool_choice")
    stream = body.get("stream", False)
    requested_model = _resolve_requested_model(body)
    if not messages:
        return JSONResponse({"error": "No messages provided"}, status_code=400)

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

    if not gemini_conn.client:
        return make_sync_response("客户端尚未就绪，请检查网络或账号配置。", model=requested_model)

    agent_type = request.headers.get("X-Agent-Type") or request.headers.get("X-OC-Agent-Type", "Main")
    parent_session_id = request.headers.get("X-Parent-Session-Id") or request.headers.get("X-OC-Parent-Id") or reverse_session_info.get("parent_session_id", "")
    session_id = _resolve_session_id(messages, reverse_session_info, request)
    existing_session = session_manager.get_session(session_id)
    start_index = int((existing_session or {}).get("last_msg_idx") or 0)
    try:
        chat_session, restored = session_manager.get_or_restore_chat_session(
            session_id,
            gemini_conn.client,
            model=requested_model,
            parent_session_id=parent_session_id,
            agent_type=agent_type,
        )
    except SessionDbPermissionError as exc:
        request_logger.log_error(
            f"[{exc.error_type}] {str(exc)}\n[db: {session_manager.db_path}]",
            "session",
        )
        return _local_error_response(exc, status_code=503)
    print(f"Session {session_id}: model={requested_model} restored={restored}")

    has_tools = bool(tools) and tool_choice != "none"
    if has_tools:
        prompt_text, extracted_files = build_tool_aware_prompt(messages, tools, tool_choice=tool_choice, max_prompt_chars=40000, start_index=start_index)
    else:
        prompt_text, extracted_files = context_manager.build_stateless_prompt(messages)
    extracted_files = _process_extracted_files(extracted_files)
    request_logger.log_request(messages, tools, prompt_text, has_tools=has_tools, model=requested_model, account=state.active_account)

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
                        stream_gen = gemini_conn.stream_with_failover(prompt_text, model=requested_model, files=extracted_files if extracted_files else None, chat=chat_session)
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
                                        request_logger.log_stream_event("tool_call_finalized", tool_name=event.tool_call.name)
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
                                    request_logger.log_stream_event("tool_call_finalized", tool_name=event.tool_call.name)
                                    yield make_sse_tool_call_delta(event.tool_call, tool_call_index, chunk_id, requested_model)
                                    tool_call_index += 1
                        if total_text_chars == 0 and not has_yielded_tool:
                            print(f"❌ [Stream] 警告：上游流已结束，但全程未收到任何正文或工具调用！(chunks={chunks_count})")
                        else:
                            print(f"✨ [Stream] 流读取完成 (chunks={chunks_count}, chars={total_text_chars})")
                        finish_reason = "tool_calls" if (decoder and decoder.had_calls) else "stop"
                        yield make_sse_done(finish_reason, chunk_id, requested_model)
                        session_manager.persist_live_session(session_id, chat_session, last_msg_idx=len(messages), model=requested_model, parent_session_id=parent_session_id, agent_type=agent_type)
                        break
                    except ContextMigrationNeeded as exc:
                        print(f"检测到上下文迁移: {exc}，正在用模型 {requested_model} 重建会话…")
                        if extracted_files:
                            for file_path in extracted_files:
                                if isinstance(file_path, str) and os.path.exists(file_path):
                                    try:
                                        os.remove(file_path)
                                    except Exception:
                                        pass
                        chat_session = gemini_conn.client.start_chat(model=requested_model) if gemini_conn.client else None
                        session_manager.create_or_reset_session(session_id, chat_session, parent_session_id=parent_session_id, model=requested_model, agent_type=agent_type)
                        prompt_text, new_ext_files = build_tool_aware_prompt(messages, tools, tool_choice=tool_choice, max_prompt_chars=40000, start_index=0)
                        extracted_files = _process_extracted_files(new_ext_files)
                        continue
            except Exception as exc:
                err_str = _normalize_proxy_error_text(str(exc) or repr(exc))
                error_type = _error_type_of(exc)
                request_logger.log_error(f"[{error_type}] {err_str}\n[模型: {requested_model} | 代理: {PROXIES or 'disabled'}]", "stream")
                yield make_sse_text_delta(f"\n[Gemini Proxy Error | {error_type}]: {err_str}", chunk_id, requested_model)
                yield make_sse_done("stop", chunk_id, requested_model)
            finally:
                if extracted_files:
                    for file_path in extracted_files:
                        if isinstance(file_path, str) and os.path.exists(file_path):
                            try:
                                os.remove(file_path)
                            except Exception:
                                pass
                yield "data: [DONE]\n\n"
        return StreamingResponse(event_generator(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"})

    try:
        while True:
            try:
                raw_text, result = await _generate_with_retry(prompt_text, tools, extracted_files, has_tools, requested_model, chat_session=chat_session)
                session_manager.persist_live_session(session_id, chat_session, last_msg_idx=len(messages), model=requested_model, parent_session_id=parent_session_id, agent_type=agent_type)
                if has_tools and result and result.has_calls:
                    return make_tool_call_response(result, prompt_text, model=requested_model)
                return make_sync_response(raw_text, prompt_text, model=requested_model)
            except ContextMigrationNeeded as exc:
                print(f"[Sync] 检测到上下文迁移: {exc}，正在重建会话…")
                if extracted_files:
                    for file_path in extracted_files:
                        if isinstance(file_path, str) and os.path.exists(file_path):
                            try:
                                os.remove(file_path)
                            except Exception:
                                pass
                chat_session = gemini_conn.client.start_chat(model=requested_model) if gemini_conn.client else None
                session_manager.create_or_reset_session(session_id, chat_session, parent_session_id=parent_session_id, model=requested_model, agent_type=agent_type)
                prompt_text, new_ext_files = build_tool_aware_prompt(messages, tools, tool_choice=tool_choice, max_prompt_chars=40000, start_index=0)
                extracted_files = _process_extracted_files(new_ext_files)
                continue
            except UsageLimitExceeded:
                request_logger.log_error("所有账号额度耗尽", "sync")
                return make_sync_response("所有账号额度已耗尽，请稍后再试。", model=requested_model)
            except SessionDbPermissionError as exc:
                request_logger.log_error(
                    f"[{exc.error_type}] {str(exc)}\n[db: {session_manager.db_path}]",
                    "sync",
                )
                return _local_error_response(exc, status_code=503)
            except Exception as exc:
                error_type = _error_type_of(exc)
                request_logger.log_error(f"[{error_type}] {str(exc)}\n[模型: {requested_model} | 代理: {PROXIES or 'disabled'}]", "sync")
                return _local_error_response(exc, status_code=500)
    finally:
        if extracted_files:
            for file_path in extracted_files:
                if isinstance(file_path, str) and os.path.exists(file_path):
                    try:
                        os.remove(file_path)
                    except Exception:
                        pass


@app.post("/v1/completions")
async def completions(request: Request):
    body = await request.json()
    prompt = body.get("prompt", "")
    stream = body.get("stream", False)
    requested_model = _resolve_requested_model(body)
    if not prompt:
        return JSONResponse({"error": "No prompt provided"}, status_code=400)
    if not gemini_conn.client:
        return JSONResponse({"error": "Client not ready"}, status_code=503)
    if stream:
        async def gen():
            chunk_id = _gen_id()
            try:
                stream_gen = gemini_conn.stream_with_failover(prompt, model=requested_model)
                async for chunk in stream_gen:
                    if chunk.text_delta:
                        yield _make_sse_chunk({"id": chunk_id, "object": "text_completion", "choices": [{"text": chunk.text_delta, "index": 0, "finish_reason": None}]})
                yield _make_sse_chunk({"id": chunk_id, "object": "text_completion", "choices": [{"text": "", "index": 0, "finish_reason": "stop"}]})
                yield "data: [DONE]\n\n"
            except Exception as exc:
                yield _make_sse_chunk({"error": str(exc)})
                yield "data: [DONE]\n\n"
        return StreamingResponse(gen(), media_type="text/event-stream")
    try:
        response = await gemini_conn.generate_with_failover(prompt, model=requested_model, stream=False)
        return JSONResponse({"id": _gen_id(), "object": "text_completion", "created": int(time.time()), "model": requested_model, "choices": [{"text": response.text, "index": 0, "finish_reason": "stop"}]})
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


@app.post("/v1/embeddings")
async def embeddings(request: Request):
    body = await request.json()
    inputs = body.get("input", [])
    if isinstance(inputs, str):
        inputs = [inputs]
    data = [{"object": "embedding", "index": index, "embedding": [0.0] * 1536} for index in range(len(inputs))]
    total = sum(len(item) // 4 for item in inputs)
    return JSONResponse({"object": "list", "data": data, "model": state.active_model, "usage": {"prompt_tokens": total, "total_tokens": total}})


@app.get("/v1/debug/last")
async def debug_last():
    last = request_logger.get_last_request()
    if not last:
        return JSONResponse({"message": "No requests recorded yet"})
    return JSONResponse(last)


@app.get("/v1/debug/logs")
async def debug_logs():
    logs = request_logger.get_recent_logs(30)
    return JSONResponse({"count": len(logs), "logs": logs})


@app.get("/v1/debug/status")
async def debug_status():
    auth_data = get_current_account_data() if state.active_account else {}
    return JSONResponse({
        "status": "running",
        "active_model": state.active_model,
        "proxy": PROXIES or "disabled",
        "active_account": state.active_account,
        "accounts_total": len(ACCOUNTS),
        "client_ready": gemini_conn.client is not None,
        "has_psid": bool(auth_data.get("SECURE_1PSID") or auth_data.get("__Secure-1PSID")),
        "has_psidts": bool(auth_data.get("SECURE_1PSIDTS") or auth_data.get("__Secure-1PSIDTS")),
        "cookies_dict_count": len(auth_data.get("cookies_dict", {})),
        "last_refresh_result": getattr(gemini_conn, "last_refresh_result", None),
        "last_request_error": getattr(gemini_conn, "last_request_error", None),
        "last_request_error_type": getattr(gemini_conn, "last_request_error_type", None),
    })


@app.get("/v1/debug/network")
async def debug_network():
    is_client_set = bool(gemini_conn.client and getattr(gemini_conn.client, "proxy", None))
    return JSONResponse({"runtime_proxy_value": PROXIES or "", "is_proxy_configured": bool(PROXIES), "is_client_initialized_with_proxy": is_client_set, "client_proxy_value": getattr(gemini_conn.client, "proxy", None), "active_model": state.active_model, "active_account": state.active_account})


@app.get("/v1/debug/doctor")
async def debug_doctor():
    return JSONResponse(await run_doctor(get_runtime_config(), AUTH_MANAGER))


@app.post("/v1/debug/auth/push_ticket")
async def push_ticket(request: Request):
    if not AUTH_MANAGER:
        return JSONResponse({"error": "Auth Manager not enabled"}, status_code=503)
    body = await request.json()
    runtime_config = get_runtime_config()
    secret = runtime_config.get("relay_shared_secret", "change_me_to_a_random_string")
    success, msg = handle_push_ticket(AUTH_MANAGER, body, secret)
    if success:
        if runtime_config.get("relay_accept_push_without_restart", True):
            reload_runtime_config()
        return JSONResponse({"status": "success", "message": msg})
    return JSONResponse({"status": "error", "message": msg}, status_code=401)


@app.get("/v1/debug/auth/status")
async def auth_status():
    if not AUTH_MANAGER:
        return JSONResponse({"error": "Auth Manager not enabled"}, status_code=503)
    return JSONResponse(get_auth_status_payload(AUTH_MANAGER))


if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    runtime_config = get_runtime_config()
    uvicorn.run(app, host=str(runtime_config.get("host") or "127.0.0.1"), port=int(runtime_config.get("port") or 8000), reload=False)
