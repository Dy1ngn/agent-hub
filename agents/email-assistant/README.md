---
name: email-assistant
description: 解析 .eml/.mbox/.txt 邮件，做摘要、优先级判断、待办提取与回复起草，输出中文报告。
---
# 邮件助手（email-assistant）

一个本地命令行邮件处理 agent：先用标准库 `email` 包把邮件拆干净（元信息、正文、附件、引用线程），跑优先级启发式和待办/日期/金额提取，再按需让模型写摘要、行动项和回复草稿，最后汇成一份中文 Markdown 报告。批量丢一个目录进去也能逐封出卡片 + 汇总统计。除 `anthropic` 外只用标准库。

## 它能做什么

- **邮件解析**：支持 `.eml`（单封）、`.mbox`（整个邮箱）、`.txt`（纯文本邮件），提取发件人/收件人/抄送/主题/日期/Message-ID/引用线程头（In-Reply-To、References），multipart 只取 `text/plain` 与 `text/html`，HTML 正文用自写的 `HTMLParser` 子类转纯文本
- **引用剥离**：去掉 `> ` 引用行，以及 `On ... wrote:`、`-----Original Message-----` 之后的转发内容，并统计剥离了多少行
- **优先级启发式**：紧急词表（urgent/紧急/尽快/ASAP/今天内/截止/deadline）、问号数量、是否点名收件人、截止日期正则（含「周X前」「X月X日」「YYYY-MM-DD」），给出 高/中/低 与理由
- **关键信息提取**：日期时间、金额（￥/$/元/USD…）、电话、链接、待办句（「请/麻烦/需要/记得/下周」开头的祈使句）
- **摘要与草稿**：模型生成 3 句内摘要、行动项清单（事项/截止/负责人），`--draft` 时按 正式/友好/简洁 三种语气起草回复并逐条回应对方问题
- **离线模式**：`--offline` 与 `--self-test` 完全确定性、不需要 API key、不联网

## 怎么用

### 第一步：安装依赖

```powershell
# Windows PowerShell
cd agents/email-assistant
py -3 -m pip install -r requirements.txt
```

```bash
# macOS / Linux
cd agents/email-assistant
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

也支持 `ANTHROPIC_AUTH_TOKEN`。走第三方兼容网关（如阿里云）时不需要官方 key，运行时用 `--model` 指定网关认得的模型名，例如 `--model qwen3.8-max`。

### 第三步：处理邮件

```powershell
# Windows PowerShell：批量解析目录并起草回复
py -3 agent.py ./邮件目录 --draft --tone 正式 --output 邮件报告.md
```

```bash
# macOS / Linux：先跑离线确定性模式看看效果
python3 agent.py ./邮件目录 --offline --output 邮件报告.md
```

报告默认写到当前目录的 `email-report.md`，用编辑器打开即可。

## 常用示例

```bash
# 1. 整个 mbox 邮箱：解析 + 摘要 + 优先级 + 待办（不需要 key）
python3 agent.py 收件箱.mbox --offline

# 2. 目录里所有邮件，按「友好」语气起草回复
python3 agent.py ./收件 --draft --tone 友好 --output 本周邮件.md

# 3. 单封 .eml，走第三方网关指定模型名
python3 agent.py 报价单.eml --model qwen3.8-max --draft --tone 简洁

# 4. 跑一遍离线自检（不联网、不需要 key）
python3 agent.py --self-test
```

## 实现方式

1. **收集输入** `collect_inputs()`：文件/目录递归收集 `.eml/.mbox/.txt`，不支持的类型给提示。
2. **解析邮件** `parse_file()` 分发到 `parse_eml_file()` / `parse_mbox_file()` / `parse_txt_file()`，统一由 `parse_message()` 抽出元信息、正文（`decode_part_text()` 处理字符集与编码差异）和附件清单（文件名/类型/大小）。
3. **确定性分析** `analyze_card()`：`strip_quotes()` 剥引用并统计行数 → `compute_priority()` 打优先级分 → `extract_info()` 抽日期/金额/电话/链接/待办。
4. **批量汇总** `aggregate()`：总邮件数、高/中/低分布、待办总数、附件总数。
5. **模型生成**（可选）`analyze_with_model()`：逐封调用 `call_model()`（三级降级，兼容官方 API 与第三方网关），要求返回 JSON `{"summary":"","actions":[...],"reply":""}`；`parse_model_json()` 解析失败时原文进报告附录，结果用 `deterministic_analysis()` 兜底。
6. **输出报告** `build_report()`：每封邮件一张卡片（元信息表/附件/摘要/优先级/待办/草稿回复/正文），末尾汇总统计与附录。

## 文件清单

| 文件 | 说明 |
| --- | --- |
| `agent.py` | 全部实现：解析、启发式规则、模型调用、报告渲染、`--self-test` |
| `README.md` | 本介绍文件 |
| `requirements.txt` | 依赖清单（仅 `anthropic`） |

## 常见问题

**Q1：没有 API key 能用吗？**
能。加 `--offline` 就是纯确定性模式：解析、优先级、待办、汇总照常出，只是摘要取正文前 3 句、草稿用模板。`--self-test` 也不需要 key。

**Q2：走第三方兼容网关怎么配？**
不需要改代码，运行时指定网关认得的模型名即可：`python3 agent.py 邮件.txt --model qwen3.8-max`。`call_model()` 会自动从「thinking + fallbacks」逐级降到最朴素的 `messages.create`，网关不认新参数也不会崩。

**Q3：HTML 邮件正文是乱的怎么办？**
正文提取优先取 `text/plain`；只有 HTML 时用 `_HTMLTextExtractor`（跳过 `script/style`）+ 正则去残留标签转纯文本。极端畸形 HTML 会走正则兜底，不会崩溃。

**Q4：回复里的旧邮件内容被一起算了怎么办？**
`strip_quotes()` 会去掉 `> ` 引用行和 `On ... wrote:`、`-----Original Message-----` 之后的整段转发内容，报告正文里注明剥离了多少行；摘要、待办、优先级都只看剥离后的正文。

**Q5：模型返回的不是 JSON 会怎样？**
不会崩。`parse_model_json()` 解析失败时，该封邮件改用确定性结果（前 3 句摘要 + 待办清单 + 模板草稿），模型原文原样放进报告「附录」，终端也会提示有几封没解析成功。

**Q6：`.txt` 文件没有邮件头能处理吗？**
能。如果开头是 `Subject:` / `From:` 这类邮件头就按邮件头解析；否则整篇当正文，主题取文件名。mbox 里一封一封的邮件会被逐封拆成卡片。
