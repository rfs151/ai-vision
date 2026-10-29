# ai-vision：基于 ZLMediaKit 的智能视频分析告警平台

实时接入摄像头 / 直播流，检测人和车辆，按规则（区域入侵、越线、逗留）告警，并保存截图和录像。

## 架构

```
摄像头 / OBS / ffmpeg ──▶ ZLMediaKit ──RTSP──▶ AI Worker ──画框后的流──▶ ZLMediaKit ──▶ 浏览器
                         (live/<name>)            │                       (ai/<name>)    WebRTC / FLV / fMP4
                              │ Hook              ├─ 解码（每一帧）→ 按时间外推跟踪框 → 画框 → x264 编码
                              ▼                   └─ 分析线程（5fps）：YOLO → ByteTrack → 规则 → 告警
                    控制服务 FastAPI :8090  ◀──── 拉取规则（热更新）/ 上报告警和截图 / 心跳 ────┘
                    设备、规则、告警（SQLite） ──▶ WebSocket /ws 实时推送给浏览器
                    Worker 管理：流上线启动、下线停止，崩溃或无心跳时自动重启
```

| 模块 | 文件 | 说明 |
|---|---|---|
| 检测 | `worker/detector.py` | OpenVINO YOLO，手写 letterbox 和 NMS |
| 跟踪 | `worker/tracker.py` | ByteTrack：高分框和低分框两轮匹配（匈牙利算法），卡尔曼滤波按实际时间间隔预测，两次检测之间的帧用速度外推框的位置 |
| 规则 | `worker/rules.py` | 区域入侵、逗留、人数超限、越线（区分方向） |
| 告警 | `worker/apiclient.py` / `worker/alarm.py` | 有 `--api` 时在后台线程上传给 API（不阻塞分析线程）；单独运行时写到本地 `data/alarms/<流名>/` |
| 控制服务 | `api/` | FastAPI + SQLAlchemy；`manager.py` 负责 Worker 进程管理，`routers/hooks.py` 处理 ZLM 回调 |

- **ZLMediaKit**：负责所有流媒体工作（接入、协议转换、分发、录制）
- **AI Worker**：一路视频对应一个进程。PyAV 负责解码；独立线程按固定帧率（默认 5fps）对最新一帧做 OpenVINO YOLO 推理；每一帧都画上最近一次的检测结果，再用 ffmpeg(x264 zerolatency) 推回 ZLM
- 运行时**不需要 torch**，只依赖 openvino、numpy、opencv、av。前处理（letterbox）和后处理（解码输出、NMS）都是自己实现的

## 开发进度

- [x] **M1** 单路：拉流 → YOLO 检测 → 画框后回推
- [x] **M2** ByteTrack 跟踪 + 规则（区域、越线、逗留）+ 告警去重
- [x] **M3** FastAPI 控制服务：设备、规则管理，通过 Hook 自动启停 Worker
- [x] **M4** Vue3 前端：多画面分屏、画区域、实时告警
- [x] **M5** 告警截图、前后 10 秒录像、企业微信和钉钉推送
- [ ] **M6** 前端叠加画框、性能压测、Docker Compose

## 快速开始

