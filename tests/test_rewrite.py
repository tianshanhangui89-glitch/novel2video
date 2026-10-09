import asyncio
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.comfy import ComfyClient
from app.convert import load_workflow
from app.demo_media import write_card
from app.main import build_app
from app.pipeline import Engine
from app.rewrite import (
    VARIANTS,
    build_rewrite_prompt,
    combo_options,
    missing_nodes,
    pick_text,
    resolve_choice,
    split_fields,
)
from app.store import ROOT, Store
from app.storyboard import mock_storyboard

CHAPTER = (ROOT / "input" / "chapter01.md").read_text(encoding="utf-8")
WORKFLOWS = ROOT / "workflows"

REWRITTEN = (
    "integrated_multimodal_description: [Shot 1] 0:00 A young swordsman pushes the inn door open.\n"
    "overall_soundscape: steady rain on the eaves, creaking hinge.\n"
    "non_diegetic_music: none."
)


def _flatten(key: str, **kwargs):
    spec = VARIANTS[key]
    workflow = load_workflow(WORKFLOWS / spec["file"])
    base = dict(spec=spec, prompt="a man walks in", aspect="9:16", duration=6.5, seed=2**33 + 7)
    base.update(kwargs)
    return build_rewrite_prompt(workflow, **base)


def _node(graph: dict, class_type: str) -> tuple[str, dict]:
    return next((nid, node) for nid, node in graph.items() if node["class_type"] == class_type)


def _object_info_for(key: str, models: list[str], adapters: list[str]) -> dict:
    """object_info that knows every flattened input, with model/adapter combos in both schemas."""
    graph, _ = _flatten(key, image="probe.png" if VARIANTS[key]["uses_image"] else None)
    info: dict = {}
    for node in graph.values():
        required = {name: ["STRING", {}] for name in node["inputs"]}
        info[node["class_type"]] = {"input": {"required": required}}
    info[VARIANTS[key]["node"]]["input"]["required"]["model"] = ["COMBO", {"options": models}]
    if "MiniMaxH3RewriterOptions" in info:
        info["MiniMaxH3RewriterOptions"]["input"]["required"]["adapter"] = [adapters, {}]
    info.setdefault("LoadImage", {"input": {"required": {"image": [[], {}]}}})
    return info


@pytest.mark.parametrize("key", ["8b", "27b", "writer"])
def test_rewrite_templates_flatten(key):
    spec = VARIANTS[key]
    graph, notes = _flatten(key)
    assert notes == []
    nid, node = _node(graph, spec["node"])
    inputs = node["inputs"]
    assert inputs["prompt"] == "a man walks in"
    assert inputs["duration"] == 6.5
    assert inputs["seed"] == 7
    assert inputs["keep_model_loaded"] is False
    assert inputs["model"]
    if "aspect_ratio" in inputs:
        assert inputs["aspect_ratio"] == "9:16"
    if key != "27b":
        assert inputs["task"] == "T2VA"
        assert inputs["resolution"] == "9:16"
    preview_id, preview = _node(graph, "PreviewAny")
    assert preview["inputs"]["source"] == [nid, 0]
    assert all(n["class_type"] != "LoadImage" for n in graph.values())
    if spec["uses_adapter"]:
        options_id, options = _node(graph, "MiniMaxH3RewriterOptions")
        assert inputs["options"] == [options_id, 0]
        assert options["inputs"]["adapter"] == "lightx2v/MiniMax-H3-Prompt-Rewriter-LoRA"
    else:
        assert all(n["class_type"] != "MiniMaxH3RewriterOptions" for n in graph.values())


def test_rewrite_8b_feeds_keyframe_as_first_frame():
    graph, _ = _flatten("8b", image="n2v_key_001.png", aspect="16:9")
    _, writer = _node(graph, "MiniMaxH3PromptWriter8B")
    load_id, load = _node(graph, "LoadImage")
    assert load["inputs"]["image"] == "n2v_key_001.png"
    assert writer["inputs"]["first_frame"] == [load_id, 0]
    assert writer["inputs"]["task"] == "I2VA"
    assert writer["inputs"]["resolution"] == "16:9"


