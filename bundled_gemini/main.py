# main.py
# Gemini 本地代理服务器 — OpenAI API 兼容层（工具调用版）
# 支持 Claude Code / Cline / Continue / Cursor 等 AI 编程工具

import asyncio
import builtins
import sys
import json
import time
import uuid
import os 
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse, JSONResponse
import uvicorn


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

from .api_client import ContextMigrationNeeded, gemini_conn
from .config import ACCOUNTS, PROXIES, get_runtime_config, state
from .context_manager import context_manager
from .logger import request_logger
from .tool_adapter import build_tool_aware_prompt
from .tool_parser import StreamToolDecoder, parse_tool_calls
from gemini_webapi.exceptions import UsageLimitExceeded, AuthError, ModelInvalid
from .reverse_session import extract_reverse_session_from_messages
from .session_manager import session_manager
# ============================================================
# 配置常量
# ============================================================
MAX_TOOL_PARSE_RETRIES = 1   # 工具调用解析失败时的最大重试次数
STREAM_CHUNK_TIMEOUT = 180   # 流式模式中每个 chunk 的超时秒数（防卡死），从 60 修改为 180
NON_STREAM_TIMEOUT = 300     # 非流式模式的整体超时秒数，从 120 修改为 300


# ============================================================
# 应用生命周期
# ============================================================

@asynccontextmanager
async def lifespan(app: FastAPI):
    print("\n" + "=" * 60)
    print("Gemini ??????????????...")
    print(f"   ??: {state.active_model}")
    from .config import PROXIES, get_current_account_data
    print(f"   ???: {len(ACCOUNTS)} ??? ({state.active_account})")
    print(f"   ????: {PROXIES if PROXIES else 'disabled'}")

    auth_data = get_current_account_data() if state.active_account else {}
    psid = auth_data.get("SECURE_1PSID", auth_data.get("__Secure-1PSID", ""))
    has_target = bool(psid)
    print(
        f"   ????: {'????? Cookie' if has_target else '??????'} "
        f"(????: {len(auth_data.get('cookies_dict', {}))} ?)"
    )

    runtime_config = get_runtime_config()
    print(f"   ??: {runtime_config.get('port', 8000)}")
    request_logger.reconfigure(str(runtime_config.get("log_dir") or "logs"))
    session_manager.set_db_path(str(runtime_config.get("session_db_path") or "reverse_sessions.sqlite3"))
    await gemini_conn.initialize()
    print("=" * 60 + "\n")
    yield
    print("??????...")
    request_logger.close()
    await gemini_conn.close()


app = FastAPI(title="Gemini Local Proxy API", lifespan=lifespan)


# ============================================================
# 响应构造函数
# ============================================================

def _gen_id():
    return f"chatcmpl-{uuid.uuid4().hex[:12]}"


def _estimate_tokens(text: str) -> int:
    """粗略估算 token 数（按 4 字符 ≈ 1 token）"""
    return max(1, len(text) // 4)


def make_sync_response(text: str, prompt_text: str = ""):
    """纯文本非流式响应"""
    prompt_tokens = _estimate_tokens(prompt_text)
    completion_tokens = _estimate_tokens(text)
    return JSONResponse({
        "id": _gen_id(),
        "object": "chat.completion",
        "created": int(time.time()),
        "model": state.active_model,
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": text},
            "finish_reason": "stop"
        }],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens
        }
    })


def make_tool_call_response(result, prompt_text: str = ""):
    """包含 tool_calls 的非流式响应"""
    tool_calls_data = []
    for i, tc in enumerate(result.tool_calls):
        tool_calls_data.append({
            "id": tc.id,
            "type": "function",
            "function": {
                "name": tc.name,
                "arguments": tc.arguments
            }
        })

    message = {"role": "assistant", "content": result.text or None}
    if tool_calls_data:
        message["tool_calls"] = tool_calls_data

    raw_text = (result.text or "") + "".join(tc.arguments for tc in result.tool_calls)
    prompt_tokens = _estimate_tokens(prompt_text)
    completion_tokens = _estimate_tokens(raw_text)

    return JSONResponse({
        "id": _gen_id(),
        "object": "chat.completion",
        "created": int(time.time()),
        "model": state.active_model,
        "choices": [{
            "index": 0,
            "message": message,
            "finish_reason": "tool_calls" if tool_calls_data else "stop"
        }],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens
        }
    })