```bash
# 1. 运行时环境
python3 -m venv .venv
.venv/bin/pip install -i https://mirrors.aliyun.com/pypi/simple/ -r requirements.txt

# 2. 导出模型（只需执行一次；把 yolo11n.pt 放到 worker/models/，下载地址见下文）
scripts/export_model.sh yolo11n          # 生成 worker/models/yolo11n_openvino_model/

# 3. 构建前端，启动控制服务（ZLM 需要先配置好 Hook，见下文）
scripts/build_web.sh                     # 需要 Node 18+，生成 web/dist
scripts/run_api.sh                       # 管理页面 http://127.0.0.1:8090/，接口文档 /docs

# 4. 推测试视频：API 会自动注册设备并启动 Worker
scripts/get_samples.sh                   # 下载并转码 6 段测试视频到 data/samples/
scripts/streams.sh start                 # 按 scripts/streams.conf 一次推 6 路 → live/<name>，输出 ai/<name>
scripts/streams.sh status                # 查看；stop / restart 后面可以跟流名，只操作其中几路
# 只推一路：scripts/push_sample.sh data/samples/pedestrians.mp4 ped

# 5. 下发规则：在页面「规则配置」里直接画；也可以调用接口（Worker 不需要重启，5 秒内生效）
curl -X PUT -H "Content-Type: application/json" --data @rules/pedestrians.json \
     http://127.0.0.1:8090/api/devices/1/rules

# 6. 查看
#   http://127.0.0.1/ai/ped.live.mp4                            带检测框的画面
#   http://127.0.0.1/webrtc/?app=ai&stream=ped&type=play        WebRTC 低延迟播放
#   http://127.0.0.1:8090/api/alarms                            告警列表（截图地址在 snapshot_url 字段）
#   http://127.0.0.1:8090/api/workers                           Worker 状态（帧率、推理耗时、区域人数、越线计数）

# 单元测试
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q tests
```

不用 API 也能单独运行 Worker：`scripts/run_worker.sh ped --rules rules/pedestrians.json`，告警会写到 `data/alarms/ped/`。

模型下载地址（国内镜像）：`https://modelscope.cn/models/Ultralytics/YOLO11/resolve/master/yolo11n.pt`

Worker 参数：`--det-fps 5`（每秒检测帧数）、`--conf 0.4`（跟踪用的高分阈值，0.1 到这个值之间的低分框只用于维持已有目标）、`--classes person,car`（只检测这些类别，留空表示全部 80 类）、`--threads 2`（OpenVINO 推理线程数）、`--device CPU|GPU`、`--rules`、`--out`。

测试视频（`scripts/get_samples.sh`，访问不了 GitHub 时设置 `PROXY=http://host:port`，直连失败才走代理）：

| 流名 | 来源 | 画面 | 适合测试 |
|---|---|---|---|
| ped | OpenCV `vtest.avi` | 广场行人，768x576 10fps | 越线、区域 |
| street | Intel `person-bicycle-car-detection` | 街道上的人、自行车、汽车 | 多类别 |
| cars | Roboflow `vehicles-2` | 高速路车流，平均每帧约 13 辆 | 车辆、越线计数 |
| people | Intel `people-detection` | 室内人员走动 | 人数超限 |
| worker | Intel `worker-zone-detection` | 工厂工人 | 危险区域入侵 |
| store | Intel `store-aisle-detection` | 超市过道 | 逗留 |

转码统一为：宽度不超过 960、帧率不超过 15fps、无 B 帧、每个关键帧带 SPS/PPS、限制关键帧大小（原因见下文排查记录）。没有选 Intel 的 `car-detection`：它是正上方俯拍，COCO 模型几乎认不出这种角度的车（63 帧里只有 1 帧得分超过 0.4）。

`scripts/streams.sh` 给每一路单独开一个进程组，推流断了（比如 ZLM 重启）2 秒后自动重推；PID 文件在 `data/run/`，日志在 `data/logs/push_<name>.log`。新推上来的 `live/*` 流由 Hook 或者每 3 秒一次的对账循环自动登记成设备（WSL 下 Hook 经常发不到，所以对账循环也会登记）。

## 控制服务（M3）

### ZLM Hook 配置（`config.ini` 的 `[hook]` 段）

```ini
enable=1
on_stream_changed=http://127.0.0.1:8090/index/hook/on_stream_changed
on_stream_not_found=http://127.0.0.1:8090/index/hook/on_stream_not_found
on_server_started=http://127.0.0.1:8090/index/hook/on_server_started
stream_changed_schemas=rtsp      # 每路流只通知一次（默认每种协议各通知一次）
```

