# api_client.py
# Gemini 客户端连接层 — 含账号自动轮换

from gemini_webapi import GeminiClient
from gemini_webapi.exceptions import AuthError, UsageLimitExceeded

from .config import ACCOUNTS, PROXIES, get_current_credentials, state
from .logger import request_logger
from .exceptions import ProxyException, ModelNotSupportedError, AuthInvalidError, NetworkOrProxyError, GoogleSilentAbortError, UnknownUpstreamError, UpstreamQueueTimeoutError


class ContextMigrationNeeded(Exception):
    """当账号发生轮换，当前物理 ChatSession 失效时抛出的特定信号，通知上层进行全量上下文重建"""
    pass



def _map_upstream_error(e: Exception):
    if isinstance(e, ProxyException):
        return e
    err_str = str(e).lower()
    if isinstance(e, AuthError) or "token" in err_str or "cookie" in err_str:
        return AuthInvalidError(str(e))
    elif "model" in err_str and ("unknown" in err_str or "not found" in err_str or "invalid" in err_str):
        return ModelNotSupportedError(str(e))
    elif "aborted by google" in err_str or "silently aborted" in err_str:
        return GoogleSilentAbortError(str(e))
    elif "queue_timeout" in err_str:
        return UpstreamQueueTimeoutError(str(e))
    elif "connect" in err_str or "timeout" in err_str or "proxy" in err_str or "dns" in err_str:
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
        psid = acc_data.get("SECURE_1PSID", getattr(acc_data, "get", lambda k,d: d)("__Secure-1PSID", ""))
        psidts = acc_data.get("SECURE_1PSIDTS", getattr(acc_data, "get", lambda k,d: d)("__Secure-1PSIDTS", ""))
        # 2. 构建基础客户端实例
        if PROXIES:
            request_logger.log_info(f"代理就绪: proxy={PROXIES}  [当前配置模型: {state.active_model} | 当前活跃池: {state.active_account}]")
        else:
            request_logger.log_info(f"代理就绪: proxy=disabled  [当前配置模型: {state.active_model} | 当前活跃池: {state.active_account}]")
        
        self.client = GeminiClient(psid, psidts, proxy=PROXIES)
        
        # 3. 注入完整 Cookie 视图
        # 策略：除了传入核心的 1PSID，还要把其余的杂散会话 Cookie (.google.com) 一并灌入 httpx.Client。
        # 并在最后以强校验得到的 SECURE_1PSID 盖过可能劣化的旧值，确保会话指纹高度逼真且主键不受污染。
        if hasattr(self.client, "cookies") and isinstance(self.client.cookies, dict):
            cookie_dict = {"__Secure-1PSID": psid, "__Secure-1PSIDTS": psidts}
            real_cookies = acc_data.get("cookies_dict", {})
            for k, v in real_cookies.items():
                if k not in ("SECURE_1PSID", "SECURE_1PSIDTS", "__Secure-1PSID", "__Secure-1PSIDTS"):
                    cookie_dict[k] = v
            self.client.cookies.update(cookie_dict)

        try:
            # 👇 修复：大幅增加整体超时与看门狗超时，防止 Gemini 思考或处理长文本时被底层库强行掐断
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
                    from reverse_runtime.ticket_refresher import refresh_active_ticket
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
                    from reverse_runtime.ticket_refresher import refresh_active_ticket
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
                raise mapped_e



# 导出单例实例
gemini_conn = GeminiConnection()
