"""并发回归测试：registry 并行登记、outbox/inbox 追加式读写不丢行。

运行：python3 -m unittest discover -s tests
"""
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

TOOL = Path(__file__).resolve().parent.parent / "scripts" / "kimi_msg.py"
sys.path.insert(0, str(TOOL.parent))
import kimi_msg as km  # noqa: E402


def run(root, *args, env=None):
    return subprocess.run([sys.executable, str(TOOL), "--root", str(root), *args],
                          capture_output=True, text=True, encoding="utf-8", env=env)


class Concurrency(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()
        (self.root / ".git").mkdir()

    def tearDown(self):
        self._tmp.cleanup()

    def test_parallel_register_keeps_all_channels(self):
        n = 12
        procs = [subprocess.Popen([sys.executable, str(TOOL), "--root", str(self.root),
                                   "register", f"ch{i}", str(self.root)],
                                  stdout=subprocess.DEVNULL) for i in range(n)]
        for p in procs:
            p.wait()
        reg = json.loads((self.root / ".harness-tmp/kimi-msg/registry.json").read_text("utf-8"))
        self.assertEqual(sorted(reg), sorted(f"ch{i}" for i in range(n)))

    def test_answer_does_not_eat_concurrent_worker_lines(self):
        run(self.root, "register", "c", str(self.root))
        outbox = self.root / ".harness-tmp/kimi-msg/c/outbox.jsonl"
        km.append_jsonl(outbox, {"id": "a1", "kind": "ask", "at": "t", "text": "q"})
        stop = threading.Event()
        written = []

        def worker():                                  # 模拟工人持续追加 say
            i = 0
            while not stop.is_set():
                rid = f"s{i}"
                km.append_jsonl(outbox, {"id": rid, "kind": "say", "at": "t", "text": "x"})
                written.append(rid)
                i += 1

        t = threading.Thread(target=worker)
        t.start()
        for _ in range(5):
            run(self.root, "answer", "c", "ok")
        stop.set()
        t.join()
        ids = {r.get("id") for r in km.read_jsonl(outbox)}
        self.assertTrue(set(written) <= ids, "answer 吃掉了工人刚追加的行")
        self.assertIn("a1", km.answers_of(km.read_jsonl(outbox)))
        self.assertEqual(km.unread(outbox)[0], [])      # 已答的提问不再算待答

    def test_hook_marks_read_by_append(self):
        run(self.root, "register", "c", str(self.root))
        d = self.root / ".harness-tmp/kimi-msg/c"
        run(self.root, "send", "c", "hello")
        env = dict(os.environ, KIMI_MSG_CH="c")
        env.pop("KIMI_MSG_DIR", None)
        payload = json.dumps({"hook_event_name": "PreToolUse", "cwd": str(self.root),
                              "tool_name": "Bash", "tool_input": {"command": "ls"}})
        hook = [sys.executable, str(TOOL), "--root", str(self.root), "--hook"]
        out = subprocess.run(hook, input=payload, capture_output=True, text=True, encoding="utf-8",
                             env=env, cwd=self.root)
        self.assertIn("hello", out.stdout)
        self.assertEqual(km.pending_inbox(km.read_jsonl(d / "inbox.jsonl")), [])
        out2 = subprocess.run(hook, input=payload, capture_output=True, text=True, encoding="utf-8",
                              env=env, cwd=self.root)
        self.assertEqual(out2.stdout, "")               # 同一条口信不重复投递；没信时一个字节都不输出


if __name__ == "__main__":
    unittest.main()
