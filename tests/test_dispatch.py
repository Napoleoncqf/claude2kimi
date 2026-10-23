"""kimi_task.sh 回归测试：用一个假的 kimi（只记参数和环境、吐 stream-json）跑真实的派单脚本。

运行：python3 -m unittest discover -s tests
"""
import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
TASK = REPO / "scripts" / "kimi_task.sh"
TOOL = REPO / "scripts" / "kimi_msg.py"

FAKE_KIMI = '''#!/usr/bin/env python3
import json, os, pathlib, subprocess, sys
args = sys.argv[1:]
pathlib.Path(os.environ["FAKE_ARGS_OUT"]).write_text(json.dumps({
    "args": args, "cwd": os.getcwd(),
    "env": {k: os.environ.get(k) for k in ("KIMI_MSG_CH", "KIMI_MSG_DIR", "KIMI_MODEL_THINKING_EFFORT")}}))
if os.environ.get("FAKE_SAY"):
    subprocess.run([sys.executable, os.environ["FAKE_MSG_TOOL"], "say",
                    os.environ["KIMI_MSG_CH"], os.environ["FAKE_SAY"]], check=True)
rows = [{"role": "meta", "type": "system.version", "version": "fake"},
        {"role": "assistant", "content": "先看看", "tool_calls": [{"id": "t1"}]},
        {"role": "tool", "tool_call_id": "t1", "content": "ok"},
        {"role": "assistant", "content": "FINAL " + pathlib.Path.cwd().name},
        {"role": "meta", "type": "session.resume_hint", "session_id": "session_fake"}]
for r in rows:
    print(json.dumps(r, ensure_ascii=False))
sys.exit(int(os.environ.get("FAKE_RC", "0")))
'''


