#!/usr/bin/env python3
"""MyResearcher 单人手机标注工具 — 唯一后端入口。仅 Python 标准库。"""

import argparse
import json
import os
import socket
import sqlite3
import sys
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

PROJECT_ROOT = Path(__file__).resolve().parent
STATIC_DIR = PROJECT_ROOT / "static"
SCHEMA_PATH = PROJECT_ROOT / "schema" / "annotation-schema.v1.json"
DEFAULT_DB = PROJECT_ROOT / "data" / "labeler.db"
DEFAULT_JSONL = PROJECT_ROOT / "data" / "annotations.jsonl"

DDL = """
CREATE TABLE IF NOT EXISTS batches(
  id TEXT PRIMARY KEY,
  name TEXT,
  created_at TEXT,
  schema_version TEXT
);
CREATE TABLE IF NOT EXISTS samples(
  batch_id TEXT,
  id TEXT,
  title TEXT,
  content TEXT,
  metadata_json TEXT,
  PRIMARY KEY(batch_id, id)
);
CREATE TABLE IF NOT EXISTS assignments(
  id TEXT PRIMARY KEY,
  batch_id TEXT,
  sample_id TEXT,
  head TEXT,
  position INTEGER,
  status TEXT DEFAULT 'pending'
);
CREATE TABLE IF NOT EXISTS annotations(
  assignment_id TEXT PRIMARY KEY,
  answer_json TEXT,
  disposition TEXT,
  is_final INTEGER,
  revision INTEGER,
  updated_at TEXT
);
"""

