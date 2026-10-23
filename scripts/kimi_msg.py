#!/usr/bin/env python3
"""kimi_msg.py —— 给正在干活的 Kimi Code 工人发消息 / 让它回话（双向对讲机）。

为什么要有它：`kimi -p` 派出去的工人是无头的，一旦开跑就没有输入口。

两个方向各走各的路：

  主线 → 工人：Kimi Code 的 `PreToolUse` 钩子在工人**每次调工具**前都会触发。
      钩子读信箱，有信就把这次调用拦下来（deny），把口信当拦截理由塞回去 ——
      拦截理由会作为这次调用的结果交给模型。没信时钩子什么都不输出，零 token。
      实测（kimi-code 2.3.1）：不拦截时钩子输出的任何内容（纯文本、`message`、
      `additionalContext`）都进不了模型上下文，所以只能走拦截。
      工人准备收工、不再调工具时，`Stop` 钩子兜底：有没送到的口信就以退出码 2
      拦下收工，口信作为新消息塞给它接着干（实测同一回合只触发一次）。

  工人 → 主线：工人跑 `kimi_msg.py ask <频道> "问题"`，本进程把问题写进发件箱后
      **原地阻塞**轮询答复。主线答一句它就接着干，不用结束回合再找人。
      Kimi 的 Bash 前台命令最长 5 分钟，所以 ask 默认只等 280 秒。

叫醒主线：
  第一层 `wait`（主线闲着也能被叫醒）：主线派完单顺手挂一个
      `python kimi_msg.py wait <频道>`（Claude Code 的 Bash run_in_background）。
      它阻塞到工人有提问 / 报告 / 收工，一退出主线就收到后台任务通知。退出码见 WAIT_* 常量。
  第二层 `kimi_inbox_hook.py`（主线正在干活时即时看到）：挂在 Claude Code 的
      `PreToolUse` 上，主线每次调工具前把各活频道的未读提问 / 报告塞进上下文。

钩子装在哪：Kimi Code 只认用户级 `~/.kimi-code/config.toml`（或 `$KIMI_CODE_HOME/config.toml`）
里的 `[[hooks]]`，没有项目级配置。所以钩子是全局的，命令开头用 shell 判断
`$KIMI_MSG_CH`：只有派单脚本起的工人进程带这个变量，你自己的 kimi 会话里它是空的，
钩子当场退出，连 Python 都不启动。

频道目录 = <工位>/.harness-tmp/kimi-msg/<频道>/；项目根 .harness-tmp/kimi-msg/registry.json
登记 频道 → 绝对路径，主线照它找。

项目根怎么定：`--root <目录>` 最优先，其次环境变量 KIMI_MSG_ROOT，再其次从当前目录
往上找第一个带 .git 的目录（worktree 里 .git 是文件，照样认），都找不到就用当前目录。

用法（主线）：
    python kimi_msg.py ls
    python kimi_msg.py send <频道> "口信"            # 下一次调工具前送到
    python kimi_msg.py send <频道> --stop "停手，…"   # 送到并挂住：回话前不许再动手
    python kimi_msg.py poll [<频道>]                 # 看工人问了什么 / 报了什么
    python kimi_msg.py answer <频道> "答复"          # 答最新一条待答的问
    python kimi_msg.py wait <频道> [--timeout 秒]    # 挂后台守着，工人一出声就退出（= 叫醒）

用法（工人，派单开头里会给它抄好整行）：
    python kimi_msg.py ask <频道> "问题" [--wait 280]   # 问一句并等答复
    python kimi_msg.py say <频道> "进度 / 发现"          # 只报不等

用法（安装 / 卸载 Kimi 侧钩子；kimi_task.sh 派单时会自动 install，已装好就不动文件）：
    python kimi_msg.py install
    python kimi_msg.py uninstall
"""
from __future__ import annotations

import json
import os
import re
import stat
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path


def find_root(start) -> "Path | None":
    """项目根 = 从 start 往上第一个带 .git 的目录（worktree 里 .git 是文件，所以用 exists()）。"""
    try:
        p = Path(start).resolve()
    except Exception:
        return None
    for cand in [p, *p.parents]:
        try:
            if (cand / ".git").exists():
                return cand
        except Exception:
            continue
    return None


ROOT = find_root(os.environ.get("KIMI_MSG_ROOT") or Path.cwd()) or Path.cwd()
REGISTRY = ROOT / ".harness-tmp" / "kimi-msg" / "registry.json"
PYX = Path(sys.executable).as_posix() if sys.executable else "python3"


