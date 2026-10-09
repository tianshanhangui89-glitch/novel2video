"""Resolve ComfyUI model filenames against a live /object_info catalog.

Workflow templates ship with one precision (e.g. ``*_int8_convrot.safetensors``).
If that file is missing but a sibling precision is installed (fp16 / bf16 / fp8 / …),
we rematch the way ``rewrite.resolve_choice`` matches dropdown labels.
"""

from __future__ import annotations

import re
from typing import Any

from app.rewrite import combo_options, match_choice, resolve_choice

# Inputs that pick files from ComfyUI model folders.
MODEL_KEYS = (
    "unet_name",
    "ckpt_name",
    "vae_name",
    "clip_name",
    "clip_name1",
    "clip_name2",
    "lora_name",
    "model_name",
    "model",
    "diffusion_model",
)

PRECISION_TAGS = (
    "int8_convrot",
    "int8",
    "fp8_scaled",
    "fp8",
    "nvfp4_awq",
    "nvfp4",
    "bf16",
    "fp16",
    "fp32",
    "pruned",
    "scaled",
)

_LOADER_CLASSES = {
    "UNETLoader": "unet_name",
    "CheckpointLoaderSimple": "ckpt_name",
    "VAELoader": "vae_name",
    "CLIPLoader": "clip_name",
    "DualCLIPLoader": "clip_name1",
    "LoraLoaderModelOnly": "lora_name",
    "LoraLoader": "lora_name",
}


def _stem_core(name: str) -> str:
    """Filename without extension and precision / quant tags."""
    base = re.sub(r"\.(safetensors|gguf|pt|bin|ckpt)$", "", name, flags=re.I)
    lowered = base
    for tag in PRECISION_TAGS:
        lowered = re.sub(rf"[_\-.]{re.escape(tag)}$", "", lowered, flags=re.I)
        lowered = re.sub(rf"[_\-.]{re.escape(tag)}[_\-.]", "_", lowered, flags=re.I)
    return re.sub(r"[_\-.]+", "", lowered.casefold())


def match_precision(options: list[str], wanted: str) -> str | None:
    """Exact / substring first, then same model family with another precision."""
    hit = match_choice(options, wanted)
    if hit:
        return hit
    core = _stem_core(wanted)
    if len(core) < 6:
        return None
    scored: list[tuple[int, str]] = []
    for option in options:
        other = _stem_core(option)
        if other == core or core in other or other in core:
            # Prefer shared prefix length.
            scored.append((len(other), option))
    if not scored:
        return None
    scored.sort(reverse=True)
    best_len = scored[0][0]
    peers = [name for length, name in scored if length == best_len]
    return peers[0] if len(peers) == 1 else peers[0]


def options_for_input(object_info: dict | None, class_type: str, key: str) -> list[str] | None:
    opts = combo_options(object_info, class_type, key)
    if opts is not None:
        return opts
    # Some loaders expose the list under a nested name; try common aliases.
    for alias in (key, key.replace("_name", ""), "ckpt_name", "unet_name"):
        opts = combo_options(object_info, class_type, alias)
        if opts is not None:
            return opts
    return None


def resolve_model_name(
    options: list[str] | None,
    wanted: str,
) -> tuple[str, str]:
    """Return (chosen, note). Note is empty on exact success."""
    wanted = (wanted or "").strip()
    if not wanted:
        return wanted, ""
    if options is None:
        return wanted, ""
    if wanted in options:
        return wanted, ""
    hit = match_precision(options, wanted)
    if hit:
        return hit, f"模型「{wanted}」不在列表，已改用「{hit}」"
    value, note = resolve_choice(options, wanted, wanted, "")
    if note:
        return value, note
    if value != wanted:
        return value, f"模型「{wanted}」不可用，已改用「{value}」"
    return wanted, f"找不到模型「{wanted}」的可用替代"


def patch_prompt_models(prompt: dict[str, dict], object_info: dict | None) -> list[str]:
    """Rewrite model filename inputs in an API prompt. Returns human notes."""
    notes: list[str] = []
    if not object_info:
        return notes
    for node in prompt.values():
        ctype = node.get("class_type") or ""
        inputs = node.get("inputs") or {}
        keys = set(MODEL_KEYS)
        preferred = _LOADER_CLASSES.get(ctype)
        if preferred:
            keys.add(preferred)
        for key in list(inputs):
            if key not in keys:
                continue
            value = inputs[key]
            if not isinstance(value, str) or not value.strip():
                continue
            options = options_for_input(object_info, ctype, key)
            chosen, note = resolve_model_name(options, value)
            if chosen != value:
                inputs[key] = chosen
            if note:
                notes.append(f"{ctype}.{key}: {note}")
    return notes


