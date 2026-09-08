#!/usr/bin/env python3
"""导出标注为 jsonl 或 csv，打印到 stdout。"""

import argparse
import csv
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
from storage import SqliteStore  # noqa: E402

DEFAULT_DB = PROJECT_ROOT / "data" / "labeler.db"

CSV_COLUMNS = [
    "batch",
    "sample_id",
    "head",
    "answer",
    "disposition",
    "is_final",
    "schema_version",
    "updated_at",
    "metadata",
]


def export_rows(db_path, final_only=False, batch_id=None):
    store = SqliteStore(str(db_path), None, None)
    try:
        yield from store.export_rows(final_only, batch_id=batch_id)
    finally:
        store.close()


def main():
    ap = argparse.ArgumentParser(description="导出标注结果")
    ap.add_argument("--format", choices=("jsonl", "csv"), default="jsonl")
    ap.add_argument("--final-only", action="store_true", help="只导出 is_final=1 的行")
    ap.add_argument("--db", default=str(DEFAULT_DB))
    args = ap.parse_args()

    if not Path(args.db).is_file():
        print(f"[export_annotations] 数据库不存在: {args.db}", file=sys.stderr)
        sys.exit(1)

    writer = None
    if args.format == "csv":
        writer = csv.writer(sys.stdout, lineterminator="\n")
        writer.writerow(CSV_COLUMNS)
    n = 0
    for row in export_rows(args.db, args.final_only):
        if args.format == "jsonl":
            print(json.dumps(row, ensure_ascii=False))
        else:
            answer = row["answer"]
            writer.writerow(
                [
                    row["batch"],
                    row["sample_id"],
                    row["head"],
                    answer if isinstance(answer, str) or answer is None else json.dumps(answer, ensure_ascii=False),
                    row["disposition"],
                    1 if row["is_final"] else 0,
                    row["schema_version"],
                    row["updated_at"],
                    json.dumps(row["metadata"], ensure_ascii=False, sort_keys=True),
                ]
            )
        n += 1
    print(f"[export_annotations] 导出 {n} 行 format={args.format} final_only={args.final_only}", file=sys.stderr)


if __name__ == "__main__":
    main()
