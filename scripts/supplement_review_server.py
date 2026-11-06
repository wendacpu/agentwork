#!/usr/bin/env python3
"""隔离补评历史候选；只读取源库和旧反馈，反馈写入指定新目录。"""
import argparse
import hashlib
import html
import json
import sqlite3
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

SOURCE = Path('runtime/cost_ab_20261105')
TARGETS = {'R06', 'R14', 'R15', 'R17', 'R20'}


def ro(path):
    return sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)


def load():
    mapping = json.loads((SOURCE / 'review_mapping.json').read_text(encoding='utf-8'))
    with ro(SOURCE / 'human_feedback_v2.db') as db:
        judged = {(t, c, h) for t, c, h in db.execute('select task_id,candidate_id,title_hash from candidate_decisions_v2 where batch_id=?', (SOURCE.name,))}
    result = []
    with ro(SOURCE / 'agent.db') as db:
        for item in mapping:
            if item['blind_id'] not in TARGETS:
                continue
            tid = item['task_id']
            row = db.execute('select status,state_json from tasks where task_id=?', (tid,)).fetchone()
            state = json.loads(row[1]); pool = (state.get('candidates') or []) + (state.get('delivery_candidates') or []) + (state.get('pending_review_candidates') or [])
            pool += [json.loads(r[0]) for r in db.execute('select payload_json from candidates_by_task where task_id=?', (tid,))]
            decisions = {str(d.get('candidate_id')): d for d in state.get('candidate_decisions') or []}
            candidates = []; seen = set()
            for x in pool:
                cid = str(x.get('candidate_id') or ''); title = str(x.get('rendered_title') or x.get('headline') or '')
                if not cid or not title:
                    continue
                digest = hashlib.sha256(title.encode()).hexdigest(); key = (tid, cid, digest)
                if key in seen or key in judged:
                    continue
                seen.add(key)
                candidates.append({'candidate_id': cid, 'title': title, 'hash': digest, 'slot': x.get('slot_id'), 'parent': x.get('parent_candidate_id'), 'decision': decisions.get(cid, {}), 'errors': (x.get('mechanical_checks') or {}).get('errors') or []})
            result.append({'blind_id': item['blind_id'], 'task_id': tid, 'video_id': item['video_id'], 'category': item.get('category'), 'source_status': row[0], 'route': state.get('route') or {}, 'video': state.get('video') or {}, 'candidates': candidates})
    return result


def page(tasks):
    esc = lambda x: html.escape(str(x or ''), quote=True)
    out = ['<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>历史候选补评</title><style>body{font:16px/1.6 system-ui;max-width:1000px;margin:auto;padding:20px;color:#203048}section,article{border:1px solid #ccd6df;border-radius:10px;padding:16px;margin:14px 0}article{background:#f8fafc}button{padding:8px 18px;margin:4px}textarea{width:95%;min-height:55px}.warn{color:#a33}pre{white-space:pre-wrap;max-height:220px;overflow:auto}</style><h1>历史候选补评</h1><p>仅评估标题内容，不改变原任务与自动放行状态。超长标题请判断内容是否值得修；超长本身仍不符合交付限制。原批历史判定不变，新意见单独保存。</p>']
    for t in tasks:
        v=t['video']; out.append(f"<section><h2>{esc(t['blind_id'])} · {esc(t['category'])}</h2><p>原视频：<a target='_blank' href='https://www.bilibili.com/video/{esc(t['video_id'])}'> {esc(t['video_id'])}</a>　原任务状态：{esc(t['source_status'])}　路由：{esc(t['route'].get('action'))} / {esc(t['route'].get('reason'))}</p><p>原题：{esc(v.get('title'))}　作者：{esc(v.get('author'))}</p><details><summary>查看简介、字幕与 OCR 素材</summary><pre>{esc(json.dumps({k:v.get(k) for k in ('description','caption','asr_segments','ocr_segments')},ensure_ascii=False,indent=2))}</pre></details>")
        if not t['candidates']: out.append('<p>没有可恢复的未评候选。</p>')
        for x in t['candidates']:
            key = f"{t['task_id']}|{x['candidate_id']}|{x['hash']}"; err = '；'.join(str(e.get('code','')) + ' ' + str(e.get('reason','')) for e in x['errors'] if isinstance(e,dict)); d=x['decision']
            out.append(f"<article data-key='{esc(key)}'><h3>{esc(x['title'])}</h3><p>候选 {esc(x['candidate_id'])} · 槽位 {esc(x['slot'])} · 父候选 {esc(x['parent'])} · 系统状态 {esc(d.get('status') or '未记录')} / {esc(d.get('reason'))}</p><p class='warn'>机械检查：{esc(err or '未记录错误（不等于已交付）')}</p><textarea placeholder='意见；勉强或不可用请填写原因'></textarea><div><button onclick=\"save(this,'approved')\">内容可用</button><button onclick=\"save(this,'marginal')\">勉强</button><button onclick=\"save(this,'rejected')\">不可用</button> <span class='result'>未评</span></div></article>")
        out.append('</section>')
    out.append('''<script>async function refresh(){let d=await (await fetch('/decisions')).json();document.querySelectorAll('article[data-key]').forEach(a=>{let x=d[a.dataset.key];if(x){a.querySelector('.result').textContent='已评：'+x.decision;a.querySelector('textarea').value=x.reason||''}})}async function save(b,v){let a=b.closest('article'),reason=a.querySelector('textarea').value.trim();if(v!=='approved'&&!reason){alert('请填写原因');return}let r=await fetch('/decision',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({key:a.dataset.key,decision:v,reason})});let x=await r.json();a.querySelector('.result').textContent=r.ok?'已评：'+v:'保存失败：'+x.error}refresh()</script></html>''')
    return ''.join(out)


