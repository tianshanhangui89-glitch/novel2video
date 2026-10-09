"""ComfyUI HTTP client: upload, queue, poll history, optional websocket progress."""

from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path
from typing import Any, Callable

import httpx

try:
    import websockets
except ImportError:  # pragma: no cover
    websockets = None


class ComfyError(RuntimeError):
    pass


LogFn = Callable[[str], None]


def _ws_url(base_url: str, client_id: str) -> str:
    http = base_url.rstrip("/")
    if http.startswith("https://"):
        return "wss://" + http[len("https://") :] + f"/ws?clientId={client_id}"
    if http.startswith("http://"):
        return "ws://" + http[len("http://") :] + f"/ws?clientId={client_id}"
    return f"ws://{http}/ws?clientId={client_id}"


def format_node_errors(node_errors: dict) -> str:
    parts = []
    for node_id, err in list(node_errors.items())[:8]:
        text = json.dumps(err, ensure_ascii=False)
        parts.append(f"节点 {node_id}: {text[:500]}")
    return "；".join(parts) or "未知节点错误"


def collect_files(outputs: dict) -> list[dict]:
    files: list[dict] = []
    for node_out in (outputs or {}).values():
        if not isinstance(node_out, dict):
            continue
        for value in node_out.values():
            if not isinstance(value, list):
                continue
            for item in value:
                if isinstance(item, dict) and item.get("filename"):
                    files.append(item)
    return files


def pick_file(files: list[dict], kind: str) -> dict | None:
    if not files:
        return None

    def ext(item: dict) -> str:
        return str(item.get("filename") or "").lower()

    if kind == "video":
        videos = [f for f in files if ext(f).endswith((".mp4", ".webm", ".mkv", ".mov", ".gif"))]
        return (videos or files)[-1]
    images = [f for f in files if ext(f).endswith((".png", ".jpg", ".jpeg", ".webp"))]
    return (images or files)[-1]


