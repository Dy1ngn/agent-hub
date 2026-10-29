# Agent Hub

> 我的私人 Agent 仓库：存放各个 Agent 的实现代码。每个 Agent 是 `agents/` 目录下的一个独立文件夹。

## 这是什么？

这个仓库是我的「Agent 仓库」，用来存放各个 Agent 的**实现代码**。每个 Agent 独占 `agents/` 目录下的一个文件夹，里面包含它自己的代码和一份介绍文件（`README.md`）。

顶层 README 会自动汇总每个 Agent 的介绍，方便一览全部 Agent。

## 目录结构

```
agent-hub/
├── README.md                  # 本文件：项目介绍（Agent 列表自动更新）
├── CLAUDE.md                  # 项目记忆（给 Claude Code 看的项目说明）
├── .gitignore
├── agents/                    # 所有 Agent（一个文件夹 = 一个 Agent）
│   └── <agent 名>/
│       ├── README.md          # 该 Agent 的介绍文件（自动汇总到顶层 README）
│       └── ...                # 该 Agent 的实现代码
├── scripts/
│   └── sync_agents.py         # 自动同步脚本：汇总各 Agent 介绍到顶层 README
└── .claude/
    └── settings.json          # Hook 配置：什么时候运行同步脚本
```

## Agent 列表

<!-- AGENTS:START -->

> 本区块由 `scripts/sync_agents.py` 自动维护，请勿手动编辑。

共 27 个 Agent：

| Agent | 描述 |
| --- | --- |
| [code-explainer](agents/code-explainer/) | 代码讲解学习助手：提取结构与复杂度，生成中文分层讲解、术语词典与自测题。 |
| [code-reviewer](agents/code-reviewer/) | 静态规则加模型语义审查，检查安全、性能、可读性与潜在 bug，输出中文 Markdown 审查报告。 |
| [code-translator](agents/code-translator/) | 跨语言代码翻译：语言检测、80+ 术语映射、结构对照与语法自检，输出译文与中文翻译报告。 |
| [commit-crafter](agents/commit-crafter/) | 读取 Git 改动并生成规范的 commit message，默认只预览，确认后才提交。 |
| [data-cleaner](agents/data-cleaner/) | CSV/TSV/JSON 表格质量体检与清洗，出中文质量报告，支持异常值、重复、格式问题检测。 |
| [dependency-auditor](agents/dependency-auditor/) | 解析多种依赖清单并按内置规则做风险审计，输出评分、风险清单与升级建议的中文报告。 |
| [doc-summarizer](agents/doc-summarizer/) | 长文摘要助手：清洗解析 + 抽取式打分 + 模型生成，输出带关键词与结构树的中文摘要报告。 |
| [doc-writer](agents/doc-writer/) | 先静态扫描项目结构，再让模型生成 README、接口文档、架构说明和新人上手指南四类文档。 |
| [email-assistant](agents/email-assistant/) | 解析 .eml/.mbox/.txt 邮件，做摘要、优先级判断、待办提取与回复起草，输出中文报告。 |
| [expense-tracker](agents/expense-tracker/) | 自然语言一句话记账，本地 JSON 账本加分类统计与预算告警，输出带 ASCII 图表的中文月报。 |
| [file-butler](agents/file-butler/) | 本地文件管家：授权一个文件夹后，用自然语言命令让它列出/读取/搜索/写入其中的文件，自动完成整理、汇总、生成报告等任务。基于 Claude function-call，不使用向量库。 |
| [grammar-checker](agents/grammar-checker/) | 规则表加模型语义校对，检查中英文错别字、标点、空格、语法与表达，输出中文 Markdown 校对报告。 |
| [interview-prep](agents/interview-prep/) | 面试准备助手：内置分类题库、JD 岗位画像、可复现抽题、复习计划与模拟面试。 |
| [knowledge-qa](agents/knowledge-qa/) | 本地知识库问答：倒排索引加 BM25 检索命中片段，再让模型生成带来源引用的中文回答。 |
| [log-detective](agents/log-detective/) | 流式扫描多种格式日志并聚合错误签名、时间分布与访问日志指标，再由 Claude 给出根因假设和排查命令的日志排查助手。 |
| [meeting-minutes](agents/meeting-minutes/) | 解析会议逐字稿，做发言人统计、议题切分、决议与待办抽取，生成中文会议纪要。 |
| [prompt-optimizer](agents/prompt-optimizer/) | 提示词体检与优化：五要素、模糊词、结构与占位符确定性体检，生成评分卡与优化版提示词。 |
| [release-notes](agents/release-notes/) | 发布说明生成：解析 conventional commits，自动分组、推断 semver、统计贡献者，可选模型润色。 |
| [resume-reviewer](agents/resume-reviewer/) | 简历诊断与 JD 匹配：结构、动词、量化、日期体检，技能关键词匹配打分，输出改写示例报告。 |
| [slide-maker](agents/slide-maker/) | 幻灯片生成：从主题或文档提炼大纲，自动排版成可导出 PDF/PPTX 的 Marp 幻灯片。 |
| [sql-chat](agents/sql-chat/) | 用中文提问，自动生成只读 SQL 查询本地 SQLite/CSV/JSON，输出结果表与中文结论。 |
| [test-generator](agents/test-generator/) | 用 ast 分析被测函数签名与异常，生成 pytest/unittest 测试骨架与用例清单，可选模型补全断言。 |
| [todo-planner](agents/todo-planner/) | 把目标描述拆成里程碑与任务，生成 WBS、依赖表、工作日甘特图和风险清单的中文计划。 |
| [translator](agents/translator/) | 多语言批量翻译助手，保护代码与占位符、按术语表统一术语，支持 Markdown、JSON、CSV。 |
| [weekly-report](agents/weekly-report/) | 采集 git 提交与工作记录，做分类统计与合并去重，生成中文周报/日报。 |
| [writing-assistant](agents/writing-assistant/) | 写作润色助手，先做文本体检评分，再按润色/重写/扩写/缩写/正式/口语改写并给出逐句对照报告。 |
| [📈 股票分析 Agent](agents/stock-analyst/) | 基于 Anthropic Claude 的智能股票分析助手，输入股票代码即可获得专业的技术分析报告。 |

