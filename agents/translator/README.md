---
name: translator
description: 多语言批量翻译助手，保护代码与占位符、按术语表统一术语，支持 Markdown、JSON、CSV。
---

# 多语言翻译助手（translator）

一个本地命令行翻译 agent：先把不该翻译的内容（代码块、行内代码、链接 URL、图片、`{name}`、`%s`、`<tag>`、`{{var}}`）抽成占位符，按段落/句子边界分批交给模型只输出译文，译后无损还原占位符，再按术语表统一术语。Markdown/文本整篇翻译，JSON 只翻字符串 value、保留 key 与结构，CSV 可按列选择翻译。除 `anthropic` 外只用标准库。

## 它能做什么

- **格式保护**：翻译前后对代码块、行内代码、链接地址、图片、各类占位符做抽取与还原，还原后与原文逐字符一致，模型碰不到它们
- **分批切分**：每批不超过 `--batch-chars`（默认 6000 字符），优先在段落、句子边界断开，硬切时也不会切断占位符
- **术语表强制替换**：`--glossary 术语表.json` 里的术语译前锁成占位符、译后兜底替换，保证「Kubernetes」不会被译成别的说法
- **结构化文件**：JSON 递归只翻译字符串 value（key、数字、布尔、null 与层级不动）；CSV/TSV 表头保留，`--columns` 按列名或序号选择要翻译的列
- **中文 Markdown 翻译报告**：`translation-report.md` 给出统计概览（字符/词/批次）、文件清单、批次明细、失败批清单
- **稳健降级**：模型回复不是约定 JSON 时该批保留原文并在报告标注，程序不崩；`--offline` 不联网也能跑通全流程

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
cd agents/translator
py -3 -m pip install -r requirements.txt
```

```bash
# macOS / Linux
cd agents/translator
python3 -m pip install -r requirements.txt
```

### 第三步：运行翻译

```powershell
# Windows PowerShell
py -3 agent.py ./docs --to 英语 --from 中文 --out-dir ./translated
```

```bash
# macOS / Linux
python3 agent.py ./docs --to 英语 --from 中文 --out-dir ./translated
```

译文按相对目录结构写进 `--out-dir`（默认 `./translated`），同目录下生成 `translation-report.md`。

## 常用示例

```bash
# 1. 整个目录翻成英语，保持目录结构输出
python3 agent.py ./docs --to 英语 --from 中文 --out-dir ./translated

# 2. 只翻 CSV 的指定列，表头保持不译
python3 agent.py table.csv --to 英语 --columns 名称,备注

# 3. 带术语表翻 JSON：术语译前锁定、译后兜底，key 不动
python3 agent.py data.json --to 日语 --glossary 术语表.json

# 4. 走第三方 Anthropic 兼容网关时，用 --model 指定网关模型名
python3 agent.py ./docs --to 英语 --model qwen3.8-max
```

第 1 条适合批量翻译文档；第 2 条避免价格、编号这类不该翻的列被改写；第 3 条保证产品名、专有名词前后一致；第 4 条用于官方 API 之外的网关。

## 实现方式

1. **保护**：`TextProtector.protect()` 按顺序把代码块、行内代码、图片、HTML 标签、`{{var}}`/`{name}`/`%s`/`${x}` 抽成 `\x00N\x00` 占位符，`restore()` 译后还原（容忍占位符被加空格的轻微变形）。术语表 `apply_glossary()` 也走同一套占位符：术语在译前就变成占位符，模型根本看不到它。
2. **分批**：`split_units()` 先按空行分段落、再按句末标点分句，超长的用 `_hard_split()` 硬切且避开占位符；`split_batches()` 贪心打包，保证每批 ≤ `--batch-chars` 且所有批次拼回等于原文。
3. **翻译**：`Translator.translate_plain()` 每批调 `call_model()`（三级参数降级，兼容官方 API 与第三方网关），要求模型只返回 `{"translation": "..."}`；`parse_translation_json()` 解析失败时该批保留原文、记入失败批。`translate_cells()` 负责 JSON value 与 CSV 单元格，走 `{"translations": [...]}` 批量协议。
4. **结构化**：`translate_json_text()` 用 `walk_json()` 递归只处理字符串 value；`translate_csv_text()` 用标准库 `csv` 读写，表头不译、按 `--columns` 选列。
5. **报告**：`build_report()` 汇总每个文件的字符/词/批次/失败批，生成中文 Markdown 报告。

## 文件清单

- `agent.py` — 主程序：占位符保护、术语表、分批、JSON/CSV 翻译、模型调用、报告生成、CLI 与离线自检
- `README.md` — 本说明
- `requirements.txt` — Python 依赖（仅 `anthropic>=0.112.0`）

## 常见问题

**1. 没有 ANTHROPIC_API_KEY 能跑吗？**
能。加 `--offline` 就跑纯确定性流程：占位符保护、术语表替换、分批、文件输出、翻译报告都照常执行，只是译文保留原文（不花一分钱）。不加 `--offline` 又检测不到 key 时，会打印含 macOS 与 PowerShell 写法的中文提示并退出。

**2. 走第三方 Anthropic 兼容网关怎么配？**
设置网关要求的 `ANTHROPIC_BASE_URL` 和 `ANTHROPIC_AUTH_TOKEN`（或 `ANTHROPIC_API_KEY`），再用 `--model` 指定网关支持的模型名，例如 `--model qwen3.8-max`。`call_model()` 三级参数降级会自动绕过网关不认识的 thinking / fallbacks 参数。

**3. 术语表文件长什么样？**
一个 JSON 对象 `{"源词": "译法"}`，也支持 `{"terms": {"源词": "译法"}}`。术语在译前被锁成占位符，译后兜底再扫一遍，所以专有名词不会被模型意译。

**4. 模型把占位符弄坏了会怎样？**
还原时能容忍占位符被加空格这类轻微变形；万一某批回复压根不是约定 JSON，该批直接保留原文，报告的「失败批」一节会列出来，人工补译即可，程序不会崩。

**5. JSON / CSV 会不会把结构翻坏？**
不会。JSON 只对字符串 value 做替换，key、数字、布尔、null、数组与嵌套层级原样保留（自检里有断言）；CSV 用标准库读写，表头不译，列数行数不变。

**6. Windows 上 `python` / `python3` 没反应？**
本机的 `python` 可能是 Microsoft Store 的占位桩，统一改用 `py -3 agent.py ...`。程序内置 UTF-8 控制台兜底，中文输出不会乱码。
