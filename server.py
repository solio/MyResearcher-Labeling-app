#!/usr/bin/env python3
"""MyResearcher 单人手机标注工具 — 唯一后端入口。

存储后端由配置文件决定（--config，默认项目根 config.json）：sqlite（零依赖）或 mysql
（需 pip install pymysql）。HTTP 层与业务校验与后端无关。
"""

import argparse
import json
import socket
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from storage import (
    DEFAULT_DB,
    DEFAULT_JSONL,
    SCHEMA_PATH,
    ConfigError,
    SqliteStore,
    create_store,
    load_config,
)

PROJECT_ROOT = Path(__file__).resolve().parent
STATIC_DIR = PROJECT_ROOT / "static"

# 兼容别名：旧代码/测试直接引用 server.Store
Store = SqliteStore

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".webmanifest": "application/manifest+json",
    ".png": "image/png",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
    ".txt": "text/plain; charset=utf-8",
}


class Handler(BaseHTTPRequestHandler):
    store = None
    protocol_version = "HTTP/1.1"
    server_version = "MyResearcherLabeler/1.1"

    def log_message(self, fmt, *args):
        sys.stderr.write("[%s] %s\n" % (self.log_date_time_string(), fmt % args))

    def _json(self, code, obj):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        parsed = urlparse(self.path)
        try:
            if parsed.path == "/api/resume":
                self._json(200, self.store.get_resume())
            elif parsed.path == "/api/batches":
                self._json(200, self.store.list_batches())
            elif parsed.path == "/api/assignments":
                qs = parse_qs(parsed.query)
                batch_id = (qs.get("batch_id") or [""])[0]
                head = (qs.get("head") or [""])[0]
                if not batch_id or not head:
                    return self._json(400, {"error": "batch_id 与 head 必填"})
                if head not in self.store.head_order():
                    return self._json(400, {"error": f"未知 head: {head}"})
                self._json(200, self.store.list_assignments(batch_id, head))
            elif parsed.path == "/api/assignment":
                qs = parse_qs(parsed.query)
                aid = (qs.get("id") or [""])[0]
                data = self.store.get_assignment(aid)
                if data is None:
                    return self._json(404, {"error": "assignment 不存在"})
                self._json(200, data)
            else:
                self._static(parsed.path)
        except BrokenPipeError:
            pass
        except Exception as exc:
            try:
                self._json(500, {"error": str(exc)})
            except Exception:
                pass

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path != "/api/annotations":
            return self._json(404, {"error": "not found"})
        try:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            body = json.loads(raw.decode("utf-8")) if raw else None
            if not isinstance(body, dict):
                raise ValueError("请求体必须是 JSON 对象")
            aid = body.get("assignment_id")
            if not isinstance(aid, str) or not aid:
                raise ValueError("assignment_id 必填")
            if "answer" not in body and body.get("disposition") is None:
                raise ValueError("answer 与 disposition 不能同时为空")
            is_final = body.get("is_final", False)
            if isinstance(is_final, int) and is_final in (0, 1):
                is_final = bool(is_final)
            if not isinstance(is_final, bool):
                raise ValueError("is_final 必须是布尔值")
            record = self.store.upsert_annotation(aid, body.get("answer"), body.get("disposition"), is_final)
            self._json(200, {"ok": True, **record})
        except ValueError as exc:
            self._json(400, {"error": str(exc)})
        except KeyError as exc:
            self._json(404, {"error": f"assignment 不存在: {exc}"})
        except BrokenPipeError:
            pass
        except Exception as exc:
            try:
                self._json(500, {"error": str(exc)})
            except Exception:
                pass

    def _static(self, path):
        if path in ("", "/"):
            path = "/index.html"
        target = (STATIC_DIR / unquote(path).lstrip("/")).resolve()
        if target != STATIC_DIR and STATIC_DIR not in target.parents:
            return self._json(403, {"error": "forbidden"})
        if not target.is_file():
            return self._json(404, {"error": "not found"})
        data = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", CONTENT_TYPES.get(target.suffix.lower(), "application/octet-stream"))
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)


def lan_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def main():
    ap = argparse.ArgumentParser(description="MyResearcher 单人手机标注工具后端")
    ap.add_argument("--config", default=None,
                    help="配置文件路径（默认项目根 config.json；不存在则用 sqlite 默认值）")
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--db", default=None, help="仅 sqlite 模式：覆盖配置中的 db_path")
    ap.add_argument("--jsonl", default=None, help="覆盖配置中的 jsonl_path")
    args = ap.parse_args()

    try:
        cfg = load_config(args.config)
    except ConfigError as exc:
        print(f"[server] 配置错误: {exc}", file=sys.stderr)
        sys.exit(2)
    if (args.db or args.jsonl) and cfg["storage"] != "sqlite":
        print("[server] --db/--jsonl 仅支持 sqlite 模式；mysql 模式请在配置文件中设置", file=sys.stderr)
        sys.exit(2)
    if args.db:
        cfg["sqlite"]["db_path"] = args.db
    if args.jsonl:
        cfg["jsonl_path"] = args.jsonl

    glossary = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    try:
        Handler.store = create_store(cfg, glossary)
    except ConfigError as exc:
        print(f"[server] {exc}", file=sys.stderr)
        sys.exit(2)
    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    httpd.daemon_threads = True
    backend = (f"mysql://{cfg['mysql']['user']}@{cfg['mysql']['host']}:{cfg['mysql']['port']}"
               f"/{cfg['mysql']['database']}") if cfg["storage"] == "mysql" else cfg["sqlite"]["db_path"]
    print(f"MyResearcher Labeler 已启动  schema={glossary.get('schema_version')}  storage={cfg['storage']}")
    print(f"  本机:   http://127.0.0.1:{args.port}")
    print(f"  局域网: http://{lan_ip()}:{args.port}   （手机连同一 Wi-Fi 打开此地址）")
    print(f"  后端:   {backend}")
    print(f"  审计:   {cfg['jsonl_path']}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")


if __name__ == "__main__":
    main()
