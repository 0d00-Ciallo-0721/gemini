import pytest
from unittest.mock import AsyncMock, MagicMock
from reverse_runtime.auth_manager import AuthManager
from reverse_runtime.ticket_refresher import refresh_active_ticket

@pytest.mark.asyncio
async def test_refresh_active_ticket_success(tmp_path):
    manager = AuthManager(str(tmp_path), {"auth_mode": "relay_ticket", "accounts": {}})
    manager.store.save_active_ticket({
        "status": "healthy",
        "client_id": "test",
        "cookie_data": {"SECURE_1PSID": "old"}
    })
    
    mock_client = MagicMock()
    mock_client.init = AsyncMock(return_value=True)
    mock_client.cookies = {"__Secure-1PSID": "new", "extra": "data"}
    
    result = await refresh_active_ticket(manager, mock_client)
    assert result is True
    
    saved = manager.store.load_active_ticket()
    # Check that it updated the time and status
    assert saved["status"] == "healthy"
    # Check write-back of cookies
    assert saved["cookie_data"]["SECURE_1PSID"] == "new"
    assert saved["cookie_data"]["cookies_dict"]["extra"] == "data"

@pytest.mark.asyncio
async def test_refresh_active_ticket_failure_when_invalidated(tmp_path):
    manager = AuthManager(str(tmp_path), {"auth_mode": "relay_ticket", "accounts": {}})
    manager.store.save_active_ticket({
        "status": "invalidated",
    })
    
    mock_client = MagicMock()
    mock_client.init = AsyncMock(return_value=True)
    
    result = await refresh_active_ticket(manager, mock_client)
    assert result is False
    assert mock_client.init.call_count == 0
