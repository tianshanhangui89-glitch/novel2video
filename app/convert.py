"""Convert ComfyUI UI workflows (including official subgraph templates) to API prompts.

Node class names come from the shipped templates. This module does not invent
node types: it flattens subgraph instances the way ComfyUI's graphToPrompt does
(execution ids like ``459:451``) and copies widget values onto inputs.
"""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path
from typing import Any

WIDGET_TYPES = {"STRING", "INT", "FLOAT", "BOOLEAN", "COMBO"}
SKIP_NODE_TYPES = {"MarkdownNote", "Note", "Note Plus (mtb)"}
DENY_WIDGETS = {"control_after_generate", "upload", "fixed", "videopreview", "preview"}
OUTPUT_CLASSES = {
    "SaveImage",
    "SaveImageAdvanced",
    "SaveVideo",
    "PreviewImage",
    "SaveAnimatedWEBP",
    "PreviewAny",
    "SaveText",
}

# Widget order for templates that store widgets_values as a flat list.
# ``skip`` entries are UI-only controls and are not sent to the API.
SKIP = "skip"
WIDGET_SPECS: dict[str, list[tuple[str, Any]]] = {
    "LoadImage": [("image", str), ("upload", SKIP)],
    "SaveImage": [("filename_prefix", str)],
    "SaveImageAdvanced": [
        ("filename_prefix", str),
        ("format", str),
        ("format.bit_depth", str),
        ("format.input_color_space", str),
    ],
    "SaveVideo": [("filename_prefix", str), ("format", str), ("codec", str)],
    "ResolutionSelector": [("aspect_ratio", str), ("megapixels", float), ("multiple", int)],
    "ImageScaleToTotalPixels": [
        ("upscale_method", str),
        ("megapixels", float),
        ("resolution_steps", int),
    ],
    "CLIPLoader": [("clip_name", str), ("type", str), ("device", str)],
    "UNETLoader": [("unet_name", str), ("weight_dtype", str)],
    "VAELoader": [("vae_name", str)],
    "LoraLoaderModelOnly": [("lora_name", str), ("strength_model", float)],
    "CLIPTextEncode": [("text", str)],
    "KSampler": [
        ("seed", int),
        ("control_after_generate", SKIP),
        ("steps", int),
        ("cfg", float),
        ("sampler_name", str),
        ("scheduler", str),
        ("denoise", float),
    ],
    "KSamplerAdvanced": [
        ("add_noise", str),
        ("noise_seed", int),
        ("control_after_generate", SKIP),
        ("steps", int),
        ("cfg", float),
        ("sampler_name", str),
        ("scheduler", str),
        ("start_at_step", int),
        ("end_at_step", int),
        ("return_with_leftover_noise", str),
    ],
    "KSamplerSelect": [("sampler_name", str)],
    "BasicScheduler": [("scheduler", str), ("steps", int), ("denoise", float)],
    "RandomNoise": [("noise_seed", int), ("control_after_generate", SKIP)],
    "EmptyLatentImage": [("width", int), ("height", int), ("batch_size", int)],
    "WanImageToVideo": [("width", int), ("height", int), ("length", int), ("batch_size", int)],
    "CreateVideo": [("fps", int), ("bit_depth", int)],
    "ModelSamplingSD3": [("shift", float)],
    "PrimitiveInt": [("value", int), ("control_after_generate", SKIP)],
    "PrimitiveFloat": [("value", float)],
    "PrimitiveBoolean": [("value", bool)],
    "PrimitiveString": [("value", str)],
    "PrimitiveStringMultiline": [("value", str)],
    "ComfySwitchNode": [("switch", bool)],
    "ComfyMathExpression": [("expression", str)],
    "TextEncodeQwenImage21": [("prompt", str), ("negative_prompt", str), ("resolution", int)],
    "QwenImage21Cache": [("device", str), ("dtype", str)],
    "MiniMaxH3ImageToVideo": [
        ("prompt", str),
        ("width", int),
        ("height", int),
        ("length", int),
    ],
    "MiniMaxH3ReferenceToVideo": [
        ("prompt", str),
        ("width", int),
        ("height", int),
        ("length", int),
        ("ref_image_size", str),
    ],
    # minimax-h3-prompt-rewriter (pytraveler/MiniMax-H3-Prompt-Rewriter-ComfyUI):
    # required widgets in INPUT_TYPES order, then the optional widget inputs.
    "MiniMaxH3PromptWriter8B": [
        ("prompt", str),
        ("model", str),
        ("task", str),
        ("resolution", str),
        ("duration", float),
        ("quantization", str),
        ("greedy", bool),
        ("seed", int),
        ("control_after_generate", SKIP),
        ("keep_model_loaded", bool),
        ("aspect_ratio", str),
        ("bypass", bool),
        ("repeat_last", bool),
        ("library_pick", str),
    ],
    "MiniMaxH3PromptRewriter": [
        ("prompt", str),
        ("model", str),
        ("resolution", str),
        ("duration", float),
        ("quantization", str),
        ("greedy", bool),
        ("seed", int),
        ("control_after_generate", SKIP),
        ("keep_model_loaded", bool),
        ("aspect_ratio", str),
        ("bypass", bool),
        ("repeat_last", bool),
        ("library_pick", str),
    ],
    "MiniMaxH3GuidedWriter": [
        ("prompt", str),
        ("model", str),
        ("task", str),
        ("resolution", str),
        ("duration", float),
        ("greedy", bool),
        ("seed", int),
        ("control_after_generate", SKIP),
        ("keep_model_loaded", bool),
        ("aspect_ratio", str),
        ("reference_material", str),
        ("bypass", bool),
        ("system_prompt", str),
        ("repeat_last", bool),
        ("library_pick", str),
    ],
    "MiniMaxH3RewriterOptions": [
        ("max_new_tokens", int),
        ("temperature", float),
        ("top_p", float),
        ("top_k", int),
        ("repetition_penalty", float),
        ("attn_implementation", str),
        ("adapter", str),
    ],
    "PreviewAny": [("preview", SKIP)],
}


