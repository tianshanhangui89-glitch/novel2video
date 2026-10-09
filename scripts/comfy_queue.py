#!/usr/bin/env python3
"""Submit a ComfyUI workflow and wait for it.

Official templates in workflows/ are UI graphs (often with subgraphs). This
script converts them to API format with the same flattener the web app uses,
then POSTs to /prompt. Pass --convert-only to write the API graph and stop.

Already-exported API graphs (no top-level "nodes" array) are submitted as-is,
with optional prompt / image injection.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.convert import build_prompt_file  # noqa: E402


def post_json(url: str, payload: dict) -> dict:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=120) as resp:
        return json.loads(resp.read().decode("utf-8"))


def get_json(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=60) as resp:
        return json.loads(resp.read().decode("utf-8"))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--server", default="http://127.0.0.1:8188")
    parser.add_argument("--workflow", required=True, help="UI 模板或 API 格式 JSON")
    parser.add_argument("--prompt", default="", help="注入正向提示词")
    parser.add_argument("--image", action="append", default=[], help="按顺序替换 LoadImage，可重复")
    parser.add_argument("--prefix", default="n2v/cli")
    parser.add_argument("--convert-only", default="", help="只写出 API JSON，不提交")
    args = parser.parse_args()

    graph = build_prompt_file(
        Path(args.workflow),
        {
            "prompt": args.prompt or None,
            "images": args.image or None,
            "filename_prefix": args.prefix,
            "seed": 1,
        },
    )
    if args.convert_only:
        dest = Path(args.convert_only)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(json.dumps(graph, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"Wrote {dest} ({len(graph)} API nodes)")
        return

    client_id = str(uuid.uuid4())
    result = post_json(f"{args.server}/prompt", {"prompt": graph, "client_id": client_id})
    if result.get("node_errors"):
        raise SystemExit(json.dumps(result["node_errors"], ensure_ascii=False, indent=2))
    prompt_id = result.get("prompt_id")
    if not prompt_id:
        raise SystemExit(f"Unexpected response: {result}")
    print(f"queued prompt_id={prompt_id} nodes={len(graph)}")
    while True:
        hist = get_json(f"{args.server}/history/{prompt_id}")
        if prompt_id in hist:
            print(json.dumps(hist[prompt_id].get("outputs", {}), ensure_ascii=False, indent=2))
            status = (hist[prompt_id].get("status") or {}).get("status_str")
            if status == "error":
                raise SystemExit("ComfyUI execution error")
            break
        time.sleep(2)


if __name__ == "__main__":
    main()
