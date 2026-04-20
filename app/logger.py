import json
import os
import re
import threading
import hashlib
from collections import deque
from datetime import datetime

from . import config as config_mod


_runtime_config_provider = config_mod.get_runtime_config


def set_runtime_config_provider(provider):
    global _runtime_config_provider
    _runtime_config_provider = provider or config_mod.get_runtime_config


def get_runtime_config_snapshot():
    return _runtime_config_provider()


class RequestLogger:
    """
    底层代理通讯总线审计记录器。
    
    【分层通道指引】：
    - request: 大模型提示词请求和调用指标记录。
    - tool_calls: AstrBot 工具调用的解析轨迹。
    - auth: Relay Ticket 与手工号的轮换、刷新事件。
    - runtime: 引擎底层配置、探针、热启自恢复事件。
    - session: (暂未独立) 物理会话上下文重建与映射事件。
    
    【核心脱敏准则】：
    此类仅负责代理端运行时输出，不在日志中硬留原始请求的 prompt 巨量文本（仅留头部/尾部或长度），
    更不允许记录含有身份敏感的 1PSID/HTTP Headers。
    """

    def __init__(self, log_dir: str | None = None, memory_size: int = 50):
        self._lock = threading.Lock()
        self._memory: deque[dict] = deque(maxlen=memory_size)
        self._last_request: dict | None = None
        
        self._log_files = {}
        self._log_dir = ""
        self.reconfigure(log_dir or get_runtime_config_snapshot().get("log_dir") or "logs")

    def reconfigure(self, log_dir: str):
        with self._lock:
            for f in self._log_files.values():
                if f:
                    f.close()
            self._log_files.clear()
            self._log_dir = log_dir
            os.makedirs(log_dir, exist_ok=True)
            
            channels = ["tool_calls", "auth", "request", "runtime"]
            for ch in channels:
                try:
                    self._log_files[ch] = open(os.path.join(log_dir, f"{ch}.jsonl"), "a", encoding="utf-8")
                except OSError:
                    self._log_files[ch] = None

    def _write(self, entry: dict, channel: str = "tool_calls"):
        entry["time"] = datetime.now().isoformat()
        with self._lock:
            self._memory.append(entry)
            f = self._log_files.get(channel)
            if f:
                try:
                    f.write(json.dumps(entry, ensure_ascii=False) + "\n")
                    f.flush()
                except OSError:
                    pass

    def _payload_logging_enabled(self) -> bool:
        return bool(get_runtime_config_snapshot().get("debug_payload_logging", False))

    def _sanitize_text(self, text: str, *, limit: int = 2000) -> str:
        value = str(text or "")
        patterns = [
            (r"(?i)(authorization\s*:\s*bearer\s+)[^\s\"']+", r"\1***"),
            (r"(?i)(x-api-key\s*:\s*)[^\s\"']+", r"\1***"),
            (r"(?i)(x-admin-token\s*:\s*)[^\s\"']+", r"\1***"),
            (r"(?i)(__Secure-1PSID(?:TS)?=)[^;\s]+", r"\1***"),
            (r"(?i)(SECURE_1PSID(?:TS)?[\"']?\s*[:=]\s*[\"'])[^\"']+", r"\1***"),
            (r"(?i)(cookie\s*:\s*)[^\r\n]+", r"\1***"),
        ]
        for pattern, repl in patterns:
            value = re.sub(pattern, repl, value)
        return value[:limit]

    def log_request(self, messages: list, tools: list, prompt_text: str, has_tools: bool, model: str, account: str):
        entry = {
            "type": "request",
            "model": model,
            "account": account,
            "has_tools": has_tools,
            "tool_count": len(tools),
            "tool_names": [t.get("function", t).get("name", "?") for t in tools] if tools else [],
            "msg_count": len(messages),
            "prompt_chars": len(prompt_text),
            "msg_roles": [m.get("role", "?") for m in messages],
        }
        self._write(entry, channel="request")
        self._last_request = {
            "tool_count": len(tools),
            "model": model,
            "prompt_chars": len(prompt_text),
            "prompt_hash": hashlib.md5(prompt_text.encode("utf-8")).hexdigest(),
        }
        if self._payload_logging_enabled():
            self._last_request["prompt_text"] = self._sanitize_text(prompt_text[-5000:])

    def log_parse_result(self, raw_text: str, has_calls: bool, call_names: list[str], mode: str = "batch"):
        entry = {
            "type": "parse_result",
            "mode": mode,
            "raw_chars": len(raw_text),
            "has_calls": has_calls,
            "call_names": call_names,
        }
        if self._payload_logging_enabled():
            entry["raw_preview"] = self._sanitize_text(raw_text[:500], limit=500)
        self._write(entry, channel="tool_calls")
        if self._last_request:
            self._last_request["parse_has_calls"] = has_calls
            self._last_request["parse_call_names"] = call_names
            if self._payload_logging_enabled():
                self._last_request["raw_output"] = self._sanitize_text(raw_text[-5000:])

    def log_stream_event(self, event_kind: str, tool_name: str | None = None):
        entry = {"type": "stream_event", "kind": event_kind}
        if tool_name:
            entry["tool_name"] = tool_name
        self._write(entry, channel="tool_calls")

    def log_error(self, error: str, context: str = ""):
        channel = "auth" if context in ("auth", "switch") else "tool_calls" if context == "tool_calls" else "runtime"
        self._write({"type": "error", "error": str(error)[:500], "context": context}, channel=channel)

    def log_info(self, msg: str, context: str = ""):
        channel = "auth" if context in ("auth", "switch") else "tool_calls" if context == "tool_calls" else "runtime"
        self._write({"type": "info", "msg": str(msg)[:500], "context": context}, channel=channel)

    def log_account_switch(self, from_account: str, to_account: str, reason: str):
        self._write({"type": "account_switch", "from": from_account, "to": to_account, "reason": reason}, channel="auth")

    def get_last_request(self) -> dict | None:
        return self._last_request

    def get_recent_logs(self, count: int = 20) -> list[dict]:
        with self._lock:
            items = list(self._memory)
        return items[-count:]

    def close(self):
        with self._lock:
            for f in self._log_files.values():
                if f:
                    f.close()
            self._log_files.clear()


request_logger = RequestLogger()
