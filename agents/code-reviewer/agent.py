#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
代码审查助手 —— 静态规则 + 模型语义审查 的两段式代码审查 agent

先跑一遍完全确定性的静态规则（残留标记、硬编码密钥、危险调用、可变默认参数等），
再把代码分批交给模型做语义审查，最后合并去重，生成按严重级别排序的中文 Markdown 报告。
不使用向量库，除 anthropic SDK 外只用标准库。

用法：
    python3 agent.py <路径...> [选项]

示例：
    python3 agent.py ./src --focus security --output 安全审查.md
    python3 agent.py ./app.py --static-only
    python3 agent.py . --min-severity 警告 --exclude "*.min.js"

环境要求：
    1. 语义审查需要设置 ANTHROPIC_API_KEY：
           export ANTHROPIC_API_KEY=你的key
       （--static-only 和 --self-test 不需要 key）
    2. 安装 SDK：
           pip3 install -r requirements.txt
"""

import argparse
import fnmatch
import json
import os
import re
import sys
import tempfile
from datetime import datetime
from pathlib import Path

import anthropic

# 默认模型（可用 --model 参数或 CODE_REVIEWER_MODEL 环境变量更换）
DEFAULT_MODEL = os.environ.get("CODE_REVIEWER_MODEL", "claude-opus-5")

DEFAULT_OUTPUT = "code-review.md"
DEFAULT_MAX_FILES = 200          # 单次最多审查多少个文件
DEFAULT_MAX_FILE_CHARS = 200_000  # 单个文件最多读入多少字符，超出截断并注明
SEMANTIC_BATCH_CHARS = 40_000    # 发给模型的每一批文本上限（字符）
MAX_FINDINGS_PER_RULE = 30       # 单文件单条规则最多报多少处，避免刷屏
LONG_LINE_LIMIT = 160            # 超过多少字符算超长行
LONG_FUNCTION_LINES = 80         # 函数体超过多少行算过长（启发式）

SEVERITIES = ("严重", "警告", "建议")
SEVERITY_ORDER = {"建议": 1, "警告": 2, "严重": 3}
FOCUS_LABELS = {
    "all": "全面审查",
    "security": "安全",
    "perf": "性能",
    "readability": "可读性",
    "bug": "潜在 bug",
}

# 递归扫描时直接忽略的目录
IGNORED_DIRS = {".git", "node_modules", "__pycache__", "dist", "build", "venv", ".venv"}

JS_EXTS = {".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs"}
PY_EXTS = {".py"}
COMMENT_PREFIXES = ("#", "//", "/*", "*", "--", "<!--")

# ---------- 正则与常量 ----------

MARKER_RE = re.compile(r"\b(TODO|FIXME|XXX|HACK)\b")
SECRET_ASSIGN_RE = re.compile(
    r"(?i)\b(password|passwd|pwd|secret|token|api[_-]?key|access[_-]?key|private[_-]?key)"
    r"\s*[:=]\s*[\"']([^\"'\s]{6,})[\"']"
)
AKIA_RE = re.compile(r"\bAKIA[0-9A-Z]{16}\b")
PRIVATE_KEY_RE = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")
BEARER_RE = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9\-._~+/]{40,}")
PLACEHOLDER_TOKENS = (
    "environ", "getenv", "process.env", "${", "<", "example", "sample",
    "placeholder", "changeme", "your_", "xxxx", "****", "...",
)
DANGEROUS_EXEC_RE = re.compile(r"(?<![\w.])(eval|exec)\s*\(")
NEW_FUNCTION_RE = re.compile(r"\bnew\s+Function\s*\(")
PY_BARE_EXCEPT_RE = re.compile(r"^\s*except\s*:")
EXCEPT_PASS_RE = re.compile(r"\bexcept\b[^:\n]*:\s*pass\b")
JS_CATCH_RE = re.compile(r"\bcatch\s*(\([^)]*\))?\s*\{")
NONE_COMPARE_RE = re.compile(r"(==|!=)\s*None\b")
MUTABLE_DEFAULT_RE = re.compile(
    r"\bdef\s+\w+\s*\([^)]*=\s*(\[\s*\]|\{\s*\}|\[[^\]\n]*\]|\{[^}\n]*\})"
)
SHELL_RISK_RES = (
    re.compile(r"\bshell\s*=\s*True\b"),
    re.compile(r"\bos\.system\s*\("),
    re.compile(r"\bchild_process\.(exec|execSync)\s*\("),
)
SQL_START_RE = re.compile(r"(?i)[\"']\s*(SELECT|INSERT\s+INTO|UPDATE\s+|DELETE\s+FROM)\b")
SQL_CONCAT_RE = re.compile(
    r"(\"\s*\+|'\s*\+|\+\s*\"|\+\s*'|\bf\"|\bf'|\.format\s*\(|\$\{|%\s*\(|%\s+[A-Za-z_])"
)
LONG_LINE_RE = re.compile(r"^.{161,}$")
DEBUG_RE = re.compile(r"(\bprint\s*\(|\bconsole\.(log|debug)\s*\(|\bdebugger\b)")


# ---------- 修复建议文案 ----------

FIX_MARKER = (
    "要么补完逻辑并删掉标记，要么把它改写成能跟踪的任务：\n\n"
    "```python\n"
    "# TODO(张三, 2026-11): 支付回调需要补幂等校验，见 issue#123\n"
    "```\n"
    "临时绕过（HACK）尤其要在注释里写清「为什么暂时这么做、什么条件下必须改回来」。"
)
FIX_SECRET = (
    "从环境变量或密钥管理服务读取，不要把密钥提交进仓库：\n\n"
    "```python\n"
    "import os\n"
    'API_KEY = os.environ["MY_API_KEY"]\n'
    "```\n"
    "如果密钥已经进入过 git 历史，立即在服务端轮换（作废旧值）；只删代码里的那一行不够。"
)
FIX_DYNAMIC_EXEC = (
    "用确定性的解析/分派代替动态执行：\n\n"
    "```python\n"
    "import ast\n"
    "value = ast.literal_eval(user_input)   # 只解析字面量\n"
    "```\n"
    "需要按名字调用函数时，用白名单字典 `{\"foo\": foo}` 分派，不要拼字符串再 eval。"
)
FIX_EXCEPT = (
    "只捕获预期内的异常，并记录或处理它：\n\n"
    "```python\n"
    "try:\n"
    "    do_work()\n"
    "except (ValueError, OSError) as exc:\n"
    "    logger.warning(\"do_work 失败：%s\", exc)\n"
    "    raise\n"
    "```\n"
    "确实要忽略的异常，也在注释里写明原因，不要用裸 `except:` 或空 catch 一把吞掉。"
)
FIX_NONE_COMPARE = (
    "判断 None 用身份比较，避免自定义 `__eq__` 干扰：\n\n"
    "```python\n"
    "if value is None:\n"
    "    ...\n"
    "```"
)
FIX_MUTABLE_DEFAULT = (
    "默认值用 None，进函数后再创建可变对象：\n\n"
    "```python\n"
    "def add_item(item, bucket=None):\n"
    "    if bucket is None:\n"
    "        bucket = []\n"
    "    bucket.append(item)\n"
    "    return bucket\n"
    "```"
)
FIX_SHELL = (
    "用参数列表调用子进程，默认 `shell=False`：\n\n"
    "```python\n"
    "import subprocess\n"
    'subprocess.run(["tar", "-czf", "backup.tar.gz", "data"], check=True)\n'
    "```\n"
    "参数列表会被当成独立参数，不会经过 shell 解释，`;`、`|`、`$()` 都失去注入能力。"
)
FIX_SQL = (
    "用参数化查询，让驱动负责转义值：\n\n"
    "```python\n"
    'cursor.execute("SELECT * FROM users WHERE id = %s", (user_id,))\n'
    "```\n"
    "表名/列名不能参数化时，用白名单校验，不要直接拼用户输入。"
)
FIX_LONG_LINE = (
    "拆成中间变量或分行书写，让一行只表达一个意思：\n\n"
    "```python\n"
    "total = base_price * quantity\n"
    "discounted = total * (1 - discount_rate)\n"
    "```"
)
FIX_LONG_FUNCTION = (
    "按职责拆成几个小函数，每个函数只做一件事：\n\n"
    "```python\n"
    "def parse_order(raw):\n"
    "    ...\n\n"
    "def validate_order(order):\n"
    "    ...\n\n"
    "def save_order(order):\n"
    "    ...\n"
    "```"
)
FIX_DEBUG = (
    "调试输出用标准日志代替，并按级别控制：\n\n"
    "```python\n"
    "import logging\n"
    'logger = logging.getLogger(__name__)\n'
    "logger.debug(\"payload=%s\", payload)\n"
    "```\n"
    "确属需要保留的 CLI 输出，可加 `--allow-debug` 跳过这条规则。"
)
FIX_FINAL_NEWLINE = (
    "在文件末尾补一个换行（POSIX 文本文件约定）：\n\n"
    "```bash\n"
    "printf '\\n' >> app.py\n"
    "```"
)
FIX_MIXED_INDENT = (
    "统一用空格缩进（Python 建议 4 个），并在仓库里加 `.editorconfig`：\n\n"
    "```ini\n"
    "[*]\n"
    "indent_style = space\n"
    "indent_size = 4\n"
    "```"
)
FIX_EMPTY_FILE = (
    "删除这个空文件，或补上真正的内容/占位说明：\n\n"
    "```text\n"
    "# 该文件用于……，当前暂无实现\n"
    "```"
)


# ---------- 数据结构 ----------

class Finding:
    """一条审查发现"""

    __slots__ = ("file", "line", "severity", "rule_id", "title", "detail", "suggestion")

    def __init__(self, file, line, severity, rule_id, title, detail, suggestion):
        self.file = file
        self.line = line
        self.severity = severity
        self.rule_id = rule_id
        self.title = title
        self.detail = detail
        self.suggestion = suggestion

    def key(self):
        return (self.file, self.line, self.title.strip().lower())


class SourceFile:
    """一个已载入内存、可审查的文本文件"""

    __slots__ = ("path", "rel", "text", "lines", "truncated")

    def __init__(self, path, rel, text, truncated):
        self.path = path
        self.rel = rel
        self.text = text
        self.lines = text.splitlines()
        self.truncated = truncated


class FileContext:
    """静态规则运行时能看到的文件信息"""

    __slots__ = ("path", "rel", "text", "lines", "ext", "truncated", "allow_debug")

    def __init__(self, source, allow_debug=False):
        self.path = source.path
        self.rel = source.rel
        self.text = source.text
        self.lines = source.lines
        self.ext = Path(source.rel).suffix.lower()
        self.truncated = source.truncated
        self.allow_debug = allow_debug


# ---------- 工具函数 ----------

def is_comment_line(line):
    s = line.strip()
    return bool(s) and s.startswith(COMMENT_PREFIXES)


def leading_width(line):
    prefix = line[: len(line) - len(line.lstrip(" \t"))]
    return len(prefix.expandtabs(4))


def _looks_like_placeholder(value, line):
    lower_line = line.lower()
    lower_value = value.lower()
    if any(token in lower_value for token in PLACEHOLDER_TOKENS):
        return True
    return "os.getenv" in lower_line or "environ" in lower_line or "process.env" in lower_line


# ---------- 静态规则实现（每个函数返回 [(行号, 说明), ...]）----------

def check_markers(ctx):
    out = []
    for lineno, line in enumerate(ctx.lines, 1):
        match = MARKER_RE.search(line)
        if match and is_comment_line(line):
            out.append((
                lineno,
                "这里残留了 %s 标记，说明逻辑可能未完成、临时绕过或待清理。" % match.group(1),
            ))
    return out


def check_secrets(ctx):
    out = []
    for lineno, line in enumerate(ctx.lines, 1):
        hit = None
        if PRIVATE_KEY_RE.search(line):
            hit = "私钥内容"
        elif AKIA_RE.search(line):
            hit = "AWS Access Key"
        elif BEARER_RE.search(line):
            hit = "Bearer Token"
        else:
            match = SECRET_ASSIGN_RE.search(line)
            if match and not _looks_like_placeholder(match.group(2), line):
                hit = "%s 的字面量" % match.group(1)
        if hit:
            out.append((
                lineno,
                "疑似把 %s 硬编码在源码里，一旦提交就会随 git 历史泄露。" % hit,
            ))
    return out


def check_dynamic_exec(ctx):
    out = []
    for lineno, line in enumerate(ctx.lines, 1):
        if is_comment_line(line):
            continue
        match = DANGEROUS_EXEC_RE.search(line) or NEW_FUNCTION_RE.search(line)
        if match:
            out.append((
                lineno,
                "使用了 %s 动态执行代码/字符串，输入一旦可控就是远程代码执行入口。" % match.group(0).strip(),
            ))
    return out


def check_empty_except(ctx):
    out = []
    lines = ctx.lines
    for idx, line in enumerate(lines):
        if is_comment_line(line):
            continue
        if PY_BARE_EXCEPT_RE.match(line):
            out.append((
                idx + 1,
                "裸 `except:` 会连 KeyboardInterrupt、SystemExit 和真实错误一起吞掉，出了问题也查不到原因。",
            ))
            continue
        if EXCEPT_PASS_RE.search(line):
            out.append((
                idx + 1,
                "`except ...: pass` 把异常静默吞掉，调用方会以为操作成功。",
            ))
            continue
        match = JS_CATCH_RE.search(line)
        if not match:
            continue
        if re.search(r"\{\s*\}", line):
            out.append((
                idx + 1,
                "空的 catch 块吞掉了异常，失败会被伪装成成功。",
            ))
            continue
        if "{" in line:
            # 处理 catch 与 } 分行的空块
            look = idx + 1
            while look < len(lines) and look <= idx + 3:
                stripped = lines[look].strip()
                if stripped == "}":
                    out.append((
                        idx + 1,
                        "空的 catch 块吞掉了异常，失败会被伪装成成功。",
                    ))
                    break
                if stripped == "" or stripped.startswith("//") or stripped.startswith("*"):
                    look += 1
                    continue
                break
    return out


def check_none_compare(ctx):
    out = []
    for lineno, line in enumerate(ctx.lines, 1):
        if is_comment_line(line):
            continue
        match = NONE_COMPARE_RE.search(line)
        if match:
            out.append((
                lineno,
                "用 `%s None` 判断空值，可能被自定义的 `__eq__` 干扰；应使用 `is None` / `is not None`。" % match.group(1),
            ))
    return out


def check_mutable_default(ctx):
    out = []
    for lineno, line in enumerate(ctx.lines, 1):
        if is_comment_line(line):
            continue
        match = MUTABLE_DEFAULT_RE.search(line)
        if match:
            out.append((
                lineno,
                "参数默认值是可变对象 `%s`，默认值只在函数定义时创建一次，多次调用会共享同一份数据。" % match.group(1),
            ))
    return out


def check_shell_risk(ctx):
    out = []
    for lineno, line in enumerate(ctx.lines, 1):
        if is_comment_line(line):
            continue
        for pattern in SHELL_RISK_RES:
            match = pattern.search(line)
            if match:
                out.append((
                    lineno,
                    "通过 shell/命令字符串执行外部命令（`%s`），拼接进来的参数可能造成命令注入。" % match.group(0).strip(),
                ))
                break
    return out


def check_sql_concat(ctx):
    out = []
    for lineno, line in enumerate(ctx.lines, 1):
        if is_comment_line(line):
            continue
        if SQL_START_RE.search(line) and SQL_CONCAT_RE.search(line):
            out.append((
                lineno,
                "SQL 语句由字符串拼接/格式化构造，用户输入可以闭合引号改变语义，造成 SQL 注入。",
            ))
    return out


def check_long_line(ctx):
    out = []
    for lineno, line in enumerate(ctx.lines, 1):
        if len(line) > LONG_LINE_LIMIT:
            out.append((
                lineno,
                "本行 %d 字符，超过 %d 字符上限，横向阅读和 diff 都很困难。" % (len(line), LONG_LINE_LIMIT),
            ))
    return out


def check_long_function(ctx):
    out = []
    lines = ctx.lines
    if ctx.ext in PY_EXTS:
        for idx, line in enumerate(lines):
            match = re.match(r"^([ \t]*)(?:async\s+)?def\s+(\w+)\s*\(", line)
            if not match:
                continue
            indent = len(match.group(1).expandtabs(4))
            end = idx
            for cursor in range(idx + 1, len(lines)):
                current = lines[cursor]
                if not current.strip():
                    continue
                if leading_width(current) <= indent:
                    break
                end = cursor
            length = end - idx
            if length > LONG_FUNCTION_LINES:
                out.append((
                    idx + 1,
                    "函数 %s 约 %d 行（启发式统计，超过 %d 行），职责多半过多，难测试也难复用。"
                    % (match.group(2), length, LONG_FUNCTION_LINES),
                ))
    elif ctx.ext in JS_EXTS:
        for idx, line in enumerate(lines):
            if not re.search(r"\bfunction\s+\w*\s*\(", line):
                continue
            depth = 0
            counted = False
            end = idx
            for cursor in range(idx, min(idx + 400, len(lines))):
                depth += lines[cursor].count("{") - lines[cursor].count("}")
                if "{" in lines[cursor]:
                    counted = True
                end = cursor
                if counted and depth <= 0:
                    break
            length = end - idx
            if length > LONG_FUNCTION_LINES:
                out.append((
                    idx + 1,
                    "函数约 %d 行（启发式统计，超过 %d 行），职责多半过多，建议拆分。" % (length, LONG_FUNCTION_LINES),
                ))
    return out


def check_debug_leftovers(ctx):
    if ctx.allow_debug:
        return []
    out = []
    for lineno, line in enumerate(ctx.lines, 1):
        if is_comment_line(line):
            continue
        match = DEBUG_RE.search(line)
        if match:
            out.append((
                lineno,
                "发现调试用输出 `%s`，上线后可能刷日志或泄露内部数据。" % match.group(0).strip(),
            ))
    return out


def check_final_newline(ctx):
    if not ctx.text or ctx.text.endswith("\n"):
        return []
    return [(len(ctx.lines) or 1, "文件末尾缺少换行，部分工具（diff、拼接、grep）会把它和下一行粘在一起。")]


def check_mixed_indent(ctx):
    out = []
    for lineno, line in enumerate(ctx.lines, 1):
        prefix = line[: len(line) - len(line.lstrip(" \t"))]
        if " " in prefix and "\t" in prefix:
            out.append((lineno, "缩进里同时出现 tab 和空格，不同编辑器下代码块层级会显示错乱。"))
    return out


def check_empty_file(ctx):
    if ctx.text.strip():
        return []
    return [(1, "文件是空的或只含空白字符，可能是误提交的占位文件。")]


# ---------- 规则表 ----------

RULE_SPECS = [
    {
        "id": "CR001",
        "name": "残留标记",
        "severity": "建议",
        "categories": ("readability",),
        "desc": "TODO / FIXME / XXX / HACK 未清理",
        "fix": FIX_MARKER,
        "check": check_markers,
    },
    {
        "id": "CR002",
        "name": "硬编码密钥",
        "severity": "严重",
        "categories": ("security",),
        "desc": "口令 / token / AK / 私钥 / 长 Bearer 写字面量",
        "fix": FIX_SECRET,
        "check": check_secrets,
    },
    {
        "id": "CR003",
        "name": "动态执行代码",
        "severity": "严重",
        "categories": ("security",),
        "desc": "eval / exec / new Function",
        "fix": FIX_DYNAMIC_EXEC,
        "check": check_dynamic_exec,
    },
    {
        "id": "CR004",
        "name": "异常被吞掉",
        "severity": "警告",
        "categories": ("bug", "security"),
        "desc": "裸 except / except: pass / 空 catch",
        "fix": FIX_EXCEPT,
        "check": check_empty_except,
    },
    {
        "id": "CR005",
        "name": "None 比较写法",
        "severity": "建议",
        "categories": ("readability",),
        "desc": "== None / != None",
        "fix": FIX_NONE_COMPARE,
        "check": check_none_compare,
    },
    {
        "id": "CR006",
        "name": "可变默认参数",
        "severity": "警告",
        "categories": ("bug",),
        "desc": "def f(x=[]) / def f(x={})",
        "fix": FIX_MUTABLE_DEFAULT,
        "check": check_mutable_default,
    },
    {
        "id": "CR007",
        "name": "命令注入风险",
        "severity": "严重",
        "categories": ("security",),
        "desc": "shell=True / os.system / child_process.exec",
        "fix": FIX_SHELL,
        "check": check_shell_risk,
    },
    {
        "id": "CR008",
        "name": "SQL 字符串拼接",
        "severity": "严重",
        "categories": ("security",),
        "desc": "用 + / f-string / format 拼 SELECT 等语句",
        "fix": FIX_SQL,
        "check": check_sql_concat,
    },
    {
        "id": "CR009",
        "name": "超长行",
        "severity": "建议",
        "categories": ("readability",),
        "desc": "单行超过 160 字符",
        "fix": FIX_LONG_LINE,
        "check": check_long_line,
    },
    {
        "id": "CR010",
        "name": "函数过长",
        "severity": "建议",
        "categories": ("readability", "perf"),
        "desc": "函数体启发式超过 80 行",
        "fix": FIX_LONG_FUNCTION,
        "check": check_long_function,
    },
    {
        "id": "CR011",
        "name": "调试残留",
        "severity": "建议",
        "categories": ("readability",),
        "desc": "print / console.log / debugger",
        "fix": FIX_DEBUG,
        "check": check_debug_leftovers,
    },
    {
        "id": "CR012",
        "name": "缺少末尾换行",
        "severity": "建议",
        "categories": ("readability",),
        "desc": "文件最后一行没有换行符",
        "fix": FIX_FINAL_NEWLINE,
        "check": check_final_newline,
    },
    {
        "id": "CR013",
        "name": "缩进混用",
        "severity": "建议",
        "categories": ("readability",),
        "desc": "同一行缩进里 tab 与空格混用",
        "fix": FIX_MIXED_INDENT,
        "check": check_mixed_indent,
    },
    {
        "id": "CR014",
        "name": "空文件",
        "severity": "警告",
        "categories": ("bug",),
        "desc": "文件为空或只含空白",
        "fix": FIX_EMPTY_FILE,
        "check": check_empty_file,
    },
]

RULES = RULE_SPECS  # 别名，便于阅读


def rules_for_focus(focus):
    """按 --focus 过滤静态规则；all 返回全部"""
    if focus == "all":
        return list(RULES)
    return [rule for rule in RULES if focus in rule["categories"]]


def analyze_source(source, rules, allow_debug):
    """对单个文件执行一组静态规则，返回 Finding 列表"""
    ctx = FileContext(source, allow_debug=allow_debug)
    findings = []
    for rule in rules:
        try:
            hits = rule["check"](ctx)
        except Exception:
            # 单条规则内部出错不能拖垮整次审查
            hits = []
        for lineno, detail in hits[:MAX_FINDINGS_PER_RULE]:
            findings.append(Finding(
                source.rel,
                lineno,
                rule["severity"],
                rule["id"],
                rule["name"],
                detail,
                rule["fix"],
            ))
    return findings


def analyze_sources(sources, focus, allow_debug=False):
    rules = rules_for_focus(focus)
    findings = []
    for source in sources:
        findings.extend(analyze_source(source, rules, allow_debug))
    return findings


# ---------- 文件收集 ----------

def _is_excluded(name, rel, excludes):
    for pattern in excludes:
        if fnmatch.fnmatch(name, pattern) or fnmatch.fnmatch(rel, pattern):
            return True
    return False


def _iter_directory(root, excludes):
    for dirpath, dirnames, filenames in os.walk(str(root)):
        dirnames[:] = sorted(
            d for d in dirnames
            if d not in IGNORED_DIRS and not _is_excluded(d, d, excludes)
        )
        for name in sorted(filenames):
            path = Path(dirpath) / name
            rel = path.relative_to(root).as_posix()
            if _is_excluded(name, rel, excludes):
                continue
            yield path, rel


def load_source(path, rel, max_chars):
    """读取一个文件；返回 (SourceFile 或 None, 跳过原因)"""
    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            raw = handle.read(max_chars * 4 + 8)
    except OSError as exc:
        return None, "读取失败：%s" % exc
    if b"\x00" in raw[:4096]:
        return None, "二进制文件"
    over_limit = size > len(raw)
    try:
        text = raw.decode("utf-8", "ignore" if over_limit else "strict")
    except UnicodeDecodeError:
        return None, "非 UTF-8 文本"
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    truncated = over_limit or len(text) > max_chars
    if len(text) > max_chars:
        text = text[:max_chars]
    return SourceFile(path, rel, text, truncated), ""


def collect_sources(paths, max_files, max_file_chars, excludes):
    """展开路径、过滤、读取。返回 (sources, skipped)"""
    sources = []
    skipped = []
    seen = set()
    for raw_path in paths:
        path = Path(raw_path).expanduser()
        if not path.exists():
            raise FileNotFoundError("路径不存在：%s" % raw_path)
        if path.is_file():
            candidates = [(path, path.name)]
            base = path.parent
        else:
            base = path
            candidates = _iter_directory(path, excludes)
        for file_path, rel in candidates:
            if len(sources) >= max_files:
                skipped.append((rel, "达到 --max-files=%d 上限，未审查" % max_files))
                continue
            if path.is_file() and _is_excluded(file_path.name, rel, excludes):
                skipped.append((rel, "--exclude 命中"))
                continue
            try:
                resolved = file_path.resolve()
            except OSError:
                resolved = file_path
            key = os.path.normcase(str(resolved))
            if key in seen:
                continue
            seen.add(key)
            source, reason = load_source(file_path, rel, max_file_chars)
            if source is None:
                skipped.append((rel, reason))
                continue
            sources.append(source)
    return sources, skipped


# ---------- 模型语义审查 ----------

def call_model(client, model, system, messages, tools=None, max_tokens=8000):
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


SEMANTIC_SYSTEM_PROMPT = """你是一名资深代码审查工程师。用户会给出若干代码文件，每行前面有行号。
请只报告真实、可定位、值得修复的问题，不要复述代码，不要编造不存在的行。