def test_combo_options_both_schemas_and_resolve_choice():
    info = {
        "A": {"input": {"required": {"model": [["Qwen3.5-4B · 2.6 GB", "Gemma 3 12B Instruct"], {}]}}},
        "B": {"input": {"optional": {"model": ["COMBO", {"options": ["x", "y"]}]}}},
    }
    assert combo_options(info, "A", "model") == ["Qwen3.5-4B · 2.6 GB", "Gemma 3 12B Instruct"]
    assert combo_options(info, "B", "model") == ["x", "y"]
    assert combo_options(info, "C", "model") is None
    assert combo_options(None, "A", "model") is None

    options = [
        "Qwen3-VL-8B-Instruct GGUF Q4_K_M · 5.4 GB download · ~9 GB with the adapter",
        "Qwen3-VL-8B-Instruct GGUF Q8_0 · 8.8 GB download · ~13 GB with the adapter",
    ]
    assert resolve_choice(options, "q8_0", "", "")[0] == options[1]
    assert resolve_choice(options, "Qwen3-VL-8B-Instruct-GGUF-Q8_0.gguf", "", "")[0] == options[1]
    value, note = resolve_choice(options, "nope-model", "", "Qwen3-VL-8B-Instruct GGUF Q4_K_M")
    assert value == options[0] and "nope-model" in note
    assert resolve_choice(None, "", "template default", "") == ("template default", "")
    assert resolve_choice(None, "my.gguf", "template default", "") == ("my.gguf", "")


def test_live_dropdowns_are_matched_when_building():
    models = ["Qwen3.5-4B · 2.6 GB download", "Qwen3.5-9B · 5.3 GB download"]
    info = _object_info_for("writer", models, [])
    graph, notes = _flatten("writer", model="Qwen3.5-9B-Q4_K_M.gguf", object_info=info)
    assert _node(graph, "MiniMaxH3GuidedWriter")[1]["inputs"]["model"] == models[1]
    assert notes == []
    graph, notes = _flatten("writer", model="does-not-exist", object_info=info)
    assert _node(graph, "MiniMaxH3GuidedWriter")[1]["inputs"]["model"] == models[0]
    assert notes and "does-not-exist" in notes[0]


def test_missing_nodes_pick_text_split_fields():
    spec = VARIANTS["8b"]
    assert set(missing_nodes(None, spec, True)) >= {"MiniMaxH3PromptWriter8B", "PreviewAny"}
    assert missing_nodes({"LoadImage": {}, "SaveText": {}}, spec, True) == [
        "MiniMaxH3PromptWriter8B",
        "MiniMaxH3RewriterOptions",
    ]
    ready = {"MiniMaxH3PromptWriter8B": {}, "MiniMaxH3RewriterOptions": {}, "PreviewAny": {}}
    assert missing_nodes(ready, spec, False) == []
    assert missing_nodes(ready, spec, True) == ["LoadImage"]
    assert missing_nodes({"MiniMaxH3GuidedWriter": {}, "PreviewAny": {}}, VARIANTS["writer"], False) == []

    outputs = {"5": {"text": ["short", REWRITTEN]}, "9": {"images": []}, "7": {"text": "x"}}
    assert pick_text(outputs) == REWRITTEN
    assert pick_text({}) == ""
    fields = split_fields("**integrated_multimodal_description:** A\nB\noverall_soundscape: rain\nnon_diegetic_music: none.")
    assert fields == {
        "integrated_multimodal_description": "A\nB",
        "overall_soundscape": "rain",
        "non_diegetic_music": "none.",
    }


def _project_with_keyframes(tmp_path: Path, settings_update: dict, video_workflow="h3_i2v"):
    store = Store(tmp_path, ROOT)
    settings = store.load_settings()
    settings.update(settings_update)
    store.save_settings(settings)
    project = store.new_project(
        name="客栈夜雨",
        script=CHAPTER,
        style="anime",
        aspect="9:16",
        video_workflow=video_workflow,
        image_workflow="01_character_t2i.json",
        edit_workflow="02_shot_edit.json",
    )
    board = mock_storyboard(CHAPTER, project_id=project["id"], title="客栈夜雨", style="anime", aspect="9:16")
    for shot in board["shots"]:
        rel = f"shots/{shot['shot_id']}_key.png"
        write_card(store.media_dir(project["id"]) / rel, shot["shot_id"], "key", "9:16", "关键帧")
        shot["keyframe"] = rel
    project["storyboard"] = board
    store.save_project(project)
    return store, Engine(store), project


def test_rewrite_falls_back_when_nodes_missing(tmp_path, monkeypatch):
    store, engine, project = _project_with_keyframes(tmp_path, {"rewrite_enabled": True, "demo_mode": False})

    async def object_info(self):
        return {"LoadImage": {}, "KSampler": {}}

    async def never(*args, **kwargs):
        raise AssertionError("must not queue when nodes are missing")

    monkeypatch.setattr(ComfyClient, "object_info", object_info)
    monkeypatch.setattr(ComfyClient, "queue_and_wait_outputs", never)
    produced = asyncio.run(engine._stage_rewrite(project["id"]))
    assert produced is False
    current = store.load_project(project["id"])
    assert current["stages"]["stage3r"]["status"] == "done"
    for shot in current["storyboard"]["shots"]:
        assert shot["h3_prompt_source"] == "fallback"
        assert shot["h3_prompt"] == shot["h3_prompt_original"]
        assert engine._video_prompt(current, shot) == engine._motion_prompt(current, shot)
    warnings = [line["message"] for line in current["logs"] if line["level"] == "warning"]
    assert any("MiniMaxH3PromptWriter8B" in line for line in warnings)