def _make_sse_chunk(data: dict) -> str:
    """构造 SSE data 行"""
    return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"


def _base_chunk(chunk_id: str) -> dict:
    """所有 SSE chunk 共享的基础结构"""
    return {
        "id": chunk_id,
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": state.active_model,
        "system_fingerprint": "fp_gemini",
    }


def make_sse_role_chunk(chunk_id: str) -> str:
    """流式首个 chunk — 必须包含 role:assistant（OpenAI 规范要求）"""
    data = _base_chunk(chunk_id)
    data["choices"] = [{
        "index": 0,
        "delta": {"role": "assistant", "content": ""},
        "finish_reason": None
    }]
    return _make_sse_chunk(data)


def make_sse_text_delta(text: str, chunk_id: str) -> str:
    """流式文本增量 chunk"""
    data = _base_chunk(chunk_id)
    data["choices"] = [{
        "index": 0,
        "delta": {"content": text},
        "finish_reason": None
    }]
    return _make_sse_chunk(data)


def make_sse_tool_call_delta(tool_call, index: int, chunk_id: str) -> str:
    """流式工具调用 chunk"""
    data = _base_chunk(chunk_id)
    data["choices"] = [{
        "index": 0,
        "delta": {
            "tool_calls": [{
                "index": index,
                "id": tool_call.id,
                "type": "function",
                "function": {
                    "name": tool_call.name,
                    "arguments": tool_call.arguments
                }
            }]
        },
        "finish_reason": None
    }]
    return _make_sse_chunk(data)


def make_sse_done(finish_reason: str, chunk_id: str) -> str:
    """流式结束 chunk"""
    data = _base_chunk(chunk_id)
    data["choices"] = [{
        "index": 0,
        "delta": {},
        "finish_reason": finish_reason
    }]
    return _make_sse_chunk(data)


# ============================================================
# 重试逻辑
# ============================================================

RETRY_HINT = (
    "\n\nIMPORTANT: You MUST use the <tool_call> XML format described in the system rules. "
    "Directly call the tool, do NOT just describe what you would do."
)


async def _generate_with_retry(prompt_text: str, tools: list,
                               extracted_files: list, has_tools: bool, chat_session=None):
    """
    带重试的非流式请求。支持物理窗口维持。
    """
    for attempt in range(MAX_TOOL_PARSE_RETRIES + 1):
        try:
            # 👇 核心修复：传入 chat=chat_session，确保非流式也具备物理上下文能力
            response = await asyncio.wait_for(
                gemini_conn.generate_with_failover(
                    prompt_text if attempt == 0 else prompt_text + RETRY_HINT,
                    model=state.active_model,
                    files=extracted_files if extracted_files else None,
                    stream=False,
                    chat=chat_session 
                ),
                timeout=NON_STREAM_TIMEOUT
            )
        except asyncio.TimeoutError:
            print(f"⚠️ 非流式请求超时 ({NON_STREAM_TIMEOUT}s)")
            request_logger.log_error(f"Non-stream timeout ({NON_STREAM_TIMEOUT}s)", "sync")
            return f"⚠️ [Gemini 响应超时 {NON_STREAM_TIMEOUT}s，请重试]", None
            
        raw_text = response.text
        request_logger.log_parse_result(raw_text, False, [], mode=f"batch_attempt_{attempt}")

        if not has_tools:
            return raw_text, None

        result = parse_tool_calls(raw_text)

        request_logger.log_parse_result(
            raw_text, result.has_calls,
            [c.name for c in result.tool_calls],
            mode=f"batch_attempt_{attempt}"
        )

        if result.has_calls:
            return raw_text, result

        # 没检测到调用 → 检查是否应该重试
        if attempt < MAX_TOOL_PARSE_RETRIES:
            tool_names = [t.get("function", t).get("name", "") for t in tools]
            if any(name in raw_text for name in tool_names if name):
                print(f"🔄 检测到工具名但格式不对，重试 (attempt {attempt + 1})...")
                continue

        return raw_text, result

    return raw_text, result



# ============================================================
# API 路由 — 核心端点
# ============================================================

@app.get("/v1/models")
async def list_models():
    """列出可用模型 (内置真实列表 + 兜底)"""
    try:
        from gemini_webapi.constants import Model
        models = [v.value[0] for v in Model if v.value[0] and v.value[0] != "unspecified"]
    except Exception:
        models = [state.active_model]
        
    if state.active_model not in models:
        models.append(state.active_model)
        
    return JSONResponse({
        "object": "list",
        "data": [{
            "id": m,
            "object": "model",
            "created": int(time.time()),
            "owned_by": "google/gemini_webapi"
        } for m in models]
    })

