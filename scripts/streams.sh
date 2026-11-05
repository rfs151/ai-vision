#!/usr/bin/env bash
# Start / stop several simulated cameras at once (looped file pushes, see scripts/streams.conf).
#
#   scripts/streams.sh start [name...]    start all streams in the conf, or only the given ones
#   scripts/streams.sh stop  [name...]
#   scripts/streams.sh restart [name...]
#   scripts/streams.sh status
#   scripts/streams.sh run [name...]      start, then stay in the foreground; Ctrl+C / SIGTERM stops
#                                         them all (for containers: docker compose --profile demo)
#
# Which streams are on: every line of the conf is a stream, a commented-out line ("#cars ...") is
# off. The web UI (设备管理 -> 测试推流) writes its choice to data/streams.enabled (one name per
# line); when that file exists it wins over the comments. "run" without names re-reads both every
# 5 s and starts / stops pushes to match, so a change in the UI applies within 5 s.
#
# Every stream runs in its own process group with a retry loop (ZLM restart -> push again in 2 s).
# PID files: data/run/push_<name>.pid   logs: data/logs/push_<name>.log (emptied on start and
# whenever it passes 1 MB, e.g. while ZLM is down and the loop retries every 2 s)
# CONF=other.conf scripts/streams.sh start   uses another list.
set -euo pipefail
cd "$(dirname "$0")/.."
CONF=${CONF:-scripts/streams.conf}
SEL=${SEL:-data/streams.enabled}
RUN=data/run
LOGS=data/logs
mkdir -p "$RUN" "$LOGS"

# All "name file on|off" lines of the conf, commented-out ones included (they are "off")
catalog() {
  sed -nE 's/^\s*(#?)\s*([A-Za-z0-9_-]+)\s+(\S+\.(mp4|mkv|mov|flv|ts))\s*$/\2 \3 \1/p' "$CONF" |
    while read -r name file off; do echo "$name $file $([ -n "$off" ] && echo off || echo on)"; done
}

is_enabled() {   # from data/streams.enabled when it exists, else the conf comments
  if [ -f "$SEL" ]; then grep -qxF "$1" "$SEL"; else [ "$2" = on ]; fi
}

# "name file" pairs: the given names (any line of the conf), or all enabled streams when none given
entries() {
  local want=" $* "
  catalog | while read -r name file state; do
    if [ $# -gt 0 ]; then
      if [[ "$want" == *" $name "* ]]; then echo "$name $file"; fi
    elif is_enabled "$name" "$state"; then echo "$name $file"; fi
  done
}

# Make the running pushes match the selection (run mode, every 5 s)
sync_all() {
  catalog | while read -r name file state; do
    if is_enabled "$name" "$state"; then
      [ -n "$(pid_of "$name")" ] || start_one "$name" "$file"
    elif [ -n "$(pid_of "$name")" ]; then
      stop_one "$name"
    fi
  done
}

pid_of() {   # running pid of a stream, empty if not running
  local f="$RUN/push_$1.pid"
  [ -f "$f" ] && kill -0 "$(cat "$f")" 2>/dev/null && cat "$f" || true
}

start_one() {
  local name=$1 file=$2
  if [ -n "$(pid_of "$name")" ]; then echo "  $name: already running"; return; fi
  if [ ! -s "$file" ]; then echo "  $name: $file not found (run scripts/get_samples.sh)"; return; fi
  local log="$LOGS/push_$name.log"
  : > "$log"
  # setsid: own process group, so stop can kill the loop and its ffmpeg together.
  # The log is opened in append mode, so truncating it in place is safe while ffmpeg writes to it.
  setsid bash -c '
    trap "exit 0" TERM
    while true; do
      if [ "$(stat -c %s "$3" 2>/dev/null || echo 0)" -gt 1048576 ]; then
        : > "$3"; echo "$(date "+%F %T") log over 1 MB, emptied"
      fi
      echo "$(date "+%F %T") push start"
      scripts/push_sample.sh "$1" "$2" || true
      echo "$(date "+%F %T") push exited, retry in 2s"
      sleep 2
    done' _ "$file" "$name" "$log" >> "$log" 2>&1 < /dev/null &
  echo $! > "$RUN/push_$name.pid"
  echo "  $name: started (pid $!)  ${ZLM_RTSP:-rtsp://127.0.0.1}/live/$name  <- $file"
}

stop_one() {
  local name=$1 pid
  pid=$(pid_of "$name")
  if [ -z "$pid" ]; then echo "  $name: not running"; rm -f "$RUN/push_$name.pid"; return; fi
  kill -TERM -- "-$pid" 2>/dev/null || true     # whole group: loop + push_sample + ffmpeg
  for _ in 1 2 3 4 5; do kill -0 "$pid" 2>/dev/null || break; sleep 0.5; done
  kill -KILL -- "-$pid" 2>/dev/null || true
  rm -f "$RUN/push_$name.pid"
  echo "  $name: stopped"
}

cmd=${1:-status}; shift || true
case $cmd in
  start)   entries "$@" | while read -r n f; do start_one "$n" "$f"; done ;;
  stop)    entries "$@" | while read -r n f; do stop_one "$n"; done ;;
  restart) entries "$@" | while read -r n f; do stop_one "$n"; start_one "$n" "$f"; done ;;
  run)
    names=("$@")
    # stop everything this script may have started (all conf lines), not only what is enabled now
    trap 'catalog | while read -r n f _; do [ -z "$(pid_of "$n")" ] || stop_one "$n"; done; exit 0' TERM INT
    if [ ${#names[@]} -gt 0 ]; then          # fixed list from the command line
      entries "${names[@]}" | while read -r n f; do start_one "$n" "$f"; done
      while true; do sleep 5 & wait $!; done
    fi
    while true; do sync_all; sleep 5 & wait $!; done ;;
  status)
    printf "  %-8s %-4s %-8s %s\n" NAME ON PID FILE
    catalog | while read -r n f st; do
      printf "  %-8s %-4s %-8s %s\n" "$n" "$(is_enabled "$n" "$st" && echo yes || echo -)" "$(pid_of "$n" | grep . || echo -)" "$f"
    done ;;
  *) sed -n '2,18p' "$0"; exit 1 ;;
esac
