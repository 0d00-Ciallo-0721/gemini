import re
from pathlib import Path

# --- PATCH 1: Fix api_client.py Exception Catch blocks --- #
file_api = Path(r"c:\Users\zlj\Desktop\llm\gemini\bundled_gemini\api_client.py")
content_api = file_api.read_text(encoding="utf-8")

# Remove all bad injections
content_api = re.sub(
    r"\s+except Exception as e:\s+mapped_e = _map_upstream_error\(e\).*?raise mapped_e",
    "",
    content_api,
    flags=re.DOTALL
)

# Append broad exception catch correctly to the END of both generate_with_failover and stream_with_failover while loops
# The end is right after the `except AuthError:` block
def append_except(content, target):
    part1, sep, part2 = content.partition(target)
    new_except = """
            except Exception as e:
                mapped_e = _map_upstream_error(e)
                gemini_conn.last_request_error = str(e)
                gemini_conn.last_request_error_type = mapped_e.error_type
                raise mapped_e
"""
    return part1 + sep + new_except + part2

content_api = content_api.replace("switched = await self._switch_account(\"认证失效导致自动轮换兜底\")\n                if not switched:\n                    raise AuthError(\"系统已被降级但无其他存活备用账号可用！\")", 
                                "switched = await self._switch_account(\"认证失效导致自动轮换兜底\")\n                if not switched:\n                    raise AuthError(\"系统已被降级但无其他存活备用账号可用！\")\n            except Exception as e:\n                mapped_e = _map_upstream_error(e)\n                gemini_conn.last_request_error = str(e)\n                gemini_conn.last_request_error_type = mapped_e.error_type\n                raise mapped_e\n")

content_api = content_api.replace("switched = await self._switch_account(\"认证失败自动轮换\")\n                if not switched:\n                    raise AuthError(\"所有账号均不可用！\")\n                    \n                raise ContextMigrationNeeded(\"账号已自动轮换，当前物理窗口失效，请求全量重建。\")", 
                                "switched = await self._switch_account(\"认证失败自动轮换\")\n                if not switched:\n                    raise AuthError(\"所有账号均不可用！\")\n                    \n                raise ContextMigrationNeeded(\"账号已自动轮换，当前物理窗口失效，请求全量重建。\")\n\n            except Exception as e:\n                mapped_e = _map_upstream_error(e)\n                gemini_conn.last_request_error = str(e)\n                gemini_conn.last_request_error_type = mapped_e.error_type\n                raise mapped_e\n")

# --- PATCH 3: Enhance Proxy logging in api_client.py --- #
content_api = re.sub(
    r"request_logger\.log_info\(f\"代理就绪: proxy=\{PROXIES\}\"\)",
    'request_logger.log_info(f"代理就绪: proxy={PROXIES}\\n[当前配置模型: {state.active_model} | 当前活跃池: {state.active_account}]")',
    content_api
)
content_api = re.sub(
    r"request_logger\.log_info\(\"代理就绪: proxy=disabled\"\)",
    'request_logger.log_info(f"代理就绪: proxy=disabled\\n[当前配置模型: {state.active_model} | 当前活跃池: {state.active_account}]")',
    content_api
)
file_api.write_text(content_api, encoding="utf-8")


# --- PATCH 4 & 5: Update lifespan and error catching in main.py --- #
file_main = Path(r"c:\Users\zlj\Desktop\llm\gemini\bundled_gemini\main.py")
content_main = file_main.read_text(encoding="utf-8")

# Lifespan
lifespan_old = "print(f\"   账号池: {len(ACCOUNTS)} 个账号 ({state.active_account})\")"
lifespan_new = "from .config import PROXIES\n    print(f\"   账号池: {len(ACCOUNTS)} 个账号 ({state.active_account})\")\n    print(f\"   网络代理: {PROXIES if PROXIES else 'disabled'}\")"
content_main = content_main.replace(lifespan_old, lifespan_new)

# /v1/debug/network
content_main = content_main.replace('"is_client_initialized_with_proxy": is_client_set,', '"is_client_initialized_with_proxy": is_client_set,\n        "client_proxy_value": getattr(gemini_conn.client, "proxy", None),\n        "active_model": state.active_model,\n        "active_account": state.active_account,')

# Stream Error Catch log enhancement
stream_catch = """print(f"❌ 流式报错 [{error_type}]: {err_str}")
                    request_logger.log_error(f"[{error_type}] {err_str}\\n[模型: {state.active_model} | 代理: {PROXIES or 'disabled'}]", "stream")"""
content_main = re.sub(r"print\(f\"❌ 流式报错 \[\{error_type\}\]: \{err_str\}\"\).*?request_logger\.log_error\(f\"\[\{error_type\}\] \{err_str\}\", \"stream\"\)", stream_catch, content_main, flags=re.DOTALL)

# Sync Error Catch log enhancement
sync_catch = """request_logger.log_error(f"[{error_type}] {str(e)}\\n[模型: {state.active_model} | 代理: {PROXIES or 'disabled'}]", "sync")
                    print(f"❌ 非流式报错 [{error_type}]: {e}")"""
content_main = re.sub(r"request_logger\.log_error\(f\"\[\{error_type\}\] \{str\(e\)\}\", \"sync\"\).*?print\(f\"❌ 非流式报错 \[\{error_type\}\]: \{e\}\"\)", sync_catch, content_main, flags=re.DOTALL)

file_main.write_text(content_main, encoding="utf-8")

# --- PATCH 6 & 7: Update test_error_tracking.py --- #
file_test = Path(r"c:\Users\zlj\Desktop\llm\gemini\tests\test_error_tracking.py")
content_test = file_test.read_text(encoding="utf-8")

new_tests = """
def test_proxy_assignment_empty_disabled():
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
            asyncio.run(conn.initialize())
            assert getattr(conn.client, "proxy", None) == ""

def test_google_silent_abort():
    from bundled_gemini.api_client import _map_upstream_error
    from bundled_gemini.exceptions import GoogleSilentAbortError
    e = Exception("The original request may have been silently aborted by Google.")
    mapped = _map_upstream_error(e)
    assert isinstance(mapped, GoogleSilentAbortError)
    assert mapped.error_type == "GOOGLE_SILENT_ABORT"
"""
file_test.write_text(content_test + new_tests, encoding="utf-8")

print("All patches applied!")