def tool_hint() -> str:
    """打给人看的命令里写哪条路径：本文件的绝对路径。"""
    return str(Path(__file__).resolve()).replace("\\", "/")


def set_root(path) -> None:
    global ROOT, REGISTRY
    ROOT = Path(path).resolve()
    REGISTRY = ROOT / ".harness-tmp" / "kimi-msg" / "registry.json"


DEFAULT_WAIT = 280          # Kimi 的 Bash 前台命令最长 300 秒，ask 要在这之前自己返回
POLL_EVERY = 2.0            # 工人 `ask` 阻塞时的轮询间隔 = 主线 `answer` 后工人几秒内拿到；别调大

# —— `wait` 守候器的退出码（主线把它挂后台，进程一退主线就收到通知，看码即知发生了什么）——
WAIT_ASK = 0                # 工人在等答复（最要紧：它原地阻塞着，不答它就干等到超时）
WAIT_SAY = 10               # 只有报告，工人没在等，看一眼就行
WAIT_GONE = 20              # 工人收工了（频道已关），没有留话
WAIT_TIMEOUT = 30           # 等到超时都没动静（工人还在干，要接着守就再挂一个）
WAIT_NOCHAN = 40            # 频道始终没出现（频道名打错 / 派单没起来）
WAIT_POLL_EVERY = 1.0
WAIT_DEFAULT_TIMEOUT = 3600


def now() -> str:
    return datetime.now(timezone.utc).astimezone().strftime("%m-%d %H:%M:%S")


def read_jsonl(path: Path) -> list:
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                rows.append(json.loads(line))
            except ValueError:
                pass
    return rows


def append_jsonl(path: Path, row: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as out:
        out.write(json.dumps(row, ensure_ascii=False) + "\n")


def answers_of(rows: list) -> dict:
    """outbox 里的答复：追加行 {"kind":"answer","of":<提问id>}。

    答复只追加、不改写 outbox —— 工人随时可能往同一个文件追加新行，改写会吃掉它刚写的那行。
    """
    return {r["of"]: r for r in rows if r.get("kind") == "answer" and r.get("of")}


def pending_inbox(rows: list) -> list:
    """inbox 里还没送达的口信；送达标记是追加行 {"kind":"read","of":<id>}。"""
    done = {r.get("of") for r in rows if r.get("kind") == "read"}
    return [r for r in rows if r.get("kind") != "read" and r.get("id") and r["id"] not in done]


class file_lock:
    """跨进程的简易互斥（O_EXCL 锁文件），只用来保护 registry.json 这种读改写。

    拿不到（超时 / 异常）就放行并继续：锁只是降低冲突概率，不能反过来卡死派单。
    """

    def __init__(self, target: Path, timeout: float = 10.0):
        self.path = Path(str(target) + ".lock")
        self.timeout = timeout
        self.held = False

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.time() + self.timeout
        while True:
            try:
                os.close(os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY))
                self.held = True
                return self
            except FileExistsError:
                try:                                   # 持锁进程崩了留下的死锁：超时后强拆
                    if time.time() - self.path.stat().st_mtime > self.timeout:
                        self.path.unlink(missing_ok=True)
                        continue
                except OSError:
                    pass
                if time.time() >= deadline:
                    return self
                time.sleep(0.05)
            except OSError:
                return self

    def __exit__(self, *exc):
        if self.held:
            self.path.unlink(missing_ok=True)
        return False


# ——————————————————————————— 频道定位 ———————————————————————————

def registry() -> dict:
    if REGISTRY.exists():
        try:
            return json.loads(REGISTRY.read_text(encoding="utf-8"))
        except ValueError:
            return {}
    return {}


