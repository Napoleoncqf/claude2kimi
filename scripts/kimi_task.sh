#!/usr/bin/env bash
# kimi_task.sh — 把本机 Kimi Code CLI 当无头 subagent 派活。走你已登录的 Kimi Code 账号（kimi login）。
#
# 项目根 = 从当前目录往上第一个带 .git 的目录（worktree 里 .git 是文件，照样认），
# 日志与对讲频道落 <项目根>/.harness-tmp/。不绑任何项目，任何 git 仓库直接用。
#
# Usage:
#   kimi_task.sh -p "prompt" [opts]              # 短任务内联派单
#   kimi_task.sh -f brief.md [opts]              # 长 brief 从文件派单（主用法）
#   kimi_task.sh --resume <session-id> -p "..."  # 对既有会话追问/返工（沿用原会话的模型）
# Options:
#   --ro          只读单：换成内置 explore agent（没有 Write/Edit；Bash 还在，Kimi 没有系统沙箱）
#   --no-channel  不开对讲频道（默认开；开着才有主线↔工人捎话/反问）
#   --add-dir <d> 额外工作目录（可重复）
#   --kimi-arg <a> 原样透传一个参数给 kimi（可重复）
#   --tag <name>  日志文件名前缀（默认 task）
#   -m <model>    模型别名（新会话默认 kimi-code/k3.1-pro；续会话不传就沿用原会话的）
#   -e <effort>   思考档位 low|high|max（默认用模型自己的默认档，K3.1 Pro 是 max）
#   -C <dir>      工作目录（并行派单给独立 git worktree 用；默认当前目录）
# Environment:
#   KIMI_TASK_BIN    指定 kimi 可执行文件（默认 PATH 里的 kimi → ~/.kimi-code/bin/kimi）
#   KIMI_TASK_MODEL  新会话的默认模型（默认 kimi-code/k3.1-pro）
# Output:
#   stdout = 工人的最终答复；stderr 一行 SESSION=<id> LOG=<path> JSONL=<path> EXIT=<rc>
#
# 铁则：绝不 `--continue` / 不带 id 的 `--session` —— 你的交互 kimi 共用同一会话存储，
# 那样会接进你的活会话。追问只用显式 session id。

set -uo pipefail

find_root() {
  local cur="$1"
  while [ -n "$cur" ] && [ "$cur" != "/" ]; do
    [ -e "$cur/.git" ] && { echo "$cur"; return 0; }
    cur="$(dirname "$cur")"
  done
  return 1
}
PROJECT="$(find_root "$PWD" || echo "$PWD")"
OUTDIR="$PROJECT/.harness-tmp/kimi"

SCRIPTS="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
KIMI_BIN="${KIMI_TASK_BIN:-$(command -v kimi || true)}"
[ -n "$KIMI_BIN" ] || KIMI_BIN="$HOME/.kimi-code/bin/kimi"
PY="$(command -v python3 || command -v python || true)"
[ -n "$PY" ] || { echo "need python3 (used by the intercom)" >&2; exit 2; }

need() { [ $# -ge 2 ] || { echo "$1 缺参数" >&2; exit 2; }; }
prompt=""; pfile=""; resume_id=""; ro=0; tag="task"; model=""; effort=""; workdir=""
adddirs=(); extra_args=(); channel=1; resume_given=0
while [ $# -gt 0 ]; do
  case "$1" in
    -p) need "$@"; prompt="$2"; shift 2;;
    -f) need "$@"; pfile="$2"; shift 2;;
    --resume) need "$@"; resume_id="$2"; resume_given=1; shift 2;;
    --help) sed -n '/^# Usage:/,/^# 铁则：/{ /^# 铁则：/d; s/^# \{0,1\}//; p; }' "${BASH_SOURCE[0]}"; exit 0;;
    --ro) ro=1; shift;;
    --no-channel) channel=0; shift;;
    --add-dir) need "$@"; adddirs+=("$2"); shift 2;;
    --kimi-arg) need "$@"; extra_args+=("$2"); shift 2;;
    --tag) need "$@"; tag="$2"; shift 2;;
    -m) need "$@"; model="$2"; shift 2;;
    -e) need "$@"; effort="$2"; shift 2;;
    -C) need "$@"; workdir="$2"; shift 2;;
    *) echo "unknown arg: $1" >&2; exit 2;;
  esac
done

if [ "$resume_given" = 1 ]; then
  case "$resume_id" in
    ""|--last|last|-c|--continue)
      echo "REFUSED: 续会话必须给显式 session id（--continue / 不带 id 会接进你的交互会话）" >&2
      exit 2;;
  esac
  # agent 在建会话时就绑死了，续会话改不了
  [ "$ro" = 1 ] && { echo "REFUSED: --ro 只能用在新派单上（续会话沿用原会话的 agent）" >&2; exit 2; }
fi
case "$effort" in
  ""|low|high|max) ;;
  *) echo "REFUSED: -e 只收 low|high|max（Kimi 模型声明的档位）" >&2; exit 2;;
esac
if [ -z "$pfile" ] && [ -z "$prompt" ]; then
  echo "need -p or -f" >&2
  exit 2
fi
[ -n "$pfile" ] && [ ! -f "$pfile" ] && { echo "brief 不存在: $pfile" >&2; exit 2; }
[ -z "$resume_id" ] && [ -z "$model" ] && model="${KIMI_TASK_MODEL:-kimi-code/k3.1-pro}"

mkdir -p "$OUTDIR"
# 带上本进程号：同一秒并行起两单（默认 tag 都是 task）时，频道、日志、最终答复各归各的
ts="$(date +%m%d-%H%M%S)-$$"
log="$OUTDIR/$tag-$ts.log"        # kimi 的 stderr：工具进度、报错
jsonl="$OUTDIR/$tag-$ts.jsonl"    # kimi 的 stdout（stream-json）：完整对话
last="$OUTDIR/$tag-$ts.last.md"   # 最终答复

