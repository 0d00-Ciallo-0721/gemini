from reverse_runtime.provider_profile import (
    REVERSE_KIND,
    REVERSE_PLUGIN,
    REVERSE_PROVIDER,
    REVERSE_SESSION_VIA,
    build_provider_profile,
)


def test_build_provider_profile_exposes_explicit_reverse_flags():
    profile = build_provider_profile(
        {
            "host": "127.0.0.1",
            "port": 8000,
            "model": "gemini-3.1-pro",
            "provider_id": "gemini_reverse",
            "provider_name": "Gemini Reverse",
        }
    )

    assert profile["type"] == "openai_chat_completion"
    assert profile["api_base"] == "http://127.0.0.1:8000/v1"
    assert profile["reverse_provider"] == REVERSE_PROVIDER
    assert profile["reverse_plugin"] == REVERSE_PLUGIN
    assert profile["reverse_kind"] == REVERSE_KIND
    assert profile["reverse_session_via"] == REVERSE_SESSION_VIA
    assert profile["supports_reverse_session"] is True
    assert profile["gemini_reverse"] is True
