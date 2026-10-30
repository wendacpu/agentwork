from __future__ import annotations

import html
import json
from pathlib import Path

from .storage import Storage


def render_task(storage: Storage, task_id: str, path: str | Path) -> Path:
    row = storage.get_task(task_id); state = json.loads(row["state_json"] or "{}")
    events = storage.events(task_id)
    body = [f"<h1>任务 {html.escape(task_id)}</h1><p>视频：{html.escape(row['video_id'])}　状态：{html.escape(row['status'])}</p>", "<p>评审标准：G1–G4 + Q1–Q3；未校准。G4 和流程路由由程序执行。</p>"]
    assets = state.get("video", {}).get("asset_paths", {})
    links = []
    for label, target in [("视频", assets.get("video")), ("素材目录", assets.get("folder"))]:
        if target: links.append(f"<li>{label}: <a href='{html.escape(target)}'>{html.escape(target)}</a></li>")
    links.append("<li>抽帧数量：%s</li>" % len(assets.get("frames", [])))
    body.append("<h2>原始材料</h2><ul>" + "".join(links) + "</ul>")
    body.append("<h2>候选与评审</h2><pre>" + html.escape(json.dumps({"candidates": state.get("candidates"), "reviews": state.get("reviews"), "candidate_decisions": state.get("candidate_decisions"), "business_policy": state.get("business_policy"), "route": state.get("route")}, ensure_ascii=False, indent=2)) + "</pre>")
    body.append("<h2>运行事件</h2><pre>" + html.escape(json.dumps(events, ensure_ascii=False, indent=2)) + "</pre>")
    out = Path(path); out.parent.mkdir(parents=True, exist_ok=True); out.write_text("<!doctype html><meta charset='utf-8'><style>body{font:14px system-ui;max-width:1100px;margin:2rem auto}pre{white-space:pre-wrap;background:#f5f5f5;padding:1rem}</style>" + "".join(body), encoding="utf-8")
    return out

