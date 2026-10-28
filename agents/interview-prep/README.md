---
name: interview-prep
description: 面试准备助手：内置分类题库、JD 岗位画像、可复现抽题、复习计划与模拟面试。
---

# 面试准备助手（interview-prep）

内置 42 道分类题库（算法与数据结构 / 系统设计 / 编程语言 / 项目深挖 / 行为STAR / HR反问，各分基础 / 进阶 / 挑战），按 `--type` / `--count` / JD 权重抽题（固定随机种子可复现），自动渲染 STAR / PREP 答题框架骨架；`--record` 记录练习并按 1/2/4/7/15 天间隔安排复习；`--mock` 让模型逐题模拟面试官追问、按「内容 / 结构 / 证据 / 表达」四维打分。除 `anthropic` 外只用标准库。

## 它能做什么

- **内置题库**：42 道题 × 6 个分类 × 3 档难度 × 标签，每题带「考察点」和「回答框架提示」
- **JD 岗位画像**：解析技能词（编程语言 / 框架 / 工具词表 + 中文技能词）、年限要求、职责动词，生成技能要求表并给题目分类加权
- **智能出题**：按类型与 JD 权重抽题，先保底每类 1 题再按权重补足，`--seed` 固定随机种子保证同样参数抽出同样题目
- **答题框架骨架**：STAR（情境 / 任务 / 行动 / 结果）、PREP（观点 / 理由 / 例子 / 重申）和要点式模板，`--offline` 也直接可用
- **练习与复习**：读写练习记录 JSON（题目 id、日期、自评分 1-5、备注），按 1/2/4/7/15 天间隔算出「今日该复习」清单和进步曲线
- **模拟面试**：`--mock` 逐题出追问、四维打分、给改进建议；模型 JSON 解析失败不崩溃，原文放报告附录
- **离线自检**：`--self-test` 覆盖题库、JD 解析、抽题、复习间隔、记录读写、JSON 兜底，不联网、不需要 API key

## 怎么用

### 第一步：安装依赖

```powershell
# Windows PowerShell
cd agents/interview-prep
py -3 -m pip install -r requirements.txt
```

```bash
# macOS / Linux
cd agents/interview-prep
python3 -m pip install -r requirements.txt
```

### 第二步：设置 API key（只有 --mock 模拟面试时需要）

```powershell
# Windows PowerShell（当前窗口有效）
$env:ANTHROPIC_API_KEY="你的key"
```

```bash
# macOS / Linux
export ANTHROPIC_API_KEY=你的key
```

只想出题、看框架和复习计划（`--offline`）可以跳过这一步。

### 第三步：生成准备文档

```powershell
# Windows PowerShell
py -3 agent.py --jd 岗位描述.txt --type 技术 --count 12 --output 面试准备.md
```

```bash
# macOS / Linux
python3 agent.py --jd 岗位描述.txt --type 技术 --count 12 --output 面试准备.md
```

## 常用示例

```bash
# 1. 按岗位描述出 12 道技术面题目（自动加权、可复现）
python3 agent.py --jd 岗位描述.txt --type 技术 --count 12 --output 面试准备.md

# 2. 离线准备行为面：只出题 + STAR 框架骨架，不调模型
python3 agent.py --type 行为 --offline --output 行为面准备.md

# 3. 模拟面试 + 练习记录：逐题出追问和四维评分，并排今日复习
python3 agent.py --jd 岗位描述.txt --mock --record 练习记录.json --output 面试准备.md

# 4. 换随机种子重抽一轮，或走第三方兼容网关
python3 agent.py --type 综合 --seed 7 --model qwen3.8-max
```

第 1 条是标准用法；第 2 条不花钱先把框架练熟；第 3 条适合每天练习后的复盘；第 4 条换题或接网关时用。

## 实现方式

确定性逻辑在前，模型只做语言生成：

1. **题库**：`build_bank()` 把内置 42 道题转成结构化条目（id / 分类 / 难度 / 标签 / 题目 / 考察点 / 框架），`TYPE_CATEGORIES` 定义每种面试类型覆盖哪些分类。
2. **JD 解析**：`parse_jd()` 调 `extract_skills()`（中文子串 + 英文词边界匹配技能词表）、`extract_years()`（年限正则）、`extract_verbs()`（职责动词），再按 `SKILL_CATEGORY_HINTS` 给题目分类加权，输出「岗位画像」。
3. **出题策略**：`select_questions()` 先每类保底 1 题（`check_coverage()` 做覆盖率检查），剩余名额用 `weighted_pick()` 按「分类权重 × 难度权重」抽取，全部随机数走 `random.Random(seed)`，同种子可复现。
4. **答题框架**：`render_framework()` 按题目 framework 字段渲染 STAR / PREP / 要点三套骨架。
5. **练习记录**：`load_record()` / `save_record()` 读写 JSON，`next_due_date()` 按 1/2/4/7/15 天间隔算下次复习日（第 n 次复习后用第 n 个间隔），`due_today()` 出「今日该复习」清单，`progress_stats()` 算进步曲线与提升幅度。
6. **模拟面试**：`mock_interview()` 让模型出追问并按 内容 / 结构 / 证据 / 表达 打分（1-5）给建议，返回 JSON；`parse_mock_json()` 解析失败不崩溃，原文进报告附录。
7. **模型调用降级**：`call_model()` 三级降级——先试 `beta.messages.create`（adaptive thinking + server-side fallback），失败退到 `messages.create(thinking=...)`，再失败退到最基础调用，兼容不认新参数的第三方网关。

## 文件清单

- `agent.py` — 主程序：内置题库、JD 解析、抽题策略、答题框架、练习记录与复习计划、模拟面试、报告生成、CLI 与离线自检
- `README.md` — 本说明
- `requirements.txt` — Python 依赖（仅 `anthropic>=0.112.0`）

## 常见问题

**1. 没有 ANTHROPIC_API_KEY 能跑吗？**
能。加 `--offline`（或不加 `--mock`）就只出题、渲染答题框架、排复习计划，完全不联网、不花钱。只有 `--mock` 需要 key，检测不到时会打印含 macOS 与 PowerShell 写法的中文提示并退出。

**2. 为什么同样命令每次出的题一样？**
抽题用固定随机种子（`--seed`，默认 42），保证可复现。想换一批题就换 `--seed`，或者加大 `--count`。

**3. 练习记录文件怎么写？**
一个 JSON 数组，每条 `{"id": "Q001", "date": "2026-10-28", "score": 4, "note": "备注"}`，自评分 1-5。文件不存在会自动从空表开始；用 `--record` 指定路径后，程序会读入并在结束时按格式统一写回（`--self-test` 里覆盖了读写与越界校验）。

**4. 复习间隔是怎么算的？**
按 1 / 2 / 4 / 7 / 15 天递进：第一次练习后第 1 天复习，复习完下一次间隔 2 天，再下一次 4 天……超过 5 次就固定 15 天。到期（含逾期）的题会进「今日该复习」清单，并显示逾期天数。

**5. 想让题目更贴合我的岗位？**
把真实的岗位描述存成 .txt 传给 `--jd`：技能词出现次数、年限、职责动词都会进「岗位画像」，架构 / 分布式类词会给系统设计题加权，团队协作类词会给行为题加权。JD 里没提的方向权重就是 1.0，仍会保底出题。

**6. `--mock` 的模型回复乱了怎么办？**
模型回复不是约定 JSON 时程序不会崩溃：跳过该题的打分、原文放进「附录：模型原始回复」，其余题目照常生成。也可以加 `--offline` 先用框架骨架练习。
