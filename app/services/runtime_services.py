from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

from fastapi import Request

from ..api_client import gemini_conn
from .. import config as config_mod
from .. import logger as logger_mod
from .. import session_manager as session_manager_mod
from ..logger import request_logger
from ..session_manager import session_manager


@dataclass
class RuntimeServices:
    gemini_conn: Any
    session_manager: Any
    request_logger: Any

    @property
    def state(self):
        return config_mod.state

    @property
    def proxy(self):
        return config_mod.PROXIES

    @property
    def accounts(self):
        return config_mod.ACCOUNTS

    @property
    def auth_manager(self):
        return getattr(config_mod, "AUTH_MANAGER", None)

    @property
    def runtime_config(self):
        return config_mod.get_runtime_config()

    def get_current_account_data(self):
        return config_mod.get_current_account_data()


def build_runtime_services() -> RuntimeServices:
    return RuntimeServices(
        gemini_conn=gemini_conn,
        session_manager=session_manager,
        request_logger=request_logger,
    )


def get_runtime_services(request: Request | None = None) -> RuntimeServices:
    if request is not None:
        app = getattr(request, "app", None)
        state = getattr(app, "state", None)
        services = getattr(state, "services", None)
        if services is not None:
            return services
    return build_runtime_services()


def attach_runtime_services(app) -> RuntimeServices:
    services = build_runtime_services()
    logger_mod.set_runtime_config_provider(lambda: services.runtime_config)
    session_manager_mod.set_runtime_config_provider(lambda: services.runtime_config)
    if not hasattr(app, "state"):
        app.state = SimpleNamespace()
    app.state.services = services
    return services
