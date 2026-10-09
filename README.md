# Novel2Video · 小说成片

把一章小说或剧本做成一条本地视频。浏览器里能看到每个阶段、当前镜头、日志，以及刚出完的参考图、关键帧和镜头视频。

流水线：

```
阶段 0  接收文稿
阶段 1  Ollama 文本模型 → 角色 / 场景 / 道具 / 镜头 JSON，可改完再继续
阶段 2  ComfyUI · Qwen-Image-2.1 文生图 → 定妆、场景、道具参考图
阶段 3  ComfyUI · Qwen-Image-2.1 图像编辑（多参考）→ 每镜关键帧
阶段 3.5 （可选）ComfyUI · MiniMax-H3 Prompt Rewriter → 每镜改写成 H3 结构化提示词
阶段 4  ComfyUI · MiniMax H3 图生视频（默认）
        或 H3 参考生视频，或低显存 Wan2.2（工作流 03c）
阶段 4.5 （可选）TTS 配音 → 每镜预设音色，对齐后混入成片
阶段 5  FFmpeg 统一分辨率后拼接（可混配音）
阶段 6  成片复制到输出目录
```

编剧用的是 **Qwen 文本模型**（Ollama）。出图用的是 **Qwen-Image-2.1**，不是把图像模型拿来写分镜。

## 启动

```bash
cd /path/to/this/repo
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
./run.sh
```

