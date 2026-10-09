"""Connectivity checks for the settings page."""

from __future__ import annotations

from app.comfy import ComfyClient, ComfyError
from app.ollama import OllamaError, list_models
from app.stitch import StitchError, ffmpeg_version


async def check_all(settings: dict) -> dict:
    ollama = await _ollama(settings["ollama_url"])
    comfy = await _comfy(settings["comfy_url"])
    ffmpeg = _ffmpeg()
    return {"ollama": ollama, "comfy": comfy, "ffmpeg": ffmpeg}


async def _ollama(url: str) -> dict:
    try:
        models = await list_models(url)
        if models:
            preview = "、".join(models[:6])
            extra = f" 等 {len(models)} 个" if len(models) > 6 else ""
            detail = f"已连接，模型：{preview}{extra}"
        else:
            detail = "已连接，但没有模型。请 ollama pull 一个 Qwen 文本模型。"
        return {"ok": True, "detail": detail, "models": models}
    except OllamaError as exc:
        return {"ok": False, "detail": str(exc), "models": []}


async def _comfy(url: str) -> dict:
    try:
        stats = await ComfyClient(url).health()
        devices = ((stats.get("devices") or [{}])[0]) if isinstance(stats.get("devices"), list) else {}
        name = devices.get("name") or "已连接"
        vram = devices.get("vram_total")
        if isinstance(vram, int) and vram > 0:
            detail = f"{name}，显存约 {vram / (1024 ** 3):.1f} GB"
        else:
            detail = f"已连接（{name}）"
        return {"ok": True, "detail": detail}
    except ComfyError as exc:
        return {"ok": False, "detail": str(exc)}


def _ffmpeg() -> dict:
    try:
        return {"ok": True, "detail": ffmpeg_version()}
    except StitchError as exc:
        return {"ok": False, "detail": str(exc)}
