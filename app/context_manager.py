# context_manager.py

import base64
import mimetypes
import os
import tempfile

from .services.runtime_services import build_runtime_services


class ChatContextManager:
    def _get_runtime_services(self):
        return build_runtime_services()

    async def process_commands(self, text: str):
        text = text.strip()
        if not text.startswith("/"):
            return False, None

        parts = text.split()
        cmd = parts[0].lower()

        try:
            services = self._get_runtime_services()
            state = services.state
            accounts = services.accounts
            if cmd == "/account":
                if len(parts) > 1 and parts[1] in accounts:
                    state.active_account = parts[1]
                    success, msg = await services.gemini_conn.initialize()
                    if success:
                        return True, f"👤 身份切换成功！当前使用: `账号 {state.active_account}`"
                    return True, f"❌ 账号切换失败: {msg}"
                return True, "⚠️ 请指定有效的账号编号，例如 `/account 1`"

            if cmd in ["/model", "/models"]:
                if len(parts) > 1:
                    state.active_model = parts[1]
                    return True, f"🧠 语言模型已切换为: `{state.active_model}`"
                return True, "📳 **可用模型**:\n- `gemini-3.1-pro`\n- `gemini-3.0-flash`\n👉 输入 `/model <代号>` 切换。"

            if cmd == "/help":
                return True, (
                    "🛠️ **指令**:\n"
                    "`/account <1|2>` : 切换账号\n"
                    "`/model <代号>` : 切换模型\n\n"
                    f"**状态**: 账号 `{state.active_account}` | 模型 `{state.active_model}`"
                )
        except Exception as e:
            return True, f"❌ 指令执行失败: {e}"

        return False, None

    def build_stateless_prompt(self, messages: list):
        """将多轮对话拍平为单次理解提示。"""
        prompt_text = ""
        extracted_files = []

        for msg in messages:
            role = msg.get("role", "user")
            content = msg.get("content", "") or ""

            if role == "system":
                prompt_text += f"==== SYSTEM RULES ====\n{content}\n======================\n\n"
            elif role == "user":
                prompt_text += "👁 User:\n"
                if isinstance(content, list):
                    for part in content:
                        if part.get("type") == "text":
                            prompt_text += part.get("text", "") + "\n"
                        elif part.get("type") == "image_url":
                            image_url = part["image_url"]["url"]
                            if "base64," in image_url:
                                ext = ".png"
                                if image_url.startswith("data:"):
                                    mime_part = image_url.split(";base64,")[0]
                                    mime_type = mime_part[5:]
                                    ext = mimetypes.guess_extension(mime_type) or ".png"

                                base64_str = image_url.split("base64,")[1]
                                image_bytes = base64.b64decode(base64_str)

                                fd, path = tempfile.mkstemp(suffix=ext)
                                with os.fdopen(fd, "wb") as f:
                                    f.write(image_bytes)
                                extracted_files.append(path)
                else:
                    prompt_text += content + "\n"
                prompt_text += "\n"
            elif role == "assistant":
                tool_calls = msg.get("tool_calls", [])
                if tool_calls:
                    prompt_text += "🤻 Assistant (You):\n"
                    for tc in tool_calls:
                        fn = tc.get("function", {})
                        prompt_text += (
                            "<tool_call>\n"
                            f"<tool_name>{fn.get('name', '')}</tool_name>\n"
                            f"<parameters>\n{fn.get('arguments', '{}')}\n</parameters>\n"
                            "</tool_call>\n"
                        )
                    prompt_text += "\n"
                elif content:
                    prompt_text += f"🤻 Assistant (You):\n{content}\n\n"
            elif role == "tool":
                tool_call_id = msg.get("tool_call_id", "unknown")
                tool_name = msg.get("name", "unknown")
                prompt_text += (
                    f"[Tool Result for {tool_name} (id: {tool_call_id})]\n"
                    f"{content}\n"
                    "[End Tool Result]\n\n"
                )

        prompt_text += "Please carefully follow the SYSTEM RULES above and output your next response:\n"
        return prompt_text.strip(), extracted_files


context_manager = ChatContextManager()
