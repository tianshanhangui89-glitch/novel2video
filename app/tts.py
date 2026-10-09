"""Pluggable TTS for per-shot voiceover.

Providers
---------
* ``confucius4`` — **default**. User's local Confucius4-TTS ``server_local.py``
  (Transformers backend). Preset speakers ``speaker1`` / ``speaker2``, or a
  per-character reference WAV for zero-shot cloning. Cloning requires an
  explicit authorization checkbox at upload time; no celebrity voices ship
  with this app.
* ``cosyvoice`` — CosyVoice FastAPI, preset speakers only (kept as optional).
* ``local`` — offline fallback (espeak if present, else timed placeholder).

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

from app import httputil

from app import confucius

SAMPLE_RATE = 22050

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
    needs_confucius_dir: bool = False


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
        reference: Path | None = None,
        lang: str = "zh",
    ) -> float:
        """Write WAV to ``dest`` and return duration in seconds."""


PROVIDERS: dict[str, ProviderInfo] = {
    "confucius4": ProviderInfo(
        id="confucius4",
        label="Confucius4-TTS（本地，默认）",
        note=(
            "连本机 Confucius4-TTS 的 server_local.py（Transformers，不要用官方 vLLM server.py）。"
            "默认 http://127.0.0.1:7860。可用 speaker1 / speaker2 预设，或为角色上传已授权的参考 wav 做零样本克隆。"
            "超过 1024 字会自动分句后拼接。"
        ),
        needs_url=True,
        voices=(
            VoiceInfo("speaker1", "speaker1（默认中文女）"),
            VoiceInfo("speaker2", "speaker2"),
        ),
        cloning=True,
        needs_confucius_dir=True,
    ),
    "cosyvoice": ProviderInfo(
        id="cosyvoice",
        label="CosyVoice（本地预设音色）",
        note="可选。连本机 CosyVoice FastAPI，只用预训练音色，不走克隆。默认地址请改成 http://127.0.0.1:50000。",
        needs_url=True,
        voices=tuple(VoiceInfo(v, v) for v in COSY_PRESETS),
    ),
    "local": ProviderInfo(
        id="local",
        label="本地占位（免安装）",
        note="不需要 GPU。有 espeak-ng / espeak 就用它；否则生成按时长估算的占位音。Confucius4 不可用且开启回退时也会用到。",
        needs_url=False,
        voices=tuple(VoiceInfo(v, v) for v in ESPEAK_VOICES),
    ),
}


def provider_info(key: str | None) -> ProviderInfo:
    return PROVIDERS.get(key or "confucius4") or PROVIDERS["confucius4"]


def list_providers() -> list[dict[str, Any]]:
    return [
        {
            "id": item.id,
            "label": item.label,
            "note": item.note,
            "needs_url": item.needs_url,
            "cloning": item.cloning,
            "needs_confucius_dir": item.needs_confucius_dir,
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
    clipped = action if len(action) <= 48 else action[:47].rstrip("，,。.") + "…"
    return clipped


def estimate_duration(text: str, speed: float = 1.0) -> float:
    chars = max(1, len(re.sub(r"\s+", "", text or "")))
    seconds = chars / max(0.5, 4.0 * max(0.5, min(2.0, float(speed))))
    return max(0.6, min(20.0, seconds + 0.25))


def wav_duration(path: Path) -> float:
    with wave.open(str(path), "rb") as handle:
        frames = handle.getnframes()
        rate = handle.getframerate() or SAMPLE_RATE
    return frames / float(rate)


def write_placeholder_wav(dest: Path, seconds: float, *, seed: int = 0) -> float:
    seconds = max(0.4, min(float(seconds), 30.0))
    n = int(SAMPLE_RATE * seconds)
    dest.parent.mkdir(parents=True, exist_ok=True)
    base = 180.0 + (seed % 7) * 18.0
    frames = bytearray()
    for i in range(n):
        t = i / SAMPLE_RATE
        env = 0.22 * (0.55 + 0.45 * math.sin(2 * math.pi * (3.2 + (seed % 5) * 0.35) * t))
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
    name = key or "confucius4"
    if name == "local":
        return LocalFallbackProvider()
    if name == "cosyvoice":
        return CosyVoiceProvider()
    if name == "clone":
        # Legacy settings value → Confucius4 (authorized cloning lives there).
        return Confucius4Provider()
    return Confucius4Provider()


class Confucius4Provider:
    info = PROVIDERS["confucius4"]

    async def release(self, settings: dict) -> str:
        if settings.get("tts_subprocess"):
            return await confucius.MANAGER.stop()
        return (
            "Confucius4 HTTP 模式无卸载接口。配音结束后可手动停止 "
            "`server_local.py`（Ctrl+C）以释放 4090 显存；"
            "或在设置里改用「按需启动子进程」，由本程序在 TTS 阶段结束后自动结束进程。"
        )

    async def health(self, settings: dict) -> dict:
        status = await confucius.health(settings)
        if status["ok"]:
            return status
        if settings.get("tts_subprocess") and confucius.confucius_dir(settings):
            return {
                "ok": False,
                "detail": status["detail"] + "。已开启子进程模式：配音阶段会尝试自动启动 server_local.py",
                "url": status.get("url"),
            }
        return status

    async def synthesize(
        self,
        text: str,
        dest: Path,
        *,
        voice: str,
        speed: float,
        settings: dict,
        reference: Path | None = None,
        lang: str = "zh",
    ) -> float:
        # Ensure UTF-8 text (Chinese) — httpx sends unicode; encode check for safety.
        text = str(text or "").encode("utf-8").decode("utf-8").strip()
        if not text:
            raise TTSError("配音文本是空的")
        lang = (lang or settings.get("tts_lang") or "zh").strip() or "zh"
        try:
            await confucius.MANAGER.ensure(settings)
        except confucius.ConfuciusError as exc:
            raise TTSError(str(exc)) from exc

        ref = reference
        if ref is None:
            preset = (voice or settings.get("tts_voice") or "speaker1").strip() or "speaker1"
            if preset in {"speaker1", "speaker2"}:
                # speaker1 may omit reference (server default); speaker2 needs the file when available.
                found = confucius.resolve_preset_wav(settings, preset)
                if found is not None:
                    ref = found
                elif preset == "speaker2":
                    raise TTSError(
                        "找不到 speaker2 参考音频。请在设置里填写 Confucius4 目录，"
                        "或为角色上传已授权的参考 wav。"
                    )
                # speaker1 with no local file → omit reference, server uses its default.

        chunks = confucius.chunk_text(text, confucius.MAX_CHARS)
        if not chunks:
            raise TTSError("切段后没有可合成的文本")
        dest.parent.mkdir(parents=True, exist_ok=True)
        part_paths: list[Path] = []
        try:
            for index, chunk in enumerate(chunks):
                part = dest.with_name(f"{dest.stem}_p{index:02d}.wav")
                await confucius.synthesize_http(
                    settings,
                    chunk,
                    part,
                    lang=lang,
                    reference=ref,
                )
                part_paths.append(part)
            if len(part_paths) == 1:
                part_paths[0].replace(dest)
                return wav_duration(dest)
            return await asyncio.to_thread(confucius.concat_wavs, part_paths, dest)
        finally:
            for part in part_paths:
                if part != dest and part.is_file():
                    part.unlink(missing_ok=True)


class CosyVoiceProvider:
    info = PROVIDERS["cosyvoice"]

    async def release(self, settings: dict) -> str:
        url = (settings.get("tts_url") or "http://127.0.0.1:50000").rstrip("/")
        for path in ("/unload", "/free", "/api/unload"):
            try:
                async with httputil.client(url, timeout=8.0) as client:
                    response = await client.post(url + path)
                    if response.status_code < 500:
                        return f"已请求 CosyVoice {path}"
            except httpx.HTTPError:
                continue
        return "CosyVoice 无卸载接口，依赖服务端自行换页或下次启动清理"

    async def health(self, settings: dict) -> dict:
        url = (settings.get("tts_url") or "http://127.0.0.1:50000").rstrip("/")
        try:
            async with httputil.client(url, timeout=4.0) as client:
                response = await client.get(url + "/docs")
                if response.status_code >= 500:
                    return {"ok": False, "detail": f"CosyVoice 返回 {response.status_code}"}
                return {"ok": True, "detail": f"已连接 {url}，请确认模型已加载"}
        except httpx.HTTPError as exc:
            return {"ok": False, "detail": f"连不上 CosyVoice（{url}）：{exc}"}

    async def synthesize(
        self,
        text: str,
        dest: Path,
        *,
        voice: str,
        speed: float,
        settings: dict,
        reference: Path | None = None,
        lang: str = "zh",
    ) -> float:
        del reference, lang  # CosyVoice path is preset-only.
        url = (settings.get("tts_url") or "http://127.0.0.1:50000").rstrip("/")
        spk = (voice or "中文女").strip() or "中文女"
        speed = max(0.5, min(2.0, float(speed or 1.0)))
        dest.parent.mkdir(parents=True, exist_ok=True)
        data = {"tts_text": text, "spk_id": spk, "speed": str(speed)}
        try:
            async with httputil.client(url, timeout=180.0) as client:
                response = await client.post(url + "/inference_sft", data=data)
                if response.status_code >= 400:
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
        try:
            return wav_duration(dest)
        except wave.Error:
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

    async def synthesize(
        self,
        text: str,
        dest: Path,
        *,
        voice: str,
        speed: float,
        settings: dict,
        reference: Path | None = None,
        lang: str = "zh",
    ) -> float:
        del reference, lang
        binary = self._espeak()
        speed = max(0.5, min(2.0, float(speed or 1.0)))
        if binary:
            return await asyncio.to_thread(self._espeak_synth, binary, text, dest, voice, speed)
        seconds = estimate_duration(text, speed)
        return await asyncio.to_thread(write_placeholder_wav, dest, seconds, seed=abs(hash(text)) % 97)

    def _espeak_synth(self, binary: str, text: str, dest: Path, voice: str, speed: float) -> float:
        dest.parent.mkdir(parents=True, exist_ok=True)
        wpm = max(80, min(350, int(175 * speed)))
        voice = (voice or "zh").strip() or "zh"
        wav = dest.with_suffix(".espeak.wav")
        cmd = [binary, "-v", voice, "-s", str(wpm), "-w", str(wav), text]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0 or not wav.is_file():
            raise TTSError((result.stderr or result.stdout or "espeak 失败").strip()[:240])
        from app.stitch import ffmpeg_bin, probe_duration, _run

        _run([ffmpeg_bin(), "-y", "-i", str(wav), "-ar", str(SAMPLE_RATE), "-ac", "1", str(dest)])
        wav.unlink(missing_ok=True)
        return probe_duration(dest)
