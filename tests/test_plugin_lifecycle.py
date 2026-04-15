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