浏览器打开 [http://127.0.0.1:43123](http://127.0.0.1:43123)。

等价命令：

```bash
python -m uvicorn app.main:app --host 127.0.0.1 --port 43123
```

不需要 Docker。第一次进入会放进示例项目「客栈夜雨」（`input/chapter01.md` + `configs/example_chapter.json`），状态停在「待确认」，可以直接改分镜表再继续。

## 环境

| 组件 | 作用 | 默认地址 |
|------|------|----------|
| Ollama | 阶段 1 分镜 | http://127.0.0.1:11434 |
| ComfyUI ≥ 0.30 | 出图、出视频 | http://127.0.0.1:8188 |
| FFmpeg | 拼接 | `PATH` 里的 `ffmpeg` |

设置页有三个检测按钮，连不上会写出原因。Ollama 模型列表来自 `ollama list`（`/api/tags`）。留空时优先选名字里带 Qwen、且不是 embedding / vision 的文本模型。

没有 GPU 时，在设置里打开 **演示模式**。界面、阶段和拼接仍会跑完，画面是本地占位，不会往 ComfyUI 提交任务。分镜来源可以选「始终本地分镜」，这样也不依赖 Ollama。

## 模型放哪

权重放到 **ComfyUI 的 `models/`**，不是这个仓库。模板里的文件名就是加载节点要选的文件名。

### Qwen-Image-2.1（阶段 2、3）

文档：<https://docs.comfy.org/tutorials/image/qwen/qwen-image-2-1>

| 文件 | 目录 |
|------|------|
| `qwen_image_2.1_int8_convrot.safetensors` | `models/diffusion_models/` |
| `qwen3vl_8b_int8_convrot.safetensors` | `models/text_encoders/` |
| `qwen_image_2.1_vae_bf16.safetensors` | `models/vae/` |
| `qwen3.5_9b_qwen_image_2.1_pe_t2i.int8_convrot.safetensors` | `models/text_encoders/`（文生图提示词增强，模板默认关着） |
| `qwen3.5_9b_qwen_image_2.1_pe_i2i.int8_convrot.safetensors` | `models/text_encoders/`（编辑增强，模板默认关着） |

- Hugging Face：<https://huggingface.co/Comfy-Org/Qwen-Image-2.1>
- ModelScope：<https://www.modelscope.cn/models/Comfy-Org/Qwen-Image-2.1>
- 采样：`steps=25, cfg=1, euler, simple`

### MiniMax H3（阶段 4 默认）

文档：<https://docs.comfy.org/tutorials/video/minimax/minimax-h3>

| 文件 | 目录 | 用途 |
|------|------|------|
| `minimax_h3_fl2va_pruned_int8_convrot.safetensors` | `diffusion_models/` | 图生视频 `03_h3_i2v.json` |
| `minimax_h3_ref2va_pruned_int8_convrot.safetensors` | `diffusion_models/` | 参考生视频 `03b_h3_r2v.json` |
| `qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors` | `text_encoders/` | 文本编码 |
| `minimax_h3_video_vae_int8_convrot.safetensors` | `vae/` | 视频 VAE |
| `minimax_h3_audio_vae_fp32.safetensors` | `vae/` | 音频 VAE |

16:9 默认画布 **1344×768**。H3 很吃显存；显存不够就改用下面的 Wan 分支。开源包没有平台侧的 2K 再生。

### Wan2.2（低显存，工作流 03c）

选「Wan2.2 图生视频」时，程序提交的是 `workflows/03c_wan22_i2v.json`，16:9 画布降到 **832×480**。

文档：<https://docs.comfy.org/tutorials/video/wan/wan2_2>

权重：<https://huggingface.co/Comfy-Org/Wan_2.2_ComfyUI_Repackaged>

模板里用到的文件包括：

- `wan2.2_i2v_high_noise_14B_fp8_scaled.safetensors`
- `wan2.2_i2v_low_noise_14B_fp8_scaled.safetensors`
- `wan2.2_i2v_lightx2v_4steps_lora_v1_high_noise.safetensors`
- `wan2.2_i2v_lightx2v_4steps_lora_v1_low_noise.safetensors`
- `umt5_xxl_fp8_e4m3fn_scaled.safetensors`
- `wan_2.1_vae.safetensors`

## H3 提示词改写（可选）/ H3 prompt rewrite (optional)

**中文**

MiniMax H3 是用结构化英文提示词训练的：`integrated_multimodal_description`（画面、动作、对白，按时间码）、`overall_soundscape`（环境音）、`non_diegetic_music`（配乐）。分镜里每镜只有一句短提示词，直接喂给 H3 效果一般。打开这个阶段后，关键帧出完（阶段 3）、提交 H3 视频（阶段 4）之前，程序会用 ComfyUI 节点包 [MiniMax-H3-Prompt-Rewriter-ComfyUI](https://github.com/pytraveler/MiniMax-H3-Prompt-Rewriter-ComfyUI) 把每镜改写成这种格式。画幅和时长取自镜头本身。

默认关闭。到 **设置 → H3 提示词改写** 里打开，再选一个改写模型：

| 选项 | 节点 | 基础模型 | 显存（约） | 看关键帧 |
|------|------|----------|------------|----------|
| **Rewriter 8B**（推荐） | `MiniMaxH3PromptWriter8B` | Qwen3-VL-8B-Instruct GGUF Q4_K_M + 8B LoRA | **9 GB** | 是，I2VA（仅 H3 图生视频分支） |
| Rewriter 27B | `MiniMaxH3PromptRewriter` | Qwen3.6-27B GGUF UD-IQ2_M + LoRA | **13 GB**（Q4_K_M 约 19 GB） | 否，纯文本 |
| Writer | `MiniMaxH3GuidedWriter` | 任意 GGUF，默认 Qwen3.5-4B Q4_K_M | **5 GB** | 否，纯文本，不用 LoRA |

改写模型和 H3 不会同时占显存：改写节点按 `keep_model_loaded=false` 提交，跑完就卸载。

**安装节点**

1. ComfyUI Manager → *Custom Nodes Manager* → 搜索 `minimax-h3-prompt-rewriter` → 安装 → 重启 ComfyUI。
   也可以用 registry：`comfy node install minimax-h3-prompt-rewriter`。
2. 或者手动：
   ```bash
   cd ComfyUI/custom_nodes
   git clone https://github.com/pytraveler/MiniMax-H3-Prompt-Rewriter-ComfyUI
   pip install -r MiniMax-H3-Prompt-Rewriter-ComfyUI/requirements.txt
   ```
   GGUF 模型需要 `llama-cpp-python`（带 CUDA 的轮子）。
3. 回到本程序设置页，点「检测连接」。开着改写时会多一行「H3 改写节点」，列出缺哪些节点。

**模型下载**

节点第一次运行时会自己从 Hugging Face 下载，放进 `ComfyUI/models/LLM/`。离线机器可以先手动下好放进去：

| 用途 | Hugging Face 仓库 | 文件 |
|------|-------------------|------|
| 8B 基础模型 | `Qwen/Qwen3-VL-8B-Instruct-GGUF` | `Qwen3VL-8B-Instruct-Q4_K_M.gguf`（5.4 GB） |
| 8B 适配器 | `lightx2v/MiniMax-H3-Prompt-Rewriter-LoRA-8B`，GGUF 版 `pytraveler/MiniMax-H3-Prompt-Rewriter-LoRA-8B-GGUF` | `MiniMax-H3-Prompt-Rewriter-LoRA-8B-F16.gguf` |
| 27B 基础模型 | `unsloth/Qwen3.6-27B-GGUF` | `Qwen3.6-27B-UD-IQ2_M.gguf`（10.1 GB），或 `Qwen3.6-27B-Q4_K_M.gguf`（15.7 GB） |
| 27B 适配器 | `lightx2v/MiniMax-H3-Prompt-Rewriter-LoRA`，GGUF 版 `pytraveler/MiniMax-H3-Prompt-Rewriter-LoRA-GGUF` | `MiniMax-H3-Prompt-Rewriter-LoRA-F16.gguf` |
| Writer | `unsloth/Qwen3.5-4B-GGUF` | `Qwen3.5-4B-Q4_K_M.gguf`（2.6 GB） |

设置里的「模型」和「适配器」两栏可以留空（用工作流里的默认），也可以填下拉里的名字或文件名。程序会读 `/object_info`，按名字或文件名片段匹配下拉选项；找不到时会写一条警告并用默认值。

**工作流**

| 文件 | 改写模型 |
|------|----------|
| `workflows/04a_h3_rewrite_8b.json` | Rewriter 8B（`LoadImage` → `first_frame`，`MiniMaxH3RewriterOptions` → `options`） |
| `workflows/04b_h3_rewrite_27b.json` | Rewriter 27B |
| `workflows/04c_h3_writer.json` | Writer |

它们和其他模板一样是界面格式，提交前摊平成 API 格式。程序会注入提示词（原始提示词加机位和动作）、`resolution` / `aspect_ratio`（镜头画幅）、`duration`（镜头秒数）、种子，以及 I2VA / T2VA。`rewritten_prompt` 接到 ComfyUI 自带的 `PreviewAny` 节点，程序从 `/history` 的 `outputs[节点]["text"]` 读回文字。

**运行页**

- 阶段条里多一个「阶段 3.5 · H3 提示词改写」。
- 每个镜头卡片里能对照看「原始提示词」和「H3 改写」，标签会写出来源：已改写、演示改写、手动修改、沿用原文。
- 「改写后人工确认」默认开：改写完会停在「待确认提示词」，每镜一个文本框，可以直接改，「恢复原始」一键还原，清空也等于沿用原文。点「确认提示词并生成视频」才进阶段 4。手动改过的镜头，旧视频会删掉重出。
- 完成或失败后可以点「从改写重跑」。

**退回原始提示词**

以下情况会写一条警告，并把原始提示词原样交给 H3，不会让整条片子失败：

- `/object_info` 里没有所选改写节点、`MiniMaxH3RewriterOptions` 或 `PreviewAny`
- 单个镜头改写报错、超时，或节点没返回文字

视频分支选 Wan2.2 时整个阶段跳过。参考生视频分支没有首帧，8B 会按 T2VA 只读文字。

**演示模式** 不调用 ComfyUI，会生成一段同样三段式的模拟改写，用来走通对照和确认界面。

**English**

MiniMax H3 was trained on structured English prompts with three sections: `integrated_multimodal_description`, `overall_soundscape` and `non_diegetic_music`. When this optional stage is on, it runs after keyframes (stage 3) and before H3 video generation (stage 4). It uses the ComfyUI node pack [MiniMax-H3-Prompt-Rewriter-ComfyUI](https://github.com/pytraveler/MiniMax-H3-Prompt-Rewriter-ComfyUI) to rewrite each shot's short prompt into that format, and takes the aspect ratio and duration from the shot.

- **Enable:** go to *Settings → H3 提示词改写* and set it to on (it is off by default). Then pick a variant: **Rewriter 8B** (Qwen3-VL-8B, about 9 GB VRAM, sees the keyframe, recommended), **Rewriter 27B** (Qwen3.6-27B UD-IQ2_M, about 13 GB, text only) or **Writer** (any GGUF, about 5 GB with Qwen3.5-4B, no adapter). The *model* and *adapter* fields are optional. They are matched against the node's dropdown options from `/object_info`, by label or by filename fragment.
- **Install:** in ComfyUI Manager, search for `minimax-h3-prompt-rewriter`, or run `comfy node install minimax-h3-prompt-rewriter`. To install manually, `git clone` the repo into `ComfyUI/custom_nodes` and `pip install -r requirements.txt` (GGUF models need `llama-cpp-python`). Restart ComfyUI, then click *检测连接* (check connections) in Settings.
- **Models:** the nodes download into `ComfyUI/models/LLM/` on first use. For offline machines, the repos and files are listed in the table above.
- **Workflows:** `workflows/04a_h3_rewrite_8b.json`, `04b_h3_rewrite_27b.json` and `04c_h3_writer.json` are UI-format graphs. Like the other templates, they are flattened to API format at queue time. The rewritten text is wired into core `PreviewAny` and read back from `/history`.
- **Review:** each shot card shows the original prompt next to the rewritten one. With *改写后人工确认* (review after rewrite) on, which is the default, the run pauses at *待确认提示词* (awaiting prompt review), so you can edit each prompt before stage 4. An empty box means "use the original".
- **Fallback:** if a node is missing, or a single rewrite fails, the app logs a warning and sends the original prompt to H3. The stage is skipped on the Wan2.2 branch. Demo mode simulates the rewrite offline.

## 工作流

`workflows/` 里是 Comfy 官方模板的 **界面格式**（带 subgraph），节点名来自这些文件，程序不会另造一套节点。

运行时会把子图摊平成 API 格式（节点 id 形如 `459:451`），再改这几处：

- 正向提示词
- `LoadImage` 的文件名（先上传到 Comfy 的 input）
- 保存前缀、种子
- 文生图和视频的宽高（编辑工作流默认跟着参考图，不强制改尺寸）
- 视频时长

所以一般不用先在界面里导出。若你的 Comfy 版本对某个官方节点不认，日志里会带上拒绝原因。那时可以：

1. 在 ComfyUI 打开对应 JSON
2. 菜单 **File → Export Workflow (API)**
3. 把导出的文件放进 `workflows/`，在设置页选它

API 格式的特征是顶层没有 `nodes` 数组，而是 `{节点id: {class_type, inputs}}`。程序识别到这种文件会直接提交，并同样注入提示词和图片。

命令行等价物：

```bash
python scripts/comfy_queue.py \
  --workflow workflows/01_character_t2i.json \
  --prompt "anime portrait of a young man in a slate-blue robe" \
  --convert-only /tmp/01_api.json

python scripts/comfy_queue.py --workflow /tmp/01_api.json
```

只做分镜、不跑界面：

```bash
python scripts/stage1_llm_storyboard.py \
  --chapter input/chapter01.md \
  --style "anime cel-shading, cinematic lighting" \
  --out configs/generated_ch01.json
```


## 配音 TTS（可选）/ Voiceover TTS (optional)

**中文**

默认关闭。打开后，阶段 4 镜头视频出完，会按分镜文本合成配音，再进入拼接：

1. 文本：优先用镜头 **对白**；没有对白时可用 **动作** 作短旁白（设置里可改成「只读对白」）。
2. 对齐：若配音比视频长，用 FFmpeg 冻结尾帧把视频垫到配音时长，并更新 `duration_sec`；配音较短则在混音时补静音。
3. 混音：阶段 5 把每镜 WAV 按设置的音量叠进对应镜头，再 concat 成片。

**提供方（可插拔）**

| id | 说明 |
|----|------|
| `cosyvoice` | 本机 [CosyVoice](https://github.com/FunAudioLLM/CosyVoice) FastAPI，**只用预训练音色**（中文女 / 中文男 / 英文女 / 英文男 / 日语男 / 粤语女）。默认地址 `http://127.0.0.1:50000`，调用 `POST /inference_sft`（`tts_text` + `spk_id`）。 |
| `local` | 免安装回退。若 `PATH` 里有 `espeak-ng` / `espeak` 就用它；否则生成按时长估算的占位音，用来走通对齐和混音。 |
| `clone` | **未实现的预留接口。** 将来只会支持用户本人的声音，或用户明确授权的声音；当前选它会报错，不会走任何 zero-shot / 参考音频路径。 |

设置项：启用开关、提供方、CosyVoice URL、音色、语速（0.5–2）、音量（0–2）、朗读文本模式。运行页每镜可播放配音；演示模式会写占位 WAV，不连 CosyVoice。

**English**

Optional stage 4.5. Prefer shot **dialogue**, else a short **action** narration. Audio longer than the clip pads the video (freeze last frame); staging mixes each WAV into the FFmpeg cut. Providers: **CosyVoice** preset speakers only (configurable API URL), **local** offline fallback (espeak if present, else timed placeholder), and a clearly marked **clone stub** that refuses to run and documents authorized-voice-only future use. Demo mode simulates speech offline.

## 低配串行 / Low-resource serial mode

**中文**

本机显存和内存有限时，Ollama、ComfyUI（出图 / H3 改写 / 视频）和配音 **不能同时占 GPU**。设置里的 **低配串行模式** 默认开启：

- 进程内全局锁：同一时间只有一个「重模型」任务在跑；其它项目/阶段在队列里等。
- 按阶段批处理，减少换模：先全部 LLM，再全部出图（参考图 + 关键帧同属 `image` 家族，中间不卸），再改写，再视频，再配音。
- 换家族时释放显存：
  - Ollama：对驻留模型 `POST /api/generate`，`keep_alive: 0`
  - ComfyUI：`POST /free`，`unload_models` + `free_memory`（出图 ↔ 改写 ↔ 视频 之间也会 free）
  - TTS：若提供方实现了 `release()` 则调用
- 运行页顶部显示 **谁占用 GPU**、**本项目是否在排队**、**队列里还有谁**。`GET /api/resources` 可查同一快照。
- 演示模式仍走锁（方便看排队），但跳过真实卸载请求。

关掉串行后不再保证互斥，多任务可能把显存打爆。

**English**

**Low-resource serial mode** defaults to on. A process-wide GPU queue allows only one heavy family at a time (`llm` → `image` → `rewrite` → `video` → `tts`). Switching family unloads the previous one (Ollama `keep_alive=0`, ComfyUI `/free`, TTS `release` when supported). Stages batch work to minimise swaps; the run page shows the holder and queue. Demo mode keeps the lock for UI visibility but skips unload HTTP calls.

## 一章冒烟

1. `./run.sh`，打开设置，点「检测连接」。
2. 没有模型时：分镜来源选「始终本地分镜」，打开演示模式，保存。有模型就保持默认。
3. 开拍页点「载入示例章节」，项目名会变成「客栈夜雨」，然后「开始生成」。
4. 运行页阶段 0、1 走完后出现分镜表。可以改林远的外形或镜头提示，再点「确认并继续」。
5. 阶段 2 出角色、场景、道具参考图；阶段 3 每镜一张关键帧（场景图作编辑底，角色图作参考）；阶段 4 出 mp4；阶段 5 拼成片。
6. 画廊里播放，或下载 `chapter.mp4`。文件同时在项目媒体目录和设置里的输出目录（默认 `output/<项目id>/chapter.mp4`）。

示例分镜只有 **2 个镜头**，用来确认整条链路，不是长片。真跑 Comfy 时一次只提交一个任务，避免把显存撑满。

低显存：开拍或设置里把视频工作流改成 **Wan2.2**。运行页会提示正在用 `03c`。

试 H3 提示词改写：设置里打开「H3 提示词改写」（演示模式下也能开），重新开拍示例章节。阶段 3 之后会停在「待确认提示词」，对照原文改一镜，再点「确认提示词并生成视频」。

试配音：设置里打开「配音」，提供方选「本地占位」或 CosyVoice，保存后重跑。阶段 4.5 会出 WAV，镜头卡片可播放，成片里能听到混音。低配串行保持默认开启；同时开两个项目时，运行页能看到 GPU 占用和排队。

## 目录

```
app/                 FastAPI、流水线、Comfy 转换与提交、FFmpeg、TTS、GPU 串行锁
static/              中文界面
workflows/           01 文生图、02 编辑、03 H3 图生视频、03b H3 参考生视频、03c Wan2.2、
                     04a/04b/04c H3 提示词改写（8B / 27B / Writer）
prompts/stage1_storyboard.txt
config/project.schema.json
configs/example_chapter.json
input/chapter01.md
scripts/             命令行分镜、提交 Comfy、拼接说明
output/              默认成片目录
data/                项目和设置（本地生成）
```

## 测试

```bash
pip install pytest
pytest -q
```

转换测试会把五个官方模板摊成 API 图，并检查提示词、参考图和负向提示有没有灌对。演示模式测试会在没有 Ollama / Comfy 的机器上跑完示例章节并得到 mp4。改写测试覆盖三个改写模板的摊平、下拉匹配、缺节点和失败时的回退、成功时读回文字，以及开着人工确认时手动改提示词、阶段 4 用上改过的文字。配音测试覆盖提供方接口、占位 WAV、混音拼接、演示模式出配音。串行锁测试覆盖互斥排队、换家族卸载、关闭串行后的并发。

## 许可

- 编排代码和示例文稿可随这个项目使用
- `workflows/*.json` 来自 [Comfy-Org/workflow_templates](https://github.com/Comfy-Org/workflow_templates)，遵循该仓库许可
- `workflows/04*_h3_*.json` 用的是 [MiniMax-H3-Prompt-Rewriter-ComfyUI](https://github.com/pytraveler/MiniMax-H3-Prompt-Rewriter-ComfyUI) 的节点，遵循该仓库许可
- 模型权重遵循 Qwen、MiniMax、Comfy-Org 各自协议。H3 有地区和商用限制，商用前看官方说明
