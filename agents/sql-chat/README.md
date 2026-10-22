---
name: sql-chat
description: 用中文提问，自动生成只读 SQL 查询本地 SQLite/CSV/JSON，输出结果表与中文结论。
---

# SQL 数据问答助手（sql-chat）

把一个本地数据文件交给它，用中文问问题，它会把问题翻译成一条**只读 SQL**，执行后给你结果表和中文结论。

## 它能做什么

- **多种数据源**：SQLite（`.sqlite` / `.db` / `.sqlite3`）、CSV / TSV、JSON，以及"一个装满 CSV 的目录"（自动变成多张表）
- **自然语言查询**：自动读表结构、写 SQL、执行、解释结果；SQL 报错时把错误原文交回模型修正，最多重试 3 次
- **不调模型也能用**：`--sql` 直接跑一条 SQL，`--show-schema` 只看表结构，`--explain` 看执行计划
- **结果可留存**：`--output 结果.md` 把 SQL、结果表、结论写成 Markdown
- **只读安全**：数据库只读打开，语句白名单，单条语句，结果行数上限 200（可调）

## 怎么用

### 1. 装依赖、设 key

先进入本 agent 目录：`cd agents/sql-chat`（下面命令里的 `agent.py` 都在这个目录下）。

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

> 走第三方 Anthropic 兼容网关时，再设置 `ANTHROPIC_BASE_URL` 和 `ANTHROPIC_AUTH_TOKEN`，并用 `--model` 指定网关支持的模型名。

### 2. 先看表结构

PowerShell：

```powershell
py -3 agent.py .\data\shop.sqlite --show-schema
```

macOS / Linux：

```bash
python3 agent.py ./data/shop.sqlite --show-schema
```

会打印每张表的建表语句、列类型和行数。确认表名和列名后，提问更准。

### 3. 用中文提问

PowerShell：

```powershell
py -3 agent.py .\data\shop.sqlite "每个城市有多少客户？"
```

macOS / Linux：

```bash
python3 agent.py ./data/shop.sqlite "每个城市有多少客户？"
```

输出依次是：**生成的 SQL**（单独代码块，可直接复制）、**结果表**、**中文结论**。结论生成失败时，结果表仍会完整打印。

## 常用示例

```bash
# 1. SQLite：聚合 + 排序（结果超过 200 行会注明已截断）
python3 agent.py ./data/shop.sqlite "每个城市的客户数是多少？按人数从多到少排"

# 2. SQLite：取 Top N
python3 agent.py ./data/shop.sqlite "销售额最高的 5 个订单是哪些？"

# 3. CSV：文件即表，表名是文件名（sales.csv → 表 sales）
python3 agent.py ./data/sales.csv "哪个月的销售额最高？" --limit 50

# 4. 目录：目录下所有 CSV 加载成多张表，适合一次问多份数据
python3 agent.py ./data/exports "所有表里金额列有没有空值？" --table orders
```

不想调模型时：

```bash
python3 agent.py ./data/shop.sqlite --sql "SELECT city, COUNT(*) FROM customers GROUP BY city"
python3 agent.py ./data/shop.sqlite --sql "SELECT * FROM orders" --explain --limit 20
python3 agent.py ./data/shop.sqlite "每个城市的客户数" --output 结果.md
```

中文文件名（如 `订单明细.csv`）清洗后可能只剩空串，此时表名回退为 `data`，也可以用 `--table orders` 改名。

## 安全说明

- SQLite 数据源用 `mode=ro` **只读**打开，不会修改原文件；CSV / JSON 只读入内存
- 只允许 `SELECT`、`WITH`、`EXPLAIN`、`PRAGMA table_info(...)` / `PRAGMA index_list(...)`
- 拒绝 `INSERT` / `UPDATE` / `DELETE` / `DROP` / `CREATE` / `ALTER` / `ATTACH` 等写操作，以及一次提交多条语句
- 结果集默认最多显示 200 行（`--limit` 可调），超出会注明"已截断"
- 被拒绝的语句以退出码 2 结束，不会执行

## 实现方式

1. 按扩展名加载数据源：SQLite 直接只读连接；CSV 用 `csv.Sniffer` 嗅探分隔符、按 `utf-8-sig → utf-8 → gbk → gb18030` 尝试编码；JSON 要求顶层是数组；目录递归收集 CSV/TSV
2. CSV / JSON 统一装入**内存 SQLite**，按值推断 `INTEGER` / `REAL` / `TEXT`，嵌套对象序列化为 JSON 字符串
3. 把表结构（最多 12000 字符）和问题交给模型，要求只输出一条 SQL；剥掉 Markdown 围栏后做白名单校验
4. 执行失败时把 SQLite 报错原文交回模型修正（最多 `--max-retry` 次）；成功后第二次调用模型，把最多 50 行结果转成中文结论

## 文件清单

- `agent.py` — 主程序：加载数据源、只读校验、执行展示、模型调用、`--self-test`
- `README.md` — 本文件
- `requirements.txt` — Python 依赖（仅 anthropic）

## 常见问题

**Q：提示"数据源不存在 / 空文件 / CSV 只有表头 / 列名重复"？**
A：都是数据本身的问题：路径写错、文件是 0 字节、CSV 只有表头没有数据行，或表头里有同名列（SQLite 不允许同名列，需先改表头）。程序会给中文提示并以退出码 1 结束，不会抛 traceback。

**Q：提示 `no such table` 或 `no such column`？**
A：先跑 `--show-schema` 看真实表名和列名。中文或带空格的文件名会被清洗，空名回退为 `data`；也可以 `--table 名字` 重命名单表 CSV。

**Q：为什么查询被拒绝、退出码是 2？**
A：只读白名单只放行 `SELECT` / `WITH` / `EXPLAIN` / 只读 `PRAGMA`，并且一次只能一条语句。把写操作或多条语句拆开，改用查询语句。

**Q：CSV 中文乱码怎么办？**
A：程序会自动尝试常见编码，仍乱码时显式指定：`--encoding gbk`（或 `utf-8`、`gb18030`）。

**Q：模型报 401 / 连接失败 / 限流？**
A：检查 `ANTHROPIC_API_KEY`；走网关时检查 `ANTHROPIC_BASE_URL` 与 `ANTHROPIC_AUTH_TOKEN`，并用 `--model` 指定网关模型名（也可设 `SQL_CHAT_MODEL`）。程序会打印中文错误并以退出码 1 结束。

**Q：能不能不联网自检？**
A：可以，`py -3 agent.py --self-test`（Windows）或 `python3 agent.py --self-test`，不需要 API key，通过后打印 `SELF-TEST OK`。
