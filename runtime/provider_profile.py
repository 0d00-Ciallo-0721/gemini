from __future__ import annotations

from typing import Any


REVERSE_PROVIDER = "gemini_web"
REVERSE_PLUGIN = "astrbot_plugin_gemini_reverse"
REVERSE_KIND = "gemini_web"
REVERSE_SESSION_VIA = "system_prompt"


def build_provider_profile(runtime_config: dict[str, Any]) -> dict[str, Any]:
    host = str(runtime_config["host"])
    port = int(runtime_config["port"])
    model = str(runtime_config["model"])
    provider_id = str(runtime_config["provider_id"])
    provider_name = str(runtime_config.get("provider_name") or provider_id)
    return {
        "id": provider_id,
        "type": "openai_chat_completion",
        "enable": True,
        "provider_type": "chat_completion",
        "meta": {"display_name": provider_name},
        "api_base": f"http://{host}:{port}/v1",
        "model": model,
        "key": ["dummy"],
        "timeout": 300,
        "reverse_provider": REVERSE_PROVIDER,
        "reverse_plugin": REVERSE_PLUGIN,
        "reverse_kind": REVERSE_KIND,
        "gemini_reverse": True,
        "supports_reverse_session": True,
        "reverse_session_via": REVERSE_SESSION_VIA,
    }


def provider_is_gemini_reverse(provider: Any) -> bool:
    if provider is None:
        return False

    provider_config = getattr(provider, "provider_config", None) or {}
    meta = None
    try:
        meta = provider.meta() if callable(getattr(provider, "meta", None)) else None
    except Exception:
        meta = None

    provider_type = str(getattr(meta, "type", "") or "").strip().lower()
    if provider_type and provider_type != "openai_chat_completion":
        return False

    truthy = {"1", "true", "yes", "on"}
    reverse_provider = str(provider_config.get("reverse_provider", "") or "").strip().lower()
    reverse_plugin = str(provider_config.get("reverse_plugin", "") or "").strip().lower()
    reverse_kind = str(provider_config.get("reverse_kind", "") or "").strip().lower()
    reverse_session_via = str(provider_config.get("reverse_session_via", "") or "").strip().lower()
    gemini_reverse = provider_config.get("gemini_reverse")
    supports_reverse_session = provider_config.get("supports_reverse_session")

    if reverse_provider == REVERSE_PROVIDER:
        return True
    if reverse_plugin == REVERSE_PLUGIN:
        return True
    if reverse_kind == REVERSE_KIND:
        return True
    if gemini_reverse is True or str(gemini_reverse or "").strip().lower() in truthy:
        return True
    if (
        supports_reverse_session is True
        or str(supports_reverse_session or "").strip().lower() in truthy
    ) and reverse_session_via == REVERSE_SESSION_VIA:
        return True
    return False
