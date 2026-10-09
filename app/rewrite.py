"""Optional H3 prompt rewrite via the minimax-h3-prompt-rewriter ComfyUI nodes.

Node classes and inputs follow pytraveler/MiniMax-H3-Prompt-Rewriter-ComfyUI:
``MiniMaxH3PromptWriter8B`` (Qwen3-VL-8B LoRA, sees frames),
``MiniMaxH3PromptRewriter`` (Qwen3.6-27B LoRA, text only) and
``MiniMaxH3GuidedWriter`` (any GGUF, guided by MiniMax's writing guide).
Each returns ``rewritten_prompt`` plus the three H3 sections; the shipped
workflows send ``rewritten_prompt`` into core ``PreviewAny`` so the text shows
up in ``/history`` as ``outputs[node]["text"]``.
"""

from __future__ import annotations

import copy
import re
from typing import Any

from app.convert import bind_images, collect_subgraphs, convert_ui, ensure_named

OUTPUT_FIELDS = ("integrated_multimodal_description", "overall_soundscape", "non_diegetic_music")
OPTIONS_CLASS = "MiniMaxH3RewriterOptions"
TEXT_CLASSES = ("PreviewAny", "SaveText")
H3_WORKFLOWS = {"h3_i2v", "h3_r2v"}
ASPECTS = {"16:9", "9:16", "1:1"}

VARIANTS: dict[str, dict[str, Any]] = {
    "8b": {
        "id": "8b",
        "label": "Rewriter 8B（Qwen3-VL-8B，看关键帧）",
        "node": "MiniMaxH3PromptWriter8B",
        "file": "04a_h3_rewrite_8b.json",
        "vram": "~9 GB",
        "uses_image": True,
        "uses_adapter": True,
        "default_model": "Qwen3-VL-8B-Instruct GGUF Q4_K_M",
        "note": "推荐。GGUF Q4_K_M 约 9 GB 显存，把镜头关键帧一起喂给模型（I2VA）。",
    },
    "27b": {
        "id": "27b",
        "label": "Rewriter 27B（Qwen3.6-27B，纯文本）",
        "node": "MiniMaxH3PromptRewriter",
        "file": "04b_h3_rewrite_27b.json",
        "vram": "~13 GB",
        "uses_image": False,
        "uses_adapter": True,
        "default_model": "Qwen3.6-27B GGUF UD-IQ2_M",
        "note": "原版 LoRA，只读文字（T2VA）。UD-IQ2_M 约 13 GB，Q4_K_M 约 19 GB。",
    },
    "writer": {
        "id": "writer",
        "label": "Writer（任意 GGUF，按写作指南）",
        "node": "MiniMaxH3GuidedWriter",
        "file": "04c_h3_writer.json",
        "vram": "~5 GB",
        "uses_image": False,
        "uses_adapter": False,
        "default_model": "Qwen3.5-4B",
        "note": "不需要 LoRA，Qwen3.5-4B Q4_K_M 约 5 GB 显存。只读文字（T2VA），没有适配器。",
    },
}


def variant(key: str | None) -> dict[str, Any]:
    return VARIANTS.get(key or "8b") or VARIANTS["8b"]


def uses_image(spec: dict, video_workflow: str) -> bool:
    """Only the I2V branch has a keyframe that is the first frame of the clip."""
    return bool(spec["uses_image"]) and video_workflow == "h3_i2v"


def required_nodes(spec: dict, with_image: bool) -> list[str]:
    needed = [spec["node"]]
    if spec["uses_adapter"]:
        needed.append(OPTIONS_CLASS)
    if with_image:
        needed.append("LoadImage")
    return needed


def missing_nodes(object_info: dict | None, spec: dict, with_image: bool) -> list[str]:
    if not isinstance(object_info, dict):
        return required_nodes(spec, with_image) + ["PreviewAny"]
    missing = [name for name in required_nodes(spec, with_image) if name not in object_info]
    if not any(name in object_info for name in TEXT_CLASSES):
        missing.append("PreviewAny")
    return missing


def combo_options(object_info: dict | None, class_type: str, name: str) -> list[str] | None:
    """Options of a COMBO input, in either the legacy or the ``COMBO`` schema."""
    if not isinstance(object_info, dict):
        return None
    info = object_info.get(class_type)
    if not isinstance(info, dict):
        return None
    inputs = info.get("input") or {}
    for section in ("required", "optional"):
        spec = (inputs.get(section) or {}).get(name)
        if not isinstance(spec, (list, tuple)) or not spec:
            continue
        head = spec[0]
        if isinstance(head, list):
            return [str(item) for item in head]
        if head == "COMBO" and len(spec) > 1 and isinstance(spec[1], dict):
            return [str(item) for item in spec[1].get("options") or []]
    return None


def _squash(text: str) -> str:
    return re.sub(r"[^0-9a-z]+", "", text.casefold())


def match_choice(options: list[str], wanted: str) -> str | None:
    """Find a dropdown label for a label, a filename or a fragment of either."""
    wanted = (wanted or "").strip()
    if not wanted or not options:
        return None
    if wanted in options:
        return wanted
    folded = wanted.casefold()
    for option in options:
        if folded in option.casefold():
            return option
    stem = re.sub(r"\.(gguf|safetensors)$", "", wanted, flags=re.I)
    squashed = _squash(stem)
    if not squashed:
        return None
    for option in options:
        if squashed in _squash(option):
            return option
    # Filenames carry quant suffixes the labels spell differently, so score by shared tokens.
    tokens = [t for t in re.split(r"[^0-9a-z]+", stem.casefold()) if t]
    if len(tokens) < 2:
        return None
    scored = []
    for option in options:
        flat = _squash(option)
        if not flat.startswith(tokens[0]) and tokens[0] not in flat:
            continue
        scored.append((sum(t in flat for t in tokens), option))
    if not scored:
        return None
    best = max(score for score, _ in scored)
    if best * 2 < len(tokens) or [score for score, _ in scored].count(best) > 1:
        return None
    return next(option for score, option in scored if score == best)


