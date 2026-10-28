#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
代码翻译助手 —— 语言检测 + 术语映射 + 结构对照 + 模型翻译 的两段式跨语言翻译 agent

先完全确定性地识别源语言（扩展名 + 内容启发式）、做语法预检（Python 用 ast.parse 报行号），
再用内置 80+ 条术语/API 对照表（python ↔ javascript ↔ typescript ↔ go ↔ java）产出
“映射命中清单”与“未映射 API 清单”，并对函数数/类数/注释数做翻译前后结构对照；
离线模式用规则做确定性翻译（Python 会把缩进块转成花括号），在线模式把对照表喂给模型做正式翻译，
最后对译文做目标语言语法自检（不合法就写 .rej 文件并告警，不静默通过）。
除 anthropic SDK 外只用标准库，Python 3.9 语法兼容。

用法：
    python3 agent.py <源文件> --to python|javascript|typescript|go|java [选项]

示例：
    python3 agent.py utils.py --to javascript --output utils.js
    python3 agent.py utils.py --to go --keep-comments --report 翻译报告.md
    python3 agent.py app.js --to python --offline

环境要求：
    1. 模型翻译需要设置 ANTHROPIC_API_KEY 或 ANTHROPIC_AUTH_TOKEN：
           export ANTHROPIC_API_KEY=你的key
       （Windows PowerShell：$env:ANTHROPIC_API_KEY="你的key"）
       （--offline 与 --self-test 不需要 key）
    2. 安装 SDK：
           pip3 install -r requirements.txt
    3. 走第三方 Anthropic 兼容网关时，用 --model 指定网关侧的模型名，
       例如：python3 agent.py utils.py --to javascript --model qwen3.8-max
