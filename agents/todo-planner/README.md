---
name: todo-planner
description: 把目标描述拆成里程碑与任务，生成 WBS、依赖表、工作日甘特图和风险清单的中文计划。
---

# 目标拆解与计划生成（todo-planner）

把一段目标描述拆成「里程碑 → 任务 → 子任务」，生成 WBS 编号（1 / 1.1 / 1.1.1）、依赖表与循环依赖检测、按工作日排布的日程与甘特式 ASCII 图、按目标类型定制的风险清单，最后输出一份中文 Markdown 计划。确定性解析打底，模型只做更细的结构化拆解，解析失败自动退回规则结果。除 `anthropic` 外只用标准库。

## 它能做什么

- **目标解析**：识别列表行、祈使句、顿号/分号并列项，抽取里程碑标记（第一阶段 / 阶段二 / M1 / 里程碑），识别时间、预算、人力约束，判断目标类型（学习/开发/活动/其他）
- **WBS 编号**：里程碑 `1`、任务 `1.1`、子任务 `1.1.1`，同时渲染 Markdown 缩进树和任务表（工期、交付物、起止日期）
- **依赖分析**：从「先 / 首先 / 然后 / 之后 / 基于 / 完成X后」等词推断前置依赖，生成依赖表，并做循环依赖检测（A→B→A 能被发现）
- **日程排布**：默认跳过周末（`--no-skip-weekends` 可关），按依赖拓扑序把任务工期铺到工作日日历，支持跨月；里程碑自动落到对应日期
- **甘特图与每日待办**：按周分组的 ASCII 甘特图 + 每天该推进哪些任务
- **风险清单**：按目标类型给风险检查表（类别、风险、可能性、影响、应对预案），模型补充的风险单独标注

## 怎么用

### 第一步：安装依赖

```powershell
# Windows PowerShell
cd agents/todo-planner
py -3 -m pip install -r requirements.txt
```

```bash
# macOS / Linux
cd agents/todo-planner
python3 -m pip install -r requirements.txt
```

### 第二步：设置 API key（默认流程会调用模型）

```powershell
# Windows PowerShell（当前窗口有效）
$env:ANTHROPIC_API_KEY="你的key"
```

```bash
# macOS / Linux
export ANTHROPIC_API_KEY=你的key
```

不想用模型就加 `--offline`：纯确定性拆解，不需要 key、不需要网络。

### 第三步：生成计划

```powershell
# Windows PowerShell
py -3 agent.py --goal "一个月内学会 Python 并做两个小项目" --offline --output 计划.md
```

```bash
# macOS / Linux
python3 agent.py --goal "一个月内学会 Python 并做两个小项目" --offline --output 计划.md
```

## 常用示例

```bash
# 1. 最短用法：目标一句话，离线出计划（不需要 key）
python3 agent.py --goal "三个月内入门数据分析" --offline

# 2. 指定开始日期与窗口，稳妥风格会留缓冲工期
python3 agent.py --goal "..." --days 45 --start 2026-10-28 --style 稳妥

# 3. 目标写在文件里（支持多行列表与阶段标记），激进风格压工期
python3 agent.py --from 目标.md --style 激进 --output 计划.md

# 4. 让模型做更细的拆解（需要 key；网关用 --model 指定模型名）
python3 agent.py --goal "..." --model qwen3.8-max --output 计划.md
```

目标文本建议这么写：第一行是目标本身；用「第一阶段：xxx」分阶段；用 `- ` 列任务；写「3 天」给工期；末尾加一行「约束：一个月内完成，预算不超过 X 元」。

## 实现方式

1. **解析层** `parse_goal_text()`：`MILESTONE_RE` 识别阶段标记，`LIST_ITEM_RE` / `SEGMENT_SPLIT_RE` 拆列表行与并列项，`_task_with_subtasks()` 处理「任务：A、B、C」，`_extract_constraints()` 抽时间/预算/人力约束，`detect_goal_type()` 判目标类型。
2. **WBS 层** `build_wbs()` 生成三级编号，`render_wbs_tree()` 渲染缩进树。
3. **依赖层** `build_dependencies()` 按先后词推断依赖边，`detect_cycle()` 深度优先找环，`topological_order()` 稳定拓扑排序（有环退回声明顺序）。
4. **排程层** `workdays_from()` / `next_workday()` 做工作日计算（跨月安全），`schedule_tasks()` 按依赖铺工期，`render_gantt()` 按周画 ASCII 甘特图，`daily_todo()` 生成每日待办。
5. **风险层** `risk_checklist()` 按目标类型取检查表。
6. **模型层** `refine_with_model()` 经三级降级 `call_model()` 拿结构化 JSON，`parse_plan_json()` 解析失败返回 `None`，`merge_plan()` 回退到确定性结果；模型原文进计划附录。

## 文件清单

| 文件 | 说明 |
| --- | --- |
| `agent.py` | 全部实现：解析、WBS、依赖、排程、甘特图、风险、报告、CLI、`--self-test` |
| `README.md` | 本说明 |
| `requirements.txt` | 依赖清单（仅 `anthropic`） |

## 常见问题

**Q1：不设置 API key 就不能用了吗？**  
不是。加 `--offline` 就是纯确定性模式（解析、WBS、排程、甘特图全都在本地算），不需要 key 也不联网；不加 `--offline` 时才会调用模型做更细的拆解，缺 key 会提示设置方法并退出。

**Q2：为什么模型给的计划和我的目标不太一样？**  
模型只负责更细的结构化拆解，且它的 JSON 解析失败时会自动退回确定性解析结果，原文放在计划附录里。想完全可控就用 `--offline`。

**Q3：排程为什么会跳到下个月？**  
默认跳过周末，任务工期按工作日计算，跨月是正常现象；甘特图按周分组，一眼能看到跨月。想连续排就加 `--no-skip-weekends`。

**Q4：任务工期是怎么定的？**  
优先用任务文本里的「X 天」；没写就按风格给默认值（稳妥 2 天、激进 1 天）。单任务上限 15 天，超出计划窗口会在报告和终端提示。

**Q5：检测到循环依赖怎么办？**  
终端和计划里都会明确标出环路径（如 `1.1 → 1.2 → 1.1`），排程自动退回声明顺序，不会死循环。建议手动理顺任务先后关系。

**Q6：想接第三方 Anthropic 兼容网关？**  
用 `--model` 指定网关认得的模型名，如 `--model qwen3.8-max`。模型调用走三级降级 `call_model()`，官方 API 和只认基础参数的网关都能跑。
