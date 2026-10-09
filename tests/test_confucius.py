import asyncio
import io
import wave
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app import confucius
from app.confucius import chunk_text, concat_wavs
from app.demo_media import write_card, write_clip
from app.main import build_app
from app.pipeline import Engine
from app.store import ROOT, Store
from app.storyboard import mock_storyboard
from app.tts import Confucius4Provider, TTSError, get_provider, write_placeholder_wav


def _wav_bytes(seconds: float = 0.4) -> bytes:
    buf = io.BytesIO()
    n = int(22050 * seconds)
    with wave.open(buf, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(22050)
        handle.writeframes(b"\x00\x00" * n)
    return buf.getvalue()


def test_chunk_text_splits_over_1024():
    short = "你好。" * 10
    assert chunk_text(short) == [short]
    long = ("这是一句用来测试自动切段的中文内容，") * 80
    assert len(long) > 1024
    parts = chunk_text(long, 1024)
    assert len(parts) >= 2
    assert all(len(part) <= 1024 for part in parts)


def test_concat_wavs(tmp_path: Path):
    a = tmp_path / "a.wav"
    b = tmp_path / "b.wav"
    write_placeholder_wav(a, 0.5, seed=1)
    write_placeholder_wav(b, 0.5, seed=2)
    dest = tmp_path / "out.wav"
    dur = concat_wavs([a, b], dest)
    assert dest.is_file() and dur >= 0.9


def test_confucius_http_synth_and_health(monkeypatch, tmp_path: Path):
    calls = []

    class FakeResponse:
        def __init__(self, status=200, content=b"", json_data=None, text=""):
            self.status_code = status
            self.content = content or _wav_bytes(0.3)
            self._json = json_data
            self.text = text

        def raise_for_status(self):
            if self.status_code >= 400:
                raise httpx.HTTPStatusError("err", request=None, response=None)

        def json(self):
            return self._json

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url):
            calls.append(("GET", url))
            return FakeResponse(json_data={"ok": True, "sample_rate": 22050, "cuda": True})

        async def post(self, url, data=None, files=None):
            calls.append(("POST", url, data, bool(files)))
            assert data["text"]
            assert data["lang"] == "zh"
            assert len(data["text"]) <= 1024
            return FakeResponse(content=_wav_bytes(0.35))

    monkeypatch.setattr(confucius.httpx, "AsyncClient", FakeClient)

    async def ok_ensure(settings):
        return ["ok"]

    monkeypatch.setattr(confucius.MANAGER, "ensure", ok_ensure)

    async def run():
        settings = {"tts_url": "http://127.0.0.1:7860", "tts_subprocess": False}
        status = await confucius.health(settings)
        assert status["ok"] is True and status["sample_rate"] == 22050
        dest = tmp_path / "line.wav"
        text = ("客栈夜雨，门轴吱呀作响。") * 100
        assert len(text) > 1024
        provider = Confucius4Provider()
        dur = await provider.synthesize(text, dest, voice="speaker1", speed=1.0, settings=settings, lang="zh")
        assert dest.is_file() and dur > 0.2
        assert sum(1 for c in calls if c[0] == "POST") >= 2

    asyncio.run(run())


def test_confucius_unavailable_clear_error(monkeypatch):
    class BoomClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url):
            raise httpx.ConnectError("refused")

    monkeypatch.setattr(confucius.httpx, "AsyncClient", BoomClient)

    async def fail_ensure(settings):
        raise confucius.ConfuciusError("连不上 Confucius4。请先启动 server_local.py")

    monkeypatch.setattr(confucius.MANAGER, "ensure", fail_ensure)

    async def run():
        status = await confucius.health({"tts_url": "http://127.0.0.1:7860"})
        assert status["ok"] is False
        assert "连不上" in status["detail"]
        provider = get_provider("confucius4")
        with pytest.raises(TTSError) as exc:
            await provider.synthesize(
                "你好",
                Path("/tmp/x.wav"),
                voice="speaker1",
                speed=1.0,
                settings={"tts_url": "http://127.0.0.1:7860", "tts_subprocess": False},
            )
        assert "server_local" in str(exc.value) or "Confucius4" in str(exc.value)

    asyncio.run(run())


