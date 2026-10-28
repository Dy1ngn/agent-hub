---
name: code-translator
description: 跨语言代码翻译：语言检测、80+ 术语映射、结构对照与语法自检，输出译文与中文翻译报告。
---

# 代码翻译助手（code-translator）

一个本地命令行代码翻译 agent：先确定性地识别源语言（扩展名 + 内容启发式）并做语法预检，再用内置 1000+ 组术语/API 对照（覆盖 python ↔ javascript ↔ typescript ↔ go ↔ java 五个方向）产出「映射命中清单」与「未映射 API 清单」，并对函数数/类数/注释数做翻译前后结构对照；离线模式直接给出确定性译文（Python 源会把缩进块转成花括号），在线模式让模型做正式翻译，最后对译文做目标语言语法自检，不合法就写 `.rej` 文件并告警，绝不静默通过。除 `anthropic` 外只用标准库。

## 它能做什么

- **语言检测**：`detect_language()` 先看扩展名（`.py/.js/.ts/.go/.java` 等），认不出时用内容启发式打分（`def/class`、`function/const/=>`、`package main/func`、`public class` 等特征）
- **语法预检**：Python 源码用 `ast.parse` 报出具体行号；其它语言做括号/引号平衡等启发式检查
- **术语对照表**：`TERMS` 表按「python / javascript / typescript / go / java」五列组织，覆盖 `print→console.log`、`len→.length`、`None→null`、`self.→this.`、`dict→Map/Object`、`f-string→模板字符串`、`json.dumps→JSON.stringify`、`.append(→.push(` 等，跨语言对统计超过 1000 条对照
- **映射命中与未映射 API**：`analyze_terms()` 列出源码里实际命中的对照（含次数、能否自动替换），`find_unmapped_apis()` 启发式找出内置 API 调用里对照表没覆盖的部分，方便人工确认
- **结构对照校验**：`count_structure()` 用目标语言正则统计函数/类/注释/import 数，`compare_structure()` 输出翻译前后差异表；`--keep-comments` 时注释行单独抽出交给模型，译后核对注释数量
- **离线确定性翻译**：`rule_translate()` 做术语级替换，Python → JS/TS/Go/Java 会额外把缩进块转成花括号（`def→function`、`class→class`、`elif→} else if`、`for x in range(n)→for (let i...`、类方法自动去掉 `self`、`__init__→constructor`、docstring 转注释）
- **目标语言语法自检**：Python 目标用 `ast.parse`；JS/TS/Go/Java 用字符串/注释剥离后的括号配对与 `function`/`=>`/方法计数启发式；不合法时告警并把译文写进 `<输出>.rej`，不会当成正常结果
- **模型翻译**：`call_model()` 三级降级（兼容官方 API 与第三方兼容网关），system 明确要求「只输出目标语言代码、不要解释、不要 markdown 围栏」，`strip_code_fence()` 剥围栏兜底
- **离线自检**：`--self-test` 在临时目录造样本，跑通全部确定性逻辑，不联网也不需要 API key

## 怎么用

### 第一步：安装依赖

```powershell
# Windows PowerShell
cd agents/code-translator
py -3 -m pip install -r requirements.txt
```

```bash
# macOS / Linux
cd agents/code-translator
python3 -m pip install -r requirements.txt
```

### 第二步：设置 API key（只在要模型翻译时需要）

```powershell
# Windows PowerShell（当前窗口有效）
$env:ANTHROPIC_API_KEY="你的key"
```

```bash
# macOS / Linux
export ANTHROPIC_API_KEY=你的key
```

走第三方 Anthropic 兼容网关时，用 `--model` 指定网关侧的模型名即可，例如 `--model qwen3.8-max`。
只想跑确定性翻译的话跳过这一步，直接加 `--offline`。

### 第三步：翻译

```powershell
# Windows PowerShell
py -3 agent.py .\utils.py --to javascript --output .\utils.js --offline
```

```bash
# macOS / Linux
python3 agent.py utils.py --to javascript --output utils.js
```

译文默认写到与源文件同名、扩展名按目标语言变化的文件；报告默认写到 `translation-report.md`。

## 常用示例

