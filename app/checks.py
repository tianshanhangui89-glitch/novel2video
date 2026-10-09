"""Connectivity checks for the settings page."""

from __future__ import annotations

from app.comfy import ComfyClient, ComfyError
from app.ollama import OllamaError, list_models
from app.rewrite import missing_nodes, variant
from app.stitch import StitchError, ffmpeg_version
from app.tts import get_provider, provider_info


async def check_all(settings: dict) -> dict:
    ollama = await _ollama(settings["ollama_url"])
    comfy = await _comfy(settings["comfy_url"])
    ffmpeg = _ffmpeg()
    result = {"ollama": ollama, "comfy": comfy, "ffmpeg": ffmpeg}
    if settings.get("rewrite_enabled"):
        result["rewriter"] = await _rewriter(settings, comfy["ok"])
    if settings.get("tts_enabled"):
        result["tts"] = await _tts(settings)
    result["serial"] = {
        "ok": True,
        "detail": (
            "低配串行已开启：Ollama / ComfyUI / 配音同一时间只跑一个，换家族前会卸模型"
            if settings.get("serial_mode", True)
            else "低配串行已关闭：不保证显存互斥，多任务可能同时占 GPU"
        ),
    }
    return result


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


async def _rewriter(settings: dict, comfy_ok: bool) -> dict:
    spec = variant(settings.get("rewrite_variant"))
    if not comfy_ok:
        return {"ok": False, "detail": f"{spec['label']}：ComfyUI 未连接，改写会退回原始提示词"}
    info = await ComfyClient(settings["comfy_url"]).object_info()
    missing = missing_nodes(info, spec, spec["uses_image"])
    if info is None:
        return {"ok": False, "detail": "读不到 /object_info，改写会退回原始提示词"}
    if missing:
        return {
            "ok": False,
            "detail": f"缺少节点 {'、'.join(missing)}。在 ComfyUI Manager 搜索 minimax-h3-prompt-rewriter 安装",
        }
    return {"ok": True, "detail": f"{spec['label']} 节点已就绪，显存约 {spec['vram'].lstrip('~')}"}


async def _tts(settings: dict) -> dict:
    provider = get_provider(settings.get("tts_provider"))
    status = await provider.health(settings)
    if status.get("ok"):
        return status
    if settings.get("tts_fallback_local") and (settings.get("tts_provider") or "") != "local":
        return {
            "ok": False,
            "detail": (status.get("detail") or "配音服务不可用")
            + "。已开启本地回退：合成失败时会改用本地占位音。",
        }
    return status
