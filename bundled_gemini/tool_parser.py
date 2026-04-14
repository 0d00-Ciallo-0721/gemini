# tool_parser.py
# 工具调用解析引擎 — 移植自 web-anytool (toolify_xml 协议)
# 职责：从 Gemini 的自由文本输出中提取结构化的工具调用

import re
import json
import uuid
from dataclasses import dataclass, field


# ============================================================
# 1. 数据模型
# ============================================================

@dataclass
class ToolCall:
    """一次工具调用的结构化表示（与 OpenAI function_call 对齐）"""
    id: str
    name: str
    arguments: str  # JSON 字符串

    @staticmethod
    def create(name: str, arguments: str) -> "ToolCall":
        return ToolCall(
            id=f"call_{uuid.uuid4().hex[:24]}",
            name=name,
            arguments=arguments,
        )


@dataclass
class ParseResult:
    """批量解析结果"""
    text: str = ""                          # 非工具调用的文本
    tool_calls: list[ToolCall] = field(default_factory=list)

    @property
    def has_calls(self) -> bool:
        return len(self.tool_calls) > 0


@dataclass
class StreamEvent:
    """流式解码事件"""
    kind: str       # "text_delta" | "tool_call_finalized"
    text: str | None = None
    tool_call: ToolCall | None = None


# ============================================================
# 2. JSON 修复引擎
# ============================================================

# 未加引号的键名模式：{key: value} → {"key": value}
_UNQUOTED_KEY_RE = re.compile(r'([{,]\s*)([a-zA-Z_][a-zA-Z0-9_]*)\s*:')

# 缺少数组括号模式：识别 : {obj1}, {obj2} → : [{obj1}, {obj2}]
_MISSING_ARRAY_RE = re.compile(
    r'(:\s*)(\{(?:[^{}]|\{[^{}]*\})*\}(?:\s*,\s*\{(?:[^{}]|\{[^{}]*\})*\})+)'
)


def repair_invalid_backslashes(s: str) -> str:
    """修复 JSON 中的无效反斜杠（常见于 Windows 路径 C:\\Users）"""
    if '\\' not in s:
        return s

    valid_escapes = set('"\\\/bfnrt')
    out = []
    i = 0
    chars = list(s)
    while i < len(chars):
        if chars[i] == '\\':
            if i + 1 < len(chars):
                nxt = chars[i + 1]
                if nxt in valid_escapes:
                    out.append('\\')
                    out.append(nxt)
                    i += 2
                    continue
                elif nxt == 'u' and i + 5 < len(chars):
                    hex_part = s[i+2:i+6]
                    if all(c in '0123456789abcdefABCDEF' for c in hex_part):
                        out.append(s[i:i+6])
                        i += 6
                        continue
                # 无效反斜杠 → 双倍转义
                out.append('\\\\')
            else:
                out.append('\\\\')
        else:
            out.append(chars[i])
        i += 1
    return ''.join(out)


def repair_loose_json(s: str) -> str:
    """修复松散 JSON：未加引号的键名、缺少数组括号"""
    s = s.strip()
    if not s:
        return s
    s = _UNQUOTED_KEY_RE.sub(r'\1"\2":', s)
    s = _MISSING_ARRAY_RE.sub(r'\1[\2]', s)
    return s


def safe_json_parse(raw: str) -> dict | None:
    """【修改】尝试解析 JSON，失败则级联修复，最后使用紧急暴力提取"""
    raw = raw.strip()
    import re
    raw = re.sub(r'```$', '', raw).strip()
    
    if not raw:
        return None

    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass

    repaired = repair_literal_newlines(raw)
    try:
        return json.loads(repaired)
    except json.JSONDecodeError:
        pass

    repaired = repair_invalid_backslashes(repaired)
    try:
        return json.loads(repaired)
    except json.JSONDecodeError:
        pass

    repaired = repair_loose_json(repaired)
    try:
        return json.loads(repaired)
    except json.JSONDecodeError:
        pass

    # 【新增】接入终极物理提取防线
    emergency = _emergency_json_parse(raw)
    if emergency:
        print("⚠️ 触发紧急 JSON 暴力提取机制成功！")
        return emergency

    return None


def coerce_arguments(v) -> dict:
    """【修改】将各种形式的参数值强制转换为 dict，并清洗过度转义"""
    if v is None:
        return {}
    if isinstance(v, dict):
        return _cleanup_over_escaped_args(v)
    if isinstance(v, str):
        raw = v.strip()
        if not raw:
            return {}
        parsed = safe_json_parse(raw)
        if isinstance(parsed, dict):
            return _cleanup_over_escaped_args(parsed)
        return {"_raw": raw}
    # 其他类型 → 先序列化再反序列化
    try:
        dumped = json.dumps(v)
        parsed = json.loads(dumped)
        return _cleanup_over_escaped_args(parsed)
    except (TypeError, json.JSONDecodeError):
        return {}


