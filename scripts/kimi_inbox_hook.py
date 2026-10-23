"""Claude Code PreToolUse 钩子：把 Kimi 工人的提问 / 报告即时塞给正在干活的主线。

为什么要有它：`kimi_msg.py` 的「工人 → 主线」方向只到发件箱为止 —— 工人 `ask` 一句就
原地阻塞等答复，而主线如果正埋头干别的活，没有任何东西告诉它信箱里有话。

两层保险，这是第二层：
  第一层 = `kimi_msg.py wait <频道>`（主线挂后台，进程一退主线就收到通知；主线闲着也叫得醒）；
  第二层 = 本钩子（主线每次调工具前塞一次；主线正忙着干活时也能即时看到）。

**只塞不拦**：走 `hookSpecificOutput.additionalContext`（非 deny），主线那次工具调用照跑不误。
没未读时一个字节都不输出。

fail-open 是铁律：找不到目录 / JSON 坏了 / 任何异常一律静默退出 0、不输出一个字 ——
本钩子挂在**用户级** `~/.claude/settings.json` 的 hooks.PreToolUse（matcher `*`），
也就是每个项目的每次工具调用前都会跑它。

不绑任何项目：项目根先认钩子 stdin JSON 里的 `cwd`，其次从当前目录往上找第一个带 `.git`
的目录，都找不到就直接退出 —— 没有 `.harness-tmp/kimi-msg/` 的项目一个字节都不写。

已读账本住在项目根 `.harness-tmp/kimi-msg/hook-delivered.json`，不往频道目录里写：
频道目录属于正在跑的工人，本钩子对它只读。账本按会话分开记（session_id + agent_id），
免得子代理的工具调用把口信「领走」、真正的主线反而看不到。
"""
from __future__ import annotations

import json
import os
import sys
import time

PYX = sys.executable.replace("\\", "/") or "python3"
TOOL = os.path.join(os.path.dirname(os.path.abspath(__file__)), "kimi_msg.py").replace("\\", "/")

STALE_SEC = 24 * 3600     # live 还在、发件箱却 24h 没动 = 崩掉没关的死频道，别翻它的旧账
ASK_RENAG = 90.0          # 没答的提问隔这么久再塞一次（工人正阻塞着，值得催）
ASK_RENAG_UNTIL = 300.0   # 首次送达 5 分钟后就不催了：工人的 ask 默认只等 280 秒
MAX_ITEMS = 6             # 一次最多塞几条，免得刷屏
MAX_TEXT = 600            # 单条正文截断长度
LEDGER_KEEP_IDS = 200     # 每频道账本最多留几条 id
LEDGER_KEEP_SEC = 7 * 24 * 3600


def find_root(start):
    """项目根 = 从 start 往上第一个带 .git 的目录；找不到返回 None（= 这次不归本钩子管）。"""
    try:
        cur = os.path.abspath(start)
    except Exception:
        return None
    while True:
        if os.path.exists(os.path.join(cur, ".git")):
            return cur
        parent = os.path.dirname(cur)
        if parent == cur:
            return None
        cur = parent


def load_json(path: str, fallback):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return fallback


def read_rows(path: str) -> list:
    rows = []
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    try:
                        rows.append(json.loads(line))
                    except ValueError:
                        pass
    except Exception:
        return []
    return rows


def clip(text: str) -> str:
    text = str(text).replace("\r", "")
    return text if len(text) <= MAX_TEXT else text[:MAX_TEXT] + " …（截断）"


def session_key(payload: dict) -> str:
    # agent_id 只在子代理的工具调用里有：分开记账，子代理不会把主线的口信领走。
    return str(payload.get("session_id") or "unknown") + "/" + str(payload.get("agent_id") or "")


