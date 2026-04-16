import pytest
from unittest.mock import patch, MagicMock

@pytest.mark.asyncio
async def test_plugin_hot_reload_survives():
    """
    Simulates a plugin hot reload to ensure that `main.py` teardown gracefully releases resources
    without crashing the main node loop.
    """
    from unittest.mock import patch, MagicMock, AsyncMock
    
    import sys
    import importlib.util
    from pathlib import Path
    
    plugin_dir = Path(__file__).parent.parent
    spec = importlib.util.spec_from_file_location(
        "astrbot_plugin_gemini_reverse", 
        str(plugin_dir / "main.py"),
        submodule_search_locations=[str(plugin_dir)]
    )
    main_mod = importlib.util.module_from_spec(spec)
    sys.modules["astrbot_plugin_gemini_reverse"] = main_mod
    spec.loader.exec_module(main_mod)
    GeminiReversePlugin = main_mod.GeminiReversePlugin
    
    # Mock Context with Async methods for AstrBot provider logic
    context_mock = MagicMock()
    context_mock.provider_manager = MagicMock()
    context_mock.provider_manager.get_provider_by_id = AsyncMock(return_value=None)
    context_mock.provider_manager.create_provider = AsyncMock()
    context_mock.provider_manager.providers_config = []
    context_mock.provider_manager.delete_provider = AsyncMock()
    
    plugin = GeminiReversePlugin(context_mock, config={"managed_service": True})
    
    # Patch instance service manager directly instead of using global patch strings
    plugin.service_manager.start = AsyncMock()
    plugin.service_manager.stop = AsyncMock()
    
    # Await the bootstrap background task fully so no leaking Tasks happen
    await plugin._bootstrap_task
    plugin.service_manager.start.assert_called_once()
    
    # Ensure it terminates safely and shuts down child process
    await plugin.terminate()
    plugin.service_manager.stop.assert_called_once()
    
    sys.modules.pop("astrbot_plugin_gemini_reverse", None)
    sys.modules.pop("astrbot_plugin_gemini_reverse.main", None)


@pytest.mark.asyncio
async def test_plugin_cleans_legacy_gemini_reverse_source_providers():
    from unittest.mock import patch, MagicMock, AsyncMock

    import sys
    import importlib.util
    from pathlib import Path

    plugin_dir = Path(__file__).parent.parent
    spec = importlib.util.spec_from_file_location(
        "astrbot_plugin_gemini_reverse",
        str(plugin_dir / "main.py"),
        submodule_search_locations=[str(plugin_dir)]
    )
    main_mod = importlib.util.module_from_spec(spec)
    sys.modules["astrbot_plugin_gemini_reverse"] = main_mod
    spec.loader.exec_module(main_mod)
    GeminiReversePlugin = main_mod.GeminiReversePlugin

    context_mock = MagicMock()
    context_mock.provider_manager = MagicMock()
    context_mock.provider_manager.get_provider_by_id = AsyncMock(return_value=None)
    context_mock.provider_manager.create_provider = AsyncMock()
    context_mock.provider_manager.delete_provider = AsyncMock()
    context_mock.provider_manager.providers_config = [
        {"id": "gemini_reverse_source/gemini-3-pro"},
        {"id": "gemini_reverse_source/gemini-3.1-pro"},
        {"id": "gemini_reverse"},
    ]

    plugin = GeminiReversePlugin(context_mock, config={"managed_service": False})
    plugin.service_manager.start = AsyncMock()
    plugin.service_manager.stop = AsyncMock()

    await plugin._bootstrap_task

    deleted_ids = [call.kwargs["provider_id"] for call in context_mock.provider_manager.delete_provider.await_args_list]
    assert deleted_ids == [
        "gemini_reverse_source/gemini-3-pro",
        "gemini_reverse_source/gemini-3.1-pro",
    ]

    await plugin.terminate()
    sys.modules.pop("astrbot_plugin_gemini_reverse", None)
    sys.modules.pop("astrbot_plugin_gemini_reverse.main", None)


@pytest.mark.asyncio
async def test_plugin_llm_command_uses_non_stream_llm_generate():
    from unittest.mock import MagicMock, AsyncMock

    import sys
    import importlib.util
    from pathlib import Path

    plugin_dir = Path(__file__).parent.parent
    spec = importlib.util.spec_from_file_location(
        "astrbot_plugin_gemini_reverse",
        str(plugin_dir / "main.py"),
        submodule_search_locations=[str(plugin_dir)]
    )
    main_mod = importlib.util.module_from_spec(spec)
    sys.modules["astrbot_plugin_gemini_reverse"] = main_mod
    spec.loader.exec_module(main_mod)
    GeminiReversePlugin = main_mod.GeminiReversePlugin

    context_mock = MagicMock()
    context_mock.provider_manager = MagicMock()
    context_mock.provider_manager.get_provider_by_id = AsyncMock(return_value=MagicMock())
    context_mock.provider_manager.create_provider = AsyncMock()
    context_mock.provider_manager.providers_config = []
    context_mock.provider_manager.delete_provider = AsyncMock()
    context_mock.get_using_provider = MagicMock(return_value=MagicMock(id="gemini_reverse/current", model="gemini-3-flash"))
    context_mock.llm_generate = AsyncMock(
        return_value=MagicMock(
            role="assistant",
            completion_text="probe ok",
            reasoning_content="",
            tools_call_name=[],
            tools_call_args=[],
        )
    )

    plugin = GeminiReversePlugin(context_mock, config={"managed_service": False})
    plugin.service_manager.start = AsyncMock()
    plugin.service_manager.stop = AsyncMock()
    await plugin._bootstrap_task

    event = MagicMock()
    event.message_str = "/gemini_reverse llm 你好 世界"
    event.plain_result = lambda text: text

    results = []
    async for item in plugin.gemini_reverse_command(event):
        results.append(item)

    assert len(results) == 1
    assert '"completion_text": "probe ok"' in results[0]
    assert '"provider_id": "gemini_reverse/current"' in results[0]
    assert '"model": "gemini-3-flash"' in results[0]
    context_mock.llm_generate.assert_awaited_once_with(
        chat_provider_id="gemini_reverse/current",
        prompt="你好 世界",
    )

    await plugin.terminate()
    sys.modules.pop("astrbot_plugin_gemini_reverse", None)
    sys.modules.pop("astrbot_plugin_gemini_reverse.main", None)


