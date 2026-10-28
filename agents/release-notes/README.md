---
name: release-notes
description: 发布说明生成：解析 conventional commits，自动分组、推断 semver、统计贡献者，可选模型润色。
---

# 发布说明生成助手（release-notes）

一个本地命令行发布说明 agent：输入 git 提交区间或每行一条 message 的文本文件，先用确定性算法解析 conventional commits、按类型分组、推断 semver 版本号、统计贡献者与改动文件，再（可选）让模型把条目润色成面向用户的描述并生成"亮点"段落，最后输出中文 Markdown 发布说明或 Keep a Changelog 格式片段。除 `anthropic` 外只用标准库。

## 它能做什么

- **conventional commits 解析**：`type(scope)!: subject` 拆出 type / scope / breaking / subject，正文里的 `BREAKING CHANGE:` 也能识别；非规范提交自动归入「其他」
- **分组渲染**：feat→新功能、fix→问题修复、perf→性能、refactor→重构、docs→文档、test→测试、build/ci→构建、chore→杂务、style→样式，组内按 scope 聚合
- **semver 推断**：破坏性变更→major、feat→minor、fix/perf→patch；给了 `--from` 旧版本号就自动算出新版本号，给 `--version` 则校验一致性并告警
- **统计**：提交总数、类型分布、贡献者分布、改动文件 Top10（`--repo` 时用 `git log --numstat` 采集）
- **破坏性变更专区**：单独醒目区块 + 逐条迁移提示模板（排查关键字、回归验证、备份回滚）
- **模型润色（`--polish`）**：把每条改写成面向用户的产品描述、生成"亮点"段落，返回 JSON 解析失败自动回退到原始条目，原文进报告附录
- **两种输出**：完整发布说明（默认）或 `--changelog` 的 Keep a Changelog 格式片段

## 怎么用

### 第一步：安装依赖

```powershell
# Windows PowerShell
cd agents/release-notes
py -3 -m pip install -r requirements.txt
```

```bash
# macOS / Linux
cd agents/release-notes
python3 -m pip install -r requirements.txt
```

### 第二步：设置 API key（只做确定性输出可以跳过）

```powershell
# Windows PowerShell（当前窗口有效）
$env:ANTHROPIC_API_KEY="你的key"
```

```bash
# macOS / Linux
export ANTHROPIC_API_KEY=你的key
```

### 第三步：生成发布说明

```powershell
# Windows PowerShell：读 git 提交区间，推断版本号
py -3 agent.py --repo 你的仓库路径 --from v1.0.0 --to HEAD --version 1.1.0
```

```bash
# macOS / Linux：从文本文件读提交列表，输出 Changelog 片段
python3 agent.py --commits commits.txt --from 1.0.0 --changelog --output CHANGELOG.md
```

结果默认写到当前目录的 `RELEASE_NOTES.md`，用编辑器打开即可。

## 常用示例

```bash
# 1. 从 git 区间生成完整发布说明，版本号自动推断（v1.0.0 + feat → 1.1.0）
python3 agent.py --repo . --from v1.0.0 --to HEAD

# 2. 手头只有一份提交清单（每行一条 message），照样能出报告
python3 agent.py --commits commits.txt --version 2.0.0 --output 发布说明.md

# 3. 调模型把条目润色成面向用户的描述，并生成亮点段落
python3 agent.py --repo . --from v1.0.0 --polish --model qwen3.8-max

# 4. 输出 Keep a Changelog 格式片段，直接粘进 CHANGELOG.md
python3 agent.py --commits commits.txt --from 1.0.0 --changelog --output CHANGELOG.md
```

第 1 条不调模型、不花钱；第 2 条适合 PR 标题、邮件列表等非 git 来源；第 3 条走模型润色，第三方网关用 `--model` 指定模型名；第 4 条输出标准片段，分组名用「中文（English）」双语，方便国内外读者。

## 实现方式

确定性部分在前，模型只做语言生成：

1. **采集**：`collect_git_commits()` 用 `git log --pretty=format:%H%x1f%an%x1f%B%x1e` 拿 hash/作者/完整 message，`collect_file_stats()` 用 `git log --numstat` 统计文件改动行数；`load_commits_file()` 读文本模式（空行与 `#` 注释忽略）。
2. **解析**：`parse_commit_message()` 正则拆 `type(scope)!: subject`，`BREAKING_BODY_RE` 检测正文 `BREAKING CHANGE:`，`Commit` 对象统一存结果；`group_commits()` 按固定顺序分组，「其他」永远最后。
3. **版本与统计**：`infer_bump()` + `bump_version()` 推断 semver，`resolve_version()` 校验 `--version` 与推断结果是否一致并告警；`build_stats()` 汇总贡献者、类型分布、改动文件 Top10。
4. **模型润色（可选）**：`polish_with_model()` 要求返回 JSON `{"highlights":"","entries":{"新功能":[...]}}`，`parse_polish_json()` 解析失败就回退到原始条目，模型原文进报告附录。
5. **渲染**：`build_release_notes()` 输出版本头、亮点、破坏性变更（含 `render_migration()` 迁移提示）、分组条目、贡献者与统计附录；`build_changelog()` 输出 Keep a Changelog 片段。

模型调用带三级参数降级：先试 `beta.messages.create`（adaptive thinking + server-side fallback），失败退到 `messages.create(thinking=...)`，再失败退到最基础的调用，兼容不认新参数的第三方网关。

## 文件清单

- `agent.py` — 主程序：commit 解析、分组渲染、semver 推断、统计采集、模型润色、两种格式输出、CLI 与离线自检
- `README.md` — 本说明
- `requirements.txt` — Python 依赖（仅 `anthropic>=0.112.0`）

## 常见问题

**1. 不想调模型（或没有 API key）能用吗？**
能。不加 `--polish` 就是纯确定性流程，永远不联网；加 `--offline` 还会显式跳过模型。加了 `--polish` 又检测不到 `ANTHROPIC_API_KEY` / `ANTHROPIC_AUTH_TOKEN`，程序会打印中文设置提示（含 macOS `export` 与 PowerShell `$env:` 两种写法）后退出。

**2. `--version` 和 `--from` 推出来的版本号对不上怎么办？**
以 `--version` 为准输出，同时在终端和报告「提示」一节里告警，说明按 semver 应该是什么版本，方便你人工判断是版本号写错了还是提交类型标错了。

**3. 提交信息不规范，全都进了「其他」？**
非 conventional commits（没有 `type:` 前缀）会被归入「其他」，这是刻意行为，避免瞎猜类型。想让分组好看，提交时用 `feat:` / `fix:` / `docs:` 这类前缀；`BREAKING CHANGE:` 写在正文里也会被识别为破坏性变更。

**4. 走第三方 Anthropic 兼容网关怎么配？**
设置网关要求的 `ANTHROPIC_BASE_URL` 和 `ANTHROPIC_AUTH_TOKEN`（或 `ANTHROPIC_API_KEY`），再用 `--model` 指定网关支持的模型名，例如 `--model qwen3.8-max`。三级参数降级会自动绕过网关不认识的 thinking / fallbacks 参数。

**5. 改动文件统计为什么是空的？**
文件统计只在 git 模式（`--repo`）下采集；`--commits` 文本模式没有文件信息，附录里会写明原因。另外二进制文件按 1 行计入。

**6. Windows 上 `python` / `python3` 没反应？**
本机的 `python` 可能是 Microsoft Store 的占位桩，统一改用 `py -3 agent.py ...`。打印中文乱码时程序已做 UTF-8 兜底，仍乱码请把终端编码切成 UTF-8。
