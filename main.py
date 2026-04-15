from __future__ import annotations

import json
import asyncio
from pathlib import Path

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
    async def on_loaded(self, event):
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
        runtime_config_path = write_runtime_config(self.runtime_config)
        
        if start_service and self.runtime_config.get("managed_service"):
            await self.service_manager.start(self.runtime_config, runtime_config_path)
            # 给予 Uvicorn 充分的启动与端口绑定时间 (暖机)
            logger.info(f"[{PLUGIN_NAME}] 正在等待独立后端服务启动并绑定端口...")
            await asyncio.sleep(3)
            
        await self._sync_provider()

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

        yield event.plain_result(
            "用法: /gemini_reverse [status|start|stop|restart|doctor|provider_profile]"
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