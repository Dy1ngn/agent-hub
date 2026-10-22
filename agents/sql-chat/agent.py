#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
SQL 数据问答助手 —— 用自然语言查询本地 SQLite / CSV / JSON

把一个数据源（SQLite 文件、CSV/TSV、JSON，或一个装满 CSV 的目录）交给它，
用中文提问，它会把问题翻译成一条只读 SQL、执行并给出结果表和中文结论。

用法：
    py -3 agent.py <数据源> "<问题>" [选项]
    py -3 agent.py <数据源> --sql "SELECT ..." [选项]
    py -3 agent.py <数据源> --show-schema
    py -3 agent.py --self-test

示例：
    py -3 agent.py ./data/shop.sqlite "每个城市的客户数是多少？"
    py -3 agent.py ./data/sales.csv "销售额最高的 5 个订单"
    py -3 agent.py ./data/exports "所有表里金额列有没有空值？"
    py -3 agent.py ./data/shop.db --sql "SELECT COUNT(*) FROM orders" --limit 10
    py -3 agent.py ./data/shop.db --show-schema

环境要求：
    1. 设置 ANTHROPIC_API_KEY 环境变量；如果走第三方 Anthropic 兼容网关，
       通常需要设置 ANTHROPIC_BASE_URL 和 ANTHROPIC_AUTH_TOKEN。
    2. 安装 SDK：py -3 -m pip install -r requirements.txt

安全边界：
    - SQLite 数据源始终以只读模式打开，不会写回原文件。
    - 只允许 SELECT / WITH / EXPLAIN / PRAGMA table_info / PRAGMA index_list。
    - 一次只允许一条语句，结果行数默认限制 200 行。
