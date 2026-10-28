---
name: writing-assistant
description: 写作润色助手，先做文本体检评分，再按润色/重写/扩写/缩写/正式/口语改写并给出逐句对照报告。
---

# 写作润色助手（writing-assistant）

一个本地命令行写作 agent：先把原文做一次确定性「体检」（字数/词数、句数、平均句长、最长句、段落数、重复词、口头禅、可读性评分），再按模式（润色/重写/扩写/缩写/正式/口语）让模型改写，最后用 difflib 生成「原文 ↔ 修改稿」逐句对照，输出一份中文 Markdown 写作报告。除 `anthropic` 外只用标准库。

## 它能做什么

- **文本体检**：中英文混合统计（字数、词数、句数、平均句长、最长句、段落数）、重复词 top5（中文 2-gram + 英文单词）、连接词/口头禅计数（然后/其实/就是/非常/可能等 20 个）、可读性启发式评分（句长 + 口头禅 + 重复 + 长难词）
- **六种改写模式**：`polish` 润色、`rewrite` 重写、`expand` 扩写、`shorten` 缩写、`formal` 正式、`casual` 口语，各自有明确的提示词模板；`--tone`、`--audience` 进一步约束语气和读者
- **逐句对照**：`difflib.SequenceMatcher` 按中文句子切分做「原文 ↔ 修改稿」对照表，`unified_diff` 兜底给出标准 diff，新增/删除/改写都标出来
- **修改点清单**：模型返回的每条修改（类型/原文片段/修改后/原因）渲染成表格，配合模型给的清晰度、简洁度、语气契合三项评分
- **稳健降级**：模型回复不是约定 JSON 时改写稿自动退回原文并标注，程序不崩；`--offline` 不联网也能拿到体检与对照
- **离线自检**：`--self-test` 用临时样本验证统计数值、对照渲染、模式模板、JSON 解析兜底，不联网也不需要 API key

## 怎么用

### 第一步：设置 API key（--offline 可跳过）

```powershell
# Windows PowerShell（当前窗口有效）
$env:ANTHROPIC_API_KEY="你的key"
```

```bash
# macOS / Linux
export ANTHROPIC_API_KEY=你的key
```

### 第二步：安装依赖

```powershell
# Windows PowerShell
cd agents/writing-assistant
py -3 -m pip install -r requirements.txt
```

```bash
# macOS / Linux
cd agents/writing-assistant
python3 -m pip install -r requirements.txt
```

### 第三步：运行润色

```powershell
# Windows PowerShell
py -3 agent.py 文章.md --mode polish --tone 温和 --audience 产品经理
```

```bash
# macOS / Linux
python3 agent.py 文章.md --mode polish --tone 温和 --audience 产品经理
```

报告默认写到当前目录的 `writing-report.md`，包含评分卡、修改对照、修改点清单和改写稿。

## 常用示例

```bash
# 1. 只做体检与对照，不花钱不联网（改写稿=原文）
python3 agent.py 文章.md --offline

# 2. 写正式邮件：正式书面语 + 商务读者
python3 agent.py 邮件.txt --mode formal --audience 客户 --output 邮件润色报告.md

# 3. 压缩啰嗦的段落，语气保持坚定
python3 agent.py 草稿.md --mode shorten --tone 坚定

# 4. 走第三方 Anthropic 兼容网关时，用 --model 指定网关模型名
python3 agent.py 文章.md --mode rewrite --model qwen3.8-max
```

第 1 条适合写完先自查啰嗦程度和口头禅；第 2 条把口语草稿改成能直接发出去的正式文本；第 3 条给长文瘦身；第 4 条用于官方 API 之外的网关。

## 实现方式

1. **体检**：`text_stats()` 一次算完所有指标，`repeated_terms()` 抽中文 2-gram 与英文单词的重复词，`filler_counts()` 数连接词/口头禅，`readability_score()` 按句长、口头禅、重复词、长难词比例扣分得到 0-100 的启发式评分。全部确定性，同样的文本永远同样的分数。
2. **改写**：`build_user_prompt()` 把 `MODE_PROMPTS[mode]` 模板 + 语气 + 读者 + 原文拼成请求，`review_text()` 调 `call_model()`（三级参数降级，兼容官方 API 与第三方网关），要求返回 `{"rewritten": ..., "changes": [...], "score": {...}}`。
3. **解析兜底**：`parse_review_json()` 严格校验并归一化模型 JSON（评分裁剪到 0-100、坏的修改点丢弃）；解析不出来时 `review_text()` 把原文当 rewritten，报告里标注「改写稿直接使用原文」并附上模型原文。
4. **对照**：`render_diff()` 用 `difflib.SequenceMatcher` 对句子列表取 opcodes，渲染「原文 ↔ 修改稿」Markdown 表格（新增/删除/改写/无变化），再用 `difflib.unified_diff` 输出标准 diff 块。
5. **报告**：`build_report()` 生成评分卡统计表、对照表、修改点清单、改写稿与可选的模型原文附录。

## 文件清单

- `agent.py` — 主程序：文本统计、对照渲染、模式模板、模型改写、报告生成、CLI 与离线自检
- `README.md` — 本说明
- `requirements.txt` — Python 依赖（仅 `anthropic>=0.112.0`）

## 常见问题

**1. 没有 ANTHROPIC_API_KEY 能跑吗？**
能。加 `--offline` 就只做体检和对照：评分卡、逐句对照表照常生成，只是改写稿=原文。不加 `--offline` 又检测不到 key 时，会打印含 macOS 与 PowerShell 写法的中文提示并退出。

**2. 走第三方 Anthropic 兼容网关怎么配？**
设置网关要求的 `ANTHROPIC_BASE_URL` 和 `ANTHROPIC_AUTH_TOKEN`（或 `ANTHROPIC_API_KEY`），再用 `--model` 指定网关支持的模型名，例如 `--model qwen3.8-max`。`call_model()` 三级参数降级会自动绕过网关不认识的 thinking / fallbacks 参数。

**3. 可读性评分是怎么算的？**
启发式：以 100 分起扣，平均句长超过 20 字、出现口头禅、重复词、长难词（≥13 字母英文词）都会按比例扣分。它衡量的是「读起来累不累」，不是绝对好坏，适合改写前后对比。

**4. 六个模式有什么区别？**
`polish` 只修语病不改结构，`rewrite` 可以重组但不丢事实，`expand` 补细节到 1.5-2 倍，`shorten` 压到一半，`formal` 改正式书面语，`casual` 改轻松口语。模板在 `MODE_PROMPTS` 里，看代码一目了然。

**5. 模型返回的不是合法 JSON 会怎样？**
程序不会崩：改写稿自动退回原文，报告里会标注「改写稿直接使用原文」，模型原文放在附录里供人工参考，可以换模式或换模型重跑。

**6. Windows 上 `python` / `python3` 没反应？**
本机的 `python` 可能是 Microsoft Store 的占位桩，统一改用 `py -3 agent.py ...`。程序内置 UTF-8 控制台兜底，中文报告与终端输出不会乱码。