def test_rewrite_failure_on_one_shot_keeps_original(tmp_path, monkeypatch):
    store, engine, project = _project_with_keyframes(
        tmp_path, {"rewrite_enabled": True, "demo_mode": False, "rewrite_variant": "writer"}
    )
    info = _object_info_for("writer", ["Qwen3.5-4B · 2.6 GB"], [])
    calls = []

    async def object_info(self):
        return info

    async def queue(self, prompt, log, timeout=3600, should_stop=None):
        calls.append(prompt)
        if len(calls) == 1:
            raise RuntimeError("CUDA out of memory")
        return {"5": {"text": [REWRITTEN]}}

    monkeypatch.setattr(ComfyClient, "object_info", object_info)
    monkeypatch.setattr(ComfyClient, "queue_and_wait_outputs", queue)
    assert asyncio.run(engine._stage_rewrite(project["id"])) is True
    first, second = store.load_project(project["id"])["storyboard"]["shots"]
    assert first["h3_prompt_source"] == "fallback" and "out of memory" in first["h3_prompt_note"]
    assert second["h3_prompt_source"] == "rewriter" and second["h3_prompt"] == REWRITTEN


def test_rewrite_success_injects_shot_and_uploads_keyframe(tmp_path, monkeypatch):
    store, engine, project = _project_with_keyframes(
        tmp_path,
        {"rewrite_enabled": True, "demo_mode": False, "rewrite_variant": "8b", "rewrite_model": "q8_0", "rewrite_adapter": "gguf"},
    )
    models = [
        "Qwen3-VL-8B-Instruct GGUF Q4_K_M · 5.4 GB download",
        "Qwen3-VL-8B-Instruct GGUF Q8_0 · 8.8 GB download",
    ]
    adapters = ["lightx2v/MiniMax-H3-Prompt-Rewriter-LoRA-8B", "pytraveler/MiniMax-H3-Prompt-Rewriter-LoRA-8B-GGUF"]
    info = _object_info_for("8b", models, adapters)
    uploads, graphs = [], []

    async def object_info(self):
        return info

    async def upload(self, path):
        uploads.append(Path(path).name)
        return f"up_{Path(path).name}"

    async def queue(self, prompt, log, timeout=3600, should_stop=None):
        graphs.append(prompt)
        return {"5": {"text": [REWRITTEN]}}

    monkeypatch.setattr(ComfyClient, "object_info", object_info)
    monkeypatch.setattr(ComfyClient, "upload_image", upload)
    monkeypatch.setattr(ComfyClient, "queue_and_wait_outputs", queue)
    assert asyncio.run(engine._stage_rewrite(project["id"])) is True

    current = store.load_project(project["id"])
    shots = current["storyboard"]["shots"]
    assert uploads == [f"{shot['shot_id']}_key.png" for shot in shots]
    for shot, graph in zip(shots, graphs):
        _, writer = _node(graph, "MiniMaxH3PromptWriter8B")
        load_id, load = _node(graph, "LoadImage")
        _, options = _node(graph, "MiniMaxH3RewriterOptions")
        assert writer["inputs"]["task"] == "I2VA"
        assert writer["inputs"]["first_frame"] == [load_id, 0]
        assert load["inputs"]["image"] == f"up_{shot['shot_id']}_key.png"
        assert writer["inputs"]["duration"] == float(shot["duration_sec"])
        assert writer["inputs"]["resolution"] == "9:16"
        assert writer["inputs"]["model"] == models[1]
        assert options["inputs"]["adapter"] == adapters[1]
        assert writer["inputs"]["prompt"].startswith(shot["h3_prompt_original"])
        assert shot["h3_prompt"] == REWRITTEN and shot["h3_prompt_source"] == "rewriter"
        assert engine._video_prompt(current, shot) == REWRITTEN


def test_rewrite_skipped_for_wan_and_when_disabled(tmp_path):
    store, engine, project = _project_with_keyframes(tmp_path / "a", {"rewrite_enabled": True, "demo_mode": True}, "wan22")
    assert asyncio.run(engine._stage_rewrite(project["id"])) is False
    current = store.load_project(project["id"])
    assert "非 H3" in current["stages"]["stage3r"]["message"]
    assert not any(shot.get("h3_prompt") for shot in current["storyboard"]["shots"])

    store, engine, project = _project_with_keyframes(tmp_path / "b", {"rewrite_enabled": False, "demo_mode": True})
    assert asyncio.run(engine._stage_rewrite(project["id"])) is False
    assert store.load_project(project["id"])["stages"]["stage3r"]["message"] == "未启用"


