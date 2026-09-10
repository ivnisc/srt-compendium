#!/usr/bin/env bash
set -euo pipefail

if [[ $# -lt 2 || $# -gt 3 ]]; then
  echo "uso: generate_cbr_source.sh OUTPUT INPUT_RATE_BPS [DURATION_S]" >&2
  exit 2
fi
output=$1
input_rate_bps=$2
duration_s=${3:-60}
if [[ ! $input_rate_bps =~ ^[0-9]+$ ]] || (( input_rate_bps < 1000000 )); then
  echo "INPUT_RATE_BPS inválido: $input_rate_bps" >&2
  exit 2
fi
if [[ -f $output ]]; then
  echo "fuente ya existente: $output"
  ffprobe -v error -show_entries format=duration,size,bit_rate \
    -of default=noprint_wrappers=1 "$output"
  exit 0
fi

output_dir=$(dirname "$output")
mkdir -p "$output_dir"
temporary=$(mktemp "$output_dir/.source-cbr.XXXXXX.ts")
cleanup() {
  rm -f -- "$temporary"
}
trap cleanup EXIT INT TERM

video_rate_bps=$((input_rate_bps * 80 / 100))
ffmpeg -hide_banner -loglevel warning -y \
  -f lavfi -i testsrc=size=1280x720:rate=30 \
  -f lavfi -i sine=frequency=1000 \
  -c:v libx264 -preset ultrafast \
  -x264-params "nal-hrd=cbr:force-cfr=1" \
  -b:v "$video_rate_bps" -minrate "$video_rate_bps" \
  -maxrate "$video_rate_bps" -bufsize "$video_rate_bps" \
  -c:a aac -b:a 128k \
  -muxrate "$input_rate_bps" -t "$duration_s" -f mpegts "$temporary"
mv "$temporary" "$output"
trap - EXIT INT TERM

ffprobe -v error -show_entries format=duration,size,bit_rate \
  -of default=noprint_wrappers=1 "$output"
