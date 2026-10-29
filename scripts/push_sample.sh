#!/usr/bin/env bash
# Loop-push a local video file to ZLM as a fake camera.
# Usage: scripts/push_sample.sh <video.mp4> [stream_name]   -> rtsp://127.0.0.1/live/<stream_name>
set -euo pipefail
FILE=$1
NAME=${2:-sample}
exec ffmpeg -hide_banner -loglevel warning -re -stream_loop -1 -i "$FILE" \
  -c:v copy -c:a libopus -ar 48000 -ac 2 -b:a 96k \
  -f rtsp -rtsp_transport tcp -pkt_size 1200 "rtsp://127.0.0.1/live/$NAME"
