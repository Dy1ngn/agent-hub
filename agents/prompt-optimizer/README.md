---
name: prompt-optimizer
description: 提示词体检与优化：五要素、模糊词、结构与占位符确定性体检，生成评分卡与优化版提示词。
---

# 提示词体检与优化（prompt-optimizer）

一个本地命令行提示词优化 agent：先用完全确定性的规则给提示词做“体检”（五要素、模糊词、结构、占位符一致性），按固定公式算出五维评分卡，套用【角色/任务/背景/约束/输出格式/示例】模板重排结构化版本，最后再让模型产出优化版提示词与逐条改动说明，输出一份中文 Markdown 报告。除 `anthropic` 外只用标准库。

## 它能做什么

- **五要素体检**：角色 / 任务 / 约束 / 输出格式 / 示例，逐项判定 有 / 弱 / 无，并给出证据行与补写建议
- **模糊词定位**：中英双语模糊词表（一些、尽量、可能、差不多、some、good、relevant……）逐处定位并给替换建议词
- **结构体检**：长度、段落、编号列表、`###`/`---` 分隔标记、多任务混杂、中英混杂、`{{var}}` 与 `{var}` 占位符一致性
- **确定性评分卡**：清晰度 / 完整性 / 可执行性 / 格式 / 约束 五维 0-100 分 + 总分，公式公开写在报告里，同样的输入永远同样的分数
- **结构化重排**：离线也能用，把原文按模板重新排版成可直接使用的新提示词
- **模型优化**：生成优化版提示词、逐条改动对照（类型 / 改动前 / 改动后 / 原因）、使用建议，返回非法 JSON 时原文进附录不崩溃
- **离线自检**：`--self-test` 不联网、不需要 key，覆盖全部确定性逻辑

## 怎么用

### 第一步：安装依赖

```powershell
# Windows PowerShell
cd agents/prompt-optimizer
py -3 -m pip install -r requirements.txt
```

```bash
# macOS / Linux
cd agents/prompt-optimizer
python3 -m pip install -r requirements.txt
```

### 第二步：设置 API key（只有调用模型做优化时需要）

```powershell
# Windows PowerShell（当前窗口有效）
$env:ANTHROPIC_API_KEY="你的key"
```

```bash
# macOS / Linux
export ANTHROPIC_API_KEY=你的key
```

加 `--offline` 可以完全不设 key、不联网，只做确定性体检。

### 第三步：运行体检

```powershell
# Windows PowerShell
py -3 agent.py prompt.txt --goal "生成小红书文案" --output 优化报告.md
```

```bash
# macOS / Linux
python3 agent.py prompt.txt --goal "生成小红书文案" --output 优化报告.md
```

报告默认写到当前目录的 `prompt-report.md`。

## 常用示例

```bash
# 1. 离线体检：不调用模型、不花钱，输出体检表 + 评分卡 + 结构化版本
python3 agent.py prompt.txt --offline

# 2. 不用文件，直接体检一段提示词文本
python3 agent.py --text "帮我写个文案，尽量好一点" --offline --output 快速体检.md

# 3. 完整体检 + 模型优化，指定任务目标与结构化风格
python3 agent.py prompt.txt --goal "月度销售分析" --style 结构化 --output 优化报告.md

# 4. 走第三方 Anthropic 兼容网关时，用 --model 指定网关认得的模型名
python3 agent.py prompt.txt --model qwen3.8-max --output 优化报告.md
```

第 1、2 条适合提交前快速自查；第 3 条会额外产出优化版提示词与改动对照；第 4 条用于官方 API 之外的网关。

## 实现方式

体检在前、模型在后的两段式流程：

1. **确定性体检**：`detect_elements()` 用强/弱两级正则判定五要素并抓证据行；`find_vague_words()` 按中英模糊词表逐处定位；`analyze_structure()` 统计长度、段落、列表、分隔标记、多任务与语言混杂、`{{var}}`/`{var}` 混用；`collect_issues()` 把发现汇总成 严重 / 警告 / 建议 三级问题清单。
2. **评分卡**：`compute_scores()` 按公开公式算五维得分（要素命中率、模糊词密度、结构分），`improvement_points()` 给每个维度的提升点；`render_structured()` 用 `classify_lines()` 把原文归入六类后按模板重排。
3. **模型优化**：`optimize_with_model()` 把原文、体检缺项和风格要求发给模型，要求只返回 JSON（`optimized` / `changes` / `tips`），`parse_optimization_json()` 解析失败不崩溃，原文进报告附录。
4. **出报告**：`build_report()` 汇总概览、体检表、评分卡（含优化前后对比）、问题清单、结构化版本、优化版本、改动对照、使用建议。

模型调用带三级参数降级 `call_model()`：先试 `beta.messages.create`（adaptive thinking + server-side fallback），失败退到 `messages.create(thinking=...)`，再失败退到最基础调用，兼容不认新参数的第三方网关。

## 文件清单

- `agent.py` — 主程序：五要素体检、模糊词与结构检测、评分卡、模板重排、模型优化、报告生成、CLI 与离线自检
- `README.md` — 本说明
- `requirements.txt` — Python 依赖（仅 `anthropic>=0.112.0`）

## 常见问题

**1. 没有 ANTHROPIC_API_KEY 能跑吗？**
能。加 `--offline` 就只跑确定性体检和模板重排，完全不联网、不花钱。不加时如果检测不到 key，会打印中英文提示（含 macOS `export` 与 PowerShell `$env:` 写法）并退出，不会卡住。

**2. `--self-test` 是什么？**
离线自检：在临时目录造强 / 弱 / 空三种提示词样本，跑全部确定性逻辑并断言，包括模型 JSON 解析兜底和 `call_model()` 三级降级（用假客户端模拟），最后打印 `SELF-TEST OK`。改完代码先跑它。

**3. 走第三方兼容网关报错怎么办？**
用 `--model` 指定网关认得的模型名，例如 `--model qwen3.8-max`；`call_model()` 会自动降级参数，网关不认 `thinking`/`fallbacks` 也能跑通。

**4. 评分为什么会变低？**
分数完全由公式决定：模糊词每处扣清晰度 8 分、多任务混杂扣 15 分、中英混杂扣 10 分，要素缺失直接影响完整性和可执行性。报告里写明了口径，可对照问题清单逐条改。

**5. 模型返回的不是 JSON 怎么办？**
程序不会崩溃：解析失败的原文会原样放进报告附录「模型原始输出」，确定性体检部分照常输出。

**6. 提示词很长会被截断吗？**
超过 20000 字符会截断并在问题清单里给出警告，建议拆成多条提示词分别体检。