<!-- AGENTS:END -->

## 评测平台接入（agent-manager）

每个 Agent 文件夹附带两份评测配置，让 [agent-manager](../agent-manager/) 以「源码模式」自适应接入、自动跑评测：

- `aep.agent.json` —— 运行清单：入口、argv 模板（`{{caseFile}}` `{{workDir}}` `{{outputDir}}` 等占位符）、
  输入物化、输出抽取、模型协议（`anthropic` 时平台自动注入 Anthropic→OpenAI 协议桥）。
- `eval/spec.json` —— 紧凑用例规格（`rows` 对齐组合 × `vars` 变量轴 × `repeat` 重复调用），
  用 `node ../agent-manager/scripts/gen_evalsets.mjs --only <agent>` 生成 5 个测试用例组
  （`eval/g1-basic` ~ `g5-stability`，每组 100 次调用）。

平台侧：`node ../agent-manager/server/dist/cli.js builtin sync` 注册 + 导入用例组，
`run-all` 批量评测。单独跑 Agent 需要 OpenAI 兼容模型时，用 `runtime/anthropic_bridge.py` 起协议桥。

## 如何添加新 Agent

1. 在 `agents/` 目录下新建一个文件夹作为这个 Agent（文件夹名用小写英文和连字符，例如 `code-reviewer`）
2. 在该文件夹里写 Agent 的实现代码
3. 在该文件夹里创建一个 `README.md` 介绍文件，说明这个 Agent 的名称和用途
4. 保存即可，**顶层 README 里的 Agent 列表会自动更新**，无需手动维护

### 介绍文件（README.md）怎么写

脚本按下面的优先级提取「名称」和「描述」来生成列表：

- **名称**：优先用 frontmatter 里的 `name`，其次用正文第一个 `#` 大标题，最后用文件夹名
- **描述**：优先用 frontmatter 里的 `description`，其次用正文第一段

**推荐写法**（顶部加 frontmatter，信息最准确）：

```markdown
---
name: code-reviewer
description: 代码审查助手：检查代码质量、发现潜在问题。
---

# Code Reviewer

这里是详细介绍……
```

**简单写法**（不写 frontmatter 也行，脚本会自动从正文提取标题和第一段）：

```markdown
# Code Reviewer

代码审查助手：检查代码质量、发现潜在问题。

这里是详细介绍……
```

## 自动更新机制

顶层 README 中「Agent 列表」区块由脚本自动维护，原理如下：

1. `.claude/settings.json` 里配置了两个 hook（钩子）：
   - **写入钩子**：每次在 Claude Code 里新增或修改某个 Agent 的 `README.md` 后，自动运行同步脚本；
   - **会话钩子**：每次打开新会话时，自动运行一次同步脚本（兜底，覆盖在 Claude Code 之外手动增删 Agent 的情况）。
2. 同步脚本 `scripts/sync_agents.py` 会扫描 `agents/` 下的每个文件夹，读取其 `README.md`，重新生成列表区块。

如需手动同步：

```bash
python3 scripts/sync_agents.py
```

> 注意：不要手动编辑顶层 README 中 `AGENTS:START` 和 `AGENTS:END` 标记之间的内容，它会被自动覆盖。

## 常见问题

**Q：我手动删除了一个 Agent 文件夹，列表怎么没变？**
列表会在下次打开 Claude Code 会话时自动同步；也可以立刻手动运行上面的同步命令。

**Q：某个 Agent 在列表里显示「缺少 README.md 介绍文件」？**
说明这个 Agent 文件夹里没有 `README.md`，给它补一个介绍文件即可。

**Q：自动更新没有生效？**
自动更新由 Claude Code 的 hook 触发，确认你是在 Claude Code 里编辑的 `README.md`；任何时候都可以手动运行同步命令兜底。