def register(channel: str, workdir: Path) -> Path:
    """派单时调用：登记频道并建目录，返回频道目录。"""
    chdir = Path(workdir).resolve() / ".harness-tmp" / "kimi-msg" / channel
    chdir.mkdir(parents=True, exist_ok=True)
    (chdir / "live").write_text(now(), encoding="utf-8")
    REGISTRY.parent.mkdir(parents=True, exist_ok=True)
    with file_lock(REGISTRY):                      # 并行派单：读改写必须互斥，否则后写的覆盖先写的登记
        reg = registry()
        reg[channel] = {"dir": str(chdir), "workdir": str(Path(workdir).resolve()), "opened": now()}
        tmp = REGISTRY.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(reg, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(REGISTRY)
    return chdir


def env_channel_dir(channel: str) -> "Path | None":
    """派单脚本给工人进程 export 了 KIMI_MSG_CH / KIMI_MSG_DIR，工人的 Bash 和钩子都继承得到。"""
    d = os.environ.get("KIMI_MSG_DIR")
    if d and os.environ.get("KIMI_MSG_CH") == channel and Path(d).is_dir():
        return Path(d)
    return None


def resolve(channel: str) -> Path:
    """主线 / 工人侧：频道名 → 频道目录。登记表优先，其次环境变量，最后按当前目录猜。"""
    hit = registry().get(channel)
    if hit:
        return Path(hit["dir"])
    from_env = env_channel_dir(channel)
    if from_env:
        return from_env
    tried = []
    for base in (Path.cwd(), find_root(Path.cwd()), ROOT):
        if base is None:
            continue
        guess = Path(base) / ".harness-tmp" / "kimi-msg" / channel
        if guess.exists():
            return guess
        tried.append(str(guess))
    raise SystemExit(f"找不到频道 {channel}（登记表 {REGISTRY} 里没有，这些落点也不存在：{tried}）")


def resolve_for_hook(payload: dict) -> "Path | None":
    """钩子侧：只认环境变量 `KIMI_MSG_CH`。

    故意不做「按目录猜唯一活频道」的兜底：你自己的交互 kimi 常在同一个仓库里干活，
    猜出来就会让你的会话收到发给工人的口信。拿不到频道 = 不归这把钩子管，安静退出。
    """
    ch = os.environ.get("KIMI_MSG_CH")
    if not ch:
        return None
    from_env = env_channel_dir(ch)
    if from_env:
        return from_env
    for base in (payload.get("cwd"), Path.cwd(), find_root(Path.cwd())):
        if not base:
            continue
        d = Path(base) / ".harness-tmp" / "kimi-msg" / ch
        if d.exists():
            return d
    return None


# ——————————————————————————— 钩子挂载（~/.kimi-code/config.toml）———————————————————————————

MARK = "kimi_msg.py"
HOOK_EVENTS = ("PreToolUse", "Stop")
HEADER = re.compile(r"^\[\[?\s*[A-Za-z0-9_\-.\"' ]+\s*\]\]?\s*(#.*)?$")


def kimi_config() -> Path:
    home = os.environ.get("KIMI_CODE_HOME")
    return (Path(home) if home else Path.home() / ".kimi-code") / "config.toml"


def hook_command() -> str:
    # 全局钩子：先用 shell 判断本进程是不是派出去的工人，不是就当场退出 0、不输出一个字。
    return f'[ -z "$KIMI_MSG_CH" ] || exec "{PYX}" "{tool_hint()}" --hook'


def split_tables(text: str) -> list:
    """把 TOML 文本按顶格的 [表头] / [[数组表头]] 切块：[(表头行或 None, 块内所有行)]。"""
    chunks = [[None, []]]
    for line in text.splitlines(keepends=True):
        if HEADER.match(line.rstrip("\r\n")):
            chunks.append([line.strip(), [line]])
        else:
            chunks[-1][1].append(line)
    return chunks


def ours(chunk) -> bool:
    head, lines = chunk
    return head is not None and head.replace(" ", "").startswith("[[hooks]]") \
        and any(MARK in l for l in lines)


def hook_blocks() -> str:
    cmd = json.dumps(hook_command(), ensure_ascii=False)   # JSON 字符串转义同时是合法的 TOML 基本字符串
    return "".join(f'\n[[hooks]]\nevent = "{ev}"\ncommand = {cmd}\ntimeout = 10\n' for ev in HOOK_EVENTS)


def wanted() -> set:
    return {(ev, hook_command()) for ev in HOOK_EVENTS}


def installed(text: str) -> "set | None":
    """config 里现有的对讲钩子 {(event, command)}；Python < 3.11 没有 tomllib 时返回 None。"""
    try:
        import tomllib
    except ImportError:
        return None
    doc = tomllib.loads(text)                      # 文件本身坏了就让异常冒出去：坏文件上不该再写
    return {(h.get("event"), h.get("command")) for h in doc.get("hooks", [])
            if isinstance(h, dict) and MARK in str(h.get("command"))}


def without_ours(text: str) -> str:
    # 拿掉对讲钩子块；块里夹带的注释行属于后面别人的配置，留着。
    out = []
    for c in split_tables(text):
        out.extend(l for l in c[1] if not ours(c) or l.lstrip().startswith("#"))
    return "".join(out).rstrip("\n")


def write_config(cfg: Path, text: str, expect: set) -> None:
    got = installed(text)
    if got is not None and got != expect:
        raise ValueError(f"改完读回来的钩子不对：{got}")
    # config.toml 里有 OAuth token / api_key，kimi 建它时是 600：备份和新文件都得保持原权限。
    mode = stat.S_IMODE(cfg.stat().st_mode) if cfg.exists() else 0o600
    backup = cfg.with_name(cfg.name + ".pre-kimi-msg")
    if cfg.exists() and not backup.exists():
        backup.write_bytes(cfg.read_bytes())
        os.chmod(backup, mode)
    tmp = cfg.with_name(cfg.name + ".kimi-msg.tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, "w", encoding="utf-8") as out:
        out.write(text)
    os.chmod(tmp, mode)                            # umask 可能把 O_CREAT 的权限削掉，补一刀
    tmp.replace(cfg)


def install() -> Path:
    """把对讲钩子挂进 Kimi 的用户级 config.toml（幂等：已经是这两条就不碰文件）。

    kimi 自己也会改写这个文件（登录、拉取模型列表时），所以没变化时绝不重写，
    免得和它撞车；要改时先备份一次（config.toml.pre-kimi-msg），写临时文件再原子替换。
    """
    cfg = kimi_config()
    text = cfg.read_text(encoding="utf-8") if cfg.exists() else ""
    got = installed(text)
    if got == wanted() or (got is None and hook_blocks().strip() in text):
        return cfg
    kept = without_ours(text)
    cfg.parent.mkdir(parents=True, exist_ok=True)
    write_config(cfg, (kept + "\n" if kept else "") + hook_blocks(), wanted())
    return cfg


def uninstall() -> Path:
    cfg = kimi_config()
    if cfg.exists():
        text = cfg.read_text(encoding="utf-8")
        if any(ours(c) for c in split_tables(text)):
            write_config(cfg, without_ours(text) + "\n", set())
    return cfg


# ——————————————————————————— 钩子面（主线 → 工人）———————————————————————————

HOW_TO_REPLY = (
    "——要回主线就用 Bash 跑这行，timeout 设 300000（问完会原地等答复，答了就接着干）："
    "\n  {PYX} {tool} ask {ch} \"你的问题\""
)

REPLY_CALL = re.compile(r"kimi_msg\.py[\"']?\s+(?:--root\s+\S+\s+)?(?:ask|say)\s")


def is_reply_call(payload: dict) -> bool:
    """这次调用是不是工人在回话（ask / say）。挂住期间只放行它，否则工人永远回不了话、也解不了挂。"""
    if payload.get("tool_name") != "Bash":
        return False
    return bool(REPLY_CALL.search(str((payload.get("tool_input") or {}).get("command", ""))))


def trace(payload: dict, chdir, decision: str) -> None:
    """钩子有没有真被叫起来、当时看见的是什么 —— 唯一的诊断口，出问题先看它。只记元数据。"""
    try:
        line = {"at": now(), "env_ch": os.environ.get("KIMI_MSG_CH"),
                "resolved": str(chdir) if chdir else None, "session": payload.get("session_id"),
                "event": payload.get("hook_event_name"), "tool": payload.get("tool_name"),
                "decision": decision}
        p = (find_root(payload.get("cwd") or Path.cwd()) or ROOT) / ".harness-tmp" / "kimi-msg" / "hook-trace.log"
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as out:
            out.write(json.dumps(line, ensure_ascii=False) + "\n")
    except Exception:
        pass


def take_pending(chdir: Path) -> list:
    inbox = chdir / "inbox.jsonl"
    pending = pending_inbox(read_jsonl(inbox))
    for r in pending:                              # 只追加已读标记，不改写 inbox（主线 send 可能正在追加）
        append_jsonl(inbox, {"kind": "read", "of": r["id"], "at": now()})
    return pending


def note_body(pending: list, ch: str) -> str:
    return "\n\n".join(f"【主线口信 · 频道 {ch} · {r['at']}】{r['text']}" for r in pending)


def hook() -> int:
    """返回进程退出码。任何异常都当没发生：钩子坏掉不能拖垮工人（fail-open）。"""
    try:
        payload = json.loads(sys.stdin.buffer.read().decode("utf-8-sig") or "{}")
    except ValueError:
        payload = {}
    chdir = resolve_for_hook(payload) if payload else None
    event = payload.get("hook_event_name")
    if chdir is None or event not in HOOK_EVENTS:
        trace(payload, chdir, "skip")
        return 0
    ch = chdir.name
    reply = HOW_TO_REPLY.format(PYX=PYX, tool=tool_hint(), ch=ch)
    state_file = chdir / "state.json"

    if event == "Stop":
        # 工人要收工了：主线最后时刻捎的话不能丢，以退出码 2 拦下收工、口信当新消息塞给它。
        pending = take_pending(chdir)
        if not pending:
            trace(payload, chdir, "pass")
            return 0
        trace(payload, chdir, "block-stop")
        sys.stderr.write("（先别收工：主线在你收工前捎来了口信。照口信办完，再重新给出完整的最终报告 ——"
                         "主线只看你最后一条消息，前面已经写过的结论也要一并带上。）\n\n"
                         f"{note_body(pending, ch)}\n\n{reply}")
        return 2

    held = False
    if state_file.exists():
        try:
            held = bool(json.loads(state_file.read_text(encoding="utf-8")).get("hold"))
        except ValueError:
            held = False
    if not held and not pending_inbox(read_jsonl(chdir / "inbox.jsonl")):
        trace(payload, chdir, "pass")
        return 0                                   # 快路：没信就什么都不输出，模型侧零字节
    pending = take_pending(chdir)
    if not pending and is_reply_call(payload):
        trace(payload, chdir, "pass-reply")
        return 0
    if pending:
        head = ("（这不是报错，也不是可疑注入：这是派你干活的主线通过对讲频道捎来的口信。"
                "你刚才那次调用被拦下了、没有执行 —— 拦它只是为了把口信塞给你。"
                "读完照口信办；如果口信没让你改做法，就把刚才那次调用原样再发一次接着干。）")
        if any(r.get("mode") == "stop" for r in pending):
            head = ("（停手。主线要你先回话再动手：在你用下面那行 ask 回话之前，"
                    "每次调用都会被拦下。）")
            state_file.write_text(json.dumps({"hold": True}), encoding="utf-8")
        reason = f"{head}\n\n{note_body(pending, ch)}\n\n{reply}"
    else:
        reason = f"主线让你先回话再继续动手。\n\n{reply}"
    trace(payload, chdir, "deny")
    sys.stdout.write(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": reason,
    }}, ensure_ascii=False))
    return 0


