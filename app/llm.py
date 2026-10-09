"""ComfyUI TextGenerate LLM backend (default) with optional Ollama fallback.

Upstream reference (Comfy-Org/ComfyUI ``comfy_extras/nodes_textgen.py``)::

    TextGenerate inputs: clip, prompt, max_length, sampling_mode (DynamicCombo
    on/off + temperature/top_k/top_p/min_p/repetition_penalty/seed/…),
    optional image/video/audio/thinking/use_default_template/mtp/system_prompt.
    Outputs: generated_text [, thinking].

Official Qwen3 text-gen template loads the encoder with CLIPLoader type
``ltxv`` and feeds TextGenerate → PreviewAny. We ship the same shape as
``workflows/05_llm_textgen.json`` and validate classes via ``/object_info``.
"""

from __future__ import annotations

import copy
import random
from pathlib import Path
from typing import Any, Callable

from app.comfy import ComfyClient, ComfyError
from app.convert import convert_ui, ensure_named, load_workflow
from app.models import match_precision, patch_prompt_models, resolve_model_name
from app.ollama import OllamaError, chat_json, choose_model, list_models
from app.rewrite import combo_options, match_choice, pick_text
from app.storyboard import StoryboardError, parse_json_content

DEFAULT_WORKFLOW = "05_llm_textgen.json"
DEFAULT_CLIP = "qwen_3_4b_fp8_mixed.safetensors"
DEFAULT_CLIP_TYPE = "ltxv"
OPTIONAL_CLIPS = (
    "qwen_3_4b_fp8_mixed.safetensors",
    "qwen_3_4b.safetensors",
    "qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors",
)
LLM_CLASSES = ("CLIPLoader", "TextGenerate", "PreviewAny")
LogFn = Callable[[str], None]


class LLMError(RuntimeError):
    pass


def infer_clip_type(model_name: str, preferred: str = "") -> str:
    preferred = (preferred or "").strip()
    if preferred:
        return preferred
    lower = (model_name or "").casefold()
    if "minimax" in lower or ("qwen3vl" in lower and "32b" in lower):
        return "minimax"
    if "vl" in lower and "qwen" in lower:
        return "qwen_image"
    return DEFAULT_CLIP_TYPE


def list_text_encoders(object_info: dict | None) -> list[str]:
    opts = combo_options(object_info, "CLIPLoader", "clip_name")
    return list(opts or [])


def missing_llm_nodes(object_info: dict | None) -> list[str]:
    if not isinstance(object_info, dict):
        return list(LLM_CLASSES)
    return [name for name in LLM_CLASSES if name not in object_info]


def _pack_sampling(inputs: dict[str, Any]) -> None:
    """Collapse flat sampling widgets into TextGenerate's DynamicCombo dict."""
    mode = inputs.get("sampling_mode", "on")
    if isinstance(mode, dict):
        return
    packed: dict[str, Any] = {"sampling_mode": mode or "on"}
    for key in (
        "temperature",
        "top_k",
        "top_p",
        "min_p",
        "repetition_penalty",
        "seed",
        "presence_penalty",
    ):
        if key in inputs:
            packed[key] = inputs.pop(key)
    inputs["sampling_mode"] = packed


