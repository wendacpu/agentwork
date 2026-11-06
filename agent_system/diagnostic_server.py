"""本机只读诊断入口：不构建 Pipeline，不改写任务数据库。"""
from __future__ import annotations

import argparse
import ipaddress
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .diagnostic import diagnose, get_task_info, list_tasks
from .diagnostic_page import PAGE
from .diagnostic_summary import summarize
from .diagnostic_chat import answer_local, LocalChatError

_MAX_BODY = 16384


def make_server(db_path: str, host: str = "127.0.0.1", port: int = 8766) -> ThreadingHTTPServer:
    if not ipaddress.ip_address(host).is_loopback:
        raise ValueError("诊断服务仅允许监听本机回环地址")
    db = Path(db_path).resolve()
    if not db.is_file():
        raise FileNotFoundError(f"任务库不存在：{db}")
    # 先验证数据库与表确实可读；不会创建库或设置 WAL。
    list_tasks(str(db))

    class Handler(BaseHTTPRequestHandler):
        server_version = "VideoDiagnostic/1.0"

        def log_message(self, format, *args):
            # 不记录任务内容和输入问题。
            return

        def _allowed(self) -> bool:
            host_header = self.headers.get("Host", "")
            try:
                address = urlsplit("http://" + host_header)
                allowed_hosts = {"127.0.0.1", "localhost", "::1"}
                if address.hostname not in allowed_hosts or address.port != self.server.server_port:
                    return False
                origin = self.headers.get("Origin")
                if origin:
                    parsed = urlsplit(origin)
                    if parsed.scheme != "http" or parsed.netloc != host_header:
                        return False
                # 拒绝其他网站通过浏览器发起的跨站请求。
                return self.headers.get("Sec-Fetch-Site", "same-origin") in {"same-origin", "none"}
            except ValueError:
                return False

        def _send(self, status: int, data, content_type: str = "application/json; charset=utf-8") -> None:
            raw = (json.dumps(data, ensure_ascii=False) if content_type.startswith("application/json") else data).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'")
            self.end_headers()
            try:
                self.wfile.write(raw)
            except BrokenPipeError:
                # 客户端切换任务后会中止旧请求（尤其长耗时的本机模型问答）；
                # 写入断开属正常现象，不能让 socketserver 把堆栈喷进终端。
                pass

        def do_GET(self):
            if not self._allowed():
                self._send(403, {"error": "仅允许本机同源访问"})
                return
            url = urlsplit(self.path)
            query = parse_qs(url.query)
            try:
                if url.path == "/":
                    self._send(200, PAGE, "text/html; charset=utf-8")
                elif url.path == "/api/tasks":
                    self._send(200, {"tasks": list_tasks(str(db))})
                elif url.path == "/api/task":
                    self._send(200, get_task_info(str(db), query.get("task_id", [""])[0]))
                else:
                    self._send(404, {"error": "接口不存在"})
            except (KeyError, ValueError) as exc:
                self._send(404, {"error": str(exc)})

        def do_POST(self):
            if not self._allowed():
                self._send(403, {"error": "仅允许本机同源访问"})
                return
            path = urlsplit(self.path).path
            if path not in {"/api/diagnose", "/api/chat"}:
                self._send(404, {"error": "接口不存在"})
                return
            if self.headers.get_content_type() != "application/json":
                self._send(415, {"error": "仅接受 JSON 请求"})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= _MAX_BODY:
                    raise ValueError("请求过大或为空")
                payload = json.loads(self.rfile.read(length))
                if not isinstance(payload, dict):
                    raise ValueError("请求必须是 JSON 对象")
                for key in ("task_id", "scenario", "candidate_id", "question"):
                    if payload.get(key) is not None and not isinstance(payload[key], str):
                        raise ValueError(f"{key} 必须是字符串")
                question = payload.get("question", "")
                result = diagnose(str(db), payload.get("task_id", ""), payload.get("scenario", ""), payload.get("candidate_id") or None, question if path == "/api/diagnose" else "")
                result['summary'] = summarize(result)
                if path == '/api/chat':
                    if payload.get('confirm_local_model') is not True:
                        raise ValueError('必须明确同意使用本机模型')
                    if not question.strip():
                        raise ValueError('请输入问题')
                    if result['scenario'] != 'understanding' and result['candidate_id'] is None and len(result['candidates']) > 1:
                        raise ValueError('请先选择候选标题')
                    history = payload.get('history', [])
                    if not isinstance(history, list):
                        raise ValueError('对话历史格式错误')
                    result['chat'] = answer_local(result, question, history=history)
                self._send(200, result)
            except LocalChatError:
                self._send(503, {"error": "本机模型不可用或输出无效；请检查 Ollama 和模型后重试。"})
            except (ValueError, KeyError, json.JSONDecodeError) as exc:
                self._send(400, {"error": str(exc)})

    return ThreadingHTTPServer((host, port), Handler)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="本机视频任务只读诊断台")
    parser.add_argument("--db", required=True, help="要查看的任务数据库")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8766)
    args = parser.parse_args(argv)
    server = make_server(args.db, args.host, args.port)
    print(f"只读诊断台：http://{args.host}:{server.server_port}/", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