def main():
    p=argparse.ArgumentParser(); p.add_argument('--output',required=True); p.add_argument('--port',type=int,default=0); args=p.parse_args()
    out=Path(args.output).resolve(); out.mkdir(parents=True,exist_ok=True); tasks=load(); allowed={f"{t['task_id']}|{c['candidate_id']}|{c['hash']}" for t in tasks for c in t['candidates']}
    manifest=out/'supplement_manifest.json'
    if manifest.exists() and json.loads(manifest.read_text(encoding='utf-8')) != tasks: raise RuntimeError('补评清单已存在且来源发生变化，拒绝覆盖')
    if not manifest.exists(): manifest.write_text(json.dumps(tasks,ensure_ascii=False,indent=2),encoding='utf-8')
    dbpath=out/'supplement_feedback.db'
    with sqlite3.connect(dbpath) as db: db.execute('create table if not exists decisions (key text primary key, decision text not null, reason text not null, updated_at text default current_timestamp)')
    class Handler(BaseHTTPRequestHandler):
        def send(self,code,data,kind='application/json; charset=utf-8'):
            b=data.encode(); self.send_response(code);self.send_header('Content-Type',kind);self.send_header('Content-Length',str(len(b)));self.end_headers();self.wfile.write(b)
        def do_GET(self):
            if self.path=='/': return self.send(200,page(tasks),'text/html; charset=utf-8')
            if self.path=='/decisions':
                with sqlite3.connect(dbpath) as db: data={k:{'decision':d,'reason':r} for k,d,r in db.execute('select key,decision,reason from decisions')}
                return self.send(200,json.dumps(data,ensure_ascii=False))
            self.send(404,'{}')
        def do_POST(self):
            if self.path!='/decision': return self.send(404,'{}')
            try:
                length=int(self.headers.get('Content-Length','0'))
                if length>16384: raise ValueError('请求过大')
                data=json.loads(self.rfile.read(length)); key=data.get('key'); decision=data.get('decision'); reason=str(data.get('reason') or '').strip()
                if key not in allowed or decision not in ('approved','marginal','rejected') or (decision!='approved' and not reason): raise ValueError('候选或判断不合法')
                with sqlite3.connect(dbpath) as db: db.execute('insert or replace into decisions(key,decision,reason,updated_at) values(?,?,?,current_timestamp)',(key,decision,reason))
                self.send(200,'{}')
            except (ValueError,TypeError,json.JSONDecodeError) as e: self.send(400,json.dumps({'error':str(e)},ensure_ascii=False))
        def log_message(self,*args): pass
    server=ThreadingHTTPServer(('127.0.0.1',args.port),Handler); print(f'http://127.0.0.1:{server.server_port}',flush=True);server.serve_forever()

if __name__=='__main__': main()
