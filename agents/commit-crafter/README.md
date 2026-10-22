---
name: commit-crafter
description: 读取 Git 改动并生成规范的 commit message，默认只预览，确认后才提交。
---

# Git 提交信息生成助手（commit-crafter）

读完你的 diff，用一条符合 Conventional Commits 的中文（或英文）提交信息替换「改了点什么」这种糊弄式 message。

## 它能做什么

- **只读收集**：看 `git status`、`git diff`、当前分支和最近 10 条提交标题，对齐你仓库既有的风格
- **启发式预判类型**：纯文档 → docs、纯测试 → test、纯 CI 配置 → ci、纯依赖/锁文件 → chore、依赖升级 → build、只有空白变化 → style、新增业务文件 → feat，其余给 fix / refactor 候选
- **模型生成正文**：subject ≤ 50 字符、祈使语气，正文最多 6 条 `- ` 要点，可带 `BREAKING CHANGE`
- **三种范围**：已暂存（默认）、工作区全部改动、任意提交区间 `A..B`
- **安全提交**：默认只打印；加 `--apply` 并确认后才执行 `git commit`，还能 `--amend`
- **离线可自检**：`--self-test` 不需要网络和 API key

## 怎么用

先进入本 agent 目录：`cd agents/commit-crafter`（下面命令里的 `agent.py` 都在这个目录下）。

### 第一步：装依赖、设 key

PowerShell：

```powershell
py -3 -m pip install -r requirements.txt
$env:ANTHROPIC_API_KEY = "你的key"
```

macOS / Linux：

```bash
python3 -m pip install -r requirements.txt
export ANTHROPIC_API_KEY="你的key"
```

### 第二步：`--dry-run` 先预览

PowerShell：

```powershell
py -3 agent.py --repo C:\path\to\your-project --dry-run
```

macOS / Linux：

```bash
python3 agent.py --repo ~/path/to/your-project --dry-run
```

它只生成并打印提交信息，不会写任何东西进 git。

### 第三步：确认后 `--apply` 真提交

PowerShell：

```powershell
py -3 agent.py --repo C:\path\to\your-project --apply --yes
```

macOS / Linux：

```bash
python3 agent.py --repo ~/path/to/your-project --apply --yes
```

不加 `--yes` 会先问你 `[y/N]`；如果 stdin 不是终端（比如被脚本调用），没有 `--yes` 一律视为取消。

> 也可以直接 `cd` 到你的项目仓库里运行（此时可省略 `--repo`，默认就是当前目录）。

## 常用示例

以下示例都在**你的项目仓库目录里**运行；如果在 agent 目录里运行，给每条命令加上 `--repo <项目路径>`。

```powershell
# 1. 分析已暂存的改动，生成本次提交信息（不提交）
py -3 agent.py

# 2. 连未跟踪文件一起看，用 gitmoji 风格
py -3 agent.py --all --style gitmoji

# 3. 给最近 3 条提交补写一条汇总说明（只读区间，不提交）
py -3 agent.py --range HEAD~3..HEAD --lang en

# 4. 确认预览没问题后，直接提交并带上正文
py -3 agent.py --apply --yes
```

补充：`--repo PATH` 可以在别的目录里操作指定仓库；`--amend` 会把信息追加到上一条提交（必须同时给 `--apply`）；`--max-diff-chars` 控制塞给模型的 diff 上限（默认 60000 字符，超出会截断并提示）。

## 输出格式示例

```
================================================================
feat(auth): 增加登录失败重试限制

- 连续失败 5 次后锁定账号 10 分钟
- 锁定状态写入 Redis，重启不丢失
- 补充 tests/test_auth.py 覆盖锁定与解锁流程
================================================================
```

`--lang en` 时正文为英文；检测到不兼容改动时会多出一个 `BREAKING CHANGE: ...` 段落。

## 实现方式

1. 用 `subprocess` 调 git 只读命令收集信息（`status --porcelain`、`diff --cached`、`diff --name-status`、`log -n 10` 等），diff 超长自动截断。
2. 纯函数 `classify_changes()` 按文件路径和 diff 内容做确定性判断，给出候选 type 和理由，输出「候选类型：feat（新增 3 个源文件）」这样的提示；拿不准就明说「无法判断」。
3. 把改动清单、diff、最近提交标题和目标风格拼成 prompt，调用模型生成提交信息。
4. 解析器做健壮处理：丢掉代码围栏和「好的，以下是……」这类前言、subject 超长截断、缺 type 自动补、正文超过 6 条丢弃多余部分；彻底解析失败就用启发式标题兜底，不抛异常。
5. 模型调用带**参数降级兼容**：先试 `beta.messages` + adaptive thinking + fallback，再退普通端点，最后退最基础调用，兼容官方 API 和第三方 Anthropic 兼容网关。

## 安全说明

- **只 commit**：唯一会写仓库的动作是 `git commit`（以及你显式要求的 `--amend`）
- **绝不 `git add`**：它不会替你暂存文件，`--all` 只是把未暂存/未跟踪的改动**读**进来生成信息
- **绝不 `git push` / `reset` / `checkout` / `stash`**：没有这些代码路径
- 非交互环境（stdin 不是 tty）且没加 `--yes` 时，一律视为取消
- 提交信息通过临时文件（`git commit -F`）传入，避免 shell 引号和换行转义问题，用完即删

## 文件清单

- `agent.py` — 主程序（git 只读收集、启发式分类、模型调用、解析、提交、`--self-test`）
- `README.md` — 本说明
- `requirements.txt` — Python 依赖（仅 `anthropic`，其余用标准库）

## 常见问题

**Q1：提示「没有检测到 ANTHROPIC_API_KEY」，还能用吗？**
能，但只会用启发式生成标题（比如 `fix: 修复问题（2 个文件）`）和文件清单正文，质量不如模型。设好 key 或用 `--model` 指定网关模型后再跑即可。

**Q2：我没有暂存任何东西，`--staged` 说没内容怎么办？**
它不会替你 `git add`。要么自己先 `git add`，要么改用 `--all` 把工作区全部改动（含未跟踪文件名）一起分析。

**Q3：想用第三方 Anthropic 兼容网关（比如阿里云）怎么配？**
设置 `ANTHROPIC_BASE_URL` 和 `ANTHROPIC_AUTH_TOKEN`，再用 `--model qwen3.8-max`（或网关上实际的模型名）覆盖默认模型；也可以用 `COMMIT_CRAFTER_MODEL` 环境变量固定默认模型。代码会自动降级去掉网关不支持的参数。

**Q4：`--apply` 之后发现信息不对，能撤销吗？**
本次不会自动撤销。提交还没 push 时，可以用 `git commit --amend` 重写（本工具支持 `py -3 agent.py --apply --amend --yes`），或 `git reset --soft HEAD~1` 自己回退。

**Q5：为什么 subject 有时候被截断了？**
规范要求 subject ≤ 50 字符，超长会在词组边界截断并去掉句尾标点。想表达更多内容请写进正文。