# ——————————————————————————— 工人面（工人 → 主线）———————————————————————————

def worker_post(channel: str, text: str, kind: str, wait: float) -> int:
    chdir = resolve(channel)
    outbox = chdir / "outbox.jsonl"
    entry = {"id": uuid.uuid4().hex[:8], "at": now(), "kind": kind, "text": text}
    append_jsonl(outbox, entry)
    (chdir / "state.json").write_text(json.dumps({"hold": False}), encoding="utf-8")  # 回话即解挂
    if kind == "say" or wait <= 0:
        print(f"[kimi_msg] 已送达主线（{entry['id']}）。不等答复，继续干。")
        return 0
    print(f"[kimi_msg] 已送达主线（{entry['id']}），等答复中，最多 {int(wait)} 秒…", flush=True)
    deadline = time.time() + wait
    while True:
        got = answers_of(read_jsonl(outbox)).get(entry["id"])
        if got:
            print(f"\n【主线答复 {got.get('answered_at')}】\n{got['answer']}")
            return 0
        if time.time() >= deadline:
            break
        time.sleep(POLL_EVERY)
    # 记下这条问题不再有人等：否则 wait 会一直报「工人原地等答复」，收工后还挡住退出码 20。
    append_jsonl(outbox, {"kind": "expired", "of": entry["id"], "at": now()})
    print("\n[kimi_msg] 主线没回（超时）。按你自己的判断继续干，"
          "并在最终报告里单独写一行：问过什么、没等到答复、你先按什么假设做了。")
    return 0


