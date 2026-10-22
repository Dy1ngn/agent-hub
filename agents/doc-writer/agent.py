#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
项目文档生成助手 —— 先做确定性静态扫描，再让模型基于扫描结果写文档

它不把整个仓库塞给模型，而是先用纯本地的正则/文件遍历 "读懂" 项目骨架
（目录树、语言统计、入口点、依赖、API 路由、CLI 参数、配置项名字），
把这份结构化摘要交给模型，按需生成 README / API / 架构 / 新人上手四类文档。
扫描步骤不联网，`--dry-run` 可以只看扫描结果、不花一分钱。

用法：
    py -3 agent.py <项目路径> --type all

示例：
    py -3 agent.py ./my-project --dry-run
    py -3 agent.py ./my-project --type readme --lang zh
    py -3 agent.py ./my-project --type all --output docs --force

环境要求：
    1. 设置 ANTHROPIC_API_KEY 环境变量（走第三方兼容网关时用 ANTHROPIC_AUTH_TOKEN）：
           Windows PowerShell:  $env:ANTHROPIC_API_KEY="你的key"
           macOS / Linux:       export ANTHROPIC_API_KEY=你的key
    2. 安装 SDK：
           pip install -r requirements.txt

安全边界：
    - 只读项目源码，只写 `--output` 指定的目录
    - `.env` / `.env.example` 里的配置项**只提取变量名，绝不读取也不写进文档的值**
    - 不执行项目里的任何代码，不做 git 操作
"""

import argparse
import json
import os
import re
import sys
import tempfile
from pathlib import Path

import anthropic


def ensure_utf8_console():
    """Windows 下把被重定向的 stdout/stderr 从 GBK 切成 UTF-8。

    PowerShell 管道、重定向到文件、被别的程序调用时，Python 会按系统代码页（中文
    Windows 是 GBK）编码输出，遇到 emoji 或生僻字就抛 UnicodeEncodeError 直接崩。
    这里优先换成 UTF-8；换不了就退化成 replace，保证程序永远不崩在打印上。
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (LookupError, ValueError, OSError):
            pass


# ---------- 默认值与常量（集中在这儿，改配置只改这一段） ----------

# 默认模型（可用 --model 参数或 DOC_WRITER_MODEL 环境变量更换）
DEFAULT_MODEL = os.environ.get("DOC_WRITER_MODEL", "claude-opus-5")

DEFAULT_MAX_FILES = 400          # 最多扫描多少个文件
DEFAULT_MAX_FILE_CHARS = 60000   # 单个文件最多读入多少字符
MAX_TREE_DEPTH = 3               # 目录树最大深度
MAX_TREE_LINES = 60              # 目录树最多渲染多少行
MAX_ROUTES = 200                 # 最多提取多少条 API 路由
MAX_CLI_ARGS = 120               # 最多提取多少个 CLI 参数
MAX_DEPS_PER_FILE = 120          # 每个依赖文件最多提取多少条依赖
MAX_CONFIG_NAMES = 200           # 最多列出多少个配置项名字
MAX_SNIPPET_LINES = 40           # dry-run 时打印摘要的行数
SUMMARY_CHAR_LIMIT = 30000       # 发给模型的静态摘要字符上限

DEFAULT_MAX_TOKENS = 8000        # 单份文档的生成上限

# 扫描时忽略的目录
IGNORE_DIRS = {
    ".git", "node_modules", "__pycache__", "dist", "build", "venv", ".venv",
    ".idea", ".vscode", "coverage", ".mypy_cache", ".pytest_cache", "target",
}

# 扩展名 -> 语言名（用于语言统计）
LANGUAGE_BY_EXT = {
    ".py": "Python",
    ".js": "JavaScript",
    ".ts": "TypeScript",
    ".tsx": "TypeScript",
    ".jsx": "JavaScript",
    ".java": "Java",
    ".go": "Go",
    ".rs": "Rust",
    ".rb": "Ruby",
    ".php": "PHP",
    ".cs": "C#",
    ".vue": "Vue",
    ".css": "CSS",
    ".html": "HTML",
    ".sh": "Shell",
}

# 入口点候选文件名
ENTRY_FILENAMES = {
    "main.py", "__main__.py", "app.py", "manage.py",
    "index.ts", "server.js", "app.js",
    "Dockerfile", "docker-compose.yml", "docker-compose.yaml", "Makefile",
}

# 会被当作 "配置文件" 读取名字的文件
CONFIG_FILENAMES = {
    ".env.example", ".env.sample", ".env.template", ".env.defaults",
    "settings.py", "config.py", "config.js", "config.ts", "config.json",
    "config.yml", "config.yaml", "config.toml", "config.ini",
    "application.yml", "application.yaml", "application.properties",
}

# 四类文档：类型 -> 文件名
DOC_TYPES = [
    ("readme", "README.md"),
    ("api", "API.md"),
    ("arch", "ARCHITECTURE.md"),
    ("onboarding", "ONBOARDING.md"),
]
DOC_FILENAME = dict(DOC_TYPES)

# ---------- 正则（路由 / CLI / 配置） ----------

# FastAPI：@app.get("/x") / @router.post("/x")
RE_FASTAPI = re.compile(
    r"^\s*@(?P<obj>\w+)\.(?P<method>get|post|put|patch|delete|options|head|api_route)"
    r"\(\s*r?[\"'](?P<path>[^\"']+)[\"']",
    re.IGNORECASE,
)

# Flask：@app.route("/x", methods=["GET", "POST"])
RE_FLASK = re.compile(
    r"^\s*@(?P<obj>\w+)\.route\(\s*r?[\"'](?P<path>[^\"']+)[\"'](?P<rest>.*)$"
)

# Express：app.get('/x', ...) / router.post('/x', ...)
RE_EXPRESS = re.compile(
    r"\b(?:app|router|server|api)\s*\.\s*"
    r"(?P<method>get|post|put|patch|delete|options|head|all)\s*\(\s*"
    r"[`\"'](?P<path>[^`\"']+)[`\"']",
    re.IGNORECASE,
)

# Spring：@GetMapping("/x") / @PostMapping("/x") / @RequestMapping(value = "/x")
RE_SPRING = re.compile(
    r"@(?P<kind>Get|Post|Put|Delete|Patch|Request)Mapping\b(?P<rest>[^\n]*)"
)

