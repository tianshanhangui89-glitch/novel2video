"""JSON project store and settings."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.rewrite import VARIANTS as REWRITE_VARIANTS
from app.storyboard import load_example, normalize_storyboard
from app.tts import PROVIDERS as TTS_PROVIDERS

ROOT = Path(__file__).resolve().parents[1]

STAGE_IDS = [
    "stage0",
    "stage1",
    "stage2",
    "stage3",
    "stage3r",
    "stage4",
    "stage4t",
    "stage5",
    "stage6",
]

STAGE_LABELS = {
    "stage0": "阶段 0 · 接收文稿",
    "stage1": "阶段 1 · 分镜结构化",
    "stage2": "阶段 2 · 参考图",
    "stage3": "阶段 3 · 镜头关键帧",
    "stage3r": "阶段 3.5 · H3 提示词改写",
    "stage4": "阶段 4 · 镜头视频",
    "stage4t": "阶段 4.5 · 配音",
    "stage5": "阶段 5 · 拼接成片",
    "stage6": "阶段 6 · 完成",
}

DEFAULT_STYLE = "anime cel-shading, consistent character design, cinematic lighting, soft rain atmosphere"

DEFAULT_SETTINGS: dict[str, Any] = {
    "ollama_url": "http://127.0.0.1:11434",
    "comfy_url": "http://127.0.0.1:8188",
    "llm_backend": "comfy",
    "llm_model": "qwen_3_4b_fp8_mixed.safetensors",
    "llm_workflow": "05_llm_textgen.json",
    "llm_clip_type": "ltxv",
    "llm_max_tokens": 4096,
    "llm_temperature": 0.4,
    "llm_fallback_ollama": True,
    "image_workflow": "01_character_t2i.json",
    "edit_workflow": "02_shot_edit.json",
    "video_workflow": "h3_i2v",
    "h3_resolution": "960x544",
    "h3_steps": 8,
    "h3_lightning": True,
    "output_dir": "output",
    "style": DEFAULT_STYLE,
    "aspect": "16:9",
    "demo_mode": False,
    "mock_llm": "auto",
    "rewrite_enabled": False,
    "rewrite_variant": "8b",
    "rewrite_model": "",
    "rewrite_adapter": "",
    "rewrite_review": True,
    "tts_enabled": False,
    "tts_provider": "confucius4",
    "tts_url": "http://127.0.0.1:7860",
    "tts_voice": "speaker1",
    "tts_lang": "zh",
    "tts_speed": 1.0,
    "tts_volume": 1.0,
    "tts_text_mode": "dialogue_or_action",
    "tts_fallback_local": True,
    "tts_subprocess": False,
    "tts_confucius_dir": "",
    "tts_confucius_python": "",
    "serial_mode": True,
}

BOOL_SETTINGS = (
    "demo_mode",
    "rewrite_enabled",
    "rewrite_review",
    "tts_enabled",
    "tts_fallback_local",
    "tts_subprocess",
    "serial_mode",
    "llm_fallback_ollama",
    "h3_lightning",
)

H3_RESOLUTION_CHOICES = ("864x480", "960x544", "1344x768")


def _as_bool(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _clamp_float(value: Any, default: float, low: float, high: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = default
    return max(low, min(high, number))

VIDEO_WORKFLOWS = {
    "h3_i2v": {
        "id": "h3_i2v",
        "file": "03_h3_i2v.json",
        "label": "MiniMax H3 图生视频",
        "note": "默认。可开 Lightning LoRA 8 步；默认画布 960×544（可在设置改）。",
    },
    "h3_r2v": {
        "id": "h3_r2v",
        "file": "03b_h3_r2v.json",
        "label": "MiniMax H3 参考生视频",
        "note": "用角色参考锁一致性；Lightning 为 4 步 LoRA。",
    },
    "wan22": {
        "id": "wan22",
        "file": "03c_wan22_i2v.json",
        "label": "Wan2.2 图生视频（低显存）",
        "note": "显存不足时用这条。对应工作流 03c，分辨率低于 H3。",
    },
}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def blank_stages() -> dict[str, dict]:
    return {key: {"status": "pending", "message": ""} for key in STAGE_IDS}


class Store:
    def __init__(self, data_dir: Path, assets: Path | None = None):
        self.data = Path(data_dir)
        self.assets = Path(assets) if assets else ROOT
        self.data.mkdir(parents=True, exist_ok=True)
        (self.data / "projects").mkdir(parents=True, exist_ok=True)

    def settings_path(self) -> Path:
        return self.data / "settings.json"

    def load_settings(self) -> dict:
        path = self.settings_path()
        current = dict(DEFAULT_SETTINGS)
        if path.is_file():
            try:
                saved = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                saved = {}
            if isinstance(saved, dict):
                current.update({k: saved[k] for k in DEFAULT_SETTINGS if k in saved})
        if current.get("video_workflow") not in VIDEO_WORKFLOWS:
            current["video_workflow"] = "h3_i2v"
        if current.get("aspect") not in {"16:9", "9:16", "1:1"}:
            current["aspect"] = "16:9"
        if current.get("mock_llm") not in {"auto", "always", "never"}:
            current["mock_llm"] = "auto"
        if current.get("rewrite_variant") not in REWRITE_VARIANTS:
            current["rewrite_variant"] = "8b"
        if current.get("tts_provider") in {"clone", ""}:
            current["tts_provider"] = "confucius4"
        if current.get("tts_provider") not in TTS_PROVIDERS:
            current["tts_provider"] = "confucius4"
        if current.get("tts_text_mode") not in {"dialogue", "dialogue_or_action"}:
            current["tts_text_mode"] = "dialogue_or_action"
        if current.get("llm_backend") not in {"comfy", "ollama"}:
            current["llm_backend"] = "comfy"
        if current.get("h3_resolution") not in H3_RESOLUTION_CHOICES:
            current["h3_resolution"] = "960x544"
        try:
            current["h3_steps"] = max(1, min(40, int(current.get("h3_steps") or 8)))
        except (TypeError, ValueError):
            current["h3_steps"] = 8
        try:
            current["llm_max_tokens"] = max(256, min(32768, int(current.get("llm_max_tokens") or 4096)))
        except (TypeError, ValueError):
            current["llm_max_tokens"] = 4096
        current["llm_temperature"] = _clamp_float(current.get("llm_temperature"), 0.4, 0.01, 2.0)
        current["llm_workflow"] = str(current.get("llm_workflow") or "05_llm_textgen.json").strip()
        current["llm_clip_type"] = str(current.get("llm_clip_type") or "ltxv").strip() or "ltxv"
        current["tts_lang"] = str(current.get("tts_lang") or "zh").strip() or "zh"
        current["tts_speed"] = _clamp_float(current.get("tts_speed"), 1.0, 0.5, 2.0)
        current["tts_volume"] = _clamp_float(current.get("tts_volume"), 1.0, 0.0, 2.0)
        for key in BOOL_SETTINGS:
            current[key] = _as_bool(current.get(key))
        return current

    def save_settings(self, payload: dict) -> dict:
        current = self.load_settings()
        for key in DEFAULT_SETTINGS:
            if key in payload and payload[key] is not None:
                current[key] = payload[key]
        for key in BOOL_SETTINGS:
            current[key] = _as_bool(current.get(key))
        if current.get("rewrite_variant") not in REWRITE_VARIANTS:
            current["rewrite_variant"] = "8b"
        if current.get("tts_provider") in {"clone", ""}:
            current["tts_provider"] = "confucius4"
        if current.get("tts_provider") not in TTS_PROVIDERS:
            current["tts_provider"] = "confucius4"
        if current.get("tts_text_mode") not in {"dialogue", "dialogue_or_action"}:
            current["tts_text_mode"] = "dialogue_or_action"
        current["rewrite_model"] = str(current.get("rewrite_model") or "").strip()
        current["rewrite_adapter"] = str(current.get("rewrite_adapter") or "").strip()
        current["tts_voice"] = str(current.get("tts_voice") or "").strip() or "speaker1"
        current["tts_lang"] = str(current.get("tts_lang") or "zh").strip() or "zh"
        current["tts_url"] = str(current.get("tts_url") or "http://127.0.0.1:7860").rstrip("/")
        current["tts_confucius_dir"] = str(current.get("tts_confucius_dir") or "").strip()
        current["tts_confucius_python"] = str(current.get("tts_confucius_python") or "").strip()
        current["tts_speed"] = _clamp_float(current.get("tts_speed"), 1.0, 0.5, 2.0)
        current["tts_volume"] = _clamp_float(current.get("tts_volume"), 1.0, 0.0, 2.0)
        if current.get("llm_backend") not in {"comfy", "ollama"}:
            current["llm_backend"] = "comfy"
        if current.get("h3_resolution") not in H3_RESOLUTION_CHOICES:
            current["h3_resolution"] = "960x544"
        try:
            current["h3_steps"] = max(1, min(40, int(current.get("h3_steps") or 8)))
        except (TypeError, ValueError):
            current["h3_steps"] = 8
        try:
            current["llm_max_tokens"] = max(256, min(32768, int(current.get("llm_max_tokens") or 4096)))
        except (TypeError, ValueError):
            current["llm_max_tokens"] = 4096
        current["llm_temperature"] = _clamp_float(current.get("llm_temperature"), 0.4, 0.01, 2.0)
        current["llm_model"] = str(current.get("llm_model") or "").strip()
        current["llm_workflow"] = str(current.get("llm_workflow") or "05_llm_textgen.json").strip()
        current["llm_clip_type"] = str(current.get("llm_clip_type") or "ltxv").strip() or "ltxv"
        current["ollama_url"] = str(current["ollama_url"]).rstrip("/")
        current["comfy_url"] = str(current["comfy_url"]).rstrip("/")
        self.settings_path().write_text(json.dumps(current, ensure_ascii=False, indent=2), encoding="utf-8")
        return current

    def project_dir(self, project_id: str) -> Path:
        return self.data / "projects" / project_id

    def media_dir(self, project_id: str) -> Path:
        return self.project_dir(project_id) / "media"

    def project_path(self, project_id: str) -> Path:
        return self.project_dir(project_id) / "project.json"

    def list_projects(self) -> list[dict]:
        items = []
        root = self.data / "projects"
        if not root.is_dir():
            return []
        for folder in root.iterdir():
            path = folder / "project.json"
            if not path.is_file():
                continue
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                continue
            items.append(self.summary(data))
        items.sort(key=lambda item: item.get("updated_at") or "", reverse=True)
        return items

    def summary(self, project: dict) -> dict:
        board = project.get("storyboard") or {}
        shots = board.get("shots") or []
        return {
            "id": project.get("id"),
            "name": project.get("name"),
            "status": project.get("status"),
            "aspect": project.get("aspect"),
            "video_workflow": project.get("video_workflow"),
            "updated_at": project.get("updated_at"),
            "created_at": project.get("created_at"),
            "shot_count": len(shots),
            "error": project.get("error") or "",
            "final_video": project.get("final_video") or "",
        }

    def load_project(self, project_id: str) -> dict:
        path = self.project_path(project_id)
        if not path.is_file():
            raise FileNotFoundError(project_id)
        project = json.loads(path.read_text(encoding="utf-8"))
        stages = project.setdefault("stages", {})
        for key in STAGE_IDS:
            stages.setdefault(key, {"status": "pending", "message": ""})
        return project

    def save_project(self, project: dict) -> dict:
        project["updated_at"] = now_iso()
        folder = self.project_dir(project["id"])
        folder.mkdir(parents=True, exist_ok=True)
        self.media_dir(project["id"]).mkdir(parents=True, exist_ok=True)
        self.project_path(project["id"]).write_text(
            json.dumps(project, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return project

    def new_project(
        self,
        *,
        name: str,
        script: str,
        style: str,
        aspect: str,
        video_workflow: str,
        image_workflow: str,
        edit_workflow: str,
    ) -> dict:
        settings = self.load_settings()
        project_id = "p_" + uuid.uuid4().hex[:8]
        stamp = now_iso()
        project = {
            "id": project_id,
            "name": name.strip() or "未命名章节",
            "created_at": stamp,
            "updated_at": stamp,
            "style": style.strip() or settings["style"],
            "aspect": aspect if aspect in {"16:9", "9:16", "1:1"} else "16:9",
            "video_workflow": video_workflow if video_workflow in VIDEO_WORKFLOWS else settings["video_workflow"],
            "image_workflow": image_workflow or settings["image_workflow"],
            "edit_workflow": edit_workflow or settings["edit_workflow"],
            "script": script,
            "status": "idle",
            "error": "",
            "current_label": "",
            "stages": blank_stages(),
            "storyboard": None,
            "logs": [],
            "final_video": "",
        }
        return self.save_project(project)

    def seed_example(self) -> None:
        if self.project_path("demo_ch01").is_file():
            return
        script_path = self.assets / "input" / "chapter01.md"
        script = script_path.read_text(encoding="utf-8") if script_path.is_file() else ""
        example = load_example()
        settings = self.load_settings()
        board = normalize_storyboard(
            example,
            project_id="demo_ch01",
            title=example.get("title") or "客栈夜雨",
            style=example.get("style") or settings["style"],
            aspect=example.get("aspect") or "16:9",
            source_name="input/chapter01.md",
        )
        stamp = now_iso()
        stages = blank_stages()
        stages["stage0"] = {"status": "done", "message": "已载入示例章节"}
        stages["stage1"] = {"status": "done", "message": "示例分镜可编辑"}
        project = {
            "id": "demo_ch01",
            "name": board["title"],
            "created_at": stamp,
            "updated_at": stamp,
            "style": board["style"],
            "aspect": board["aspect"],
            "video_workflow": settings["video_workflow"],
            "image_workflow": settings["image_workflow"],
            "edit_workflow": settings["edit_workflow"],
            "script": script,
            "status": "awaiting_approval",
            "error": "",
            "current_label": "等待确认分镜",
            "stages": stages,
            "storyboard": board,
            "logs": [
                {
                    "ts": stamp,
                    "level": "info",
                    "message": "已载入示例章节《客栈夜雨》。分镜来自 configs/example_chapter.json，确认后继续出图出视频。",
                }
            ],
            "final_video": "",
        }
        self.save_project(project)

    def output_dir(self) -> Path:
        raw = Path(self.load_settings()["output_dir"])
        if not raw.is_absolute():
            raw = self.assets / raw
        raw.mkdir(parents=True, exist_ok=True)
        return raw

    def workflow_path(self, name: str) -> Path:
        base = (self.assets / "workflows").resolve()
        path = (base / Path(name).name).resolve()
        if path.parent != base or not path.is_file():
            raise FileNotFoundError(f"找不到工作流 {name}")
        return path

    def list_workflow_files(self) -> list[str]:
        folder = self.assets / "workflows"
        return sorted(p.name for p in folder.glob("*.json"))
