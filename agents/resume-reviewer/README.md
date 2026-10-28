---
name: resume-reviewer
description: 简历诊断与 JD 匹配：结构、动词、量化、日期体检，技能关键词匹配打分，输出改写示例报告。
---

# 简历诊断与 JD 匹配（resume-reviewer）

一个本地命令行简历优化 agent：先用完全确定性的规则给简历做“体检”（章节结构、要点动词、量化成果、日期与联系方式规范、错别字、无关信息），再按岗位描述（JD）做技能关键词加权匹配打分，得到四维评分卡和优化路线图，最后把最差的要点交给模型做“差 → 好”逐条改写示例。除 `anthropic` 外只用标准库。

## 它能做什么

- **章节结构检测**：中英文标题都认（基本信息 / 教育 / 工作经历 / 项目 / 技能 / 证书 / 自我评价），输出结构完整度评分，缺教育、经历、技能等关键章节时给警告
- **内容质量规则**（10 条 RULE_SPECS）：要点强动词开头、量化成果逐条判定、每段经历要点数（<3 偏少 / >8 偏多）、第一人称、中英混排、常见错别字、超长要点（>60 字）、日期格式不一致（2026.10 / 2026-10 / 2026年10月）、联系方式缺失或写法不规范、身高婚姻等无关信息
- **JD 匹配**：从 JD 提取技术词 / 中文技能词 / 工具词（加权 1.0 / 0.8 / 0.6），与简历词频对比，输出匹配度评分、命中技能表、缺失技能表、可迁移技能提示
- **四维评分卡**：结构 25 / 内容 30 / 量化 25 / 匹配 20，公式公开写在报告里，每个维度给提升点
- **逐条改写示例**：模型给出“改写前 → 改写后（带量化）+ 理由”，`--offline` 时退化为确定性改写指导（不编造数字，用【补数字】占位）
- **优化路线图**：按 严重 / 警告 / 建议 三档排出“今天就改 → 本周内 → 长期积累”
- **离线自检**：`--self-test` 不联网、不需要 key，覆盖全部确定性逻辑

## 怎么用

### 第一步：安装依赖

```powershell
# Windows PowerShell
cd agents/resume-reviewer
py -3 -m pip install -r requirements.txt
```

```bash
# macOS / Linux
cd agents/resume-reviewer
python3 -m pip install -r requirements.txt
```

### 第二步：设置 API key（只有调用模型改写时需要）

```powershell
# Windows PowerShell（当前窗口有效）
$env:ANTHROPIC_API_KEY="你的key"
```

```bash
# macOS / Linux
export ANTHROPIC_API_KEY=你的key
```

加 `--offline` 可以完全不设 key、不联网，只做确定性诊断。

### 第三步：运行诊断

```powershell
# Windows PowerShell
py -3 agent.py 简历.md --jd 数据分析师JD.txt --role 数据分析师 --output 诊断报告.md
```

```bash
# macOS / Linux
python3 agent.py 简历.md --jd 数据分析师JD.txt --role 数据分析师 --output 诊断报告.md
```

报告默认写到当前目录的 `review-report.md`。

## 常用示例

```bash
# 1. 离线诊断：不调用模型、不花钱，输出评分卡 + 问题清单 + 改写指导
python3 agent.py 简历.md --offline

# 2. 带 JD 一起看，得到技能匹配度、命中/缺失技能表和可迁移提示
python3 agent.py 简历.md --jd jd.txt --role "Java 后端开发工程师" --offline

# 3. 完整诊断 + 模型改写示例，报告写到指定文件
python3 agent.py 简历.md --jd jd.txt --role 数据分析师 --output 诊断报告.md

# 4. 走第三方 Anthropic 兼容网关时，用 --model 指定网关认得的模型名
python3 agent.py 简历.md --jd jd.txt --model qwen3.8-max --output 诊断报告.md
```

第 1 条适合投递前快速自查；第 2 条针对具体岗位看差距；第 3 条额外产出“差 → 好”改写示例与亮点风险总结；第 4 条用于官方 API 之外的网关。

## 实现方式

体检在前、模型在后的两段式流程：

1. **解析与统计**：`parse_sections()` 用 `match_section()` 认中英文章节标题并把正文、要点归位；`split_experience_blocks()` 按条目标题把经历切成若干段，统计每段要点数；`date_styles()`、`quant_kinds()` 统计日期写法与量化类型。
2. **规则体检**：`RULE_SPECS` 表里的 10 条规则（`check_bullet_verbs` / `check_quantification` / `check_bullet_count` / `check_first_person` / `check_mixed_language` / `check_typos` / `check_long_bullet` / `check_date_format` / `check_contact` / `check_irrelevant`）由 `analyze_resume()` 统一执行，输出按 严重 → 警告 → 建议 排序的 Issue 列表。
3. **JD 匹配**：`find_skill_keywords()` 从 JD 抽技能词（技术 1.0 / 技能 0.8 / 工具 0.6 三档权重），`match_jd()` 算加权覆盖率得到匹配度，`transferable_hints()` 给缺失技能提可迁移建议。
4. **评分卡与报告**：`score_card()` 按公开公式算结构 25 / 内容 30 / 量化 25 / 匹配 20，`improvement_points()` 给每个维度提升点，`build_report()` 汇总评分卡、结构体检、问题清单、改写示例、JD 匹配表、优化路线图。
5. **模型改写**：`rewrite_with_model()` 只把最差的要点（≤8 条）发给模型，要求返回 JSON（`rewrites` / `summary` / `match_advice`），`parse_rewrites_json()` 解析失败不崩溃，原文进报告附录；离线模式用 `offline_rewrites()` 给确定性改写指导。

模型调用带三级参数降级 `call_model()`：先试 `beta.messages.create`（adaptive thinking + server-side fallback），失败退到 `messages.create(thinking=...)`，再失败退到最基础调用，兼容不认新参数的第三方网关。

## 文件清单

- `agent.py` — 主程序：章节解析、10 条体检规则、JD 技能匹配、评分卡、模型改写、报告生成、CLI 与离线自检
- `README.md` — 本说明
- `requirements.txt` — Python 依赖（仅 `anthropic>=0.112.0`）

## 常见问题

**1. 没有 ANTHROPIC_API_KEY 能跑吗？**
能。加 `--offline` 就只跑确定性诊断和改写指导，完全不联网、不花钱。不加时如果检测不到 key，会打印中文提示（含 macOS `export` 与 PowerShell `$env:` 写法）并退出，不会卡住。

**2. 简历是 .txt 还是 .md 都支持吗？**
都支持。章节识别同时认「## 教育背景」这类 Markdown 标题和纯文本标题（教育背景 / Education / 个人评价 等中英文写法）。

**3. 为什么量化分这么低？**
量化分 = 25 × 有量化要点占比。HR 平均 10 秒看一份简历，只写“负责 XX”没有数字基本不加分，每条要点至少带一个数字（规模、提升幅度、耗时、覆盖量、成本）。

**4. 改写示例里的【补数字】是什么？**
模型和离线规则都不会编造业绩数据，信息不足时用【补数字：规模或提升幅度】占位，提醒你填真实数据。

**5. 走第三方兼容网关报错怎么办？**
用 `--model` 指定网关认得的模型名，例如 `--model qwen3.8-max`；`call_model()` 会自动降级参数，网关不认 `thinking`/`fallbacks` 也能跑通。

**6. 没给 JD 会怎样？**
诊断照常进行，匹配维度按简历技能关键词丰富度估算并在报告里注明；建议带上目标岗位 JD，这样才有命中/缺失技能表和可迁移提示。
