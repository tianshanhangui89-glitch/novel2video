# 手工跑通顺序（冒烟：1 章、2–5 镜）

交互界面见仓库根目录 README：`./run.sh` 后打开 http://127.0.0.1:43123 。下面是不打开网页时的命令行顺序。

1. 准备小说：`input/chapter01.md`
2. Stage1：`python scripts/stage1_llm_storyboard.py --chapter input/chapter01.md --style "..." --out configs/generated_ch01.json`
3. ComfyUI ≥0.30.0，下载 Qwen-Image-2.1 与 MiniMax-H3 权重（见 README）
4. 打开 `workflows/01_character_t2i.json` → 为每个角色出定妆图 → 存 `output/characters/`
5. 打开 `workflows/02_shot_edit.json` → 角色图作参考 → 每镜关键帧 → `output/shots/shot_XXX_key.png`
6. 打开 `workflows/03_h3_i2v.json` → 关键帧图生视频 → `output/shots/shot_XXX.mp4`  
   VRAM 不够改用 `03c_wan22_i2v.json`
7. `./scripts/stitch_shots.sh output/shots output/final/chapter.mp4`