def test_voice_upload_requires_authorization(tmp_path: Path):
    app = build_app(tmp_path)
    with TestClient(app) as client:
        sample = client.get("/api/sample-chapter").json()
        project = client.post(
            "/api/projects",
            json={"name": "v", "script": sample["script"], "aspect": "16:9"},
        ).json()
        board = mock_storyboard(
            sample["script"], project_id=project["id"], title="t", style="anime", aspect="16:9"
        )
        client.put(f"/api/projects/{project['id']}/storyboard", json=board)
        cid = board["characters"][0]["id"]
        denied = client.post(
            f"/api/projects/{project['id']}/voices",
            data={"character_id": cid, "authorized": "false"},
            files={"file": ("me.wav", _wav_bytes(0.5), "audio/wav")},
        )
        assert denied.status_code == 400
        assert "授权" in denied.json()["detail"]
        ok = client.post(
            f"/api/projects/{project['id']}/voices",
            data={"character_id": cid, "authorized": "true"},
            files={"file": ("me.wav", _wav_bytes(0.5), "audio/wav")},
        )
        assert ok.status_code == 200
        body = ok.json()
        character = next(c for c in body["project"]["storyboard"]["characters"] if c["id"] == cid)
        assert character["voice_ref_authorized"] is True
        cleared = client.delete(f"/api/projects/{project['id']}/voices/{cid}")
        assert cleared.status_code == 200
        character = next(c for c in cleared.json()["project"]["storyboard"]["characters"] if c["id"] == cid)
        assert character["voice_ref"] == ""


def test_settings_default_confucius4(tmp_path: Path):
    app = build_app(tmp_path)
    with TestClient(app) as client:
        settings = client.get("/api/settings").json()
        assert settings["tts_provider"] == "confucius4"
        assert "7860" in settings["tts_url"]
        assert settings["tts_voice"] == "speaker1"
        assert settings["tts_fallback_local"] is True
        saved = client.put("/api/settings", json={"tts_provider": "clone"}).json()
        assert saved["tts_provider"] == "confucius4"
        res = client.get("/api/resources").json()
        assert "confucius" in res
        providers = client.get("/api/workflows").json()["tts"]
        assert providers[0]["id"] == "confucius4"
        assert "clone" not in [item["id"] for item in providers]


def test_pipeline_fallback_to_local(monkeypatch, tmp_path: Path):
    store = Store(tmp_path, ROOT)
    settings = store.load_settings()
    settings.update({
        "demo_mode": False,
        "tts_enabled": True,
        "tts_provider": "confucius4",
        "tts_fallback_local": True,
        "serial_mode": False,
        "mock_llm": "always",
    })
    store.save_settings(settings)
    project = store.new_project(
        name="t",
        script="雨。",
        style="anime",
        aspect="16:9",
        video_workflow="h3_i2v",
        image_workflow="01_character_t2i.json",
        edit_workflow="02_shot_edit.json",
    )
    board = mock_storyboard("雨夜。总算到了。", project_id=project["id"], title="t", style="anime", aspect="16:9")
    board["shots"][0]["dialogue"] = "总算到了。"
    for shot in board["shots"]:
        rel = f"shots/{shot['shot_id']}.mp4"
        still = store.media_dir(project["id"]) / f"shots/{shot['shot_id']}_key.png"
        write_card(still, shot["shot_id"], "k", "16:9", "关键帧")
        write_clip(still, store.media_dir(project["id"]) / rel, 1.0)
        shot["video"] = rel
        shot["keyframe"] = f"shots/{shot['shot_id']}_key.png"
    project["storyboard"] = board
    store.save_project(project)

    async def boom(self, *a, **k):
        raise TTSError("连不上 Confucius4")

    monkeypatch.setattr(Confucius4Provider, "synthesize", boom)
    engine = Engine(store)
    asyncio.run(engine._stage_tts(project["id"]))
    current = store.load_project(project["id"])
    voiced = [s for s in current["storyboard"]["shots"] if s.get("tts_source") == "local_fallback"]
    assert voiced
    assert (store.media_dir(project["id"]) / voiced[0]["tts_audio"]).is_file()
