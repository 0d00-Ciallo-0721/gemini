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
