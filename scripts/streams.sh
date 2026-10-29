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
# Every stream runs in its own process group with a retry loop (ZLM restart -> push again in 2 s).
# PID files: data/run/push_<name>.pid   logs: data/logs/push_<name>.log
# CONF=other.conf scripts/streams.sh start   uses another list.
set -euo pipefail
cd "$(dirname "$0")/.."
CONF=${CONF:-scripts/streams.conf}
RUN=data/run
LOGS=data/logs
mkdir -p "$RUN" "$LOGS"

# "name file" pairs from the conf, optionally filtered by the names given on the command line
entries() {
  local want=" $* "
  grep -vE '^\s*(#|$)' "$CONF" | while read -r name file _; do
    if [ $# -eq 0 ] || [[ "$want" == *" $name "* ]]; then echo "$name $file"; fi
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
  # setsid: own process group, so stop can kill the loop and its ffmpeg together
  setsid bash -c '
    trap "exit 0" TERM
    while true; do
      echo "$(date "+%F %T") push start"
      scripts/push_sample.sh "$1" "$2" || true
      echo "$(date "+%F %T") push exited, retry in 2s"
      sleep 2
    done' _ "$file" "$name" >> "$LOGS/push_$name.log" 2>&1 < /dev/null &
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
    entries "${names[@]}" | while read -r n f; do start_one "$n" "$f"; done
    trap 'entries "${names[@]}" | while read -r n f; do stop_one "$n"; done; exit 0' TERM INT
    while true; do sleep 5 & wait $!; done ;;
  status)
    printf "  %-8s %-8s %s\n" NAME PID FILE
    entries | while read -r n f; do
      printf "  %-8s %-8s %s\n" "$n" "$(pid_of "$n" | grep . || echo -)" "$f"
    done ;;
  *) sed -n '2,12p' "$0"; exit 1 ;;
esac
