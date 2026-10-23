"""ask 等到超时的回归测试：工人自己往下干之后，主线不该再被告知「工人原地等答复」。

运行：python3 -m unittest discover -s tests
"""
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
TOOL = SCRIPTS / "kimi_msg.py"


class ExpiredAsk(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()
        (self.root / ".git").mkdir()
        self.run_tool("register", "c", str(self.root))

    def tearDown(self):
        self._tmp.cleanup()

    def run_tool(self, *args):
        return subprocess.run([sys.executable, str(TOOL), "--root", str(self.root), *args],
                              capture_output=True, text=True, encoding="utf-8", cwd=self.root)

    def test_timed_out_ask_no_longer_counts_as_pending(self):
        r = self.run_tool("ask", "c", "用 A 还是 B？", "--wait", "0.1")
        self.assertIn("超时", r.stdout)
        self.assertIn("没有待答的提问", self.run_tool("answer", "c", "A").stdout)
        self.run_tool("close", "c")
        self.assertEqual(self.run_tool("wait", "c", "--timeout", "5").returncode, 20)

    def test_answered_ask_is_not_marked_expired(self):
        ask = subprocess.Popen([sys.executable, str(TOOL), "--root", str(self.root),
                                "ask", "c", "用 A 还是 B？", "--wait", "20"],
                               stdout=subprocess.PIPE, text=True, encoding="utf-8", cwd=self.root)
        outbox = self.root / ".harness-tmp" / "kimi-msg" / "c" / "outbox.jsonl"
        for _ in range(100):                       # 等工人那条 ask 落盘再答
            if outbox.exists() and '"ask"' in outbox.read_text(encoding="utf-8"):
                break
            time.sleep(0.1)
        self.assertEqual(self.run_tool("wait", "c", "--timeout", "5").returncode, 0)
        self.run_tool("answer", "c", "用 A")
        self.assertIn("用 A", ask.communicate(timeout=20)[0])
        kinds = [json.loads(l).get("kind") for l in outbox.read_text(encoding="utf-8").splitlines()]
        self.assertNotIn("expired", kinds)

    def test_main_line_hook_stops_nagging_about_expired_ask(self):
        self.run_tool("ask", "c", "用 A 还是 B？", "--wait", "0.1")
        env = {k: v for k, v in os.environ.items() if k != "CLAUDE_MSG_CH"}
        payload = json.dumps({"hook_event_name": "PreToolUse", "cwd": str(self.root),
                              "session_id": "s", "tool_name": "Bash"})
        out = subprocess.run([sys.executable, str(SCRIPTS / "kimi_inbox_hook.py")],
                             input=payload, capture_output=True, text=True, encoding="utf-8",
                             env=env, cwd=self.root).stdout
        self.assertNotIn("用 A 还是 B", out)


if __name__ == "__main__":
    unittest.main()
