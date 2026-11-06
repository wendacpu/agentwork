"""对话 Agent HTTP 服务：回环 + session + SSE 流式。

与 diagnostic_server.py 的关系：独立入口、不同路由集；共用 _allowed 同源校验思路。
前端沿用单 HTML 常量模式（内嵌在 PAGE 中）。
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import ipaddress
import json
import os
import sqlite3
import threading
import time
import uuid
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit
from typing import Any

from .chat_agent import ChatClient, TurnBudget, build_messages
from .chat_tools import ToolContext
from .config import RunConfig
from .model import ModelError


_MAX_BODY = 65536
_SESSION_DB = 'runtime/chat/sessions.db'


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class _DeepSeekClient(ChatClient):
    def _post(self, payload: dict, key: str) -> dict:
        req = urllib.request.Request(
            self.base_url + '/chat/completions',
            data=json.dumps(payload, ensure_ascii=False).encode('utf-8'),
            headers={'Authorization': f'Bearer {key}', 'Content-Type': 'application/json'},
        )
        with urllib.request.build_opener(_NoRedirect()).open(req, timeout=self.timeout) as response:
            return json.loads(response.read().decode('utf-8'))


def _init_session_db(path: str) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(p) as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS sessions(id TEXT PRIMARY KEY, created_at TEXT, focus_json TEXT);
        CREATE TABLE IF NOT EXISTS messages(session_id TEXT, seq INTEGER, role TEXT, content_json TEXT, created_at TEXT, PRIMARY KEY(session_id,seq));
        CREATE TABLE IF NOT EXISTS turn_ledger(session_id TEXT, turn INTEGER, tool_calls_json TEXT, usage_json TEXT, outbound_chars INTEGER, illegal_refs INTEGER, created_at TEXT);
        """)


class SessionStore:
    def __init__(self, path: str):
        self.conn = sqlite3.connect(path, timeout=15)
        self.conn.row_factory = sqlite3.Row

    def create(self) -> str:
        sid = str(uuid.uuid4())
        self.conn.execute('INSERT INTO sessions VALUES(?,?,?)', (sid, time.strftime('%Y-%m-%dT%H:%M:%S'), '{}'))
        self.conn.commit()
        return sid

    def exists(self, sid: str) -> bool:
        return self.conn.execute('SELECT 1 FROM sessions WHERE id=?', (sid,)).fetchone() is not None

    def save_turn(self, sid: str, message: str, answer: str, focus: dict, budget: TurnBudget, final, illegal: int) -> None:
        now = time.strftime('%Y-%m-%dT%H:%M:%S')
        with self.conn:
            seq = self.conn.execute('SELECT COALESCE(MAX(seq), 0) FROM messages WHERE session_id=?', (sid,)).fetchone()[0]
            self.conn.executemany('INSERT INTO messages VALUES(?,?,?,?,?)', [
                (sid, seq + 1, 'user', json.dumps(message, ensure_ascii=False), now),
                (sid, seq + 2, 'assistant', json.dumps(answer, ensure_ascii=False), now),
            ])
            self.conn.execute('UPDATE sessions SET focus_json=? WHERE id=?', (json.dumps(focus, ensure_ascii=False), sid))
            self.conn.execute('INSERT INTO turn_ledger VALUES(?,?,?,?,?,?,?)',
                (sid, seq // 2 + 1, json.dumps(budget.calls, ensure_ascii=False),
                 json.dumps(final.usage, ensure_ascii=False), budget.total_outbound_chars, illegal, now))

    def get_focus(self, sid: str) -> dict:
        row = self.conn.execute('SELECT focus_json FROM sessions WHERE id=?', (sid,)).fetchone()
        return json.loads(row['focus_json'] or '{}') if row else {}

    def set_focus(self, sid: str, focus: dict) -> None:
        self.conn.execute('UPDATE sessions SET focus_json=? WHERE id=?', (json.dumps(focus, ensure_ascii=False), sid))
        self.conn.commit()

    def append_message(self, sid: str, role: str, content: Any) -> int:
        row = self.conn.execute('SELECT MAX(seq) as m FROM messages WHERE session_id=?', (sid,)).fetchone()
        seq = (row['m'] or 0) + 1
        self.conn.execute('INSERT INTO messages VALUES(?,?,?,?,?)', (sid, seq, role, json.dumps(content, ensure_ascii=False), time.strftime('%Y-%m-%dT%H:%M:%S')))
        self.conn.commit()
        return seq

    def load_messages(self, sid: str, limit: int = 20) -> list[dict]:
        rows = self.conn.execute('SELECT role, content_json FROM messages WHERE session_id=? ORDER BY seq DESC LIMIT ?', (sid, limit)).fetchall()
        out = []
        for r in reversed(rows):
            try:
                content = json.loads(r['content_json'])
                if r['role'] in ('user', 'assistant') and isinstance(content, str):
                    out.append({'role': r['role'], 'content': content})
            except (json.JSONDecodeError, TypeError):
                pass
        return out

    def record_turn(self, sid: str, turn: int, calls: list, usage: dict, outbound: int, illegal: int) -> None:
        self.conn.execute('INSERT INTO turn_ledger VALUES(?,?,?,?,?,?,?)',
                          (sid, turn, json.dumps(calls, ensure_ascii=False), json.dumps(usage, ensure_ascii=False),
                           outbound, illegal, time.strftime('%Y-%m-%dT%H:%M:%S')))
        self.conn.commit()


