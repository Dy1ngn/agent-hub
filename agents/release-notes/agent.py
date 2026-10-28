#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
发布说明生成助手 —— conventional commits 解析 + 模型润色 的 Release Notes / Changelog 生成 agent

输入可以是 git 仓库的一段提交区间（--repo + --from/--to），也可以是每行一条 commit message
的文本文件（--commits）。先做完全确定性的 conventional commits 解析、分组渲染、semver 版本推断
和统计（贡献者、改动文件 top10），再（可选）让模型把条目润色成面向用户的描述并生成"亮点"段落，
最后输出中文 Markdown 发布说明，或 Keep a Changelog 格式片段。除 anthropic SDK 外只用标准库。

用法：
    python3 agent.py [选项]

示例：
    python3 agent.py --repo . --from v1.0.0 --to HEAD --version 1.1.0 --output RELEASE_NOTES.md
    python3 agent.py --commits commits.txt --from 1.0.0 --changelog --output CHANGELOG.md
    python3 agent.py --repo . --from v1.0.0 --polish

环境要求：
    1. 模型润色（--polish）需要设置 API key（--offline 与 --self-test 不需要 key）：
           macOS / Linux：      export ANTHROPIC_API_KEY=你的key
           Windows PowerShell： $env:ANTHROPIC_API_KEY="你的key"
       也可以用 ANTHROPIC_AUTH_TOKEN 代替 ANTHROPIC_API_KEY。
    2. 安装依赖：pip3 install -r requirements.txt
    3. 走第三方 Anthropic 兼容网关时，用 --model 指定网关支持的模型名，
       例如 --model qwen3.8-max，并自行设置 ANTHROPIC_BASE_URL。
    4. 从 git 采集提交需要本机装有 git；用 --commits 时不需要 git。
