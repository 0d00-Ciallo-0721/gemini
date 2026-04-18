from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

try:
    from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path
except Exception:  # pragma: no cover - local unit tests may not import AstrBot
    def get_astrbot_plugin_data_path() -> str:
        return str(Path.cwd() / "data" / "plugin_data")

from .provider_profile import provider_is_gemini_reverse

try:
    from ..update_cookie import (
        extract_cookie_strings,
        normalize_cookie_accounts,
        patch_runtime_config,
    )
except ImportError:  # pragma: no cover - local tests import reverse_runtime as top-level package
    from update_cookie import extract_cookie_strings, normalize_cookie_accounts, patch_runtime_config


PLUGIN_NAME = "astrbot_plugin_gemini_reverse"
REVERSE_SESSION_TAG = "astrbot_reverse_session"
REVERSE_SESSION_PATTERN = re.compile(
    rf"<{REVERSE_SESSION_TAG}>\s*(.*?)\s*</{REVERSE_SESSION_TAG}>",
    re.DOTALL | re.IGNORECASE,
)


def get_plugin_data_dir() -> Path:
    return Path(get_astrbot_plugin_data_path()) / PLUGIN_NAME


def ensure_plugin_runtime_dirs() -> Path:
    plugin_data_dir = get_plugin_data_dir()
    plugin_data_dir.mkdir(parents=True, exist_ok=True)
    (plugin_data_dir / "logs").mkdir(parents=True, exist_ok=True)
    return plugin_data_dir


def resolve_runtime_config(raw_config: dict[str, Any] | None, auth_manager=None) -> dict[str, Any]:
    """
    配置规范化转换层桥模型：
    唯一职责是把外层的原始 AstrBot 配置，组装成为底层独立引擎所需的字典视图。
    不承担任何运行时状态机逻辑。
    """
    config = dict(raw_config or {})
    plugin_data_dir = ensure_plugin_runtime_dirs()
    session_db_path = str(config.get("session_db_path") or plugin_data_dir / "reverse_sessions.sqlite3")
    log_dir = str(config.get("log_dir") or plugin_data_dir / "logs")

    accounts = config.get("accounts", []) or []
    cookie_accounts = extract_cookie_strings(accounts)
    normalized_accounts = normalize_cookie_accounts(accounts)

    base = {
        "managed_service": bool(config.get("managed_service", True)),
        "host": str(config.get("host") or "127.0.0.1"),
        "port": int(config.get("port") or 8000),
        "model": str(config.get("model") or "gemini-3.1-pro"),
        "provider_id": str(config.get("provider_id") or "gemini_reverse"),
        "provider_name": str(config.get("provider_name") or "Gemini Reverse"),
        "proxy": str(config.get("proxy") or ""),
        "session_db_path": session_db_path,
        "log_dir": log_dir,
        "debug_mode": bool(config.get("debug_mode", False)),
        "healthcheck_interval_sec": max(int(config.get("healthcheck_interval_sec") or 5), 1),
        "cookie_accounts": cookie_accounts,
        "accounts": normalized_accounts,
        "runtime_config_path": str(plugin_data_dir / "runtime_config.json"),
        "auth_mode": "relay_ticket",

        "relay_shared_secret": str(config.get("relay_shared_secret", "change_me_to_a_random_string")),
        "relay_primary_client_id": str(config.get("relay_primary_client_id", "my_desktop")),
        "relay_ticket_ttl_sec": int(config.get("relay_ticket_ttl_sec", 172800)),
        "relay_refresh_interval_sec": int(config.get("relay_refresh_interval_sec", 3600)),
        "relay_accept_push_without_restart": bool(config.get("relay_accept_push_without_restart", True)),
        "plugin_data_dir": str(plugin_data_dir),
    }
    
    if auth_manager:
        view = auth_manager.get_auth_view()
        base["active_ticket"] = view.get("active_ticket")
        base["fallback_accounts"] = view.get("fallback_accounts", {})
        base["auth_status"] = view.get("status")
        
    return base


def write_runtime_config(runtime_config: dict[str, Any]) -> Path:
    """
    持久化输出运行时配置出口。
    写入链路两步走策略：
    1. 首先采用 atomic_write_json 完整原子化落盘（屏蔽崩溃损毁风险）。
    2. 其次通过 patch_runtime_config 特殊通道同步复杂长串 Cookie 数据。
       该设计用以隔离超长乱码数据对核心配置文件的格式污染，同时继续兼容旧版代码期望。
    """
    runtime_path = Path(runtime_config["runtime_config_path"])
    try:
        from .ticket_store import atomic_write_json
        atomic_write_json(runtime_path, runtime_config)
    except ImportError:
        runtime_path.parent.mkdir(parents=True, exist_ok=True)
        runtime_path.write_text(json.dumps(runtime_config, ensure_ascii=False, indent=2), encoding="utf-8")
    patch_runtime_config(runtime_path, runtime_config.get("cookie_accounts", []))
    return runtime_path


# =====================================================================
# Reverse Session 哨兵标识封印与分流
# 用于实现：当此插件同时服务于 AstrMai（独立智能体）等具备独立内存环境的调用时，
# 确保在 Gemini 侧也能无缝拉出互相隔离的光滑多实例对话体验。
# =====================================================================

def render_reverse_session_block(
    session_id: str,
    *,
    session_scope: str = "",
    parent_session_id: str = "",
    session_kind: str = "",
    source: str = "",
) -> str:
    normalized_session_id = str(session_id or "").strip()
    if not normalized_session_id:
        return ""
    payload = {
        "session_id": normalized_session_id,
        "session_scope": str(session_scope or "").strip(),
        "parent_session_id": str(parent_session_id or "").strip(),
        "session_kind": str(session_kind or "").strip(),
        "source": str(source or "").strip(),
    }
    body = "\n".join(f"{key}={value}" for key, value in payload.items())
    return f"<{REVERSE_SESSION_TAG}>\n{body}\n</{REVERSE_SESSION_TAG}>"


def strip_reverse_session_block(text: str) -> str:
    raw = str(text or "")
    if not raw:
        return ""
    return REVERSE_SESSION_PATTERN.sub("", raw).strip()


def maybe_attach_reverse_session_block(
    system_prompt: str,
    provider: Any,
    *,
    session_id: str,
    session_scope: str = "",
    parent_session_id: str = "",
    session_kind: str = "",
    source: str = "",
) -> str:
    current = str(system_prompt or "")
    if REVERSE_SESSION_PATTERN.search(current):
        return current
    if not provider_is_gemini_reverse(provider):
        return current
    block = render_reverse_session_block(
        session_id,
        session_scope=session_scope,
        parent_session_id=parent_session_id,
        session_kind=session_kind,
        source=source,
    )
    if not block:
        return strip_reverse_session_block(current)
    base = strip_reverse_session_block(current)
    if not base:
        return block
    return f"{base}\n\n{block}"
