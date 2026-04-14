from reverse_runtime.session_bridge import (
    maybe_attach_reverse_session_block,
    render_reverse_session_block,
    resolve_runtime_config,
)


class _Provider:
    def __init__(self):
        self.provider_config = {
            "reverse_provider": "gemini_web",
            "reverse_plugin": "astrbot_plugin_gemini_reverse",
            "reverse_kind": "gemini_web",
            "supports_reverse_session": True,
            "reverse_session_via": "system_prompt",
        }

    def meta(self):
        class _Meta:
            type = "openai_chat_completion"

        return _Meta()


def test_render_reverse_session_block_contains_session_id():
    block = render_reverse_session_block("umo:group:123", session_scope="umo:group:123")
    assert "session_id=umo:group:123" in block
    assert "session_scope=umo:group:123" in block


def test_attach_reverse_session_block_only_for_matching_provider():
    prompt = maybe_attach_reverse_session_block(
        "system prompt",
        _Provider(),
        session_id="lane:1",
        session_scope="umo:1",
        session_kind="astrbot_native",
        source="astrbot",
    )
    assert "session_id=lane:1" in prompt
    assert "system prompt" in prompt


def test_resolve_runtime_config_parses_cookie_based_accounts_and_proxy():
    runtime = resolve_runtime_config(
        {
            "proxy": "http://127.0.0.1:7890",
            "accounts": [
                "foo=bar; __Secure-1PSID=psid_value; a=b; __Secure-1PSIDTS=psidts_value;"
            ],
        }
    )
    assert runtime["proxy"] == "http://127.0.0.1:7890"
    assert runtime["accounts"]["1"]["SECURE_1PSID"] == "psid_value"
    assert runtime["accounts"]["1"]["SECURE_1PSIDTS"] == "psidts_value"
    assert runtime["accounts"]["1"]["label"] == "account_1"


def test_resolve_runtime_config_accepts_template_list_accounts():
    runtime = resolve_runtime_config(
        {
            "accounts": [
                {
                    "__template_key": "cookie_account",
                    "cookie": "foo=bar; __Secure-1PSID=psid_value; __Secure-1PSIDTS=psidts_value;",
                }
            ],
        }
    )
    assert runtime["cookie_accounts"] == [
        "foo=bar; __Secure-1PSID=psid_value; __Secure-1PSIDTS=psidts_value;"
    ]
    assert runtime["accounts"]["1"]["SECURE_1PSID"] == "psid_value"