只开启"通知类"的 Hook。推流和播放鉴权（`on_publish`、`on_play`）默认不开启：鉴权类 Hook 调用失败时 ZLM 会拒绝请求，开启后控制服务一旦挂掉，所有推流和播放都会失败。`on_publish` 接口已经实现好（只允许本机推送到 `ai/` 应用），需要时可以手动开启。

### 流程

| 事件 | 处理 |
|---|---|
| 有流推到 `live/<流名>` | `on_stream_changed` 回调：不认识的流会自动注册成设备（`AUTO_APPS=live`），然后启动 Worker |
| 流下线 | 停止对应的 Worker |
| 播放一个还没拉取的摄像头 | `on_stream_not_found` 回调：对设置了 `source_url` 的设备调用 ZLM 的 `addStreamProxy` 按需拉流 |
| ZLM 重启 | `on_server_started` 回调：重新添加拉流代理；控制服务自己启动时也会同步一次（避免漏掉期间的 Hook） |
| Worker 崩溃 | 管理器每 3 秒检查一次，按 2、4、8……30 秒的间隔递增等待后重启 |
| Worker 卡死 | 超过 20 秒没有心跳就杀掉进程再重启（`HEARTBEAT_TIMEOUT`） |
| 修改规则 | 规则版本号加 1；Worker 每 5 秒检查一次版本号，发现变化就重建规则引擎（热更新） |

### 接口

| 接口 | 说明 |
|---|---|
| `GET/POST /api/devices`，`GET/PATCH/DELETE /api/devices/{id}` | 设备管理。返回结果里带在线状态、Worker 状态，以及原始流和 AI 流的各种播放地址 |
| `GET/PUT /api/devices/{id}/rules` | 规则（用 Pydantic 校验，比如多边形至少 3 个点） |
| `POST /api/devices/{id}/worker/{start\|stop\|restart}` | 手动控制 Worker |
| `GET /api/alarms?stream=&type=&status=&since=&until=&page=&size=` | 告警分页查询 |
| `PATCH /api/alarms/{id}` | 处理告警：`new` 未处理 / `ack` 已确认 / `false` 误报 |
| `GET /api/alarms/stats?hours=24` | 按类型、按设备统计数量，以及未处理的数量 |
| `GET /api/workers`，`GET /api/system` | Worker 运行状态、系统状态 |
| `WS /ws` | 实时推送 `alarm`、`worker`、`stream`、`rules` 事件 |
| `/api/internal/*` | 给 Worker 调用：拉取规则、上报告警（multipart，JSON 加截图）、心跳。只允许本机访问，或者设置 `INTERNAL_TOKEN` 后凭令牌访问 |

配置通过环境变量设置，见 `api/config.py`：`ZLM_API`、`ZLM_SECRET`（没设置时从 `ZLM_CONFIG=/opt/zlm/config.ini` 读取）、`AUTO_APPS`、`MAX_WORKERS`、`WORKER_ARGS`、`DATA_DIR`、`DB_URL` 等。

注意事项：
- **端口用 8090**：WSL 镜像网络模式下 Windows 和 WSL 共用端口，8080 被 Windows 上的 VS Code 占用了
- **不在日志里记录 secret**：httpx 默认在 INFO 级别记录完整 URL，而 ZLM 的 secret 是 URL 参数，所以把 httpx 的日志级别调到了 WARNING
- **本机请求不走代理**：调用 ZLM 和 API 的 httpx 客户端都设置了 `trust_env=False`，避免环境变量里的 http 代理拦截本地请求

## 前端（M4）

Vue3 + Vite + Element Plus（`web/`）。构建后由控制服务提供页面，整个系统只需要一个端口 8090。开发时运行 `cd web && npm run dev`，访问 5173 端口，Vite 会把 `/api`、`/ws`、`/media` 代理到 8090。

