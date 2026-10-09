"""Process-wide serial lock for heavy GPU / RAM work.

One local machine often cannot hold Ollama, ComfyUI (image / rewrite / video)
and TTS in VRAM at once. When *low-resource serial mode* is on (the default),
every heavy stage must take this lock. Only one holder runs; everyone else
waits in a visible queue. Switching family triggers an unload of the previous
family's models before the next job starts.

Families (batched by pipeline stage to minimise swaps)::

    llm      → stage 1 (Ollama)
    image    → stage 2 + 3 (ComfyUI Qwen-Image)
    rewrite  → stage 3.5 (ComfyUI H3 rewriter)
    video    → stage 4 (ComfyUI H3 / Wan)
    tts      → stage 4.5 (CosyVoice / local)

FFmpeg stitch is CPU-bound and does not take the lock.
"""

from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Callable

import httpx

log = logging.getLogger("novel2video.resources")

FAMILY_LABELS = {
    "llm": "Ollama 语言模型",
    "image": "ComfyUI 出图",
    "rewrite": "ComfyUI H3 改写",
    "video": "ComfyUI 视频",
    "tts": "配音 TTS",
}

# ComfyUI-backed families share one process; free VRAM whenever we leave any of them
# for a different family, and also when hopping image → rewrite → video.
COMFY_FAMILIES = frozenset({"image", "rewrite", "video"})


@dataclass
class Ticket:
    id: str
    project_id: str
    family: str
    stage: str
    label: str
    enqueued_at: float = field(default_factory=time.time)


NotifyFn = Callable[[dict], None]