class ComfyClient:
    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")

    async def health(self) -> dict:
        try:
            async with httpx.AsyncClient(timeout=8) as client:
                response = await client.get(self.base_url + "/system_stats")
                response.raise_for_status()
                return response.json()
        except httpx.HTTPError as exc:
            raise ComfyError(f"无法连接 ComfyUI（{self.base_url}）：{exc}") from exc

    async def free(self, unload_models: bool = True, free_memory: bool = True) -> None:
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                response = await client.post(
                    self.base_url + "/free",
                    json={"unload_models": unload_models, "free_memory": free_memory},
                )
                response.raise_for_status()
        except httpx.HTTPError as exc:
            raise ComfyError(f"ComfyUI /free 失败：{exc}") from exc

    async def object_info(self) -> dict | None:
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                response = await client.get(self.base_url + "/object_info")
                response.raise_for_status()
                data = response.json()
                return data if isinstance(data, dict) else None
        except httpx.HTTPError:
            return None

    async def upload_image(self, path: Path) -> str:
        data = path.read_bytes()
        mime = "image/png"
        if path.suffix.lower() in {".jpg", ".jpeg"}:
            mime = "image/jpeg"
        elif path.suffix.lower() == ".webp":
            mime = "image/webp"
        try:
            async with httpx.AsyncClient(timeout=120) as client:
                response = await client.post(
                    self.base_url + "/upload/image",
                    data={"overwrite": "true", "type": "input"},
                    files={"image": (path.name, data, mime)},
                )
                response.raise_for_status()
                body = response.json()
        except httpx.HTTPError as exc:
            raise ComfyError(f"上传参考图失败 {path.name}：{exc}") from exc
        name = body.get("name")
        if not name:
            raise ComfyError(f"ComfyUI 上传没有返回文件名：{body}")
        sub = body.get("subfolder") or ""
        return f"{sub}/{name}" if sub else name

    async def queue_and_wait(
        self,
        prompt: dict,
        log: LogFn,
        timeout: float = 3600,
        should_stop: Callable[[], bool] | None = None,
    ) -> list[dict]:
        outputs = await self.queue_and_wait_outputs(prompt, log, timeout, should_stop)
        return collect_files(outputs)

    async def queue_and_wait_outputs(
        self,
        prompt: dict,
        log: LogFn,
        timeout: float = 3600,
        should_stop: Callable[[], bool] | None = None,
    ) -> dict:
        """Queue a prompt and return the ``outputs`` block of its history entry."""
        client_id = str(uuid.uuid4())
        try:
            async with httpx.AsyncClient(timeout=60) as client:
                response = await client.post(
                    self.base_url + "/prompt",
                    json={"prompt": prompt, "client_id": client_id},
                )
                if response.status_code >= 400:
                    detail = response.text[:1200]
                    raise ComfyError(f"ComfyUI 拒绝了工作流（HTTP {response.status_code}）：{detail}")
                body = response.json()
        except httpx.HTTPError as exc:
            raise ComfyError(f"提交 ComfyUI 失败：{exc}") from exc
        if body.get("node_errors"):
            raise ComfyError("ComfyUI 节点校验失败：" + format_node_errors(body["node_errors"]))
        prompt_id = body.get("prompt_id")
        if not prompt_id:
            raise ComfyError(f"ComfyUI 没有返回 prompt_id：{body}")
        log(f"已入队 ComfyUI prompt_id={prompt_id}")

        watcher = asyncio.create_task(self._watch(client_id, prompt_id, log))
        try:
            loop = asyncio.get_running_loop()
            deadline = loop.time() + timeout
            while loop.time() < deadline:
                if should_stop and should_stop():
                    raise ComfyError("已停止")
                entry = await self._history(prompt_id)
                if entry is not None:
                    status = entry.get("status") or {}
                    if status.get("status_str") == "error":
                        messages = status.get("messages") or status
                        raise ComfyError("ComfyUI 执行失败：" + json.dumps(messages, ensure_ascii=False)[:1200])
                    return entry.get("outputs") or {}
                await asyncio.sleep(1.5)
            raise ComfyError(f"等待 ComfyUI 超时（{int(timeout)} 秒）prompt_id={prompt_id}")
        finally:
            watcher.cancel()
            try:
                await watcher
            except asyncio.CancelledError:
                pass

    async def _history(self, prompt_id: str) -> dict | None:
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                response = await client.get(self.base_url + f"/history/{prompt_id}")
                response.raise_for_status()
                payload = response.json()
        except httpx.HTTPError as exc:
            raise ComfyError(f"读取 ComfyUI 历史失败：{exc}") from exc
        if isinstance(payload, dict) and prompt_id in payload:
            return payload[prompt_id]
        return None

    async def _watch(self, client_id: str, prompt_id: str, log: LogFn) -> None:
        if websockets is None:
            return
        url = _ws_url(self.base_url, client_id)
        try:
            async with websockets.connect(url, open_timeout=5, close_timeout=2) as ws:
                async for message in ws:
                    if isinstance(message, bytes):
                        continue
                    try:
                        event = json.loads(message)
                    except json.JSONDecodeError:
                        continue
                    data = event.get("data") or {}
                    event_prompt = data.get("prompt_id")
                    if event_prompt not in (None, prompt_id):
                        continue
                    kind = event.get("type")
                    if kind == "progress":
                        value = int(data.get("value") or 0)
                        maximum = int(data.get("max") or 0)
                        if maximum and (value == 1 or value == maximum or value % 5 == 0):
                            log(f"采样进度 {value}/{maximum}")
                    elif kind == "executing" and data.get("node"):
                        log(f"正在执行节点 {data.get('node')}")
                    elif kind == "execution_error":
                        log("ComfyUI 报告执行错误，等待历史记录确认")
                        return
                    elif kind == "execution_success" and event_prompt == prompt_id:
                        return
        except Exception:
            return

    async def download(self, item: dict, dest: Path) -> None:
        params = {
            "filename": item["filename"],
            "subfolder": item.get("subfolder") or "",
            "type": item.get("type") or "output",
        }
        try:
            async with httpx.AsyncClient(timeout=180) as client:
                response = await client.get(self.base_url + "/view", params=params)
                response.raise_for_status()
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(response.content)
        except httpx.HTTPError as exc:
            raise ComfyError(f"下载 ComfyUI 产物失败 {item.get('filename')}：{exc}") from exc