"""

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from collections import Counter, OrderedDict
from datetime import datetime
from pathlib import Path

import anthropic

# 默认模型（可用 --model 参数或 RELEASE_NOTES_MODEL 环境变量更换）
DEFAULT_MODEL = os.environ.get("RELEASE_NOTES_MODEL", "claude-opus-5")

DEFAULT_OUTPUT = "RELEASE_NOTES.md"

# 分组顺序与中文标签（build/ci 归并到"构建"）
GROUP_ORDER = (
    ("feat", "新功能"),
    ("fix", "问题修复"),
    ("perf", "性能"),
    ("refactor", "重构"),
    ("docs", "文档"),
    ("test", "测试"),
    ("build", "构建"),
    ("ci", "构建"),
    ("chore", "杂务"),
    ("style", "样式"),
)
TYPE_LABELS = OrderedDict(GROUP_ORDER)
OTHER_LABEL = "其他"
BREAKING_LABEL = "破坏性变更"

# Keep a Changelog 分类（--changelog 输出用）
CHANGELOG_ORDER = (
    ("新增（Added）", ("feat",)),
    ("修复（Fixed）", ("fix",)),
    ("变更（Changed）", ("perf", "refactor")),
    ("杂项（Miscellaneous）", ("docs", "test", "chore", "build", "ci", "style")),
)

BUMP_LABELS = {"major": "主版本（破坏性变更）", "minor": "次版本（新功能）", "patch": "修订版本（修复/性能）", "none": "无变化"}

# ---------- 正则 ----------

CONVENTIONAL_RE = re.compile(
    r"^(?P<type>[A-Za-z]+)(?:\((?P<scope>[^)]*)\))?(?P<bang>!)?:\s*(?P<subject>.+)$"
)
BREAKING_BODY_RE = re.compile(r"^BREAKING(\s|-)?CHANGE(S)?:", re.MULTILINE | re.IGNORECASE)
VERSION_RE = re.compile(r"^[vV]?(\d+)\.(\d+)\.(\d+)(?:[-+][0-9A-Za-z.\-]+)?$")


# ---------- commit 解析 ----------

def parse_commit_message(message):
    """解析 conventional commit：type(scope)!: subject，返回 type/scope/breaking/subject"""
    text = (message or "").strip()
    first_line, _, body = text.partition("\n")
    first_line = first_line.strip()
    body = body.strip()
    match = CONVENTIONAL_RE.match(first_line)
    breaking = bool(BREAKING_BODY_RE.search(body))
    if not match:
        return {
            "type": None,
            "scope": None,
            "breaking": breaking,
            "subject": first_line,
            "conventional": False,
        }
    return {
        "type": match.group("type").lower(),
        "scope": (match.group("scope") or "").strip() or None,
        "breaking": breaking or bool(match.group("bang")),
        "subject": match.group("subject").strip(),
        "conventional": True,
    }


class Commit(object):
    """一条提交：保留原始信息 + 解析结果"""

    def __init__(self, hash_value, author, message):
        self.hash = (hash_value or "").strip()
        self.author = (author or "").strip() or "未知"
        self.message = (message or "").strip()
        parsed = parse_commit_message(self.message)
        self.type = parsed["type"]
        self.scope = parsed["scope"]
        self.breaking = parsed["breaking"]
        self.conventional = parsed["conventional"]
        self.subject = parsed["subject"]

    @property
    def short_hash(self):
        return self.hash[:7] if self.hash else ""

    @property
    def label(self):
        """所属分组的中文标签"""
        if not self.conventional:
            return OTHER_LABEL
        return TYPE_LABELS.get(self.type, OTHER_LABEL)


def group_commits(commits):
    """按分组标签聚合，顺序固定，"其他"永远在最后，返回 OrderedDict"""
    grouped = OrderedDict()
    for _, label in GROUP_ORDER:
        grouped.setdefault(label, [])
    grouped.setdefault(OTHER_LABEL, [])
    for commit in commits:
        grouped[commit.label].append(commit)
    for label in list(grouped.keys()):
        if not grouped[label]:
            del grouped[label]
    return grouped


# ---------- semver ----------

def parse_version(text):
    """解析 x.y.z（允许 v 前缀），失败返回 None"""
    if not text:
        return None
    match = VERSION_RE.match(text.strip())
    if not match:
        return None
    return (int(match.group(1)), int(match.group(2)), int(match.group(3)))


def bump_version(version, bump):
    """按 major/minor/patch 推进版本号；解析不了就原样返回"""
    parsed = parse_version(version)
    if parsed is None:
        return version
    major, minor, patch = parsed
    if bump == "major":
        return "%d.%d.%d" % (major + 1, 0, 0)
    if bump == "minor":
        return "%d.%d.%d" % (major, minor + 1, 0)
    if bump == "patch":
        return "%d.%d.%d" % (major, minor, patch + 1)
    return "%d.%d.%d" % parsed


def infer_bump(commits):
    """破坏性变更→major，新功能→minor，修复/性能→patch，其余→none"""
    if any(commit.breaking for commit in commits):
        return "major"
    if any(commit.type == "feat" for commit in commits):
        return "minor"
    if any(commit.type in ("fix", "perf") for commit in commits):
        return "patch"
    return "none"


def resolve_version(from_version, version_arg, commits):
    """推断新版本号并做一致性校验，返回 (新版本号, 警告列表)"""
    warnings = []
    bump = infer_bump(commits)
    expected = bump_version(from_version, bump) if from_version else None
    if version_arg:
        new_version = version_arg.strip()
        if expected and parse_version(expected) and parse_version(new_version):
            if parse_version(expected) != parse_version(new_version):
                warnings.append(
                    "版本不一致：--version 给的是 %s，但按 %s 和这批提交推断应为 %s（已按 --version 输出）。"
                    % (new_version, from_version, expected)
                )
    elif expected:
        new_version = expected
        if bump == "none":
            warnings.append("这批提交没有 feat / fix / 破坏性变更，按 semver 无需升版本，仍输出 %s。" % new_version)
    else:
        new_version = ""
        if bump != "none":
            warnings.append("没有给 --from 旧版本号，无法自动推算新版本号，可用 --version 指定。")
    return new_version, warnings


# ---------- 统计 ----------

def build_stats(commits, file_stats=None):
    """提交总数、贡献者分布、类型分布、改动文件 top10"""
    contributors = Counter()
    types = Counter()
    for commit in commits:
        contributors[commit.author] += 1
        types[commit.label] += 1
    files = []
    if file_stats:
        files = sorted(file_stats.items(), key=lambda item: (-item[1], item[0]))[:10]
    return {
        "total": len(commits),
        "contributors": sorted(contributors.items(), key=lambda item: (-item[1], item[0])),
        "types": sorted(types.items(), key=lambda item: (-item[1], item[0])),
        "files": files,
    }


def render_stats(stats):
    """统计附录的 Markdown"""
    lines = []
    lines.append("- 提交总数：%d" % stats["total"])
    type_desc = "、".join("%s %d 条" % (label, count) for label, count in stats["types"])
    lines.append("- 类型分布：%s" % (type_desc or "（无）"))
    lines.append("")
    lines.append("### 贡献者")
    lines.append("")
    if stats["contributors"]:
        lines.append("| 贡献者 | 提交数 |")
        lines.append("| --- | --- |")
        for name, count in stats["contributors"]:
            lines.append("| %s | %d |" % (name, count))
    else:
        lines.append("（无）")
    lines.append("")
    lines.append("### 改动文件 Top10")
    lines.append("")
    if stats["files"]:
        lines.append("| 文件 | 改动行数（增+删） |")
        lines.append("| --- | --- |")
        for path, changes in stats["files"]:
            lines.append("| `%s` | %d |" % (path, changes))
    else:
        lines.append("（文本模式没有文件统计；加 --repo 从 git 采集）")
    return "\n".join(lines)


# ---------- git / 文本采集 ----------

def build_range(from_ref, to_ref):
    """拼 git 区间：v1.0.0..HEAD"""
    to = to_ref or "HEAD"
    if from_ref:
        return "%s..%s" % (from_ref, to)
    return to


def _run_git(repo, args):
    command = ["git", "-C", str(repo)] + args
    try:
        result = subprocess.run(
            command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False
        )
    except OSError as exc:
        raise RuntimeError("无法执行 git（%s），请确认已安装 git，或改用 --commits 文本模式。" % exc)
    if result.returncode != 0:
        detail = decode_bytes(result.stderr).strip() or decode_bytes(result.stdout).strip()
        raise RuntimeError("git 命令失败：%s\n%s" % (" ".join(command), detail))
    return decode_bytes(result.stdout)


def decode_bytes(data):
    for encoding in ("utf-8", "utf-8-sig", "gbk", "latin-1"):
        try:
            return data.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode("utf-8", errors="replace")


def collect_git_commits(repo, from_ref, to_ref):
    """用 git log 采集提交：hash / 作者 / 完整 message"""
    repo_path = Path(repo).expanduser()
    if not repo_path.exists():
        raise RuntimeError("仓库路径不存在：%s" % repo)
    range_spec = build_range(from_ref, to_ref)
    output = _run_git(repo_path, ["log", "--pretty=format:%H%x1f%an%x1f%B%x1e", range_spec])
    commits = []
    for record in output.split("\x1e"):
        record = record.strip("\n")
        if not record.strip():
            continue
        parts = record.split("\x1f", 2)
        if len(parts) < 3:
            continue
        commits.append(Commit(parts[0].strip(), parts[1].strip(), parts[2].strip()))
    return commits


def collect_file_stats(repo, from_ref, to_ref):
    """用 git log --numstat 统计每个文件的改动行数"""
    repo_path = Path(repo).expanduser()
    range_spec = build_range(from_ref, to_ref)
    output = _run_git(repo_path, ["log", "--numstat", "--pretty=format:%x1f%H%x1e", range_spec])
    changes = {}
    for line in output.split("\n"):
        line = line.strip()
        if not line or line.startswith("\x1f"):
            continue
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        added, deleted, path = parts
        if added == "-" or deleted == "-":        # 二进制文件
            value = 1
        else:
            try:
                value = int(added) + int(deleted)
            except ValueError:
                continue
        changes[path] = changes.get(path, 0) + value
    return changes


def load_commits_file(path):
    """从文本读 commit 列表：每行一条 message，空行与 # 开头的行忽略"""
    file_path = Path(path).expanduser()
    if not file_path.exists():
        raise RuntimeError("找不到提交列表文件：%s" % path)
    data = file_path.read_bytes()
    commits = []
    for line in decode_bytes(data).split("\n"):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        commits.append(Commit("", "未知", line))
    return commits