class ConvertError(RuntimeError):
    pass


def is_api_format(graph: Any) -> bool:
    if not isinstance(graph, dict) or not graph:
        return False
    if isinstance(graph.get("nodes"), list):
        return False
    sample = next(iter(graph.values()))
    return isinstance(sample, dict) and "class_type" in sample


def load_workflow(path: Path) -> dict:
    with path.open(encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ConvertError(f"工作流不是 JSON 对象: {path}")
    return data


def collect_subgraphs(workflow: dict) -> dict[str, dict]:
    found: dict[str, dict] = {}

    def walk(defs: Any) -> None:
        if not isinstance(defs, dict):
            return
        for sg in defs.get("subgraphs") or []:
            if isinstance(sg, dict) and sg.get("id"):
                found[sg["id"]] = sg
                walk(sg.get("definitions"))

    walk(workflow.get("definitions"))
    return found


def _coerce(value: Any, typ: Any) -> Any:
    if typ in (SKIP, "skip"):
        return value
    if typ in ("INT", int):
        if isinstance(value, bool):
            return int(value)
        if isinstance(value, int):
            return value
        return int(float(value))
    if typ in ("FLOAT", float):
        if isinstance(value, bool):
            return float(int(value))
        if isinstance(value, (int, float)):
            return float(value)
        return float(value)
    if typ in ("BOOLEAN", bool):
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return bool(value)
        return str(value).strip().lower() in {"1", "true", "yes", "on"}
    if typ in ("STRING", "COMBO", str):
        return value if isinstance(value, str) else value
    return value


def _infer_subgraph_widgets(node: dict, sg: dict) -> dict[str, Any]:
    lst = node.get("widgets_values") or []
    if not isinstance(lst, list):
        return {}
    slots = [inp for inp in sg.get("inputs") or [] if inp.get("type") in WIDGET_TYPES]
    out: dict[str, Any] = {}
    for inp, raw in zip(slots, lst):
        out[inp["name"]] = _coerce(raw, inp.get("type"))
    return out


def _infer_spec_widgets(node: dict) -> dict[str, Any]:
    spec = WIDGET_SPECS.get(node.get("type") or "")
    lst = node.get("widgets_values") or []
    if not spec or not isinstance(lst, list):
        return {}
    out: dict[str, Any] = {}
    for (name, typ), raw in zip(spec, lst):
        if typ == SKIP:
            continue
        out[name] = _coerce(raw, typ)
    return out


def widget_map(node: dict, subgraphs: dict[str, dict]) -> dict[str, Any]:
    named = node.get("widgets_values_named")
    if isinstance(named, dict) and named:
        return {k: v for k, v in named.items() if k not in DENY_WIDGETS}
    if node.get("type") in subgraphs:
        return _infer_subgraph_widgets(node, subgraphs[node["type"]])
    return _infer_spec_widgets(node)


def ensure_named(node: dict, subgraphs: dict[str, dict]) -> dict[str, Any]:
    named = node.get("widgets_values_named")
    if not isinstance(named, dict) or not named:
        named = widget_map(node, subgraphs)
        node["widgets_values_named"] = named
    return named


def _normalize_links(links: Any) -> dict[Any, dict]:
    out: dict[Any, dict] = {}
    for link in links or []:
        if isinstance(link, dict):
            out[link["id"]] = link
        elif isinstance(link, (list, tuple)) and len(link) >= 5:
            out[link[0]] = {
                "id": link[0],
                "origin_id": link[1],
                "origin_slot": link[2],
                "target_id": link[3],
                "target_slot": link[4],
                "type": link[5] if len(link) > 5 else None,
            }
    return out


class _Graph:
    def __init__(self, workflow_or_sg: dict, subgraphs: dict[str, dict], path: list[Any], input_node_id: Any):
        self.subgraphs = subgraphs
        self.path = path
        self.input_node_id = input_node_id
        self.nodes = {n["id"]: n for n in workflow_or_sg.get("nodes") or []}
        self.links = _normalize_links(workflow_or_sg.get("links"))
        out = workflow_or_sg.get("outputNode") or {}
        self.output_node_id = out.get("id")


def _exec_id(path: list[Any], node_id: Any) -> str:
    return ":".join(str(p) for p in [*path, node_id])


def _resolve(ctx: _Graph, origin_id: Any, origin_slot: int, resolver, seen: set[tuple]) -> tuple[str, Any] | None:
    key = (id(ctx), origin_id, origin_slot)
    if key in seen:
        raise ConvertError(f"工作流连线成环: {origin_id}:{origin_slot}")
    seen.add(key)

    if ctx.input_node_id is not None and origin_id == ctx.input_node_id:
        return resolver(origin_slot) if resolver else None

    node = ctx.nodes.get(origin_id)
    if node is None:
        return None

    if node.get("type") in ctx.subgraphs:
        sg = ctx.subgraphs[node["type"]]
        inner = _Graph(sg, ctx.subgraphs, [*ctx.path, origin_id], (sg.get("inputNode") or {}).get("id"))
        target = None
        for link in inner.links.values():
            if link["target_id"] == inner.output_node_id and link["target_slot"] == origin_slot:
                target = link
                break
        if target is None:
            return None
        return _resolve(inner, target["origin_id"], target["origin_slot"], _make_resolver(node, ctx), seen)

    return ("link", _exec_id(ctx.path, origin_id), int(origin_slot))


def _make_resolver(outer: dict, parent: _Graph):
    """Map a subgraph input-node slot to the parent value.

    Internal links use the subgraph definition's input index. The instance node
    only exposes some of those inputs as sockets, so the slot must be resolved
    by name — not by the instance's socket index.
    """
    sg = parent.subgraphs.get(outer.get("type"))
    if sg is None:
        raise ConvertError(f"缺少子图定义: {outer.get('type')}")
    widgets = widget_map(outer, parent.subgraphs)
    by_name = {inp.get("name"): inp for inp in outer.get("inputs") or []}
    sg_inputs = sg.get("inputs") or []

    def resolver(slot: int):
        if slot < 0 or slot >= len(sg_inputs):
            return None
        name = sg_inputs[slot].get("name")
        outer_inp = by_name.get(name)
        if outer_inp and outer_inp.get("link") is not None:
            link = parent.links.get(outer_inp["link"])
            if not link:
                return None
            return _resolve(parent, link["origin_id"], int(link["origin_slot"]), None, set())
        if name in widgets:
            return ("value", widgets[name])
        return None

    return resolver


def _emit(ctx: _Graph, resolver, into: dict[str, dict]) -> None:
    for node in ctx.nodes.values():
        ntype = node.get("type")
        if ntype in SKIP_NODE_TYPES:
            continue
        if node.get("mode") == 2:
            continue
        if ntype in ctx.subgraphs:
            sg = ctx.subgraphs[ntype]
            inner = _Graph(
                sg,
                ctx.subgraphs,
                [*ctx.path, node["id"]],
                (sg.get("inputNode") or {}).get("id"),
            )
            _emit(inner, _make_resolver(node, ctx), into)
            continue

        inputs: dict[str, Any] = {}
        for key, value in widget_map(node, ctx.subgraphs).items():
            inputs[key] = value

        for inp in node.get("inputs") or []:
            if inp.get("link") is None:
                continue
            link = ctx.links.get(inp["link"])
            if not link:
                continue
            resolved = _resolve(ctx, link["origin_id"], int(link["origin_slot"]), resolver, set())
            name = inp.get("name")
            if not name:
                continue
            if resolved is None:
                inputs.pop(name, None)
                continue
            kind = resolved[0]
            if kind == "value":
                inputs[name] = resolved[1]
            else:
                inputs[name] = [resolved[1], int(resolved[2])]

        into[_exec_id(ctx.path, node["id"])] = {
            "class_type": ntype,
            "inputs": inputs,
            "_meta": {"title": node.get("title") or ntype},
        }


def prune_to_outputs(prompt: dict[str, dict]) -> dict[str, dict]:
    roots = [nid for nid, node in prompt.items() if node.get("class_type") in OUTPUT_CLASSES]
    if not roots:
        raise ConvertError("工作流里没有 SaveImage / SaveVideo 输出节点，无法执行")
    needed: set[str] = set()
    stack = list(roots)
    while stack:
        nid = stack.pop()
        if nid in needed or nid not in prompt:
            continue
        needed.add(nid)
        for value in prompt[nid].get("inputs", {}).values():
            if _is_link(value):
                stack.append(str(value[0]))
    pruned = {nid: prompt[nid] for nid in prompt if nid in needed}
    for node in pruned.values():
        dead = []
        for key, value in node.get("inputs", {}).items():
            if _is_link(value) and str(value[0]) not in pruned:
                dead.append(key)
        for key in dead:
            del node["inputs"][key]
    return pruned


def _is_link(value: Any) -> bool:
    return (
        isinstance(value, list)
        and len(value) == 2
        and isinstance(value[0], str)
        and isinstance(value[1], int)
        and not isinstance(value[1], bool)
    )


def filter_with_object_info(prompt: dict[str, dict], object_info: dict | None) -> dict[str, dict]:
    """Drop literal widget keys the live ComfyUI install does not recognize.

    Links are kept. Dynamic socket names (containing a dot) are kept because
    object_info often omits variadic image slots.
    """
    if not object_info:
        return prompt
    for node in prompt.values():
        info = object_info.get(node.get("class_type"))
        if not isinstance(info, dict):
            continue
        allowed: set[str] = set()
        inp = info.get("input") or {}
        for section in ("required", "optional"):
            block = inp.get(section) or {}
            if isinstance(block, dict):
                allowed.update(block.keys())
        if not allowed:
            continue
        inputs = node.get("inputs") or {}
        for key, value in list(inputs.items()):
            if _is_link(value) or "." in key or key in allowed:
                continue
            del inputs[key]
    return prompt


def convert_ui(workflow: dict, object_info: dict | None = None) -> dict[str, dict]:
    subgraphs = collect_subgraphs(workflow)
    ctx = _Graph(workflow, subgraphs, [], None)
    prompt: dict[str, dict] = {}
    _emit(ctx, None, prompt)
    prompt = prune_to_outputs(prompt)
    return filter_with_object_info(prompt, object_info)


def _link_id(link: Any) -> Any:
    return link["id"] if isinstance(link, dict) else link[0]


def _link_origin(link: Any) -> Any:
    return link["origin_id"] if isinstance(link, dict) else link[1]


def _ref_input(node: dict, inp: dict) -> bool:
    if node.get("type") in SKIP_NODE_TYPES | {
        "ImageCompare",
        "LoadImage",
        "GetImageSize",
        "ImageScaleToTotalPixels",
        "SaveImage",
        "SaveImageAdvanced",
        "SaveVideo",
    }:
        return False
    if inp.get("type") != "IMAGE":
        return False
    name = inp.get("name") or ""
    if name in {"first_frame", "start_image"}:
        return True
    if name.startswith("images.image") or name.startswith("ref_images.ref_image"):
        return True
    return False


def bind_images(workflow: dict, filenames: list[str]) -> None:
    """Replace template LoadImage nodes and connect the first N reference slots."""
    nodes: list[dict] = workflow.setdefault("nodes", [])
    load_ids = {n["id"] for n in nodes if n.get("type") == "LoadImage"}
    remove_links: set[Any] = set()
    kept = []
    for link in workflow.get("links") or []:
        if _link_origin(link) in load_ids:
            remove_links.add(_link_id(link))
        else:
            kept.append(link)
    for node in nodes:
        for inp in node.get("inputs") or []:
            if inp.get("link") in remove_links:
                inp["link"] = None
        for out in node.get("outputs") or []:
            if isinstance(out.get("links"), list):
                out["links"] = [x for x in out["links"] if x not in remove_links]
    workflow["nodes"] = [n for n in nodes if n.get("id") not in load_ids]
    workflow["links"] = kept
    nodes = workflow["nodes"]
    if not filenames:
        return

    targets = []
    for node in nodes:
        for index, inp in enumerate(node.get("inputs") or []):
            if _ref_input(node, inp):
                targets.append((node, index, inp))
    targets.sort(key=lambda item: (0 if item[2].get("link") is not None else 1, item[0]["id"], item[1]))

    last_id = int(workflow.get("last_node_id") or 0)
    last_link = int(workflow.get("last_link_id") or 0)
    for node in nodes:
        last_id = max(last_id, int(node["id"]))
    for link in workflow["links"]:
        last_link = max(last_link, int(_link_id(link)))

    use = min(len(filenames), len(targets))
    for idx in range(use):
        fname = filenames[idx]
        last_id += 1
        last_link += 1
        load = {
            "id": last_id,
            "type": "LoadImage",
            "pos": [40, 40 + idx * 180],
            "size": [320, 280],
            "flags": {},
            "order": 0,
            "mode": 0,
            "inputs": [],
            "outputs": [
                {"name": "IMAGE", "type": "IMAGE", "links": [last_link], "slot_index": 0},
                {"name": "MASK", "type": "MASK", "links": None},
            ],
            "properties": {"Node name for S&R": "LoadImage"},
            "widgets_values": [fname, "image"],
            "widgets_values_named": {"image": fname, "upload": "image"},
            "title": "LoadImage",
        }
        nodes.append(load)
        target, slot, inp = targets[idx]
        inp["link"] = last_link
        workflow["links"].append([last_link, last_id, 0, target["id"], slot, "IMAGE"])
    workflow["last_node_id"] = last_id
    workflow["last_link_id"] = last_link


def bind_prompt(workflow: dict, text: str) -> None:
    subgraphs = collect_subgraphs(workflow)
    hit = False
    for node in workflow.get("nodes") or []:
        if node.get("type") not in subgraphs:
            continue
        sg = subgraphs[node["type"]]
        names = {inp.get("name") for inp in sg.get("inputs") or []}
        named = ensure_named(node, subgraphs)
        if "prompt" in names:
            named["prompt"] = text
            hit = True
        elif "text" in names:
            named["text"] = text
            hit = True
    if hit:
        return
    for node in workflow.get("nodes") or []:
        if node.get("type") == "PrimitiveStringMultiline":
            named = ensure_named(node, subgraphs)
            named["value"] = text
            node["widgets_values"] = [text]
            return
    encodes = [n for n in workflow.get("nodes") or [] if n.get("type") == "CLIPTextEncode"]
    if len(encodes) == 1:
        named = ensure_named(encodes[0], subgraphs)
        named["text"] = text


def bind_size(workflow: dict, width: int, height: int) -> None:
    subgraphs = collect_subgraphs(workflow)
    remove: set[Any] = set()
    for node in workflow.get("nodes") or []:
        for inp in node.get("inputs") or []:
            if inp.get("name") in {"width", "height"} and inp.get("link") is not None:
                remove.add(inp["link"])
                inp["link"] = None
    if remove:
        workflow["links"] = [link for link in workflow.get("links") or [] if _link_id(link) not in remove]
        for node in workflow.get("nodes") or []:
            for out in node.get("outputs") or []:
                if isinstance(out.get("links"), list):
                    out["links"] = [x for x in out["links"] if x not in remove]

    size_types = {
        "MiniMaxH3ReferenceToVideo",
        "MiniMaxH3ImageToVideo",
        "EmptyLatentImage",
        "WanImageToVideo",
    }
    for node in workflow.get("nodes") or []:
        if node.get("type") in subgraphs:
            names = {inp.get("name") for inp in subgraphs[node["type"]].get("inputs") or []}
            if "width" not in names and "height" not in names:
                continue
            named = ensure_named(node, subgraphs)
            if "width" in names:
                named["width"] = int(width)
            if "height" in names:
                named["height"] = int(height)
        elif node.get("type") in size_types:
            named = ensure_named(node, subgraphs)
            named["width"] = int(width)
            named["height"] = int(height)


def bind_duration(workflow: dict, seconds: float) -> None:
    subgraphs = collect_subgraphs(workflow)
    for node in workflow.get("nodes") or []:
        if node.get("type") not in subgraphs:
            continue
        inputs = {inp.get("name"): inp for inp in subgraphs[node["type"]].get("inputs") or []}
        slot = inputs.get("value_1")
        if slot and slot.get("type") == "FLOAT":
            named = ensure_named(node, subgraphs)
            named["value_1"] = float(seconds)

    links = _normalize_links(workflow.get("links"))
    nodes = {n["id"]: n for n in workflow.get("nodes") or []}
    for node in workflow.get("nodes") or []:
        if node.get("type") != "ComfyMathExpression":
            continue
        for inp in node.get("inputs") or []:
            if inp.get("name") != "values.a" or inp.get("link") is None:
                continue
            link = links.get(inp["link"])
            if not link:
                continue
            src = nodes.get(link["origin_id"])
            if src and src.get("type") == "PrimitiveFloat":
                named = ensure_named(src, subgraphs)
                named["value"] = float(seconds)


def bind_seed(workflow: dict, seed: int) -> None:
    subgraphs = collect_subgraphs(workflow)
    for node in workflow.get("nodes") or []:
        if node.get("type") in subgraphs:
            names = {inp.get("name") for inp in subgraphs[node["type"]].get("inputs") or []}
            named = ensure_named(node, subgraphs)
            if "seed" in names:
                named["seed"] = int(seed)
            if "noise_seed" in names:
                named["noise_seed"] = int(seed)
        elif node.get("type") in {"RandomNoise", "KSampler", "KSamplerAdvanced"}:
            named = ensure_named(node, subgraphs)
            if "noise_seed" in named:
                named["noise_seed"] = int(seed)
            if "seed" in named:
                named["seed"] = int(seed)


def bind_prefix(workflow: dict, prefix: str) -> None:
    subgraphs = collect_subgraphs(workflow)
    for node in workflow.get("nodes") or []:
        if node.get("type") in {"SaveImage", "SaveImageAdvanced", "SaveVideo"}:
            named = ensure_named(node, subgraphs)
            named["filename_prefix"] = prefix


def prepare_ui(workflow: dict, job: dict) -> dict:
    graph = copy.deepcopy(workflow)
    if job.get("prompt"):
        bind_prompt(graph, job["prompt"])
    if job.get("images") is not None:
        bind_images(graph, list(job["images"]))
    if job.get("duration_sec") is not None:
        bind_duration(graph, float(job["duration_sec"]))
    if job.get("seed") is not None:
        bind_seed(graph, int(job["seed"]))
    if job.get("filename_prefix"):
        bind_prefix(graph, job["filename_prefix"])
    if job.get("force_size") and job.get("width") and job.get("height"):
        bind_size(graph, int(job["width"]), int(job["height"]))
    return graph


_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)


