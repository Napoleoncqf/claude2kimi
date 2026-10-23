"""Kimi 侧钩子回归测试：挂住 / 回话放行、Stop 兜底送口信、全局钩子的 shell 门控。

payload 结构照 kimi-code 2.3.1 实测：PreToolUse 带 tool_name / tool_input.command，
Stop 带 stop_hook_active；Stop 以退出码 2 + stderr 拦下收工时，stderr 会作为新消息交给模型。

运行：python3 -m unittest discover -s tests
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

TOOL = Path(__file__).resolve().parent.parent / "scripts" / "kimi_msg.py"
sys.path.insert(0, str(TOOL.parent))
import kimi_msg as km  # noqa: E402


class Base(unittest.TestCase):
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

    def fire(self, event, command="ls", argv=None, **env_extra):
        env = {k: v for k, v in os.environ.items() if k not in ("KIMI_MSG_CH", "KIMI_MSG_DIR")}
        env.update(env_extra)
        payload = {"hook_event_name": event, "cwd": str(self.root), "session_id": "s"}
        if event == "PreToolUse":
            payload.update(tool_name="Bash", tool_input={"command": command})
        else:
            payload["stop_hook_active"] = False
        return subprocess.run(argv or [sys.executable, str(TOOL), "--root", str(self.root), "--hook"],
                              input=json.dumps(payload), capture_output=True, text=True,
                              encoding="utf-8", env=env, cwd=self.root)

    def decision(self, command):
        out = self.fire("PreToolUse", command, KIMI_MSG_CH="c").stdout
        return json.loads(out)["hookSpecificOutput"]["permissionDecision"] if out else None


class Hold(Base):
    def test_held_worker_can_still_reply(self):
        self.run_tool("send", "c", "--stop", "先停")
        self.assertEqual(self.decision("ls"), "deny")                 # 送达口信，挂住
        self.assertEqual(self.decision("pytest -q"), "deny")          # 挂住期间别的命令照拦
        for verb in ("ask", "say"):
            reply = f'python3 "{TOOL}" {verb} c "收到，已停"'
            self.assertIsNone(self.decision(reply), f"挂住时 {verb} 被拦，工人回不了话")
        self.run_tool("say", "c", "收到，已停")                        # 回话真正执行后解挂
        self.assertIsNone(self.decision("ls"))

    def test_reply_lookalike_is_still_held(self):
        self.run_tool("send", "c", "--stop", "先停")
        self.decision("ls")
        self.assertEqual(self.decision("grep ask kimi_msg.py.bak"), "deny")

    def test_note_carries_the_channel_name(self):
        self.run_tool("send", "c", "改用方案 B")
        out = json.loads(self.fire("PreToolUse", KIMI_MSG_CH="c").stdout)
        self.assertIn("主线口信 · 频道 c", out["hookSpecificOutput"]["permissionDecisionReason"])


class Stop(Base):
    def test_stop_delivers_pending_note_once(self):
        self.run_tool("send", "c", "收工前再补一条测试")
        r = self.fire("Stop", KIMI_MSG_CH="c")
        self.assertEqual(r.returncode, 2)
        self.assertIn("收工前再补一条测试", r.stderr)
        self.assertEqual(r.stdout, "")
        again = self.fire("Stop", KIMI_MSG_CH="c")
        self.assertEqual((again.returncode, again.stdout, again.stderr), (0, "", ""))

    def test_stop_without_notes_lets_worker_finish(self):
        r = self.fire("Stop", KIMI_MSG_CH="c")
        self.assertEqual((r.returncode, r.stdout, r.stderr), (0, "", ""))


class ShellGate(Base):
    """装进 config.toml 的那条命令本身：你自己的 kimi 会话（没有 KIMI_MSG_CH）里必须安静放行。"""

    def gate(self, event="PreToolUse", **env):
        return self.fire(event, argv=["sh", "-c", km.hook_command()], **env)

    def test_own_sessions_are_left_alone(self):
        self.run_tool("send", "c", "只给工人的口信")
        for event in ("PreToolUse", "Stop"):
            r = self.gate(event)
            self.assertEqual((r.returncode, r.stdout, r.stderr), (0, "", ""), event)
        self.assertEqual(len(km.pending_inbox(km.read_jsonl(
            self.root / ".harness-tmp/kimi-msg/c/inbox.jsonl"))), 1, "口信被别人的会话领走了")

    def test_worker_process_gets_the_note(self):
        self.run_tool("send", "c", "只给工人的口信")
        r = self.gate(KIMI_MSG_CH="c")
        self.assertIn("只给工人的口信", r.stdout)

    def test_env_channel_dir_wins_over_cwd_guess(self):
        other = self.root / "elsewhere"
        other.mkdir()
        self.run_tool("send", "c", "按 KIMI_MSG_DIR 找到的")
        env = {k: v for k, v in os.environ.items() if k not in ("KIMI_MSG_CH", "KIMI_MSG_DIR")}
        env.update(KIMI_MSG_CH="c", KIMI_MSG_DIR=str(self.root / ".harness-tmp/kimi-msg/c"))
        payload = json.dumps({"hook_event_name": "PreToolUse", "cwd": str(other),
                              "tool_name": "Bash", "tool_input": {"command": "ls"}})
        r = subprocess.run(["sh", "-c", km.hook_command()], input=payload, capture_output=True,
                           text=True, encoding="utf-8", env=env, cwd=other)
        self.assertIn("按 KIMI_MSG_DIR 找到的", r.stdout)


if __name__ == "__main__":
    unittest.main()
