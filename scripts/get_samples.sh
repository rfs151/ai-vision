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
#                             (except hd: 1080p on purpose, see below)
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

convert() {   # raw_file out_name [max_width (960)] [maxrate (1200k)] [bufsize (300k)]
  # Skip existing outputs (FORCE=1 to redo). Encode to a temp file and rename: a running push keeps
  # reading the old file instead of a half-written one (overwriting in place broke live pushes).
  if [ -s "$2.mp4" ] && [ -z "${FORCE:-}" ]; then echo "data/samples/$2.mp4  exists"; return; fi
  local fps
  fps=$(ffprobe -v error -select_streams v -show_entries stream=r_frame_rate -of csv=p=0 "raw/$1" | head -1)
  fps=$(python3 -c "f=$fps; print(min(15, round(f, 2)))")
  local gop=$(python3 -c "print(int(round($fps*2)))")
  ffmpeg -v error -y -i "raw/$1" -an \
    -vf "fps=$fps,scale='min(${3:-960},iw)':-2" \
    -c:v libx264 -preset fast -crf 23 -maxrate "${4:-1200k}" -bufsize "${5:-300k}" \
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
# hd: the highway clip at its full 1920x1080, for YUV 直通 (the color conversions it saves grow with
# the picture size). More bits than the others so 1080p does not turn to mush; keyframes stay ~60 KB.
convert vehicles-2.mp4                   hd 1920 2500k 500k

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

# fall: 姿态识别 / 摔倒检测 demo, built from the UR Fall Detection Dataset (University of Rzeszów,
# http://fenix.ur.edu.pl/~mkepski/ds/uf.html, CC BY-NC-SA 4.0: non-commercial use, credit the source).
# Each clip is depth (left half) + RGB (right half), 320x240 per half, a few seconds long. We keep the
# RGB half of the front camera (cam0), upscale to 640x480, and join 9 falls with 3 daily activities
# that must NOT alarm (crouch, bend down, sit on a bed). A fall clip ends right after the person hits
# the floor, so its last frame is held for 4 s: the 摔倒 rule needs the person to stay down.
UR=http://fenix.ur.edu.pl/~mkepski/ds/data
FALL_PARTS="fall-01 adl-01 fall-03 fall-07 adl-04 fall-02 fall-05 fall-11 adl-09 fall-25 fall-29 fall-08"
if [ ! -s fall.mp4 ] || [ -n "${FORCE:-}" ]; then
  for p in $FALL_PARTS; do fetch "$UR/$p-cam0.mp4" "ur-$p-cam0.mp4"; done
  inputs=() filters="" n=0
  for p in $FALL_PARTS; do
    hold=1; [[ $p == fall-* ]] && hold=4
    inputs+=(-i "raw/ur-$p-cam0.mp4")
    filters+="[$n:v]crop=320:240:320:0,scale=640:480,fps=15,setsar=1,tpad=stop_mode=clone:stop_duration=$hold[v$n];"
    n=$((n + 1))
  done
  for ((i = 0; i < n; i++)); do filters+="[v$i]"; done
  ffmpeg -v error -y "${inputs[@]}" -an -filter_complex "${filters}concat=n=$n:v=1:a=0" \
    -c:v libx264 -preset fast -crf 23 -maxrate 1200k -bufsize 300k -pix_fmt yuv420p -r 15 -g 30 -bf 0 \
    -x264-params repeat-headers=1 fall.tmp.mp4
  mv -f fall.tmp.mp4 fall.mp4
fi
echo "data/samples/fall.mp4  $(ffprobe -v error -select_streams v -show_entries stream=width,height:format=duration -of csv=p=0 fall.mp4 | paste -sd' ')"
