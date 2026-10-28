---
name: knowledge-qa
description: 本地知识库问答：倒排索引加 BM25 检索命中片段，再让模型生成带来源引用的中文回答。
---

# 本地知识库问答（knowledge-qa）

把一个本地目录（`.md` / `.txt` / `.json` / `.csv`）当成知识库：先做完全确定性的分段、分词、倒排索引与 BM25 打分，再把命中的片段交给模型生成带 `[来源: 文件名:起始行-结束行]` 引用的中文回答。检索部分不需要 key、不需要联网；索引缓存在知识库目录的 `.kb_index.json`，改了文件自动失效重建。除 `anthropic` 外只用标准库，不依赖向量库。

## 它能做什么

- **知识库加载**：递归读取 `.md` / `.txt` / `.json` / `.csv`，自动跳过二进制文件、`.git` 等目录和隐藏文件，每个片段都记录来源文件与起止行号
- **智能分段**：Markdown 按标题层级切并保留「标题面包屑」（如 `安装指南 > 依赖安装`），纯文本按空行段落切，超长段落再按句切到 800 字符以内
- **BM25 检索**：纯 Python 倒排索引（中文 2-gram + 英文单词分词），BM25 打分（k1=1.5、b=0.75），支持同义词查询扩展（`--no-expand` 可关）
- **索引缓存**：`.kb_index.json` 落在知识库目录，按文件 mtime + size 失效检测，`--rebuild` 强制重建
- **带引用回答**：模型只依据检索片段回答，引用格式固定；片段不足时明说「根据现有资料无法回答」；模型 JSON 解析失败不崩溃，原文放进报告附录
- **无命中兜底**：最高得分低于 `--min-score` 阈值时明确告诉你「知识库里没有相关内容」
- **离线模式**：`--offline` 只做检索，打印命中的原文片段和得分表，不联网不花钱
- **离线自检**：`--self-test` 用临时目录造样本跑通全部确定性逻辑，不联网、不需要 API key

## 怎么用

### 第一步：安装依赖

```powershell
# Windows PowerShell
cd agents/knowledge-qa
py -3 -m pip install -r requirements.txt
```

```bash
# macOS / Linux
cd agents/knowledge-qa
python3 -m pip install -r requirements.txt
```

### 第二步：设置 API key（只有调模型回答时需要）

```powershell
# Windows PowerShell（当前窗口有效）
$env:ANTHROPIC_API_KEY="你的key"
```

```bash
# macOS / Linux
export ANTHROPIC_API_KEY=你的key
```

只想检索不回答（`--offline`）可以跳过这一步。

### 第三步：提问

```powershell
# Windows PowerShell
py -3 agent.py --kb ./docs --ask "如何安装依赖"
```

```bash
# macOS / Linux
python3 agent.py --kb ./docs --ask "如何安装依赖"
```

不带 `--ask` 就进入交互问答，输入 `exit` 退出。走第三方 Anthropic 兼容网关时加 `--model qwen3.8-max` 这类网关认得的模型名。

## 常用示例

```bash
# 1. 提问并把报告存成 Markdown（含答案、引用清单、检索得分表）
python3 agent.py --kb ./docs --ask "配置项都有哪些" --output 答案.md

# 2. 离线只检索：打印命中的原文片段，不调模型、不需要 key
python3 agent.py --kb ./docs --ask "如何部署" --offline

# 3. 新增文档后强制重建索引，并多看几个候选片段
python3 agent.py --kb ./docs --ask "日志怎么排查" --rebuild --top-k 8

# 4. 走第三方兼容网关：指定网关认得的模型名
python3 agent.py --kb ./docs --ask "常见错误有哪些" --model qwen3.8-max
```

第 1 条是标准用法；第 2 条适合先确认「库里到底有没有答案」；第 3 条在批量改完文档后用；第 4 条不走官方 API 时用。

## 实现方式

确定性检索在前，模型只做语言生成：

1. **加载与分段**：`load_kb()` 递归读文本文件（跳过二进制、`.git`、隐藏文件），`segment_markdown()` 按标题层级切段并维护标题面包屑，`segment_text()` 按空行切段，`split_block()` 把超长段落按句切到 800 字符以内，每段带 `(文件, 起始行, 结束行)`。
2. **索引与检索**：`tokenize()` 中文按 2-gram、英文按单词切分；`build_index()` 建倒排索引和文档长度表；`bm25_search()` 按 BM25 公式打分（k1=1.5、b=0.75），`expand_query()` 做同义词查询扩展；`select_hits()` 过滤低分片段，全被过滤就走「知识库里没有相关内容」兜底。
3. **索引缓存**：`save_cache()` / `load_cache()` 把索引写进知识库目录的 `.kb_index.json`，`cache_is_valid()` 比对文件 mtime 与 size，不一致就重建；`--rebuild` 直接跳过缓存。
4. **模型回答**：`ask_model()` 把 top-k 片段（带来源标注）发给模型，system 要求只依据片段回答、引用格式固定为 `[来源: 文件名:起始行-结束行]`、片段不足时明说不知道，返回 JSON `{"answer","citations","confidence"}`；`parse_answer_json()` 解析失败不崩溃，检索片段原文照样输出，模型原文进报告附录。
5. **模型调用降级**：`call_model()` 三级降级——先试 `beta.messages.create`（adaptive thinking + server-side fallback），失败退到 `messages.create(thinking=...)`，再失败退到最基础调用，兼容不认新参数的第三方网关。

## 文件清单

- `agent.py` — 主程序：知识库加载、分段、倒排索引、BM25 检索、索引缓存、模型问答、报告生成、CLI 与离线自检
- `README.md` — 本说明
- `requirements.txt` — Python 依赖（仅 `anthropic>=0.112.0`）

## 常见问题

**1. 没有 ANTHROPIC_API_KEY 能跑吗？**
能。加 `--offline` 就只做本地检索，打印命中的原文片段和得分表，完全不联网、不花钱。不加时如果检测不到 key，会打印含 macOS 与 PowerShell 写法的中文提示并退出。

**2. 检索结果不对 / 命中的不是我要的段落？**
先看报告里的「检索得分表」，命中词一栏能看出是哪些词在起作用。可以换更具体的关键词、加大 `--top-k`，或用 `--no-expand` 关掉同义词扩展减少噪音；确认相关文档确实在 `--kb` 目录里（只读 `.md/.txt/.json/.csv`）。

**3. 明明库里有内容，却提示「知识库里没有相关内容」？**
说明最高 BM25 得分低于阈值。用 `--min-score 0` 关掉阈值强制返回 top-k，或者补充同义词到代码里的 `SYNONYMS` 小词表，让查询扩展能连上你的说法。

**4. 加了新文档后回答还是旧的？**
索引缓存按文件 mtime + size 自动失效，正常情况下会自动重建；如果怀疑缓存脏了，加 `--rebuild` 强制重建，或直接删掉知识库目录下的 `.kb_index.json`。

**5. 模型回答没有引用 / 输出乱了？**
模型回复不是约定 JSON 时程序不会崩溃：报告会给出检索到的片段原文，并把模型原文放进「附录：模型原始回复」供人工参考。也可以加 `--offline` 看纯检索结果核对。

**6. 交互模式怎么退出？**
输入 `exit`、`quit`、`退出` 或 `q` 都可以，Ctrl+C / Ctrl+D 也能结束。交互中每次都按当前 `--top-k` / `--offline` / `--output` 设置执行。
