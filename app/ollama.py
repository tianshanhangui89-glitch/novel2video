"""Ollama HTTP client for stage-1 storyboard JSON."""

from __future__ import annotations

from typing import Any

import httpx


class OllamaError(RuntimeError):
    pass


def choose_model(models: list[str], preferred: str = "") -> str:
    cleaned = [m.strip() for m in models if m and m.strip()]
    preferred = (preferred or "").strip()
    if preferred:
        if preferred in cleaned or not cleaned:
            return preferred
        for name in cleaned:
            if preferred in name:
                return name
        return preferred

    def score(name: str) -> tuple[int, str]:
        lower = name.lower()
        value = 0
        if "qwen" in lower:
            value += 50
        if "qwen3" in lower:
            value += 20
        if "qwen2.5" in lower or "qwen2" in lower:
            value += 10
        if any(token in lower for token in ("embed", "vision", "vl")):
            value -= 25
        return value, name

    if not cleaned:
        raise OllamaError("Ollama 没有可用模型。请先 ollama pull 一个 Qwen 文本模型。")
    return max(cleaned, key=score)


async def list_models(base_url: str, timeout: float = 8) -> list[str]:
    url = base_url.rstrip("/") + "/api/tags"
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.get(url)
            response.raise_for_status()
            payload = response.json()
    except httpx.HTTPError as exc:
        raise OllamaError(f"无法连接 Ollama（{base_url}）：{exc}") from exc
    models = []
    for item in payload.get("models") or []:
        name = item.get("name") or item.get("model")
        if name:
            models.append(name)
    return models


async def chat_json(
    base_url: str,
    model: str,
    system: str,
    user: str,
    timeout: float = 600,
    *,
    keep_alive: Any = None,
) -> str:
    url = base_url.rstrip("/") + "/api/chat"
    payload: dict[str, Any] = {
        "model": model,
        "stream": False,
        "format": "json",
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    }
    if keep_alive is not None:
        payload["keep_alive"] = keep_alive
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(url, json=payload)
            response.raise_for_status()
            body = response.json()
    except httpx.HTTPError as exc:
        raise OllamaError(f"Ollama 对话失败（{model}）：{exc}") from exc
    message = body.get("message") or {}
    content = message.get("content") or ""
    if not str(content).strip():
        raise OllamaError("Ollama 返回了空内容。请换一个文本模型，不要用图像模型写分镜。")
    return str(content)