"""

import argparse
import ast
import json
import os
import re
import sys
import tempfile
from datetime import datetime
from pathlib import Path

import anthropic

# 默认模型（可用 --model 参数或 CODE_TRANSLATOR_MODEL 环境变量更换）
DEFAULT_MODEL = os.environ.get("CODE_TRANSLATOR_MODEL", "claude-opus-5")

DEFAULT_REPORT = "translation-report.md"
MAX_SOURCE_CHARS = 100_000      # 源文件最多读入多少字符，超出截断并注明

TARGET_LANGS = ("python", "javascript", "typescript", "go", "java")
LANG_ALIASES = {
    "py": "python", "python3": "python",
    "js": "javascript", "jsx": "javascript", "node": "javascript",
    "ts": "typescript", "tsx": "typescript",
    "golang": "go", "c++": "cpp",
}
LANG_EXTENSIONS = {
    ".py": "python", ".pyi": "python",
    ".js": "javascript", ".jsx": "javascript", ".mjs": "javascript", ".cjs": "javascript",
    ".ts": "typescript", ".tsx": "typescript",
    ".go": "go",
    ".java": "java",
}
TARGET_EXT = {
    "python": ".py", "javascript": ".js", "typescript": ".ts",
    "go": ".go", "java": ".java",
}
LANG_INDEX = {"python": 0, "javascript": 1, "typescript": 2, "go": 3, "java": 4}
COMMENT_PREFIX = {
    "python": "#", "javascript": "//", "typescript": "//",
    "go": "//", "java": "//",
}

# ---------- 术语 / API 对照表（列顺序：python, javascript, typescript, go, java, 可安全文本替换） ----------
# 前 5 列是各语言写法，第 6 列表示能否直接做文本替换（False 表示结构不同、只做对照提示）
TERMS = [
    ("print(", "console.log(", "console.log(", "fmt.Println(", "System.out.println(", True),
    ("len(", ".length", ".length", "len(", ".length", False),
    ("range(", "Array.from(", "Array.from(", "make([]int, 0)", "IntStream.range(", False),
    ("str(", "String(", "String(", "fmt.Sprint(", "String.valueOf(", True),
    ("int(", "parseInt(", "parseInt(", "strconv.Atoi(", "Integer.parseInt(", True),
    ("float(", "parseFloat(", "parseFloat(", "strconv.ParseFloat(", "Double.parseDouble(", True),
    ("bool(", "Boolean(", "Boolean(", "strconv.ParseBool(", "Boolean.parseBoolean(", True),
    ("abs(", "Math.abs(", "Math.abs(", "math.Abs(", "Math.abs(", True),
    ("min(", "Math.min(", "Math.min(", "math.Min(", "Math.min(", True),
    ("max(", "Math.max(", "Math.max(", "math.Max(", "Math.max(", True),
    ("sum(", "reduce((a, b) => a + b, 0)", "reduce((a, b) => a + b, 0)", "循环累加", "Arrays.stream(x).sum()", False),
    ("sorted(", "[...x].sort(", "[...x].sort(", "sort.Slice(", "Arrays.sort(", False),
    ("reversed(", "[...x].reverse()", "[...x].reverse()", "反向切片", "Collections.reverse(", False),
    ("enumerate(", "entries()", "entries()", "下标 + range", "下标 + 循环", False),
    ("zip(", "zip 辅助函数", "zip 辅助函数", "并行遍历", "并行遍历", False),
    ("isinstance(", "typeof / instanceof", "typeof / instanceof", "type 断言", "instanceof", False),
    ("type(", "typeof", "typeof", "%T", "x.getClass()", False),
    ("append(", ".push(", ".push(", "append(", ".add(", True),
    ("dict.items()", "Object.entries()", "Object.entries()", "for range 遍历", "entrySet()", False),
    ("dict.keys()", "Object.keys()", "Object.keys()", "for range 遍历", "keySet()", False),
    ("dict.values()", "Object.values()", "Object.values()", "for range 遍历", "values()", False),
    ("None", "null", "null", "nil", "null", True),
    ("True", "true", "true", "true", "true", True),
    ("False", "false", "false", "false", "false", True),
    ("self.", "this.", "this.", "this.", "this.", True),
    ("elif ", "else if ", "else if ", "} else if ", "} else if ", True),
    ("f-string", "模板字符串 `${...}`", "模板字符串 `${...}`", "fmt.Sprintf", "String.format", False),
    ("raise ", "throw ", "throw ", "panic(", "throw ", True),
    ("try:", "try {", "try {", "defer + 错误返回", "try {", False),
    ("except ", "catch (", "catch (", "if err != nil", "catch (", False),
    ("finally:", "} finally {", "} finally {", "defer", "} finally {", False),
    ("with ", "try / finally", "try / finally", "defer", "try / finally", False),
    ("open(", "fs.readFileSync(", "fs.readFileSync(", "os.Open(", "new FileInputStream(", False),
    ("json.dumps(", "JSON.stringify(", "JSON.stringify(", "json.Marshal(", "gson.toJson(", True),
    ("json.loads(", "JSON.parse(", "JSON.parse(", "json.Unmarshal(", "gson.fromJson(", True),
    ("os.path.join(", "path.join(", "path.join(", "path.Join(", "Paths.get(", True),
    (".strip()", ".trim()", ".trim()", "strings.TrimSpace(", ".trim()", True),
    (".split(", ".split(", ".split(", "strings.Split(", ".split(", True),
    (".join(", ".join(", ".join(", "strings.Join(", "String.join(", True),
    (".upper()", ".toUpperCase()", ".toUpperCase()", "strings.ToUpper(", ".toUpperCase()", True),
    (".lower()", ".toLowerCase()", ".toLowerCase()", "strings.ToLower(", ".toLowerCase()", True),
    (".startswith(", ".startsWith(", ".startsWith(", "strings.HasPrefix(", ".startsWith(", True),
    (".endswith(", ".endsWith(", ".endsWith(", "strings.HasSuffix(", ".endsWith(", True),
    (".replace(", ".replace(", ".replace(", "strings.ReplaceAll(", ".replace(", True),
    (".format(", "模板字符串", "模板字符串", "fmt.Sprintf(", "String.format(", False),
    (".append(", ".push(", ".push(", "append(", ".add(", True),
    ("range(len(", "Array.from(x.keys())", "Array.from(x.keys())", "for i := range", "IntStream.range(", False),
    ("lambda ", "箭头函数 =>", "箭头函数 =>", "匿名函数", "Lambda 表达式", False),
    ("yield ", "function* + yield", "function* + yield", "channel / 回调", "Stream / Iterator", False),
    ("__init__", "constructor", "constructor", "NewXxx()", "构造方法", False),
    ("@staticmethod", "static", "static", "包级函数", "static", False),
    ("@property", "get 访问器", "get 访问器", "方法", "get 访问器", False),
    ("super()", "super()", "super()", "内嵌结构体", "super()", True),
    ("str.join", "Array.join", "Array.join", "strings.Join", "String.join", False),
    ("input(", "readline(", "readline(", "fmt.Scan(", "Scanner.nextLine(", False),
    ("round(", "Math.round(", "Math.round(", "math.Round(", "Math.round(", True),
    ("math.sqrt(", "Math.sqrt(", "Math.sqrt(", "math.Sqrt(", "Math.sqrt(", True),
    ("collections.Counter", "Map 计数", "Map 计数", "map 计数", "Map 计数", False),
    ("数据类型 dict", "Map / Object", "Map<K, V>", "map[K]V", "HashMap<K, V>", False),
    ("数据类型 list", "Array", "T[]", "[]T", "List<T>", False),
    ("数据类型 str", "string", "string", "string", "String", False),
    ("数据类型 int", "number", "number", "int", "int", False),
    ("数据类型 float", "number", "number", "float64", "double", False),
    ("数据类型 bool", "boolean", "boolean", "bool", "boolean", False),
    ("单行注释 #", "//", "//", "//", "//", False),
    ("多行字符串 \"\"\"", "反引号 `", "反引号 `", "反引号 `", "文本块", False),
    ("docstring", "JSDoc 注释", "JSDoc 注释", "包注释", "Javadoc", False),
    ("元组 tuple", "数组 [a, b]", "元组 [a, b]", "结构体 / 切片", "List / record", False),
    ("集合 set", "Set", "Set<T>", "map[T]bool", "HashSet<T>", False),
    ("推导式", "map / filter", "map / filter", "for 循环", "Stream API", False),
]

# 各语言内置 API 名（启发式找“未映射 API”用）
BUILTIN_APIS = {
    "python": {
        "abs", "all", "any", "ascii", "bin", "bool", "bytearray", "bytes", "callable",
        "chr", "classmethod", "compile", "complex", "delattr", "dict", "dir", "divmod",
        "enumerate", "eval", "exec", "filter", "float", "format", "frozenset", "getattr",
        "globals", "hasattr", "hash", "help", "hex", "id", "input", "int", "isinstance",
        "issubclass", "iter", "len", "list", "locals", "map", "max", "memoryview", "min",
        "next", "object", "oct", "open", "ord", "pow", "print", "property", "range",
        "repr", "reversed", "round", "set", "setattr", "slice", "sorted", "staticmethod",
        "str", "sum", "super", "tuple", "type", "vars", "zip",
    },
    "javascript": {
        "Array", "Boolean", "Date", "Error", "JSON", "Map", "Math", "Number", "Object",
        "Promise", "RegExp", "Set", "String", "Symbol", "console", "decodeURI",
        "encodeURI", "eval", "isFinite", "isNaN", "parseInt", "parseFloat", "require",
    },
    "typescript": {
        "Array", "Boolean", "Date", "Error", "JSON", "Map", "Math", "Number", "Object",
        "Promise", "RegExp", "Set", "String", "Symbol", "console", "parseInt", "parseFloat",
    },
    "go": {
        "append", "cap", "close", "complex", "copy", "delete", "len", "make", "new",
        "panic", "print", "println", "recover",
    },
    "java": {
        "System", "String", "Math", "Arrays", "Collections", "List", "Map", "Set",
        "Integer", "Double", "Boolean", "Objects", "Stream", "Scanner",
    },
}

# Python 关键字（不算 API 调用）
PY_KEYWORDS = {
    "if", "elif", "else", "for", "while", "def", "class", "return", "yield", "lambda",
    "try", "except", "finally", "with", "as", "import", "from", "raise", "assert",
    "pass", "break", "continue", "global", "nonlocal", "del", "in", "is", "not",
    "and", "or", "print",
}


# ---------- 语言检测与语法预检 ----------

def normalize_lang(name):
    text = str(name or "").strip().lower()
    return LANG_ALIASES.get(text, text)


def detect_language(path, text):
    """语言检测：先看扩展名，再看内容特征，返回 (语言, 判定依据)"""
    suffix = Path(path).suffix.lower() if path else ""
    if suffix in LANG_EXTENSIONS:
        return LANG_EXTENSIONS[suffix], "扩展名 %s" % suffix

    content = text or ""
    score = {
        "python": len(re.findall(r"(?m)^\s*(def|class|import|from)\s+\w", content))
                  + 2 * len(re.findall(r"(?m)^\s*(if|for|while)\s.*:\s*$", content)),
        "javascript": len(re.findall(r"\b(function|const|let|var|=>)\b", content)),
        "typescript": len(re.findall(r"\b(interface|type\s+\w+\s*=|:\s*(string|number|boolean)\b)", content)),
        "go": len(re.findall(r"(?m)^(package|func|import)\s", content)) * 2
              + len(re.findall(r":=", content)),
        "java": len(re.findall(r"\b(public|private|static)\s+(class|void|int|String)\b", content)) * 2,
    }
    best = max(score, key=lambda key: score[key])
    if score[best] == 0:
        return "python", "无法识别，默认按 python 处理"
    return best, "内容启发式（得分 %d）" % score[best]


def strip_strings_and_comments(text, lang):
    """去掉字符串与注释，返回 (净化后的文本, 问题列表)，用于括号配对等检查"""
    out = []
    problems = []
    i, n = 0, len(text)
    line_no = 1
    quote = None
    quote_line = 0
    block_comment = False
    line_comment = False
    line_comment_mark = COMMENT_PREFIX.get(lang, "//")
    while i < n:
        ch = text[i]
        nxt = text[i + 1] if i + 1 < n else ""
        if block_comment:
            if ch == "*" and nxt == "/":
                block_comment = False
                i += 2
                continue
            if ch == "\n":
                line_no += 1
                out.append("\n")
            i += 1
            continue
        if line_comment:
            if ch == "\n":
                line_comment = False
                line_no += 1
                out.append("\n")
            i += 1
            continue
        if quote:
            if ch == "\\":
                i += 2
                continue
            if ch == quote:
                quote = None
            elif ch == "\n":
                problems.append({"line": quote_line, "message": "字符串引号未闭合"})
                quote = None
                line_no += 1
                out.append("\n")
            i += 1
            continue
        if ch in "\"'`":
            quote = ch
            quote_line = line_no
            i += 1
            continue
        if ch == "/" and nxt == "/":
            line_comment = True
            line_comment_mark = "//"
            i += 2
            continue
        if ch == "/" and nxt == "*":
            block_comment = True
            i += 2
            continue
        if ch == "#" and lang == "python":
            line_comment = True
            line_comment_mark = "#"
            i += 1
            continue
        if ch == "\n":
            line_no += 1
        out.append(ch)
        i += 1
    if quote:
        problems.append({"line": quote_line, "message": "字符串引号未闭合"})
    if block_comment:
        problems.append({"line": line_no, "message": "块注释未闭合"})
    return "".join(out), problems


def check_syntax(lang, text, expect_functions=False):
    """目标语言语法自检（尽力而为），返回问题列表 [{"line", "message"}]"""
    issues = []
    if not (text or "").strip():
        return [{"line": 0, "message": "译文为空"}]
    if lang == "python":
        try:
            ast.parse(text)
        except SyntaxError as exc:
            issues.append({"line": exc.lineno or 0,
                           "message": "Python 语法错误：%s" % (exc.msg or exc)})
        return issues

    stripped, problems = strip_strings_and_comments(text, lang)
    issues.extend(problems)
    for open_ch, close_ch, name in (("{", "}", "花括号"), ("(", ")", "圆括号"), ("[", "]", "方括号")):
        delta = stripped.count(open_ch) - stripped.count(close_ch)
        if delta:
            issues.append({"line": 0,
                           "message": "%s不配对，差 %d 个" % (name, abs(delta))})
    if lang in ("javascript", "typescript") and expect_functions:
        if count_js_functions(stripped) == 0:
            issues.append({"line": 0, "message": "启发式检查：没有发现 function / => / 方法定义"})
    return issues


# ---------- 结构对照 ----------

def python_comment_lines(text):
    """Python 的注释行 + docstring 行（docstring 算文档注释，便于与译文注释对齐）"""
    lines = []
    doc_open = False
    doc_delim = ""
    for raw in text.splitlines():
        stripped = raw.strip()
        if doc_open:
            lines.append(raw)
            if doc_delim in stripped:
                doc_open = False
            continue
        if stripped.startswith("#"):
            lines.append(raw)
            continue
        match = re.match(r"^(?:[rbufRBUF]*)('''|\"\"\")", stripped)
        if match:
            lines.append(raw)
            delim = match.group(1)
            if delim not in stripped[match.end():]:
                doc_open = True
                doc_delim = delim
    return lines


def count_js_functions(text):
    """JS/TS 函数计数：function 声明 + 箭头函数 + constructor + 类方法简写"""
    count = text.count("=>")
    for raw in text.splitlines():
        line = raw.strip()
        if re.match(r"^(?:async\s+)?function\b", line):
            count += 1
        elif re.match(r"^(?:async\s+)?(?:constructor|\w+)\s*\([^)]*\)\s*\{\s*$", line) and not re.match(
                r"^(?:if|for|while|switch|catch|do|else|try|return|new|typeof|function)\b", line):
            count += 1
    return count


def count_structure(lang, text):
    """统计函数数 / 类数 / 注释数 / import 数（目标语言正则计数）"""
    counts = {"函数": 0, "类": 0, "注释": 0, "导入": 0}
    if lang == "python":
        counts["函数"] = len(re.findall(r"(?m)^\s*(?:async\s+)?def\s+\w+", text))
        counts["类"] = len(re.findall(r"(?m)^\s*class\s+\w+", text))
        counts["注释"] = len(python_comment_lines(text))
        counts["导入"] = len(re.findall(r"(?m)^\s*(?:import|from)\s+\w", text))
    elif lang in ("javascript", "typescript"):
        counts["函数"] = count_js_functions(text)
        counts["类"] = len(re.findall(r"\bclass\s+\w+", text))
        counts["注释"] = len(re.findall(r"(?m)^\s*(?://|/\*|\*)", text))
        counts["导入"] = len(re.findall(r"(?m)^\s*import\b", text))
    elif lang == "go":
        counts["函数"] = len(re.findall(r"(?m)^\s*func\s", text))
        counts["类"] = len(re.findall(r"\btype\s+\w+\s+struct\b", text))
        counts["注释"] = len(re.findall(r"(?m)^\s*//", text))
        counts["导入"] = len(re.findall(r"(?m)^\s*import\b", text))
    else:  # java
        counts["函数"] = len(re.findall(
            r"(?m)^\s*(?:public|private|protected|static|final|\s)*[\w<>\[\],. ]+\s+\w+\s*\([^;]*\)\s*\{", text))
        counts["类"] = len(re.findall(r"\bclass\s+\w+", text))
        counts["注释"] = len(re.findall(r"(?m)^\s*(?://|/\*|\*)", text))
        counts["导入"] = len(re.findall(r"(?m)^\s*import\b", text))
    return counts


def compare_structure(src_counts, dst_counts):
    """翻译前后结构计数对照，返回差异列表"""
    rows = []
    for key in ("函数", "类", "注释", "导入"):
        src = src_counts.get(key, 0)
        dst = dst_counts.get(key, 0)
        rows.append({"item": key, "src": src, "dst": dst, "diff": dst - src,
                     "ok": src == dst})
    return rows


# ---------- 术语映射 ----------

def build_mapping(src_lang, dst_lang):
    """按语言对生成对照字典：源写法 -> (目标写法, 是否可安全文本替换)"""
    if src_lang not in LANG_INDEX or dst_lang not in LANG_INDEX:
        return {}
    si, di = LANG_INDEX[src_lang], LANG_INDEX[dst_lang]
    mapping = {}
    for row in TERMS:
        left, right = row[si], row[di]
        if left and right and left != right:
            mapping[left] = (right, bool(row[5]))
    return mapping


def count_mapping_pairs():
    """统计内置对照条目总数（跨全部语言对）"""
    total = 0
    for row in TERMS:
        for a in LANG_INDEX:
            for b in LANG_INDEX:
                if a == b:
                    continue
                left, right = row[LANG_INDEX[a]], row[LANG_INDEX[b]]
                if left and right and left != right:
                    total += 1
    return total


def token_pattern(token):
    """给术语生成带词边界的正则，避免误伤标识符"""
    escaped = re.escape(token)
    if re.match(r"\w", token):
        escaped = r"\b" + escaped
    if re.search(r"\w$", token):
        escaped = escaped + r"\b"
    return re.compile(escaped)


def analyze_terms(text, src_lang, dst_lang):
    """找出源代码里命中的术语，返回 [{src, dst, count, auto}]"""
    mapping = build_mapping(src_lang, dst_lang)
    hits = []
    for token in sorted(mapping, key=lambda item: (-len(item), item)):
        target, auto = mapping[token]
        count = len(token_pattern(token).findall(text))
        if count:
            hits.append({"src": token, "dst": target, "count": count, "auto": auto})
    return hits


def find_unmapped_apis(text, lang, dst_lang):
    """启发式找内置 API 调用里没被对照表覆盖的部分"""
    mapping = build_mapping(lang, dst_lang)
    covered = set()
    for token in mapping:
        for piece in re.findall(r"[A-Za-z_][A-Za-z0-9_]*", token):
            covered.add(piece)
    builtins = BUILTIN_APIS.get(lang, set())
    found = {}
    for match in re.finditer(r"([A-Za-z_][A-Za-z0-9_.]*)\s*\(", text):
        raw = match.group(1)
        name = raw.split(".")[-1]
        if name not in builtins:
            continue
        if raw in mapping or name in covered:
            continue
        found[raw] = found.get(raw, 0) + 1
    return [{"api": name, "count": count} for name, count in sorted(found.items())]


def map_terms(text, src_lang, dst_lang):
    """做安全的术语级文本替换，返回 (替换后的文本, 命中清单)"""
    mapping = build_mapping(src_lang, dst_lang)
    hits = []
    result = text
    for token in sorted(mapping, key=lambda item: (-len(item), item)):
        target, auto = mapping[token]
        if not auto:
            continue
        pattern = token_pattern(token)
        count = len(pattern.findall(result))
        if count:
            result = pattern.sub(target, result)
            hits.append({"src": token, "dst": target, "count": count})
    return result, hits


def extract_comments(text, lang):
    """抽出注释行（含原始文本），供 --keep-comments 使用"""
    if lang == "python":
        return python_comment_lines(text)
    lines = []
    in_block = False
    for raw in text.splitlines():
        stripped = raw.strip()
        if in_block:
            lines.append(raw)
            if "*/" in stripped:
                in_block = False
            continue
        if stripped.startswith("//"):
            lines.append(raw)
        elif stripped.startswith("/*"):
            lines.append(raw)
            if "*/" not in stripped:
                in_block = True
    return lines


# ---------- 确定性翻译（术语映射 + Python 缩进块 → 花括号） ----------

def strip_param_annotations(params):
    """去掉参数里的类型注解与默认值保留，兼容 JS/TS/Go/Java 风格"""
    parts = []
    for chunk in params.split(","):
        piece = chunk.strip()
        if not piece:
            continue
        piece = re.sub(r"^\*+", "", piece)
        piece = re.sub(r"^(\w+)\s*:[^=]+", r"\1", piece)
        parts.append(piece.strip())
    return ", ".join(parts)


def drop_self_param(params, target):
    """目标语言的方法都不需要 self / cls 参数（Go 用接收者 this *X 代替），直接去掉"""
    parts = [item.strip() for item in params.split(",") if item.strip()]
    if parts and parts[0] in ("self", "cls"):
        parts = parts[1:]
    return ", ".join(parts)


def convert_statement(line, target, class_name=None):
    """把一条 Python 语句转换成目标语言写法；返回 None 表示按原样保留"""
    stripped = line.strip()
    if not stripped:
        return ""
    if stripped.startswith("#"):
        return None

    match = re.match(r"^async\s+def\s+(\w+)\s*\((.*)\)\s*(->\s*[^:]+)?\s*:\s*$", stripped)
    if match:
        name = match.group(1)
        params = drop_self_param(strip_param_annotations(match.group(2)), target)
        if class_name:
            if target in ("javascript", "typescript"):
                return "async %s(%s) {" % (name, params)
            if target == "go":
                return "func (this *%s) %s(%s) {" % (class_name, name, params)
            return "public Object %s(%s) {" % (name, params)
        if target in ("javascript", "typescript"):
            return "async function %s(%s) {" % (name, params)
        if target == "go":
            return "func %s(%s) {" % (name, params)
        return "public static Object %s(%s) {" % (name, params)

    match = re.match(r"^def\s+(\w+)\s*\((.*)\)\s*(->\s*[^:]+)?\s*:\s*$", stripped)
    if match:
        name = match.group(1)
        params = drop_self_param(strip_param_annotations(match.group(2)), target)
        if class_name:
            # 类里的方法：JS/TS 用方法简写，Go 带接收者，Java 用实例方法
            if target in ("javascript", "typescript"):
                if name == "__init__":
                    return "constructor(%s) {" % params
                return "%s(%s) {" % (name, params)
            if target == "go":
                return "func (this *%s) %s(%s) {" % (class_name, name, params)
            return "public Object %s(%s) {" % (name, params)
        if target in ("javascript", "typescript"):
            return "function %s(%s) {" % (name, params)
        if target == "go":
            return "func %s(%s) {" % (name, params)
        return "public static Object %s(%s) {" % (name, params)

    match = re.match(r"^class\s+(\w+)\s*\(([^)]*)\)\s*:\s*$", stripped)
    if match:
        name, bases = match.group(1), match.group(2).strip()
        if target in ("javascript", "typescript"):
            return "class %s extends %s {" % (name, bases) if bases else "class %s {" % name
        if target == "go":
            return "type %s struct {" % name
        return "public class %s%s {" % (name, (" extends %s" % bases) if bases else "")

    match = re.match(r"^class\s+(\w+)\s*:\s*$", stripped)
    if match:
        name = match.group(1)
        if target in ("javascript", "typescript"):
            return "class %s {" % name
        if target == "go":
            return "type %s struct {" % name
        return "public class %s {" % name

    match = re.match(r"^if\s+(.+):\s*$", stripped)
    if match:
        cond = match.group(1).strip()
        return ("if %s {" % cond) if target == "go" else ("if (%s) {" % cond)

    match = re.match(r"^elif\s+(.+):\s*$", stripped)
    if match:
        cond = match.group(1).strip()
        return ("} else if %s {" % cond) if target == "go" else ("} else if (%s) {" % cond)

    if re.match(r"^else\s*:\s*$", stripped):
        return "} else {"

    match = re.match(r"^for\s+(\w+)\s+in\s+range\((.+)\)\s*:\s*$", stripped)
    if match:
        if target in ("javascript", "typescript"):
            return "for (let %s = 0; %s < %s; %s++) {" % (
                match.group(1), match.group(1), match.group(2).strip(), match.group(1))
        if target == "go":
            return "for %s := 0; %s < %s; %s++ {" % (
                match.group(1), match.group(1), match.group(2).strip(), match.group(1))
        return "for (int %s = 0; %s < %s; %s++) {" % (
            match.group(1), match.group(1), match.group(2).strip(), match.group(1))

    match = re.match(r"^for\s+(\w+)\s+in\s+(.+):\s*$", stripped)
    if match:
        if target in ("javascript", "typescript"):
            return "for (const %s of %s) {" % (match.group(1), match.group(2).strip())
        if target == "go":
            return "for _, %s := range %s {" % (match.group(1), match.group(2).strip())
        return "for (Object %s : %s) {" % (match.group(1), match.group(2).strip())

    match = re.match(r"^while\s+(.+):\s*$", stripped)
    if match:
        cond = match.group(1).strip()
        # Go 没有 while 关键字，用 for 代替
        return ("for %s {" % cond) if target == "go" else ("while (%s) {" % cond)

    if re.match(r"^try\s*:\s*$", stripped):
        return "try {"

    match = re.match(r"^except\s*([^:]*)\s*(as\s+\w+)?\s*:\s*$", stripped)
    if match:
        var = (match.group(2) or "as e").split()[-1]
        return "} catch (%s) {" % var

    if re.match(r"^finally\s*:\s*$", stripped):
        return "} finally {"

    match = re.match(r"^with\s+(.+?)\s+as\s+(\w+)\s*:\s*$", stripped)
    if match:
        return "const %s = %s; // with 语句请手工管理资源" % (match.group(2), match.group(1).strip())

    match = re.match(r"^with\s+(.+):\s*$", stripped)
    if match:
        return "// with 语句请手工管理资源：%s" % match.group(1).strip()

    match = re.match(r"^raise\s+(\w+)\s*\((.*)\)\s*$", stripped)
    if match:
        message = match.group(2).strip()
        if target in ("javascript", "typescript"):
            return "throw new Error(%s);" % (message or '""')
        if target == "go":
            return "panic(%s)" % (message or '""')
        return "throw new RuntimeException(%s);" % (message or '""')

    if re.match(r"^raise\s*$", stripped):
        return "throw new Error();" if target in ("javascript", "typescript") else "panic()"

    match = re.match(r"^import\s+(\w+)\s*$", stripped)
    if match:
        module = match.group(1)
        if target in ("javascript", "typescript"):
            return 'import %s from "%s";' % (module, module)
        return "// 导入 %s（按目标语言的包管理方式手工补充）" % module

    match = re.match(r"^from\s+([\w.]+)\s+import\s+(.+)$", stripped)
    if match:
        module, names = match.group(1), match.group(2).strip()
        if target in ("javascript", "typescript"):
            return 'import { %s } from "%s";' % (names, module)
        return "// 从 %s 导入 %s（按目标语言的包管理方式手工补充）" % (module, names)

    if stripped == "pass":
        return ";"
    return None


def depth_delta(text, lang):
    """统计一行里未闭合的括号数量（用于判断多行表达式的续行）"""
    stripped, _ = strip_strings_and_comments(text, lang)
    return (stripped.count("(") + stripped.count("[") + stripped.count("{")
            - stripped.count(")") - stripped.count("]") - stripped.count("}"))


def is_function_header(body):
    """判断花括号开头那一行是不是函数/方法头（而不是 if/for/while 这类控制语句）"""
    if re.match(r"^(?:if|for|while|switch|catch|do|else|try|finally|with)\b", body):
        return False
    if body.startswith(("func ", "public ", "private ", "protected ", "static ", "constructor(")):
        return True
    return bool(re.match(r"^(?:async\s+)?(?:function\s+)?\w+\s*\(", body))


def python_to_braces(text, target, keep_comments=True):
    """把 Python 的缩进块结构转成花括号结构（JS/TS/Go/Java 通用，尽力而为）"""
    out = []
    stack = []          # [(缩进, 块类型, 类名)]，块类型：class / def / block
    pending = 0
    doc_open = False
    doc_delim = ""
    doc_indent = 0

    def enclosing_class():
        for _level, kind, name in reversed(stack):
            if kind == "class":
                return name
        return None

    for raw in text.splitlines():
        line = raw.rstrip()
        stripped = line.strip()
        indent = len(line) - len(line.lstrip())
        if doc_open:
            # docstring 块整体转成注释（或按需丢弃）
            if keep_comments:
                out.append(" " * doc_indent + COMMENT_PREFIX.get(target, "//") + " " + stripped)
            if doc_delim in stripped:
                doc_open = False
            continue
        if not stripped:
            out.append("")
            continue

        doc = re.match(r"^(?:[rbufRBUF]*)('''|\"\"\")", stripped)
        if doc:
            if keep_comments:
                out.append(" " * indent + COMMENT_PREFIX.get(target, "//") + " " + stripped)
            if doc.group(1) not in stripped[doc.end():]:
                doc_open = True
                doc_delim = doc.group(1)
                doc_indent = indent
            continue

        converted = convert_statement(line, target, class_name=enclosing_class())
        if converted is None:
            if stripped.startswith("#"):
                if keep_comments:
                    out.append(" " * indent + COMMENT_PREFIX.get(target, "//") + " "
                               + stripped.lstrip("#").strip())
                continue
            converted = stripped

        is_continuation = bool(re.match(r"^(elif\b|else\s*:|except\b|finally\s*:)", stripped))
        if pending <= 0:
            if is_continuation:
                while stack and indent < stack[-1][0]:
                    out.append(" " * stack.pop()[0] + "}")
                if stack and indent == stack[-1][0]:
                    stack.pop()  # 由 "} else ..." 这一行自带的右花括号闭合
            else:
                while stack and indent <= stack[-1][0]:
                    out.append(" " * stack.pop()[0] + "}")

        prefix = " " * indent
        opens_block = converted.endswith("{") and pending <= 0
        if (not opens_block and pending <= 0 and converted.strip()
                and not converted.strip().startswith("//")
                and not converted.rstrip().endswith(("{", "}", ";", ",", "(", "["))
                and not is_continuation):
            converted = converted.rstrip() + ";"
        out.append(prefix + converted)

        if opens_block:
            body = converted.strip()
            # 用 Python 源码判断是不是类块（目标语言里类可能叫 struct / class 等）
            class_match = re.match(r"^class\s+(\w+)", stripped)
            if class_match:
                stack.append((indent, "class", class_match.group(1)))
            elif is_function_header(body):
                stack.append((indent, "def", ""))
            else:
                stack.append((indent, "block", ""))
        pending += depth_delta(line, "python")
        if pending < 0:
            pending = 0

    while stack:
        out.append(" " * stack.pop()[0] + "}")
    return "\n".join(out).rstrip() + "\n"


def rule_translate(text, src_lang, dst_lang, keep_comments=True):
    """离线确定性翻译：术语映射 + （Python→JS/TS/Go/Java）缩进块转花括号"""
    if not keep_comments:
        text = "\n".join(line for line in text.splitlines()
                         if not line.strip().startswith(COMMENT_PREFIX.get(src_lang, "#")))
    if src_lang == "python" and dst_lang in ("javascript", "typescript", "go", "java"):
        converted = python_to_braces(text, dst_lang, keep_comments=keep_comments)
        result, _ = map_terms(converted, src_lang, dst_lang)
        return result
    result, _ = map_terms(text, src_lang, dst_lang)
    if src_lang != dst_lang and COMMENT_PREFIX.get(src_lang) != COMMENT_PREFIX.get(dst_lang):
        prefix = COMMENT_PREFIX.get(dst_lang, "//")
        lines = []
        for line in result.splitlines():
            stripped = line.strip()
            if stripped.startswith(COMMENT_PREFIX.get(src_lang, "#")):
                line = line.replace(COMMENT_PREFIX.get(src_lang, "#"), prefix, 1)
            lines.append(line)
        result = "\n".join(lines)
    return result if result.endswith("\n") else result + "\n"


# ---------- 模型翻译 ----------

TRANSLATE_SYSTEM_PROMPT = """你是一名资深多语言工程师，负责把源代码翻译成目标语言。
要求：
1. 只输出目标语言的代码本身，绝对不要输出解释文字，绝对不要使用 Markdown 代码围栏。
2. 保持原有结构：函数数量、类数量、注释数量尽量与源代码一致，函数与类的名字保持一致。
3. 优先使用目标语言的惯用写法；源代码里的注释请翻译成中文并保留（用户要求保留注释时）。
4. 不要遗漏任何函数，不要编造源代码里没有的依赖。
5. 术语对照表如下（仅供参考，输出仍然是目标语言代码）：
"""


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


def response_text(response):
    parts = []
    for block in getattr(response, "content", []) or []:
        if getattr(block, "type", "") == "text":
            parts.append(getattr(block, "text", ""))
    return "\n".join(parts).strip()


def detect_fence_language(text):
    """识别 ```javascript 这类代码围栏的语言标记，没有就返回空串"""
    match = re.search(r"```([A-Za-z+#]+)\s*\n", text or "")
    return match.group(1).lower() if match else ""


def strip_code_fence(text):
    """剥掉 Markdown 代码围栏与前后解释文字，只留代码"""
    if not text:
        return ""
    content = text.strip()
    if "```" in content:
        start = content.find("```")
        rest = content[start + 3:]
        newline = rest.find("\n")
        rest = rest[newline + 1:] if newline >= 0 else ""
        end = rest.rfind("```")
        if end >= 0:
            return rest[:end].strip() + "\n"
        return rest.strip() + "\n"
    return content + ("\n" if not content.endswith("\n") else "")


def build_model_prompt(source, src_lang, dst_lang, keep_comments, comments):
    """拼装给模型的翻译请求：术语对照 + 可选注释清单 + 源代码"""
    mapping = build_mapping(src_lang, dst_lang)
    table_lines = ["%s → %s" % (item[0], item[1]) for item in sorted(mapping.items())[:60]]
    parts = [
        "源语言：%s，目标语言：%s" % (src_lang, dst_lang),
        "术语对照表（部分）：",
        "；".join(table_lines) if table_lines else "（无）",
    ]
    if keep_comments and comments:
        parts.append("必须保留的注释（共 %d 条）：" % len(comments))
        parts.extend(comments[:50])
    parts.append("源代码：")
    parts.append(source)
    parts.append("请输出目标语言代码。")
    return TRANSLATE_SYSTEM_PROMPT + "\n".join(table_lines), "\n".join(parts)


# ---------- 报告 ----------

def build_report(info, term_hits, unmapped, structure_rows, issues, comments,
                 model_notes, raw_model_text, offline, notes):
    """生成中文 Markdown 翻译报告"""
    lines = ["# 代码翻译报告", ""]
    lines.append("> 由 code-translator 生成；确定性分析 + %s。"
                 % ("离线规则翻译（--offline）" if offline else "模型翻译"))
    lines.append("")
    lines.append("## 概览")
    lines.append("")
    lines.append("| 指标 | 数值 |")
    lines.append("| --- | --- |")
    lines.append("| 源文件 | `%s` |" % info["source"])
    lines.append("| 源语言 | %s（%s） |" % (info["src_lang"], info["detect_reason"]))
    lines.append("| 目标语言 | %s |" % info["dst_lang"])
    lines.append("| 输出文件 | `%s` |" % info["output"])
    lines.append("| 保留注释 | %s |" % ("是" if info["keep_comments"] else "否"))
    lines.append("| 术语对照条目 | %d |" % count_mapping_pairs())
    lines.append("| 映射命中 | %d 条 |" % len(term_hits))
    lines.append("| 未映射 API | %d 条 |" % len(unmapped))
    lines.append("| 语法自检 | %s |" % ("通过" if not issues else "有 %d 条警告" % len(issues)))
    lines.append("")

    lines.append("## 语言检测与语法预检")
    lines.append("")
    lines.append("- 语言判定：%s" % info["detect_reason"])
    if info["precheck"]:
        for item in info["precheck"]:
            lines.append("- 源代码第 %s 行：%s" % (item["line"] or "?", item["message"]))
    else:
        lines.append("- 源代码语法预检：未发现问题")
    lines.append("")

    lines.append("## 映射命中清单")
    lines.append("")
    if not term_hits:
        lines.append("没有命中内置术语对照表。")
        lines.append("")
    else:
        lines.append("| 源写法 | 目标写法 | 次数 | 可自动替换 |")
        lines.append("| --- | --- | --- | --- |")
        for hit in term_hits:
            lines.append("| `%s` | `%s` | %d | %s |" % (
                hit["src"], hit["dst"], hit["count"], "是" if hit["auto"] else "否（需手工）"))
        lines.append("")

    lines.append("## 未映射 API 清单")
    lines.append("")
    if not unmapped:
        lines.append("没有发现对照表未覆盖的内置 API 调用。")
        lines.append("")
    else:
        lines.append("| API | 次数 | 说明 |")
        lines.append("| --- | --- | --- |")
        for item in unmapped:
            lines.append("| `%s` | %d | 对照表里没有这个 API，需要人工确认译法 |"
                         % (item["api"], item["count"]))
        lines.append("")

    lines.append("## 结构对照")
    lines.append("")
    lines.append("| 结构 | 源代码 | 译文 | 差异 | 是否一致 |")
    lines.append("| --- | --- | --- | --- | --- |")
    for row in structure_rows:
        lines.append("| %s | %d | %d | %+d | %s |" % (
            row["item"], row["src"], row["dst"], row["diff"],
            "一致" if row["ok"] else "不一致"))
    lines.append("")
    if info["keep_comments"]:
        lines.append("注释保留核对：源代码注释 %d 条，译文注释 %d 条。"
                     % (info["src_comments"], info["dst_comments"]))
        lines.append("")

    lines.append("## 警告与建议")
    lines.append("")
    if issues:
        for item in issues:
            lines.append("- 第 %s 行：%s" % (item["line"] or "?", item["message"]))
        if info.get("rej_path"):
            lines.append("- 译文已写入 `%s`，请人工修正后再用。" % info["rej_path"])
    else:
        lines.append("- 目标语言语法自检通过（启发式检查，不能替代编译器）。")
    for row in structure_rows:
        if not row["ok"]:
            if row["item"] == "注释" and not info.get("keep_comments"):
                continue  # 用户没要求保留注释时，注释数差异是预期内的
            lines.append("- %s数量不一致（源 %d / 译 %d），请检查是否有遗漏或冗余。"
                         % (row["item"], row["src"], row["dst"]))
    lines.append("")

    if model_notes:
        lines.append("## 模型翻译说明")
        lines.append("")
        for note in model_notes:
            lines.append("- %s" % note)
        lines.append("")

    if notes:
        lines.append("## 备注")
        lines.append("")
        for note in notes:
            lines.append("- %s" % note)
        lines.append("")

    if raw_model_text and not model_notes:
        lines.append("## 附录：模型原始输出")
        lines.append("")
        lines.append("```text")
        lines.append(raw_model_text[:8000])
        lines.append("```")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


# ---------- 自检 ----------

class _FakeMessages:
    """假的消息接口：第一次抛 TypeError（模拟网关不认 thinking），第二次成功"""

    def __init__(self, outer):
        self.outer = outer

    def create(self, **kwargs):
        self.outer["calls"] += 1
        if self.outer["calls"] == 1:
            raise TypeError("网关可能不认识 thinking 参数")
        return {"ok": True, "level": 3, "kwargs": kwargs}


class _FakeBetaMessages:
    """假的 beta 接口：永远抛 TypeError（模拟网关不认 beta 参数）"""

    def __init__(self, outer):
        self.outer = outer

    def create(self, **kwargs):
        self.outer["calls"] += 1
        raise TypeError("网关不认识 beta 参数")


class _FakeBeta:
    def __init__(self, outer):
        self.messages = _FakeBetaMessages(outer)


class _FakeClient:
    def __init__(self):
        self.state = {"calls": 0}
        self.beta = _FakeBeta(self.state)
        self.messages = _FakeMessages(self.state)


SAMPLE_PY = '''"""示例：自检用的 Python 源码"""
import math


def add(a, b=0):
    """把两个数相加

    Args:
        a: 第一个加数
        b: 第二个加数

    Returns:
        两数之和
    """
    # 正常相加
    return a + b


class Counter:
    """一个计数器"""

    def __init__(self, start=0):
        self.value = start

    def bump(self, step=1):
        """加一"""
        for i in range(3):
            if i == 0:
                self.value = add(self.value, step)
            elif i == 1:
                self.value = add(self.value, 0)
            else:
                print("done", self.value)
        return self.value
'''


def run_self_test():
    """离线自检：造样本、跑全部确定性逻辑、assert 校验，不联网、不需要 API key"""
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        src_path = root / "sample.py"
        src_path.write_text(SAMPLE_PY, encoding="utf-8")

        # --- 语言检测：扩展名 + 内容启发式 ---
        lang, reason = detect_language(str(src_path), SAMPLE_PY)
        assert lang == "python" and "扩展名" in reason
        lang, reason = detect_language("noext", "def foo():\n    return 1\n")
        assert lang == "python", "内容启发式应识别出 python：%s" % reason
        lang, _ = detect_language("noext", "function foo() {\n  return 1;\n}\nconst x = 1;\n")
        assert lang in ("javascript", "typescript")
        lang, _ = detect_language("noext", "package main\nfunc main() {}\n")
        assert lang == "go"
        lang, _ = detect_language("a.ts", "interface X {}")
        assert lang == "typescript"
        assert normalize_lang("JS") == "javascript" and normalize_lang("py") == "python"

        # --- 语法预检：Python 用 ast.parse 报行号 ---
        assert not check_syntax("python", SAMPLE_PY)
        broken = check_syntax("python", "def f(:\n    pass\n")
        assert broken and broken[0]["line"] == 1 and "语法错误" in broken[0]["message"]

        # --- 目标语言语法自检（Python 目标 + JS 目标） ---
        assert not check_syntax("python", "def g(x):\n    return x\n")
        bad_js = check_syntax("javascript", "function f( {\n  return 1\n")
        assert bad_js, "括号不配对必须报出来"
        assert any("不配对" in item["message"] for item in bad_js)
        empty = check_syntax("javascript", "   ")
        assert empty and "为空" in empty[0]["message"]

        # --- 术语对照表：条目数量、命中、未映射 API ---
        assert count_mapping_pairs() >= 80, "内置对照条目不足 80 条"
        hits = analyze_terms(SAMPLE_PY, "python", "javascript")
        hit_src = [item["src"] for item in hits]
        assert "print(" in hit_src, "print( 应当命中对照表"
        assert "self." in hit_src, "self. 应当命中对照表"
        print_hit = [item for item in hits if item["src"] == "print("][0]
        assert print_hit["dst"] == "console.log("
        unmapped = find_unmapped_apis("y = hash('f')\nz = iter([1, 2])\nw = all([True])\n",
                                      "python", "javascript")
        unmapped_names = [item["api"] for item in unmapped]
        assert "hash" in unmapped_names and "iter" in unmapped_names, \
            "未映射 API 检测失效：%s" % unmapped_names
        mapped_apis = find_unmapped_apis("x = [1].append(2)\n", "python", "javascript")
        assert not [item for item in mapped_apis if item["api"] == "append"], \
            "对照表已覆盖的 API 不该重复报"
        js_unmapped = find_unmapped_apis("const a = JSON.parse('{}');\n", "javascript", "python")
        assert not [item for item in js_unmapped if item["api"] == "JSON.parse"], "已映射的 API 不该重复报"

        # --- 内置小样本对照：Python → JS，映射生效 + 结构计数一致 ---
        js_code = rule_translate(SAMPLE_PY, "python", "javascript", keep_comments=True)
        assert "console.log(" in js_code, "print( → console.log( 映射没有生效"
        assert "this.value" in js_code, "self. → this. 映射没有生效"
        assert "function add(" in js_code, "def → function 转换没有生效"
        assert "class Counter {" in js_code, "class 转换没有生效"
        assert "for (let i = 0;" in js_code, "range 循环转换没有生效"
        assert "} else if (" in js_code, "elif → else if 转换没有生效"
        assert "// 正常相加" in js_code, "注释应当保留"
        src_counts = count_structure("python", SAMPLE_PY)
        dst_counts = count_structure("javascript", js_code)
        rows = compare_structure(src_counts, dst_counts)
        row_map = dict((item["item"], item) for item in rows)
        assert row_map["函数"]["src"] == row_map["函数"]["dst"], "函数数不一致：%s" % rows
        assert row_map["类"]["src"] == row_map["类"]["dst"], "类数不一致：%s" % rows
        assert row_map["注释"]["src"] == row_map["注释"]["dst"], "注释数不一致：%s" % rows
        js_issues = check_syntax("javascript", js_code)
        assert not js_issues, "规则翻译出的 JS 不通过语法自检：%s" % js_issues

        # 不保留注释时注释应被去掉
        js_no_comment = rule_translate(SAMPLE_PY, "python", "javascript", keep_comments=False)
        assert "// 正常相加" not in js_no_comment

        # --- 注释抽取 ---
        comments = extract_comments(SAMPLE_PY, "python")
        assert any("正常相加" in item for item in comments)
        assert len(comments) == src_counts["注释"], "注释抽取数量应与结构计数一致"

        # --- 结构对照：差异要能被发现 ---
        diff_rows = compare_structure({"函数": 3, "类": 1, "注释": 2, "导入": 1},
                                      {"函数": 2, "类": 1, "注释": 5, "导入": 1})
        assert [row["item"] for row in diff_rows if not row["ok"]] == ["函数", "注释"]

        # --- 代码围栏剥离 + 语言标记识别 ---
        fenced = "这是说明\n```javascript\nconst a = 1;\n```\n补充说明"
        assert strip_code_fence(fenced).strip() == "const a = 1;"
        assert detect_fence_language(fenced) == "javascript"
        assert strip_code_fence("const a = 1;\n") == "const a = 1;\n"
        assert detect_fence_language("const a = 1;") == ""

        # --- 报告生成 ---
        info = {
            "source": str(src_path), "src_lang": "python", "dst_lang": "javascript",
            "detect_reason": "扩展名 .py", "output": str(root / "sample.js"),
            "keep_comments": True, "precheck": [], "src_comments": src_counts["注释"],
            "dst_comments": dst_counts["注释"], "rej_path": "",
        }
        report = build_report(info, hits, unmapped, rows, [], comments, [], "",
                              True, ["示例备注"])
        assert report.startswith("# 代码翻译报告")
        for heading in ("## 概览", "## 语言检测与语法预检", "## 映射命中清单",
                        "## 未映射 API 清单", "## 结构对照", "## 警告与建议"):
            assert heading in report, "报告缺少 %s" % heading
        assert "console.log(" in report

        warn_report = build_report(info, hits, unmapped, diff_rows,
                                   [{"line": 3, "message": "花括号不配对，差 1 个"}],
                                   comments, ["模型补了 2 条断言"], "", False, [])
        assert "花括号不配对" in warn_report
        assert "模型补了 2 条断言" in warn_report

        raw_report = build_report(info, hits, unmapped, diff_rows, [],
                                  comments, [], "原始模型输出", False, [])
        assert "原始模型输出" in raw_report

        # --- 落盘与 .rej 文件语义：译文合法写正式文件，不合法写 .rej ---
        out_path = root / "sample.js"
        out_path.write_text(js_code, encoding="utf-8")
        assert out_path.read_text(encoding="utf-8") == js_code
        bad_js_code = "function f( {\n  return 1\n"
        rej_path = root / "broken.js.rej"
        if check_syntax("javascript", bad_js_code):
            rej_path.write_text(bad_js_code, encoding="utf-8")
        assert rej_path.exists() and rej_path.read_text(encoding="utf-8") == bad_js_code

        # --- call_model 逐级降级（离线假客户端） ---
        fake = _FakeClient()
        result = call_model(fake, "fake-model", "system", [{"role": "user", "content": "hi"}])
        assert result["ok"] is True
        assert fake.state["calls"] == 2, "降级调用次数应该是 2，实际 %s" % fake.state["calls"]

    print("SELF-TEST OK")


# ---------- CLI ----------

def build_parser():
    parser = argparse.ArgumentParser(
        description="跨语言代码翻译：语言检测 + 术语映射 + 结构对照 + 模型翻译，输出译文与中文报告",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            "  python3 agent.py utils.py --to javascript --output utils.js\n"
            "  python3 agent.py utils.py --to go --keep-comments --report 翻译报告.md\n"
            "  python3 agent.py app.js --to python --offline\n"
        ),
    )
    parser.add_argument("source", nargs="?", default="", help="源代码文件路径")
    parser.add_argument(
        "--to",
        choices=list(TARGET_LANGS),
        default="",
        help="目标语言（翻译时必填）",
    )
    parser.add_argument(
        "--output",
        default="",
        help="译文写入路径（默认与源文件同名、扩展名按目标语言）",
    )
    parser.add_argument(
        "--report",
        default=DEFAULT_REPORT,
        help="翻译报告写入路径（默认 %s）" % DEFAULT_REPORT,
    )
    parser.add_argument(
        "--keep-comments",
        action="store_true",
        help="保留注释：注释行会单独交给模型，并在报告里核对注释数量",
    )
    parser.add_argument(
        "--offline",
        action="store_true",
        help="纯确定性模式：只做术语映射与结构转换，不调用模型、不需要 API key",
    )
    parser.add_argument(
        "--model",
        default=DEFAULT_MODEL,
        help="翻译使用的模型（默认 %s）" % DEFAULT_MODEL,
    )
    parser.add_argument(
        "--self-test",
        action="store_true",
        help="离线自检：不联网、不需要 API key",
    )
    return parser


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass

    parser = build_parser()
    args = parser.parse_args()

    if args.self_test:
        run_self_test()
        return

    if not args.source:
        parser.error("请提供源代码文件路径")
    if not args.to:
        parser.error("请用 --to 指定目标语言（python / javascript / typescript / go / java）")

    source_path = Path(args.source).expanduser()
    if not source_path.exists() or not source_path.is_file():
        print("错误：源文件不存在：%s" % args.source, file=sys.stderr)
        sys.exit(1)

    notes = []
    try:
        source_text = source_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        print("错误：读取源文件失败：%s" % exc, file=sys.stderr)
        sys.exit(1)
    if len(source_text) > MAX_SOURCE_CHARS:
        notes.append("源文件超过 %d 字符，已截断处理" % MAX_SOURCE_CHARS)
        source_text = source_text[:MAX_SOURCE_CHARS]

    src_lang, detect_reason = detect_language(str(source_path), source_text)
    dst_lang = args.to
    precheck = check_syntax(src_lang, source_text) if src_lang == "python" else []
    print("[检测] 源语言 %s（%s） → 目标语言 %s" % (src_lang, detect_reason, dst_lang))
    for item in precheck:
        print("[预检] 源代码第 %s 行：%s" % (item["line"] or "?", item["message"]))

    term_hits = analyze_terms(source_text, src_lang, dst_lang)
    unmapped = find_unmapped_apis(source_text, src_lang, dst_lang)
    comments = extract_comments(source_text, src_lang) if args.keep_comments else []
    print("[映射] 命中 %d 条术语对照，未映射 API %d 条"
          % (len(term_hits), len(unmapped)))

    src_counts = count_structure(src_lang, source_text)
    model_notes = []
    raw_model_text = ""
    output_path = Path(args.output).expanduser() if args.output else \
        source_path.with_name(source_path.stem + TARGET_EXT[dst_lang])

    if args.offline:
        if src_lang == "python":
            print("[离线] --offline 模式：术语映射 + 缩进块转花括号")
            model_notes.append("离线模式：译文由确定性规则生成（术语映射 + 缩进块转花括号），"
                               "复杂语义与类型细节请用模型模式复核。")
        else:
            print("[离线] --offline 模式：仅做术语映射（%s → %s 没有确定性语法转换）"
                  % (src_lang, dst_lang))
            model_notes.append("离线模式：%s → %s 只做术语级对照，不做完整语法转换，"
                               "译文很可能需要人工改写或改用模型模式。" % (src_lang, dst_lang))
        translated = rule_translate(source_text, src_lang, dst_lang,
                                    keep_comments=args.keep_comments)
    else:
        if not os.environ.get("ANTHROPIC_API_KEY") and not os.environ.get("ANTHROPIC_AUTH_TOKEN"):
            print(
                "提示：没有检测到 ANTHROPIC_API_KEY / ANTHROPIC_AUTH_TOKEN，无法调用模型翻译。\n"
                "请先设置：\n"
                "  macOS / Linux：export ANTHROPIC_API_KEY=你的key\n"
                "  Windows PowerShell：$env:ANTHROPIC_API_KEY=\"你的key\"\n"
                "或者加 --offline 使用确定性规则翻译（不需要 key）。",
                file=sys.stderr,
            )
            sys.exit(1)
        print("[翻译] 模型：%s" % args.model)
        system_prompt, user_prompt = build_model_prompt(
            source_text, src_lang, dst_lang, args.keep_comments, comments)
        client = anthropic.Anthropic()
        try:
            response = call_model(client, args.model, system_prompt,
                                  [{"role": "user", "content": user_prompt}],
                                  max_tokens=16000)
            raw_model_text = response_text(response)
        except anthropic.AuthenticationError:
            print("错误：API key 无效或没有权限，请检查 ANTHROPIC_API_KEY。", file=sys.stderr)
            sys.exit(1)
        except anthropic.APIConnectionError:
            print("错误：无法连接模型服务，请检查网络后重试。", file=sys.stderr)
            sys.exit(1)
        except anthropic.RateLimitError:
            print("错误：请求太频繁被限流，请稍后再试。", file=sys.stderr)
            sys.exit(1)
        except anthropic.APIStatusError as exc:
            print("错误：API 返回错误（%s）：%s" % (exc.status_code, exc.message), file=sys.stderr)
            sys.exit(1)

        fence_lang = detect_fence_language(raw_model_text)
        translated = strip_code_fence(raw_model_text)
        if fence_lang and fence_lang not in (dst_lang, dst_lang[:2]):
            notes.append("模型返回的代码围栏语言标记是 %s，与目标语言 %s 不一致" % (fence_lang, dst_lang))
        if not translated.strip():
            notes.append("模型没有返回有效代码，已改用确定性规则翻译")
            translated = rule_translate(source_text, src_lang, dst_lang,
                                        keep_comments=args.keep_comments)

    dst_counts = count_structure(dst_lang, translated)
    structure_rows = compare_structure(src_counts, dst_counts)
    issues = check_syntax(dst_lang, translated, expect_functions=src_counts["函数"] > 0)

    rej_path = ""
    try:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        if issues:
            rej_path = str(output_path) + ".rej"
            Path(rej_path).write_text(translated, encoding="utf-8")
            notes.append("译文语法自检未通过，已写入 %s 而不是当成正常结果" % rej_path)
            print("[警告] 译文语法自检未通过，结果已写入 %s" % rej_path)
        else:
            output_path.write_text(translated, encoding="utf-8")
            print("[完成] 译文已写入：%s" % output_path)
    except OSError as exc:
        print("错误：写入译文失败：%s" % exc, file=sys.stderr)
        sys.exit(1)

    info = {
        "source": str(source_path), "src_lang": src_lang, "dst_lang": dst_lang,
        "detect_reason": detect_reason, "output": str(output_path),
        "keep_comments": args.keep_comments, "precheck": precheck,
        "src_comments": src_counts["注释"], "dst_comments": dst_counts["注释"],
        "rej_path": rej_path,
    }
    report = build_report(info, term_hits, unmapped, structure_rows, issues,
                          comments, model_notes, raw_model_text, args.offline, notes)

    report_path = Path(args.report).expanduser()
    try:
        if report_path.parent and not report_path.parent.exists():
            report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(report, encoding="utf-8")
    except OSError as exc:
        print("错误：报告写入失败：%s" % exc, file=sys.stderr)
        sys.exit(1)

    print("[完成] 报告已写入：%s" % report_path)
    mismatched = [row for row in structure_rows if not row["ok"]
                  and not (row["item"] == "注释" and not args.keep_comments)]
    if mismatched:
        print("[提示] 结构计数有 %d 项不一致：%s"
              % (len(mismatched),
                 "、".join("%s %d→%d" % (row["item"], row["src"], row["dst"])
                           for row in mismatched)))
    if args.keep_comments:
        print("[提示] 注释核对：源 %d 条 / 译 %d 条" % (src_counts["注释"], dst_counts["注释"]))


if __name__ == "__main__":
    main()
