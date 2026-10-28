---
name: dependency-auditor
description: 解析多种依赖清单并按内置规则做风险审计，输出评分、风险清单与升级建议的中文报告。
---

# 依赖风险审计助手（dependency-auditor）

一个本地命令行依赖审计 agent：先把项目里的依赖清单（requirements.txt / pyproject.toml / package.json / Pipfile / Pipfile.lock）全部解析出来，再用 9 条确定性风险规则逐条检查（无版本约束、过宽约束、固定旧版本、git/本地路径依赖、疑似 typosquat 投毒包、重复声明冲突、许可证缺失或有传染风险、dev 依赖混入生产、命名不规范），按权重算出 0-100 风险分与等级，最后可选让模型给出中文升级/替换建议。除 `anthropic` 外只用标准库。

## 它能做什么

- **多清单解析**：支持 `requirements.txt`（含 `-r` 引用、环境标记 `; python_version<...`、extras `requests[socks]`、URL/git/本地路径依赖）、`pyproject.toml`（PEP 621 与 poetry 两种写法，3.9 下用正则解析，不依赖 tomllib）、`package.json`（dependencies / devDependencies / peerDependencies）、`Pipfile` 与 `Pipfile.lock`
- **9 条内置风险规则**：`RULE_SPECS` 表驱动，每条带级别、权重、说明和修复建议；规则命中不依赖模型
- **typosquat 检测**：包名与内置流行包表做编辑距离（相邻换位算 1 次编辑）比较，`requets` → `requests`、`lodahs` → `lodash` 都能认出来，且不会把常见包自己误报
- **本地安全公告匹配**：`--advisory` 传一个 JSON（`[{package, versions, severity, title, link}]`），按包名 + 版本范围命中，无固定版本时提示“可能受影响”
- **评分与分级**：按规则权重累加（单条规则最多计 5 次）得 0-100 风险分，60 分以上「风险」、25-59「警告」、25 以下「提示」，`--min-level` 可过滤报告
- **模型升级建议**：对命中项让模型给中文升级/替换建议（返回严格 JSON），解析失败不崩溃，原文进报告附录
- **离线自检**：`--self-test` 在临时目录造样本，跑通全部确定性逻辑，不联网也不需要 API key

## 怎么用

### 第一步：安装依赖

```powershell
# Windows PowerShell
cd agents/dependency-auditor
py -3 -m pip install -r requirements.txt
```

```bash
# macOS / Linux
cd agents/dependency-auditor
python3 -m pip install -r requirements.txt
```

### 第二步：设置 API key（只在要模型建议时需要）

```powershell
# Windows PowerShell（当前窗口有效）
$env:ANTHROPIC_API_KEY="你的key"
```

```bash
# macOS / Linux
export ANTHROPIC_API_KEY=你的key
```

走第三方 Anthropic 兼容网关时，用 `--model` 指定网关侧的模型名即可，例如 `--model qwen3.8-max`。
只想跑确定性审计的话跳过这一步，直接加 `--offline`。

### 第三步：运行审计

```powershell
# Windows PowerShell
py -3 agent.py . --offline --output 审计报告.md
```

```bash
# macOS / Linux
python3 agent.py . --output 审计报告.md
```

报告默认写到当前目录的 `audit-report.md`，用编辑器打开即可。

## 常用示例

```bash
# 1) 审计整个项目里的所有依赖清单，纯离线、不花钱
python3 agent.py . --offline --output 审计报告.md

# 2) 只看比较要紧的问题：过滤掉「提示」级别
python3 agent.py ./requirements.txt --min-level 警告

# 3) 带上本地安全公告一起比对（公告 JSON 放在仓库外也可以）
python3 agent.py . --advisory ~/security/advisories.json

# 4) 走第三方兼容网关生成升级建议（模型名按网关要求填）
python3 agent.py ./package.json --model qwen3.8-max --output npm审计.md
```

## 实现方式

