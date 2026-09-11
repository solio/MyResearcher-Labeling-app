#!/usr/bin/env python3
"""stdlib unittest：幂等 upsert、import→export round-trip、fail-closed 导入、resume。"""

import json
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
TOOLS_DIR = PROJECT_ROOT / "tools"
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(TOOLS_DIR))

import server as server_mod  # noqa: E402
import import_batch  # noqa: E402
import export_annotations as export_mod  # noqa: E402
from storage import V03_SCHEMA_VERSION  # noqa: E402

SCHEMA_PATH = PROJECT_ROOT / "schema" / "annotation-schema.v1.json"
SCHEMA_VERSION = "semantic-schema-calibrated-v0.2.1"

BASE_SAMPLES = [
    {"sample_id": "s-1", "text": "往返测试文本一", "title": None,
     "metadata": {"date": "2026-08-01", "split_provenance": "dev/test_v1"}},
    {"sample_id": "s-2", "text": "往返测试文本二", "title": "标题二", "metadata": {}},
]


class ServerTestBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        self.db = base / "labeler.db"
        self.jsonl = base / "annotations.jsonl"
        glossary = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
        server_mod.Handler.store = server_mod.Store(str(self.db), str(self.jsonl), glossary)
        server_mod.Handler.cfg = {"jsonl_path": str(self.jsonl)}
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server_mod.Handler)
        self.port = self.httpd.server_address[1]
        self._thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self._thread.start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self._tmp.cleanup()

    # ---- helpers ----
    def seed(self, batch="tb", samples=None, heads=("stance",)):
        import_batch.import_samples(
            str(self.db), batch, samples or BASE_SAMPLES, list(heads), SCHEMA_VERSION
        )

    def get(self, path):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{self.port}{path}") as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def post(self, body):
        return self.post_path("/api/annotations", body)

    def post_path(self, path, body):
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def db_count(self, table):
        import sqlite3
        con = sqlite3.connect(str(self.db))
        try:
            return con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        finally:
            con.close()


class TestIdempotentUpsert(ServerTestBase):
    def test_same_assignment_upserts_no_duplicate(self):
        self.seed()
        code, r1 = self.post({"assignment_id": "tb:s-1:stance", "answer": "BULL", "is_final": False})
        self.assertEqual(code, 200)
        self.assertEqual(r1["revision"], 1)
        code, r2 = self.post({"assignment_id": "tb:s-1:stance", "answer": "BEAR", "is_final": False})
        self.assertEqual(code, 200)
        self.assertEqual(r2["revision"], 2)
        self.assertEqual(r2["answer"], "BEAR")
        # 幂等：同 assignment 只有一行，且为最新值
        self.assertEqual(self.db_count("annotations"), 1)
        _, rows = self.get("/api/assignments?batch_id=tb&head=stance")
        row = next(r for r in rows if r["id"] == "tb:s-1:stance")
        self.assertEqual(row["answer"], "BEAR")
        self.assertEqual(row["is_final"], False)
        # jsonl append 流水：2 行
        lines = self.jsonl.read_text(encoding="utf-8").strip().splitlines()
        self.assertEqual(len(lines), 2)

    def test_multi_label_dedupe_and_validation(self):
        self.seed(heads=("stance", "reasoning_tags"))
        code, r = self.post({"assignment_id": "tb:s-1:reasoning_tags",
                             "answer": ["RUMOR", "RUMOR", "SARCASM_IRONY"], "is_final": False})
        self.assertEqual(code, 200)
        self.assertEqual(r["answer"], ["RUMOR", "SARCASM_IRONY"])
        # 多选 head 传字符串 → 400
        code, _ = self.post({"assignment_id": "tb:s-1:reasoning_tags", "answer": "RUMOR", "is_final": False})
        self.assertEqual(code, 400)
        # 单选 head 未知标签 → 400
        code, _ = self.post({"assignment_id": "tb:s-1:stance", "answer": "NO_SUCH_LABEL", "is_final": True})
        self.assertEqual(code, 400)

    def test_disposition_only_and_terminal_status(self):
        self.seed()
        code, r = self.post({"assignment_id": "tb:s-1:stance", "answer": None,
                             "disposition": "跳过", "is_final": False})
        self.assertEqual(code, 200)
        _, rows = self.get("/api/assignments?batch_id=tb&head=stance")
        row = next(r2 for r2 in rows if r2["id"] == "tb:s-1:stance")
        self.assertEqual(row["disposition"], "跳过")
        self.assertEqual(row["status"], "completed")
        code, _ = self.post({"assignment_id": "tb:s-1:stance", "answer": None,
                             "disposition": "不是合法词", "is_final": False})
        self.assertEqual(code, 400)

    def test_disposition_cancel_and_reset(self):
        self.seed()
        # 终态 → completed；再点取消（null/null 且非 final）→ 回未完成
        self.post({"assignment_id": "tb:s-1:stance", "answer": None,
                   "disposition": "跳过", "is_final": False})
        code, r = self.post({"assignment_id": "tb:s-1:stance", "answer": None,
                             "disposition": None, "is_final": False})
        self.assertEqual(code, 200)
        self.assertEqual(r["revision"], 2)
        self.assertIsNone(r["disposition"])
        _, rows = self.get("/api/assignments?batch_id=tb&head=stance")
        row = next(r2 for r2 in rows if r2["id"] == "tb:s-1:stance")
        self.assertEqual(row["status"], "in_progress")
        self.assertIsNone(row["disposition"])
        # null/null 且 is_final=true 仍 400（不允许 final 化空标注）
        code, _ = self.post({"assignment_id": "tb:s-1:stance", "answer": None,
                             "disposition": None, "is_final": True})
        self.assertEqual(code, 400)
        # 有草稿答案时取消 disposition：答案保留
        self.post({"assignment_id": "tb:s-2:stance", "answer": "BULL", "is_final": False})
        self.post({"assignment_id": "tb:s-2:stance", "answer": "BULL",
                   "disposition": "稍后再看", "is_final": False})
        code, r2 = self.post({"assignment_id": "tb:s-2:stance", "answer": "BULL",
                              "disposition": None, "is_final": False})
        self.assertEqual(code, 200)
        self.assertEqual(r2["answer"], "BULL")
        self.assertIsNone(r2["disposition"])
        self.assertEqual(r2["is_final"], False)