def inject_api(prompt: dict, job: dict, object_info: dict | None = None) -> dict:
    """Patch an already-exported API workflow. Only touches known input names."""
    graph = copy.deepcopy(prompt)
    images = list(job.get("images") or [])
    loaders = [node for node in graph.values() if node.get("class_type") == "LoadImage"]
    for node, name in zip(loaders, images):
        node.setdefault("inputs", {})["image"] = name
    text = job.get("prompt")
    if text:
        prompt_nodes = []
        for node in graph.values():
            inputs = node.setdefault("inputs", {})
            ctype = node.get("class_type")
            if isinstance(inputs.get("prompt"), str) and ctype != "CLIPTextEncode":
                prompt_nodes.append(node)
            elif ctype == "PrimitiveStringMultiline" and isinstance(inputs.get("value"), str):
                prompt_nodes.append(node)
        if prompt_nodes:
            target = prompt_nodes[0]
            if "prompt" in target["inputs"] and isinstance(target["inputs"]["prompt"], str):
                target["inputs"]["prompt"] = text
            elif "value" in target["inputs"]:
                target["inputs"]["value"] = text
        else:
            encodes = [n for n in graph.values() if n.get("class_type") == "CLIPTextEncode"]
            if encodes:
                encodes[0].setdefault("inputs", {})["text"] = text
    if job.get("filename_prefix"):
        for node in graph.values():
            if node.get("class_type") in OUTPUT_CLASSES and "filename_prefix" in node.get("inputs", {}):
                node["inputs"]["filename_prefix"] = job["filename_prefix"]
    if job.get("seed") is not None:
        for node in graph.values():
            inputs = node.get("inputs") or {}
            if isinstance(inputs.get("seed"), int):
                inputs["seed"] = int(job["seed"])
            if isinstance(inputs.get("noise_seed"), int):
                inputs["noise_seed"] = int(job["seed"])
    if job.get("force_size") and job.get("width") and job.get("height"):
        for node in graph.values():
            inputs = node.get("inputs") or {}
            if isinstance(inputs.get("width"), int):
                inputs["width"] = int(job["width"])
            if isinstance(inputs.get("height"), int):
                inputs["height"] = int(job["height"])
    return filter_with_object_info(graph, object_info)


