---
name: meeting-minutes
description: 解析会议逐字稿，做发言人统计、议题切分、决议与待办抽取，生成中文会议纪要。
---
# 会议纪要助手（meeting-minutes）

一个本地命令行会议纪要 agent：吃进「说话人」逐字稿，先做完全确定性的解析（发言人统计、时间戳会议时段、议题切分、决议/待办/未决问题抽取），再让模型把这些材料组织成正式纪要，输出可直接归档的中文 Markdown 文件。除 `anthropic` 外只用标准库。

## 它能做什么

- **三种逐字稿写法都认**：`说话人: 内容`、`[12:03] 说话人：内容`、`说话人> 内容`，可以混用，容忍空行、时间戳和续行
- **发言人统计**：发言次数、字数、发言占比、平均单次长度、最长发言，一张表看清谁说了多少
- **会议时段**：从时间戳解析出开始/结束时间与会议时长（分钟）
- **议题切分**：按「议题/Topic/议程/讨论/关于XXX」标记行切段；没有标记行就按「说话人切换 + 空行」自动切段，并统计每议题的参与人、时长、发言占比
- **决议与待办抽取**：命中「决定/决议/结论/确认」→ 决议；命中「负责/跟进/下周/截止/DL/落地/安排」→ 待办，并用正则抽出负责人（「张三负责」「由李四跟进」「负责人：王五」）和截止时间
- **未决问题与风险**：命中「待定/未决/还需要/下次讨论/有争议」的问题单独列出，「风险/担心/阻塞/卡点」进风险清单
- **正式纪要**：模型生成背景、议题讨论与观点、决议、待办表、风险；`--offline` 用确定性结果直接拼出同样结构的骨架纪要

## 怎么用

### 第一步：安装依赖

```powershell
# Windows PowerShell
cd agents/meeting-minutes
py -3 -m pip install -r requirements.txt
```

```bash
# macOS / Linux
cd agents/meeting-minutes
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

### 第三步：生成纪要

```powershell
# Windows PowerShell
py -3 agent.py 会议记录.txt --title 支付模块评审 --date 2026-10-28 --output 纪要.md
```

```bash
# macOS / Linux：先跑离线确定性模式看看结构
python3 agent.py 会议记录.txt --offline --output 纪要.md
```

纪要默认写到当前目录的 `minutes.md`。

## 常用示例

```bash
# 1. 逐字稿文件 → 完整纪要（模型润色，需要 key）
python3 agent.py 会议记录.txt --title 周例会 --date 2026-10-28

# 2. 没有文件，直接传逐字稿内容，纯确定性模式
python3 agent.py --text "张三：决定下周灰度。李四：我负责盯监控。" --offline

# 3. 走第三方兼容网关，指定模型名
python3 agent.py 会议记录.txt --model qwen3.8-max --output 评审纪要.md

# 4. 跑一遍离线自检（不联网、不需要 key）
python3 agent.py --self-test
```

## 实现方式

1. **逐字稿解析** `parse_transcript()`：`split_timestamp()` 去掉 `[12:03]` / `12:03` 时间戳（裸时间戳要后面跟着说话人才认），`match_topic()` 认议题标记行，`match_speaker()` 认三种说话人写法，剩下的行当续行并回上一条发言。
2. **确定性统计** `collect_facts()`：`speaker_stats()`（次数/字数/占比/平均/最长）、`meeting_span()`（时段与时长）、`split_topics()`（有标记按标记切，没有按「说话人切换 + 空行」切，每段补参与人与时长）。
3. **规则抽取**：`extract_decisions()` / `extract_todos()` / `extract_opens()` / `extract_risks()`，负责人用 `extract_owner()`（三条正则按优先级：`负责人：X` → `由X负责/跟进` → `X负责`），时间用 `extract_due()`。
4. **模型生成**（可选）`analyze_with_model()`：`call_model()` 三级降级调用（兼容官方 API 与第三方网关），要求返回 JSON `{"background":"","topics":[...],"decisions":[],"todos":[...],"risks":[]}`；`parse_model_json()` 解析失败时原文进附录，内容用 `deterministic_minutes()` 兜底。
5. **输出纪要** `build_report()`：会议信息表、背景、议题（参与人/时长/讨论/观点）、决议、待办表、发言统计表、未决问题、风险、附录。

## 文件清单

| 文件 | 说明 |
| --- | --- |
| `agent.py` | 全部实现：逐字稿解析、统计、规则抽取、模型调用、纪要渲染、`--self-test` |
| `README.md` | 本介绍文件 |
| `requirements.txt` | 依赖清单（仅 `anthropic`） |

## 常见问题

**Q1：没有 API key 能用吗？**
能。`--offline` 只跑确定性部分：发言人统计、议题切分、决议/待办/未决问题都照常出，纪要用这些结果拼成骨架，只是少了模型润色的背景与观点综述。`--self-test` 也不需要 key。

**Q2：逐字稿格式很乱，有空行也有时间戳，会解析错吗？**
`parse_transcript()` 专门做了容忍：空行只是段落分隔，不产生空发言；时间戳可带可不带；一条发言换行了会自动并回上一条；识别不出说话人的短行（如议题标记）不会污染发言统计。

**Q3：没有「议题一：」这种标记行怎么办？**
`split_topics()` 会退化成按「说话人切换 + 空行」切段，每段自动生成「议题 N：开场句片段」的标题，参与人、时长、占比照常统计。

**Q4：负责人和截止时间抽错了怎么办？**
这是启发式抽取（`extract_owner()` / `extract_due()` 只认「负责人：X」「由X负责/跟进」「X负责」和常见日期写法）。生成正式纪要时模型会综合上下文修正，但不会编造逐字稿里没有的人名和时间；拿不准的地方请在纪要里人工确认。

**Q5：模型返回的不是 JSON 会怎样？**
不会崩。终端会提示「模型输出不是合法 JSON」，纪要改用确定性抽取结果生成，模型原文原样放进纪要「附录」。

**Q6：走第三方兼容网关怎么配？**
运行时加 `--model 网关认得的模型名`，例如 `python3 agent.py 会议记录.txt --model qwen3.8-max`。`call_model()` 会自动从「thinking + fallbacks」逐级降到最朴素的 `messages.create`，网关不认新参数也不会报错。