"""

import argparse
import csv
import io
import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import unicodedata
from pathlib import Path

import anthropic

# 默认模型（可用 --model 参数或 SQL_CHAT_MODEL 环境变量更换）
DEFAULT_MODEL = os.environ.get("SQL_CHAT_MODEL", "claude-opus-5")

DEFAULT_LIMIT = 200          # 结果表默认最多显示多少行
DEFAULT_MAX_RETRY = 3        # SQL 生成/执行失败后最多再重试几轮
SCHEMA_MAX_CHARS = 12000     # 喂给模型的表结构最多多少字符
CONCLUSION_ROWS = 50         # 生成结论时最多送给模型多少行结果
MAX_CELL_WIDTH = 40          # 结果表单元格最大显示宽度

# CSV/TSV 自动编码探测顺序（gb18030 是 gbk 的超集，放在最后兜底）
ENCODING_CANDIDATES = ("utf-8-sig", "utf-8", "gbk", "gb18030")

SQLITE_SUFFIXES = (".sqlite", ".db", ".sqlite3")
CSV_SUFFIXES = (".csv", ".tsv")
SUPPORTED_SUFFIXES = SQLITE_SUFFIXES + CSV_SUFFIXES + (".json",)

# 写操作/危险语句关键字（在去掉字符串、注释、replace() 函数之后按单词匹配）
BANNED_KEYWORDS = frozenset([
    "INSERT", "UPDATE", "DELETE", "DROP", "CREATE", "ALTER", "ATTACH",
    "DETACH", "VACUUM", "REPLACE", "REINDEX", "TRUNCATE",
])


class SqlChatError(Exception):
    """本程序主动抛出的、需要以中文提示用户的错误"""

    def __init__(self, message, exit_code=1):
        super(SqlChatError, self).__init__(message)
        self.message = message
        self.exit_code = exit_code


class DataSourceError(SqlChatError):
    """数据源不存在、为空、格式不合法等"""


class UnsafeSqlError(SqlChatError):
    """SQL 未通过只读安全检查，退出码固定为 2"""

    def __init__(self, message):
        super(UnsafeSqlError, self).__init__(message, 2)


class QueryError(SqlChatError):
    """SQL 本身合法，但 SQLite 执行失败"""


class TableInfo(object):
    """一张表的展示信息：表名、建表语句、列类型、行数"""

    def __init__(self, name, ddl, columns, row_count):
        self.name = name
        self.ddl = ddl
        self.columns = columns      # [(列名, 类型), ...]
        self.row_count = row_count


class Database(object):
    """统一的查询入口：SQLite 用只读连接，CSV/JSON 用内存 sqlite"""

    def __init__(self, conn, tables, label):
        self.conn = conn
        self.tables = tables
        self.label = label
        self.focused = None         # --table 指定的主表

    def close(self):
        try:
            self.conn.close()
        except sqlite3.Error:
            pass

    def select_tables(self):
        """--table 指定了就只看那一张表，否则返回全部"""
        if self.focused is not None:
            return [self.focused]
        return list(self.tables)

    def focus(self, name):
        for table in self.tables:
            if table.name == name:
                self.focused = table
                return
        available = "、".join(t.name for t in self.tables) or "（无）"
        raise DataSourceError("找不到表「%s」。可用的表：%s" % (name, available))


# ---------- 通用小工具 ----------

def quote_ident(name):
    """把标识符安全地加双引号（列名里可能有引号或中文）"""
    return '"%s"' % str(name).replace('"', '""')


def sanitize_table_name(raw):
    """文件名 → 合法表名：转小写，非字母数字下划线替换为 _"""
    name = re.sub(r"[^A-Za-z0-9_]", "_", str(raw).lower())
    name = re.sub(r"_+", "_", name).strip("_")
    return name or "data"


def unique_table_name(base, used):
    """目录里可能有同名 CSV，追加 _2/_3 去重"""
    name = base
    index = 2
    while name in used:
        name = "%s_%d" % (base, index)
        index += 1
    used.add(name)
    return name


def decode_text(path, encoding):
    """按 --encoding 读取文本；auto 时依次尝试常见中文编码"""
    try:
        raw = path.read_bytes()
    except OSError as e:
        raise DataSourceError("读取文件失败：%s（%s）" % (path, e))
    if not raw.strip():
        raise DataSourceError("数据源是空文件：%s" % path)

    if encoding == "auto":
        for enc in ENCODING_CANDIDATES:
            try:
                return raw.decode(enc)
            except UnicodeDecodeError:
                continue
        raise DataSourceError(
            "无法识别文件编码，请用 --encoding 指定（utf-8 / gbk / gb18030）：%s" % path
        )
    try:
        return raw.decode(encoding)
    except LookupError:
        raise DataSourceError("不支持的编码：%s" % encoding)
    except UnicodeDecodeError:
        raise DataSourceError(
            "按 %s 解码失败，可改用 --encoding auto 或 --encoding gbk：%s" % (encoding, path)
        )


def normalize_headers(header, source_label):
    """清理表头，空列名自动补 column_N，重复列名直接报错"""
    names = []
    for index, cell in enumerate(header, 1):
        name = (cell or "").strip().lstrip("\ufeff")
        if not name:
            name = "column_%d" % index
        names.append(name)

    seen = {}
    for name in names:
        key = name.lower()
        if key in seen:
            raise DataSourceError(
                "列名重复：「%s」（%s）。SQLite 不允许同名列，请先修正表头。"
                % (name, source_label)
            )
        seen[key] = True
    return names


def rows_with_width(rows, width):
    """把每行对齐到表头宽度：短的补空串，长的截断多余列"""
    result = []
    for row in rows:
        cells = list(row[:width])
        if len(cells) < width:
            cells.extend([""] * (width - len(cells)))
        result.append(cells)
    return result


# ---------- 类型推断（CSV/JSON 统一走这里） ----------

INT_PATTERN = re.compile(r"^[+-]?\d+$")
FLOAT_PATTERN = re.compile(r"^[+-]?(?:\d+\.\d*|\.\d+|\d+)(?:[eE][+-]?\d+)?$")


def is_int_text(text):
    text = text.strip()
    if not INT_PATTERN.match(text):
        return False
    digits = text.lstrip("+-")
    if len(digits) > 19:
        return False  # 超出 sqlite INTEGER 范围，按文本处理
    return -2 ** 63 <= int(text) <= 2 ** 63 - 1


def is_float_text(text):
    text = text.strip()
    if not FLOAT_PATTERN.match(text):
        return False
    try:
        float(text)
    except ValueError:
        return False
    return True


def infer_types(rows, column_count):
    """逐列推断：全是整数 → INTEGER，全是数字 → REAL，否则 TEXT；全空 → TEXT"""
    types = []
    for index in range(column_count):
        values = [row[index].strip() for row in rows if row[index].strip() != ""]
        if not values:
            types.append("TEXT")
        elif all(is_int_text(v) for v in values):
            types.append("INTEGER")
        elif all(is_float_text(v) for v in values):
            types.append("REAL")
        else:
            types.append("TEXT")
    return types


def convert_value(text, type_name):
    """把 CSV/JSON 的字符串值转换成 sqlite 能存的 Python 值"""
    stripped = text.strip()
    if stripped == "":
        return None
    if type_name == "INTEGER":
        return int(stripped)
    if type_name == "REAL":
        return float(stripped)
    return text


# ---------- 各数据源的加载 ----------

def load_source(path_str, encoding, table_override):
    """根据路径类型分发到具体 loader"""
    try:
        path = Path(path_str).expanduser()
    except (ValueError, OSError) as e:
        raise DataSourceError("无法解析数据源路径：%s（%s）" % (path_str, e))

    if not path.exists():
        raise DataSourceError("数据源不存在：%s" % path)
    if path.is_dir():
        return load_directory(path, encoding, table_override)
    if not path.is_file():
        raise DataSourceError("数据源既不是文件也不是目录：%s" % path)

    try:
        size = path.stat().st_size
    except OSError as e:
        raise DataSourceError("无法读取数据源信息：%s（%s）" % (path, e))
    if size == 0:
        raise DataSourceError("数据源是空文件：%s" % path)

    suffix = path.suffix.lower()
    if suffix in SQLITE_SUFFIXES:
        return load_sqlite(path, table_override)
    if suffix in CSV_SUFFIXES:
        return load_single_csv(path, encoding, table_override)
    if suffix == ".json":
        return load_json(path, encoding, table_override)
    raise DataSourceError(
        "不支持的数据源类型「%s」。支持 .sqlite/.db/.sqlite3、.csv/.tsv、.json 或目录。"
        % (suffix or "无扩展名")
    )


def load_sqlite(path, table_override):
    """以只读模式打开 SQLite，绝不写回原文件"""
    uri_path = str(path.resolve()).replace("\\", "/")
    try:
        conn = sqlite3.connect("file:%s?mode=ro" % uri_path, uri=True)
    except sqlite3.Error as e:
        raise DataSourceError("无法以只读方式打开 SQLite 数据库：%s（%s）" % (path, e))

    tables = []
    try:
        rows = conn.execute(
            "SELECT name, sql FROM sqlite_master "
            "WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ).fetchall()
        if not rows:
            raise DataSourceError("数据库里没有可用的表：%s" % path)

        for name, ddl in rows:
            columns = [
                (row[1], row[2])
                for row in conn.execute("PRAGMA table_info(%s)" % quote_ident(name))
            ]
            count = conn.execute(
                "SELECT COUNT(*) FROM %s" % quote_ident(name)
            ).fetchone()[0]
            if not ddl:
                ddl = "-- 建表语句不可用（可能是虚拟表）：%s" % name
            tables.append(TableInfo(name, ddl, columns, count))
    except DataSourceError:
        conn.close()
        raise
    except sqlite3.DatabaseError as e:
        conn.close()
        raise DataSourceError("不是有效的 SQLite 数据库（%s）：%s" % (e, path))
    except sqlite3.Error as e:
        conn.close()
        raise DataSourceError("读取 SQLite 表结构失败：%s" % e)

    db = Database(conn, tables, str(path))
    if table_override:
        db.focus(table_override)
    return db


def _csv_delimiter(text, path):
    """优先用 csv.Sniffer 嗅探分隔符，失败则按扩展名猜"""
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",\t;|")
        if dialect.delimiter:
            return dialect.delimiter
    except csv.Error:
        pass
    return "\t" if path.suffix.lower() == ".tsv" else ","


def read_csv_spec(path, encoding, table_name):
    """读取一个 CSV/TSV，返回 (表名, 表头, 字符串行列表)"""
    text = decode_text(path, encoding)
    delimiter = _csv_delimiter(text, path)

    try:
        reader = csv.reader(io.StringIO(text), delimiter=delimiter)
        try:
            header = next(reader)
        except StopIteration:
            raise DataSourceError("CSV 是空文件：%s" % path)
        raw_rows = list(reader)
    except csv.Error as e:
        raise DataSourceError("CSV 解析失败：%s（%s）" % (e, path))

    if not header:
        raise DataSourceError("CSV 没有表头：%s" % path)

    headers = normalize_headers(header, str(path))
    rows = []
    for raw in raw_rows:
        if not raw or not any(cell.strip() for cell in raw):
            continue  # 跳过空行
        rows.append(raw)

    if not rows:
        raise DataSourceError("CSV 只有表头、没有数据行：%s" % path)

    return (table_name, headers, rows_with_width(rows, len(headers)))


def load_single_csv(path, encoding, table_override):
    """单个 CSV/TSV：--table 可以改表名"""
    if table_override:
        table_name = sanitize_table_name(table_override)
    else:
        table_name = sanitize_table_name(path.stem)
    spec = read_csv_spec(path, encoding, table_name)
    return build_memory_db([spec], str(path))


def _json_cell(value):
    """JSON 值 → 字符串；嵌套 dict/list 序列化为 JSON 文本，null → 空（NULL）"""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def load_json(path, encoding, table_override):
    """JSON 顶层必须是数组，数组元素为对象（也允许标量，会包成 value 列）"""
    text = decode_text(path, encoding)
    try:
        data = json.loads(text)
    except ValueError as e:
        raise DataSourceError("JSON 解析失败：%s" % e)

    if not isinstance(data, list):
        raise DataSourceError("JSON 顶层必须是数组（数组元素为对象）：%s" % path)
    if not data:
        raise DataSourceError("JSON 数组为空，没有数据：%s" % path)

    objects = []
    keys = []
    seen = set()
    for item in data:
        obj = item if isinstance(item, dict) else {"value": item}
        objects.append(obj)
        for key in obj.keys():
            if key not in seen:
                seen.add(key)
                keys.append(key)

    if not keys:
        raise DataSourceError("JSON 里没有任何字段：%s" % path)

    display_keys = []
    for index, key in enumerate(keys, 1):
        name = str(key).strip() or "column_%d" % index
        display_keys.append(name)
    # JSON 对象的 key 区分大小写，但 sqlite 列名不区分，这里统一查重
    headers = normalize_headers(display_keys, str(path))

    rows = []
    for obj in objects:
        rows.append([_json_cell(obj.get(key)) for key in keys])

    table_name = sanitize_table_name(table_override or path.stem)
    return build_memory_db([(table_name, headers, rows)], str(path))


def load_directory(path, encoding, table_override):
    """目录下所有 CSV/TSV（含子目录）各加载成一张表"""
    try:
        files = sorted(
            (p for p in path.rglob("*") if p.is_file() and p.suffix.lower() in CSV_SUFFIXES),
            key=lambda p: str(p).lower(),
        )
    except OSError as e:
        raise DataSourceError("读取目录失败：%s（%s）" % (path, e))

    if not files:
        raise DataSourceError("目录里没有找到 CSV/TSV 文件：%s" % path)

    specs = []
    for file_path in files:
        specs.append(read_csv_spec(file_path, encoding, sanitize_table_name(file_path.stem)))

    db = build_memory_db(specs, str(path))
    if table_override:
        db.focus(table_override)
    return db


def build_memory_db(specs, label):
    """把若干 (表名, 表头, 字符串行) 装进内存 sqlite"""
    conn = sqlite3.connect(":memory:")
    tables = []
    used = set()
    try:
        for raw_name, headers, raw_rows in specs:
            table_name = unique_table_name(sanitize_table_name(raw_name), used)
            types = infer_types(raw_rows, len(headers))
            column_defs = []
            for column, column_type in zip(headers, types):
                column_defs.append("  %s %s" % (quote_ident(column), column_type))
            ddl = "CREATE TABLE %s (\n%s\n)" % (quote_ident(table_name), ",\n".join(column_defs))
            conn.execute(ddl)

            if raw_rows:
                placeholders = ", ".join(["?"] * len(headers))
                values = [
                    tuple(convert_value(cell, column_type) for cell, column_type in zip(row, types))
                    for row in raw_rows
                ]
                conn.executemany(
                    "INSERT INTO %s VALUES (%s)" % (quote_ident(table_name), placeholders),
                    values,
                )
            conn.commit()

            tables.append(
                TableInfo(
                    table_name,
                    ddl,
                    list(zip(headers, types)),
                    len(raw_rows),
                )
            )
    except sqlite3.Error as e:
        conn.close()
        raise DataSourceError("把数据装入内存 SQLite 失败：%s" % e)
    except DataSourceError:
        conn.close()
        raise

    return Database(conn, tables, label)


# ---------- SQL 只读安全检查 ----------

def split_statements(sql):
    """按分号切语句，但忽略字符串/注释里的分号（用 complete_statement 判断）"""
    statements = []
    buffer = ""
    for char in sql:
        buffer += char
        if char == ";" and sqlite3.complete_statement(buffer):
            statements.append(buffer)
            buffer = ""
    if buffer.strip():
        statements.append(buffer)
    return statements


def strip_sql_noise(sql):
    """去掉注释和字符串/引号标识符内容，只留下用于关键字判断的结构文本"""
    out = []
    index = 0
    length = len(sql)
    while index < length:
        char = sql[index]

        if char == "-" and index + 1 < length and sql[index + 1] == "-":
            newline = sql.find("\n", index)
            out.append(" ")
            index = length if newline < 0 else newline + 1
            continue

        if char == "/" and index + 1 < length and sql[index + 1] == "*":
            end = sql.find("*/", index + 2)
            out.append(" ")
            index = length if end < 0 else end + 2
            continue

        if char in ("'", '"', "`"):
            quote = char
            index += 1
            while index < length:
                if sql[index] == quote:
                    if index + 1 < length and sql[index + 1] == quote:
                        index += 2
                        continue
                    index += 1
                    break
                index += 1
            out.append(" ? ")
            continue

        out.append(char)
        index += 1
    return "".join(out)


def _meaningful_statements(sql):
    result = []
    for statement in split_statements(sql):
        plain = strip_sql_noise(statement).strip(" \t\r\n;")
        if plain:
            result.append(statement)
    return result


PRAGMA_ALLOWED_PATTERN = re.compile(
    r"^PRAGMA\s+(?:[A-Za-z_][A-Za-z0-9_]*\s*\.\s*)?"
    r"(table_info|index_list)\s*\(\s*"
    r"(?:'[^']*'|\"[^\"]*\"|`[^`]*`|[A-Za-z_][A-Za-z0-9_]*)"
    r"\s*\)\s*;?\s*$",
    re.IGNORECASE,
)


def _banned_hits(plain_sql):
    """找出被禁止的写操作关键字；replace( 函数调用不算写操作"""
    cleaned = re.sub(r"\bREPLACE\s*\(", " ", plain_sql, flags=re.IGNORECASE)
    tokens = re.findall(r"[A-Za-z_][A-Za-z0-9_]*", cleaned)
    return sorted(set(token.upper() for token in tokens) & BANNED_KEYWORDS)


def validate_sql(sql):
    """只读白名单校验；不通过抛 UnsafeSqlError（退出码 2）"""
    if not sql or not sql.strip():
        raise UnsafeSqlError("SQL 为空，没有可执行的语句。")

    statements = _meaningful_statements(sql)
    if not statements:
        raise UnsafeSqlError("SQL 为空（只有注释或分号），没有可执行的语句。")
    if len(statements) > 1:
        raise UnsafeSqlError(
            "一次只允许执行一条 SQL 语句，检测到 %d 条。请拆开分别执行。" % len(statements)
        )

    statement = statements[0]
    plain = strip_sql_noise(statement)
    match = re.match(r"\s*\(*\s*([A-Za-z_][A-Za-z0-9_]*)", plain)
    if not match:
        raise UnsafeSqlError("无法识别 SQL 语句类型，已拒绝执行。")

    keyword = match.group(1).upper()

    if keyword == "PRAGMA":
        if not PRAGMA_ALLOWED_PATTERN.match(statement.strip()):
            raise UnsafeSqlError(
                "只允许只读 PRAGMA：table_info(...) 和 index_list(...)。"
                "PRAGMA 的写入形式（如 PRAGMA user_version = 1）已被拒绝。"
            )
        return

    if keyword in ("SELECT", "WITH"):
        hits = _banned_hits(plain)
        if hits:
            raise UnsafeSqlError(
                "语句包含被禁止的写操作关键字：%s。这里只允许只读查询。" % "、".join(hits)
            )
        return

    if keyword == "EXPLAIN":
        inner = re.sub(
            r"^\s*EXPLAIN\s+(?:QUERY\s+PLAN\s+)?", "", plain, count=1, flags=re.IGNORECASE
        )
        if not re.match(r"^\s*\(*\s*(SELECT|WITH)\b", inner, re.IGNORECASE):
            raise UnsafeSqlError("EXPLAIN 后面只能跟 SELECT 或 WITH 查询。")
        hits = _banned_hits(plain)
        if hits:
            raise UnsafeSqlError(
                "语句包含被禁止的写操作关键字：%s。这里只允许只读查询。" % "、".join(hits)
            )
        return

    raise UnsafeSqlError(
        "只允许 SELECT、WITH、EXPLAIN、PRAGMA table_info/index_list，"
        "当前语句以「%s」开头，已拒绝。" % keyword
    )


# ---------- 执行与结果展示 ----------

def execute_query(conn, sql, limit):
    """执行一条 SQL，最多取 limit 行；返回 (列名, 行, 是否被截断)"""
    cursor = conn.execute(sql)
    columns = [desc[0] for desc in cursor.description] if cursor.description else []
    if not columns:
        return [], [], False
    rows = cursor.fetchmany(limit + 1)
    truncated = len(rows) > limit
    return columns, rows[:limit], truncated


def execute_explain(conn, sql):
    """返回 EXPLAIN QUERY PLAN 的列和行"""
    statement = sql.strip()
    if re.match(r"(?i)^EXPLAIN\b", statement):
        plan_sql = statement
    else:
        plan_sql = "EXPLAIN QUERY PLAN " + statement
    cursor = conn.execute(plan_sql)
    columns = [desc[0] for desc in cursor.description] if cursor.description else []
    return columns, cursor.fetchall()


def _char_width(char):
    if unicodedata.combining(char):
        return 0
    return 2 if unicodedata.east_asian_width(char) in ("W", "F") else 1


def display_width(text):
    return sum(_char_width(char) for char in text)


def truncate_display(text, max_width=MAX_CELL_WIDTH):
    """按显示宽度截断（中文算 2 列），超宽补省略号"""
    if display_width(text) <= max_width:
        return text
    output = []
    width = 0
    for char in text:
        char_width = _char_width(char)
        if width + char_width > max_width - 1:
            break
        output.append(char)
        width += char_width
    output.append("…")
    return "".join(output)


def stringify_value(value):
    if value is None:
        return "NULL"
    if isinstance(value, bytes):
        return "<BLOB %d 字节>" % len(value)
    text = str(value)
    return text.replace("\r\n", " ").replace("\r", " ").replace("\n", " ")


def _pad(text, width):
    gap = width - display_width(text)
    return text + (" " * gap if gap > 0 else "")


def render_text_table(columns, rows):
    """等宽对齐的纯文本结果表"""
    if not columns:
        return "（该语句没有返回结果集）"

    headers = [truncate_display(stringify_value(column)) for column in columns]
    cells = []
    for row in rows:
        line = []
        for value in row:
            line.append(truncate_display(stringify_value(value)))
        cells.append(line)

    widths = [max(1, display_width(header)) for header in headers]
    for line in cells:
        for index, cell in enumerate(line):
            if index < len(widths):
                widths[index] = max(widths[index], display_width(cell))

    output = [
        " | ".join(_pad(header, widths[index]) for index, header in enumerate(headers)),
        "-+-".join("-" * widths[index] for index in range(len(headers))),
    ]
    for line in cells:
        output.append(" | ".join(_pad(cell, widths[index]) for index, cell in enumerate(line)))

    if not rows:
        output.append("（查询结果为空）")
    return "\n".join(output)


def _md_escape(text):
    return text.replace("|", "\\|").replace("\n", " ")


def render_markdown_table(columns, rows):
    if not columns:
        return "（该语句没有返回结果集）"
    lines = [
        "| " + " | ".join(_md_escape(str(column)) for column in columns) + " |",
        "| " + " | ".join(["---"] * len(columns)) + " |",
    ]
    for row in rows:
        lines.append(
            "| "
            + " | ".join(_md_escape(truncate_display(stringify_value(value))) for value in row)
            + " |"
        )
    return "\n".join(lines)


def schema_text(db, max_chars=SCHEMA_MAX_CHARS):
    """喂给模型的表结构文本，总长度不超过 max_chars"""
    chunks = []
    for table in db.select_tables():
        columns = "\n".join(
            "  - %s %s" % (name, column_type) for name, column_type in table.columns
        )
        chunks.append(
            "表：%s\n行数：%d\n建表语句：\n%s\n列：\n%s"
            % (table.name, table.row_count, (table.ddl or "").strip(), columns)
        )
    text = "\n\n".join(chunks)
    if len(text) > max_chars:
        note = "\n……（表结构过长，已截断）"
        text = text[: max_chars - len(note)] + note
    return text


def print_schema(db):
    selected = db.select_tables()
    print("📋 数据源：%s" % db.label)
    if db.focused is not None:
        print("已指定表：%s（数据源共 %d 张表）" % (db.focused.name, len(db.tables)))
    else:
        print("共 %d 张表" % len(selected))

    for table in selected:
        print("")
        print("── 表：%s ──" % table.name)
        print("行数：%d" % table.row_count)
        print("建表语句：")
        print((table.ddl or "").strip())
        print("列：")
        for name, column_type in table.columns:
            print("  - %s  %s" % (name, column_type))


def print_sql_block(sql):
    print("生成的 SQL：")
    print("```sql")
    print(sql.strip())
    print("```")


def print_explain(conn, sql):
    print("执行计划：")
    try:
        columns, rows = execute_explain(conn, sql)
    except sqlite3.Error as e:
        print("执行计划获取失败：%s" % e, file=sys.stderr)
        return
    print(render_text_table(columns, rows))


# ---------- 模型调用 ----------

def call_model(client, model, system, messages, tools=None, max_tokens=4000):
    """按"能力从高到低"逐级降级，兼容官方 API 和第三方兼容网关。"""
    kwargs = dict(model=model, max_tokens=max_tokens, system=system, messages=messages)
    if tools is not None:
        kwargs["tools"] = tools
    try:
        return client.beta.messages.create(
            thinking={"type": "adaptive"},
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            **kwargs,
        )
    except (TypeError, anthropic.BadRequestError, anthropic.NotFoundError):
        pass
    try:
        return client.messages.create(thinking={"type": "adaptive"}, **kwargs)
    except (TypeError, anthropic.BadRequestError, anthropic.NotFoundError):
        pass
    return client.messages.create(**kwargs)


def create_client():
    if not os.environ.get("ANTHROPIC_API_KEY") and not os.environ.get(
        "ANTHROPIC_AUTH_TOKEN"
    ):
        print(
            "提示：没有检测到 ANTHROPIC_API_KEY 环境变量，模型调用可能失败。\n"
            "请先设置：export ANTHROPIC_API_KEY=你的key",
            file=sys.stderr,
        )
    try:
        return anthropic.Anthropic()
    except Exception as e:
        raise SqlChatError("创建 API 客户端失败：%s（请检查 ANTHROPIC_API_KEY）" % e)


def describe_model_error(exc):
    """把各类模型/网络异常转成一句中文说明"""
    if isinstance(exc, anthropic.AuthenticationError):
        return "API key 无效或没有权限，请检查 ANTHROPIC_API_KEY。"
    if isinstance(exc, anthropic.APIConnectionError):
        return "无法连接 Anthropic API，请检查网络或 ANTHROPIC_BASE_URL。"
    if isinstance(exc, anthropic.RateLimitError):
        return "请求太频繁被限流，请稍后再试。"
    if isinstance(exc, anthropic.APIStatusError):
        return "API 返回错误（%s）：%s" % (exc.status_code, exc.message)
    if isinstance(exc, anthropic.APIError):
        return "Anthropic API 调用失败：%s" % exc
    return "%s: %s" % (type(exc).__name__, exc)


def extract_response_text(response):
    parts = []
    for block in getattr(response, "content", None) or []:
        text = getattr(block, "text", None)
        if text:
            parts.append(text)
    return "".join(parts).strip()


def extract_sql(text):
    """从模型回复里抠出 SQL：剥掉 ```sql 围栏、前缀说明和结尾多余内容"""
    if not text:
        return ""
    result = text.strip()

    fenced = re.search(r"```(?:sql|sqlite)?\s*(.*?)```", result, re.S | re.IGNORECASE)
    if fenced:
        result = fenced.group(1).strip()

    result = re.sub(r"^(?:sql|sqlite)\s*[:：]\s*", "", result, flags=re.IGNORECASE).strip()

    match = re.search(r"(?i)\b(SELECT|WITH|EXPLAIN|PRAGMA)\b", result)
    if match:
        result = result[match.start():].strip()
    if not result:
        return ""

    statements = _meaningful_statements(result)
    if statements:
        result = statements[0].strip()
    return result


SQL_SYSTEM_PROMPT = """你是「SQL 数据问答助手」，负责把用户的中文问题翻译成一条 SQLite 查询语句。

规则：
1. 只输出一条 SQL，不要输出解释、不要用 Markdown 代码块。
2. 只能使用 SELECT / WITH / EXPLAIN / PRAGMA table_info / PRAGMA index_list。
   绝对不能写 INSERT、UPDATE、DELETE、DROP、CREATE、ALTER、ATTACH 等任何写操作。
3. 表名和列名严格以给定的表结构为准，并且一律用双引号包起来（如 "orders"、"城市"）。
   中文字符串常量用单引号。
5. 不要自己加 LIMIT，除非用户明确要求；程序会统一截断结果。
6. 如果不确定字段含义，按最合理的理解写查询，不要反问用户。
"""

CONCLUSION_SYSTEM_PROMPT = """你是数据分析师。根据用户问题、执行的 SQL 和查询结果，用中文给出简洁结论。

要求：
- 直接回答用户问题，先给结论，再给依据
- 引用关键数字（合计、占比、最大/最小、行数等）
- 如果发现数据异常（空值、负数、重复、明显离群值），主动指出
- 不要复述 SQL，不要编造结果里没有的数据
- 结果为空时明确说明没有匹配数据
- 控制在 5 句话以内
"""


def run_nl_query(db, question, model, max_retry, limit):
    """自然语言 → SQL → 执行；失败时把 sqlite 报错喂回模型修正"""
    client = create_client()
    schema = schema_text(db)

    messages = [
        {
            "role": "user",
            "content": (
                "数据源：%s\n\n用户问题：%s\n\n表结构（SQLite）：\n%s\n\n"
                "请只输出一条 SQL。" % (db.label, question, schema)
            ),
        }
    ]

    last_error = ""
    last_error_is_safety = False
    attempts = max_retry + 1

    for _ in range(attempts):
        response = call_model(
            client, model, SQL_SYSTEM_PROMPT, messages, max_tokens=1500
        )
        reply_text = extract_response_text(response)
        sql = extract_sql(reply_text)

        if not sql:
            last_error = "模型没有返回可识别的 SQL"
            last_error_is_safety = False
            messages.append({"role": "assistant", "content": reply_text or "（空回复）"})
            messages.append(
                {"role": "user", "content": "上一条回复为空，请只输出一条 SQL。"}
            )
            continue

        try:
            validate_sql(sql)
        except UnsafeSqlError as e:
            last_error = e.message
            last_error_is_safety = True
            messages.append({"role": "assistant", "content": reply_text})
            messages.append(
                {
                    "role": "user",
                    "content": (
                        "你给出的 SQL 被只读安全检查拒绝：%s\n"
                        "请修正后重新只输出一条 SQL。" % e.message
                    ),
                }
            )
            continue

        try:
            columns, rows, truncated = execute_query(db.conn, sql, limit)
        except sqlite3.Error as e:
            last_error = str(e)
            last_error_is_safety = False
            messages.append({"role": "assistant", "content": reply_text})
            messages.append(
                {
                    "role": "user",
                    "content": (
                        "你给出的 SQL 执行失败，SQLite 报错：%s\n"
                        "请修正后重新只输出一条 SQL。" % e
                    ),
                }
            )
            continue

        return {
            "client": client,
            "sql": sql,
            "columns": columns,
            "rows": rows,
            "truncated": truncated,
        }

    exit_code = 2 if last_error_is_safety else 1
    raise SqlChatError(
        "连续 %d 次都没能得到可执行的 SQL。最后一次错误：%s" % (attempts, last_error),
        exit_code,
    )


def generate_conclusion(client, model, question, sql, columns, rows, truncated):
    """第二次模型调用：把结果表转成中文结论（最多送 50 行）"""
    sample = rows[:CONCLUSION_ROWS]
    note = ""
    if len(rows) > CONCLUSION_ROWS:
        note = "（结果共 %d 行，这里只列前 %d 行）\n" % (len(rows), CONCLUSION_ROWS)
    elif truncated:
        note = "（结果被 --limit 截断）\n"

    prompt = (
        "用户问题：%s\n\n执行的 SQL：\n%s\n\n查询结果：\n%s%s"
        % (question, sql.strip(), note, render_markdown_table(columns, sample))
    )
    response = call_model(
        client,
        model,
        CONCLUSION_SYSTEM_PROMPT,
        [{"role": "user", "content": prompt}],
        max_tokens=1500,
    )
    return extract_response_text(response)


# ---------- 输出文件 ----------

def write_text_file(path_str, content):
    target = Path(path_str).expanduser()
    try:
        parent = target.parent
        if parent and not parent.exists():
            parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    except OSError as e:
        raise SqlChatError("写入结果文件失败：%s（%s）" % (target, e))
    print("📄 已写入：%s" % target)


def build_result_markdown(db, sql, columns, rows, truncated, limit, conclusion):
    lines = [
        "# sql-chat 查询结果",
        "",
        "**数据源**：%s" % db.label,
        "",
        "## 生成的 SQL",
        "",
        "```sql",
        sql.strip(),
        "```",
        "",
        "## 查询结果",
        "",
        render_markdown_table(columns, rows),
    ]
    if truncated:
        lines.extend(["", "> 结果超过 %d 行，已截断，仅显示前 %d 行。" % (limit, limit)])
    lines.extend(["", "## 结论", "", conclusion or "（无）", ""])
    return "\n".join(lines)


