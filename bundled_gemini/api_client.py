# api_client.py
# Gemini 客户端连接层 — 含账号自动轮换

from gemini_webapi import GeminiClient
from gemini_webapi.exceptions import AuthError, UsageLimitExceeded

from .config import ACCOUNTS, PROXIES, get_current_credentials, state
from .logger import request_logger

class ContextMigrationNeeded(Exception):
    """当账号发生轮换，当前物理 ChatSession 失效时抛出的特定信号，通知上层进行全量上下文重建"""
    pass


class GeminiConnection:
    """封装底层的 Gemini 客户端连接，支持账号池自动轮换"""

    def __init__(self):
        self.client: GeminiClient = None

    async def initialize(self):
        """初始化网络连接和身份验证"""
        psid, psidts = get_current_credentials()
        self.client = GeminiClient(psid, psidts, proxy=PROXIES)

        try:
            # 👇 修复：大幅增加整体超时与看门狗超时，防止 Gemini 思考或处理长文本时被底层库强行掐断
            await self.client.init(timeout=300, watchdog_timeout=300)
            print(f"✅ 账号 {state.active_account} 鉴权并初始化成功！")
            return True, "成功"
        except AuthError:
            err_msg = f"❌ 账号 {state.active_account} 的令牌已过期或无效！请更新 config.py 或切换账号。"
            print(err_msg)
            self.client = None
            return False, err_msg
        except Exception as e:
            err_msg = f"❌ 初始化失败: {e}"
            print(err_msg)
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

            print(f"🔄 正在切换到账号 {next_id} (原因: {reason})...")
            request_logger.log_account_switch(current, next_id, reason)

            state.active_account = next_id
            success, msg = await self.initialize()
            if success:
                print(f"✅ 自动切换到账号 {next_id} 成功！")
                return True
            print(f"⚠️ 账号 {next_id} 不可用: {msg}")

        # 所有账号都失败了，恢复原账号
        state.active_account = current
        return False

    async def generate_with_failover(self, prompt: str, model: str,
                                     files=None, stream: bool = False, chat=None):
        """
        带账号自动轮换的请求方法。
        额度耗尽时自动切换到下一个账号重试。
        """
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

                # 尝试切换账号
                if len(tried_accounts) >= len(ACCOUNTS):
                    raise UsageLimitExceeded("所有账号额度已耗尽！请更新账号或等待重置。")

                switched = await self._switch_account("额度耗尽自动轮换")
                if not switched:
                    raise UsageLimitExceeded("所有账号均不可用！")
                # 切换成功后循环重试

            except AuthError:
                request_logger.log_error(
                    f"账号 {state.active_account} 认证失败",
                    context="generate_with_failover"
                )

                if len(tried_accounts) >= len(ACCOUNTS):
                    raise AuthError("所有账号认证均失败！请更新 Cookie。")

                switched = await self._switch_account("认证失败自动轮换")
                if not switched:
                    raise AuthError("所有账号均不可用！")

    async def stream_with_failover(self, prompt: str, model: str, files=None, chat=None):
        """
        专为流式设计的带账号轮换生成方法，支持物理会话维持。
        """
        tried_accounts = set()

        # 仅进行首次尝试，如果不成功且发生轮换，则抛出迁移信号交由外层处理
        tried_accounts.add(state.active_account)
        try:
            if not self.client:
                success, msg = await self.initialize()
                if not success:
                    raise ConnectionError(f"客户端初始化失败: {msg}")

            # 核心：将外层维护的 chat (ChatSession) 传入底层，维持物理窗口不新建
            async for chunk in self.client.generate_content_stream(prompt, model=model, files=files, chat=chat):
                yield chunk
            
            return  # 成功完成流式输出，安全退出

        except UsageLimitExceeded:
            request_logger.log_error(f"账号 {state.active_account} 额度耗尽", context="stream")
            if len(tried_accounts) >= len(ACCOUNTS):
                raise UsageLimitExceeded("所有账号额度已耗尽！请更新账号或等待重置。")
            switched = await self._switch_account("额度耗尽自动轮换")
            if not switched:
                raise UsageLimitExceeded("所有账号均不可用！")
            
            # 轮换成功，旧的 chat 对象已对新账号失效，立刻通知外层重建
            raise ContextMigrationNeeded("账号已自动轮换，当前物理窗口失效，请求全量重建。")

        except AuthError:
            request_logger.log_error(f"账号 {state.active_account} 认证失败", context="stream")
            if len(tried_accounts) >= len(ACCOUNTS):
                raise AuthError("所有账号认证均失败！请更新 Cookie。")
            switched = await self._switch_account("认证失败自动轮换")
            if not switched:
                raise AuthError("所有账号均不可用！")
                
            raise ContextMigrationNeeded("账号已自动轮换，当前物理窗口失效，请求全量重建。")


# 导出单例实例
gemini_conn = GeminiConnection()
