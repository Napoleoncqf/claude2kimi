# claude2kimi

**English** | [简体中文](README.zh-CN.md)

Let [Claude Code](https://claude.com/claude-code) hand work to a local [Kimi Code CLI](https://moonshotai.github.io/kimi-code/)
worker (`kimi -p`, K3.1 Pro by default), and keep talking to Kimi while it works.

This is the Kimi port of [claude2codex](https://github.com/Napoleoncqf/claude2codex). Claude still dispatches
the work. The worker is now Kimi instead of Codex. It installs as a Claude Code skill (folder name
`dispatch-kimi`) made of three scripts. Nothing runs in the background.

## What it does

| You want to… | Command | What happens |
|---|---|---|
| Dispatch a job | `kimi_task.sh -f brief.md` | Starts a headless `kimi -p`; prints the final answer on stdout when done |
| Tell the worker something mid-run | `kimi_msg.py send` | The worker reads it right before its next tool call. That call is blocked, and the worker re-sends it after reading |
| Make the worker stop and reply | `kimi_msg.py send --stop` | Every tool call is blocked until the worker replies |
| Let the worker ask *you* | worker runs `ask` | It waits in place (up to about 5 minutes); after you `answer`, it continues in the same run |
| Wait until the worker speaks or finishes | `kimi_msg.py wait` | Blocks until there is a question, a report, or the run ends; the exit code says which |
| Follow up / request a fix | `kimi_task.sh --resume <session-id> -p "…"` | Continues the original session with its original model |

## How it works

- **Notes to the worker.** Kimi Code runs a `PreToolUse` hook before every tool call the worker makes. The hook
  checks the inbox. If a note is waiting, it denies the call and uses the note as the deny reason. The model then
  receives that reason as the result of the call. With an empty inbox the hook prints nothing.
  The worker may stop making tool calls and get ready to finish. If a note is still undelivered at that point, a
  `Stop` hook blocks the finish (exit code 2) and hands the note over as a new message.
- **Mailboxes.** Each dispatch gets a channel: a folder `<workdir>/.harness-tmp/kimi-msg/<channel>/` holding
  `inbox.jsonl` (you → worker) and `outbox.jsonl` (worker → you). Both files are append-only.
- **Waking Claude.** `wait` runs as a Claude Code background Bash task and exits the moment the worker speaks, so
  Claude gets a task notification. The optional Claude-side hook `kimi_inbox_hook.py` adds unread questions and
  reports to Claude's context before each of Claude's tool calls.

Differences from claude2codex. Each one comes from how Kimi Code works:

- **The hook is global.** Kimi reads `[[hooks]]` only from the user-level `~/.kimi-code/config.toml`. It has no
  project-level config. So dispatching adds two hooks to that file (PreToolUse and Stop). Their command starts with
  `[ -z "$KIMI_MSG_CH" ] || exec python3 …`. Only worker processes started by the dispatcher have `KIMI_MSG_CH`
  set. In your own kimi sessions the variable is empty, so the hook exits right away without even starting Python.
- **Notes can only arrive by blocking a call.** Tested: when a PreToolUse hook does not deny the call, nothing it
  prints reaches the model. That covers plain text, a `message` field, and `additionalContext`. So, as in
  claude2codex, a note is delivered by blocking the call, and the worker re-sends it afterwards.
- **Notes carry the channel name.** Tested: without any explanation, K3.1 Pro Highspeed (low effort) treated the deny
  reason as a possible prompt injection and ignored it. The dispatcher now tells the worker up front that a block
  starting with `主线口信 · 频道 <channel>` ("main-line note · channel <channel>") is a real note from the main line.
- **No OS sandbox.** In `kimi -p` every tool call is approved automatically, and Bash runs outside any sandbox
  (tested: it has network access). `--ro` switches to Kimi's built-in explore agent, which has no Write/Edit tools.
  It still has Bash, so read-only is not enforced.
- **`ask` waits about 5 minutes at most.** Kimi caps a foreground Bash command at 5 minutes, so `ask` waits 280 s
  by default.
- **A few things are missing.**
  - No `--net` / `--full`: there is no sandbox to toggle.
  - No `-i`: `kimi -p` takes no image argument; put image paths in the brief instead.
  - No capacity retry and no watchdog: Kimi retries 429/overload errors itself, and it force-exits when a
    headless run finishes.

## Requirements

| Requirement | Tested version | Check |
|---|---|---|
| Kimi Code CLI | 2.3.1 | `kimi --version`; sign in with `kimi login` |
| Python | 3.15 (scripts need ≥ 3.9; TOML validation during hook install and the tests need ≥ 3.11) | `python3 --version` |
| bash, git | bundled with macOS | |
| Claude Code | 2.1.295 | `claude --version` |

Tested on macOS only. Your project must be a git repository: the scripts look upward from the current directory
for `.git` to find the project root.

## Install

```bash
git clone https://github.com/Napoleoncqf/claude2kimi.git && cd claude2kimi

# 1. Copy into Claude Code's skills folder
mkdir -p ~/.claude/skills/dispatch-kimi
cp SKILL.md ~/.claude/skills/dispatch-kimi/
cp -R scripts ~/.claude/skills/dispatch-kimi/

# 2. Install the Kimi-side hooks (the first dispatch does this too; once installed, the file is left alone)
python3 ~/.claude/skills/dispatch-kimi/scripts/kimi_msg.py install
kimi doctor config
```

Before changing anything, step 2 backs up the original file to `~/.kimi-code/config.toml.pre-kimi-msg`. Both
files keep the original 600 permissions, because the config holds your tokens.

```jsonc
// 3. (optional) Add this group to hooks.PreToolUse in ~/.claude/settings.json,
//    so a busy Claude session also sees worker questions right away
{
  "matcher": "*",
  "hooks": [
    { "type": "command",
      "command": "python3 \"$HOME/.claude/skills/dispatch-kimi/scripts/kimi_inbox_hook.py\"" }
  ]
}
```

Restart Claude Code afterwards. Step 3 is optional; without it, only `wait` wakes Claude up.

## Usage

```bash
S=~/.claude/skills/dispatch-kimi/scripts
bash $S/kimi_task.sh -f .harness-tmp/brief-x.md --tag x        # main use; run it as a background Bash task in Claude Code
bash $S/kimi_task.sh -p "Count the .py files under src/" --ro  # small read-only job
bash $S/kimi_task.sh -m kimi-code/k3.1-pro-highspeed -e high -f brief.md
bash $S/kimi_task.sh --resume session_xxx -p "Two tests fail. Fix them."
```

Right after dispatch, stderr shows two lines, `CHANNEL=` and `WAKEUP=`. When the run ends, stderr shows
`SESSION=<id> LOG=<stderr log> JSONL=<full transcript> EXIT=<exit code>`, and stdout holds the final answer.
All logs go to `<project root>/.harness-tmp/kimi/`.

Intercom:

```bash
M=~/.claude/skills/dispatch-kimi/scripts/kimi_msg.py
python3 $M send <channel> "Switch to plan B"        # send a note
python3 $M send <channel> --stop "Stop and tell me where you are"
python3 $M wait <channel>                           # exit 0 question / 10 report / 20 finished / 30 timeout / 40 no such channel
python3 $M answer <channel> "Use A"
python3 $M poll [<channel>]
python3 $M ls
```

| Environment variable | Effect |
|---|---|
| `KIMI_TASK_BIN` | Path to the kimi executable (default: `kimi` on PATH, then `~/.kimi-code/bin/kimi`) |
| `KIMI_TASK_MODEL` | Default model for new sessions (default `kimi-code/k3.1-pro`) |
| `KIMI_CODE_HOME` | Kimi's own data-directory variable; if set, the hooks go into `$KIMI_CODE_HOME/config.toml` |

`-e` sets the effort for that dispatch only, through Kimi's `KIMI_MODEL_THINKING_EFFORT` environment variable.
Your config is not changed.

## Language

What the worker reads is in Chinese: the intercom instructions at the top of each prompt, the hook's deny reason,
and the `ask` replies. The scripts' terminal output is in Chinese too. Kimi handles this fine, whatever language
your brief is in. To change it, edit the strings in `kimi_task.sh` and `kimi_msg.py`.

## Troubleshooting

| Symptom | Fix |
|---|---|
| stderr shows `WARN: 对讲钩子没装进 Kimi 配置` ("intercom hook was not installed into the Kimi config") | Run `python3 $M install` to see the error. Usually `config.toml` itself has a syntax error, so run `kimi doctor config` first |
| A note never arrives | Notes can't arrive while the worker runs a long command; they arrive at its next tool call. Check `<project root>/.harness-tmp/kimi-msg/hook-trace.log`, which logs one line every time the hook fires |
| The worker got the note but ignored it | Find the blocked call in the `JSONL=` file. Phrase notes as instructions, not as chat |
| The worker `ask`s, then carries on almost immediately | Its Bash call timed out: the default is 60 s, and it did not set the 300000 ms timeout it was told to use. Kimi moves the command to the background, where it keeps waiting; the worker is told when the answer arrives |
| `REFUSED: 续会话必须给显式 session id` ("resume needs an explicit session id") | Intended: `--continue` or `--session` with no id would attach to your own interactive session |

## Uninstall

```bash
python3 ~/.claude/skills/dispatch-kimi/scripts/kimi_msg.py uninstall   # removes only its own two [[hooks]]
rm -rf ~/.claude/skills/dispatch-kimi
# Then remove the step-3 hook group from ~/.claude/settings.json. Each project's .harness-tmp/kimi* can simply be deleted.
```

## Tests

```bash
python3 -m unittest discover -s tests
```

The tests need no packages and never call the real kimi; dispatch tests use a fake kimi that prints stream-json.
They cover:

- Dispatch arguments: default model, model kept on resume, `--continue` refused, `--ro`, `-e`.
- Separate channels for parallel dispatches.
- Notes the worker leaves just before it finishes.
- Holding a worker while still letting its replies through.
- Notes delivered by the Stop hook.
- The shell gate on the global hook.
- `config.toml` install: idempotent, and keeps the file's permissions.
- The Claude-side inbox hook.

## License

[MIT](LICENSE) © Napoleoncqf
