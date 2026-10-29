#!/bin/sh
# ZLMediaKit container entry: take the image's default config, apply our settings from env, start.
# Settings not listed here keep ZLM's defaults.
set -eu
SRC=/opt/media/conf/config.ini
CONF=/opt/media/conf/ai-vision.ini

: "${ZLM_SECRET:?set ZLM_SECRET in .env}"
API_PORT=${API_PORT:-8090}
HOOK=http://127.0.0.1:$API_PORT/index/hook

# set <section> <key> <value>: replace key inside [section] (the key must exist in the default config)
set_ini() {
  awk -v s="[$1]" -v k="$2" -v v="$3" '
    /^\[/ { in_s = ($0 == s) }
    in_s && index($0, k "=") == 1 { print k "=" v; done = 1; next }
    { print }
    END { if (!done) { print "zlm-start: [" s "] " k " not found" > "/dev/stderr"; exit 1 } }
  ' "$CONF" > "$CONF.tmp" && mv "$CONF.tmp" "$CONF"
}

cp "$SRC" "$CONF"
set_ini api secret "$ZLM_SECRET"
# WebRTC candidates: the public IP browsers connect to (empty: ZLM uses the local interface IP)
set_ini rtc externIP "${PUBLIC_IP:-}"
set_ini http port "${ZLM_HTTP_PORT:-80}"
set_ini http sslport "${ZLM_HTTPS_PORT:-0}"
# Newer ZLM builds only serve HTTP (players, ZLMRTCClient.js, WebRTC signaling) to private IPs by
# default; public browsers get 401. Empty = allow all. The management API still needs the secret.
set_ini http allow_ip_range "${ZLM_HTTP_ALLOW_IP:-}"
set_ini rtsp port "${ZLM_RTSP_PORT:-554}"
set_ini rtmp port "${ZLM_RTMP_PORT:-1935}"
set_ini rtc port "${ZLM_RTC_PORT:-8000}"
set_ini rtc tcpPort "${ZLM_RTC_PORT:-8000}"
set_ini rtp_proxy port "${ZLM_RTP_PORT:-0}"      # GB28181 not used: 0 = off
set_ini srt port "${ZLM_SRT_PORT:-0}"
set_ini onvif port 0
# Hooks to the control service (non-blocking ones only; ZLM keeps working if the API is down)
set_ini hook enable 1
set_ini hook on_stream_changed "$HOOK/on_stream_changed"
set_ini hook on_stream_not_found "$HOOK/on_stream_not_found"
set_ini hook on_server_started "$HOOK/on_server_started"
set_ini hook stream_changed_schemas rtsp
# HLS only when someone plays it (the UI uses WebRTC / HTTP-fMP4): no constant disk writes
set_ini protocol hls_demand 1

echo "zlm-start: http=${ZLM_HTTP_PORT:-80} rtsp=${ZLM_RTSP_PORT:-554} rtc=${ZLM_RTC_PORT:-8000} externIP=${PUBLIC_IP:-auto} hook=$HOOK"
cd /opt/media/bin
exec ./MediaServer -s default.pem -c "$CONF" -l "${ZLM_LOG_LEVEL:-2}"
