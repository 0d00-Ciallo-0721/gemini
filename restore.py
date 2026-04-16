import os

path = r'c:\Users\zlj\Desktop\llm\gemini\bundled_gemini\main.py'
with open(path, 'r', encoding='utf-8') as f:
    content = f.read()

# 找到断点 1：process_extracted_files 结束处
marker_start = "        return retained_files\n"
idx_start = content.find(marker_start) + len(marker_start)

# 找到断点 2：event_generator 尾部的变量清理处
marker_end = "            # 无论最终请求成功、断开或报错"
idx_end = content.find(marker_end)

if idx_start < len(marker_start) or idx_end == -1:
    print(f"Markers not found. start={idx_start}, end={idx_end}")
    import sys
    sys.exit(1)

# 准备恢复的代码（包含增强的流式日志）
restoration = \"\"\"
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
    
    print(f"?? [{state.active_account}|{state.active_model}] {'?? 工具调用' if has_tools else '?? 对话'} "
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
            total_text_chars = 0
            chunks_count = 0
            while True:
                try:
                    print(f"?? [Stream] 正在开启上游流式生成 (逻辑ID: {oc_session_id})")
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
                            chunks_count += 1
                        except StopAsyncIteration:
                            break
                        except asyncio.TimeoutError:
                            print(f"?? 流式 chunk 等待超时 ({STREAM_CHUNK_TIMEOUT}s)，强制结束流 (已收 {chunks_count} 个块)")
                            request_logger.log_error(f"Stream chunk timeout ({STREAM_CHUNK_TIMEOUT}s)", "stream")
                            yield make_sse_text_delta(f"\\n?? [Gemini 响应超时 {STREAM_CHUNK_TIMEOUT}s，已强制结束]", chunk_id)
                            break

                        if not chunk.text_delta:
                            continue

                        total_text_chars += len(chunk.text_delta)
                        if chunks_count % 10 == 1 or len(chunk.text_delta) > 50:
                             print(f"  ?? [Chunk #{chunks_count}] 收到正文增量 ({len(chunk.text_delta)} chars)")

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
                                    total_text_chars += len(event.text)
                            elif event.kind == "tool_call_finalized" and event.tool_call:
                                has_yielded_tool = True
                                request_logger.log_stream_event("tool_call_finalized", tool_name=event.tool_call.name)
                                yield make_sse_tool_call_delta(event.tool_call, tool_call_index, chunk_id)
                                tool_call_index += 1

                    if total_text_chars == 0 and not has_yielded_tool:
                         print(f"? [Stream] 警告：上游流已结束，但全程未收到任何正文或工具调用！(总块数: {chunks_count})")
                    else:
                         print(f"? [Stream] 流读取完成 (总块数: {chunks_count}, 总字数: {total_text_chars})")

                    finish = "tool_calls" if (decoder and decoder.had_calls) else "stop"
                    yield make_sse_done(finish, chunk_id)

                    # ? 核心：请求完全成功，记录新的差分起点
                    session_manager.persist_live_session(
                        oc_session_id,
                        chat_session,
                        last_msg_idx=len(messages),
                        model=state.active_model,
                        parent_session_id=oc_parent_id,
                        agent_type=oc_agent_type,
                    )
                    
                    final_cid = getattr(chat_session, "cid", "UNKNOWN") if chat_session else "UNKNOWN"
                    print(f"? 请求完成，已成功绑定并更新物理会话 (物理CID: {final_cid})")
                    break # 完全成功，跳出外层重试环

                except ContextMigrationNeeded as e:
                    # ?? 发生账号轮换！立刻开启【全量降级重构】！
                    print(f"{COLOR_YELLOW}?? 捕获迁移信号: {e} | 生成新窗口：正在新账号重建全量物理上下文...{COLOR_RESET}")
                    
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
                    
                    print(f"{COLOR_YELLOW}? 全量上下文重新编译完毕，准备向新账号提交无缝请求。{COLOR_RESET}")
                    continue # 携带全新的 payload 进入下一次 while 循环，实现断点续传

                except Exception as e:
                    from .exceptions import ProxyException
                    err_str = _normalize_proxy_error_text(str(e) or repr(e))
                    error_type = e.error_type if isinstance(e, ProxyException) else "UNKNOWN_ERROR"
                    print(f"? 流式报错 [{error_type}]: {err_str}")
                    request_logger.log_error(
                        f"[{error_type}] {err_str}\\n[模型: {state.active_model} | 代理: {PROXIES or 'disabled'}]",
                        "stream",
                    )
                    yield make_sse_text_delta(f"\\n?? [Gemini Proxy Error | {error_type}]: {err_str}", chunk_id)
                    yield make_sse_done("stop", chunk_id)
                    break
\"\"\"

new_content = content[:idx_start] + restoration + content[idx_end:]
with open(path, 'w', encoding='utf-8') as f:
    f.write(new_content)
print("Successfully restored chat_completions and event_generator with enhanced logging.")
