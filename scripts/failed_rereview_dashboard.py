#!/usr/bin/env python3
"""只读展示冻结重评失败批次；不提供反馈或源库写入功能。"""
import hashlib
import html
import json
import mimetypes
import re
import sqlite3
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / 'runtime/review_calibration_authorized_20261105/manifest.json'
DB = ROOT / 'runtime/cost_ab_20261105/agent.db'
PORT = 18765

def e(value):
    return html.escape(str(value if value is not None else ''), quote=True)

def render_data():
    manifest = json.loads(MANIFEST.read_text(encoding='utf-8'))
    conn = sqlite3.connect(DB.resolve().as_uri() + '?mode=ro', uri=True)
    conn.execute('PRAGMA query_only=ON')
    tasks = []
    try:
        for item in manifest['tasks']:
            row = conn.execute('SELECT video_id,status,state_json FROM tasks WHERE task_id=?', (item['task_id'],)).fetchone()
            if row is None:
                raise ValueError('源任务不存在: ' + item['task_id'])
            video_id, old_status, raw = row
            state = json.loads(raw)
            if video_id != item.get('video_id'):
                raise ValueError('视频 ID 不匹配: ' + item['task_id'])
            pool = {c['candidate_id']: c for c in state.get('candidates', [])}
            candidates = []
            for frozen in item['candidates']:
                candidate = pool.get(frozen['candidate_id'])
                if candidate is None:
                    raise ValueError('冻结候选不存在: ' + item['task_id'] + '/' + frozen['candidate_id'])
                headline = candidate.get('headline') or ''
                title = candidate.get('rendered_title') or headline
                for text, key in ((headline, 'headline_sha256'), (title, 'title_sha256')):
                    if hashlib.sha256(text.encode('utf-8')).hexdigest() != frozen[key]:
                        raise ValueError('冻结候选哈希不匹配: ' + item['task_id'] + '/' + frozen['candidate_id'])
                candidates.append(candidate)
            tasks.append((item, old_status, state, candidates))
    finally:
        conn.close()
    return tasks