| 页面 | 功能 |
|---|---|
| 监控大屏 | 1/4/9 分屏；可切换 AI 画面或原始画面、WebRTC 或 HTTP-fMP4；每格显示帧率、推理耗时、目标数、区域人数、越线计数；右侧是实时告警列表；新告警时弹窗，对应画面的边框闪红 |
| 设备管理 | 设备增删改（推流设备或摄像头拉流地址）；开关 AI 分析；Worker 状态、帧率、重启次数；重启或停止 Worker |
| 规则配置 | **在实时画面上直接画**区域（多边形）和越线；可以拖动顶点、整体移动、双击边线插入顶点、右键删除顶点；越线可以反转方向；侧边栏编辑类别、逗留、人数上限、告警方向；保存后热更新 |
| 告警中心 | 24 小时统计卡片；按设备、类型、状态、时间筛选；截图缩略图和大图预览；详情；确认或标记误报；新告警实时追加 |

实现要点：
- `components/Player.vue`：WebRTC 使用 ZLM 自带的 `ZLMRTCClient.js`（从 ZLM 的 Web 根目录动态加载）；fMP4 使用原生 `<video>`，并且在落后实时画面超过 2 秒时跳到最新位置。两种方式断流后都会每 3 秒自动重试
- `components/RuleCanvas.vue`：canvas 覆盖在视频上，坐标按画面宽高归一化，和 `worker/rules.py` 一致；容器保持视频原始宽高比，视频用 `object-fit: fill`，保证画的位置就是 Worker 判断用的位置；IN 箭头的方向和 Worker 用同一个公式计算
- `store.js`：WebSocket 断线后按 1、2、4……15 秒的间隔自动重连，告警、Worker 状态、设备变化都实时更新

**端到端播放测试**（`web/e2e/playtest.mjs`）：用无界面 Chrome 打开监控大屏，分别测试 AI/原始画面 × WebRTC/fMP4 四种组合，每 5 秒统计一次实际解码的帧数。帧数稳定在"帧率 × 5"表示播放流畅，出现 0 表示卡住。

```bash
cd web && node e2e/playtest.mjs "$(which google-chrome)" /tmp/shots 40     # 同时保存各页面截图
```

这个测试发现并验证修复了两个问题：
- fMP4 卡住：直播流的时间戳从推流开始累计（比如 438 秒），而视频刚开始播放时播放进度是 0，原来的追帧逻辑把这个差距当成落后了 438 秒，于是每秒都往前跳，打断加载，画面卡死。现在等真正开始播放后才检查；落后不多时用 1.1 倍速追赶，落后超过 3 秒才跳转，两次跳转至少间隔 5 秒
- WebRTC 播放带 B 帧的流会卡住：测试视频改成不带 B 帧编码（`-bf 0`）。接入真实摄像头时，也要在摄像头设置里关闭 B 帧（大部分网络摄像头默认就没有）

## 告警录像和推送（M5）

### 前后 10 秒录像

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

- **录像带检测框**：用的是 Worker 输出的画面，和监控大屏看到的一样。
- **几乎不占资源**：切片和推流共用同一次编码；拼接只是复制数据，每段几毫秒。片段放在内存盘里，不写磁盘。
- **相近的告警共用一段录像**：新告警落在已有录像任务的时间窗口内，就延长这个任务（最长 40 秒），不会切出几乎一样的多段录像。
- **长度**：按 2 秒一段对齐，所以录像一般是 20–22 秒。Worker 刚启动、前面还没有画面时会短一些。
- **参数**：`--clip-pre 10 --clip-post 10` 调整前后时长，`--no-clips` 关闭录像。

### 企业微信、钉钉推送

在页面「通知设置」里配置，保存在数据库的 `settings` 表；也可以调用接口 `GET/PUT /api/notify`，`POST /api/notify/test` 发送测试消息。