@pytest.mark.asyncio
async def test_plugin_llm_command_times_out_cleanly():
    from unittest.mock import MagicMock, AsyncMock

    import sys
    import importlib.util
    from pathlib import Path

    plugin_dir = Path(__file__).parent.parent
    spec = importlib.util.spec_from_file_location(
        "astrbot_plugin_gemini_reverse",
        str(plugin_dir / "main.py"),
        submodule_search_locations=[str(plugin_dir)]
    )
    main_mod = importlib.util.module_from_spec(spec)
    sys.modules["astrbot_plugin_gemini_reverse"] = main_mod
    spec.loader.exec_module(main_mod)
    GeminiReversePlugin = main_mod.GeminiReversePlugin

    context_mock = MagicMock()
    context_mock.provider_manager = MagicMock()
    context_mock.provider_manager.get_provider_by_id = AsyncMock(return_value=MagicMock())
    context_mock.provider_manager.create_provider = AsyncMock()
    context_mock.provider_manager.providers_config = []
    context_mock.provider_manager.delete_provider = AsyncMock()
    context_mock.get_using_provider = MagicMock(return_value=MagicMock(id="gemini_reverse/current", model="gemini-3-flash"))

    async def _hang_forever(*args, **kwargs):
        import asyncio
        await asyncio.sleep(3600)

    context_mock.llm_generate = AsyncMock(side_effect=_hang_forever)

    plugin = GeminiReversePlugin(context_mock, config={"managed_service": False})
    plugin.service_manager.start = AsyncMock()
    plugin.service_manager.stop = AsyncMock()
    await plugin._bootstrap_task

    event = MagicMock()
    event.message_str = "/gemini_reverse llm 你好"
    event.plain_result = lambda text: text

    import asyncio
    original_wait_for = asyncio.wait_for

    async def _fast_wait_for(awaitable, timeout):
        return await original_wait_for(awaitable, 0.01)

    with patch.object(main_mod.asyncio, "wait_for", side_effect=_fast_wait_for):
        results = []
        async for item in plugin.gemini_reverse_command(event):
            results.append(item)

    assert len(results) == 1
    assert '"error_type": "LLMProbeTimeout"' in results[0]
    assert '"provider_id": "gemini_reverse/current"' in results[0]

    await plugin.terminate()
    sys.modules.pop("astrbot_plugin_gemini_reverse", None)
    sys.modules.pop("astrbot_plugin_gemini_reverse.main", None)


@pytest.mark.asyncio
async def test_plugin_api_sync_probe_returns_local_response():
    from unittest.mock import MagicMock, AsyncMock

    import sys
    import importlib.util
    from pathlib import Path

    plugin_dir = Path(__file__).parent.parent
    spec = importlib.util.spec_from_file_location(
        "astrbot_plugin_gemini_reverse",
        str(plugin_dir / "main.py"),
        submodule_search_locations=[str(plugin_dir)]
    )
    main_mod = importlib.util.module_from_spec(spec)
    sys.modules["astrbot_plugin_gemini_reverse"] = main_mod
    spec.loader.exec_module(main_mod)
    GeminiReversePlugin = main_mod.GeminiReversePlugin

    context_mock = MagicMock()
    context_mock.provider_manager = MagicMock()
    context_mock.provider_manager.get_provider_by_id = AsyncMock(return_value=MagicMock())
    context_mock.provider_manager.create_provider = AsyncMock()
    context_mock.provider_manager.providers_config = []
    context_mock.provider_manager.delete_provider = AsyncMock()
    context_mock.get_using_provider = MagicMock(return_value=MagicMock(id="gemini_reverse/current", model="gemini-3-flash"))

    plugin = GeminiReversePlugin(context_mock, config={"managed_service": False})
    plugin.service_manager.start = AsyncMock()
    plugin.service_manager.stop = AsyncMock()
    await plugin._bootstrap_task

    event = MagicMock()
    event.unified_msg_origin = "test-origin"
    event.message_str = "/gemini_reverse api sync 你好"
    event.plain_result = lambda text: text

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "choices": [
            {
                "message": {"content": "hello sync"},
                "finish_reason": "stop",
            }
        ]
    }
    mock_response.raise_for_status = MagicMock()

    mock_client = MagicMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.post = AsyncMock(return_value=mock_response)

    with patch.object(main_mod.httpx, "AsyncClient", return_value=mock_client):
        results = []
        async for item in plugin.gemini_reverse_command(event):
            results.append(item)

    assert len(results) == 1
    assert '"mode": "sync"' in results[0]
    assert '"content": "hello sync"' in results[0]
    assert '"provider_id": "gemini_reverse/current"' in results[0]

    await plugin.terminate()
    sys.modules.pop("astrbot_plugin_gemini_reverse", None)
    sys.modules.pop("astrbot_plugin_gemini_reverse.main", None)
