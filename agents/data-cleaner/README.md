---
name: data-cleaner
description: CSV/TSV/JSON 表格质量体检与清洗，出中文质量报告，支持异常值、重复、格式问题检测。
---

# 表格数据清洗与质量报告（data-cleaner）

给表格数据做一次「体检 + 干洗」：确定性规则检查缺失、重复、异常值、格式不一致、常量列、高基数列与类别分布，再按规则清洗（去空白、全角转半角、统一空值、去重、数值还原、日期统一）。默认 dry-run 只出报告不碰数据，加 `--apply` 才写出清洗结果。除 `anthropic` 外只用标准库。

## 它能做什么

- **读取**：CSV / TSV / JSON，自动探测编码（utf-8 / gbk 回退）与分隔符（逗号、制表符、分号、竖线）；JSON 支持 `list[dict]` 和 dict 列式
- **列类型推断**：整数 / 浮点数 / 布尔 / 日期 / 日期时间 / 文本，按非空值命中率 ≥90% 判定
- **质量检查**：缺失率（按严重度分级）、完全重复行（整行哈希）、疑似重复（关键列组合）、异常值（IQR 1.5 倍与 z-score>3 双口径）、前后空白、全角半角混用、日期多格式混用、数字带单位/千分位、常量列、高基数列、类别分布 top
- **清洗操作**（每条都记进操作日志）：去前后空白、全角转半角、统一空值（`NA`/`N/A`/`null`/`-`/`无`… → 空）、去完全重复行、数值列去千分位/货币符号、日期统一 `YYYY-MM-DD`、布尔统一 `true/false`
- **统计摘要**：数值列 min/max/mean/median/std/P25/P75，文本列长度分布，类别列分布
- **输出**：中文 Markdown 质量报告（概览、列类型、按严重度排序的问题清单、统计摘要、操作日志、建议）；`--apply` 额外写出清洗后的原格式文件与 `clean-log.json`
- **可选模型解读**：`--explain` 让模型读质量问题给治理建议，JSON 解析失败原文进附录，绝不影响确定性流程

## 怎么用

### 第一步：安装依赖

```powershell
# Windows PowerShell
cd agents/data-cleaner
py -3 -m pip install -r requirements.txt
```

```bash
# macOS / Linux
cd agents/data-cleaner
python3 -m pip install -r requirements.txt
```

### 第二步：设置 API key（只有 `--explain` 需要）

```powershell
# Windows PowerShell（当前窗口有效）
$env:ANTHROPIC_API_KEY="你的key"
```

```bash
# macOS / Linux
export ANTHROPIC_API_KEY=你的key
```

不做模型解读的话这步可跳过，体检与清洗完全离线。

### 第三步：体检（默认不动数据）

```powershell
# Windows PowerShell
py -3 agent.py sales.csv --report 质量报告.md
```

```bash
# macOS / Linux
python3 agent.py sales.csv --report 质量报告.md
```

确认报告没问题后再加 `--apply` 真正清洗。

## 常用示例

```bash
# 1. 只体检不改数据（dry-run），出一份质量报告
python3 agent.py sales.csv

# 2. 体检 + 清洗：清洗结果写到 clean/，附 clean-log.json 操作明细
python3 agent.py sales.csv users.json --apply --output-dir clean

# 3. TSV 也能处理，报告换个位置
python3 agent.py data.tsv --report 体检.md

# 4. 让模型解读报告并给治理建议（需要 API key；网关用 --model 指定模型名）
python3 agent.py sales.csv --explain --model qwen3.8-max --report 质量报告.md
```

第 2 条会保持原格式输出（CSV 还是 CSV、JSON 还是 JSON），被去掉的重复行和每个单元格的前后值都在 `clean-log.json` 里可追溯。

## 实现方式

1. **读取层** `detect_encoding()`（utf-8 → gbk → latin-1 回退）、`detect_delimiter()`（Sniffer + 次数兜底）、`load_table()`（CSV/TSV 用 `csv` 模块，JSON 支持行式/列式）。
2. **推断层** `infer_column_type()` 按 布尔→整数→浮点→日期时间→日期→文本 的优先级，用 `INT_RE` / `FLOAT_RE` / `DATE_FORMATS` 计算命中率，阈值 `TYPE_THRESHOLD=0.9`。
3. **检查层** `analyze_table()` 产出带严重度的问题清单：缺失率分级、整行哈希查重复、`key_columns()` 疑似重复、`detect_outliers()` 双口径异常值、格式不一致、常量列、高基数列。
4. **清洗层** `clean_table()` 按「去空白 → 全角转半角 → 统一空值 → 数值还原 → 日期归一 → 布尔归一 → 去重」顺序处理，每步都写操作日志；`percentile()` 自实现分位数保证结果跨平台一致。
5. **展示层** `build_report()` 拼中文 Markdown，`write_cleaned()` 按原格式落盘，`--apply` 追加写 `clean-log.json`。
6. **模型层（可选）** `explain_with_model()` 经三级降级 `call_model()` 拿 JSON，`parse_explain_json()` 解析失败返回 `None`，原文进报告附录。

## 文件清单

| 文件 | 说明 |
| --- | --- |
| `agent.py` | 全部实现：读取、推断、检查、清洗、统计、报告、CLI、`--self-test` |
| `README.md` | 本说明 |
| `requirements.txt` | 依赖清单（仅 `anthropic`） |

## 常见问题

**Q1：dry-run 和 `--apply` 到底差在哪？**  
dry-run 只生成报告，一个数据文件都不写；`--apply` 才会在 `--output-dir`（默认 `clean/`）写出清洗后的文件和 `clean-log.json`。原始文件永不修改。

**Q2：清洗会不会把我的数据洗坏？**  
每个单元格的改动都记在操作日志里（行号、列名、原值、新值），去重也只删「清洗后整行完全一致」的行。先 dry-run 看报告，再决定要不要 apply。

**Q3：日期被推断成整数怎么办？**  
纯 8 位数字（如 `20261028`）既像整数也像日期，工具优先判整数。如果这列确实是日期，可以先把格式改成带分隔符（`2026-10-28`）再跑。

**Q4：GBK 老文件能读吗？**  
能，编码探测顺序是 utf-8(-sig) → gbk → latin-1；清洗结果按原编码写回（utf-8 系列带 BOM，Excel 打开不乱码）。

**Q5：不设置 API key 能用吗？**  
可以。只有 `--explain` 需要 key；体检、清洗、`--self-test` 全部离线，加 `--offline` 还能显式禁止模型调用。

**Q6：想接第三方 Anthropic 兼容网关？**  
`--explain --model qwen3.8-max` 这样指定网关认得的模型名即可。模型调用走三级降级 `call_model()`，官方 API 和只认基础参数的网关都能跑。