| 渠道 | 内容 | 说明 |
|---|---|---|
| 企业微信群机器人 | Markdown 文字 + 截图 | 截图以图片消息直接发送（base64），**不需要公网地址** |
| 钉钉群机器人 | Markdown | 支持"加签"；钉钉只能用链接显示图片，填了「平台访问地址」才显示截图。用关键词安全设置时填"告警" |

- **不阻塞告警入库**：每条推送是一个后台异步任务，机器人接口慢或失败都不影响告警本身。
- **限流**：两个机器人都限制每分钟 20 条。超出的告警不推送、只计数，不排队：几分钟后才补发的旧告警没有意义。
- **按类型过滤**：可以只推送部分告警类型。
- **查看详情**：填了「平台访问地址」后，消息里带链接 `#/alarms?id=<告警ID>`，点开直接弹出这条告警。
- **推送状态**：每个渠道最近一次是否成功、失败原因、因限流未推送的条数，都显示在设置页上。

### 数据库升级

新增了 `alarms.clip` 字段和 `settings` 表。启动时 `api/db.py` 的 `migrate()` 会自动补上缺少的字段，旧数据库不用删。正式项目应该用 Alembic，这里为了一条命令就能跑起来，做了简化。

## 排查记录：WebRTC 播放半秒后卡住

**现象**：WebRTC 只显示第一帧画面就卡住；fMP4 和 RTSP 播放都正常。同一个文件用 Windows 上的 ffmpeg 推流正常，用 WSL 里的 ffmpeg 6.1 推流就会卡。

**定位过程**：
1. Chrome 的 `getStats()` 显示：ICE 连接成功，但丢包率超过 60%，NACK 和 PLI 不断，一帧都没解码出来
2. 用 Python 测本机 UDP 回环，0% 丢包，说明不是普通的网络问题
3. 用 tcpdump 抓包：ZLM 发出的包大多是 1400–1599 字节，Chrome 收到的只有那些小包
4. ZLM 对 RTSP 推流的 RTP 包是**原样转发**给 WebRTC 的，并不重新分包。ffmpeg 推 RTSP 时默认 `pkt_size=1472`，再加上 WebRTC 的 RTP 扩展头和 SRTP 加密，就超过了 1500 字节。WSL 镜像网络模式下，本机通信走的 `loopback0` 网卡 MTU 是 1500，超过的包被直接丢掉

**修复**：推流时加 `-pkt_size 1200`（`scripts/push_sample.sh` 和 Worker 的输出编码器都已经加上）。WebRTC 的常规做法也是把 RTP 包控制在 1200 字节左右。

**验证**：同一个视频，默认包大小时解码 0 帧、丢包超过 60%；改成 1200 字节后丢包为 0，帧率稳定在 10fps，用 `web/e2e/playtest.mjs` 测试 4 种播放组合都正常。

> 自己推流给 ZLM、又需要 WebRTC 播放时，建议都加上 `-pkt_size 1200`。

### 第二个原因：关键帧前没有 SPS/PPS，Windows 浏览器里仍然卡

包大小修好后，WSL 里的 Chrome 能正常播放，但 **Windows 上的浏览器**播放 `pedestrians.mp4` 仍然一秒左右就卡住；播放 `yybht.mp4` 却正常。

**走过的弯路**：一开始怀疑关键帧太大（`pedestrians.mp4` 106KB，`yybht.mp4` 26KB）：WebRTC 一帧的包是连着发的，实测 WSL → Windows 方向一次连发 100 个包会丢 15%。于是加了 `-maxrate 1200k -bufsize 300k` 限制关键帧到约 30KB，但**还是卡**。这个限制保留了下来，因为它确实能减小突发。还试过 ZLM 的 `rtc.preferred_tcp=1`，它只是把 TCP 候选地址的优先级稍微调高，Chrome 仍走 UDP，没有效果，已改回。