# ============================================================
# 3. XML 解析正则表达式 (移植自 web-anytool toolify_xml.go)
# ============================================================

# 支持 4 种 XML 方言
_XML_TOOL_CALLS_BLOCK = re.compile(
    r'<tool_calls\b[^>]*>(.*?)</tool_calls>', re.DOTALL | re.IGNORECASE
)
_XML_TOOL_CALL_BLOCK = re.compile(
    r'<tool_call\b[^>]*>(.*?)</tool_call>', re.DOTALL | re.IGNORECASE
)
_XML_INVOKE = re.compile(
    r'<invoke\s+name="([^"]+)"\s*>(.*?)</invoke>', re.DOTALL | re.IGNORECASE
)
_XML_INVOKE_PARAM = re.compile(
    r'<parameter\s+name="([^"]+)"\s*>(.*?)</parameter>', re.DOTALL | re.IGNORECASE
)
_XML_TOOL_USE = re.compile(
    r'<tool_use\b[^>]*>(.*?)</tool_use>', re.DOTALL | re.IGNORECASE
)

# 【核心修改】极其宽容的标签匹配：LLM 可能会输出 </_name>, </params> 等拼写错误，甚至带空格
_XML_TOOL_NAME = re.compile(
    r'<tool_name\b[^>]*>(.*?)</(?:tool_name|name|_name|function_name)[^>]*>', re.DOTALL | re.IGNORECASE
)
_XML_FUNCTION_NAME = re.compile(
    r'<function_name\b[^>]*>(.*?)</(?:function_name|name|_name|tool_name)[^>]*>', re.DOTALL | re.IGNORECASE
)
_XML_PARAMETERS = re.compile(
    r'<parameters\b[^>]*>(.*?)</(?:parameters|params|args|arguments|_parameters)[^>]*>', re.DOTALL | re.IGNORECASE
)

# 流式检测用：是否包含 XML 工具标签开头
_XML_OPEN_TAG = re.compile(
    r'<(?:tool_calls?|tool_use|invoke|function_calls?)\b', re.IGNORECASE
)

# ============================================================
# 4. 批量解析器
# ============================================================

def _extract_name_from_json(payload: dict) -> str:
    """从 JSON 对象中提取工具名（兼容多种键名约定）"""
    for key in ("tool", "tool_name", "name"):
        val = payload.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return ""


def _extract_args_from_json(payload: dict) -> dict:
    """从 JSON 对象中提取参数（兼容多种键名约定）"""
    for key in ("params", "parameters", "arguments", "input"):
        if key in payload:
            return coerce_arguments(payload[key])
    return {}


def _parse_single_xml_block(inner: str, index: int) -> ToolCall | None:
    """解析单个 <tool_call> 块的内容"""
    trimmed = inner.strip()

    # 策略 1：内部是 JSON 载荷
    if trimmed.startswith('{'):
        parsed = safe_json_parse(trimmed)
        if isinstance(parsed, dict):
            name = _extract_name_from_json(parsed)
            if name:
                args = _extract_args_from_json(parsed)
                return ToolCall.create(name, json.dumps(args, ensure_ascii=False))

    # 策略 2：XML 子元素 <tool_name> + <parameters>
    name = ""
    m = _XML_TOOL_NAME.search(inner)
    if m:
        name = m.group(1).strip()
    if not name:
        m = _XML_FUNCTION_NAME.search(inner)
        if m:
            name = m.group(1).strip()
    if not name:
        return None

    args = {}
    m = _XML_PARAMETERS.search(inner)
    if m:
        raw_params = m.group(1).strip()
        if raw_params:
            args = coerce_arguments(raw_params)

    return ToolCall.create(name, json.dumps(args, ensure_ascii=False))


def _parse_xml_tool_call_blocks(text: str) -> list[ToolCall]:
    """提取所有 <tool_call> 块"""
    calls = []
    for i, m in enumerate(_XML_TOOL_CALL_BLOCK.finditer(text)):
        inner = m.group(1).strip()
        call = _parse_single_xml_block(inner, i)
        if call:
            calls.append(call)
    return calls


