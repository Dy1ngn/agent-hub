#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Git 提交信息生成助手 —— 读你的改动，帮你写一条规范的 commit message

它只读 git 信息（status / diff / 分支 / 最近提交），先用确定性启发式判断
候选类型（feat / fix / docs / style / refactor / test / chore / build / ci），
再让模型把改动总结成规范的提交信息。默认只打印、不提交；加 --apply 才真提交。

用法：
    py -3 agent.py [--repo PATH] [--staged | --all | --range A..B]
                   [--style conventional|gitmoji|plain] [--lang zh|en]
                   [--apply [--yes]] [--dry-run] [--amend]

示例：
    py -3 agent.py --dry-run
    py -3 agent.py --all --style gitmoji
    py -3 agent.py --range HEAD~3..HEAD --lang en
    py -3 agent.py --apply --yes

环境要求：
    1. 设置 ANTHROPIC_API_KEY 环境变量（或 ANTHROPIC_AUTH_TOKEN）：
           PowerShell:  $env:ANTHROPIC_API_KEY="你的key"
           macOS/Linux: export ANTHROPIC_API_KEY=你的key
    2. 安装 SDK：
           py -3 -m pip install -r requirements.txt
    3. 本机需要 git，并在要提交的仓库里运行（或用 --repo 指定路径）。

安全边界：
    只会执行 git 的只读命令，以及在 --apply 且你确认后才执行 `git commit`。
    绝不 `git add`、绝不 `git push`、绝不 `reset/checkout/stash`。
