#!/usr/bin/env python3
"""前端纯转换回归：v0.3 multi UNKNOWN 与具体标签互斥。"""

import json
import shutil
import subprocess
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
APP_JS = PROJECT_ROOT / "static" / "app.js"


@unittest.skipUnless(shutil.which("node"), "需要 Node.js 执行前端纯函数回归")
class TestFrontendUnknownExclusive(unittest.TestCase):
    def transition(self, labels):
        script = r'''
const fs = require("fs");
const source = fs.readFileSync(process.argv[1], "utf8");
const start = source.indexOf("function nextMultiAnswer");
const end = source.indexOf("\nconst isDone", start);
if (start < 0 || end < 0) throw new Error("nextMultiAnswer helper not found");
eval(source.slice(start, end));
let answer = [];
const states = [];
for (const label of JSON.parse(process.argv[2])) {
  answer = nextMultiAnswer(answer, label, true);
  states.push(answer);
}
process.stdout.write(JSON.stringify(states));
'''
        result = subprocess.run(
            [shutil.which("node"), "-e", script, str(APP_JS), json.dumps(labels)],
            capture_output=True, text=True, check=True, timeout=10,
        )
        return json.loads(result.stdout)

    def test_concrete_then_unknown_clears_concrete(self):
        self.assertEqual(
            self.transition(["TRADING_FLOW", "UNKNOWN"]),
            [["TRADING_FLOW"], ["UNKNOWN"]],
        )

    def test_unknown_then_concrete_removes_unknown(self):
        self.assertEqual(
            self.transition(["UNKNOWN", "BROAD_MARKET"]),
            [["UNKNOWN"], ["BROAD_MARKET"]],
        )


if __name__ == "__main__":
    unittest.main()