# ---------- 模型调用 ----------

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


POLISH_SYSTEM_PROMPT = """你是一名资深的产品发布说明编辑。用户会给你一批提交记录（已按分组整理），
请把每条改写成面向用户的描述：说清楚"给用户带来什么"，不要写实现细节，不要出现 commit hash。

只输出一个 JSON 对象，不要解释、不要 Markdown 代码围栏，字段为：
- highlights：一段"亮点"介绍，80~160 字，突出本版本最重要的变化
- entries：对象，键与用户给出的分组名完全一致（如 新功能 / 问题修复 / 其它），值是改写后的条目数组，
  每个分组的条目数必须与输入条数一致，顺序也保持一致

全部用中文，不要编造输入里没有的功能。"""


def build_polish_prompt(groups, version):
    """拼装润色提示词：按分组列出原始 subject"""
    parts = ["版本：%s" % (version or "未指定")]
    for label, commits in groups.items():
        parts.append("")
        parts.append("【%s】" % label)
        for index, commit in enumerate(commits, 1):
            parts.append("%d. %s" % (index, commit.subject))
    return "\n".join(parts)


def parse_polish_json(text):
    """尽力把模型回复解析成 {highlights, entries}；解析不出来返回 None"""
    if not text:
        return None
    content = text.strip()
    if content.startswith("```"):
        content = re.sub(r"^```[A-Za-z]*\s*", "", content)
        content = re.sub(r"\s*```$", "", content)
    start = content.find("{")
    end = content.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(content[start:end + 1])
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    highlights = str(data.get("highlights") or "").strip()
    entries = {}
    raw_entries = data.get("entries")
    if isinstance(raw_entries, dict):
        for label, items in raw_entries.items():
            if isinstance(items, list):
                cleaned = [str(item or "").strip() for item in items]
                entries[str(label)] = [item for item in cleaned if item]
    if not highlights and not entries:
        return None
    return {"highlights": highlights, "entries": entries}