# ==========================================
# 函数位置: main.py
# 函数状态: 完整修改
# ==========================================

@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    """OpenAI Chat Completions 兼容端点 — 核心路由"""
    body = await request.json()
    messages = body.get("messages", [])
    tools = body.get("tools", [])
    tool_choice = body.get("tool_choice")
    stream = body.get("stream", False)

    if not messages:
        return JSONResponse({"error": "No messages provided"}, status_code=400)

    reverse_session_info, messages = extract_reverse_session_from_messages(messages)

    # ── 命令拦截（保持原有功能）──
    last_msg_content = ""
    last_msg = messages[-1]
    if isinstance(last_msg.get("content"), str):
        last_msg_content = last_msg["content"]
    elif isinstance(last_msg.get("content"), list):
        for p in last_msg["content"]:
            if p.get("type") == "text":
                last_msg_content += p.get("text", "")

    from .context_manager import context_manager
    is_cmd, cmd_reply = await context_manager.process_commands(last_msg_content)
    if is_cmd:
        if stream:
            async def cmd_generator():
                chunk_id = _gen_id()
                yield make_sse_role_chunk(chunk_id)
                yield make_sse_text_delta(cmd_reply, chunk_id)
                yield make_sse_done("stop", chunk_id)
                yield "data: [DONE]\n\n"
            return StreamingResponse(cmd_generator(), media_type="text/event-stream")
        return make_sync_response(cmd_reply)

    if not gemini_conn.client:
        return make_sync_response("⚠️ 客户端尚未就绪。请检查网络或账号配置。")

    # ============================================================================
    # 架构升级 1：会话路由 (Session Routing) 与上下文分叉
    # ============================================================================
    from .session_manager import session_manager
    import uuid
    import hashlib

    # ANSI 终端颜色定义
    COLOR_YELLOW = "\033[93m"
    COLOR_GREEN = "\033[92m"
    COLOR_RESET = "\033[0m"

    # 提取 OpenCode 多智能体识别头
    oc_agent_type = request.headers.get("X-OC-Agent-Type", "Main")
    oc_parent_id = request.headers.get("X-OC-Parent-Id") or reverse_session_info.get("parent_session_id", "")
    
    # 👇 核心修复：净化消息内容，剔除动态对象，确保同一任务的哈希绝对一致
    oc_session_id = reverse_session_info.get("session_id") or request.headers.get("X-OC-Session-Id")
    if not oc_session_id:
        stable_msgs = []
        for m in messages[:2]:
            content = m.get("content", "")
            if isinstance(content, list):
                content = "".join([part.get("text", "") for part in content if part.get("type") == "text"])
            stable_msgs.append({"r": m.get("role"), "c": content})
            
        fingerprint = json.dumps(stable_msgs, ensure_ascii=False, sort_keys=True)
        oc_session_id = "hash_" + hashlib.md5(fingerprint.encode('utf-8')).hexdigest()[:12]

    start_index = 0
    chat_session = None
    existing_session = session_manager.get_session(oc_session_id)
    start_index = int((existing_session or {}).get("last_msg_idx") or 0)
    chat_session, restored = session_manager.get_or_restore_chat_session(
        oc_session_id,
        gemini_conn.client,
        model=state.active_model,
        parent_session_id=oc_parent_id,
        agent_type=oc_agent_type,
    )
    current_cid = getattr(chat_session, "cid", "UNKNOWN") if chat_session else "UNKNOWN"
    action_label = "SubAgent Fork" if oc_agent_type == "Sub" else "MainAgent"
    if existing_session:
        print(
            f"{COLOR_GREEN}♻️ [{action_label}] 继续对话：沿用当前物理窗口 "
            f"(逻辑ID: {oc_session_id} | 物理CID: {current_cid} | restored={restored}){COLOR_RESET}"
        )
    else:
        print(
            f"{COLOR_YELLOW}🌱 [{action_label}] 生成新窗口：创建新的 Gemini 物理对话窗口 "
            f"(逻辑ID: {oc_session_id}){COLOR_RESET}"
        )

    if False and oc_agent_type == "Sub" and not session_manager.get_session(oc_session_id) and session_manager.has_parent_session(oc_parent_id):
        # 子智能体创立：在 Gemini 侧建立【全新物理窗口】以避免污染主对话
        chat_session = gemini_conn.client.start_chat() if gemini_conn.client else None
        session_manager.create_or_reset_session(oc_session_id, chat_session)
        print(f"{COLOR_YELLOW}🌱 [SubAgent Fork] 生成新窗口：已为子智能体开启专属隔离窗口 (逻辑ID: {oc_session_id}){COLOR_RESET}")
        
    elif False and session_manager.get_session(oc_session_id):
        # 持续对话：触发差分构建机制 (Delta Prompting)
        session_data = session_manager.get_session(oc_session_id)
        start_index = session_data["last_msg_idx"]
        chat_session = session_data["chat"]
        current_cid = getattr(chat_session, "cid", "UNKNOWN") if chat_session else "UNKNOWN"
        print(f"{COLOR_GREEN}♻️ [MainAgent] 继续对话：沿用当前物理窗口 (逻辑ID: {oc_session_id} | 物理CID: {current_cid}){COLOR_RESET}")
        
    elif False:
        # 全新主任务创立
        chat_session = gemini_conn.client.start_chat() if gemini_conn.client else None
        session_manager.create_or_reset_session(oc_session_id, chat_session)
        print(f"{COLOR_YELLOW}🌱 [MainAgent] 生成新窗口：创立全新核心物理对话窗口 (逻辑ID: {oc_session_id}){COLOR_RESET}")

    # ============================================================================
    # 架构升级 2：合并分块逻辑闭包化（方便发生迁移时复用）
    # ============================================================================
    def process_extracted_files(files_list):
        """强制限制附件数量最大为 9 个，采用“合并旧文件”的分块策略"""
        if not files_list or len(files_list) <= 9:
            return files_list
            
        print(f"⚠️ 附件数量达到 {len(files_list)} 个，执行超过9个文件的合并分块策略...")
        files_to_merge = files_list[:-8]
        retained_files = files_list[-8:]
        merged_content = ""
        merged_count = 0
        
        for f_path in files_to_merge:
            if isinstance(f_path, str) and os.path.exists(f_path):
                try:
                    with open(f_path, 'r', encoding='utf-8') as f: file_data = f.read()
                    filename = os.path.basename(f_path)
                    merged_content += f"\n=================================\n📂 FILE CHUNK: {filename}\n=================================\n\n{file_data}\n\n"
                    merged_count += 1
                    os.remove(f_path)
                except Exception:
                    try: os.remove(f_path)
                    except: pass
                    
        if merged_content:
            import tempfile
            fd, merged_path = tempfile.mkstemp(suffix=".txt", prefix="merged_history_chunks_")
            with os.fdopen(fd, 'w', encoding='utf-8') as f: f.write(merged_content)
            print(f"✅ 已将 {merged_count} 个早期历史代码文件合并为分块包，当前附件已安全控制在 9 个内。")
            return [merged_path] + retained_files
        return retained_files

    # ── 首次 Prompt 构建与文件处理 ──
    has_tools = bool(tools) and tool_choice != "none"
    if has_tools:
        # 差分模式下，只解析从 start_index 开始的新消息
        prompt_text, extracted_files = build_tool_aware_prompt(
            messages, tools, tool_choice=tool_choice, max_prompt_chars=40000, start_index=start_index
        )
    else:
        prompt_text, extracted_files = context_manager.build_stateless_prompt(messages)

    extracted_files = process_extracted_files(extracted_files)

    # ── 日志记录与物理 CID 提取 ──
    request_logger.log_request(messages, tools, prompt_text, has_tools=has_tools, model=state.active_model, account=state.active_account)
    
    current_cid = getattr(chat_session, "cid", "") if chat_session else ""
    display_cid = current_cid if current_cid else "等待分配(Pending)"
    
    print(f"📩 [{state.active_account}|{state.active_model}] {'🔧 工具调用' if has_tools else '💬 对话'} "
          f"[逻辑ID: {oc_session_id} | 物理CID: {display_cid}] "
          f"(差分基准: {start_index}, 字符: {len(prompt_text)}, 附件: {len(extracted_files)}, 流式: {stream})")

    # ============================================================================
    # 架构升级 3：流式响应嵌入“上下文迁移重试环” (Context Migration Loop)
    # ============================================================================
    if stream:
        async def event_generator():
            nonlocal prompt_text, extracted_files, start_index, chat_session
            from .api_client import ContextMigrationNeeded  # 引入自定义的迁移异常

            chunk_id = _gen_id()
            decoder = StreamToolDecoder() if has_tools else None
            tool_call_index = 0
            has_yielded_tool = False 
            yield make_sse_role_chunk(chunk_id)

            # 外层循环：专门用于捕获底层的【跨账号上下文迁移】事件
            while True:
                try:
                    # 传入 chat_session，维持物理窗口状态
                    stream_gen = gemini_conn.stream_with_failover(
                        prompt_text,
                        model=state.active_model,
                        files=extracted_files if extracted_files else None,
                        chat=chat_session 
                    )

                    aiter = stream_gen.__aiter__()
                    while True:
                        try:
                            chunk = await asyncio.wait_for(aiter.__anext__(), timeout=STREAM_CHUNK_TIMEOUT)
                        except StopAsyncIteration:
                            break
                        except asyncio.TimeoutError:
                            print(f"⚠️ 流式 chunk 超时 ({STREAM_CHUNK_TIMEOUT}s)，强制结束流")
                            request_logger.log_error(f"Stream chunk timeout ({STREAM_CHUNK_TIMEOUT}s)", "stream")
                            yield make_sse_text_delta(f"\n⚠️ [Gemini 响应超时 {STREAM_CHUNK_TIMEOUT}s，已强制结束]", chunk_id)
                            break

                        if not chunk.text_delta:
                            continue

                        if decoder:
                            events = decoder.push(chunk.text_delta)
                            for event in events:
                                if event.kind == "text_delta" and event.text:
                                    if not has_yielded_tool:
                                        yield make_sse_text_delta(event.text, chunk_id)
                                elif event.kind == "tool_call_finalized" and event.tool_call:
                                    has_yielded_tool = True
                                    request_logger.log_stream_event("tool_call_finalized", tool_name=event.tool_call.name)
                                    yield make_sse_tool_call_delta(event.tool_call, tool_call_index, chunk_id)
                                    tool_call_index += 1
                        else:
                            yield make_sse_text_delta(chunk.text_delta, chunk_id)

                    # 流结束刷新
                    if decoder:
                        final_events = decoder.flush()
                        for event in final_events:
                            if event.kind == "text_delta" and event.text:
                                if not has_yielded_tool:
                                    yield make_sse_text_delta(event.text, chunk_id)
                            elif event.kind == "tool_call_finalized" and event.tool_call:
                                has_yielded_tool = True
                                request_logger.log_stream_event("tool_call_finalized", tool_name=event.tool_call.name)
                                yield make_sse_tool_call_delta(event.tool_call, tool_call_index, chunk_id)
                                tool_call_index += 1

                    finish = "tool_calls" if (decoder and decoder.had_calls) else "stop"
                    yield make_sse_done(finish, chunk_id)

                    # ✅ 核心：请求完全成功，记录新的差分起点
                    session_manager.persist_live_session(
                        oc_session_id,
                        chat_session,
                        last_msg_idx=len(messages),
                        model=state.active_model,
                        parent_session_id=oc_parent_id,
                        agent_type=oc_agent_type,
                    )
                    
                    final_cid = getattr(chat_session, "cid", "UNKNOWN") if chat_session else "UNKNOWN"
                    print(f"✅ 请求完成，已成功绑定并更新物理会话 (物理CID: {final_cid})")
                    break # 完全成功，跳出外层重试环

                except ContextMigrationNeeded as e:
                    # ⚠️ 发生账号轮换！立刻开启【全量降级重构】！
                    print(f"{COLOR_YELLOW}🔄 捕获迁移信号: {e} | 生成新窗口：正在新账号重建全量物理上下文...{COLOR_RESET}")
                    
                    # 1. 彻底清理上一轮生成的废弃临时文件
                    if extracted_files:
                        for f_path in extracted_files:
                            if isinstance(f_path, str) and os.path.exists(f_path):
                                try: os.remove(f_path)
                                except: pass
                                
                    # 2. 重置物理会话，重新在新账号申请专属 cid
                    chat_session = gemini_conn.client.start_chat(model=state.active_model) if gemini_conn.client else None
                    session_manager.create_or_reset_session(
                        oc_session_id,
                        chat_session,
                        parent_session_id=oc_parent_id,
                        model=state.active_model,
                        agent_type=oc_agent_type,
                    )
                    
                    # 3. 退化为全量读取模式 (start_index = 0)
                    prompt_text, new_ext_files = build_tool_aware_prompt(
                        messages, tools, tool_choice=tool_choice, max_prompt_chars=40000, start_index=0
                    )
                    
                    # 4. 再次执行附件合并策略
                    extracted_files = process_extracted_files(new_ext_files)
                    
                    print(f"{COLOR_YELLOW}✅ 全量上下文重新编译完毕，准备向新账号提交无缝请求。{COLOR_RESET}")
                    continue # 携带全新的 payload 进入下一次 while 循环，实现断点续传

                except UsageLimitExceeded:
                    request_logger.log_error("额度耗尽（流式）", "stream")
                    yield make_sse_text_delta("\n🛑 所有账号额度已彻底耗尽！请更新账号或等待重置。", chunk_id)
                    yield make_sse_done("stop", chunk_id)
                    break

                except Exception as e:
                    from .exceptions import ProxyException
                    err_str = str(e) or repr(e)
                    error_type = e.error_type if isinstance(e, ProxyException) else "UNKNOWN_ERROR"
                    print(f"❌ 流式报错 [{error_type}]: {err_str}")
                    request_logger.log_error(
                        f"[{error_type}] {err_str}\n[模型: {state.active_model} | 代理: {PROXIES or 'disabled'}]",
                        "stream",
                    )
                    yield make_sse_text_delta(f"\n⚠️ [Gemini Proxy Error | {error_type}]: {err_str}", chunk_id)
                    yield make_sse_done("stop", chunk_id)
                    break
                    request_logger.log_error(err_str, "stream")
                    print(f"❌ 流式报错: {err_str}")
                    yield make_sse_text_delta(f"\n⚠️ [Gemini Proxy Error]: {err_str}", chunk_id)
                    yield make_sse_done("stop", chunk_id)
                    break

            # 无论最终请求成功、断开或报错，退出 generator 前务必清理物理文件
            if extracted_files:
                for f_path in extracted_files:
                    if isinstance(f_path, str) and os.path.exists(f_path):
                        try: os.remove(f_path)
                        except: pass

            # 强制发送终止信号
            yield "data: [DONE]\n\n"

        sse_headers = {
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        }
        return StreamingResponse(event_generator(), media_type="text/event-stream", headers=sse_headers)

    # ── 非流式响应 (同步模式) - 闭环重构 ──
    else:
        # 使用 try-finally 确保不管因为 return 还是报错退出，都会安全清理文件附件
        try:
            # 外层循环：拦截同步请求中的账号轮换迁移信号
            while True:
                try:
                    # 透传 chat_session 到底层以维持物理上下文
                    raw_text, result = await _generate_with_retry(
                        prompt_text, tools, extracted_files, has_tools, chat_session=chat_session
                    )

                    # ✅ 请求成功，更新物理基准点
                    session_manager.persist_live_session(
                        oc_session_id,
                        chat_session,
                        last_msg_idx=len(messages),
                        model=state.active_model,
                        parent_session_id=oc_parent_id,
                        agent_type=oc_agent_type,
                    )
                    
                    final_cid = getattr(chat_session, "cid", "UNKNOWN") if chat_session else "UNKNOWN"
                    print(f"✅ [Sync] 非流式请求完成，已成功绑定并更新物理会话 (物理CID: {final_cid})")

                    if has_tools and result and result.has_calls:
                        print(f"🔧 解析到 {len(result.tool_calls)} 个工具调用: "
                              f"{[c.name for c in result.tool_calls]}")
                        return make_tool_call_response(result, prompt_text)

                    if has_tools:
                        print("💬 模型未调用工具，返回文本。")

                    return make_sync_response(raw_text, prompt_text)

                except ContextMigrationNeeded as e:
                    # ⚠️ 账号轮换，全量重构同步上下文
                    print(f"{COLOR_YELLOW}🔄 [Sync] 捕获迁移信号: {e} | 生成新窗口：正在新账号重建全量物理上下文...{COLOR_RESET}")
                    
                    # 清理作废的旧文件
                    if extracted_files:
                        for f_path in extracted_files:
                            if isinstance(f_path, str) and os.path.exists(f_path):
                                try: os.remove(f_path)
                                except: pass
                                
                    chat_session = gemini_conn.client.start_chat(model=state.active_model) if gemini_conn.client else None
                    session_manager.create_or_reset_session(
                        oc_session_id,
                        chat_session,
                        parent_session_id=oc_parent_id,
                        model=state.active_model,
                        agent_type=oc_agent_type,
                    )
                    
                    prompt_text, new_ext_files = build_tool_aware_prompt(
                        messages, tools, tool_choice=tool_choice, max_prompt_chars=40000, start_index=0
                    )
                    extracted_files = process_extracted_files(new_ext_files)
                    print(f"{COLOR_YELLOW}✅ [Sync] 全量上下文重新编译完毕，准备向新账号提交无缝请求。{COLOR_RESET}")
                    continue # 发起重新请求

                except UsageLimitExceeded:
                    request_logger.log_error("所有账号额度耗尽", "sync")
                    return make_sync_response("🛑 所有账号额度已耗尽！请更新账号或等待重置。")
                except Exception as e:
                    from .exceptions import ProxyException
                    error_type = e.error_type if isinstance(e, ProxyException) else "UNKNOWN_ERROR"
                    request_logger.log_error(
                        f"[{error_type}] {str(e)}\n[模型: {state.active_model} | 代理: {PROXIES or 'disabled'}]",
                        "sync",
                    )
                    print(f"❌ 非流式报错 [{error_type}]: {e}")
                    return JSONResponse({"error": str(e), "error_type": error_type}, status_code=500)
        finally:
            # 无论外层抛出错误还是成功返回（return），都安全清理最终的物理附件资源
            if extracted_files:
                for f_path in extracted_files:
                    if isinstance(f_path, str) and os.path.exists(f_path):
                        try: os.remove(f_path)
                        except: pass
