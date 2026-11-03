#!/usr/bin/env bash
# Web copy of a finished cut, for serving from a site: H.264 two-pass to a target size, AAC, faststart, plus a poster.
#   webencode.sh <in.mp4> <out.mp4> [height=1080] [target_mb=30] [poster_seconds=30]
# Use the -nosubs master when the page adds the .vtt as a track. Keep the master; this is a delivery copy.
set -euo pipefail
in="$1"; out="$2"; h="${3:-1080}"; mb="${4:-30}"; at="${5:-30}"
dur="$(ffprobe -v error -show_entries format=duration -of csv=p=0 "$in")"
# 3 % for the container, 96 kb/s for the audio
v="$(python3 -c "print(int($mb*8192*0.97/$dur-96))")"
tmp="$(mktemp -d)"; trap 'rm -rf "$tmp"' EXIT
args=(-vf "scale=-2:$h,format=yuv420p" -c:v libx264 -preset slow -tune stillimage -b:v "${v}k" -maxrate "$((v*3))k" -bufsize "$((v*6))k" -g 120)
(cd "$tmp" && ffmpeg -v error -y -i "$in" "${args[@]}" -pass 1 -an -f null /dev/null)
(cd "$tmp" && ffmpeg -v error -y -i "$in" "${args[@]}" -pass 2 -c:a aac -b:a 96k -movflags +faststart "$out")
ffmpeg -v error -y -ss "$at" -i "$in" -frames:v 1 -vf "scale=-2:$h" -q:v 3 "${out%.mp4}.jpg"
ls -l "$out" | awk -v k="$v" '{printf "%.1f MB, video %s kb/s\n", $5/1048576, k}'