```bash
# 1) Python → JavaScript，纯离线、不花钱（会自动把缩进块转成花括号）
python3 agent.py utils.py --to javascript --output utils.js --offline

# 2) Python → Go，保留注释，报告单独放一份
python3 agent.py utils.py --to go --keep-comments --report 翻译报告.md

# 3) JavaScript → Python，走第三方兼容网关做正式翻译
python3 agent.py app.js --to python --model qwen3.8-max --output app.py

# 4) 先看源码的语法预检与术语命中情况（离线即可，报告里有完整清单）
python3 agent.py big_module.py --to typescript --offline --report 术语对照.md
```

## 实现方式

1. **语言检测与预检**：`detect_language()`（扩展名 → 内容启发式），`check_syntax()` 对 Python 用 `ast.parse` 报行号，其它语言用 `strip_strings_and_comments()` 去掉字符串/注释后做括号配对。
2. **术语映射**：`TERMS` 五列对照表 + `build_mapping()` 按语言对生成字典，`token_pattern()` 带词边界避免误伤标识符；`analyze_terms()` 出命中清单，`find_unmapped_apis()` 用 `BUILTIN_APIS` 找未覆盖的内置调用。
3. **结构对照**：`count_structure()` 按目标语言正则计数（JS/TS 用 `count_js_functions()` 同时识别 function / 箭头函数 / constructor / 类方法简写），`compare_structure()` 出差异表；`extract_comments()` 负责 `--keep-comments` 的注释抽取（Python 的 docstring 也算文档注释）。
4. **确定性翻译**：`rule_translate()` → `python_to_braces()` 用缩进栈把 Python 块结构换成花括号，`convert_statement()` 处理 def/class/if/elif/for/while/try/except/with/raise/import 等语句，`drop_self_param()` 去掉方法的 `self`，docstring 整块转注释。
5. **模型翻译**：`build_model_prompt()` 把术语对照与注释清单一起给模型，`call_model()` 三级降级调用，`strip_code_fence()` / `detect_fence_language()` 做返回值兜底，模型没返回代码时回退到规则翻译。
6. **落盘与报告**：译文先过 `check_syntax()`，不合法写 `<输出>.rej`；`build_report()` 生成中文 Markdown（概览、语言检测与语法预检、映射命中清单、未映射 API 清单、结构对照、警告与建议）。

## 文件清单

- `agent.py` —— 全部实现：语言检测、语法预检、术语映射、结构对照、规则翻译、模型翻译、报告生成、离线自检
- `README.md` —— 本说明文件
- `requirements.txt` —— 依赖清单（只有 anthropic SDK）

## 常见问题

**Q：不给 API key 能用吗？**
A：能。`--offline` 是纯确定性模式：语言检测、术语映射、结构对照、报告全部本地完成；Python → JS/TS/Go/Java 还会做缩进块转花括号，产物可直接用 node 等做语法验证。不加 `--offline` 又没 key 时会打印中文提示并退出。

**Q：`--offline` 的译文能直接用吗？**
A：Python → JavaScript/TypeScript 的效果最好（自检样例的译文能直接通过 `node --check`）；Python → Go/Java 只做结构性转换，类型、包导入、错误处理都要人工补；反过来 JS/TS → Python 离线只做术语级对照，通常会触发 `.rej` 告警，建议这种情况走模型模式。

**Q：为什么会生成 `.rej` 文件？**
A：译文没通过目标语言语法自检时（括号不配对、字符串未闭合、Python 语法错误等），结果不会当成正常输出，而是写进 `<输出文件>.rej` 并在报告里列出问题行号，避免半成品被当成可用代码。

**Q：翻译后函数/类数量对不上怎么办？**
A：报告的「结构对照」表会标出差异（比如源 3 个函数、译 2 个），说明有遗漏或多余定义，按表里的数量去核对；注释数量在没加 `--keep-comments` 时对不上是正常的（本来就会丢注释）。

**Q：注释怎么保留？**
A：加 `--keep-comments`：注释行会被单独抽出来（`extract_comments()`）交给模型，Python 的 docstring 也按文档注释处理，译完在报告里核对注释条数；离线模式则把 `#` 注释与 docstring 逐行转成 `//`。

**Q：`--to` 之外还有哪些参数？**
A：`--output` 指定译文路径（默认与源文件同名、扩展名按目标语言），`--report` 指定报告路径（默认 `translation-report.md`），`--keep-comments` 保留注释，`--offline` 纯确定性模式，`--model` 指定模型，`--self-test` 离线自检。