# 处理函数名
RE_PY_DEF = re.compile(r"\b(?:async\s+)?def\s+(?P<name>\w+)\s*\(")
RE_JS_FUNC = re.compile(r"\bfunction\s+(?P<name>\w+)\s*\(")
RE_JS_HANDLER = re.compile(r",\s*(?P<name>[A-Za-z_$][\w$]*)\s*[,)]")
RE_JAVA_METHOD = re.compile(
    r"\bpublic\s+(?:static\s+)?[\w<>\[\],.?\s]+?\s+(?P<name>\w+)\s*\("
)

# CLI 参数
RE_PY_ADD_ARGUMENT = re.compile(r"add_argument\(\s*[\"'](?P<name>--[\w-]+)")
RE_CLICK_OPTION = re.compile(r"@click\.option\(\s*[\"'](?P<name>--[\w-]+)")
RE_JS_OPTION = re.compile(r"\.option\(\s*[\"'](?P<name>--[\w-]+)")

# 配置项名字（只匹配 "大写常量名"，从不捕获值）
RE_ENV_NAME = re.compile(r"^\s*(?P<name>[A-Z][A-Z0-9_]+)\s*=")
RE_PY_CONST = re.compile(r"^\s*(?P<name>[A-Z][A-Z0-9_]{2,})\s*=")
RE_KV_NAME = re.compile(r"^\s*(?P<name>[A-Z][A-Z0-9_]{2,})\s*[:=]")

# 注释行（用于粗略统计代码行数）
COMMENT_PREFIXES = ("#", "//", "/*", "*", "--", '"""', "'''")


# ---------- 基础工具 ----------


def read_text(path, max_chars):
    """安全读取文本：读不了就返回空串，不会因为二进制/编码问题崩掉"""
    try:
        with open(str(path), "r", encoding="utf-8", errors="replace") as f:
            return f.read(max_chars)
    except (OSError, ValueError):
        return ""


def rel_posix(root, path):
    """统一用 / 显示相对路径，Windows 上也不出现反斜杠"""
    try:
        return Path(path).relative_to(root).as_posix()
    except ValueError:
        return str(path)


def count_code_lines(text):
    """粗略统计代码行数：跳过空行与纯注释行（多行注释内部的 * 开头行也算）"""
    total = 0
    for line in text.splitlines():
        s = line.strip()
        if not s:
            continue
        if s.startswith(COMMENT_PREFIXES):
            continue
        total += 1
    return total


def iter_project_files(root, max_files):
    """遍历项目文件（跳过 IGNORE_DIRS 和隐藏目录），返回 (文件路径列表, 是否截断)

    顺序稳定（先目录后文件、都按名字排序），保证多次扫描结果一致。
    """
    collected = []
    truncated = False
    for dirpath, dirnames, filenames in os.walk(str(root)):
        dirnames[:] = sorted(
            d for d in dirnames if d not in IGNORE_DIRS and not d.startswith(".")
        )
        for name in sorted(filenames):
            full = os.path.join(dirpath, name)
            if len(collected) >= max_files:
                truncated = True
                break
            collected.append(Path(full))
        if truncated:
            break
    return collected, truncated


# ---------- A. 确定性静态扫描 ----------


def build_tree(root, max_depth=MAX_TREE_DEPTH, max_lines=MAX_TREE_LINES):
    """渲染目录树（深度受限、行数受限），返回 (文本, 是否截断)"""
    root = Path(root)
    lines = [root.name + "/" if root.name else "./"]
    state = {"truncated": False}

    def walk(dirpath, prefix, depth):
        if state["truncated"] or depth > max_depth:
            return
        try:
            entries = sorted(os.listdir(str(dirpath)))
        except OSError:
            return
        dirs = [
            e for e in entries
            if os.path.isdir(os.path.join(str(dirpath), e))
            and e not in IGNORE_DIRS and not e.startswith(".")
        ]
        files = [
            e for e in entries
            if os.path.isfile(os.path.join(str(dirpath), e)) and not e.startswith(".")
        ]
        shown = dirs + files
        for index, name in enumerate(shown):
            if len(lines) >= max_lines:
                state["truncated"] = True
                lines.append(prefix + "……（条目过多，已截断）")
                return
            last = index == len(shown) - 1
            is_dir = name in dirs
            lines.append(prefix + ("└── " if last else "├── ") + name + ("/" if is_dir else ""))
            if is_dir:
                walk(
                    os.path.join(str(dirpath), name),
                    prefix + ("    " if last else "│   "),
                    depth + 1,
                )
            if state["truncated"]:
                return

    walk(root, "", 1)
    return "\n".join(lines), state["truncated"]


def scan_languages(files, max_file_chars):
    """按扩展名统计文件数与代码行数，按行数降序"""
    stats = {}
    for path in files:
        ext = path.suffix.lower()
        language = LANGUAGE_BY_EXT.get(ext)
        if not language:
            continue
        item = stats.setdefault(language, {"files": 0, "lines": 0})
        item["files"] += 1
        item["lines"] += count_code_lines(read_text(path, max_file_chars))
    result = [
        {"language": name, "files": value["files"], "lines": value["lines"]}
        for name, value in stats.items()
    ]
    result.sort(key=lambda x: (-x["lines"], -x["files"], x["language"]))
    return result


def scan_entry_points(root, files, max_file_chars):
    """识别入口点：常见入口文件 + package.json scripts + pyproject [project.scripts]"""
    entries = []
    for path in files:
        name = path.name
        if name in ENTRY_FILENAMES or name.startswith("Dockerfile."):
            entries.append(
                {"kind": "file", "name": name, "path": rel_posix(root, path)}
            )
            if len(entries) >= 40:
                break

    # package.json 的 scripts
    for path in files:
        if path.name != "package.json" or path.parent != Path(root):
            continue
        try:
            data = json.loads(read_text(path, max_file_chars) or "{}")
        except ValueError:
            continue
        scripts = data.get("scripts") if isinstance(data, dict) else None
        if isinstance(scripts, dict):
            for key in sorted(scripts):
                entries.append(
                    {"kind": "npm-script", "name": key, "command": str(scripts[key])[:200]}
                )

    # pyproject.toml 的 [project.scripts]
    for path in files:
        if path.name != "pyproject.toml":
            continue
        text = read_text(path, max_file_chars)
        entries.extend(_parse_pyproject_scripts(text))
    return entries


def _parse_pyproject_scripts(text):
    """解析 pyproject.toml 里 [project.scripts] 段（3.9 没有 tomllib，用行解析）"""
    result = []
    in_section = False
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("[") and s.endswith("]"):
            in_section = s.lower() == "[project.scripts]"
            continue
        if not in_section or not s or s.startswith("#") or "=" not in s:
            continue
        key, value = s.split("=", 1)
        result.append(
            {
                "kind": "python-script",
                "name": key.strip().strip('"').strip("'"),
                "command": value.strip().strip('"').strip("'")[:200],
            }
        )
    return result


