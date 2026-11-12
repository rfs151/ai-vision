"""Build the development docs (docs/yolo-acceleration-*.md) as standalone HTML pages.

    .venv-export/bin/python scripts/build_docs.py          # -> web/public/docs/*.html (the 开发文档 menu)
    .venv-export/bin/python scripts/build_docs.py <md> <out.html> [title] [src] --artifact

The main ASCII diagrams become Mermaid flow charts (MERMAID below, matched by the block's first
line). Pages for the app load Mermaid from web/public/docs/mermaid.min.js (no CDN, no web fonts:
the demo is used where Google and some CDNs are unreachable); --artifact leaves both to the
claude.ai viewer. Needs the `markdown` package (requirements-export.txt).
"""
import html
import re
import sys

import markdown

# ---- Mermaid replacements for the main ASCII diagrams (matched by the block's first line) ----
MERMAID = {
    "摄像头 ──▶ ZLMediaKit ──RTSP──▶ Worker": """flowchart TD
  CAM["摄像头"] --> ZLM["ZLMediaKit"]
  ZLM -->|RTSP| DEC["主线程：PyAV 解码每一帧"]
  DEC --> MV["读运动矢量，判断画面是否静止<br/>加速 5"]
  DEC --> SLOT["最新帧槽：只保留最新一帧"]
  SLOT --> AN["分析线程：每秒 5 次<br/>加速 4"]
  MV -. 画面静止 .-> SKIP["跳过推理，沿用上次结果"]
  AN --> YUV["YUV 直通：解码帧直接缩到检测尺寸<br/>加速 6"]
  YUV --> INF["letterbox → OpenVINO 推理<br/>2 线程、不绑核，加速 1–3"]
  INF --> TR["ByteTrack 跟踪 → 规则 → 告警"]
  SKIP --> TR
  DEC --> OUT["每一帧：按卡尔曼速度推算框位置<br/>画框 → x264 编码"]
  OUT --> AI["推回 ZLM：ai/name"]""",
    "摄像头 ──▶ ZLMediaKit（媒体中心）": ("""flowchart TD
  CAM["摄像头"] --> ZLM["ZLMediaKit 媒体中心"]
  ZLM -->|RTSP 拉流| A["推理节点 A<br/>节点代理 + Worker × N"]
  ZLM -->|RTSP 拉流| B["推理节点 B"]
  ZLM -->|RTSP 拉流| C["推理节点 C"]
  A -->|推回 AI 流| ZLM
  B -->|推回 AI 流| ZLM
  C -->|推回 AI 流| ZLM
  A -->|告警 / 心跳 / 负载| API["控制服务<br/>调度 · 规则 · 告警"]
  B -->|告警 / 心跳 / 负载| API
  C -->|告警 / 心跳 / 负载| API""",
        "控制服务的调度职责：\n\n"
        "- **节点注册**：上报核心数、有无 GPU、当前负载。\n"
        "- **分配**：新流交给负载最低、还有余量的节点。\n"
        "- **故障迁移**：节点或 Worker 心跳超时，把它的流改派到其他节点。\n"),
    "视频帧\n  ├─ yolo11n（自训练": ("""flowchart TD
  F["视频帧"] --> Y["yolo11n 自训练<br/>加“香烟”类别"]
  F --> P["yolo11n-pose（已有）<br/>每人 17 个关键点"]
  P --> T["ByteTrack（已有）<br/>按人 ID 串成约 2 秒的关键点序列"]
  T --> S["骨架分类器（新训练）<br/>正常 / 抽烟 / 翻墙 / 投掷 / 摔倒"]
  S --> R["规则合并"]
  Y --> R
  R --> AL["告警"]
  AL --> V["多模态大模型复核（可选）"]
  V --> PUSH["推送"]""",
        "规则合并：\n\n"
        "- **抽烟**：骨架判为“手到嘴”，且手附近检测到香烟。\n"
        "- **翻墙**：骨架判为“攀爬”，且越过围栏线。\n"
        "- **投掷**：骨架判为“投掷”。\n"),
    "YOLO 实时检测 → 规则触发告警": """flowchart TD
  Y["YOLO 实时检测"] --> R["规则触发告警"]
  R --> M["截图 + 3–5 张连续帧 + 规则说明"]
  M --> L["多模态大模型<br/>图中是否真的有人翻越围栏？"]
  L -->|是| P["推送企业微信 / 钉钉"]
  L -->|否| F["标记“大模型判定误报”<br/>保存理由，不推送"]
  L -->|超时 / 失败| P2["按原告警推送，宁可多报"]""",
    "【入库阶段": """flowchart TD
  subgraph ING["入库阶段：视频分析时提前准备"]
    V["视频帧"] --> D["yolo11n：人、自行车、摩托车<br/>每秒 5 次"]
    D --> T["ByteTrack 跟踪"]
    T --> RR["规则：人框与车框大面积重叠、人在车上方<br/>标记“骑行”"]
    RR --> C["选最清晰的一帧<br/>裁出人 + 车区域"]
    C --> ATTR["属性模型：上衣、下衣颜色（可选）"]
    C --> CI["Chinese-CLIP 图像编码器 → 向量"]
    C --> RE["ReID 模型 → 外观特征<br/>跨摄像头合并成全局 ID"]
    T --> FP["脚底点 → 标定矩阵<br/>平面图坐标，每秒 1 个点"]
    ATTR --> DB[("SQLite<br/>时间 · 类型 · 标签 · 属性 · 向量 · 全局 ID · 轨迹点 · 截图 · 录像")]
    CI --> DB
    RE --> DB
    FP --> DB
  end
  subgraph Q["查询阶段：用户输入后"]
    U["“最近三天穿黑衣服骑车的人”"] --> QW["DeepSeek API（或本地 Qwen）<br/>按查询工具说明解析，输出 JSON 条件"]
    QW --> SQL["后端检查字段<br/>SQL 筛选时间和标签"]
    QW --> CT["Chinese-CLIP 文字编码器 → 向量"]
    SQL --> RK["按相似度排序"]
    CT --> RK
    RK --> UI["页面列出截图和录像"]
    UI --> TW["选中一个人：按全局 ID 列出出现过的摄像头<br/>三维场景回放轨迹，停留越久越红"]
  end
  DB --> SQL""",
    "每路摄像头：YOLO → ByteTrack": """flowchart TD
  CAM["每路摄像头：YOLO → ByteTrack<br/>单路轨迹，换摄像头 ID 就断"] --> RE["轨迹截图 → ReID 模型<br/>外观特征"]
  CAM --> FP["脚底点 → 标定矩阵<br/>平面图坐标 (x, y, 时间)"]
  RE --> M["跨摄像头合并<br/>外观相似，且时间、位置说得通"]
  FP --> M
  M --> G["全局 ID"]
  Q["查询结果"] --> G
  G --> REC["各摄像头的出现记录<br/>时间 · 截图 · 录像"]
  G --> TW["三维场景回放轨迹<br/>按停留时间着色：绿 → 黄 → 红"]""",
}