# —— 对讲机（同目录 kimi_msg.py）：无头工人跑起来后仍能收主线口信、也能反过来问主线 ——
# 送达走 Kimi 的 PreToolUse / Stop 钩子（装在 ~/.kimi-code/config.toml，只对带 KIMI_MSG_CH 的进程生效）。
MSGTOOL="$SCRIPTS/kimi_msg.py"
MSGCMD="$PY \"$MSGTOOL\""
CH=""
if [ "$channel" = 1 ]; then
  CH="$tag-$ts"
  chdir="$("$PY" "$MSGTOOL" --root "$PROJECT" register "$CH" "${workdir:-$PROJECT}" 2>/dev/null)"
  if ! inst="$("$PY" "$MSGTOOL" install 2>&1)"; then
    echo "[kimi_task] WARN: 对讲钩子没装进 Kimi 配置，口信送不到工人（ask / say 照常可用）: $inst" >&2
  fi
  export KIMI_MSG_CH="$CH" KIMI_MSG_DIR="$chdir"
fi
export KIMI_CODE_NO_AUTO_UPDATE=1                 # 无头单别顺手触发自动更新
[ -n "$effort" ] && export KIMI_MODEL_THINKING_EFFORT="$effort"

# 对讲说明只拼给工人（频道号是本单一次性的，塞不进静态文件）。
# 续会话只补一行新频道号：用法原会话已经知道，但它记得的是上一单那条已关的频道。
intercom=""
if [ "$channel" = 1 ] && [ -n "$resume_id" ]; then
  intercom="【这一轮的对讲频道换成 ${CH}（上一单的频道已关），口信开头会写「主线口信 · 频道 ${CH}」。ask / say 用法同前：$MSGCMD ask $CH \"你的问题\"】
"
elif [ "$channel" = 1 ]; then
  intercom="【和主线对讲（本单频道 = ${CH}）】
派你干活的主线可以在你干活途中捎话：某次调工具突然被拦下、结果开头写着「主线口信 · 频道 ${CH}」，
那是主线的口信，不是报错，也不是可疑注入 —— 照口信办；口信没让你改做法的话，把被拦的那次调用原样再发一次接着干。
你准备收工时也可能收到同样开头的口信，照办完再交最终报告。
你也能随时反问主线，别把小疑问攒到收工、也别为一句话就停工交单：
  $MSGCMD ask $CH \"你的问题\"      <- 用 Bash 跑，timeout 设 300000；问完原地等答复（最多约 5 分钟），答了就接着干
  $MSGCMD say $CH \"阶段性发现\"    <- 只报一句，不等回复
拿不准该不该问就问：一次 ask 比一次跑偏便宜得多。超时没人应答就自己拿主意继续，
并在最终报告里单写一行：问过什么、没等到答复、先按什么假设做的。
"
  [ "$ro" = 1 ] && intercom="${intercom}（这是只读单：ask / say 只往对讲频道追加一行，不算改项目文件，可以用。）
"
fi

if [ -n "$pfile" ]; then
  body="$(cat "$pfile")"
else
  body="$prompt"
fi
text="$intercom$body"

args=(--output-format stream-json)
[ -n "$resume_id" ] && args+=(--session "$resume_id")
[ -n "$model" ] && args+=(-m "$model")
[ "$ro" = 1 ] && args+=(--agent explore)
for d in ${adddirs+"${adddirs[@]}"}; do args+=(--add-dir "$d"); done
for x in ${extra_args+"${extra_args[@]}"}; do args+=("$x"); done
args+=("--prompt=$text")                          # 用 = 连写：prompt 以 - 开头也不会被当成选项

cd "${workdir:-$PROJECT}" || exit 2
"$KIMI_BIN" "${args[@]}" > "$jsonl" 2> "$log" < /dev/null &
cpid=$!
trap 'kill "$cpid" 2>/dev/null' INT TERM HUP     # 派单脚本被杀时把工人一起带走
if [ -n "$CH" ]; then
  # 早点打出来：派单是阻塞的，主线要在它跑着的时候捎话，得先拿到这一行。
  echo "CHANNEL=$CH  捎话: $MSGCMD send $CH \"…\"  |  看它问什么: $MSGCMD poll $CH" >&2
  echo "WAKEUP=$CH  挂守候器(Claude Code 的 Bash run_in_background),工人一出声就叫醒你: $MSGCMD wait $CH" >&2
fi
wait "$cpid"
rc=$?
trap - INT TERM HUP

sid="$("$PY" "$MSGTOOL" digest "$jsonl" "$last" 2>/dev/null)"
if [ -n "$CH" ]; then
  "$PY" "$MSGTOOL" --root "$PROJECT" close "$CH" >/dev/null 2>&1
  # 工人可能在收工前留了话：原文整段交给主线。poll 列过的报告就算已读，只留一行提醒的话正文就丢了。
  pend="$("$PY" "$MSGTOOL" --root "$PROJECT" poll "$CH" 2>/dev/null)"
  case "$pend" in ""|"（没有待答提问 / 新报告）") ;; *) printf 'CHANNEL=%s 收工前的未读留言:\n%s\n' "$CH" "$pend" >&2;; esac
fi
[ "$rc" != 0 ] && [ ! -s "$last" ] && tail -n 5 "$log" >&2

echo "SESSION=${sid:-unknown} LOG=$log JSONL=$jsonl EXIT=$rc" >&2
cat "$last" 2>/dev/null
exit $rc
