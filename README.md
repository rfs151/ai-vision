# ai-vision：基于 ZLMediaKit 的智能视频分析告警平台

实时接入摄像头 / 直播流，检测人和车辆，按规则（区域入侵、越线、逗留）告警，并保存截图和录像。

## 架构

```
摄像头 / OBS / ffmpeg ──▶ ZLMediaKit ──RTSP──▶ AI Worker ──画框后的流──▶ ZLMediaKit ──▶ 浏览器
                         (live/<name>)            │                       (ai/<name>)    WebRTC / FLV / fMP4
                                                  ├─ 解码（每一帧）→ 按时间外推跟踪框 → 画框 → x264 编码
                                                  └─ 分析线程（5fps）：YOLO → ByteTrack → 规则 → 告警（截图 + JSONL）
```

| 模块 | 文件 | 说明 |
|---|---|---|
| 检测 | `worker/detector.py` | OpenVINO YOLO，手写 letterbox 和 NMS |
| 跟踪 | `worker/tracker.py` | ByteTrack：高分框和低分框两轮匹配（匈牙利算法），卡尔曼滤波按实际时间间隔预测，两次检测之间的帧用速度外推框的位置 |
| 规则 | `worker/rules.py` | 区域入侵、逗留、人数超限、越线（区分方向） |
| 告警 | `worker/alarm.py` | 截图（就是触发告警的那一帧）和 `data/alarms/<流名>/events.jsonl` |

- **ZLMediaKit**：负责所有流媒体工作（接入、协议转换、分发、录制）
- **AI Worker**：一路视频对应一个进程。PyAV 负责解码；独立线程按固定帧率（默认 5fps）对最新一帧做 OpenVINO YOLO 推理；每一帧都画上最近一次的检测结果，再用 ffmpeg(x264 zerolatency) 推回 ZLM
- 运行时**不需要 torch**，只依赖 openvino、numpy、opencv、av。前处理（letterbox）和后处理（解码输出、NMS）都是自己实现的

## 开发进度

- [x] **M1** 单路：拉流 → YOLO 检测 → 画框后回推
- [x] **M2** ByteTrack 跟踪 + 规则（区域、越线、逗留）+ 告警去重
- [ ] **M3** FastAPI 控制服务：设备、规则管理，通过 Hook 自动启停 Worker
- [ ] **M4** Vue3 前端：多画面分屏、画区域、实时告警
- [ ] **M5** 告警截图、前后 10 秒录像、企业微信推送
- [ ] **M6** 前端叠加画框、性能压测、Docker Compose

## 快速开始

```bash
# 1. 运行时环境
python3 -m venv .venv
.venv/bin/pip install -i https://mirrors.aliyun.com/pypi/simple/ -r requirements.txt

# 2. 导出模型（只需执行一次；把 yolo11n.pt 放到 worker/models/，下载地址见下文）
scripts/export_model.sh yolo11n          # 生成 worker/models/yolo11n_openvino_model/

# 3. 推一路测试视频（模拟摄像头）
scripts/push_sample.sh /path/to/video.mp4 sample      # → rtsp://127.0.0.1/live/sample

# 4. 启动 Worker（--rules 可选，不加就只做检测和跟踪）
scripts/run_worker.sh sample --rules rules/pedestrians.json   # live/sample → ai/sample

# 单元测试
.venv/bin/python -m pytest -q tests

# 5. 浏览器播放
#   http://127.0.0.1/live/sample.live.mp4                                  原始画面
#   http://127.0.0.1/ai/sample.live.mp4                                    带检测框的画面
#   http://127.0.0.1/webrtc/?app=ai&stream=sample&type=play                WebRTC 低延迟播放
```

模型下载地址（国内镜像）：`https://modelscope.cn/models/Ultralytics/YOLO11/resolve/master/yolo11n.pt`

Worker 参数：`--det-fps 5`（每秒检测帧数）、`--conf 0.4`（跟踪用的高分阈值，0.1 到这个值之间的低分框只用于维持已有目标）、`--classes person,car`（只检测这些类别，留空表示全部 80 类）、`--threads 2`（OpenVINO 推理线程数）、`--device CPU|GPU`、`--rules`、`--out`。

测试视频：执行 `scripts/get_sample.sh` 生成 `data/samples/pedestrians.mp4`。它是 OpenCV 自带的 `vtest.avi`（行人场景，768x576，10fps）转成 H.264 后的文件。访问不了 GitHub 时，设置 `PROXY=http://host:port` 走代理下载。

## 告警规则

配置文件是 JSON，坐标按画面宽高归一化到 0~1，所以和分辨率无关。示例见 `rules/pedestrians.json`。

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

M1 版本在两次检测之间沿用上一次的框，镜头切换或目标移动快时会滞后最多 200ms。M2 加上了卡尔曼速度外推（最多外推 0.5 秒），解决了这个问题。

> 许可证：Ultralytics YOLO 采用 AGPL-3.0 许可；商用请换成 YOLOX、RT-DETR 等 Apache 许可的模型。