def _parse_xml_invoke_blocks(text: str) -> list[ToolCall]:
    """【修改】提取所有 <invoke name="x"> 块"""
    calls = []
    for i, m in enumerate(_XML_INVOKE.finditer(text)):
        name = m.group(1).strip()
        if not name:
            continue
        body = m.group(2)
        args = {}
        for pm in _XML_INVOKE_PARAM.finditer(body):
            k = pm.group(1).strip()
            v = pm.group(2).strip()
            if k:
                args[k] = v
        # 在生成工具调用前注入过度转义清洗
        args = _cleanup_over_escaped_args(args)
        calls.append(ToolCall.create(name, json.dumps(args, ensure_ascii=False)))
    return calls


def _parse_xml_tool_use_blocks(text: str) -> list[ToolCall]:
    """提取所有 <tool_use> 块"""
    calls = []
    for i, m in enumerate(_XML_TOOL_USE.finditer(text)):
        inner = m.group(1).strip()

        name = ""
        nm = _XML_TOOL_NAME.search(inner)
        if nm:
            name = nm.group(1).strip()
        if not name:
            nm = _XML_FUNCTION_NAME.search(inner)
            if nm:
                name = nm.group(1).strip()
        if not name:
            continue

        args = {}
        pm = _XML_PARAMETERS.search(inner)
        if pm:
            raw = pm.group(1).strip()
            if raw:
                args = coerce_arguments(raw)

        calls.append(ToolCall.create(name, json.dumps(args, ensure_ascii=False)))
    return calls


def _text_outside_pattern(full: str, pattern: re.Pattern) -> str:
    """去除所有匹配区域，返回剩余文本"""
    return pattern.sub(' ', full).strip()


def parse_tool_calls(text: str) -> ParseResult:
    """
    批量解析：从完整模型输出中提取所有工具调用。
    按优先级尝试 4 种 XML 方言。
    """
    result = ParseResult()
    
    # 修复：暴力清理模型可能输出的 Markdown 转义符
    text = text.replace(r'\<', '<').replace(r'\>', '>').replace(r'\_', '_').replace(r'\/', '/')
    
    trimmed = text.strip()

    if not trimmed:
        result.text = text
        return result
    # 1. <tool_calls> 包装器（含多个 <tool_call>）
    m = _XML_TOOL_CALLS_BLOCK.search(trimmed)
    if m:
        inner = m.group(1)
        calls = _parse_xml_tool_call_blocks(inner)
        if calls:
            result.tool_calls = calls
            # 提取 <tool_calls> 块之外的文本
            idx = trimmed.index(m.group(0))
            prefix = trimmed[:idx].strip()
            suffix = trimmed[idx + len(m.group(0)):].strip()
            result.text = (prefix + " " + suffix).strip()
            return result

    # 2. 独立的 <tool_call> 块
    calls = _parse_xml_tool_call_blocks(trimmed)
    if calls:
        result.tool_calls = calls
        result.text = _text_outside_pattern(trimmed, _XML_TOOL_CALL_BLOCK)
        return result

    # 3. <invoke> 风格
    calls = _parse_xml_invoke_blocks(trimmed)
    if calls:
        result.tool_calls = calls
        result.text = _text_outside_pattern(trimmed, _XML_INVOKE)
        return result

    # 4. <tool_use> 风格
    calls = _parse_xml_tool_use_blocks(trimmed)
    if calls:
        result.tool_calls = calls
        result.text = _text_outside_pattern(trimmed, _XML_TOOL_USE)
        return result

    # 没有工具调用
    result.text = text
    return result


# ============================================================
# 5. 流式解码器 (移植自 web-anytool stream/toolify_xml.go)
# ============================================================

# 可能是 XML 工具标签的部分前缀
_PARTIAL_TAG_PREFIXES = [
    "<t", "<to", "<too", "<tool", "<tool_", "<tool_c", "<tool_ca",
    "<tool_cal", "<tool_call", "<tool_calls", "<tool_u", "<tool_us",
    "<tool_use",
    "<f", "<fu", "<fun", "<func", "<funct", "<functi", "<functio",
    "<function", "<function_", "<function_c", "<function_ca",
    "<function_cal", "<function_call", "<function_calls",
    "<i", "<in", "<inv", "<invo", "<invok", "<invoke",
]

# 完整的 XML 标签对（按优先级排列）
_TAG_PAIRS = [
    ("<tool_calls", "</tool_calls>"),
    ("<tool_call", "</tool_call>"),
    ("<function_calls", "</function_calls>"),
    ("<function_call", "</function_call>"),
    ("<invoke", "</invoke>"),
    ("<tool_use", "</tool_use>"),
]


