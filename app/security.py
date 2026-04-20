from __future__ import annotations

import hmac
import ipaddress
from typing import Iterable

from fastapi import Request


def _iter_networks(values: Iterable[str]):
    for raw in values or []:
        candidate = str(raw or "").strip()
        if not candidate:
            continue
        try:
            if "/" in candidate:
                yield ipaddress.ip_network(candidate, strict=False)
            else:
                ip_obj = ipaddress.ip_address(candidate)
                suffix = "/32" if ip_obj.version == 4 else "/128"
                yield ipaddress.ip_network(f"{candidate}{suffix}", strict=False)
        except ValueError:
            continue


def _ip_matches_ranges(client_ip: str, ranges: Iterable[str]) -> bool:
    if not client_ip:
        return False
    try:
        ip_obj = ipaddress.ip_address(client_ip)
    except ValueError:
        return False
    return any(ip_obj in network for network in _iter_networks(ranges))


def secure_compare(left: str, right: str) -> bool:
    return hmac.compare_digest((left or "").encode("utf-8"), (right or "").encode("utf-8"))


def extract_bearer_or_key(request: Request) -> str:
    supplied = (request.headers.get("x-api-key") or "").strip()
    if supplied:
        return supplied
    auth_header = request.headers.get("authorization", "")
    if auth_header.lower().startswith("bearer "):
        return auth_header[7:].strip()
    return ""


def get_real_client_ip(request: Request, trusted_proxies: Iterable[str]) -> str:
    peer_ip = (request.client.host if request.client else "") or ""
    if not _ip_matches_ranges(peer_ip, trusted_proxies):
        return peer_ip
    forwarded_for = request.headers.get("x-forwarded-for", "")
    if forwarded_for:
        return forwarded_for.split(",")[0].strip()
    return peer_ip


def is_ip_allowed(client_ip: str, runtime_config: dict) -> bool:
    if not runtime_config.get("allowlist_enabled", True):
        return True
    return _ip_matches_ranges(client_ip, runtime_config.get("allowed_client_ips", []) or [])


def require_admin_token(request: Request, runtime_config: dict) -> bool:
    expected = str(runtime_config.get("admin_token") or "").strip()
    if not expected:
        return True
    supplied = (request.headers.get("x-admin-token") or "").strip()
    if not supplied:
        supplied = extract_bearer_or_key(request)
    return bool(supplied) and secure_compare(supplied, expected)


def has_valid_service_key(request: Request, runtime_config: dict) -> bool:
    configured = [str(item).strip() for item in (runtime_config.get("api_keys", []) or []) if str(item).strip()]
    if not configured:
        return False
    supplied = extract_bearer_or_key(request)
    if not supplied:
        return False
    return any(secure_compare(supplied, expected) for expected in configured)
