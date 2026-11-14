// ai-vision CI/CD (Jenkins: ci/jenkins/ctl.sh up). Stages:
//   单元测试 (pytest) → 构建前端 (vite) → 部署 (opt-in, ssh → scripts/deploy.sh) → 冒烟测试
// The job polls GitHub every 5 minutes; "Build with Parameters" runs it by hand.
pipeline {
  agent any
  options {
    timestamps()
    ansiColor('xterm')    // render colored output (vite, pytest) instead of raw escape codes
    timeout(time: 30, unit: 'MINUTES')
    disableConcurrentBuilds()
  }
  parameters {
    booleanParam(name: 'DEPLOY', defaultValue: false,
                 description: '测试通过后部署到本机（需先运行 ci/jenkins/ctl.sh enable-deploy）')
  }
  environment {
    VENV = "${env.JENKINS_HOME}/cache/venv"     // shared across builds: pip only reinstalls when requirements change
    NPM_CONFIG_CACHE = "${env.JENKINS_HOME}/cache/npm"
    API = "http://${env.DEPLOY_HOST ?: 'host.docker.internal'}:8090"
  }
  stages {
    stage('准备环境') {
      steps {
        sh '''
          set -e
          # Models and sample videos are not in git: link the host copies (mounted read-only)
          for m in /models/*; do [ -e "$m" ] && ln -sfn "$m" "worker/models/$(basename "$m")"; done
          mkdir -p data && ln -sfn /samples data/samples
          HASH=$(cat requirements.txt | sha1sum | cut -c1-12)
          if [ ! -f "$VENV/.req-$HASH" ]; then
            rm -rf "$VENV" && python3 -m venv "$VENV"
            "$VENV/bin/pip" install -q ${PIP_INDEX_URL:+-i $PIP_INDEX_URL} -r requirements.txt pytest
            touch "$VENV/.req-$HASH"
          fi
        '''
      }
    }
    stage('单元测试') {
      steps {
        sh '"$VENV/bin/python" -m pytest -q tests --junitxml=reports/pytest.xml'
      }
      post { always { junit allowEmptyResults: true, testResults: 'reports/pytest.xml' } }
    }
    stage('构建前端') {
      steps {
        dir('web') {
          sh 'npm ci --no-audit --no-fund && npx vite build'
        }
        archiveArtifacts artifacts: 'web/dist/**', fingerprint: true
      }
    }
    stage('部署') {
      when { expression { params.DEPLOY } }
      steps {
        // The key only runs scripts/deploy.sh on the host (forced command in authorized_keys)
        sh '''
          ssh -i /var/jenkins_home/deploy/deploy_key -p "${DEPLOY_PORT:-2212}" \
              -o StrictHostKeyChecking=accept-new -o BatchMode=yes \
              root@"${DEPLOY_HOST:-host.docker.internal}" "$GIT_COMMIT"
        '''
      }
    }
    stage('冒烟测试') {
      steps {
        sh '''
          set -e
          for i in $(seq 30); do curl -sf "$API/api/system" >/dev/null && break; sleep 2; done
          curl -sf "$API/api/system"; echo
          curl -sf "$API/api/devices" | python3 -c "
import json, sys
d = json.load(sys.stdin)
on = [x['stream'] for x in d if x.get('online')]
print('设备', len(d), '在线', on)
assert d, 'no devices'"
          curl -sf "$API/api/search/status" >/dev/null && echo "AI 查询接口正常"
          curl -sf "$API/" | grep -q '<div id="app"' && echo "前端页面正常"
        '''
      }
    }
  }
  post {
    success { echo "✅ ai-vision ${(env.GIT_COMMIT ?: "local").take(7)} 通过${params.DEPLOY ? '并已部署' : ''}" }
    failure { echo "❌ 构建失败，见上方日志" }
  }
}
