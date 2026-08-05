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

共 1 个 Agent：

| Agent | 描述 |
| --- | --- |
| [file-butler](agents/file-butler/) | 本地文件管家：授权一个文件夹后，用自然语言命令让它列出/读取/搜索/写入其中的文件，自动完成整理、汇总、生成报告等任务。基于 Claude function-call，不使用向量库。 |

<!-- AGENTS:END -->

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
