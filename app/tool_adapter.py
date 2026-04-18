# tool_adapter.py
# Prompt 注入层 — 将 OpenAI tools[] 定义渲染为 Gemini 能理解的提示词
# 含 tool_choice 支持、Prompt 压缩、上下文裁剪

import io
import json
import base64
import tempfile
import os
import mimetypes
import re
from functools import lru_cache


TOOL_CALLING_SYSTEM_PROMPT = """
# Tool Use Instructions

You have access to a set of tools. You may call tools to assist you in responding to the user.

## How to call tools

When you need to call a tool, output exactly this XML format:

<tool_call>
<tool_name>TOOL_NAME</tool_name>
<parameters>
{"param1": "value1", "param2": "value2"}
</parameters>
</tool_call>

### Rules:
1. The parameters value MUST be a valid JSON object.
2. You can call multiple tools in a single response by including multiple <tool_call> blocks.
3. If you do NOT need to call any tool, respond normally with plain text. Do NOT wrap normal text in <tool_call> tags.
4. Always call a tool when the task requires reading, writing, or executing something on the user's system.
5. After calling a tool, STOP and wait for the tool result. Do NOT continue writing after the tool call unless you are providing additional text before the call.
6. For file paths on Windows, always use double backslashes (e.g., "C:\\\\Users\\\\file.py") in JSON arguments.
7. CRITICAL: Do NOT escape XML tags. Use strict `<tool_call>` without any backslashes (e.g., NEVER output `\<tool_call\>`).
8. CRITICAL: DO NOT write any explanations or text AFTER the `</tool_call>` tag. You MUST stop generating text immediately to wait for the tool execution result.
9. CRITICAL: When writing code or Markdown content inside JSON parameters, use standard single escaping for newlines (\\n). Do NOT double-escape (NEVER use \\\\n). Do NOT escape Markdown formatting symbols (use `#`, NEVER use `\\#`).
10. CRITICAL: NEVER use Markdown link formatting (e.g., `[text](url)`) inside tool arguments. For URLs or paths, provide the raw plain string ONLY (e.g., "https://example.com").
11. CRITICAL (WINDOWS ENVIRONMENT): You are operating in a Windows PowerShell environment. Use standard PowerShell commands. DO NOT use Linux-exclusive tools like `rg` or `grep` (use `Select-String` instead). NEVER wrap URLs in brackets like `[url]` or `https[url](url)` when using curl or webfetch.
12. CRITICAL (JSON STRICTNESS): When using `edit` or `bash` with multi-line code, your JSON MUST be perfectly valid. You MUST properly escape all double quotes (`\\"`) inside `oldString` and `newString`.
13. CRITICAL (ACTION-ORIENTED): When fetching or reading installation guides, DO NOT summarize the content. You must immediately use the `bash`, `edit`, or `write` tools to execute the installation steps on the user's machine. Be a doer, not a talker.
## Available Tools
""".strip()

def _tools_hash(tools: list[dict]) -> str:
    names = tuple(t.get("function", t).get("name", "?") for t in tools)
    return str(names)

_prompt_cache: dict[str, str] = {}
_CACHE_MAX = 32

