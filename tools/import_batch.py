#!/usr/bin/env python3
"""导入 samples.jsonl → batches/samples/assignments。fail-closed：任何校验失败不写库。

导入逻辑在 storage.SqliteStore.import_samples；本文件保留 (db_path, ...) 旧签名，
并输出与既有调用方（tests/seed_demo）兼容的 fail-closed 行为。
"""

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
from storage import SCHEMA_PATH, SqliteStore  # noqa: E402

DEFAULT_DB = PROJECT_ROOT / "data" / "labeler.db"
REQUIRED_FIELDS = ("sample_id", "text")
ALLOWED_FIELDS = {"sample_id", "text", "title", "metadata"}


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def fail(msg):
    print(f"[import_batch] FAIL-CLOSED: {msg}", file=sys.stderr)
    sys.exit(1)


def parse_samples(path):
    p = Path(path)
    if not p.is_file():
        fail(f"文件不存在: {p}")
    samples, seen = [], set()
    with p.open(encoding="utf-8") as f:
        for lineno, raw in enumerate(f, 1):
            line = raw.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError as exc:
                fail(f"第 {lineno} 行不是合法 JSON: {exc}")
            if not isinstance(rec, dict):
                fail(f"第 {lineno} 行必须是 JSON 对象")
            unknown = sorted(set(rec) - ALLOWED_FIELDS)
            if unknown:
                fail(f"第 {lineno} 行存在未知字段: {unknown}（允许: {sorted(ALLOWED_FIELDS)}）")
            missing = [k for k in REQUIRED_FIELDS if not (isinstance(rec.get(k), str) and rec[k].strip())]
            if missing:
                fail(f"第 {lineno} 行缺少必填字段或为空: {missing}（必填: {list(REQUIRED_FIELDS)}）")
            sid = rec["sample_id"].strip()
            if sid in seen:
                fail(f"第 {lineno} 行 sample_id 重复: {sid}")
            seen.add(sid)
            title = rec.get("title")
            if title is not None and not isinstance(title, str):
                fail(f"第 {lineno} 行 title 必须是字符串")
            meta = rec.get("metadata")
            if meta is None:
                meta = {}
            if not isinstance(meta, dict):
                fail(f"第 {lineno} 行 metadata 必须是对象")
            samples.append({"sample_id": sid, "text": rec["text"], "title": title, "metadata": meta})
    if not samples:
        fail("文件没有任何有效行")
    return samples


def import_samples(db_path, batch_id, samples, heads, schema_version):
    glossary = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    head_order = glossary.get("head_order", [])
    unknown = [h for h in heads if h not in head_order]
    if unknown:
        fail(f"未知 head: {unknown}（可用: {head_order}）")
    store = SqliteStore(str(db_path), None, glossary)
    try:
        return store.import_samples(batch_id, samples, heads, schema_version)
    except ValueError as exc:
        fail(str(exc))
    finally:
        store.close()


def main():
    ap = argparse.ArgumentParser(description="导入 samples.jsonl 生成标注任务")
    ap.add_argument("--batch", required=True, help="batch id（同时作为显示名）")
    ap.add_argument("--file", required=True, help="samples.jsonl 路径")
    ap.add_argument("--heads", required=True, help="逗号分隔的 head 列表，如 target_mode,stance")
    ap.add_argument("--db", default=str(DEFAULT_DB))
    args = ap.parse_args()

    glossary = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    head_order = glossary.get("head_order", [])
    heads = [h.strip() for h in args.heads.split(",") if h.strip()]
    if not heads:
        fail("--heads 不能为空")
    if len(set(heads)) != len(heads):
        fail("--heads 存在重复")
    unknown = [h for h in heads if h not in head_order]
    if unknown:
        fail(f"未知 head: {unknown}（可用: {head_order}）")

    samples = parse_samples(args.file)
    stats = import_samples(args.db, args.batch, samples, heads, glossary.get("schema_version"))
    print(
        f"[import_batch] OK batch={args.batch} samples={stats['samples']} "
        f"heads={len(heads)} assignments={stats['assignments']} db={args.db}"
    )


if __name__ == "__main__":
    main()
