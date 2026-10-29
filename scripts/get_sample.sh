#!/usr/bin/env bash
# Download OpenCV's pedestrian sample (vtest.avi) and convert to H.264 (RTSP can't carry MS-MPEG4).
# Set PROXY=http://host:port if GitHub is not reachable directly.
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p data/samples && cd data/samples
URL=https://raw.githubusercontent.com/opencv/opencv/4.x/samples/data/vtest.avi
[ -s vtest.avi ] || curl -fL ${PROXY:+-x "$PROXY"} -o vtest.avi "$URL"
ffmpeg -v error -y -i vtest.avi -c:v libx264 -preset fast -crf 23 -pix_fmt yuv420p -g 20 -an pedestrians.mp4
echo "data/samples/pedestrians.mp4"
