#!/usr/bin/env python3
"""GPT 专用任务工具：add（导入任务）/ pull（拉取标注结果）。

读取与服务端完全相同的配置文件（--config，默认项目根 config.json）：
本地 GPT 与服务器各放一份相同配置，即可对同一个 sqlite/mysql 库操作，
实现「本地专家加任务 → 服务端网页标注 → 本地专家拉结果」的闭环。

退出码：0 成功；1 数据/校验失败（fail-closed，未写入任何数据）；2 配置错误。
"""

import argparse
import csv
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "tools"))
from storage import SCHEMA_PATH, ConfigError, create_store, load_config  # noqa: E402
from import_batch import parse_samples  # noqa: E402

CSV_COLUMNS = [
    "batch", "sample_id", "head", "answer", "disposition", "is_final",
    "schema_version", "updated_at", "metadata",
]


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


def cmd_add(args):
    glossary = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    head_order = glossary.get("head_order", [])
    heads = [h.strip() for h in args.heads.split(",") if h.strip()]
    if not heads:
        die("--heads 不能为空")
    if len(set(heads)) != len(heads):
        die("--heads 存在重复")
    unknown = [h for h in heads if h not in head_order]
    if unknown:
        die(f"未知 head: {unknown}（可用: {head_order}）")
    samples = parse_samples(args.file)
    cfg, _, store = _load_env(args.config)
    try:
        stats = store.import_samples(args.batch, samples, heads, glossary.get("schema_version"))
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


def main():
    ap = argparse.ArgumentParser(description="GPT 任务工具：add 导入任务 / pull 拉取结果")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_add = sub.add_parser("add", help="导入 samples.jsonl 生成标注任务")
    p_add.add_argument("--batch", required=True, help="batch id（同时作为显示名）")
    p_add.add_argument("--file", required=True, help="samples.jsonl 路径")
    p_add.add_argument("--heads", required=True, help="逗号分隔的 head 列表，如 target_mode,stance")
    p_add.add_argument("--config", default=None, help="配置文件路径（默认项目根 config.json）")

    p_pull = sub.add_parser("pull", help="拉取标注结果（jsonl/csv）")
    p_pull.add_argument("--format", choices=("jsonl", "csv"), default="jsonl")
    p_pull.add_argument("--final-only", action="store_true", help="只导出 is_final=1 的行")
    p_pull.add_argument("--batch", default=None, help="只拉取指定 batch")
    p_pull.add_argument("--out", default=None, help="写入文件（默认打印到 stdout）")
    p_pull.add_argument("--config", default=None, help="配置文件路径（默认项目根 config.json）")

    args = ap.parse_args()
    if args.cmd == "add":
        cmd_add(args)
    else:
        cmd_pull(args)


if __name__ == "__main__":
    main()
