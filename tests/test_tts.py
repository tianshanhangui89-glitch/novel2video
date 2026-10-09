import asyncio
import wave
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.demo_media import write_card, write_clip, write_speech
from app.main import build_app
from app.pipeline import Engine
from app.stitch import normalize_clip, probe_duration, stitch
from app.store import ROOT, Store
from app.storyboard import mock_storyboard
from app.tts import (
    TTSError,
    estimate_duration,
    get_provider,
    list_providers,
    speakable_text,
    write_placeholder_wav,
)

CHAPTER = (ROOT / "input" / "chapter01.md").read_text(encoding="utf-8")


def test_speakable_text_and_providers():
    assert speakable_text({"dialogue": "总算到了。", "action": "推门"}) == "总算到了。"
    assert speakable_text({"dialogue": "", "action": "推门而入，环顾"}, "dialogue_or_action").startswith("推门")
    assert speakable_text({"dialogue": "", "action": "推门"}, "dialogue") == ""
    ids = [item["id"] for item in list_providers()]
    assert ids == ["confucius4", "cosyvoice", "local"]
    conf = next(item for item in list_providers() if item["id"] == "confucius4")
    assert conf["cloning"] is True
    assert "授权" in conf["note"] or "参考" in conf["note"]


def test_placeholder_and_local_provider(tmp_path: Path):
    dest = tmp_path / "a.wav"
    seconds = write_placeholder_wav(dest, 1.2, seed=3)
    assert abs(seconds - 1.2) < 0.01
    with wave.open(str(dest), "rb") as handle:
        assert handle.getnframes() > 1000
    assert estimate_duration("总算到了。", 1.0) >= 0.6

    async def run():
        provider = get_provider("local")
        health = await provider.health({})
        assert health["ok"] is True
        dur = await provider.synthesize("客栈夜雨到了", dest, voice="zh", speed=1.0, settings={})
        assert dur >= 0.5
        assert (await provider.release({}))

        conf = get_provider("confucius4")
        # Without a live server, health is false; synthesize errors clearly.
        health = await conf.health({"tts_url": "http://127.0.0.1:9"})
        assert health["ok"] is False

    asyncio.run(run())


def test_stitch_mixes_voiceover(tmp_path: Path):
    still = tmp_path / "frame.png"
    clip = tmp_path / "shot.mp4"
    voice = tmp_path / "line.wav"
    write_card(still, "001", "test", "16:9", "关键帧")
    write_clip(still, clip, 1.0)
    write_placeholder_wav(voice, 0.9)
    dest = tmp_path / "chapter.mp4"
    stitch([clip], dest, 320, 180, tmp_path / "work", voiceovers=[voice], volume=0.8)
    assert dest.is_file() and dest.stat().st_size > 1000
    # Mixed clip should carry an audio stream longer than silence-only would imply.
    assert probe_duration(dest) >= 0.8


def test_align_pads_short_video(tmp_path: Path):
    from app.stitch import pad_or_trim_video

    still = tmp_path / "frame.png"
    clip = tmp_path / "shot.mp4"
    write_card(still, "001", "test", "16:9", "关键帧")
    write_clip(still, clip, 1.0)
    out = tmp_path / "padded.mp4"
    dur = pad_or_trim_video(clip, out, 2.0)
    assert dur >= 1.9


def test_settings_and_workflows_expose_tts(tmp_path: Path):
    app = build_app(tmp_path)
    with TestClient(app) as client:
        defaults = client.get("/api/settings").json()
        assert defaults["tts_enabled"] is False
        assert defaults["serial_mode"] is True
        assert defaults["tts_provider"] == "confucius4"
        saved = client.put(
            "/api/settings",
            json={
                "tts_enabled": True,
                "tts_provider": "local",
                "tts_voice": "zh",
                "tts_speed": 1.2,
                "tts_volume": 0.7,
                "tts_text_mode": "dialogue",
                "serial_mode": True,
            },
        ).json()
        assert saved["tts_enabled"] is True and saved["tts_provider"] == "local"
        assert saved["tts_speed"] == 1.2 and saved["serial_mode"] is True
        bad = client.put("/api/settings", json={"tts_provider": "nope", "tts_speed": 9}).json()
        assert bad["tts_provider"] == "confucius4"
        assert bad["tts_speed"] == 2.0
        client.put("/api/settings", json={"tts_enabled": True, "tts_provider": "local"}).json()
        tts = client.get("/api/workflows").json()["tts"]
        assert [item["id"] for item in tts] == ["confucius4", "cosyvoice", "local"]
        res = client.get("/api/resources").json()
        assert "holder" in res and res["serial_mode"] is True
        check = client.post("/api/settings/check").json()
        assert check["tts"]["ok"] is True
        assert check["serial"]["ok"] is True


def test_demo_pipeline_with_tts(tmp_path: Path):
    from app.resources import GATE

    GATE.reset()
    store = Store(tmp_path, ROOT)
    settings = store.load_settings()
    settings.update({
        "demo_mode": True,
        "mock_llm": "always",
        "output_dir": str(tmp_path / "out"),
        "tts_enabled": True,
        "tts_provider": "local",
        "tts_voice": "zh",
        "serial_mode": True,
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

    async def wait_for(statuses, rounds=400):
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
        current = await wait_for({"done", "failed", "cancelled"}, rounds=500)
        assert current["status"] == "done", current.get("error")
        assert current["stages"]["stage4t"]["status"] == "done"
        shots = current["storyboard"]["shots"]
        voiced = [shot for shot in shots if shot.get("tts_audio")]
        assert voiced, "expected at least one voiced shot (shot 001 has dialogue)"
        for shot in voiced:
            path = store.media_dir(project["id"]) / shot["tts_audio"]
            assert path.is_file()
            assert shot["tts_source"] == "demo"
            assert shot["tts_text"]
        final = store.media_dir(project["id"]) / current["final_video"]
        assert final.is_file() and probe_duration(final) > 1.0

    asyncio.run(run())
