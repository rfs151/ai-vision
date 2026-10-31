# ai-vision：基于 ZLMediaKit 的智能视频分析告警平台

实时接入摄像头 / 直播流，检测人和车辆，按规则（区域入侵、越线、逗留、人数超限）告警，保存截图和前后 10 秒录像，并推送到企业微信、钉钉。

这份文档记录整个项目从零开始的全过程：ZLMediaKit 的编译、部署和配置，ai-vision 的软件安装、功能设计、遇到的问题和定位过程，以及本机和云服务器的部署运维。

**目录**

1. [项目概览](#1-项目概览)
2. [环境与软件安装](#2-环境与软件安装)
3. [ZLMediaKit 配置修改](#3-zlmediakit-配置修改)
4. [快速开始（本机）](#4-快速开始本机)
5. [功能设计](#5-功能设计)
6. [问题定位记录](#6-问题定位记录)
7. [性能](#7-性能)
8. [部署与运维](#8-部署与运维)

---

## 1. 项目概览

### 架构

```
摄像头 / OBS / ffmpeg ──▶ ZLMediaKit ──RTSP──▶ AI Worker ──画框后的流──▶ ZLMediaKit ──▶ 浏览器
                         (live/<name>)            │                       (ai/<name>)    WebRTC / FLV / fMP4
                              │ Hook              ├─ 解码（每一帧）→ 按时间外推跟踪框 → 画框 → x264 编码
                              ▼                   └─ 分析线程（5fps）：YOLO → ByteTrack → 规则 → 告警
                    控制服务 FastAPI :8090  ◀──── 拉取规则（热更新）/ 上报告警和截图 / 心跳 ────┘
                    设备、规则、告警（SQLite） ──▶ WebSocket /ws 实时推送给浏览器
                    Worker 管理：流上线启动、下线停止，崩溃或无心跳时自动重启
```

- **ZLMediaKit**：负责所有流媒体工作（接入、协议转换、分发）。
- **AI Worker**：一路视频一个进程。PyAV 解码；独立线程按固定帧率（默认 5fps）对最新一帧做 OpenVINO YOLO 推理；每一帧都画上最近一次的检测结果（按卡尔曼速度外推），再用 ffmpeg（x264 zerolatency）推回 ZLM。
- **控制服务**：设备、规则、告警的管理，Worker 进程管理，WebSocket 实时推送，同时提供网页。
- 三者独立部署、独立扩展：ZLM 只管视频流，Worker 只管计算，控制服务只管业务。

| 模块 | 文件 | 说明 |
|---|---|---|
| 检测 | `worker/detector.py` | OpenVINO YOLO，手写 letterbox 和 NMS，运行时不需要 torch |
| 跟踪 | `worker/tracker.py` | ByteTrack：高分框和低分框两轮匹配（匈牙利算法），卡尔曼滤波按实际时间间隔预测 |
| 规则 | `worker/rules.py` | 区域入侵、逗留、人数超限、越线（区分方向） |
| 录像 | `worker/clip.py` | 内存盘环形切片，告警时拼接前后 10 秒 |
| 告警上报 | `worker/apiclient.py` / `worker/alarm.py` | 后台线程上传（不阻塞分析线程）；单独运行时写到本地 `data/alarms/` |
| 控制服务 | `api/` | FastAPI + SQLAlchemy；`manager.py` 管理 Worker 进程，`routers/hooks.py` 处理 ZLM 回调，`notify.py` 推送 |
| 前端 | `web/` | Vue3 + Vite + Element Plus |
| 部署 | `Dockerfile`、`docker-compose.yml`、`deploy/zlm-start.sh` | Docker Compose 一键部署 |

### 技术选型

| 模块 | 选择 | 理由 |
|---|---|---|
| 流媒体 | ZLMediaKit | 多协议接入和转换、Hook 回调、WebRTC |
| 控制服务 | Python + FastAPI | 和 AI 部分同一种语言，开发快，自带 Swagger 文档 |
| 视频解码 | PyAV（基于 FFmpeg） | 比 OpenCV 的 `VideoCapture` 更可控，能拿到时间戳、能处理断线重连 |
| 检测模型 | YOLO11n，导出成 OpenVINO 格式 | 机器没有 NVIDIA 显卡（i5-13500H），OpenVINO 在 Intel CPU 上比 PyTorch 快很多 |
| 目标跟踪 | 自己实现的 ByteTrack | 越线、逗留这类规则必须依赖稳定的跟踪 ID |
| 数据库 | SQLite | 演示项目，一个文件，零运维 |
| 前端 | Vue3 + Element Plus + ZLM 自带的 `ZLMRTCClient.js` | |
| 部署 | Docker Compose | 一条命令启动全部服务 |

### 开发进度

| 阶段 | 内容 | 提交 |
|---|---|---|
| ✅ M1 | 单路：拉流 → YOLO 检测 → 画框后回推 | `d2b5c94` |
| ✅ M2 | ByteTrack 跟踪 + 规则（区域、越线、逗留）+ 告警去重 | `dc47e6d` |
| ✅ M3 | FastAPI 控制服务：设备、规则管理，通过 Hook 自动启停 Worker | `f3e3cb3` |
| ✅ M4 | Vue3 前端：多画面分屏、画区域、实时告警 | `2ffa7ab` |
| ✅ 修复 | Worker 不恢复、B 帧、fMP4 卡死、WebRTC 卡住（MTU、SPS/PPS）、多路推理变慢 | `c143985` … `3007925` |
| ✅ M5 | 告警截图、前后 10 秒录像、企业微信和钉钉推送 | `86b7007` |
| ✅ M6 | 前端叠加画框、解码延迟修复、压测脚本、Docker Compose、云服务器部署 | `d1adc4f`、`8cb13be` |

---

## 2. 环境与软件安装

### 2.1 开发机

| 项目 | 配置 |
|---|---|
| 系统 | Windows + WSL2 Ubuntu 24.04 |
| CPU | Intel i5-13500H（12 核 16 线程），**没有 NVIDIA 显卡**，所以推理用 CPU + OpenVINO |
| WSL 网络 | 镜像模式（`C:\Users\<用户>\.wslconfig`） |

```ini
[wsl2]
networkingMode=mirrored    # Windows 和 WSL 共用 IP 和端口，Windows 浏览器直接用 127.0.0.1 访问
dnsTunneling=true
autoProxy=false
firewall=false
```

镜像模式的影响（后面多个问题都和它有关）：
- Windows 和 WSL 共用端口：8080 被 Windows 上的 VS Code 占用，所以控制服务用 **8090**。
- 本机通信走 `loopback0` 网卡，MTU 1500，超过的 UDP 包会被直接丢掉（见 [6.4](#64-webrtc-播放半秒后卡住rtp-包超过-mtu)）。
- WebRTC 回包到 Windows 浏览器要走 `127.0.0.1`（见 [6.2](#62-webrtc-页面一直转圈externip)）。

### 2.2 软件源和代理

开发机上有一个给 Claude 用的 HTTP 代理（12334 端口）。**执行主机命令不走这个代理**，统一使用国内镜像；只有直连下载失败（GitHub、国外素材站）时才用代理重试。

| 软件 | 源 |
|---|---|
| apt | 阿里云：`https://mirrors.aliyun.com/ubuntu/`（原配置备份在 `/etc/apt/sources.list.d/ubuntu.sources.bak`） |
| pip | `https://mirrors.aliyun.com/pypi/simple/` |
| torch（只在导出模型时用） | `https://mirrors.aliyun.com/pytorch-wheels/cpu/`（CPU 版，避免下载几个 GB 的 CUDA 版） |
| npm | `https://registry.npmmirror.com` |
| Docker 镜像 | `docker.1ms.run`（加在镜像名前面，见 `.env.example`） |
| 模型 | ModelScope：`https://modelscope.cn/models/Ultralytics/YOLO11/resolve/master/yolo11n.pt` |

换 apt 源（Ubuntu 24.04 是 deb822 格式）：

```bash
cp /etc/apt/sources.list.d/ubuntu.sources /etc/apt/sources.list.d/ubuntu.sources.bak
sed -i -e 's#http://archive.ubuntu.com/ubuntu/#https://mirrors.aliyun.com/ubuntu/#' \
       -e 's#http://security.ubuntu.com/ubuntu/#https://mirrors.aliyun.com/ubuntu/#' \
       /etc/apt/sources.list.d/ubuntu.sources
apt-get update
```

执行主机命令时去掉代理变量：

```bash
env -u http_proxy -u https_proxy -u HTTP_PROXY -u HTTPS_PROXY apt-get install -y ffmpeg
```

### 2.3 编译 ZLMediaKit（带 WebRTC）

源码在 `/root/proj/ZLMediaKit`（master 分支）。

```bash
cd /root/proj/ZLMediaKit
git submodule update --init                 # 1. 拉子模块（clone 时没加 --recursive）

apt install -y build-essential cmake libssl-dev pkg-config   # 2. OpenSSL：没有它 WebRTC 会被静默关掉

# 3. libsrtp 2.3，要带 OpenSSL 支持（WebRTC 的 SRTP 加密）
git clone -b v2.3.0 --depth 1 https://github.com/cisco/libsrtp.git /opt/libsrtp
cd /opt/libsrtp && ./configure --enable-openssl && make -j16 && make install   # → /usr/local/lib/libsrtp2.a

# 4. 编译
cd /root/proj/ZLMediaKit && rm -rf build && mkdir build && cd build
cmake .. -DCMAKE_BUILD_TYPE=Release -DENABLE_TESTS=OFF
make -j16                                    # 产物在 release/linux/Release/
```

**检查是否真的编进了 WebRTC**（cmake 找不到 OpenSSL 时只给一个警告，不会报错）：

```bash
grep -E "OPENSSL_SSL_LIBRARY|SRTP_LIBRARIES|ENABLE_WEBRTC" build/CMakeCache.txt    # 不能是 NOTFOUND
grep MK_COMPILE_DEFINITIONS build/CMakeCache.txt     # 应包含 ENABLE_OPENSSL;ENABLE_WEBRTC
ldd release/linux/Release/MediaServer | grep ssl     # 应看到 libssl.so.3
```

当前产物：`ENABLE_VERSION;ENABLE_OPENSSL;ENABLE_SRT;ENABLE_WEBRTC;ENABLE_MP4;ENABLE_RTPPROXY;ENABLE_HLS`，链接 `libssl.so.3`，`libsrtp2.a` 静态链接。

### 2.4 部署 ZLMediaKit 到本机（systemd）

```bash
mkdir -p /opt/zlm /var/log/zlm
cd /root/proj/ZLMediaKit/release/linux/Release
cp -r MediaServer config.ini default.pem www /opt/zlm/     # test_*、*.a、*.debug 不用拷
```

`/etc/systemd/system/zlm.service`：

```ini
[Unit]
Description=ZLMediaKit
After=network.target

[Service]
WorkingDirectory=/opt/zlm
ExecStart=/opt/zlm/MediaServer -c /opt/zlm/config.ini -s /opt/zlm/default.pem --log-dir /var/log/zlm
Restart=always
RestartSec=3
LimitNOFILE=1048576
LimitCORE=infinity

[Install]
WantedBy=multi-user.target
```

```bash
systemctl daemon-reload && systemctl enable --now zlm
journalctl -u zlm -f                      # 或看 /var/log/zlm/
# 验证（config.ini 是 CRLF 换行，取 secret 时要去掉 \r）
curl "http://127.0.0.1/index/api/getServerConfig?secret=$(grep -m1 '^secret=' /opt/zlm/config.ini | cut -d= -f2 | tr -d '\r')"
```

- 用 systemd 时不要加 `-d`，由 systemd 负责守护和重启。
- 改配置后可以 `systemctl restart zlm`，也可以 `kill -HUP` 热加载。**两种方式都会断开所有推流**，`scripts/streams.sh` 会自动重推，其他推流端要手动重推。

### 2.5 ai-vision 的依赖

| 软件 | 版本 | 安装 | 用途 |
|---|---|---|---|
| ffmpeg | 6.1.1 | `apt install -y ffmpeg` | 推测试流、Worker 输出编码、录像切片和拼接 |
| Python | 3.12 | 系统自带 | |
| 运行时依赖 | 见 `requirements.txt` | `python3 -m venv .venv && .venv/bin/pip install -i <pip源> -r requirements.txt` | openvino、numpy、opencv-headless、av、scipy、httpx、fastapi、uvicorn、sqlalchemy |
| 导出模型依赖 | 见 `requirements-export.txt` | `scripts/export_model.sh` 自动建 `.venv-export` | ultralytics + CPU 版 torch，只用一次 |
| Node.js | 18+（本机 22） | | 构建前端（`scripts/build_web.sh`） |
| Google Chrome | 156 | 手动安装 deb 包 | 无头浏览器端到端播放测试（`web/e2e/playtest.mjs`） |
| Docker | 29 + compose v2 + buildx | `apt install -y docker.io docker-compose-v2 docker-buildx` | 容器部署 |

```bash
cd /root/proj/ai-vision
python3 -m venv .venv
.venv/bin/pip install -i https://mirrors.aliyun.com/pypi/simple/ -r requirements.txt
.venv/bin/pip install -i https://mirrors.aliyun.com/pypi/simple/ -r requirements-dev.txt   # pytest
# 下载 yolo11n.pt 到 worker/models/，然后导出（只需一次）
scripts/export_model.sh yolo11n          # → worker/models/yolo11n_openvino_model/
```

运行时**不需要 torch**：前处理（letterbox）和后处理（解码输出、NMS）都是自己实现的，和 ultralytics 官方推理结果一致（类别、置信度、框坐标相同）。

---

## 3. ZLMediaKit 配置修改

### 3.1 本机 `/opt/zlm/config.ini` 的改动

和仓库默认的 `conf/config.ini` 相比：

| 段 | 配置项 | 默认 | 改成 | 原因 |
|---|---|---|---|---|
| `[api]` | `secret` | 仓库自带的值 | 随机串 | 默认值公开在仓库里 |
| `[rtc]` | `externIP` | 空 | `127.0.0.1,172.20.10.3` | WebRTC 告诉浏览器去连哪个 IP。原来误填了一个外网 IP，页面一直转圈；WSL 镜像模式下本机浏览器要走 `127.0.0.1`，局域网设备走网卡 IP（见 [6.2](#62-webrtc-页面一直转圈externip)） |
| `[hook]` | `enable` | `0` | `1` | 开启回调 |
| `[hook]` | `on_stream_changed` | 空 | `http://127.0.0.1:8090/index/hook/on_stream_changed` | 流上线、下线时启停 Worker |
| `[hook]` | `on_stream_not_found` | 空 | `http://127.0.0.1:8090/index/hook/on_stream_not_found` | 播放还没拉取的摄像头时按需拉流 |
| `[hook]` | `on_server_started` | 空 | `http://127.0.0.1:8090/index/hook/on_server_started` | ZLM 重启后恢复拉流代理 |
| `[hook]` | `stream_changed_schemas` | `rtsp/rtmp/fmp4/ts/hls/hls.fmp4` | `rtsp` | 每路流只通知一次（默认每种协议各通知一次） |
| `[protocol]` | `hls_demand` | `0` | `1` | 默认每路流都实时写 HLS 切片，12 路一直写盘约 1.2 MB/s；页面用 WebRTC 和 fMP4，改成有人播 HLS 才生成，降到约 0.3 MB/s |

- **试过又改回的**：`[rtc] preferred_tcp=1`（只调高 TCP 候选的优先级，Chrome 仍走 UDP，没用）。
- **备份**：每次改动前都留了备份：`config.ini.bak`（externIP）、`.bak-m3`（Hook）、`.bak-tcp*`（preferred_tcp 试验）、`.bak-hls`（hls_demand）。
- **注意 CRLF**：这个 `config.ini` 是 Windows 换行。用 `sed` 修改时要匹配行尾的 `\r`，读取 secret 时要 `tr -d '\r'`，否则 secret 末尾多一个 `\r`，验证失败。

只开启"通知类"的 Hook。推流和播放鉴权（`on_publish`、`on_play`）不开：鉴权类 Hook 调用失败时 ZLM 会拒绝请求，控制服务一旦挂掉，所有推流和播放都会失败。`on_publish` 接口已经实现（只允许本机推送到 `ai/` 应用），需要时手动开启。

### 3.2 Docker 里的 ZLM（`deploy/zlm-start.sh`）

容器用官方镜像 `zlmediakit/zlmediakit:master`。启动脚本复制镜像自带的配置，按 `.env` 改写后再启动，不需要手工维护一份 `config.ini`：

| 配置 | 值 |
|---|---|
| `api.secret` | `ZLM_SECRET` |
| `rtc.externIP` | `PUBLIC_IP` |
| `http.port` / `rtsp.port` / `rtmp.port` / `rtc.port` / `rtc.tcpPort` | `ZLM_HTTP_PORT`（80）、`ZLM_RTSP_PORT`（554）、`ZLM_RTMP_PORT`（1935）、`ZLM_RTC_PORT`（8000） |
| `http.allow_ip_range` | **清空**（新版 ZLM 默认只允许内网 IP 访问 HTTP，公网浏览器得到 401，见 [6.12](#612-云服务器上浏览器播放失败http-401)） |
| `rtp_proxy.port` / `srt.port` / `onvif.port` | `0`，不用的服务都关掉 |
| `hook.*` | 同本机，地址里的端口是 `API_PORT` |
| `protocol.hls_demand` | `1` |

### 3.3 ZLM 基础使用

推流后，ZLM 不转码，只把同一份数据重新封装成各种协议，任何协议推上去都可以用任何协议拉下来：

| 用途 | 地址（app=`live`，stream=`test`） |
|---|---|
| RTMP 推流 | `ffmpeg -re -stream_loop -1 -i test.mp4 -c copy -f flv rtmp://127.0.0.1/live/test` |
| RTSP 推流（带 Opus 音频，推荐） | `ffmpeg -re -stream_loop -1 -i test.mp4 -c:v copy -bsf:v h264_mp4toannexb -c:a libopus -ar 48000 -ac 2 -b:a 96k -f rtsp -rtsp_transport tcp -pkt_size 1200 rtsp://127.0.0.1/live/test` |
| WebRTC 播放 | `http://127.0.0.1/webrtc/?app=live&stream=test&type=play` |
| HTTP-fMP4 播放 | `http://127.0.0.1/live/test.live.mp4`（Chrome 地址栏直接打开） |
| HTTP-FLV / HLS / RTSP | `.live.flv`（需 mpegts.js）、`/live/test/hls.m3u8`、`rtsp://127.0.0.1/live/test`（VLC） |
| 管理页 | `http://127.0.0.1/webassist/`（需要 secret） |
| OBS 推流 | 服务器 `rtmp://127.0.0.1/live`，推流码 `test`；OBS 30+ 也可以用 WHIP：`http://127.0.0.1/index/api/whip?app=live&stream=test` |

- 推流地址里的 IP 是"推流端能访问到的 ZLM 地址"，和 `externIP` 无关；`externIP` 只用于 WebRTC。
- **WebRTC 没声音**：浏览器 WebRTC 只支持 Opus 和 G.711，不支持 AAC，开源版 ZLM 也不做音频转码。推流时用 `-c:a libopus` 转成 Opus，并改用 RTSP 推流（传统 RTMP/FLV 不支持 Opus）。
- **`-bsf:v h264_mp4toannexb -pkt_size 1200`** 两个参数对 WebRTC 播放很重要，原因见 [6.4](#64-webrtc-播放半秒后卡住rtp-包超过-mtu) 和 [6.5](#65-windows-浏览器里-webrtc-仍然卡关键帧前没有-spspps)。

---

## 4. 快速开始（本机）

```bash
cd /root/proj/ai-vision

# 1. 环境和模型（见 2.5）
python3 -m venv .venv && .venv/bin/pip install -i https://mirrors.aliyun.com/pypi/simple/ -r requirements.txt
scripts/export_model.sh yolo11n

# 2. 构建前端，启动控制服务（ZLM 需要先配置好 Hook，见 3.1）
scripts/build_web.sh                     # 生成 web/dist
scripts/run_api.sh                       # 管理页面 http://127.0.0.1:8090/，接口文档 /docs

# 3. 推测试视频：API 会自动注册设备并启动 Worker
scripts/get_samples.sh                   # 下载并转码 6 段测试视频到 data/samples/
scripts/streams.sh start                 # 按 scripts/streams.conf 一次推 6 路 → live/<name>，输出 ai/<name>
scripts/streams.sh status                # 查看；stop / restart 后面可以跟流名，只操作其中几路
# 只推一路：scripts/push_sample.sh data/samples/pedestrians.mp4 ped

# 4. 下发规则：在页面「规则配置」里直接画；也可以调接口（Worker 不需要重启，5 秒内生效）
curl -X PUT -H "Content-Type: application/json" --data @rules/pedestrians.json \
     http://127.0.0.1:8090/api/devices/1/rules

# 5. 查看
#   http://127.0.0.1:8090/#/wall                                监控大屏
#   http://127.0.0.1/ai/ped.live.mp4                            带检测框的画面
#   http://127.0.0.1:8090/api/workers                           Worker 状态（帧率、推理耗时、区域人数、越线计数）

# 单元测试（37 个）
.venv/bin/python -m pytest -q tests
```

不用 API 也能单独运行 Worker：`scripts/run_worker.sh ped --rules rules/pedestrians.json`，告警写到 `data/alarms/ped/`。

Worker 参数：`--det-fps 5`（每秒检测帧数）、`--conf 0.4`（跟踪用的高分阈值，0.1 到这个值之间的低分框只用于维持已有目标）、`--classes person,car`（留空表示全部 80 类）、`--threads 2`（OpenVINO 推理线程数）、`--device CPU|GPU`、`--clip-pre 10 --clip-post 10`、`--no-clips`、`--rules`、`--out`。

### 测试视频

`scripts/get_samples.sh`：访问不了 GitHub 时设置 `PROXY=http://host:port`，直连失败才走代理；已存在的文件跳过（`FORCE=1` 强制重做），先写临时文件再改名，不会破坏正在推流的文件。

| 流名 | 来源 | 画面 | 适合测试 |
|---|---|---|---|
| ped | OpenCV `vtest.avi` | 广场行人，768x576 10fps | 越线、区域 |
| street | Intel `person-bicycle-car-detection` | 街道上的人、自行车、汽车 | 多类别 |
| cars | Roboflow `vehicles-2` | 高速路车流，平均每帧约 13 辆 | 车辆、越线计数 |
| people | Intel `people-detection` | 室内人员走动 | 人数超限 |
| worker | Intel `worker-zone-detection` | 工厂工人 | 危险区域入侵 |
| store | Intel `store-aisle-detection` | 超市过道 | 逗留 |

转码统一为：宽度不超过 960、帧率不超过 15fps、无 B 帧、每个关键帧带 SPS/PPS、限制关键帧大小（`-crf 23 -maxrate 1200k -bufsize 300k -g 2*fps -bf 0 -x264-params repeat-headers=1`，原因见第 6 节）。

`scripts/streams.sh`：每一路单独一个进程组，推流断了（比如 ZLM 重启）2 秒后自动重推；PID 文件在 `data/run/`，日志在 `data/logs/push_<name>.log`；`run` 模式在前台运行（给 Docker 用）。新推上来的 `live/*` 流由 Hook 或者每 3 秒一次的对账循环自动登记成设备，默认开启 AI。

---

## 5. 功能设计

### 5.1 Worker：检测、跟踪、画框（M1、M2）

```
PyAV 解码线程（每一帧）──最新帧──▶ 分析线程（5fps）：YOLO → ByteTrack → 规则 → 告警 / 录像请求 / 前端画框数据
        │                                              │
        └── 每一帧：取最近一次跟踪结果，按卡尔曼速度外推到当前帧时间 → 画框 → ffmpeg 编码推回 ai/<name>
```

- **抽帧检测**：视频 10–25fps，检测只需要 5fps，算力降到几分之一；中间的帧用跟踪器的速度外推框的位置，画面上的框依然连贯（最多外推 0.5 秒）。
- **只检测最新帧**：分析线程总是取最新的一帧，算不过来时丢中间帧，不会越积越多、延迟越来越大。
- **断线重连**：拉流断开后按指数退避重连。
- **解码用 SLICE 线程模式**：AUTO 模式会让每帧晚出来 1.4 秒（见 [6.11](#611-前端画框总跟在人后面解码延迟-14-秒)）。
- **OpenVINO**：每路 2 个推理线程，关闭绑核（见 [6.8](#68-6-路同时跑推理从-40-ms-变成-380-msopenvino-绑核)）。

### 5.2 告警规则

规则通过 API（`PUT /api/devices/{id}/rules`）、页面或 JSON 文件（`--rules`）配置。坐标按画面宽高归一化到 0~1，和分辨率无关。示例见 `rules/pedestrians.json`。

| 规则 | 配置 | 触发条件 |
|---|---|---|
| 区域入侵 | `zones[].intrusion` | 目标的**脚底点**（检测框底边中点）连续 `confirm_frames` 次检测都在区域内。每个目标只触发一次 |
| 逗留 | `zones[].dwell_sec` | 确认进入区域后停留超过 N 秒 |
| 人数超限 | `zones[].max_count` | 区域内已确认的目标数超过 N |
| 越线 | `lines[]` | 目标前后两次的脚底点连成的线段和规则线相交，按方向计入 IN 或 OUT。`alarm_on: in/out/both` 决定是否告警，`null` 表示只计数 |

去重（不处理的话，同一个人每秒会触发 5 次告警）：
1. **连续帧确认**：过滤误检造成的闪烁。
2. **每个目标只告警一次**：按跟踪 ID 记录状态。
3. **规则冷却**：同一条规则的同一种告警，在 `cooldown_sec` 内只推送一次。
4. **越线防抖**：同一个目标连续两次同方向越线只算一次。

画面上：IN 箭头指向的方向表示"进"；告警时相关的目标和区域变红，底部显示 3 秒红色横幅。截图直接用触发告警的那一帧，比调 ZLM 的 `getSnap` 更准。

### 5.3 控制服务（M3）

| 事件 | 处理 |
|---|---|
| 有流推到 `live/<流名>` | `on_stream_changed`：不认识的流自动注册成设备（`AUTO_APPS=live`），然后启动 Worker |
| 流下线 | 停止对应的 Worker |
| 播放一个还没拉取的摄像头 | `on_stream_not_found`：对设置了 `source_url` 的设备调用 ZLM 的 `addStreamProxy` 按需拉流 |
| ZLM 重启 | `on_server_started`：重新添加拉流代理；控制服务自己启动时也同步一次 |
| Hook 丢失 | **每 3 秒和 ZLM 的 `getMediaList` 对账**，补启或停止 Worker、补登记新流。Hook 只是加快响应，不依赖它（见 [6.6](#66-重新推流后-worker-不恢复)） |
| Worker 崩溃 | 按 2、4、8……30 秒的间隔递增等待后重启 |
| Worker 卡死 | 超过 20 秒没有心跳就杀掉再重启（`HEARTBEAT_TIMEOUT`） |
| 修改规则 | 规则版本号加 1；Worker 每 5 秒检查版本号，有变化就重建规则引擎（热更新） |

| 接口 | 说明 |
|---|---|
| `GET/POST /api/devices`，`GET/PATCH/DELETE /api/devices/{id}` | 设备管理。返回在线状态、Worker 状态、原始流和 AI 流的各种播放地址 |
| `GET/PUT /api/devices/{id}/rules` | 规则（Pydantic 校验，比如多边形至少 3 个点） |
| `POST /api/devices/{id}/worker/{start\|stop\|restart}` | 手动控制 Worker |
| `GET /api/alarms?stream=&type=&status=&since=&until=&page=&size=`，`GET /api/alarms/{id}` | 告警查询 |
| `PATCH /api/alarms/{id}` | 处理告警：`new` / `ack` 已确认 / `false` 误报 |
| `GET /api/alarms/stats?hours=24` | 按类型、设备统计 |
| `GET/PUT /api/notify`，`POST /api/notify/test` | 推送配置和测试 |
| `GET /api/workers`，`GET /api/system` | Worker、系统状态 |
| `WS /ws` | 实时推送 `alarm`、`alarm_clip`、`worker`、`stream`、`rules`、`device` 事件；订阅后推送 `tracks` |
| `/api/internal/*` | 给 Worker 用：拉规则、上报告警和录像、心跳。只允许本机访问，或设置 `INTERNAL_TOKEN` 后凭令牌访问 |

配置用环境变量，见 `api/config.py`：`ZLM_API`、`ZLM_SECRET`（没设置时从 `ZLM_CONFIG=/opt/zlm/config.ini` 读取）、`AUTO_APPS`、`MAX_WORKERS`、`WORKER_ARGS`、`DATA_DIR`、`DB_URL`、`PLAY_HTTP_PORT`、`PLAY_RTSP_PORT` 等。

安全细节：
- **不在日志里记录 secret**：httpx 默认在 INFO 级别记录完整 URL，而 ZLM 的 secret 是 URL 参数，所以把 httpx 的日志级别调到了 WARNING。
- **本机请求不走代理**：httpx 客户端都设置了 `trust_env=False`，避免环境变量里的代理拦截本地请求。
- **时间用本地时间**：SQLite 的 `CURRENT_TIMESTAMP` 是 UTC，统计和筛选会差 8 小时，改成本地时间。
- **数据库升级**：启动时 `api/db.py` 的 `migrate()` 自动补上缺少的字段，旧数据库不用删（正式项目应该用 Alembic）。

### 5.4 前端（M4）

Vue3 + Vite + Element Plus。构建后由控制服务提供页面，整个系统只需要一个端口 8090。开发时运行 `cd web && npm run dev`（5173 端口，代理 `/api`、`/ws`、`/media` 到 8090）。

| 页面 | 功能 |
|---|---|
| 监控大屏 | 1/4/9 分屏；AI 画面 / 原始 + 前端画框 / 原始画面；WebRTC 或 HTTP-fMP4；每格显示帧率、推理耗时、目标数、区域人数、越线计数；右侧实时告警；新告警时对应画面边框闪红 |
| 设备管理 | 设备增删改（推流设备或摄像头拉流地址）；开关 AI 分析；Worker 状态、重启或停止 |
| 规则配置 | **在实时画面上直接画**区域（多边形）和越线；拖动顶点、整体移动、双击边线插入顶点、右键删除顶点；越线可以反转方向；保存后热更新 |
| 告警中心 | 24 小时统计；按设备、类型、状态、时间筛选；截图和录像；确认或标记误报；`?id=` 直接打开某条告警 |
| 通知设置 | 企业微信、钉钉机器人配置和测试，推送状态 |

- `components/Player.vue`：WebRTC 用 ZLM 自带的 `ZLMRTCClient.js`（从 ZLM 的 Web 根目录动态加载）；fMP4 用原生 `<video>`，落后超过 3 秒时跳到最新。断流后每 3 秒重试；切回标签页时自动重连；6 秒没有播放进度的卡顿看门狗。
- `components/RuleCanvas.vue`：canvas 覆盖在视频上，坐标归一化，和 `worker/rules.py` 一致；IN 箭头的方向和 Worker 用同一个公式计算。
- `store.js`：WebSocket 断线后按 1、2、4……15 秒自动重连。

**端到端播放测试**（`web/e2e/playtest.mjs`）：用无头 Chrome 打开监控大屏，测试 AI/原始 × WebRTC/fMP4 四种组合，每 5 秒统计一次实际解码帧数，"帧率 × 5"表示流畅，0 表示卡住。

```bash
cd web && node e2e/playtest.mjs "$(which google-chrome)" /tmp/shots 40
```

### 5.5 告警录像和推送（M5）

```
Worker 输出编码（x264，只编码一次）
  └─ ffmpeg tee ─┬─> RTSP 推给 ZLM（ai/<stream>）
                 └─> 2 秒一段的 MPEG-TS，文件名是开始时间，放在内存盘 /dev/shm/ai-vision-ring/<stream>/
                       只保留最近 60 秒（每路约 150 KB/s，约 9 MB）
告警 ──> 登记录像任务 [告警前 10 秒, 告警后 10 秒]
          └─ 告警后约 13 秒：把覆盖这段时间的片段直接拼成 MP4（-c copy，不重新编码）
                └─ 等告警上传拿到 ID ──> POST /api/internal/clips ──> data/clips/<stream>/<日期>/
                      └─ WebSocket alarm_clip 事件，页面上的告警自动出现录像
```

- **没有用 ZLM 的分段录制**：一开始的方案是让 ZLM 对每路持续录 MP4 再截取，但那样要一直写盘，而且录的是原始画面。现在直接复用 Worker 的输出编码，录像带检测框，切片在内存盘，不写磁盘。
- **相近的告警共用一段录像**：新告警落在已有任务的时间窗口内，就延长这个任务（最长 40 秒）。
- **长度**：按 2 秒一段对齐，一般 20–22 秒；Worker 刚启动时会短一些。

| 渠道 | 内容 | 说明 |
|---|---|---|
| 企业微信群机器人 | Markdown 文字 + 截图 | 截图以图片消息直接发送（base64），**不需要公网地址** |
| 钉钉群机器人 | Markdown | 支持"加签"（HMAC-SHA256）；钉钉只能用链接显示图片，填了「平台访问地址」才显示截图 |

- **不阻塞告警入库**：每条推送是一个后台异步任务。
- **限流**：每个渠道每分钟最多 20 条，超出的只计数、不排队（几分钟后才补发的旧告警没有意义）。
- **按类型过滤**、**推送状态**（最近一次成功与否、失败原因、限流条数）都在设置页上。

**自动清理**（`api/retention.py`）：每段录像约 3.4 MB。一路告警多的视频（每小时 40 多条）一天能产生约 3 GB，所以控制服务每小时清理一次（启动时也清理一次）：
- 截图和录像超过 `MEDIA_KEEP_DAYS`（默认 7 天）就删除。
- 两者合计超过 `MEDIA_MAX_GB`（默认 5 GB），就从最旧的开始删，直到不超过上限。
- 告警记录保留在数据库里，只清空截图和录像链接；手动删掉的文件，对应的链接也会被清空，页面上不会出现打不开的录像。
- 设置为 `0` 表示不限制。

### 5.6 前端叠加画框（M6）

| 选项 | 画面 | 框 |
|---|---|---|
| AI 画面 | Worker 画好框后重新编码的 `ai/<stream>` | 画在视频里，每秒随检测更新 5 次 |
| **原始 + 前端画框** | 原始流 `live/<stream>`，不经过重新编码 | 浏览器用 canvas 画，按卡尔曼速度外推，随屏幕刷新率平滑移动 |
| 原始画面 | 原始流 | 无 |

```
Worker 每次检测后 ──UDP──> API（与 HTTP 同端口号，只监听 127.0.0.1）──WebSocket──> 订阅了这一路的页面
  {"s":"ped","p":0.06,"w":768,"h":576,"tr":[[id, 类别, 置信度, x1, y1, x2, y2, vx, vy, 是否告警], ...]}
```

- **坐标**：归一化到 0–1；速度单位是"每秒移动多少个画面宽高"。
- **只发给需要的页面**：页面发送 `{"sub": ["ped"]}` 订阅；数据不入库，也不进 Vue 的响应式系统（每路每秒 5 条，避免无谓的重新渲染）。
- **时间对齐**：`p` 是 Worker 从收到这一帧到发出结果的耗时，在服务器上测量，不需要对时。浏览器按"收到时间 − p + WebRTC 抖动缓冲延迟"推算这一帧什么时候显示，再按速度外推到当前时刻。实测 WebRTC 从收到一帧到显示约 5 ms。
- **只支持 WebRTC**：fMP4 在浏览器里有 1–3 秒缓冲，页面拿不到精确值，所以 fMP4 模式下这个选项是灰的。

### 5.7 更换模型

`worker/detector.py` 适用于所有输出为 `[1, 4+类别数, N]` 的 YOLOv8/YOLO11 检测模型：
- **只检测人**：`--classes person`，不需要换模型。
- **自己训练的模型**（安全帽、烟火等）：导出成 OpenVINO 格式，传入 `--model`，并换成自己的类别列表。
- **人体姿态（pose）**：输出多了关键点，需要另写后处理。

---

## 6. 问题定位记录

| # | 现象 | 根因 | 修复 |
|---|---|---|---|
| 6.1 | cmake 报 `ZLToolKit does not contain a CMakeLists.txt`；编译成功但 WebRTC 不可用 | 子模块没拉；没装 OpenSSL，WebRTC 被静默关闭 | `git submodule update --init`；装 `libssl-dev` 和 libsrtp 后重编 |
| 6.2 | ZLM 自带 WebRTC 页面一直转圈 | `externIP` 填了错误的外网 IP；WSL 镜像模式下回包回不到 Windows | `externIP=127.0.0.1,172.20.10.3` |
| 6.3 | WebRTC 有画面没声音 | 视频是 AAC 音频，WebRTC 不支持 | 推流时转 Opus，用 RTSP 推 |
| 6.4 | WebRTC 播放半秒后卡住 | RTP 包超过 MTU，被 loopback 丢弃 | `-pkt_size 1200` |
| 6.5 | Windows 浏览器里 WebRTC 仍然一秒后卡住 | 关键帧前没有 SPS/PPS | `h264_mp4toannexb` / `dump_extra` |
| 6.6 | 重新推流后 Worker 不恢复 | Hook 处理阻塞 5 秒导致丢通知，且只依赖 Hook | 停止改为非阻塞；每 3 秒对账 |
| 6.7 | fMP4 只播几秒就卡住 | 追帧逻辑在开始播放前就不停跳转 | 开始播放后才检查，限制跳转频率 |
| 6.8 | 6 路同时跑，推理从 40 ms 变成 380 ms | OpenVINO 绑核，所有进程挤在同几个核上 | 关闭 `ENABLE_CPU_PINNING` |
| 6.9 | 多路时磁盘一直在写 | ZLM 默认给每路实时生成 HLS | `hls_demand=1` |
| 6.10 | cars、street 两路没有框 | 原素材是正上方俯拍，COCO 模型认不出 | 换成 Roboflow `vehicles-2` |
| 6.11 | 前端画框总跟在人后面 | PyAV 帧级多线程解码，每帧晚 1.4 秒 | 解码改成 SLICE 线程 |
| 6.12 | 云服务器上浏览器播放失败 | 新版 ZLM `allow_ip_range` 只允许内网 IP | 清空白名单 |
| 6.13 | 其他小问题 | 见下文 | |

### 6.1 编译：子模块缺失、OpenSSL 缺失导致 WebRTC 被静默关闭

- **子模块**：`git clone` 没加 `--recursive`，`3rdpart/ZLToolKit` 等目录是空的，cmake 报错。`git submodule update --init` 拉下 ZLToolKit、jsoncpp、media-server、pybind11、webassist。
- **OpenSSL**：cmake 只给了一个警告 `openssl 未找到, rtmp 将不支持 flash 播放器, https/wss/rtsps/rtmps/webrtc 也将失效`，编译照样成功。检查发现：`OPENSSL_*-NOTFOUND`、编译宏里没有 `ENABLE_WEBRTC`、`ldd` 看不到 libssl、启动时 `rtc.*` 配置全部报 `unknow config`。
- **装 libssl-dev 时 404**：本地包索引太旧，`3ubuntu3.11` 已经从官方源下架；换阿里云源并 `apt-get update` 后装上 `3ubuntu3.16`。
- **教训**：编译后要检查 `CMakeCache.txt` 和 `ldd`，不能只看编译是否成功。

### 6.2 WebRTC 页面一直转圈（externIP）

1. **第一次**：`config.ini` 里 `externIP=13.60.141.242`（一个无关的外网 IP）。WebRTC 握手时服务器让浏览器去连这个地址的 8000 端口，连不上，服务端超时断开（日志 `接受rtp/rtcp/datachannel超时`）。改成本机 IP `172.20.10.3`。
   - 第一次用 `sed` 修改没生效：文件是 CRLF 换行，正则 `$` 前面还有 `\r`。
2. **第二次**：浏览器的包能到服务器（日志里看到来源 `172.20.10.3`），但服务器回包时大量 `udp socket: connection refused`。WSL 镜像模式下 Windows 和 WSL 共用 `172.20.10.3`，WSL 把发给这个 IP 的回包当成发给自己的，没有回到 Windows 上的浏览器。fMP4 走 `127.0.0.1` 不受影响。
3. **修复**：`externIP` 支持逗号分隔多个 IP，改成 `127.0.0.1,172.20.10.3`：本机浏览器走 loopback，局域网设备走网卡 IP。

### 6.3 WebRTC 有画面没声音

浏览器 WebRTC 只支持 Opus 和 G.711，而 MP4 文件和 RTMP 推流通常是 AAC。查了 ZLM 源码，开源版不在服务端做 AAC → Opus 转码。推流时 `-c:a libopus -ar 48000 -ac 2` 转成 Opus，并改用 RTSP 推（传统 RTMP/FLV 不支持 Opus）。代价是 FLV 播放这路流时没有声音。

### 6.4 WebRTC 播放半秒后卡住（RTP 包超过 MTU）

**现象**：WebRTC 只显示第一帧就卡住；fMP4 和 RTSP 都正常。同一个文件用 Windows 上的 ffmpeg 推流正常，用 WSL 里的 ffmpeg 6.1 推流就卡。

**定位过程**：
1. Chrome 的 `getStats()`：ICE 连接成功，但丢包率超过 60%，NACK 和 PLI 不断，一帧都没解码出来。
2. 用 Python 测本机 UDP 回环，0% 丢包，说明不是普通的网络问题。
3. tcpdump 抓包：ZLM 发出的包大多是 1400–1599 字节，Chrome 收到的只有小包。
4. ZLM 对 RTSP 推流的 RTP 包是**原样转发**给 WebRTC 的，不重新分包。ffmpeg 推 RTSP 默认 `pkt_size=1472`，再加上 WebRTC 的 RTP 扩展头和 SRTP 加密就超过了 1500 字节。WSL 镜像模式下本机通信走的 `loopback0` MTU 是 1500，超过的包被直接丢掉。

**修复**：推流和 Worker 输出都加 `-pkt_size 1200`（WebRTC 的常规做法也是把 RTP 包控制在 1200 字节左右）。修改后丢包为 0，帧率稳定。

同一轮还修了播放器：切换标签页后卡住（Chrome 暂停后台无音轨视频）、6 秒无进度的卡顿看门狗、`watch(() => [a, b])` 每次返回新数组导致设备列表一刷新播放器就重启。

### 6.5 Windows 浏览器里 WebRTC 仍然卡（关键帧前没有 SPS/PPS）

包大小修好后，WSL 里的 Chrome 能正常播放，但 **Windows 上的浏览器**播放 `pedestrians.mp4` 仍然一秒左右就卡住，播放 `yybht.mp4` 却正常。用 ZLM 自带的 WebRTC 页面也一样。

**走过的弯路**：
- 怀疑关键帧太大（106KB vs 26KB）：实测 WSL → Windows 一次连发 100 个 UDP 包会丢 15%。加了 `-maxrate 1200k -bufsize 300k` 把关键帧限制到约 30KB，**还是卡**。这个限制保留了，因为确实能减小突发。
- 试 `rtc.preferred_tcp=1`：只是稍微调高 TCP 候选的优先级，Chrome 仍走 UDP，没效果，改回。

**定位**：让用户导出 Windows Chrome 的 `chrome://webrtc-internals`（`webrtc_internals_dump.gz`）：
- `packetsLost=0`：网络没有丢包（有一次连接走的是 TCP，也一样卡）。
- `framesReceived` 一直在涨，`framesDecoded` 停在 **20**（正好是第一个 GOP），`keyFramesDecoded=1`。
- `pliCount` 持续上涨：浏览器一直在要关键帧。
- 解码器：`FFmpeg (fallback from: ExternalDecoder (D3D11VideoDecoder))`：硬件解码器失败后换成了软件解码器。

再看两个文件每个关键帧里的 NAL 单元：`yybht.mp4` 是 `SPS PPS SEI IDR`，`pedestrians.mp4` 只有 `IDR`（SPS/PPS 只在 MP4 文件头里）。Worker 输出也一样，x264 只在开头写一次。浏览器换解码器之后，没有 SPS/PPS 就解不出关键帧，只能不停发 PLI。

**修复**：每个关键帧前都带上 SPS/PPS：
- MP4 源 `-c:v copy` 推流：`-bsf:v h264_mp4toannexb`。
- Worker 输出（x264）：`-bsf:v dump_extra=freq=keyframe`。
- 转码生成的测试视频：`-x264-params repeat-headers=1`。
- **注意**：MP4 源不能用 `dump_extra`，它会把 MP4 格式的参数集直接拼到帧前面，拼出来的流是坏的（`Invalid NAL unit size`）。

### 6.6 重新推流后 Worker 不恢复

**现象**：重推 `live/ped` 后 Worker 没有重新启动，`ai/ped` 打不开。

**原因链**：
1. Worker 收到 SIGTERM 后，OpenVINO 的 C++ 线程在 Python 解释器销毁时 abort 并写 core，约 5 秒才退出。
2. Hook 处理里同步等待 Worker 退出，`on_stream_changed` 响应用了 5008 ms，期间 ZLM 后续的 Hook 发送失败，"上线"通知丢了。
3. 只依赖 Hook，丢一次就再也不会启动。（另外 WSL 镜像网络下 ZLM → API 的 Hook 经常 `EADDRINUSE` 失败。）

**修复**：
- `manager.stop()` 不阻塞：只发 SIGTERM，宽限期后由监督循环强杀；停止中收到启动请求就排队。
- **每 3 秒和 ZLM 的 `getMediaList` 对账**（按状态而不是按事件），补启或停止 Worker；手动停止的不自动拉起。
- Worker 收到 SIGTERM 只设置标志，主循环正常退出后 `os._exit`，避免 abort。
- 同时发现原始测试视频带 B 帧，WebRTC 播放会卡，转码加 `-bf 0`。

### 6.7 fMP4 只播几秒就卡住

监控大屏的 fMP4 画面循环播放开头 8 秒左右。直播流的时间戳从推流开始累计（比如 438 秒），而视频刚开始播放时进度是 0，原来的追帧逻辑把这个差距当成落后了 438 秒，每秒都往前跳，打断加载，画面卡死。现在等真正开始播放后才检查；落后不多时用 1.1 倍速追，超过 3 秒才跳，两次跳转至少间隔 5 秒。这个问题是用新写的无头 Chrome 端到端测试发现并验证的。

### 6.8 6 路同时跑，推理从 40 ms 变成 380 ms（OpenVINO 绑核）

CPU 还有 80% 空闲，推理却慢了 10 倍。OpenVINO 默认把推理线程绑到固定的 CPU 核上（`ENABLE_CPU_PINNING`），每个 Worker 进程各自做决定，6 个进程绑到了同样的几个核上，只能排队。关掉绑核交给系统调度：

| 6 路负载下单独测试 | 推理耗时（中位数） |
|---|---|
| 2 线程，绑核（默认） | 380 ms |
| **2 线程，不绑核（现在的配置）** | **62 ms** |
| 4 线程，绑核 | 271 ms |

### 6.9 多路时磁盘一直在写

用 `/proc/<pid>/io` 统计每个进程的写入，发现 ZLM 给每一路流（12 路：6 路原始 + 6 路 AI）都实时生成 HLS 切片，约 1.2 MB/s。改 `hls_demand=1` 后降到约 0.3 MB/s。

### 6.10 cars、street 两路没有检测框

Intel 的 `car-detection` 素材是正上方俯拍，COCO 训练的模型几乎认不出这种角度的车（63 帧里只有 1 帧得分超过 0.4）。换成 Roboflow 的 `vehicles-2`（高速路斜视角，平均每帧约 13 辆）。

### 6.11 前端画框总跟在人后面（解码延迟 1.4 秒）

用 `requestVideoFrameCallback` 测得浏览器从收到一帧到显示只有约 5 ms，所以问题在 Worker。逐帧记录"收到数据包 → 解出画面"的时间：

| PyAV 解码线程模式 | 从收到数据包到解出画面 |
|---|---|
| AUTO（帧级多线程，16 线程） | 1388 ms |
| SLICE（片级多线程，现在的设置） | 1.5 ms |

帧级多线程会同时解码"CPU 线程数"那么多帧，每一帧都要晚出来十几帧。这 1.4 秒原来也一直加在 AI 画面和告警时间上，改成 SLICE 后一起消除了。

### 6.12 云服务器上浏览器播放失败（HTTP 401）

**现象**：部署到 Vultr 后，外网能打开管理页面和 API，但监控大屏的 WebRTC 和 fMP4 都没有画面。

**定位**：用无头 Chrome 打开公网地址并记录请求：`http://<服务器IP>/ZLMRTCClient.js` 返回 **401**。容器里的 ZLM 版本比本机新，`[http]` 段多了 `allow_ip_range`，默认只允许 `127.0.0.1` 和内网网段访问 HTTP 服务。本机一直用 `127.0.0.1` 访问，所以没遇到。

**修复**：`deploy/zlm-start.sh` 把 `allow_ip_range` 清空（可用 `ZLM_HTTP_ALLOW_IP` 指定）。ZLM 的管理接口仍然需要 secret；代价是知道地址的人都能播放 ZLM 上的流。正式环境可以只允许固定 IP 段，或者把 ZLM 的 HTTP 放到反向代理后面加鉴权。

### 6.13 其他小问题

| 问题 | 处理 |
|---|---|
| 重新运行 `get_samples.sh` 覆盖了正在推流的文件，推流中断 | 已存在的跳过；先写临时文件再 `mv` |
| 给 ZLM 发 SIGHUP 重载配置，所有推流断开 | `streams.sh` 断了 2 秒后自动重推；Windows 上手动推的流要重推 |
| 8080 端口被占用 | WSL 镜像模式下和 Windows 共用端口，VS Code 占了 8080，控制服务改用 8090 |
| 告警时间差 8 小时 | SQLite `CURRENT_TIMESTAMP` 是 UTC，改为本地时间；Docker 镜像设置 `TZ=Asia/Shanghai` |
| 单元测试偶发失败 | 测试先发 SIGTERM，子进程还没来得及设置 `SIG_IGN`。改为子进程准备好后写一个文件，测试等这个文件出现再发信号；连续 20 次通过 |
| Docker 本机测试端口冲突 | 本机已经有 ZLM 和 API 在跑；用另一套端口的 env 文件和 `-p aivtest` 项目名，或直接在服务器上测 |
| Ubuntu 自带的 `docker.io` 没有 `docker compose` | `apt install docker-compose-v2 docker-buildx` |

---

## 7. 性能

### 7.1 单路（i5-13500H）

| 测试项 | 结果 |
|---|---|
| yolo11n OpenVINO FP32，640 输入，单独测试（含前后处理） | 37 ms/帧 |
| 单路 1028x480@24fps，每秒检测 5 帧 | 输出 23.9fps，不掉帧 |
| 单路 768x576@10fps，检测、跟踪和规则都开启，2 线程 | 推理 39 ms；Worker 进程约 45% CPU |

**OpenVINO 推理线程数**（每秒检测 5 帧）：

| 推理线程数 | 单帧延迟 | CPU 占用 |
|---|---|---|
| 自动（默认） | 42.9 ms | 158% |
| 1 | 119.6 ms | 41% |
| **2（默认采用）** | **44.2 ms** | **43%** |
| 4 | 31.9 ms | 58% |

默认配置下线程分散到所有大小核上，还会空转等待，延迟几乎没变，CPU 却多用了 3 倍多。一台机器跑多路时，每路只分少量线程，总吞吐量更高。

### 7.2 多路：6 路同时运行（i5-13500H）

6 路视频（768x432 到 960x540，10–15fps），每路每秒检测 5 帧，每路 2 个推理线程：

| 进程（每路一组） | CPU | 内存（PSS） |
|---|---|---|
| Worker（解码、检测、跟踪、画框） | 70–85% | 约 195 MB |
| 输出编码 ffmpeg（x264 ultrafast） | 8–16% | 约 27 MB |
| 推流 ffmpeg（模拟摄像头，`-c:v copy`） | 不到 1% | 约 15 MB |
| ZLM（全部 12 路流共用） | 约 11% | 约 105 MB |
| **合计** | **约 5.6 核，整机 CPU 约 44%** | **约 1.6 GB** |

每路推理 65–90 ms，输出帧率与原视频一致。估算这台机器大约能跑 10 路。

### 7.3 云服务器：Vultr 8 vCPU（Docker）

| 项目 | 结果 |
|---|---|
| 机型 | Vultr Optimized Cloud Compute（CPU Optimized），8 vCPU、16 GB 内存，没有 GPU |
| 6 路 AI 全开 | 整机 CPU 45%，内存约 1.5 GB |
| 每路推理耗时 | 47–63 ms，输出帧率和原视频一致 |
| 估算容量 | 每路约 0.6 核，约 10 路 |

演示 4 路以上：8 vCPU 的 CPU 型独享主机就够了，不需要 GPU；内存 8 GB 足够，磁盘 50 GB 以上。

### 7.4 压测脚本

```bash
python3 scripts/bench.py -s 30                      # 系统运行中，采样 30 秒
python3 scripts/bench.py --api http://127.0.0.1:18090
```

从 `/proc` 统计每个进程的 CPU、内存（PSS）和磁盘写入，从 API 取每路的推理耗时和帧率，判断是否跟得上（每秒检测 5 次时单次要小于 200 ms），最后估算能跑多少路。Docker 部署时在宿主机上运行即可（host 网络，能看到容器里的进程）。用目标路数实测再看结果：机器越满，睿频越低，超线程互相争用，每路开销会变大。

---

## 8. 部署与运维

### 8.1 本机运维（原生方式）

本机是开发机，用原生方式运行（systemd 的 ZLM + venv 里的控制服务），改完代码重启即可生效。Docker 只用于部署云服务器，本机只在部署前验证镜像能否构建（见 [8.4](#84-部署前在本机验证镜像)）。

| 操作 | 命令 |
|---|---|
| ZLM 启停、日志 | `systemctl restart zlm`，`journalctl -u zlm -f`，`/var/log/zlm/` |
| 控制服务 | `scripts/run_api.sh`（前台运行；Worker 由它启动和管理） |
| 测试推流 | `scripts/streams.sh start|stop|restart|status [流名...]` |
| 日志 | `data/logs/worker_<流名>.log`、`data/logs/push_<流名>.log` |
| 数据 | `data/ai-vision.db`（SQLite）、`data/alarms/`（截图）、`data/clips/`（录像） |
| 前端改动后 | `scripts/build_web.sh`，刷新页面即可 |
| 资源统计 | `python3 scripts/bench.py` |
| 测试 | `.venv/bin/python -m pytest -q tests`；播放测试 `web/e2e/playtest.mjs` |

### 8.2 Docker Compose

服务器上只需要装 Docker（含 compose 插件）。Node、Python、ffmpeg、OpenVINO 都在镜像里：

```bash
cp .env.example .env
vi .env                                    # 至少设置 ZLM_SECRET（openssl rand -hex 16）和 PUBLIC_IP
docker compose up -d --build               # 首次构建约 5–10 分钟
docker compose --profile demo up -d        # 可选：下载 6 段测试视频并循环推流
# 浏览器打开 http://<服务器IP>:8090
```

| 服务 | 镜像 | 说明 |
|---|---|---|
| zlm | ZLMediaKit 官方镜像 | `deploy/zlm-start.sh` 按 `.env` 改写配置后启动（见 3.2） |
| app | 本项目 `Dockerfile` | API、网页、Worker（由 API 按需启动）、ffmpeg。数据在 `./data` |
| demo | 同 app 镜像 | `--profile demo` 时才启动：`get_samples.sh` 下载素材，`streams.sh run` 循环推流 |

镜像构建分三个阶段：Node 构建前端 → 准备 OpenVINO 模型（构建目录里有导出好的模型就直接用，否则下载 `yolo11n.pt` 导出，torch 不进最终镜像）→ `python:3.12-slim` 运行时。最终镜像约 1.4 GB。

- **网络**：全部用 `network_mode: host`。WebRTC 走 UDP，还要把服务器 IP 告诉浏览器，单台服务器上 host 网络最简单。
- **防火墙**：放行 8090、80（TCP）和 8000（TCP + UDP）。外部摄像头要推流进来时再开 554、1935。
- **公网 IP**：`.env` 的 `PUBLIC_IP` 写入 ZLM 的 `externIP`。
- **`/dev/shm`**：录像的环形缓冲在这里，每路约 10 MB，Docker 默认只给 64 MB，所以设置了 `shm_size: 1gb`。
- **国内构建**：`.env.example` 里列出了镜像站和软件源，把注释打开即可。国外服务器直接用官方源，不用改。

### 8.3 云服务器部署实操（Vultr）

1. **开机器**：Vultr Optimized Cloud Compute → CPU Optimized，8 vCPU，Ubuntu 24.04。机器自带 Docker 和 ufw 防火墙，网卡上直接是公网 IP。
2. **装 compose 插件**：
   ```bash
   apt-get update && apt-get install -y docker-compose-v2 docker-buildx
   ```
3. **上传代码**（在开发机上打包，带上已经导出的模型，服务器就不用装 torch）：
   ```bash
   cd /root/proj/ai-vision
   tar czf /tmp/ai-vision.tgz --exclude=.git --exclude=.venv --exclude=.venv-export --exclude=./data \
       --exclude=web/node_modules --exclude=web/dist --exclude=__pycache__ --exclude='*.pt' --exclude=.env .
   scp /tmp/ai-vision.tgz root@<服务器IP>:/root/
   ssh root@<服务器IP> 'mkdir -p /opt/ai-vision && tar xzf /root/ai-vision.tgz -C /opt/ai-vision'
   ```
   有 Git 仓库时也可以直接 `git clone`。
4. **配置**（secret 在服务器上生成，不经过聊天或日志）：
   ```bash
   cd /opt/ai-vision && cp .env.example .env
   sed -i "s/^ZLM_SECRET=.*/ZLM_SECRET=$(openssl rand -hex 16)/; s/^PUBLIC_IP=.*/PUBLIC_IP=<服务器IP>/" .env
   chmod 600 .env
   ```
5. **防火墙**：
   ```bash
   ufw allow 80/tcp && ufw allow 8090/tcp && ufw allow 8000/tcp && ufw allow 8000/udp
   ```
   如果在 Vultr 控制台还配了云防火墙组，也要放行同样的端口。
6. **启动**：
   ```bash
   docker compose up -d --build              # 国外服务器用官方源，构建约 10 分钟
   docker compose --profile demo up -d       # 演示素材
   docker compose ps                         # app 显示 healthy
   ```
7. **验证**：
   ```bash
   curl http://<服务器IP>:8090/api/system           # {"zlm": {"ok": true}, "workers": 6}
   curl -o /dev/null -w "%{http_code}\n" http://<服务器IP>/ZLMRTCClient.js   # 200，401 见 6.12
   python3 scripts/bench.py -s 20                   # 在服务器上看负载
   ```
   再在浏览器里打开 `http://<服务器IP>:8090/#/wall`，分别测试 WebRTC、HTTP-fMP4、原始 + 前端画框。

### 8.4 部署前在本机验证镜像

改了 `Dockerfile`、`docker-compose.yml`、`requirements.txt` 或前端依赖后，先在本机构建一次，确认没问题再上服务器。只构建，不启动，原生 ZLM 和控制服务照常运行（启动容器会和原生 ZLM 抢 80/554/1935/8000 端口）。

```bash
cd /root/proj/ai-vision
# 本机的 .env 只用于构建：打开国内镜像源（compose 要求 ZLM_SECRET 有值，随便填）
cp .env.example .env
sed -i -e "s/^ZLM_SECRET=.*/ZLM_SECRET=$(openssl rand -hex 16)/" \
       -e 's/^# \(ZLM_IMAGE\|REGISTRY\|PIP_INDEX\|NPM_REGISTRY\|APT_MIRROR\|TORCH_INDEX\|MODEL_URL\|DOWNLOAD_PROXY\)=/\1=/' .env
chmod 600 .env
docker compose build app                 # 用 worker/models 里导出好的模型，不装 torch
docker builder prune -f                  # 可选：清理构建缓存
```

### 8.5 更新和日常操作（Docker）

| 操作 | 命令（在 `/opt/ai-vision`） |
|---|---|
| 更新代码 | 重新上传解压（或 `git pull`），然后 `docker compose up -d --build` |
| 只改了 `deploy/zlm-start.sh` 或 `.env` | `docker compose up -d`（`.env` 变化会重建容器）或 `docker compose restart zlm` |
| 看日志 | `docker compose logs -f app`，`docker compose logs zlm`；Worker 日志在 `data/logs/` |
| 停止 / 启动演示推流 | `docker compose stop demo` / `docker compose --profile demo up -d` |
| 全部停止 | `docker compose --profile demo down`（数据在 `./data`，不会丢） |
| 备份 | 打包 `data/`（数据库、截图、录像）和 `.env` |
| 改清理策略 | `.env` 里设置 `MEDIA_KEEP_DAYS`、`MEDIA_MAX_GB`，然后 `docker compose up -d` |

- 重启 ZLM 会断开所有推流：demo 容器会自动重推，Worker 跟着恢复；外部推流端要自己重推。
- 页面上关闭了 AI 的设备不会自动启动 Worker，要在「设备管理」里打开。
- 演示完建议修改服务器 root 密码，改用 SSH 密钥登录。

### 8.6 磁盘占用

| 位置 | 增长 | 上限 / 清理方式 |
|---|---|---|
| `data/clips` 告警录像 | 每条告警约 3.4 MB，告警多时一天约 3 GB | `MEDIA_KEEP_DAYS` / `MEDIA_MAX_GB` 自动清理（见 5.5） |
| `data/alarms` 告警截图 | 每张约 100 KB | 同上，和录像一起算 |
| `data/logs/push_*.log` | ZLM 停止时每 2 秒一条重试记录 | 每次启动时清空，超过 1 MB 自动清空 |
| `data/logs/worker_*.log` | 很少，Worker 反复崩溃重启时会变多 | 启动 Worker 时超过 5 MB 就改名为 `.log.1`，每路最多 10 MB |
| `/dev/shm/ai-vision-ring` | 每路约 10 MB，内存盘 | 只保留最近 60 秒 |
| ZLM 日志（本机 `/var/log/zlm`） | 约 30 MB/天 | ZLM 默认只保留 7 天（`-m` 参数） |
| ZLM HLS 切片（`www/<app>/<stream>/`） | 只在有人播放 HLS 时生成 | `hls_demand=1`，ZLM 自动删除旧切片 |
| Docker 容器日志 | 控制服务的访问日志 | 每个容器 3 × 10 MB；Worker 心跳和规则检查不记访问日志 |
| Docker 构建缓存 | 每次 `--build` 增加 | `docker builder prune -f` 手动清理 |

告警数量主要由规则的**冷却时间**决定：同一条规则的同一种告警，冷却时间内最多发一次。人多的画面上"逗留""人数超限"会持续触发，冷却时间从 20 秒改到 300 秒，录像量能少到原来的十几分之一。


---

> 许可证：Ultralytics YOLO 采用 AGPL-3.0 许可，个人项目和展示没问题；商用请换成 YOLOX、RT-DETR 等 Apache 许可的模型。
