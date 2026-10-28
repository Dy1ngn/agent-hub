---
name: expense-tracker
description: 自然语言一句话记账，本地 JSON 账本加分类统计与预算告警，输出带 ASCII 图表的中文月报。
---

# 自然语言记账助手（expense-tracker）

用一句话记一笔账（“午饭 35 元”“打车￥45.5 昨天 支付宝”），确定性规则自动解析金额、日期、分类、商户摘要和支付方式，存进本地 JSON 账本；再按日/月做统计与预算对比，生成带 ASCII 柱状图的中文 Markdown 开销报告。除 `anthropic` 外只用标准库。

## 它能做什么

- **自然语言解析**：金额（`35元` / `￥35` / `$35.5` / `35.5块`）、日期（今天/昨天/前天/周X/10月27日/2026-10-27/X号）、支付方式（微信/支付宝/现金/卡）、9 大分类词典（每类 20+ 关键词）；解析不出的字段标 `None` 并给出中文提示（例如中文数字金额会提醒改用阿拉伯数字）
- **账本存储**：JSON 文件，追加时自动按「日期+金额+分类+摘要」去重，带 id 与时间戳，原子写（临时文件 + `os.replace`）不会写坏
- **统计分析**：总额、笔数、日均、消费频次、分类占比、top 商户、最大单笔、环比上月、每日分布
- **预算告警**：读 `category,limit` 预算表，超支类别排序置顶并给出异常消费提示（超预算、超常大额、单日爆发）
- **ASCII 图表**：分类占比柱状图、每日趋势图直接渲染进 Markdown，不需要 matplotlib
- **可选模型归一化**：`--ask-model` 让模型把模糊记账归一成结构化 JSON，解析失败自动退回规则结果，不影响记账

## 怎么用

### 第一步：安装依赖

```powershell
# Windows PowerShell
cd agents/expense-tracker
py -3 -m pip install -r requirements.txt
```

```bash
# macOS / Linux
cd agents/expense-tracker
python3 -m pip install -r requirements.txt
```

### 第二步：设置 API key（只有 `--ask-model` 需要）

```powershell
# Windows PowerShell（当前窗口有效）
$env:ANTHROPIC_API_KEY="你的key"
```

```bash
# macOS / Linux
export ANTHROPIC_API_KEY=你的key
```

只做记账和报表可以直接跳过这一步（所有确定性功能都离线可用）。

### 第三步：记账和出报表

```powershell
# Windows PowerShell
py -3 agent.py add "午饭 35 元" --book 账本.json
py -3 agent.py report --month 2026-10 --output 月报.md
```

```bash
# macOS / Linux
python3 agent.py add "午饭 35 元" --book 账本.json
python3 agent.py report --month 2026-10 --output 月报.md
```

## 常用示例

```bash
# 1. 一次记多笔（支持 35元/￥35/$35.5/35.5块、今天/昨天/周X/X号等写法）
python3 agent.py add "午饭 35 元" "打车￥45.5 昨天 支付宝" "电影票 88 元"

# 2. 批量导入：每行一条记账原文（重复条目自动跳过）
python3 agent.py add --file 记账文本.txt --book 账本.json

# 3. 出月报并做预算对比（超支类别会置顶告警）
python3 agent.py report --month 2026-10 --budget 预算.csv --output 月报.md

# 4. 查看某分类明细，或导出 CSV 给 Excel
python3 agent.py list --category 餐饮
python3 agent.py export --csv 账本.csv
```

第 1、2 条是日常记账入口；第 3 条的预算表格式是两列 CSV：`category,limit`（如 `餐饮,100`）；第 4 条用于核对明细和二次分析。走第三方兼容网关时加 `--model qwen3.8-max` 之类的模型名即可。

## 实现方式

1. **解析层** `parse_entry()` 依次调用 `parse_date()`（8 种日期写法，都基于可注入的 `today` 便于测试）、`parse_amount()`（5 组金额正则 + 裸数字兜底 + 中文数字提示）、`parse_pay_method()`、`match_category()`（9 类关键词词典），把命中的片段从文本里摘掉后剩下的就是商户/摘要。
2. **存储层** `add_entries()` → `make_entry()` 生成 sha1 短 id，用 `entry_key()` 判重，`save_book()` 原子写 JSON。
3. **统计层** `summarize()`（分类/商户/每日/日均/频次）、`filter_month()`、`previous_month()`、`budget_check()`、`detect_anomalies()` 全部是纯函数。
4. **展示层** `render_bar_chart()` / `render_trend_chart()` 画 ASCII 图，`build_report()` 拼中文 Markdown（概览、分类、趋势、预算、异常、明细）。
5. **模型层（可选）** `normalize_with_model()` 经三级降级的 `call_model()` 请求 JSON，`parse_normalize_json()` 解析失败返回 `None`，`merge_normalized()` 只补空缺字段，保证规则结果永远可用。

## 文件清单

| 文件 | 说明 |
| --- | --- |
| `agent.py` | 全部实现：解析、账本、统计、图表、报告、CLI、`--self-test` |
| `README.md` | 本说明 |
| `requirements.txt` | 依赖清单（仅 `anthropic`） |

## 常见问题

**Q1：跑 `add` 说“未识别到日期”，是出错了吗？**  
不是。没写日期就按今天记账，`[提示]` 只是提醒你核对；想指定日期就把“昨天 / 10月27日 / 2026-10-27 / 27号 / 周一”写进那句话。

**Q2：为什么“午饭三十五元”记不上？**  
暂不支持中文数字金额，会明确提示改用阿拉伯数字（“午饭 35 元”）。这是为了避免“三块五”被误读成整数。

**Q3：同一笔账重复记了两次怎么办？**  
`add` 会按「日期+金额+分类+摘要+支付方式」自动去重并提示跳过，不需要手工清理。

**Q4：不设置 API key 能用吗？**  
完全可以。只有 `--ask-model` 会调用模型；`add / report / list / export` 与 `--self-test` 都是纯确定性、离线可用。加 `--offline` 可显式禁止任何模型调用。

**Q5：预算表怎么写？**  
两列 CSV，第一行表头随意（推荐 `category,limit`），例如：

```csv
category,limit
餐饮,1500
交通,500
购物,800
```

**Q6：想接第三方 Anthropic 兼容网关？**  
用 `--model` 指定网关认得的模型名即可，例如 `python3 agent.py report --ask-model --model qwen3.8-max`。模型调用带三级降级（`call_model()`），官方 API 和只认基础参数的网关都能跑。
