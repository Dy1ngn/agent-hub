---
name: weekly-report
description: 采集 git 提交与工作记录，做分类统计与合并去重，生成中文周报/日报。
---
# 周报/日报生成助手（weekly-report）

一个本地命令行周报 agent：用 `git log --numstat` 采集提交数据（说明、作者、日期、改动文件与增删行数），配合工作记录 markdown，先做完全确定性的分类统计与合并去重，再让模型写成正式周报/日报，输出中文 Markdown 文件。`--offline` 直接出确定性骨架周报。除 `anthropic` 外只用标准库。

## 它能做什么

- **git 采集**：解析 commit hash/日期/作者/说明与每个文件的增删行数，支持 `--repo` 指定仓库、`--days` 控制时间窗、`--author` 过滤作者
- **commit 列表文件**：`--commits 文件` 从文本读 commit 列表（每行 `日期 | 提交说明 | +新增 -删除 | 文件列表`，字段可省略、`#` 为注释），没有 git 仓库也能出完整的提交统计；可与 `--repo` 同时用
- **提交分类**：conventional 前缀（feat/fix/docs/refactor/test/chore）优先，中文说明按关键词（新增/修复/文档/重构/测试/杂项）兜底，认不出归「其他」；`#1022 xxx`、`【标签】xxx` 这类前缀会先剥掉
- **模块聚合**：按改动文件的顶层目录聚合（`src/…` 归 src，根目录文件归「根目录」），统计每个模块的提交数、增删行、文件数
- **活跃分布**：commit 总数、代码增删总量、活跃天数与每日提交分布
- **工作记录解析**：按日期标题分组（`## 2026-10-27` / `## 10.27` / `## 周一`），抽取 `- [x]` / `- [ ]` 条目与勾选状态
- **合并去重**：git 提交说明与工作记录条目按日期归到同一天，归一化后重复的条目只留一份，重复数写进报告
- **正式周报**：模型生成概述、本周完成、进行中、下周计划、风险与需要支持；数据附录固定给出分类统计表、模块统计表、每日分布、按日明细

## 怎么用

### 第一步：安装依赖

```powershell
# Windows PowerShell
cd agents/weekly-report
py -3 -m pip install -r requirements.txt
```

```bash
# macOS / Linux
cd agents/weekly-report
python3 -m pip install -r requirements.txt
```

### 第二步：设置 API key（只在需要模型时；`--offline` 可跳过）

```powershell
# Windows PowerShell（当前窗口有效）
$env:ANTHROPIC_API_KEY="你的key"
```

```bash
# macOS / Linux
export ANTHROPIC_API_KEY=你的key
```

也支持 `ANTHROPIC_AUTH_TOKEN`。走第三方兼容网关（如阿里云）时用 `--model` 指定网关认得的模型名，例如 `--model qwen3.8-max`。

### 第三步：生成周报

```powershell
# Windows PowerShell
py -3 agent.py --repo D:\projects\myapp --notes 工作记录.md --output 周报.md
```

```bash
# macOS / Linux：先跑离线确定性骨架看看数据
python3 agent.py --repo ~/projects/myapp --notes 工作记录.md --offline
```

报告默认写到当前目录的 `weekly-report.md`。

## 常用示例

```bash
# 1. 最近 7 天的周报：git 仓库 + 工作记录合并
python3 agent.py --repo ~/projects/myapp --notes 工作记录.md

# 2. 只看自己的提交，只统计 14 天
python3 agent.py --repo . --author zhangsan --days 14 --output 我的两周报.md

# 3. 只有工作记录、没有仓库（或没装 git），纯确定性骨架
python3 agent.py --notes 工作记录.md --offline

# 4. 生成日报 + 走第三方兼容网关指定模型名
python3 agent.py --repo . --type daily --model qwen3.8-max

# 5. 没有 git 仓库：用 commit 列表文件 + 工作记录，纯确定性骨架
python3 agent.py --commits commits.txt --notes 工作记录.md --offline
```

## 实现方式

1. **git 采集** `collect_git_commits()`：跑 `git log --numstat --date=iso --pretty=format:%H%x1f%ad%x1f%an%x1f%s%x1f%b`，`parse_git_log()` 按 `\x1f` 分隔头字段、按制表符解析 numstat 行（二进制文件记 0 行），多行 body 续接；空仓库（没有 commit）返回空列表不报错。
2. **统计** `aggregate_git()`：`classify_commit()` 分类、`module_of()` 归模块、`commit_day()` 取日期，汇总出分类表、模块表、每日分布、活跃天数。
3. **工作记录** `parse_notes()`：日期标题三种写法（完整日期/月.日/周X，周X映射到最近的那一天），`- [x]`/`- [ ]` 抽条目与勾选状态，纯标题行不算分组。
4. **合并去重** `merge_by_day()`：`normalize_item()` 去类型前缀、标点、空白后比较，git 与记录条目按日期归并，重复条目丢弃并计数。
5. **生成**：`--offline` 用 `skeleton_analysis()`（本周完成=commit 分类清单、下周计划=未勾选项）；否则 `analyze_with_model()` 里 `call_model()` 三级降级调用（兼容官方 API 与第三方网关），要求返回 JSON `{"summary":"","done":[],"in_progress":[],"next_plan":[],"risks":[],"help_needed":[]}`，`parse_model_json()` 解析失败就退回骨架，模型原文进附录。
6. **输出报告** `build_report()`：概述、本周完成、进行中、下周计划、风险与需要支持、数据附录（分类统计表/模块统计表/每日分布/按日明细/数量汇总）。

## 文件清单

| 文件 | 说明 |
| --- | --- |
| `agent.py` | 全部实现：git 采集、统计分类、记录解析、合并去重、模型生成、报告渲染、`--self-test` |
| `README.md` | 本介绍文件 |
| `requirements.txt` | 依赖清单（仅 `anthropic`） |

## 常见问题

**Q1：没有 API key 能用吗？**
能。`--offline` 直接生成确定性骨架周报：概述里的数据、按类型分组的 commit 清单、代码量、活跃分布、下周计划（工作记录未勾选项）都在，只是没有模型润色的表述。`--self-test` 也不需要 key。

**Q2：没装 git 或者只写了工作记录怎么办？**
只给 `--notes` 就行；或者给 `--repo` 但本机没装 git 时会提示「跳过 git 采集」并只用工作记录。也可以用 `--commits 文件` 提供 commit 列表（每行 `日期 | 提交说明 | +新增 -删除 | 文件列表`，除说明外都可省略），没有仓库也能出完整提交统计。各来源都没数据才会报错退出。

**Q3：commit 说明和工作记录写了同一件事，会不会重复算？**
不会。`merge_by_day()` 会去掉 conventional 前缀、标点和空白后比较，完全相同或互相包含的条目只保留 git 那份，丢弃数量写在报告「数量汇总」里。

**Q4：提交类型分类不准怎么办？**
分类是启发式：先认 conventional 前缀（`feat:` / `fix(scope):`），再认中文关键词开头（新增/修复/文档/重构/测试/杂项），`#123`、`【标签】`前缀会先剥掉，认不出的归「其他」并在分类表里列出原始说明，方便人工核对。

**Q5：周报里的日期范围是怎么算的？**
`--days` 控制窗口（weekly 默认 7 天、daily 默认 1 天），报告标题是「起始日 ~ 今天」，git 采集用 `git log --since=N days ago`。工作记录里的「周X」标题会映射到最近 7 天内对应的那一天。

**Q6：模型返回的不是 JSON 会怎样？**
不会崩。终端会提示并自动退回确定性骨架周报，模型原文原样放进报告「附录」，数据附录不受影响。
