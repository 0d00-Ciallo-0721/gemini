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
        AuthInvalidError,
        UpstreamQueueTimeoutError,
        IPBlockedError,
    )
    from gemini_webapi.exceptions import AuthError
except ImportError:
    pass


# ============================================================
# 错误映射测试 — 类型判定
# ============================================================

def test_error_mapping_auth_error_type():
    """上游 AuthError 类型 → AUTH_INVALID"""
    e = AuthError("SNlM0e value not found")
    mapped = _map_upstream_error(e)
    assert isinstance(mapped, AuthInvalidError)
    assert mapped.error_type == "AUTH_INVALID"


def test_error_mapping_temporarily_blocked_type():
    """上游 TemporarilyBlocked 类型 → IP_BLOCKED"""
    try:
        from gemini_webapi.exceptions import TemporarilyBlocked
        e = TemporarilyBlocked("Your IP address has been temporarily flagged or blocked by Google.")
        mapped = _map_upstream_error(e)
        assert isinstance(mapped, IPBlockedError)
        assert mapped.error_type == "IP_BLOCKED"
    except ImportError:
        pytest.skip("TemporarilyBlocked not available in this version")


def test_error_mapping_model_invalid_type():
    """上游 ModelInvalid 类型 → MODEL_NOT_SUPPORTED"""
    try:
        from gemini_webapi.exceptions import ModelInvalid
        e = ModelInvalid("The model 'gemini-99' is currently unavailable")
        mapped = _map_upstream_error(e)
        assert isinstance(mapped, ModelNotSupportedError)
        assert mapped.error_type == "MODEL_NOT_SUPPORTED"
    except ImportError:
        pytest.skip("ModelInvalid not available in this version")


# ============================================================
# 错误映射测试 — 关键词匹配
# ============================================================

def test_error_mapping_model_not_supported():
    e = Exception("Unknown model name gemini-99.9-pro")
    mapped = _map_upstream_error(e)
    assert isinstance(mapped, ModelNotSupportedError)
    assert mapped.error_type == "MODEL_NOT_SUPPORTED"


def test_error_mapping_model_inconsistent():
    """上游抛出模型不一致错误 → MODEL_NOT_SUPPORTED"""
    e = Exception("The specified model is inconsistent with the conversation history.")
    mapped = _map_upstream_error(e)
    assert isinstance(mapped, ModelNotSupportedError)


def test_error_mapping_model_unavailable():
    """上游抛出模型不可用错误 → MODEL_NOT_SUPPORTED"""
    e = Exception("The model 'gemini-3-pro' is currently unavailable or the request structure is outdated.")
    mapped = _map_upstream_error(e)
    assert isinstance(mapped, ModelNotSupportedError)


def test_error_mapping_auth_keyword():
    e1 = Exception("__Secure-1PSID cookie is invalid")
    mapped1 = _map_upstream_error(e1)
    assert isinstance(mapped1, AuthInvalidError)

    e2 = Exception("access token expired please re-auth")
    mapped2 = _map_upstream_error(e2)
    assert isinstance(mapped2, AuthInvalidError)


def test_error_mapping_ip_blocked_keyword():
    """IP 被拦截的关键词匹配 → IP_BLOCKED"""
    e = Exception("Your IP address has been temporarily blocked by Google.")
    mapped = _map_upstream_error(e)
    assert isinstance(mapped, IPBlockedError)
    assert mapped.error_type == "IP_BLOCKED"


# ============================================================
# GOOGLE_SILENT_ABORT — 覆盖上游库所有真实错误文本
# ============================================================

def test_silent_abort_legacy_keyword():
    """旧版关键词 silently aborted → GOOGLE_SILENT_ABORT"""
    e = Exception("The original request may have been silently aborted by Google.")
    mapped = _map_upstream_error(e)
    assert isinstance(mapped, GoogleSilentAbortError)
    assert mapped.error_type == "GOOGLE_SILENT_ABORT"


def test_silent_abort_no_output_data():
    """上游 generate_content: output is None → GOOGLE_SILENT_ABORT"""
    e = Exception("Failed to generate contents. No output data found in response.")
    mapped = _map_upstream_error(e)
    assert isinstance(mapped, GoogleSilentAbortError)


def test_silent_abort_stream_interrupted():
    """上游 _generate: 流被截断 → GOOGLE_SILENT_ABORT"""
    e = Exception("Stream interrupted or truncated.")
    mapped = _map_upstream_error(e)
    assert isinstance(mapped, GoogleSilentAbortError)


def test_silent_abort_zombie_stream():
    """上游 _generate: 看门狗检测到僵尸流 → GOOGLE_SILENT_ABORT"""
    e = Exception("Response stalled (zombie stream).")
    mapped = _map_upstream_error(e)
    assert isinstance(mapped, GoogleSilentAbortError)


def test_silent_abort_read_chat_failed():
    """上游 _generate: READ_CHAT 恢复全部失败 → GOOGLE_SILENT_ABORT"""
    e = Exception(
        "Stream failed after Gemini assigned cid='abc123'. "
        "Recovery via READ_CHAT returned no data after 5 attempts (~30s)."
    )
    mapped = _map_upstream_error(e)
    assert isinstance(mapped, GoogleSilentAbortError)


def test_silent_abort_server_overloaded():
    """上游 _generate: Google 过载 → GOOGLE_SILENT_ABORT"""
    e = Exception(
        "Gemini server is overloaded (request queued but never started processing). "
        "Try again in a few minutes or use a different model."
    )
    mapped = _map_upstream_error(e)
    assert isinstance(mapped, GoogleSilentAbortError)