# ============================================================
# API 路由 — 兼容端点
# ============================================================

@app.post("/v1/completions")
async def completions(request: Request):
    """Completions API 兼容端点（旧版工具可能使用）"""
    body = await request.json()
    prompt = body.get("prompt", "")
    stream = body.get("stream", False)

    if not prompt:
        return JSONResponse({"error": "No prompt provided"}, status_code=400)

    if not gemini_conn.client:
        return JSONResponse({"error": "Client not ready"}, status_code=503)

    if stream:
        async def gen():
            chunk_id = _gen_id()
            try:
                stream_gen = await gemini_conn.generate_with_failover(
                    prompt, model=state.active_model, stream=True
                )
                async for chunk in stream_gen:
                    if chunk.text_delta:
                        yield _make_sse_chunk({
                            "id": chunk_id,
                            "object": "text_completion",
                            "choices": [{"text": chunk.text_delta, "index": 0, "finish_reason": None}]
                        })
                yield _make_sse_chunk({
                    "id": chunk_id,
                    "object": "text_completion",
                    "choices": [{"text": "", "index": 0, "finish_reason": "stop"}]
                })
                yield "data: [DONE]\n\n"
            except Exception as e:
                yield _make_sse_chunk({"error": str(e)})
                yield "data: [DONE]\n\n"
        return StreamingResponse(gen(), media_type="text/event-stream")
    else:
        try:
            response = await gemini_conn.generate_with_failover(
                prompt, model=state.active_model, stream=False
            )
            return JSONResponse({
                "id": _gen_id(),
                "object": "text_completion",
                "created": int(time.time()),
                "model": state.active_model,
                "choices": [{"text": response.text, "index": 0, "finish_reason": "stop"}]
            })
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)


