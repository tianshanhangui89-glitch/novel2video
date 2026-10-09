"""Confucius4-TTS local server helper: health, HTTP synth, optional subprocess lifecycle.

Talks to the user's ``server_local.py`` (Transformers backend), **not** the
official vLLM ``server.py``:

    GET  /health      → {ok, sample_rate, cuda}
    POST /api/tts     multipart: text (<=1024), lang, reference? → audio/wav
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import socket
import time
from pathlib import Path
from typing import Any

import httpx

log = logging.getLogger("novel2video.confucius")

DEFAULT_URL = "http://127.0.0.1:7860"
MAX_CHARS = 1024
PRESET_REL = {
    "speaker1": Path("pretrained/campplus/examples/speaker1_a_cn_16k.wav"),
    "speaker2": Path("pretrained/campplus/examples/speaker2_a_cn_16k.wav"),
}


class ConfuciusError(RuntimeError):
    pass


def base_url(settings: dict) -> str:
    return (settings.get("tts_url") or DEFAULT_URL).rstrip("/")


def confucius_dir(settings: dict) -> Path | None:
    raw = (settings.get("tts_confucius_dir") or "").strip()
    if not raw:
        return None
    path = Path(raw).expanduser()
    return path if path.is_dir() else None


def confucius_python(settings: dict) -> str:
    raw = (settings.get("tts_confucius_python") or "").strip()
    if raw:
        return raw
    root = confucius_dir(settings)
    if root:
        for candidate in (
            root / ".venv" / "Scripts" / "python.exe",  # Windows
            root / ".venv" / "bin" / "python",
            root / "venv" / "Scripts" / "python.exe",
            root / "venv" / "bin" / "python",
        ):
            if candidate.is_file():
                return str(candidate)
    return "python"


def resolve_preset_wav(settings: dict, voice: str) -> Path | None:
    """Locate speaker1 / speaker2 WAV under the Confucius4 install (optional)."""
    key = (voice or "speaker1").strip() or "speaker1"
    root = confucius_dir(settings)
    if not root:
        return None
    rel = PRESET_REL.get(key)
    if rel and (root / rel).is_file():
        return root / rel
    examples = root / "pretrained" / "campplus" / "examples"
    if examples.is_dir():
        hits = sorted(examples.glob(f"{key}*.wav"))
        if hits:
            return hits[0]
        # Fall back to any speakerN match.
        hits = sorted(examples.glob(f"*{key}*.wav"))
        if hits:
            return hits[0]
    return None


async def health(settings: dict, timeout: float = 4.0) -> dict[str, Any]:
    url = base_url(settings)
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.get(url + "/health")
            response.raise_for_status()
            data = response.json()
    except httpx.HTTPError as exc:
        return {"ok": False, "detail": f"连不上 Confucius4（{url}）：{exc}", "url": url}
    except ValueError as exc:
        return {"ok": False, "detail": f"Confucius4 /health 不是 JSON：{exc}", "url": url}
    ok = bool(data.get("ok", True))
    sample_rate = data.get("sample_rate")
    cuda = data.get("cuda")
    bits = [f"已连接 {url}"]
    if sample_rate:
        bits.append(f"{sample_rate} Hz")
    if cuda is not None:
        bits.append("CUDA" if cuda else "CPU")
    return {
        "ok": ok,
        "detail": "，".join(bits),
        "url": url,
        "sample_rate": sample_rate,
        "cuda": cuda,
        "raw": data,
    }


async def synthesize_http(
    settings: dict,
    text: str,
    dest: Path,
    *,
    lang: str = "zh",
    reference: Path | None = None,
    timeout: float = 300.0,
) -> float:
    """POST one chunk (<=1024 chars). Returns duration seconds."""
    from app.tts import wav_duration

    text = (text or "").strip()
    if not text:
        raise ConfuciusError("文本是空的")
    if len(text) > MAX_CHARS:
        raise ConfuciusError(f"单段超过 {MAX_CHARS} 字，请先切段")
    url = base_url(settings)
    dest.parent.mkdir(parents=True, exist_ok=True)
    data = {"text": text, "lang": (lang or "zh").strip() or "zh"}
    files = None
    handle = None
    try:
        if reference is not None:
            if not reference.is_file():
                raise ConfuciusError(f"参考音频不存在：{reference}")
            handle = reference.open("rb")
            files = {"reference": (reference.name, handle, "audio/wav")}
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(url + "/api/tts", data=data, files=files)
            if response.status_code >= 400:
                detail = (response.text or "")[:240]
                raise ConfuciusError(f"Confucius4 /api/tts 返回 {response.status_code}：{detail}")
            body = response.content
    except httpx.HTTPError as exc:
        raise ConfuciusError(f"Confucius4 合成失败：{exc}") from exc
    finally:
        if handle is not None:
            handle.close()
    if len(body) < 64:
        raise ConfuciusError("Confucius4 返回的音频是空的")
    dest.write_bytes(body)
    try:
        return wav_duration(dest)
    except Exception:
        from app.stitch import probe_duration

        return probe_duration(dest)


def chunk_text(text: str, limit: int = MAX_CHARS) -> list[str]:
    """Split long Chinese/English copy on sentence boundaries, never above ``limit``."""
    text = (text or "").strip()
    if not text:
        return []
    if len(text) <= limit:
        return [text]
    # Prefer sentence ends, then commas, then hard cuts.
    parts: list[str] = []
    buf = ""
    for ch in text:
        buf += ch
        if ch in "。！？!?；;\n" and len(buf) >= max(8, limit // 8):
            parts.append(buf.strip())
            buf = ""
        elif len(buf) >= limit:
            # Backtrack to a softer break.
            cut = max(buf.rfind("，"), buf.rfind(","), buf.rfind("、"), buf.rfind(" "))
            if cut < limit // 3:
                cut = limit
            parts.append(buf[:cut].strip())
            buf = buf[cut:].lstrip()
    if buf.strip():
        parts.append(buf.strip())
    # Guard any leftover oversize pieces.
    out: list[str] = []
    for part in parts:
        if not part:
            continue
        if len(part) <= limit:
            out.append(part)
            continue
        for i in range(0, len(part), limit):
            out.append(part[i : i + limit])
    return out


def concat_wavs(parts: list[Path], dest: Path) -> float:
    from app.stitch import ffmpeg_bin, probe_duration, _run

    dest.parent.mkdir(parents=True, exist_ok=True)
    if len(parts) == 1:
        dest.write_bytes(parts[0].read_bytes())
        return probe_duration(dest)
    list_path = dest.with_suffix(".concat.txt")
    lines = []
    for path in parts:
        escaped = str(path.resolve()).replace("'", r"'\''")
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
    return probe_duration(dest)


class ServerManager:
    """Optional on-demand ``server_local.py`` subprocess for low-VRAM machines."""

    def __init__(self) -> None:
        self._proc: asyncio.subprocess.Process | None = None
        self._started_by_us = False

    @property
    def running(self) -> bool:
        return self._proc is not None and self._proc.returncode is None

    async def ensure(self, settings: dict) -> list[str]:
        notes: list[str] = []
        status = await health(settings, timeout=2.0)
        if status["ok"]:
            return ["Confucius4 已在运行，复用现有进程"]
        if not settings.get("tts_subprocess"):
            raise ConfuciusError(
                status["detail"]
                + "。请先在 Confucius4-TTS 目录启动：python server_local.py --host 127.0.0.1 --port 7860"
                + "；或在设置里打开「按需启动子进程」。"
            )
        root = confucius_dir(settings)
        if root is None:
            raise ConfuciusError("子进程模式需要填写 Confucius4 目录（tts_confucius_dir）")
        script = root / "server_local.py"
        if not script.is_file():
            raise ConfuciusError(f"找不到 {script}。请确认目录是 Confucius4-TTS 根目录。")
        py = confucius_python(settings)
        host, port = _host_port(base_url(settings))
        if _port_open(host, port):
            # Something is listening but /health failed — still try once more after a short wait.
            await asyncio.sleep(1.0)
            status = await health(settings, timeout=3.0)
            if status["ok"]:
                return ["端口已有服务，健康检查通过"]
        notes.append(f"启动 Confucius4：{py} server_local.py --host {host} --port {port}")
        self._proc = await asyncio.create_subprocess_exec(
            py,
            str(script),
            "--host",
            host,
            "--port",
            str(port),
            cwd=str(root),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            env={**os.environ, "PYTHONUTF8": "1"},
        )
        self._started_by_us = True
        deadline = time.time() + 180
        while time.time() < deadline:
            if self._proc.returncode is not None:
                raise ConfuciusError(f"server_local.py 退出，代码 {self._proc.returncode}")
            status = await health(settings, timeout=2.0)
            if status["ok"]:
                notes.append(status["detail"])
                return notes
            await asyncio.sleep(1.0)
        await self.stop()
        raise ConfuciusError("等待 Confucius4 /health 超时（180s）。请检查模型是否已下载、显存是否够用。")

    async def stop(self) -> str:
        if not self._started_by_us or self._proc is None:
            return "Confucius4 不是本程序启动的，请自行停止 server_local.py 以释放显存"
        proc = self._proc
        self._proc = None
        self._started_by_us = False
        if proc.returncode is not None:
            return "Confucius4 子进程已退出"
        try:
            if os.name == "nt":
                proc.terminate()
            else:
                proc.send_signal(signal.SIGTERM)
            try:
                await asyncio.wait_for(proc.wait(), timeout=15)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
        except ProcessLookupError:
            pass
        return "已结束 Confucius4 子进程，显存应已释放"


MANAGER = ServerManager()


def _host_port(url: str) -> tuple[str, int]:
    from urllib.parse import urlparse

    parsed = urlparse(url if "://" in url else "http://" + url)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or 7860
    return host, port


def _port_open(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=0.4):
            return True
    except OSError:
        return False