# ——————————————————————————— 主线面 ———————————————————————————

def unread(outbox: Path) -> tuple[list, list]:
    """(待答的提问, 没列过的报告) —— `poll` 与 `wait` 共用这一个判据。

    报告列过一次就算收到，已读标记走追加一行 `seen`；提问不标已读，答了或过期才算完。
    """
    rows = read_jsonl(outbox)
    seen = {r.get("of") for r in rows if r.get("kind") == "seen"}
    answered = answers_of(rows)
    expired = {r.get("of") for r in rows if r.get("kind") == "expired"}
    asks = [r for r in rows
            if r.get("kind") == "ask" and r.get("id") not in answered and r.get("id") not in expired]
    says = [r for r in rows if r.get("kind") == "say" and r.get("id") not in seen]
    return asks, says


def cmd_ls() -> int:
    reg = registry()
    if not reg:
        print("（还没有登记过的频道）")
        return 0
    for ch, info in reg.items():
        d = Path(info["dir"])
        pend_in = len(pending_inbox(read_jsonl(d / "inbox.jsonl")))
        pend_out = len(unread(d / "outbox.jsonl")[0])
        alive = "活" if (d / "live").exists() else "关"
        print(f"{ch}  [{alive}]  未读口信 {pend_in}  待答提问 {pend_out}  {info['dir']}")
    return 0