def collect_workflow_models(prompt: dict[str, dict]) -> list[tuple[str, str, str]]:
    """List (class_type, key, filename) model references in a prompt."""
    found: list[tuple[str, str, str]] = []
    for node in prompt.values():
        ctype = node.get("class_type") or ""
        inputs = node.get("inputs") or {}
        for key, value in inputs.items():
            if key in MODEL_KEYS and isinstance(value, str) and value.strip():
                found.append((ctype, key, value))
    return found


def missing_models(prompt: dict[str, dict], object_info: dict | None) -> list[str]:
    """Filenames that cannot be resolved even after precision matching."""
    if not object_info:
        return [f"{ctype}.{key}={name}" for ctype, key, name in collect_workflow_models(prompt)]
    missing: list[str] = []
    for ctype, key, name in collect_workflow_models(prompt):
        options = options_for_input(object_info, ctype, key)
        if options is None:
            continue  # class missing — reported separately as a node
        chosen, note = resolve_model_name(options, name)
        if chosen not in options and "找不到" in note:
            missing.append(f"{name}（{ctype}.{key}）")
        elif chosen not in (options or []) and options is not None:
            # resolve_choice may still return wanted when empty options
            if not options:
                missing.append(f"{name}（{ctype}.{key}，列表为空）")
    return missing


def missing_classes(prompt: dict[str, dict], object_info: dict | None) -> list[str]:
    if not object_info:
        return sorted({node.get("class_type") for node in prompt.values() if node.get("class_type")})
    return sorted(
        {
            node.get("class_type")
            for node in prompt.values()
            if node.get("class_type") and node["class_type"] not in object_info
        }
    )


def enable_lightning_lora(workflow: dict, *, enabled: bool, steps: int) -> list[str]:
    """Flip Lightning LoRA booleans / step ints in a UI-format workflow (in place)."""
    from app.convert import collect_subgraphs, ensure_named

    notes: list[str] = []
    subgraphs = collect_subgraphs(workflow)

    def visit(nodes: list | None) -> None:
        if not nodes:
            return
        for node in nodes:
            title = str(node.get("title") or "")
            ntype = node.get("type")
            widgets = node.get("widgets_values")
            if ntype == "PrimitiveBoolean" and "Lightning" in title and isinstance(widgets, list) and widgets:
                widgets[0] = bool(enabled)
                notes.append(f"{'启用' if enabled else '关闭'} Lightning LoRA（{title or ntype}）")
            if ntype == "PrimitiveInt" and "Lightning" in title and isinstance(widgets, list) and widgets:
                widgets[0] = int(steps)
                notes.append(f"Lightning 步数设为 {steps}")
            # Subgraph instances expose turbo_mode / turbo_steps as named widgets.
            if ntype in subgraphs or isinstance(node.get("widgets_values_named"), dict):
                try:
                    named = ensure_named(node, subgraphs)
                except Exception:
                    named = node.get("widgets_values_named") or {}
                if isinstance(named, dict):
                    if "turbo_mode" in named:
                        named["turbo_mode"] = bool(enabled)
                        notes.append(f"{'启用' if enabled else '关闭'} turbo_mode")
                    if "turbo_steps" in named and enabled:
                        named["turbo_steps"] = int(steps)
                        notes.append(f"turbo_steps={steps}")

    visit(workflow.get("nodes"))
    for subgraph in (workflow.get("definitions") or {}).get("subgraphs") or []:
        visit(subgraph.get("nodes"))
    # De-dupe while preserving order.
    seen: set[str] = set()
    unique: list[str] = []
    for note in notes:
        if note not in seen:
            seen.add(note)
            unique.append(note)
    return unique


def lightning_lora_available(object_info: dict | None, filenames: list[str]) -> tuple[bool, str]:
    """Whether at least one Lightning LoRA filename resolves in object_info."""
    if not filenames:
        return False, "工作流未声明 Lightning LoRA"
    if not object_info:
        return True, ""  # cannot verify offline — allow and let ComfyUI fail loudly
    options = options_for_input(object_info, "LoraLoaderModelOnly", "lora_name")
    if options is None:
        options = options_for_input(object_info, "LoraLoader", "lora_name")
    if options is None:
        return True, ""
    for name in filenames:
        chosen, _ = resolve_model_name(options, name)
        if chosen in options:
            return True, ""
    return False, "找不到 Lightning LoRA：" + "、".join(filenames)


def lightning_lora_filenames(workflow: dict) -> list[str]:
    names: list[str] = []

    def visit(nodes: list | None) -> None:
        if not nodes:
            return
        for node in nodes:
            if node.get("type") != "LoraLoaderModelOnly":
                continue
            widgets = node.get("widgets_values") or []
            if widgets and isinstance(widgets[0], str) and "turbo" in widgets[0].lower():
                names.append(widgets[0])

    visit(workflow.get("nodes"))
    for subgraph in (workflow.get("definitions") or {}).get("subgraphs") or []:
        visit(subgraph.get("nodes"))
    return names
