from __future__ import annotations

import json
from pathlib import Path

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

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

    async def _sync_runtime(self, start_service: bool = True) -> None:
        self.runtime_config = resolve_runtime_config(self.raw_config)
        runtime_config_path = write_runtime_config(self.runtime_config)
        if start_service and self.runtime_config["managed_service"]:
            await self.service_manager.start(self.runtime_config, runtime_config_path)
        await self._sync_provider()

    async def _sync_provider(self) -> dict:
        profile = build_provider_profile(self.runtime_config)
        existing = await self.context.provider_manager.get_provider_by_id(profile["id"])
        if existing:
            await self.context.provider_manager.update_provider(profile["id"], profile)
        else:
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

    @filter.on_astrbot_loaded()
    async def on_program_start(self):
        await self._sync_runtime(start_service=True)
        logger.info("[gemini_reverse] plugin loaded and runtime synced.")

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
            if not self.runtime_config["managed_service"]:
                yield event.plain_result("managed_service=false，插件当前不接管外部 reverse 服务的停止操作。")
                return
            status = await self.service_manager.stop(self.runtime_config)
            yield event.plain_result(json.dumps(status.__dict__, ensure_ascii=False, indent=2))
            return

        if action == "restart":
            if self.runtime_config["managed_service"]:
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
        if self.runtime_config["managed_service"]:
            await self.service_manager.stop(self.runtime_config)