class ResourceGate:
    """Single-worker queue. Safe to share across all Engine runs in this process."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._cond = asyncio.Condition()
        self._holder: Ticket | None = None
        self._queue: list[Ticket] = []
        self._last_family: str | None = None
        self.last_unload_notes: list[str] = []
        self.confucius_status: dict[str, Any] | None = None
        self._seq = 0
        self._listeners: list[NotifyFn] = []

    def subscribe(self, callback: NotifyFn) -> None:
        self._listeners.append(callback)

    def unsubscribe(self, callback: NotifyFn) -> None:
        try:
            self._listeners.remove(callback)
        except ValueError:
            pass

    def _notify(self) -> None:
        snap = self.snapshot()
        for callback in list(self._listeners):
            try:
                callback(snap)
            except Exception:  # noqa: BLE001 — UI fan-out must not break the gate
                log.exception("resource listener failed")

    def reset(self) -> None:
        """Drop holder/queue state (tests). Listeners are kept."""
        self._holder = None
        self._queue.clear()
        self._last_family = None
        self.last_unload_notes = []
        self._notify()

    def snapshot(self) -> dict[str, Any]:
        holder = None
        if self._holder:
            holder = {
                "project_id": self._holder.project_id,
                "family": self._holder.family,
                "family_label": FAMILY_LABELS.get(self._holder.family, self._holder.family),
                "stage": self._holder.stage,
                "label": self._holder.label,
            }
        return {
            "holder": holder,
            "queue": [
                {
                    "project_id": item.project_id,
                    "family": item.family,
                    "family_label": FAMILY_LABELS.get(item.family, item.family),
                    "stage": item.stage,
                    "label": item.label,
                    "waited_sec": round(time.time() - item.enqueued_at, 1),
                }
                for item in self._queue
            ],
            "last_family": self._last_family,
            "last_family_label": FAMILY_LABELS.get(self._last_family or "", "") if self._last_family else "",
            "confucius": getattr(self, "confucius_status", None),
        }

    @asynccontextmanager
    async def hold(
        self,
        *,
        family: str,
        project_id: str,
        stage: str,
        label: str,
        settings: dict,
        enabled: bool | None = None,
    ) -> AsyncIterator[Ticket | None]:
        """Acquire the GPU queue for one stage. No-op when serial mode is off."""
        serial = settings.get("serial_mode") if enabled is None else enabled
        if serial is None:
            serial = True
        if not serial:
            yield None
            return

        self._seq += 1
        ticket = Ticket(
            id=f"t{self._seq}",
            project_id=project_id,
            family=family,
            stage=stage,
            label=label or FAMILY_LABELS.get(family, family),
        )
        self._queue.append(ticket)
        self._notify()
        await self._lock.acquire()
        try:
            if ticket in self._queue:
                self._queue.remove(ticket)
            previous = self._last_family
            self._holder = ticket
            self._notify()
            self.last_unload_notes = []
            if previous and previous != family:
                self.last_unload_notes = await unload_family(previous, settings)
            self._last_family = family
            yield ticket
        finally:
            self._holder = None
            self._lock.release()
            self._notify()


GATE = ResourceGate()


async def unload_family(family: str, settings: dict) -> list[str]:
    """Best-effort memory release before another family takes the GPU."""
    notes: list[str] = []
    if settings.get("demo_mode"):
        notes.append(f"演示模式：跳过卸载 {FAMILY_LABELS.get(family, family)}")
        return notes
    try:
        if family == "llm":
            notes.extend(await unload_ollama(settings.get("ollama_url") or ""))
        elif family in COMFY_FAMILIES:
            notes.extend(await free_comfy(settings.get("comfy_url") or ""))
        elif family == "tts":
            notes.extend(await release_tts(settings))
    except Exception as exc:  # noqa: BLE001 — unload must never fail the next job
        notes.append(f"卸载 {FAMILY_LABELS.get(family, family)} 时出错：{exc}")
        log.warning("unload %s failed: %s", family, exc)
    return notes


async def unload_ollama(base_url: str) -> list[str]:
    """Ask Ollama to drop loaded weights (keep_alive=0)."""
    if not base_url:
        return ["Ollama 地址为空，跳过卸载"]
    url = base_url.rstrip("/")
    notes: list[str] = []
    models: list[str] = []
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            response = await client.get(url + "/api/ps")
            if response.status_code < 400:
                payload = response.json() or {}
                for item in payload.get("models") or []:
                    name = item.get("name") or item.get("model")
                    if name:
                        models.append(name)
    except httpx.HTTPError:
        models = []
    if not models:
        # Still poke generate with keep_alive 0 using an empty model list fallback —
        # /api/ps may be older; try tags and unload the preferred one is too aggressive.
        notes.append("Ollama 没有驻留模型，或 /api/ps 不可用")
        return notes
    async with httpx.AsyncClient(timeout=60.0) as client:
        for name in models:
            try:
                await client.post(
                    url + "/api/generate",
                    json={"model": name, "prompt": "", "keep_alive": 0, "stream": False},
                )
                notes.append(f"已卸载 Ollama 模型 {name}")
            except httpx.HTTPError as exc:
                notes.append(f"卸载 {name} 失败：{exc}")
    return notes


async def free_comfy(base_url: str) -> list[str]:
    """POST /free with unload_models + free_memory (ComfyUI ≥ 0.2)."""
    if not base_url:
        return ["ComfyUI 地址为空，跳过释放"]
    url = base_url.rstrip("/")
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                url + "/free",
                json={"unload_models": True, "free_memory": True},
            )
            if response.status_code >= 400:
                return [f"ComfyUI /free 返回 {response.status_code}"]
            return ["已请求 ComfyUI 卸载模型并释放显存"]
    except httpx.HTTPError as exc:
        return [f"ComfyUI /free 失败：{exc}"]


async def release_tts(settings: dict) -> list[str]:
    from app.tts import get_provider

    provider = get_provider(settings.get("tts_provider"))
    release = getattr(provider, "release", None)
    if release is None:
        return [f"配音提供方 {provider.info.id} 不支持显式卸载"]
    try:
        detail = await release(settings)
        return [detail or f"已释放配音提供方 {provider.info.id}"]
    except Exception as exc:  # noqa: BLE001
        return [f"释放配音失败：{exc}"]