def _looks_like_partial_xml_tag(s: str) -> bool:
    """检查字符串是否像一个未完成的 XML 工具标签"""
    lower = s.lower()
    for prefix in _PARTIAL_TAG_PREFIXES:
        if lower == prefix or (lower.startswith(prefix) and len(lower) <= len(prefix) + 5):
            return True
    return False


def _xml_safe_prefix(text: str) -> str:
    """返回可以安全发射的文本前缀（不可能是标签开头的部分）"""
    last_lt = text.rfind('<')
    if last_lt < 0:
        return text
    after = text[last_lt:]
    if _looks_like_partial_xml_tag(after):
        return text[:last_lt] if last_lt > 0 else ""
    return text


class StreamToolDecoder:
    """
    流式工具调用增量解码器。
    逐 chunk 接收模型输出，实时分离普通文本和工具调用。
    """

    def __init__(self):
        self._buf = ""
        self._done = False
        self.had_calls = False  # 整个流是否产生过工具调用

    def push(self, chunk: str) -> list[StreamEvent]:
        """接收一个增量 chunk，返回事件列表"""
        if self._done:
            return []
            
        # 修复：在流式拼接前，实时清理转义字符
        chunk = chunk.replace(r'\<', '<').replace(r'\>', '>').replace(r'\_', '_').replace(r'\/', '/')
        
        self._buf += chunk
        return self._try_extract()


    def flush(self) -> list[StreamEvent]:
        """流结束时调用，最终化缓冲区"""
        if self._done:
            return []
        self._done = True

        text = self._buf
        self._buf = ""

        if not text.strip():
            return []

        events = self._extract_xml(text)
        if events:
            return events

        # 没有工具调用，发射剩余文本
        return [StreamEvent(kind="text_delta", text=text)]

    def reset(self):
        """重置状态供复用"""
        self._buf = ""
        self._done = False
        self.had_calls = False

    def _try_extract(self) -> list[StreamEvent]:
        """尝试从缓冲区中提取内容"""
        text = self._buf

        # 检查是否有 XML 工具标签开头
        if not _XML_OPEN_TAG.search(text):
            # 没有标签 → 发射安全前缀
            safe = _xml_safe_prefix(text)
            if not safe:
                return []
            self._buf = text[len(safe):]
            return [StreamEvent(kind="text_delta", text=safe)]

        # 检测到开口标签，尝试提取完整块
        events = self._extract_xml(text)
        if events:
            self._buf = ""
            return events

        # 开口标签但还没闭合 → 发射标签前的文本
        loc = _XML_OPEN_TAG.search(text)
        if loc and loc.start() > 0:
            prefix = text[:loc.start()]
            self._buf = text[loc.start():]
            return [StreamEvent(kind="text_delta", text=prefix)]

        # 继续缓冲
        return []

    def _extract_xml(self, text: str) -> list[StreamEvent]:
        """尝试匹配完整的 XML 标签对并提取工具调用"""
        lower = text.lower()

        for open_tag, close_tag in _TAG_PAIRS:
            open_idx = lower.find(open_tag)
            if open_idx < 0:
                continue
            close_idx = lower.find(close_tag, open_idx)
            if close_idx < 0:
                continue
            block_end = close_idx + len(close_tag)

            block = text[open_idx:block_end]
            result = parse_tool_calls(block)
            if not result.has_calls:
                continue

            self.had_calls = True
            events = []

            # 标签前的文本
            prefix = text[:open_idx]
            if prefix.strip():
                events.append(StreamEvent(kind="text_delta", text=prefix))

            # 工具调用事件
            for call in result.tool_calls:
                events.append(StreamEvent(kind="tool_call_finalized", tool_call=call))

            # 标签后的文本
            suffix = text[block_end:]
            if suffix.strip():
                events.append(StreamEvent(kind="text_delta", text=suffix))

            return events

        return []


def repair_literal_newlines(s: str) -> str:
    """【新增】修复 JSON 字符串内部非法的真实换行符"""
    out = []
    in_string = False
    escape = False
    for char in s:
        if char == '"' and not escape:
            in_string = not in_string
            out.append(char)
        elif char == '\\' and not escape:
            escape = True
            out.append(char)
        elif char == '\n':
            if in_string:
                out.append('\\n')  # 强制转义
            else:
                out.append(char)
            escape = False
        elif char == '\r':
            if not in_string:
                out.append(char)
            escape = False
        else:
            escape = False
            out.append(char)
    return ''.join(out)