def replace_blocks(text):
    out, pos = [], 0
    for m in re.finditer(r"^```([a-z]*)\n(.*?)^```\n", text, flags=re.S | re.M):
        lang, body = m.group(1), m.group(2)
        repl = None
        if not lang:
            for key, val in MERMAID.items():
                if body.startswith(key):
                    if isinstance(val, tuple):
                        repl = "```mermaid\n" + val[0] + "\n```\n\n" + val[1]
                    else:
                        repl = "```mermaid\n" + val + "\n```\n"
                    break
        out.append(text[pos:m.start()])
        out.append(repl if repl is not None else m.group(0))
        pos = m.end()
    out.append(text[pos:])
    return "".join(out)


def slugify(value, separator):
    m = re.match(r"\s*(\d+(?:\.\d+)*)", value)
    if m:
        return "sec-" + m.group(1).replace(".", "-")
    return "sec-" + re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-") or "sec"


def nav(tokens):
    items = []
    for t in tokens:
        sub = ""
        if t.get("children"):
            sub = "<ol>" + "".join(
                f'<li><a href="#{c["id"]}">{html.escape(html.unescape(c["name"]))}</a></li>'
                for c in t["children"]) + "</ol>"
        items.append(f'<li><a href="#{t["id"]}">{html.escape(html.unescape(t["name"]))}</a>{sub}</li>')
    return "<ol>" + "".join(items) + "</ol>"


# Pages served by the app render the charts themselves (the claude.ai viewer does it for artifacts)
MERMAID_LOCAL = """<script src="mermaid.min.js"></script>
<script>
(function () {
  var dark = document.documentElement.dataset.theme === "dark" ||
    (document.documentElement.dataset.theme !== "light" && window.matchMedia("(prefers-color-scheme: dark)").matches);
  mermaid.initialize({ startOnLoad: false, theme: dark ? "dark" : "default", securityLevel: "strict",
                       flowchart: { htmlLabels: true, useMaxWidth: true } });
  mermaid.run({ querySelector: "pre.mermaid" });
})();
</script>
"""