class TestV03AuxiliaryApi(ServerTestBase):
    def test_batches_and_assignment_api_expose_versioned_sparse_head(self):
        assignments = [{
            "sample_id": "v-1",
            "text": "辅助因子 API 测试",
            "title": "标题",
            "metadata": {"source": "fixture"},
            "heads": ["arousal_level", "market_scope"],
        }]
        server_mod.Handler.store.import_sparse_samples("v03-api", assignments, V03_SCHEMA_VERSION)

        code, batches = self.get("/api/batches")
        self.assertEqual(code, 200)
        batch = next(b for b in batches if b["id"] == "v03-api")
        self.assertEqual(batch["schema_version"], V03_SCHEMA_VERSION)
        arousal = next(h for h in batch["heads"] if h["id"] == "arousal_level")
        self.assertEqual(arousal["task_type"], "ordered_single_label")
        self.assertEqual(arousal["ordered_values"], ["LOW", "MEDIUM", "HIGH"])

        code, rows = self.get("/api/assignments?batch_id=v03-api&head=arousal_level")
        self.assertEqual(code, 200)
        self.assertEqual([r["id"] for r in rows], ["v03-api:v-1:arousal_level"])
        code, detail = self.get("/api/assignment?id=v03-api:v-1:arousal_level")
        self.assertEqual(code, 200)
        self.assertEqual(detail["schema_version"], V03_SCHEMA_VERSION)
        self.assertEqual(detail["glossary"]["task_type"], "ordered_single_label")
        self.assertEqual(detail["glossary"]["labels"][0]["id"], "LOW")

        code, record = self.post({
            "assignment_id": "v03-api:v-1:arousal_level",
            "answer": "MEDIUM",
            "is_final": True,
        })
        self.assertEqual(code, 200, record)
        self.assertEqual(record["schema_version"], V03_SCHEMA_VERSION)


