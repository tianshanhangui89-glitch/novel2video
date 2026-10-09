"""Storyboard JSON: parse model output, normalize, and build a mock when Ollama is down."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]


class StoryboardError(ValueError):
    pass


def parse_json_content(raw: str) -> dict:
    text = (raw or "").strip()
    if not text:
        raise StoryboardError("模型没有返回内容")
    fence = re.search(r"```(?:json)?\s*([\s\S]*?)```", text)
    if fence:
        text = fence.group(1).strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end <= start:
            raise StoryboardError("模型输出不是 JSON") from None
        try:
            data = json.loads(text[start : end + 1])
        except json.JSONDecodeError as exc:
            raise StoryboardError(f"无法解析分镜 JSON：{exc}") from exc
    if isinstance(data, dict) and isinstance(data.get("storyboard"), dict):
        data = data["storyboard"]
    if not isinstance(data, dict):
        raise StoryboardError("分镜 JSON 必须是对象")
    return data


def _clip(value: Any, limit: int = 4000) -> str:
    text = "" if value is None else str(value).strip()
    return text[:limit]


def _duration(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = 5
    return max(2.0, min(15.0, number))


def _float_field(value: Any, default: float, low: float, high: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = default
    return max(low, min(high, number))


def _string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        parts = re.split(r"[,，\s]+", value)
        return [p for p in parts if p]
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return []


def normalize_storyboard(
    data: dict,
    *,
    project_id: str,
    title: str,
    style: str,
    aspect: str,
    source_name: str = "",
) -> dict:
    if aspect not in {"16:9", "9:16", "1:1"}:
        aspect = "16:9"
    characters = []
    for index, raw in enumerate(data.get("characters") or []):
        if not isinstance(raw, dict):
            continue
        appearance = _clip(raw.get("appearance"))
        characters.append(
            {
                "id": _clip(raw.get("id") or f"c{index + 1}", 64) or f"c{index + 1}",
                "name": _clip(raw.get("name") or f"角色{index + 1}", 80),
                "appearance": appearance,
                "ref_prompt": _clip(raw.get("ref_prompt") or appearance),
                "voice_note": _clip(raw.get("voice_note"), 200),
                "ref_image": _clip(raw.get("ref_image"), 300),
            }
        )
    scenes = []
    for index, raw in enumerate(data.get("scenes") or []):
        if not isinstance(raw, dict):
            continue
        scenes.append(
            {
                "id": _clip(raw.get("id") or f"s{index + 1}", 64) or f"s{index + 1}",
                "name": _clip(raw.get("name") or f"场景{index + 1}", 80),
                "visual": _clip(raw.get("visual")),
                "ref_image": _clip(raw.get("ref_image"), 300),
            }
        )
    props = []
    for index, raw in enumerate(data.get("props") or []):
        if not isinstance(raw, dict):
            continue
        visual = _clip(raw.get("visual") or raw.get("appearance"))
        props.append(
            {
                "id": _clip(raw.get("id") or f"p{index + 1}", 64) or f"p{index + 1}",
                "name": _clip(raw.get("name") or f"道具{index + 1}", 80),
                "visual": visual,
                "ref_prompt": _clip(raw.get("ref_prompt") or visual),
                "ref_image": _clip(raw.get("ref_image"), 300),
            }
        )
    shots = []
    for index, raw in enumerate(data.get("shots") or []):
        if not isinstance(raw, dict):
            continue
        shot_id = _clip(raw.get("shot_id") or f"{index + 1:03d}", 16)
        if shot_id.isdigit():
            shot_id = f"{int(shot_id):03d}"
        action = _clip(raw.get("action"))
        shots.append(
            {
                "shot_id": shot_id or f"{index + 1:03d}",
                "duration_sec": _duration(raw.get("duration_sec")),
                "scene_id": _clip(raw.get("scene_id"), 64),
                "characters": _string_list(raw.get("characters")),
                "camera": _clip(raw.get("camera"), 200),
                "action": action,
                "dialogue": _clip(raw.get("dialogue"), 500),
                "sfx": _clip(raw.get("sfx"), 200),
                "image_prompt": _clip(raw.get("image_prompt") or action),
                "motion_prompt": _clip(raw.get("motion_prompt") or "slow cinematic camera move, natural motion"),
                "keyframe": _clip(raw.get("keyframe"), 300),
                "video": _clip(raw.get("video"), 300),
                "h3_prompt_original": _clip(raw.get("h3_prompt_original"), 8000),
                "h3_prompt": _clip(raw.get("h3_prompt"), 8000),
                "h3_prompt_source": _clip(raw.get("h3_prompt_source"), 32),
                "h3_prompt_note": _clip(raw.get("h3_prompt_note"), 500),
                "tts_audio": _clip(raw.get("tts_audio"), 300),
                "tts_text": _clip(raw.get("tts_text"), 1000),
                "tts_duration": _float_field(raw.get("tts_duration"), 0.0, 0.0, 60.0),
                "tts_source": _clip(raw.get("tts_source"), 32),
                "tts_note": _clip(raw.get("tts_note"), 500),
                "status": _clip(raw.get("status") or "pending", 32) or "pending",
                "error": _clip(raw.get("error"), 500),
            }
        )
    if not shots:
        raise StoryboardError("分镜里没有镜头")
    return {
        "project_id": project_id,
        "title": title or project_id,
        "style": style,
        "aspect": aspect,
        "source": {
            "type": "novel_chapter",
            "path": source_name,
            "language": "zh",
        },
        "characters": characters,
        "scenes": scenes,
        "props": props,
        "shots": shots,
    }


def load_example() -> dict:
    path = ROOT / "configs" / "example_chapter.json"
    return json.loads(path.read_text(encoding="utf-8"))


def mock_storyboard(
    script: str,
    *,
    project_id: str,
    title: str,
    style: str,
    aspect: str,
) -> dict:
    """Offline stand-in. The sample chapter keeps the curated example."""
    text = script or ""
    if "林远" in text or "客栈夜雨" in text:
        data = load_example()
    else:
        data = _heuristic(text, style)
    data["style"] = style or data.get("style") or ""
    data["aspect"] = aspect
    return normalize_storyboard(
        data,
        project_id=project_id,
        title=title or data.get("title") or project_id,
        style=data["style"],
        aspect=aspect,
        source_name="mock",
    )


def _heuristic(script: str, style: str) -> dict:
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", script) if p.strip()]
    if not paragraphs:
        line = script.strip() or "空镜头"
        paragraphs = [line]
    shots = []
    for index, para in enumerate(paragraphs[:8], start=1):
        summary = re.sub(r"\s+", " ", para)[:120]
        shots.append(
            {
                "shot_id": f"{index:03d}",
                "duration_sec": 4,
                "scene_id": "s1",
                "characters": ["c1"],
                "camera": "中景",
                "action": summary,
                "dialogue": "",
                "sfx": "",
                "image_prompt": f"{style}, cinematic still, {summary}",
                "motion_prompt": "slow cinematic camera move, subtle character motion, natural lighting",
            }
        )
    return {
        "characters": [
            {
                "id": "c1",
                "name": "叙述者",
                "appearance": "与章节描述一致的主角，服装和脸部在全章保持一致",
                "ref_prompt": "story protagonist, consistent face and costume, full body character sheet",
                "voice_note": "",
            }
        ],
        "scenes": [{"id": "s1", "name": "章节主场景", "visual": paragraphs[0][:180]}],
        "props": [],
        "shots": shots,
    }


def system_prompt() -> str:
    path = ROOT / "prompts" / "stage1_storyboard.txt"
    return path.read_text(encoding="utf-8")