输出要求：
1. 只输出一个 JSON 数组，不要输出任何解释文字，不要使用 Markdown 代码围栏。
2. 数组每个元素包含字段：
   - file：相对路径，必须与给出的文件名完全一致
   - line：整数，文件内的绝对行号（以每行前面的行号为准）
   - severity：只能是 严重 / 警告 / 建议 之一
   - title：一句话标题
   - detail：为什么危险、会有什么后果
   - suggestion：怎么改，可以包含简短代码示意
3. 没有发现问题就输出 []。
"""

FOCUS_HINTS = {
    "all": "全面检查：安全、性能、可读性、潜在 bug 都要看。",
    "security": "重点检查安全：注入、越权、密钥泄露、不安全反序列化、输入校验、路径穿越。",
    "perf": "重点检查性能：算法复杂度、循环内重复 IO、N+1 查询、无界内存增长、可缓存点。",
    "readability": "重点检查可读性：命名、函数职责、重复代码、注释与文档、错误信息质量。",
    "bug": "重点检查潜在 bug：边界条件、异常处理、并发竞争、资源泄漏、off-by-one、时区/编码。",
}


def render_numbered(rel, start_line, lines):
    body = "\n".join("%6d | %s" % (start_line + offset, line) for offset, line in enumerate(lines))
    return "### 文件：%s\n```\n%s\n```\n\n" % (rel, body)


def build_semantic_batches(sources, limit=SEMANTIC_BATCH_CHARS):
    """把文件切成不超过 limit 字符的批次，每行带绝对行号"""
    segments = []
    for source in sources:
        lines = source.lines
        if not lines:
            segments.append({"rel": source.rel, "content": "### 文件：%s\n```\n(空文件)\n```\n\n" % source.rel})
            continue
        chunk = []
        chunk_size = 0
        start_line = 1
        for offset, line in enumerate(lines):
            rendered = "%6d | %s" % (offset + 1, line[:4000])
            if chunk and chunk_size + len(rendered) + 1 > limit - 400:
                segments.append({
                    "rel": source.rel,
                    "content": render_numbered(source.rel, start_line, chunk),
                })
                chunk = []
                chunk_size = 0
                start_line = offset + 1
            chunk.append(line[:4000])
            chunk_size += len(rendered) + 1
        if chunk:
            segments.append({
                "rel": source.rel,
                "content": render_numbered(source.rel, start_line, chunk),
            })

    batches = []
    current = []
    current_size = 0
    for segment in segments:
        content = segment["content"]
        if current and current_size + len(content) > limit:
            batches.append({
                "label": "、".join(dict.fromkeys(item["rel"] for item in current))[:120],
                "content": "".join(item["content"] for item in current),
            })
            current = []
            current_size = 0
        current.append(segment)
        current_size += len(content)
    if current:
        batches.append({
            "label": "、".join(dict.fromkeys(item["rel"] for item in current))[:120],
            "content": "".join(item["content"] for item in current),
        })
    return batches


def response_text(response):
    parts = []
    for block in getattr(response, "content", []) or []:
        if getattr(block, "type", "") == "text":
            parts.append(getattr(block, "text", ""))
    return "\n".join(parts).strip()


def parse_findings_json(text):
    """尽力把模型回复解析成 Finding 列表；解析不出来返回 None"""
    if not text:
        return None
    content = text.strip()
    if content.startswith("```"):
        content = re.sub(r"^```[A-Za-z]*\s*", "", content)
        content = re.sub(r"\s*```$", "", content)
    start = content.find("[")
    end = content.rfind("]")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(content[start:end + 1])
    except ValueError:
        return None
    if not isinstance(data, list):
        return None
    findings = []
    for item in data:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "").strip()
        rel = str(item.get("file") or "").strip()
        detail = str(item.get("detail") or "").strip()
        suggestion = str(item.get("suggestion") or "").strip()
        if not title or not rel:
            continue
        try:
            line = int(item.get("line") or 0)
        except (TypeError, ValueError):
            line = 0
        severity = str(item.get("severity") or "").strip()
        if severity not in SEVERITIES:
            severity = "建议"
        findings.append(Finding(
            rel,
            max(1, line),
            severity,
            "MODEL",
            "[模型] " + title,
            detail or "（模型未给出说明）",
            suggestion or "（模型未给出建议）",
        ))
    return findings or None


def semantic_review(sources, model, focus, batch_chars=SEMANTIC_BATCH_CHARS, verbose=True):
    """分批调用模型做语义审查，返回 (findings, raw_outputs)"""
    client = anthropic.Anthropic()
    batches = build_semantic_batches(sources, batch_chars)
    findings = []
    raw_outputs = []
    total = len(batches)
    for index, batch in enumerate(batches, 1):
        if verbose:
            print("[语义审查] 第 %d/%d 批：%s" % (index, total, batch["label"]))
        prompt = "审查重点：%s\n\n%s" % (FOCUS_HINTS.get(focus, FOCUS_HINTS["all"]), batch["content"])
        response = call_model(
            client,
            model,
            SEMANTIC_SYSTEM_PROMPT,
            [{"role": "user", "content": prompt}],
            max_tokens=8000,
        )
        text = response_text(response)
        parsed = parse_findings_json(text)
        if parsed is None:
            raw_outputs.append({"label": "第 %d 批（%s）" % (index, batch["label"]), "text": text})
            if verbose:
                print("[语义审查] 第 %d 批没有返回可解析的 JSON，已原样放入报告附录。" % index)
            continue
        findings.extend(parsed)
    return findings, raw_outputs


# ---------- 报告 ----------

def dedupe_findings(findings):
    seen = set()
    result = []
    for finding in findings:
        key = finding.key()
        if key in seen:
            continue
        seen.add(key)
        result.append(finding)
    return result


def sort_findings(findings):
    return sorted(findings, key=lambda f: (-SEVERITY_ORDER.get(f.severity, 0), f.file.lower(), f.line))


def filter_by_severity(findings, min_severity):
    threshold = SEVERITY_ORDER.get(min_severity, 1)
    return [f for f in findings if SEVERITY_ORDER.get(f.severity, 0) >= threshold]


def _indent(text, spaces=2):
    prefix = " " * spaces
    return "\n".join(prefix + line if line else line for line in text.splitlines())


def render_finding(index, finding):
    rule_label = "模型语义审查" if finding.rule_id == "MODEL" else "规则 %s" % finding.rule_id
    return "\n".join([
        "### %d. [%s] %s" % (index, finding.severity, finding.title),
        "",
        "位置：`%s:%d` · %s" % (finding.file, finding.line, rule_label),
        "",
        "- 为什么危险：%s" % finding.detail,
        "- 修复建议：",
        "",
        _indent(finding.suggestion, 2),
        "",
    ])


def render_section(title, findings):
    lines = ["## %s" % title, ""]
    if not findings:
        lines.append("（无）")
        lines.append("")
        return lines
    for index, finding in enumerate(findings, 1):
        lines.append(render_finding(index, finding))
    return lines


def build_report(sources, findings, raw_outputs, min_severity, focus, model,
                 static_only, skipped=None, generated_at=None):
    """生成完整的 Markdown 报告文本"""
    skipped = skipped or []
    filtered = filter_by_severity(findings, min_severity)
    filtered = sort_findings(dedupe_findings(filtered))
    counts = dict((severity, 0) for severity in SEVERITIES)
    for finding in filtered:
        counts[finding.severity] = counts.get(finding.severity, 0) + 1

    total_lines = sum(len(source.lines) for source in sources)
    truncated_files = [source.rel for source in sources if source.truncated]

    now = generated_at or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    lines = [
        "# 代码审查报告",
        "",
        "- 生成时间：%s" % now,
        "- 审查重点：%s" % FOCUS_LABELS.get(focus, focus),
        "- 审查模式：%s" % ("仅静态规则" if static_only else "静态规则 + 模型语义审查（%s）" % model),
        "- 最小展示级别：%s" % min_severity,
        "",
        "## 概览",
        "",
        "- 审查文件：%d 个，共 %d 行" % (len(sources), total_lines),
        "- 问题合计：%d 个（严重 %d、警告 %d、建议 %d）"
        % (len(filtered), counts["严重"], counts["警告"], counts["建议"]),
        "- 去重前发现：%d 条（已按 file:line+标题去重、按严重级别排序）" % len(findings),
        "",
    ]
    if truncated_files:
        lines.append("- 注意：%d 个文件超过 --max-file-chars，已截断审查" % len(truncated_files))
        lines.append("")

    lines.extend(render_section(
        "严重问题",
        [f for f in filtered if f.severity == "严重"],
    ))
    lines.extend(render_section(
        "警告",
        [f for f in filtered if f.severity == "警告"],
    ))
    lines.extend(render_section(
        "建议",
        [f for f in filtered if f.severity == "建议"],
    ))

    lines.append("## 附录：检查过的文件清单")
    lines.append("")
    lines.append("| 文件 | 行数 | 状态 |")
    lines.append("| --- | --- | --- |")
    for source in sources:
        status = "已截断（超过 %d 字符）" % DEFAULT_MAX_FILE_CHARS if source.truncated else "正常"
        lines.append("| `%s` | %d | %s |" % (source.rel, len(source.lines), status))
    if not sources:
        lines.append("| （无） | 0 | - |")
    lines.append("")
    if skipped:
        lines.append("已跳过：")
        lines.append("")
        for rel, reason in skipped[:100]:
            lines.append("- `%s`：%s" % (rel, reason))
        if len(skipped) > 100:
            lines.append("- ……（其余 %d 个跳过项省略）" % (len(skipped) - 100))
        lines.append("")

    if raw_outputs:
        lines.append("## 附录：模型补充意见（未能解析为 JSON）")
        lines.append("")
        lines.append("以下内容来自模型，但不符合约定的 JSON 格式，原样保留供人工参考。")
        lines.append("")
        for item in raw_outputs:
            lines.append("### %s" % item["label"])
            lines.append("")
            lines.append("```text")
            lines.append(item["text"] or "（模型没有返回任何内容）")
            lines.append("```")
            lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def render_rules_table():
    lines = [
        "代码审查内置静态规则（不需要模型）：",
        "",
        "ID      级别  类别          说明",
        "------  ----  ------------  ------------------------------------------",
    ]
    for rule in RULES:
        lines.append("%-6s  %-4s  %-12s  %s" % (
            rule["id"],
            rule["severity"],
            "/".join(rule["categories"]),
            rule["desc"],
        ))
    lines.append("")
    lines.append("共 %d 条。用 --focus 可按类别筛选，用 --allow-debug 可关闭 CR011。" % len(RULES))
    return "\n".join(lines)


# ---------- 自检 ----------

class _FakeMessages:
    def __init__(self, outer):
        self.outer = outer

    def create(self, **kwargs):
        self.outer["calls"] += 1
        if self.outer["calls"] == 1:
            raise TypeError("网关可能不认识 thinking 参数")
        return {"ok": True, "level": 3, "kwargs": kwargs}


class _FakeBetaMessages:
    def create(self, **kwargs):
        raise TypeError("网关不认识 beta 参数")


class _FakeBeta:
    def __init__(self):
        self.messages = _FakeBetaMessages()


class _FakeClient:
    def __init__(self):
        self.state = {"calls": 0}
        self.beta = _FakeBeta()
        self.messages = _FakeMessages(self.state)


def run_self_test():
    """离线自检：造样本、跑全部确定性逻辑、assert 校验，不联网、不需要 API key"""
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        (root / "sub").mkdir()

        too_long_body = ["def too_long():"] + ["    value_%d = %d" % (i, i) for i in range(85)]
        quote = '"'
        secret_name = "pass" + "word"
        exec_name = "ev" + "al"
        system_name = "os." + "system"
        sql_word = "SELECT"
        bad_parts = [
            "# TODO: 清理这段临时逻辑",
            secret_name + " = " + quote + "hunter2" + "secret" + quote,
            "def risky(x, items=" + "[]):",
            "    if x " + "==" + " None:",
            "        pass",
            "    try:",
            "        " + exec_name + "(" + quote + "1 + 1" + quote + ")",
            "        " + system_name + "(" + quote + "rm -rf /tmp/x" + quote + ")",
            "    except:",
            "        pass",
            "    sql = f" + quote + sql_word + " * FROM users WHERE id = {x}" + quote,
            '    print("debug:", x)',
            "    return items",
            "",
            "long_line = " + quote + "x" * 180 + quote,
            "",
        ] + too_long_body + [
            "",
            "\t    mixed = 1",
        ]
        bad_text = "\n".join(bad_parts)  # 故意没有末尾换行
        (root / "bad.py").write_text(bad_text, encoding="utf-8")

        clean_text = "def add(a, b):\n    return a + b\n"
        (root / "clean.py").write_text(clean_text, encoding="utf-8")
        (root / "empty.py").write_text("", encoding="utf-8")
        (root / "blob.bin").write_bytes(b"\x00\x01\x02binary")
        (root / "sub" / "todo.js").write_text(
            "function load() {\n"
            "  try {\n"
            "    run();\n"
            "  } catch (e) {}\n"
            "}\n"
            'console.log("x");\n',
            encoding="utf-8",
        )

        # --- 文件收集：二进制跳过、max-files、exclude ---
        sources, skipped = collect_sources([str(root)], 200, DEFAULT_MAX_FILE_CHARS, [])
        rels = [source.rel for source in sources]
        assert "bad.py" in rels
        assert "empty.py" in rels
        assert "blob.bin" not in rels
        assert any("二进制" in reason for _, reason in skipped)

        sources_no_js, _ = collect_sources([str(root)], 200, DEFAULT_MAX_FILE_CHARS, ["*.js"])
        assert "sub/todo.js" not in [source.rel for source in sources_no_js]

        sources_one, skipped_one = collect_sources([str(root)], 1, DEFAULT_MAX_FILE_CHARS, [])
        assert len(sources_one) == 1
        assert skipped_one

        # --- 静态规则：14 条都要能命中 ---
        findings = analyze_sources(sources, "all", allow_debug=False)
        rule_ids = set(finding.rule_id for finding in findings)
        expected = set(rule["id"] for rule in RULES)
        missing = expected - rule_ids
        assert not missing, "以下规则未命中：%s" % sorted(missing)
        assert len(RULES) >= 12
        for finding in findings:
            assert finding.file
            assert finding.line >= 1
            assert finding.severity in SEVERITIES
            assert finding.detail
            assert finding.suggestion
        assert not [f for f in findings if f.file == "clean.py"], "clean.py 不应有发现"
        pretty = [f for f in findings if f.file == "bad.py"]
        assert sum(1 for f in pretty if f.rule_id == "CR009") == 1
        assert any(f.rule_id == "CR004" and f.file == "sub/todo.js" for f in findings)
        assert any(f.rule_id == "CR002" and f.file == "bad.py" for f in findings)

        no_debug = analyze_sources(sources, "all", allow_debug=True)
        assert not [f for f in no_debug if f.rule_id == "CR011"]

        security_only = analyze_sources(sources, "security", allow_debug=False)
        allowed_security = set(
            rule["id"] for rule in RULES if "security" in rule["categories"]
        )
        assert security_only
        assert set(f.rule_id for f in security_only).issubset(allowed_security)

        # --- 去重 / 排序 / 严重级别过滤 ---
        duplicate = findings[0]
        deduped = dedupe_findings([duplicate, duplicate])
        assert len(deduped) == 1
        ordered = sort_findings(findings)
        assert SEVERITY_ORDER[ordered[0].severity] >= SEVERITY_ORDER[ordered[-1].severity]
        filtered = filter_by_severity(findings, "警告")
        assert filtered and all(SEVERITY_ORDER[f.severity] >= 2 for f in filtered)
        assert len(filtered) < len(findings)

        # --- 报告结构与输出 ---
        report = build_report(
            sources, findings, [], "建议", "all", "claude-opus-5", True,
            skipped=skipped, generated_at="2026-01-01 00:00:00",
        )
        assert report.startswith("# 代码审查报告")
        for heading in (
            "## 概览", "## 严重问题", "## 警告", "## 建议",
            "## 附录：检查过的文件清单",
        ):
            assert heading in report, "报告缺少：%s" % heading
        assert "clean.py" in report
        assert "`bad.py:" in report
        assert "为什么危险" in report and "修复建议" in report

        report_strict = build_report(
            sources, findings, [], "警告", "all", "claude-opus-5", True,
            generated_at="2026-01-01 00:00:00",
        )
        assert "- 最小展示级别：警告" in report_strict
        assert "### 1. [建议]" not in report_strict

        # --- 截断与提示 ---
        (root / "trunc").mkdir()
        (root / "trunc" / "big.py").write_text("a" * 500, encoding="utf-8")
        big_sources, _ = collect_sources([str(root / "trunc")], 10, 50, [])
        assert len(big_sources) == 1
        assert big_sources[0].truncated
        assert len(big_sources[0].text) <= 50
        report_trunc = build_report(
            big_sources, [], [], "建议", "all", "claude-opus-5", True,
            generated_at="2026-01-01 00:00:00",
        )
        assert "已截断" in report_trunc

        # --- 模型 JSON 解析与兜底 ---
        good = (
            "```json\n"
            '[{"file":"a.py","line":3,"severity":"严重","title":"注入",'
            '"detail":"危险","suggestion":"用参数化查询"}]\n'
            "```"
        )
        parsed = parse_findings_json(good)
        assert parsed and len(parsed) == 1
        assert parsed[0].line == 3 and parsed[0].severity == "严重"
        assert parsed[0].rule_id == "MODEL"
        assert parse_findings_json("这不是 JSON") is None
        assert parse_findings_json("[not json") is None
        assert parse_findings_json("[]") is None
        weird = '[{"file":"b.py","line":"x","severity":"未知","title":"t","detail":"d","suggestion":"s"}]'
        coerced = parse_findings_json(weird)
        assert coerced and coerced[0].line == 1 and coerced[0].severity == "建议"

        report_raw = build_report(
            sources, findings, [{"label": "第 1 批（a.py）", "text": "自由格式意见"}],
            "建议", "all", "claude-opus-5", False,
            generated_at="2026-01-01 00:00:00",
        )
        assert "模型补充意见（未能解析为 JSON）" in report_raw
        assert "自由格式意见" in report_raw

        # --- 批次切分：每批不超过上限，行号连续 ---
        batches = build_semantic_batches(sources, 3000)
        assert batches
        for batch in batches:
            assert len(batch["content"]) <= 3000 + 500
        numbered = batches[0]["content"]
        assert "文件：" in numbered and "|" in numbered

        # --- call_model 逐级降级（离线假客户端） ---
        fake = _FakeClient()
        result = call_model(fake, "fake-model", "system", [{"role": "user", "content": "hi"}])
        assert result["ok"] is True
        assert fake.state["calls"] == 2

        # --- 规则表与报告落盘 ---
        table = render_rules_table()
        assert "CR001" in table and "CR014" in table
        assert all(rule["id"] in table for rule in RULES)

        output_path = root / "report.md"
        output_path.write_text(report, encoding="utf-8")
        assert output_path.read_text(encoding="utf-8").startswith("# 代码审查报告")

    print("SELF-TEST OK")


# ---------- CLI ----------

def build_parser():
    parser = argparse.ArgumentParser(
        description="代码审查助手：先用确定性静态规则扫描，再交给模型做语义审查，生成中文 Markdown 报告",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            "  py -3 agent.py ./src --focus security --output 安全审查.md\n"
            "  py -3 agent.py app.py --static-only\n"
            "  py -3 agent.py . --min-severity 警告 --exclude \"*.min.js\"\n"
        ),
    )
    parser.add_argument("paths", nargs="*", help="要审查的文件或目录，可以给多个")
    parser.add_argument(
        "--focus",
        choices=["all", "security", "perf", "readability", "bug"],
        default="all",
        help="审查重点（默认 all）",
    )
    parser.add_argument(
        "--output",
        default=DEFAULT_OUTPUT,
        help="报告写入路径（默认 %s）" % DEFAULT_OUTPUT,
    )
    parser.add_argument(
        "--max-files",
        type=int,
        default=DEFAULT_MAX_FILES,
        help="最多审查多少个文件（默认 %d）" % DEFAULT_MAX_FILES,
    )
    parser.add_argument(
        "--max-file-chars",
        type=int,
        default=DEFAULT_MAX_FILE_CHARS,
        help="单文件最多读入多少字符，超出截断（默认 %d）" % DEFAULT_MAX_FILE_CHARS,
    )
    parser.add_argument(
        "--exclude",
        action="append",
        default=[],
        metavar="GLOB",
        help="排除匹配的文件/目录，可重复，如 \"*.min.js\" 或 \"tests/*\"",
    )
    parser.add_argument(
        "--static-only",
        action="store_true",
        help="只跑静态规则，不调用模型（不需要 API key）",
    )
    parser.add_argument(
        "--list-rules",
        action="store_true",
        help="打印全部内置静态规则后退出",
    )
    parser.add_argument(
        "--min-severity",
        choices=list(SEVERITIES),
        default="建议",
        help="报告里展示的最低严重级别（默认 建议，即全部展示）",
    )
    parser.add_argument(
        "--model",
        default=DEFAULT_MODEL,
        help="语义审查使用的模型（默认 %s）" % DEFAULT_MODEL,
    )
    parser.add_argument(
        "--allow-debug",
        action="store_true",
        help="不把 print / console.log / debugger 当作调试残留（关闭 CR011）",
    )
    parser.add_argument(
        "--self-test",
        action="store_true",
        help="离线自检：不联网、不需要 API key",
    )
    return parser


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except (AttributeError, OSError):
        pass

    parser = build_parser()
    args = parser.parse_args()

    if args.self_test:
        run_self_test()
        return

    if args.list_rules:
        print(render_rules_table())
        return

    if not args.paths:
        parser.error("请至少提供一个要审查的文件或目录路径")
    if args.max_files < 1:
        parser.error("--max-files 必须大于 0")
    if args.max_file_chars < 100:
        parser.error("--max-file-chars 不能小于 100")

    try:
        sources, skipped = collect_sources(
            args.paths, args.max_files, args.max_file_chars, args.exclude
        )
    except FileNotFoundError as exc:
        print("错误：%s" % exc, file=sys.stderr)
        sys.exit(1)

    if not sources:
        print(
            "错误：没有可审查的文本文件（可能被 --exclude 排除、都是二进制，或路径下没有文件）。",
            file=sys.stderr,
        )
        sys.exit(1)

    print("[静态扫描] %d 个文件，规则 %d 条，重点 %s"
          % (len(sources), len(rules_for_focus(args.focus)), FOCUS_LABELS.get(args.focus, args.focus)))
    findings = analyze_sources(sources, args.focus, allow_debug=args.allow_debug)
    semantic_findings = []
    raw_outputs = []

    if args.static_only:
        print("[静态扫描] 完成，发现 %d 条" % len(findings))
    else:
        if not os.environ.get("ANTHROPIC_API_KEY") and not os.environ.get("ANTHROPIC_AUTH_TOKEN"):
            print(
                "提示：没有检测到 ANTHROPIC_API_KEY 环境变量，无法进行模型语义审查。\n"
                "请先设置：export ANTHROPIC_API_KEY=你的key（Windows PowerShell 用 $env:ANTHROPIC_API_KEY=\"...\"）\n"
                "或者加 --static-only 只跑静态规则。",
                file=sys.stderr,
            )
            sys.exit(1)
        print("[语义审查] 模型：%s" % args.model)
        try:
            semantic_findings, raw_outputs = semantic_review(
                sources, args.model, args.focus
            )
        except anthropic.AuthenticationError:
            print("错误：API key 无效或没有权限，请检查 ANTHROPIC_API_KEY。", file=sys.stderr)
            sys.exit(1)
        except anthropic.APIConnectionError:
            print("错误：无法连接 Anthropic API，请检查网络后重试。", file=sys.stderr)
            sys.exit(1)
        except anthropic.RateLimitError:
            print("错误：请求太频繁被限流，请稍后再试。", file=sys.stderr)
            sys.exit(1)
        except anthropic.APIStatusError as exc:
            print("错误：API 返回错误（%s）：%s" % (exc.status_code, exc.message), file=sys.stderr)
            sys.exit(1)

    all_findings = list(findings) + list(semantic_findings)
    report = build_report(
        sources,
        all_findings,
        raw_outputs,
        args.min_severity,
        args.focus,
        args.model,
        args.static_only,
        skipped=skipped,
    )

    output_path = Path(args.output).expanduser()
    try:
        if output_path.parent and not output_path.parent.exists():
            output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(report, encoding="utf-8")
    except OSError as exc:
        print("错误：报告写入失败：%s" % exc, file=sys.stderr)
        sys.exit(1)

    kept = sort_findings(dedupe_findings(filter_by_severity(all_findings, args.min_severity)))
    counts = dict((severity, 0) for severity in SEVERITIES)
    for finding in kept:
        counts[finding.severity] = counts.get(finding.severity, 0) + 1
    print("[完成] 报告已写入：%s" % output_path)
    print("[完成] 文件 %d 个，问题 %d 个（严重 %d、警告 %d、建议 %d）"
          % (len(sources), len(kept), counts["严重"], counts["警告"], counts["建议"]))
    if raw_outputs:
        print("[提示] 有 %d 批模型输出不是合法 JSON，已原样放入报告附录。" % len(raw_outputs))


if __name__ == "__main__":
    main()
