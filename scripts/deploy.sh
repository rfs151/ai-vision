#!/usr/bin/env bash
# Deploy the pushed main branch on this machine. Called by Jenkins over ssh (ci/jenkins/ctl.sh
# enable-deploy limits its key to this script), or by hand: scripts/deploy.sh [commit]
#   fast-forward pull → pip install if requirements changed → build web → restart ai-vision-api → health check
set -euo pipefail
cd "$(dirname "$0")/.."
unset http_proxy https_proxy HTTP_PROXY HTTPS_PROXY
want=${SSH_ORIGINAL_COMMAND:-${1:-}}
want=$(printf '%s' "$want" | tr -cd '0-9a-f' | cut -c1-40)    # only a commit hash is accepted

before=$(git rev-parse HEAD)
git -c http.proxy= fetch -q origin main
git merge -q --ff-only origin/main          # refuses (and fails the build) if this checkout has diverged
now=$(git rev-parse HEAD)
if [ -n "$want" ] && ! git merge-base --is-ancestor "$want" "$now"; then
  echo "commit $want 不在 origin/main 上" >&2; exit 1
fi
echo "部署 ${before:0:7} → ${now:0:7}"

if ! git diff --quiet "$before" "$now" -- requirements.txt; then
  .venv/bin/pip install -q -r requirements.txt
fi
if [ "$before" != "$now" ] || [ ! -d web/dist ]; then
  scripts/build_web.sh >/dev/null
fi
systemctl restart ai-vision-api
for _ in $(seq 30); do
  curl -sf --noproxy '*' -m 3 http://127.0.0.1:8090/api/system >/dev/null && { echo "ai-vision-api 已重启，健康检查通过"; exit 0; }
  sleep 2
done
echo "ai-vision-api 健康检查超时" >&2; journalctl -u ai-vision-api -n 30 --no-pager >&2; exit 1
