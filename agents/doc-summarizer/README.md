---
name: doc-summarizer
description: 长文摘要助手：清洗解析 + 抽取式打分 + 模型生成，输出带关键词与结构树的中文摘要报告。
---

# 长文摘要助手（doc-summarizer）

一个本地命令行长文摘要 agent：先把 .txt / .md / .html / .json / .csv（装了 pypdf 还支持 .pdf）清洗成统一正文，用纯确定性算法做抽取式摘要和中英文关键词统计，再让模型生成一句话概括、要点列表和详细摘要，最后合并成一份中文 Markdown 摘要报告。除 `anthropic` 外只用标准库，不需要向量库。

## 它能做什么

- **多格式清洗**：HTML 去标签（跳过 `script`/`style`，保留标题层级）、Markdown 去符号、JSON/CSV 拍平成文本、纯文本标题识别、空行压缩
- **结构解析**：标题层级树、章节字数分布、段落与句子切分，报告里直接展示结构树
- **抽取式摘要（不调模型也能用）**：句子打分（词频 TF × 位置权重 × 标题词命中 × 句长惩罚）+ TextRank-lite（句子相似度矩阵幂迭代），取 top 句并按原文顺序输出
- **中英文关键词**：英文单词 + 中文 2/3/4-gram 词频统计，带停用词过滤和互相包含的冗余词去重
- **超长文本 map-reduce**：正文超过 8000 字符自动分块，每块先抽句、合并后再抽一次
- **生成式摘要**：按 `--level brief|standard|detailed` 让模型产出一句话概括 / 要点 / 详细摘要，返回 JSON 解析失败自动回退到抽取式结果，原文进报告附录
- **离线自检**：`--self-test` 在临时目录造样本跑通全部确定性逻辑，不联网也不需要 API key

## 怎么用

### 第一步：安装依赖

```powershell
# Windows PowerShell
cd agents/doc-summarizer
py -3 -m pip install -r requirements.txt
```

```bash
# macOS / Linux
cd agents/doc-summarizer
python3 -m pip install -r requirements.txt
```

### 第二步：设置 API key（只做抽取式摘要可以跳过）

```powershell
# Windows PowerShell（当前窗口有效）
$env:ANTHROPIC_API_KEY="你的key"
```

```bash
# macOS / Linux
export ANTHROPIC_API_KEY=你的key
```

### 第三步：运行摘要

```powershell
# Windows PowerShell：生成式摘要
py -3 agent.py 年度报告.md --level standard --output 摘要.md
```

```bash
# macOS / Linux：纯抽取式摘要（不调模型）
python3 agent.py 年度报告.md --offline --output 摘要.md
```

摘要报告默认写到当前目录的 `summary.md`，用编辑器打开即可。

## 常用示例

```bash
# 1. 详细模式 + 更多关键词，适合长报告
python3 agent.py 年度报告.md --level detailed --keywords 15 --output 摘要.md

# 2. 一次喂多个文件，快速看要点（不调模型、不花钱）
python3 agent.py 说明.html 数据.csv --level brief --offline

# 3. 走第三方 Anthropic 兼容网关时，用 --model 指定网关模型名
python3 agent.py 报告.md --model qwen3.8-max --output 摘要.md

# 4. 先离线自检，确认环境没问题再跑正式任务
python3 agent.py --self-test
```

第 1 条会调模型生成更完整的摘要；第 2 条完全离线，只用抽取式算法；第 3 条配合 `ANTHROPIC_BASE_URL` 走网关；第 4 条不联网、不需要 key，适合提交前自查。

## 实现方式

流程分两段，确定性部分永远在前：

1. **读取与清洗**：`read_document()` 按扩展名分派，`decode_bytes()` 依次尝试 utf-8 / utf-8-sig / gbk / latin-1；`clean_html()`（`HTMLParser` 子类 `_HTMLTextExtractor`）、`clean_markdown()`、`flatten_json()`、`flatten_csv()`、`mark_plain_headings()` 把各种格式压成统一正文，标题写成 `【Hn】` 保留层级。
2. **结构与统计**：`parse_sections()` 解析章节层级树与字数分布，`split_paragraphs()` / `split_sentences()` 切段落句子（返回的句子都是原文连续子串），`extract_keywords()` 做中英文词频统计与冗余去重。
3. **抽取式摘要**：`score_sentences()` 打分（TF × 位置 × 标题词命中 × 句长惩罚）+ `textrank_scores()` 幂迭代，`extractive_summary()` 合并两路得分取 top 句并按原文顺序返回；正文超过 8000 字符走 `split_chunks()` + `map_reduce_summary()`。
4. **生成式摘要**：`generate_summary()` 调模型，要求返回 JSON `{"headline":"","points":[],"summary":""}`，`parse_summary_json()` 解析失败就回退到抽取式结果，模型原文原样进报告附录。
5. **报告**：`build_report()` 输出文档信息表、一句话概括、要点、详细摘要、关键词表、章节结构树、抽取式原句。

模型调用带三级参数降级：先试 `beta.messages.create`（adaptive thinking + server-side fallback），失败退到 `messages.create(thinking=...)`，再失败退到最基础的调用，兼容不认新参数的第三方网关。

## 文件清单

- `agent.py` — 主程序：多格式清洗、结构解析、抽取式摘要（TF + TextRank-lite）、关键词统计、map-reduce 分块、模型生成、报告生成、CLI 与离线自检
- `README.md` — 本说明
- `requirements.txt` — Python 依赖（仅 `anthropic>=0.112.0`）

## 常见问题

**1. 没有 API key 能用吗？**
能。加 `--offline` 就只跑确定性部分：清洗、抽取式摘要、关键词、结构树，完全不联网、不花钱。不加 `--offline` 又检测不到 `ANTHROPIC_API_KEY` / `ANTHROPIC_AUTH_TOKEN`，程序会打印中文设置提示（含 macOS `export` 与 PowerShell `$env:` 两种写法）后退出，不会卡住。

**2. 想摘要 PDF 怎么办？**
先 `pip3 install pypdf` 装可选依赖，再直接喂 `.pdf`。没装也不影响其它格式，程序会打印中文提示并告诉你怎么装，不会崩溃。`pypdf` 不写进 `requirements.txt`，避免强依赖。

**3. 走第三方 Anthropic 兼容网关怎么配？**
设置网关要求的 `ANTHROPIC_BASE_URL` 和 `ANTHROPIC_AUTH_TOKEN`（或 `ANTHROPIC_API_KEY`），再用 `--model` 指定网关支持的模型名，例如 `--model qwen3.8-max`。三级参数降级会自动绕过网关不认识的 thinking / fallbacks 参数。

**4. 摘要里出现原文没有的内容？**
生成式摘要由模型撰写，虽然提示词明确要求「不要编造原文没有的事实」，仍建议对关键数字核对原文。报告里的「抽取式原句」一节是纯算法结果，每句都保证来自原文，可作为对照；也可以直接用 `--offline` 只看抽取式结果。

**5. 抽取式摘要的句子被截断了？**
要点单条上限 40 字，超长会截断并加省略号；「详细摘要」和「抽取式原句」保留完整句子。想看更多内容用 `--level detailed`，或调大正文输入。

**6. Windows 上 `python` / `python3` 没反应？**
本机的 `python` 可能是 Microsoft Store 的占位桩，统一改用 `py -3 agent.py ...`。打印中文乱码时，程序已经做了 `sys.stdout.reconfigure(encoding="utf-8", errors="replace")` 兜底，若仍乱码请把终端编码切成 UTF-8。
