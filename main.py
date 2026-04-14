from __future__ import annotations

import json
import asyncio
import sys
import subprocess
from pathlib import Path

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

# =====================================================================
# [新增] 自动依赖检查与安装逻辑
# 在 AstrBot 加载此模块时，拦截 ImportError 并自动向当前虚拟环境注入依赖
# =====================================================================
def _ensure_deps():
    try:
        import fastapi
        import uvicorn
        import httpx
        import gemini_webapi # 增加对核心库的检测
    except ImportError:
        logger.info("[gemini_reverse] 检测到缺失后端依赖，正在自动安装 (fastapi, uvicorn, httpx, gemini_webapi)...")
        try:
            # sys.executable 指向当前 AstrBot 正在使用的 Python 解释器
            subprocess.check_call([
                sys.executable, "-m", "pip", "install", 
                "fastapi", "uvicorn", "httpx", "gemini_webapi"
            ])
            logger.info("[gemini_reverse] 🚀 依赖自动安装成功！")
        except Exception as e:
            logger.error(f"[gemini_reverse] ❌ 依赖自动安装失败，错误详情: {e}")
# 模块加载时立即执行自检
_ensure_deps()
# =====================================================================

from .reverse_runtime.healthcheck import run_doctor
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
        super().__init__(context)
        self.raw_config = config or {}
        self.runtime_config = resolve_runtime_config(self.raw_config)
        self.service_manager = GeminiReverseServiceManager(plugin_root=Path(__file__).resolve().parent)
        
        # 兼容 WebUI 载入插件和保存配置的热重载机制
        asyncio.create_task(self.bootstrap())

    async def bootstrap(self) -> None:
        """异步启动流程"""
        try:
            # 稍微等待 0.5s 确保上一个实例的 terminate 已经释放了端口，以及管理器完全挂载
            await asyncio.sleep(0.5) 
            await self._sync_runtime(start_service=True)
            logger.info(f"[{PLUGIN_NAME}] 插件初始化/重载完成，后端服务已同步拉起。")
        except Exception as e:
            logger.error(f"[{PLUGIN_NAME}] 自动拉起后端服务失败: {e}")

    async def _sync_runtime(self, start_service: bool = True) -> None:
        self.runtime_config = resolve_runtime_config(self.raw_config)
        runtime_config_path = write_runtime_config(self.runtime_config)
        
        if start_service and self.runtime_config.get("managed_service"):
            await self.service_manager.start(self.runtime_config, runtime_config_path)
            # 给予 Uvicorn 充分的启动与端口绑定时间 (暖机)
            logger.info(f"[{PLUGIN_NAME}] 正在等待独立后端服务启动并绑定端口...")
            await asyncio.sleep(3)
            
        await self._sync_provider()

    async def _sync_provider(self) -> dict:
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
        status = await self.service_manager.status(self.runtime_config)
        return {
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
            payload = await run_doctor(self.runtime_config)
            yield event.plain_result(json.dumps(payload, ensure_ascii=False, indent=2))
            return

        if action == "provider_profile":
            profile = await self._sync_provider()
            yield event.plain_result(json.dumps(profile, ensure_ascii=False, indent=2))
            return

        yield event.plain_result(
            "用法: /gemini_reverse [status|start|stop|restart|doctor|provider_profile]"
        )

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