PAGE = r'''<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>任务问诊助手</title><style>
:root{--bg:#f7f8fa;--card:#fff;--ink:#1a1a2e;--muted:#6b7280;--accent:#2563eb;--border:#e5e7eb}*{box-sizing:border-box}body{margin:0;font:15px/1.6 system-ui,-apple-system,"PingFang SC",sans-serif;background:var(--bg);color:var(--ink)}main{max-width:760px;margin:0 auto;padding:20px 16px 100px}h1{font-size:1.3rem;margin:0 0 4px}.sub{color:var(--muted);font-size:.85rem;margin-bottom:20px}.messages{display:flex;flex-direction:column;gap:14px;margin-bottom:20px}.msg{padding:12px 16px;border-radius:12px;max-width:92%;white-space:pre-wrap;overflow-wrap:anywhere}.msg.user{align-self:flex-end;background:var(--accent);color:#fff;border-bottom-right-radius:4px}.msg.agent{align-self:flex-start;background:var(--card);border:1px solid var(--border);border-bottom-left-radius:4px}.msg.system-note{align-self:center;background:#fef9c3;border:1px solid #fde68a;font-size:.82rem;color:#92400e;border-radius:8px;padding:8px 14px}.tool-trace{font-size:.78rem;color:var(--muted);margin-top:6px}.citation{display:inline-block;background:#eff6ff;border:1px solid #bfdbfe;border-radius:4px;padding:1px 6px;font-size:.75rem;color:#1d4ed8;cursor:pointer;margin:0 2px}.composer{position:fixed;bottom:0;left:50%;transform:translateX(-50%);width:min(760px,100%-32px);background:var(--card);border:1px solid var(--border);border-radius:14px;padding:10px 14px;display:flex;gap:10px;align-items:end;box-shadow:0 -2px 12px rgba(0,0,0,.06)}textarea{flex:1;border:none;outline:none;font:inherit;resize:none;min-height:24px;max-height:120px;line-height:1.5}.send-btn{background:var(--accent);color:#fff;border:none;border-radius:8px;padding:8px 16px;font:inherit;font-weight:600;cursor:pointer;white-space:nowrap}.send-btn:disabled{opacity:.4;cursor:not-allowed}.chips{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:16px}.chip{background:var(--card);border:1px solid var(--border);border-radius:20px;padding:6px 14px;font-size:.82rem;cursor:pointer;color:var(--ink)}.chip:hover{border-color:var(--accent);color:var(--accent)}@media(max-width:600px){main{padding:12px 8px 90px}.msg{max-width:96%}}
</style></head><body>
<main>
<h1>任务问诊助手</h1><p class="sub">只读诊断。每轮将问题、最近 6 条会话、当前焦点及工具查询到的白名单数据库内容发送至 DeepSeek（deepseek-chat），可能产生费用；最多 8 次模型请求，每次输出最多 2048 token（整轮最多 16384 输出 token；输入 token 不设独立上限）。引用仅为记录标识，内容未自动核实。会话、焦点及调用记录保存在配置输出目录的 chat/sessions.db；需要清除时，请关闭服务后手动删除该数据库及其 SQLite 辅助文件。</p><label><input type="checkbox" id="consent"> 我同意本轮将上述数据发送至 DeepSeek 并承担可能产生的费用</label>
<div class="chips" id="chips"><button class="chip" data-q="有哪些视频任务？">有哪些视频任务？</button><button class="chip" data-q="帮我看看视频的主旨是什么">看看视频主旨</button><button class="chip" data-q="这个标题的判断依据是什么？">标题的判断依据</button></div>
<div class="messages" id="messages"></div>
</main>
<form class="composer" id="form"><textarea id="input" placeholder="问点什么…" rows="1" maxlength="2000"></textarea><button class="send-btn" id="send" type="submit" disabled>发送</button></form>
<script>
(()=>{'use strict';const $=id=>document.getElementById(id);const msgs=$('messages'),input=$('input'),send=$('send');let sessionId=null,busy=false;
async function api(path,body){const r=await fetch(path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});if(!r.ok)throw new Error((await r.json().catch(()=>({}))).error||`HTTP ${r.status}`);return r.json()}
async function init(){try{const d=await api('/api/session',{});sessionId=d.session_id;if(!sessionId)throw new Error('无有效会话');send.disabled=false}catch(e){addMsg('system-note','会话初始化失败，请刷新页面重试：'+e.message)}}
function addMsg(cls,text,trace){const el=document.createElement('div');el.className='msg '+cls;el.textContent=text;if(trace){const t=document.createElement('div');t.className='tool-trace';t.textContent=trace;el.append(t)}msgs.append(el);el.scrollIntoView({behavior:'smooth',block:'nearest'});return el}
function renderCitations(el,cites,records){if(!cites||!cites.length)return;cites.forEach(c=>{const d=document.createElement('details'),s=document.createElement('summary'),pre=document.createElement('pre');s.className='citation';s.textContent=c+' · 查看记录（未自动核实）';pre.textContent=records&&Object.hasOwn(records,c)?JSON.stringify(records[c],null,2):'本轮未提供原文';d.append(s,pre);el.append(d)})}
async function ask(text){if(busy||!sessionId||!text.trim())return;if(!$('consent').checked){addMsg('system-note','请先勾选本轮云端处理同意。');return}busy=true;send.disabled=true;input.readOnly=true;addMsg('user',text);$('consent').checked=false;
const el=addMsg('agent','思考中…');try{const d=await api('/api/chat',{session_id:sessionId,message:text,confirm_cloud:true});if(input.value===text){input.value='';input.style.height='auto'}el.textContent=d.answer||'(空回答)';renderCitations(el,d.citations,d.citation_records);if(d.tool_trace)addMsg('agent','',d.tool_trace)}catch(e){if(!input.value)input.value=text;el.textContent='查询失败：'+e.message}finally{busy=false;send.disabled=!sessionId;input.readOnly=false;input.focus()}}
$('form').addEventListener('submit',e=>{e.preventDefault();ask(input.value)});
input.addEventListener('keydown',e=>{if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();ask(input.value)}});
input.addEventListener('input',()=>{input.style.height='auto';input.style.height=Math.min(input.scrollHeight,120)+'px'});
document.querySelectorAll('.chip').forEach(c=>c.addEventListener('click',()=>ask(c.dataset.q)));
init()})();
</script></body></html>'''