def build_schema_markdown(db):
    lines = ["# 表结构", "", "**数据源**：%s" % db.label, ""]
    for table in db.select_tables():
        lines.extend(["## %s" % table.name, "", "- 行数：%d" % table.row_count, ""])
        lines.extend(["```sql", (table.ddl or "").strip(), "```", "", "| 列名 | 类型 |", "| --- | --- |"])
        for name, column_type in table.columns:
            lines.append("| %s | %s |" % (_md_escape(name), column_type))
        lines.append("")
    return "\n".join(lines)


# ---------- 三种执行模式 ----------

def run_direct_sql(db, args):
    """--sql：直接执行，不调模型"""
    validate_sql(args.sql)

    print_sql_block(args.sql)
    print()
    if args.explain:
        print_explain(db.conn, args.sql)
        print()

    columns, rows, truncated = execute_query(db.conn, args.sql, args.limit)
    print(render_text_table(columns, rows))
    if truncated:
        print("（已截断：结果超过 %d 行，仅显示前 %d 行，可用 --limit 调整）" % (args.limit, args.limit))

    if args.output:
        write_text_file(
            args.output,
            build_result_markdown(db, args.sql, columns, rows, truncated, args.limit, None),
        )


def run_natural_language(db, args):
    """自然语言模式：生成 SQL → 执行 → 生成中文结论"""
    result = run_nl_query(db, args.question, args.model, args.max_retry, args.limit)
    sql = result["sql"]

    print_sql_block(sql)
    print()
    if args.explain:
        print_explain(db.conn, sql)
        print()

    print(render_text_table(result["columns"], result["rows"]))
    if result["truncated"]:
        print("（已截断：结果超过 %d 行，仅显示前 %d 行，可用 --limit 调整）" % (args.limit, args.limit))
    print()

    conclusion = None
    conclusion_error = None
    try:
        conclusion = generate_conclusion(
            result["client"],
            args.model,
            args.question,
            sql,
            result["columns"],
            result["rows"],
            result["truncated"],
        )
    except Exception as e:  # 结论失败不能影响已经拿到的查询结果
        conclusion_error = describe_model_error(e)

    if conclusion:
        print("结论：")
        print(conclusion)
    elif conclusion_error:
        print("⚠️ 结论生成失败：%s" % conclusion_error, file=sys.stderr)
        print("（查询结果已在上方完整给出）", file=sys.stderr)
    else:
        print("结论：（模型没有返回结论，以上为查询结果）")

    if args.output:
        write_text_file(
            args.output,
            build_result_markdown(
                db,
                sql,
                result["columns"],
                result["rows"],
                result["truncated"],
                args.limit,
                conclusion,
            ),
        )

    if conclusion_error:
        sys.exit(1)


