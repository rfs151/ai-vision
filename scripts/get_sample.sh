#!/usr/bin/env bash
# Download OpenCV's pedestrian sample (vtest.avi) and convert to H.264 (RTSP can't carry MS-MPEG4).
# -bf 0: no B-frames. WebRTC does not support B-frames (browsers freeze on them).
# -maxrate/-bufsize: cap keyframes at ~30 KB. A 100 KB keyframe is a burst of ~90 UDP packets
# and WSL -> Windows drops part of such bursts, so WebRTC in a Windows browser froze.
# Set PROXY=http://host:port if GitHub is not reachable directly.
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p data/samples && cd data/samples
URL=https://raw.githubusercontent.com/opencv/opencv/4.x/samples/data/vtest.avi
[ -s vtest.avi ] || curl -fL ${PROXY:+-x "$PROXY"} -o vtest.avi "$URL"
ffmpeg -v error -y -i vtest.avi -c:v libx264 -preset fast -crf 23 -maxrate 1200k -bufsize 300k -pix_fmt yuv420p -g 20 -bf 0 -an pedestrians.mp4
echo "data/samples/pedestrians.mp4"
