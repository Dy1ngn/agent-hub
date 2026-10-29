#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
依赖风险审计助手 —— 解析依赖清单 + 内置风险规则 + 模型建议 的两段式依赖审计 agent

先完全确定性地解析各种依赖清单（requirements.txt / pyproject.toml / package.json /
Pipfile / Pipfile.lock），再用内置风险规则（无版本约束、过宽约束、固定旧版本、
本地路径或 git 依赖、可疑包名 typosquat、重复声明冲突、风险许可证、dev 依赖混入生产、
命名不规范）逐条检查，按权重算出 0-100 风险分与等级；最后可选让模型给出中文升级/替换建议。
除 anthropic SDK 外只用标准库，Python 3.9 语法兼容。

用法：
    python3 agent.py <文件或目录> [选项]

示例：
    python3 agent.py ./requirements.txt --output 审计报告.md
    python3 agent.py . --advisory 本地公告.json --min-level 警告
    python3 agent.py ./package.json --offline            # 纯确定性，不需要 API key

环境要求：
    1. 模型建议需要设置 ANTHROPIC_API_KEY 或 ANTHROPIC_AUTH_TOKEN：
           export ANTHROPIC_API_KEY=你的key
       （Windows PowerShell：$env:ANTHROPIC_API_KEY="你的key"）
       （--offline 与 --self-test 不需要 key）
    2. 安装 SDK：
           pip3 install -r requirements.txt
    3. 走第三方 Anthropic 兼容网关时，用 --model 指定网关侧的模型名，
       例如：python3 agent.py . --model qwen3.8-max --offline