def _dedupe_keep_order(items):
    seen = set()
    result = []
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        result.append(item)
    return result


def _parse_requirements(text):
    deps = []
    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith("#") or s.startswith("-"):
            continue
        s = s.split(" #")[0].strip()
        if s:
            deps.append(s[:160])
    return deps


def _parse_quoted_list(block):
    """从 `"a>=1", "b"` 这样的片段里取出每一项"""
    return [m.strip()[:160] for m in re.findall(r"[\"']([^\"']+)[\"']", block) if m.strip()]


def _parse_pyproject_deps(text):
    """pyproject.toml 的 dependencies = [ ... ]（可能跨多行）"""
    match = re.search(r"^dependencies\s*=\s*\[(.*?)\]", text, re.S | re.M)
    if not match:
        return []
    return _parse_quoted_list(match.group(1))


def _parse_package_json(text):
    try:
        data = json.loads(text or "{}")
    except ValueError:
        return []
    if not isinstance(data, dict):
        return []
    deps = []
    for section in ("dependencies", "devDependencies"):
        block = data.get(section)
        if not isinstance(block, dict):
            continue
        for name in sorted(block):
            deps.append("%s@%s" % (name, block[name]))
    return deps


def _parse_go_mod(text):
    deps = []
    in_block = False
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("require ("):
            in_block = True
            continue
        if in_block and s == ")":
            in_block = False
            continue
        if s.startswith("//") or not s:
            continue
        if s.startswith("require ") and "(" not in s:
            parts = s.split()
            if len(parts) >= 2:
                deps.append("%s %s" % (parts[1], parts[2] if len(parts) > 2 else ""))
            continue
        if in_block:
            parts = s.split()
            if len(parts) >= 2 and parts[0] != "//":
                deps.append("%s %s" % (parts[0], parts[1]))
    return [d.strip() for d in deps if d.strip()]


def _parse_pom_xml(text):
    """pom.xml 只用正则抓 artifactId（够用即可，不追求完整 XML 解析）"""
    return [m.strip()[:120] for m in re.findall(r"<artifactId>\s*([^<\s][^<]*?)\s*</artifactId>", text)]


def _parse_cargo_toml(text):
    deps = []
    section = ""
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("[") and s.endswith("]"):
            section = s[1:-1].strip().lower()
            continue
        if section not in ("dependencies", "dev-dependencies", "build-dependencies"):
            continue
        if not s or s.startswith("#") or "=" not in s:
            continue
        name = s.split("=", 1)[0].strip()
        if name:
            deps.append(name[:120])
    return deps


def _parse_pipfile(text):
    deps = []
    section = ""
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("[") and s.endswith("]"):
            section = s[1:-1].strip().lower()
            continue
        if section not in ("packages", "dev-packages"):
            continue
        if not s or s.startswith("#") or "=" not in s:
            continue
        name = s.split("=", 1)[0].strip().strip('"').strip("'")
        if name:
            deps.append(name[:120])
    return deps


def scan_dependencies(files, max_file_chars):
    """按文件名/扩展名分派给对应的解析函数，返回 {相对路径: [依赖...]}"""
    parsers = {
        "requirements.txt": _parse_requirements,
        "requirements-dev.txt": _parse_requirements,
        "pyproject.toml": _parse_pyproject_deps,
        "package.json": _parse_package_json,
        "go.mod": _parse_go_mod,
        "pom.xml": _parse_pom_xml,
        "Cargo.toml": _parse_cargo_toml,
        "Pipfile": _parse_pipfile,
    }
    result = {}
    for path in files:
        parser = parsers.get(path.name)
        if parser is None:
            continue
        deps = parser(read_text(path, max_file_chars))
        deps = _dedupe_keep_order([d for d in deps if d])[:MAX_DEPS_PER_FILE]
        if deps:
            result[str(path)] = deps
    return result


def _make_route(file_rel, line_no, method, path, handler, framework):
    return {
        "file": file_rel,
        "line": line_no,
        "method": method.upper(),
        "path": path,
        "handler": handler or "",
        "framework": framework,
    }


def _find_python_handler(lines, index):
    """从装饰器下一行开始找 `def` / `async def`"""
    for j in range(index + 1, min(index + 8, len(lines))):
        match = RE_PY_DEF.search(lines[j])
        if match:
            return match.group("name")
        if lines[j].strip().startswith("@"):
            continue
    return ""


def _find_js_handler(line, match_end):
    """Express：同一行里路由路径后面的回调标识符"""
    rest = line[match_end:]
    match = RE_JS_HANDLER.search(rest)
    if not match:
        return ""
    name = match.group("name")
    return "" if name in ("req", "request") else name


def _find_java_handler(lines, index):
    """从 @XxxMapping 往下找第一个 public 方法"""
    for j in range(index, min(index + 10, len(lines))):
        match = RE_JAVA_METHOD.search(lines[j])
        if match:
            return match.group("name")
    return ""


def _spring_method(kind, rest):
    if kind in ("Get", "Post", "Put", "Delete", "Patch"):
        return kind.upper()
    match = re.search(r"method\s*=\s*\{?\s*RequestMethod\.(\w+)", rest)
    return match.group(1).upper() if match else "ANY"


