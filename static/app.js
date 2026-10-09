const STAGE_LABELS = {
  stage0: "接收文稿",
  stage1: "分镜结构化",
  stage2: "参考图",
  stage3: "镜头关键帧",
  stage4: "镜头视频",
  stage5: "拼接成片",
  stage6: "完成",
};

const STATUS_TEXT = {
  idle: "未开始",
  running: "进行中",
  awaiting_approval: "待确认",
  done: "已完成",
  failed: "失败",
  cancelled: "已停止",
  pending: "等待",
  keyframe: "有关键帧",
  video: "有视频",
};

const state = {
  view: "home",
  projectId: "",
  project: null,
  settings: null,
  projects: [],
  services: null,
  workflows: null,
  draft: null,
  editorKey: "",
  stream: null,
  poll: null,
};

const main = document.getElementById("main");

function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, (ch) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#39;",
  }[ch]));
}

async function api(path, options = {}) {
  const opts = { ...options };
  opts.headers = { ...(options.body ? { "Content-Type": "application/json" } : {}), ...(options.headers || {}) };
  const response = await fetch(path, opts);
  const text = await response.text();
  let data = null;
  if (text) {
    try { data = JSON.parse(text); } catch { data = { detail: text }; }
  }
  if (!response.ok) {
    const detail = data && data.detail;
    const message = typeof detail === "string"
      ? detail
      : Array.isArray(detail)
        ? detail.map((item) => item.msg || JSON.stringify(item)).join("；")
        : response.statusText;
    throw new Error(message || "请求失败");
  }
  return data;
}

