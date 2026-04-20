import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from bundled_gemini.api_client import GeminiConnection

@pytest.mark.asyncio
async def test_api_client_initialize_full_cookies():
    conn = GeminiConnection()
    
    mock_config_data = {
        "SECURE_1PSID": "mock_psid",
        "SECURE_1PSIDTS": "mock_psidts",
        "raw_cookie": "meta=data",
        "cookie": "meta=data",
        "cookies_dict": {
            "OTHER_COOKIE": "extra_value"
        }
    }
    
    with patch("bundled_gemini.config.get_current_account_data", return_value=mock_config_data):
        with patch("bundled_gemini.api_client.GeminiClient") as MockClientType:
            mock_client_instance = MagicMock()
            mock_client_instance.cookies = {}
            mock_client_instance.init = AsyncMock(return_value=(True, ""))
            MockClientType.return_value = mock_client_instance
            
            success, msg = await conn.initialize()
            
            assert msg == "Success"
            assert success is True
            # Standalone/bundled current strategy only initializes with PSID/PSIDTS + proxy.
            # Full cookies stay in config for doctor/hygiene diagnostics and are not injected.
            args, kwargs = MockClientType.call_args
            assert args[:2] == ("mock_psid", "mock_psidts")
            assert "proxy" in kwargs
            assert mock_client_instance.cookies == {}
            
@pytest.mark.asyncio
async def test_api_client_refresh_fallback():
    conn = GeminiConnection()
    
    with patch("bundled_gemini.api_client.state") as mock_state:
        # simulate relay_active
        mock_state.active_account = "relay_active"
        
        # mock refresh trigger
        with patch("bundled_gemini.config.AUTH_MANAGER") as auth_mgr:
            with patch("reverse_runtime.ticket_refresher.refresh_active_ticket", new_callable=AsyncMock) as mock_refresh:
                mock_refresh.return_value = True
                
                conn.client = MagicMock()
                # 第一次返回异常触发 refresh_active_ticket，第二次返回成功
                mock_generate = AsyncMock()
                mock_generate.side_effect = [
                    __import__("gemini_webapi.exceptions", fromlist=["AuthError"]).AuthError("Token invalid test"), 
                    "success result"
                ]
                conn.client.generate_content = mock_generate
                conn.initialize = AsyncMock(return_value=(True, ""))
                
                with patch("bundled_gemini.api_client.ACCOUNTS", {"relay_active": {}}):
                    with patch("bundled_gemini.config.reload_runtime_config"):
                        res = await conn.generate_with_failover("test prompt", "gemini-1.5-pro")
                        
                        assert res == "success result"
                        assert mock_refresh.call_count == 1
                        assert mock_generate.call_count == 2
                        
@pytest.mark.asyncio
async def test_api_client_refresh_fallback_failure():
    conn = GeminiConnection()
    
    with patch("bundled_gemini.api_client.state") as mock_state:
        # simulate relay_active
        mock_state.active_account = "relay_active"
        
        # mock refresh trigger
        with patch("bundled_gemini.config.AUTH_MANAGER") as auth_mgr:
            with patch("reverse_runtime.ticket_refresher.refresh_active_ticket", new_callable=AsyncMock) as mock_refresh:
                mock_refresh.return_value = False # refresh fails
                
                conn.client = MagicMock()
                mock_generate = AsyncMock()
                
                AuthError = __import__("gemini_webapi.exceptions", fromlist=["AuthError"]).AuthError
                mock_generate.side_effect = [AuthError("Token invalid test")]
                conn.client.generate_content = mock_generate
                conn._switch_account = AsyncMock(return_value=False) # fallback also fails
                
                with patch("bundled_gemini.api_client.ACCOUNTS", {"relay_active": {}, "backup": {}}):
                    try:
                        await conn.generate_with_failover("test prompt", "gemini-1.5-pro")
                    except AuthError:
                        pass
                        
                    assert mock_refresh.call_count == 1
                    assert mock_generate.call_count == 1
                    assert conn._switch_account.call_count == 1