class Dispatch(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()
        (self.root / ".git").mkdir()
        fake = self.root / "fake-kimi"
        fake.write_text(FAKE_KIMI)
        fake.chmod(0o755)
        self.args_out = self.root / "fake-args.json"
        self.env = dict(os.environ, KIMI_TASK_BIN=str(fake), FAKE_MSG_TOOL=str(TOOL),
                        FAKE_ARGS_OUT=str(self.args_out), KIMI_CODE_HOME=str(self.root / "kimi-home"))
        self.env.pop("KIMI_TASK_MODEL", None)

    def tearDown(self):
        self._tmp.cleanup()

    def dispatch(self, *args, **env):
        r = subprocess.run(["bash", str(TASK), *args], cwd=self.root, env=dict(self.env, **env),
                           capture_output=True, text=True, timeout=30)
        seen = json.loads(self.args_out.read_text()) if self.args_out.exists() else None
        return r, seen

    def test_help_prints_only_the_usage_block(self):
        out = subprocess.run(["bash", str(TASK), "--help"], capture_output=True, text=True).stdout
        self.assertIn("Usage:", out)
        self.assertNotIn("exit $rc", out, "--help 一路打到了脚本末尾")
        self.assertLess(len(out.splitlines()), 40)

    def test_new_dispatch_defaults_to_k31_pro_and_prints_final_answer(self):
        r, seen = self.dispatch("-p", "work")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "FINAL " + self.root.name)
        self.assertIn("SESSION=session_fake", r.stderr)
        args = seen["args"]
        self.assertEqual(args[args.index("-m") + 1], "kimi-code/k3.1-pro")
        self.assertIn("--output-format", args)
        prompt = next(a for a in args if a.startswith("--prompt="))
        self.assertIn("主线口信 · 频道 " + seen["env"]["KIMI_MSG_CH"], prompt)
        self.assertTrue(prompt.endswith("work"))
        self.assertTrue(Path(seen["env"]["KIMI_MSG_DIR"]).is_dir())
        self.assertIsNone(seen["env"]["KIMI_MODEL_THINKING_EFFORT"])

    def test_dispatch_installs_the_kimi_hook(self):
        self.dispatch("-p", "work")
        cfg = (self.root / "kimi-home" / "config.toml").read_text()
        self.assertIn("kimi_msg.py", cfg)
        self.assertIn('event = "Stop"', cfg)

    def test_resume_keeps_the_session_model_unless_asked(self):
        _, seen = self.dispatch("--resume", "session_x", "-p", "again")
        self.assertNotIn("-m", seen["args"])
        self.assertEqual(seen["args"][seen["args"].index("--session") + 1], "session_x")
        _, seen = self.dispatch("--resume", "session_x", "-m", "kimi-code/k3", "-p", "again")
        self.assertEqual(seen["args"][seen["args"].index("-m") + 1], "kimi-code/k3")

    def test_resume_refuses_continue_and_missing_id(self):
        for bad in (["--resume", "--last"], ["--resume", "-c"], ["--resume"]):
            with self.subTest(bad=bad):
                r, seen = self.dispatch(*bad, "-p", "x") if len(bad) > 1 else self.dispatch("-p", "x", *bad)
                self.assertEqual(r.returncode, 2)
                self.assertIsNone(seen, "被拒的单不该真的起 kimi")

    def test_ro_uses_explore_agent_and_effort_goes_through_env(self):
        _, seen = self.dispatch("--ro", "-e", "low", "-p", "look")
        self.assertEqual(seen["args"][seen["args"].index("--agent") + 1], "explore")
        self.assertEqual(seen["env"]["KIMI_MODEL_THINKING_EFFORT"], "low")
        r, _ = self.dispatch("-e", "xhigh", "-p", "look")
        self.assertEqual(r.returncode, 2)

    def test_prompt_starting_with_dash_stays_one_argument(self):
        _, seen = self.dispatch("--no-channel", "-p", "--version 是什么")
        self.assertIn("--prompt=--version 是什么", seen["args"])
        self.assertIsNone(seen["env"]["KIMI_MSG_CH"])

    def test_failed_run_reports_exit_code(self):
        r, _ = self.dispatch("-p", "work", FAKE_RC="3")
        self.assertEqual(r.returncode, 3)
        self.assertIn("EXIT=3", r.stderr)

    def test_note_left_at_close_reaches_the_main_line_in_full(self):
        r, _ = self.dispatch("-p", "work", FAKE_SAY="收工前的报告正文：第三步没跑")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("收工前的报告正文：第三步没跑", r.stderr)

    def test_same_second_dispatches_get_separate_channels(self):
        bin_dir = self.root / "bin"
        bin_dir.mkdir()
        date = bin_dir / "date"                    # 把派单用的时间戳钉在同一秒
        date.write_text('#!/bin/sh\nif [ "$1" = "+%m%d-%H%M%S" ]; then echo 1018-120000; '
                        'else exec "' + shutil.which("date") + '" "$@"; fi\n')
        date.chmod(0o755)
        procs = []
        for name in ("one", "two"):
            wd = self.root / name
            wd.mkdir()
            env = dict(self.env, PATH=f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
                       FAKE_ARGS_OUT=str(wd / "args.json"))
            procs.append(subprocess.Popen(["bash", str(TASK), "-p", "work", "-C", str(wd)],
                                          cwd=self.root, env=env, text=True,
                                          stdout=subprocess.PIPE, stderr=subprocess.PIPE))
        outs = [p.communicate(timeout=30) for p in procs]
        channels = [re.search(r"CHANNEL=(\S+)", err).group(1) for _, err in outs]
        self.assertNotEqual(channels[0], channels[1])
        self.assertIn("FINAL one", outs[0][0])
        self.assertIn("FINAL two", outs[1][0])


class SkillExample(unittest.TestCase):
    """SKILL.md 里的对讲示例照抄就要能跑：Claude Code 在 macOS 上用 zsh，别处常是 bash。"""

    def test_intercom_example_passes_an_absolute_script_path(self):
        lines = (REPO / "SKILL.md").read_text(encoding="utf-8").splitlines()
        assign = next(l for l in lines if l.startswith("M="))
        call = next(l for l in lines if " send <频道>" in l and "$M" in l)
        call = call.replace("<频道>", "c").split("   #", 1)[0]
        script = 'python3() { printf "%s\\n" "$1"; }\n' + assign + "\n" + call
        for shell in ("bash", "zsh"):
            if not shutil.which(shell):
                continue
            with self.subTest(shell=shell):
                r = subprocess.run([shell, "-c", script], capture_output=True, text=True)
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertTrue(r.stdout.strip().startswith("/"), r.stdout)
                self.assertTrue(r.stdout.strip().endswith("kimi_msg.py"), r.stdout)


if __name__ == "__main__":
    unittest.main()