def cmd_send(channel: str, text: str, stop: bool) -> int:
    chdir = resolve(channel)
    append_jsonl(chdir / "inbox.jsonl",
                 {"id": uuid.uuid4().hex[:8], "at": now(), "text": text,
                  "mode": "stop" if stop else "note"})
    print(f"已投递到 {channel}：工人下一次调工具前（或准备收工时）会看到"
          + ("（并会被挂住直到回话）" if stop else ""))
    return 0


def cmd_poll(channel: str | None) -> int:
    chans = [channel] if channel else list(registry().keys())
    anything = False
    for ch in chans:
        try:
            d = resolve(ch)
        except SystemExit:
            continue
        outbox = d / "outbox.jsonl"
        asks, says = unread(outbox)
        for r in asks:
            anything = True
            print(f"[{ch}] {r['at']} 提问 {r['id']}（等答复中）：\n  {r['text']}\n")
        for r in says:
            anything = True
            print(f"[{ch}] {r['at']} 报告：\n  {r['text']}\n")
            append_jsonl(outbox, {"kind": "seen", "of": r["id"], "at": now()})
    if not anything:
        print("（没有待答提问 / 新报告）")
    return 0


def cmd_wait(channel: str, timeout: float) -> int:
    """阻塞到工人出声（提问 / 报告）或收工，把内容原样打出来后退出 —— 「工人 → 主线」的叫醒器。"""
    grace = time.time() + min(30.0, timeout)   # 频道可能还没建（派单脚本刚起），给一段宽限
    while True:
        try:
            chdir = resolve(channel)
            break
        except SystemExit:
            if time.time() >= grace:
                print(f"[wait] 频道 {channel} 始终没出现（名字打错？派单没起来？）")
                return WAIT_NOCHAN
            time.sleep(WAIT_POLL_EVERY)
    outbox = chdir / "outbox.jsonl"
    tool = tool_hint()
    deadline = time.time() + timeout
    print(f"[wait] 守着频道 {channel}，最多 {int(timeout)} 秒；工人一出声就退出并打在这里。", flush=True)
    while True:
        # 先看有没有话，再看人还在不在：工人收工前最后一秒留的话不能漏。
        asks, says = unread(outbox)
        if asks or says:
            for r in says:
                append_jsonl(outbox, {"kind": "seen", "of": r["id"], "at": now()})
            print()
            for r in asks:
                print(f"[{channel}] {r['at']} 提问 {r['id']}（工人原地等答复中）：\n  {r['text']}\n")
            for r in says:
                print(f"[{channel}] {r['at']} 报告：\n  {r['text']}\n")
            if asks:
                print(f"→ 答它（工人 {int(POLL_EVERY)} 秒内收到）："
                      f"{PYX} {tool} answer {channel} \"你的答复\"")
                print(f"→ 答完再挂一个：{PYX} {tool} wait {channel}")
                return WAIT_ASK
            print(f"→ 接着守：{PYX} {tool} wait {channel}")
            return WAIT_SAY
        if not (chdir / "live").exists():
            print(f"\n[wait] 工人已收工（频道 {channel} 已关），没有留话。")
            return WAIT_GONE
        if time.time() >= deadline:
            print(f"\n[wait] {int(timeout)} 秒内没动静，工人还在干。要接着守就再挂一个："
                  f"{PYX} {tool} wait {channel}")
            return WAIT_TIMEOUT
        time.sleep(WAIT_POLL_EVERY)


