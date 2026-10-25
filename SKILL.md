---
name: dispatch-kimi
description: 用户说「让 kimi 审 / 复审 / 挑错」「kimi 盯返工」「派 kimi」或 "have kimi review this" 时用；我做完一段规划或实现、要交付前也主动派。分工：Claude 规划和执行，本机 Kimi Code CLI（K3.1 Pro）只做复审——对照计划审改动、挑矛盾和漏洞，返工后在同一会话里逐条复核。支持运行中双向对讲（捎话 / 反问 / 唤醒）。
---

# 派 Kimi 复审

分工：**我（Claude）规划和动手，Kimi 只审不改。** K3.1 Pro 单轮语义漏洞小、会把能说的都说完，
适合挑矛盾、查漏，不当执行手。用户明说要 Kimi 动手时，才去掉 `--ro` 派执行单。

流程 = 我做完一段 → 写审查 brief → `--ro` 派审 → 逐条回应发现（改，或写理由反驳）→
`--resume` 同一会话复审 → 直到 `VERDICT: PASS`。

## 派单

```bash
S=~/.claude/skills/dispatch-kimi/scripts
# 首审（用 Bash run_in_background:true，别前台干等；K3.1 Pro max 档审一段改动要几分钟）
bash $S/kimi_task.sh --ro -f .harness-tmp/review-<name>.md --tag review-<name>
# 复审：必须显式 session id（= 上一轮 stderr 的 SESSION=），模型和上下文沿用原会话
bash $S/kimi_task.sh --resume <session-id> -p "<逐条回应>。复审：……（见下）"
```

常用开关：`-m <model>`（默认 `kimi-code/k3.1-pro`；`kimi-code/k3.1-pro-highspeed` 更快但更粗）·
`-e low|high|max`（默认用模型自己的档，K3.1 Pro 是 max）· `--add-dir <d>` 多一个工作目录 ·
`-C <dir>` 指到独立 git worktree · `--no-channel` 不开对讲。完整列表：`bash $S/kimi_task.sh --help`。

**Kimi 没有系统沙箱**：`kimi -p` 里工具调用全部自动放行，Bash 不在沙箱里（实测能联网）。
`--ro` 只是拿掉 Write/Edit，Bash 还在，所以审完要看 `git status`。
要附图就在 brief 里写图片路径，工人自己用 ReadMediaFile 读。

## 审查 brief（写进 `.harness-tmp/review-<name>.md`）

```text
你是复审员，只审不改：不许改任何项目文件；Bash 只用来读代码、跑测试、看 git diff。
审查对象：<改动范围，如 `git diff main...HEAD`、某几个 commit、某几个文件>
对照依据：<计划 / 规格 / 需求文档的路径>。依据里定下的取舍不算问题，除非它和别处自相矛盾。
重点：<一两句，如「文档和实现是否一致」「并发写同一个文件」>
可以跑：<测试命令>
报告只交一份，格式：
- 每条发现以「[高|中|低] 文件:行 —— 问题」开头，下面写证据（引用代码或命令输出）和为什么错。
- 拿不准是不是故意这么写的，先用 ask 问主线，别直接记成发现。
- 最后一行写 VERDICT: PASS 或 VERDICT: FAIL（有高或中就是 FAIL）。
```

一定给「对照依据」：只给 diff 它只能抓局部 bug，代码和计划、文档和实现之间的矛盾要有依据才抓得到。

复审的 prompt：

```text
上一轮的发现我这样处理了：
1. <发现摘要> —— 已改，见 <文件:行 / commit>
2. <发现摘要> —— 不改，理由：<…>。你认不认这个理由？
复审：逐条标「已修 / 未修 / 修出新问题」，再看这次的改动本身有没有新问题。格式同前，最后一行 VERDICT。
```

## 我这边的规矩

1. **每条发现都要回应**：改，或把不改的理由写进复审 prompt 让它判。不许默默略过。
2. **不照单全收**：每条先自己核实（读代码、跑测试复现），误报在回应里说明。
3. **审完看 `git status`**：它动了文件就还原，并在复审 prompt 里指出。
4. **复审用 `--resume` 同一会话**（它记得上一轮提了什么）；审查范围换了才开新单。
5. **同一条争了两轮还没结论**：把双方理由摆给用户拍板。

## 对讲（派单自动开频道）

无头工人一开跑就没有输入口，所以每次派单自动开一条频道，stderr 打 `CHANNEL=<频道>` 和 `WAKEUP=<频道>`。
复审时最常见的是它 `ask`「这里是故意的吗」。

```bash
M="$HOME/.claude/skills/dispatch-kimi/scripts/kimi_msg.py"   # 调用写 python3 "$M"：bash 与 zsh 都认
python3 "$M" send <频道> "补充：重点看 parser.py"   # 捎话：工人下一次调工具前送到（加 --stop = 回话前把它挂住）
python3 "$M" poll <频道>                           # 看工人问了什么 / 报了什么
python3 "$M" answer <频道> "答复"                  # 答它：工人 2 秒内拿到，在同一个回合里接着干
python3 "$M" wait <频道>                           # 守候器：挂 Bash run_in_background，工人一出声就叫醒我
```

派完单后**顺手挂一个 `wait`**；它退出时看退出码：0 = 工人在等答复（赶紧 answer，它最多等约 5 分钟）/
10 = 只有报告 / 20 = 收工 / 30 = 超时 / 40 = 没这频道。`wait` 要**裸跑，别套管道**（套了就看不到真退出码）。
答完再挂一个。

工人准备收工时如果还有没送到的口信，Stop 钩子会拦下收工把口信交给它，所以收尾阶段捎的话也不会丢。
Claude 侧的收件钩子（见 README 安装第 3 步）会在我每次调工具前，把未读提问 / 报告塞进上下文。

## 铁则

1. **绝不 `--continue` / 不带 id 的 `--session`**——用户的交互 kimi 共用会话存储，会接进他的活会话。脚本对它硬拒。
2. **它的报告是线索不是结论**：采纳哪条、驳回哪条，由我核实后决定，再告诉用户。
3. 把 `.harness-tmp/` 加进该项目 `.gitignore`（日志 / brief / 频道都落这儿，别提交）。