def test_demo_pipeline_with_prompt_review(tmp_path, monkeypatch):
    store = Store(tmp_path, ROOT)
    settings = store.load_settings()
    settings.update({
        "demo_mode": True,
        "mock_llm": "always",
        "output_dir": str(tmp_path / "out"),
        "rewrite_enabled": True,
        "rewrite_review": True,
    })
    store.save_settings(settings)
    engine = Engine(store)
    project = store.new_project(
        name="客栈夜雨",
        script=CHAPTER,
        style="anime",
        aspect="16:9",
        video_workflow="h3_i2v",
        image_workflow="01_character_t2i.json",
        edit_workflow="02_shot_edit.json",
    )
    used: dict[str, str] = {}
    original_render = Engine._render_video

    async def spy(self, project_id, workflow_name, prompt, images, dest_rel, **kwargs):
        used[Path(dest_rel).stem] = prompt
        return await original_render(self, project_id, workflow_name, prompt, images, dest_rel, **kwargs)

    monkeypatch.setattr(Engine, "_render_video", spy)

    async def wait_for(statuses, rounds=200):
        current = {}
        for _ in range(rounds):
            current = store.load_project(project["id"])
            if current["status"] in statuses:
                return current
            await asyncio.sleep(0.1)
        return current

    async def run():
        await engine.start(project["id"], from_stage="stage0")
        current = await wait_for({"awaiting_approval", "failed"})
        assert current["status"] == "awaiting_approval", current.get("error")
        await engine.continue_run(project["id"], current["storyboard"])
        current = await wait_for({"awaiting_prompt_review", "failed", "done"})
        assert current["status"] == "awaiting_prompt_review", current.get("error")
        assert current["stages"]["stage3r"]["status"] == "done"
        assert current["stages"]["stage4"]["status"] == "pending"
        assert not used
        shots = current["storyboard"]["shots"]
        for shot in shots:
            assert shot["h3_prompt_source"] == "demo"
            assert "integrated_multimodal_description:" in shot["h3_prompt"]
            assert shot["h3_prompt_original"] in shot["h3_prompt"]
        first, second = shots[0]["shot_id"], shots[1]["shot_id"]
        edited = "integrated_multimodal_description: hand edited.\noverall_soundscape: rain.\nnon_diegetic_music: none."
        await engine.continue_run(project["id"], prompts={first: edited, second: ""})
        current = await wait_for({"done", "failed", "cancelled"}, rounds=400)
        assert current["status"] == "done", current.get("error")
        by_id = {shot["shot_id"]: shot for shot in current["storyboard"]["shots"]}
        assert by_id[first]["h3_prompt_source"] == "manual"
        assert by_id[second]["h3_prompt_source"] == "fallback"
        assert used[first] == edited
        assert used[second] == by_id[second]["h3_prompt_original"]

    asyncio.run(run())


def test_settings_roundtrip_and_api(tmp_path):
    app = build_app(tmp_path)
    with TestClient(app) as client:
        defaults = client.get("/api/settings").json()
        assert defaults["rewrite_enabled"] is False
        assert defaults["rewrite_variant"] == "8b"
        assert defaults["rewrite_review"] is True
        saved = client.put("/api/settings", json={
            "rewrite_enabled": True,
            "rewrite_variant": "27b",
            "rewrite_model": "  Qwen3.6-27B-UD-IQ2_M.gguf ",
            "rewrite_adapter": "lightx2v/MiniMax-H3-Prompt-Rewriter-LoRA",
            "rewrite_review": False,
        }).json()
        assert saved["rewrite_enabled"] is True and saved["rewrite_review"] is False
        assert saved["rewrite_variant"] == "27b"
        assert saved["rewrite_model"] == "Qwen3.6-27B-UD-IQ2_M.gguf"
        assert client.get("/api/settings").json()["rewrite_variant"] == "27b"
        bad = client.put("/api/settings", json={"rewrite_variant": "70b"}).json()
        assert bad["rewrite_variant"] == "8b"
        rewrite = client.get("/api/workflows").json()["rewrite"]
        assert [item["id"] for item in rewrite] == ["8b", "27b", "writer"]
        assert "04a_h3_rewrite_8b.json" in client.get("/api/workflows").json()["files"]
        check = client.post("/api/settings/check").json()
        assert check["rewriter"]["ok"] is False
