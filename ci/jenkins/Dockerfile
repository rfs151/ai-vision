# Jenkins for ai-vision (see ci/jenkins/ctl.sh). Official LTS image + what the Jenkinsfile runs:
# python3 + venv (pytest), ffmpeg (encoder tests), Node.js + npm (vite build), ssh client (deploy), plugins from plugins.txt.
ARG REGISTRY=
FROM ${REGISTRY}jenkins/jenkins:lts-jdk21
ARG APT_MIRROR=
ARG JENKINS_UC_DOWNLOAD=
USER root
RUN if [ -n "$APT_MIRROR" ]; then sed -i "s|deb.debian.org|$APT_MIRROR|g" /etc/apt/sources.list.d/debian.sources; fi \
 && apt-get update \
 && apt-get install -y --no-install-recommends python3 python3-venv python3-pip nodejs npm openssh-client curl ffmpeg \
 && rm -rf /var/lib/apt/lists/*
USER jenkins
COPY plugins.txt /usr/share/jenkins/ref/plugins.txt
RUN JENKINS_UC_DOWNLOAD="${JENKINS_UC_DOWNLOAD}" jenkins-plugin-cli --plugin-file /usr/share/jenkins/ref/plugins.txt
