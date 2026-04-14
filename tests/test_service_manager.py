import asyncio
from pathlib import Path

from reverse_runtime.service_manager import GeminiReverseServiceManager


class _FakeProcess:
    def __init__(self):
        self.pid = 4321
        self._poll = None

    def poll(self):
        return self._poll

    def terminate(self):
        self._poll = 0

    def wait(self, timeout=None):
        self._poll = 0

    def kill(self):
        self._poll = 0


async def _healthy_probe(host, port, timeout_sec):
    return {
        "base_url": f"http://{host}:{port}",
        "models_ok": True,
        "debug_status_ok": True,
        "error": "",
    }


def test_status_reports_healthy_when_probe_succeeds():
    manager = GeminiReverseServiceManager(
        plugin_root=Path("."),
        probe_func=_healthy_probe,
        process_factory=lambda *args, **kwargs: _FakeProcess(),
    )
    status = asyncio.run(
        manager.status(
            {
                "managed_service": True,
                "host": "127.0.0.1",
                "port": 8000,
                "healthcheck_interval_sec": 1,
            }
        )
    )
    assert status.healthy is True
