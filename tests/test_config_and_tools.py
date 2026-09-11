#!/usr/bin/env python3
"""配置加载与 gpt_tasks 工具的 unittest；MySQL 集成测试需设 MR_LABELER_TEST_MYSQL 环境变量（JSON）。"""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from storage import (  # noqa: E402
    BASE_SCHEMA_VERSION, V03_SCHEMA_VERSION, ConfigError, SqliteStore,
    load_config, load_schema_catalog,
)

SCHEMA_PATH = PROJECT_ROOT / "schema" / "annotation-schema.v1.json"
SCHEMA_VERSION = "semantic-schema-calibrated-v0.2.1"


def write(tmp, name, obj_or_text):
    p = Path(tmp) / name
    p.write_text(obj_or_text if isinstance(obj_or_text, str) else json.dumps(obj_or_text, ensure_ascii=False),
                 encoding="utf-8")
    return str(p)


class TestLoadConfig(unittest.TestCase):
    def test_defaults_when_no_config_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = load_config(None, base_dir=tmp)
        self.assertEqual(cfg["storage"], "sqlite")
        self.assertTrue(cfg["sqlite"]["db_path"].endswith("labeler.db"))
        self.assertTrue(cfg["jsonl_path"].endswith("annotations.jsonl"))

    def test_relative_paths_resolve_against_config_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            sub = Path(tmp) / "conf"
            sub.mkdir()
            p = write(sub, "config.json", {
                "storage": "sqlite", "jsonl_path": "data/a.jsonl", "sqlite": {"db_path": "data/labeler.db"},
            })
            cfg = load_config(p)
            self.assertEqual(cfg["sqlite"]["db_path"], str((sub / "data" / "labeler.db").resolve()))
            self.assertEqual(cfg["jsonl_path"], str((sub / "data" / "a.jsonl").resolve()))

    def test_mysql_valid_config_normalized(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = write(tmp, "config.json", {
                "storage": "mysql",
                "mysql": {"host": "127.0.0.1", "user": "u", "password": "p", "database": "db"},
            })
            cfg = load_config(p)
        self.assertEqual(cfg["storage"], "mysql")
        self.assertEqual(cfg["mysql"]["port"], 3306)
        self.assertEqual(cfg["mysql"]["charset"], "utf8mb4")

    def test_mysql_missing_password_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = write(tmp, "config.json", {
                "storage": "mysql",
                "mysql": {"host": "127.0.0.1", "user": "u", "database": "db"},
            })
            with self.assertRaises(ConfigError):
                load_config(p)

    def test_unknown_top_level_key_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = write(tmp, "config.json", {"storage": "sqlite", "mstorage": "mysql"})
            with self.assertRaises(ConfigError):
                load_config(p)

    def test_unknown_mysql_key_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = write(tmp, "config.json", {
                "storage": "mysql",
                "mysql": {"host": "h", "user": "u", "password": "p", "database": "d", "passwd": "x"},
            })
            with self.assertRaises(ConfigError):
                load_config(p)

    def test_bad_storage_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = write(tmp, "config.json", {"storage": "oracle"})
            with self.assertRaises(ConfigError):
                load_config(p)

    def test_missing_config_file_rejected_when_explicit(self):
        with self.assertRaises(ConfigError):
            load_config("/no/such/config.json")

    def test_committed_example_file_loads(self):
        cfg = load_config(str(PROJECT_ROOT / "config.example.json"))
        self.assertEqual(cfg["storage"], "sqlite")
        self.assertIn("password", cfg["mysql"])


class TestGptTasks(unittest.TestCase):
    """子进程走 tools/gpt_tasks.py，配置指向临时 sqlite。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        tmp = self._tmp.name
        self.cfg_path = write(tmp, "config.json", {
            "storage": "sqlite", "jsonl_path": "data/a.jsonl", "sqlite": {"db_path": "data/labeler.db"},
        })
        self.samples_path = write(tmp, "samples.jsonl", "\n".join([
            json.dumps({"sample_id": "g-1", "text": "文本一", "metadata": {"k": "v"}}, ensure_ascii=False),
            json.dumps({"sample_id": "g-2", "text": "文本二"}),
        ]) + "\n")
        self.out_path = str(Path(tmp) / "pulled.jsonl")

    def tearDown(self):
        self._tmp.cleanup()

    def run_tool(self, *argv):
        return subprocess.run(
            [sys.executable, str(PROJECT_ROOT / "tools" / "gpt_tasks.py"), *argv],
            capture_output=True, text=True, timeout=60,
        )

    def test_add_then_pull_round_trip(self):
        r = self.run_tool("add", "--batch", "gp", "--file", self.samples_path,
                          "--heads", "stance,reasoning_tags", "--config", self.cfg_path)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("OK add batch=gp", r.stdout)
        self.assertIn("assignments=4", r.stdout)
        self.assertIn("storage=sqlite", r.stdout)

        # 模拟一条标注直接写库
        store = SqliteStore(str(Path(self._tmp.name) / "data" / "labeler.db"), None,
                            json.loads(SCHEMA_PATH.read_text(encoding="utf-8")))
        try:
            store.upsert_annotation("gp:g-1:stance", "BULL", None, True)
        finally:
            store.close()

        r2 = self.run_tool("pull", "--config", self.cfg_path)
        self.assertEqual(r2.returncode, 0, r2.stderr)
        lines = [json.loads(l) for l in r2.stdout.strip().splitlines()]
        self.assertEqual(len(lines), 4)
        self.assertEqual({l["batch"] for l in lines}, {"gp"})
        g1 = next(l for l in lines if l["sample_id"] == "g-1" and l["head"] == "stance")
        self.assertEqual(g1["answer"], "BULL")
        self.assertTrue(g1["is_final"])
        self.assertEqual(g1["metadata"], {"k": "v"})
        g2 = next(l for l in lines if l["sample_id"] == "g-2" and l["head"] == "reasoning_tags")
        self.assertIsNone(g2["answer"])

        r3 = self.run_tool("pull", "--final-only", "--format", "csv", "--config", self.cfg_path)
        self.assertEqual(r3.returncode, 0, r3.stderr)
        rows = r3.stdout.strip().splitlines()
        self.assertEqual(rows[0].split(",")[0], "batch")
        self.assertEqual(len(rows), 2)  # header + 1 final 行
        self.assertIn("rows=1 ", r3.stderr)
        self.assertIn("final_only=True", r3.stderr)

        r4 = self.run_tool("pull", "--out", self.out_path, "--config", self.cfg_path)
        self.assertEqual(r4.returncode, 0, r4.stderr)
        self.assertEqual(r4.stdout, "")
        self.assertTrue(Path(self.out_path).is_file())
        self.assertIn("OK pull rows=4", r4.stderr)

    def test_status_reports_completion(self):
        r = self.run_tool("add", "--batch", "gp", "--file", self.samples_path,
                          "--heads", "stance", "--config", self.cfg_path)
        self.assertEqual(r.returncode, 0, r.stderr)
        # 未标注：complete=false
        r1 = self.run_tool("status", "--batch", "gp", "--config", self.cfg_path)
        self.assertEqual(r1.returncode, 0, r1.stderr)
        s1 = json.loads(r1.stdout)
        self.assertEqual((s1["total"], s1["done"], s1["finals"], s1["complete"]), (2, 0, 0, False))
        # 标完全部（1 final + 1 终态处置）：complete=true
        store = SqliteStore(str(Path(self._tmp.name) / "data" / "labeler.db"), None,
                            json.loads(SCHEMA_PATH.read_text(encoding="utf-8")))
        try:
            store.upsert_annotation("gp:g-1:stance", "BULL", None, True)
            store.upsert_annotation("gp:g-2:stance", None, "跳过", False)
        finally:
            store.close()
        r2 = self.run_tool("status", "--batch", "gp", "--config", self.cfg_path)
        s2 = json.loads(r2.stdout)
        self.assertEqual((s2["done"], s2["finals"], s2["complete"]), (2, 1, True))
        # 批次不存在 → exit 1
        r3 = self.run_tool("status", "--batch", "no_such", "--config", self.cfg_path)
        self.assertEqual(r3.returncode, 1)
        self.assertIn("批次不存在", r3.stderr)

    def test_add_fail_closed_unknown_head(self):
        r = self.run_tool("add", "--batch", "gp", "--file", self.samples_path,
                          "--heads", "not_a_head", "--config", self.cfg_path)
        self.assertEqual(r.returncode, 1)
        self.assertIn("FAIL-CLOSED", r.stderr)
        self.assertFalse((Path(self._tmp.name) / "data" / "labeler.db").exists())

    def test_add_rejects_duplicate_against_db(self):
        first = self.run_tool("add", "--batch", "gp", "--file", self.samples_path,
                              "--heads", "stance", "--config", self.cfg_path)
        self.assertEqual(first.returncode, 0, first.stderr)
        again = self.run_tool("add", "--batch", "gp", "--file", self.samples_path,
                              "--heads", "stance", "--config", self.cfg_path)
        self.assertEqual(again.returncode, 1)
        self.assertIn("FAIL-CLOSED", again.stderr)

    def test_sparse_assignment_file_creates_only_requested_assignments(self):
        sparse_path = write(self._tmp.name, "assignments.jsonl", "\n".join([
            json.dumps({"sample_id": "s-1", "text": "只标 stance", "heads": ["stance"]}, ensure_ascii=False),
            json.dumps({
                "sample_id": "s-2", "text": "标 action 和 reasoning", "title": "标题",
                "metadata": {"source": "fixture"}, "heads": ["action_tendency", "reasoning_tags"],
            }, ensure_ascii=False),
        ]) + "\n")
        r = self.run_tool(
            "add", "--batch", "sparse", "--assignment-file", sparse_path,
            "--config", self.cfg_path,
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("samples=2", r.stdout)
        self.assertIn("heads=3", r.stdout)
        self.assertIn("assignments=3", r.stdout)

        import sqlite3
        db_path = Path(self._tmp.name) / "data" / "labeler.db"
        con = sqlite3.connect(db_path)
        try:
            rows = con.execute(
                "SELECT sample_id, head FROM assignments WHERE batch_id = ? ORDER BY sample_id, head",
                ("sparse",),
            ).fetchall()
            self.assertEqual(rows, [
                ("s-1", "stance"),
                ("s-2", "action_tendency"),
                ("s-2", "reasoning_tags"),
            ])
            sample = con.execute(
                "SELECT title, metadata_json FROM samples WHERE batch_id = ? AND id = ?",
                ("sparse", "s-2"),
            ).fetchone()
            self.assertEqual(sample[0], "标题")
            self.assertEqual(json.loads(sample[1]), {"source": "fixture"})
        finally:
            con.close()

    def test_sparse_assignment_file_rejects_invalid_heads_before_writing(self):
        invalid_records = {
            "unknown": {"sample_id": "bad", "text": "文本", "heads": ["not_a_head"]},
            "duplicate": {"sample_id": "bad", "text": "文本", "heads": ["stance", "stance"]},
            "empty": {"sample_id": "bad", "text": "文本", "heads": []},
        }
        for label, record in invalid_records.items():
            path = write(self._tmp.name, f"{label}.jsonl", json.dumps(record, ensure_ascii=False) + "\n")
            r = self.run_tool(
                "add", "--batch", f"bad-{label}", "--assignment-file", path,
                "--config", self.cfg_path,
            )
            self.assertEqual(r.returncode, 1, (label, r.stderr))
            self.assertIn("FAIL-CLOSED", r.stderr)
            self.assertFalse((Path(self._tmp.name) / "data" / "labeler.db").exists())

    def test_sparse_assignment_file_cannot_be_combined_with_heads(self):
        path = write(self._tmp.name, "with_heads.jsonl", json.dumps({
            "sample_id": "bad", "text": "文本", "heads": ["stance"],
        }, ensure_ascii=False) + "\n")
        r = self.run_tool(
            "add", "--batch", "bad-both", "--assignment-file", path,
            "--heads", "stance", "--config", self.cfg_path,
        )
        self.assertEqual(r.returncode, 1)
        self.assertIn("不能与 --heads 同时使用", r.stderr)
        self.assertFalse((Path(self._tmp.name) / "data" / "labeler.db").exists())

    def test_bad_config_exits_2(self):
        bad = write(self._tmp.name, "bad.json", {"storage": "mysql", "mysql": {}})
        r = self.run_tool("pull", "--config", bad)
        self.assertEqual(r.returncode, 2)
        self.assertIn("配置错误", r.stderr)


class TestV03AuxiliaryAssignments(unittest.TestCase):
    """v0.3 稀疏辅助因子：版本绑定、合法答案与导出契约。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        tmp = self._tmp.name
        self.cfg_path = write(tmp, "config.json", {
            "storage": "sqlite", "jsonl_path": "data/a.jsonl", "sqlite": {"db_path": "data/labeler.db"},
        })

    def tearDown(self):
        self._tmp.cleanup()

    def run_tool(self, *argv):
        return subprocess.run(
            [sys.executable, str(PROJECT_ROOT / "tools" / "gpt_tasks.py"), *argv],
            capture_output=True, text=True, timeout=60,
        )

    def v03_path(self, name="v03.jsonl"):
        return write(self._tmp.name, name, "\n".join([
            json.dumps({
                "sample_id": "v-a", "text": "预期上涨，情绪平静。",
                "heads": ["expectation_phase", "arousal_level"],
            }, ensure_ascii=False),
            json.dumps({
                "sample_id": "v-b", "text": "市场资金与大盘都在观察。",
                "heads": ["market_scope", "discourse_focus"],
            }, ensure_ascii=False),
        ]) + "\n")

    def test_v03_sparse_round_trip_exposes_order_and_preserves_answers(self):
        path = self.v03_path()
        r = self.run_tool(
            "add", "--batch", "v03", "--assignment-file", path,
            "--schema-version", V03_SCHEMA_VERSION, "--config", self.cfg_path,
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("samples=2", r.stdout)
        self.assertIn("heads=4", r.stdout)
        self.assertIn("assignments=4", r.stdout)

        db_path = Path(self._tmp.name) / "data" / "labeler.db"
        catalog = load_schema_catalog()
        store = SqliteStore(str(db_path), None, catalog[V03_SCHEMA_VERSION])
        try:
            batch = store.list_batches()[0]
            self.assertEqual(batch["schema_version"], V03_SCHEMA_VERSION)
            specs = {s["id"]: s for s in batch["heads"]}
            self.assertEqual(specs["arousal_level"]["task_type"], "ordered_single_label")
            self.assertEqual(specs["arousal_level"]["ordered_values"], ["LOW", "MEDIUM", "HIGH"])
            self.assertEqual([r["head"] for r in store.list_assignments("v03", "arousal_level")], ["arousal_level"])

            # 单选与多选均按 v0.3 合同保存；未回答仍是 null/false。
            saved = store.upsert_annotation("v03:v-a:arousal_level", "HIGH", None, True)
            self.assertEqual(saved["schema_version"], V03_SCHEMA_VERSION)
            store.upsert_annotation("v03:v-b:market_scope", ["UNKNOWN"], None, True)
            store.upsert_annotation("v03:v-b:discourse_focus", None, None, False)
            with self.assertRaises(ValueError):
                store.upsert_annotation("v03:v-b:market_scope", ["UNKNOWN", "BROAD_MARKET"], None, True)
            with self.assertRaises(ValueError):
                store.upsert_annotation("v03:v-b:discourse_focus", [], None, True)

            rows = [r for r in store.export_rows(batch_id="v03")]
        finally:
            store.close()

        self.assertEqual(len(rows), 4)
        self.assertTrue(all(r["schema_version"] == V03_SCHEMA_VERSION for r in rows))
        arousal = next(r for r in rows if r["head"] == "arousal_level")
        self.assertEqual(arousal["answer"], "HIGH")
        self.assertTrue(arousal["is_final"])
        unfinished = next(r for r in rows if r["head"] == "discourse_focus")
        self.assertIsNone(unfinished["answer"])
        self.assertFalse(unfinished["is_final"])

        pulled = self.run_tool("pull", "--batch", "v03", "--config", self.cfg_path)
        self.assertEqual(pulled.returncode, 0, pulled.stderr)
        exported = [json.loads(line) for line in pulled.stdout.strip().splitlines()]
        self.assertEqual(len(exported), 4)
        self.assertEqual({r["head"] for r in exported}, {
            "expectation_phase", "arousal_level", "market_scope", "discourse_focus",
        })

    def test_batch_rejects_append_with_different_schema_version(self):
        old = write(self._tmp.name, "old.jsonl", json.dumps({"sample_id": "old-1", "text": "旧批次"}, ensure_ascii=False) + "\n")
        first = self.run_tool(
            "add", "--batch", "mixed", "--file", old, "--heads", "stance", "--config", self.cfg_path,
        )
        self.assertEqual(first.returncode, 0, first.stderr)
        v03 = write(self._tmp.name, "new.jsonl", json.dumps({
            "sample_id": "new-1", "text": "新辅助因子", "heads": ["arousal_level"],
        }, ensure_ascii=False) + "\n")
        second = self.run_tool(
            "add", "--batch", "mixed", "--assignment-file", v03,
            "--schema-version", V03_SCHEMA_VERSION, "--config", self.cfg_path,
        )
        self.assertEqual(second.returncode, 1)
        self.assertIn("schema_version", second.stderr)

        import sqlite3
        con = sqlite3.connect(Path(self._tmp.name) / "data" / "labeler.db")
        try:
            self.assertEqual(con.execute("SELECT schema_version FROM batches WHERE id='mixed'").fetchone()[0], BASE_SCHEMA_VERSION)
            self.assertEqual(con.execute("SELECT COUNT(*) FROM samples WHERE batch_id='mixed'").fetchone()[0], 1)
        finally:
            con.close()


class TestSqliteLegacyMigration(unittest.TestCase):
    """老版本（batches 无 archived_at 列）的库，打开时应自动补列且归档功能可用。"""

    LEGACY_DDL = """
    CREATE TABLE IF NOT EXISTS batches(id TEXT PRIMARY KEY, name TEXT, created_at TEXT, schema_version TEXT);
    CREATE TABLE IF NOT EXISTS samples(batch_id TEXT, id TEXT, title TEXT, content TEXT,
                                       metadata_json TEXT, PRIMARY KEY(batch_id, id));
    CREATE TABLE IF NOT EXISTS assignments(id TEXT PRIMARY KEY, batch_id TEXT, sample_id TEXT,
                                           head TEXT, position INTEGER, status TEXT DEFAULT 'pending');
    CREATE TABLE IF NOT EXISTS annotations(assignment_id TEXT PRIMARY KEY, answer_json TEXT,
                                           disposition TEXT, is_final INTEGER, revision INTEGER, updated_at TEXT);
    """

    def test_legacy_db_gets_archived_at_and_archive_works(self):
        import sqlite3
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "legacy.db"
            con = sqlite3.connect(str(db))
            try:
                con.executescript(self.LEGACY_DDL)
                con.execute("INSERT INTO batches VALUES('lg', 'legacy', '2026-01-01T00:00:00Z', 'x')")
                con.execute("INSERT INTO samples VALUES('lg', 's-1', NULL, '正文', '{}')")
                con.execute("INSERT INTO assignments VALUES('lg:s-1:stance', 'lg', 's-1', 'stance', 0, 'pending')")
                con.commit()
            finally:
                con.close()

            glossary = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
            store = SqliteStore(str(db), None, glossary)
            try:
                self.assertFalse(store.is_batch_archived("lg"))
                ts = store.archive_batch("lg")
                self.assertTrue(ts)
                self.assertTrue(store.is_batch_archived("lg"))
                self.assertEqual(store.list_batches(), [])
                self.assertEqual(store.get_resume()["batch_id"], None)
                with self.assertRaises(ValueError):
                    store.upsert_annotation("lg:s-1:stance", "BULL", None, False)
                with self.assertRaises(ValueError):
                    store.archive_batch("lg")
                with self.assertRaises(KeyError):
                    store.archive_batch("no_such")
            finally:
                store.close()

            # 重开：迁移幂等（不重复加列）、归档状态保留
            store2 = SqliteStore(str(db), None, glossary)
            try:
                self.assertTrue(store2.is_batch_archived("lg"))
            finally:
                store2.close()


class TestMysqlStore(unittest.TestCase):
    """opt-in：MR_LABELER_TEST_MYSQL='{"host":"127.0.0.1","port":13306,"user":"labeler",
    "password":"...","database":"myresearcher_labeler"}' 时才跑。"""

    MYSQL_ENV = os.environ.get("MR_LABELER_TEST_MYSQL")

    @classmethod
    def setUpClass(cls):
        if not cls.MYSQL_ENV:
            raise unittest.SkipTest("未设置 MR_LABELER_TEST_MYSQL，跳过 MySQL 集成测试")
        cfg = json.loads(cls.MYSQL_ENV)
        cfg.setdefault("port", 3306)
        cfg.setdefault("charset", "utf8mb4")
        cls.mysql_cfg = cfg
        from storage import MysqlStore
        cls.MysqlStore = MysqlStore
        glossary = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
        cls.store = MysqlStore(cfg, None, glossary)

    @classmethod
    def tearDownClass(cls):
        if not cls.MYSQL_ENV:
            return
        cur = cls.store.conn.cursor()
        try:
            for t in ("annotations", "assignments", "samples", "batches"):
                cur.execute(f"DROP TABLE IF EXISTS {t}")
            cls.store.conn.commit()
        finally:
            cur.close()
        cls.store.close()

    def test_import_upsert_export_resume_lifecycle(self):
        batch = "mt1"
        samples = [
            {"sample_id": "m-1", "text": "mysql 文本一", "title": None, "metadata": {"date": "2026-09-01"}},
            {"sample_id": "m-2", "text": "mysql 文本二", "title": "标题二", "metadata": {}},
        ]
        stats = self.store.import_samples(batch, samples, ["stance", "reasoning_tags"], SCHEMA_VERSION)
        self.assertEqual(stats, {"samples": 2, "assignments": 4})

        # 中文与 emoji 经 utf8mb4 往返无损
        r1 = self.store.upsert_annotation("mt1:m-1:stance", "BULL", None, False)
        self.assertEqual(r1["revision"], 1)
        r2 = self.store.upsert_annotation("mt1:m-1:stance", "BEAR", "跳过", True)
        self.assertEqual(r2["revision"], 2)
        self.assertEqual(r2["answer"], "BEAR")

        rows = self.store.list_assignments(batch, "stance")
        self.assertEqual(len(rows), 2)
        row = next(r for r in rows if r["id"] == "mt1:m-1:stance")
        self.assertEqual(row["answer"], "BEAR")
        self.assertEqual(row["disposition"], "跳过")
        self.assertTrue(row["is_final"])
        self.assertEqual(row["status"], "completed")

        detail = self.store.get_assignment("mt1:m-2:reasoning_tags")
        self.assertEqual(detail["sample"]["content"], "mysql 文本二")
        self.assertEqual(detail["glossary"]["type"], "multi")

        # 导出：未标注行也在（按 batch 过滤，与其他测试的 batch 隔离）
        exp = [r for r in self.store.export_rows() if r["batch"] == batch]
        self.assertEqual(len(exp), 4)
        finals = [r for r in self.store.export_rows(final_only=True, batch_id=batch)]
        self.assertEqual(len(finals), 1)

        # resume 生命周期（position 序，同 position 按 id 字典序；m-1:stance 已 final）
        self.assertEqual(self.store.get_resume()["assignment_id"], "mt1:m-1:reasoning_tags")
        self.store.upsert_annotation("mt1:m-1:reasoning_tags", ["RUMOR"], "缺少上下文", False)
        self.assertEqual(self.store.get_resume()["assignment_id"], "mt1:m-2:reasoning_tags")
        self.store.upsert_annotation("mt1:m-2:reasoning_tags", ["RUMOR"], "缺少上下文", False)
        self.assertEqual(self.store.get_resume()["assignment_id"], "mt1:m-2:stance")
        self.store.upsert_annotation("mt1:m-2:stance", "NEUTRAL", None, True)
        self.assertIsNone(self.store.get_resume()["assignment_id"])

        # 幂等：同 assignment 只有一行
        cur = self.store.conn.cursor()
        try:
            cur.execute("SELECT COUNT(*) AS c FROM annotations")
            self.assertEqual(cur.fetchone()["c"], 4)
        finally:
            cur.close()

    def test_archive_batch_hides_and_blocks(self):
        batch = "mt-arch"
        samples = [{"sample_id": "a-1", "text": "归档文本", "title": None, "metadata": {}}]
        self.store.import_samples(batch, samples, ["stance"], SCHEMA_VERSION)
        ts = self.store.archive_batch(batch)
        self.assertTrue(ts)
        self.assertTrue(self.store.is_batch_archived(batch))
        self.assertEqual([b["id"] for b in self.store.list_batches() if b["id"] == batch], [])
        with self.assertRaises(ValueError):
            self.store.upsert_annotation("mt-arch:a-1:stance", "BULL", None, False)
        with self.assertRaises(ValueError):
            self.store.archive_batch(batch)
        # 自清理
        cur = self.store.conn.cursor()
        try:
            cur.execute("DELETE FROM assignments WHERE batch_id = 'mt-arch'")
            cur.execute("DELETE FROM samples WHERE batch_id = 'mt-arch'")
            cur.execute("DELETE FROM batches WHERE id = 'mt-arch'")
            self.store.conn.commit()
        finally:
            cur.close()

    def test_import_duplicate_vs_db_rejected_no_partial_write(self):
        batch = "mt-dup"
        samples = [{"sample_id": "d-1", "text": "首导", "title": None, "metadata": {}}]
        stats = self.store.import_samples(batch, samples, ["stance"], SCHEMA_VERSION)
        self.assertEqual(stats["samples"], 1)
        with self.assertRaises(ValueError):
            self.store.import_samples(batch, samples, ["stance"], SCHEMA_VERSION)
        cur = self.store.conn.cursor()
        try:
            cur.execute("SELECT COUNT(*) AS c FROM samples WHERE batch_id = 'mt-dup'")
            self.assertEqual(cur.fetchone()["c"], 1)
            cur.execute("SELECT COUNT(*) AS c FROM assignments WHERE batch_id = 'mt-dup'")
            self.assertEqual(cur.fetchone()["c"], 1)
            # 自清理，避免干扰按时间序断言 resume 的另一个用例
            cur.execute("DELETE FROM annotations WHERE assignment_id LIKE 'mt-dup:%'")
            cur.execute("DELETE FROM assignments WHERE batch_id = 'mt-dup'")
            cur.execute("DELETE FROM samples WHERE batch_id = 'mt-dup'")
            cur.execute("DELETE FROM batches WHERE id = 'mt-dup'")
            self.store.conn.commit()
        finally:
            cur.close()


if __name__ == "__main__":
    unittest.main()
