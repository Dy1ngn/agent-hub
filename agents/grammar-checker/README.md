---
name: grammar-checker
description: 规则表加模型语义校对，检查中英文错别字、标点、空格、语法与表达，输出中文 Markdown 校对报告。
---

# 中英文校对助手（grammar-checker）

一个本地命令行文字校对 agent：先用 17 条确定性规则扫一遍（的/地/得、在/再、做/作、重复词、重复标点、标点混用、缺空格、拼写、there/their、超长句……），再让模型做语义级校对（语法、搭配、表达不地道），最后合并去重成一份按级别分组的中文报告。除 `anthropic` 外只用标准库。

## 它能做什么

- **规则表扫描**：17 条不看模型也能跑的规则，中文（的/地/得、在/再、做/作、重复词、标点混用、缺空格）、英文（句首小写、双空格、重复单词、30 组常见拼写误用、there/their）、通用（重复标点、全角/半角、行尾空白、超长句、TODO 残留）
- **模型语义校对**：按每批不超过 20000 字符把带行号的文本发给模型，要求返回严格 JSON，找规则覆盖不到的语法、搭配、语序问题
- **分级报告**：按 错误 → 警告 → 提示 分组，每条给出位置（`文件:行号`）、问题说明和可直接照抄的修复建议
- **离线自检**：`--self-test` 在临时目录造样本逐条验证 17 条规则，不联网也不需要 API key
- **可控开关**：`--lang` 选语言规则族、`--min-level` 过滤噪音、`--no-space-between` 关掉缺空格检查、`--offline` 纯规则模式

## 怎么用

### 第一步：设置 API key（只跑规则可以跳过）

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
cd agents/grammar-checker
py -3 -m pip install -r requirements.txt
```

```bash
# macOS / Linux
cd agents/grammar-checker
python3 -m pip install -r requirements.txt
```

### 第三步：运行校对

```powershell
# Windows PowerShell：先跑不花钱的纯规则校对
py -3 agent.py 文章.md --offline --output 校对报告.md
```

```bash
# macOS / Linux：规则 + 模型语义校对
python3 agent.py 文章.md --output 校对报告.md
```

报告默认写到当前目录的 `proofread-report.md`，用编辑器打开即可。

## 常用示例

```bash
# 1. 只跑规则，快速自查（不联网、不花钱）
python3 agent.py 文章.md --offline

# 2. 只看警告及以上，减少提示类噪音
python3 agent.py ./docs --min-level 警告

# 3. 只跑英文规则，适合英文文档
python3 agent.py README.en.md --lang 英文 --offline