def build_llm_prompt(
    workflow: dict,
    *,
    prompt: str,
    model: str = "",
    clip_type: str = "",
    seed: int = 0,
    max_length: int = 4096,
    temperature: float = 0.4,
    object_info: dict | None = None,
) -> tuple[dict[str, dict], list[str]]:
    """Inject prompt/model/seed/tokens/temp into the UI LLM workflow."""
    graph = copy.deepcopy(workflow)
    notes: list[str] = []
    loader = next((n for n in graph.get("nodes") or [] if n.get("type") == "CLIPLoader"), None)
    gen = next((n for n in graph.get("nodes") or [] if n.get("type") == "TextGenerate"), None)
    if loader is None or gen is None:
        raise LLMError("LLM 工作流缺少 CLIPLoader 或 TextGenerate")

    named = ensure_named(loader, {})
    options = combo_options(object_info, "CLIPLoader", "clip_name")
    wanted = (model or "").strip() or str(named.get("clip_name") or DEFAULT_CLIP)
    chosen, note = resolve_model_name(options, wanted)
    if options is not None and chosen not in options:
        # Prefer known defaults when the configured name is absent.
        for candidate in OPTIONAL_CLIPS:
            hit = match_precision(options, candidate) or match_choice(options, candidate)
            if hit:
                chosen, note = hit, f"模型「{wanted}」不可用，已改用「{hit}」"
                break
    named["clip_name"] = chosen
    if note:
        notes.append(note)

    types = combo_options(object_info, "CLIPLoader", "type")
    ctype = infer_clip_type(chosen, clip_type)
    if types is not None:
        hit = match_choice(types, ctype) or match_choice(types, DEFAULT_CLIP_TYPE)
        named["type"] = hit or types[0]
        if hit is None and ctype not in types:
            notes.append(f"CLIP 类型「{ctype}」不在列表，已改用「{named['type']}」")
    else:
        named["type"] = ctype
    named.setdefault("device", "default")

    gnamed = ensure_named(gen, {})
    gnamed["prompt"] = prompt
    gnamed["max_length"] = int(max_length)
    gnamed["sampling_mode"] = "on"
    gnamed["temperature"] = float(temperature)
    gnamed["top_k"] = int(gnamed.get("top_k") or 64)
    gnamed["top_p"] = float(gnamed.get("top_p") or 0.95)
    gnamed["min_p"] = float(gnamed.get("min_p") or 0.05)
    gnamed["repetition_penalty"] = float(gnamed.get("repetition_penalty") or 1.05)
    gnamed["seed"] = int(seed) % (2**32)
    if "thinking" in gnamed:
        gnamed["thinking"] = False
    if "use_default_template" in gnamed:
        gnamed["use_default_template"] = True

    api = convert_ui(graph, object_info)
    for node in api.values():
        if node.get("class_type") == "TextGenerate":
            _pack_sampling(node.setdefault("inputs", {}))
    notes.extend(patch_prompt_models(api, object_info))
    return api, notes


async def generate_text_comfy(
    settings: dict,
    system: str,
    user: str,
    *,
    log: LogFn | None = None,
    should_stop: Callable[[], bool] | None = None,
    seed: int | None = None,
) -> str:
    """Run the TextGenerate workflow once and return generated text."""
    base = (settings.get("comfy_url") or "").rstrip("/")
    if not base:
        raise LLMError("ComfyUI 地址为空")
    client = ComfyClient(base)
    info = await client.object_info()
    missing = missing_llm_nodes(info)
    if info is None:
        raise LLMError("读不到 ComfyUI /object_info，无法确认 TextGenerate")
    if missing:
        raise LLMError(
            "ComfyUI 缺少节点 "
            + "、".join(missing)
            + "。请升级 ComfyUI 至含 comfy_extras/nodes_textgen.py 的版本。"
        )

    root = Path(__file__).resolve().parents[1]
    wf_name = settings.get("llm_workflow") or DEFAULT_WORKFLOW
    path = root / "workflows" / wf_name
    if not path.is_file():
        raise LLMError(f"找不到 LLM 工作流 {wf_name}")
    workflow = load_workflow(path)

    prompt = f"{system.strip()}\n\n---\n\n{user.strip()}\n"
    model = (settings.get("llm_model") or "").strip() or DEFAULT_CLIP
    max_length = int(settings.get("llm_max_tokens") or 4096)
    temperature = float(settings.get("llm_temperature") or 0.4)
    clip_type = (settings.get("llm_clip_type") or "").strip()
    use_seed = int(seed if seed is not None else random.randint(1, 2**31))

    graph, notes = build_llm_prompt(
        workflow,
        prompt=prompt,
        model=model,
        clip_type=clip_type,
        seed=use_seed,
        max_length=max_length,
        temperature=temperature,
        object_info=info,
    )
    logger = log or (lambda _m: None)
    for note in notes:
        logger(note)
    logger(f"提交 ComfyUI LLM（{wf_name}，模型 {model}，seed={use_seed}）")
    outputs = await client.queue_and_wait_outputs(
        graph,
        logger,
        timeout=float(settings.get("llm_timeout") or 600),
        should_stop=should_stop,
    )
    text = pick_text(outputs)
    if not text.strip():
        # Some builds put string outputs under different keys.
        for node_out in (outputs or {}).values():
            if not isinstance(node_out, dict):
                continue
            for key in ("generated_text", "string", "text"):
                value = node_out.get(key)
                if isinstance(value, str) and value.strip():
                    text = value
                    break
                if isinstance(value, list) and value and isinstance(value[0], str):
                    text = value[0]
                    break
            if text.strip():
                break
    if not text.strip():
        raise LLMError("TextGenerate 没有返回文本（检查 PreviewAny /history 输出）")
    return text.strip()


