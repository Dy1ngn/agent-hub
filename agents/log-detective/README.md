---
name: log-detective
description: 流式扫描多种格式日志并聚合错误签名、时间分布与访问日志指标，再由 Claude 给出根因假设和排查命令的日志排查助手。
---

# 日志排查助手（log-detective）

给它一个或多个日志文件，它先把日志「读薄」：流式扫描、识别格式、归一化错误签名并做确定性聚合；然后把**统计摘要**（不是全文）交给 Claude，产出一份带根因假设和排查命令的 Markdown 报告。

## 它能做什么

- **流式扫描**：逐行读取，百万行也不吃内存；`.gz` 直接读，`utf-8`/`gbk` 自动识别兜底
- **混合格式**：同一次运行里 Python logging、ISO8601、Java/log4j、nginx access、JSON lines 可以混着来
- **错误签名聚合**：把数字、IP、UUID、时间戳、URL 里的 ID 归一化成占位符，同类错误自动合并，输出次数和首末时间
- **时间分布**：按 1m/5m/1h 分桶，找出前 5 个错误爆发桶（相对峰值）
- **访问日志专节**：状态码分布（2xx/3xx/4xx/5xx）、最慢 10 个请求、错误率
- **跨文件时间线**：多文件错误按时间排序（最多 50 条）
- **省 token**：`--stats-only` 完全离线、不发任何数据；发给模型的统计摘要 ≤20000 字符

## 怎么用

### 第一步：装依赖、设 key

先进入本 agent 目录：`cd agents/log-detective`（下面命令里的 `agent.py` 都在这个目录下）。

PowerShell：

```powershell
py -3 -m pip install -r requirements.txt
$env:ANTHROPIC_API_KEY = "你的key"
```

macOS / Linux：

```bash
python3 -m pip install -r requirements.txt
export ANTHROPIC_API_KEY=你的key
```

### 第二步：先跑统计（不花 token、不联网）

PowerShell：

```powershell
py -3 agent.py app.log access.log --stats-only
```

macOS / Linux：

```bash
python3 agent.py app.log access.log --stats-only
```

终端会直接打印概览和前 10 条错误签名，足够快速定位问题。

### 第三步：带模型分析出报告

PowerShell：

```powershell
py -3 agent.py app.log access.log --output 排查报告.md
```

macOS / Linux：

```bash
python3 agent.py app.log access.log --output 排查报告.md
```

报告结构固定：概览表 → 级别分布 → Top 错误签名 → 错误时间分布 →（访问日志专节）→ 跨文件时间线 → 模型分析。

## 常用示例

```powershell
# 1. 多文件混合摸底：Python 日志 + nginx access 一起看
py -3 agent.py logs/app.log logs/access.log --stats-only

# 2. 只看 ERROR，缩小排查范围（--top 控制展开几条签名）
py -3 agent.py app.log --level-min ERROR --top 20

# 3. 压缩日志 + 时间窗口：只看 10 月 20 号当天、按 5 分钟分桶
py -3 agent.py app.log.gz --since 2026-10-20 --until "2026-10-20 23:59:59" --bucket 5m

# 4. 走第三方 Anthropic 兼容网关（如阿里云）：换模型名
py -3 agent.py app.log --model qwen3.8-max --output 排查报告.md
```

说明：

- `--grep REGEX` 可先按关键词过滤（如 `--grep "payment|timeout"`），只处理匹配行
- `--max-lines` 默认 500000，超过会截断并在报告里注明「已截断」
- 报告默认写 `log-report.md`

## 支持的日志格式

| 格式 | 样例 |
| --- | --- |
| Python logging | `2026-10-20 12:00:01,123 ERROR module.name: message` |
| ISO8601 | `2026-10-20T12:00:01.123Z ERROR message`（也支持 `+08:00`） |
| Java / log4j | `12:00:01.123 [thread-1] ERROR com.x.Y - message` |
| nginx / apache access | `1.2.3.4 - - [20/Oct/2026:12:00:01 +0800] "GET /api/x HTTP/1.1" 500 123 0.032` |
| JSON lines | `{"time":"...","level":"error","msg":"...","service":"..."}` |
| 兜底关键词行 | 行内含 `ERROR`/`WARN`/`FATAL`/`CRITICAL`/`Exception`/`Traceback` |