# 4. 走第三方 Anthropic 兼容网关时，换模型名即可
python3 agent.py 文章.md --model qwen3.8-max --output 校对报告.md
```

第 1 条适合写作过程中反复自查；第 2 条过滤掉「提示」级的小问题；第 3 条避免中文规则在纯英文文档上刷屏；第 4 条用于官方 API 之外的网关。

## 实现方式

两段式流程：

1. **规则扫描**：`collect_sources()` 递归收集文本文件（跳过二进制），`analyze_source()` 对每个文件逐行跑 `RULE_SPECS` 规则表里的检查函数（`check_de_di_misuse`、`check_zai_misuse`、`check_spelling`……）。这一步完全确定性，同样的文本永远得到同样的结果。
2. **模型语义校对**：`build_semantic_batches()` 把文本按每批 ≤20000 字符切分、每行带绝对行号，`semantic_check()` 发给模型并要求只返回 JSON 数组（`line / severity / type / problem / suggestion`）。`parse_model_findings()` 解析失败的批次不会让程序崩溃，原始回复原样放进报告附尾的「模型补充意见」。
3. **合并出报告**：`dedupe_findings()` 去重 → `filter_by_level()` 按 `--min-level` 过滤 → `sort_findings()` 按级别排序 → `build_report()` 生成概览、错误、警告、提示、文件清单。

模型调用带三级参数降级 `call_model()`：先试 `beta.messages.create`（adaptive thinking + server-side fallback），失败退到 `messages.create(thinking=...)`，再失败退到最基础的调用，兼容不认新参数的第三方网关。

## 内置规则表

| ID | 级别 | 类别 | 检查内容 |
| --- | --- | --- | --- |
| GR001 | 提示 | 中文 | 的/地/得误用启发式（的+动词、地+名词） |
| GR002 | 警告 | 中文 | 在/再误用（「在见」「在也不会」「再家」） |
| GR003 | 警告 | 中文 | 做/作误用（「做用」「作饭」等 17 组） |
| GR004 | 警告 | 中文 | 重复词（「的的」「了了」「就就」） |
| GR005 | 提示 | 通用 | 重复标点（「？？」「！！」「。。」） |
| GR006 | 警告 | 中文 | 中文句子里混用半角 , . ? ! : ; |
| GR007 | 提示 | 中文 | 中英文之间缺空格（`--no-space-between` 可关） |
| GR008 | 提示 | 通用 | 全角数字/字母/全角空格混进正文 |
| GR009 | 提示 | 中文 | 中文行内连续空格 |
| GR010 | 提示 | 通用 | 行尾多余空格/制表符 |
| GR011 | 警告 | 英文 | 英文句子首字母小写 |
| GR012 | 提示 | 英文 | 英文行内双空格 |
| GR013 | 警告 | 英文 | 相邻重复单词（the the） |
| GR014 | 警告 | 英文 | 常见拼写误用（teh→the、recieve→receive 等 30 组） |
| GR015 | 警告 | 英文 | there/their 启发式误用 |
| GR016 | 提示 | 通用 | 超长句（中文 >60 字或英文 >25 词） |
| GR017 | 提示 | 通用 | TODO / FIXME / XXX / HACK 残留 |

`--list-rules` 会打印完整规则表，不需要 API key。

## 文件清单

- `agent.py` — 主程序：文件收集、17 条校对规则、模型语义校对、报告生成、CLI 与离线自检
- `README.md` — 本说明
- `requirements.txt` — Python 依赖（仅 `anthropic>=0.112.0`）

## 常见问题

**1. 没有 ANTHROPIC_API_KEY 能跑吗？**
能。加 `--offline` 就只跑 17 条规则，完全不联网、不花钱。不加 `--offline` 又检测不到 key 时，会打印中文提示（含 macOS 与 PowerShell 写法）并退出，不会卡住。

**2. 走第三方 Anthropic 兼容网关怎么配？**
设置网关要求的 `ANTHROPIC_BASE_URL` 和 `ANTHROPIC_AUTH_TOKEN`（或 `ANTHROPIC_API_KEY`），再用 `--model` 指定网关支持的模型名，例如 `--model qwen3.8-max`。`call_model()` 的三级参数降级会自动绕过网关不认识的 thinking / fallbacks 参数。

**3. 规则误报了怎么办？**
这些规则是文本启发式，天然有误报：比如「的的确确」已经做了排除，但「天地万物」这类构词仍可能撞上 GR001 的启发式；英文缩写 e.g./i.e. 已跳过句首大写检查，但少见缩写仍可能误判。先用 `--min-level` 和 `--lang` 降噪，逐条判断时以报告里的 `文件:行号` 为准，不要盲改。

**4. 只想查中文，不想让英文规则刷屏？**
加 `--lang 中文`，只保留中文类规则加通用规则；纯英文文档用 `--lang 英文`。默认 `auto` 会全部跑，适合中英混排的文档。

**5. 模型返回的不是合法 JSON 会怎样？**
程序不会崩。解析失败的批次会跳过自动合并，原始文本原样追加到报告末尾的「模型补充意见（未能解析为 JSON）」，可以人工看，也可以重跑那一批。

**6. Windows 上 `python` / `python3` 没反应？**
本机的 `python` 可能是 Microsoft Store 的占位桩，统一改用 `py -3 agent.py ...`。打印中文乱码时，程序已内置 UTF-8 控制台兜底（`sys.stdout.reconfigure`），无需额外设置。
