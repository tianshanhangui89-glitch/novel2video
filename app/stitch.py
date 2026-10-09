"""Normalize shot videos with FFmpeg, mix voiceovers, and concatenate them."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path


class StitchError(RuntimeError):
    pass


def ffmpeg_bin() -> str:
    path = shutil.which("ffmpeg")
    if not path:
        raise StitchError("未找到 ffmpeg。请先安装并加入 PATH。")
    return path


def ffprobe_bin() -> str:
    path = shutil.which("ffprobe")
    if not path:
        raise StitchError("未找到 ffprobe。请先安装并加入 PATH。")
    return path


def ffmpeg_version() -> str:
    binary = ffmpeg_bin()
    result = subprocess.run([binary, "-version"], capture_output=True, text=True, timeout=15)
    if result.returncode != 0:
        raise StitchError(result.stderr.strip() or "ffmpeg -version 失败")
    return (result.stdout or result.stderr).splitlines()[0]


def _run(cmd: list[str]) -> None:
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        tail = (result.stderr or result.stdout or "").strip().splitlines()
        detail = "\n".join(tail[-12:])
        raise StitchError(detail or "ffmpeg 失败")


def probe_duration(path: Path) -> float:
    """Media duration in seconds (video or audio)."""
    result = subprocess.run(
        [
            ffprobe_bin(),
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "json",
            str(path),
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise StitchError((result.stderr or "ffprobe 失败").strip()[:240])
    try:
        data = json.loads(result.stdout or "{}")
        return max(0.0, float((data.get("format") or {}).get("duration") or 0))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise StitchError(f"无法读取时长：{path.name}") from exc


def pad_or_trim_video(src: Path, dest: Path, target_sec: float) -> float:
    """Stretch a clip to ``target_sec`` by freezing the last frame, or trim if longer."""
    target_sec = max(0.5, float(target_sec))
    current = probe_duration(src)
    dest.parent.mkdir(parents=True, exist_ok=True)
    if abs(current - target_sec) < 0.05:
        if src.resolve() != dest.resolve():
            shutil.copy2(src, dest)
        return current
    if current < target_sec:
        pad = target_sec - current
        _run(
            [
                ffmpeg_bin(),
                "-y",
                "-i",
                str(src),
                "-vf",
                f"tpad=stop_mode=clone:stop_duration={pad:.3f}",
                "-c:v",
                "libx264",
                "-preset",
                "veryfast",
                "-crf",
                "20",
                "-an",
                str(dest),
            ]
        )
    else:
        _run(
            [
                ffmpeg_bin(),
                "-y",
                "-i",
                str(src),
                "-t",
                f"{target_sec:.3f}",
                "-c:v",
                "libx264",
                "-preset",
                "veryfast",
                "-crf",
                "20",
                "-an",
                str(dest),
            ]
        )
    return probe_duration(dest)


def normalize_clip(
    src: Path,
    dst: Path,
    width: int,
    height: int,
    *,
    voiceover: Path | None = None,
    volume: float = 1.0,
) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    vf = (
        f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,fps=24,format=yuv420p"
    )
    vol = max(0.0, min(2.0, float(volume)))
    if voiceover and voiceover.is_file():
        # Video length wins for the picture; voice is padded/trimmed to match.
        filter_complex = (
            f"[0:v]{vf}[v];"
            f"[1:a]volume={vol:.3f},apad,atrim=0:9999,asetpts=PTS-STARTPTS[a]"
        )
        _run(
            [
                ffmpeg_bin(),
                "-y",
                "-i",
                str(src),
                "-i",
                str(voiceover),
                "-filter_complex",
                filter_complex,
                "-map",
                "[v]",
                "-map",
                "[a]",
                "-shortest",
                "-c:v",
                "libx264",
                "-preset",
                "veryfast",
                "-crf",
                "20",
                "-c:a",
                "aac",
                "-ar",
                "44100",
                "-ac",
                "2",
                str(dst),
            ]
        )
        return
    _run(
        [
            ffmpeg_bin(),
            "-y",
            "-i",
            str(src),
            "-f",
            "lavfi",
            "-i",
            "anullsrc=channel_layout=stereo:sample_rate=44100",
            "-shortest",
            "-vf",
            vf,
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "20",
            "-c:a",
            "aac",
            "-ar",
            "44100",
            "-ac",
            "2",
            str(dst),
        ]
    )


def concat_clips(clips: list[Path], dest: Path) -> None:
    if not clips:
        raise StitchError("没有可拼接的镜头视频")
    dest.parent.mkdir(parents=True, exist_ok=True)
    if len(clips) == 1:
        _run([ffmpeg_bin(), "-y", "-i", str(clips[0]), "-c", "copy", str(dest)])
        return
    list_path = dest.with_suffix(".concat.txt")
    lines = []
    for clip in clips:
        escaped = str(clip.resolve()).replace("'", r"'\''")
        lines.append(f"file '{escaped}'")
    list_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    try:
        _run(
            [
                ffmpeg_bin(),
                "-y",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                str(list_path),
                "-c",
                "copy",
                str(dest),
            ]
        )
    finally:
        list_path.unlink(missing_ok=True)


def stitch(
    shots: list[Path],
    dest: Path,
    width: int,
    height: int,
    work_dir: Path,
    *,
    voiceovers: list[Path | None] | None = None,
    volume: float = 1.0,
) -> None:
    work_dir.mkdir(parents=True, exist_ok=True)
    voices = voiceovers or [None] * len(shots)
    if len(voices) != len(shots):
        raise StitchError("配音列表和镜头数量不一致")
    normalized: list[Path] = []
    for index, (src, voice) in enumerate(zip(shots, voices), start=1):
        if not src.is_file():
            raise StitchError(f"缺少镜头文件 {src.name}")
        out = work_dir / f"norm_{index:03d}.mp4"
        normalize_clip(src, out, width, height, voiceover=voice, volume=volume)
        normalized.append(out)
    concat_clips(normalized, dest)
