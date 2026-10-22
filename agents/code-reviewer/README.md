---
name: code-reviewer
description: 静态规则加模型语义审查，检查安全、性能、可读性与潜在 bug，输出中文 Markdown 审查报告。
---

# 代码审查助手（code-reviewer）

一个本地命令行代码审查 agent：先用确定性静态规则扫一遍，再让模型读代码做语义审查，最后合并去重成一份按严重级别排序的中文报告。不需要向量库，除 `anthropic` 外只用标准库。

## 它能做什么

- **静态规则扫描**：14 条不看模型也能跑的规则，覆盖硬编码密钥、`eval`、命令注入、SQL 拼接、裸 `except`、可变默认参数、调试残留等
- **模型语义审查**：按每批不超过 40000 字符把代码分批发给模型，要求返回严格 JSON，找规则覆盖不到的深层问题
- **分级报告**：合并静态和语义结果，按 `file:line` + 标题去重，按 严重 → 警告 → 建议 排序，附文件清单和修复代码示意
- **离线自检**：`--self-test` 在临时目录造样本，跑通全部确定性逻辑，不联网也不需要 API key
- **可控范围**：支持 `--focus` 选重点、`--exclude` 排文件、`--max-files` / `--max-file-chars` 限制规模、`--min-severity` 过滤报告

## 怎么用

### 第一步：设置 API key（只做语义审查时需要）

```powershell
# Windows PowerShell（当前窗口有效）
$env:ANTHROPIC_API_KEY="你的key"
```

```bash
# macOS / Linux
export ANTHROPIC_API_KEY=你的key
```

只想跑静态规则可以跳过这一步。

### 第二步：安装依赖

```powershell
# Windows PowerShell
cd agents/code-reviewer
py -3 -m pip install -r requirements.txt
```

```bash
# macOS / Linux
cd agents/code-reviewer
python3 -m pip install -r requirements.txt
```

### 第三步：运行审查

```powershell
# Windows PowerShell：先跑不花钱的静态审查
py -3 agent.py ./src --static-only --output 审查报告.md
```

```bash
# macOS / Linux：静态 + 模型语义审查
python3 agent.py ./src --output 审查报告.md
```

报告默认写到当前目录的 `code-review.md`，用编辑器打开即可。

## 常用示例

```bash
# 1. 只跑静态规则，快速扫全仓库（不联网、不花钱）
python3 agent.py . --static-only

# 2. 专看安全，把结果写到单独文件
python3 agent.py ./src --focus security --output 安全审查.md

# 3. 接第三方 Anthropic 兼容网关时，换模型名即可
python3 agent.py ./src --model qwen3.8-max --output 审查报告.md

# 4. 只看警告及以上，排除压缩产物和测试数据
python3 agent.py ./src --min-severity 警告 --exclude "*.min.js" --exclude "fixtures/*"
```

第 1 条不调用模型，适合提交前快速自查；第 2 条会跳过无关规则，只留安全和需要模型重点看的方向；第 3 条用于官方 API 之外的网关；第 4 条减少噪音，报告只保留需要马上处理的问题。

## 实现方式

两段式流程：

1. **静态规则**：`collect_sources()` 递归收集文本文件（跳过二进制、`.git`、`node_modules`、`__pycache__`、`dist`、`build`、`venv`、`.venv`），对每个文件逐行跑规则表里的检查函数。这一步完全确定性，同样的代码永远得到同样的结果。
2. **模型语义审查**：把文件按每批 ≤40000 字符切分，每行带绝对行号发给模型，要求只返回 JSON 数组（`file / line / severity / title / detail / suggestion`）。解析失败的批次不会让程序崩溃，原始回复会原样放进报告末尾的「模型补充意见」。
3. **合并出报告**：静态发现 + 模型发现 → 按 `file:line` + 标题去重 → 按严重级别排序 → 用 `--min-severity` 过滤 → 生成概览、严重、警告、建议、附录。

模型调用带三级参数降级：先试 `beta.messages.create`（adaptive thinking + server-side fallback），失败退到 `messages.create(thinking=...)`，再失败退到最基础的调用，兼容不认新参数的第三方网关。

## 内置规则表

| ID | 级别 | 检查内容 |
| --- | --- | --- |
| CR001 | 建议 | 注释里的 TODO / FIXME / XXX / HACK 残留 |
| CR002 | 严重 | 硬编码口令 / token / API key / AKIA / 私钥 / 长 Bearer |
| CR003 | 严重 | `eval` / `exec` / `new Function` 动态执行 |
| CR004 | 警告 | 裸 `except` / `except: pass` / JS、Java 空 `catch` |
| CR005 | 建议 | `== None` / `!= None`（应改用 `is None`） |
| CR006 | 警告 | 可变默认参数 `def f(x=[])` / `def f(x={})` |
| CR007 | 严重 | `shell=True` / `os.system` / `child_process.exec` |
| CR008 | 严重 | 用 `+` / f-string / `format` 拼 SQL 语句 |
| CR009 | 建议 | 单行超过 160 字符 |
| CR010 | 建议 | 函数体启发式超过 80 行 |
| CR011 | 建议 | 调试残留 `print` / `console.log` / `debugger`（`--allow-debug` 可关） |
| CR012 | 建议 | 文件末尾缺少换行 |
| CR013 | 建议 | 同一行缩进里 tab 与空格混用 |
| CR014 | 警告 | 空文件或只含空白字符 |

`--list-rules` 会打印完整规则表（含类别和说明），不需要 API key。

## 文件清单

- `agent.py` — 主程序：文件收集、14 条静态规则、模型语义审查、报告生成、CLI 与离线自检
- `README.md` — 本说明
- `requirements.txt` — Python 依赖（仅 `anthropic>=0.112.0`）

## 常见问题

**1. 没有 ANTHROPIC_API_KEY 能跑吗？**
能。加 `--static-only` 就只跑 14 条静态规则，完全不联网、不花钱。不加时如果检测不到 key，会打印中文提示并退出，不会卡住。

**2. 走第三方 Anthropic 兼容网关怎么配？**
设置网关要求的 `ANTHROPIC_BASE_URL` 和 `ANTHROPIC_AUTH_TOKEN`（或 `ANTHROPIC_API_KEY`），再用 `--model` 指定网关支持的模型名，例如 `--model qwen3.8-max`。代码里的三级参数降级会自动绕过网关不认识的 thinking / fallbacks 参数。

**3. 规则误报了怎么办？**
这些规则是文本启发式，会有误报：例如 CLI 程序里正常的 `print` 会命中 CR011（加 `--allow-debug` 关闭），格式化后的长 SQL 会命中 CR008，注释里提到 TODO 会命中 CR001。先用 `--min-severity` 和 `--exclude` 降噪；逐条判断时以报告里的 `file:line` 为准，不要盲改。

**4. 大仓库一次跑不完？**
用 `--max-files` 限制文件数、`--exclude "tests/*" --exclude "*.min.js"` 排除目录、`--max-file-chars` 控制单文件读入量。超过上限的文件会被截断，报告附录会标注「已截断」。

**5. 模型返回的不是合法 JSON 会怎样？**
程序不会崩。解析失败的批次会跳过自动合并，原始文本原样追加到报告末尾的「模型补充意见（未能解析为 JSON）」，可以人工看，也可以重跑那一批。

**6. Windows 上 `python` / `python3` 没反应？**
本机的 `python` 可能是 Microsoft Store 的占位桩，统一改用 `py -3 agent.py ...`。
