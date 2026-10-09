"""Pluggable TTS for per-shot voiceover.

Providers
---------
* ``cosyvoice`` — local CosyVoice HTTP server, **preset speakers only**
  (``POST /inference_sft`` with ``tts_text`` + ``spk_id``). Default URL
  ``http://127.0.0.1:50000``.
* ``local`` — offline fallback that needs no extra install. Prefer
  ``espeak-ng`` / ``espeak`` when on ``PATH``; otherwise write a soft
  speech-shaped placeholder WAV sized by character count so alignment
  and the mix still run.
* ``clone`` — **stub only**. Voice cloning is not implemented. When it
  is added later it must use only the user's own voice or a voice they
  have explicit authorization to use. Do not wire zero-shot / reference
  audio paths here.

Demo mode never calls a provider: the pipeline writes a short chirp WAV
itself (see ``app.demo_media.write_speech``).
"""

from __future__ import annotations

import asyncio
import math
import re
import shutil
import struct
import subprocess
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import httpx

SAMPLE_RATE = 22050

# CosyVoice-300M-SFT preset speakers commonly shipped with the official server.
COSY_PRESETS = (
    "中文女",
    "中文男",
    "英文女",
    "英文男",
    "日语男",
    "粤语女",
)

ESPEAK_VOICES = (
    "zh",
    "zh+f2",
    "zh+m3",
    "en",
    "en+f3",
    "en+m3",
)


class TTSError(RuntimeError):
    pass


@dataclass(frozen=True)
class VoiceInfo:
    id: str
    label: str


@dataclass(frozen=True)
class ProviderInfo:
    id: str
    label: str
    note: str
    needs_url: bool
    voices: tuple[VoiceInfo, ...]
    cloning: bool = False


class TTSProvider(Protocol):
    info: ProviderInfo

    async def health(self, settings: dict) -> dict: ...

    async def synthesize(
        self,
        text: str,
        dest: Path,
        *,
        voice: str,
        speed: float,
        settings: dict,
    ) -> float:
        """Write WAV to ``dest`` and return duration in seconds."""


PROVIDERS: dict[str, ProviderInfo] = {
    "cosyvoice": ProviderInfo(
        id="cosyvoice",
        label="CosyVoice（本地预设音色）",
        note="连本机 CosyVoice FastAPI。只用预训练音色（中文女 / 中文男 等），不走克隆。默认 http://127.0.0.1:50000。",
        needs_url=True,
        voices=tuple(VoiceInfo(v, v) for v in COSY_PRESETS),
    ),
    "local": ProviderInfo(
        id="local",
        label="本地占位（免安装）",
        note="不需要 GPU。有 espeak-ng / espeak 就用它；否则生成按时长估算的占位音，用来走通对齐和混音。",
        needs_url=False,
        voices=tuple(VoiceInfo(v, v) for v in ESPEAK_VOICES),
    ),
    "clone": ProviderInfo(
        id="clone",
        label="声音克隆（未实现）",
        note=(
            "预留接口。将来只会支持用户本人的声音，或用户明确授权的声音；"
            "不会在未授权情况下复刻他人音色。当前选择此项会报错。"
        ),
        needs_url=False,
        voices=(),
        cloning=True,
    ),
}


def provider_info(key: str | None) -> ProviderInfo:
    return PROVIDERS.get(key or "cosyvoice") or PROVIDERS["cosyvoice"]


def list_providers() -> list[dict[str, Any]]:
    return [
        {
            "id": item.id,
            "label": item.label,
            "note": item.note,
            "needs_url": item.needs_url,
            "cloning": item.cloning,
            "voices": [{"id": v.id, "label": v.label} for v in item.voices],
        }
        for item in PROVIDERS.values()
    ]


def speakable_text(shot: dict, mode: str = "dialogue_or_action") -> str:
    """Prefer dialogue; optionally fall back to a short narration from the action."""
    dialogue = (shot.get("dialogue") or "").strip()
    if dialogue:
        return dialogue
    if mode != "dialogue_or_action":
        return ""
    action = (shot.get("action") or "").strip()
    if not action:
        return ""
    # Keep narration short so placeholder / CosyVoice stays within the shot.
    clipped = action if len(action) <= 48 else action[:47].rstrip("，,。.") + "…"
    return clipped


