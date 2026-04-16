from __future__ import annotations

import json
import asyncio
from pathlib import Path
import httpx

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

from .reverse_runtime.healthcheck import run_doctor
from .reverse_runtime.auth_manager import AuthManager
from .reverse_runtime.provider_profile import build_provider_profile
from .reverse_runtime.service_manager import GeminiReverseServiceManager
from .reverse_runtime.session_bridge import (
    PLUGIN_NAME,
    maybe_attach_reverse_session_block,
    resolve_runtime_config,
    write_runtime_config,
)


@register(
    PLUGIN_NAME,
    "zlj",
    "Gemini reverse provider plugin for AstrBot",
    "0.1.0",
    "local",
)
class GeminiReversePlugin(Star):
    def __init__(self, context: Context, config: dict | None = None) -> None:
        """
        阶段 1：插件初始化
        只做只读配置的拼装和底层对象挂载，不触发繁重的副作用。
        """
        super().__init__(context)
        self.raw_config = config or {}
        # runtime_config: 核心状态隔离层，提供给后端服务的真实配置大盘
        self.runtime_config = resolve_runtime_config(self.raw_config)
        # auth_manager: 长期饭票控制面视图
        self.auth_manager = AuthManager(self.runtime_config["plugin_data_dir"], self.runtime_config)
        # service_manager: Uvicorn 后端进程托管器
        self.service_manager = GeminiReverseServiceManager(plugin_root=Path(__file__).resolve().parent)
        
        # 幂等标记防重复触发
        self._bootstrapped = False
        # 兼容 WebUI 热重载场景自举
        self._bootstrap_task = asyncio.create_task(self._bootstrap_once())

    # =====================================================================
    # 阶段 2：生命周期启动
    # =====================================================================
    @filter.on_astrbot_loaded()
    async def on_loaded(self):
        """AstrBot 官方标准生命周期启动入口"""
        await self._bootstrap_once()

    async def _bootstrap_once(self) -> None:
        """幂等的后端同步拉起引擎，支持作为热重载 fallback 与标准加载的双入口"""
        if self._bootstrapped:
            return
        self._bootstrapped = True
        try:
            # 等待旧实例可能存在的 terminate 中止完毕
            await asyncio.sleep(0.5) 
            await self._sync_runtime(start_service=True)
            logger.info(f"[{PLUGIN_NAME}] 插件初始化完成，Gemini Reverse 原生代理引擎已起步。")
        except Exception as e:
            logger.error(f"[{PLUGIN_NAME}] 自动拉起后端代理服务失败: {e}")

    async def _sync_runtime(self, start_service: bool = True) -> None:
        """根据当前态全量刷新运行时配置，并托管拉起真正的业务子进程"""
        self.runtime_config = resolve_runtime_config(self.raw_config)
        
        # 嗅探并接管 bootstrap_cookie
        bootstrap_cookie = str(self.raw_config.get("bootstrap_cookie") or "").strip()
        if bootstrap_cookie:
            import time
            from .update_cookie import standardize_cookie_payload
            from .reverse_runtime.auth_status import AuthStatus
            
            current_ticket = self.auth_manager.store.load_active_ticket()
            if not current_ticket or current_ticket.get("bootstrap_source") != bootstrap_cookie:
                logger.info(f"[{PLUGIN_NAME}] 检测到新的 bootstrap_cookie，正在接管为长期凭证...")
                payload = standardize_cookie_payload(bootstrap_cookie, default_label="bootstrap")
                if payload:
                    ticket = {
                        "cookie_data": payload,
                        "push_time": time.time(),
                        "last_refresh_time": time.time(),
                        "client_id": "bootstrap",
                        "status": AuthStatus.HEALTHY.value,
                        "bootstrap_source": bootstrap_cookie
                    }
                    self.auth_manager.store.save_active_ticket(ticket)
                    self.auth_manager.transition_state(AuthStatus.HEALTHY, "Imported from bootstrap_cookie")
                    logger.info(f"[{PLUGIN_NAME}] 导入完成，已接管后续动态刷新生命周期。")

        runtime_config_path = write_runtime_config(self.runtime_config)
        
        if start_service and self.runtime_config.get("managed_service"):
            await self.service_manager.start(self.runtime_config, runtime_config_path)
            # 给予 Uvicorn 充分的启动与端口绑定时间 (暖机)
            logger.info(f"[{PLUGIN_NAME}] 正在等待独立后端服务启动并绑定端口...")
            await asyncio.sleep(3)
            
        await self._cleanup_legacy_source_providers()
        await self._sync_provider()

    async def _cleanup_legacy_source_providers(self) -> None:
        """清理旧版 gemini_reverse_source/* provider 残留，避免 AstrBot 继续加载失效 source。"""
        provider_manager = getattr(self.context, "provider_manager", None)
        if not provider_manager:
            return

        providers_config = list(getattr(provider_manager, "providers_config", []) or [])
        delete_provider = getattr(provider_manager, "delete_provider", None)
        if not providers_config or not callable(delete_provider):
            return

        legacy_ids = []
        for provider in providers_config:
            provider_id = str((provider or {}).get("id") or "").strip()
            if provider_id.startswith("gemini_reverse_source/"):
                legacy_ids.append(provider_id)

        for legacy_id in legacy_ids:
            try:
                await delete_provider(provider_id=legacy_id)
                logger.info(f"[{PLUGIN_NAME}] 检测并清理旧版 source provider 残留: {legacy_id}")
            except Exception as e:
                logger.warning(f"[{PLUGIN_NAME}] 清理旧版 source provider 残留失败 {legacy_id}: {e}")

    async def _sync_provider(self) -> dict:
        """维护当前 provider 至 AstrBot 核心引擎进行挂载同步"""
        profile = build_provider_profile(self.runtime_config)
        provider_id = profile["id"]
        
        try:
            # 尝试从上下文获取已存在的提供商
            existing = await self.context.provider_manager.get_provider_by_id(provider_id)
        except Exception:
            existing = None
            
        if existing:
            # [修复核心] 如果存在，直接跳过。不调用 update_provider，防止触发 AstrBot 核心配置重载和模型引用断裂
            logger.info(f"[{PLUGIN_NAME}] 💎 提供商 [{provider_id}] 已挂载，沿用当前已有配置。")
        else:
            # 只有在完全没有该提供商（例如第一次安装插件）时才去自动创建
            logger.info(f"[{PLUGIN_NAME}] 🌟 未找到提供商 [{provider_id}]，正在向 AstrBot 自动注册...")
            await self.context.provider_manager.create_provider(profile)
            
        return profile

    async def _status_payload(self) -> dict:
        """统一管理命令与诊断钩子输出，展示服务健康度全景图"""
        status = await self.service_manager.status(self.runtime_config)
        auth_view = self.auth_manager.get_auth_view()
        return {
            "auth": auth_view,
            "managed_service": status.managed,
            "running": status.running,
            "owned_by_plugin": status.owned_by_plugin,
            "pid": status.pid,
            "host": status.host,
            "port": status.port,
            "healthy": status.healthy,
            "detail": status.detail,
            "health": status.health or {},
            "session_db_path": self.runtime_config["session_db_path"],
            "model": self.runtime_config["model"],
            "provider_id": self.runtime_config["provider_id"],
        }

    def _resolve_active_provider_view(self, event: AstrMessageEvent) -> dict:
        provider = None
        try:
            provider = self.context.get_using_provider(event.unified_msg_origin)
        except Exception:
            provider = None

        provider_id = self.runtime_config["provider_id"]
        model = self.runtime_config["model"]

        if provider is not None:
            provider_id = (
                getattr(provider, "id", None)
                or getattr(provider, "provider_id", None)
                or provider_id
            )
            model = (
                getattr(provider, "model", None)
                or getattr(provider, "model_name", None)
                or getattr(getattr(provider, "meta", None), "model", None)
                or model
            )

        return {
            "provider_id": str(provider_id),
            "model": str(model),
            "provider": provider,
        }

    async def _run_direct_llm_probe(self, event: AstrMessageEvent, prompt: str) -> dict:
        active = self._resolve_active_provider_view(event)
        timeout_sec = 45
        response = await asyncio.wait_for(
            self.context.llm_generate(
                chat_provider_id=active["provider_id"],
                prompt=prompt,
            ),
            timeout=timeout_sec,
        )
        return {
            "provider_id": active["provider_id"],
            "model": active["model"],
            "timeout_sec": timeout_sec,
            "role": getattr(response, "role", ""),
            "completion_text": getattr(response, "completion_text", ""),
            "reasoning_content": getattr(response, "reasoning_content", ""),
            "tools_call_name": list(getattr(response, "tools_call_name", []) or []),
            "tools_call_args": list(getattr(response, "tools_call_args", []) or []),
        }

    async def _run_direct_api_probe(self, event: AstrMessageEvent, mode: str, prompt: str) -> dict:
        active = self._resolve_active_provider_view(event)
        host = self.runtime_config["host"]
        port = self.runtime_config["port"]
        base_url = f"http://{host}:{port}/v1/chat/completions"
        timeout_sec = 45
        payload = {
            "model": active["model"],
            "messages": [{"role": "user", "content": prompt}],
            "stream": mode == "stream",
        }

        timeout = httpx.Timeout(timeout_sec)
        async with httpx.AsyncClient(timeout=timeout) as client:
            if mode == "sync":
                response = await client.post(base_url, json=payload)
                response.raise_for_status()
                data = response.json()
                choice = ((data.get("choices") or [{}])[0]) if isinstance(data, dict) else {}
                message = choice.get("message") or {}
                return {
                    "provider_id": active["provider_id"],
                    "model": active["model"],
                    "mode": mode,
                    "timeout_sec": timeout_sec,
                    "status_code": response.status_code,
                    "finish_reason": choice.get("finish_reason"),
                    "content": message.get("content", ""),
                    "raw_choice": choice,
                }

            chunk_lines = []
            first_line_received = False
            try:
                async with client.stream("POST", base_url, json=payload) as response:
                    response.raise_for_status()
                    line_iter = response.aiter_lines()
                    
                    # 1. 尝试等待首行 (通常包含 role: assistant)
                    try:
                        first_line = await asyncio.wait_for(line_iter.__anext__(), timeout=timeout_sec)
                        chunk_lines.append(first_line)
                        first_line_received = True
                    except asyncio.TimeoutError:
                        return {
                            "provider_id": active["provider_id"],
                            "model": active["model"],
                            "mode": mode,
                            "error_type": "DirectAPIProbeFirstLineTimeout",
                            "error": f"首个 SSE 行在 {timeout_sec} 秒内未返回，上游可能已默默中止或严重拥堵。",
                            "lines": []
                        }

                    # 2. 持续读取并提供更长的空闲允许时间 (20s)
                    while len(chunk_lines) < 20:
                        try:
                            # 既然首行已到，后续只要不长期断流即可
                            line = await asyncio.wait_for(line_iter.__anext__(), timeout=20)
                            chunk_lines.append(line)
                            if line.strip() == "data: [DONE]":
                                break
                        except asyncio.TimeoutError:
                            return {
                                "provider_id": active["provider_id"],
                                "model": active["model"],
                                "mode": mode,
                                "first_line_received": True,
                                "error_type": "DirectAPIProbeIdleTimeout",
                                "error": "已收到首个 SSE 行，但后续内容块在 20 秒内未按时到来。可能是生成过慢或流被截断。",
                                "lines": chunk_lines,
                            }
                        except StopAsyncIteration:
                            break

                    return {
                        "provider_id": active["provider_id"],
                        "model": active["model"],
                        "mode": mode,
                        "status_code": response.status_code,
                        "lines": chunk_lines,
                    }
            except Exception as e:
                return {
                    "provider_id": active["provider_id"],
                    "model": active["model"],
                    "mode": mode,
                    "error_type": type(e).__name__,
                    "error": str(e),
                    "lines": chunk_lines
                }

    @filter.on_llm_request()
    async def inject_reverse_session(self, event: AstrMessageEvent, request):
        try:
            provider = self.context.get_using_provider(event.unified_msg_origin)
        except Exception:
            provider = None
        request.system_prompt = maybe_attach_reverse_session_block(
            getattr(request, "system_prompt", "") or "",
            provider,
            session_id=str(getattr(request, "session_id", "") or event.unified_msg_origin),
            session_scope=str(event.unified_msg_origin),
            parent_session_id="",
            session_kind="astrbot_native",
            source="astrbot",
        )

    # =====================================================================
    # 阶段 3：命令处理
    # =====================================================================
    @filter.command("gemini_reverse")
    async def gemini_reverse_command(self, event: AstrMessageEvent):
        parts = (event.message_str or "").split()
        action = parts[1].strip().lower() if len(parts) > 1 else "status"

        if action == "status":
            payload = await self._status_payload()
            yield event.plain_result(json.dumps(payload, ensure_ascii=False, indent=2))
            return

        if action == "start":
            runtime_config_path = write_runtime_config(self.runtime_config)
            status = await self.service_manager.start(self.runtime_config, runtime_config_path)
            yield event.plain_result(json.dumps(status.__dict__, ensure_ascii=False, indent=2))
            return

        if action == "stop":
            if not self.runtime_config.get("managed_service"):
                yield event.plain_result("managed_service=false，插件当前不接管外部 reverse 服务的停止操作。")
                return
            status = await self.service_manager.stop(self.runtime_config)
            yield event.plain_result(json.dumps(status.__dict__, ensure_ascii=False, indent=2))
            return

        if action == "restart":
            if self.runtime_config.get("managed_service"):
                await self.service_manager.stop(self.runtime_config)
            runtime_config_path = write_runtime_config(self.runtime_config)
            status = await self.service_manager.start(self.runtime_config, runtime_config_path)
            yield event.plain_result(json.dumps(status.__dict__, ensure_ascii=False, indent=2))
            return

        if action == "doctor":
            payload = await run_doctor(self.runtime_config, self.auth_manager)
            yield event.plain_result(json.dumps(payload, ensure_ascii=False, indent=2))
            return

        if action == "provider_profile":
            profile = await self._sync_provider()
            yield event.plain_result(json.dumps(profile, ensure_ascii=False, indent=2))
            return

        if action == "llm":
            prompt = " ".join(parts[2:]).strip()
            if not prompt:
                yield event.plain_result("用法: /gemini_reverse llm <要发送给当前 provider 的消息>")
                return
            active = self._resolve_active_provider_view(event)
            try:
                payload = await self._run_direct_llm_probe(event, prompt)
            except asyncio.TimeoutError:
                payload = {
                    "provider_id": active["provider_id"],
                    "model": active["model"],
                    "error_type": "LLMProbeTimeout",
                    "error": "llm_generate 在 45 秒内未返回，说明非流式直调本身也卡住了。",
                }
            except Exception as e:
                payload = {
                    "provider_id": active["provider_id"],
                    "model": active["model"],
                    "error_type": type(e).__name__,
                    "error": str(e),
                }
            yield event.plain_result(json.dumps(payload, ensure_ascii=False, indent=2))
            return

        if action == "api":
            mode = parts[2].strip().lower() if len(parts) > 2 else "sync"
            prompt = " ".join(parts[3:]).strip() if len(parts) > 3 else ""
            if mode not in {"sync", "stream"} or not prompt:
                yield event.plain_result("用法: /gemini_reverse api <sync|stream> <要发送给本地 /v1/chat/completions 的消息>")
                return
            active = self._resolve_active_provider_view(event)
            try:
                payload = await self._run_direct_api_probe(event, mode, prompt)
            except asyncio.TimeoutError:
                payload = {
                    "provider_id": active["provider_id"],
                    "model": active["model"],
                    "mode": mode,
                    "error_type": "DirectAPIProbeTimeout",
                    "error": "直连本地 /v1/chat/completions 在 45 秒内未返回。",
                }
            except Exception as e:
                payload = {
                    "provider_id": active["provider_id"],
                    "model": active["model"],
                    "mode": mode,
                    "error_type": type(e).__name__,
                    "error": str(e),
                }
            yield event.plain_result(json.dumps(payload, ensure_ascii=False, indent=2))
            return

        yield event.plain_result(
            "用法: /gemini_reverse [status|start|stop|restart|doctor|provider_profile|llm|api]"
        )

    # =====================================================================
    # 阶段 4：terminate 清理
    # =====================================================================
    async def terminate(self):
        """当插件被重载、卸载或停用时调用，在此处完成旧进程的安全清理"""
        logger.info(f"[{PLUGIN_NAME}] 收到卸载/重载信号，正在清理资源并中止后端进程...")
        if self.runtime_config.get("managed_service"):
            try:
                await self.service_manager.stop(self.runtime_config)
            except AttributeError:
                # 忽略因频繁重载导致底层进程尚未初始化完毕的空指针异常
                pass
            except Exception as e:
                logger.debug(f"[{PLUGIN_NAME}] 停止旧服务时遇到可忽略的错误: {e}")