# 只允许页面中固定脚本；不允许内联事件处理或外部脚本。
_SCRIPT = PAGE.split('<script>', 1)[1].split('</script>', 1)[0]
_SCRIPT_HASH = base64.b64encode(hashlib.sha256(_SCRIPT.encode('utf-8')).digest()).decode('ascii')
_CSP = ("default-src 'none'; script-src 'sha256-" + _SCRIPT_HASH + "'; "
        "style-src 'unsafe-inline'; connect-src 'self'; base-uri 'none'; "
        "form-action 'self'; frame-ancestors 'none'")


def make_chat_server(db_aliases: dict[str, str], host: str = '127.0.0.1', port: int = 8770,
                     config: RunConfig | None = None) -> ThreadingHTTPServer:
    if not ipaddress.ip_address(host).is_loopback:
        raise ValueError('对话服务仅允许监听本机回环地址')
    if not db_aliases:
        raise ValueError('至少需要一个白名单批次库')
    for alias, path in db_aliases.items():
        if not Path(path).is_file():
            raise FileNotFoundError(f'批次 {alias} 的数据库不存在：{path}')

    cfg = config or RunConfig()
    configured = (cfg.model_provider == 'deepseek' and
                  cfg.base_url in ('https://api.deepseek.com', 'https://api.deepseek.com/v1') and
                  cfg.generation_model == 'deepseek-chat' and bool(cfg.api_key_env) and
                  bool(os.getenv(cfg.api_key_env)))
    client = (_DeepSeekClient(cfg.base_url, cfg.api_key_env, cfg.generation_model,
                         timeout=min(cfg.model_timeout, 90)) if configured else None)
    locks: dict[str, threading.Lock] = {}
    locks_guard = threading.Lock()

    session_path = str(Path(cfg.output_dir) / 'chat' / 'sessions.db')
    _init_session_db(session_path)

    class Handler(BaseHTTPRequestHandler):
        server_version = "ChatAgent/1.0"
        _lock = threading.Lock()

        def log_message(self, format, *args):
            return

        def _allowed(self) -> bool:
            host_header = self.headers.get('Host', '')
            try:
                if not ipaddress.ip_address(self.client_address[0]).is_loopback:
                    return False
                address = urlsplit('http://' + host_header)
                if address.hostname not in {'127.0.0.1', 'localhost', '::1'} or address.port != self.server.server_port:
                    return False
                origin = self.headers.get('Origin')
                if origin:
                    parsed = urlsplit(origin)
                    if parsed.scheme != 'http' or parsed.netloc != host_header:
                        return False
                site = self.headers.get('Sec-Fetch-Site', 'same-origin')
                if (self.command == 'GET' and urlsplit(self.path).path == '/' and
                        self.headers.get('Sec-Fetch-Mode') == 'navigate' and
                        self.headers.get('Sec-Fetch-Dest') == 'document' and not origin):
                    return site in {'same-origin', 'same-site', 'none', 'cross-site'}
                return site in {'same-origin', 'none'}
            except ValueError:
                return False

        def _send(self, status: int, data, ct: str = 'application/json; charset=utf-8') -> None:
            raw = (json.dumps(data, ensure_ascii=False) if ct.startswith('application/') else data).encode('utf-8')
            self.send_response(status)
            self.send_header('Content-Type', ct)
            self.send_header('Content-Length', str(len(raw)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Content-Security-Policy', _CSP)
            self.send_header('Referrer-Policy', 'no-referrer')
            self.end_headers()
            try:
                self.wfile.write(raw)
            except BrokenPipeError:
                pass

        def do_GET(self):
            if not self._allowed():
                self._send(403, {'error': '仅允许本机同源访问'})
                return
            if urlsplit(self.path).path == '/':
                self._send(200, PAGE, 'text/html; charset=utf-8')
            else:
                self._send(404, {'error': '接口不存在'})

        def do_POST(self):
            if not self._allowed():
                self._send(403, {'error': '仅允许本机同源访问'})
                return
            path = urlsplit(self.path).path
            if self.headers.get_content_type() != 'application/json':
                self._send(415, {'error': '仅接受 JSON 请求'})
                return
            try:
                length = int(self.headers.get('Content-Length', '0'))
                if not 0 < length <= _MAX_BODY:
                    raise ValueError('请求过大或为空')
                payload = json.loads(self.rfile.read(length))
            except (ValueError, UnicodeDecodeError):
                self._send(400, {'error': '请求 JSON 无效或超出大小限制'})
                return
            if not isinstance(payload, dict):
                self._send(400, {'error': '请求必须是 JSON 对象'})
                return

            if path == '/api/session':
                store = SessionStore(session_path)
                sid = store.create()
                store.conn.close()
                self._send(200, {'session_id': sid})
                return

            if path == '/api/chat':
                sid = payload.get('session_id')
                message = payload.get('message')
                if not isinstance(sid, str) or not isinstance(message, str) or not message.strip() or len(message) > 2000:
                    self._send(400, {'error': '会话或消息无效；消息最多 2000 字符'})
                    return
                if payload.get('confirm_cloud') is not True:
                    self._send(403, {'error': '请明确同意本轮发送至 DeepSeek'})
                    return
                if client is None:
                    self._send(503, {'error': 'DeepSeek 未配置：须显式设置 provider、官方地址、deepseek-chat 和 API key 环境变量'})
                    return
                message = message.strip()
                try:
                    probe = SessionStore(session_path)
                    try:
                        exists = probe.exists(sid)
                    finally:
                        probe.conn.close()
                    if not exists:
                        self._send(404, {'error': '会话不存在，请重新创建'})
                        return
                    with locks_guard:
                        lock = locks.setdefault(sid, threading.Lock())
                    with lock:
                        store = SessionStore(session_path)
                        try:
                            if not store.exists(sid):
                                self._send(404, {'error': '会话不存在，请重新创建'})
                                return
                            history = store.load_messages(sid)
                            focus = store.get_focus(sid)
                            ctx = ToolContext(db_aliases=dict(db_aliases), default_alias=next(iter(db_aliases)), focus=focus)
                            budget = TurnBudget(max_tool_steps=8)
                            final = client.run_turn(build_messages(focus, history, message), ctx, budget)
                            if not isinstance(final.text, str):
                                raise ValueError('回答类型无效')
                            records = final.citation_records
                            legal_cites = [c for c in final.citations if c in budget.refs_seen and c in records]
                            illegal = len(final.citations) - len(legal_cites)
                            tool_trace = ' → '.join(c['tool'] for c in budget.calls) if budget.calls else ''
                            result = {'answer': final.text, 'citations': legal_cites,
                                      'citation_records': {c: records[c] for c in legal_cites},
                                      'tool_trace': tool_trace, 'steps': final.steps}
                            store.save_turn(sid, message, final.text, ctx.focus, budget, final, illegal)
                        finally:
                            store.conn.close()
                    self._send(200, result)
                except ModelError:
                    self._send(502, {'error': '模型不可用，请稍后重试'})
                except Exception:
                    self._send(500, {'error': '处理失败，请稍后重试'})
                return

            self._send(404, {'error': '接口不存在'})

    return ThreadingHTTPServer((host, port), Handler)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description='本机对话式任务问诊 Agent')
    parser.add_argument('--db', action='append', required=True, help='白名单批次库（可重复），格式 alias=path 或直接路径')
    parser.add_argument('--config', help='RunConfig JSON 路径（可选）')
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=8770)
    args = parser.parse_args(argv)

    aliases: dict[str, str] = {}
    for item in args.db:
        if '=' in item:
            alias, path = item.split('=', 1)
        else:
            path = item
            alias = Path(path).parent.name or 'default'
        aliases[alias] = str(Path(path).resolve())

    config = RunConfig.from_json(args.config) if args.config else RunConfig()
    config.apply_env()

    server = make_chat_server(aliases, args.host, args.port, config)
    print(f'对话问诊台：http://{args.host}:{server.server_port}/', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