def polish_with_model(client, model, groups, version):
    """调模型润色条目，返回 (解析结果或 None, 模型原文)"""
    response = call_model(
        client, model, POLISH_SYSTEM_PROMPT,
        [{"role": "user", "content": build_polish_prompt(groups, version)}],
        max_tokens=6000,
    )
    raw = response_text(response)
    return parse_polish_json(raw), raw


# ---------- 渲染 ----------

MIGRATION_TEMPLATE = (
    "迁移提示：\n"
    "1. 排查调用方是否依赖「{scope_desc}」的旧行为（全仓搜索关键字：{keyword}）；\n"
    "2. 按变更说明调整代码与配置，在测试环境跑一遍回归；\n"
    "3. 升级前备份配置与数据，确认可回滚后再上线。"
)


def render_migration(commit):
    """破坏性变更的迁移提示模板"""
    scope_desc = commit.scope or commit.subject
    keyword = commit.scope or commit.subject[:12]
    return MIGRATION_TEMPLATE.format(scope_desc=scope_desc, keyword=keyword)


def default_highlights(commits, stats, breaking):
    """没走模型时的确定性亮点段落"""
    parts = ["本次共 %d 个提交" % stats["total"]]
    top = stats["types"][:3]
    if top:
        parts.append("其中" + "、".join("%s %d 条" % (label, count) for label, count in top))
    if breaking:
        parts.append("含 %d 项破坏性变更，升级前请务必阅读迁移提示" % len(breaking))
    return "，".join(parts) + "。"


def render_group_entries(label, commits, polished):
    """渲染一个分组：组内按 scope 聚合"""
    lines = []
    lines.append("## %s" % label)
    lines.append("")
    by_scope = OrderedDict()
    for commit in commits:
        by_scope.setdefault(commit.scope or "", []).append(commit)
    polished_items = polished.get(label, []) if polished else []
    flat_index = 0
    for scope, items in by_scope.items():
        if scope:
            lines.append("### %s" % scope)
            lines.append("")
        for commit in items:
            text = commit.subject
            if flat_index < len(polished_items) and polished_items[flat_index]:
                text = polished_items[flat_index]
            flat_index += 1
            suffix = "（`%s`）" % commit.short_hash if commit.short_hash else ""
            lines.append("- %s%s" % (text, suffix))
        lines.append("")
    return lines


