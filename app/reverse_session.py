import copy
import re
from typing import Dict, List, Tuple


REVERSE_SESSION_TAG = "astrbot_reverse_session"
REVERSE_SESSION_PATTERN = re.compile(
    rf"<{REVERSE_SESSION_TAG}>\s*(.*?)\s*</{REVERSE_SESSION_TAG}>",
    re.DOTALL | re.IGNORECASE,
)


def parse_reverse_session_payload(text: str) -> Dict[str, str]:
    raw_text = str(text or "")
    match = REVERSE_SESSION_PATTERN.search(raw_text)
    if not match:
        return {}
    parsed: Dict[str, str] = {}
    for line in match.group(1).splitlines():
        normalized = str(line or "").strip()
        if not normalized or "=" not in normalized:
            continue
        key, value = normalized.split("=", 1)
        parsed[key.strip()] = value.strip()
    return parsed


def strip_reverse_session_payload(text: str) -> str:
    raw_text = str(text or "")
    if not raw_text:
        return ""
    return REVERSE_SESSION_PATTERN.sub("", raw_text).strip()


def _extract_from_content(content) -> Tuple[Dict[str, str], object]:
    if isinstance(content, str):
        parsed = parse_reverse_session_payload(content)
        if not parsed:
            return {}, content
        stripped = strip_reverse_session_payload(content)
        return parsed, stripped

    if isinstance(content, list):
        parsed: Dict[str, str] = {}
        cleaned_parts = []
        changed = False
        for part in content:
            if not isinstance(part, dict):
                cleaned_parts.append(part)
                continue
            cloned = copy.deepcopy(part)
            if part.get("type") == "text":
                found, stripped = _extract_from_content(part.get("text", ""))
                if found and not parsed:
                    parsed = found
                if found:
                    changed = True
                    if stripped:
                        cloned["text"] = stripped
                        cleaned_parts.append(cloned)
                    continue
            cleaned_parts.append(cloned)
        if not changed:
            return {}, content
        return parsed, cleaned_parts

    return {}, content


def extract_reverse_session_from_messages(messages: List[dict]) -> Tuple[Dict[str, str], List[dict]]:
    parsed: Dict[str, str] = {}
    cleaned_messages: List[dict] = []
    for message in messages or []:
        cloned = copy.deepcopy(message)
        found, cleaned_content = _extract_from_content(cloned.get("content"))
        if found and not parsed:
            parsed = found
        cloned["content"] = cleaned_content
        role = str(cloned.get("role", "")).strip().lower()
        if role == "system" and cleaned_content in ("", [], None):
            continue
        cleaned_messages.append(cloned)
    return parsed, cleaned_messages