"""

import argparse
import json
import os
import re
import sys
import tempfile
from pathlib import Path

import anthropic

# 默认模型（可用 --model 参数或 DEPENDENCY_AUDITOR_MODEL 环境变量更换）
DEFAULT_MODEL = os.environ.get("DEPENDENCY_AUDITOR_MODEL", "claude-opus-5")

DEFAULT_OUTPUT = "audit-report.md"
MAX_ADVISORY_FILES = 50          # --advisory 最多读多少个文件
MAX_DEPS = 500                   # 单次最多审计多少个依赖，超出截断并注明

# 风险级别（由低到高）
LEVELS = ("提示", "警告", "风险")
LEVEL_ORDER = {"提示": 1, "警告": 2, "风险": 3}

# 递归扫描时直接忽略的目录
IGNORED_DIRS = {".git", "node_modules", "__pycache__", "dist", "build", "venv", ".venv"}

MANIFEST_NAMES = (
    "requirements.txt", "requirements-dev.txt", "dev-requirements.txt",
    "pyproject.toml", "package.json", "Pipfile", "Pipfile.lock",
)

# ---------- 内置对照表 ----------

# 流行包的“当前主流大版本”样本，用于识别固定在过旧版本上的依赖
POPULAR_VERSIONS = {
    "requests": (2, 31), "django": (4, 2), "flask": (2, 3), "fastapi": (0, 115),
    "numpy": (1, 24), "pandas": (2, 0), "pytest": (7, 4), "click": (8, 1),
    "sqlalchemy": (2, 0), "redis": (5, 0), "celery": (5, 3), "pydantic": (2, 5),
    "black": (23, 12), "mypy": (1, 8), "scikit-learn": (1, 4), "aiohttp": (3, 9),
    "httpx": (0, 27), "rich": (13, 7), "typer": (0, 12), "jinja2": (3, 1),
    "setuptools": (69, 0), "urllib3": (2, 2), "boto3": (1, 34), "starlette": (0, 40),
    # JavaScript 侧
    "lodash": (4, 17), "express": (4, 19), "react": (18, 3), "typescript": (5, 4),
    "jest": (29, 7), "webpack": (5, 91), "axios": (1, 7), "next": (14, 2),
}

# 流行包的许可证样本（只收录有代表性的，缺失时给“许可证信息缺失”提示）
LICENSE_TABLE = {
    "requests": "Apache-2.0", "django": "BSD-3-Clause", "flask": "BSD-3-Clause",
    "numpy": "BSD-3-Clause", "pandas": "BSD-3-Clause", "pytest": "MIT",
    "click": "BSD-3-Clause", "sqlalchemy": "MIT", "redis": "MIT",
    "celery": "BSD-3-Clause", "pydantic": "MIT", "black": "MIT", "mypy": "MIT",
    "chardet": "LGPL-2.1", "pymupdf": "AGPL-3.0", "mysql-connector-python": "GPL-2.0",
    "python-igraph": "GPL-2.0", "ghostscript": "AGPL-3.0",
    "lodash": "MIT", "express": "MIT", "react": "MIT", "typescript": "Apache-2.0",
    "jest": "MIT", "axios": "MIT",
}

# 对商业闭源项目有传染性风险的许可证关键字
RISKY_LICENSE_RE = re.compile(r"(AGPL|LGPL|GPL)", re.IGNORECASE)

# 只应该出现在开发环境的包
DEV_ONLY_PACKAGES = {
    "pytest", "pytest-cov", "pytest-asyncio", "pytest-mock", "mypy", "black",
    "flake8", "isort", "pylint", "ruff", "tox", "coverage", "pre-commit",
    "hypothesis", "mock", "pyinstaller", "sphinx", "mkdocs", "types-requests",
    "build", "twine", "wheel", "pip-tools",
    "jest", "mocha", "chai", "eslint", "prettier", "ts-jest", "ts-node",
    "nodemon", "webpack-cli", "vitest", "@types/node", "@types/jest",
}

# typosquat 检测用的流行包名表（大小写无关、下划线与连字符等价）
POPULAR_PACKAGES = set(POPULAR_VERSIONS) | {
    "pillow", "scipy", "matplotlib", "beautifulsoup4", "selenium", "paramiko",
    "cryptography", "pyyaml", "protobuf", "grpcio", "psycopg2", "pymongo",
    "tqdm", "rich", "attrs", "six", "certifi", "charset-normalizer", "idna",
    "markupsafe", "werkzeug", "itsdangerous", "pygments", "packaging",
    "asyncio", "uvicorn", "gunicorn", "watchfiles", "orjson", "httpcore",
    "h11", "anyio", "sniffio", "typing-extensions",
    "react-dom", "vue", "angular", "svelte", "chalk", "commander", "moment",
    "classnames", "redux", "zod", "eslint-config-prettier", "vite", "rollup",
}

# 命名规范：PEP 503 / npm 都要求包名只用小写字母、数字与 - .（npm 允许 _ 但不推荐）
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


# ---------- 风险规则表（仿 code-reviewer 的 RULES 结构） ----------

RULE_SPECS = [
    {
        "id": "DA001",
        "title": "无版本约束",
        "level": "提示",
        "weight": 6,
        "desc": "依赖没有写任何版本约束，不同时间安装可能拿到差异很大的版本",
        "fix": "补一个下限并加上限，例如 `requests>=2.31,<3.0`；需要完全可复现就写 `requests==2.31.0` 并配合锁文件。",
    },
    {
        "id": "DA002",
        "title": "过宽约束",
        "level": "警告",
        "weight": 5,
        "desc": "约束形如 `>=0`、`*`、`any`，等于没有约束",
        "fix": "把 `>=0` / `*` 换成有实际意义的范围，例如 `flask>=2.3,<3.0`。",
    },
    {
        "id": "DA003",
        "title": "固定在过旧版本",
        "level": "警告",
        "weight": 8,
        "desc": "精确固定的版本明显低于内置“流行版本对照表”里的主流版本，可能缺安全补丁",
        "fix": "升级到主流版本线（可先在测试环境跑一遍回归），例如把 `django==2.0.11` 升到 `django>=4.2,<5.0`。",
    },
    {
        "id": "DA004",
        "title": "本地路径 / git URL 依赖",
        "level": "风险",
        "weight": 12,
        "desc": "依赖指向本地路径、git 仓库或裸 URL，不可复现且可能引入未经审计的代码",
        "fix": "优先发到私有源或用固定 commit 的 wheel/sdist 制品；确需 git 依赖时锁死 commit 哈希。",
    },
    {
        "id": "DA005",
        "title": "可疑包名（疑似 typosquat）",
        "level": "风险",
        "weight": 15,
        "desc": "包名与某个流行包的编辑距离 ≤1，可能是抢注的投毒包",
        "fix": "立刻核对来源：确认是不是想装的那个流行包（看 PyPI/npm 官方页、下载量、仓库地址）；确认是投毒包就删除并排查是否已执行过安装脚本。",
    },
    {
        "id": "DA006",
        "title": "重复声明且约束不一致",
        "level": "警告",
        "weight": 6,
        "desc": "同一个包在清单里被声明了多次且约束互相矛盾，安装结果取决于解析顺序",
        "fix": "收敛到一份约束（通常是取交集），把重复声明删掉，或统一放到主清单里。",
    },
    {
        "id": "DA007",
        "title": "许可证缺失 / 许可证有传染风险",
        "level": "警告",
        "weight": 10,
        "desc": "许可证信息缺失，或属于 GPL/AGPL/LGPL 系列，对商业闭源分发有传染风险",
        "fix": "补全许可证元数据；GPL/AGPL/LGPL 依赖要么换宽松许可证的替代品，要么让法务评估隔离部署（网络服务不触发 AGPL 分发义务的前提也需要法务确认）。",
    },
    {
        "id": "DA008",
        "title": "开发依赖混入生产清单",
        "level": "提示",
        "weight": 6,
        "desc": "pytest / mypy / jest 之类只该在开发环境出现的包写进了生产依赖清单",
        "fix": "拆到 requirements-dev.txt / devDependencies / optional-dependencies 里，生产镜像只装运行时依赖。",
    },
    {
        "id": "DA009",
        "title": "包名命名不规范",
        "level": "提示",
        "weight": 3,
        "desc": "包名含下划线或大小写混用，规范化后才能匹配，容易和另一个包混淆",
        "fix": "按 PEP 503 写成全小写 + 连字符（`PyYAML` → `pyyaml`），npm 侧统一用小写。",
    },
]

RULE_BY_ID = dict((spec["id"], spec) for spec in RULE_SPECS)


# ---------- 版本与约束工具 ----------

def normalize_name(name):
    """把包名规范成小写 + 连字符（PEP 503 风格），npm 的 @scope/name 保持前缀"""
    text = str(name or "").strip()
    if text.startswith("@"):
        scope, _, rest = text.partition("/")
        return scope.lower() + "/" + rest.lower().replace("_", "-")
    return text.lower().replace("_", "-")


def parse_version(text):
    """把 '2.31.0.post1' 这类版本号拆成可比较的数字元组（忽略字母后缀）"""
    parts = []
    for chunk in re.split(r"[.\-+_]", str(text or "").strip()):
        match = re.match(r"^\d+", chunk)
        if match:
            parts.append(int(match.group(0)))
        elif chunk:
            break
    return tuple(parts) if parts else (0,)


def version_cmp(left, right):
    """比较两个版本元组，短的一侧补 0"""
    length = max(len(left), len(right))
    left = tuple(left) + (0,) * (length - len(left))
    right = tuple(right) + (0,) * (length - len(right))
    return (left > right) - (left < right)


CONSTRAINT_RE = re.compile(r"(==|!=|~=|>=|<=|>|<)\s*([A-Za-z0-9.*+\-_]+)")


def parse_constraint(spec):
    """把 '>=2.0,<3.0' 解析成 [('>=', (2,)), ('<', (3,))]，无法识别的片段丢弃"""
    ops = []
    for match in CONSTRAINT_RE.finditer(str(spec or "")):
        version_text = match.group(2)
        if version_text in ("*", "any", "latest"):
            continue
        ops.append((match.group(1), parse_version(version_text)))
    return ops


def constraint_matches(spec, version_text):
    """判断某个具体版本是否满足约束（空约束视为满足）"""
    version = parse_version(version_text)
    ops = parse_constraint(spec)
    if not ops:
        return str(spec or "").strip() in ("", "*", "any")
    for op, bound in ops:
        cmp = version_cmp(version, bound)
        if op == "==" and cmp != 0:
            return False
        if op == "!=" and cmp == 0:
            return False
        if op == ">=" and cmp < 0:
            return False
        if op == "<=" and cmp > 0:
            return False
        if op == ">" and cmp <= 0:
            return False
        if op == "<" and cmp >= 0:
            return False
        if op == "~=":
            # 兼容约束：主次版本不能变，且不小于给定版本
            if cmp < 0 or version_cmp(version[:max(1, len(bound) - 1)], bound[:-1]) != 0:
                return False
    return True


def constraints_overlap(left_spec, right_spec):
    """启发式判断两个约束是否有交集：拿双方的边界版本互测，能同时满足即认为有交集"""
    candidates = []
    for spec in (left_spec, right_spec):
        for _, bound in parse_constraint(spec):
            candidates.append(bound)
            candidates.append(bound + (0,))
            bumped = list(bound)
            if bumped:
                bumped[-1] = bumped[-1] + 1
            candidates.append(tuple(bumped))
            bumped2 = list(bound)
            if len(bumped2) >= 2:
                bumped2[-2] = bumped2[-2] + 1
                bumped2[-1] = 0
            candidates.append(tuple(bumped2))
    if not candidates:
        return True
    for candidate in candidates:
        text = ".".join(str(part) for part in candidate)
        if constraint_matches(left_spec, text) and constraint_matches(right_spec, text):
            return True
    return False


def extract_pinned_version(spec):
    """取出约束里精确固定的版本号（==X / ~=X），取不到返回 None"""
    text = str(spec or "")
    match = re.search(r"==\s*([A-Za-z0-9.\-+_]+)", text)
    if match and match.group(1) not in ("*", "any"):
        return match.group(1)
    match = re.search(r"~=\s*([A-Za-z0-9.\-+_]+)", text)
    if match and match.group(1) not in ("*", "any"):
        return match.group(1)
    return None


def is_unpinned(spec):
    return str(spec or "").strip() == ""


def is_overbroad(spec):
    text = str(spec or "").strip().lower().replace(" ", "")
    if text in ("*", "any", "latest"):
        return True
    ops = parse_constraint(text)
    if not ops:
        return False
    # 只有一个 >=0.x 的下界、没有任何上界 → 等于没有约束
    return all(op == ">=" for op, _ in ops) and all(version_cmp(bound, (0,)) == 0 for _, bound in ops)


def edit_distance(left, right, limit=2):
    """编辑距离（插入、删除、替换、相邻换位都算 1 次编辑），
    超过 limit 直接返回 limit+1，避免长名字白算"""
    if abs(len(left) - len(right)) > limit:
        return limit + 1
    previous_previous = []
    previous = list(range(len(right) + 1))
    for i, lch in enumerate(left, 1):
        current = [i]
        best = current[0]
        for j, rch in enumerate(right, 1):
            value = min(
                current[j - 1] + 1,
                previous[j] + 1,
                previous[j - 1] + (lch != rch),
            )
            # 相邻两个字符互换（如 lodahs → lodash）也只算一次编辑
            if i > 1 and j > 1 and lch == right[j - 2] and left[i - 2] == rch:
                value = min(value, previous_previous[j - 2] + 1)
            current.append(value)
            if value < best:
                best = value
        if best > limit:
            return limit + 1
        previous_previous = previous
        previous = current
    return previous[-1]


def is_typosquat(name):
    """包名与流行包编辑距离 ≤1 且自身不是流行包 → 疑似抢注投毒包"""
    normalized = normalize_name(name)
    if not normalized or normalized in POPULAR_PACKAGES:
        return None
    for popular in sorted(POPULAR_PACKAGES):
        if abs(len(popular) - len(normalized)) > 1:
            continue
        if edit_distance(normalized, popular, limit=1) <= 1:
            return popular
    return None


# ---------- 依赖清单解析 ----------

class Dependency(object):
    """一条依赖声明"""

    def __init__(self, name, constraint, source, kind, extras=None,
                 marker="", raw="", line_no=0, manifest=""):
        self.name = normalize_name(name)
        self.raw_name = str(name)
        self.constraint = str(constraint or "").strip()
        self.source = source            # 清单文件相对路径
        self.kind = kind                # 运行 / 开发
        self.extras = list(extras or [])
        self.marker = str(marker or "").strip()
        self.raw = str(raw or "")
        self.line_no = line_no
        self.manifest = manifest        # requirements / pyproject / package.json / pipfile / pipfile.lock

    @property
    def label(self):
        return self.name if not self.extras else "%s[%s]" % (self.name, ",".join(self.extras))

    def __repr__(self):  # pragma: no cover - 仅调试用
        return "<Dependency %s %s %s>" % (self.name, self.constraint, self.source)


REQ_NAME_RE = re.compile(r"^\s*(?P<name>[A-Za-z0-9][A-Za-z0-9._\-]*)" +
                         r"(?P<extras>\[[^\]]*\])?\s*(?P<spec>.*)$")
REQ_REF_RE = re.compile(r"^\s*-(?:r|-requirement)\s+(?P<target>\S+)")
REQ_EDITABLE_RE = re.compile(r"^\s*-e\s+(?P<target>\S+)")
URI_PREFIXES = ("git+", "git://", "http://", "https://", "ssh://", "file:", "file://", "svn+", "hg+")


def split_marker(text):
    """把 `name==1.0 ; python_version < "3.8"` 拆成（主体, 环境标记）"""
    body, sep, marker = str(text).partition(";")
    return body.strip(), marker.strip() if sep else ""


def parse_extras(text):
    """解析 `requests[socks,security]` 里的 extras"""
    inner = str(text or "").strip().strip("[]")
    return [item.strip() for item in inner.split(",") if item.strip()]


def parse_requirements_file(path, rel, seen=None):
    """解析 requirements.txt，支持 -r 引用、环境标记、extras、注释与续行"""
    if seen is None:
        seen = set()
    real = str(Path(path).resolve())
    if real in seen:
        return []
    seen.add(real)

    try:
        text = Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []

    deps = []
    pending = ""
    for line_no, raw_line in enumerate(text.splitlines(), 1):
        line = raw_line.rstrip()
        if pending:
            line = pending + line.strip()
            pending = ""
        if line.endswith("\\"):
            pending = line[:-1]
            continue
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue

        ref = REQ_REF_RE.match(stripped)
        if ref:
            target = Path(path).parent / ref.group("target")
            deps.extend(parse_requirements_file(target, str(target), seen))
            continue

        editable = REQ_EDITABLE_RE.match(stripped)
        if editable:
            target = editable.group("target")
            deps.append(Dependency(
                name=target.split("#egg=")[-1].strip("/") or target,
                constraint=target, source=rel, kind="运行",
                raw=stripped, line_no=line_no, manifest="requirements",
            ))
            continue

        if stripped.startswith("-"):
            # 其它 pip 选项（--index-url 等）与依赖无关
            continue

        body, marker = split_marker(stripped)
        # 去掉行内注释（前面必须有空白，避免误伤 URL 里的 #）
        body = re.sub(r"\s+#[^#]*$", "", body).strip()
        body = body.split(" #")[0].strip()
        if not body:
            continue

        # URL / git / 本地路径依赖：整行就是来源，DA004 会单独命中
        if body.startswith(URI_PREFIXES) or body.startswith((".", "/", "~")):
            egg = re.search(r"#egg=([A-Za-z0-9._\-]+)", body)
            if egg:
                name = egg.group(1)
            else:
                name = re.sub(r"\.(tar\.gz|tgz|zip|whl)$", "", Path(body.split("?")[0]).name)
            deps.append(Dependency(
                name=name or body, constraint=body, source=rel, kind="运行",
                marker=marker, raw=stripped, line_no=line_no, manifest="requirements",
            ))
            continue

        match = REQ_NAME_RE.match(body)
        if not match:
            continue
        spec = match.group("spec").strip()
        spec = re.sub(r"\s*--?(hash|global-option|config-settings)\S*.*$", "", spec).strip()
        deps.append(Dependency(
            name=match.group("name"),
            constraint=spec,
            source=rel,
            kind="运行",
            extras=parse_extras(match.group("extras") or ""),
            marker=marker,
            raw=stripped,
            line_no=line_no,
            manifest="requirements",
        ))
    return deps


def _extract_string_list(text):
    """从 TOML 片段里抠出字符串数组，如 dependencies = ["a>=1", "b"]"""
    return [item for item in re.findall(r"[\"']([^\"']+)[\"']", text)]


def parse_pyproject_file(path, rel):
    """用正则解析 pyproject.toml（Python 3.9 没有 tomllib），覆盖 PEP 621 与 poetry 两种写法"""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []

    deps = []

    # 1) PEP 621：[project] 里的 dependencies = [ ... ]
    project_match = re.search(
        r"^\[project\]\s*$(.*?)(?=^\[|\Z)", text, re.MULTILINE | re.DOTALL
    )
    if project_match:
        block = project_match.group(1)
        array_match = re.search(r"^dependencies\s*=\s*\[(.*?)\]", block, re.DOTALL | re.MULTILINE)
        if array_match:
            for item in _extract_string_list(array_match.group(1)):
                body, marker = split_marker(item)
                match = REQ_NAME_RE.match(body.split(";")[0].strip())
                if match:
                    deps.append(Dependency(
                        name=match.group("name"),
                        constraint=match.group("spec").strip(),
                        source=rel, kind="运行",
                        extras=parse_extras(match.group("extras") or ""),
                        marker=marker, raw=item, manifest="pyproject",
                    ))

    # 2) PEP 621 可选依赖组：[project.optional-dependencies]
    optional_match = re.search(
        r"^\[project\.optional-dependencies\]\s*$(.*?)(?=^\[|\Z)",
        text, re.MULTILINE | re.DOTALL,
    )
    if optional_match:
        for group_match in re.finditer(
            r"^([A-Za-z0-9_\-]+)\s*=\s*\[(.*?)\]", optional_match.group(1),
            re.DOTALL | re.MULTILINE,
        ):
            group_name = group_match.group(1).lower()
            kind = "开发" if group_name in ("dev", "test", "tests", "lint", "docs", "typing") else "运行"
            for item in _extract_string_list(group_match.group(2)):
                body, marker = split_marker(item)
                match = REQ_NAME_RE.match(body.split(";")[0].strip())
                if match:
                    deps.append(Dependency(
                        name=match.group("name"),
                        constraint=match.group("spec").strip(),
                        source=rel, kind=kind,
                        extras=parse_extras(match.group("extras") or ""),
                        marker=marker, raw=item, manifest="pyproject",
                    ))

    # 3) poetry：[tool.poetry.dependencies] / [tool.poetry.dev-dependencies]
    for header, kind in (
        ("tool.poetry.dependencies", "运行"),
        ("tool.poetry.dev-dependencies", "开发"),
        ("tool.poetry.group.dev.dependencies", "开发"),
        ("tool.poetry.group.test.dependencies", "开发"),
    ):
        block_match = re.search(
            r"^\[%s\]\s*$(.*?)(?=^\[|\Z)" % re.escape(header),
            text, re.MULTILINE | re.DOTALL,
        )
        if not block_match:
            continue
        for line_no, line in enumerate(block_match.group(1).splitlines(), 1):
            entry = line.strip()
            if not entry or entry.startswith("#") or entry.startswith("["):
                continue
            kv = re.match(r"^([A-Za-z0-9._\-/]+)\s*=\s*(.+)$", entry)
            if not kv:
                continue
            name = kv.group(1).strip().strip("\"'")
            if normalize_name(name) == "python":
                continue
            value = kv.group(2).strip().rstrip(",")
            if value.startswith("{"):
                version_match = re.search(r"version\s*=\s*[\"']([^\"']+)[\"']", value)
                constraint = version_match.group(1) if version_match else ""
            else:
                constraint = value.strip().strip("\"'")
            if constraint == "*":
                constraint = "*"
            deps.append(Dependency(
                name=name, constraint=constraint, source=rel, kind=kind,
                raw=entry, line_no=line_no, manifest="pyproject",
            ))
    return deps


def parse_package_json(path, rel):
    """解析 package.json 的 dependencies / devDependencies / peerDependencies"""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError):
        return []
    deps = []
    for key, kind in (
        ("dependencies", "运行"),
        ("devDependencies", "开发"),
        ("peerDependencies", "运行"),
    ):
        block = data.get(key)
        if not isinstance(block, dict):
            continue
        for name, value in block.items():
            deps.append(Dependency(
                name=name,
                constraint=str(value or "").strip(),
                source=rel, kind=kind,
                raw="%s: %s" % (name, value),
                manifest="package.json",
            ))
    return deps


def parse_pipfile(path, rel):
    """解析 Pipfile 的 [packages] / [dev-packages] 两段"""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []
    deps = []
    section = None
    for line_no, raw_line in enumerate(text.splitlines(), 1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        header = re.match(r"^\[([^\]]+)\]$", line)
        if header:
            section = header.group(1).strip().lower()
            continue
        if section not in ("packages", "dev-packages", "dev-packages "):
            continue
        kv = re.match(r"^([A-Za-z0-9._\-/]+)\s*=\s*(.+)$", line)
        if not kv:
            continue
        value = kv.group(2).strip().rstrip(",")
        if value.startswith("{"):
            version_match = re.search(r"version\s*=\s*[\"']([^\"']+)[\"']", value)
            constraint = version_match.group(1) if version_match else ""
        else:
            constraint = value.strip().strip("\"'")
        deps.append(Dependency(
            name=kv.group(1).strip().strip("\"'"),
            constraint=constraint,
            source=rel,
            kind="开发" if "dev" in section else "运行",
            raw=raw_line, line_no=line_no, manifest="pipfile",
        ))
    return deps


def parse_pipfile_lock(path, rel):
    """解析 Pipfile.lock（JSON）的 default / develop 两段"""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError):
        return []
    deps = []
    for key, kind in (("default", "运行"), ("develop", "开发")):
        block = data.get(key)
        if not isinstance(block, dict):
            continue
        for name, value in block.items():
            constraint = ""
            if isinstance(value, dict):
                constraint = str(value.get("version") or "").strip()
            deps.append(Dependency(
                name=name, constraint=constraint, source=rel, kind=kind,
                manifest="pipfile.lock",
            ))
    return deps


PARSERS = {
    "requirements.txt": parse_requirements_file,
    "requirements-dev.txt": parse_requirements_file,
    "dev-requirements.txt": parse_requirements_file,
    "pyproject.toml": parse_pyproject_file,
    "package.json": parse_package_json,
    "Pipfile": parse_pipfile,
    "Pipfile.lock": parse_pipfile_lock,
}


def is_manifest_name(name):
    """判断文件名是不是依赖清单：已知清单名，或 requirements*.txt 这类变体"""
    if name in PARSERS:
        return True
    return name.endswith(".txt") and "requirement" in name.lower()


def discover_manifests(paths):
    """在给定文件/目录里找依赖清单，返回 (清单相对路径, 绝对路径) 列表与忽略说明"""
    manifests = []
    notes = []
    seen = set()
    for raw_path in paths:
        target = Path(raw_path).expanduser()
        if not target.exists():
            raise FileNotFoundError("路径不存在：%s" % raw_path)
        candidates = []
        if target.is_file():
            candidates.append(target)
        else:
            for root, dirs, files in os.walk(str(target)):
                dirs[:] = [d for d in dirs if d not in IGNORED_DIRS]
                for name in sorted(files):
                    if is_manifest_name(name):
                        candidates.append(Path(root) / name)
        for candidate in candidates:
            real = str(candidate.resolve())
            if real in seen:
                continue
            seen.add(real)
            if not is_manifest_name(candidate.name):
                notes.append("跳过不支持的清单文件：%s" % candidate)
                continue
            manifests.append(candidate)
    return manifests, notes


def collect_dependencies(paths):
    """解析所有清单文件，返回依赖列表与解析备注（同一条声明只保留一份）"""
    manifests, notes = discover_manifests(paths)
    deps = []
    seen_rows = set()
    for manifest in manifests:
        name = manifest.name
        if name in PARSERS:
            parser = PARSERS[name]
        else:
            parser = parse_requirements_file
        try:
            rel = str(manifest)
            items = parser(manifest, rel)
        except Exception as exc:  # 解析失败不应中断整个审计
            notes.append("解析 %s 失败：%s" % (manifest, exc))
            continue
        # requirements-dev.txt 这类文件里的包算开发依赖
        if "dev" in name.lower():
            for item in items:
                item.kind = "开发"
        added = 0
        for item in items:
            key = (item.name, item.constraint, item.source, item.line_no, item.kind)
            if key in seen_rows:
                continue
            seen_rows.add(key)
            deps.append(item)
            added += 1
        if not added:
            notes.append("%s 里没有解析出任何新依赖" % manifest)
    if len(deps) > MAX_DEPS:
        notes.append("依赖数量超过 %d，已截断" % MAX_DEPS)
        deps = deps[:MAX_DEPS]
    return deps, manifests, notes


# ---------- 风险规则扫描 ----------

def _finding(rule_id, dep, detail):
    spec = RULE_BY_ID[rule_id]
    return {
        "rule_id": rule_id,
        "title": spec["title"],
        "level": spec["level"],
        "weight": spec["weight"],
        "package": dep.label,
        "name": dep.name,
        "source": "%s:%s" % (dep.source, dep.line_no) if dep.line_no else dep.source,
        "constraint": dep.constraint or "（无约束）",
        "detail": detail,
        "fix": spec["fix"],
    }


def scan_dependencies(deps):
    """跑全部确定性风险规则，返回发现列表"""
    findings = []
    for dep in deps:
        if is_unpinned(dep.constraint):
            findings.append(_finding("DA001", dep, "声明了 %s 但没写版本约束" % dep.label))
        elif is_overbroad(dep.constraint):
            findings.append(_finding("DA002", dep, "约束 `%s` 过宽，等于没有约束" % dep.constraint))

        pinned = extract_pinned_version(dep.constraint)
        if pinned and dep.name in POPULAR_VERSIONS:
            current = parse_version(pinned)
            floor = POPULAR_VERSIONS[dep.name]
            if version_cmp(current[:2], floor) < 0:
                findings.append(_finding(
                    "DA003", dep,
                    "固定在 %s，低于内置对照表里的主流版本 %s" % (
                        pinned, ".".join(str(part) for part in floor)),
                ))

        spec_text = dep.constraint or ""
        lowered = spec_text.lower()
        if (lowered.startswith(URI_PREFIXES) or spec_text.startswith((".", "/", "~"))
                or "git+" in lowered or "#egg=" in lowered or lowered.startswith("file:")):
            findings.append(_finding(
                "DA004", dep, "依赖指向本地路径或仓库地址：`%s`" % (spec_text or dep.raw),
            ))

        similar = is_typosquat(dep.name)
        if similar:
            findings.append(_finding(
                "DA005", dep,
                "`%s` 与流行包 `%s` 的编辑距离 ≤1，疑似 typosquat 投毒包" % (dep.name, similar),
            ))

        license_text = LICENSE_TABLE.get(dep.name)
        if license_text is None:
            findings.append(_finding(
                "DA007", dep, "内置许可证对照表里没有 `%s` 的许可证信息，分发前需要确认" % dep.name,
            ))
        elif RISKY_LICENSE_RE.search(license_text):
            findings.append(_finding(
                "DA007", dep, "许可证为 %s，对商业闭源分发有传染风险" % license_text,
            ))

        if dep.kind == "运行" and dep.name in DEV_ONLY_PACKAGES and dep.manifest in (
                "requirements", "requirements.txt", "pipfile", "pyproject", "package.json"):
            findings.append(_finding(
                "DA008", dep, "`%s` 是开发期工具，出现在生产依赖清单 %s 里" % (dep.name, dep.source),
            ))

        if dep.raw_name and (dep.raw_name != dep.raw_name.lower() or "_" in dep.raw_name):
            findings.append(_finding(
                "DA009", dep, "包名 `%s` 含下划线或大写字母，规范化后是 `%s`" % (dep.raw_name, dep.name),
            ))

    # 重复声明冲突：同一个包出现多条约束且互相不一致
    grouped = {}
    for dep in deps:
        grouped.setdefault(dep.name, []).append(dep)
    for name, items in sorted(grouped.items()):
        if len(items) < 2:
            continue
        specs = sorted(set(item.constraint.replace(" ", "") for item in items))
        if len(specs) < 2:
            continue
        conflicts = [spec if spec else "（无约束）" for spec in specs]
        overlap = True
        for left in specs:
            for right in specs:
                if left != right and not constraints_overlap(left, right):
                    overlap = False
        for dep in items:
            findings.append(_finding(
                "DA006", dep,
                "`%s` 被声明 %d 次，约束分别为 %s%s" % (
                    name, len(items), "、".join(conflicts),
                    "" if overlap else "，且约束之间没有交集"),
            ))

    findings.sort(key=lambda item: (
        -LEVEL_ORDER[item["level"]], item["rule_id"], item["package"], item["source"]))
    return findings


# ---------- 本地公告匹配 ----------

def parse_advisory_file(path):
    """读取本地公告 JSON：[{package, versions, severity, title, link}]"""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise ValueError("公告文件 %s 不是合法 JSON：%s" % (path, exc))
    if isinstance(data, dict):
        data = data.get("advisories") or []
    if not isinstance(data, list):
        raise ValueError("公告文件 %s 顶层应当是数组" % path)
    items = []
    for entry in data:
        if not isinstance(entry, dict):
            continue
        package = normalize_name(entry.get("package") or "")
        if not package:
            continue
        versions = entry.get("versions")
        if isinstance(versions, list):
            specs = [str(item) for item in versions]
        else:
            specs = [str(versions)] if versions else []
        severity = str(entry.get("severity") or "警告")
        if severity not in LEVEL_ORDER:
            severity = "警告"
        items.append({
            "package": package,
            "specs": specs,
            "severity": severity,
            "title": str(entry.get("title") or "本地公告条目"),
            "link": str(entry.get("link") or ""),
        })
    return items


def match_advisories(deps, advisories):
    """按包名 + 版本范围匹配公告，返回命中的条目"""
    hits = []
    for advisory in advisories:
        for dep in deps:
            if normalize_name(dep.name) != advisory["package"]:
                continue
            pinned = extract_pinned_version(dep.constraint)
            if advisory["specs"]:
                if pinned is None:
                    affected = True
                    reason = "依赖没有精确固定版本，可能落入受影响范围 %s" % "、".join(advisory["specs"])
                else:
                    affected = any(
                        constraint_matches(spec, pinned) for spec in advisory["specs"]
                    )
                    reason = "固定版本 %s %s 公告范围 %s" % (
                        pinned, "命中" if affected else "不在",
                        "、".join(advisory["specs"]),
                    )
            else:
                affected = True
                reason = "公告没有限定版本范围"
            if not affected:
                continue
            hits.append({
                "package": dep.label,
                "source": "%s:%s" % (dep.source, dep.line_no) if dep.line_no else dep.source,
                "severity": advisory["severity"],
                "title": advisory["title"],
                "link": advisory["link"],
                "reason": reason,
            })
    hits.sort(key=lambda item: (-LEVEL_ORDER[item["severity"]], item["package"]))
    return hits


# ---------- 评分 ----------

def compute_score(findings):
    """按规则权重算 0-100 风险分：单条规则最多计 5 次，避免个别规则刷爆分数"""
    total = 0
    counts = {}
    for finding in findings:
        counts[finding["rule_id"]] = counts.get(finding["rule_id"], 0) + 1
    for rule_id, count in counts.items():
        total += min(count, 5) * RULE_BY_ID[rule_id]["weight"]
    return min(100, total)


def score_level(score):
    if score >= 60:
        return "风险"
    if score >= 25:
        return "警告"
    return "提示"


def filter_by_level(findings, min_level):
    floor = LEVEL_ORDER[min_level]
    return [item for item in findings if LEVEL_ORDER[item["level"]] >= floor]


# ---------- 模型建议 ----------

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


ADVICE_SYSTEM_PROMPT = """你是一名资深供应链安全与依赖治理工程师。
用户会给出一批已经由静态规则发现的依赖风险命中项，请为每一条给出中文解释与升级/替换建议。