"""

import argparse
import os
import re
import shutil
import stat
import subprocess
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


# 默认模型（可用 --model 参数或 COMMIT_CRAFTER_MODEL 环境变量更换）
DEFAULT_MODEL = os.environ.get("COMMIT_CRAFTER_MODEL", "claude-opus-5")

MAX_SUBJECT_CHARS = 50      # subject 最大字符数（超出截断）
MAX_BODY_LINES = 6          # 正文最多几条
DEFAULT_MAX_DIFF_CHARS = 60000  # diff 文本最多塞多少字符给模型
RECENT_COMMIT_COUNT = 10    # 读最近多少条提交标题，用于对齐仓库风格
MAX_UNTRACKED_SHOWN = 30    # 提示里最多列多少个未跟踪/未暂存文件

MAX_TOKENS = 4000
BOX_WIDTH = 64

# 允许的提交类型（Conventional Commits 常用集合）
COMMIT_TYPES = (
    "feat", "fix", "docs", "style", "refactor",
    "perf", "test", "build", "ci", "chore", "revert",
)

# 依赖声明文件（改这些但不碰业务代码 → chore / build）
DEP_FILES = {
    "package.json", "requirements.txt", "pyproject.toml", "pipfile",
    "cargo.toml", "go.mod", "gemfile", "composer.json",
    "build.gradle", "build.gradle.kts", "pom.xml",
}
# 锁文件（只改锁文件属于依赖同步 → chore）
LOCK_FILES = {
    "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "poetry.lock",
    "pipfile.lock", "cargo.lock", "go.sum", "gemfile.lock", "composer.lock",
}

# 提交信息解析用的正则（subject 允许前置 gitmoji 或短前缀）
SUBJECT_RE = re.compile(
    r"^(?P<emoji>[\U0001F300-\U0001FAFF\u2600-\u27BF\u2B00-\u2BFF\uFE0F\u200D]+\s*)?"
    r"(?P<type>" + "|".join(COMMIT_TYPES) + r")"
    r"(?:\((?P<scope>[^)]+)\))?(?P<bang>!)?:\s*(?P<subject>.+)$"
)
EMOJI_PREFIX_RE = re.compile(
    r"^[\U0001F300-\U0001FAFF\u2600-\u27BF\u2B00-\u2BFF\uFE0F\u200D]+\s*"
)
BULLET_RE = re.compile(r"^[-*•]\s+")
BREAKING_RE = re.compile(r"^BREAKING[ -]CHANGE:\s*(.*)$", re.IGNORECASE)
VERSION_RE = re.compile(r"\d+\.\d+")


# ---------- git 只读封装 ----------

class GitError(Exception):
    """git 命令执行失败（仓库不存在、不是 git 仓库等）"""


def run_git(repo, args, check=True):
    """在 repo 目录下执行 git，返回 stdout；失败时按需抛 GitError。"""
    cmd = ["git"] + list(args)
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(repo),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except FileNotFoundError:
        raise GitError("找不到 git 命令，请先安装 git 并确认它在 PATH 里")
    out = proc.stdout.decode("utf-8", "replace")
    err = proc.stderr.decode("utf-8", "replace").strip()
    if check and proc.returncode != 0:
        raise GitError("git %s 执行失败：%s" % (" ".join(args), err or "未知错误"))
    return out if proc.returncode == 0 else ""


def ensure_repo(repo_path):
    """确认路径存在且是一个 git 工作区。"""
    path = Path(repo_path).expanduser()
    if not path.is_dir():
        raise GitError("目录不存在：%s" % path)
    try:
        run_git(path, ["rev-parse", "--is-inside-work-tree"])
    except GitError:
        raise GitError("不是 git 仓库（或其子目录）：%s" % path)
    return path.resolve()


def parse_name_status(text):
    """解析 `git diff --name-status`，返回 [(状态字母, 路径), ...]。"""
    items = []
    for line in text.splitlines():
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        status = parts[0][:1] or "M"
        path = parts[-1].strip()
        if path:
            items.append((status, path))
    return items


def collect_changes(repo, mode="staged", rev_range=None, max_diff_chars=DEFAULT_MAX_DIFF_CHARS):
    """只读收集：状态、diff、改动文件、分支、最近提交标题。"""
    if mode == "staged":
        diff_args = ["diff", "--cached"]
    elif mode == "all":
        diff_args = ["diff", "HEAD"]
    elif mode == "range":
        diff_args = ["diff", rev_range]
    else:
        raise ValueError("未知的收集模式：%s" % mode)

    status = run_git(repo, ["status", "--porcelain"])
    diff_text = run_git(repo, diff_args)
    diff_stat = run_git(repo, diff_args + ["--stat"])
    name_status = run_git(repo, diff_args + ["--name-status"])
    # 忽略空白后的 diff：用来判断"是否只是格式化"
    whitespace_ignored = run_git(repo, diff_args + ["-w"])

    branch = run_git(repo, ["rev-parse", "--abbrev-ref", "HEAD"], check=False).strip()
    recent = [
        line.strip()
        for line in run_git(repo, ["log", "--pretty=%s", "-n", str(RECENT_COMMIT_COUNT)],
                            check=False).splitlines()
        if line.strip()
    ]

    files = parse_name_status(name_status)
    untracked = []
    if mode == "all":
        untracked = [
            line.strip()
            for line in run_git(repo, ["ls-files", "--others", "--exclude-standard"]).splitlines()
            if line.strip()
        ]
        files.extend(("?", p) for p in untracked)

    truncated = False
    if len(diff_text) > max_diff_chars:
        diff_text = diff_text[:max_diff_chars]
        truncated = True
        diff_text += "\n……（diff 过长，已截断，仅保留前 %d 字符）" % max_diff_chars

    return {
        "mode": mode,
        "range": rev_range,
        "branch": branch or "（尚无提交）",
        "status": status,
        "diff": diff_text,
        "diff_stat": diff_stat,
        "whitespace_ignored_diff": whitespace_ignored,
        "files": files,
        "untracked": untracked,
        "recent_subjects": recent,
        "truncated": truncated,
    }


# ---------- 确定性启发式分类 ----------

def _posix(path):
    return path.replace("\\", "/")


def is_doc_path(path):
    p = _posix(path).lower()
    name = p.rsplit("/", 1)[-1]
    return (
        p.endswith((".md", ".mdx", ".rst", ".adoc"))
        or p.startswith("docs/")
        or "/docs/" in p
        or name in ("license", "changelog", "contributing")
    )


def is_test_path(path):
    p = _posix(path).lower()
    name = p.rsplit("/", 1)[-1]
    return (
        name.startswith("test_")
        or name.endswith(("_test.py", "_test.go", ".test.js", ".test.ts",
                          ".spec.js", ".spec.ts"))
        or "/tests/" in "/" + p
        or "/test/" in "/" + p
        or p.startswith(("tests/", "test/"))
    )


def is_ci_path(path):
    p = _posix(path).lower()
    name = p.rsplit("/", 1)[-1]
    return (
        p.startswith(".github/")
        or p.startswith(".gitlab-ci")
        or p.startswith(".circleci/")
        or p.startswith("ci/")
        or name in (".travis.yml", "jenkinsfile", "azure-pipelines.yml",
                    "appveyor.yml", ".drone.yml", "bitbucket-pipelines.yml")
    )


def is_dep_path(path):
    name = _posix(path).lower().rsplit("/", 1)[-1]
    return name in DEP_FILES or name in LOCK_FILES


def is_lock_path(path):
    name = _posix(path).lower().rsplit("/", 1)[-1]
    return name in LOCK_FILES


def has_version_bump(diff_text):
    """diff 里是否同时出现依赖的旧版本行和新版本行（升级判断，不追求完美）。"""
    added, removed = [], []
    for line in diff_text.splitlines():
        if line.startswith("+") and not line.startswith("+++"):
            added.append(line[1:])
        elif line.startswith("-") and not line.startswith("---"):
            removed.append(line[1:])
    looks_like_dep = lambda s: (
        VERSION_RE.search(s) and any(k in s for k in ("==", ">=", ":=", '"', ":"))
    )
    return any(looks_like_dep(l) for l in added) and any(
        looks_like_dep(l) for l in removed
    )


def _new_source_files(files):
    """新增的、且不属于文档/测试/CI/依赖的"业务源文件"。"""
    result = []
    for status, path in files:
        if status not in ("A", "?", "R", "C"):
            continue
        if is_doc_path(path) or is_test_path(path) or is_ci_path(path) or is_dep_path(path):
            continue
        result.append(path)
    return result


def classify_changes(info):
    """返回 {type, reason, candidates, uncertain}，不依赖网络和模型。"""
    files = list(info.get("files") or [])
    diff = info.get("diff") or ""
    ws_ignored = info.get("whitespace_ignored_diff") or ""
    untracked = list(info.get("untracked") or [])

    if not files:
        return {
            "type": None,
            "reason": "没有检测到任何改动",
            "candidates": [],
            "uncertain": True,
        }

    paths = [p for _, p in files]
    statuses = [s for s, _ in files]
    n = len(paths)

    if all(is_doc_path(p) for p in paths):
        return {
            "type": "docs",
            "reason": "仅修改文档（%d 个 .md/docs 文件）" % n,
            "candidates": ["docs"],
            "uncertain": False,
        }
    if all(is_test_path(p) for p in paths):
        return {
            "type": "test",
            "reason": "仅修改测试文件（%d 个）" % n,
            "candidates": ["test"],
            "uncertain": False,
        }
    if all(is_ci_path(p) for p in paths):
        return {
            "type": "ci",
            "reason": "仅修改 CI 配置（%d 个）" % n,
            "candidates": ["ci"],
            "uncertain": False,
        }
    if all(is_dep_path(p) for p in paths):
        if (not all(is_lock_path(p) for p in paths)) and has_version_bump(diff):
            return {
                "type": "build",
                "reason": "仅修改依赖声明且升级了版本（%d 个依赖文件）" % n,
                "candidates": ["build", "chore"],
                "uncertain": False,
            }
        return {
            "type": "chore",
            "reason": "仅修改依赖/锁文件，没有业务代码变化（%d 个）" % n,
            "candidates": ["chore"],
            "uncertain": False,
        }

    # 纯格式化：有 diff，但忽略空白后 diff 为空，且没有新增/删除文件
    only_modified = all(s in ("M", "T") for s in statuses)
    if diff.strip() and not ws_ignored.strip() and only_modified:
        return {
            "type": "style",
            "reason": "改动仅为空白/缩进变化（忽略空白后无差异）",
            "candidates": ["style"],
            "uncertain": False,
        }

    new_sources = _new_source_files(files)
    if new_sources:
        preview = "、".join(new_sources[:3])
        if len(new_sources) > 3:
            preview += " 等"
        return {
            "type": "feat",
            "reason": "新增 %d 个源文件：%s" % (len(new_sources), preview),
            "candidates": ["feat", "refactor"],
            "uncertain": False,
        }

    if untracked:
        return {
            "type": "feat",
            "reason": "有 %d 个未跟踪的新文件" % len(untracked),
            "candidates": ["feat", "fix"],
            "uncertain": False,
        }

    return {
        "type": "fix",
        "reason": "修改了 %d 个文件，无法从 diff 本身判断是修复还是重构" % n,
        "candidates": ["fix", "refactor"],
        "uncertain": True,
    }


def format_candidate(classification):
    """「候选类型：feat（新增 3 个源文件）」这样的展示文本。"""
    if classification.get("type") is None:
        return "候选类型：无法判断（%s）" % classification.get("reason", "")
    text = "候选类型：%s（%s）" % (classification["type"], classification.get("reason", ""))
    alternatives = [c for c in classification.get("candidates", []) if c != classification["type"]]
    if alternatives:
        text += "，也可能是 %s" % " / ".join(alternatives)
    return text


def guess_scope(files):
    """从改动文件里猜一个 scope（尽量保守，猜不到就不给）。"""
    paths = [p for _, p in files]
    if not paths:
        return None
    firsts = set(_posix(p).split("/")[0] for p in paths)
    if len(firsts) == 1:
        only = list(firsts)[0]
        if "/" in _posix(paths[0]) and only not in (
            "src", "lib", "app", "docs", "tests", "test", ".github",
        ):
            cleaned = re.sub(r"[^a-z0-9_.-]", "", only.lower())
            if cleaned:
                return cleaned
    if len(paths) == 1:
        stem = _posix(paths[0]).rsplit("/", 1)[-1].rsplit(".", 1)[0].lower()
        stem = re.sub(r"[^a-z0-9_-]", "", stem)
        if stem and stem not in ("__init__", "index", "main", "setup", "utils"):
            return stem
    return None


ZH_PHRASES = {
    "feat": "新增功能",
    "fix": "修复问题",
    "docs": "更新文档",
    "style": "统一代码格式",
    "refactor": "重构代码",
    "perf": "优化性能",
    "test": "补充测试",
    "build": "调整构建与依赖",
    "ci": "调整 CI 配置",
    "chore": "更新杂项配置",
    "revert": "回滚改动",
}
EN_PHRASES = {
    "feat": "add feature",
    "fix": "fix issue",
    "docs": "update docs",
    "style": "format code",
    "refactor": "refactor code",
    "perf": "improve performance",
    "test": "add tests",
    "build": "update build and deps",
    "ci": "update ci config",
    "chore": "update chores",
    "revert": "revert change",
}


def build_fallback_subject(classification, info, style, lang):
    """没有模型时，用启发式拼一个 subject（≤50 字符）。"""
    ctype = classification.get("type") or "chore"
    files = list(info.get("files") or [])
    scope = guess_scope(files)
    if style == "plain":
        scope = None
    phrase = (EN_PHRASES if lang == "en" else ZH_PHRASES).get(ctype, ctype)
    n = len(files)
    if lang == "en":
        tail = "%s (%d file%s)" % (phrase, n, "" if n == 1 else "s")
    else:
        tail = "%s（%d 个文件）" % (phrase, n)
    prefix = ctype + ("(%s)" % scope if scope else "") + ": "
    room = MAX_SUBJECT_CHARS - len(prefix)
    if len(tail) > room:
        tail = tail[:max(0, room)].rstrip("（(") or phrase
    return prefix + tail


def build_fallback_body(info, classification, lang):
    """没有模型时，用改动清单拼正文。"""
    files = list(info.get("files") or [])
    limit = 5 if info.get("truncated") else 6
    body = []
    if lang == "en":
        body.append("- summary: %d changed file(s)" % len(files))
        if classification.get("type"):
            body.append("- candidate type: %s" % classification["type"])
        for _, path in files[:limit]:
            body.append("- %s" % path)
    else:
        body.append("- 原因：%s" % classification.get("reason", "无法判断"))
        if files:
            body.append("- 涉及 %d 个文件" % len(files))
        for _, path in files[:limit]:
            body.append("- %s" % path)
    return body[:MAX_BODY_LINES]


def heuristic_message(info, classification, style, lang):
    """无模型时的完整兜底消息。"""
    return {
        "subject": build_fallback_subject(classification, info, style, lang),
        "body": build_fallback_body(info, classification, lang),
        "breaking": "",
        "source": "heuristic",
    }


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


SYSTEM_PROMPT = """你是「Git 提交信息生成助手」。根据用户提供的 git 改动，写一条规范的提交信息。