async def chat_json_resilient(
    settings: dict,
    system: str,
    user: str,
    *,
    log: LogFn | None = None,
    should_stop: Callable[[], bool] | None = None,
    attempts: int = 3,
) -> tuple[dict, str]:
    """Generate JSON with retries; returns (parsed, backend_label)."""
    backend = (settings.get("llm_backend") or "comfy").strip().lower() or "comfy"
    logger = log or (lambda _m: None)
    last_exc: Exception | None = None

    if backend == "comfy":
        for attempt in range(1, max(1, attempts) + 1):
            if should_stop and should_stop():
                raise LLMError("已停止")
            try:
                # Slightly raise temperature on retries to escape bad JSON.
                tweaked = dict(settings)
                base_temp = float(settings.get("llm_temperature") or 0.4)
                tweaked["llm_temperature"] = min(1.2, base_temp + 0.15 * (attempt - 1))
                raw = await generate_text_comfy(
                    tweaked,
                    system,
                    user if attempt == 1 else user + "\n\n请只输出合法 JSON 对象，不要 Markdown。",
                    log=logger,
                    should_stop=should_stop,
                    seed=random.randint(1, 2**31),
                )
                return parse_json_content(raw), "comfy"
            except (LLMError, ComfyError, StoryboardError) as exc:
                last_exc = exc
                logger(f"ComfyUI LLM 第 {attempt}/{attempts} 次失败：{exc}")
        if settings.get("llm_fallback_ollama", True):
            try:
                data = await _ollama_json(settings, system, user, log=logger)
                logger("ComfyUI LLM 失败，已回退到 Ollama")
                return data, "ollama_fallback"
            except (OllamaError, StoryboardError) as exc:
                raise LLMError(f"ComfyUI LLM 与 Ollama 均失败：{last_exc}；Ollama：{exc}") from exc
        raise LLMError(str(last_exc) if last_exc else "ComfyUI LLM 失败")

    if backend == "ollama":
        return await _ollama_json(settings, system, user, log=logger), "ollama"

    raise LLMError(f"未知 llm_backend：{backend}")


async def _ollama_json(settings: dict, system: str, user: str, *, log: LogFn | None = None) -> dict:
    models = await list_models(settings["ollama_url"])
    # When llm_model looks like a Comfy filename, ignore it for Ollama.
    preferred = settings.get("llm_model") or ""
    if preferred.endswith((".safetensors", ".gguf", ".bin")):
        preferred = ""
    model = choose_model(models, preferred)
    if log:
        log(f"使用 Ollama 模型 {model}")
    raw = await chat_json(settings["ollama_url"], model, system, user)
    return parse_json_content(raw)