class TestArchive(ServerTestBase):
    def test_archive_flow(self):
        self.seed()
        self.post({"assignment_id": "tb:s-1:stance", "answer": "BULL", "is_final": True})
        # 归档：200 + 导出 CSV 快照
        code, r = self.post_path("/api/batch/archive", {"batch_id": "tb"})
        self.assertEqual(code, 200, r)
        self.assertTrue(r["ok"])
        self.assertTrue(r["archived_at"])
        export = Path(r["export_path"])
        self.assertTrue(export.is_file())
        lines = export.read_text(encoding="utf-8-sig").strip().splitlines()
        self.assertEqual(len(lines), 3)  # header + 2 行（含未标注行）
        self.assertIn("BULL", lines[1])
        # 数据保留：annotations 表不动
        self.assertEqual(self.db_count("annotations"), 1)
        # 归档后：列表隐藏、任务清单 404、resume 不再指向、写入被拒、重复归档 400、未知批次 404
        _, batches = self.get("/api/batches")
        self.assertEqual([b["id"] for b in batches], [])
        code, err = self.get("/api/assignments?batch_id=tb&head=stance")
        self.assertEqual(code, 404)
        self.assertIn("已归档", err["error"])
        self.assertEqual(self.get("/api/resume")[1]["batch_id"], None)
        code, err = self.post({"assignment_id": "tb:s-2:stance", "answer": "BEAR", "is_final": True})
        self.assertEqual(code, 400)
        self.assertIn("已归档", err["error"])
        code, err = self.post_path("/api/batch/archive", {"batch_id": "tb"})
        self.assertEqual(code, 400)
        self.assertIn("已归档", err["error"])
        code, _ = self.post_path("/api/batch/archive", {"batch_id": "no_such"})
        self.assertEqual(code, 404)


class TestRoundTrip(ServerTestBase):
    def test_import_export_round_trip(self):
        self.seed(heads=("stance", "reasoning_tags"))
        self.assertEqual(self.db_count("assignments"), 4)
        # 标两条：一 draft 一 final
        self.post({"assignment_id": "tb:s-1:stance", "answer": "BULL", "is_final": False})
        self.post({"assignment_id": "tb:s-2:stance", "answer": "BEAR",
                   "disposition": "跳过", "is_final": True})

        rows = list(export_mod.export_rows(str(self.db), final_only=False))
        tb = [r for r in rows if r["batch"] == "tb"]
        self.assertEqual(len(tb), 4)
        by_id = {(r["sample_id"], r["head"]): r for r in tb}
        # 未标注行也在（answer=null），供审计
        u = by_id[("s-1", "reasoning_tags")]
        self.assertIsNone(u["answer"])
        self.assertFalse(u["is_final"])
        # 字段与输入一致
        a = by_id[("s-1", "stance")]
        self.assertEqual(a["answer"], "BULL")
        self.assertEqual(a["is_final"], False)
        self.assertEqual(a["metadata"]["split_provenance"], "dev/test_v1")
        self.assertEqual(a["schema_version"], SCHEMA_VERSION)
        self.assertIsNotNone(a["updated_at"])
        b = by_id[("s-2", "stance")]
        self.assertEqual(b["answer"], "BEAR")
        self.assertEqual(b["disposition"], "跳过")
        self.assertEqual(b["is_final"], True)
        self.assertEqual(b["metadata"], {})

        # --final-only 只留 is_final=1
        finals = list(export_mod.export_rows(str(self.db), final_only=True))
        self.assertEqual([r for r in finals if r["batch"] == "tb"],
                         [r for r in tb if r["is_final"]])

    def test_csv_export_shape(self):
        import csv
        import io
        self.seed()
        self.post({"assignment_id": "tb:s-1:stance", "answer": "BULL", "is_final": True})
        buf = io.StringIO()
        rows = list(export_mod.export_rows(str(self.db), final_only=True))
        self.assertEqual(len(rows), 1)
        writer = csv.writer(buf)
        writer.writerow(export_mod.CSV_COLUMNS)
        r = rows[0]
        writer.writerow([r["batch"], r["sample_id"], r["head"], r["answer"], r["disposition"],
                         1 if r["is_final"] else 0, r["schema_version"], r["updated_at"],
                         json.dumps(r["metadata"], ensure_ascii=False, sort_keys=True)])
        parsed = list(csv.reader(io.StringIO(buf.getvalue())))
        self.assertEqual(parsed[0], export_mod.CSV_COLUMNS)
        self.assertEqual(parsed[1][0:4], ["tb", "s-1", "stance", "BULL"])