def estimate_duration(text: str, speed: float = 1.0) -> float:
    """Rough spoken length used by the placeholder path and demo mode."""
    chars = max(1, len(re.sub(r"\s+", "", text or "")))
    # ~4 Chinese chars/sec at speed 1.0; clamp so a one-liner still has body.
    seconds = chars / max(0.5, 4.0 * max(0.5, min(2.0, float(speed))))
    return max(0.6, min(20.0, seconds + 0.25))


def wav_duration(path: Path) -> float:
    with wave.open(str(path), "rb") as handle:
        frames = handle.getnframes()
        rate = handle.getframerate() or SAMPLE_RATE
    return frames / float(rate)


def write_placeholder_wav(dest: Path, seconds: float, *, seed: int = 0) -> float:
    """Soft amplitude-modulated tone — not speech, but enough for mix/align tests."""
    seconds = max(0.4, min(float(seconds), 30.0))
    n = int(SAMPLE_RATE * seconds)
    dest.parent.mkdir(parents=True, exist_ok=True)
    base = 180.0 + (seed % 7) * 18.0
    frames = bytearray()
    for i in range(n):
        t = i / SAMPLE_RATE
        env = 0.22 * (0.55 + 0.45 * math.sin(2 * math.pi * (3.2 + (seed % 5) * 0.35) * t))
        # Brief syllable-like gaps every ~0.28s so it is audibly "spoken-ish".
        if (i % int(SAMPLE_RATE * 0.28)) > int(SAMPLE_RATE * 0.22):
            sample = 0
        else:
            sample = int(env * 32767 * math.sin(2 * math.pi * base * t))
        frames += struct.pack("<h", max(-32767, min(32767, sample)))
    with wave.open(str(dest), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(SAMPLE_RATE)
        handle.writeframes(frames)
    return seconds


def get_provider(key: str | None) -> TTSProvider:
    name = key or "cosyvoice"
    if name == "local":
        return LocalFallbackProvider()
    if name == "clone":
        return VoiceCloneStub()
    return CosyVoiceProvider()


class CosyVoiceProvider:
    info = PROVIDERS["cosyvoice"]

    async def release(self, settings: dict) -> str:
        """Best-effort unload. Official CosyVoice FastAPI has no standard free route."""
        url = (settings.get("tts_url") or "http://127.0.0.1:50000").rstrip("/")
        for path in ("/unload", "/free", "/api/unload"):
            try:
                async with httpx.AsyncClient(timeout=8.0) as client:
                    response = await client.post(url + path)
                    if response.status_code < 500:
                        return f"已请求 CosyVoice {path}"
            except httpx.HTTPError:
                continue
        return "CosyVoice 无卸载接口，依赖服务端自行换页或下次启动清理"

    async def health(self, settings: dict) -> dict:
        url = (settings.get("tts_url") or "http://127.0.0.1:50000").rstrip("/")
        try:
            async with httpx.AsyncClient(timeout=4.0) as client:
                # Official server has no dedicated health route; any response short of a
                # connection error means the process is up.
                response = await client.get(url + "/docs")
                if response.status_code >= 500:
                    return {"ok": False, "detail": f"CosyVoice 返回 {response.status_code}"}
                return {"ok": True, "detail": f"已连接 {url}，请确认模型已加载"}
        except httpx.HTTPError as exc:
            return {"ok": False, "detail": f"连不上 CosyVoice（{url}）：{exc}"}

    async def synthesize(self, text: str, dest: Path, *, voice: str, speed: float, settings: dict) -> float:
        url = (settings.get("tts_url") or "http://127.0.0.1:50000").rstrip("/")
        spk = (voice or "中文女").strip() or "中文女"
        speed = max(0.5, min(2.0, float(speed or 1.0)))
        dest.parent.mkdir(parents=True, exist_ok=True)
        data = {"tts_text": text, "spk_id": spk}
        # Official runtime_fastapi.py accepts form fields; speed is supported by the
        # inference API even when the thin HTTP wrapper ignores unknown keys.
        data["speed"] = str(speed)
        try:
            async with httpx.AsyncClient(timeout=180.0) as client:
                response = await client.post(url + "/inference_sft", data=data)
                if response.status_code >= 400:
                    # Some forks expose JSON /inference instead.
                    response = await client.post(
                        url + "/inference",
                        json={"mode": "sft", "tts_text": text, "spk_id": spk, "speed": speed, "stream": False},
                    )
                response.raise_for_status()
                body = response.content
        except httpx.HTTPError as exc:
            raise TTSError(f"CosyVoice 合成失败：{exc}") from exc
        if len(body) < 64:
            raise TTSError("CosyVoice 返回的音频是空的")
        dest.write_bytes(body)
        # CosyVoice streams raw PCM/WAV; probe with wave, fall back to ffprobe via duration estimate.
        try:
            return wav_duration(dest)
        except wave.Error:
            # Non-WAV container (e.g. bare PCM). Re-wrap as WAV via ffmpeg if needed.
            from app.stitch import ffmpeg_bin, probe_duration, _run

            wrapped = dest.with_suffix(".wrap.wav")
            try:
                _run([ffmpeg_bin(), "-y", "-i", str(dest), "-ar", str(SAMPLE_RATE), "-ac", "1", str(wrapped)])
                wrapped.replace(dest)
                return probe_duration(dest)
            except Exception as exc:
                raise TTSError(f"CosyVoice 音频无法解析：{exc}") from exc


class LocalFallbackProvider:
    info = PROVIDERS["local"]

    async def release(self, settings: dict) -> str:
        return "本地占位配音无需卸载"

    def _espeak(self) -> str | None:
        return shutil.which("espeak-ng") or shutil.which("espeak")

    async def health(self, settings: dict) -> dict:
        binary = self._espeak()
        if binary:
            return {"ok": True, "detail": f"将使用 {binary} 合成语音"}
        return {
            "ok": True,
            "detail": "未找到 espeak-ng，将生成按时长估算的占位音（免安装，可走通对齐和混音）",
        }

    async def synthesize(self, text: str, dest: Path, *, voice: str, speed: float, settings: dict) -> float:
        binary = self._espeak()
        speed = max(0.5, min(2.0, float(speed or 1.0)))
        if binary:
            return await asyncio.to_thread(self._espeak_synth, binary, text, dest, voice, speed)
        seconds = estimate_duration(text, speed)
        return await asyncio.to_thread(write_placeholder_wav, dest, seconds, seed=abs(hash(text)) % 97)

    def _espeak_synth(self, binary: str, text: str, dest: Path, voice: str, speed: float) -> float:
        dest.parent.mkdir(parents=True, exist_ok=True)
        # espeak -s is words-per-minute; 175 ≈ 1.0x for Chinese.
        wpm = max(80, min(350, int(175 * speed)))
        voice = (voice or "zh").strip() or "zh"
        wav = dest.with_suffix(".espeak.wav")
        cmd = [binary, "-v", voice, "-s", str(wpm), "-w", str(wav), text]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0 or not wav.is_file():
            raise TTSError((result.stderr or result.stdout or "espeak 失败").strip()[:240])
        # Normalize sample rate for the mixer.
        from app.stitch import ffmpeg_bin, probe_duration, _run

        _run([ffmpeg_bin(), "-y", "-i", str(wav), "-ar", str(SAMPLE_RATE), "-ac", "1", str(dest)])
        wav.unlink(missing_ok=True)
        return probe_duration(dest)


class VoiceCloneStub:
    """Future home for authorized voice cloning. Not wired to any zero-shot path yet."""

    info = PROVIDERS["clone"]

    async def release(self, settings: dict) -> str:
        return "声音克隆未实现，无需卸载"

    async def health(self, settings: dict) -> dict:
        return {
            "ok": False,
            "detail": (
                "声音克隆尚未实现。上线后只会接受用户本人的参考音，"
                "或用户明确授权的声音；当前请改用 CosyVoice 预设音色或本地占位。"
            ),
        }

    async def synthesize(self, text: str, dest: Path, *, voice: str, speed: float, settings: dict) -> float:
        raise TTSError(
            "声音克隆尚未实现（预留接口）。请改用 CosyVoice 预设音色或本地占位。"
            "将来只会支持用户本人的声音，或用户明确授权的声音。"
        )
