from __future__ import annotations

import asyncio
import os
import socket
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .healthcheck import probe_reverse_service


@dataclass
class ServiceStatus:
    managed: bool
    running: bool
    owned_by_plugin: bool
    pid: int | None
    host: str
    port: int
    healthy: bool
    detail: str
    health: dict[str, Any] | None = None


class GeminiReverseServiceManager:
    def __init__(
        self,
        plugin_root: Path,
        probe_func: Callable[..., Any] = probe_reverse_service,
        process_factory: Callable[..., subprocess.Popen] = subprocess.Popen,
    ) -> None:
        self.plugin_root = Path(plugin_root)
        self._probe_func = probe_func
        self._process_factory = process_factory
        self._process: subprocess.Popen | None = None
        self._last_health: dict[str, Any] | None = None
        self._last_error: str = ""
        self._stdout_log_path = self.plugin_root / "service_stdout.log"
        self._stderr_log_path = self.plugin_root / "service_stderr.log"

    @staticmethod
    def _is_port_in_use(host: str, port: int) -> bool:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.5)
            return sock.connect_ex((host, int(port))) == 0

    def _owned_process_running(self) -> bool:
        return self._process is not None and self._process.poll() is None

    async def status(self, runtime_config: dict[str, Any]) -> ServiceStatus:
        host = str(runtime_config["host"])
        port = int(runtime_config["port"])
        health = await self._probe_func(
            host,
            port,
            int(runtime_config["healthcheck_interval_sec"]),
        )
        self._last_health = health
        running = self._owned_process_running() or self._is_port_in_use(host, port)
        healthy = bool(health.get("models_ok")) and bool(health.get("debug_status_ok"))
        detail = self._last_error or health.get("error", "")
        if running and healthy and not detail:
            detail = "reverse service is healthy"
        return ServiceStatus(
            managed=bool(runtime_config["managed_service"]),
            running=running,
            owned_by_plugin=self._owned_process_running(),
            pid=self._process.pid if self._owned_process_running() else None,
            host=host,
            port=port,
            healthy=healthy,
            detail=detail,
            health=health,
        )

    async def start(self, runtime_config: dict[str, Any], runtime_config_path: Path) -> ServiceStatus:
        host = str(runtime_config["host"])
        port = int(runtime_config["port"])
        if self._owned_process_running():
            return await self.status(runtime_config)

        health = await self._probe_func(
            host,
            port,
            int(runtime_config["healthcheck_interval_sec"]),
        )
        if health.get("models_ok") and health.get("debug_status_ok"):
            self._last_health = health
            self._last_error = ""
            return await self.status(runtime_config)

        if self._is_port_in_use(host, port):
            self._last_error = f"port {port} is occupied by another process"
            return await self.status(runtime_config)

        env = os.environ.copy()
        env["ASTRBOT_GEMINI_REVERSE_CONFIG"] = str(runtime_config_path)
        log_root = Path(runtime_config_path).parent
        self._stdout_log_path = log_root / "service_stdout.log"
        self._stderr_log_path = log_root / "service_stderr.log"
        self._stdout_log_path.parent.mkdir(parents=True, exist_ok=True)
        stdout_log = open(self._stdout_log_path, "ab")
        stderr_log = open(self._stderr_log_path, "ab")
        try:
            self._process = self._process_factory(
                [sys.executable, "-m", "bundled_gemini.main"],
                cwd=str(self.plugin_root),
                env=env,
                stdout=stdout_log,
                stderr=stderr_log,
            )
        finally:
            stdout_log.close()
            stderr_log.close()

        for _ in range(20):
            await asyncio.sleep(0.5)
            health = await self._probe_func(
                host,
                port,
                int(runtime_config["healthcheck_interval_sec"]),
            )
            if health.get("models_ok") and health.get("debug_status_ok"):
                self._last_error = ""
                self._last_health = health
                return await self.status(runtime_config)
            if self._process.poll() is not None:
                break

        exit_code = self._process.poll() if self._process else None
        self._last_error = (
            "reverse service failed to become healthy after startup"
            f" (exit_code={exit_code}, stderr_log={self._stderr_log_path})"
        )
        return await self.status(runtime_config)

    async def stop(self, runtime_config: dict[str, Any]) -> ServiceStatus:
        if self._owned_process_running():
            self._process.terminate()
            try:
                self._process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._process.kill()
                self._process.wait(timeout=5)
        self._process = None
        self._last_health = None
        return await self.status(runtime_config)