1. **发现清单**：`discover_manifests()` 在给定文件/目录里找依赖清单（递归跳过 `.git`、`node_modules` 等目录），`is_manifest_name()` 判定文件名。
2. **解析依赖**：`parse_requirements_file()`（支持 `-r` 递归引用与循环保护）、`parse_pyproject_file()`（正则抠 PEP 621 数组与 poetry 表）、`parse_package_json()`、`parse_pipfile()`、`parse_pipfile_lock()`，统一产出 `Dependency` 对象；`collect_dependencies()` 去重后汇总。
3. **规则扫描**：`scan_dependencies()` 按 `RULE_SPECS` 逐条跑：`is_unpinned()` / `is_overbroad()` 判约束、`extract_pinned_version()` + `POPULAR_VERSIONS` 判旧版本、`is_typosquat()` + `edit_distance()` 判抢注包名、`LICENSE_TABLE` + `RISKY_LICENSE_RE` 判许可证、`DEV_ONLY_PACKAGES` 判 dev 混入、再加重复声明冲突与命名规范。
4. **公告匹配**：`parse_advisory_file()` 读本地公告，`match_advisories()` 用 `constraint_matches()` / `constraints_overlap()` 判断版本范围是否命中。
5. **评分**：`compute_score()` 按规则权重累加（单条规则最多计 5 次）封顶 100，`score_level()` 映射等级，`filter_by_level()` 按 `--min-level` 过滤。
6. **模型建议**：`generate_advice()` 把命中项压成 JSON 交给 `call_model()`（三级降级，兼容官方 API 与第三方网关），`parse_advice_json()` 兜底解析，失败时原文进报告附录。
7. **报告**：`build_report()` 生成中文 Markdown（概览、风险清单、逐包明细、规则命中统计、建议、附录规则表）。

## 文件清单

- `agent.py` —— 全部实现：清单解析、9 条风险规则、公告匹配、评分、模型建议、报告生成、离线自检
- `README.md` —— 本说明文件
- `requirements.txt` —— 依赖清单（只有 anthropic SDK）

## 常见问题

**Q：不给 API key 能用吗？**
A：能。加 `--offline` 就是纯确定性模式，解析、规则、评分、报告全部本地完成；不加 `--offline` 又没 key 时会打印中文提示并退出。

**Q：我的项目用 poetry/pdm，写在 pyproject.toml 里，能识别吗？**
A：能。PEP 621 的 `dependencies` 与 `[project.optional-dependencies]`、poetry 的 `[tool.poetry.dependencies]` 与 dev-dependencies 都支持；poetry 的 `^1.2` 这类约束会原样保留到报告里。Python 3.9 没有 tomllib，所以用正则解析，遇到特别奇怪的写法可能会漏，报告的「解析备注」会提示。

**Q：为什么把 `chardet` 报成有风险？**
A：内置许可证对照表里 chardet 是 LGPL-2.1，对商业闭源分发有传染风险，所以给「警告」；如果是内部服务不分发，可以忽略或在 `--min-level` 里过滤掉。想改判可以直接改 `LICENSE_TABLE`。

**Q：typosquat 检测会不会把正常包误报？**
A：不会报内置流行包表里的名字本身（先查表直接放行），只有「不在表里、且与某个流行包编辑距离 ≤1」才命中。如果业务里真有个叫 `reqests` 的内部包，也会被报——那正是这类检测的目的，人工确认后忽略即可。

**Q：公告 JSON 该长什么样？**
A：数组形式，每项形如 `{"package": "requests", "versions": ">=2.0,<2.26", "severity": "风险", "title": "…", "link": "…"}`；`versions` 也可以是多个范围字符串的数组（满足任意一个就算命中），`severity` 取 提示/警告/风险 之一。

**Q：报告里的风险分是怎么算的？**
A：按命中规则的权重累加（DA005 typosquat 权重最高 15，DA009 命名规范最低 3），同一条规则最多计 5 次以免刷分，最后封顶 100。分数只是排序参考，具体以风险清单为准。
