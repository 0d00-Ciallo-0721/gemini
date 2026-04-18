# api_client.py
# Gemini 客户端连接层 — 含账号自动轮换

from gemini_webapi import GeminiClient
from gemini_webapi.exceptions import AuthError, UsageLimitExceeded

# 可选的上游异常类型（防御性导入，避免版本差异导致 ImportError）
try:
    from gemini_webapi.exceptions import TemporarilyBlocked as _TemporarilyBlocked
except ImportError:
    _TemporarilyBlocked = type(None)
try:
    from gemini_webapi.exceptions import ModelInvalid as _ModelInvalid
except ImportError:
    _ModelInvalid = type(None)

from .config import ACCOUNTS, PROXIES, get_current_credentials, state
from .logger import request_logger
from .exceptions import (
    ProxyException, ModelNotSupportedError, AuthInvalidError,
    NetworkOrProxyError, GoogleSilentAbortError, UnknownUpstreamError,
    UpstreamQueueTimeoutError, IPBlockedError,
)


class ContextMigrationNeeded(Exception):
    """当账号发生轮换，当前物理 ChatSession 失效时抛出的特定信号，通知上层进行全量上下文重建"""
    pass



# 上游 gemini_webapi 实际异常文本 → GOOGLE_SILENT_ABORT 的匹配关键词
_SILENT_ABORT_MARKERS = (
    "aborted by google", "silently aborted",
    # generate_content: 非流式请求成功但 Google 未返回任何内容
    "no output data found", "no data found in response",
    # _generate (stream): 流中途断裂或被截断
    "stream interrupted", "truncated",
    # _generate (stream): 看门狗检测到活连接但无进展
    "zombie stream", "response stalled",
    # _generate (stream): READ_CHAT 恢复尝试全部失败
    "read_chat returned no data",
    # _generate (stream): Google 过载，请求入队但从未开始处理
    "never started processing", "server is overloaded",
)


def _map_upstream_error(e: Exception):
    """将上游 gemini_webapi 的原始异常映射为结构化的 ProxyException 子类。
    
    映射优先级：类型判定 > 关键词匹配。
    覆盖上游库 client.py 中 _generate / generate_content 的所有 raise 路径。
    """
    if isinstance(e, ProxyException):
        return e
    err_str = str(e).lower()

    # ── 1. 类型优先判定 ──
    if isinstance(e, AuthError):
        return AuthInvalidError(str(e))
    if isinstance(e, _TemporarilyBlocked):
        return IPBlockedError(str(e))
    if isinstance(e, _ModelInvalid):
        return ModelNotSupportedError(str(e))

    # ── 2. 关键词匹配 ──
    # Auth（字符串层面兜底）
    if "token" in err_str or "cookie" in err_str:
        return AuthInvalidError(str(e))
    # Model
    if "model" in err_str and any(w in err_str for w in ("unknown", "not found", "invalid", "inconsistent", "unavailable")):
        return ModelNotSupportedError(str(e))
    # IP 被 Google 风控拦截
    if "temporarily blocked" in err_str or "temporarily flagged" in err_str:
        return IPBlockedError(str(e))
    # Google 静默丢弃 — 覆盖上游库所有 "请求被无声吞掉" 的真实错误文本
    if any(marker in err_str for marker in _SILENT_ABORT_MARKERS):
        return GoogleSilentAbortError(str(e))
    # 排队超时
    if "queue_timeout" in err_str:
        return UpstreamQueueTimeoutError(str(e))
    # 网络 / 代理
    if any(w in err_str for w in ("connect", "timeout", "proxy", "dns")):
        return NetworkOrProxyError(str(e))

    return UnknownUpstreamError(str(e))

