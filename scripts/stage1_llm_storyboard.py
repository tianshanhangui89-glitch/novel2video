#!/usr/bin/env python3
"""Stage1 helper: build an Ollama chat request for storyboard JSON.

Example (Ollama running locally):
  python stage1_llm_storyboard.py \\
    --chapter ../input/chapter01.md \\
    --style "anime cel-shading, cinematic lighting" \\
    --model qwen2.5:14b \\
    --out ../configs/generated_ch01.json
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.storyboard import normalize_storyboard, parse_json_content  # noqa: E402


def ollama_chat(model: str, system: str, user: str, host: str) -> str:
    payload = {
        "model": model,
        "stream": False,
        "format": "json",
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    }
    req = urllib.request.Request(
        f"{host}/api/chat",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=600) as resp:
        body = json.loads(resp.read().decode("utf-8"))
    return body["message"]["content"]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--chapter", required=True)
    ap.add_argument("--style", required=True)
    ap.add_argument("--model", default="qwen2.5:14b")
    ap.add_argument("--host", default="http://127.0.0.1:11434")
    ap.add_argument("--prompt-file", default=None)
    ap.add_argument("--out", required=True)
    ap.add_argument("--project-id", default="generated")
    ap.add_argument("--title", default="")
    args = ap.parse_args()

    root = pathlib.Path(__file__).resolve().parents[1]
    prompt_path = pathlib.Path(args.prompt_file) if args.prompt_file else root / "prompts" / "stage1_storyboard.txt"
    system = prompt_path.read_text(encoding="utf-8")
    chapter = pathlib.Path(args.chapter).read_text(encoding="utf-8")
    user = f"全局画风 style: {args.style}\n\n---章节正文---\n{chapter}"
    raw = ollama_chat(args.model, system, user, args.host)
    data = parse_json_content(raw)
    project = normalize_storyboard(
        data,
        project_id=args.project_id,
        title=args.title or args.project_id,
        style=args.style,
        aspect="16:9",
        source_name=str(args.chapter),
    )
    out = pathlib.Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(project, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Wrote {out} with {len(project['shots'])} shots")


if __name__ == "__main__":
    main()