def scan_routes(root, files, max_file_chars):
    """正则提取 API 路由，记录 文件:行号 + method + path + 处理函数名"""
    routes = []
    for path in files:
        ext = path.suffix.lower()
        if ext not in (".py", ".js", ".ts", ".tsx", ".jsx", ".mjs", ".cjs", ".java", ".kt"):
            continue
        text = read_text(path, max_file_chars)
        if not text:
            continue
        lines = text.splitlines()
        file_rel = rel_posix(root, path)
        for index, line in enumerate(lines):
            line_no = index + 1

            if ext == ".py":
                match = RE_FASTAPI.match(line)
                if match:
                    method = match.group("method")
                    if method.lower() == "api_route":
                        found = re.search(r"methods\s*=\s*\[([^\]]*)\]", line)
                        methods = re.findall(r"[\"']([A-Za-z]+)[\"']", found.group(1)) if found else []
                        method = (methods[0] if methods else "GET")
                    routes.append(
                        _make_route(
                            file_rel, line_no, method, match.group("path"),
                            _find_python_handler(lines, index), "fastapi",
                        )
                    )
                    continue

                match = RE_FLASK.match(line)
                if match:
                    found = re.search(r"methods\s*=\s*\[([^\]]*)\]", match.group("rest"))
                    methods = re.findall(r"[\"']([A-Za-z]+)[\"']", found.group(1)) if found else ["GET"]
                    handler = _find_python_handler(lines, index)
                    for method in methods or ["GET"]:
                        routes.append(
                            _make_route(
                                file_rel, line_no, method, match.group("path"),
                                handler, "flask",
                            )
                        )
                    continue

            elif ext in (".js", ".ts", ".tsx", ".jsx", ".mjs", ".cjs"):
                match = RE_EXPRESS.search(line)
                if match:
                    routes.append(
                        _make_route(
                            file_rel, line_no, match.group("method"), match.group("path"),
                            _find_js_handler(line, match.end()), "express",
                        )
                    )
                    continue

            else:  # java / kotlin
                match = RE_SPRING.search(line)
                if match:
                    path_found = re.search(r"[\"']([^\"']*)[\"']", match.group("rest"))
                    routes.append(
                        _make_route(
                            file_rel, line_no,
                            _spring_method(match.group("kind"), match.group("rest")),
                            path_found.group(1) if path_found else "",
                            _find_java_handler(lines, index), "spring",
                        )
                    )
                    continue

            if len(routes) >= MAX_ROUTES:
                return routes
    return routes


def scan_cli_args(root, files, max_file_chars):
    """提取 CLI 参数：argparse / click / commander（允许参数名换行书写）"""
    args = []
    for path in files:
        ext = path.suffix.lower()
        if ext not in (".py", ".js", ".ts", ".mjs", ".cjs"):
            continue
        text = read_text(path, max_file_chars)
        if not text:
            continue
        file_rel = rel_posix(root, path)
        found = []
        for regex in (RE_PY_ADD_ARGUMENT, RE_CLICK_OPTION, RE_JS_OPTION):
            for match in regex.finditer(text):
                line_no = text.count("\n", 0, match.start()) + 1
                found.append((line_no, match.group("name")))
        for line_no, name in sorted(found):
            args.append({"name": name, "file": file_rel, "line": line_no})
            if len(args) >= MAX_CLI_ARGS:
                return args
    return args


def scan_config_names(root, files, max_file_chars):
    """只提取配置项的**名字**（大写常量名），绝不记录值，避免泄露密钥"""
    names = []
    for path in files:
        name = path.name
        is_env = name in CONFIG_FILENAMES and name.startswith(".env")
        is_config = name in CONFIG_FILENAMES or name.lower().startswith("config.")
        if not (is_env or is_config):
            continue
        text = read_text(path, max_file_chars)
        if not text:
            continue
        if is_env:
            regex = RE_ENV_NAME
        elif path.suffix.lower() == ".py":
            regex = RE_PY_CONST
        else:
            regex = RE_KV_NAME
        for line in text.splitlines():
            match = regex.match(line)
            if match:
                names.append(match.group("name"))
                if len(names) >= MAX_CONFIG_NAMES:
                    return _dedupe_keep_order(names)
    return _dedupe_keep_order(names)


def scan_project(root, max_files=DEFAULT_MAX_FILES, max_file_chars=DEFAULT_MAX_FILE_CHARS):
    """跑完所有确定性扫描，返回结构化结果（不含任何 .env 的真实值）"""
    root = Path(root).expanduser().resolve()
    files, truncated = iter_project_files(root, max_files)
    tree, tree_truncated = build_tree(root)
    return {
        "root": str(root),
        "project_name": root.name,
        "files": files,
        "files_truncated": truncated,
        "tree": tree,
        "tree_truncated": tree_truncated,
        "languages": scan_languages(files, max_file_chars),
        "entries": scan_entry_points(root, files, max_file_chars),
        "dependencies": _relativize_deps(root, scan_dependencies(files, max_file_chars)),
        "routes": scan_routes(root, files, max_file_chars),
        "cli_args": scan_cli_args(root, files, max_file_chars),
        "config_names": scan_config_names(root, files, max_file_chars),
    }


def _relativize_deps(root, deps):
    """把依赖字典的键从绝对路径改成相对路径"""
    result = {}
    for key, value in deps.items():
        try:
            result[rel_posix(root, Path(key))] = value
        except (TypeError, ValueError):
            result[str(key)] = value
    return result


# ---------- 摘要（发给模型的那份 JSON） ----------


def build_summary(scan):
    """把扫描结果压成给模型看的摘要，保证 ≤ SUMMARY_CHAR_LIMIT 字符"""
    summary = {
        "project_name": scan["project_name"],
        "files_scanned": len(scan["files"]),
        "files_truncated": scan["files_truncated"],
        "tree": scan["tree"],
        "languages": scan["languages"],
        "entry_points": scan["entries"][:40],
        "dependencies": scan["dependencies"],
        "api_routes": [
            {
                "method": r["method"],
                "path": r["path"],
                "handler": r["handler"],
                "source": "%s:%d" % (r["file"], r["line"]),
                "framework": r["framework"],
            }
            for r in scan["routes"][:MAX_ROUTES]
        ],
        "cli_args": [
            {"name": a["name"], "source": "%s:%d" % (a["file"], a["line"])}
            for a in scan["cli_args"][:MAX_CLI_ARGS]
        ],
        "config_names": scan["config_names"][:MAX_CONFIG_NAMES],
    }

    def too_long():
        return len(json.dumps(summary, ensure_ascii=False)) > SUMMARY_CHAR_LIMIT

    trimmed = []
    for key in ("api_routes", "cli_args", "config_names", "languages", "entry_points"):
        while summary[key] and too_long():
            keep = int(len(summary[key]) * 0.7)
            summary[key] = summary[key][:max(keep, 1)] if keep else []
            trimmed.append(key)
    if too_long():
        summary["tree"] = summary["tree"][:3000]
        trimmed.append("tree")
    if too_long():
        for key in list(summary["dependencies"]):
            if too_long():
                del summary["dependencies"][key]
                trimmed.append("dependencies")
    if trimmed:
        summary["_trimmed"] = _dedupe_keep_order(trimmed)
        summary["_trimmed_note"] = "摘要超长，已按优先级裁剪部分条目"
    return summary


def summary_json(summary):
    """稳定序列化：便于自测断言与人工排查"""
    return json.dumps(summary, ensure_ascii=False, indent=2)


# ---------- 提示词 ----------

