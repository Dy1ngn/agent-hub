---
name: code-explainer
description: 代码讲解学习助手：提取结构与复杂度，生成中文分层讲解、术语词典与自测题。
---

# 代码讲解 / 学习助手（code-explainer）

给一段代码生成一份中文讲解文档：先做完全确定性的分析（语言检测、结构提取、圈复杂度近似、术语扫描），再让模型按「总体作用 → 逐段讲解 → 坑点 → 改进建议 → 自测题」分层讲解，输出 `explanation.md`。支持 Python（走 `ast`）、JavaScript / TypeScript、Go、Java；`--offline` 只用确定性分析也能生成结构化骨架讲解。除 `anthropic` 外只用标准库。

## 它能做什么

- **语言检测**：扩展名 + 内容启发式（shebang、import 语法、花括号密度），`--lang` 可手动指定
- **结构提取**：Python 用 `ast` 提取模块 docstring、导入、函数/类/签名/装饰器/嵌套函数、全局变量、`if __name__` 入口；JS/TS/Go/Java 用正则提取函数、类、导入清单
- **复杂度启发式**：圈复杂度近似（1 + if/for/while/case/and/or/三元 计数）、函数长度、参数个数、嵌套深度（缩进/花括号启发式），自动标注「高复杂度函数 top3」
- **术语小词典**：内置 100 条常见编程术语中英对照（closure 闭包、decorator 装饰器、幂等、竞态……），扫描代码命中就输出词典
- **骨架讲解**：`--offline` 也能按函数逐个生成「签名 / 参数 / 返回 / 复杂度 / 依赖调用」结构化说明表格
- **模型分层讲解**：生成 JSON（总体作用、逐段讲解、坑点、改进建议、3 道自测题），解析失败不崩溃，自动回退到骨架讲解，原文放报告附录
- **离线自检**：`--self-test` 用临时文件造样本跑通全部确定性逻辑，不联网、不需要 API key

## 怎么用

### 第一步：安装依赖

```powershell
# Windows PowerShell
cd agents/code-explainer
py -3 -m pip install -r requirements.txt
```

```bash
# macOS / Linux
cd agents/code-explainer
python3 -m pip install -r requirements.txt
```

### 第二步：设置 API key（只有调模型讲解时需要）

```powershell
# Windows PowerShell（当前窗口有效）
$env:ANTHROPIC_API_KEY="你的key"
```

```bash
# macOS / Linux
export ANTHROPIC_API_KEY=你的key
```

只想看骨架讲解（`--offline`）可以跳过这一步。

### 第三步：讲解代码

```powershell
# Windows PowerShell
py -3 agent.py .\src\app.py --level 入门 --output 讲解.md
```

```bash
# macOS / Linux
python3 agent.py ./src/app.py --level 入门 --output 讲解.md
```

## 常用示例

```bash
# 1. 讲解一个 Python 文件，按入门深度输出讲解文档
python3 agent.py ./src/app.py --level 入门 --output 讲解.md

# 2. 离线只做结构分析与骨架讲解：不调模型、不需要 key
python3 agent.py ./src/utils.js --lang javascript --offline

# 3. 一次讲解多个文件（按顺序合并进同一份文档）
python3 agent.py ./a.py ./b.py --level 深入 --output 深入讲解.md

# 4. 走第三方 Anthropic 兼容网关：指定网关认得的模型名
python3 agent.py ./Demo.java --model qwen3.8-max
```

第 1 条是标准用法；第 2 条适合快速摸清代码结构；第 3 条对比学习多个文件；第 4 条不走官方 API 时用。

## 实现方式

确定性分析在前，模型只做语言生成：

1. **语言检测**：`detect_language()` 按扩展名加内容启发式打分（shebang、`import ... from`、`package main`、`public class`、花括号密度）选出最可能的语言。
2. **结构提取**：Python 走 `extract_python_structure()`（`ast` 解析，含 `py_function_entry()` 的嵌套函数与装饰器、`py_class_entry()` 的方法列表）；其它语言走 `extract_generic_structure()`（正则匹配函数/类/导入，`extract_block()` 按花括号配平截取函数体）。Python 语法错误时退回正则提取并提示，不崩溃。
3. **复杂度启发式**：`estimate_complexity()` 数分支关键字与 `&&`/`||`/三元，`nesting_depth()` 用缩进栈（Python）或花括号深度（其它语言），`function_metrics()` 补齐长度与参数个数，`top_complex_functions()` 输出高复杂度 top3。
4. **术语扫描**：`find_terms()` 拿内置 `GLOSSARY`（100 条中英对照）匹配代码与注释，命中词按出现次数排序成「术语小词典」。
5. **模型讲解**：`explain_with_model()` 把结构摘要 + 代码发给模型，要求只返回 JSON `{"overview","sections","quiz"}`（`EXPLAIN_SYSTEM_PROMPT` 按 `--level` 调整深浅）；`parse_explain_json()` 解析失败就回退到 `render_skeleton_table()` 的骨架讲解，模型原文进报告附录。
6. **模型调用降级**：`call_model()` 三级降级——先试 `beta.messages.create`（adaptive thinking + server-side fallback），失败退到 `messages.create(thinking=...)`，再失败退到最基础调用，兼容不认新参数的第三方网关。

## 文件清单

- `agent.py` — 主程序：语言检测、结构提取、复杂度启发式、术语表与扫描、骨架讲解、模型讲解、报告生成、CLI 与离线自检
- `README.md` — 本说明
- `requirements.txt` — Python 依赖（仅 `anthropic>=0.112.0`）

## 常见问题

**1. 没有 ANTHROPIC_API_KEY 能跑吗？**
能。加 `--offline` 就只做确定性分析，输出结构表、函数骨架表、复杂度提示、术语词典，不联网、不花钱。不加时如果检测不到 key，会打印含 macOS 与 PowerShell 写法的中文提示并退出。

**2. 语言检测错了怎么办？**
用 `--lang` 手动指定 `python` / `javascript` / `go` / `java`。`.txt` 里放的 Python 代码一般也能靠内容启发式认出来（shebang、`def `、`import ` 等特征）。

**3. 讲解里说「Python 语法解析失败」？**
说明文件本身有语法错误，`ast` 解不开，程序会退回正则提取（结构可能不完整），并在报告开头标注原因。先修好语法错误再讲会更准。

**4. 为什么复杂度数字和别的工具不一样？**
这里用的是启发式近似（1 + if/for/while/case/and/or/三元 计数），不是严格的圈复杂度；它的作用是横向比较「哪个函数更难读」，绝对值不必当真。

**5. 模型没生成自测题 / 输出乱了怎么办？**
模型回复不是约定 JSON 时程序不会崩溃：自动回退到确定性的骨架讲解，并把模型原文放进「附录：模型原始回复」。也可以换 `--level` 或换个更小的文件再试。

**6. 可以讲解整个目录吗？**
当前按文件工作，一次给多个文件路径即可（会合并进一份文档）。太大文件会被截断到 200000 字符并在报告里注明，建议拆开讲。
