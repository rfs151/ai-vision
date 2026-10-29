#!/usr/bin/env bash
# Loop-push a local video file to ZLM as a fake camera.
# Usage: scripts/push_sample.sh <video.mp4> [stream_name]   -> rtsp://127.0.0.1/live/<stream_name>
set -euo pipefail
FILE=$1
NAME=${2:-sample}
# h264_mp4toannexb: put SPS/PPS in front of every keyframe. MP4 keeps them only in the file header;
# a player that joins or resets its decoder later (Windows Chrome falls back from the hardware
# decoder) cannot decode any keyframe without them and freezes.
exec ffmpeg -hide_banner -loglevel warning -re -stream_loop -1 -i "$FILE" \
  -c:v copy -bsf:v h264_mp4toannexb -c:a libopus -ar 48000 -ac 2 -b:a 96k \
  -f rtsp -rtsp_transport tcp -pkt_size 1200 "rtsp://127.0.0.1/live/$NAME"
