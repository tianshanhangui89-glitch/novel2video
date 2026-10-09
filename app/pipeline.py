"""Stage runner: storyboard, ComfyUI images and videos, FFmpeg stitch."""

from __future__ import annotations

import asyncio
import json
import logging
import random
import shutil
from pathlib import Path

from app.comfy import ComfyClient, ComfyError, pick_file
from app.convert import build_prompt_file, dimensions, load_workflow
from app.rewrite import (
    H3_WORKFLOWS,
    build_rewrite_prompt,
    demo_rewrite,
    missing_nodes,
    pick_text,
    split_fields,
    uses_image,
)
from app.rewrite import variant as rewrite_variant
from app.demo_media import write_card, write_clip
from app.ollama import OllamaError, chat_json, choose_model, list_models
from app.store import STAGE_IDS, VIDEO_WORKFLOWS, Store, now_iso
from app.storyboard import StoryboardError, mock_storyboard, normalize_storyboard, parse_json_content, system_prompt
from app.stitch import StitchError, stitch

log = logging.getLogger("novel2video")


class Cancelled(RuntimeError):
    pass


class Engine:
    def __init__(self, store: Store):
        self.store = store
        self.tasks: dict[str, asyncio.Task] = {}
        self.gates: dict[str, asyncio.Event] = {}
        self.cancel_flags: dict[str, bool] = {}
        self.subscribers: dict[str, set[asyncio.Queue]] = {}

    def subscribe(self, project_id: str) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue()
        self.subscribers.setdefault(project_id, set()).add(queue)
        return queue

    def unsubscribe(self, project_id: str, queue: asyncio.Queue) -> None:
        self.subscribers.get(project_id, set()).discard(queue)

    def publish(self, project: dict) -> None:
        self.store.save_project(project)
        payload = {"type": "snapshot", "project": project}
        for queue in list(self.subscribers.get(project["id"], set())):
            try:
                queue.put_nowait(payload)
            except asyncio.QueueFull:
                pass

    def log(self, project: dict, message: str, level: str = "info") -> None:
        project.setdefault("logs", []).append(
            {"ts": now_iso(), "level": level, "message": message}
        )
        project["logs"] = project["logs"][-400:]
        logging.getLogger("novel2video").log(getattr(logging, level.upper(), logging.INFO), "%s %s", project["id"], message)
        self.publish(project)

    def set_stage(self, project: dict, stage: str, status: str, message: str = "") -> None:
        project["stages"][stage] = {"status": status, "message": message}
        self.publish(project)

    def _check(self, project_id: str) -> None:
        if self.cancel_flags.get(project_id):
            raise Cancelled("已停止")

    async def start(self, project_id: str, from_stage: str = "stage0") -> dict:
        existing = self.tasks.get(project_id)
        if existing and not existing.done():
            self.cancel_flags[project_id] = True
            gate = self.gates.get(project_id)
            if gate:
                gate.set()
            try:
                await existing
            except (Cancelled, Exception):
                pass
        self.cancel_flags[project_id] = False
        if from_stage not in STAGE_IDS:
            from_stage = "stage0"
        self._reset_from(project_id, from_stage)
        self.tasks[project_id] = asyncio.create_task(self._run(project_id, from_stage))
        return self.store.load_project(project_id)

    def _reset_from(self, project_id: str, from_stage: str) -> None:
        project = self.store.load_project(project_id)
        start = STAGE_IDS.index(from_stage)
        for stage in STAGE_IDS[start:]:
            project["stages"][stage] = {"status": "pending", "message": ""}
        if start <= STAGE_IDS.index("stage1"):
            project["storyboard"] = None
            project["final_video"] = ""
            media = self.store.media_dir(project_id)
            if media.exists():
                shutil.rmtree(media)
        if start <= STAGE_IDS.index("stage3r") and project.get("storyboard"):
            for shot in project["storyboard"].get("shots") or []:
                if shot.get("h3_prompt"):
                    self._drop_video(project, shot)
                for key in ("h3_prompt", "h3_prompt_original", "h3_prompt_source", "h3_prompt_note"):
                    shot.pop(key, None)
        if start <= STAGE_IDS.index("stage5"):
            project["final_video"] = ""
        project["status"] = "running"
        project["error"] = ""
        project["current_label"] = ""
        self.store.save_project(project)

    async def continue_run(
        self,
        project_id: str,
        storyboard: dict | None = None,
        prompts: dict[str, str] | None = None,
    ) -> dict:
        project = self.store.load_project(project_id)
        resume_from = "stage4" if project.get("status") == "awaiting_prompt_review" else "stage2"
        if prompts and project.get("storyboard"):
            edited = 0
            for shot in project["storyboard"].get("shots") or []:
                if shot.get("shot_id") not in prompts:
                    continue
                text = str(prompts[shot["shot_id"]] or "").strip()
                if not text:
                    text = shot.get("h3_prompt_original") or self._motion_prompt(project, shot)
                    shot["h3_prompt_source"] = "fallback"
                elif text != (shot.get("h3_prompt") or "").strip():
                    shot["h3_prompt_source"] = "manual"
                    edited += 1
                if text != (shot.get("h3_prompt") or "").strip():
                    self._drop_video(project, shot)
                shot["h3_prompt"] = text
                shot["h3_prompt_original"] = shot.get("h3_prompt_original") or self._motion_prompt(project, shot)
            self.store.save_project(project)
            if edited:
                self.log(self.store.load_project(project_id), f"手动改了 {edited} 个镜头的 H3 提示词")
        if storyboard:
            project["storyboard"] = normalize_storyboard(
                storyboard,
                project_id=project["id"],
                title=project["name"],
                style=storyboard.get("style") or project["style"],
                aspect=storyboard.get("aspect") or project["aspect"],
                source_name=(project.get("storyboard") or {}).get("source", {}).get("path", ""),
            )
            project["style"] = project["storyboard"]["style"]
            project["aspect"] = project["storyboard"]["aspect"]
            self.store.save_project(project)
        gate = self.gates.get(project_id)
        task = self.tasks.get(project_id)
        if gate and task and not task.done():
            what = "H3 提示词已确认，开始生成视频" if resume_from == "stage4" else "分镜已确认，继续后续阶段"
            self.log(self.store.load_project(project_id), what)
            gate.set()
            return self.store.load_project(project_id)
        return await self.start(project_id, from_stage=resume_from)

    async def cancel(self, project_id: str) -> dict:
        self.cancel_flags[project_id] = True
        gate = self.gates.get(project_id)
        if gate:
            gate.set()
        task = self.tasks.get(project_id)
        if task and not task.done():
            try:
                await asyncio.wait_for(task, timeout=5)
            except (asyncio.TimeoutError, Cancelled, Exception):
                pass
        else:
            project = self.store.load_project(project_id)
            project["status"] = "cancelled"
            project["current_label"] = "已停止"
            self.log(project, "已停止", "warning")
        return self.store.load_project(project_id)

    async def _run(self, project_id: str, from_stage: str) -> None:
        project = self.store.load_project(project_id)
        try:
            start = STAGE_IDS.index(from_stage)

            def due(stage: str) -> bool:
                return start <= STAGE_IDS.index(stage)

            if due("stage0"):
                await self._stage0(project_id)
            if due("stage1"):
                await self._stage1(project_id)
                await self._wait_approval(project_id)
            if due("stage2"):
                await self._stage2(project_id)
            if due("stage3"):
                await self._stage3(project_id)
            if due("stage3r"):
                rewrote = await self._stage_rewrite(project_id)
                if rewrote and self.store.load_settings().get("rewrite_review"):
                    await self._wait_gate(
                        project_id,
                        "awaiting_prompt_review",
                        "等待确认 H3 提示词",
                        "H3 提示词已改写。可以对照原文逐镜修改，确认后才开始生成视频。",
                    )
            if due("stage4"):
                await self._stage4(project_id)
            if due("stage5"):
                await self._stage5(project_id)
            await self._stage6(project_id)
            project = self.store.load_project(project_id)
            project["status"] = "done"
            project["current_label"] = "成片已就绪"
            self.log(project, "流水线完成")
        except Cancelled:
            project = self.store.load_project(project_id)
            project["status"] = "cancelled"
            project["current_label"] = "已停止"
            self.log(project, "流水线已停止", "warning")
        except Exception as exc:
            project = self.store.load_project(project_id)
            project["status"] = "failed"
            project["error"] = str(exc)
            project["current_label"] = "失败"
            failed = next((s for s in STAGE_IDS if project["stages"].get(s, {}).get("status") == "running"), None)
            if failed:
                self.set_stage(project, failed, "failed", str(exc)[:240])
            self.log(project, str(exc), "error")
            log.exception("pipeline failed")

    async def _wait_approval(self, project_id: str) -> None:
        await self._wait_gate(
            project_id,
            "awaiting_approval",
            "等待确认分镜",
            "分镜已生成。可在表格里修改角色、场景、道具和镜头，然后确认继续。",
        )

    async def _wait_gate(self, project_id: str, status: str, label: str, message: str) -> None:
        gate = asyncio.Event()
        self.gates[project_id] = gate
        project = self.store.load_project(project_id)
        project["status"] = status
        project["current_label"] = label
        self.log(project, message)
        while not gate.is_set():
            self._check(project_id)
            try:
                await asyncio.wait_for(gate.wait(), timeout=0.4)
            except asyncio.TimeoutError:
                continue
        self._check(project_id)
        project = self.store.load_project(project_id)
        project["status"] = "running"
        self.publish(project)

    async def _stage0(self, project_id: str) -> None:
        self._check(project_id)
        project = self.store.load_project(project_id)
        self.set_stage(project, "stage0", "running", "整理文稿")
        script = (project.get("script") or "").strip()
        if not script:
            raise StoryboardError("文稿是空的")
        project = self.store.load_project(project_id)
        self.set_stage(project, "stage0", "done", f"{len(script)} 字")
        self.log(project, f"已接收文稿，{len(script)} 字")

    async def _stage1(self, project_id: str) -> None:
        self._check(project_id)
        project = self.store.load_project(project_id)
        self.set_stage(project, "stage1", "running", "请求语言模型")
        settings = self.store.load_settings()
        script = project["script"]
        style = project["style"]
        aspect = project["aspect"]
        if settings.get("mock_llm") == "always":
            board = mock_storyboard(script, project_id=project_id, title=project["name"], style=style, aspect=aspect)
            project = self.store.load_project(project_id)
            self.log(project, f"按设置使用本地分镜：{len(board['shots'])} 个镜头")
            project = self.store.load_project(project_id)
            project["storyboard"] = board
            self.store.save_project(project)
            self.set_stage(project, "stage1", "done", f"{len(board['shots'])} 镜")
            return
        try:
            board = await self._storyboard_from_llm(project, settings, script, style, aspect)
            project = self.store.load_project(project_id)
            self.log(project, f"分镜完成：{len(board['characters'])} 个角色，{len(board['scenes'])} 个场景，{len(board['props'])} 个道具，{len(board['shots'])} 个镜头")
        except Exception as exc:
            if settings.get("mock_llm") == "never":
                raise
            if settings.get("mock_llm") == "always" or isinstance(exc, (OllamaError, StoryboardError, OSError)):
                project = self.store.load_project(project_id)
                self.log(project, f"语言模型不可用，改用本地分镜。原因：{exc}", "warning")
                board = mock_storyboard(
                    script,
                    project_id=project_id,
                    title=project["name"],
                    style=style,
                    aspect=aspect,
                )
                project = self.store.load_project(project_id)
                self.log(project, f"本地分镜：{len(board['shots'])} 个镜头")
            else:
                raise
        project = self.store.load_project(project_id)
        project["storyboard"] = board
        self.store.save_project(project)
        self.set_stage(project, "stage1", "done", f"{len(board['shots'])} 镜")

    async def _storyboard_from_llm(self, project, settings, script, style, aspect) -> dict:
        if settings.get("mock_llm") == "always":
            raise OllamaError("设置要求使用本地分镜")
        models = await list_models(settings["ollama_url"])
        model = choose_model(models, settings.get("llm_model") or "")
        self.log(project, f"使用 Ollama 模型 {model}")
        user = (
            f"全局画风 style: {style}\n"
            f"画幅 aspect: {aspect}\n"
            "请同时给出 props（关键道具，可为空数组）。只输出 JSON。\n\n"
            f"---章节正文---\n{script}"
        )
        raw = await chat_json(settings["ollama_url"], model, system_prompt(), user)
        data = parse_json_content(raw)
        return normalize_storyboard(
            data,
            project_id=project["id"],
            title=project["name"],
            style=style,
            aspect=aspect,
            source_name=project["name"],
        )

    async def _stage2(self, project_id: str) -> None:
        project = self.store.load_project(project_id)
        board = project.get("storyboard")
        if not board:
            raise StoryboardError("没有分镜，无法出参考图")
        self.set_stage(project, "stage2", "running", "角色 / 场景 / 道具")
        jobs = []
        for item in board.get("characters") or []:
            jobs.append(("characters", item, "角色定妆", self._character_prompt(project, item)))
        for item in board.get("scenes") or []:
            jobs.append(("scenes", item, "场景", self._scene_prompt(project, item)))
        for item in board.get("props") or []:
            jobs.append(("props", item, "道具", self._prop_prompt(project, item)))
        if not jobs:
            project = self.store.load_project(project_id)
            self.log(project, "分镜没有角色、场景或道具，跳过参考图", "warning")
            self.set_stage(project, "stage2", "done", "无参考项")
            return
        for folder, item, kind, prompt in jobs:
            self._check(project_id)
            rel = f"{folder}/{item['id']}.png"
            if self._exists(project_id, rel):
                project = self.store.load_project(project_id)
                self._assign_ref(project, folder, item["id"], rel)
                self.publish(project)
                continue
            project = self.store.load_project(project_id)
            project["current_label"] = f"{kind} · {item.get('name') or item['id']}"
            self.log(project, f"参考图：{item.get('name') or item['id']}")
            await self._render_image(
                project_id,
                workflow_name=project["image_workflow"],
                prompt=prompt,
                images=[],
                dest_rel=rel,
                prefix=f"n2v/{project_id}/ref_{item['id']}",
                force_size=True,
                kind="image",
            )
            project = self.store.load_project(project_id)
            self._assign_ref(project, folder, item["id"], rel)
            self.publish(project)
        project = self.store.load_project(project_id)
        self.set_stage(project, "stage2", "done", f"{len(jobs)} 张")

    async def _stage3(self, project_id: str) -> None:
        project = self.store.load_project(project_id)
        board = project["storyboard"]
        shots = board.get("shots") or []
        self.set_stage(project, "stage3", "running", f"0/{len(shots)}")
        for index, shot in enumerate(shots, start=1):
            self._check(project_id)
            rel = shot.get("keyframe") or f"shots/{shot['shot_id']}_key.png"
            project = self.store.load_project(project_id)
            project["current_label"] = f"镜头 {shot['shot_id']} · 关键帧"
            self.set_stage(project, "stage3", "running", f"{index}/{len(shots)}")
            if self._exists(project_id, rel):
                self.log(project, f"镜头 {shot['shot_id']} 已有关键帧，跳过")
                self._assign_shot(project, shot["shot_id"], keyframe=rel, status="keyframe")
                self.publish(project)
                continue
            refs = self._shot_refs(project, shot)
            prompt = self._keyframe_prompt(project, shot)
            workflow = project["edit_workflow"] if refs else project["image_workflow"]
            if not refs:
                self.log(project, f"镜头 {shot['shot_id']} 没有参考图，关键帧改走文生图")
            else:
                self.log(project, f"镜头 {shot['shot_id']} 关键帧，参考图 {len(refs)} 张")
            await self._render_image(
                project_id,
                workflow_name=workflow,
                prompt=prompt,
                images=refs,
                dest_rel=rel,
                prefix=f"n2v/{project_id}/shot_{shot['shot_id']}_key",
                force_size=not bool(refs),
                kind="image",
            )
            project = self.store.load_project(project_id)
            self._assign_shot(project, shot["shot_id"], keyframe=rel, status="keyframe")
            self.publish(project)
        project = self.store.load_project(project_id)
        self.set_stage(project, "stage3", "done", f"{len(shots)} 张")

    async def _stage_rewrite(self, project_id: str) -> bool:
        """Rewrite each shot's H3 prompt. Returns True when prompts were produced for review."""
        project = self.store.load_project(project_id)
        settings = self.store.load_settings()
        shots = (project.get("storyboard") or {}).get("shots") or []
        if not settings.get("rewrite_enabled"):
            self.set_stage(project, "stage3r", "done", "未启用")
            return False
        workflow_id = project.get("video_workflow") or "h3_i2v"
        if workflow_id not in H3_WORKFLOWS:
            self.log(project, "视频分支不是 MiniMax H3，跳过 H3 提示词改写")
            self.set_stage(project, "stage3r", "done", "非 H3 分支，跳过")
            return False
        spec = rewrite_variant(settings.get("rewrite_variant"))
        with_image = uses_image(spec, workflow_id)
        demo = bool(settings.get("demo_mode"))
        self.set_stage(project, "stage3r", "running", spec["label"])
        if workflow_id == "h3_r2v" and spec["uses_image"]:
            self.log(project, "参考生视频分支没有首帧，Rewriter 8B 按 T2VA 只读文字")

        info: dict | None = None
        blocked = ""
        if not demo:
            info = await ComfyClient(settings["comfy_url"]).object_info()
            missing = missing_nodes(info, spec, with_image)
            if info is None:
                blocked = "读不到 ComfyUI /object_info"
            elif missing:
                blocked = "ComfyUI 缺少节点 " + "、".join(missing) + "（请安装 minimax-h3-prompt-rewriter）"
            if blocked:
                project = self.store.load_project(project_id)
                self.log(project, f"{blocked}，所有镜头沿用原始提示词", "warning")

        rewritten = 0
        for index, shot in enumerate(shots, start=1):
            self._check(project_id)
            project = self.store.load_project(project_id)
            original = self._motion_prompt(project, shot)
            sid = shot["shot_id"]
            if shot.get("h3_prompt") and shot.get("h3_prompt_original") == original:
                self.log(project, f"镜头 {sid} 已有 H3 提示词，跳过")
                rewritten += shot.get("h3_prompt_source") not in {"fallback", ""}
                continue
            project["current_label"] = f"镜头 {sid} · H3 提示词"
            self.set_stage(project, "stage3r", "running", f"{index}/{len(shots)} · {spec['label']}")
            duration = float(shot.get("duration_sec") or 5)
            key_path = self.store.media_dir(project_id) / (shot.get("keyframe") or f"shots/{sid}_key.png")
            image = key_path if with_image and key_path.is_file() else None
            note = ""
            if demo:
                await asyncio.sleep(0.15)
                text, source = demo_rewrite(original, shot, project.get("aspect") or "16:9", duration, image is not None), "demo"
                self.log(self.store.load_project(project_id), f"演示模式模拟改写镜头 {sid}")
            elif blocked:
                text, source, note = original, "fallback", blocked
            else:
                try:
                    text = await self._comfy_rewrite(project_id, spec, shot, original, duration, image, info, settings)
                    source = "rewriter"
                except Cancelled:
                    raise
                except Exception as exc:
                    if self.cancel_flags.get(project_id):
                        raise Cancelled("已停止") from exc
                    text, source, note = original, "fallback", str(exc)[:300]
                    self.log(self.store.load_project(project_id), f"镜头 {sid} 改写失败，沿用原始提示词：{exc}", "warning")
            if source != "fallback":
                rewritten += 1
            project = self.store.load_project(project_id)
            for item in project["storyboard"].get("shots") or []:
                if item.get("shot_id") == sid:
                    item["h3_prompt_original"] = original
                    item["h3_prompt"] = text
                    item["h3_prompt_source"] = source
                    item["h3_prompt_note"] = note
            self.publish(project)
        project = self.store.load_project(project_id)
        self.set_stage(project, "stage3r", "done", f"{rewritten}/{len(shots)} 已改写")
        return rewritten > 0

    async def _comfy_rewrite(self, project_id, spec, shot, original, duration, image, info, settings) -> str:
        project = self.store.load_project(project_id)
        client = ComfyClient(settings["comfy_url"])
        uploaded = await client.upload_image(image) if image else None
        try:
            workflow = load_workflow(self.store.workflow_path(spec["file"]))
        except FileNotFoundError as exc:
            raise ComfyError(str(exc)) from exc
        graph, notes = build_rewrite_prompt(
            workflow,
            spec=spec,
            prompt=self._rewrite_input(project, shot, original),
            aspect=project.get("aspect") or "16:9",
            duration=duration,
            seed=random.randint(1, 2**31),
            image=uploaded,
            model=settings.get("rewrite_model") or "",
            adapter=(settings.get("rewrite_adapter") or "") if spec["uses_adapter"] else "",
            object_info=info,
        )
        for note in notes:
            self.log(self.store.load_project(project_id), note, "warning")
        mode = "I2VA，带关键帧" if uploaded else "T2VA"
        self.log(project, f"镜头 {shot['shot_id']} 提交 {spec['file']}（{mode}，{duration:g} 秒）")

        def logger(message: str) -> None:
            self.log(self.store.load_project(project_id), message)

        outputs = await client.queue_and_wait_outputs(
            graph,
            logger,
            should_stop=lambda: bool(self.cancel_flags.get(project_id)),
        )
        text = pick_text(outputs)
        if not text:
            raise ComfyError("改写节点没有返回文本")
        if not any(split_fields(text).values()):
            self.log(self.store.load_project(project_id), f"镜头 {shot['shot_id']} 的改写没有三段标签，仍然使用", "warning")
        return text

    def _rewrite_input(self, project: dict, shot: dict, original: str) -> str:
        beats = [shot.get("camera"), shot.get("action")]
        context = "; ".join(str(b).strip() for b in beats if b and str(b).strip())
        return f"{original} Shot context: {context}." if context else original

    def _video_prompt(self, project: dict, shot: dict) -> str:
        original = self._motion_prompt(project, shot)
        settings = self.store.load_settings()
        if (
            settings.get("rewrite_enabled")
            and project.get("video_workflow") in H3_WORKFLOWS
            and shot.get("h3_prompt")
            and shot.get("h3_prompt_original") == original
        ):
            return shot["h3_prompt"]
        return original

    async def _stage4(self, project_id: str) -> None:
        project = self.store.load_project(project_id)
        shots = (project.get("storyboard") or {}).get("shots") or []
        spec = VIDEO_WORKFLOWS[project.get("video_workflow") or "h3_i2v"]
        self.set_stage(project, "stage4", "running", spec["label"])
        if project.get("video_workflow") == "wan22":
            self.log(project, "视频分支是 Wan2.2，使用工作流 03c（低显存）。")
        for index, shot in enumerate(shots, start=1):
            self._check(project_id)
            rel = shot.get("video") or f"shots/{shot['shot_id']}.mp4"
            project = self.store.load_project(project_id)
            project["current_label"] = f"镜头 {shot['shot_id']} · 视频"
            self.set_stage(project, "stage4", "running", f"{index}/{len(shots)} · {spec['label']}")
            if self._exists(project_id, rel):
                self.log(project, f"镜头 {shot['shot_id']} 已有视频，跳过")
                self._assign_shot(project, shot["shot_id"], video=rel, status="video")
                self.publish(project)
                continue
            key_rel = shot.get("keyframe") or f"shots/{shot['shot_id']}_key.png"
            key_path = self.store.media_dir(project_id) / key_rel
            if not key_path.is_file():
                raise ComfyError(f"镜头 {shot['shot_id']} 没有关键帧，无法生成视频")
            if project.get("video_workflow") == "h3_r2v":
                images = self._shot_refs(project, shot)[:2] + [key_path]
            else:
                images = [key_path]
            self.log(project, f"镜头 {shot['shot_id']} 视频，{spec['label']}，{shot.get('duration_sec')} 秒")
            await self._render_video(
                project_id,
                workflow_name=spec["file"],
                prompt=self._video_prompt(project, shot),
                images=images,
                dest_rel=rel,
                prefix=f"n2v/{project_id}/shot_{shot['shot_id']}",
                duration=float(shot.get("duration_sec") or 5),
                video_kind="wan" if project.get("video_workflow") == "wan22" else "h3",
            )
            project = self.store.load_project(project_id)
            self._assign_shot(project, shot["shot_id"], video=rel, status="video")
            self.publish(project)
        project = self.store.load_project(project_id)
        self.set_stage(project, "stage4", "done", f"{len(shots)} 条")

    async def _stage5(self, project_id: str) -> None:
        project = self.store.load_project(project_id)
        self.set_stage(project, "stage5", "running", "FFmpeg 拼接")
        project["current_label"] = "拼接成片"
        self.publish(project)
        shots = (project.get("storyboard") or {}).get("shots") or []
        paths = []
        for shot in sorted(shots, key=lambda item: item.get("shot_id") or ""):
            rel = shot.get("video")
            if not rel:
                continue
            path = self.store.media_dir(project_id) / rel
            if path.is_file():
                paths.append(path)
        if not paths:
            raise StitchError("没有镜头视频可拼接")
        settings = self.store.load_settings()
        demo = bool(settings.get("demo_mode"))
        if demo:
            width, height = _demo_size(project.get("aspect") or "16:9")
        else:
            kind = "wan" if project.get("video_workflow") == "wan22" else "h3"
            width, height = dimensions(project.get("aspect") or "16:9", kind)
        dest = self.store.media_dir(project_id) / "final" / "chapter.mp4"
        work = self.store.media_dir(project_id) / "final" / "norm"
        self.log(project, f"拼接 {len(paths)} 条镜头为 {width}×{height}")
        await asyncio.to_thread(stitch, paths, dest, width, height, work)
        project = self.store.load_project(project_id)
        project["final_video"] = "final/chapter.mp4"
        self.set_stage(project, "stage5", "done", dest.name)
        self.log(project, "成片已写出 final/chapter.mp4")

    async def _stage6(self, project_id: str) -> None:
        project = self.store.load_project(project_id)
        self.set_stage(project, "stage6", "running", "归档")
        final = self.store.media_dir(project_id) / (project.get("final_video") or "final/chapter.mp4")
        out_dir = self.store.output_dir() / project_id
        out_dir.mkdir(parents=True, exist_ok=True)
        if final.is_file():
            shutil.copy2(final, out_dir / "chapter.mp4")
        manifest = {
            "id": project["id"],
            "name": project["name"],
            "style": project["style"],
            "aspect": project["aspect"],
            "video_workflow": project["video_workflow"],
            "storyboard": project.get("storyboard"),
            "final_video": str(out_dir / "chapter.mp4"),
        }
        (out_dir / "project.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        project = self.store.load_project(project_id)
        self.set_stage(project, "stage6", "done", str(out_dir))
        self.log(project, f"已复制到 {out_dir}")

    async def _render_image(self, project_id, workflow_name, prompt, images, dest_rel, prefix, force_size, kind) -> None:
        settings = self.store.load_settings()
        dest = self.store.media_dir(project_id) / dest_rel
        project = self.store.load_project(project_id)
        if settings.get("demo_mode"):
            title = Path(dest_rel).stem
            await asyncio.to_thread(write_card, dest, title, prompt[:70], project.get("aspect") or "16:9", "参考" if "characters" in dest_rel or "scenes" in dest_rel or "props" in dest_rel else "关键帧")
            self.log(self.store.load_project(project_id), f"演示模式写出 {dest_rel}")
            return
        await self._comfy_render(
            project_id,
            workflow_name=workflow_name,
            prompt=prompt,
            images=images,
            dest=dest,
            prefix=prefix,
            force_size=force_size,
            kind=kind,
            media_kind="image",
            duration=None,
        )

    async def _render_video(self, project_id, workflow_name, prompt, images, dest_rel, prefix, duration, video_kind) -> None:
        settings = self.store.load_settings()
        dest = self.store.media_dir(project_id) / dest_rel
        project = self.store.load_project(project_id)
        if settings.get("demo_mode"):
            still = dest.with_suffix(".png")
            if not still.is_file():
                source = images[0] if images else None
                if source and Path(source).is_file():
                    still.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source, still)
                else:
                    await asyncio.to_thread(write_card, still, Path(dest_rel).stem, prompt[:70], project.get("aspect") or "16:9", "视频")
            await asyncio.to_thread(write_clip, still, dest, duration)
            self.log(self.store.load_project(project_id), f"演示模式写出 {dest_rel}（预览最长 2 秒）")
            return
        await self._comfy_render(
            project_id,
            workflow_name=workflow_name,
            prompt=prompt,
            images=images,
            dest=dest,
            prefix=prefix,
            force_size=True,
            kind=video_kind,
            media_kind="video",
            duration=duration,
        )

    async def _comfy_render(
        self,
        project_id,
        workflow_name,
        prompt,
        images,
        dest: Path,
        prefix: str,
        force_size: bool,
        kind: str,
        media_kind: str,
        duration: float | None,
    ) -> None:
        project = self.store.load_project(project_id)
        settings = self.store.load_settings()
        client = ComfyClient(settings["comfy_url"])
        uploaded: list[str] = []
        for image in images:
            path = Path(image)
            if not path.is_file():
                raise ComfyError(f"参考图不存在：{path}")
            uploaded.append(await client.upload_image(path))
        width, height = dimensions(project.get("aspect") or "16:9", kind)
        info = await client.object_info()
        try:
            workflow = self.store.workflow_path(workflow_name)
        except FileNotFoundError as exc:
            raise ComfyError(str(exc)) from exc
        graph = build_prompt_file(
            workflow,
            {
                "prompt": prompt,
                "images": uploaded,
                "filename_prefix": prefix,
                "seed": random.randint(1, 2**48),
                "width": width,
                "height": height,
                "force_size": force_size,
                "duration_sec": duration,
            },
            info,
        )
        self.log(project, f"提交 {workflow.name}，API 节点 {len(graph)} 个")

        def logger(message: str) -> None:
            current = self.store.load_project(project_id)
            self.log(current, message)

        try:
            files = await client.queue_and_wait(
                graph,
                logger,
                should_stop=lambda: bool(self.cancel_flags.get(project_id)),
            )
        except ComfyError as exc:
            if self.cancel_flags.get(project_id):
                raise Cancelled("已停止") from exc
            raise
        chosen = pick_file(files, media_kind)
        if not chosen:
            raise ComfyError("ComfyUI 完成了，但没有返回图片或视频文件")
        await client.download(chosen, dest)
        self.log(self.store.load_project(project_id), f"已保存 {dest.relative_to(self.store.media_dir(project_id))}")

    def _drop_video(self, project: dict, shot: dict) -> None:
        rel = shot.get("video") or f"shots/{shot.get('shot_id')}.mp4"
        path = self.store.media_dir(project["id"]) / rel
        if path.is_file():
            path.unlink()
        shot["video"] = ""
        if shot.get("status") == "video":
            shot["status"] = "keyframe"

    def _exists(self, project_id: str, rel: str) -> bool:
        return bool(rel) and (self.store.media_dir(project_id) / rel).is_file()

    def _assign_ref(self, project: dict, folder: str, item_id: str, rel: str) -> None:
        board = project["storyboard"]
        for item in board.get(folder) or []:
            if item.get("id") == item_id:
                item["ref_image"] = rel

    def _assign_shot(self, project: dict, shot_id: str, keyframe: str | None = None, video: str | None = None, status: str | None = None) -> None:
        for shot in project["storyboard"].get("shots") or []:
            if shot.get("shot_id") == shot_id:
                if keyframe:
                    shot["keyframe"] = keyframe
                if video:
                    shot["video"] = video
                if status:
                    shot["status"] = status
                shot["error"] = ""

    def _by_id(self, items: list[dict]) -> dict[str, dict]:
        return {item.get("id"): item for item in items if item.get("id")}

    def _shot_refs(self, project: dict, shot: dict) -> list[Path]:
        board = project["storyboard"]
        scenes = self._by_id(board.get("scenes") or [])
        characters = self._by_id(board.get("characters") or [])
        media = self.store.media_dir(project["id"])
        paths: list[Path] = []
        scene = scenes.get(shot.get("scene_id"))
        if scene and scene.get("ref_image"):
            paths.append(media / scene["ref_image"])
        for cid in shot.get("characters") or []:
            character = characters.get(cid)
            if character and character.get("ref_image"):
                paths.append(media / character["ref_image"])
        blob = " ".join(
            str(shot.get(key) or "")
            for key in ("action", "image_prompt", "dialogue", "motion_prompt")
        )
        for prop in board.get("props") or []:
            if prop.get("name") and prop["name"] in blob and prop.get("ref_image"):
                paths.append(media / prop["ref_image"])
        unique = []
        seen = set()
        for path in paths:
            if path in seen or not path.is_file():
                continue
            seen.add(path)
            unique.append(path)
        return unique[:10]

    def _character_prompt(self, project: dict, item: dict) -> str:
        look = item.get("ref_prompt") or item.get("appearance") or item.get("name")
        return (
            f"{project['style']}, character reference sheet, front view and three-quarter view, "
            f"neutral background, consistent costume and face, {look}"
        )

    def _scene_prompt(self, project: dict, item: dict) -> str:
        return (
            f"{project['style']}, environment concept art, no people, empty set, "
            f"cinematic wide shot, {item.get('visual') or item.get('name')}"
        )

    def _prop_prompt(self, project: dict, item: dict) -> str:
        return (
            f"{project['style']}, prop design, single object, isolated on a neutral background, "
            f"{item.get('ref_prompt') or item.get('visual') or item.get('name')}"
        )

    def _keyframe_prompt(self, project: dict, shot: dict) -> str:
        return (
            f"{project['style']}. {shot.get('image_prompt') or shot.get('action')}. "
            "Keep character identity from the reference images. "
            "image 1 is the scene or edit target, later images are character and prop references."
        )

    def _motion_prompt(self, project: dict, shot: dict) -> str:
        dialogue = (shot.get("dialogue") or "").strip()
        spoken = f' The character speaks in Chinese: "{dialogue}".' if dialogue else ""
        sfx = f" Sound: {shot['sfx']}." if shot.get("sfx") else ""
        return f"{project['style']}. {shot.get('motion_prompt') or 'slow cinematic motion'}.{spoken}{sfx}"


def _demo_size(aspect: str) -> tuple[int, int]:
    if aspect == "9:16":
        return 540, 960
    if aspect == "1:1":
        return 768, 768
    return 960, 540
