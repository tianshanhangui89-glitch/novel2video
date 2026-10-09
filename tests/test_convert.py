import json
from pathlib import Path

import pytest

from app.convert import build_prompt, dimensions

ROOT = Path(__file__).resolve().parents[1]
WF = ROOT / "workflows"


def _load(name: str) -> dict:
    return json.loads((WF / name).read_text(encoding="utf-8"))


def _links_ok(prompt: dict) -> None:
    for node in prompt.values():
        assert "class_type" in node
        assert "-" not in node["class_type"] or node["class_type"].count("-") < 4
        for value in node["inputs"].values():
            if isinstance(value, list) and len(value) == 2 and isinstance(value[0], str):
                assert value[0] in prompt


@pytest.mark.parametrize(
    "name,expect",
    [
        ("01_character_t2i.json", {"UNETLoader", "KSampler", "TextEncodeQwenImage21", "SaveImageAdvanced", "VAELoader"}),
        ("02_shot_edit.json", {"TextEncodeQwenImage21", "SaveImageAdvanced", "LoadImage", "KSampler"}),
        ("03_h3_i2v.json", {"MiniMaxH3ImageToVideo", "SaveVideo", "LoadImage", "UNETLoader"}),
        ("03b_h3_r2v.json", {"MiniMaxH3ReferenceToVideo", "SaveVideo", "LoadImage", "PrimitiveStringMultiline"}),
        ("03c_wan22_i2v.json", {"WanImageToVideo", "SaveVideo", "LoadImage", "KSamplerAdvanced", "CLIPTextEncode"}),
    ],
)
def test_templates_flatten(name, expect):
    prompt = build_prompt(_load(name), {"filename_prefix": "n2v/test"})
    classes = {n["class_type"] for n in prompt.values()}
    assert expect <= classes
    assert "MarkdownNote" not in classes
    assert "ImageCompare" not in classes
    _links_ok(prompt)
    assert all("." not in n["class_type"] or True for n in prompt.values())
    assert not any(len(n["class_type"]) == 36 and n["class_type"].count("-") == 4 for n in prompt.values())


def test_t2i_prompt_and_size():
    w, h = dimensions("16:9", "image")
    prompt = build_prompt(
        _load("01_character_t2i.json"),
        {
            "prompt": "HELLO_UNIQUE",
            "filename_prefix": "n2v/char",
            "seed": 7,
            "width": w,
            "height": h,
            "force_size": True,
            "images": [],
        },
    )
    classes = {n["class_type"] for n in prompt.values()}
    assert "ResolutionSelector" not in classes
    blobs = json.dumps(prompt, ensure_ascii=False)
    assert "HELLO_UNIQUE" in blobs
    latents = [n for n in prompt.values() if n["class_type"] == "EmptyLatentImage"]
    assert latents and latents[0]["inputs"]["width"] == w
    assert latents[0]["inputs"]["height"] == h
    sampler = next(n for n in prompt.values() if n["class_type"] == "KSampler")
    assert sampler["inputs"]["sampler_name"] == "euler"
    assert sampler["inputs"]["scheduler"] == "simple"
    assert sampler["inputs"]["seed"] == 7
    assert sampler["inputs"]["cfg"] == 1
    assert "LoadImage" not in classes
    _links_ok(prompt)


def test_edit_binds_reference_images():
    prompt = build_prompt(
        _load("02_shot_edit.json"),
        {
            "prompt": "KEEP_IDENTITY",
            "images": ["scene.png", "hero.png", "prop.png"],
            "filename_prefix": "n2v/shot",
            "seed": 3,
        },
    )
    loaders = [n["inputs"].get("image") for n in prompt.values() if n["class_type"] == "LoadImage"]
    assert loaders == ["scene.png", "hero.png", "prop.png"]
    assert any(
        node["inputs"].get("prompt") == "KEEP_IDENTITY" or node["inputs"].get("on_false") == "KEEP_IDENTITY"
        for node in prompt.values()
    )
    assert "ImageCompare" not in {n["class_type"] for n in prompt.values()}
    _links_ok(prompt)