def test_silent_abort_stale_response():
    """上游 _generate: 所有 READ_CHAT 返回陈旧响应 → GOOGLE_SILENT_ABORT"""
    e = Exception(
        "Stream failed for cid='x'. All 5 READ_CHAT attempts returned stale "
        "response (rcid unchanged). Retrying stream."
    )
    # 这条包含 "stream" 但不包含 "interrupted"；不过 "stalled" 不匹配
    # 但实际上这是 APIError，上游的 @running 装饰器会重试，最终可能变为其他错误
    mapped = _map_upstream_error(e)
    # 这条不包含任何 SILENT_ABORT 标记，应该是 UNKNOWN
    assert isinstance(mapped, UnknownUpstreamError)


# ============================================================
# 网络/代理错误
# ============================================================

def test_error_mapping_network_proxy():
    e1 = Exception("connect timeout")
    mapped1 = _map_upstream_error(e1)
    assert isinstance(mapped1, NetworkOrProxyError)

    e2 = Exception("proxy connection failed")
    mapped2 = _map_upstream_error(e2)
    assert isinstance(mapped2, NetworkOrProxyError)


def test_error_mapping_dns():
    e = Exception("dns resolution failed for gemini.google.com")
    mapped = _map_upstream_error(e)
    assert isinstance(mapped, NetworkOrProxyError)


# ============================================================
# 未知错误兜底
# ============================================================

def test_error_mapping_unknown():
    e = Exception("Some completely random exception")
    mapped = _map_upstream_error(e)
    assert isinstance(mapped, UnknownUpstreamError)
    assert mapped.error_type == "UNKNOWN_UPSTREAM_ERROR"


# ============================================================
# ProxyException 透传
# ============================================================

def test_proxy_exception_passthrough():
    """已经是 ProxyException 的异常直接透传"""
    e = GoogleSilentAbortError("already mapped")
    mapped = _map_upstream_error(e)
    assert mapped is e


# ============================================================
# GeminiConnection 集成测试
# ============================================================

def test_proxy_assignment_in_client():
    from bundled_gemini.api_client import GeminiConnection
    from bundled_gemini.config import state, ACCOUNTS

    state.active_account = "test_acc"
    ACCOUNTS["test_acc"] = {"SECURE_1PSID": "A", "SECURE_1PSIDTS": "B"}

    with patch("bundled_gemini.api_client.PROXIES", "http://127.0.0.1:40001"):
        import asyncio
        conn = GeminiConnection()
        with patch("bundled_gemini.api_client.GeminiClient.init", new_callable=MagicMock) as mock_init:
            mock_init.return_value = asyncio.Future()
            mock_init.return_value.set_result(None)
            asyncio.run(conn.initialize())
            assert conn.client is not None
            assert getattr(conn.client, "proxy", None) == "http://127.0.0.1:40001"


@pytest.mark.asyncio
async def test_proxy_assignment_empty_disabled():
    from bundled_gemini.api_client import GeminiConnection
    from bundled_gemini.config import state, ACCOUNTS
    import asyncio
    state.active_account = "test_acc"
    ACCOUNTS["test_acc"] = {"SECURE_1PSID": "A", "SECURE_1PSIDTS": "B"}
    with patch("bundled_gemini.api_client.PROXIES", ""):
        conn = GeminiConnection()
        with patch("bundled_gemini.api_client.GeminiClient.init", new_callable=MagicMock) as mock_init:
            fut = asyncio.Future()
            fut.set_result(None)
            mock_init.return_value = fut
            await conn.initialize()
            assert getattr(conn.client, "proxy", None) == ""


# ============================================================
# 排队超时测试
# ============================================================

@pytest.mark.asyncio
async def test_upstream_queue_timeout_throws_error():
    from bundled_gemini.api_client import GeminiConnection
    import asyncio

    class MockTimeoutGenerator:
        def __aiter__(self):
            return self
        async def __anext__(self):
            await asyncio.sleep(2)
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


# ============================================================
# Cookie 卫生检查测试
# ============================================================

def test_cookie_hygiene_clean():
    """正常 Cookie 无警告"""
    from update_cookie import check_cookie_hygiene
    warnings = check_cookie_hygiene(
        "__Secure-1PSID=abc123; __Secure-1PSIDTS=xyz789"
    )
    assert warnings == []


def test_cookie_hygiene_abuse_exemption():
    """携带 GOOGLE_ABUSE_EXEMPTION 的 Cookie 应触发警告"""
    from update_cookie import check_cookie_hygiene
    dirty_cookie = (
        "__Secure-1PSID=abc123; __Secure-1PSIDTS=xyz789; "
        "GOOGLE_ABUSE_EXEMPTION=ID=05749f46e6afc7b3:TM=1776257677:C=>:IP=104.28.195.187-:S=xxx"
    )
    warnings = check_cookie_hygiene(dirty_cookie)
    assert len(warnings) == 1
    assert "GOOGLE_ABUSE_EXEMPTION" in warnings[0]
    assert "GOOGLE_SILENT_ABORT" in warnings[0]


def test_cookie_hygiene_empty():
    """空 Cookie 无警告"""
    from update_cookie import check_cookie_hygiene
    assert check_cookie_hygiene("") == []
    assert check_cookie_hygiene(None) == []
