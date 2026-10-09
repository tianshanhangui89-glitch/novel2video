import asyncio
import json
import subprocess
from pathlib import Path

from fastapi.testclient import TestClient

from app.main import build_app
from app.ollama import choose_model
from app.pipeline import Engine
from app.store import ROOT, Store
from app.storyboard import mock_storyboard, parse_json_content
from app.stitch import stitch

CHAPTER = (ROOT / "input" / "chapter01.md").read_text(encoding="utf-8")


def test_parse_fenced_json_and_mock_example():
    raw = """```json
    {"characters":[{"name":"甲"}],"scenes":[{"name":"厅"}],"props":[],"shots":[{"shot_id":"1","action":"走进来","image_prompt":"walk in","motion_prompt":"move"}]}
    ```"""
    data = parse_json_content(raw)
    assert data["characters"][0]["name"] == "甲"
    board = mock_storyboard(CHAPTER, project_id="t", title="客栈夜雨", style="anime", aspect="16:9")
    assert any(item["name"] == "林远" for item in board["characters"])
    assert board["props"] and board["props"][0]["name"] == "油灯"
    assert len(board["shots"]) == 2


def test_choose_model_prefers_qwen_text():
    assert choose_model(["llama3", "qwen2.5:14b", "nomic-embed"], "") == "qwen2.5:14b"
    assert choose_model(["qwen2.5vl", "qwen3:8b"], "") == "qwen3:8b"
    assert choose_model(["a", "b"], "b") == "b"


def test_stitch_two_clips(tmp_path: Path):
    clips = []
    for name, color in (("a.mp4", "red"), ("b.mp4", "blue")):
        path = tmp_path / name
        subprocess.check_call(
            [
                "ffmpeg", "-y", "-f", "lavfi", "-i", f"color=c={color}:s=320x180:d=1",
                "-pix_fmt", "yuv420p", str(path),
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        clips.append(path)
    dest = tmp_path / "chapter.mp4"
    stitch(clips, dest, 320, 180, tmp_path / "work")
    assert dest.is_file() and dest.stat().st_size > 1000


def test_http_shell(tmp_path: Path):
    app = build_app(tmp_path)
    with TestClient(app) as client:
        page = client.get("/")
        assert page.status_code == 200
        assert "小说成片" in page.text
        assert "开始生成" in client.get("/static/app.js").text
        sample = client.get("/api/sample-chapter")
        assert sample.status_code == 200
        assert "林远" in sample.json()["script"]
        projects = client.get("/api/projects").json()
        assert any(item["id"] == "demo_ch01" for item in projects)
        files = client.get("/api/workflows").json()["files"]
        assert "03c_wan22_i2v.json" in files
        health = client.post("/api/settings/check").json()
        assert health["ffmpeg"]["ok"] is True


def test_demo_pipeline_end_to_end(tmp_path: Path):
    store = Store(tmp_path, ROOT)
    settings = store.load_settings()
    settings.update({"demo_mode": True, "mock_llm": "always", "output_dir": str(tmp_path / "out")})
    store.save_settings(settings)
    engine = Engine(store)
    project = store.new_project(
        name="客栈夜雨",
        script=CHAPTER,
        style="anime cel-shading, cinematic lighting",
        aspect="16:9",
        video_workflow="h3_i2v",
        image_workflow="01_character_t2i.json",
        edit_workflow="02_shot_edit.json",
    )

    async def run():
        await engine.start(project["id"], from_stage="stage0")
        current = {}
        for _ in range(80):
            current = store.load_project(project["id"])
            if current["status"] in {"awaiting_approval", "failed", "done"}:
                break
            await asyncio.sleep(0.05)
        assert current["status"] == "awaiting_approval", current.get("error")
        board = current["storyboard"]
        board["characters"][0]["name"] = "林远改"
        await engine.continue_run(project["id"], board)
        for _ in range(180):
            current = store.load_project(project["id"])
            if current["status"] in {"done", "failed", "cancelled"}:
                break
            await asyncio.sleep(0.5)
        assert current["status"] == "done", current.get("error")
        assert current["storyboard"]["characters"][0]["name"] == "林远改"
        final = store.media_dir(project["id"]) / current["final_video"]
        assert final.is_file() and final.stat().st_size > 1000
        archived = tmp_path / "out" / project["id"] / "chapter.mp4"
        assert archived.is_file()
        manifest = json.loads((tmp_path / "out" / project["id"] / "project.json").read_text(encoding="utf-8"))
        assert manifest["storyboard"]["shots"]

    asyncio.run(run())