class GeminiConnection:
    """封装底层的 Gemini 客户端连接，支持账号池自动轮换"""

    def __init__(self):
        self.client: GeminiClient = None
        self.last_request_error = None
        self.last_request_error_type = None
        self.last_refresh_result = None

    async def initialize(self):
        """初始化网络连接和身份验证"""
        from .config import get_current_account_data

        # 1. 提取当前核心身份标识
        acc_data = get_current_account_data()
        psid = acc_data.get("SECURE_1PSID", acc_data.get("__Secure-1PSID", ""))
        psidts = acc_data.get("SECURE_1PSIDTS", acc_data.get("__Secure-1PSIDTS", ""))

        # 2. 构建基础客户端实例（对齐备份版：仅 PSID/PSIDTS + proxy）
        if PROXIES:
            request_logger.log_info(f"代理就绪: proxy={PROXIES}  [模型: {state.active_model} | 账号: {state.active_account}]")
        else:
            request_logger.log_info(f"代理就绪: proxy=disabled  [模型: {state.active_model} | 账号: {state.active_account}]")

        self.client = GeminiClient(psid, psidts, proxy=PROXIES)

        # 3. 不注入整包 cookies_dict（对齐备份版成功路径）
        # 完整浏览器 Cookie 集 + 非浏览器请求行为的组合容易触发 Google WAF
        # cookies_dict 仅用于 doctor 诊断和卫生检查，不参与实际请求构造

        try:
            await self.client.init(timeout=300, watchdog_timeout=300)
            request_logger.log_info(f"Account {state.active_account} initialized smoothly!")
            return True, "Success"
        except AuthError:
            err_msg = f"Account {state.active_account} token expired or invalid."
            request_logger.log_error(err_msg, context="auth")
            self.client = None
            return False, err_msg
        except Exception as e:
            err_msg = f"Init failed: {e}"
            request_logger.log_error(err_msg, context="init")
            self.client = None
            return False, err_msg

    async def close(self):
        """安全关闭连接"""
        if self.client:
            await self.client.close()
            self.client = None

    async def _switch_account(self, reason: str) -> bool:
        """切换到下一个可用账号"""
        current = state.active_account
        account_ids = sorted(ACCOUNTS.keys())
        current_idx = account_ids.index(current) if current in account_ids else 0

        for offset in range(1, len(account_ids)):
            next_idx = (current_idx + offset) % len(account_ids)
            next_id = account_ids[next_idx]

            request_logger.log_info(f"Switching to account {next_id} (reason: {reason})...")
            request_logger.log_account_switch(current, next_id, reason)

            state.active_account = next_id
            success, msg = await self.initialize()
            if success:
                request_logger.log_info(f"Switched to account {next_id} successfully!")
                return True
            request_logger.log_error(f"Account {next_id} unavailable: {msg}", context="switch")

        # 所有账号都失败了，恢复原账号
        state.active_account = current
        return False

    async def generate_with_failover(self, prompt: str, model: str,
                                     files=None, stream: bool = False, chat=None):
        """
        带账号自动轮换的请求方法。
        额度耗尽时自动切换到下一个账号重试。
        """
        from .config import AUTH_MANAGER
        
        tried_accounts = set()

        while True:
            tried_accounts.add(state.active_account)

            try:
                if not self.client:
                    success, msg = await self.initialize()
                    if not success:
                        raise ConnectionError(f"客户端初始化失败: {msg}")

                if stream:
                    return self.client.generate_content_stream(
                        prompt, model=model, files=files, chat=chat
                    )
                else:
                    return await self.client.generate_content(
                        prompt, model=model, files=files, chat=chat
                    )

            except UsageLimitExceeded:
                request_logger.log_error(
                    f"账号 {state.active_account} 额度耗尽",
                    context="generate_with_failover"
                )

                if len(tried_accounts) >= len(ACCOUNTS):
                    raise UsageLimitExceeded("所有账号额度已耗尽！请更新账号或等待重置。")

                switched = await self._switch_account("额度耗尽自动轮换")
                if not switched:
                    raise UsageLimitExceeded("所有账号均不可用！")

            except AuthError:
                request_logger.log_error(f"账号 {state.active_account} 认证失败", context="generate_with_failover")
                
                # -----------------------------------------------------------
                # Refresh-First 控制流：遇到权限异常，先尝试抢救（长期饭票特权）
                # -----------------------------------------------------------
                if state.active_account == "relay_active" and getattr(self, "_refresh_tried", False) is False and AUTH_MANAGER:
                    from runtime.ticket_refresher import refresh_active_ticket
                    self._refresh_tried = True
                    request_logger.log_error("正在尝试刷新 Relay 长期饭票...", context="auth")
                    
                    refreshed = await refresh_active_ticket(AUTH_MANAGER, self.client)
                    self.last_refresh_result = refreshed
                    if refreshed:
                        AUTH_MANAGER.set_fallback_state(False)
                        from .config import reload_runtime_config
                        reload_runtime_config() # 刷新本地配置以同步最新的 active_account
                        request_logger.log_error("刷新成功，携带热态票据重新下发请求...", context="auth")
                        continue # retry current request
                    
                # -----------------------------------------------------------
                # Fallback 控制流：抢救失败，进入死亡降级，尝试轮换账号
                # -----------------------------------------------------------
                if len(tried_accounts) >= len(ACCOUNTS):
                    raise AuthError("全部可用认证均已失效！请手工或通过 Helper 补充 Cookie。")

                if getattr(self, "_refresh_tried", False):
                    self._refresh_tried = False

                switched = await self._switch_account("认证失效导致自动轮换兜底")
                if not switched:
                    raise AuthError("系统已被降级但无其他存活备用账号可用！")
            except Exception as e:
                mapped_e = _map_upstream_error(e)
                self.last_request_error = str(e)
                self.last_request_error_type = mapped_e.error_type

                # GOOGLE_SILENT_ABORT: 关闭当前连接，重新初始化后重试一次
                # 对于 IP 被风控 (IPBlockedError) 不重试，因为根因是代理 IP 而非临时故障
                if isinstance(mapped_e, GoogleSilentAbortError) and not getattr(self, '_silent_abort_retried', False):
                    self._silent_abort_retried = True
                    request_logger.log_error(
                        f"[GOOGLE_SILENT_ABORT] 上游静默丢弃，关闭连接后重试... (原始: {str(e)[:200]})",
                        context="generate_with_failover"
                    )
                    await self.close()
                    continue
                self._silent_abort_retried = False
                raise mapped_e


    async def stream_with_failover(self, prompt: str, model: str, files=None, chat=None):
        """
        专为流式设计的带账号轮换生成方法，支持物理会话维持。
        """
        from .config import AUTH_MANAGER
        tried_accounts = set()

        while True:
            tried_accounts.add(state.active_account)
            try:
                if not self.client:
                    success, msg = await self.initialize()
                    if not success:
                        raise ConnectionError(f"客户端初始化失败: {msg}")

                import asyncio
                from .config import RUNTIME_CONFIG
                
                stream_iter = self.client.generate_content_stream(prompt, model=model, files=files, chat=chat)
                if hasattr(stream_iter, "__aiter__"):
                    aiter = stream_iter.__aiter__()
                else:
                    aiter = stream_iter
                
                queue_timeout = RUNTIME_CONFIG.get("stream_first_chunk_timeout_sec", 45)
                idle_timeout = RUNTIME_CONFIG.get("stream_idle_timeout_sec", queue_timeout)
                
                try:
                    # 强验证首包
                    first_chunk = await asyncio.wait_for(aiter.__anext__(), timeout=queue_timeout)
                    yield first_chunk
                except asyncio.TimeoutError:
                    err_msg = f"queue_timeout: 超过首包最长等待时限 ({queue_timeout}s)，Google 长时间排队或静默。请求被终止。"
                    request_logger.log_error(f"[{state.active_model}] 上游请求首包超时: {err_msg}", "stream")
                    raise UpstreamQueueTimeoutError(err_msg)
                except StopAsyncIteration:
                    return # 对方直接关门了
                
                # 延续正常遍历模式
                while True:
                    try:
                        chunk = await asyncio.wait_for(aiter.__anext__(), timeout=idle_timeout)
                        yield chunk
                    except asyncio.TimeoutError:
                        err_msg = (
                            f"queue_timeout: 首包后超过最大空闲等待时限 ({idle_timeout}s)，"
                            "Google 长时间排队或静默。请求被终止。"
                        )
                        request_logger.log_error(
                            f"[{state.active_model}] 上游请求流式空闲超时: {err_msg}",
                            "stream",
                        )
                        raise UpstreamQueueTimeoutError(err_msg)
                    except StopAsyncIteration:
                        break
                        
                return

            except UsageLimitExceeded:
                request_logger.log_error(f"账号 {state.active_account} 额度耗尽", context="stream")
                if len(tried_accounts) >= len(ACCOUNTS):
                    raise UsageLimitExceeded("所有账号额度已耗尽！请更新账号或等待重置。")
                switched = await self._switch_account("额度耗尽自动轮换")
                if not switched:
                    raise UsageLimitExceeded("所有账号均不可用！")
                
                raise ContextMigrationNeeded("账号已自动轮换，当前物理窗口失效，请求全量重建。")

            except AuthError:
                request_logger.log_error(f"账号 {state.active_account} 认证失败", context="stream")
                
                # Try refresh
                if state.active_account == "relay_active" and getattr(self, "_stream_refresh_tried", False) is False and AUTH_MANAGER:
                    from runtime.ticket_refresher import refresh_active_ticket
                    self._stream_refresh_tried = True
                    refreshed = await refresh_active_ticket(AUTH_MANAGER, self.client)
                    self.last_refresh_result = refreshed
                    if refreshed:
                        AUTH_MANAGER.set_fallback_state(False)
                        request_logger.log_error("流式生成期间刷新长期饭票成功，继续当前逻辑会话。", context="stream")
                        from .config import reload_runtime_config
                        reload_runtime_config()
                        continue # Re-try without losing Context!
                        
                if len(tried_accounts) >= len(ACCOUNTS):
                    raise AuthError("所有账号认证均失败！请更新 Cookie。")
                    
                if getattr(self, "_stream_refresh_tried", False):
                    self._stream_refresh_tried = False
                    
                switched = await self._switch_account("认证失败自动轮换")
                if not switched:
                    raise AuthError("所有账号均不可用！")
                    
                raise ContextMigrationNeeded("账号已自动轮换，当前物理窗口失效，请求全量重建。")

            except Exception as e:
                mapped_e = _map_upstream_error(e)
                self.last_request_error = str(e)
                self.last_request_error_type = mapped_e.error_type

                # GOOGLE_SILENT_ABORT: 关闭当前连接，重新初始化后重试一次
                if isinstance(mapped_e, GoogleSilentAbortError) and not getattr(self, '_stream_silent_abort_retried', False):
                    self._stream_silent_abort_retried = True
                    request_logger.log_error(
                        f"[GOOGLE_SILENT_ABORT] 流式生成期间上游静默丢弃，关闭连接后重试...",
                        context="stream"
                    )
                    await self.close()
                    continue
                self._stream_silent_abort_retried = False
                raise mapped_e



# 导出单例实例
gemini_conn = GeminiConnection()
