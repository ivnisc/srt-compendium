#!/usr/bin/env bash
set -euo pipefail

script_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
project_dir=$(cd "$script_dir/.." && pwd)
output=${1:-"$project_dir/assets/source_8_5mbps.ts"}
duration=${2:-60}

mkdir -p "$(dirname "$output")"
ffmpeg -hide_banner -loglevel warning -y \
  -f lavfi -i testsrc=size=1280x720:rate=30 \
  -f lavfi -i sine=frequency=1000 \
  -c:v libx264 -preset ultrafast \
  -x264-params "nal-hrd=cbr:force-cfr=1" \
  -b:v 8M -minrate 8M -maxrate 8M -bufsize 8M \
  -c:a aac -b:a 128k \
  -muxrate 8.5M -t "$duration" -f mpegts "$output"

ffprobe -v error -show_entries format=duration,size,bit_rate \
  -of default=noprint_wrappers=1 "$output"

