#!/usr/bin/env python3
"""存储层：配置加载 + sqlite/mysql 双后端。server.py 与 tools 共用。

配置文件（默认项目根 config.json，敏感含凭据，已 gitignore）：
  {
    "storage": "sqlite" | "mysql",
    "jsonl_path": "data/annotations.jsonl",
    "sqlite": {"db_path": "data/labeler.db"},
    "mysql": {"host","port","user","password","database","charset","connect_timeout","ssl_ca"}
  }
相对路径按配置文件所在目录解析。校验 fail-closed：未知字段、缺字段一律 ConfigError。

sqlite 后端零依赖；mysql 后端需要 `pip install pymysql`（纯 Python 驱动），
仅在 storage=mysql 时才会 import，sqlite 模式保持零安装。
"""

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
SCHEMA_PATH = PROJECT_ROOT / "schema" / "annotation-schema.v1.json"
DEFAULT_DB = PROJECT_ROOT / "data" / "labeler.db"
DEFAULT_JSONL = PROJECT_ROOT / "data" / "annotations.jsonl"

# 不可逆的处置：视为该条已完成，不再进入 /api/resume；"稍后再看"仍是未完成。
TERMINAL_DISPOSITIONS = ("跳过", "无法判断", "缺少上下文")
ALL_DISPOSITIONS = ("无法判断", "缺少上下文", "跳过", "稍后再看")