def cmd_answer(channel: str, text: str, msg_id: str | None) -> int:
    outbox = resolve(channel) / "outbox.jsonl"
    asks, _ = unread(outbox)
    if msg_id is not None:
        asks = [r for r in asks if r.get("id") == msg_id]
    if not asks:
        print("没有待答的提问")
        return 1
    target = asks[-1]                              # 不指定就答最后一条待答的
    append_jsonl(outbox, {"kind": "answer", "of": target["id"], "answer": text, "answered_at": now()})
    print(f"已答复 {target['id']}：工人最多 {int(POLL_EVERY)} 秒内收到")
    return 0


def content_text(content) -> str:
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        return "\n".join(str(p.get("text", "")) for p in content
                         if isinstance(p, dict) and p.get("type") == "text").strip()
    return ""


def cmd_digest(stream: Path, last: Path) -> int:
    """kimi_task.sh 收工时调用：从 stream-json 里取最终答复写进 last，stdout 打 session id。

    最终答复 = 最后一条带正文、不带 tool_calls 的 assistant 消息（Stop 钩子逼它接着干时，
    前面那条「最终答复」会被后来的覆盖）。
    """
    sid, final, fallback = "", "", ""
    for row in read_jsonl(stream):
        if row.get("type") == "session.resume_hint":
            sid = row.get("session_id") or sid
        if row.get("role") == "assistant":
            text = content_text(row.get("content"))
            if text:
                fallback = text
                if not row.get("tool_calls"):
                    final = text
    if final or fallback:
        last.write_text((final or fallback) + "\n", encoding="utf-8")
    print(sid)
    return 0


def main(argv: list[str]) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")   # Windows 默认 GBK，中文口信会乱码
        except Exception:
            pass
    argv = list(argv)
    if "--root" in argv:                       # 显式指到哪个项目（kimi_task.sh 派单时会传）
        i = argv.index("--root")
        if i + 1 < len(argv):
            set_root(argv[i + 1])
        del argv[i:i + 2]
    if "--hook" in argv:
        try:
            return hook()
        except Exception:
            return 0
    if not argv:
        print(__doc__)
        return 2
    verb, rest = argv[0], argv[1:]

    def opt(name: str, default=None):
        if name in rest:
            i = rest.index(name)
            val = rest[i + 1] if i + 1 < len(rest) else default
            del rest[i:i + 2]
            return val
        return default

    if verb == "ls":
        return cmd_ls()
    if verb == "register":                      # kimi_task.sh 派单时调用
        print(register(rest[0], Path(rest[1])))
        return 0
    if verb == "install":
        print(install())
        return 0
    if verb == "uninstall":
        print(uninstall())
        return 0
    if verb == "digest":                        # kimi_task.sh 收工时调用
        return cmd_digest(Path(rest[0]), Path(rest[1]))
    if verb == "close":
        (resolve(rest[0]) / "live").unlink(missing_ok=True)
        return 0
    if verb in {"ask", "say"}:
        wait = float(opt("--wait", DEFAULT_WAIT) or DEFAULT_WAIT)
        if len(rest) < 2:
            print("用法：kimi_msg.py ask <频道> \"问题\" [--wait 秒]")
            return 2
        return worker_post(rest[0], " ".join(rest[1:]), verb, wait)
    if verb == "send":
        stop = "--stop" in rest
        if stop:
            rest.remove("--stop")
        if len(rest) < 2:
            print("用法：kimi_msg.py send <频道> [--stop] \"口信\"")
            return 2
        return cmd_send(rest[0], " ".join(rest[1:]), stop)
    if verb == "poll":
        return cmd_poll(rest[0] if rest else None)
    if verb == "wait":
        secs = float(opt("--timeout", WAIT_DEFAULT_TIMEOUT) or WAIT_DEFAULT_TIMEOUT)
        if not rest:
            print("用法：kimi_msg.py wait <频道> [--timeout 秒]")
            return 2
        return cmd_wait(rest[0], secs)
    if verb == "answer":
        msg_id = opt("--id")
        if len(rest) < 2:
            print("用法：kimi_msg.py answer <频道> [--id <id>] \"答复\"")
            return 2
        return cmd_answer(rest[0], " ".join(rest[1:]), msg_id)
    print(f"未知子命令 {verb}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