**定位**：Windows Chrome 导出的 `chrome://webrtc-internals` 数据：
- `packetsLost=0`：网络没有丢包（有一次连接走的是 TCP，也一样卡）
- `framesReceived` 一直在涨，`framesDecoded` 停在 **20**（正好是第一个 GOP，`-g 20`），`keyFramesDecoded=1`
- `pliCount` 持续上涨：浏览器一直在要关键帧
- 解码器：`FFmpeg (fallback from: ExternalDecoder (D3D11VideoDecoder))`：硬件解码器失败后换成了软件解码器

再看两个文件每个关键帧里的 NAL 单元：
- `yybht.mp4`：`SPS PPS SEI IDR`，每个关键帧都自带参数集
- `pedestrians.mp4`：只有 `IDR`，SPS/PPS 只存在 MP4 文件头里

AI 输出流也一样：ffmpeg 推 RTSP 时，x264 只在开头写一次 SPS/PPS。播放器换了解码器之后，没有 SPS/PPS 就解不出任何关键帧，只能不停地发 PLI 要关键帧。

**修复**：每个关键帧前都带上 SPS/PPS：
- 推流（MP4 源、`-c:v copy`）：`-bsf:v h264_mp4toannexb`（`scripts/push_sample.sh`）
- Worker 输出（x264）：`-bsf:v dump_extra=freq=keyframe`
- 注意：MP4 源不能用 `dump_extra`，它会把 MP4 格式的参数集直接拼到帧前面，拼出来的流是坏的，拉流端解不出画面

> 用 Windows 上的 ffmpeg 推 MP4 文件时，同样加上 `-bsf:v h264_mp4toannexb -pkt_size 1200`。

同一轮还修复了播放器的几个问题（`Player.vue`）：
- **切换浏览器标签后卡住**：Chrome 会暂停后台标签页里只有视频、没有音频的媒体（AI 输出流没有音频），切回来时 HTTP 直播流已经落后很多甚至卡住。现在切回标签页时，fMP4 模式会自动重连，直接跳到最新画面
- **卡顿看门狗**：页面可见、应该在播放，但播放进度 6 秒没动，就自动重连。两种播放模式都适用
- **不必要的重启**：原来监听属性变化的写法 `watch(() => [a, b])` 每次都会返回一个新数组，导致设备列表一刷新播放器就重启。改成分别比较每个值
- **追帧阈值**：Chrome 本身会缓冲约 2.5 秒，把落后的判断阈值调大了，避免一直处于 1.1 倍速

## 告警规则

规则通过 API（`PUT /api/devices/{id}/rules`）或者 JSON 文件（`--rules`）配置，坐标按画面宽高归一化到 0~1，所以和分辨率无关。示例见 `rules/pedestrians.json`。

| 规则 | 配置 | 触发条件 |
|---|---|---|
| 区域入侵 | `zones[].intrusion` | 目标的**脚底点**（检测框底边中点）连续 `confirm_frames` 次检测都在区域内。每个目标只触发一次 |
| 逗留 | `zones[].dwell_sec` | 确认进入区域后停留超过 N 秒 |
| 人数超限 | `zones[].max_count` | 区域内已确认的目标数超过 N |
| 越线 | `lines[]` | 目标前后两次的脚底点连成的线段和规则线相交，按方向计入 IN 或 OUT。`alarm_on: in/out/both` 决定是否告警，`null` 表示只计数 |

去重方式：
1. **连续帧确认**：过滤误检造成的闪烁
2. **每个目标只告警一次**：按跟踪 ID 记录状态
3. **规则冷却**：同一条规则的同一种告警，在 `cooldown_sec` 内只推送一次
4. **越线防抖**：同一个目标连续两次同方向越线只算一次

画面上：IN 箭头指向的方向表示"进"；告警时相关的目标和区域会变红，底部显示 3 秒红色横幅。

## 更换模型

检测器 `worker/detector.py` 适用于所有输出格式为 `[1, 4+类别数, N]` 的 YOLOv8/YOLO11 检测模型：

