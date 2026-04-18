import httpx
import time
from typing import Dict, Any

async def probe_gemini_upstream(timeout_sec: int = 5) -> Dict[str, Any]:
    """
    轻量级网络前置探针。
    用于验证运行机器发包至 gemini.google.com 基本通信联通可用度，
    但不承载、介入大模型业务流的任何实体鉴权或功能调用链路。
    任何此探针的失败尽作诊断提示用，不可阻断核心引擎运转回路。
    """
    endpoints = [
        "https://gemini.google.com/app",
        "https://clients6.google.com"
    ]
    
    results = {
        "timestamp": time.time(),
        "endpoints": {},
        "upstream_healthy": True,
        "warnings": []
    }
    
    timeout = httpx.Timeout(float(timeout_sec))
    async with httpx.AsyncClient(timeout=timeout) as client:
        for url in endpoints:
            try:
                # Need follow_redirects for Google auth redirects
                resp = await client.head(url, follow_redirects=True)
                ok = resp.status_code in (200, 302, 401, 403) # Any successful response that confirms domain existence
                results["endpoints"][url] = {
                    "status_code": resp.status_code,
                    "reachable": ok
                }
                if not ok:
                    results["upstream_healthy"] = False
                    results["warnings"].append(f"Unexpected status {resp.status_code} for {url}")
            except Exception as e:
                results["upstream_healthy"] = False
                results["endpoints"][url] = {"reachable": False, "error": str(e)}
                results["warnings"].append(f"Connection failed for {url}: {e}")
                
    return results