# ---------- 自检（离线，不需要 API key） ----------

def _expect_data_error(func, keyword=""):
    try:
        func()
    except DataSourceError as e:
        assert keyword in e.message, "错误信息里没有「%s」：%s" % (keyword, e.message)
    else:
        raise AssertionError("应当报 DataSourceError 但没有报错")


def run_self_test():
    """不联网、不需要 API key，验证全部确定性逻辑"""
    tmpdir = tempfile.mkdtemp(prefix="sql_chat_selftest_")
    try:
        base = Path(tmpdir)

        # 1) SQLite：只读打开、枚举表、分组查询
        db_path = base / "shop.sqlite"
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "CREATE TABLE customers (id INTEGER PRIMARY KEY, name TEXT, city TEXT)"
        )
        conn.executemany(
            "INSERT INTO customers (id, name, city) VALUES (?, ?, ?)",
            [(1, "张三", "北京"), (2, "李四", "上海"), (3, "王五", "北京")],
        )
        conn.execute("CREATE TABLE orders (id INTEGER, customer_id INTEGER, amount REAL)")
        conn.executemany(
            "INSERT INTO orders VALUES (?, ?, ?)",
            [(1, 1, 99.5), (2, 1, 20.0), (3, 2, 15.0)],
        )
        conn.commit()
        conn.close()

        db = load_source(str(db_path), "auto", None)
        assert len(db.tables) == 2, db.tables
        columns, rows, truncated = execute_query(
            db.conn,
            "SELECT city, COUNT(*) AS n FROM customers GROUP BY city ORDER BY n DESC",
            200,
        )
        assert columns == ["city", "n"], columns
        assert rows[0] == ("北京", 2), rows
        assert truncated is False

        write_blocked = False
        try:
            db.conn.execute(
                "INSERT INTO customers (id, name, city) VALUES (9, 'x', 'y')"
            )
        except sqlite3.OperationalError:
            write_blocked = True
        assert write_blocked, "SQLite 数据源必须只读打开"
        db.conn.rollback()

        db.focus("orders")
        focused_schema = schema_text(db)
        assert "表：orders" in focused_schema
        assert "表：customers" not in focused_schema
        db.close()

        # 2) CSV：分隔符嗅探、带引号的逗号/分号、类型推断、NULL、--table 重命名
        csv_path = base / "order_detail.csv"
        csv_path.write_text(
            "order_id,customer,amount,note\n"
            'A001,张三,12.5,"含,逗号;和分号"\n'
            "A002,李四,7,普通\n"
            "A003,王五,,空值\n",
            encoding="utf-8",
        )
        db = load_source(str(csv_path), "auto", None)
        table = db.tables[0]
        assert table.name == "order_detail", table.name
        assert table.row_count == 3, table.row_count
        type_map = dict(table.columns)
        assert type_map["amount"] == "REAL", type_map
        assert type_map["order_id"] == "TEXT", type_map
        assert type_map["customer"] == "TEXT", type_map

        columns, rows, _ = execute_query(
            db.conn,
            "SELECT note FROM order_detail WHERE order_id = 'A001'",
            200,
        )
        assert rows[0][0] == "含,逗号;和分号", rows
        columns, rows, _ = execute_query(
            db.conn, "SELECT amount FROM order_detail WHERE amount IS NULL", 200
        )
        assert len(rows) == 1, rows
        db.close()

        db = load_source(str(csv_path), "auto", "Renamed Table")
        assert db.tables[0].name == "renamed_table", db.tables[0].name
        db.close()

        # 3) GBK 编码自动探测
        gbk_path = base / "gbk_demo.csv"
        gbk_path.write_bytes("城市,数量\n北京,3\n".encode("gbk"))
        db = load_source(str(gbk_path), "auto", None)
        columns, rows, _ = execute_query(
            db.conn, 'SELECT "城市" FROM gbk_demo WHERE "数量" = 3', 200
        )
        assert rows[0][0] == "北京", rows
        db.close()

        # 4) JSON：数组 + 嵌套值序列化 + 类型推断
        json_path = base / "users.json"
        json_path.write_text(
            json.dumps(
                [
                    {"name": "张三", "age": 30, "tags": ["a", "b"], "meta": {"vip": True}},
                    {"name": "李四", "age": 25, "tags": [], "meta": {"vip": False}},
                ],
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        db = load_source(str(json_path), "auto", None)
        type_map = dict(db.tables[0].columns)
        assert type_map["age"] == "INTEGER", type_map
        assert type_map["tags"] == "TEXT", type_map
        columns, rows, _ = execute_query(db.conn, 'SELECT COUNT(*) FROM "users"', 200)
        assert rows[0][0] == 2, rows
        db.close()

        # 5) 目录：多张表 + TSV
        dir_path = base / "csv_dir"
        dir_path.mkdir()
        (dir_path / "a.csv").write_text("x,y\n1,2\n", encoding="utf-8")
        (dir_path / "b.tsv").write_text("m\tn\n1\t2\n3\t4\n", encoding="utf-8")
        db = load_source(str(dir_path), "auto", None)
        assert [t.name for t in db.tables] == ["a", "b"], [t.name for t in db.tables]
        assert db.tables[1].row_count == 2, db.tables[1].row_count
        db.close()

        # 6) 表名清洗
        assert sanitize_table_name("Sales Data-2024") == "sales_data_2024"
        assert sanitize_table_name("用户订单") == "data"
        assert sanitize_table_name("  A B  ") == "a_b"

        # 7) SQL 白名单：允许
        for sql in [
            "SELECT 1",
            "SELECT 'a;b' AS x;",
            "WITH t AS (SELECT 1 AS n) SELECT n FROM t",
            "EXPLAIN QUERY PLAN SELECT 1",
            "PRAGMA table_info('customers')",
            "SELECT replace(name, '张', '章') FROM customers",
        ]:
            validate_sql(sql)

        # 8) SQL 白名单：拒绝
        for sql in [
            "INSERT INTO customers VALUES (9, 'x', 'y')",
            "UPDATE customers SET name = 'x'",
            "DELETE FROM customers",
            "DROP TABLE customers",
            "CREATE TABLE x (a INTEGER)",
            "ALTER TABLE customers ADD COLUMN z TEXT",
            "ATTACH DATABASE 'x.db' AS x",
            "DETACH DATABASE x",
            "VACUUM",
            "PRAGMA user_version = 1",
            "SELECT 1; DROP TABLE customers",
            "WITH t AS (SELECT 1) DELETE FROM customers",
            "EXPLAIN DELETE FROM customers",
            "REPLACE INTO customers VALUES (9, 'x', 'y')",
            "",
        ]:
            try:
                validate_sql(sql)
            except UnsafeSqlError:
                pass
            else:
                raise AssertionError("应当拒绝的 SQL 没有被拒绝：%s" % sql)

        # 9) 模型输出清洗：剥 Markdown 围栏、前缀
        assert extract_sql("```sql\nSELECT 1;\n```") == "SELECT 1;"
        assert extract_sql("SQL: SELECT 2") == "SELECT 2"
        assert extract_sql("SELECT 3") == "SELECT 3"
        assert extract_sql("") == ""

        # 10) 行数截断
        memory = sqlite3.connect(":memory:")
        memory.execute("CREATE TABLE nums (n INTEGER)")
        memory.executemany("INSERT INTO nums VALUES (?)", [(i,) for i in range(5)])
        columns, rows, truncated = execute_query(
            memory, "SELECT n FROM nums ORDER BY n", 3
        )
        assert len(rows) == 3 and truncated is True, (rows, truncated)
        memory.close()

        # 11) 输出格式：中文宽度、超宽截断、Markdown 转义
        text = render_text_table(["城市", "人数"], [("北京", 2), ("San Francisco", 1)])
        assert "城市" in text and "北京" in text
        assert text.splitlines()[0].startswith("城市")
        assert display_width(truncate_display("x" * 100)) <= MAX_CELL_WIDTH
        markdown = render_markdown_table(["a|b"], [("c|d",)])
        assert "a\\|b" in markdown and "c\\|d" in markdown, markdown

        # 12) 异常输入：不存在 / 空文件 / 只有表头 / 列名重复 / JSON 非数组
        _expect_data_error(
            lambda: load_source(str(base / "missing.csv"), "auto", None), "不存在"
        )
        empty_path = base / "empty.csv"
        empty_path.write_text("", encoding="utf-8")
        _expect_data_error(lambda: load_source(str(empty_path), "auto", None), "空文件")
        header_only = base / "header_only.csv"
        header_only.write_text("a,b\n", encoding="utf-8")
        _expect_data_error(
            lambda: load_source(str(header_only), "auto", None), "只有表头"
        )
        duplicate = base / "duplicate.csv"
        duplicate.write_text("a,a\n1,2\n", encoding="utf-8")
        _expect_data_error(
            lambda: load_source(str(duplicate), "auto", None), "列名重复"
        )
        bad_json = base / "bad.json"
        bad_json.write_text('{"a": 1}', encoding="utf-8")
        _expect_data_error(lambda: load_source(str(bad_json), "auto", None), "数组")

        print("SELF-TEST OK")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ---------- CLI ----------

def force_utf8_output():
    """Windows 下 stdout 默认走本地编码（如 gbk），统一改成 UTF-8 避免中文乱码"""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            pass


def build_parser():
    parser = argparse.ArgumentParser(
        description="SQL 数据问答助手：用自然语言查询本地 SQLite / CSV / JSON（只读）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            "  py -3 agent.py ./data/shop.sqlite \"每个城市有多少客户？\"\n"
            "  py -3 agent.py ./data/sales.csv \"销售额最高的 5 个订单\"\n"
            "  py -3 agent.py ./data/shop.db --show-schema\n"
            "  py -3 agent.py ./data/shop.db --sql \"SELECT COUNT(*) FROM orders\"\n"
        ),
    )
    parser.add_argument(
        "source",
        nargs="?",
        help="数据源：.sqlite/.db/.sqlite3、.csv/.tsv、.json，或放 CSV 的目录",
    )
    parser.add_argument(
        "question",
        nargs="?",
        help="自然语言问题，如：每个城市的客户数是多少？",
    )
    parser.add_argument("--table", help="指定表名：单表 CSV 可重命名，多表时用于消歧")
    parser.add_argument(
        "--limit",
        type=int,
        default=DEFAULT_LIMIT,
        help="结果表最多显示多少行（默认 %d）" % DEFAULT_LIMIT,
    )
    parser.add_argument("--sql", help="直接执行一条只读 SQL，不调用模型")
    parser.add_argument("--explain", action="store_true", help="打印 EXPLAIN QUERY PLAN")
    parser.add_argument("--show-schema", action="store_true", help="只打印表结构，不调用模型")
    parser.add_argument("--output", help="把结果写成 Markdown 文件，如 结果.md")
    parser.add_argument(
        "--encoding",
        default="auto",
        choices=["auto", "utf-8", "utf-8-sig", "gbk", "gb18030"],
        help="CSV/TSV/JSON 文件编码（默认 auto 自动探测）",
    )
    parser.add_argument(
        "--max-retry",
        type=int,
        default=DEFAULT_MAX_RETRY,
        help="SQL 失败后最多让模型修正几轮（默认 %d）" % DEFAULT_MAX_RETRY,
    )
    parser.add_argument(
        "--model",
        default=DEFAULT_MODEL,
        help="使用的模型（默认 %s，走网关时可换 qwen3.8-max 等）" % DEFAULT_MODEL,
    )
    parser.add_argument("--self-test", action="store_true", help="离线自检，不联网、不需要 API key")
    return parser


def main():
    force_utf8_output()
    parser = build_parser()
    args = parser.parse_args()

    if args.self_test:
        run_self_test()
        return

    if not args.source:
        print(
            "错误：缺少数据源路径。用法：py -3 agent.py <数据源> \"<问题>\"",
            file=sys.stderr,
        )
        sys.exit(1)

    if args.limit <= 0:
        print("错误：--limit 必须是正整数。", file=sys.stderr)
        sys.exit(1)
    if args.max_retry < 0:
        print("错误：--max-retry 不能是负数。", file=sys.stderr)
        sys.exit(1)
    if not args.sql and not args.question and not args.show_schema:
        print(
            "错误：请提供自然语言问题，或使用 --sql / --show-schema。",
            file=sys.stderr,
        )
        sys.exit(1)

    try:
        db = load_source(args.source, args.encoding, args.table)
    except SqlChatError as e:
        print("错误：%s" % e.message, file=sys.stderr)
        sys.exit(e.exit_code)
    except OSError as e:
        print("错误：读取数据源失败：%s" % e, file=sys.stderr)
        sys.exit(1)

    try:
        if args.show_schema:
            print_schema(db)
            if args.output:
                write_text_file(args.output, build_schema_markdown(db))
            return
        if args.sql:
            run_direct_sql(db, args)
        else:
            run_natural_language(db, args)
    except SqlChatError as e:
        print("错误：%s" % e.message, file=sys.stderr)
        sys.exit(e.exit_code)
    except anthropic.APIError as e:
        print("错误：%s" % describe_model_error(e), file=sys.stderr)
        sys.exit(1)
    except sqlite3.Error as e:
        print("错误：SQLite 执行失败：%s" % e, file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print("错误：程序运行失败：%s" % e, file=sys.stderr)
        sys.exit(1)
    finally:
        db.close()


if __name__ == "__main__":
    main()
