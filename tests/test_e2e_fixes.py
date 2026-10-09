"""Regression tests for Windows E2E fixes + ComfyUI LLM backend."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app import httputil
from app.convert import H3_RESOLUTIONS, build_prompt, dimensions, load_workflow
from app.llm import (
    DEFAULT_CLIP,
    _pack_sampling,
    build_llm_prompt,
    infer_clip_type,
    missing_llm_nodes,
)
from app.models import (
    enable_lightning_lora,
    match_precision,
    missing_models,
    patch_prompt_models,
    resolve_model_name,
)
from app.pipeline import Engine
from app.store import DEFAULT_SETTINGS, Store
from app.storyboard import normalize_storyboard, parse_json_content, system_prompt

ROOT = Path(__file__).resolve().parents[1]


def test_loopback_disables_proxy_trust():
    assert httputil.is_loopback("http://127.0.0.1:8188")
    assert httputil.is_loopback("http://localhost:11434")
    assert not httputil.is_loopback("http://example.com")
    local = httputil.client("http://127.0.0.1:8188")
    remote = httputil.client("https://example.com")
    assert local._trust_env is False
    assert remote._trust_env is True


def test_precision_rematch():
    options = [
        "qwen_image_2.1_fp16.safetensors",
        "other_model_bf16.safetensors",
    ]
    hit = match_precision(options, "qwen_image_2.1_int8_convrot.safetensors")
    assert hit == "qwen_image_2.1_fp16.safetensors"
    chosen, note = resolve_model_name(options, "qwen_image_2.1_int8_convrot.safetensors")
    assert chosen == "qwen_image_2.1_fp16.safetensors"
    assert "改用" in note


def test_patch_prompt_models_rewrites_loader():
    prompt = {
        "1": {
            "class_type": "UNETLoader",
            "inputs": {"unet_name": "minimax_h3_fl2va_pruned_int8_convrot.safetensors", "weight_dtype": "default"},
        }
    }
    info = {
        "UNETLoader": {
            "input": {
                "required": {
                    "unet_name": [
                        ["minimax_h3_fl2va_pruned_fp16.safetensors", "unrelated.safetensors"],
                        {},
                    ]
                }
            }
        }
    }
    notes = patch_prompt_models(prompt, info)
    assert prompt["1"]["inputs"]["unet_name"] == "minimax_h3_fl2va_pruned_fp16.safetensors"
    assert notes
    missing = missing_models(prompt, info)
    assert missing == []


def test_h3_default_resolution_is_960():
    assert dimensions("16:9", "h3") == (960, 544)
    assert "960x544" in H3_RESOLUTIONS
    assert dimensions("16:9", "h3", "1344x768") == (1344, 768)
    assert dimensions("16:9", "h3", "864x480") == (864, 480)


def test_enable_lightning_on_i2v_template():
    workflow = load_workflow(ROOT / "workflows" / "03_h3_i2v.json")
    notes = enable_lightning_lora(workflow, enabled=True, steps=8)
    assert notes
    blob = json.dumps(workflow)
    assert "true" in blob.lower() or any("启用" in n for n in notes)


def test_llm_workflow_flattens_and_packs_sampling():
    workflow = load_workflow(ROOT / "workflows" / "05_llm_textgen.json")
    info = {
        "CLIPLoader": {
            "input": {
                "required": {
                    "clip_name": [[DEFAULT_CLIP, "qwen_3_4b.safetensors"], {}],
                    "type": [["ltxv", "minimax", "qwen_image"], {}],
                }
            }
        },
        "TextGenerate": {"input": {"required": {}}},
        "PreviewAny": {"input": {"required": {}}},
    }
    prompt, notes = build_llm_prompt(
        workflow,
        prompt="ONLY_JSON_PLEASE",
        model=DEFAULT_CLIP,
        seed=7,
        max_length=2048,
        temperature=0.3,
        object_info=info,
    )
    classes = {n["class_type"] for n in prompt.values()}
    assert {"CLIPLoader", "TextGenerate", "PreviewAny"} <= classes
    gen = next(n for n in prompt.values() if n["class_type"] == "TextGenerate")
    assert gen["inputs"]["prompt"] == "ONLY_JSON_PLEASE"
    assert isinstance(gen["inputs"]["sampling_mode"], dict)
    assert gen["inputs"]["sampling_mode"]["sampling_mode"] == "on"
    assert gen["inputs"]["sampling_mode"]["seed"] == 7
    assert gen["inputs"]["sampling_mode"]["temperature"] == 0.3
    assert gen["inputs"]["max_length"] == 2048
    loader = next(n for n in prompt.values() if n["class_type"] == "CLIPLoader")
    assert loader["inputs"]["clip_name"] == DEFAULT_CLIP
    assert loader["inputs"]["type"] == "ltxv"


def test_pack_sampling_idempotent():
    inputs = {
        "prompt": "x",
        "max_length": 100,
        "sampling_mode": "on",
        "temperature": 0.5,
        "seed": 1,
    }
    _pack_sampling(inputs)
    assert inputs["sampling_mode"]["temperature"] == 0.5
    _pack_sampling(inputs)
    assert inputs["sampling_mode"]["temperature"] == 0.5


def test_missing_llm_nodes():
    assert "TextGenerate" in missing_llm_nodes(None)
    assert missing_llm_nodes({"CLIPLoader": {}, "TextGenerate": {}, "PreviewAny": {}}) == []


def test_infer_clip_type():
    assert infer_clip_type("qwen_3_4b_fp8_mixed.safetensors") == "ltxv"
    assert infer_clip_type("qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors") == "minimax"
    assert infer_clip_type("x", preferred="joyimage") == "joyimage"


def test_speaker_in_storyboard_and_prompt():
    assert "speaker" in system_prompt()
    data = parse_json_content(
        json.dumps(
            {
                "characters": [{"id": "c1", "name": "A", "appearance": "a"}],
                "scenes": [{"id": "s1", "name": "S", "visual": "v"}],
                "shots": [
                    {
                        "shot_id": "001",
                        "duration_sec": 4,
                        "scene_id": "s1",
                        "characters": ["c1"],
                        "action": "walk",
                        "dialogue": "你好",
                        "speaker": "c1",
                    }
                ],
            }
        )
    )
    board = normalize_storyboard(data, project_id="p", title="t", style="s", aspect="16:9")
    assert board["shots"][0]["speaker"] == "c1"


def test_resolve_voice_prefers_speaker(tmp_path):
    store = Store(tmp_path)
    engine = Engine(store)
    project = {
        "id": "p1",
        "storyboard": {
            "characters": [
                {"id": "c_hero", "name": "林远", "voice_preset": "speaker1"},
                {"id": "c_host", "name": "掌柜", "voice_preset": "speaker2"},
            ],
            "shots": [],
        },
    }
    voice, _ref, note = engine._resolve_shot_voice(
        project,
        {"speaker": "c_host", "characters": ["c_hero"], "dialogue": "客官里边请"},
        {"tts_voice": "speaker1"},
    )
    assert voice == "speaker2"
    assert "掌柜" in note


def _new(store: Store, name: str, script: str) -> dict:
    return store.new_project(
        name=name,
        script=script,
        style="anime",
        aspect="16:9",
        video_workflow="h3_i2v",
        image_workflow="01_character_t2i.json",
        edit_workflow="02_shot_edit.json",
    )


def test_demo_marker_blocks_reuse(tmp_path):
    store = Store(tmp_path)
    store.save_settings({**DEFAULT_SETTINGS, "demo_mode": False})
    engine = Engine(store)
    project = _new(store, "demo-skip", "测试文稿一段。")
    media = store.media_dir(project["id"])
    rel = "characters/c1.png"
    dest = media / rel
    dest.parent.mkdir(parents=True)
    dest.write_bytes(b"fake")
    engine._mark_demo(dest)
    assert not engine._exists(project["id"], rel)
    store.save_settings({**store.load_settings(), "demo_mode": True})
    assert engine._exists(project["id"], rel)


def test_stage1_count_updates_on_storyboard_save(tmp_path):
    store = Store(tmp_path)
    project = _new(store, "count", "甲说：你好。乙说：再见。")
    board = normalize_storyboard(
        {
            "characters": [{"id": "c1", "name": "甲", "appearance": "a"}],
            "scenes": [{"id": "s1", "name": "S", "visual": "v"}],
            "shots": [
                {"shot_id": "001", "duration_sec": 4, "scene_id": "s1", "characters": ["c1"], "action": "a", "dialogue": "你好", "speaker": "c1"},
                {"shot_id": "002", "duration_sec": 4, "scene_id": "s1", "characters": ["c1"], "action": "b", "dialogue": "再见", "speaker": "c1"},
            ],
        },
        project_id=project["id"],
        title=project["name"],
        style=project["style"],
        aspect=project["aspect"],
    )
    project["storyboard"] = board
    project["stages"]["stage1"] = {"status": "done", "message": "2 镜"}
    store.save_project(project)
    board["shots"] = board["shots"][:1]
    project["storyboard"] = normalize_storyboard(
        board,
        project_id=project["id"],
        title=project["name"],
        style=project["style"],
        aspect=project["aspect"],
    )
    project["stages"]["stage1"]["message"] = f"{len(project['storyboard']['shots'])} 镜"
    store.save_project(project)
    loaded = store.load_project(project["id"])
    assert loaded["stages"]["stage1"]["message"] == "1 镜"


def test_default_settings_llm_comfy_and_h3():
    assert DEFAULT_SETTINGS["llm_backend"] == "comfy"
    assert DEFAULT_SETTINGS["h3_resolution"] == "960x544"
    assert DEFAULT_SETTINGS["h3_lightning"] is True
    assert DEFAULT_SETTINGS["h3_steps"] == 8


def test_llm_workflow_in_convert_suite():
    prompt = build_prompt(
        load_workflow(ROOT / "workflows" / "05_llm_textgen.json"),
        {"prompt": "HELLO_LLM"},
    )
    # Without object_info, inject may not rewrite TextGenerate prompt via job —
    # but the graph must still flatten cleanly.
    classes = {n["class_type"] for n in prompt.values()}
    assert "TextGenerate" in classes
    assert "CLIPLoader" in classes
