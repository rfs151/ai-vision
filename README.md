# ai-vision：基于 ZLMediaKit 的智能视频分析告警平台

实时接入摄像头 / 直播流，检测人和车辆，按规则（区域入侵、越线、逗留）告警，并保存截图和录像。

## 架构

```
摄像头 / OBS / ffmpeg ──▶ ZLMediaKit ──RTSP──▶ AI Worker ──画框后的流──▶ ZLMediaKit ──▶ 浏览器
                         (live/<name>)        解码 → 抽帧 → YOLO          (ai/<name>)    WebRTC / FLV / fMP4
```

- **ZLMediaKit**：负责所有流媒体工作（接入、协议转换、分发、录制）
- **AI Worker**：一路视频对应一个进程。PyAV 负责解码；独立线程按固定帧率（默认 5fps）对最新一帧做 OpenVINO YOLO 推理；每一帧都画上最近一次的检测结果，再用 ffmpeg(x264 zerolatency) 推回 ZLM
- 运行时**不需要 torch**，只依赖 openvino、numpy、opencv、av。前处理（letterbox）和后处理（解码输出、NMS）都是自己实现的

## 开发进度

- [x] **M1** 单路：拉流 → YOLO 检测 → 画框后回推
- [ ] **M2** ByteTrack 跟踪 + 规则（区域、越线、逗留）+ 告警去重
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

# 4. 启动 Worker
scripts/run_worker.sh sample                           # live/sample → ai/sample

# 5. 浏览器播放
#   http://127.0.0.1/live/sample.live.mp4                                  原始画面
#   http://127.0.0.1/ai/sample.live.mp4                                    带检测框的画面
#   http://127.0.0.1/webrtc/?app=ai&stream=sample&type=play                WebRTC 低延迟播放
```

模型下载地址（国内镜像）：`https://modelscope.cn/models/Ultralytics/YOLO11/resolve/master/yolo11n.pt`

Worker 参数：`--det-fps 5`（每秒检测帧数）、`--conf 0.35`（置信度阈值）、`--classes person,car`（只检测这些类别，留空表示检测全部 80 类）、`--device CPU|GPU`。

## 更换模型

检测器 `worker/detector.py` 适用于所有输出格式为 `[1, 4+类别数, N]` 的 YOLOv8/YOLO11 检测模型：

- **只检测人**：`--classes person`，不需要换模型
- **自己训练的模型**（安全帽、烟火等）：导出成 OpenVINO 格式，传入 `--model`，并把 `names` 换成你自己的类别列表
- **人体姿态（pose）**：输出里多了关键点，需要另外写后处理（计划在 M2 之后做）

## 性能（i5-13500H，CPU）

| 测试项 | 结果 |
|---|---|
| yolo11n OpenVINO FP32，640 输入，单独测试（含前后处理） | 37 ms/帧 |
| Worker 实际运行时的推理耗时（同时在解码和编码） | 44–51 ms/帧 |
| 单路 1028x480@24fps，每秒检测 5 帧 | 输出 23.9fps 不掉帧；Python 约 20% CPU，编码 ffmpeg 约 20%（均为单核占比） |
| 准确性 | 与 ultralytics 官方推理结果一致（类别、置信度、框坐标相同） |

**已知问题（M2 解决）**：两次检测之间沿用上一次的检测框，镜头切换或目标快速移动时框会滞后最多 200ms。接入跟踪器后可以用预测位置补上中间帧。

> 许可证：Ultralytics YOLO 采用 AGPL-3.0 许可；商用请换成 YOLOX、RT-DETR 等 Apache 许可的模型。
