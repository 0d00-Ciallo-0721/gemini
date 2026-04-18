from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

import uvicorn


def parse_args():
    parser = argparse.ArgumentParser(description="Start Gemini Reverse standalone server")
    parser.add_argument("--config", default="data/runtime_config.json", help="Path to runtime_config.json")
    parser.add_argument("--host", default="", help="Override bind host")
    parser.add_argument("--port", type=int, default=0, help="Override bind port")
    return parser.parse_args()


def load_runtime_config(config_path: str) -> dict:
    path = Path(config_path)
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8") or "{}")


def main():
    args = parse_args()
    os.environ["GEMINI_REVERSE_CONFIG"] = str(Path(args.config).resolve())
    runtime_config = load_runtime_config(args.config)
    host = args.host or str(runtime_config.get("host") or "127.0.0.1")
    port = int(args.port or runtime_config.get("port") or 8000)
    uvicorn.run("app.main:app", host=host, port=port, reload=False)


if __name__ == "__main__":
    main()
