#!/usr/bin/env python3
"""导出标注为 jsonl 或 csv，打印到 stdout。"""

import argparse
import csv
import json
import sqlite3
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
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


def export_rows(db_path, final_only):
    con = sqlite3.connect(str(db_path))
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
        if final_only:
            sql += " WHERE n.is_final = 1"
        sql += " ORDER BY b.id, a.head, a.position"
        for r in con.execute(sql):
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
