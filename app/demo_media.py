"""Offline stand-in frames and clips when ComfyUI is not running."""

from __future__ import annotations

import subprocess
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from app.stitch import StitchError, ffmpeg_bin
from app.tts import estimate_duration, write_placeholder_wav

FONT = Path("/usr/share/fonts/truetype/wqy/wqy-microhei.ttc")


def _font(size: int) -> ImageFont.ImageFont:
    if FONT.is_file():
        return ImageFont.truetype(str(FONT), size)
    return ImageFont.load_default()


def _canvas(aspect: str) -> tuple[int, int]:
    if aspect == "9:16":
        return 540, 960
    if aspect == "1:1":
        return 768, 768
    return 960, 540


def write_card(path: Path, title: str, subtitle: str, aspect: str, kind: str) -> None:
    width, height = _canvas(aspect)
    image = Image.new("RGB", (width, height), "#1a1612")
    draw = ImageDraw.Draw(image)
    margin = max(24, width // 28)
    draw.rounded_rectangle(
        (margin, margin, width - margin, height - margin),
        radius=18,
        outline="#e39b45",
        width=3,
    )
    label_font = _font(max(18, width // 38))
    title_font = _font(max(28, width // 18))
    sub_font = _font(max(16, width // 42))
    draw.text((margin + 24, margin + 20), "演示画面 · " + kind, fill="#e39b45", font=label_font)
    draw.text((margin + 24, height // 2 - 36), title[:18], fill="#f6f0e6", font=title_font)
    draw.multiline_text(
        (margin + 24, height // 2 + 28),
        (subtitle or "")[:80],
        fill="#b3a48f",
        font=sub_font,
        spacing=6,
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    image.save(path, "PNG")


def write_clip(image: Path, dest: Path, seconds: float) -> None:
    seconds = max(1.0, min(float(seconds), 2.0))
    dest.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        ffmpeg_bin(),
        "-y",
        "-loop",
        "1",
        "-i",
        str(image),
        "-t",
        f"{seconds:.2f}",
        "-r",
        "24",
        "-vf",
        "format=yuv420p",
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        "-crf",
        "28",
        "-an",
        str(dest),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        tail = "\n".join((result.stderr or "").splitlines()[-8:])
        raise StitchError(tail or "演示视频编码失败")


def write_speech(dest: Path, text: str, speed: float = 1.0) -> float:
    """Demo-mode stand-in voiceover: soft chirp sized to the spoken text."""
    seconds = estimate_duration(text, speed)
    return write_placeholder_wav(dest, seconds, seed=abs(hash(text)) % 97)