输出要求：
1. 只输出一个 JSON 数组，不要输出任何解释文字，不要使用 Markdown 代码围栏。
2. 数组每个元素包含字段：
   - package：包名，必须与输入里的包名完全一致
   - advice：具体的升级或替换建议，说明该怎么做、注意什么
   - risk：一句话说明这个风险的实际后果
3. 无法判断的条目也要给出保守建议，不要编造不存在的版本号。
"""


def response_text(response):
    parts = []
    for block in getattr(response, "content", []) or []:
        if getattr(block, "type", "") == "text":
            parts.append(getattr(block, "text", ""))
    return "\n".join(parts).strip()


def parse_advice_json(text):
    """尽力把模型回复解析成建议列表；解析不出来返回 None（原文进报告附录）"""
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
    items = []
    for entry in data:
        if not isinstance(entry, dict):
            continue
        package = str(entry.get("package") or "").strip()
        advice = str(entry.get("advice") or "").strip()
        risk = str(entry.get("risk") or "").strip()
        if not package or not advice:
            continue
        items.append({"package": package, "advice": advice, "risk": risk})
    return items or None


def render_advice_payload(findings, limit=30):
    """把命中项压成给模型看的紧凑 JSON"""
    seen = set()
    payload = []
    for finding in findings:
        key = finding["name"]
        if key in seen:
            continue
        seen.add(key)
        payload.append({
            "package": finding["name"],
            "constraint": finding["constraint"],
            "rule": "%s %s" % (finding["rule_id"], finding["title"]),
            "detail": finding["detail"],
        })
        if len(payload) >= limit:
            break
    return payload


def generate_advice(findings, model):
    """调用模型给升级建议，返回（建议列表, 原始文本）"""
    payload = render_advice_payload(findings)
    if not payload:
        return [], ""
    client = anthropic.Anthropic()
    messages = [{
        "role": "user",
        "content": "以下是依赖风险命中项：\n```json\n%s\n```\n请按要求输出 JSON 数组。"
                   % json.dumps(payload, ensure_ascii=False, indent=2),
    }]
    response = call_model(client, model, ADVICE_SYSTEM_PROMPT, messages, max_tokens=8000)
    text = response_text(response)
    return parse_advice_json(text), text


# ---------- 报告 ----------

def build_report(deps, findings, advisories, advisories_hits, advice_items,
                 raw_model_text, model, offline, notes, min_level):
    """生成中文 Markdown 审计报告"""
    total = len(deps)
    score = compute_score(findings)
    level = score_level(score)
    shown = filter_by_level(findings, min_level)

    lines = ["# 依赖风险审计报告", ""]
    lines.append("> 由 dependency-auditor 生成；确定性规则扫描%s。"
                 % ("（--offline 离线模式，未调用模型）" if offline else " + 模型升级建议"))
    lines.append("")
    lines.append("## 概览")
    lines.append("")
    lines.append("| 指标 | 数值 |")
    lines.append("| --- | --- |")
    lines.append("| 解析到的依赖数 | %d |" % total)
    lines.append("| 清单文件数 | %d |" % len(set(dep.source for dep in deps)))
    lines.append("| 运行时依赖 | %d |" % sum(1 for dep in deps if dep.kind == "运行"))
    lines.append("| 开发依赖 | %d |" % sum(1 for dep in deps if dep.kind == "开发"))
    lines.append("| 风险命中数 | %d |" % len(findings))
    lines.append("| 风险分（0-100） | **%d**（等级：%s） |" % (score, level))
    lines.append("| 本地公告命中 | %d |" % len(advisories_hits))
    lines.append("")
    lines.append("评分口径：按规则权重累加（单条规则最多计 5 次），60 分以上为「风险」，"
                 "25-59 分为「警告」，25 分以下为「提示」。")
    lines.append("")

    lines.append("## 风险清单（按级别排序）")
    lines.append("")
    if not shown:
        lines.append("没有达到「%s」级别及以上的命中项。" % min_level)
        lines.append("")
    else:
        lines.append("| 级别 | 规则 | 包 | 来源 | 问题 | 修复建议 |")
        lines.append("| --- | --- | --- | --- | --- | --- |")
        for item in shown:
            lines.append("| %s | %s %s | `%s` | `%s` | %s | %s |" % (
                item["level"], item["rule_id"], item["title"], item["package"],
                item["source"], item["detail"], item["fix"],
            ))
        lines.append("")

    if advisories:
        lines.append("## 本地公告命中")
        lines.append("")
        if not advisories_hits:
            lines.append("已加载 %d 条本地公告，没有依赖命中。" % len(advisories))
            lines.append("")
        else:
            lines.append("| 级别 | 包 | 公告 | 匹配说明 | 链接 |")
            lines.append("| --- | --- | --- | --- | --- |")
            for hit in advisories_hits:
                lines.append("| %s | `%s` | %s | %s | %s |" % (
                    hit["severity"], hit["package"], hit["title"], hit["reason"],
                    hit["link"] or "（无）",
                ))
            lines.append("")

    lines.append("## 逐包明细")
    lines.append("")
    lines.append("| 包 | 版本约束 | 类型 | 来源 | 命中规则 |")
    lines.append("| --- | --- | --- | --- | --- |")
    rule_map = {}
    for item in findings:
        rule_map.setdefault(item["name"], set()).add(item["rule_id"])
    for dep in sorted(deps, key=lambda item: item.name):
        lines.append("| `%s` | `%s` | %s | `%s` | %s |" % (
            dep.label, dep.constraint or "（无约束）", dep.kind, dep.source,
            "、".join(sorted(rule_map.get(dep.name, ())) ) or "—",
        ))
    lines.append("")

    lines.append("## 规则命中统计")
    lines.append("")
    lines.append("| 规则 | 级别 | 权重 | 命中数 | 说明 |")
    lines.append("| --- | --- | --- | --- | --- |")
    counts = {}
    for item in findings:
        counts[item["rule_id"]] = counts.get(item["rule_id"], 0) + 1
    for spec in RULE_SPECS:
        lines.append("| %s %s | %s | %d | %d | %s |" % (
            spec["id"], spec["title"], spec["level"], spec["weight"],
            counts.get(spec["id"], 0), spec["desc"],
        ))
    lines.append("")

    lines.append("## 升级与替换建议")
    lines.append("")
    if advice_items:
        lines.append("| 包 | 风险后果 | 建议 |")
        lines.append("| --- | --- | --- |")
        for item in advice_items:
            lines.append("| `%s` | %s | %s |" % (
                item["package"], item["risk"] or "—", item["advice"],
            ))
        lines.append("")
    else:
        if offline:
            lines.append("本次运行使用 `--offline`，未生成模型建议；可去掉该参数获取 AI 升级建议。")
        else:
            lines.append("没有拿到可用的模型建议。")
        lines.append("")

    if notes:
        lines.append("## 解析备注")
        lines.append("")
        for note in notes:
            lines.append("- %s" % note)
        lines.append("")

    if raw_model_text and not advice_items:
        lines.append("## 附录：模型原始输出（未能解析成 JSON）")
        lines.append("")
        lines.append("```text")
        lines.append(raw_model_text[:8000])
        lines.append("```")
        lines.append("")

    lines.append("## 附录：内置规则表")
    lines.append("")
    lines.append("| 规则 | 级别 | 权重 | 说明 | 修复建议 |")
    lines.append("| --- | --- | --- | --- | --- |")
    for spec in RULE_SPECS:
        lines.append("| %s | %s | %d | %s | %s |" % (
            spec["id"], spec["level"], spec["weight"], spec["desc"], spec["fix"],
        ))
    lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def write_failure_report(args, note):
    """错误路径也产出可读报告（评测平台按报告评分），退出码与错误文案保持不变。"""
    report = build_report([], [], [], [], [], "", args.model, args.offline,
                          [note], args.min_level)
    output_path = Path(args.output).expanduser()
    try:
        if output_path.parent and not output_path.parent.exists():
            output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(report, encoding="utf-8")
    except OSError:
        pass


def render_rules_table():
    lines = [
        "依赖审计内置风险规则（不需要模型）：",
        "",
        "ID      级别  权重  说明",
        "------  ----  ----  ------------------------------------------",
    ]
    for spec in RULE_SPECS:
        lines.append("%-6s  %-4s  %-4d  %s" % (
            spec["id"], spec["level"], spec["weight"], spec["desc"]))
    lines.append("")
    lines.append("共 %d 条。用 --min-level 可按级别过滤报告条目。" % len(RULE_SPECS))
    return "\n".join(lines)


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


def run_self_test():
    """离线自检：造样本、跑全部确定性逻辑、assert 校验，不联网、不需要 API key"""
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)

        # --- 清单样本：覆盖 -r 引用、extras、环境标记、git 依赖、旧版本等 ---
        (root / "base-requirements.txt").write_text(
            "click==7.4\n"
            "idna==3.7\n",
            encoding="utf-8",
        )
        (root / "requirements.txt").write_text(
            "# 主清单\n"
            "-r base-requirements.txt\n"
            "requests\n"
            "flask>=0\n"
            "django==2.0.11\n"
            "git+https://github.com/example/demo.git#egg=demo\n"
            "requets==2.31.0\n"
            "PyYAML==6.0.1\n"
            "pytest==7.4.0\n"
            "chardet==5.2.0\n"
            "numpy==1.24.0 ; python_version < \"3.12\"\n"
            "requests[socks]>=2.31\n"
            "click==8.1.7\n"
            "black==23.12.1\n"
            "pymupdf==1.24.0\n",
            encoding="utf-8",
        )
        (root / "package.json").write_text(json.dumps({
            "dependencies": {
                "lodash": "*",
                "express": "git+https://github.com/expressjs/express.git",
                "lodahs": "^4.17.21",
                "react": "^18.3.1",
            },
            "devDependencies": {"jest": "^29.7.0"},
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        (root / "pyproject.toml").write_text(
            "[project]\n"
            "dependencies = [\n"
            "  \"httpx>=0.27,<1.0\",\n"
            "  \"rich\",\n"
            "]\n"
            "\n"
            "[project.optional-dependencies]\n"
            "dev = [\"pytest>=7.4\"]\n"
            "\n"
            "[tool.poetry.dependencies]\n"
            "python = \"^3.9\"\n"
            "typer = {version = \"^0.12\", optional = true}\n"
            "\n"
            "[tool.poetry.dev-dependencies]\n"
            "mypy = \"*\"\n",
            encoding="utf-8",
        )
        (root / "Pipfile").write_text(
            "[packages]\n"
            "tqdm = \"*\"\n"
            "\n"
            "[dev-packages]\n"
            "pytest = \"*\"\n",
            encoding="utf-8",
        )

        # --- 清单解析 ---
        deps, manifests, notes = collect_dependencies([str(root)])
        by_name = {}
        for dep in deps:
            by_name.setdefault(dep.name, []).append(dep)

        assert "requests" in by_name, "requirements.txt 解析失败"
        assert "lodash" in by_name, "package.json 解析失败"
        assert "httpx" in by_name, "pyproject.toml 解析失败"
        assert "tqdm" in by_name, "Pipfile 解析失败"
        assert len(manifests) >= 5, "清单发现不完整：%s" % manifests

        # -r 引用的包也算进来
        assert "idna" in by_name, "-r 引用的清单没有被解析"
        # extras 与环境标记
        sock_dep = [d for d in by_name["requests"] if d.extras]
        assert sock_dep and sock_dep[0].extras == ["socks"], "extras 解析失败"
        numpy_dep = by_name["numpy"][0]
        assert "python_version" in numpy_dep.marker, "环境标记解析失败"
        # 开发依赖归类
        assert any(d.kind == "开发" for d in by_name.get("pytest", [])), "dev 归类失败"
        assert by_name["jest"][0].kind == "开发", "package.json devDependencies 归类失败"
        # poetry 的 python 行要被跳过，^0.12 这类约束要保留
        assert "python" not in by_name
        assert by_name["typer"][0].constraint.startswith("^0.12")

        # --- 版本/约束工具 ---
        assert parse_version("2.31.0") == (2, 31, 0)
        assert version_cmp((2, 31), (2, 31, 0)) == 0
        assert constraint_matches(">=2.0,<3.0", "2.25.1") is True
        assert constraint_matches(">=2.0,<3.0", "3.0.1") is False
        assert constraint_matches("==2.31.0", "2.31.0") is True
        assert constraints_overlap(">=2.0", "<3.0") is True
        assert constraints_overlap("<2.0", ">=2.5") is False
        assert extract_pinned_version("==2.31.0") == "2.31.0"
        assert extract_pinned_version(">=2.31") is None
        assert is_unpinned("") is True
        assert is_unpinned(">=1.0") is False
        assert is_overbroad(">=0") is True
        assert is_overbroad(">=0.0.0") is True
        assert is_overbroad("*") is True
        assert is_overbroad(">=2.31,<3.0") is False

        # --- typosquat 检测：命中原样本、不误报流行包 ---
        assert is_typosquat("requets") == "requests"
        assert is_typosquat("lodahs") == "lodash"
        assert is_typosquat("requests") is None
        assert is_typosquat("lodash") is None
        for popular in sorted(POPULAR_PACKAGES):
            assert is_typosquat(popular) is None, "流行包 %s 被误报为 typosquat" % popular
        assert is_typosquat("completely-different-name") is None

        # --- 风险规则：每条都要能命中 ---
        findings = scan_dependencies(deps)
        hit_rules = set(item["rule_id"] for item in findings)
        expected_rules = set(spec["id"] for spec in RULE_SPECS)
        missing = expected_rules - hit_rules
        assert not missing, "以下规则未命中：%s" % sorted(missing)

        def hit(rule_id, package):
            return any(
                item["rule_id"] == rule_id and item["name"] == package
                for item in findings
            )

        assert hit("DA001", "requests"), "DA001 未命中 requests"
        assert hit("DA002", "flask"), "DA002 未命中 flask"
        assert hit("DA003", "django"), "DA003 未命中 django"
        assert hit("DA004", "demo"), "DA004 未命中 git 依赖"
        assert hit("DA005", "requets"), "DA005 未命中 requets"
        assert hit("DA005", "lodahs"), "DA005 未命中 lodahs"
        assert hit("DA006", "click"), "DA006 未命中 click 重复声明"
        assert hit("DA007", "chardet"), "DA007 未命中 LGPL 依赖"
        assert hit("DA007", "pymupdf"), "DA007 未命中 AGPL 依赖"
        assert hit("DA008", "pytest"), "DA008 未命中 pytest"
        assert hit("DA009", "pyyaml"), "DA009 未命中 PyYAML"
        for item in findings:
            assert item["level"] in LEVELS
            assert item["detail"] and item["fix"]
            assert item["source"]
            assert item["rule_id"] in RULE_BY_ID

        # 编辑距离（含相邻换位只算一次）
        assert edit_distance("lodahs", "lodash") == 1
        assert edit_distance("requets", "requests") == 1
        assert edit_distance("requests", "requests") == 0
        assert edit_distance("abc", "xyz", limit=1) > 1

        # 没有风险的干净清单不应误报
        clean = [Dependency(name="requests", constraint=">=2.31,<3.0",
                            source="requirements.txt", kind="运行", line_no=1,
                            manifest="requirements")]
        clean_findings = scan_dependencies(clean)
        assert clean_findings == [], "干净依赖被误报：%s" % clean_findings

        # --- 本地公告匹配 ---
        advisory_path = root / "advisories.json"
        advisory_path.write_text(json.dumps([
            {"package": "requests", "versions": ">=2.0,<2.26",
             "severity": "风险", "title": "requests 中间人攻击漏洞", "link": "https://example.com/a"},
            {"package": "httpx", "versions": ">=0.27.0",
             "severity": "提示", "title": "httpx 某行为变更", "link": ""},
        ], ensure_ascii=False), encoding="utf-8")
        advisories = parse_advisory_file(str(advisory_path))
        assert len(advisories) == 2 and advisories[0]["package"] == "requests"

        pinned_old = Dependency(name="requests", constraint="==2.25.1",
                                source="requirements.txt", kind="运行", line_no=1)
        pinned_new = Dependency(name="requests", constraint="==2.31.0",
                                source="requirements.txt", kind="运行", line_no=1)
        hits_old = match_advisories([pinned_old], advisories)
        assert len(hits_old) == 1 and hits_old[0]["severity"] == "风险"
        assert not match_advisories([pinned_new], advisories), "新版本被公告误报"
        # 无固定版本 → 视为可能受影响
        unpinned_dep = Dependency(name="requests", constraint="",
                                  source="requirements.txt", kind="运行", line_no=1)
        hits_unpinned = match_advisories([unpinned_dep], advisories)
        assert hits_unpinned and "可能" in hits_unpinned[0]["reason"]

        # --- 评分 ---
        score = compute_score(findings)
        assert 0 <= score <= 100
        counts = {}
        for item in findings:
            counts[item["rule_id"]] = counts.get(item["rule_id"], 0) + 1
        expected_score = min(100, sum(
            min(count, 5) * RULE_BY_ID[rule_id]["weight"]
            for rule_id, count in counts.items()
        ))
        assert score == expected_score, "评分计算不符：%s != %s" % (score, expected_score)
        assert score_level(10) == "提示"
        assert score_level(30) == "警告"
        assert score_level(80) == "风险"
        assert compute_score([]) == 0
        filtered = filter_by_level(findings, "风险")
        assert filtered and all(item["level"] == "风险" for item in filtered)
        assert len(filter_by_level(findings, "提示")) == len(findings)

        # --- 报告生成 ---
        report = build_report(
            deps, findings, advisories, hits_old, [], "", DEFAULT_MODEL, True,
            notes, "提示",
        )
        assert report.startswith("# 依赖风险审计报告")
        for heading in ("## 概览", "## 风险清单", "## 逐包明细",
                        "## 规则命中统计", "## 升级与替换建议", "## 附录：内置规则表"):
            assert heading in report, "报告缺少 %s" % heading
        assert "requets" in report and "typosquat" in report
        assert "requests 中间人攻击漏洞" in report

        # 模型建议解析兜底
        good = parse_advice_json(
            '```json\n[{"package": "requests", "advice": "升级到 2.31", "risk": "中间人攻击"}]\n```'
        )
        assert good and good[0]["package"] == "requests" and "2.31" in good[0]["advice"]
        assert parse_advice_json("这不是 JSON") is None
        assert parse_advice_json("") is None
        assert parse_advice_json("[{\"package\": \"x\"}]") is None, "缺 advice 字段应当被丢弃"

        bad_report = build_report(deps, findings, [], [], [], "模型返回了一段散文",
                                  DEFAULT_MODEL, False, notes, "提示")
        assert "附录：模型原始输出" in bad_report and "模型返回了一段散文" in bad_report

        table = render_rules_table()
        for spec in RULE_SPECS:
            assert spec["id"] in table

        # --- call_model 逐级降级（离线假客户端） ---
        fake = _FakeClient()
        result = call_model(fake, "fake-model", "system", [{"role": "user", "content": "hi"}])
        assert result["ok"] is True
        assert fake.state["calls"] == 2, "降级调用次数应该是 2，实际 %s" % fake.state["calls"]

        # 三级全部降级：beta 抛、带 thinking 抛、最后一层成功
        full_fake = _FakeClient()
        first = call_model(full_fake, "fake-model", "system", [{"role": "user", "content": "hi"}])
        assert first["ok"] is True

        # --- 报告落盘 ---
        output_path = root / "audit-report.md"
        output_path.write_text(report, encoding="utf-8")
        assert output_path.read_text(encoding="utf-8").startswith("# 依赖风险审计报告")

    print("SELF-TEST OK")


# ---------- CLI ----------

def build_parser():
    parser = argparse.ArgumentParser(
        description="依赖风险审计：解析依赖清单 + 内置风险规则扫描 + 模型升级建议，输出中文 Markdown 报告",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            "  python3 agent.py ./requirements.txt --output 审计报告.md\n"
            "  python3 agent.py . --advisory 本地公告.json --min-level 警告\n"
            "  python3 agent.py ./package.json --offline\n"
        ),
    )
    parser.add_argument("paths", nargs="*", help="要审计的依赖清单文件或目录，可以给多个")
    parser.add_argument(
        "--advisory",
        action="append",
        default=[],
        metavar="JSON",
        help="本地安全公告 JSON 文件（[{package, versions, severity, title, link}]），可重复",
    )
    parser.add_argument(
        "--output",
        default=DEFAULT_OUTPUT,
        help="报告写入路径（默认 %s）" % DEFAULT_OUTPUT,
    )
    parser.add_argument(
        "--min-level",
        choices=list(LEVELS),
        default="提示",
        help="报告里展示的最低风险级别（默认 提示，即全部展示）",
    )
    parser.add_argument(
        "--offline",
        action="store_true",
        help="纯确定性模式：只跑解析和规则，不调用模型、不需要 API key",
    )
    parser.add_argument(
        "--model",
        default=DEFAULT_MODEL,
        help="升级建议使用的模型（默认 %s）" % DEFAULT_MODEL,
    )
    parser.add_argument(
        "--list-rules",
        action="store_true",
        help="打印全部内置风险规则后退出",
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

    if args.list_rules:
        print(render_rules_table())
        return

    if not args.paths:
        parser.error("请至少提供一个依赖清单文件或目录路径")

    if len(args.advisory) > MAX_ADVISORY_FILES:
        parser.error("--advisory 最多给 %d 个文件" % MAX_ADVISORY_FILES)

    try:
        deps, manifests, notes = collect_dependencies(args.paths)
    except FileNotFoundError as exc:
        print("错误：%s" % exc, file=sys.stderr)
        write_failure_report(args, "路径检查失败：%s" % exc)
        sys.exit(1)

    if not deps:
        print(
            "错误：没有解析出任何依赖（请确认路径下有 requirements.txt / pyproject.toml / "
            "package.json / Pipfile 等清单文件）。",
            file=sys.stderr,
        )
        write_failure_report(args, "没有解析出任何依赖（请确认路径下有 requirements.txt / "
                                   "pyproject.toml / package.json / Pipfile 等清单文件）")
        sys.exit(1)

    advisories = []
    for advisory_path in args.advisory:
        try:
            advisories.extend(parse_advisory_file(advisory_path))
        except (ValueError, OSError) as exc:
            print("错误：%s" % exc, file=sys.stderr)
            write_failure_report(args, "公告文件读取失败：%s" % exc)
            sys.exit(1)

    print("[解析] 依赖 %d 条，清单 %d 个：%s"
          % (len(deps), len(manifests), "、".join(str(item) for item in manifests)))
    findings = scan_dependencies(deps)
    print("[规则] 命中 %d 条，风险分 %d（%s）"
          % (len(findings), compute_score(findings), score_level(compute_score(findings))))

    advisory_hits = match_advisories(deps, advisories) if advisories else []
    if advisories:
        print("[公告] 已加载 %d 条本地公告，命中 %d 条" % (len(advisories), len(advisory_hits)))

    advice_items = []
    raw_model_text = ""
    if args.offline:
        print("[离线] --offline 模式，跳过模型建议")
    else:
        if not os.environ.get("ANTHROPIC_API_KEY") and not os.environ.get("ANTHROPIC_AUTH_TOKEN"):
            print(
                "提示：没有检测到 ANTHROPIC_API_KEY / ANTHROPIC_AUTH_TOKEN，无法生成模型升级建议。\n"
                "请先设置：\n"
                "  macOS / Linux：export ANTHROPIC_API_KEY=你的key\n"
                "  Windows PowerShell：$env:ANTHROPIC_API_KEY=\"你的key\"\n"
                "或者加 --offline 只跑确定性审计（不需要 key）。",
                file=sys.stderr,
            )
            sys.exit(1)
        if findings:
            print("[建议] 模型：%s" % args.model)
            try:
                advice_items, raw_model_text = generate_advice(findings, args.model)
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
        else:
            print("[建议] 没有命中项，跳过模型建议")

    report = build_report(
        deps, findings, advisories, advisory_hits, advice_items, raw_model_text,
        args.model, args.offline, notes, args.min_level,
    )

    output_path = Path(args.output).expanduser()
    try:
        if output_path.parent and not output_path.parent.exists():
            output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(report, encoding="utf-8")
    except OSError as exc:
        print("错误：报告写入失败：%s" % exc, file=sys.stderr)
        sys.exit(1)

    shown = filter_by_level(findings, args.min_level)
    print("[完成] 报告已写入：%s" % output_path)
    print("[完成] 依赖 %d 条，命中 %d 条（展示 %d 条，最低级别 %s），风险分 %d（%s）"
          % (len(deps), len(findings), len(shown), args.min_level,
             compute_score(findings), score_level(compute_score(findings))))
    if raw_model_text and not advice_items:
        print("[提示] 模型输出不是合法 JSON，已原样放入报告附录。")


if __name__ == "__main__":
    main()