def build_release_notes(commits, stats, version, from_ref, to_ref, source_desc,
                        polished, highlights, raw_model_text, warnings, generated_at,
                        model_name, used_model):
    """生成中文 Markdown 发布说明"""
    groups = group_commits(commits)
    breaking = [commit for commit in commits if commit.breaking]
    lines = []
    lines.append("# 版本 %s 发布说明" % (version or "（未指定版本）"))
    lines.append("")
    lines.append(
        "> 生成时间：%s ｜ 来源：%s ｜ 提交 %d 条 ｜ 建议版本级别：%s"
        % (generated_at, source_desc, stats["total"], BUMP_LABELS[infer_bump(commits)])
    )
    if from_ref or to_ref:
        lines.append(">")
        lines.append("> 对比区间：%s .. %s" % (from_ref or "起点", to_ref or "HEAD"))
    lines.append("")

    lines.append("## 亮点")
    lines.append("")
    lines.append(highlights or default_highlights(commits, stats, breaking))
    lines.append("")

    if breaking:
        lines.append("## ⚠️ %s" % BREAKING_LABEL)
        lines.append("")
        lines.append("> 以下改动不向后兼容，升级前请按迁移提示逐条处理。")
        lines.append("")
        for commit in breaking:
            head = "**%s**" % commit.subject
            if commit.scope:
                head += "（%s）" % commit.scope
            if commit.short_hash:
                head += " `%s`" % commit.short_hash
            lines.append("- %s" % head)
            lines.append("")
            lines.append(render_migration(commit))
            lines.append("")

    for label, items in groups.items():
        if label == BREAKING_LABEL:
            continue
        lines.extend(render_group_entries(label, items, polished))

    lines.append("## 贡献者与统计")
    lines.append("")
    lines.append(render_stats(stats))
    lines.append("")

    if warnings:
        lines.append("## 提示")
        lines.append("")
        for warning in warnings:
            lines.append("- %s" % warning)
        lines.append("")

    if raw_model_text and not polished:
        lines.append("## 附录：模型原始输出（未能解析为 JSON）")
        lines.append("")
        lines.append("```text")
        lines.append(raw_model_text)
        lines.append("```")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def build_changelog(commits, stats, version, polished, highlights, generated_at, from_ref, to_ref):
    """生成 Keep a Changelog 格式片段"""
    breaking = [commit for commit in commits if commit.breaking]
    groups = group_commits(commits)
    lines = []
    lines.append("## [%s] - %s" % (version or "未指定版本", generated_at[:10]))
    lines.append("")
    if highlights:
        lines.append(highlights)
        lines.append("")
    if breaking:
        lines.append("### ⚠️ %s" % BREAKING_LABEL)
        lines.append("")
        for commit in breaking:
            lines.append("- %s%s" % (commit.subject, "（`%s`）" % commit.short_hash if commit.short_hash else ""))
        lines.append("")
    for changelog_label, types in CHANGELOG_ORDER:
        items = []
        for type_name in types:
            items.extend(groups.get(TYPE_LABELS[type_name], []))
        if not items:
            continue
        lines.append("### %s" % changelog_label)
        lines.append("")
        polished_items = polished.get(changelog_label, []) if polished else []
        for index, commit in enumerate(items):
            text = commit.subject
            if index < len(polished_items) and polished_items[index]:
                text = polished_items[index]
            lines.append("- %s%s" % (text, "（`%s`）" % commit.short_hash if commit.short_hash else ""))
        lines.append("")
    others = groups.get(OTHER_LABEL, [])
    if others:
        lines.append("### %s" % OTHER_LABEL)
        lines.append("")
        for commit in others:
            lines.append("- %s%s" % (commit.subject, "（`%s`）" % commit.short_hash if commit.short_hash else ""))
        lines.append("")
    lines.append("<!-- 提交总数：%d，区间：%s .. %s -->" % (stats["total"], from_ref or "起点", to_ref or "HEAD"))
    return "\n".join(lines).rstrip() + "\n"


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

        # --- conventional commits 解析 ---
        parsed = parse_commit_message("feat(api)!: 添加批量导出接口")
        assert parsed["type"] == "feat" and parsed["scope"] == "api"
        assert parsed["breaking"] is True and parsed["subject"] == "添加批量导出接口"
        assert parsed["conventional"] is True

        parsed = parse_commit_message("fix: 修复登录偶发崩溃")
        assert parsed["type"] == "fix" and parsed["scope"] is None
        assert parsed["breaking"] is False and parsed["subject"] == "修复登录偶发崩溃"

        parsed = parse_commit_message("把 README 改漂亮了")
        assert parsed["conventional"] is False and parsed["type"] is None
        assert parsed["subject"] == "把 README 改漂亮了" and parsed["breaking"] is False

        parsed = parse_commit_message("refactor(core): 重构调度器\n\nBREAKING CHANGE: 删除旧版调度接口")
        assert parsed["type"] == "refactor" and parsed["scope"] == "core"
        assert parsed["breaking"] is True, "正文 BREAKING CHANGE: 应被识别"

        parsed = parse_commit_message("feat!: 全面调整配置格式")
        assert parsed["breaking"] is True and parsed["scope"] is None

        parsed = parse_commit_message("chore(deps): 升级依赖")
        assert parsed["type"] == "chore" and parsed["scope"] == "deps"
        assert parsed["conventional"] is True and parsed["breaking"] is False

        # --- Commit 对象与分组 ---
        commits = [
            Commit("aaa1111", "张三", "feat(api): 添加批量导出接口"),
            Commit("bbb2222", "李四", "feat: 支持暗色主题"),
            Commit("ccc3333", "张三", "fix: 修复登录偶发崩溃"),
            Commit("ddd4444", "王五", "docs(readme): 更新安装说明"),
            Commit("eee5555", "王五", "把 README 改漂亮了"),
            Commit("fff6666", "张三", "refactor(core): 重构调度器\n\nBREAKING CHANGE: 删除旧版调度接口"),
        ]
        assert commits[0].label == "新功能" and commits[0].short_hash == "aaa1111"
        assert commits[4].label == "其他"
        assert commits[5].breaking is True and commits[5].label == "重构"
        groups = group_commits(commits)
        labels = list(groups.keys())
        assert labels == ["新功能", "问题修复", "重构", "文档", "其他"], labels
        assert len(groups["新功能"]) == 2 and len(groups["其他"]) == 1

        # --- semver 推断与版本一致性 ---
        assert bump_version("1.2.3", "major") == "2.0.0"
        assert bump_version("1.2.3", "minor") == "1.3.0"
        assert bump_version("v1.2.3", "patch") == "1.2.4"
        assert bump_version("1.2.3", "none") == "1.2.3"
        assert parse_version("v2.0.0-rc.1") == (2, 0, 0)
        assert parse_version("不是版本") is None
        assert infer_bump(commits) == "major"
        assert infer_bump([commits[0]]) == "minor"
        assert infer_bump([commits[2]]) == "patch"
        assert infer_bump([commits[3]]) == "none"

        version, warnings = resolve_version("1.0.0", None, [commits[0]])
        assert version == "1.1.0", version
        assert not warnings
        version, warnings = resolve_version("1.0.0", "9.9.9", [commits[0]])
        assert version == "9.9.9" and warnings and "版本不一致" in warnings[0]
        version, warnings = resolve_version("1.0.0", None, [commits[3]])
        assert version == "1.0.0" and warnings and "无需升版本" in warnings[0]
        version, warnings = resolve_version(None, None, [commits[0]])
        assert version == "" and warnings and "无法自动推算" in warnings[0]

        # --- 统计 ---
        stats = build_stats(commits, {"src/a.py": 120, "src/b.py": 30, "README.md": 5})
        assert stats["total"] == 6
        contributors = dict(stats["contributors"])
        assert contributors["张三"] == 3 and contributors["王五"] == 2
        assert stats["files"][0] == ("src/a.py", 120) and len(stats["files"]) == 3
        stats_text = render_stats(stats)
        assert "提交总数：6" in stats_text and "src/a.py" in stats_text
        assert "贡献者" in stats_text and "改动文件 Top10" in stats_text

        # --- 发布说明渲染 ---
        generated_at = "2026-01-01 00:00:00"
        report = build_release_notes(
            commits, stats, "1.1.0", "1.0.0", "HEAD", "git 仓库 .（1.0.0..HEAD）",
            {}, "", "", [], generated_at, DEFAULT_MODEL, False,
        )
        assert report.startswith("# 版本 1.1.0 发布说明")
        for heading in ("## 亮点", "## ⚠️ 破坏性变更", "## 新功能", "## 问题修复",
                        "## 重构", "## 文档", "## 其他", "## 贡献者与统计"):
            assert heading in report, "发布说明缺少：%s" % heading
        assert "### api" in report, "分组内应按 scope 聚合"
        assert "（`aaa1111`）" in report
        assert "迁移提示" in report and "BREAKING" not in report
        assert "提交总数：6" in report

        # --- Keep a Changelog 片段 ---
        changelog = build_changelog(
            commits, stats, "1.1.0", {}, "亮点说明文字", generated_at, "1.0.0", "HEAD"
        )
        assert "## [1.1.0] - 2026-01-01" in changelog
        assert "### 新增（Added）" in changelog and "### 修复（Fixed）" in changelog
        assert "### ⚠️ 破坏性变更" in changelog
        assert "支持暗色主题" in changelog and "其他" in changelog
        assert "亮点说明文字" in changelog

        # --- 模型润色结果合并（含 JSON 解析兜底） ---
        good = (
            "```json\n"
            '{"highlights":"本次亮点是批量导出与暗色主题。",'
            '"entries":{"新功能":["批量导出一次搞定","夜间模式上线"]}}\n'
            "```"
        )
        parsed_polish = parse_polish_json(good)
        assert parsed_polish["highlights"].startswith("本次亮点")
        assert parsed_polish["entries"]["新功能"] == ["批量导出一次搞定", "夜间模式上线"]
        assert parse_polish_json("这不是 JSON") is None
        assert parse_polish_json("{}") is None
        assert parse_polish_json("") is None

        polished_report = build_release_notes(
            commits, stats, "1.1.0", "1.0.0", "HEAD", "文本文件 commits.txt",
            parsed_polish["entries"], parsed_polish["highlights"], "", [],
            generated_at, DEFAULT_MODEL, True,
        )
        assert "批量导出一次搞定" in polished_report
        assert "本次亮点是批量导出与暗色主题。" in polished_report

        raw_report = build_release_notes(
            commits, stats, "1.1.0", "1.0.0", "HEAD", "文本文件 commits.txt",
            {}, "", "自由格式的模型回复", [], generated_at, DEFAULT_MODEL, False,
        )
        assert "附录：模型原始输出（未能解析为 JSON）" in raw_report
        assert "自由格式的模型回复" in raw_report

        # --- --commits 文本模式 ---
        commits_file = root / "commits.txt"
        commits_file.write_text(
            "# 注释行会被忽略\n"
            "feat(ui): 新增暗色主题\n"
            "\n"
            "fix: 修复闪退\n"
            "随手写的提交说明\n",
            encoding="utf-8",
        )
        text_commits = load_commits_file(str(commits_file))
        assert len(text_commits) == 3
        assert text_commits[0].type == "feat" and text_commits[0].scope == "ui"
        assert text_commits[2].label == "其他" and text_commits[2].author == "未知"
        assert infer_bump(text_commits) == "minor"
        text_report = build_release_notes(
            text_commits, build_stats(text_commits), "1.1.0", "1.0.0", "HEAD",
            "文本文件 commits.txt", {}, "", "", [], generated_at, DEFAULT_MODEL, False,
        )
        assert "新增暗色主题" in text_report and "## 新功能" in text_report
        assert "（文本模式没有文件统计" in text_report

        # --- call_model 逐级降级（离线假客户端） ---
        fake = _FakeClient()
        result = call_model(fake, "fake-model", "system", [{"role": "user", "content": "hi"}])
        assert result["ok"] is True
        assert fake.state["calls"] == 2

        # --- 落盘 ---
        output_path = root / "RELEASE_NOTES.md"
        output_path.write_text(report, encoding="utf-8")
        assert output_path.read_text(encoding="utf-8").startswith("# 版本 1.1.0 发布说明")

    print("SELF-TEST OK")