@app.post("/v1/embeddings")
async def embeddings(request: Request):
    """Embeddings API 桩端点（Continue 等工具可能调用）"""
    body = await request.json()
    inputs = body.get("input", [])
    if isinstance(inputs, str):
        inputs = [inputs]

    data = [
        {
            "object": "embedding",
            "index": i,
            "embedding": [0.0] * 1536  # 零向量占位
        }
        for i in range(len(inputs))
    ]
    return JSONResponse({
        "object": "list",
        "data": data,
        "model": state.active_model,
        "usage": {"prompt_tokens": sum(len(s)//4 for s in inputs), "total_tokens": sum(len(s)//4 for s in inputs)}
    })


# =====================================================================
# API 路由 — 内部运维与调试专用端点 (Admin & Debug)
# 注意：以下所有 /v1/debug/* 路由均不是给最终外部大模型客户端消费的，
# 它们专用于 Helper 推送、面板数据获取和脱机排障诊断。
# =====================================================================

@app.get("/v1/debug/last")
async def debug_last():
    """诊断用途：返回系统内存拦截到的最近一次 OpenAI 格式原始入参及输出快照"""
    last = request_logger.get_last_request()
    if not last:
        return JSONResponse({"message": "No requests recorded yet"})
    return JSONResponse(last)


@app.get("/v1/debug/logs")
async def debug_logs():
    """诊断用途：导出内存环形队列中的最近审计日志线"""
    logs = request_logger.get_recent_logs(30)
    return JSONResponse({"count": len(logs), "logs": logs})


@app.get("/v1/debug/status")
async def debug_status():
    """系统运维监控：探活当前 Uvicorn 进程池和连接活跃度"""
    from .config import PROXIES, get_current_account_data
    auth_data = getattr(get_current_account_data, '__call__', lambda: ACCOUNTS.get(state.active_account, {}))() if state.active_account else {}
    return JSONResponse({
        "status": "running",
        "active_model": state.active_model,
        "proxy": PROXIES or "disabled",
        "has_active_ticket": bool(auth_data),
        "active_account": state.active_account,
        "has_psid": bool(auth_data.get('SECURE_1PSID') or auth_data.get('__Secure-1PSID')),
        "has_psidts": bool(auth_data.get('SECURE_1PSIDTS') or auth_data.get('__Secure-1PSIDTS')),
        "cookies_dict_count": len(auth_data.get('cookies_dict', {})),
        "client_ready": gemini_conn.client is not None,
        "accounts_total": len(ACCOUNTS),
        "last_refresh_result": getattr(gemini_conn, "last_refresh_result", None),
        "last_request_error": getattr(gemini_conn, "last_request_error", None),
        "last_request_error_type": getattr(gemini_conn, "last_request_error_type", None)
    })

@app.get("/v1/debug/network")
async def debug_network():
    """代理与出口环回侦测，供排障时分析代理是否真实穿透"""
    from .config import PROXIES
    is_client_set = False
    if gemini_conn.client and getattr(gemini_conn.client, "proxy", None):
        is_client_set = True
        
    return JSONResponse({
        "runtime_proxy_value": PROXIES or "",
        "is_proxy_configured": bool(PROXIES),
        "is_client_initialized_with_proxy": is_client_set,
        "client_proxy_value": getattr(gemini_conn.client, "proxy", None),
        "active_model": state.active_model,
        "active_account": state.active_account,
        "diagnostic_msg": "代理设定已于当前生命周期内生效" if is_client_set else "代理值为空或客户端未重置加载"
    })




# ---------------------------------------------------------------------
# 认证控制面子路由
# ---------------------------------------------------------------------

@app.post("/v1/debug/auth/push_ticket")
async def push_ticket(request: Request):
    """
    接收来自助手端 (Helper) 上报的一手长期饭票。
    执行全套签名防伪检验、重放查杀与持久化存储操作。
    """
    from .config import AUTH_MANAGER, RUNTIME_CONFIG, reload_runtime_config
    if not AUTH_MANAGER:
        return JSONResponse({"error": "Auth Manager not enabled"}, status_code=503)
    
    body = await request.json()
    secret = RUNTIME_CONFIG.get("relay_shared_secret", "change_me_to_a_random_string")
    
    from reverse_runtime.ticket_receiver import handle_push_ticket
    success, msg = handle_push_ticket(AUTH_MANAGER, body, secret)
    if success:
        if RUNTIME_CONFIG.get("relay_accept_push_without_restart", True):
            reload_runtime_config()
        return JSONResponse({"status": "success", "message": msg})
    else:
        return JSONResponse({"status": "error", "message": msg}, status_code=401)

@app.get("/v1/debug/auth/status")
async def auth_status(request: Request):
    """
    统一暴露当前认证机全景快照。被主控插件或 /gemini_reverse doctor 外部探针消费。
    """
    from .config import AUTH_MANAGER
    if not AUTH_MANAGER:
        return JSONResponse({"error": "Auth Manager not enabled"}, status_code=503)
    from reverse_runtime.auth_status import get_auth_status_payload
    return JSONResponse(get_auth_status_payload(AUTH_MANAGER))


# =====================================================================
# 守护进程主入口点
# 确保采用 Uvicorn 非热重载模式冷暴力托管，不产生副作用。
# =====================================================================

if __name__ == "__main__":
    if sys.platform == 'win32':
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    runtime_config = get_runtime_config()
    uvicorn.run(
        app,
        host=str(runtime_config.get("host") or "127.0.0.1"),
        port=int(runtime_config.get("port") or 8000),
        reload=False,
    )