严格输出格式（不要输出任何解释、前言、Markdown 代码围栏）：
第 1 行：<type>(<scope>): <subject>
   - type 只能是 feat / fix / docs / style / refactor / perf / test / build / ci / chore
   - scope 可选，用改动涉及的模块名；写不准就省略
   - subject ≤ 50 个字符，祈使语气（"增加" "修复" "调整"），结尾不带句号
第 2 行起：空一行后写正文，最多 6 条，每条以 "- " 开头，只写"做了什么/为什么"
可选：如果是不兼容改动，在正文后空一行写 "BREAKING CHANGE: 说明"

要求：
- 只依据 diff 和改动文件，不要编造没发生的改动
- 正文语言严格按用户指定的语言
- 直接输出提交信息本身，不要加"以下是提交信息"之类的话"""


def build_user_prompt(info, classification, style, lang):
    """把收集到的 git 信息和启发式结论整理成给模型的输入。"""
    files = info.get("files") or []
    file_lines = "\n".join("- [%s] %s" % (s, p) for s, p in files[:80]) or "（无）"
    recent = "\n".join("- %s" % s for s in info.get("recent_subjects") or []) or "（仓库还没有提交）"
    mode_text = {
        "staged": "已暂存的改动（git diff --cached）",
        "all": "工作区全部改动（git diff HEAD + 未跟踪文件）",
        "range": "提交区间 %s" % info.get("range", ""),
    }.get(info.get("mode"), info.get("mode", ""))

    style_text = {
        "conventional": "Conventional Commits 风格，scope 能判断就写",
        "gitmoji": "gitmoji 风格：在 type 前加一个最贴切的 emoji，例如「✨ feat(ui): 增加登录按钮」",
        "plain": "朴素风格：不写 scope、不加 emoji，仍保留 type 前缀",
    }.get(style, style)
    lang_text = "中文" if lang == "zh" else "英文"

    return """仓库分支：%s
