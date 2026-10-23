"""收件钩子回归测试：主线能看到工人留言；codex2claude 派出去的 Claude 工人看不到。

运行：python3 -m unittest discover -s tests
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"


class InboxHook(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()
        (self.root / ".git").mkdir()
        msg = [sys.executable, str(SCRIPTS / "kimi_msg.py"), "--root", str(self.root)]
        subprocess.run([*msg, "register", "c", str(self.root)], capture_output=True, check=True)
        subprocess.run([*msg, "say", "c", "kimi 工人的报告"], capture_output=True, check=True,
                       cwd=self.root)

    def tearDown(self):
        self._tmp.cleanup()

    def hook(self, **extra_env):
        env = {k: v for k, v in os.environ.items() if k != "CLAUDE_MSG_CH"}
        env.update(extra_env)
        payload = json.dumps({"hook_event_name": "PreToolUse", "cwd": str(self.root),
                              "session_id": "s", "tool_name": "Bash"})
        return subprocess.run([sys.executable, str(SCRIPTS / "kimi_inbox_hook.py")],
                              input=payload, capture_output=True, text=True, encoding="utf-8",
                              env=env, cwd=self.root).stdout

    def test_main_line_sees_worker_report_once(self):
        out = json.loads(self.hook())
        self.assertIn("kimi 工人的报告", out["hookSpecificOutput"]["additionalContext"])
        self.assertEqual(self.hook(), "")

    def test_claude_worker_is_skipped(self):
        self.assertEqual(self.hook(CLAUDE_MSG_CH="claude-task-1"), "")


if __name__ == "__main__":
    unittest.main()