def _cleanup_over_escaped_args(args: dict) -> dict:
    """【修改】深度清洗，增加对 shell 命令中 Markdown 链接的正则扒皮"""
    if not isinstance(args, dict):
        return args
        
    import re
    result = {}
    for k, v in args.items():
        if isinstance(v, dict):
            result[k] = _cleanup_over_escaped_args(v)
        elif isinstance(v, list):
            new_list = []
            for item in v:
                if isinstance(item, dict):
                    new_list.append(_cleanup_over_escaped_args(item))
                elif isinstance(item, str):
                    if k.lower() in ('url', 'link', 'href'):
                        urls = re.findall(r'(https?://[^\s\)\]">]+)', item)
                        if urls:
                            clean_url = urls[-1]
                            if clean_url.count('http') > 1:
                                clean_url = clean_url[clean_url.rfind('http'):]
                            item = clean_url
                            
                    if k in ('content', 'code', 'text', 'command', 'file_content'):
                        item = item.replace('\\n', '\n').replace('\\#', '#').replace('\\*', '*').replace('\\_', '_').replace('\\`', '`').replace('\\!', '!')
                        item = re.sub(r'(?:https?://[^\s\[]*\s*)?\[[^\]]*\]\((https?://[^\)]+)\)', r'\1', item)

                    new_list.append(item)
                else:
                    new_list.append(item)
            result[k] = new_list
        elif isinstance(v, str):
            if k.lower() in ('url', 'link', 'href'):
                urls = re.findall(r'(https?://[^\s\)\]">]+)', v)
                if urls:
                    clean_url = urls[-1] 
                    if clean_url.count('http') > 1:
                        clean_url = clean_url[clean_url.rfind('http'):]
                    v = clean_url
            
            if k in ('content', 'code', 'text', 'command', 'file_content'):
                v = v.replace('\\n', '\n').replace('\\#', '#').replace('\\*', '*').replace('\\_', '_').replace('\\`', '`').replace('\\!', '!')
                v = re.sub(r'(?:https?://[^\s\[]*\s*)?\[[^\]]*\]\((https?://[^\)]+)\)', r'\1', v)
            result[k] = v
        else:
            result[k] = v
    return result


def _emergency_json_parse(raw: str) -> dict | None:
    """【新增】最后的防线：当 JSON 彻底崩溃时，使用物理指针暴力提取已知关键字段"""
    result = {}
    # 定义常见工具的核心字段
    keys = ["filePath", "oldString", "newString", "command", "content", "url", "description", "query"]
    found = False
    
    for k in keys:
        import re
        # 寻找 "key" : " 的起始位置
        start_match = re.search(rf'"{k}"\s*:\s*"', raw)
        if not start_match:
            continue
        
        start_idx = start_match.end()
        end_idx = start_idx
        escaped = False
        
        # 逐字符向后寻找正确的闭合引号
        while end_idx < len(raw):
            char = raw[end_idx]
            if char == '\\' and not escaped:
                escaped = True
            elif char == '"' and not escaped:
                # 遇到了未转义的引号，判断其后是否紧跟逗号、大括号或空白符，以此确认是否为值结尾
                remainder = raw[end_idx+1:].strip()
                if remainder.startswith(',') or remainder.startswith('}') or remainder == '':
                    break
            else:
                escaped = False
            end_idx += 1
        
        if end_idx < len(raw):
            val = raw[start_idx:end_idx]
            # 简单清洗并反转义
            val = val.replace('\\"', '"').replace('\\n', '\n').replace('\\\\', '\\')
            result[k] = val
            found = True
            
    return result if found else None


def _emergency_json_parse(raw: str) -> dict | None:
    """【新增】终极防线：当 JSON 彻底崩溃时，使用正则物理指针暴力提取已知关键字段"""
    result = {}
    keys = ["filePath", "oldString", "newString", "command", "content", "url", "description", "query"]
    found = False
    
    for k in keys:
        import re
        start_match = re.search(rf'"{k}"\s*:\s*"', raw)
        if not start_match:
            continue
        
        start_idx = start_match.end()
        end_idx = start_idx
        escaped = False
        
        while end_idx < len(raw):
            char = raw[end_idx]
            if char == '\\' and not escaped:
                escaped = True
            elif char == '"' and not escaped:
                # 遇到了未转义的引号，判断其后是否紧跟逗号、大括号或空白符
                remainder = raw[end_idx+1:].strip()
                if remainder.startswith(',') or remainder.startswith('}') or remainder == '':
                    break
            else:
                escaped = False
            end_idx += 1
        
        if end_idx < len(raw):
            val = raw[start_idx:end_idx]
            val = val.replace('\\"', '"').replace('\\n', '\n').replace('\\\\', '\\')
            result[k] = val
            found = True
            
    return result if found else None
