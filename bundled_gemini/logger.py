import json
import os
import threading
from collections import deque
from datetime import datetime

from .config import get_runtime_config


class RequestLogger:
    """请求日志记录器，写入 JSONL + 内存缓存最近 N 条。"""

    def __init__(self, log_dir: str | None = None, memory_size: int = 50):
        self._lock = threading.Lock()
        self._memory: deque[dict] = deque(maxlen=memory_size)
        self._last_request: dict | None = None
        self._log_file = None
        self._log_dir = ""
        self.reconfigure(log_dir or get_runtime_config().get("log_dir") or "logs")

    def reconfigure(self, log_dir: str):
        with self._lock:
            if self._log_file:
                self._log_file.close()
                self._log_file = None
            self._log_dir = log_dir
            os.makedirs(log_dir, exist_ok=True)
            log_path = os.path.join(log_dir, "tool_calls.jsonl")
            try:
                self._log_file = open(log_path, "a", encoding="utf-8")
            except OSError:
                self._log_file = None

    def _write(self, entry: dict):
        entry["time"] = datetime.now().isoformat()
        with self._lock:
            self._memory.append(entry)
            if self._log_file:
                try:
                    self._log_file.write(json.dumps(entry, ensure_ascii=False) + "\n")
                    self._log_file.flush()
                except OSError:
                    pass

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
        self._write(entry)
        self._last_request = {
            "prompt_text": prompt_text[-5000:],
            "tool_count": len(tools),
            "model": model,
        }

    def log_parse_result(self, raw_text: str, has_calls: bool, call_names: list[str], mode: str = "batch"):
        entry = {
            "type": "parse_result",
            "mode": mode,
            "raw_chars": len(raw_text),
            "has_calls": has_calls,
            "call_names": call_names,
            "raw_preview": raw_text[:500],
        }
        self._write(entry)
        if self._last_request:
            self._last_request["raw_output"] = raw_text[-5000:]
            self._last_request["parse_has_calls"] = has_calls
            self._last_request["parse_call_names"] = call_names

    def log_stream_event(self, event_kind: str, tool_name: str | None = None):
        entry = {"type": "stream_event", "kind": event_kind}
        if tool_name:
            entry["tool_name"] = tool_name
        self._write(entry)

    def log_error(self, error: str, context: str = ""):
        self._write({"type": "error", "error": str(error)[:500], "context": context})

    def log_account_switch(self, from_account: str, to_account: str, reason: str):
        self._write({"type": "account_switch", "from": from_account, "to": to_account, "reason": reason})

    def get_last_request(self) -> dict | None:
        return self._last_request

    def get_recent_logs(self, count: int = 20) -> list[dict]:
        with self._lock:
            items = list(self._memory)
        return items[-count:]

    def close(self):
        if self._log_file:
            self._log_file.close()
            self._log_file = None


request_logger = RequestLogger()