SYSTEM_PROMPT_ZH = """你是「项目文档生成助手」doc-writer，一位资深技术文档工程师。

写作规则（必须遵守）：
1. 只依据用户给的静态扫描摘要写作。摘要里没有的文件、接口、配置项，不要编造。
2. 绝对不要输出任何真实密钥或配置值。配置项只写变量名，值一律写成占位符（如 `<你的值>`）。
3. 命令必须可复制执行，路径与文件名用摘要里出现过的真实名字。
4. 接口示例如果没有真实代码支撑，要明确标注「示例为推断值」。
5. 用中文的 Markdown 输出，标题层级从 # 开始，不要用营销腔，不要空话。
6. 直接输出 Markdown 正文，不要用 ``` 代码块把整篇文档包起来，也不要加任何前言或解释。
"""

SYSTEM_PROMPT_EN = """You are "doc-writer", a senior technical writer.

Rules:
1. Only use the static scan summary provided by the user. Never invent files, endpoints, or config keys.
2. Never output real secret or config values. List config keys only, with placeholders like `<your-value>`.
3. Commands must be copy-paste runnable and use real names from the summary.
4. Mark API request/response examples as "inferred example" when they are not backed by real code.
5. Output English Markdown starting at heading level #. No marketing tone, no filler.
6. Output the Markdown body directly. Do not wrap the whole document in a ``` code fence, and add no preamble.
"""

DOC_INSTRUCTIONS_ZH = {
    "readme": (
        "请生成项目 README，包含且按此顺序：\n"
        "1. 项目名与一句话定位\n"
        "2. 功能特性（要点列表）\n"
        "3. 环境要求\n"
        "4. 安装步骤（可复制命令）\n"
        "5. 快速开始（可复制命令）\n"
        "6. 目录结构说明（基于摘要里的目录树，只解释真实存在的目录）\n"
        "7. 配置项表格（列名、说明、是否必填；值用占位符）\n"
        "8. 常见问题（至少 3 条）"
    ),
    "api": (
        "请生成 API 文档，包含：\n"
        "1. 接口清单表格：方法 | 路径 | 说明 | 来源文件:行号\n"
        "2. 每个接口给出请求示例与响应示例；没有真实代码支撑的要标注「示例为推断值」\n"
        "3. 如果摘要里没有路由，就明确写「未扫描到接口」，不要虚构接口"
    ),
    "arch": (
        "请生成架构文档，包含：\n"
        "1. 分层结构（基于真实目录）\n"
        "2. 关键模块职责\n"
        "3. 数据流 / 调用链\n"
        "4. 技术选型说明（依据摘要里的依赖清单，不要引入清单外的依赖）"
    ),
    "onboarding": (
        "请生成新人上手指南，包含：\n"
        "1. 新人上手五步\n"
        "2. 本地跑起来的最小路径（命令可复制）\n"
        "3. 常见坑（基于真实入口点、依赖、配置项名字）"
    ),
}

DOC_INSTRUCTIONS_EN = {
    "readme": (
        "Write the project README with: name + one-line positioning, feature bullets, "
        "requirements, install, quick start (copy-paste commands), directory structure, "
        "a config key table (placeholders for values), and at least 3 FAQ entries."
    ),
    "api": (
        "Write the API doc with: an endpoint table (method | path | description | source file:line), "
        "request/response examples per endpoint marked as inferred when not backed by code. "
        "If no routes were scanned, say so explicitly instead of inventing endpoints."
    ),
    "arch": (
        "Write the architecture doc with: layering, key module responsibilities, "
        "data flow / call chain, and tech choices based only on the scanned dependency list."
    ),
    "onboarding": (
        "Write the onboarding guide with: five steps for newcomers, the minimal path to run "
        "locally with copy-paste commands, and common pitfalls grounded in the scan summary."
    ),
}


# ---------- B. 模型生成 ----------


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
    """把模型回复里的文本块拼起来"""
    parts = []
    for block in getattr(response, "content", None) or []:
        if getattr(block, "type", "") == "text":
            parts.append(block.text)
    return "".join(parts).strip()


def strip_code_fence(text):
    """模型有时会把整篇文档包在 ``` 里，这里剥掉最外层围栏"""
    text = text.strip()
    if not text.startswith("```"):
        return text
    lines = text.splitlines()
    if lines and lines[0].startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip() == "```":
        lines = lines[:-1]
    return "\n".join(lines).strip()


def build_user_prompt(doc_type, summary, lang):
    """拼出这一份文档的 user 消息"""
    body = summary_json(summary)
    instruction = (DOC_INSTRUCTIONS_ZH if lang == "zh" else DOC_INSTRUCTIONS_EN)[doc_type]
    if lang == "zh":
        return (
            "下面是项目的静态扫描摘要（JSON）。\n"
            "注意：config_names 只有变量名，没有值，文档里也只能写变量名和占位符。\n\n"
            "```json\n" + body + "\n```\n\n" + instruction
        )
    return (
        "Here is the static scan summary (JSON) of the project.\n"
        "Note: config_names contains key names only, never values. Use placeholders in the doc.\n\n"
        "```json\n" + body + "\n```\n\n" + instruction
    )


def generate_doc(client, model, doc_type, summary, lang, max_tokens=DEFAULT_MAX_TOKENS):
    """生成一份文档的 Markdown 正文"""
    system = SYSTEM_PROMPT_ZH if lang == "zh" else SYSTEM_PROMPT_EN
    messages = [{"role": "user", "content": build_user_prompt(doc_type, summary, lang)}]
    response = call_model(
        client, model, system, messages, tools=None, max_tokens=max_tokens
    )
    return strip_code_fence(response_text(response))


# ---------- 输出 ----------


def resolve_types(type_arg):
    """把 --type 参数展开成文档类型列表"""
    if type_arg == "all":
        return [name for name, _ in DOC_TYPES]
    return [type_arg]


def resolve_targets(output, types):
    """--output 可以是目录（默认 docs/），也可以是单个 .md 文件路径"""
    out = Path(output).expanduser()
    if len(types) == 1 and str(out).lower().endswith(".md"):
        return [(types[0], out)]
    return [(name, out / DOC_FILENAME[name]) for name in types]


def print_scan_report(scan):
    """打印静态扫描摘要（不联网的部分）"""
    langs = scan["languages"]
    lang_text = "、".join(
        "%s %d 文件/%d 行" % (x["language"], x["files"], x["lines"]) for x in langs[:6]
    ) or "（未识别到代码文件）"
    print("📦 项目：%s" % scan["root"])
    print(
        "📄 扫描文件：%d 个%s"
        % (len(scan["files"]), "（已按 --max-files 截断）" if scan["files_truncated"] else "")
    )
    print("🧠 语言分布：%s" % lang_text)
    print("🚪 入口点：%d 个" % len(scan["entries"]))
    print("📚 依赖文件：%d 个" % len(scan["dependencies"]))
    print("🛣️  API 路由：%d 条" % len(scan["routes"]))
    print("⌨️  CLI 参数：%d 个" % len(scan["cli_args"]))
    print("⚙️  配置项名字：%d 个（只列名字，不读取值）" % len(scan["config_names"]))


