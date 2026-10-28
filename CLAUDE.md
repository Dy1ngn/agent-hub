# Agent Hub — 项目记忆

## 项目定位
用户的私人 Agent 仓库（GitHub: Dy1ngn/agent-hub），存放各个 Agent 的**实现代码**。
每个 Agent 是顶层 `agents/` 目录下的一个独立文件夹，内含代码和一份 `README.md` 介绍文件。

## 目录结构
- `agents/` — 所有 Agent，一个文件夹 = 一个 Agent
  - `agents/<agent 名>/README.md` — 该 Agent 的介绍文件（汇总到顶层 README 的数据来源）
  - `agents/<agent 名>/...` — 该 Agent 的实现代码
- `scripts/sync_agents.py` — 顶层 README Agent 列表自动同步脚本
- `.claude/settings.json` — hook 配置（PostToolUse + SessionStart 触发同步）
- `README.md` — 项目介绍，包含自动维护的 Agent 列表区块

## 核心约定（必须遵守）
1. **顶层 README.md 中的 Agent 列表必须始终与 `agents/` 目录的实际内容保持一致** —— 这是用户明确要求长期自动维护的事项。
2. 正常情况下 hook 会自动同步；若发现列表与实际不符，立即手动运行：`python3 scripts/sync_agents.py`
3. 不要手动编辑顶层 README.md 中 `AGENTS:START` / `AGENTS:END` 标记之间的内容（会被自动覆盖）。
4. 新增 Agent 时：在 `agents/` 下建一个文件夹，写好代码，并创建 `README.md` 介绍文件（见 README「如何添加新 Agent」）。

## 介绍信息提取规则（sync_agents.py 的逻辑）
- 名称优先级：frontmatter `name` > 正文第一个 `#` 大标题 > 文件夹名
- 描述优先级：frontmatter `description` > 正文第一段 > 「（暂无描述）」
- 缺少 README.md 的 Agent 会在列表中标注「（缺少 README.md 介绍文件）」

## 运行环境（本机）
- 代码按 Python 3.9 语法写（兼容老系统），但本机必须用 `py -3` 运行（实测 3.13.x）；`python` / `python3` 是失效的 Microsoft Store 桩，直接调会 exit 1
- 写打印中文/emoji 的脚本要防 Windows GBK 编码：`sys.stdout.reconfigure(encoding="utf-8", errors="replace")`（管道输出时否则会 UnicodeEncodeError 崩溃），参考各 agent 的 `ensure_utf8_console()` / `configure_output()`
- anthropic SDK 已装（实测 0.112.0）
- 本机 Claude Code 通过**阿里云 Anthropic 兼容网关**连接（`ANTHROPIC_BASE_URL` 指向 token-plan.cn-beijing.maas.aliyuncs.com），模型名 `qwen3.8-max`；`ANTHROPIC_AUTH_TOKEN` 只在 Claude Code 进程环境里有，用户终端需要自行 export
- 官方 Anthropic API key（ANTHROPIC_API_KEY）未在本机配置；agent 代码默认用 `claude-opus-5`（官方 API），走网关时用 `--model qwen3.8-max`
- agents 的模型调用代码要做**参数降级兼容**（第三方网关可能不支持 thinking/fallbacks 等新参数），参考 `agents/file-butler/agent.py` 的 `call_model()`

## 现有 Agent
共 27 个。除 `stock-analyst` 外，其余 agent 统一约定：`agent.py` + `README.md`（frontmatter）+ `requirements.txt`，支持 `--model` / `--offline` / `--self-test`（离线自检打印 `SELF-TEST OK`），模型调用统一用三级降级 `call_model()`。

**开发效率**
- `agents/file-butler` — 本地文件管家（function-call 文件操作，沙盒安全，已通过端到端测试）
- `agents/code-reviewer` — 代码审查（14 条静态规则 + 模型语义审查，输出中文 Markdown 报告，`--self-test` 自检）
- `agents/commit-crafter` — 提交信息生成（读 git diff 生成 conventional/gitmoji/plain 提交信息，默认只预览，`--apply` 才提交）
- `agents/log-detective` — 日志排查（流式扫描多格式日志，确定性聚合错误签名/时间分布/访问指标，再让模型给根因假设）
- `agents/doc-writer` — 项目文档生成（静态扫描项目结构后生成 README/API/架构/新人指南四类文档）
- `agents/test-generator` — 单元测试生成（ast 提取签名/异常→pytest/unittest 骨架+用例清单，可选模型补全断言）
- `agents/code-translator` — 跨语言代码翻译（语言检测、术语映射、结构对照、目标语言语法自检）
- `agents/dependency-auditor` — 依赖风险审计（requirements/pyproject/package.json 解析，9 条规则+评分+升级建议）
- `agents/release-notes` — 发布说明（conventional commits 解析、分组、semver 推断、贡献者统计）
- `agents/code-explainer` — 代码讲解（结构/复杂度提取、术语词典、分层讲解与自测题）

**数据与查询**
- `agents/sql-chat` — SQL 问答（中文提问→只读 SQL→结果表+中文结论，支持 SQLite/CSV/TSV/JSON）
- `agents/data-cleaner` — 表格清洗（CSV/TSV/JSON 体检：缺失/重复/异常值/格式，dry-run + `--apply`）
- `agents/stock-analyst` — 股票分析助手（A股/美股，技术分析+资金流向+舆情+综合评分+预测）
- `agents/expense-tracker` — 记账分析（自然语言记账→分类统计/预算告警/ASCII 图表月报）

**写作与沟通**
- `agents/translator` — 多语言翻译（代码块/占位符保护、术语表、Markdown/JSON/CSV 批量翻译）
- `agents/writing-assistant` — 写作润色（文本体检评分 + 六模式改写 + 逐句对照报告）
- `agents/grammar-checker` — 中英校对（17 条规则 + 模型语义校对，分级报告）
- `agents/email-assistant` — 邮件助手（eml/mbox 解析、摘要、优先级、待办提取、回复起草）
- `agents/meeting-minutes` — 会议纪要（逐字稿解析、发言人统计、议题切分、决议/待办抽取）
- `agents/weekly-report` — 周报日报（git log + 工作记录采集、分类统计、骨架/模型双模式）

**知识与学习**
- `agents/knowledge-qa` — 知识库问答（倒排索引 + BM25 检索，模型回答带来源引用）
- `agents/doc-summarizer` — 长文摘要（清洗解析 + 抽取式打分 + 模型生成，关键词与结构树）
- `agents/slide-maker` — 幻灯片生成（主题/文档→大纲→Marp 幻灯片，可导出 PDF/PPTX）
- `agents/interview-prep` — 面试准备（分类题库、JD 画像、可复现抽题、复习计划、模拟面试）
- `agents/resume-reviewer` — 简历诊断（结构/量化/日期体检 + JD 技能匹配打分 + 改写示例）

**效率工具**
- `agents/todo-planner` — 任务拆解（目标→WBS/依赖/工作日甘特图/风险清单）
- `agents/prompt-optimizer` — 提示词优化（五要素/模糊词/结构体检 + 评分卡 + 优化版提示词）

## 常见问题
- 删除 Agent 文件夹后列表未即时更新 → SessionStart hook 会在下次会话开始时同步，或手动运行同步脚本。
- hook 未生效（如刚克隆仓库、或配置是本会话中途创建的）→ 手动运行同步脚本，或重启 Claude Code / 打开一次 /hooks。
