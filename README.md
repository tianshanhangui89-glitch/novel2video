# Novel2Video · 小说成片

把一章小说或剧本做成一条本地视频。浏览器里能看到每个阶段、当前镜头、日志，以及刚出完的参考图、关键帧和镜头视频。

流水线：

```
阶段 0  接收文稿
阶段 1  Ollama 文本模型 → 角色 / 场景 / 道具 / 镜头 JSON，可改完再继续
阶段 2  ComfyUI · Qwen-Image-2.1 文生图 → 定妆、场景、道具参考图
阶段 3  ComfyUI · Qwen-Image-2.1 图像编辑（多参考）→ 每镜关键帧
阶段 4  ComfyUI · MiniMax H3 图生视频（默认）
        或 H3 参考生视频，或低显存 Wan2.2（工作流 03c）
阶段 5  FFmpeg 统一分辨率后拼接
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

## 一章冒烟

1. `./run.sh`，打开设置，点「检测连接」。
2. 没有模型时：分镜来源选「始终本地分镜」，打开演示模式，保存。有模型就保持默认。
3. 开拍页点「载入示例章节」，项目名会变成「客栈夜雨」，然后「开始生成」。
4. 运行页阶段 0、1 走完后出现分镜表。可以改林远的外形或镜头提示，再点「确认并继续」。
5. 阶段 2 出角色、场景、道具参考图；阶段 3 每镜一张关键帧（场景图作编辑底，角色图作参考）；阶段 4 出 mp4；阶段 5 拼成片。
6. 画廊里播放，或下载 `chapter.mp4`。文件同时在项目媒体目录和设置里的输出目录（默认 `output/<项目id>/chapter.mp4`）。

示例分镜只有 **2 个镜头**，用来确认整条链路，不是长片。真跑 Comfy 时一次只提交一个任务，避免把显存撑满。

低显存：开拍或设置里把视频工作流改成 **Wan2.2**。运行页会提示正在用 `03c`。

## 目录

```
app/                 FastAPI、流水线、Comfy 转换与提交、FFmpeg
static/              中文界面
workflows/           01 文生图、02 编辑、03 H3 图生视频、03b H3 参考生视频、03c Wan2.2
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

转换测试会把五个官方模板摊成 API 图，并检查提示词、参考图和负向提示有没有灌对。演示模式测试会在没有 Ollama / Comfy 的机器上跑完示例章节并得到 mp4。

## 许可

- 编排代码和示例文稿可随这个项目使用
- `workflows/*.json` 来自 [Comfy-Org/workflow_templates](https://github.com/Comfy-Org/workflow_templates)，遵循该仓库许可
- 模型权重遵循 Qwen、MiniMax、Comfy-Org 各自协议。H3 有地区和商用限制，商用前看官方说明