function parseHash() {
  const parts = (location.hash.replace(/^#/, "") || "/").split("/").filter(Boolean);
  if (parts[0] === "run") return { view: "run", id: parts[1] || "" };
  if (parts[0] === "gallery") return { view: "gallery", id: parts[1] || "" };
  if (parts[0] === "settings") return { view: "settings", id: "" };
  return { view: "home", id: "" };
}

function paintNav() {
  document.querySelectorAll("nav a").forEach((link) => {
    link.classList.toggle("active", link.dataset.nav === state.view);
  });
}

function paintServices() {
  const host = document.getElementById("svc");
  const services = state.services;
  if (!services) {
    host.innerHTML = "<div><i class='dot'></i><span>正在检测本机服务</span></div>";
    return;
  }
  const row = (ok, name) => `<div><i class="dot ${ok ? "ok" : "bad"}"></i><span>${name}${ok ? " 就绪" : " 未连接"}</span></div>`;
  host.innerHTML = [
    row(services.ollama && services.ollama.ok, "Ollama"),
    row(services.comfy && services.comfy.ok, "ComfyUI"),
    row(services.ffmpeg && services.ffmpeg.ok, "FFmpeg"),
  ].join("");
}

function closeStream() {
  if (state.stream) {
    state.stream.close();
    state.stream = null;
  }
  if (state.poll) {
    clearInterval(state.poll);
    state.poll = null;
  }
}

function statusChip(status) {
  const kind = status === "running" ? "run" : status === "done" ? "done" : status === "failed" ? "bad" : status === "awaiting_approval" ? "wait" : "";
  return `<span class="chip ${kind}">${esc(STATUS_TEXT[status] || status || "")}</span>`;
}

function videoChoices() {
  return (state.workflows && state.workflows.video) || [
    { id: "h3_i2v", label: "MiniMax H3 图生视频" },
    { id: "h3_r2v", label: "MiniMax H3 参考生视频" },
    { id: "wan22", label: "Wan2.2 图生视频（低显存）" },
  ];
}

function videoNote(id) {
  const found = videoChoices().find((item) => item.id === id);
  if (!found) return "";
  if (id === "wan22") return "低显存走 Wan2.2，使用工作流 03c。分辨率低于 H3，角色一致性也弱一些。";
  return found.note || "";
}

async function renderHome() {
  state.editorKey = "";
  const settings = state.settings || {};
  const files = (state.workflows && state.workflows.files) || [];
  const projects = state.projects || [];
  const offline = state.services && !state.services.ollama.ok && !state.services.comfy.ok;
  main.innerHTML = `
    <header class="page-head">
      <div>
        <h1>把一章小说，做成一条片子</h1>
        <p class="lede">本地 Ollama 写分镜，ComfyUI 出参考图、关键帧和镜头视频，FFmpeg 接成片。每一步都在运行页里看得见。</p>
      </div>
    </header>
    ${offline ? `<div class="banner">Ollama 和 ComfyUI 都还没连上。可以先到设置里打开演示模式，把界面流程走通；模型就绪后再关掉。</div>` : ""}
    ${settings.demo_mode ? `<div class="banner">演示模式开着：不会调用 ComfyUI，画面是本地占位，用来看流水线。</div>` : ""}
    <form id="home-form" class="layout">
      <section class="card">
        <h2>章节</h2>
        <div class="drop" id="drop">
          <textarea class="script" name="script" placeholder="把小说或剧本贴在这里。也可以把 .md / .txt 拖进来。"></textarea>
          <div class="row" style="margin-top:10px">
            <button type="button" data-action="sample">载入示例章节</button>
            <label class="btn">上传文稿<input id="script-file" type="file" accept=".md,.txt,text/plain,text/markdown" hidden></label>
          </div>
          <p class="hint">示例是《客栈夜雨》，两镜，带林远、掌柜、客栈和油灯。</p>
        </div>
      </section>
      <aside class="card">
        <h2>这场怎么拍</h2>
        <label class="field">项目名称<input name="name" type="text" placeholder="客栈夜雨"></label>
        <label class="field">画风提示<input name="style" type="text" value="${esc(settings.style || "")}"></label>
        <div class="field">画幅
          <div class="seg" id="aspect">
            ${["16:9", "9:16", "1:1"].map((item) => `<button type="button" data-aspect="${item}" class="${(settings.aspect || "16:9") === item ? "on" : ""}">${item}</button>`).join("")}
          </div>
          <input type="hidden" name="aspect" value="${esc(settings.aspect || "16:9")}">
        </div>
        <label class="field">视频工作流
          <select name="video_workflow">
            ${videoChoices().map((item) => `<option value="${esc(item.id)}" ${item.id === (settings.video_workflow || "h3_i2v") ? "selected" : ""}>${esc(item.label)}</option>`).join("")}
          </select>
        </label>
        <p class="callout" id="vram-note">${esc(videoNote(settings.video_workflow || "h3_i2v"))}</p>
        <label class="field">参考图工作流
          <select name="image_workflow">
            ${files.map((name) => `<option ${name === (settings.image_workflow || "01_character_t2i.json") ? "selected" : ""}>${esc(name)}</option>`).join("")}
          </select>
        </label>
        <label class="field">关键帧工作流
          <select name="edit_workflow">
            ${files.map((name) => `<option ${name === (settings.edit_workflow || "02_shot_edit.json") ? "selected" : ""}>${esc(name)}</option>`).join("")}
          </select>
        </label>
        <button class="primary" type="submit" id="run-btn">开始生成</button>
        <p class="hint" id="home-error"></p>
      </aside>
    </form>
    <section style="margin-top:22px">
      <h2>最近的项目</h2>
      <div class="projects">
        ${projects.length ? projects.map((item) => `
          <a class="project-link" href="#/run/${esc(item.id)}">
            <span><strong>${esc(item.name)}</strong><span>${esc(item.shot_count || 0)} 镜 · ${esc(item.aspect || "")}</span></span>
            ${statusChip(item.status)}
          </a>`).join("") : `<p class="empty">还没有项目。贴上文稿，或载入示例章节。</p>`}
      </div>
    </section>`;
}

async function renderSettings() {
  state.editorKey = "";
  const settings = state.settings || {};
  const files = (state.workflows && state.workflows.files) || [];
  const models = (state.services && state.services.ollama && state.services.ollama.models) || [];
  main.innerHTML = `
    <header class="page-head">
      <div>
        <h1>设置</h1>
        <p class="lede">把地址指到本机的 Ollama 和 ComfyUI。检测按钮会直接告诉你连没连上。</p>
      </div>
      <button type="button" data-action="check">检测连接</button>
    </header>
    <form id="settings-form" class="card">
      <div class="layout">
        <div>
          <label class="field">Ollama 地址<input name="ollama_url" type="text" value="${esc(settings.ollama_url || "")}"></label>
          <label class="field">语言模型
            <input name="llm_model" type="text" list="model-list" value="${esc(settings.llm_model || "")}" placeholder="留空则优先选 Qwen">
            <datalist id="model-list">${models.map((name) => `<option value="${esc(name)}"></option>`).join("")}</datalist>
          </label>
          <label class="field">分镜来源
            <select name="mock_llm">
              <option value="auto" ${settings.mock_llm === "auto" ? "selected" : ""}>自动：Ollama 失败时用本地分镜</option>
              <option value="always" ${settings.mock_llm === "always" ? "selected" : ""}>始终本地分镜（不调用 Ollama）</option>
              <option value="never" ${settings.mock_llm === "never" ? "selected" : ""}>必须使用 Ollama</option>
            </select>
          </label>
          <label class="field">ComfyUI 地址<input name="comfy_url" type="text" value="${esc(settings.comfy_url || "")}"></label>
          <label class="field">输出目录<input name="output_dir" type="text" value="${esc(settings.output_dir || "output")}"></label>
        </div>
        <div>
          <label class="field">默认画风<input name="style" type="text" value="${esc(settings.style || "")}"></label>
          <label class="field">默认画幅
            <select name="aspect">
              ${["16:9", "9:16", "1:1"].map((item) => `<option ${settings.aspect === item ? "selected" : ""}>${item}</option>`).join("")}
            </select>
          </label>
          <label class="field">默认视频工作流
            <select name="video_workflow" id="settings-video">
              ${videoChoices().map((item) => `<option value="${esc(item.id)}" ${settings.video_workflow === item.id ? "selected" : ""}>${esc(item.label)}</option>`).join("")}
            </select>
          </label>
          <p class="callout" id="vram-note">${esc(videoNote(settings.video_workflow || "h3_i2v"))}</p>
          <label class="field">参考图工作流
            <select name="image_workflow">${files.map((name) => `<option ${name === settings.image_workflow ? "selected" : ""}>${esc(name)}</option>`).join("")}</select>
          </label>
          <label class="field">关键帧工作流
            <select name="edit_workflow">${files.map((name) => `<option ${name === settings.edit_workflow ? "selected" : ""}>${esc(name)}</option>`).join("")}</select>
          </label>
          <label class="field"><span>演示模式</span>
            <select name="demo_mode">
              <option value="false" ${settings.demo_mode ? "" : "selected"}>关闭，调用 ComfyUI</option>
              <option value="true" ${settings.demo_mode ? "selected" : ""}>开启，只生成占位画面</option>
            </select>
          </label>
        </div>
      </div>
      <div class="row">
        <button class="primary" type="submit">保存设置</button>
        <span class="hint" id="settings-msg"></span>
      </div>
      <div id="check-result"></div>
    </form>`;
  if (state.services) paintChecks(state.services);
}

function paintChecks(result) {
  const host = document.getElementById("check-result");
  if (!host || !result) return;
  const line = (item, title) => `<p class="check-line ${item && item.ok ? "ok" : "bad"}">${title}：${esc(item ? item.detail : "没有结果")}</p>`;
  host.innerHTML = line(result.ollama, "Ollama") + line(result.comfy, "ComfyUI") + line(result.ffmpeg, "FFmpeg");
}

function runSkeleton() {
  return `
    <header class="page-head">
      <div>
        <h1 id="run-title">运行</h1>
        <p class="lede" id="run-sub"></p>
      </div>
      <div class="row" id="run-actions"></div>
    </header>
    <div id="banners"></div>
    <div class="stages" id="stages"></div>
    <p class="now" id="now"></p>
    <div id="editor-host"></div>
    <h2>镜头</h2>
    <div class="shots" id="shots"></div>
    <details class="shot" id="script-fold">
      <summary>本章文稿</summary>
      <pre id="script-view" class="meta"></pre>
    </details>
    <div class="logs-title">日志</div>
    <div class="logs" id="logs"></div>`;
}

function paintRun(project, rebuildEditor) {
  if (!document.getElementById("stages")) {
    main.innerHTML = runSkeleton();
    rebuildEditor = true;
  }
  document.getElementById("run-title").textContent = project.name || "运行";
  document.getElementById("run-sub").textContent = `${project.aspect || ""} · ${videoLabel(project.video_workflow)}`;
  document.getElementById("run-actions").innerHTML = actionButtons(project);
  document.getElementById("banners").innerHTML = banners(project);
  const stageHost = document.getElementById("stages");
  const stageSig = JSON.stringify(project.stages || {});
  if (stageHost.dataset.sig !== stageSig) {
    stageHost.dataset.sig = stageSig;
    stageHost.innerHTML = Object.keys(STAGE_LABELS).map((key, index) => {
    const stage = (project.stages && project.stages[key]) || { status: "pending", message: "" };
    return `<article class="stage ${esc(stage.status)}">
      <div class="k"><span>阶段 ${index}</span><i class="lamp"></i></div>
      <strong>${esc(STAGE_LABELS[key])}</strong>
      <em>${esc(STATUS_TEXT[stage.status] || stage.status)}${stage.message ? " · " + esc(stage.message) : ""}</em>
    </article>`;
    }).join("");
  }
  document.getElementById("now").textContent = project.current_label || "";
  const script = document.getElementById("script-view");
  if (script.textContent !== (project.script || "")) script.textContent = project.script || "";
  paintShots(project);
  paintLogs(project);
  paintEditor(project, rebuildEditor);
}

function videoLabel(id) {
  const found = videoChoices().find((item) => item.id === id);
  return found ? found.label : (id || "");
}

function actionButtons(project) {
  const id = project.id;
  if (project.status === "running") return `<button class="danger" data-action="cancel" data-id="${esc(id)}">停止</button>`;
  if (project.status === "awaiting_approval") {
    return `<button class="primary" data-action="continue" data-id="${esc(id)}">确认并继续</button>
      <button data-action="rerun" data-id="${esc(id)}" data-from="stage1">重新分镜</button>`;
  }
  if (project.status === "failed") {
    return `<button class="primary" data-action="retry" data-id="${esc(id)}">重试失败阶段</button>
      <button data-action="rerun" data-id="${esc(id)}" data-from="stage0">从头重跑</button>
      <a class="btn" href="#/gallery/${esc(id)}">画廊</a>`;
  }
  if (project.status === "done") {
    return `<a class="btn primary" href="#/gallery/${esc(id)}">打开画廊</a>
      <button data-action="rerun" data-id="${esc(id)}" data-from="stage0">从头重跑</button>`;
  }
  return `<button class="primary" data-action="rerun" data-id="${esc(id)}" data-from="stage0">开始生成</button>`;
}

function banners(project) {
  const bits = [];
  if (state.settings && state.settings.demo_mode) bits.push(`<div class="banner">演示模式：参考图和镜头视频是本地占位，用来看进度。关掉后才会提交 ComfyUI。</div>`);
  if (project.status === "awaiting_approval") bits.push(`<div class="banner">分镜可以改。确认后才会出参考图、关键帧和视频。</div>`);
  if (project.status === "failed" && project.error) bits.push(`<div class="banner bad">${esc(project.error)}</div>`);
  if (project.video_workflow === "wan22") bits.push(`<div class="banner">这条片子走 Wan2.2 低显存分支，工作流是 03c。</div>`);
  return bits.join("");
}

function paintShots(project) {
  const host = document.getElementById("shots");
  const shots = (project.storyboard && project.storyboard.shots) || [];
  const signature = JSON.stringify(shots.map((shot) => [shot.shot_id, shot.status, shot.keyframe, shot.video, shot.action, shot.dialogue]));
  if (host.dataset.sig === signature) return;
  host.dataset.sig = signature;
  const open = new Set([...host.querySelectorAll("details[open]")].map((node) => node.dataset.id));
  if (!shots.length) {
    host.innerHTML = `<p class="empty">分镜还没出来。</p>`;
    return;
  }
  const current = project.current_label || "";
  host.innerHTML = shots.map((shot) => {
    const opened = open.has(shot.shot_id) || current.includes(shot.shot_id);
    return `<details class="shot" data-id="${esc(shot.shot_id)}" ${opened ? "open" : ""}>
      <summary>
        <span class="shot-no">${esc(shot.shot_id)}</span>
        <span>${esc(shot.camera || shot.action || "")}</span>
        <span class="chip">${esc(shot.duration_sec || "")} 秒</span>
        ${statusChip(shot.status)}
      </summary>
      <div class="shot-body">
        <div>
          ${shot.keyframe ? `<img alt="镜头 ${esc(shot.shot_id)} 关键帧" src="${mediaUrl(project, shot.keyframe)}">` : `<p class="empty">关键帧还没有</p>`}
          ${shot.video ? `<video controls src="${mediaUrl(project, shot.video)}" style="margin-top:8px"></video>` : ""}
        </div>
        <div class="meta">
          <p>场景 ${esc(shot.scene_id || "—")} · 角色 ${esc((shot.characters || []).join("、") || "—")}</p>
          <p>${esc(shot.action || "")}</p>
          ${shot.dialogue ? `<p>「${esc(shot.dialogue)}」</p>` : ""}
          <p>${esc(shot.image_prompt || "")}</p>
          <p>${esc(shot.motion_prompt || "")}</p>
        </div>
      </div>
    </details>`;
  }).join("");
}

function mediaUrl(project, rel) {
  return `/media/${encodeURIComponent(project.id)}/${rel.split("/").map(encodeURIComponent).join("/")}?v=${encodeURIComponent(project.updated_at || "")}`;
}

function paintLogs(project) {
  const box = document.getElementById("logs");
  const logs = project.logs || [];
  const signature = `${logs.length}:${logs.length ? logs[logs.length - 1].message : ""}`;
  if (box.dataset.sig === signature) return;
  box.dataset.sig = signature;
  const stick = box.scrollHeight - box.scrollTop - box.clientHeight < 48;
  box.replaceChildren();
  if (!logs.length) {
    const empty = document.createElement("div");
    empty.className = "empty";
    empty.textContent = "流水线日志会显示在这里。";
    box.append(empty);
    return;
  }
  for (const line of logs) {
    const row = document.createElement("div");
    row.className = `log ${line.level || "info"}`;
    const time = document.createElement("time");
    time.textContent = (line.ts || "").slice(11, 19);
    const message = document.createElement("span");
    message.textContent = line.message || "";
    row.append(time, message);
    box.append(row);
  }
  if (stick) box.scrollTop = box.scrollHeight;
}

function paintEditor(project, rebuild) {
  const host = document.getElementById("editor-host");
  if (project.status !== "awaiting_approval" || !project.storyboard) {
    host.innerHTML = "";
    state.editorKey = "";
    state.draft = null;
    return;
  }
  if (!rebuild && state.editorKey === project.id && host.childElementCount) return;
  state.editorKey = project.id;
  state.draft = structuredClone(project.storyboard);
  state.draft.style = project.style || state.draft.style;
  state.draft.aspect = project.aspect || state.draft.aspect;
  drawEditor();
}

function drawEditor() {
  const host = document.getElementById("editor-host");
  const draft = state.draft;
  if (!host || !draft) return;
  host.innerHTML = `
    <section class="card editor">
      <h2>分镜表</h2>
      <div class="layout">
        <label class="field">画风<input data-bind="style" type="text" value="${esc(draft.style || "")}"></label>
        <label class="field">画幅
          <select data-bind="aspect">
            ${["16:9", "9:16", "1:1"].map((item) => `<option ${draft.aspect === item ? "selected" : ""}>${item}</option>`).join("")}
          </select>
        </label>
      </div>
      ${tableBlock("角色", "characters", [
        ["id", "id", "text"],
        ["名字", "name", "text"],
        ["外形", "appearance", "area"],
        ["参考提示", "ref_prompt", "area"],
        ["声线", "voice_note", "text"],
      ], () => ({ id: "c_" + Date.now().toString().slice(-4), name: "", appearance: "", ref_prompt: "", voice_note: "", ref_image: "" }))}
      ${tableBlock("场景", "scenes", [
        ["id", "id", "text"],
        ["名字", "name", "text"],
        ["画面", "visual", "area"],
      ], () => ({ id: "s_" + Date.now().toString().slice(-4), name: "", visual: "", ref_image: "" }))}
      ${tableBlock("道具", "props", [
        ["id", "id", "text"],
        ["名字", "name", "text"],
        ["外观", "visual", "area"],
        ["参考提示", "ref_prompt", "area"],
      ], () => ({ id: "p_" + Date.now().toString().slice(-4), name: "", visual: "", ref_prompt: "", ref_image: "" }))}
      ${tableBlock("镜头", "shots", [
        ["镜号", "shot_id", "text"],
        ["秒", "duration_sec", "number"],
        ["场景", "scene_id", "text"],
        ["角色", "characters", "tags"],
        ["机位", "camera", "text"],
        ["动作", "action", "area"],
        ["对白", "dialogue", "text"],
        ["音效", "sfx", "text"],
        ["画面提示", "image_prompt", "area"],
        ["运动提示", "motion_prompt", "area"],
      ], () => ({ shot_id: String((draft.shots || []).length + 1).padStart(3, "0"), duration_sec: 4, scene_id: "", characters: [], camera: "", action: "", dialogue: "", sfx: "", image_prompt: "", motion_prompt: "", status: "pending" }))}
    </section>`;
}

function tableBlock(title, key, columns, factory) {
  state._factories = state._factories || {};
  state._factories[key] = factory;
  const rows = state.draft[key] || [];
  const head = columns.map(([label]) => `<th>${esc(label)}</th>`).join("") + "<th></th>";
  const body = rows.map((row, index) => `<tr>${columns.map(([, field, kind]) => `<td>${cell(key, index, field, row[field], kind)}</td>`).join("")}<td><button type="button" data-action="del-row" data-key="${esc(key)}" data-index="${index}">删除</button></td></tr>`).join("");
  return `<h2>${esc(title)}</h2><div class="table-wrap"><table><thead><tr>${head}</tr></thead><tbody>${body || `<tr><td colspan="${columns.length + 1}">还没有${esc(title)}</td></tr>`}</tbody></table></div><p><button type="button" data-action="add-row" data-key="${esc(key)}">添加${esc(title)}</button></p>`;
}

function cell(group, index, field, value, kind) {
  const path = `${group}.${index}.${field}`;
  if (kind === "area") return `<textarea data-path="${path}">${esc(value || "")}</textarea>`;
  if (kind === "number") return `<input data-path="${path}" type="number" min="2" max="15" step="0.5" value="${esc(value ?? 4)}">`;
  if (kind === "tags") return `<input data-path="${path}" data-tags="1" type="text" value="${esc((value || []).join(", "))}">`;
  return `<input data-path="${path}" type="text" value="${esc(value || "")}">`;
}

function readDraftFromDom() {
  const draft = state.draft;
  if (!draft) return null;
  document.querySelectorAll("[data-bind]").forEach((node) => {
    draft[node.dataset.bind] = node.value;
  });
  document.querySelectorAll("[data-path]").forEach((node) => {
    const [group, index, field] = node.dataset.path.split(".");
    if (!draft[group] || !draft[group][index]) return;
    if (node.dataset.tags) {
      draft[group][index][field] = node.value.split(/[,，\s]+/).map((item) => item.trim()).filter(Boolean);
    } else if (node.type === "number") {
      draft[group][index][field] = Number(node.value);
    } else {
      draft[group][index][field] = node.value;
    }
  });
  return draft;
}

async function openRun(id) {
  if (!id) {
    state.projects = await api("/api/projects");
    if (state.projects[0]) {
      location.hash = `#/run/${state.projects[0].id}`;
      return;
    }
    main.innerHTML = `<h1>运行</h1><p class="empty">还没有项目。先回开拍页贴一章。</p>`;
    return;
  }
  const project = await api(`/api/projects/${id}`);
  state.project = project;
  state.editorKey = "";
  main.innerHTML = runSkeleton();
  paintRun(project, true);
  openStream(id);
}

function openStream(id) {
  closeStream();
  const source = new EventSource(`/api/projects/${id}/events`);
  state.stream = source;
  source.onmessage = (event) => {
    const data = JSON.parse(event.data);
    if (data.project && state.view === "run" && state.projectId === id) {
      state.project = data.project;
      paintRun(data.project, false);
    }
  };
  state.poll = setInterval(async () => {
    if (state.view !== "run" || state.projectId !== id) return;
    try {
      const project = await api(`/api/projects/${id}`);
      state.project = project;
      paintRun(project, false);
      if (["done", "failed", "cancelled"].includes(project.status)) {
        clearInterval(state.poll);
        state.poll = null;
      }
    } catch { /* 下一轮再试 */ }
  }, 2500);
}

async function openGallery(id) {
  state.editorKey = "";
  closeStream();
  if (!id) {
    state.projects = await api("/api/projects");
    const picked = state.projects.find((item) => item.final_video) || state.projects[0];
    if (picked) {
      location.hash = `#/gallery/${picked.id}`;
      return;
    }
    main.innerHTML = `<h1>画廊</h1><p class="empty">还没有成片。</p>`;
    return;
  }
  const project = await api(`/api/projects/${id}`);
  state.project = project;
  const board = project.storyboard || {};
  const refs = ["characters", "scenes", "props"].flatMap((key) => (board[key] || []).filter((item) => item.ref_image).map((item) => ({
    title: item.name || item.id,
    kind: key === "characters" ? "角色" : key === "scenes" ? "场景" : "道具",
    src: mediaUrl(project, item.ref_image),
    video: false,
  })));
  const frames = (board.shots || []).filter((shot) => shot.keyframe).map((shot) => ({
    title: `镜头 ${shot.shot_id}`,
    kind: "关键帧",
    src: mediaUrl(project, shot.keyframe),
    video: false,
  }));
  const clips = (board.shots || []).filter((shot) => shot.video).map((shot) => ({
    title: `镜头 ${shot.shot_id}`,
    kind: "视频",
    src: mediaUrl(project, shot.video),
    video: true,
  }));
  const finalSrc = project.final_video ? mediaUrl(project, project.final_video) : "";
  main.innerHTML = `
    <header class="page-head">
      <div>
        <h1>${esc(project.name)}</h1>
        <p class="lede">参考图、关键帧、镜头视频和成片。</p>
      </div>
      <div class="row">
        <a class="btn" href="#/run/${esc(project.id)}">返回运行</a>
        ${finalSrc ? `<a class="btn primary" href="${finalSrc}" download="chapter.mp4">下载成片</a>` : ""}
      </div>
    </header>
    ${finalSrc ? `<div class="gallery-hero card"><video controls src="${finalSrc}"></video></div>` : `<p class="empty">成片还没拼出来。${project.status === "failed" ? esc(project.error || "") : ""}</p>`}
    ${gallerySection("参考图", refs)}
    ${gallerySection("关键帧", frames)}
    ${gallerySection("镜头视频", clips)}`;
}

function gallerySection(title, items) {
  if (!items.length) return `<h2 class="section-title">${esc(title)}</h2><p class="empty">这一栏还是空的。</p>`;
  return `<h2 class="section-title">${esc(title)}</h2><div class="grid">${items.map((item) => `
    <figure class="tile">
      ${item.video ? `<video controls src="${item.src}"></video>` : `<img alt="${esc(item.title)}" src="${item.src}">`}
      <figcaption>${esc(item.kind)} · ${esc(item.title)}</figcaption>
    </figure>`).join("")}</div>`;
}

async function createProject(form) {
  const button = document.getElementById("run-btn");
  const error = document.getElementById("home-error");
  error.textContent = "";
  button.disabled = true;
  try {
    const data = Object.fromEntries(new FormData(form).entries());
    const project = await api("/api/projects", { method: "POST", body: JSON.stringify(data) });
    await api(`/api/projects/${project.id}/run`, { method: "POST", body: JSON.stringify({ from_stage: "stage0" }) });
    location.hash = `#/run/${project.id}`;
  } catch (err) {
    error.textContent = err.message;
    button.disabled = false;
  }
}

async function saveSettings(form) {
  const data = Object.fromEntries(new FormData(form).entries());
  data.demo_mode = data.demo_mode === "true";
  state.settings = await api("/api/settings", { method: "PUT", body: JSON.stringify(data) });
  const msg = document.getElementById("settings-msg");
  if (msg) msg.textContent = "已保存";
}

function readFile(file) {
  if (!file) return;
  const reader = new FileReader();
  reader.onload = () => {
    const area = document.querySelector("textarea[name=script]");
    if (area) area.value = String(reader.result || "");
    const name = document.querySelector("input[name=name]");
    if (name && !name.value) name.value = file.name.replace(/\.(md|txt)$/i, "");
  };
  reader.readAsText(file);
}

async function route() {
  const next = parseHash();
  state.view = next.view;
  state.projectId = next.id;
  paintNav();
  if (next.view !== "run") closeStream();
  try {
    if (next.view === "home") await renderHome();
    else if (next.view === "settings") await renderSettings();
    else if (next.view === "run") await openRun(next.id);
    else await openGallery(next.id);
  } catch (err) {
    main.innerHTML = `<div class="banner bad">${esc(err.message)}</div>`;
  }
}

main.addEventListener("click", async (event) => {
  const aspect = event.target.closest("[data-aspect]");
  if (aspect) {
    document.querySelectorAll("[data-aspect]").forEach((node) => node.classList.toggle("on", node === aspect));
    const hidden = document.querySelector("input[name=aspect]");
    if (hidden) hidden.value = aspect.dataset.aspect;
  }
  const button = event.target.closest("[data-action]");
  if (!button) return;
  const action = button.dataset.action;
  try {
    if (action === "sample") {
      const sample = await api("/api/sample-chapter");
      const area = document.querySelector("textarea[name=script]");
      const name = document.querySelector("input[name=name]");
      const style = document.querySelector("#home-form input[name=style]");
      if (area) area.value = sample.script;
      if (name) name.value = sample.name;
      if (style) style.value = sample.style;
      const hidden = document.querySelector("input[name=aspect]");
      if (hidden) hidden.value = sample.aspect;
      document.querySelectorAll("[data-aspect]").forEach((node) => node.classList.toggle("on", node.dataset.aspect === sample.aspect));
    } else if (action === "check") {
      state.services = await api("/api/settings/check", { method: "POST" });
      paintServices();
      paintChecks(state.services);
      const list = document.getElementById("model-list");
      if (list && state.services.ollama) {
        list.innerHTML = (state.services.ollama.models || []).map((name) => `<option value="${esc(name)}"></option>`).join("");
      }
    } else if (action === "continue") {
      button.disabled = true;
      const storyboard = readDraftFromDom();
      state.project = await api(`/api/projects/${button.dataset.id}/continue`, {
        method: "POST",
        body: JSON.stringify({ storyboard }),
      });
      state.editorKey = "";
      paintRun(state.project, true);
    } else if (action === "rerun") {
      button.disabled = true;
      state.editorKey = "";
      state.project = await api(`/api/projects/${button.dataset.id}/run`, {
        method: "POST",
        body: JSON.stringify({ from_stage: button.dataset.from || "stage0" }),
      });
      paintRun(state.project, true);
      openStream(button.dataset.id);
    } else if (action === "retry") {
      button.disabled = true;
      state.project = await api(`/api/projects/${button.dataset.id}/retry`, { method: "POST" });
      paintRun(state.project, true);
      openStream(button.dataset.id);
    } else if (action === "cancel") {
      button.disabled = true;
      state.project = await api(`/api/projects/${button.dataset.id}/cancel`, { method: "POST" });
      paintRun(state.project, false);
    } else if (action === "add-row") {
      readDraftFromDom();
      const factory = state._factories && state._factories[button.dataset.key];
      state.draft[button.dataset.key] = state.draft[button.dataset.key] || [];
      state.draft[button.dataset.key].push(factory ? factory() : {});
      drawEditor();
    } else if (action === "del-row") {
      readDraftFromDom();
      state.draft[button.dataset.key].splice(Number(button.dataset.index), 1);
      drawEditor();
    }
  } catch (err) {
    const banner = document.getElementById("banners") || document.getElementById("home-error");
    if (banner) {
      if (banner.id === "home-error") banner.textContent = err.message;
      else banner.insertAdjacentHTML("afterbegin", `<div class="banner bad">${esc(err.message)}</div>`);
    }
    button.disabled = false;
  }
});

main.addEventListener("submit", async (event) => {
  if (event.target.id === "home-form") {
    event.preventDefault();
    await createProject(event.target);
  } else if (event.target.id === "settings-form") {
    event.preventDefault();
    try { await saveSettings(event.target); } catch (err) {
      const msg = document.getElementById("settings-msg");
      if (msg) msg.textContent = err.message;
    }
  }
});

main.addEventListener("change", (event) => {
  if (event.target.id === "script-file") readFile(event.target.files[0]);
  if (event.target.name === "video_workflow") {
    const note = document.getElementById("vram-note");
    if (note) note.textContent = videoNote(event.target.value);
  }
});

main.addEventListener("dragover", (event) => {
  if (event.target.closest("#drop")) event.preventDefault();
});
main.addEventListener("drop", (event) => {
  if (!event.target.closest("#drop")) return;
  event.preventDefault();
  const file = event.dataTransfer.files && event.dataTransfer.files[0];
  readFile(file);
});

window.addEventListener("hashchange", () => { route(); });

async function boot() {
  try {
    state.settings = await api("/api/settings");
    state.workflows = await api("/api/workflows");
    state.projects = await api("/api/projects");
  } catch (err) {
    main.innerHTML = `<div class="banner bad">${esc(err.message)}</div>`;
    return;
  }
  await route();
  try {
    state.services = await api("/api/settings/check", { method: "POST" });
    paintServices();
    if (state.view === "home") await renderHome();
    if (state.view === "settings") paintChecks(state.services);
  } catch { /* 检测失败不挡住页面 */ }
}

boot();
