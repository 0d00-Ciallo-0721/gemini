import json
from types import SimpleNamespace
from unittest.mock import patch


def test_runtime_services_prefers_app_state_services():
    runtime_mod = __import__("app.services.runtime_services", fromlist=["dummy"])
    expected = SimpleNamespace(name="from-app-state")
    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                services=expected,
            )
        )
    )

    actual = runtime_mod.get_runtime_services(request)
    assert actual is expected


def test_runtime_services_exposes_dynamic_config_properties():
    runtime_mod = __import__("app.services.runtime_services", fromlist=["dummy"])

    with patch.object(runtime_mod.config_mod, "PROXIES", "socks5://127.0.0.1:40000"):
        with patch.object(runtime_mod.config_mod.state, "active_model", "gemini-3-flash"):
            services = runtime_mod.build_runtime_services()
            assert services.proxy == "socks5://127.0.0.1:40000"
            assert services.state.active_model == "gemini-3-flash"
            assert isinstance(services.runtime_config, dict)


def test_strict_json_object_parse_rejects_loose_json():
    parser_mod = __import__("app.tool_parser", fromlist=["dummy"])

    assert parser_mod.strict_json_object_parse('{foo: "bar"}') is None
    assert parser_mod.strict_json_object_parse("hello world") is None


def test_parse_tool_calls_requires_json_object_parameters():
    parser_mod = __import__("app.tool_parser", fromlist=["dummy"])

    result = parser_mod.parse_tool_calls(
        "<tool_call><tool_name>read_file</tool_name><parameters>{foo: \"bar\"}</parameters></tool_call>"
    )

    assert result.has_calls is False
    assert "<tool_call>" in result.text


def test_parse_tool_calls_accepts_standard_tool_call_json():
    parser_mod = __import__("app.tool_parser", fromlist=["dummy"])

    result = parser_mod.parse_tool_calls(
        "<tool_call><tool_name>read_file</tool_name><parameters>{\"path\": \"C:\\\\tmp\\\\a.txt\"}</parameters></tool_call>"
    )

    assert result.has_calls is True
    assert result.tool_calls[0].name == "read_file"
    assert json.loads(result.tool_calls[0].arguments)["path"] == "C:\\tmp\\a.txt"


def test_context_manager_uses_runtime_services_for_account_switch():
    context_mod = __import__("app.context_manager", fromlist=["dummy"])

    async def run():
        with patch.object(
            context_mod.ChatContextManager,
            "_get_runtime_services",
            return_value=SimpleNamespace(
                state=SimpleNamespace(active_account="0", active_model="gemini-3-flash"),
                accounts={"1": {}},
                gemini_conn=SimpleNamespace(
                    initialize=lambda: __import__("asyncio").sleep(0, result=(True, "ok"))
                ),
            ),
        ):
            return await context_mod.ChatContextManager().process_commands("/account 1")

    handled, reply = __import__("asyncio").run(run())
    assert handled is True
    assert "账号 1" in reply


def test_tool_parser_logs_emergency_json_path(tmp_path):
    parser_mod = __import__("app.tool_parser", fromlist=["dummy"])
    logger_mod = __import__("app.logger", fromlist=["dummy"])
    logger = logger_mod.RequestLogger(str(tmp_path))

    with patch.object(parser_mod.logger_mod, "request_logger", logger):
        parsed = parser_mod.safe_json_parse('{"command":"echo hi"} trailing')

    assert parsed["command"] == "echo hi"
    logs = logger.get_recent_logs(10)
    assert any(item.get("context") == "tool_calls" for item in logs)
