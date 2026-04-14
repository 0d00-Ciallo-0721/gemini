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


def resolve_runtime_config(raw_config: dict[str, Any] | None) -> dict[str, Any]:
    config = dict(raw_config or {})
    plugin_data_dir = ensure_plugin_runtime_dirs()
    session_db_path = str(config.get("session_db_path") or plugin_data_dir / "reverse_sessions.sqlite3")
    log_dir = str(config.get("log_dir") or plugin_data_dir / "logs")

    accounts = config.get("accounts", []) or []
    cookie_accounts = extract_cookie_strings(accounts)
    normalized_accounts = normalize_cookie_accounts(accounts)

    return {
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
    }


def write_runtime_config(runtime_config: dict[str, Any]) -> Path:
    runtime_path = Path(runtime_config["runtime_config_path"])
    runtime_path.parent.mkdir(parents=True, exist_ok=True)
    runtime_path.write_text(json.dumps(runtime_config, ensure_ascii=False, indent=2), encoding="utf-8")
    patch_runtime_config(runtime_path, runtime_config.get("cookie_accounts", []))
    return runtime_path


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
