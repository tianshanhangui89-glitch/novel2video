"""Novel2Video local web app."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app.checks import check_all
from app.pipeline import Engine
from app.rewrite import VARIANTS as REWRITE_VARIANTS
from app.tts import list_providers as list_tts_providers
from app.resources import GATE, FAMILY_LABELS
from app.store import ROOT, STAGE_IDS, VIDEO_WORKFLOWS, Store
from app.storyboard import StoryboardError, load_example

STATIC = ROOT / "static"
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


class SettingsIn(BaseModel):
    ollama_url: str | None = None
    comfy_url: str | None = None
    llm_model: str | None = None
    image_workflow: str | None = None
    edit_workflow: str | None = None
    video_workflow: str | None = None
    output_dir: str | None = None
    style: str | None = None
    aspect: str | None = None
    demo_mode: bool | None = None
    mock_llm: str | None = None
    rewrite_enabled: bool | None = None
    rewrite_variant: str | None = None
    rewrite_model: str | None = None
    rewrite_adapter: str | None = None
    rewrite_review: bool | None = None
    tts_enabled: bool | None = None
    tts_provider: str | None = None
    tts_url: str | None = None
    tts_voice: str | None = None
    tts_lang: str | None = None
    tts_speed: float | None = None
    tts_volume: float | None = None
    tts_text_mode: str | None = None
    tts_fallback_local: bool | None = None
    tts_subprocess: bool | None = None
    tts_confucius_dir: str | None = None
    tts_confucius_python: str | None = None
    serial_mode: bool | None = None


class ProjectIn(BaseModel):
    name: str = ""
    script: str = ""
    style: str = ""
    aspect: str = "16:9"
    video_workflow: str = ""
    image_workflow: str = ""
    edit_workflow: str = ""


class RunIn(BaseModel):
    from_stage: str = "stage0"


class ContinueIn(BaseModel):
    storyboard: dict | None = None
    prompts: dict[str, str] | None = None


def build_app(data_dir: Path | None = None) -> FastAPI:
    raw = data_dir or Path(os.environ.get("N2V_DATA", ROOT / "data"))
    store = Store(raw, ROOT)
    engine = Engine(store)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.engine.store.load_settings()
        app.state.engine.store.seed_example()
        yield

    app = FastAPI(title="Novel2Video", lifespan=lifespan)
    app.state.engine = engine

    @app.get("/")
    async def index():
        return FileResponse(STATIC / "index.html")

    @app.get("/api/health")
    async def health():
        return {"ok": True, "name": "Novel2Video"}

    @app.get("/api/resources")
    async def resources():
        from app import confucius

        settings = store.load_settings()
        snap = engine.resources()
        snap["serial_mode"] = settings.get("serial_mode", True)
        if settings.get("tts_provider", "confucius4") in {"confucius4", "clone"} or settings.get("tts_enabled"):
            status = await confucius.health(settings)
            GATE.confucius_status = status
            snap["confucius"] = status
        return snap

    @app.get("/api/settings")
    async def get_settings():
        return store.load_settings()

    @app.put("/api/settings")
    async def put_settings(body: SettingsIn):
        return store.save_settings(body.model_dump(exclude_none=True))

    @app.post("/api/settings/check")
    async def post_check():
        return await check_all(store.load_settings())

    @app.get("/api/workflows")
    async def workflows():
        return {
            "files": store.list_workflow_files(),
            "video": list(VIDEO_WORKFLOWS.values()),
            "rewrite": list(REWRITE_VARIANTS.values()),
            "tts": list_tts_providers(),
            "gpu_families": [
                {"id": key, "label": label} for key, label in FAMILY_LABELS.items()
            ],
        }

    @app.get("/api/sample-chapter")
    async def sample_chapter():
        example = load_example()
        script = (ROOT / "input" / "chapter01.md").read_text(encoding="utf-8")
        return {
            "name": example.get("title") or "客栈夜雨",
            "style": example.get("style") or "",
            "aspect": example.get("aspect") or "16:9",
            "script": script,
        }

    @app.get("/api/projects")
    async def list_projects():
        return store.list_projects()

    @app.post("/api/projects")
    async def create_project(body: ProjectIn):
        if not body.script.strip():
            raise HTTPException(400, "请先粘贴或上传章节")
        settings = store.load_settings()
        project = store.new_project(
            name=body.name,
            script=body.script,
            style=body.style or settings["style"],
            aspect=body.aspect or settings["aspect"],
            video_workflow=body.video_workflow or settings["video_workflow"],
            image_workflow=body.image_workflow or settings["image_workflow"],
            edit_workflow=body.edit_workflow or settings["edit_workflow"],
        )
        return project

    @app.get("/api/projects/{project_id}")
    async def get_project(project_id: str):
        try:
            return store.load_project(project_id)
        except FileNotFoundError:
            raise HTTPException(404, "没有找到这个项目") from None

    @app.put("/api/projects/{project_id}/storyboard")
    async def put_storyboard(project_id: str, body: dict):
        try:
            project = store.load_project(project_id)
        except FileNotFoundError:
            raise HTTPException(404, "没有找到这个项目") from None
        try:
            from app.storyboard import normalize_storyboard

            project["storyboard"] = normalize_storyboard(
                body,
                project_id=project["id"],
                title=project["name"],
                style=body.get("style") or project["style"],
                aspect=body.get("aspect") or project["aspect"],
            )
        except StoryboardError as exc:
            raise HTTPException(400, str(exc)) from exc
        project["style"] = project["storyboard"]["style"]
        project["aspect"] = project["storyboard"]["aspect"]
        return store.save_project(project)

    @app.post("/api/projects/{project_id}/voices")
    async def upload_voice(
        project_id: str,
        character_id: str = Form(...),
        authorized: str = Form("false"),
        file: UploadFile = File(...),
    ):
        """Upload a reference WAV for zero-shot cloning. Requires authorization attestation."""
        try:
            project = store.load_project(project_id)
        except FileNotFoundError:
            raise HTTPException(404, "没有找到这个项目") from None
        if not _as_truthy(authorized):
            raise HTTPException(
                400,
                "必须勾选「我确认拥有该声音的使用授权」。只接受你本人的声音，或你已获得明确授权的声音；不得上传名人或未授权他人音色。",
            )
        if not project.get("storyboard"):
            raise HTTPException(400, "请先生成分镜")
        name = (file.filename or "voice.wav").lower()
        if not name.endswith((".wav", ".wave")):
            raise HTTPException(400, "请上传 wav 格式的参考音频")
        data = await file.read()
        if len(data) < 1000:
            raise HTTPException(400, "参考音频太短或文件损坏")
        if len(data) > 20 * 1024 * 1024:
            raise HTTPException(400, "参考音频请小于 20 MB")
        safe_id = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in character_id)[:64] or "voice"
        rel = f"voices/{safe_id}.wav"
        dest = store.media_dir(project_id) / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        found = False
        for character in project["storyboard"].get("characters") or []:
            if character.get("id") == character_id:
                character["voice_ref"] = rel
                character["voice_ref_authorized"] = True
                found = True
                break
        if not found:
            raise HTTPException(400, f"分镜里没有角色 {character_id}")
        store.save_project(project)
        return {"ok": True, "character_id": character_id, "voice_ref": rel, "project": project}

    @app.delete("/api/projects/{project_id}/voices/{character_id}")
    async def clear_voice(project_id: str, character_id: str):
        try:
            project = store.load_project(project_id)
        except FileNotFoundError:
            raise HTTPException(404, "没有找到这个项目") from None
        board = project.get("storyboard") or {}
        for character in board.get("characters") or []:
            if character.get("id") != character_id:
                continue
            rel = character.get("voice_ref") or ""
            if rel:
                path = store.media_dir(project_id) / rel
                if path.is_file():
                    path.unlink()
            character["voice_ref"] = ""
            character["voice_ref_authorized"] = False
            store.save_project(project)
            return {"ok": True, "project": project}
        raise HTTPException(404, "没有找到这个角色")

    @app.post("/api/projects/{project_id}/run")
    async def run_project(project_id: str, body: RunIn | None = None):
        try:
            store.load_project(project_id)
        except FileNotFoundError:
            raise HTTPException(404, "没有找到这个项目") from None
        stage = (body or RunIn()).from_stage
        if stage not in STAGE_IDS:
            raise HTTPException(400, "未知阶段")
        return await engine.start(project_id, from_stage=stage)

    @app.post("/api/projects/{project_id}/continue")
    async def continue_project(project_id: str, body: ContinueIn | None = None):
        try:
            store.load_project(project_id)
        except FileNotFoundError:
            raise HTTPException(404, "没有找到这个项目") from None
        try:
            body = body or ContinueIn()
            return await engine.continue_run(project_id, body.storyboard, body.prompts)
        except StoryboardError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.post("/api/projects/{project_id}/cancel")
    async def cancel_project(project_id: str):
        try:
            store.load_project(project_id)
        except FileNotFoundError:
            raise HTTPException(404, "没有找到这个项目") from None
        return await engine.cancel(project_id)

    @app.post("/api/projects/{project_id}/retry")
    async def retry_project(project_id: str):
        try:
            project = store.load_project(project_id)
        except FileNotFoundError:
            raise HTTPException(404, "没有找到这个项目") from None
        failed = next((stage for stage in STAGE_IDS if project["stages"].get(stage, {}).get("status") == "failed"), "stage2")
        return await engine.start(project_id, from_stage=failed)

    @app.get("/api/projects/{project_id}/events")
    async def events(project_id: str, request: Request):
        try:
            store.load_project(project_id)
        except FileNotFoundError:
            raise HTTPException(404, "没有找到这个项目") from None
        queue = engine.subscribe(project_id)

        async def gen():
            try:
                current = store.load_project(project_id)
                yield _sse({"type": "snapshot", "project": current, "resources": engine.resources()})
                while True:
                    if await request.is_disconnected():
                        break
                    try:
                        item = await asyncio.wait_for(queue.get(), timeout=12)
                    except asyncio.TimeoutError:
                        yield ": ping\n\n"
                        continue
                    yield _sse(item)
            finally:
                engine.unsubscribe(project_id, queue)

        return StreamingResponse(
            gen(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.get("/media/{project_id}/{path:path}")
    async def media(project_id: str, path: str):
        root = store.media_dir(project_id).resolve()
        target = (root / path).resolve()
        if root not in target.parents and target != root:
            raise HTTPException(404, "文件不存在")
        if not target.is_file():
            raise HTTPException(404, "文件不存在")
        return FileResponse(target)

    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    return app


def _sse(payload: dict) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


def _as_truthy(value: str | bool | None) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


app = build_app()