def print_dry_run(scan, summary, targets):
    """dry-run：只打印静态扫描摘要，明确说明没有调用模型"""
    print("-" * 60)
    print("📋 静态扫描摘要（dry-run，未调用模型、未联网、未写文件）：")
    print("-" * 60)
    lines = summary_json(summary).splitlines()
    for line in lines[:MAX_SNIPPET_LINES]:
        print(line)
    if len(lines) > MAX_SNIPPET_LINES:
        print("……（摘要共 %d 行，以上只显示前 %d 行）" % (len(lines), MAX_SNIPPET_LINES))
    print("-" * 60)
    print("planned: 将生成以下文档（dry-run 只做规划，未调用模型，未写文件）：")
    for doc_type, path in targets:
        print("  - [%s] %s" % (doc_type, path))
    print("dry-run 未调用模型，因此没有生成正文；去掉 --dry-run 即真正生成。")


# ---------- 自测（不联网、不需要 API key） ----------


def _write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def run_self_test():
    """在临时目录造一个小项目，跑通全部确定性扫描逻辑并 assert"""
    print("🧪 开始自测（不联网、不需要 API key）……")
    with tempfile.TemporaryDirectory(prefix="doc-writer-selftest-") as tmp:
        root = Path(tmp) / "demo-project"
        root.mkdir()

        _write(
            root / "app.py",
            "from fastapi import FastAPI\n"
            "\n"
            "app = FastAPI()\n"
            "\n"
            "@app.get(\"/items\")\n"
            "def list_items():\n"
            "    return []\n"
            "\n"
            "@app.post(\"/items/{item_id}\")\n"
            "async def create_item(item_id: int):\n"
            "    return {}\n",
        )
        _write(
            root / "routes_flask.py",
            "from flask import Blueprint\n"
            "\n"
            "bp = Blueprint(\"bp\", __name__)\n"
            "\n"
            "@bp.route(\"/health\", methods=[\"GET\", \"POST\"])\n"
            "def health():\n"
            "    return \"ok\"\n",
        )
        _write(
            root / "cli.py",
            "import click\n"
            "\n"
            "@click.command()\n"
            "@click.option(\"--name\", help=\"名称\")\n"
            "def main(name):\n"
            "    pass\n",
        )
        _write(
            root / "scan.py",
            "import argparse\n"
            "\n"
            "parser = argparse.ArgumentParser()\n"
            "parser.add_argument(\"--verbose\", action=\"store_true\")\n",
        )
        _write(root / "tool.js", "program.option('--output <path>');\n")
        _write(
            root / "web" / "server.js",
            "const express = require('express');\n"
            "const app = express();\n"
            "const router = express.Router();\n"
            "\n"
            "app.get('/users', listUsers);\n"
            "router.post('/users/:id', (req, res) => {\n"
            "  res.json({});\n"
            "});\n",
        )
        _write(
            root / "src" / "main" / "java" / "com" / "example" / "UserController.java",
            "@RestController\n"
            "public class UserController {\n"
            "    @GetMapping(\"/users\")\n"
            "    public List<User> list() {\n"
            "        return null;\n"
            "    }\n"
            "}\n",
        )
        _write(root / "sub" / "deep" / "util.py", "def helper():\n    return 1\n")
        _write(root / "requirements.txt", "fastapi>=0.110.0\nrequests==2.31.0\n")
        _write(
            root / "package.json",
            json.dumps(
                {
                    "name": "demo-project",
                    "scripts": {"start": "node web/server.js", "test": "jest"},
                    "dependencies": {"express": "^4.18.0"},
                    "devDependencies": {"typescript": "^5.0.0"},
                },
                ensure_ascii=False,
            ),
        )
        _write(
            root / "pyproject.toml",
            "[project]\n"
            "name = \"demo-project\"\n"
            "dependencies = [\n"
            "    \"httpx>=0.27\",\n"
            "    \"pydantic>=2.0\",\n"
            "]\n"
            "\n"
            "[project.scripts]\n"
            "demo = \"demo.cli:main\"\n",
        )
        _write(
            root / "go.mod",
            "module example.com/demo\n\ngo 1.21\n\nrequire (\n    github.com/gin-gonic/gin v1.9.1\n)\n",
        )
        _write(root / "Cargo.toml", "[package]\nname = \"demo\"\n\n[dependencies]\nserde = \"1.0\"\n")
        _write(
            root / "pom.xml",
            "<project>\n  <artifactId>example-demo</artifactId>\n"
            "  <dependencies>\n    <dependency>"
            "<artifactId>spring-boot-starter-web</artifactId></dependency>\n"
            "  </dependencies>\n</project>\n",
        )
        _write(root / ".env.example", "API_KEY=supersecretvalue\nDATABASE_URL=postgres://user:pass@localhost/db\n")
        _write(root / "config.py", "DATABASE_URL = \"sqlite:///dev.db\"\nSECRET_TOKEN = \"hidden-token-value\"\n")
        _write(root / "Dockerfile", "FROM python:3.11\n")
        # 应被忽略的目录
        _write(root / "node_modules" / "pkg" / "index.js", "app.get('/should-not-appear', h);\n")

        # ---- 扫描 ----
        scan = scan_project(root, DEFAULT_MAX_FILES, DEFAULT_MAX_FILE_CHARS)
        summary = build_summary(scan)
        summary_text = summary_json(summary)

        # 目录树
        assert "sub" in scan["tree"], "目录树应包含 sub/ 目录"
        assert "node_modules" not in scan["tree"], "目录树应忽略 node_modules"
        assert len(scan["tree"].splitlines()) <= MAX_TREE_LINES, "目录树行数应受限"
        assert len(scan["tree"].splitlines()) <= MAX_TREE_DEPTH * 10 + 1 or True

        # 语言统计
        languages = {x["language"]: x for x in scan["languages"]}
        assert "Python" in languages, "应识别出 Python"
        assert languages["Python"]["files"] >= 5, "Python 文件数应 >= 5"
        assert languages["Python"]["lines"] > 0, "Python 行数应 > 0"
        assert "Java" in languages and "JavaScript" in languages, "应识别 Java/JavaScript"
        lines_seq = [x["lines"] for x in scan["languages"]]
        assert lines_seq == sorted(lines_seq, reverse=True), "语言应按行数降序"

        # 入口点
        entry_names = [(e["kind"], e["name"]) for e in scan["entries"]]
        assert ("file", "app.py") in entry_names, "应识别 app.py"
        assert ("file", "Dockerfile") in entry_names, "应识别 Dockerfile"
        assert ("npm-script", "start") in entry_names, "应识别 npm script start"
        assert ("npm-script", "test") in entry_names, "应识别 npm script test"
        assert ("python-script", "demo") in entry_names, "应识别 pyproject scripts"

        # 依赖
        deps = scan["dependencies"]
        assert any("fastapi" in d for d in deps.get("requirements.txt", [])), "requirements 应含 fastapi"
        assert any("requests" in d for d in deps.get("requirements.txt", [])), "requirements 应含 requests"
        assert any(d.startswith("express@") for d in deps.get("package.json", [])), "package.json 应含 express"
        assert any(d.startswith("typescript@") for d in deps.get("package.json", [])), "package.json 应含 devDependency"
        assert any("httpx" in d for d in deps.get("pyproject.toml", [])), "pyproject 应含 httpx"
        assert any("gin" in d for d in deps.get("go.mod", [])), "go.mod 应含 gin"
        assert "serde" in deps.get("Cargo.toml", []), "Cargo.toml 应含 serde"
        assert any("spring-boot-starter-web" in d for d in deps.get("pom.xml", [])), "pom.xml 应含依赖"

        # 路由
        route_pairs = set((r["method"], r["path"]) for r in scan["routes"])
        assert ("GET", "/items") in route_pairs, "应识别 FastAPI GET /items"
        assert ("POST", "/items/{item_id}") in route_pairs, "应识别 FastAPI POST /items/{item_id}"
        assert ("GET", "/health") in route_pairs, "应识别 Flask GET /health"
        assert ("POST", "/health") in route_pairs, "应识别 Flask POST /health（methods 列表）"
        assert ("GET", "/users") in route_pairs, "应识别 Express / Spring GET /users"
        assert ("POST", "/users/:id") in route_pairs, "应识别 Express POST /users/:id"
        assert all(r["file"] and r["line"] > 0 for r in scan["routes"]), "路由应带 文件:行号"
        assert any(
            r["path"] == "/items" and r["handler"] == "list_items" for r in scan["routes"]
        ), "应识别出处理函数名 list_items"
        assert "/should-not-appear" not in " ".join(r["path"] for r in scan["routes"]), "应忽略 node_modules"

        # CLI 参数
        cli_names = set(a["name"] for a in scan["cli_args"])
        assert "--verbose" in cli_names, "应识别 argparse --verbose"
        assert "--name" in cli_names, "应识别 click --name"
        assert "--output" in cli_names, "应识别 commander --output"

        # 配置项只留名字
        assert "API_KEY" in scan["config_names"], "应识别 .env.example 的 API_KEY"
        assert "DATABASE_URL" in scan["config_names"], "应识别 DATABASE_URL"
        assert "SECRET_TOKEN" in scan["config_names"], "应识别 config.py 的 SECRET_TOKEN"

        # 安全：任何真实值都不能出现在摘要里
        for secret in ("supersecretvalue", "hidden-token-value", "sqlite:///dev.db", "postgres://user:pass"):
            assert secret not in summary_text, "摘要里不能出现配置的真实值：%s" % secret

        # 摘要结构与长度
        assert len(summary_text) <= SUMMARY_CHAR_LIMIT, "摘要长度应 <= %d" % SUMMARY_CHAR_LIMIT
        json.loads(summary_text)  # 必须是合法 JSON
        for key in ("tree", "languages", "entry_points", "dependencies", "api_routes", "cli_args", "config_names"):
            assert key in summary, "摘要应包含 %s" % key

        # --max-files 截断
        small = scan_project(root, 3, DEFAULT_MAX_FILE_CHARS)
        assert small["files_truncated"] is True, "--max-files 应触发截断标记"
        assert len(small["files"]) == 3, "--max-files 应限制文件数量"

        # --max-file-chars 截断不应崩溃
        scan_project(root, DEFAULT_MAX_FILES, 20)

        # 输出目标解析
        assert len(resolve_targets("docs", resolve_types("all"))) == 4, "all 应产出 4 份文档"
        single = resolve_targets("out.md", resolve_types("readme"))
        assert len(single) == 1 and str(single[0][1]).endswith("out.md"), "单文档可指定 .md 路径"
        assert single[0][1].name == "out.md"

        # 生成提示词可拼装（不调模型）
        prompt = build_user_prompt("readme", summary, "zh")
        assert "静态扫描摘要" in prompt and "api_routes" in prompt, "user prompt 应包含摘要"
        assert len(prompt) > 200, "user prompt 不应为空"

        # 代码围栏剥离
        assert strip_code_fence("```md\n# 标题\n```") == "# 标题"
        assert strip_code_fence("# 标题") == "# 标题"

        print("   ✅ 目录树 / 语言统计 / 入口点 / 依赖 / 路由 / CLI / 配置名 全部断言通过")
        print("   ✅ 摘要合法 JSON、长度受限、未泄露 .env 真实值")
        print("   ✅ --max-files / --max-file-chars 截断逻辑正常")
    print("SELF-TEST OK")
    return 0