# 不可逆的处置：视为该条已完成，不再进入 /api/resume；"稍后再看"仍是未完成。
TERMINAL_DISPOSITIONS = ("跳过", "无法判断", "缺少上下文")
ALL_DISPOSITIONS = ("无法判断", "缺少上下文", "跳过", "稍后再看")


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Store:
    """sqlite3 + annotations.jsonl 的统一存取，线程安全。"""

    def __init__(self, db_path, jsonl_path, glossary):
        self.glossary = glossary
        self.lock = threading.RLock()
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        with self.lock:
            self.conn.executescript(DDL)
            self.conn.commit()
        self.jsonl_path = Path(jsonl_path)
        self.jsonl_path.parent.mkdir(parents=True, exist_ok=True)

    # ---- schema ----
    def head_order(self):
        return self.glossary.get("head_order", [])

    def head_glossary(self, head):
        return self.glossary.get("heads", {}).get(head)

    def head_type(self, head):
        return (self.head_glossary(head) or {}).get("type", "single")

    def head_labels(self, head):
        return [l["id"] for l in (self.head_glossary(head) or {}).get("labels", [])]

    # ---- queries ----
    def list_batches(self):
        with self.lock:
            rows = self.conn.execute(
                """SELECT b.id, b.name, b.created_at, b.schema_version,
                          COUNT(a.id) AS total,
                          SUM(CASE WHEN n.is_final=1
                                     OR n.disposition IN (?, ?, ?)
                                   THEN 1 ELSE 0 END) AS done
                   FROM batches b
                   LEFT JOIN assignments a ON a.batch_id = b.id
                   LEFT JOIN annotations n ON n.assignment_id = a.id
                   GROUP BY b.id
                   ORDER BY b.created_at DESC, b.id""",
                TERMINAL_DISPOSITIONS,
            ).fetchall()
        return [dict(r) for r in rows]

    def get_resume(self):
        with self.lock:
            row = self.conn.execute(
                """SELECT a.batch_id, a.id
                   FROM assignments a
                   LEFT JOIN annotations n ON n.assignment_id = a.id
                   WHERE n.assignment_id IS NULL
                      OR (IFNULL(n.is_final, 0) = 0
                          AND (n.disposition IS NULL
                               OR n.disposition NOT IN (?, ?, ?)))
                   ORDER BY CASE WHEN n.updated_at IS NULL THEN 1 ELSE 0 END,
                            n.updated_at DESC, a.position ASC, a.id
                   LIMIT 1""",
                TERMINAL_DISPOSITIONS,
            ).fetchone()
        if row is None:
            return {"batch_id": None, "assignment_id": None}
        return {"batch_id": row["batch_id"], "assignment_id": row["id"]}

    def list_assignments(self, batch_id, head):
        with self.lock:
            rows = self.conn.execute(
                """SELECT a.id, a.batch_id, a.sample_id, a.head, a.position, a.status,
                          s.title,
                          n.answer_json, n.disposition, n.is_final, n.revision, n.updated_at
                   FROM assignments a
                   JOIN samples s ON s.batch_id = a.batch_id AND s.id = a.sample_id
                   LEFT JOIN annotations n ON n.assignment_id = a.id
                   WHERE a.batch_id = ? AND a.head = ?
                   ORDER BY a.position ASC, a.id""",
                (batch_id, head),
            ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            raw_answer = d.pop("answer_json")
            d["answer"] = json.loads(raw_answer) if raw_answer is not None else None
            d["is_final"] = bool(d["is_final"]) if d["is_final"] is not None else False
            out.append(d)
        return out

    def get_assignment(self, assignment_id):
        with self.lock:
            row = self.conn.execute(
                """SELECT a.id, a.batch_id, a.sample_id, a.head, a.position, a.status,
                          s.title, s.content, s.metadata_json,
                          b.schema_version,
                          n.answer_json, n.disposition, n.is_final, n.revision, n.updated_at
                   FROM assignments a
                   JOIN samples s ON s.batch_id = a.batch_id AND s.id = a.sample_id
                   JOIN batches b ON b.id = a.batch_id
                   LEFT JOIN annotations n ON n.assignment_id = a.id
                   WHERE a.id = ?""",
                (assignment_id,),
            ).fetchone()
        if row is None:
            return None
        d = dict(row)
        answer = json.loads(d.pop("answer_json")) if d["answer_json"] is not None else None
        metadata = json.loads(d.pop("metadata_json") or "{}")
        return {
            "assignment": {k: d[k] for k in ("id", "batch_id", "sample_id", "head", "position", "status")},
            "sample": {"id": d["sample_id"], "title": d["title"], "content": d["content"], "metadata": metadata},
            "answer": answer,
            "disposition": d["disposition"],
            "is_final": bool(d["is_final"]) if d["is_final"] is not None else False,
            "revision": d["revision"] or 0,
            "updated_at": d["updated_at"],
            "schema_version": d["schema_version"],
            "glossary": self.head_glossary(d["head"]),
        }

    # ---- writes ----
    def upsert_annotation(self, assignment_id, answer, disposition, is_final):
        with self.lock:
            arow = self.conn.execute(
                "SELECT id, batch_id, sample_id, head FROM assignments WHERE id = ?",
                (assignment_id,),
            ).fetchone()
            if arow is None:
                raise KeyError(assignment_id)
            head = arow["head"]
            if answer is None and not disposition:
                raise ValueError("answer 与 disposition 不能同时为空")
            if answer is not None:
                labels = self.head_labels(head)
                if self.head_type(head) == "multi":
                    if not isinstance(answer, list) or not all(isinstance(x, str) for x in answer):
                        raise ValueError("reasoning_tags 的 answer 必须是字符串数组")
                    unknown = [x for x in answer if x not in labels]
                    if unknown:
                        raise ValueError(f"未知标签: {unknown}")
                    seen = set()
                    answer = [x for x in answer if not (x in seen or seen.add(x))]
                else:
                    if not isinstance(answer, str):
                        raise ValueError(f"{head} 的 answer 必须是字符串")
                    if answer not in labels:
                        raise ValueError(f"未知标签: {answer}")
            if disposition is not None and disposition not in ALL_DISPOSITIONS:
                raise ValueError(f"非法 disposition: {disposition}")

            prev = self.conn.execute(
                "SELECT revision FROM annotations WHERE assignment_id = ?", (assignment_id,)
            ).fetchone()
            revision = (prev["revision"] if prev else 0) + 1
            updated_at = now_iso()
            is_final_i = 1 if is_final else 0
            self.conn.execute(
                """INSERT INTO annotations(assignment_id, answer_json, disposition, is_final, revision, updated_at)
                   VALUES(?, ?, ?, ?, ?, ?)
                   ON CONFLICT(assignment_id) DO UPDATE SET
                     answer_json = excluded.answer_json,
                     disposition = excluded.disposition,
                     is_final = excluded.is_final,
                     revision = excluded.revision,
                     updated_at = excluded.updated_at""",
                (
                    assignment_id,
                    json.dumps(answer, ensure_ascii=False) if answer is not None else None,
                    disposition,
                    is_final_i,
                    revision,
                    updated_at,
                ),
            )
            status = "completed" if (is_final or disposition in TERMINAL_DISPOSITIONS) else "in_progress"
            self.conn.execute("UPDATE assignments SET status = ? WHERE id = ?", (status, assignment_id))
            brow = self.conn.execute(
                "SELECT schema_version FROM batches WHERE id = ?", (arow["batch_id"],)
            ).fetchone()
            self.conn.commit()
            record = {
                "assignment_id": assignment_id,
                "batch_id": arow["batch_id"],
                "sample_id": arow["sample_id"],
                "head": head,
                "answer": answer,
                "disposition": disposition,
                "is_final": bool(is_final_i),
                "revision": revision,
                "updated_at": updated_at,
                "schema_version": brow["schema_version"] if brow else None,
            }
            with open(self.jsonl_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
            return record


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
    server_version = "MyResearcherLabeler/1.0"

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
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--db", default=str(DEFAULT_DB))
    ap.add_argument("--jsonl", default=str(DEFAULT_JSONL))
    args = ap.parse_args()

    glossary = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    Handler.store = Store(args.db, args.jsonl, glossary)
    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    httpd.daemon_threads = True
    print(f"MyResearcher Labeler 已启动  schema={glossary.get('schema_version')}")
    print(f"  本机:   http://127.0.0.1:{args.port}")
    print(f"  局域网: http://{lan_ip()}:{args.port}   （手机连同一 Wi-Fi 打开此地址）")
    print(f"  数据库: {args.db}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")


if __name__ == "__main__":
    main()
