#!/usr/bin/env python3
"""GPT 专用任务工具：add（导入任务）/ pull（拉取标注结果）/ status（批次完成度）。

读取与服务端完全相同的配置文件（--config，默认项目根 config.json）：
本地 GPT 与服务器各放一份相同配置，即可对同一个 sqlite/mysql 库操作，
实现「本地专家加任务 → 服务端网页标注 → 本地专家拉结果」的闭环。

退出码：0 成功；1 数据/校验失败（fail-closed，未写入任何数据；status 批次不存在也走 1）；2 配置错误。
"""

import argparse
import csv
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "tools"))
from storage import (  # noqa: E402
    BASE_SCHEMA_VERSION, SCHEMA_PATH, TERMINAL_DISPOSITIONS, ConfigError,
    create_store, load_config, load_schema_catalog,
)
from import_batch import parse_samples  # noqa: E402

CSV_COLUMNS = [
    "batch", "sample_id", "head", "answer", "disposition", "is_final",
    "schema_version", "updated_at", "metadata",
]

ASSIGNMENT_ALLOWED_FIELDS = {"sample_id", "text", "title", "metadata", "heads"}


def die(msg):
    print(f"[gpt_tasks] FAIL-CLOSED: {msg}", file=sys.stderr)
    sys.exit(1)


def _load_env(config_path):
    try:
        cfg = load_config(config_path)
    except ConfigError as exc:
        print(f"[gpt_tasks] 配置错误: {exc}", file=sys.stderr)
        sys.exit(2)
    glossary = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    try:
        store = create_store(cfg, glossary)
    except ConfigError as exc:
        print(f"[gpt_tasks] {exc}", file=sys.stderr)
        sys.exit(2)
    return cfg, glossary, store


def parse_assignment_file(path, head_order):
    """读取逐样本 head 指定文件，任何错误都在写库前 fail-closed。"""
    p = Path(path)
    if not p.is_file():
        die(f"文件不存在: {p}")
    assignments, seen = [], set()
    with p.open(encoding="utf-8") as f:
        for lineno, raw in enumerate(f, 1):
            line = raw.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError as exc:
                die(f"第 {lineno} 行不是合法 JSON: {exc}")
            if not isinstance(rec, dict):
                die(f"第 {lineno} 行必须是 JSON 对象")
            unknown_fields = sorted(set(rec) - ASSIGNMENT_ALLOWED_FIELDS)
            if unknown_fields:
                die(
                    f"第 {lineno} 行存在未知字段: {unknown_fields}"
                    f"（允许: {sorted(ASSIGNMENT_ALLOWED_FIELDS)}）"
                )
            if not (isinstance(rec.get("sample_id"), str) and rec["sample_id"].strip()):
                die(f"第 {lineno} 行 sample_id 缺失或为空")
            if not (isinstance(rec.get("text"), str) and rec["text"].strip()):
                die(f"第 {lineno} 行 text 缺失或为空")
            sid = rec["sample_id"].strip()
            if sid in seen:
                die(f"第 {lineno} 行 sample_id 重复: {sid}")
            seen.add(sid)

            heads = rec.get("heads")
            if not isinstance(heads, list) or not heads:
                die(f"第 {lineno} 行 heads 必须是非空数组")
            if any(not isinstance(h, str) or not h.strip() for h in heads):
                die(f"第 {lineno} 行 heads 必须全部是非空字符串")
            heads = [h.strip() for h in heads]
            if len(set(heads)) != len(heads):
                die(f"第 {lineno} 行 heads 存在重复")
            unknown_heads = [h for h in heads if h not in head_order]
            if unknown_heads:
                die(f"第 {lineno} 行未知 head: {unknown_heads}（可用: {head_order}）")

            title = rec.get("title")
            if title is not None and not isinstance(title, str):
                die(f"第 {lineno} 行 title 必须是字符串")
            metadata = rec.get("metadata")
            if metadata is None:
                metadata = {}
            if not isinstance(metadata, dict):
                die(f"第 {lineno} 行 metadata 必须是对象")
            assignments.append({
                "sample_id": sid,
                "text": rec["text"],
                "title": title,
                "metadata": metadata,
                "heads": heads,
            })
    if not assignments:
        die("文件没有任何有效行")
    return assignments


