from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any


COOKIE_KEYS = {
    "SECURE_1PSID": ("SECURE_1PSID", "__Secure-1PSID"),
    "SECURE_1PSIDTS": ("SECURE_1PSIDTS", "__Secure-1PSIDTS"),
}


def extract_cookie_value(raw_cookie: str, aliases: tuple[str, ...]) -> str:
    cookie_text = str(raw_cookie or "")
    for alias in aliases:
        pattern = re.compile(rf"(?:^|;\s*){re.escape(alias)}=([^;]+)")
        match = pattern.search(cookie_text)
        if match:
            return match.group(1).strip()
    return ""


def extract_cookie_pair(raw_cookie: str) -> dict[str, str]:
    return {
        key: extract_cookie_value(raw_cookie, aliases)
        for key, aliases in COOKIE_KEYS.items()
    }


def extract_cookie_strings(cookie_list: list[Any] | None) -> list[str]:
    cookies: list[str] = []
    for item in cookie_list or []:
        cookie_text = ""
        if isinstance(item, dict):
            cookie_text = str(item.get("cookie") or "").strip()
        else:
            cookie_text = str(item or "").strip()
        if cookie_text:
            cookies.append(cookie_text)
    return cookies


def normalize_cookie_accounts(cookie_list: list[Any] | None) -> dict[str, dict[str, str]]:
    normalized: dict[str, dict[str, str]] = {}
    for index, cookie_text in enumerate(extract_cookie_strings(cookie_list), start=1):
        parsed = extract_cookie_pair(cookie_text)
        if not parsed["SECURE_1PSID"] or not parsed["SECURE_1PSIDTS"]:
            continue
        normalized[str(index)] = {
            "cookie": cookie_text,
            "SECURE_1PSID": parsed["SECURE_1PSID"],
            "SECURE_1PSIDTS": parsed["SECURE_1PSIDTS"],
            "label": f"account_{index}",
        }
    return normalized


def parse_cookie_string(cookie_text: str) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for part in cookie_text.split(';'):
        part = part.strip()
        if not part or '=' not in part:
            continue
        k, v = part.split('=', 1)
        parsed[k.strip()] = v.strip()
    return parsed


def standardize_cookie_payload(payload: Any, default_label: str = "") -> dict[str, Any] | None:
    """标准化传入的完整 cookie dict 或 raw cookie 字符串"""
    if not payload:
        return None
        
    raw_cookie = ""
    cookies_dict = {}
    
    if isinstance(payload, str):
        raw_cookie = payload.strip()
        cookies_dict = parse_cookie_string(raw_cookie)
    elif isinstance(payload, dict):
        raw_cookie = str(payload.get("cookie") or payload.get("raw_cookie") or "").strip()
        cookies_dict = payload.get("cookies_dict") or {}
        if not cookies_dict and raw_cookie:
            cookies_dict = parse_cookie_string(raw_cookie)
        elif cookies_dict and not raw_cookie:
            raw_cookie = "; ".join(f"{k}={v}" for k, v in cookies_dict.items())
            
    extracted = extract_cookie_pair(raw_cookie)
    psid = extracted.get("SECURE_1PSID")
    psidts = extracted.get("SECURE_1PSIDTS")
    
    if not psid or not psidts:
        return None
        
    label = default_label
    if isinstance(payload, dict) and payload.get("label"):
        label = str(payload.get("label"))
        
    return {
        "raw_cookie": raw_cookie,
        "cookie": raw_cookie,
        "cookies_dict": cookies_dict,
        "SECURE_1PSID": psid,
        "SECURE_1PSIDTS": psidts,
        "label": label,
    }


def patch_runtime_config(runtime_config_path: str | Path, cookie_list: list[Any] | None) -> Path:
    path = Path(runtime_config_path)
    payload = {}
    if path.exists():
        payload = json.loads(path.read_text(encoding="utf-8") or "{}")
    payload["accounts"] = normalize_cookie_accounts(cookie_list)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description="Extract Gemini cookies and write runtime config accounts.")
    parser.add_argument("--runtime-config", required=True, help="Path to runtime_config.json")
    parser.add_argument(
        "--cookie",
        action="append",
        default=[],
        help="Raw cookie string. Can be provided multiple times.",
    )
    parser.add_argument(
        "--cookie-file",
        default="",
        help="Optional text file containing one raw cookie string per line.",
    )
    args = parser.parse_args()

    cookies = list(args.cookie or [])
    if args.cookie_file:
        cookie_file = Path(args.cookie_file)
        if cookie_file.exists():
            cookies.extend(
                [line.strip() for line in cookie_file.read_text(encoding="utf-8").splitlines() if line.strip()]
            )

    patch_runtime_config(args.runtime_config, cookies)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