# ---------- CLI ----------

def build_parser():
    parser = argparse.ArgumentParser(
        description="发布说明生成助手：解析 conventional commits，生成中文 Release Notes 或 Keep a Changelog 片段",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            "  python3 agent.py --repo . --from v1.0.0 --to HEAD --version 1.1.0\n"
            "  python3 agent.py --commits commits.txt --from 1.0.0 --changelog\n"
            "  python3 agent.py --repo . --from v1.0.0 --polish --model qwen3.8-max\n"
            "  python3 agent.py --commits commits.txt --offline\n"
        ),
    )
    parser.add_argument("--repo", default=".", help="git 仓库路径（默认当前目录）")
    parser.add_argument("--commits", metavar="文件", help="从文本文件读 commit 列表（每行一条 message），给了它就不读 git")
    parser.add_argument("--from", dest="from_ref", metavar="版本", help="起始版本或 tag，如 v1.0.0（用于推算新版本号）")
    parser.add_argument("--to", dest="to_ref", metavar="版本", default="HEAD", help="结束版本、tag 或 HEAD（默认 HEAD）")
    parser.add_argument("--version", metavar="版本", help="本次发布版本号；与 --from 推断结果不一致时会告警")
    parser.add_argument("--output", default=DEFAULT_OUTPUT, help="输出文件路径（默认 %s）" % DEFAULT_OUTPUT)
    parser.add_argument("--changelog", action="store_true", help="按 Keep a Changelog 格式输出片段（默认输出完整发布说明）")
    parser.add_argument("--polish", action="store_true", help="调用模型润色条目并生成亮点段落（需要 API key）")
    parser.add_argument("--offline", action="store_true", help="纯确定性模式：解析、分组、统计都本地完成，不调用模型")
    parser.add_argument("--model", default=DEFAULT_MODEL, help="润色使用的模型（默认 %s）" % DEFAULT_MODEL)
    parser.add_argument("--self-test", action="store_true", help="离线自检：不联网、不需要 API key")
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

    warnings = []
    file_stats = {}
    if args.commits:
        try:
            commits = load_commits_file(args.commits)
        except (RuntimeError, OSError) as exc:
            print("错误：%s" % exc, file=sys.stderr)
            sys.exit(1)
        source_desc = "文本文件 %s" % args.commits
    else:
        source_desc = "git 仓库 %s（%s .. %s）" % (args.repo, args.from_ref or "起点", args.to_ref or "HEAD")
        try:
            commits = collect_git_commits(args.repo, args.from_ref, args.to_ref)
            file_stats = collect_file_stats(args.repo, args.from_ref, args.to_ref)
        except RuntimeError as exc:
            print("错误：%s" % exc, file=sys.stderr)
            sys.exit(1)

    if not commits:
        print("错误：没有采集到任何提交（请检查区间 %s .. %s 是否正确）。"
              % (args.from_ref or "起点", args.to_ref or "HEAD"), file=sys.stderr)
        sys.exit(1)

    version, version_warnings = resolve_version(args.from_ref, args.version, commits)
    warnings.extend(version_warnings)
    stats = build_stats(commits, file_stats)

    print("[采集] 来源：%s" % source_desc)
    print("[解析] 提交 %d 条，其中破坏性变更 %d 条，建议版本级别：%s"
          % (stats["total"], sum(1 for c in commits if c.breaking), BUMP_LABELS[infer_bump(commits)]))

    polished = {}
    highlights = ""
    raw_model_text = ""
    used_model = False
    if args.polish and args.offline:
        print("[提示] --offline 模式忽略 --polish，条目保持原始提交说明")
    elif args.polish:
        if not os.environ.get("ANTHROPIC_API_KEY") and not os.environ.get("ANTHROPIC_AUTH_TOKEN"):
            print(
                "提示：没有检测到 ANTHROPIC_API_KEY 或 ANTHROPIC_AUTH_TOKEN，无法调用模型润色。\n"
                "请先设置：\n"
                "  macOS / Linux：      export ANTHROPIC_API_KEY=你的key\n"
                "  Windows PowerShell： $env:ANTHROPIC_API_KEY=\"你的key\"\n"
                "也可以加 --offline 跳过模型，直接输出确定性结果。",
                file=sys.stderr,
            )
            sys.exit(1)
        print("[模型] %s 润色中……" % args.model)
        groups = group_commits(commits)
        try:
            result, raw_model_text = polish_with_model(
                anthropic.Anthropic(), args.model, groups, version
            )
        except anthropic.AuthenticationError:
            print("错误：API key 无效或没有权限，请检查 ANTHROPIC_API_KEY。", file=sys.stderr)
            sys.exit(1)
        except anthropic.APIConnectionError:
            print("错误：无法连接模型服务，请检查网络或 ANTHROPIC_BASE_URL。", file=sys.stderr)
            sys.exit(1)
        except anthropic.RateLimitError:
            print("错误：请求太频繁被限流，请稍后再试。", file=sys.stderr)
            sys.exit(1)
        except anthropic.APIStatusError as exc:
            print("错误：API 返回错误（%s）：%s" % (exc.status_code, exc.message), file=sys.stderr)
            sys.exit(1)
        if result:
            polished = result["entries"]
            highlights = result["highlights"]
            used_model = True
            print("[模型] 润色完成")
        else:
            print("[模型] 返回内容不是合法 JSON，已回退到原始条目（原文进报告附录）")

    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    if args.changelog:
        report = build_changelog(
            commits, stats, version, polished, highlights,
            generated_at, args.from_ref, args.to_ref,
        )
    else:
        report = build_release_notes(
            commits, stats, version, args.from_ref, args.to_ref, source_desc,
            polished, highlights, raw_model_text, warnings, generated_at,
            args.model, used_model,
        )

    output_path = Path(args.output).expanduser()
    try:
        if output_path.parent and not output_path.parent.exists():
            output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(report, encoding="utf-8")
    except OSError as exc:
        print("错误：报告写入失败：%s" % exc, file=sys.stderr)
        sys.exit(1)

    for warning in warnings:
        print("[警告] %s" % warning)
    print("[完成] %s 已写入：%s" % ("Changelog 片段" if args.changelog else "发布说明", output_path))
    print("[完成] 版本：%s，提交 %d 条，分组 %d 个%s"
          % (version or "未指定", stats["total"], len(group_commits(commits)),
             "，已润色" if used_model else ""))


if __name__ == "__main__":
    main()
