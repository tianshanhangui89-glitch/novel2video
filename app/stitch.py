"""Normalize shot videos with FFmpeg and concatenate them."""

from __future__ import annotations

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


def normalize_clip(src: Path, dst: Path, width: int, height: int) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    vf = (
        f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,fps=24,format=yuv420p"
    )
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


def stitch(shots: list[Path], dest: Path, width: int, height: int, work_dir: Path) -> None:
    work_dir.mkdir(parents=True, exist_ok=True)
    normalized: list[Path] = []
    for index, src in enumerate(shots, start=1):
        if not src.is_file():
            raise StitchError(f"缺少镜头文件 {src.name}")
        out = work_dir / f"norm_{index:03d}.mp4"
        normalize_clip(src, out, width, height)
        normalized.append(out)
    concat_clips(normalized, dest)