- **只检测人**：`--classes person`，不需要换模型
- **自己训练的模型**（安全帽、烟火等）：导出成 OpenVINO 格式，传入 `--model`，并把 `names` 换成你自己的类别列表
- **人体姿态（pose）**：输出里多了关键点，需要另外写后处理（计划在 M2 之后做）

## 性能（i5-13500H，CPU）

| 测试项 | 结果 |
|---|---|
| yolo11n OpenVINO FP32，640 输入，单独测试（含前后处理） | 37 ms/帧 |
| 单路 1028x480@24fps，每秒检测 5 帧 | 输出 23.9fps，不掉帧 |
| 单路 768x576@10fps，检测、跟踪和规则都开启，2 线程 | 推理 39 ms；Worker 进程约 45% CPU（10 秒平均） |
| 准确性 | 与 ultralytics 官方推理结果一致（类别、置信度、框坐标相同） |

**OpenVINO 线程数的影响**（yolo11n，每秒检测 5 帧，10 秒平均）：

| 推理线程数 | 单帧延迟 | CPU 占用 |
|---|---|---|
| 自动（默认） | 42.9 ms | 158% |
| 1 | 119.6 ms | 41% |
| **2（默认采用）** | **44.2 ms** | **43%** |
| 4 | 31.9 ms | 58% |

默认配置下线程会分散到所有大小核上，还会空转等待，延迟几乎没变，CPU 却多用了 3 倍多。一台机器要跑多路视频时，每路只分少量线程，总吞吐量更高。

### 多路压测：6 路同时运行

6 路视频（768x432 到 960x540，10–15fps），每路每秒检测 5 帧，每路 2 个推理线程：

| 进程（每路一组） | CPU | 内存（PSS） |
|---|---|---|
| Worker（解码、检测、跟踪、画框） | 70–85% | 约 195 MB |
| 输出编码 ffmpeg（x264 ultrafast） | 8–16% | 约 27 MB |
| 推流 ffmpeg（模拟摄像头，`-c:v copy`） | 不到 1% | 约 15 MB |
| ZLM（全部 12 路流共用） | 约 11% | 约 105 MB |
| **合计** | **约 5.6 核，整机 CPU 约 44%** | **约 1.6 GB** |

每路推理耗时 65–90 ms，输出帧率与原视频一致，没有掉帧。按这个比例估算，这台机器大约能跑 10 路。

**踩坑：OpenVINO 绑核**。6 路一起跑时，每帧推理从 40 ms 涨到了 **380 ms**，CPU 却有 80% 空闲。原因是 OpenVINO 默认把推理线程绑到固定的 CPU 核上（`ENABLE_CPU_PINNING`），而每个 Worker 进程各自做决定，6 个进程都绑到了同样的几个核上，只能排队。关掉绑核交给系统调度后降到 62 ms：

| 6 路负载下单独测试 | 推理耗时（中位数） |
|---|---|
| 2 线程，绑核（默认） | 380 ms |
| **2 线程，不绑核（现在的配置）** | **62 ms** |
| 4 线程，绑核 | 271 ms |

**磁盘 IO**：ZLM 默认给每一路流都实时生成 HLS 切片，12 路一直在写磁盘，约 1.2 MB/s。我们的页面用的是 WebRTC 和 HTTP-fMP4，所以把 `config.ini` 里的 `[protocol] hls_demand` 改成了 `1`，有人播放 HLS 时才生成，写入降到约 0.3 MB/s。

M1 版本在两次检测之间沿用上一次的框，镜头切换或目标移动快时会滞后最多 200ms。M2 加上了卡尔曼速度外推（最多外推 0.5 秒），解决了这个问题。

> 许可证：Ultralytics YOLO 采用 AGPL-3.0 许可；商用请换成 YOLOX、RT-DETR 等 Apache 许可的模型。
