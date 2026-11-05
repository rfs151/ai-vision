#!/usr/bin/env bash
# Download public test videos and convert them for streaming tests -> data/samples/<name>.mp4
#   OpenCV vtest.avi (pedestrians), Roboflow supervision example (highway traffic),
#   Intel IoT sample videos (street, people, factory, store)
# Usage: scripts/get_samples.sh    (FORCE=1 re-encodes existing files)
#                   (PROXY=http://host:port if GitHub is not reachable directly)
#
# Encoding rules (each one fixes a WebRTC freeze we hit, see README):
#   -bf 0                     WebRTC does not support B-frames
#   repeat-headers=1          SPS/PPS in front of every keyframe; a player that restarts its decoder
#                             (Windows Chrome hardware -> software fallback) needs them
#   -maxrate/-bufsize         keyframes ~30 KB, smaller UDP bursts
#   <=960 px wide, <=15 fps   enough for detection, keeps many streams cheap on one machine
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p data/samples/raw
cd data/samples

fetch() {   # url file
  [ -s "raw/$2" ] && return
  # Direct first; fall back to $PROXY only when it fails
  env -u http_proxy -u https_proxy -u HTTP_PROXY -u HTTPS_PROXY curl -fsSL -m 300 -o "raw/$2" "$1" \
    || { [ -n "${PROXY:-}" ] && curl -fsSL -m 600 -x "$PROXY" -o "raw/$2" "$1"; } \
    || { echo "download failed: $1 (set PROXY=...)"; rm -f "raw/$2"; return 1; }
}

convert() {   # raw_file out_name
  # Skip existing outputs (FORCE=1 to redo). Encode to a temp file and rename: a running push keeps
  # reading the old file instead of a half-written one (overwriting in place broke live pushes).
  if [ -s "$2.mp4" ] && [ -z "${FORCE:-}" ]; then echo "data/samples/$2.mp4  exists"; return; fi
  local fps
  fps=$(ffprobe -v error -select_streams v -show_entries stream=r_frame_rate -of csv=p=0 "raw/$1" | head -1)
  fps=$(python3 -c "f=$fps; print(min(15, round(f, 2)))")
  local gop=$(python3 -c "print(int(round($fps*2)))")
  ffmpeg -v error -y -i "raw/$1" -an \
    -vf "fps=$fps,scale='min(960,iw)':-2" \
    -c:v libx264 -preset fast -crf 23 -maxrate 1200k -bufsize 300k \
    -pix_fmt yuv420p -g "$gop" -bf 0 -x264-params repeat-headers=1 \
    "$2.tmp.mp4"
  mv -f "$2.tmp.mp4" "$2.mp4"
  echo "data/samples/$2.mp4  $(ffprobe -v error -select_streams v -show_entries stream=width,height:format=duration -of csv=p=0 "$2.mp4" | paste -sd' ')  ${fps}fps"
}

INTEL=https://raw.githubusercontent.com/intel-iot-devkit/sample-videos/master
fetch https://raw.githubusercontent.com/opencv/opencv/4.x/samples/data/vtest.avi vtest.avi
fetch $INTEL/person-bicycle-car-detection.mp4 person-bicycle-car-detection.mp4
# (Intel's car-detection.mp4 is filmed straight from above: COCO models barely see those cars)
fetch https://media.roboflow.com/supervision/video-examples/vehicles-2.mp4 vehicles-2.mp4
fetch $INTEL/people-detection.mp4             people-detection.mp4
fetch $INTEL/worker-zone-detection.mp4        worker-zone-detection.mp4
fetch $INTEL/store-aisle-detection.mp4        store-aisle-detection.mp4

convert vtest.avi                        pedestrians   # square, walking people
convert person-bicycle-car-detection.mp4 street        # people, bikes, cars
convert vehicles-2.mp4                   cars          # highway traffic, ~13 vehicles per frame
convert people-detection.mp4             people        # people indoors
convert worker-zone-detection.mp4        worker        # factory workers (danger zone)
convert store-aisle-detection.mp4        store         # store aisle (dwell)

# still: a mostly static camera, to show 画面静止时跳过推理 (skip_static). 20 s of the store aisle
# frozen while it is still empty (light sensor noise, like a real camera), then the first 30 s of
# the real clip (people walk in). Built from store.mp4, so it needs no download.
if [ ! -s still.mp4 ] || [ -n "${FORCE:-}" ]; then
  enc=(-c:v libx264 -preset fast -crf 23 -maxrate 1200k -bufsize 300k -pix_fmt yuv420p -r 15 -g 30 -bf 0
       -x264-params repeat-headers=1)
  ffmpeg -v error -y -i store.mp4 -an -filter_complex \
    "[0:v]trim=end_frame=1,loop=299:1:0,setpts=N/15/TB,noise=alls=3:allf=t[a];
     [0:v]trim=duration=30,setpts=PTS-STARTPTS[b];[a][b]concat=n=2:v=1:a=0" \
    "${enc[@]}" still.tmp.mp4
  mv -f still.tmp.mp4 still.mp4
fi
echo "data/samples/still.mp4  $(ffprobe -v error -select_streams v -show_entries stream=width,height:format=duration -of csv=p=0 still.mp4 | paste -sd' ')"
