import asyncio
import time

from app.resources import FAMILY_LABELS, GATE, ResourceGate, free_comfy, unload_family, unload_ollama


def test_family_labels_cover_pipeline():
    assert set(FAMILY_LABELS) == {"llm", "image", "rewrite", "video", "tts"}


def test_serial_lock_is_exclusive_and_queues():
    gate = ResourceGate()
    order: list[str] = []
    snapshots: list[dict] = []

    def on_snap(snap):
        snapshots.append(snap)

    gate.subscribe(on_snap)

    async def worker(name: str, family: str, hold: float):
        async with gate.hold(
            family=family,
            project_id=name,
            stage="stageX",
            label=name,
            settings={"serial_mode": True, "demo_mode": True},
        ):
            order.append(f"start:{name}")
            await asyncio.sleep(hold)
            order.append(f"end:{name}")

    async def run():
        first = asyncio.create_task(worker("a", "llm", 0.25))
        await asyncio.sleep(0.05)
        second = asyncio.create_task(worker("b", "image", 0.05))
        # While a holds, b must be queued.
        await asyncio.sleep(0.05)
        snap = gate.snapshot()
        assert snap["holder"]["project_id"] == "a"
        assert any(item["project_id"] == "b" for item in snap["queue"])
        await asyncio.gather(first, second)

    asyncio.run(run())
    assert order == ["start:a", "end:a", "start:b", "end:b"]
    assert any(snap.get("queue") for snap in snapshots)


def test_family_switch_triggers_unload(monkeypatch):
    gate = ResourceGate()
    calls: list[str] = []

    async def fake_unload(family, settings):
        calls.append(family)
        return [f"unloaded {family}"]

    monkeypatch.setattr("app.resources.unload_family", fake_unload)

    async def run():
        async with gate.hold(
            family="llm",
            project_id="p1",
            stage="stage1",
            label="llm",
            settings={"serial_mode": True, "demo_mode": False},
        ):
            pass
        async with gate.hold(
            family="image",
            project_id="p1",
            stage="stage2",
            label="image",
            settings={"serial_mode": True, "demo_mode": False},
        ):
            assert gate.last_unload_notes == ["unloaded llm"]
        # Same family again: no unload.
        async with gate.hold(
            family="image",
            project_id="p1",
            stage="stage3",
            label="image2",
            settings={"serial_mode": True, "demo_mode": False},
        ):
            assert gate.last_unload_notes == []

    asyncio.run(run())
    assert calls == ["llm"]


def test_serial_mode_off_skips_lock():
    gate = ResourceGate()
    concurrent = 0
    max_seen = 0

    async def worker():
        nonlocal concurrent, max_seen
        async with gate.hold(
            family="video",
            project_id="x",
            stage="stage4",
            label="v",
            settings={"serial_mode": False},
        ) as ticket:
            assert ticket is None
            concurrent += 1
            max_seen = max(max_seen, concurrent)
            await asyncio.sleep(0.05)
            concurrent -= 1

    async def run():
        await asyncio.gather(worker(), worker(), worker())

    asyncio.run(run())
    assert max_seen == 3


def test_unload_helpers_demo_and_http(monkeypatch):
    async def run():
        notes = await unload_family("llm", {"demo_mode": True})
        assert any("演示" in note for note in notes)

        class FakeResponse:
            status_code = 200

            def json(self):
                return {"models": [{"name": "qwen3:8b"}]}

            def raise_for_status(self):
                return None

        class FakeClient:
            def __init__(self, *a, **k):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def get(self, url):
                return FakeResponse()

            async def post(self, url, json=None):
                assert "keep_alive" in (json or {}) and json["keep_alive"] == 0
                return FakeResponse()

        monkeypatch.setattr("app.resources.httpx.AsyncClient", FakeClient)
        notes = await unload_ollama("http://127.0.0.1:11434")
        assert any("qwen3:8b" in note for note in notes)

        class FreeClient(FakeClient):
            async def post(self, url, json=None):
                assert url.endswith("/free")
                assert json == {"unload_models": True, "free_memory": True}
                return FakeResponse()

        monkeypatch.setattr("app.resources.httpx.AsyncClient", FreeClient)
        notes = await free_comfy("http://127.0.0.1:8188")
        assert any("ComfyUI" in note for note in notes)

    asyncio.run(run())


def test_engine_publishes_resources(tmp_path, monkeypatch):
    from app.pipeline import Engine
    from app.resources import GATE as live
    from app.store import ROOT, Store

    live.reset()
    store = Store(tmp_path, ROOT)
    settings = store.load_settings()
    settings.update({"demo_mode": True, "mock_llm": "always", "serial_mode": True})
    store.save_settings(settings)
    engine = Engine(store)
    seen: list[dict] = []

    def capture(snap):
        seen.append(snap)

    live.subscribe(capture)
    project = store.new_project(
        name="t",
        script="雨。",
        style="anime",
        aspect="16:9",
        video_workflow="h3_i2v",
        image_workflow="01_character_t2i.json",
        edit_workflow="02_shot_edit.json",
    )

    async def run():
        # Hold the gate as another "project" so the real run queues.
        async def blocker():
            async with live.hold(
                family="image",
                project_id="blocker",
                stage="stage2",
                label="block",
                settings={"serial_mode": True, "demo_mode": True},
            ):
                await asyncio.sleep(0.35)

        block = asyncio.create_task(blocker())
        await asyncio.sleep(0.05)
        await engine.start(project["id"], from_stage="stage0")
        # Wait until the project either queues for image or finishes approval wait.
        for _ in range(80):
            current = store.load_project(project["id"])
            if current.get("status") == "awaiting_approval":
                await engine.continue_run(project["id"], current["storyboard"])
            gpu = current.get("gpu") or {}
            if gpu.get("state") == "queued":
                break
            if current["status"] in {"done", "failed"}:
                break
            await asyncio.sleep(0.05)
        await block
        for _ in range(200):
            current = store.load_project(project["id"])
            if current["status"] in {"done", "failed", "cancelled", "awaiting_approval"}:
                if current["status"] == "awaiting_approval":
                    await engine.continue_run(project["id"], current["storyboard"])
                elif current["status"] in {"done", "failed", "cancelled"}:
                    break
            await asyncio.sleep(0.1)
        return store.load_project(project["id"])

    final = asyncio.run(run())
    assert final["status"] == "done", final.get("error")
    assert any(snap.get("holder") or snap.get("queue") for snap in seen)
    live.unsubscribe(capture)