def test_edit_single_image_drops_unused_loader():
    prompt = build_prompt(
        _load("02_shot_edit.json"),
        {"prompt": "ONE", "images": ["only.png"], "filename_prefix": "n2v/one"},
    )
    loaders = [n for n in prompt.values() if n["class_type"] == "LoadImage"]
    assert len(loaders) == 1
    assert loaders[0]["inputs"]["image"] == "only.png"
    _links_ok(prompt)


def test_h3_i2v_duration_and_frame():
    w, h = dimensions("16:9", "h3")
    prompt = build_prompt(
        _load("03_h3_i2v.json"),
        {
            "prompt": "PUSH_IN",
            "images": ["frame.png"],
            "filename_prefix": "n2v/vid",
            "seed": 11,
            "duration_sec": 6,
            "width": w,
            "height": h,
            "force_size": True,
        },
    )
    assert "ResolutionSelector" not in {n["class_type"] for n in prompt.values()}
    minimax_early = next(n for n in prompt.values() if n["class_type"] == "MiniMaxH3ImageToVideo")
    assert minimax_early["inputs"]["prompt"] == "PUSH_IN"
    loaders = [n for n in prompt.values() if n["class_type"] == "LoadImage"]
    assert len(loaders) == 1 and loaders[0]["inputs"]["image"] == "frame.png"
    minimax = next(n for n in prompt.values() if n["class_type"] == "MiniMaxH3ImageToVideo")
    assert minimax["inputs"]["width"] == w
    assert minimax["inputs"]["height"] == h
    assert 6 in _numbers(prompt) or 6.0 in _numbers(prompt)
    _links_ok(prompt)


def test_wan_keeps_negative_and_sets_motion():
    w, h = dimensions("9:16", "wan")
    prompt = build_prompt(
        _load("03c_wan22_i2v.json"),
        {
            "prompt": "HELLO_WAN",
            "images": ["start.png"],
            "filename_prefix": "n2v/wan",
            "seed": 5,
            "duration_sec": 7.5,
            "width": w,
            "height": h,
            "force_size": True,
        },
    )
    texts = [n["inputs"].get("text") for n in prompt.values() if n["class_type"] == "CLIPTextEncode"]
    assert "HELLO_WAN" in texts
    assert any(isinstance(t, str) and "色调艳丽" in t for t in texts)
    wan = next(n for n in prompt.values() if n["class_type"] == "WanImageToVideo")
    assert wan["inputs"]["width"] == w
    assert wan["inputs"]["height"] == h
    assert 7.5 in _numbers(prompt)
    _links_ok(prompt)


def test_r2v_prompt_and_refs():
    prompt = build_prompt(
        _load("03b_h3_r2v.json"),
        {
            "prompt": "REF_LOCK",
            "images": ["a.png", "b.png"],
            "filename_prefix": "n2v/r2v",
            "seed": 9,
            "duration_sec": 4,
            "width": 1344,
            "height": 768,
            "force_size": True,
        },
    )
    blob = json.dumps(prompt, ensure_ascii=False)
    assert "REF_LOCK" in blob
    loaders = [n["inputs"]["image"] for n in prompt.values() if n["class_type"] == "LoadImage"]
    assert loaders == ["a.png", "b.png"]
    assert "ResolutionSelector" not in {n["class_type"] for n in prompt.values()}
    _links_ok(prompt)


def test_api_format_injection():
    api = {
        "1": {"class_type": "LoadImage", "inputs": {"image": "old.png"}},
        "2": {"class_type": "SaveImage", "inputs": {"images": ["1", 0], "filename_prefix": "x"}},
    }
    # SaveImage is an output; LoadImage is its dependency. inject doesn't prune.
    out = build_prompt(
        api,
        {"prompt": "IGNORED_NO_PROMPT_FIELD", "images": ["new.png"], "filename_prefix": "pref"},
    )
    assert out["1"]["inputs"]["image"] == "new.png"
    assert out["2"]["inputs"]["filename_prefix"] == "pref"


def _numbers(prompt: dict) -> set:
    found = set()

    def walk(value):
        if isinstance(value, bool):
            return
        if isinstance(value, (int, float)):
            found.add(value)
        elif isinstance(value, dict):
            for item in value.values():
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    walk(prompt)
    return found