def page(tasks):
    count = sum(len(c) for _, _, _, c in tasks)
    chunks = [f'''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>隔离重评 · 失败展示台</title><style>
    :root{{color-scheme:light;font-family:-apple-system,BlinkMacSystemFont,"PingFang SC",sans-serif;color:#172334;background:#f5f7fa}}body{{margin:0}}main{{max-width:1100px;margin:auto;padding:32px 20px 80px}}h1{{font-size:clamp(27px,4vw,40px);margin:8px 0 12px}}h2{{font-size:20px;margin:0}}p{{line-height:1.65}}.eyebrow{{color:#345575;font-size:12px;font-weight:700;letter-spacing:.1em}}.alert{{border-left:5px solid #b3392e;background:#fff0ee;padding:14px 20px;margin:24px 0;color:#64251e}}.stats{{display:flex;gap:8px;flex-wrap:wrap}}.pill{{background:#e7edf3;padding:7px 11px;border-radius:5px;font-size:14px}}article{{background:white;border:1px solid #d4dce5;border-radius:8px;margin-top:18px;padding:22px}}summary{{cursor:pointer;font-weight:650;line-height:1.6}}.muted{{color:#4a5c6e}}.error{{color:#a52e25;font-weight:700}}.grid{{display:grid;grid-template-columns:minmax(200px,300px) 1fr;gap:24px;margin-top:18px}}video{{width:100%;max-height:410px;background:#111}}a{{color:#135491}}.candidate{{border-top:1px solid #dfe5ec;padding:14px 0}}.candidate strong{{font-size:18px}}.tag{{display:inline-block;background:#edf1f6;padding:3px 7px;margin:5px 4px 0 0;font-size:13px}}pre{{white-space:pre-wrap;overflow-wrap:anywhere;font:13px/1.55 ui-monospace,monospace;background:#f6f8fa;padding:12px}}details details{{margin:12px 0}}@media(max-width:650px){{.grid{{grid-template-columns:1fr}}main{{padding:20px 14px}}article{{padding:16px}}}}
    </style></head><body><main><div class="eyebrow">READ-ONLY / ISOLATED RUN</div><h1>隔离重评 · 失败展示台</h1><p class="muted">仅用于核对运行失败及冻结素材；不是新的评审结果。</p><div class="alert"><b>新评审未运行，未产生新结果。</b> 20 项任务均因缺少 SILICONFLOW_API_KEY 在模型初始化时失败。以下候选、推荐和状态均来自原系统，不能视为本次重评结论。无需重复进行人工评价。</div><div class="stats"><span class="pill">任务 {len(tasks)}</span><span class="pill">冻结候选 {count}</span><span class="pill">新结果 0</span><span class="pill">只读 · 无反馈入口</span></div>''']
    for i, (item, old_status, state, candidates) in enumerate(tasks, 1):
        v = state.get('video') or {}; route = state.get('route') or {}
        decisions = {x.get('candidate_id'): x for x in (state.get('candidate_decisions') or [])}
        slots = state.get('delivery_slots') or {}
        chunks.append(f'''<article id="task-{i}"><details><summary>{i:02d} / {e(v.get('title') or item.get('video_id'))} <span class="muted"> · {e(item['task_id'])} · {len(candidates)} 条冻结候选</span></summary><p class="error">新评审：未运行 / 任务 {e(item.get('status'))} · {e(item.get('error_type'))}: {e(item.get('error'))}</p><p>原任务状态：<b>{e(old_status)}</b> · 原路由：<b>{e(route.get('action') or '无')}</b> · 原推荐：<b>{e(route.get('recommended_candidate_id') or '无')}</b>（均为历史状态）</p><div class="grid"><div><video controls preload="none" src="/video/{i-1}">浏览器不支持视频播放</video><p><a href="{e(v.get('url') or 'https://www.bilibili.com/video/'+item['video_id'])}" target="_blank" rel="noopener noreferrer">查看原视频 ↗</a></p></div><div><p><b>原视频标题：</b>{e(v.get('title'))}<br><b>作者：</b>{e(v.get('author'))}<br><b>视频 ID：</b>{e(item['video_id'])}</p><p><b>素材描述（原系统生成，未校准）：</b>{e(v.get('description') or '无')}<br><b>原系统解读（未校准）：</b>{e(v.get('caption') or '无')}</p><p><b>原交付槽位：</b>已填 {e(', '.join(slots.get('filled') or []) or '无')}；缺槽 {e(', '.join(slots.get('missing') or []) or '无记录')}</p><details><summary>查看原始 ASR 与 OCR 素材</summary><pre>{e(json.dumps({'asr_segments':v.get('asr_segments',[]),'ocr_segments':v.get('ocr_segments',[])},ensure_ascii=False,indent=2))}</pre></details></div></div><h2>冻结候选 / 原系统状态</h2>''')
        for candidate in candidates:
            cid = candidate['candidate_id']; d = decisions.get(cid) or {}; status = d.get('status') or '无原判定记录'
            recommended = ' · 原推荐' if cid == route.get('recommended_candidate_id') else ''
            chunks.append(f'''<div class="candidate"><strong>{e(candidate.get('rendered_title') or candidate.get('headline') or '无标题')}</strong><br><span class="tag">{e(cid)}</span><span class="tag">原状态：{e(status)}{recommended}</span><span class="tag">原槽位：{e(candidate.get('slot_id') or '未记录')}</span><span class="tag">新评审：未运行</span><details><summary>原系统判定及机械检查</summary><pre>{e(json.dumps({'原系统候选判定':d or None,'原机械检查':candidate.get('mechanical_checks'),'原标题':candidate.get('headline')},ensure_ascii=False,indent=2))}</pre></details></div>''')
        chunks.append('</details></article>')
    chunks.append('</main></body></html>')
    return ''.join(chunks).encode('utf-8')

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        path = urlparse(self.path).path
        if path == '/':
            body = page(TASKS)
            self.send_response(200); self.send_header('Content-Type','text/html; charset=utf-8'); self.send_header('Content-Length',str(len(body))); self.send_header('Cache-Control','no-store'); self.end_headers(); self.wfile.write(body); return
        match = re.fullmatch(r'/video/(\d+)',path)
        if not match or int(match.group(1)) >= len(TASKS):
            self.send_error(404); return
        v = TASKS[int(match.group(1))][2].get('video') or {}
        path = Path((v.get('asset_paths') or {}).get('video') or '/nonexistent')
        if not path.is_file():
            self.send_error(404, '原视频文件不可用'); return
        size = path.stat().st_size; start = 0; end = size - 1
        range_header = self.headers.get('Range')
        if range_header:
            m = re.fullmatch(r'bytes=(\d+)-(\d*)', range_header)
            if not m or int(m.group(1)) >= size:
                self.send_response(416); self.send_header('Content-Range',f'bytes */{size}'); self.end_headers(); return
            start = int(m.group(1)); end = min(int(m.group(2)) if m.group(2) else end, end)
            if end < start:
                self.send_response(416); self.send_header('Content-Range',f'bytes */{size}'); self.end_headers(); return
        self.send_response(206 if range_header else 200)
        self.send_header('Content-Type',mimetypes.guess_type(path.name)[0] or 'application/octet-stream')
        self.send_header('Accept-Ranges','bytes'); self.send_header('Content-Length',str(end-start+1))
        if range_header: self.send_header('Content-Range',f'bytes {start}-{end}/{size}')
        self.end_headers()
        try:
            with path.open('rb') as f:
                f.seek(start); remaining = end-start+1
                while remaining:
                    block = f.read(min(1024*256,remaining))
                    if not block: break
                    self.wfile.write(block); remaining -= len(block)
        except (BrokenPipeError, ConnectionResetError):
            pass

if __name__ == '__main__':
    TASKS = render_data()
    print(f'http://127.0.0.1:{PORT}/ tasks={len(TASKS)} candidates={sum(len(x[3]) for x in TASKS)}',flush=True)
    ThreadingHTTPServer(('127.0.0.1', PORT), Handler).serve_forever()
