# claude2kimi

[English](README.md) | **简体中文**

让 [Claude Code](https://claude.com/claude-code) 把活派给本机的 [Kimi Code CLI](https://moonshotai.github.io/kimi-code/)
（`kimi -p`，默认模型 K3.1 Pro），并且在 Kimi 干活的过程中还能继续和它对话。

它是 [claude2codex](https://github.com/Napoleoncqf/claude2codex) 的 Kimi 版：派单方还是 Claude，工人换成 Kimi。
装好后是一个 Claude Code skill，文件夹名叫 `dispatch-kimi`，由三个脚本组成，不需要常驻服务。

推荐分工：Claude 规划和执行，Kimi 只做复审。用 `--ro` 派审，返工后用 `--resume` 在同一会话里复审，
它记得上一轮提了什么，可以逐条核对。审查 brief 模板和 Claude 回应发现的规矩写在 [SKILL.md](SKILL.md)。
脚本本身不限用途，去掉 `--ro` 也能派执行单。

## 它能做什么

| 你想… | 命令 | 效果 |
|---|---|---|
| 派一单复审 | `kimi_task.sh --ro -f review.md` | 起一个无头 `kimi -p`（explore agent，没有 Write/Edit），跑完 stdout 打审查报告 |
| 返工后复审 | `kimi_task.sh --resume <session-id> -p "…"` | 接着原会话审，逐条核对上一轮的发现 |
| 派一单活 | `kimi_task.sh -f brief.md` | 起一个无头 `kimi -p`，跑完 stdout 打最终答复 |
| 运行中给工人捎一句话 | `kimi_msg.py send` | 工人下一次调工具前读到（那次调用被拦下，读完重发） |
| 让工人先停下回话 | `kimi_msg.py send --stop` | 工人回话前，别的工具调用都会被拦下 |
| 让工人反过来问你 | 工人运行 `ask` | 它原地等（最多约 5 分钟），你 `answer` 后它在同一次运行里接着干 |
| 等工人出声或收工 | `kimi_msg.py wait` | 阻塞到有提问、有报告或收工，退出码说明是哪种 |
| 追问 / 返工 | `kimi_task.sh --resume <session-id> -p "…"` | 接着原会话干，沿用原会话的模型 |

## 原理

- **捎话**：Kimi Code 的 `PreToolUse` 钩子在工人每次调工具前触发。钩子读信箱，有信就拦下这次调用（deny），
  把口信当拦截理由塞回去，拦截理由会作为这次调用的结果交给模型。没信时钩子什么都不输出。
  工人准备收工、不再调工具时，`Stop` 钩子兜底：还有没送到的口信就拦下收工（退出码 2），口信作为新消息交给它。
- **信箱**：每单一条频道，就是 `<工位>/.harness-tmp/kimi-msg/<频道>/` 下的 `inbox.jsonl`（你 → 工人）
  和 `outbox.jsonl`（工人 → 你），只追加不改写。
- **叫醒主线**：`wait` 挂在 Claude Code 的后台 Bash 上，工人一出声就退出，Claude 收到后台任务通知；
  Claude 侧的收件钩子 `kimi_inbox_hook.py` 在 Claude 每次调工具前把未读提问 / 报告塞进上下文。

和 claude2codex 的几处不同，都来自 Kimi Code 本身：

- **钩子是全局的。** Kimi 只认用户级 `~/.kimi-code/config.toml` 里的 `[[hooks]]`，没有项目级配置。
  所以派单时会往这个文件里加两条钩子（PreToolUse、Stop），命令开头是
  `[ -z "$KIMI_MSG_CH" ] || exec python3 …`：只有派单脚本起的工人进程带 `KIMI_MSG_CH`，
  你自己开的 kimi 会话里它是空的，钩子当场退出，连 Python 都不启动。
- **只能拦截送达。** 实测 PreToolUse 不拦截时，钩子输出的纯文本、`message`、`additionalContext`
  都进不了模型上下文，所以和 claude2codex 一样走「拦下再重发」。
- **口信带频道号。** 实测 K3.1 Pro Highspeed（low 档）在没有交代的情况下会把拦截理由当成可疑注入、不照办。
  所以派单开头会告诉工人：开头写着「主线口信 · 频道 <本单频道>」的拦截是主线的口信。
- **没有系统沙箱。** `kimi -p` 里工具调用全部自动放行，Bash 不在沙箱里（实测能联网）。
  `--ro` 换成 Kimi 内置的 explore agent，拿掉了 Write / Edit，但 Bash 还在，只是软约束。
- **ask 最多等约 5 分钟。** Kimi 的 Bash 前台命令上限是 5 分钟，`ask` 默认等 280 秒。
- **少了几样东西。** 没有 `--net` / `--full`（没沙箱可开关）、没有 `-i`（`kimi -p` 不收图片参数，
  在 brief 里写图片路径即可）、没有容量重试和看门狗（Kimi 自己会重试 429 / 过载，headless 收尾会强制退出）。

## 依赖

| 依赖 | 实测版本 | 检查 |
|---|---|---|
| Kimi Code CLI | 2.3.1 | `kimi --version`；登录用 `kimi login` |
| Python | 3.15（脚本需 ≥ 3.9；装钩子时的 TOML 校验和跑测试要 ≥ 3.11） | `python3 --version` |
| bash、git | macOS 自带 | |
| Claude Code | 2.1.295 | `claude --version` |

只在 macOS 上实测过。项目必须是 git 仓库（脚本从当前目录往上找 `.git` 定项目根）。

## 安装

```bash
git clone https://github.com/Napoleoncqf/claude2kimi.git && cd claude2kimi

# 1. 拷进 Claude Code 的 skills 目录
mkdir -p ~/.claude/skills/dispatch-kimi
cp SKILL.md ~/.claude/skills/dispatch-kimi/
cp -R scripts ~/.claude/skills/dispatch-kimi/

# 2. 装 Kimi 侧钩子（第一次派单也会自动装；已装好就不碰文件）
python3 ~/.claude/skills/dispatch-kimi/scripts/kimi_msg.py install
kimi doctor config
```

第 2 步会先把原文件备份成 `~/.kimi-code/config.toml.pre-kimi-msg`，保持原来的 600 权限（里面有 token）。

```jsonc
// 3.（可选）~/.claude/settings.json 的 hooks.PreToolUse 里加一组，让埋头干活的 Claude 也能即时看到工人提问
{
  "matcher": "*",
  "hooks": [
    { "type": "command",
      "command": "python3 \"$HOME/.claude/skills/dispatch-kimi/scripts/kimi_inbox_hook.py\"" }
  ]
}
```

改完重启 Claude Code。不装第 3 步也能用，只是要靠 `wait` 叫醒。

## 用法

```bash
S=~/.claude/skills/dispatch-kimi/scripts
bash $S/kimi_task.sh -f .harness-tmp/brief-x.md --tag x        # 主用法，在 Claude Code 里用后台 Bash 跑
bash $S/kimi_task.sh -p "数一下 src/ 里有几个 .py" --ro        # 只读小活
bash $S/kimi_task.sh -m kimi-code/k3.1-pro-highspeed -e high -f brief.md
bash $S/kimi_task.sh --resume session_xxx -p "测试挂了两条，修。"
```

派单后 stderr 先打两行 `CHANNEL=` / `WAKEUP=`，收工时打
`SESSION=<id> LOG=<stderr 日志> JSONL=<完整对话> EXIT=<退出码>`，stdout 是最终答复。
日志都在 `<项目根>/.harness-tmp/kimi/`。

对讲：

```bash
M=~/.claude/skills/dispatch-kimi/scripts/kimi_msg.py
python3 $M send <频道> "改用方案 B"        # 捎话
python3 $M send <频道> --stop "先停，说说现状"
python3 $M wait <频道>                    # 退出码 0 提问 / 10 报告 / 20 收工 / 30 超时 / 40 没频道
python3 $M answer <频道> "用 A"
python3 $M poll [<频道>]
python3 $M ls
```

| 环境变量 | 作用 |
|---|---|
| `KIMI_TASK_BIN` | 指定 kimi 可执行文件（默认 PATH 里的 `kimi`，再退到 `~/.kimi-code/bin/kimi`） |
| `KIMI_TASK_MODEL` | 新会话的默认模型（默认 `kimi-code/k3.1-pro`） |
| `KIMI_CODE_HOME` | Kimi 自己的数据目录变量；设了的话钩子装到 `$KIMI_CODE_HOME/config.toml` |

`-e` 通过 Kimi 的 `KIMI_MODEL_THINKING_EFFORT` 环境变量传给这一单，不改你的配置。

## 排障

| 现象 | 处理 |
|---|---|
| stderr 有 `WARN: 对讲钩子没装进 Kimi 配置` | 跑 `python3 $M install` 看报错；多半是 `config.toml` 本身有语法错，先 `kimi doctor config` |
| 捎话一直没送到 | 工人在跑一条长命令时送不到，下一次调工具才送；看 `<项目根>/.harness-tmp/kimi-msg/hook-trace.log`，每次钩子触发都记一行 |
| 工人收到口信但没照办 | 看 `JSONL=` 那个文件里被拦的那次调用；口信要写得像指令，别写成闲聊 |
| 工人 `ask` 后很快就自己往下干了 | 它的 Bash 调用超时了（默认 60 秒，没按说明设 300000）。命令会被 Kimi 转到后台继续等，答复到了会通知它 |
| `REFUSED: 续会话必须给显式 session id` | 故意的：`--continue` / 不带 id 的 `--session` 会接进你自己的交互会话 |

## 卸载

```bash
python3 ~/.claude/skills/dispatch-kimi/scripts/kimi_msg.py uninstall   # 只删自己那两条 [[hooks]]
rm -rf ~/.claude/skills/dispatch-kimi
# 再删掉 ~/.claude/settings.json 里第 3 步加的那组钩子；各项目的 .harness-tmp/kimi* 可以直接删
```

## 测试

```bash
python3 -m unittest discover -s tests
```

不需要装任何包，也不会调用真的 kimi（派单测试用一个假 kimi 吐 stream-json）。覆盖：派单参数（默认模型、
续会话不换模型、拒绝 `--continue`、`--ro`、`-e`）、并行派单分频道、收工前的留言、钩子挂住与回话放行、
Stop 兜底送口信、全局钩子的 shell 门控、`config.toml` 安装的幂等与权限、收件钩子。

## License

[MIT](LICENSE) © Napoleoncqf