def cmd_add(args):
    try:
        catalog = load_schema_catalog()
        glossary = catalog[args.schema_version]
    except (ConfigError, KeyError) as exc:
        die(f"不支持的 schema_version: {args.schema_version}")
    head_order = glossary.get("head_order", [])
    if bool(args.file) == bool(args.assignment_file):
        die("必须且只能指定 --file 或 --assignment-file")
    if args.assignment_file:
        if args.heads is not None:
            die("--assignment-file 不能与 --heads 同时使用")
        assignments = parse_assignment_file(args.assignment_file, head_order)
        samples = assignments
        heads = sorted({h for s in assignments for h in s["heads"]}, key=head_order.index)
        import_sparse = True
    else:
        if args.heads is None:
            die("--file 模式必须指定 --heads")
        heads = [h.strip() for h in args.heads.split(",") if h.strip()]
        if not heads:
            die("--heads 不能为空")
        if len(set(heads)) != len(heads):
            die("--heads 存在重复")
        unknown = [h for h in heads if h not in head_order]
        if unknown:
            die(f"未知 head: {unknown}（可用: {head_order}）")
        samples = parse_samples(args.file)
        import_sparse = False
    cfg, _, store = _load_env(args.config)
    try:
        if import_sparse:
            stats = store.import_sparse_samples(args.batch, samples, args.schema_version)
        else:
            stats = store.import_samples(args.batch, samples, heads, args.schema_version)
    except ValueError as exc:
        die(f"导入被拒绝（未写入任何数据）: {exc}")
    finally:
        store.close()
    print(
        f"[gpt_tasks] OK add batch={args.batch} samples={stats['samples']} "
        f"heads={len(heads)} assignments={stats['assignments']} storage={cfg['storage']}"
    )


def cmd_pull(args):
    cfg, _, store = _load_env(args.config)
    try:
        rows = list(store.export_rows(final_only=args.final_only, batch_id=args.batch))
    finally:
        store.close()
    out = open(args.out, "w", encoding="utf-8") if args.out else sys.stdout
    try:
        if args.format == "csv":
            writer = csv.writer(out, lineterminator="\n")
            writer.writerow(CSV_COLUMNS)
        for row in rows:
            if args.format == "jsonl":
                out.write(json.dumps(row, ensure_ascii=False) + "\n")
            else:
                answer = row["answer"]
                writer.writerow([
                    row["batch"], row["sample_id"], row["head"],
                    answer if isinstance(answer, str) or answer is None
                    else json.dumps(answer, ensure_ascii=False),
                    row["disposition"], 1 if row["is_final"] else 0,
                    row["schema_version"], row["updated_at"],
                    json.dumps(row["metadata"], ensure_ascii=False, sort_keys=True),
                ])
    finally:
        if out is not sys.stdout:
            out.close()
    print(
        f"[gpt_tasks] OK pull rows={len(rows)} format={args.format} "
        f"final_only={args.final_only} batch={args.batch or '全部'} "
        f"storage={cfg['storage']}" + (f" out={args.out}" if args.out else ""),
        file=sys.stderr,
    )


def cmd_status(args):
    cfg, _, store = _load_env(args.config)
    try:
        rows = list(store.export_rows(final_only=False, batch_id=args.batch))
    finally:
        store.close()
    if not rows:
        print(f"[gpt_tasks] 批次不存在或没有任何任务: {args.batch}", file=sys.stderr)
        sys.exit(1)
    done = sum(1 for r in rows if r["is_final"] or r["disposition"] in TERMINAL_DISPOSITIONS)
    finals = sum(1 for r in rows if r["is_final"])
    print(json.dumps({
        "batch": args.batch,
        "total": len(rows),
        "done": done,
        "finals": finals,
        "complete": done >= len(rows),
    }, ensure_ascii=False))


def main():
    ap = argparse.ArgumentParser(description="GPT 任务工具：add 导入任务 / pull 拉取结果")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_add = sub.add_parser("add", help="导入 samples.jsonl 生成标注任务")
    p_add.add_argument("--batch", required=True, help="batch id（同时作为显示名）")
    p_add.add_argument("--file", default=None, help="samples.jsonl 路径（与 --assignment-file 二选一）")
    p_add.add_argument(
        "--assignment-file", default=None,
        help="逐行指定 heads 的 JSONL 路径（与 --file 二选一；不可同时传 --heads）",
    )
    p_add.add_argument("--heads", default=None, help="逗号分隔的 head 列表，如 target_mode,stance（--file 模式）")
    p_add.add_argument("--config", default=None, help="配置文件路径（默认项目根 config.json）")
    p_add.add_argument(
        "--schema-version", default=BASE_SCHEMA_VERSION,
        help="批次绑定的 Schema 版本（默认 semantic-schema-calibrated-v0.2.1）",
    )

    p_pull = sub.add_parser("pull", help="拉取标注结果（jsonl/csv）")
    p_pull.add_argument("--format", choices=("jsonl", "csv"), default="jsonl")
    p_pull.add_argument("--final-only", action="store_true", help="只导出 is_final=1 的行")
    p_pull.add_argument("--batch", default=None, help="只拉取指定 batch")
    p_pull.add_argument("--out", default=None, help="写入文件（默认打印到 stdout）")
    p_pull.add_argument("--config", default=None, help="配置文件路径（默认项目根 config.json）")

    p_status = sub.add_parser("status", help="查询批次完成度（JSON：total/done/finals/complete）")
    p_status.add_argument("--batch", required=True, help="batch id")
    p_status.add_argument("--config", default=None, help="配置文件路径（默认项目根 config.json）")

    args = ap.parse_args()
    if args.cmd == "add":
        cmd_add(args)
    elif args.cmd == "status":
        cmd_status(args)
    else:
        cmd_pull(args)


if __name__ == "__main__":
    main()