def resolve_choice(
    options: list[str] | None,
    wanted: str,
    default: str,
    fallback: str,
) -> tuple[str, str]:
    """Pick the widget value and say how it was picked.

    ``wanted`` is the user's field, ``default`` the template value, ``fallback``
    a fragment of the recommended catalog entry. Without object_info the user's
    text (or the template value) is sent unchanged.
    """
    if options is None:
        return (wanted.strip() or default), ""
    if wanted.strip():
        hit = match_choice(options, wanted)
        if hit:
            return hit, ""
        note = f"ComfyUI 下拉里找不到「{wanted.strip()}」，改用默认"
    else:
        note = ""
    for candidate in (default, fallback):
        hit = match_choice(options, candidate)
        if hit:
            return hit, note
    return (options[0] if options else default), note


def _find(graph: dict, class_type: str) -> dict | None:
    return next((n for n in graph.get("nodes") or [] if n.get("type") == class_type), None)


def build_rewrite_prompt(
    workflow: dict,
    *,
    spec: dict,
    prompt: str,
    aspect: str,
    duration: float,
    seed: int,
    image: str | None = None,
    model: str = "",
    adapter: str = "",
    object_info: dict | None = None,
) -> tuple[dict[str, dict], list[str]]:
    """Inject one shot into a rewriter UI workflow and flatten it to an API prompt."""
    graph = copy.deepcopy(workflow)
    subgraphs = collect_subgraphs(graph)
    notes: list[str] = []
    node = _find(graph, spec["node"])
    if node is None:
        raise ValueError(f"工作流里没有 {spec['node']} 节点")
    aspect = aspect if aspect in ASPECTS else "16:9"
    named = ensure_named(node, subgraphs)
    named["prompt"] = prompt
    named["resolution"] = aspect
    named["aspect_ratio"] = aspect
    named["duration"] = float(duration)
    named["seed"] = int(seed) % (2 ** 32)
    named["greedy"] = True
    named["keep_model_loaded"] = False
    named["bypass"] = False
    named["repeat_last"] = False
    named["library_pick"] = ""
    if spec["node"] != "MiniMaxH3PromptRewriter":
        named["task"] = "I2VA" if image else "T2VA"

    options = combo_options(object_info, spec["node"], "model")
    value, note = resolve_choice(options, model, str(named.get("model") or ""), spec["default_model"])
    named["model"] = value
    if note:
        notes.append(note)

    opts = _find(graph, OPTIONS_CLASS)
    if opts is not None and spec["uses_adapter"]:
        opt_named = ensure_named(opts, subgraphs)
        adapters = combo_options(object_info, OPTIONS_CLASS, "adapter")
        if adapter.strip() or adapters is not None:
            value, note = resolve_choice(adapters, adapter, str(opt_named.get("adapter") or ""), "")
            opt_named["adapter"] = value
            if note:
                notes.append(note.replace("改用默认", "改用默认适配器"))

    bind_images(graph, [image] if image else [])
    return convert_ui(graph, object_info), notes


_LABEL = re.compile(
    r"^[ \t]*[*_#>\-\s]*(" + "|".join(OUTPUT_FIELDS) + r")[*_ \t]*:[*_ \t]*",
    re.IGNORECASE | re.MULTILINE,
)


def split_fields(text: str) -> dict[str, str]:
    fields = {name: "" for name in OUTPUT_FIELDS}
    marks = list(_LABEL.finditer(text or ""))
    for index, mark in enumerate(marks):
        end = marks[index + 1].start() if index + 1 < len(marks) else len(text)
        fields[mark.group(1).lower()] = text[mark.end() : end].strip()
    return fields


def pick_text(outputs: dict) -> str:
    """The longest text a PreviewAny / SaveText node left in ``/history`` outputs."""
    found: list[str] = []
    for node_out in (outputs or {}).values():
        if not isinstance(node_out, dict):
            continue
        value = node_out.get("text")
        if isinstance(value, str):
            value = [value]
        if isinstance(value, (list, tuple)):
            found.extend(str(item) for item in value if isinstance(item, str) and item.strip())
    return max(found, key=len).strip() if found else ""


def demo_rewrite(prompt: str, shot: dict, aspect: str, duration: float, with_image: bool) -> str:
    """Offline stand-in shaped like a real rewrite, so the UI and stage 4 can be exercised."""
    action = (shot.get("action") or "").strip()
    camera = (shot.get("camera") or "medium shot").strip()
    dialogue = (shot.get("dialogue") or "").strip()
    sfx = (shot.get("sfx") or "").strip()
    head = (
        f"Align the opening with the reference image; the clip runs {duration:.2f} seconds.\n"
        if with_image
        else ""
    )
    spoken = f' (S1) says in Chinese: [Chinese] "{dialogue}".' if dialogue else ""
    return (
        f"{head}integrated_multimodal_description: [Shot 1] 0:00 {camera}, {aspect} frame. "
        f"{prompt.rstrip('.')}. {('Story beat: ' + action + '.') if action else ''}{spoken}\n"
        f"overall_soundscape: {sfx or 'quiet room tone, soft rain outside'}, close and natural.\n"
        "non_diegetic_music: none."
    )