def build_prompt(workflow: dict, job: dict | None = None, object_info: dict | None = None) -> dict[str, dict]:
    job = job or {}
    if is_api_format(workflow):
        prompt = inject_api(workflow, job, object_info)
        if any(_UUID.match(node.get("class_type") or "") for node in prompt.values()):
            raise ConvertError("API 工作流仍包含子图 UUID，请在 ComfyUI 中使用 Export Workflow (API) 重新导出")
        return prompt
    prepared = prepare_ui(workflow, job)
    prompt = convert_ui(prepared, object_info)
    bad = [node["class_type"] for node in prompt.values() if _UUID.match(node.get("class_type") or "")]
    if bad:
        raise ConvertError("子图未被展开: " + ", ".join(bad[:4]))
    return prompt


def build_prompt_file(path: Path, job: dict | None = None, object_info: dict | None = None) -> dict[str, dict]:
    return build_prompt(load_workflow(path), job, object_info)


def dimensions(aspect: str, kind: str) -> tuple[int, int]:
    """Pixel sizes used when injecting width/height.

    H3 16:9 follows the template note: 1344×768 (about 0.98 MP). Wan stays
    smaller for the low-VRAM branch.
    """
    aspect = aspect if aspect in {"16:9", "9:16", "1:1"} else "16:9"
    table = {
        "image": {"16:9": (1344, 768), "9:16": (768, 1344), "1:1": (1024, 1024)},
        "h3": {"16:9": (1344, 768), "9:16": (768, 1344), "1:1": (1024, 1024)},
        "wan": {"16:9": (832, 480), "9:16": (480, 832), "1:1": (640, 640)},
    }
    return table.get(kind, table["image"])[aspect]