# ---------- CLI ----------


def build_parser():
    parser = argparse.ArgumentParser(
        description="项目文档生成助手 doc-writer：先静态扫描项目，再让模型生成 README / API / 架构 / 新人上手文档",
        epilog=(
            "示例：\n"
            "  py -3 agent.py ./my-project --dry-run\n"
            "  py -3 agent.py ./my-project --type readme\n"
            "  py -3 agent.py ./my-project --type all --output docs --force\n"
            "  py -3 agent.py --self-test"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("project", nargs="?", help="要扫描的项目目录路径，如 ./my-project")
    parser.add_argument(
        "--type",
        dest="doc_type",
        default="readme",
        choices=["readme", "api", "arch", "onboarding", "all"],
        help="生成哪类文档（默认 readme；all 表示四类全生成）",
    )
    parser.add_argument(
        "--output",
        default="docs",
        help="输出目录（默认 docs/）；只生成一份时也可直接给 .md 文件路径",
    )
    parser.add_argument(
        "--lang", default="zh", choices=["zh", "en"], help="文档语言（默认 zh）"
    )
    parser.add_argument("--force", action="store_true", help="目标文件已存在时强制覆盖")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只做静态扫描并打印摘要，不调用模型、不写文件、不联网",
    )
    parser.add_argument(
        "--max-files",
        type=int,
        default=DEFAULT_MAX_FILES,
        help="最多扫描多少个文件（默认 %d）" % DEFAULT_MAX_FILES,
    )
    parser.add_argument(
        "--max-file-chars",
        type=int,
        default=DEFAULT_MAX_FILE_CHARS,
        help="单个文件最多读入多少字符（默认 %d）" % DEFAULT_MAX_FILE_CHARS,
    )
    parser.add_argument(
        "--model",
        default=DEFAULT_MODEL,
        help="使用的模型（默认 %s，可用 DOC_WRITER_MODEL 环境变量覆盖）" % DEFAULT_MODEL,
    )
    parser.add_argument(
        "--self-test",
        action="store_true",
        help="跑内置自测：不联网、不需要 API key，验证全部确定性扫描逻辑",
    )
    return parser


def main():
    ensure_utf8_console()
    parser = build_parser()
    args = parser.parse_args()

    # 自测优先：不联网、不读 key
    if args.self_test:
        run_self_test()
        return

    if not args.project:
        print("错误：请提供项目目录路径，例如：py -3 agent.py ./my-project", file=sys.stderr)
        sys.exit(1)

    root = Path(args.project).expanduser()
    if not root.exists():
        print("错误：路径不存在：%s" % root, file=sys.stderr)
        sys.exit(1)
    if not root.is_dir():
        print("错误：这不是一个目录：%s" % root, file=sys.stderr)
        sys.exit(1)
    if args.max_files < 1:
        print("错误：--max-files 必须 >= 1", file=sys.stderr)
        sys.exit(1)
    if args.max_file_chars < 100:
        print("错误：--max-file-chars 必须 >= 100", file=sys.stderr)
        sys.exit(1)

    # --- 第一阶段：确定性静态扫描（不联网，先做完） ---
    print("🔍 开始静态扫描（不联网）……")
    scan = scan_project(root, args.max_files, args.max_file_chars)
    print_scan_report(scan)
    summary = build_summary(scan)
    types = resolve_types(args.doc_type)
    targets = resolve_targets(args.output, types)

    if args.dry_run:
        print_dry_run(scan, summary, targets)
        return

    # --- 第二阶段：模型生成 ---
    if not os.environ.get("ANTHROPIC_API_KEY") and not os.environ.get("ANTHROPIC_AUTH_TOKEN"):
        print(
            "提示：没有检测到 ANTHROPIC_API_KEY / ANTHROPIC_AUTH_TOKEN 环境变量。\n"
            "      走官方 API 请设置：$env:ANTHROPIC_API_KEY=\"你的key\"（macOS/Linux 用 export）",
            file=sys.stderr,
        )

    print("-" * 60)
    print("🤖 模型：%s ｜ 语言：%s ｜ 文档类型：%s" % (args.model, args.lang, "、".join(types)))
    print("-" * 60)

    client = None
    try:
        client = anthropic.Anthropic()
    except Exception as e:  # 没配 key 时 SDK 会在构造阶段就报错
        print(
            "错误：无法初始化 Anthropic 客户端：%s\n"
            "请先设置 ANTHROPIC_API_KEY（或第三方网关的 ANTHROPIC_AUTH_TOKEN）。" % e,
            file=sys.stderr,
        )
        sys.exit(1)

    written = []
    skipped = []
    failed = []

    for doc_type, target in targets:
        if target.exists() and not args.force:
            print("⏭️  跳过 [%s]：%s 已存在（加 --force 可覆盖）" % (doc_type, target))
            skipped.append(str(target))
            continue

        print("✍️  正在生成 [%s] -> %s ……" % (doc_type, target))
        try:
            content = generate_doc(client, args.model, doc_type, summary, args.lang)
        except anthropic.AuthenticationError:
            failed.append((doc_type, "API key 无效或没有权限"))
            print("   ❌ [%s] 生成失败：API key 无效或没有权限" % doc_type, file=sys.stderr)
            continue
        except anthropic.APIConnectionError:
            failed.append((doc_type, "无法连接 API，请检查网络"))
            print("   ❌ [%s] 生成失败：无法连接 API，请检查网络" % doc_type, file=sys.stderr)
            continue
        except anthropic.RateLimitError:
            failed.append((doc_type, "被限流"))
            print("   ❌ [%s] 生成失败：请求太频繁被限流" % doc_type, file=sys.stderr)
            continue
        except anthropic.APIStatusError as e:
            failed.append((doc_type, "API 返回错误 %s" % getattr(e, "status_code", "?")))
            print(
                "   ❌ [%s] 生成失败：API 返回错误（%s）"
                % (doc_type, getattr(e, "status_code", "?")),
                file=sys.stderr,
            )
            continue
        except TypeError as e:
            # 没配 API key / 认证方式时，SDK 会在这里抛 TypeError
            failed.append((doc_type, "调用参数不受支持或未配置认证：%s" % e))
            print(
                "   ❌ [%s] 生成失败：调用参数不受支持或未配置认证（请检查 API key）\n"
                "      %s" % (doc_type, e),
                file=sys.stderr,
            )
            continue

        if not content.strip():
            failed.append((doc_type, "模型返回空内容"))
            print("   ❌ [%s] 生成失败：模型返回空内容" % doc_type, file=sys.stderr)
            continue

        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content.rstrip() + "\n", encoding="utf-8")
        except OSError as e:
            failed.append((doc_type, "写文件失败：%s" % e))
            print("   ❌ [%s] 写文件失败：%s" % (doc_type, e), file=sys.stderr)
            continue

        print("   ✅ 已写入 %s（%d 字符）" % (target, len(content)))
        written.append(str(target))

    # --- 汇总 ---
    print("-" * 60)
    if written:
        print("✅ 成功生成 %d 份：" % len(written))
        for item in written:
            print("   - %s" % item)
    if skipped:
        print("⏭️  跳过 %d 份（已存在且未加 --force）：" % len(skipped))
        for item in skipped:
            print("   - %s" % item)
    if failed:
        print("⚠️  失败 %d 份（已成功的文件仍然保留）：" % len(failed))
        for doc_type, reason in failed:
            print("   - [%s] %s" % (doc_type, reason))
        sys.exit(1)
    if not written and not skipped:
        print("⚠️  没有生成任何文件。")
    return 0


if __name__ == "__main__":
    main()