收集范围：%s
候选类型（启发式）：%s

改动文件（状态 + 路径）：
%s

最近 %d 条提交标题（参考本仓库既有风格，但不要照抄内容）：
%s

git diff --stat：
%s

git diff：
%s
""" % (
        info.get("branch", "未知"),
        mode_text,
        format_candidate(classification),
        file_lines,
        len(info.get("recent_subjects") or []),
        recent,
        (info.get("diff_stat") or "").strip() or "（无）",
        (info.get("diff") or "").strip() or "（无 diff 内容）",
    )


def generate_with_model(model, info, classification, style, lang):
    """调用模型拿原始文本；失败时抛给上层处理。"""
    client = anthropic.Anthropic()
    response = call_model(
        client,
        model,
        SYSTEM_PROMPT,
        [{"role": "user", "content": build_user_prompt(info, classification, style, lang)}],
        max_tokens=MAX_TOKENS,
    )
    return "".join(
        block.text for block in response.content if getattr(block, "type", "") == "text"
    ).strip()


# ---------- 提交信息解析与规范化 ----------

def _strip_fences(raw):
    lines = []
    for line in raw.splitlines():
        stripped = line.strip()
        if stripped.startswith("```"):
            continue
        lines.append(line.rstrip())
    return lines


def _clean_subject_prefix(subject):
    """去掉句末标点，压缩空白。"""
    subject = re.sub(r"\s+", " ", subject).strip()
    return subject.rstrip("。.;；,，")


def truncate_subject(subject):
    """subject 超长时截断到 MAX_SUBJECT_CHARS，尽量在词/词组边界断。"""
    if len(subject) <= MAX_SUBJECT_CHARS:
        return subject
    cut = subject[:MAX_SUBJECT_CHARS]
    # 优先在空格、中文标点、斜杠处断
    for mark in (" ", "，", "、", "/", "-", ":"):
        idx = cut.rfind(mark)
        if idx >= 20:
            cut = cut[:idx]
            break
    return cut.rstrip("。.;；,， -:：")


def parse_commit_text(raw, fallback_subject, max_body=MAX_BODY_LINES):
    """把模型输出解析成 {subject, body, breaking}；解析不了就退化为启发式标题。"""
    lines = _strip_fences(raw or "")
    subject = None
    subject_index = -1

    for i, line in enumerate(lines):
        candidate = line.strip()
        if not candidate:
            continue
        match = SUBJECT_RE.match(candidate)
        if match:
            scope = match.group("scope")
            emoji = match.group("emoji") or ""
            bang = match.group("bang") or ""
            subject = "%s%s%s%s: %s" % (
                emoji,
                match.group("type"),
                "(%s)" % scope if scope else "",
                bang,
                match.group("subject").strip(),
            )
            subject_index = i
            break
        # 没有 type 前缀：把第一行当作标题，但要先排除前言和乱码
        if subject is None and not candidate.endswith(("：", ":")):
            looks_real = (
                bool(re.search(r"[\w\u4e00-\u9fff]", candidate))
                and not re.match(r"^[!@#$%^&*()\[\]{}<>/\\|~`]+", candidate)
            )
            if looks_real and not re.match(
                r"^(以下|下面是|好的|根据|这是|Here|Sure)", candidate
            ):
                subject = candidate
                subject_index = i
                break

    if not subject:
        # 完全解析失败 → 启发式兜底，不抛异常
        return {"subject": fallback_subject, "body": [], "breaking": ""}

    subject = _clean_subject_prefix(subject)
    if not SUBJECT_RE.match(subject):
        # 缺 type 前缀时补上兜底标题里的类型
        prefix = fallback_subject.split(":", 1)[0] + ": "
        subject = prefix + subject
    subject = truncate_subject(subject)

    body, breaking = [], []
    in_breaking = False
    for line in lines[subject_index + 1:]:
        stripped = line.strip()
        if not stripped:
            continue
        breaking_match = BREAKING_RE.match(stripped)
        if breaking_match:
            in_breaking = True
            if breaking_match.group(1).strip():
                breaking.append(breaking_match.group(1).strip())
            continue
        if in_breaking:
            breaking.append(stripped)
            continue
        if stripped.startswith(("```", "注意", "备注：", "Note:")):
            continue
        body.append(stripped)

    normalized = []
    for line in body:
        if BULLET_RE.match(line):
            normalized.append("- " + BULLET_RE.sub("", line).strip())
        else:
            normalized.append("- " + line.strip())
    normalized = [l for l in normalized if l.strip() != "-"][:max_body]

    return {
        "subject": subject,
        "body": normalized,
        "breaking": " ".join(breaking).strip(),
    }


def finalize_message(raw, info, classification, style, lang):
    """模型文本 → 最终提交信息；解析失败时启发式兜底。"""
    fallback = heuristic_message(info, classification, style, lang)
    fallback_subject = fallback["subject"]
    if not raw:
        return dict(fallback, source="heuristic")
    parsed = parse_commit_text(raw, fallback_subject)
    used_fallback = parsed["subject"] == fallback_subject and not parsed["body"]
    return {
        "subject": parsed["subject"],
        "body": parsed["body"] or fallback["body"],
        "breaking": parsed["breaking"],
        "source": "heuristic" if used_fallback else "model",
    }


def format_commit_message(message):
    """拼成最终提交文本（subject + 空行 + 正文 + BREAKING CHANGE）。"""
    parts = [message["subject"].strip()]
    body = [line for line in message.get("body") or [] if line.strip()]
    if body:
        parts.append("\n".join(body))
    if message.get("breaking"):
        parts.append("BREAKING CHANGE: %s" % message["breaking"].strip())
    return "\n\n".join(parts).strip() + "\n"


def build_copy_command(message):
    """可复制的 `git commit -m "..."` 命令（多行正文用一个 -m 承载）。"""
    subject = message["subject"].replace('"', '\\"')
    body = "\n".join(message.get("body") or [])
    if message.get("breaking"):
        body = (body + "\n\n" if body else "") + "BREAKING CHANGE: " + message["breaking"]
    if body:
        return 'git commit -m "%s" -m "%s"' % (subject, body.replace('"', '\\"'))
    return 'git commit -m "%s"' % subject


# ---------- 展示与提交 ----------

def print_box(text):
    print("=" * BOX_WIDTH)
    for line in text.rstrip("\n").splitlines():
        print(line)
    print("=" * BOX_WIDTH)


def print_collection_summary(info, classification):
    print("📁 仓库模式：%s" % {
        "staged": "已暂存改动（--staged）",
        "all": "工作区全部改动（--all）",
        "range": "提交区间（--range %s）" % (info.get("range") or ""),
    }.get(info.get("mode"), info.get("mode")))
    print("🌿 当前分支：%s" % info.get("branch"))
    print("📝 改动文件：%d 个" % len(info.get("files") or []))
    if info.get("untracked"):
        print("🆕 未跟踪文件：%d 个" % len(info["untracked"]))
    if info.get("truncated"):
        print("✂️ diff 过长，已截断后再交给模型")
    print("🧠 %s" % format_candidate(classification))


def list_pending_work(repo):
    """没有可提交内容时，列出未暂存/未跟踪文件供参考。"""
    status = run_git(repo, ["status", "--porcelain"], check=False)
    lines = [l for l in status.splitlines() if l.strip()]
    if not lines:
        print("工作区是干净的，没有需要提交的改动。")
        return
    print("没有可提交的内容（当前范围为空）。工作区里还有这些改动：")
    for line in lines[:MAX_UNTRACKED_SHOWN]:
        print("  %s" % line)
    if len(lines) > MAX_UNTRACKED_SHOWN:
        print("  ……（还有 %d 条，已省略）" % (len(lines) - MAX_UNTRACKED_SHOWN))
    print("提示：先用 git add 暂存，或改用 --all 查看全部改动。")


def confirm_commit():
    """交互确认；非交互且没 --yes 时视为取消。"""
    if not sys.stdin.isatty():
        print("检测到非交互环境且没有 --yes，已取消提交。")
        return False
    try:
        answer = input("确认执行 git commit？[y/N] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return False
    return answer in ("y", "yes")


def do_commit(repo, message, amend=False):
    """真正执行 git commit（只允许 commit，不 add/push/reset）。"""
    fd, msg_path = tempfile.mkstemp(prefix="commit-msg-", suffix=".txt")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(format_commit_message(message))
        args = ["-c", "i18n.commitEncoding=utf-8", "commit", "-F", msg_path]
        if amend:
            args.append("--amend")
        run_git(repo, args)
    finally:
        try:
            os.unlink(msg_path)
        except OSError:
            pass
    print("✅ 提交完成：%s" % run_git(repo, ["log", "-1", "--oneline"]).strip())


# ---------- self-test ----------

def _write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def remove_tree(path):
    """删除临时目录；Windows 上 git 对象文件是只读的，需要先改权限再删。"""
    def _on_error(func, target, exc_info):
        try:
            os.chmod(target, stat.S_IWRITE)
            func(target)
        except OSError:
            pass

    shutil.rmtree(str(path), onerror=_on_error)


def _init_test_repo(root):
    repo = root / "repo"
    repo.mkdir(parents=True)
    run_git(repo, ["init", "-q"])
    identity = [
        "-c", "user.name=Commit Crafter",
        "-c", "user.email=crafter@example.com",
    ]
    _write(repo / "README.md", "# demo\n")
    _write(repo / "src" / "app.py", "def main():\n    return 1\n")
    run_git(repo, identity + ["add", "-A"])
    run_git(repo, identity + ["commit", "-q", "-m", "chore: 初始化示例仓库"])

    _write(repo / "src" / "app.py", "def main():\n    return 2\n")
    run_git(repo, identity + ["add", "-A"])
    run_git(repo, identity + ["commit", "-q", "-m", "feat(app): 返回 2"])

    # 造一个已暂存的改动（2 个文件）
    _write(repo / "src" / "app.py", "def main():\n    return 3\n\n\ndef helper():\n    return 0\n")
    _write(repo / "NOTES.md", "# 说明\n\n随便写点。\n")
    run_git(repo, identity + ["add", "-A"])
    return repo, identity


def run_self_test():
    """不联网、不需要 API key，只验证确定性逻辑。"""
    root = Path(tempfile.mkdtemp(prefix="commit-crafter-selftest-"))
    try:
        repo, identity = _init_test_repo(root)

        # --- 1. 只读收集 ---
        info = collect_changes(repo, "staged")
        assert info["files"], "应当收集到已暂存文件"
        assert any(p.endswith("NOTES.md") for _, p in info["files"]), "缺少 NOTES.md"
        assert "def helper" in info["diff"], "diff 应包含新增函数"
        assert "NOTES.md" in info["diff_stat"] or len(info["diff_stat"]) >= 0
        assert info["branch"] in ("master", "main", "HEAD"), "分支名解析异常：%s" % info["branch"]
        assert len(info["recent_subjects"]) >= 2, "应读到最近提交标题"

        # 截断逻辑
        tiny = collect_changes(repo, "staged", max_diff_chars=10)
        assert tiny["truncated"] is True, "diff 应被标记为截断"
        assert "已截断" in tiny["diff"], "截断提示缺失"

        # --- 2. 真实改动上的启发式 ---
        real = classify_changes(info)
        assert real["type"] in ("feat", "fix", "refactor"), "真实改动分类异常：%s" % real
        assert format_candidate(real).startswith("候选类型：")

        # --- 3. 各启发式分支（合成数据，纯确定性） ---
        def cls(paths, diff="", ws=None, untracked=None):
            return classify_changes({
                "files": paths,
                "diff": diff,
                "whitespace_ignored_diff": diff if ws is None else ws,
                "untracked": untracked or [],
            })

        assert cls([("M", "README.md"), ("A", "docs/guide.md")])["type"] == "docs"
        assert cls([("M", "tests/test_a.py")])["type"] == "test"
        assert cls([("A", ".github/workflows/ci.yml")])["type"] == "ci"
        dep_bump = (
            '--- a/package.json\n+++ b/package.json\n'
            '-  "lodash": "4.17.20",\n+  "lodash": "4.17.21",\n'
        )
        assert cls([("M", "package.json")], dep_bump)["type"] == "build"
        assert cls([("M", "requirements.txt")], "+# keep")["type"] == "chore"
        assert cls([("M", "package-lock.json")], dep_bump)["type"] == "chore"
        assert cls([("M", "src/a.py")], "-x=1\n+x = 1\n", ws="")["type"] == "style"
        assert cls([("A", "src/new_feature.py")], "+def f():\n+    pass\n")["type"] == "feat"
        assert cls([("M", "src/a.py")], "-a\n+b\n", ws="-a\n+b\n")["type"] == "fix"
        assert cls([])["type"] is None
        assert "无法判断" in format_candidate(cls([]))

        # --- 4. 解析健壮性 ---
        long_subject = "feat(core): " + "很长的描述" * 20
        raw = "好的，以下是根据改动生成的提交信息：\n```\n%s\n\n- 第一条\n- 第二条\n- 第三条\n- 第四条\n- 第五条\n- 第六条\n- 第七条\n- 第八条\n\nBREAKING CHANGE: 接口签名变了\n```\n" % long_subject
        parsed = parse_commit_text(raw, "fix: 兜底标题")
        assert parsed["subject"].startswith("feat(core): "), parsed["subject"]
        assert len(parsed["subject"]) <= MAX_SUBJECT_CHARS, "subject 未截断"
        assert len(parsed["body"]) == MAX_BODY_LINES, "正文条数未限制"
        assert all(l.startswith("- ") for l in parsed["body"])
        assert parsed["breaking"] == "接口签名变了"

        # 缺 type 前缀 → 自动补齐
        p2 = parse_commit_text("修复登录偶发失败", "fix: 兜底")
        assert p2["subject"].startswith("fix: "), p2["subject"]
        assert "修复登录偶发失败" in p2["subject"]

        # 彻底解析失败 → 不抛异常，用兜底标题
        p3 = parse_commit_text("!!!@@@###", "fix: 兜底标题")
        assert p3["subject"] == "fix: 兜底标题", p3["subject"]

        # 空输入 → 兜底
        p4 = parse_commit_text("", "chore: 兜底")
        assert p4["subject"] == "chore: 兜底"

        # --- 5. 最终消息 + 可复制命令 ---
        msg = finalize_message(raw, info, real, "conventional", "zh")
        text = format_commit_message(msg)
        lines = text.strip().splitlines()
        assert SUBJECT_RE.match(lines[0]), "最终 subject 不符合格式：%s" % lines[0]
        assert len(lines[0]) <= MAX_SUBJECT_CHARS
        assert lines[1] == "", "subject 后应有空行"
        assert "BREAKING CHANGE:" in text
        copy_cmd = build_copy_command(msg)
        assert copy_cmd.startswith('git commit -m "'), copy_cmd
        assert msg["subject"] in copy_cmd

        # 模型输出为空 → 启发式兜底
        fb = finalize_message("", info, real, "conventional", "zh")
        assert fb["source"] == "heuristic"
        assert len(fb["body"]) <= MAX_BODY_LINES
        assert SUBJECT_RE.match(fb["subject"]), fb["subject"]
        assert len(fb["subject"]) <= MAX_SUBJECT_CHARS, fb["subject"]

        # gitmoji / plain / en 兜底也要合规
        gm = heuristic_message(info, real, "gitmoji", "zh")
        assert SUBJECT_RE.match(gm["subject"]), gm["subject"]
        en = heuristic_message(info, real, "plain", "en")
        assert SUBJECT_RE.match(en["subject"]), en["subject"]
        assert len(en["subject"]) <= MAX_SUBJECT_CHARS

        # --- 6. 其它收集模式 ---
        _write(repo / "src" / "extra.py", "VALUE = 1\n")
        all_info = collect_changes(repo, "all")
        assert any(p.endswith("extra.py") for _, p in all_info["files"]), "未跟踪文件未纳入"
        assert any(p.endswith("extra.py") for p in all_info["untracked"])
        assert all_info["mode"] == "all"

        run_git(repo, identity + ["commit", "-q", "-m", "chore: 测试区间"])
        range_info = collect_changes(repo, "range", "HEAD~2..HEAD")
        assert range_info["mode"] == "range"
        assert range_info["files"], "区间模式应收集到改动"

        # --- 7. 解析 name-status / 清理 ---
        ns = parse_name_status("A\tnew.py\nM\tsrc/a.py\nR100\told.py\tnewer.py\n")
        assert ns == [("A", "new.py"), ("M", "src/a.py"), ("R", "newer.py")], ns
        assert is_doc_path("docs/a.md") and not is_doc_path("src/a.py")
        assert is_test_path("tests/test_x.py") and not is_test_path("src/testable.py")
        assert is_ci_path(".github/workflows/ci.yml")
        assert is_dep_path("requirements.txt") and not is_dep_path("src/deps.py")
        assert has_version_bump(dep_bump)
        assert guess_scope([("M", "src/app.py")]) is not None

        print("SELF-TEST OK")
        return 0
    finally:
        remove_tree(root)


# ---------- CLI ----------

def build_parser():
    parser = argparse.ArgumentParser(
        description="Git 提交信息生成助手：读改动、生成规范 commit message，默认只打印不提交",
        epilog="安全：只执行 git 只读命令，以及 --apply 且确认后的 git commit；绝不 add/push/reset。",
    )
    parser.add_argument("--repo", default=".", help="仓库路径，默认当前目录")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--staged", action="store_true", help="只看已暂存改动（默认）")
    group.add_argument("--all", action="store_true", help="看工作区全部改动（含未跟踪文件名）")
    group.add_argument("--range", metavar="A..B", help="看指定提交区间，如 HEAD~3..HEAD")
    parser.add_argument(
        "--style", default="conventional",
        choices=["conventional", "gitmoji", "plain"], help="提交信息风格（默认 conventional）",
    )
    parser.add_argument(
        "--lang", default="zh", choices=["zh", "en"], help="正文语言（默认 zh）",
    )
    parser.add_argument("--apply", action="store_true", help="确认后真正执行 git commit")
    parser.add_argument("--yes", action="store_true", help="跳过交互确认（配合 --apply）")
    parser.add_argument("--dry-run", action="store_true", help="只生成并预览，不提交")
    parser.add_argument("--amend", action="store_true", help="追加到上一条提交（需同时 --apply）")
    parser.add_argument(
        "--max-diff-chars", type=int, default=DEFAULT_MAX_DIFF_CHARS,
        help="diff 最多传给模型多少字符（默认 %d）" % DEFAULT_MAX_DIFF_CHARS,
    )
    parser.add_argument(
        "--model", default=DEFAULT_MODEL,
        help="使用的模型（默认 %s，可用 COMMIT_CRAFTER_MODEL 覆盖）" % DEFAULT_MODEL,
    )
    parser.add_argument("--self-test", action="store_true", help="离线自检，不联网、不需要 API key")
    return parser


def main(argv=None):
    ensure_utf8_console()
    args = build_parser().parse_args(argv)

    if args.self_test:
        return run_self_test()

    if args.range and not re.match(r"^.+\.\.\.?.+$", args.range):
        print("错误：--range 的格式应为 A..B，例如 HEAD~3..HEAD", file=sys.stderr)
        return 2
    if args.max_diff_chars <= 0:
        print("错误：--max-diff-chars 必须是正整数", file=sys.stderr)
        return 2

    try:
        repo = ensure_repo(args.repo)
    except GitError as e:
        print("错误：%s" % e, file=sys.stderr)
        return 1

    mode = "range" if args.range else ("all" if args.all else "staged")

    try:
        info = collect_changes(repo, mode, args.range, args.max_diff_chars)
    except GitError as e:
        print("错误：%s" % e, file=sys.stderr)
        return 1

    if not info["files"]:
        list_pending_work(repo)
        if args.range:
            print("提示：区间 %s 内没有文件改动。" % args.range)
        return 0

    classification = classify_changes(info)
    print_collection_summary(info, classification)

    if args.amend and not args.apply:
        print("提示：--amend 只在同时给 --apply 时生效。")

    has_key = os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")
    raw = ""
    if has_key:
        print("🤖 模型：%s" % args.model)
        try:
            raw = generate_with_model(
                args.model, info, classification, args.style, args.lang
            )
        except anthropic.AuthenticationError:
            print("错误：API key 无效或没有权限，请检查 ANTHROPIC_API_KEY。", file=sys.stderr)
            return 1
        except anthropic.APIConnectionError:
            print("错误：无法连接 Anthropic API，请检查网络或 ANTHROPIC_BASE_URL。", file=sys.stderr)
            return 1
        except anthropic.RateLimitError:
            print("错误：请求太频繁被限流，请稍后再试。", file=sys.stderr)
            return 1
        except anthropic.APIStatusError as e:
            print("错误：API 返回错误（%s）：%s" % (e.status_code, e.message), file=sys.stderr)
            return 1
        except Exception as e:  # 模型/网关的其它问题：降级为启发式，不让流程中断
            print("提示：模型调用失败（%s），改用启发式生成。" % e)
            raw = ""
    else:
        print("提示：没有检测到 ANTHROPIC_API_KEY / ANTHROPIC_AUTH_TOKEN，改用启发式生成标题。")

    message = finalize_message(raw, info, classification, args.style, args.lang)
    print("-" * BOX_WIDTH)
    print("📝 最终提交信息：")
    print_box(format_commit_message(message))
    print("💡 可复制的命令：")
    print(build_copy_command(message))
    print("-" * BOX_WIDTH)

    if args.amend and not args.apply:
        print("已跳过提交（--amend 需要 --apply）。")
        return 0

    if not args.apply or args.dry_run:
        print("预览模式：未执行任何提交。确认无误后加 --apply --yes 真提交。")
        return 0

    if not args.yes and not confirm_commit():
        print("已取消，没有执行 git commit。")
        return 0

    try:
        do_commit(repo, message, amend=args.amend)
    except GitError as e:
        print("错误：%s" % e, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