SQLITE_DDL = """
CREATE TABLE IF NOT EXISTS batches(
  id TEXT PRIMARY KEY,
  name TEXT,
  created_at TEXT,
  schema_version TEXT,
  archived_at TEXT
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

# MySQL 尺寸约束：batch_id ≤64 字符、sample_id ≤255 字符、head ≤64 字符；
# 超限会在导入事务内报错回滚（fail-closed），不会写半截数据。
MYSQL_DDL = (
    """CREATE TABLE IF NOT EXISTS batches(
  id VARCHAR(64) PRIMARY KEY,
  name VARCHAR(255),
  created_at VARCHAR(32),
  schema_version VARCHAR(64),
  archived_at VARCHAR(32)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci""",
    """CREATE TABLE IF NOT EXISTS samples(
  batch_id VARCHAR(64),
  id VARCHAR(255),
  title TEXT,
  content MEDIUMTEXT,
  metadata_json MEDIUMTEXT,
  PRIMARY KEY(batch_id, id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci""",
    """CREATE TABLE IF NOT EXISTS assignments(
  id VARCHAR(512) PRIMARY KEY,
  batch_id VARCHAR(64),
  sample_id VARCHAR(255),
  head VARCHAR(64),
  position INT,
  status VARCHAR(32) DEFAULT 'pending'
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci""",
    """CREATE TABLE IF NOT EXISTS annotations(
  assignment_id VARCHAR(512) PRIMARY KEY,
  answer_json MEDIUMTEXT,
  disposition VARCHAR(64),
  is_final TINYINT,
  revision INT,
  updated_at VARCHAR(32)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci""",
)

CONFIG_TOP_KEYS = {"storage", "jsonl_path", "sqlite", "mysql"}
SQLITE_KEYS = {"db_path"}
MYSQL_KEYS = {"host", "port", "user", "password", "database", "charset", "connect_timeout", "ssl_ca"}
MYSQL_REQUIRED = ("host", "user", "password", "database")


class ConfigError(ValueError):
    pass


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _resolve_path(base_dir, val, field):
    if not isinstance(val, str) or not val.strip():
        raise ConfigError(f"{field} 必须是非空字符串")
    p = Path(val).expanduser()
    if not p.is_absolute():
        p = base_dir / p
    return str(p.resolve())


def load_config(path=None, base_dir=None):
    """读取并校验配置。path 为空时找 base_dir（默认项目根）下的 config.json，
    不存在则返回 sqlite 默认值。永不回显 password。"""
    if base_dir is None:
        base_dir = PROJECT_ROOT
    if path is None:
        p = Path(base_dir) / "config.json"
        if not p.is_file():
            return {
                "storage": "sqlite",
                "jsonl_path": str(DEFAULT_JSONL),
                "sqlite": {"db_path": str(DEFAULT_DB)},
                "mysql": {},
            }
    else:
        p = Path(path)
        if not p.is_file():
            raise ConfigError(f"配置文件不存在: {p}")
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ConfigError(f"配置文件不是合法 JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError("配置文件顶层必须是 JSON 对象")
    unknown = sorted(set(raw) - CONFIG_TOP_KEYS)
    if unknown:
        raise ConfigError(f"配置文件存在未知字段: {unknown}（允许: {sorted(CONFIG_TOP_KEYS)}）")

    storage = raw.get("storage", "sqlite")
    if storage not in ("sqlite", "mysql"):
        raise ConfigError(f"storage 必须是 sqlite 或 mysql，当前: {storage!r}")

    jsonl_abs = _resolve_path(p.parent, raw.get("jsonl_path", str(DEFAULT_JSONL)), "jsonl_path")

    sqlite_sec = raw.get("sqlite", {})
    if not isinstance(sqlite_sec, dict):
        raise ConfigError("sqlite 配置必须是对象")
    unknown = sorted(set(sqlite_sec) - SQLITE_KEYS)
    if unknown:
        raise ConfigError(f"sqlite 配置存在未知字段: {unknown}（允许: {sorted(SQLITE_KEYS)}）")
    db_abs = _resolve_path(p.parent, sqlite_sec.get("db_path", str(DEFAULT_DB)), "sqlite.db_path")

    mysql_sec = raw.get("mysql", {})
    if not isinstance(mysql_sec, dict):
        raise ConfigError("mysql 配置必须是对象")
    unknown = sorted(set(mysql_sec) - MYSQL_KEYS)
    if unknown:
        raise ConfigError(f"mysql 配置存在未知字段: {unknown}（允许: {sorted(MYSQL_KEYS)}）")
    if storage == "mysql":
        missing = [k for k in MYSQL_REQUIRED if k not in mysql_sec]
        if missing:
            raise ConfigError(f"storage=mysql 缺少 mysql.{missing}（参考 config.example.json）")
        for k in ("host", "user", "database"):
            if not isinstance(mysql_sec[k], str) or not mysql_sec[k].strip():
                raise ConfigError(f"mysql.{k} 不能为空")
        if not isinstance(mysql_sec["password"], str):
            raise ConfigError("mysql.password 必须是字符串")
    else:
        mysql_sec = dict(mysql_sec)

    mysql_sec["port"] = _opt_int(mysql_sec, "port", 3306)
    mysql_sec["connect_timeout"] = _opt_int(mysql_sec, "connect_timeout", 10)
    mysql_sec.setdefault("charset", "utf8mb4")
    if not isinstance(mysql_sec["charset"], str) or not mysql_sec["charset"].strip():
        raise ConfigError("mysql.charset 必须是非空字符串")
    ssl_ca = mysql_sec.get("ssl_ca")
    if ssl_ca is not None and (not isinstance(ssl_ca, str) or not ssl_ca.strip()):
        raise ConfigError("mysql.ssl_ca 必须是非空字符串或 null")

    return {
        "storage": storage,
        "jsonl_path": jsonl_abs,
        "sqlite": {"db_path": db_abs},
        "mysql": mysql_sec,
    }


def _opt_int(sec, key, default):
    val = sec.get(key, default)
    if isinstance(val, bool) or not isinstance(val, int):
        raise ConfigError(f"mysql.{key} 必须是整数，当前: {val!r}")
    return val


def create_store(cfg, glossary, jsonl_path=None):
    """按配置创建存储实例。jsonl_path 为空时用 cfg 里的值（显式传 None 可关闭 jsonl）。"""
    jp = cfg.get("jsonl_path") if jsonl_path is None else jsonl_path
    if cfg["storage"] == "mysql":
        return MysqlStore(cfg["mysql"], jp, glossary)
    return SqliteStore(cfg["sqlite"]["db_path"], jp, glossary)


class BaseStore:
    """两个后端共享的 glossary 访问、输入校验、jsonl 审计流水。"""

    def __init__(self, glossary, jsonl_path):
        self.glossary = glossary or {}
        self.lock = threading.RLock()
        self.jsonl_path = Path(jsonl_path) if jsonl_path else None
        if self.jsonl_path is not None:
            self.jsonl_path.parent.mkdir(parents=True, exist_ok=True)

    def head_order(self):
        return self.glossary.get("head_order", [])

    def head_glossary(self, head):
        return self.glossary.get("heads", {}).get(head)

    def head_type(self, head):
        return (self.head_glossary(head) or {}).get("type", "single")

    def head_labels(self, head):
        return [l["id"] for l in (self.head_glossary(head) or {}).get("labels", [])]

    def _validate_input(self, head, answer, disposition, is_final=False):
        if answer is None and not disposition:
            # null/null 且非 final = 「清除标注」（前端再点一次 disposition 取消），
            # 放行后 revision+1 落 NULL 行、status 回 in_progress；final 化仍拒绝。
            if is_final:
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
        return answer

    def _append_jsonl(self, record):
        if self.jsonl_path is None:
            return
        with open(self.jsonl_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

    @staticmethod
    def _assignment_row(r):
        d = dict(r)
        raw_answer = d.pop("answer_json")
        d["answer"] = json.loads(raw_answer) if raw_answer is not None else None
        d["is_final"] = bool(d["is_final"]) if d["is_final"] is not None else False
        return d

    def _detail_row(self, d):
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
            "invariants": self.glossary.get("invariants", []),
            "dispositions": list(ALL_DISPOSITIONS),
        }

    def _validate_import(self, batch_id, samples, heads):
        if not batch_id or not isinstance(batch_id, str):
            raise ValueError("batch_id 必须是非空字符串")
        if not samples:
            raise ValueError("samples 不能为空")
        unknown = [h for h in heads if h not in self.head_order()]
        if unknown:
            raise ValueError(f"未知 head: {unknown}（可用: {self.head_order()}）")
        sids = set()
        for s in samples:
            if not isinstance(s, dict) or not s.get("sample_id") or not s.get("text"):
                raise ValueError(f"sample 缺少 sample_id/text: {s!r}")
            if s["sample_id"] in sids:
                raise ValueError(f"文件内 sample_id 重复: {s['sample_id']}")
            sids.add(s["sample_id"])

    def _validate_sparse_import(self, batch_id, assignments):
        if not batch_id or not isinstance(batch_id, str):
            raise ValueError("batch_id 必须是非空字符串")
        if not assignments:
            raise ValueError("assignments 不能为空")
        sids = set()
        for s in assignments:
            if not isinstance(s, dict):
                raise ValueError(f"assignment 必须是对象: {s!r}")
            sample_id = s.get("sample_id")
            text = s.get("text")
            if not isinstance(sample_id, str) or not sample_id.strip():
                raise ValueError(f"sample 缺少 sample_id: {s!r}")
            if not isinstance(text, str) or not text.strip():
                raise ValueError(f"sample 缺少 text: {s!r}")
            sample_id = sample_id.strip()
            if sample_id in sids:
                raise ValueError(f"文件内 sample_id 重复: {sample_id}")
            sids.add(sample_id)
            heads = s.get("heads")
            if not isinstance(heads, list) or not heads:
                raise ValueError(f"sample {sample_id} 的 heads 必须是非空数组")
            if any(not isinstance(h, str) or not h.strip() for h in heads):
                raise ValueError(f"sample {sample_id} 的 heads 必须全部是非空字符串")
            if len(set(heads)) != len(heads):
                raise ValueError(f"sample {sample_id} 的 heads 存在重复")
            unknown = [h for h in heads if h not in self.head_order()]
            if unknown:
                raise ValueError(f"sample {sample_id} 未知 head: {unknown}（可用: {self.head_order()}）")


class SqliteStore(BaseStore):
    """sqlite3 + annotations.jsonl 的统一存取，线程安全（单连接 + RLock）。"""

    def __init__(self, db_path, jsonl_path=None, glossary=None):
        super().__init__(glossary, jsonl_path)
        self.db_path = str(db_path)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        with self.lock:
            self.conn.executescript(SQLITE_DDL)
            # 存量库迁移：老版本的 batches 表没有 archived_at 列
            cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(batches)").fetchall()}
            if "archived_at" not in cols:
                self.conn.execute("ALTER TABLE batches ADD COLUMN archived_at TEXT")
            self.conn.commit()

    def close(self):
        self.conn.close()

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
                   WHERE b.archived_at IS NULL
                   GROUP BY b.id
                   ORDER BY b.created_at DESC, b.id""",
                TERMINAL_DISPOSITIONS,
            ).fetchall()
        return [dict(r) for r in rows]

    def is_batch_archived(self, batch_id):
        with self.lock:
            row = self.conn.execute(
                "SELECT archived_at FROM batches WHERE id = ?", (batch_id,)
            ).fetchone()
        return bool(row and row["archived_at"])

    def archive_batch(self, batch_id):
        """标记归档（数据不动，列表隐藏、禁止再标注）。返回 archived_at。"""
        with self.lock:
            row = self.conn.execute(
                "SELECT id, archived_at FROM batches WHERE id = ?", (batch_id,)
            ).fetchone()
            if row is None:
                raise KeyError(batch_id)
            if row["archived_at"]:
                raise ValueError("批次已归档")
            ts = now_iso()
            self.conn.execute("UPDATE batches SET archived_at = ? WHERE id = ?", (ts, batch_id))
            self.conn.commit()
        return ts

    def get_resume(self):
        with self.lock:
            row = self.conn.execute(
                """SELECT a.batch_id, a.id
                   FROM assignments a
                   JOIN batches b ON b.id = a.batch_id AND b.archived_at IS NULL
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
        return [self._assignment_row(r) for r in rows]

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
        return self._detail_row(dict(row))

    # ---- writes ----
    def upsert_annotation(self, assignment_id, answer, disposition, is_final):
        with self.lock:
            arow = self.conn.execute(
                "SELECT id, batch_id, sample_id, head FROM assignments WHERE id = ?",
                (assignment_id,),
            ).fetchone()
            if arow is None:
                raise KeyError(assignment_id)
            if self.is_batch_archived(arow["batch_id"]):
                raise ValueError("批次已归档，禁止标注")
            answer = self._validate_input(arow["head"], answer, disposition, is_final)
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
                "head": arow["head"],
                "answer": answer,
                "disposition": disposition,
                "is_final": bool(is_final_i),
                "revision": revision,
                "updated_at": updated_at,
                "schema_version": brow["schema_version"] if brow else None,
            }
            self._append_jsonl(record)
            return record

    def import_samples(self, batch_id, samples, heads, schema_version):
        self._validate_import(batch_id, samples, heads)
        with self.lock:
            try:
                self.conn.execute("BEGIN IMMEDIATE")
                existing = {r["id"] for r in self.conn.execute(
                    "SELECT id FROM samples WHERE batch_id = ?", (batch_id,))}
                dup = existing & {s["sample_id"] for s in samples}
                if dup:
                    raise ValueError(f"与库中已有 sample_id 重复: {sorted(dup)[:10]}")
                if self.conn.execute("SELECT 1 FROM batches WHERE id = ?", (batch_id,)).fetchone() is None:
                    self.conn.execute(
                        "INSERT INTO batches(id, name, created_at, schema_version) VALUES(?, ?, ?, ?)",
                        (batch_id, batch_id, now_iso(), schema_version),
                    )
                for s in samples:
                    self.conn.execute(
                        "INSERT INTO samples(batch_id, id, title, content, metadata_json) VALUES(?, ?, ?, ?, ?)",
                        (
                            batch_id,
                            s["sample_id"],
                            s.get("title"),
                            s["text"],
                            json.dumps(s.get("metadata") or {}, ensure_ascii=False, sort_keys=True),
                        ),
                    )
                for h in heads:
                    row = self.conn.execute(
                        "SELECT MAX(position) AS m FROM assignments WHERE batch_id = ? AND head = ?",
                        (batch_id, h),
                    ).fetchone()
                    pos = (row["m"] + 1) if row["m"] is not None else 0
                    for s in samples:
                        self.conn.execute(
                            "INSERT INTO assignments(id, batch_id, sample_id, head, position, status)"
                            " VALUES(?, ?, ?, ?, ?, 'pending')",
                            (f"{batch_id}:{s['sample_id']}:{h}", batch_id, s["sample_id"], h, pos),
                        )
                        pos += 1
                self.conn.commit()
            except Exception:
                self.conn.rollback()
                raise
        return {"samples": len(samples), "assignments": len(samples) * len(heads)}

    def import_sparse_samples(self, batch_id, assignments, schema_version):
        """原子导入逐样本 heads；未指定的 head 不生成 assignment。"""
        self._validate_sparse_import(batch_id, assignments)
        with self.lock:
            try:
                self.conn.execute("BEGIN IMMEDIATE")
                existing = {r["id"] for r in self.conn.execute(
                    "SELECT id FROM samples WHERE batch_id = ?", (batch_id,))}
                dup = existing & {s["sample_id"] for s in assignments}
                if dup:
                    raise ValueError(f"与库中已有 sample_id 重复: {sorted(dup)[:10]}")
                if self.conn.execute("SELECT 1 FROM batches WHERE id = ?", (batch_id,)).fetchone() is None:
                    self.conn.execute(
                        "INSERT INTO batches(id, name, created_at, schema_version) VALUES(?, ?, ?, ?)",
                        (batch_id, batch_id, now_iso(), schema_version),
                    )
                for s in assignments:
                    self.conn.execute(
                        "INSERT INTO samples(batch_id, id, title, content, metadata_json) VALUES(?, ?, ?, ?, ?)",
                        (
                            batch_id,
                            s["sample_id"],
                            s.get("title"),
                            s["text"],
                            json.dumps(s.get("metadata") or {}, ensure_ascii=False, sort_keys=True),
                        ),
                    )
                heads = list(dict.fromkeys(h for s in assignments for h in s["heads"]))
                next_positions = {}
                for h in heads:
                    row = self.conn.execute(
                        "SELECT MAX(position) AS m FROM assignments WHERE batch_id = ? AND head = ?",
                        (batch_id, h),
                    ).fetchone()
                    next_positions[h] = (row["m"] + 1) if row["m"] is not None else 0
                for s in assignments:
                    for h in s["heads"]:
                        self.conn.execute(
                            "INSERT INTO assignments(id, batch_id, sample_id, head, position, status)"
                            " VALUES(?, ?, ?, ?, ?, 'pending')",
                            (f"{batch_id}:{s['sample_id']}:{h}", batch_id, s["sample_id"], h, next_positions[h]),
                        )
                        next_positions[h] += 1
                self.conn.commit()
            except Exception:
                self.conn.rollback()
                raise
        return {
            "samples": len(assignments),
            "assignments": sum(len(s["heads"]) for s in assignments),
        }

    def export_rows(self, final_only=False, batch_id=None):
        con = sqlite3.connect(self.db_path)
        con.row_factory = sqlite3.Row
        try:
            sql = (
                """SELECT b.id AS batch, a.sample_id, a.head,
                          n.answer_json, n.disposition, n.is_final,
                          b.schema_version, n.updated_at, s.metadata_json
                   FROM assignments a
                   JOIN batches b ON b.id = a.batch_id
                   JOIN samples s ON s.batch_id = a.batch_id AND s.id = a.sample_id
                   LEFT JOIN annotations n ON n.assignment_id = a.id"""
            )
            params = []
            if batch_id:
                sql += " WHERE b.id = ?"
                params.append(batch_id)
            if final_only:
                sql += " AND n.is_final = 1" if batch_id else " WHERE n.is_final = 1"
            sql += " ORDER BY b.id, a.head, a.position"
            for r in con.execute(sql, params):
                yield {
                    "batch": r["batch"],
                    "sample_id": r["sample_id"],
                    "head": r["head"],
                    "answer": json.loads(r["answer_json"]) if r["answer_json"] is not None else None,
                    "disposition": r["disposition"],
                    "is_final": bool(r["is_final"]) if r["is_final"] is not None else False,
                    "schema_version": r["schema_version"],
                    "updated_at": r["updated_at"],
                    "metadata": json.loads(r["metadata_json"] or "{}"),
                }
        finally:
            con.close()


class MysqlStore(BaseStore):
    """MySQL 后端（pymysql DictCursor，单连接 + RLock 串行，与 sqlite 同一套语义）。"""

    def __init__(self, mysql_cfg, jsonl_path=None, glossary=None):
        super().__init__(glossary, jsonl_path)
        try:
            import pymysql
        except ImportError as exc:
            raise ConfigError(
                "storage=mysql 需要 MySQL 驱动：pip install pymysql（sqlite 模式无需安装）"
            ) from exc
        self._pymysql = pymysql
        kwargs = dict(
            host=mysql_cfg["host"],
            port=mysql_cfg["port"],
            user=mysql_cfg["user"],
            password=mysql_cfg["password"],
            database=mysql_cfg["database"],
            charset=mysql_cfg.get("charset", "utf8mb4"),
            # 必须开 autocommit：单条长连接下 REPEATABLE READ 的读事务若不结束，
            # 之后所有读都冻结在旧快照里，看不到其他进程（GPT/导入）刚提交的数据。
            autocommit=True,
            connect_timeout=mysql_cfg.get("connect_timeout", 10),
            cursorclass=pymysql.cursors.DictCursor,
        )
        if mysql_cfg.get("ssl_ca"):
            kwargs["ssl_ca"] = mysql_cfg["ssl_ca"]
        self.conn = pymysql.connect(**kwargs)
        with self.lock:
            cur = self.conn.cursor()
            try:
                for stmt in MYSQL_DDL:
                    cur.execute(stmt)
                # 存量库迁移：老版本的 batches 表没有 archived_at 列（1060=列已存在）
                try:
                    cur.execute("ALTER TABLE batches ADD COLUMN archived_at VARCHAR(32) NULL")
                except Exception as exc:
                    if getattr(exc, "args", [None])[0] != 1060:
                        raise
                self.conn.commit()
            finally:
                cur.close()

    def close(self):
        self.conn.close()

    def _ping(self):
        # 长连接被 wait_timeout 掐断后自动重连
        self.conn.ping(reconnect=True)

    # ---- queries ----
    def list_batches(self):
        with self.lock:
            self._ping()
            cur = self.conn.cursor()
            try:
                cur.execute(
                    """SELECT b.id, b.name, b.created_at, b.schema_version,
                              COUNT(a.id) AS total,
                              CAST(SUM(CASE WHEN n.is_final=1
                                               OR n.disposition IN (%s, %s, %s)
                                             THEN 1 ELSE 0 END) AS SIGNED) AS done
                       FROM batches b
                       LEFT JOIN assignments a ON a.batch_id = b.id
                       LEFT JOIN annotations n ON n.assignment_id = a.id
                       WHERE b.archived_at IS NULL
                       GROUP BY b.id, b.name, b.created_at, b.schema_version
                       ORDER BY b.created_at DESC, b.id""",
                    TERMINAL_DISPOSITIONS,
                )
                rows = cur.fetchall()
            finally:
                cur.close()
        return [dict(r) for r in rows]

    def get_resume(self):
        with self.lock:
            self._ping()
            cur = self.conn.cursor()
            try:
                cur.execute(
                    """SELECT a.batch_id, a.id
                       FROM assignments a
                       JOIN batches b ON b.id = a.batch_id AND b.archived_at IS NULL
                       LEFT JOIN annotations n ON n.assignment_id = a.id
                       WHERE n.assignment_id IS NULL
                          OR (IFNULL(n.is_final, 0) = 0
                              AND (n.disposition IS NULL
                                   OR n.disposition NOT IN (%s, %s, %s)))
                       ORDER BY CASE WHEN n.updated_at IS NULL THEN 1 ELSE 0 END,
                                n.updated_at DESC, a.position ASC, a.id
                       LIMIT 1""",
                    TERMINAL_DISPOSITIONS,
                )
                row = cur.fetchone()
            finally:
                cur.close()
        if row is None:
            return {"batch_id": None, "assignment_id": None}
        return {"batch_id": row["batch_id"], "assignment_id": row["id"]}

    def is_batch_archived(self, batch_id):
        with self.lock:
            self._ping()
            cur = self.conn.cursor()
            try:
                cur.execute("SELECT archived_at FROM batches WHERE id = %s", (batch_id,))
                row = cur.fetchone()
            finally:
                cur.close()
        return bool(row and row["archived_at"])

    def archive_batch(self, batch_id):
        """标记归档（数据不动，列表隐藏、禁止再标注）。返回 archived_at。"""
        with self.lock:
            self._ping()
            cur = self.conn.cursor()
            try:
                cur.execute("SELECT archived_at FROM batches WHERE id = %s", (batch_id,))
                row = cur.fetchone()
                if row is None:
                    raise KeyError(batch_id)
                if row["archived_at"]:
                    raise ValueError("批次已归档")
                ts = now_iso()
                cur.execute("UPDATE batches SET archived_at = %s WHERE id = %s", (ts, batch_id))
            finally:
                cur.close()
        return ts

    def list_assignments(self, batch_id, head):
        with self.lock:
            self._ping()
            cur = self.conn.cursor()
            try:
                cur.execute(
                    """SELECT a.id, a.batch_id, a.sample_id, a.head, a.position, a.status,
                              s.title,
                              n.answer_json, n.disposition, n.is_final, n.revision, n.updated_at
                       FROM assignments a
                       JOIN samples s ON s.batch_id = a.batch_id AND s.id = a.sample_id
                       LEFT JOIN annotations n ON n.assignment_id = a.id
                       WHERE a.batch_id = %s AND a.head = %s
                       ORDER BY a.position ASC, a.id""",
                    (batch_id, head),
                )
                rows = cur.fetchall()
            finally:
                cur.close()
        return [self._assignment_row(r) for r in rows]

    def get_assignment(self, assignment_id):
        with self.lock:
            self._ping()
            cur = self.conn.cursor()
            try:
                cur.execute(
                    """SELECT a.id, a.batch_id, a.sample_id, a.head, a.position, a.status,
                              s.title, s.content, s.metadata_json,
                              b.schema_version,
                              n.answer_json, n.disposition, n.is_final, n.revision, n.updated_at
                       FROM assignments a
                       JOIN samples s ON s.batch_id = a.batch_id AND s.id = a.sample_id
                       JOIN batches b ON b.id = a.batch_id
                       LEFT JOIN annotations n ON n.assignment_id = a.id
                       WHERE a.id = %s""",
                    (assignment_id,),
                )
                row = cur.fetchone()
            finally:
                cur.close()
        if row is None:
            return None
        return self._detail_row(dict(row))

    # ---- writes ----
    def upsert_annotation(self, assignment_id, answer, disposition, is_final):
        with self.lock:
            self._ping()
            cur = self.conn.cursor()
            try:
                cur.execute("START TRANSACTION")
                cur.execute(
                    "SELECT id, batch_id, sample_id, head FROM assignments WHERE id = %s",
                    (assignment_id,),
                )
                arow = cur.fetchone()
                if arow is None:
                    raise KeyError(assignment_id)
                cur.execute(
                    "SELECT archived_at FROM batches WHERE id = %s", (arow["batch_id"],)
                )
                _brow = cur.fetchone()
                if _brow and _brow["archived_at"]:
                    raise ValueError("批次已归档，禁止标注")
                head = arow["head"]
                answer = self._validate_input(head, answer, disposition, is_final)
                cur.execute(
                    "SELECT revision FROM annotations WHERE assignment_id = %s", (assignment_id,)
                )
                prev = cur.fetchone()
                revision = (prev["revision"] if prev else 0) + 1
                updated_at = now_iso()
                is_final_i = 1 if is_final else 0
                cur.execute(
                    """INSERT INTO annotations(assignment_id, answer_json, disposition, is_final, revision, updated_at)
                       VALUES(%s, %s, %s, %s, %s, %s)
                       ON DUPLICATE KEY UPDATE
                         answer_json = VALUES(answer_json),
                         disposition = VALUES(disposition),
                         is_final = VALUES(is_final),
                         revision = VALUES(revision),
                         updated_at = VALUES(updated_at)""",
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
                cur.execute("UPDATE assignments SET status = %s WHERE id = %s", (status, assignment_id))
                cur.execute("SELECT schema_version FROM batches WHERE id = %s", (arow["batch_id"],))
                brow = cur.fetchone()
                self.conn.commit()
            except Exception:
                self.conn.rollback()
                raise
            finally:
                cur.close()
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
            self._append_jsonl(record)
            return record

    def import_samples(self, batch_id, samples, heads, schema_version):
        self._validate_import(batch_id, samples, heads)
        with self.lock:
            self._ping()
            cur = self.conn.cursor()
            try:
                cur.execute("START TRANSACTION")
                cur.execute("SELECT id FROM samples WHERE batch_id = %s", (batch_id,))
                existing = {r["id"] for r in cur.fetchall()}
                dup = existing & {s["sample_id"] for s in samples}
                if dup:
                    raise ValueError(f"与库中已有 sample_id 重复: {sorted(dup)[:10]}")
                cur.execute("SELECT 1 FROM batches WHERE id = %s", (batch_id,))
                if cur.fetchone() is None:
                    cur.execute(
                        "INSERT INTO batches(id, name, created_at, schema_version) VALUES(%s, %s, %s, %s)",
                        (batch_id, batch_id, now_iso(), schema_version),
                    )
                for s in samples:
                    cur.execute(
                        "INSERT INTO samples(batch_id, id, title, content, metadata_json) VALUES(%s, %s, %s, %s, %s)",
                        (
                            batch_id,
                            s["sample_id"],
                            s.get("title"),
                            s["text"],
                            json.dumps(s.get("metadata") or {}, ensure_ascii=False, sort_keys=True),
                        ),
                    )
                for h in heads:
                    cur.execute(
                        "SELECT MAX(position) AS m FROM assignments WHERE batch_id = %s AND head = %s",
                        (batch_id, h),
                    )
                    m = cur.fetchone()["m"]
                    pos = (m + 1) if m is not None else 0
                    for s in samples:
                        cur.execute(
                            "INSERT INTO assignments(id, batch_id, sample_id, head, position, status)"
                            " VALUES(%s, %s, %s, %s, %s, 'pending')",
                            (f"{batch_id}:{s['sample_id']}:{h}", batch_id, s["sample_id"], h, pos),
                        )
                        pos += 1
                self.conn.commit()
            except Exception:
                self.conn.rollback()
                raise
            finally:
                cur.close()
        return {"samples": len(samples), "assignments": len(samples) * len(heads)}

    def import_sparse_samples(self, batch_id, assignments, schema_version):
        """原子导入逐样本 heads；未指定的 head 不生成 assignment。"""
        self._validate_sparse_import(batch_id, assignments)
        with self.lock:
            self._ping()
            cur = self.conn.cursor()
            try:
                cur.execute("START TRANSACTION")
                cur.execute("SELECT id FROM samples WHERE batch_id = %s", (batch_id,))
                existing = {r["id"] for r in cur.fetchall()}
                dup = existing & {s["sample_id"] for s in assignments}
                if dup:
                    raise ValueError(f"与库中已有 sample_id 重复: {sorted(dup)[:10]}")
                cur.execute("SELECT 1 FROM batches WHERE id = %s", (batch_id,))
                if cur.fetchone() is None:
                    cur.execute(
                        "INSERT INTO batches(id, name, created_at, schema_version) VALUES(%s, %s, %s, %s)",
                        (batch_id, batch_id, now_iso(), schema_version),
                    )
                for s in assignments:
                    cur.execute(
                        "INSERT INTO samples(batch_id, id, title, content, metadata_json) VALUES(%s, %s, %s, %s, %s)",
                        (
                            batch_id,
                            s["sample_id"],
                            s.get("title"),
                            s["text"],
                            json.dumps(s.get("metadata") or {}, ensure_ascii=False, sort_keys=True),
                        ),
                    )
                heads = list(dict.fromkeys(h for s in assignments for h in s["heads"]))
                next_positions = {}
                for h in heads:
                    cur.execute(
                        "SELECT MAX(position) AS m FROM assignments WHERE batch_id = %s AND head = %s",
                        (batch_id, h),
                    )
                    m = cur.fetchone()["m"]
                    next_positions[h] = (m + 1) if m is not None else 0
                for s in assignments:
                    for h in s["heads"]:
                        cur.execute(
                            "INSERT INTO assignments(id, batch_id, sample_id, head, position, status)"
                            " VALUES(%s, %s, %s, %s, %s, 'pending')",
                            (
                                f"{batch_id}:{s['sample_id']}:{h}",
                                batch_id,
                                s["sample_id"],
                                h,
                                next_positions[h],
                            ),
                        )
                        next_positions[h] += 1
                self.conn.commit()
            except Exception:
                self.conn.rollback()
                raise
            finally:
                cur.close()
        return {
            "samples": len(assignments),
            "assignments": sum(len(s["heads"]) for s in assignments),
        }

    def export_rows(self, final_only=False, batch_id=None):
        with self.lock:
            self._ping()
            cur = self.conn.cursor()
            try:
                sql = (
                    """SELECT b.id AS batch, a.sample_id, a.head,
                              n.answer_json, n.disposition, n.is_final,
                              b.schema_version, n.updated_at, s.metadata_json
                       FROM assignments a
                       JOIN batches b ON b.id = a.batch_id
                       JOIN samples s ON s.batch_id = a.batch_id AND s.id = a.sample_id
                       LEFT JOIN annotations n ON n.assignment_id = a.id"""
                )
                params = []
                if batch_id:
                    sql += " WHERE b.id = %s"
                    params.append(batch_id)
                if final_only:
                    sql += " AND n.is_final = 1" if batch_id else " WHERE n.is_final = 1"
                sql += " ORDER BY b.id, a.head, a.position"
                cur.execute(sql, params or None)
                rows = cur.fetchall()
            finally:
                cur.close()
        for r in rows:
            yield {
                "batch": r["batch"],
                "sample_id": r["sample_id"],
                "head": r["head"],
                "answer": json.loads(r["answer_json"]) if r["answer_json"] is not None else None,
                "disposition": r["disposition"],
                "is_final": bool(r["is_final"]) if r["is_final"] is not None else False,
                "schema_version": r["schema_version"],
                "updated_at": r["updated_at"],
                "metadata": json.loads(r["metadata_json"] or "{}"),
            }
