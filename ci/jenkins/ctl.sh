#!/usr/bin/env bash
# Jenkins for ai-vision, started only when needed (about 0.6-1 GB RAM while running).
#   ci/jenkins/ctl.sh up              start (builds the image the first time), prints URL and admin password
#   ci/jenkins/ctl.sh build           rebuild the image (after editing Dockerfile / plugins.txt)
#   ci/jenkins/ctl.sh down            stop and remove the container (jobs and build history stay in the volume)
#   ci/jenkins/ctl.sh status | logs | password
#   ci/jenkins/ctl.sh enable-deploy   let the DEPLOY stage restart the demo on this machine (see below)
#   ci/jenkins/ctl.sh disable-deploy
#
# Deploy: Jenkins runs in Docker and reaches this machine over ssh with its own key. The key is
# added to /root/.ssh/authorized_keys with a forced command, so it can only run scripts/deploy.sh
# (fast-forward pull, rebuild, restart ai-vision-api, health check): no shell, no forwarding.
set -euo pipefail
cd "$(dirname "$0")"
ROOT=$(cd ../.. && pwd)
MARK="ai-vision-jenkins-deploy"

# Mirrors from the project .env (same ones the Docker build uses); nothing else is read from it
if [ -f "$ROOT/.env" ]; then
  for k in REGISTRY APT_MIRROR PIP_INDEX NPM_REGISTRY; do
    v=$(grep -E "^$k=" "$ROOT/.env" | tail -1 | cut -d= -f2- || true)
    [ -n "$v" ] && export "$k=$v"
  done
fi
# jenkins/jenkins is not an official "library/" image: keep only the mirror host
export JENKINS_REGISTRY="${REGISTRY%library/}"
# Mainland China: plugin downloads from the Tsinghua mirror (set JENKINS_UC_DOWNLOAD= to use the official site)
if [ -n "${REGISTRY:-}" ] && [ -z "${JENKINS_UC_DOWNLOAD+x}" ]; then
  export JENKINS_UC_DOWNLOAD=https://mirrors.tuna.tsinghua.edu.cn/jenkins
fi
[ -f .admin-password ] || (umask 077; head -c 18 /dev/urandom | base64 | tr -d '/+=' > .admin-password)
export JENKINS_ADMIN_PASSWORD=$(cat .admin-password)
mkdir -p secrets

case "${1:-status}" in
  build)    docker compose build --pull ;;   # rebuild after editing Dockerfile / plugins.txt, or to update Jenkins
  up)
    # Build only the first time: a rebuild asks the registry mirror about the base image on every start,
    # and a slow mirror (TLS handshake timeout) would then block a start that needs nothing new
    if docker image inspect ai-vision-jenkins:lts >/dev/null 2>&1; then
      docker compose up -d
    else
      docker compose up -d --build
    fi
    printf "等待 Jenkins 启动"
    for _ in $(seq 120); do
      curl -s -o /dev/null -w '%{http_code}' --noproxy '*' http://127.0.0.1:8070/login 2>/dev/null | grep -q 200 && break
      printf "."; sleep 2
    done
    echo
    # git inside Jenkins goes through the same proxy as this machine (GitHub drops direct connections at times)
    GP=${GIT_PROXY:-${https_proxy:-${HTTPS_PROXY:-}}}
    if [ -n "$GP" ] && ! printf '%s' "$GP" | grep -qE '127\.0\.0\.1|localhost'; then
      docker compose exec -T jenkins git config --global http.https://github.com/.proxy "$GP"
    fi
    echo "Jenkins: http://127.0.0.1:8070/   用户 admin   密码 $JENKINS_ADMIN_PASSWORD"
    echo "用完记得 ci/jenkins/ctl.sh down"
    ;;
  down)     docker compose down ;;
  status)   docker compose ps ;;
  logs)     docker compose logs -f --tail 100 ;;
  password) echo "$JENKINS_ADMIN_PASSWORD" ;;
  enable-deploy)
    if [ ! -f secrets/deploy_key ]; then
      ssh-keygen -q -t ed25519 -N "" -C "$MARK" -f secrets/deploy_key
      chown 1000:1000 secrets/deploy_key secrets/deploy_key.pub    # uid of the jenkins user in the image
    fi
    mkdir -p /root/.ssh && touch /root/.ssh/authorized_keys && chmod 600 /root/.ssh/authorized_keys
    sed -i "/$MARK/d" /root/.ssh/authorized_keys
    echo "command=\"$ROOT/scripts/deploy.sh\",no-pty,no-port-forwarding,no-agent-forwarding,no-X11-forwarding $(cat secrets/deploy_key.pub)" \
      >> /root/.ssh/authorized_keys
    echo "已允许 Jenkins 部署：这把 key 只能执行 $ROOT/scripts/deploy.sh"
    ;;
  disable-deploy)
    sed -i "/$MARK/d" /root/.ssh/authorized_keys 2>/dev/null || true
    rm -f secrets/deploy_key secrets/deploy_key.pub
    echo "已撤销 Jenkins 部署权限"
    ;;
  *) sed -n "2,9p" "$0"; exit 1 ;;
esac