def render_tools_prompt(tools: list[dict], max_total_chars: int = 0) -> str:
    if not tools:
        return ""
    cache_key = _tools_hash(tools)
    if cache_key in _prompt_cache and max_total_chars == 0:
        return _prompt_cache[cache_key]

    sections = [TOOL_CALLING_SYSTEM_PROMPT, ""]
    for tool_def in tools:
        fn = tool_def.get("function", tool_def)
        name = fn.get("name", "unknown")
        desc = fn.get("description", "")
        params = fn.get("parameters", {})

        sections.append(f"### {name}")
        if desc:
            if max_total_chars > 0 and len(desc) > 200:
                desc = desc[:200] + "..."
            sections.append(f"Description: {desc}")

        properties = params.get("properties", {})
        required = set(params.get("required", []))

        if properties:
            sections.append("Parameters:")
            for pname, pinfo in properties.items():
                ptype = pinfo.get("type", "any")
                pdesc = pinfo.get("description", "")
                is_req = "required" if pname in required else "optional"
                line = f"- `{pname}` ({ptype}, {is_req})"
                if pdesc:
                    if max_total_chars > 0 and len(pdesc) > 100:
                        pdesc = pdesc[:100] + "..."
                    line += f": {pdesc}"
                sections.append(line)

            if any(p.get("type") in ("object", "array") for p in properties.values()):
                schema_str = json.dumps(params, indent=2)
                if max_total_chars == 0 or len(schema_str) < 500:
                    sections.append(f"Full JSON Schema: ```json\n{schema_str}\n```")
        else:
            sections.append("Parameters: none")
        sections.append("")

    result = "\n".join(sections)
    if max_total_chars > 0 and len(result) > max_total_chars:
        result = _render_ultra_compact(tools)

    if len(_prompt_cache) >= _CACHE_MAX:
        keys = list(_prompt_cache.keys())
        for k in keys[:len(keys)//2]:
            del _prompt_cache[k]
    _prompt_cache[cache_key] = result
    return result

def _render_ultra_compact(tools: list[dict]) -> str:
    lines = [TOOL_CALLING_SYSTEM_PROMPT, ""]
    for tool_def in tools:
        fn = tool_def.get("function", tool_def)
        name = fn.get("name", "unknown")
        params = fn.get("parameters", {})
        properties = params.get("properties", {})
        required = set(params.get("required", []))
        param_list = ", ".join(f"{p}{'*' if p in required else ''}" for p in properties)
        lines.append(f"- {name}({param_list})")
    return "\n".join(lines)

def render_tool_result(tool_call_id: str, name: str, content: str) -> str:
    return (
        f"[Tool Result for {name} (id: {tool_call_id})]\n"
        f"{content}\n"
        f"[End Tool Result]"
    )

def render_assistant_tool_calls(tool_calls: list[dict]) -> str:
    parts = []
    for tc in tool_calls:
        fn = tc.get("function", {})
        name = fn.get("name", "")
        arguments = fn.get("arguments", "{}")
        parts.append(
            f"<tool_call>\n"
            f"<tool_name>{name}</tool_name>\n"
            f"<parameters>\n{arguments}\n</parameters>\n"
            f"</tool_call>"
        )
    return "\n".join(parts)

def _build_tool_choice_suffix(tool_choice, tools: list[dict]) -> str:
    if not tool_choice or tool_choice == "auto":
        return "Now respond to the user's latest message. If you need to use a tool, output the <tool_call> XML block. If no tool is needed, respond with plain text only.\n"
    elif tool_choice == "none":
        return "Now respond to the user's latest message with plain text only. Do NOT use any tools.\n"
    elif tool_choice == "required":
        return "Now respond to the user's latest message. You MUST call at least one tool using the <tool_call> XML format. Do NOT respond with text only.\n"
    elif isinstance(tool_choice, dict):
        forced_name = tool_choice.get("function", {}).get("name", "")
        if forced_name:
            return f"Now respond to the user's latest message. You MUST call the tool '{forced_name}' using the <tool_call> XML format.\n"
    return "Now respond to the user's latest message. If you need to use a tool, output the <tool_call> XML block.\n"

def _truncate_tool_results(prompt: str, max_chars: int) -> str:
    """智能上下文截断机制：绝对保护 System Rules，强制裁切早期对话"""
    if len(prompt) <= max_chars:
        return prompt

    system_rules = ""
    import re
    sys_match = re.match(r'(==== SYSTEM RULES ====\n.*?\n======================\n\n)', prompt, re.DOTALL)
    
    if sys_match:
        system_rules = sys_match.group(1)
        prompt = prompt[len(system_rules):] 
        
    remaining_limit = max_chars - len(system_rules)
    
    # 👇 核心漏洞修复：即使系统规则极其庞大导致 remaining_limit 变为负数，
    # 也必须给近期的对话保留至少 4000 字符的底线空间，并强制执行截断！
    if remaining_limit < 4000:
        remaining_limit = 4000
        
    if len(prompt) > remaining_limit:
        overflow = len(prompt) - remaining_limit
        prompt = f"[Earlier context truncated ({overflow} chars)]\n\n" + prompt[overflow:]
        
    return system_rules + prompt

def build_tool_aware_prompt(messages: list, tools: list,
                            tool_choice=None,
                            max_prompt_chars: int = 40000,
                            start_index: int = 0) -> tuple[str, list]:
    prompt_text = ""
    extracted_files = []

    # 差分标识：只要 start_index > 0，说明是在同一个物理窗口接续对话
    is_delta = start_index > 0

    compress_limit = 8000 if len(tools) > 20 else 0
    tool_prompt = render_tools_prompt(tools, max_total_chars=compress_limit) if tools else ""

    if tool_choice == "none":
        tool_prompt = ""

    system_injected = False
    tool_call_args_map = {}
    tool_call_name_map = {}  # 👇 新增：用来存储 tool_call_id 到工具名称的映射

    for msg in messages:
        role = msg.get("role", "user")
        if role == "assistant" and msg.get("tool_calls"):
            for tc in msg.get("tool_calls"):
                tc_id = tc.get("id", "unknown")
                tool_call_args_map[tc_id] = tc.get("function", {}).get("arguments", "{}")
                tool_call_name_map[tc_id] = tc.get("function", {}).get("name", "unknown") # 👇 记录名称

    # 2. 差分遍历：只提取从 start_index 开始的新增消息
    for msg in messages[start_index:]:
        role = msg.get("role", "user")
        content = msg.get("content", "") or ""

        if role == "system":
            prompt_text += f"==== SYSTEM RULES ====\n{content}\n"
            if tool_prompt and not system_injected:
                prompt_text += f"\n{tool_prompt}\n"
                system_injected = True
            prompt_text += "======================\n\n"

        elif role == "user":
            prompt_text += "👤 User:\n"
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
                            with os.fdopen(fd, 'wb') as f:
                                f.write(image_bytes)
                            extracted_files.append(path)
            else:
                prompt_text += content + "\n"
            prompt_text += "\n"

        elif role == "assistant":
            tool_calls_list = msg.get("tool_calls", [])
            if tool_calls_list:
                prompt_text += "🤖 Assistant (You):\n"
                prompt_text += render_assistant_tool_calls(tool_calls_list) + "\n\n"
            elif content:
                prompt_text += f"🤖 Assistant (You):\n{content}\n\n"

        elif role == "tool":
            tool_call_id = msg.get("tool_call_id", "unknown")
            # 👇 核心修复：如果 msg 中没有 name，就通过 tool_call_id 去历史记录里找！
            tool_name = msg.get("name") or tool_call_name_map.get(tool_call_id, "unknown")
            
            # 现在，这里的判断就能 100% 成功命中了！
            if tool_name in ["read", "read_file", "glob", "grep", "cat"]:
                args_str = tool_call_args_map.get(tool_call_id, "{}")
                target_filename = f"output_{tool_name}_{tool_call_id}.txt"
                try:
                    args_dict = json.loads(args_str)
                    target_path = args_dict.get("filePath") or args_dict.get("path") or args_dict.get("file")
                    if target_path:
                        target_filename = os.path.basename(target_path)
                except Exception:
                    pass
                    
                import tempfile
                temp_dir = tempfile.mkdtemp()
                actual_file_path = os.path.join(temp_dir, target_filename)
                
                with open(actual_file_path, "w", encoding="utf-8") as f:
                    f.write(content)
                    
                extracted_files.append(actual_file_path)
                
                content = f"[SYSTEM NOTIFICATION: The full content of `{target_filename}` has been extracted and uploaded as a FILE ATTACHMENT. Please check the attached files to read its complete content.]"
                prompt_text += render_tool_result(tool_call_id, tool_name, content) + "\n\n"
                
            else:
                if len(content) > 4000:
                    head = content[:2000]
                    tail = content[-2000:]
                    content = (
                        f"{head}\n\n"
                        f"... [SYSTEM WARNING: The output was too long and has been truncated by {len(content) - 4000} characters. "
                        f"DO NOT try to read the entire file or directory at once! Please use tools to `grep` specific patterns, "
                        f"read specific line ranges, or narrow down your search.] ...\n\n"
                        f"{tail}"
                    )
                prompt_text += render_tool_result(tool_call_id, tool_name, content) + "\n\n"

    # 3. 如果是全新会话 (非差分)，才注入系统级的 Prompt 规则
    if not is_delta and tool_prompt and not system_injected:
        prompt_text = f"==== SYSTEM RULES ====\n{tool_prompt}\n======================\n\n" + prompt_text

    if tools and not is_delta:
        prompt_text += _build_tool_choice_suffix(tool_choice, tools)
    elif not is_delta:
        prompt_text += "Please carefully follow the SYSTEM RULES above and output your next response:\n"

    # 4. 差分模式下如果只有极少量的文字，为了防止模型迷失，增加一句上下文过渡引导
    if is_delta:
        prompt_text += "\n[SYSTEM: Continue the current task based on the new updates above:]\n"

    # 👇 修复点：将 max_prompt_chars=max_prompt_chars 修正为 max_chars=max_prompt_chars
    prompt_text = _truncate_tool_results(prompt_text, max_chars=max_prompt_chars)
    
    return prompt_text, extracted_files