def scan(registry: str, ledger_ch: dict, now_ts: float) -> tuple:
    """扫所有活频道，返回 (要塞给主线的条目, 账本有没有变)。只读频道目录。"""
    reg = load_json(registry, None)
    if not isinstance(reg, dict) or not reg:
        return [], False
    hits = []
    dirty = False
    for ch, info in reg.items():
        try:
            chdir = (info or {}).get("dir")
            if not chdir or not os.path.exists(os.path.join(chdir, "live")):
                continue                      # 频道已关 = 工人收工，不翻旧账
            outbox = os.path.join(chdir, "outbox.jsonl")
            mtime = os.path.getmtime(outbox)
            if now_ts - mtime > STALE_SEC:
                continue
            book = ledger_ch.setdefault(ch, {})
            ids = book.setdefault("ids", {})
            # 快路：发件箱没动过、也没有到点该再催的提问 ⇒ 连解析都省了
            if book.get("mt") == mtime and now_ts < book.get("next", 0.0):
                continue
            rows = read_rows(outbox)
            seen = {r.get("of") for r in rows if r.get("kind") == "seen"}
            # expired = 工人等到超时已自己往下干，别再催主线
            done = {r.get("of") for r in rows if r.get("kind") in ("answer", "expired")}
            next_due = 0.0
            for r in rows:
                kind, rid = r.get("kind"), r.get("id")
                if not rid:
                    continue
                if kind == "ask" and rid not in done:
                    stamp = ids.get(rid)
                    if stamp is None:
                        ids[rid] = [now_ts, now_ts]
                        dirty = True
                        hits.append((ch, r, True))
                    elif now_ts - stamp[1] >= ASK_RENAG and now_ts - stamp[0] <= ASK_RENAG_UNTIL:
                        stamp[1] = now_ts
                        dirty = True
                        hits.append((ch, r, True))
                    if now_ts - ids[rid][0] <= ASK_RENAG_UNTIL:
                        due = ids[rid][1] + ASK_RENAG
                        next_due = due if next_due == 0.0 else min(next_due, due)
                elif kind == "say" and rid not in seen and rid not in ids:
                    ids[rid] = [now_ts, now_ts]
                    dirty = True
                    hits.append((ch, r, False))
            if book.get("mt") != mtime or book.get("next", 0.0) != next_due:
                book["mt"] = mtime
                book["next"] = next_due
                dirty = True
        except Exception:
            continue                          # 单个频道出问题不连累别的
    return hits, dirty


def render(hits: list) -> str:
    lines = ["【Kimi 工人来话 —— 不是报错，你这次工具调用照常执行；只有主线会话可以 answer / send，"
             "子代理（Agent 工具派出的工人）看到这条只当背景信息，绝不回它】"]
    asked = []
    for ch, row, is_ask in hits[:MAX_ITEMS]:
        if is_ask:
            asked.append(ch)
            lines.append("[%s] %s 提问 %s（工人**原地阻塞等答复**，不答它就干等到超时）：\n  %s"
                         % (ch, row.get("at", "?"), row.get("id", "?"), clip(row.get("text", ""))))
        else:
            lines.append("[%s] %s 报告：\n  %s" % (ch, row.get("at", "?"), clip(row.get("text", ""))))
    if len(hits) > MAX_ITEMS:
        lines.append("（还有 %d 条，跑 %s %s poll 看全）" % (len(hits) - MAX_ITEMS, PYX, TOOL))
    for ch in dict.fromkeys(asked):
        lines.append("→ 答它（工人 2 秒内收到）：%s %s answer %s \"你的答复\"" % (PYX, TOOL, ch))
    return "\n\n".join(lines)


def prune(ledger: dict, now_ts: float) -> None:
    for key in list(ledger.keys()):
        sess = ledger.get(key) or {}
        if now_ts - float(sess.get("ts") or 0) > LEDGER_KEEP_SEC:
            del ledger[key]
            continue
        for book in (sess.get("ch") or {}).values():
            ids = book.get("ids") or {}
            if len(ids) > LEDGER_KEEP_IDS:
                book["ids"] = dict(sorted(ids.items(), key=lambda kv: kv[1][0])[-LEDGER_KEEP_IDS:])


def build() -> dict | None:
    raw = sys.stdin.buffer.read().decode("utf-8-sig") if not sys.stdin.isatty() else ""
    payload = json.loads(raw or "{}")
    if not isinstance(payload, dict):
        return None
    if os.environ.get("CLAUDE_MSG_CH"):
        return None                                 # 这是 codex2claude 派出去的 Claude 工人，不是主线
    root = find_root(payload.get("cwd") or os.getcwd()) or find_root(os.getcwd())
    if not root:
        return None
    msgroot = os.path.join(root, ".harness-tmp", "kimi-msg")
    if not os.path.isdir(msgroot):
        return None                                 # 这个项目没派过 kimi，零成本退出
    ledger_path = os.path.join(msgroot, "hook-delivered.json")
    now_ts = time.time()
    ledger = load_json(ledger_path, {})
    if not isinstance(ledger, dict):
        ledger = {}
    sess = ledger.setdefault(session_key(payload), {})
    sess["ts"] = now_ts
    hits, dirty = scan(os.path.join(msgroot, "registry.json"), sess.setdefault("ch", {}), now_ts)
    if dirty or hits:
        prune(ledger, now_ts)
        tmp = ledger_path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(ledger, fh, ensure_ascii=False)
            os.replace(tmp, ledger_path)
        except Exception:
            pass
    if not hits:
        return None
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "additionalContext": render(hits)}}


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")   # Windows 默认 GBK，中文口信会乱码
    except Exception:
        pass
    try:
        out = build()
        if out:
            sys.stdout.write(json.dumps(out, ensure_ascii=False))
    except Exception:
        pass                                        # fail-open：绝不挡主线的工具调用
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
