#!/usr/bin/env bash
# ai-vision 视频 AI 分析 —— 本机启停 / 查看状态
# 部署方式（本机）：纯 systemd 服务，不用 Docker（Docker 只用于云服务器，见 README 8.2）
#   zlm                 ZLMediaKit 流媒体（/opt/zlm，:80 :554 :1935 :8000）
#   ai-vision-api       控制服务 :8090，Worker 由它启动和管理（deploy/systemd/）
#   ai-vision-streams   测试推流，按 scripts/streams.conf 循环推测试视频（deploy/systemd/）
# 三个服务都已 enable，开机自启。
#
#   ./ctl.sh start | stop | restart | status | logs [api|streams|zlm]
#
# 公网（国内 VPS frp 转发）：http://47.117.29.74:8090/
set -euo pipefail
cd "$(dirname "$0")"
UNITS=(zlm ai-vision-api ai-vision-streams)

case ${1:-status} in
  start)   systemctl start "${UNITS[@]}" ;;
  stop)    systemctl stop ai-vision-streams ai-vision-api zlm ;;
  restart) systemctl restart "${UNITS[@]}" ;;
  status)
    for u in "${UNITS[@]}"; do printf '%-18s %s\n' "$u" "$(systemctl is-active $u)"; done
    echo "接口 http://127.0.0.1:8090/api/system -> $(curl -s --noproxy '*' -m 5 http://127.0.0.1:8090/api/system || echo 无响应)"
    curl -s --noproxy '*' -m 5 http://127.0.0.1:8090/api/devices | python3 -c '
import json, sys
for d in json.load(sys.stdin):
    if d["online"]:
        w = d.get("worker") or {}
        s = w.get("stats") or {}
        print("  %-8s 在线  worker=%s  %sfps  推理 %sms" % (d["stream"], w.get("state"), s.get("out_fps", "-"), s.get("infer_ms", "-")))
' 2>/dev/null || true
    echo "公网地址 http://47.117.29.74:8090/" ;;
  logs)
    case ${2:-api} in
      zlm)     journalctl -u zlm -f -n 100 ;;
      streams) journalctl -u ai-vision-streams -f -n 100 ;;
      *)       journalctl -u ai-vision-api -f -n 100 ;;
    esac ;;
  *) sed -n '2,10p' "$0"; exit 1 ;;
esac