DOCS = [  # (markdown, page in web/public/docs, title)
    ("docs/yolo-acceleration-multimodal.md", "inference.html", "ai-vision 推理加速与多模态"),
    ("docs/yolo-acceleration-summary.md", "summary.html", "ai-vision 推理加速速览"),
]


def build(MD_PATH, OUT_PATH, PAGE_TITLE, SRC_NAME, local):
    src = open(MD_PATH, encoding="utf-8").read()
    # ---- Split off title + intro (they become the page header), drop the markdown table of contents ----
    lines = src.split("\n")
    assert lines[0].startswith("# ")
    title = lines[0][2:].strip()
    if "**目录**" in src:
        toc_start = src.index("**目录**")
        intro_md = src[len(lines[0]) + 1:toc_start].strip()
        body_md = src[src.index("\n---\n", toc_start) + 5:]
    else:
        first_h2 = src.index("\n## ")
        intro_md = src[len(lines[0]) + 1:first_h2].strip()
        body_md = src[first_h2 + 1:]
    body_md = replace_blocks(body_md)
    print("mermaid blocks:", body_md.count("```mermaid"))
    md = markdown.Markdown(extensions=["tables", "fenced_code", "toc"],
                           extension_configs={"toc": {"slugify": slugify, "toc_depth": "2-3"}})
    body_html = md.convert(body_md)
    toc_tokens = md.toc_tokens
    intro_html = markdown.markdown(intro_md, extensions=["tables"])

    # Mermaid fences -> <pre class="mermaid"> (rendered natively by the artifact viewer)
    body_html = re.sub(r'<pre><code class="language-mermaid">(.*?)</code></pre>',
                       lambda m: f'<figure class="flow"><pre class="mermaid">{m.group(1)}</pre></figure>',
                       body_html, flags=re.S)
    # Plain fences (ASCII diagrams) get their own class; language fences stay code
    body_html = body_html.replace("<pre><code>", '<pre class="ascii"><code>')
    # Wide tables scroll inside their own box
    body_html = body_html.replace("<table>", '<div class="table-wrap"><table>').replace("</table>", "</table></div>")
    # External links open in a new tab
    body_html = re.sub(r'<a href="(https?://[^"]+)">', r'<a href="\1" target="_blank" rel="noopener">', body_html)
    # Section 2 heading divider: drop the stray <hr> before each h2 (sections are styled on their own)
    body_html = re.sub(r"<hr\s*/?>\s*", "", body_html)
    toc_html = nav(toc_tokens)

    TEMPLATE = open(__file__.replace("build_docs.py", "docs_template.html"), encoding="utf-8").read()
    page = (TEMPLATE.replace("{{PAGE_TITLE}}", html.escape(PAGE_TITLE))
            .replace("{{SRC}}", html.escape(SRC_NAME))
            .replace("{{TITLE}}", html.escape(title))
            .replace("{{INTRO}}", intro_html)
            .replace("{{TOC}}", toc_html)
            .replace("{{BODY}}", body_html))
    if local:
        page = re.sub(r'<link rel="(?:preconnect|stylesheet)" href="https://fonts\.[^"]*"[^>]*>\n?', "", page)
        # the artifact host adds the document skeleton; a page served by the app needs its own
        page = ('<!doctype html>\n<html lang="zh-CN">\n<head>\n<meta charset="utf-8">\n'
                '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
                + page + MERMAID_LOCAL + "</html>\n")
    open(OUT_PATH, "w", encoding="utf-8").write(page)
    print(f"wrote {OUT_PATH}: {len(page)} bytes, {len(toc_tokens)} sections")


if __name__ == "__main__":
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent
    args = [a for a in sys.argv[1:] if a != "--artifact"]
    if args:
        md_path, out = args[0], args[1]
        rel = str(Path(md_path).resolve().relative_to(root)) if Path(md_path).resolve().is_relative_to(root) else md_path
        title = args[2] if len(args) > 2 else dict((d[0], d[2]) for d in DOCS).get(rel, "ai-vision")
        build(md_path, out, title, args[3] if len(args) > 3 else rel, local="--artifact" not in sys.argv)
    else:
        for md_path, page, title in DOCS:
            build(root / md_path, root / "web/public/docs" / page, title, md_path, local=True)
