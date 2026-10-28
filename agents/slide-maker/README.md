---
name: slide-maker
description: 幻灯片生成：从主题或文档提炼大纲，自动排版成可导出 PDF/PPTX 的 Marp 幻灯片。
---

# 幻灯片生成助手（slide-maker）

一个本地命令行幻灯片 agent：给一个主题或一份现有文档，先用确定性算法提炼大纲（按标题切章节、每章取要点句、词频启发式提关键词），再排成标准 Marp Markdown（封面 / 目录 / 章节内容页 / 小结 / Q&A），自动控制每页 ≤5 个要点、每要点 ≤30 字、超限自动拆页，每页带演讲备注；再（可选）让模型按页产出标题、要点、备注与版式。输出 `slides.md`，可直接用 Marp CLI 导出 PDF / PPTX。除 `anthropic` 外只用标准库。

## 它能做什么

- **文档提炼**：Markdown / 纯文本按标题切章节，列表项与段落首句作要点，词频启发式提取中英文关键词（自动跳过 YAML frontmatter 和代码围栏）
- **Marp 渲染**：头部 `marp: true` / `theme: default` / `paginate: true`，用 `---` 分页；封面页、目录页、章节内容页、小结页、Q&A 结束页，页脚含主题名与「第 N/M 页」页码
- **排版约束**：每页 ≤5 个要点、每要点 ≤30 字（超出截断加省略号），要点太多自动拆页（标题加「（续N）」）
- **结构校验**：页数与 `--slides` 目标差异提示、每页字数上限校验、要点密度检查；输出可被按 Marp 结构解析（`---` 分隔符数量与页数一一对应）
- **演讲备注**：每页生成 `<!-- notes: ... -->` 备注块，导出后可用于演示者视图
- **模型生成（可选）**：按页产出 `{"title":"","bullets":[],"notes":"","layout":"content|chart|quote"}` JSON 数组，解析失败自动回退确定性大纲模板，仍然输出合法 Marp

## 怎么用

### 第一步：安装依赖

```powershell
# Windows PowerShell
cd agents/slide-maker
py -3 -m pip install -r requirements.txt
```

```bash
# macOS / Linux
cd agents/slide-maker
python3 -m pip install -r requirements.txt
```

### 第二步：设置 API key（离线排版可以跳过）

```powershell
# Windows PowerShell（当前窗口有效）
$env:ANTHROPIC_API_KEY="你的key"
```

```bash
# macOS / Linux
export ANTHROPIC_API_KEY=你的key
```

### 第三步：生成幻灯片

```powershell
# Windows PowerShell：按主题生成
py -3 agent.py --topic "季度汇报" --slides 12 --audience "管理层"
```

```bash
# macOS / Linux：从现有文档提炼大纲，纯确定性排版
python3 agent.py --from 方案.md --topic "方案评审" --offline --output 评审slides.md
```

结果默认写到 `slides.md`。想导出成 PDF / PPTX：`npx @marp-team/marp-cli slides.md --pdf`（或 `--pptx`）。

## 常用示例

```bash
# 1. 给主题和听众，让模型按页生成内容（需要 key）
python3 agent.py --topic "Kubernetes 上手" --slides 12 --audience "后端工程师"

# 2. 手头已有文档，直接提炼大纲离线排版，不花钱
python3 agent.py --from 方案.md --topic "方案评审" --offline

# 3. 走第三方 Anthropic 兼容网关时，用 --model 指定网关模型名
python3 agent.py --topic "技术分享" --model qwen3.8-max

# 4. 先跑离线自检，确认环境没问题
python3 agent.py --self-test
```

第 1 条走模型生成每页内容；第 2 条完全确定性，适合文档已经写好、只想快速出幻灯片；第 3 条配合 `ANTHROPIC_BASE_URL` 走网关；第 4 条不联网、不需要 key。

## 实现方式

确定性排版在前，模型只做语言生成：

1. **文档提炼**：`extract_doc_outline()` 按标题切章节，列表项直接作要点、段落取首句；`extract_keywords()` 用中英文混合词频（英文单词 + 中文 2/3/4-gram）提关键词并去冗余。
2. **排版**：`build_deck()` 先按目标页数在 5/4/3 个要点一页之间选最接近的粒度（`build_content_pages()`），再拼封面、目录、内容页、小结、Q&A；`guess_layout()` 按数字密度猜 `content`/`chart`/`quote` 版式。
3. **约束与拆页**：`shorten_bullet()` 把要点截到 30 字，`enforce_bullet_limit()` 对超过 5 个要点的页自动拆页，`validate_deck()` 检查页数目标差异、要点数量与长度并给中文提示。
4. **Marp 渲染**：`render_marp()` 输出头部指令 + `---` 分页 + `<!-- _footer: 主题 · 第 N/M 页 -->` 页脚 + `<!-- notes: ... -->` 备注；`parse_pages()` / `count_page_separators()` 反向解析校验结构（含 front matter 收尾的 `---` 在内，`---` 分隔符数量与页数一致）。
5. **模型生成（可选）**：`generate_deck_with_model()` 要求返回每页 JSON 数组，`parse_deck_json()` 解析失败就回退到确定性大纲（`template_sections()` 或文档提炼结果），永远输出合法 Marp。

模型调用带三级参数降级：先试 `beta.messages.create`（adaptive thinking + server-side fallback），失败退到 `messages.create(thinking=...)`，再失败退到最基础的调用，兼容不认新参数的第三方网关。

## 文件清单

- `agent.py` — 主程序：文档提炼、要点切分、自动拆页、Marp 渲染与结构校验、模型生成、CLI 与离线自检
- `README.md` — 本说明
- `requirements.txt` — Python 依赖（仅 `anthropic>=0.112.0`）

## 常见问题

**1. 没有 API key 能用吗？**
能。加 `--offline` 就用文档提炼结果（或大纲模板）直接排版，完全不联网、不花钱。不加 `--offline` 又检测不到 `ANTHROPIC_API_KEY` / `ANTHROPIC_AUTH_TOKEN`，程序会打印中文设置提示（含 macOS `export` 与 PowerShell `$env:` 两种写法）后退出。

**2. 页数和 `--slides` 目标对不上？**
内容多少决定实际页数：要点多就自动拆页，页数超过目标 ±2 会在终端提示「相差 N 页」。想控制页数，精简 `--from` 文档、减少章节要点，或调整 `--slides` 到合理值。

**3. 生成的 slides.md 怎么变成 PDF / PPTX？**
用 Marp CLI：`npx @marp-team/marp-cli slides.md --pdf` 或 `--pptx`。输出是标准 Marp Markdown，任何支持 Marp 的编辑器插件（如 VS Code 的 Marp for VS Code）也能直接预览。

**4. 每页的备注是干嘛的？**
`<!-- notes: ... -->` 是演讲备注，导出 PDF/PPTX 后在演示者视图里能看到，帮讲者记住每页该说什么。模型模式下备注由模型写，离线模式下是确定性模板（讲什么、控制几分钟）。

**5. 模型返回的不是合法 JSON 会怎样？**
程序不会崩，也不会输出坏文件：直接回退到确定性大纲模板（封面、目录、内容页、小结、Q&A 齐全），并在终端说明原因；想要确定性结果可以直接加 `--offline`。

**6. Windows 上 `python` / `python3` 没反应？**
本机的 `python` 可能是 Microsoft Store 的占位桩，统一改用 `py -3 agent.py ...`。打印中文乱码时程序已做 UTF-8 兜底，仍乱码请把终端编码切成 UTF-8。
