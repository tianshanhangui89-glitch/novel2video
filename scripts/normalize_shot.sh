#!/usr/bin/env bash
# 分辨率不一致时先统一再拼接
set -euo pipefail
IN="$1"
OUT="$2"
ffmpeg -y -i "$IN" \
  -vf "scale=1344:768:force_original_aspect_ratio=decrease,pad=1344:768:(ow-iw)/2:(oh-ih)/2,fps=24" \
  -c:v libx264 -pix_fmt yuv420p -c:a aac -ar 32000 -ac 2 "$OUT"
