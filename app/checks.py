"""Connectivity checks for the settings page."""

from __future__ import annotations

from app.comfy import ComfyClient, ComfyError
from app.convert import build_prompt, load_workflow
from app.llm import DEFAULT_CLIP, DEFAULT_WORKFLOW, list_text_encoders, missing_llm_nodes
from app.models import lightning_lora_available, lightning_lora_filenames, missing_classes, missing_models
from app.ollama import OllamaError, list_models
from app.rewrite import H3_WORKFLOWS, missing_nodes, variant
from app.stitch import StitchError, ffmpeg_version
from app.store import ROOT, VIDEO_WORKFLOWS
from app.tts import get_provider, provider_info


async def check_all(settings: dict) -> dict:
    ollama = await _ollama(settings["ollama_url"])
    comfy = await _comfy(settings["comfy_url"])
    ffmpeg = _ffmpeg()
    info = comfy.pop("object_info", None)
    result = {"ollama": ollama, "comfy": comfy, "ffmpeg": ffmpeg}
    result["llm"] = await _llm(settings, {**comfy, "object_info": info, "text_encoders": comfy.get("text_encoders")})
    result["workflows"] = await _workflows(settings, info)
    if settings.get("rewrite_enabled"):
        result["rewriter"] = await _rewriter(settings, comfy["ok"], info)
    if settings.get("tts_enabled"):
        result["tts"] = await _tts(settings)
    result["serial"] = {
        "ok": True,
        "detail": (
            "低配串行已开启：LLM / ComfyUI / 配音同一时间只跑一个，换家族前会卸模型"
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
        client = ComfyClient(url)
        stats = await client.health()
        devices = ((stats.get("devices") or [{}])[0]) if isinstance(stats.get("devices"), list) else {}
        name = devices.get("name") or "已连接"
        vram = devices.get("vram_total")
        if isinstance(vram, int) and vram > 0:
            detail = f"{name}，显存约 {vram / (1024 ** 3):.1f} GB"
        else:
            detail = f"已连接（{name}）"
        info = await client.object_info()
        encoders = list_text_encoders(info)
        return {
            "ok": True,
            "detail": detail,
            "object_info": info,
            "text_encoders": encoders,
        }
    except ComfyError as exc:
        return {"ok": False, "detail": str(exc), "object_info": None, "text_encoders": []}


def _ffmpeg() -> dict:
    try:
        return {"ok": True, "detail": ffmpeg_version()}
    except StitchError as exc:
        return {"ok": False, "detail": str(exc)}


async def _llm(settings: dict, comfy: dict) -> dict:
    backend = (settings.get("llm_backend") or "comfy").strip().lower()
    if backend == "ollama":
        return {
            "ok": True,
            "detail": "分镜后端：Ollama（可选）。" + (comfy.get("detail") or ""),
            "backend": "ollama",
            "models": [],
        }
    if not comfy.get("ok"):
        return {
            "ok": False,
            "detail": "分镜默认走 ComfyUI TextGenerate，但 ComfyUI 未连接",
            "backend": "comfy",
            "models": comfy.get("text_encoders") or [],
        }
    info = comfy.get("object_info")
    missing = missing_llm_nodes(info)
    encoders = comfy.get("text_encoders") or list_text_encoders(info)
    if missing:
        return {
            "ok": False,
            "detail": "缺少节点 "
            + "、".join(missing)
            + "。请升级 ComfyUI（comfy_extras/nodes_textgen.py）",
            "backend": "comfy",
            "models": encoders,
        }
    wanted = (settings.get("llm_model") or DEFAULT_CLIP).strip()
    if encoders and wanted and wanted not in encoders:
        # precision rematch still counts as ok — pipeline will remap
        from app.models import match_precision

        alt = match_precision(encoders, wanted)
        if alt:
            detail = f"TextGenerate 就绪；模型「{wanted}」将映射为「{alt}」"
        else:
            return {
                "ok": False,
                "detail": f"text_encoders 中找不到「{wanted}」。可用：{'、'.join(encoders[:8])}",
                "backend": "comfy",
                "models": encoders,
            }
    else:
        detail = f"TextGenerate 就绪，默认模型 {wanted or DEFAULT_CLIP}"
        if encoders:
            detail += f"（已发现 {len(encoders)} 个文本编码器）"
    return {"ok": True, "detail": detail, "backend": "comfy", "models": encoders}


async def _workflows(settings: dict, object_info: dict | None) -> dict:
    """List missing nodes/models for the active image/video templates."""
    if object_info is None:
        return {"ok": False, "detail": "无 /object_info，无法检查工作流节点与模型", "missing_nodes": [], "missing_models": []}
    files = [
        settings.get("image_workflow") or "01_character_t2i.json",
        settings.get("edit_workflow") or "02_shot_edit.json",
    ]
    video_id = settings.get("video_workflow") or "h3_i2v"
    if video_id in VIDEO_WORKFLOWS:
        files.append(VIDEO_WORKFLOWS[video_id]["file"])
    if (settings.get("llm_backend") or "comfy") == "comfy":
        files.append(settings.get("llm_workflow") or DEFAULT_WORKFLOW)

    missing_node_list: list[str] = []
    missing_model_list: list[str] = []
    lightning_note = ""
    for name in files:
        path = ROOT / "workflows" / name
        if not path.is_file():
            missing_node_list.append(f"（缺少工作流文件 {name}）")
            continue
        try:
            workflow = load_workflow(path)
            prompt = build_prompt(workflow, {"filename_prefix": "n2v/check"}, object_info)
        except Exception as exc:  # noqa: BLE001
            missing_node_list.append(f"{name}: {exc}")
            continue
        for cls in missing_classes(prompt, object_info):
            missing_node_list.append(f"{name} → {cls}")
        for model in missing_models(prompt, object_info):
            missing_model_list.append(f"{name} → {model}")
        if video_id in H3_WORKFLOWS and name == VIDEO_WORKFLOWS[video_id]["file"] and settings.get("h3_lightning", True):
            names = lightning_lora_filenames(workflow)
            ok, note = lightning_lora_available(object_info, names)
            if not ok:
                lightning_note = note + "（将关闭 Lightning 并回退完整步数）"

    ok = not missing_node_list and not missing_model_list
    parts = []
    if missing_node_list:
        parts.append("缺失节点：" + "；".join(missing_node_list[:12]))
    if missing_model_list:
        parts.append("缺失模型：" + "；".join(missing_model_list[:12]))
    if lightning_note:
        parts.append(lightning_note)
    if not parts:
        parts.append("活动工作流的节点与模型均可解析（含精度回退）")
    return {
        "ok": ok,
        "detail": "。".join(parts),
        "missing_nodes": missing_node_list,
        "missing_models": missing_model_list,
        "lightning": lightning_note,
    }


async def _rewriter(settings: dict, comfy_ok: bool, object_info: dict | None = None) -> dict:
    spec = variant(settings.get("rewrite_variant"))
    if not comfy_ok:
        return {"ok": False, "detail": f"{spec['label']}：ComfyUI 未连接，改写会退回原始提示词"}
    info = object_info
    if info is None:
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
