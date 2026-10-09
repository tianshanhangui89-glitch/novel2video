#!/usr/bin/env bash
# 按 shot_*.mp4 文件名顺序拼接成片
set -euo pipefail
SHOT_DIR="${1:-./output/shots}"
OUT="${2:-./output/final/chapter.mp4}"
LIST="$(mktemp)"
trap 'rm -f "$LIST"' EXIT
ls "$SHOT_DIR"/shot_*.mp4 | sort | while read -r f; do
  printf "file '%s'\n" "$(realpath "$f")" >> "$LIST"
done
mkdir -p "$(dirname "$OUT")"
ffmpeg -y -f concat -safe 0 -i "$LIST" -c copy "$OUT"
echo "Wrote $OUT"