堆栈续行（以空白开头、跟在错误行后面的 `File "...", line 42` 之类）会并入上一条错误的样例，不单独计数。

## 报告长什么样（节选）

````markdown
## 一、概览

| 项目 | 值 |
| --- | --- |
| 扫描文件数 | 4 |
| 总行数（已读取） | 20 |
| ERROR / WARN | 11 / 1 |
| 时间跨度 | 2026-10-20 09:59:58 → 2026-10-20 10:01:05（约 1 分 7 秒） |

## 三、Top 错误签名

### 1. `upstream timeout after 30s order=<num>` — 2 次

- 级别：ERROR
- 首次：2026-10-20 10:00:00
- 末次：2026-10-20 10:00:31
- 首次出现位置：`app.log:3`
- 样例原文：

```text
2026-10-20 10:00:00,010 ERROR api.handler: upstream timeout after 30s order=10023
```

## 四、错误时间分布

| 时间桶（1 分钟） | 错误条数 | 相对峰值 |
| --- | ---: | ---: |
| 2026-10-20 10:00:00 | 8 | 100% |
| 2026-10-20 10:01:00 | 1 | 12% |

## 七、模型分析

### 1. 最可能的根因假设
...（Claude 输出，按可能性排序并给依据）
````

## 实现方式

两段式设计，**先确定性、后模型**：

1. **确定性统计**（纯标准库）：`io.TextIOWrapper` + `gzip` 流式读取并嗅探编码 → 一组正则在「最具体到最宽松」顺序上识别格式 → 归一化消息签名（`<num>` `<hex>` `<uuid>` `<ip>` `<ts>` `<url>` `<path>`）→ 聚合级别、签名、时间桶、状态码；时间线和最慢请求用定长堆保留，只为大文件占用常量内存
2. **模型分析**：把统计摘要（≤20000 字符）发给 Claude，要求输出「根因假设 / 关键证据 / 下一步命令（Linux+PowerShell）/ 还需补充的信息」四节；模型不可用时报告照常生成，并标注「模型分析不可用」
3. **参数降级兼容**：`call_model()` 按「adaptive thinking + fallbacks → thinking → 裸调用」三级降级，官方 API 和第三方兼容网关都能跑

## 文件清单

- `agent.py` — 主程序（扫描、解析、聚合、报告、模型分析、`--self-test`）
- `README.md` — 本说明
- `requirements.txt` — 唯一依赖 `anthropic`

## 常见问题

**1. 没有 API key 能用吗？**
能。`--stats-only` 完全离线，只做统计和报告，不需要联网也不需要 key。

**2. 模型分析失败了，统计结果会不会丢？**
不会。报告一定先落盘，模型那一节写「模型分析不可用：原因」。但为了提醒你，这种情况下退出码是 1（统计和报告本身已成功）。

**3. 报告里的时间跨度看起来不对 / 跨到了今天？**
Java/log4j 风格只有时间没有日期。程序会优先沿用**同一文件里前面出现过的日期**，一次都没出现过才用文件的修改日期补，并在报告「统计口径与已知限制」里注明条目数。跨天轮转的日志建议先用 `--since/--until` 限定范围。

**4. 带时区的日志（`+08:00`、`Z`）怎么算的？**
按日志里写的墙上时间聚合，时区偏移不参与换算。同一台机器、同一时区的日志没有影响；混合多时区日志时排序可能有偏差。

**5. 大文件会不会读爆内存？**
不会。逐行流式处理，只保留聚合结果和有上限的堆；`--max-lines` 再兜一层，截断会在报告里注明。

**6. 走阿里云等兼容网关报 404/400 怎么办？**
先确认 `ANTHROPIC_BASE_URL` 指向网关、模型名是网关支持的（如 `--model qwen3.8-max`）。程序已经会自动降级去掉 `thinking`/`fallbacks` 等新参数，若仍失败可先用 `--stats-only` 拿到统计结果。

**7. Windows 上中文输出乱码？**
输出被重定向到文件/管道时程序会自动切 UTF-8；若在旧版 cmd 里仍乱码，用 `chcp 65001` 或改用 Windows Terminal。
