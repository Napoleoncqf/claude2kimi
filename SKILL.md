---
name: dispatch-kimi
description: 用户说「派 kimi」「让 kimi 做 X」「交给 Kimi」或 "delegate this to kimi" 时用。把本机 Kimi Code CLI（默认 K3.1 Pro）当无头 subagent 派活，并支持运行中双向对讲（捎话 / 反问 / 唤醒）。
---

# 派遣 Kimi 当 subagent

流程 = 写 brief → 派单 → 收尾消息 → 按 session id 追问/返工 → **我审 diff + 跑测试**。
工人的产出是未审草稿，和对待自己 subagent 的报告同一怀疑度。

## 派单

```bash
S=~/.claude/skills/dispatch-kimi/scripts
# 长活写 brief 文件派（主用法；用 Bash run_in_background:true，别前台干等）
bash $S/kimi_task.sh -f .harness-tmp/brief-<name>.md --tag <name>
bash $S/kimi_task.sh --ro -f brief.md --tag review        # 只读（评审用，换 explore agent）
# 短任务内联：-p "..."。stdout = 最终答复；stderr 一行 SESSION=<id> LOG=<path> JSONL=<path> EXIT=<rc>
# 追问/返工（必须显式 session id；模型沿用原会话的）：
bash $S/kimi_task.sh --resume <session-id> -p "测试挂两条：<粘输出>。修。"
```

常用开关：`-m <model>`（默认 `kimi-code/k3.1-pro`；`kimi-code/k3.1-pro-highspeed` 更快）·
`-e low|high|max`（默认用模型自己的档，K3.1 Pro 是 max）· `--ro` 只读 · `--add-dir <d>` 多一个工作目录 ·
`-C <dir>` 指到独立 git worktree 并行派单 · `--no-channel` 不开对讲。完整列表：`bash $S/kimi_task.sh --help`。

**Kimi 没有系统沙箱**：`kimi -p` 里工具调用全部自动放行，Bash 不在沙箱里（实测能联网）。
`--ro` 只是拿掉 Write/Edit，Bash 还在。所以只派你敢让它在本机直接动手的活，高风险的放独立 worktree。
要附图就在 brief 里写图片路径，工人自己用 ReadMediaFile 读。

## 对讲（派单自动开频道）

无头工人一开跑就没有输入口，所以每次派单自动开一条频道，stderr 打 `CHANNEL=<频道>` 和 `WAKEUP=<频道>`。

```bash
M="$HOME/.claude/skills/dispatch-kimi/scripts/kimi_msg.py"   # 调用写 python3 "$M"：bash 与 zsh 都认
python3 "$M" send <频道> "改主意了：数到 4 就停"   # 捎话：工人下一次调工具前送到（加 --stop = 回话前把它挂住）
python3 "$M" poll <频道>                           # 看工人问了什么 / 报了什么
python3 "$M" answer <频道> "答复"                  # 答它：工人 2 秒内拿到，在同一个回合里接着干
python3 "$M" wait <频道>                           # 守候器：挂 Bash run_in_background，工人一出声就叫醒我
```

派完单后**顺手挂一个 `wait`**；它退出时看退出码：0 = 工人在等答复（赶紧 answer，它最多等约 5 分钟）/
10 = 只有报告 / 20 = 收工 / 30 = 超时 / 40 = 没这频道。`wait` 要**裸跑，别套管道**（套了就看不到真退出码）。
答完再挂一个。

工人准备收工时如果还有没送到的口信，Stop 钩子会拦下收工把口信交给它，所以收尾阶段捎的话也不会丢。
Claude 侧的收件钩子（见 README 安装第 3 步）会在我每次调工具前，把未读提问 / 报告塞进上下文。

## brief 写法（≤15 行）

只写：**目标 + 文件白名单 + 完成判据 + 背景文档指针**。项目规矩放项目根 `AGENTS.md`（kimi 自动读），不进 brief。
白名单不相交的两单可同 worktree 并行；工人在跑时我只读不写，要并行改就给它开独立 worktree。

## 铁则

1. **绝不 `--continue` / 不带 id 的 `--session`**——用户的交互 kimi 共用会话存储，会接进他的活会话。脚本对它硬拒。
2. **收单后必审**：`git status` / `git diff` + 跑项目测试，才并进结论。
3. 联网/采集类 brief 末尾写死一行：「全程后台干活；禁用真实浏览器 / computer-use 等会动用户桌面或弹权限窗的东西」。
4. 把 `.harness-tmp/` 加进该项目 `.gitignore`（日志 / brief / 频道都落这儿，别提交）。
