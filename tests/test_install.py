"""Kimi 配置安装回归测试：只动自己那两条 [[hooks]]，别人的配置原样保留，已装好就不重写文件。

运行：python3 -m unittest discover -s tests
"""
import os
import subprocess
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path

TOOL = Path(__file__).resolve().parent.parent / "scripts" / "kimi_msg.py"
sys.path.insert(0, str(TOOL.parent))
import kimi_msg as km  # noqa: E402

ORIGINAL = '''default_model = "kimi-code/k3.1-pro-highspeed"

[models."kimi-code/k3.1-pro"]
provider = "managed:kimi-code"
capabilities = [ "thinking", "tool_use" ]

[[hooks]]
event = "Notification"
matcher = "task\\\\.completed"
command = "terminal-notifier -message done"

[thinking]
enabled = true
effort = "max"
'''


class Install(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name).resolve()
        self.cfg = self.home / "config.toml"
        self.cfg.write_text(ORIGINAL, encoding="utf-8")
        self.cfg.chmod(0o600)                      # kimi 建的 config.toml 就是 600（里面有 token）
        self.env = dict(os.environ, KIMI_CODE_HOME=str(self.home))

    def tearDown(self):
        self._tmp.cleanup()

    def tool(self, verb):
        r = subprocess.run([sys.executable, str(TOOL), verb], capture_output=True, text=True,
                           encoding="utf-8", env=self.env)
        self.assertEqual(r.returncode, 0, r.stderr)

    def doc(self):
        return tomllib.loads(self.cfg.read_text(encoding="utf-8"))

    def test_install_adds_both_hooks_and_keeps_everything_else(self):
        self.tool("install")
        doc = self.doc()
        ours = {(h["event"], h["command"]) for h in doc["hooks"] if "kimi_msg.py" in h["command"]}
        self.assertEqual({e for e, _ in ours}, {"PreToolUse", "Stop"})
        self.assertIn({"event": "Notification", "matcher": "task\\.completed",
                       "command": "terminal-notifier -message done"}, doc["hooks"])
        self.assertEqual(doc["thinking"], {"enabled": True, "effort": "max"})
        self.assertEqual(doc["models"]["kimi-code/k3.1-pro"]["capabilities"], ["thinking", "tool_use"])
        self.assertEqual((self.home / "config.toml.pre-kimi-msg").read_text(encoding="utf-8"), ORIGINAL)

    def test_install_keeps_the_secret_file_private(self):
        self.tool("install")
        for f in (self.cfg, self.home / "config.toml.pre-kimi-msg"):
            self.assertEqual(f.stat().st_mode & 0o777, 0o600, f.name)
        self.tool("uninstall")
        self.assertEqual(self.cfg.stat().st_mode & 0o777, 0o600)

    def test_second_install_does_not_touch_the_file(self):
        self.tool("install")
        before = self.cfg.stat().st_mtime_ns, self.cfg.read_bytes()
        os.utime(self.cfg, ns=(before[0] - 10**9, before[0] - 10**9))
        stamp = self.cfg.stat().st_mtime_ns
        self.tool("install")
        self.assertEqual(self.cfg.stat().st_mtime_ns, stamp, "装好了还在重写 kimi 的配置")
        self.assertEqual(self.cfg.read_bytes(), before[1])

    def test_moved_skill_refreshes_the_command(self):
        self.tool("install")
        stale = self.cfg.read_text(encoding="utf-8").replace(str(TOOL), "/old/place/kimi_msg.py")
        self.cfg.write_text(stale + '\n[background]\nkeep_alive_on_exit = false\n', encoding="utf-8")
        self.tool("install")
        text = self.cfg.read_text(encoding="utf-8")
        self.assertNotIn("/old/place", text)
        self.assertEqual(self.doc()["background"], {"keep_alive_on_exit": False})
        self.assertEqual(sum("kimi_msg.py" in h["command"] for h in self.doc()["hooks"]), 2)

    def test_uninstall_restores_the_original_settings(self):
        self.tool("install")
        self.tool("uninstall")
        self.assertEqual(self.doc(), tomllib.loads(ORIGINAL))

    def test_hook_command_is_valid_toml_and_round_trips(self):
        self.tool("install")
        cmds = {h["command"] for h in self.doc()["hooks"] if "kimi_msg.py" in h["command"]}
        self.assertEqual(cmds, {km.hook_command()})


if __name__ == "__main__":
    unittest.main()
