import pytest
from unittest.mock import patch, MagicMock

# Import the error mapping function
try:
    from bundled_gemini.api_client import _map_upstream_error
    from bundled_gemini.exceptions import (
        ModelNotSupportedError,
        GoogleSilentAbortError,
        NetworkOrProxyError,
        UnknownUpstreamError,
        AuthInvalidError
    )
    from gemini_webapi.exceptions import AuthError
except ImportError:
    pass

def test_error_mapping_model_not_supported():
    e = Exception("Unknown model name gemini-99.9-pro")
    mapped = _map_upstream_error(e)
    assert isinstance(mapped, ModelNotSupportedError)
    assert mapped.error_type == "MODEL_NOT_SUPPORTED"

def test_error_mapping_google_silent_abort():
    e = Exception("The original request may have been silently aborted by Google.")
    mapped = _map_upstream_error(e)
    assert isinstance(mapped, GoogleSilentAbortError)
    assert mapped.error_type == "GOOGLE_SILENT_ABORT"

def test_error_mapping_network_proxy():
    e1 = Exception("connect timeout")
    mapped1 = _map_upstream_error(e1)
    assert isinstance(mapped1, NetworkOrProxyError)
    
    e2 = Exception("proxy connection failed")
    mapped2 = _map_upstream_error(e2)
    assert isinstance(mapped2, NetworkOrProxyError)

def test_error_mapping_auth():
    e1 = AuthError("Token expired")
    mapped1 = _map_upstream_error(e1)
    assert isinstance(mapped1, AuthInvalidError)
    
    e2 = Exception("__Secure-1PSID cookie is invalid")
    mapped2 = _map_upstream_error(e2)
    assert isinstance(mapped2, AuthInvalidError)

def test_error_mapping_unknown():
    e = Exception("Some completely random exception")
    mapped = _map_upstream_error(e)
    assert isinstance(mapped, UnknownUpstreamError)
    assert mapped.error_type == "UNKNOWN_UPSTREAM_ERROR"

def test_proxy_assignment_in_client():
    from bundled_gemini.api_client import GeminiConnection
    from bundled_gemini.config import state, ACCOUNTS
    
    # Mock global state
    state.active_account = "test_acc"
    ACCOUNTS["test_acc"] = {"SECURE_1PSID": "A", "SECURE_1PSIDTS": "B"}
    
    with patch("bundled_gemini.api_client.PROXIES", "http://127.0.0.1:40001"):
        import asyncio
        conn = GeminiConnection()
        # Mock the underlying class to not actually make network calls during init
        with patch("bundled_gemini.api_client.GeminiClient.init", new_callable=MagicMock) as mock_init:
            mock_init.return_value = asyncio.Future()
            mock_init.return_value.set_result(None)
            
            asyncio.run(conn.initialize())
            assert conn.client is not None
            # Check if proxy was assigned
            assert getattr(conn.client, "proxy", None) == "http://127.0.0.1:40001"

@pytest.mark.asyncio
async def test_proxy_assignment_empty_disabled():
    from bundled_gemini.api_client import GeminiConnection
    from bundled_gemini.config import state, ACCOUNTS
    state.active_account = "test_acc"
    ACCOUNTS["test_acc"] = {"SECURE_1PSID": "A", "SECURE_1PSIDTS": "B"}
    with patch("bundled_gemini.api_client.PROXIES", ""):
        import asyncio
        conn = GeminiConnection()
        with patch("bundled_gemini.api_client.GeminiClient.init", new_callable=MagicMock) as mock_init:
            mock_init.return_value = asyncio.Future()
            mock_init.return_value.set_result(None)
            await conn.initialize()
            assert getattr(conn.client, "proxy", None) == ""

def test_google_silent_abort():
    from bundled_gemini.api_client import _map_upstream_error
    from bundled_gemini.exceptions import GoogleSilentAbortError
    e = Exception("The original request may have been silently aborted by Google.")
    mapped = _map_upstream_error(e)
    assert isinstance(mapped, GoogleSilentAbortError)
    assert mapped.error_type == "GOOGLE_SILENT_ABORT"

@pytest.mark.asyncio
async def test_upstream_queue_timeout_throws_error():
    from bundled_gemini.api_client import GeminiConnection
    from bundled_gemini.exceptions import UpstreamQueueTimeoutError
    import asyncio
    
    # Mock stream hitting timeout
    class MockTimeoutGenerator:
        def __aiter__(self):
            return self
        async def __anext__(self):
            await asyncio.sleep(2)  # longer than 0.1s
            return "FakeChunk"
            
    conn = GeminiConnection()
    conn.client = MagicMock()
    conn.client.generate_content_stream = MagicMock(return_value=MockTimeoutGenerator())
    
    with patch("bundled_gemini.config.RUNTIME_CONFIG", {"stream_first_chunk_timeout_sec": 0.1}):
        with pytest.raises(UpstreamQueueTimeoutError) as exc_info:
            async for chunk in conn.stream_with_failover("test prompt", "valid_model"):
                pass
        
        assert exc_info.value.error_type == "UPSTREAM_QUEUE_TIMEOUT"
        assert conn.last_request_error_type == "UPSTREAM_QUEUE_TIMEOUT"

@pytest.mark.asyncio
async def test_upstream_queue_timeout_successful_yield():
    from bundled_gemini.api_client import GeminiConnection
    import asyncio
    
    class MockSuccessGenerator:
        def __init__(self):
            self.i = 0
        def __aiter__(self):
            return self
        async def __anext__(self):
            if self.i == 0:
                self.i += 1
                await asyncio.sleep(0.01)
                return "FirstChunk"
            raise StopAsyncIteration
            
    conn = GeminiConnection()
    conn.client = MagicMock()
    conn.client.generate_content_stream = MagicMock(return_value=MockSuccessGenerator())
    
    with patch("bundled_gemini.config.RUNTIME_CONFIG", {"stream_first_chunk_timeout_sec": 0.5}):
        chunks = []
        async for chunk in conn.stream_with_failover("test prompt", "valid_model"):
            chunks.append(chunk)
            
        assert len(chunks) == 1
        assert chunks[0] == "FirstChunk"


@pytest.mark.asyncio
async def test_upstream_queue_idle_timeout_after_first_chunk():
    from bundled_gemini.api_client import GeminiConnection
    from bundled_gemini.exceptions import UpstreamQueueTimeoutError
    import asyncio

    class MockIdleAfterFirstChunkGenerator:
        def __init__(self):
            self.i = 0

        def __aiter__(self):
            return self

        async def __anext__(self):
            if self.i == 0:
                self.i += 1
                return "FirstChunk"
            await asyncio.sleep(2)
            return "LateChunk"

    conn = GeminiConnection()
    conn.client = MagicMock()
    conn.client.generate_content_stream = MagicMock(return_value=MockIdleAfterFirstChunkGenerator())

    with patch(
        "bundled_gemini.config.RUNTIME_CONFIG",
        {"stream_first_chunk_timeout_sec": 0.5, "stream_idle_timeout_sec": 0.1},
    ):
        chunks = []
        with pytest.raises(UpstreamQueueTimeoutError) as exc_info:
            async for chunk in conn.stream_with_failover("test prompt", "valid_model"):
                chunks.append(chunk)

        assert chunks == ["FirstChunk"]
        assert exc_info.value.error_type == "UPSTREAM_QUEUE_TIMEOUT"
        assert conn.last_request_error_type == "UPSTREAM_QUEUE_TIMEOUT"
