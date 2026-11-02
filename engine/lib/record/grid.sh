#!/usr/bin/env bash
# Contact sheet of a clip: one frame every SECS (default 1.5 s), tiled COLS wide.
#   lib/record/grid.sh <video.mp4> [out.jpg] [secs] [cols]
set -euo pipefail
IN="$1"; OUT="${2:-${IN%.*}.contact.jpg}"; SECS="${3:-1.5}"; COLS="${4:-6}"
DUR=$(ffprobe -v error -show_entries format=duration -of csv=p=0 "$IN")
N=$(awk -v d="$DUR" -v s="$SECS" 'BEGIN{n=int(d/s)+1; print n}')
ROWS=$(( (N + COLS - 1) / COLS ))
ffmpeg -y -v error -i "$IN" \
  -vf "fps=1/${SECS},scale=320:-1,drawtext=text='%{pts\:hms}':x=4:y=h-16:fontsize=14:fontcolor=white:box=1:boxcolor=black@0.5,tile=${COLS}x${ROWS}" \
  -frames:v 1 -q:v 3 "$OUT"
echo "$OUT"
