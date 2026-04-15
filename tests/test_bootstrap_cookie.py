import pytest
from unittest.mock import patch, MagicMock, AsyncMock
import sys
import importlib.util
from pathlib import Path

@pytest.fixture
def clean_gemini_plugin():
    plugin_dir = Path(__file__).parent.parent
    spec = importlib.util.spec_from_file_location(
        "astrbot_plugin_gemini_reverse", 
        str(plugin_dir / "main.py"),
        submodule_search_locations=[str(plugin_dir)]
    )
    main_mod = importlib.util.module_from_spec(spec)
    sys.modules["astrbot_plugin_gemini_reverse"] = main_mod
    sys.modules["astrbot_plugin_gemini_reverse.main"] = main_mod
    spec.loader.exec_module(main_mod)
    GeminiReversePlugin = main_mod.GeminiReversePlugin

    context_mock = MagicMock()
    context_mock.provider_manager = MagicMock()
    context_mock.provider_manager.get_provider_by_id = AsyncMock(return_value=None)
    context_mock.provider_manager.create_provider = AsyncMock()
    
    yield GeminiReversePlugin, context_mock

    sys.modules.pop("astrbot_plugin_gemini_reverse", None)
    sys.modules.pop("astrbot_plugin_gemini_reverse.main", None)


@pytest.mark.asyncio
async def test_bootstrap_cookie_initial_import(clean_gemini_plugin, tmp_path):
    GeminiReversePlugin, context_mock = clean_gemini_plugin
    
    config = {
        "managed_service": False,
        "bootstrap_cookie": "SECURE_1PSID=val1; SECURE_1PSIDTS=val2;"
    }
    
    with patch("astrbot_plugin_gemini_reverse.reverse_runtime.session_bridge.get_plugin_data_dir", return_value=(tmp_path / "plugin")):
        plugin = GeminiReversePlugin(context_mock, config=config)
        plugin.service_manager.start = AsyncMock()
        plugin.service_manager.stop = AsyncMock()
        
        await plugin._bootstrap_task
        
        # 验证是否写入 active ticket
        ticket = plugin.auth_manager.store.load_active_ticket()
        assert ticket is not None, "Bootstrap cookie should be imported as active ticket"
        assert ticket["status"] == "healthy"
        assert ticket["client_id"] == "bootstrap"
        assert ticket["bootstrap_source"] == config["bootstrap_cookie"]
        assert ticket["cookie_data"]["SECURE_1PSID"] == "val1"
        
        await plugin.terminate()


@pytest.mark.asyncio
async def test_bootstrap_cookie_no_reimport_if_unchanged_but_reimports_if_changed(clean_gemini_plugin, tmp_path):
    GeminiReversePlugin, context_mock = clean_gemini_plugin
    
    config1 = {
        "managed_service": False,
        "bootstrap_cookie": "SECURE_1PSID=v1; SECURE_1PSIDTS=t1;"
    }
    
    with patch("astrbot_plugin_gemini_reverse.reverse_runtime.session_bridge.get_plugin_data_dir", return_value=(tmp_path / "plugin")):
        # 第1次启动
        plugin1 = GeminiReversePlugin(context_mock, config=config1)
        plugin1.service_manager.start = AsyncMock()
        await plugin1._bootstrap_task
        
        ticket1 = plugin1.auth_manager.store.load_active_ticket()
        assert ticket1["cookie_data"]["SECURE_1PSID"] == "v1"
        
        # 模拟经过一段时间，ticket的内容被刷新过
        ticket1["last_refresh_time"] = 99999999
        plugin1.auth_manager.store.save_active_ticket(ticket1)
        await plugin1.terminate()
        
        # 第2次启动，用户未修改过 bootstrap_cookie
        plugin2 = GeminiReversePlugin(context_mock, config=config1)
        plugin2.service_manager.start = AsyncMock()
        await plugin2._bootstrap_task
        
        ticket2 = plugin2.auth_manager.store.load_active_ticket()
        # 应该是我们改过后的那个，说明没有被重复拦截写入覆盖
        assert ticket2["last_refresh_time"] == 99999999
        await plugin2.terminate()
        
        # 第3次启动，用户重填了 / 填入了新的 bootstrap_cookie
        config3 = {
            "managed_service": False,
            "bootstrap_cookie": "SECURE_1PSID=v2; SECURE_1PSIDTS=t2;"
        }
        plugin3 = GeminiReversePlugin(context_mock, config=config3)
        plugin3.service_manager.start = AsyncMock()
        await plugin3._bootstrap_task
        
        ticket3 = plugin3.auth_manager.store.load_active_ticket()
        assert ticket3["cookie_data"]["SECURE_1PSID"] == "v2"
        assert ticket3["last_refresh_time"] != 99999999 # 验证确实被新值覆盖
        await plugin3.terminate()
