---
name: doc-writer
description: 先静态扫描项目结构，再让模型生成 README、接口文档、架构说明和新人上手指南四类文档。
---

# 项目文档生成助手（doc-writer）

给它一个项目目录，它先用纯本地的正则和文件遍历理清项目骨架，再让模型基于这份结构化摘要写出四类项目文档。

扫描阶段不联网、不调用模型，所以可以先用 `--dry-run` 白嫖一次完整的项目探查结果。

## 它能做什么

- **README.md**：项目定位、功能特性、环境要求、安装、快速开始、目录说明、配置项表格、常见问题
- **API.md**：接口清单表（方法 / 路径 / 说明 / 来源文件:行号），每个接口给请求与响应示例，推断出来的示例会明确标注
- **ARCHITECTURE.md**：分层结构、关键模块职责、数据流与调用链、技术选型说明
- **ONBOARDING.md**：新人上手五步、本地跑起来的最小路径、常见坑

四份文档的素材都来自同一份静态扫描摘要：目录树、语言统计、入口点、依赖清单、API 路由、CLI 参数、配置项名字。

## 怎么用

### 第一步：装依赖、设 key

先进入本 agent 目录：`cd agents/doc-writer`（下面命令里的 `agent.py` 都在这个目录下）。

```powershell
# Windows PowerShell
py -3 -m pip install -r requirements.txt
$env:ANTHROPIC_API_KEY="你的key"
```

```bash
# macOS / Linux
python3 -m pip install -r requirements.txt
export ANTHROPIC_API_KEY=你的key
```

### 第二步：先 `--dry-run` 预览

只做静态扫描，打印摘要，不调用模型、不写文件、不花 token。

```powershell
# Windows
py -3 agent.py ./my-project --dry-run
```

```bash
# macOS / Linux
python3 agent.py ./my-project --dry-run
```

看到的摘要如果明显不对（比如路由一条没抓到），先别生成，检查项目路径和 `--max-files`。

### 第三步：真正生成

`--type all --force` 表示四类全生成、已存在也覆盖。

```powershell
# Windows
py -3 agent.py ./my-project --type all --force
```

```bash
# macOS / Linux
python3 agent.py ./my-project --type all --force
```

默认写到当前目录下的 `docs/`。已存在的文件不加 `--force` 会跳过并提示，不会偷偷覆盖。

## 常用示例

1. 只看扫描结果，不花钱

   ```powershell
   py -3 agent.py ./my-project --dry-run
   ```

2. 只生成一份 README，并直接指定文件名

   ```powershell
   py -3 agent.py ./my-project --type readme --output ./README.generated.md
   ```

3. 生成英文接口文档

   ```powershell
   py -3 agent.py ./my-project --type api --lang en
   ```

4. 四类全生成，覆盖旧文件，并换模型省钱

   ```powershell
   py -3 agent.py ./my-project --type all --force --model claude-sonnet-5
   ```

   macOS / Linux 把 `py -3` 换成 `python3` 即可。

## 它会自动识别什么

| 识别项 | 来源 | 说明 |
| --- | --- | --- |
| 目录树 | 文件系统遍历 | 深度 ≤3、最多 60 行，忽略 `.git`、`node_modules`、`__pycache__`、`dist`、`build`、`venv`、`target` 等 |
| 语言统计 | 扩展名 | `.py/.js/.ts/.tsx/.jsx/.java/.go/.rs/.rb/.php/.cs/.vue/.css/.html/.sh`，统计文件数与代码行数（跳过空行和纯注释行） |
| 入口点 | 固定文件名 + 清单文件 | `main.py`、`app.py`、`manage.py`、`index.ts`、`server.js`、`Dockerfile`、`Makefile`，以及 `package.json` 的 `scripts`、`pyproject.toml` 的 `[project.scripts]` |
| 依赖 | 6 种清单文件 | `requirements.txt`、`pyproject.toml`、`package.json`（含 devDependencies）、`go.mod`、`pom.xml`、`Cargo.toml`、`Pipfile` |
| API 路由 | 正则 | FastAPI 装饰器、Flask `@app.route`、Express `app.get` / `router.post`、Spring `@GetMapping` / `@RequestMapping`，输出 文件:行号 + 方法 + 路径 + 处理函数名 |
| CLI 参数 | 正则 | `argparse.add_argument("--x")`、click `@click.option("--x")`、commander `program.option('--x')` |
| 配置项名字 | `.env.example` / `config.*` / `settings.py` | **只提取大写变量名，不读取也不输出值**，避免把密钥写进文档 |

## 实现方式

分成严格的两个阶段：

1. **确定性扫描**（`scan_project()`）：只用 `os.walk`、`pathlib` 和正则，纯本地、可重复，不联网。扫描结果经 `build_summary()` 压成一份 ≤30000 字符的 JSON 摘要。
2. **模型生成**：把摘要和 `--type` 对应的写作指令发给模型，一份文档一次调用。模型调用走 `call_model()` 的三级降级（adaptive thinking + fallback → 只带 thinking → 最基础调用），兼容官方 API 和第三方兼容网关。

`--dry-run` 在第一阶段结束后就返回，因此不会产生任何网络请求。

## 文件清单

- `agent.py` — 主程序：静态扫描 + 摘要构建 + 模型生成 + CLI
- `README.md` — 本文件
- `requirements.txt` — Python 依赖（只有 anthropic）

## 常见问题

**Q：没有 API key 能用吗？**
能，`--dry-run` 完全不需要 key，也不联网。真正生成时才需要 `ANTHROPIC_API_KEY`（走第三方网关则用 `ANTHROPIC_AUTH_TOKEN`）。

**Q：会不会把 `.env` 里的密钥写进文档？**
不会。配置项扫描只匹配大写变量名，值从头到尾没被读取过；提示词里也明确要求模型只写变量名和占位符，自测里有断言专门检查这一点。

**Q：生成的路由表不全怎么办？**
路由是正则提取的，只认 FastAPI / Flask / Express / Spring 这四种写法，且不支持多行装饰器。Django 的 `urls.py`、gRPC、GraphQL、动态拼接的路径都不会被识别；这种情况把 `--type api` 的结果当草稿，人工补全。

**Q：文档写得不准确？**
先看 `--dry-run` 的摘要对不对。摘要里没有的信息模型也不该编，但推断性的接口示例可能和真实响应有出入，这类示例都会标注「示例为推断值」，上线前请对照代码核对。

**Q：能换模型吗？**
可以。`--model qwen3.8-max` 或设置环境变量 `DOC_WRITER_MODEL`，不传则默认 `claude-opus-5`。