class TestFailClosedImport(ServerTestBase):
    def write(self, name, content):
        p = Path(self._tmp.name) / name
        p.write_text(content, encoding="utf-8")
        return str(p)

    def test_missing_required_field_exits_without_writes(self):
        f = self.write("bad.jsonl", '{"sample_id":"x-1","title":"缺 text"}\n')
        with self.assertRaises(SystemExit) as cm:
            import_batch.parse_samples(f)
        self.assertEqual(cm.exception.code, 1)
        self.assertEqual(self.db_count("samples"), 0)

    def test_duplicate_sample_id_in_file(self):
        f = self.write("dup.jsonl",
                       '{"sample_id":"x-1","text":"a"}\n{"sample_id":"x-1","text":"b"}\n')
        with self.assertRaises(SystemExit) as cm:
            import_batch.parse_samples(f)
        self.assertEqual(cm.exception.code, 1)

    def test_duplicate_against_db_no_partial_write(self):
        self.seed(batch="dup")
        before_samples = self.db_count("samples")
        before_assignments = self.db_count("assignments")
        samples = [{"sample_id": "s-1", "text": "撞库文本", "title": None, "metadata": {}}]
        with self.assertRaises(SystemExit) as cm:
            import_batch.import_samples(str(self.db), "dup", samples, ["stance"], SCHEMA_VERSION)
        self.assertEqual(cm.exception.code, 1)
        self.assertEqual(self.db_count("samples"), before_samples)
        self.assertEqual(self.db_count("assignments"), before_assignments)

    def test_unknown_head_rejected(self):
        samples = [{"sample_id": "y-1", "text": "t", "title": None, "metadata": {}}]
        with self.assertRaises(SystemExit) as cm:
            import_batch.import_samples(str(self.db), "uh", samples, ["not_a_head"], SCHEMA_VERSION)
        self.assertEqual(cm.exception.code, 1)

    def test_unknown_top_level_field_rejected(self):
        f = self.write("extra.jsonl", '{"sample_id":"z-1","text":"t","metdata":{"a":1}}\n')
        with self.assertRaises(SystemExit) as cm:
            import_batch.parse_samples(f)
        self.assertEqual(cm.exception.code, 1)


class TestResume(ServerTestBase):
    def test_resume_lifecycle(self):
        self.seed()
        # 从未动过 → 第一个 position
        _, r = self.get("/api/resume")
        self.assertEqual(r["assignment_id"], "tb:s-1:stance")
        # 最近活动未完成优先
        self.post({"assignment_id": "tb:s-2:stance", "answer": "BEAR", "is_final": False})
        _, r = self.get("/api/resume")
        self.assertEqual(r["assignment_id"], "tb:s-2:stance")
        # final 后排除
        self.post({"assignment_id": "tb:s-2:stance", "answer": "BEAR", "is_final": True})
        _, r = self.get("/api/resume")
        self.assertEqual(r["assignment_id"], "tb:s-1:stance")
        # 稍后再看仍是未完成
        self.post({"assignment_id": "tb:s-1:stance", "answer": None,
                   "disposition": "稍后再看", "is_final": False})
        _, r = self.get("/api/resume")
        self.assertEqual(r["assignment_id"], "tb:s-1:stance")
        # 终态 disposition 后全部完成 → null
        self.post({"assignment_id": "tb:s-1:stance", "answer": None,
                   "disposition": "跳过", "is_final": False})
        _, r = self.get("/api/resume")
        self.assertIsNone(r["assignment_id"])
        self.assertIsNone(r["batch_id"])


class TestApiBasics(ServerTestBase):
    def test_static_index_served(self):
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/") as r:
            body = r.read().decode("utf-8")
            self.assertEqual(r.status, 200)
            self.assertIn("MyResearcher", body)

    def test_assignments_requires_params(self):
        code, _ = self.get("/api/assignments")
        self.assertEqual(code, 400)

    def test_unknown_assignment_404(self):
        code, _ = self.get("/api/assignment?id=nope")
        self.assertEqual(code, 404)
        code, _ = self.post({"assignment_id": "nope", "answer": "BULL"})
        self.assertEqual(code, 404)

    def test_assignment_detail_contains_glossary(self):
        self.seed()
        _, d = self.get("/api/assignment?id=tb:s-1:stance")
        self.assertEqual(d["assignment"]["head"], "stance")
        self.assertEqual(d["sample"]["metadata"]["split_provenance"], "dev/test_v1")
        self.assertEqual(d["glossary"]["type"], "single")
        self.assertEqual([l["id"] for l in d["glossary"]["labels"]],
                         ["BULL", "BEAR", "NEUTRAL", "MIXED", "UNKNOWN"])
        self.assertTrue(any("UNKNOWN≠NEUTRAL" in inv for inv in d["invariants"]))


if __name__ == "__main__":
    unittest.main()
