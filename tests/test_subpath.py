#!/usr/bin/env python3
"""子路径部署（nginx location /labeler/）支持：prefix 挂载下的 API/静态 + 相对路径回归守卫。"""

import json
import sys
import tempfile
import threading
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
TOOLS_DIR = PROJECT_ROOT / "tools"
sys.path.insert(0, str(TOOLS_DIR))

import server as server_mod  # noqa: E402
import import_batch  # noqa: E402

STATIC_DIR = PROJECT_ROOT / "static"
SCHEMA_VERSION = "semantic-schema-calibrated-v0.2.1"


class PrefixHandler(server_mod.Handler):
    """模拟 nginx `location /labeler/ { proxy_pass http://up/; }` 的前缀剥离。"""

    PREFIX = "/labeler"

    def _strip(self):
        if self.path == self.PREFIX:
            self.path = "/"
        elif self.path.startswith(self.PREFIX + "/"):
            self.path = self.path[len(self.PREFIX):]

    def do_GET(self):
        self._strip()
        super().do_GET()

    def do_POST(self):
        self._strip()
        super().do_POST()


class TestSubpathMount(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        glossary = json.loads((PROJECT_ROOT / "schema" / "annotation-schema.v1.json").read_text(encoding="utf-8"))
        server_mod.Handler.store = server_mod.Store(str(Path(self._tmp.name) / "labeler.db"),
                                                    str(Path(self._tmp.name) / "a.jsonl"), glossary)
        server_mod.Handler.store.import_samples(
            "sp", [{"sample_id": "s-1", "text": "子路径文本", "title": None, "metadata": {}}],
            ["stance"], SCHEMA_VERSION)
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), PrefixHandler)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self._tmp.cleanup()

    def get(self, path):
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}{path}") as r:
            return r.status, r.read()

    def test_api_and_static_under_prefix(self):
        code, body = self.get("/labeler/api/batches")
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(body)[0]["id"], "sp")

        code, body = self.get("/labeler/api/assignments?batch_id=sp&head=stance")
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(body)[0]["id"], "sp:s-1:stance")

        code, _ = self.get("/labeler/")
        self.assertEqual(code, 200)
        code, _ = self.get("/labeler/app.js")
        self.assertEqual(code, 200)
        code, _ = self.get("/labeler/manifest.json")
        self.assertEqual(code, 200)

        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/labeler/api/annotations",
            data=json.dumps({"assignment_id": "sp:s-1:stance", "answer": "BULL", "is_final": True}).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req) as r:
            self.assertEqual(json.loads(r.read())["revision"], 1)

    def test_frontend_has_no_root_absolute_asset_paths(self):
        html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
        self.assertNotIn('href="/', html)
        self.assertNotIn('src="/', html)
        man = json.loads((STATIC_DIR / "manifest.json").read_text(encoding="utf-8"))
        self.assertFalse(man["start_url"].startswith("/"))
        self.assertFalse(man.get("scope", "/").startswith("/") and man["scope"] != "./")
        for icon in man["icons"]:
            self.assertFalse(icon["src"].startswith("/"))


if __name__ == "__main__":
    unittest.main()
