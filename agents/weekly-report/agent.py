#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
周报/日报生成助手 —— 从 git 提交与工作记录生成中文周报

先做完全确定性的数据采集与统计：git log 解析 commit（hash/日期/作者/说明/改动文件
与增删行数），按 conventional 类型分类、按顶层目录聚合模块、统计代码增删总量与活跃
天数分布；再解析工作记录 markdown（按日期标题分组、抽取勾选状态），与 commit 按日期
合并去重；最后让模型把这些数据写成正式周报，输出中文 Markdown 文件。
除 anthropic SDK 外只用标准库。

用法：
    python3 agent.py [选项]

示例：
    python3 agent.py --repo ~/projects/myapp --days 7 --output 周报.md
    python3 agent.py --repo . --notes 工作记录.md --type weekly
    python3 agent.py --notes 工作记录.md --offline          # 只用工作记录出骨架
    python3 agent.py --repo . --model qwen3.8-max           # 走第三方兼容网关

环境要求：
    1. 需要模型的路径（不带 --offline）要设置 API key：
           export ANTHROPIC_API_KEY=你的key        # macOS / Linux
           $env:ANTHROPIC_API_KEY="你的key"         # Windows PowerShell
       也支持 ANTHROPIC_AUTH_TOKEN。--offline 与 --self-test 不需要 key、不联网。
    2. 安装 SDK：
           pip3 install -r requirements.txt
    3. 走第三方兼容网关时（如阿里云等），用 --model 指定网关认得的模型名，
       例如 --model qwen3.8-max；代码里的 call_model() 会自动逐级降级参数。
    4. git 采集需要本机安装 git；没有 git 或没有仓库时可以只用 --notes。
"""

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

import anthropic

# 默认模型（可用 --model 参数或 WEEKLY_REPORT_MODEL 环境变量更换）
DEFAULT_MODEL = os.environ.get("WEEKLY_REPORT_MODEL", "claude-opus-5")
DEFAULT_OUTPUT = "weekly-report.md"
DEFAULT_DAYS = 7
MAX_ITEMS = 50            # 单个清单最多列多少条
MAX_NOTES_CHARS = 8000    # 发给模型的工作记录上限

COMMIT_TYPES = ("feat", "fix", "docs", "refactor", "test", "chore", "其他")
TYPE_LABELS = {
    "feat": "新功能",
    "fix": "缺陷修复",
    "docs": "文档",
    "refactor": "重构/优化",
    "test": "测试",
    "chore": "杂项",
    "其他": "其他",
}

# ---------- 正则与词表 ----------

NUMSTAT_RE = re.compile(r"^(\d+|-)\t(\d+|-)\t(.+)$")
CONVENTIONAL_RE = re.compile(r"^(feat|fix|docs|refactor|test|chore)(\([^)]*\))?!?\s*[:：]", re.I)
STRIP_PREFIX_RE = re.compile(
    r"^(?:feat|fix|docs|refactor|test|chore|style|perf|build|ci|chore)"
    r"(?:\([^)]*\))?!?\s*[:：]\s*",
    re.I,
)
DATE_IN_TEXT_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
# 提交说明开头的 issue 编号 / 标签前缀，关键词分类前先去掉
ISSUE_PREFIX_RE = re.compile(r"^(?:#\d+[\s:：、\-]*|【[^】]{0,12}】\s*|\[[^\]]{0,12}\]\s*)")

# 中文提交说明的常见开头 → 类型
TYPE_KEYWORDS = (
    ("feat", ("新增", "添加", "实现", "支持", "接入")),
    ("fix", ("修复", "修正", "解决", "处理")),
    ("docs", ("文档", "注释", "补文档", "更新文档")),
    ("refactor", ("重构", "优化", "调整", "简化")),
    ("test", ("测试", "用例", "单测")),
    ("chore", ("杂项", "依赖", "构建", "升级", "发版", "清理")),
)

WEEKDAY_CHARS = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6}

HEAD_FULL_DATE_RE = re.compile(r"^\s*#{1,6}\s*(\d{4})[-/.年](\d{1,2})[-/.月](\d{1,2})日?\s*$")
HEAD_SHORT_DATE_RE = re.compile(r"^\s*#{1,6}\s*(\d{1,2})[./](\d{1,2})\s*$")
HEAD_WEEKDAY_RE = re.compile(r"^\s*#{1,6}\s*(?:周|星期)([一二三四五六日天])\s*$")
CHECKBOX_RE = re.compile(r"^\s*[-*+]\s*\[([ xX])\]\s*(.+)$")
BULLET_RE = re.compile(r"^\s*[-*+]\s+(.+)$")

IN_PROGRESS_WORDS = ("进行中", "在做", "未完成", "WIP", "wip")


# ---------- git 采集 ----------

def run_git(cmd, cwd):
    """执行 git 命令，返回 CompletedProcess（输出统一按 UTF-8 解码）"""
    return subprocess.run(
        cmd, cwd=str(cwd), capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    )


def git_available():
    """本机是否可用 git"""
    try:
        proc = subprocess.run(["git", "--version"], capture_output=True)
        return proc.returncode == 0
    except OSError:
        return False


def parse_git_log(output):
    """解析 `git log --numstat --pretty=format:%H%x1f%ad%x1f%an%x1f%s%x1f%b` 的输出"""
    commits = []
    current = None
    for line in (output or "").splitlines():
        if "\x1f" in line:
            parts = line.split("\x1f")
            current = {
                "hash": parts[0].strip(),
                "date": parts[1].strip() if len(parts) > 1 else "",
                "author": parts[2].strip() if len(parts) > 2 else "",
                "subject": parts[3].strip() if len(parts) > 3 else "",
                "body": parts[4].strip() if len(parts) > 4 else "",
                "files": [],
                "added": 0,
                "deleted": 0,
            }
            commits.append(current)
            continue
        if current is None:
            continue
        match = NUMSTAT_RE.match(line)
        if match:
            added = 0 if match.group(1) == "-" else int(match.group(1))
            deleted = 0 if match.group(2) == "-" else int(match.group(2))
            current["files"].append({
                "path": match.group(3).strip(),
                "added": added,
                "deleted": deleted,
            })
            current["added"] += added
            current["deleted"] += deleted
        elif line.strip() and not NUMSTAT_RE.match(line):
            # %b 里的多行说明会续在下面
            if len(current["body"]) < 400:
                current["body"] = (current["body"] + " " + line.strip()).strip()
    return commits


def collect_git_commits(repo, days=None, author=None):
    """采集指定仓库最近 N 天的 commit，返回解析好的列表"""
    cmd = [
        "git", "log", "--numstat", "--date=iso",
        "--pretty=format:%H%x1f%ad%x1f%an%x1f%s%x1f%b",
    ]
    if days:
        cmd.append("--since=%d days ago" % days)
    if author:
        cmd.append("--author=%s" % author)
    proc = run_git(cmd, repo)
    if proc.returncode != 0:
        stderr = proc.stderr.strip()
        if "does not have any commits" in stderr or "unknown revision" in stderr:
            return []
        raise RuntimeError(stderr or "git log 执行失败")
    return parse_git_log(proc.stdout)


def parse_commit_lines(text):
    """从文本解析 commit 列表（与 parse_git_log 同构）。

    每行一条：`日期 | 提交说明 | +新增 -删除 | 文件列表（逗号/空格分隔）`，
    除提交说明外各字段都可省略；'#' 开头是注释行、空行跳过。
    格式不严格的行尽量宽容解析（缺日期归入"未标注日期"，缺统计按 0 计）。
    """
    commits = []
    for raw_line in (text or "").lstrip("\ufeff").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        fields = [field.strip() for field in line.split("|")]
        head = fields[0]
        date_match = DATE_IN_TEXT_RE.search(head)
        date = date_match.group(0) if date_match else ""
        if len(fields) > 1:
            subject = fields[1]
        else:
            subject = head
            if date_match:
                subject = (head[:date_match.start()] + " " + head[date_match.end():]).strip()
        if not subject:
            continue

        added = deleted = 0
        if len(fields) > 2 and fields[2]:
            plus = re.search(r"\+(\d+)", fields[2])
            minus = re.search(r"\D-(\d+)", fields[2]) or re.search(r"^-(\d+)", fields[2])
            numbers = re.findall(r"\d+", fields[2])
            if plus or minus:
                added = int(plus.group(1)) if plus else 0
                deleted = int(minus.group(1)) if minus else 0
            elif numbers:
                added = int(numbers[0])
                deleted = int(numbers[1]) if len(numbers) > 1 else 0

        paths = []
        if len(fields) > 3 and fields[3]:
            paths = [item for item in re.split(r"[,，;；\s]+", fields[3]) if item]
        files = []
        if paths:
            per_added, rest_added = added // len(paths), added % len(paths)
            per_deleted, rest_deleted = deleted // len(paths), deleted % len(paths)
            for index, path in enumerate(paths):
                files.append({
                    "path": path,
                    "added": per_added + (rest_added if index == 0 else 0),
                    "deleted": per_deleted + (rest_deleted if index == 0 else 0),
                })

        commits.append({
            "hash": "",
            "date": date,
            "author": "",
            "subject": subject,
            "body": "",
            "files": files,
            "added": added,
            "deleted": deleted,
        })
    return commits


# ---------- 统计与分类 ----------

def classify_commit(subject):
    """conventional 提交类型分类：feat/fix/docs/refactor/test/chore/其他"""
    match = CONVENTIONAL_RE.match(subject or "")
    if match:
        return match.group(1).lower()
    text = ISSUE_PREFIX_RE.sub("", (subject or "").strip())
    for type_name, keywords in TYPE_KEYWORDS:
        for keyword in keywords:
            if text.startswith(keyword) or text.lower().startswith(keyword):
                return type_name
    return "其他"


def module_of(path):
    """按顶层目录聚合模块；根目录下的文件归入'根目录'"""
    cleaned = (path or "").strip().replace("\\", "/")
    if "/" in cleaned:
        return cleaned.split("/", 1)[0]
    return "根目录"


def commit_day(commit):
    """从 commit 日期里取 YYYY-MM-DD"""
    match = DATE_IN_TEXT_RE.search(commit.get("date", "") or "")
    return match.group(0) if match else ""


def aggregate_git(commits):
    """git 数据汇总：分类、模块、代码量、活跃天数分布"""
    stats = {
        "count": len(commits),
        "added": 0,
        "deleted": 0,
        "files": 0,
        "types": dict((name, 0) for name in COMMIT_TYPES),
        "type_details": dict((name, []) for name in COMMIT_TYPES),
        "modules": {},
        "days": {},
    }
    for commit in commits:
        type_name = classify_commit(commit["subject"])
        stats["types"][type_name] += 1
        stats["type_details"][type_name].append(commit["subject"])
        stats["added"] += commit["added"]
        stats["deleted"] += commit["deleted"]
        stats["files"] += len(commit["files"])
        for item in commit["files"]:
            module = module_of(item["path"])
            entry = stats["modules"].setdefault(
                module, {"commits": 0, "added": 0, "deleted": 0, "files": 0}
            )
            entry["added"] += item["added"]
            entry["deleted"] += item["deleted"]
            entry["files"] += 1
        day = commit_day(commit)
        if day:
            stats["days"][day] = stats["days"].get(day, 0) + 1
    # 模块的 commit 数按"该 commit 是否动过该模块"计
    for commit in commits:
        touched = set(module_of(item["path"]) for item in commit["files"])
        for module in touched:
            if module in stats["modules"]:
                stats["modules"][module]["commits"] += 1
    stats["active_days"] = len(stats["days"])
    return stats


# ---------- 工作记录解析 ----------

def _weekday_date(weekday, reference_date):
    """把'周X'映射到参考日期往前最近的那一天"""
    for back in range(0, 7):
        candidate = reference_date - timedelta(days=back)
        if candidate.weekday() == weekday:
            return candidate
    return reference_date


def parse_notes(text, reference_date=None):
    """解析工作记录 markdown：
    - 按日期标题分组：## 2026-10-27 / ## 10.27 / ## 周一
    - 抽取条目与勾选状态：- [x] 已完成 / - [ ] 未完成
    """
    reference_date = reference_date or datetime.now().date()
    groups = []
    current = None
    year = reference_date.year
    for raw_line in (text or "").lstrip("\ufeff").splitlines():
        line = raw_line.rstrip()
        if not line.strip():
            continue
        head_date = None
        label = None
        match = HEAD_FULL_DATE_RE.match(line)
        if match:
            try:
                head_date = datetime(int(match.group(1)), int(match.group(2)), int(match.group(3))).date()
            except ValueError:
                head_date = None
            label = line.strip("# ").strip()
        if head_date is None:
            match = HEAD_SHORT_DATE_RE.match(line)
            if match:
                try:
                    head_date = datetime(year, int(match.group(1)), int(match.group(2))).date()
                except ValueError:
                    head_date = None
                label = line.strip("# ").strip()
        if head_date is None:
            match = HEAD_WEEKDAY_RE.match(line)
            if match:
                head_date = _weekday_date(WEEKDAY_CHARS[match.group(1)], reference_date)
                label = line.strip("# ").strip()
        if head_date is not None or (line.lstrip().startswith("#") and not CHECKBOX_RE.match(line)
                                     and not BULLET_RE.match(line)):
            current = {
                "label": label or line.strip("# ").strip() or "（未命名分组）",
                "date": head_date.isoformat() if head_date else "",
                "items": [],
            }
            groups.append(current)
            continue
        match = CHECKBOX_RE.match(line)
        if match:
            item = {"text": match.group(2).strip(), "done": match.group(1).lower() == "x"}
            if current is None:
                current = {"label": "（未分组）", "date": "", "items": []}
                groups.append(current)
            current["items"].append(item)
            continue
        match = BULLET_RE.match(line)
        if match:
            item = {"text": match.group(1).strip(), "done": None}
            if current is None:
                current = {"label": "（未分组）", "date": "", "items": []}
                groups.append(current)
            current["items"].append(item)
    # 没有任何条目的标题行（比如文件大标题）不算分组
    groups = [group for group in groups if group["items"]]
    done = [item["text"] for group in groups for item in group["items"] if item["done"] is True]
    undone = [item["text"] for group in groups for item in group["items"] if item["done"] is False]
    unlabeled = [item["text"] for group in groups for item in group["items"] if item["done"] is None]
    return {
        "groups": groups,
        "done": done,
        "undone": undone,
        "unlabeled": unlabeled,
    }


# ---------- 合并去重 ----------

def normalize_item(text):
    """条目归一化：去类型前缀、去标点空白、转小写，用于去重比较"""
    text = STRIP_PREFIX_RE.sub("", (text or "").strip())
    return re.sub(r"[^\w\u4e00-\u9fa5]+", "", text).lower()


def merge_by_day(commits, notes):
    """git commit 说明与工作记录条目按日期归到同一天，并按归一化文本去重"""
    days = {}
    for commit in commits:
        day = commit_day(commit) or "未标注日期"
        entry = days.setdefault(day, {"git": [], "notes": []})
        entry["git"].append(commit["subject"])

    seen = set(normalize_item(commit["subject"]) for commit in commits)
    merged_away = []
    for group in notes["groups"]:
        day = group["date"] or "未标注日期"
        entry = days.setdefault(day, {"git": [], "notes": []})
        for item in group["items"]:
            key = normalize_item(item["text"])
            duplicate = False
            if key and key in seen:
                duplicate = True
            else:
                for existing in seen:
                    if key and len(key) >= 4 and (key in existing or existing in key):
                        duplicate = True
                        break
            if duplicate:
                merged_away.append(item["text"])
                continue
            entry["notes"].append(item["text"])
            if key:
                seen.add(key)
    return dict(sorted(days.items())), merged_away


# ---------- 骨架周报（离线/兜底） ----------

def skeleton_analysis(git_stats, notes):
    """纯确定性骨架：本周完成=commit 分类清单，下周计划=工作记录未勾选项"""
    done = []
    for type_name in COMMIT_TYPES:
        subjects = git_stats["type_details"].get(type_name) or []
        if not subjects:
            continue
        done.append("【%s】%s" % (TYPE_LABELS[type_name], "；".join(subjects[:MAX_ITEMS])))
    if not done and notes["done"]:
        done = ["【已完成】%s" % text for text in notes["done"][:MAX_ITEMS]]
    summary = (
        "本周期共 %d 次提交，新增 %d 行、删除 %d 行，覆盖 %d 个文件，活跃 %d 天；"
        "工作记录里已完成 %d 项、未完成 %d 项。"
        % (
            git_stats["count"], git_stats["added"], git_stats["deleted"],
            git_stats["files"], git_stats["active_days"],
            len(notes["done"]), len(notes["undone"]),
        )
    )
    in_progress = [
        text for text in notes["unlabeled"] + notes["undone"]
        if any(word in text for word in IN_PROGRESS_WORDS)
    ]
    return {
        "summary": summary,
        "done": done,
        "in_progress": in_progress,
        "next_plan": notes["undone"][:MAX_ITEMS],
        "risks": [],
        "help_needed": [],
    }


# ---------- 模型生成 ----------

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


REPORT_SYSTEM_PROMPT = """你是一名专业的研发团队成员，要写一份给主管看的中文%s。
用户会给出确定性统计数据（git 提交分类、模块分布、每日活跃）和工作记录。
只输出一个 JSON 对象，不要解释文字，不要 Markdown 代码围栏：
{
  "summary": "3-5 句概述本周期整体进展",
  "done": ["已完成事项，逐条，写结果和价值"],
  "in_progress": ["进行中事项，逐条"],
  "next_plan": ["下周期计划，逐条，可执行"],
  "risks": ["风险与阻塞，没有就留空数组"],
  "help_needed": ["需要的支持，没有就留空数组"]
}
要求：只依据给出的数据写，不要编造没提到的工作；语言正式、简洁、突出结果。"""


def build_prompt(git_stats, notes, merged_days, report_type, period):
    """拼模型提示词：统计数据 + 工作记录 + 按日合并明细"""
    type_lines = []
    for type_name in COMMIT_TYPES:
        if git_stats["types"].get(type_name):
            type_lines.append("- %s：%d 次" % (TYPE_LABELS[type_name], git_stats["types"][type_name]))
    day_lines = ["- %s：%d 次提交" % (day, count) for day, count in sorted(git_stats["days"].items())]
    merged_lines = []
    for day, entry in sorted(merged_days.items()):
        for subject in entry["git"]:
            merged_lines.append("- %s [git] %s" % (day, subject))
        for text in entry["notes"]:
            merged_lines.append("- %s [记录] %s" % (day, text))
    notes_text = "\n".join(
        "- [%s] %s" % ("x" if item["done"] else " ", item["text"])
        for group in notes["groups"] for item in group["items"]
    )
    parts = [
        "【报告类型】%s（%s）" % ("周报" if report_type == "weekly" else "日报", period),
        "",
        "【git 统计】提交 %d 次；新增 %d 行、删除 %d 行；活跃天数 %d"
        % (git_stats["count"], git_stats["added"], git_stats["deleted"], git_stats["active_days"]),
        "分类：" + ("；".join(type_lines) or "无"),
        "模块：" + ("；".join(
            "%s（%d 次提交，+%d/-%d）" % (name, entry["commits"], entry["added"], entry["deleted"])
            for name, entry in sorted(git_stats["modules"].items())) or "无"),
        "每日活跃：" + ("；".join(day_lines) or "无"),
        "",
        "【工作记录】",
        notes_text or "（无）",
        "",
        "【按日合并明细】",
        "\n".join(merged_lines) or "（无）",
        "",
        "只输出 JSON 对象。",
    ]
    return "\n".join(parts)


def response_text(response):
    """提取模型响应里的文本块"""
    parts = []
    for block in getattr(response, "content", []) or []:
        if getattr(block, "type", "") == "text":
            parts.append(getattr(block, "text", ""))
    return "\n".join(parts).strip()


def parse_model_json(text):
    """尽力把模型回复解析成周报 JSON 对象；解析不出来返回 None（调用方负责兜底）"""
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

    def as_list(key):
        raw = data.get(key)
        if not isinstance(raw, list):
            return []
        return [str(item).strip() for item in raw if str(item).strip()]

    return {
        "summary": str(data.get("summary") or "").strip(),
        "done": as_list("done"),
        "in_progress": as_list("in_progress"),
        "next_plan": as_list("next_plan"),
        "risks": as_list("risks"),
        "help_needed": as_list("help_needed"),
    }


def analyze_with_model(git_stats, notes, merged_days, model, report_type, period):
    """调用模型生成周报内容，返回 (analysis, raw_text 或 None)"""
    client = anthropic.Anthropic()
    prompt = build_prompt(git_stats, notes, merged_days, report_type, period)
    system = REPORT_SYSTEM_PROMPT % ("周报" if report_type == "weekly" else "日报")
    response = call_model(
        client,
        model,
        system,
        [{"role": "user", "content": prompt}],
        max_tokens=6000,
    )
    text = response_text(response)
    parsed = parse_model_json(text)
    if parsed is None:
        return None, text or "（模型没有返回文本）"
    return parsed, None


# ---------- 报告渲染 ----------

def render_list(items, empty="（无）"):
    if not items:
        return empty
    return "\n".join("%d. %s" % (index, item) for index, item in enumerate(items, 1))


def build_report(analysis, git_stats, notes, merged_days, meta, raw_output=None, generated_at=None):
    """生成中文 Markdown 周报/日报"""
    generated_at = generated_at or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    title = "工作周报" if meta["report_type"] == "weekly" else "工作日报"
    lines = [
        "# %s（%s）" % (title, meta["period"]),
        "",
        "- 生成时间：%s" % generated_at,
        "- 数据来源：%s" % meta["source"],
        "- 生成方式：%s" % meta["mode"],
        "",
        "## 概述",
        "",
        analysis.get("summary") or "（无）",
        "",
        "## %s完成" % ("本周" if meta["report_type"] == "weekly" else "今日"),
        "",
        render_list(analysis.get("done") or []),
        "",
        "## 进行中",
        "",
        render_list(analysis.get("in_progress") or []),
        "",
        "## %s计划" % ("下周" if meta["report_type"] == "weekly" else "明日"),
        "",
        render_list(analysis.get("next_plan") or []),
        "",
        "## 风险与需要支持",
        "",
        "**风险**：",
        "",
        render_list(analysis.get("risks") or []),
        "",
        "**需要支持**：",
        "",
        render_list(analysis.get("help_needed") or []),
        "",
        "## 数据附录",
        "",
        "### 分类统计表",
        "",
        "| 类型 | 提交次数 | 说明 |",
        "| --- | --- | --- |",
    ]
    for type_name in COMMIT_TYPES:
        count = git_stats["types"].get(type_name, 0)
        if not count:
            continue
        lines.append("| %s（%s） | %d | %s |" % (
            TYPE_LABELS[type_name], type_name, count,
            "；".join(git_stats["type_details"][type_name][:5]) or "-",
        ))
    lines += [
        "",
        "### 模块统计表",
        "",
        "| 模块 | 提交次数 | 新增行 | 删除行 | 文件数 |",
        "| --- | --- | --- | --- | --- |",
    ]
    if git_stats["modules"]:
        for name, entry in sorted(git_stats["modules"].items(), key=lambda kv: -kv[1]["added"]):
            lines.append("| %s | %d | %d | %d | %d |" % (
                name, entry["commits"], entry["added"], entry["deleted"], entry["files"],
            ))
    else:
        lines.append("| （无 git 数据） | - | - | - | - |")
    lines += [
        "",
        "### 每日分布",
        "",
        "| 日期 | 提交次数 |",
        "| --- | --- |",
    ]
    if git_stats["days"]:
        for day, count in sorted(git_stats["days"].items()):
            lines.append("| %s | %d |" % (day, count))
    else:
        lines.append("| （无） | - |")
    lines += [
        "",
        "### 按日明细（git 与工作记录合并去重后）",
        "",
    ]
    if merged_days:
        for day, entry in sorted(merged_days.items()):
            lines.append("**%s**" % day)
            lines.append("")
            for subject in entry["git"]:
                lines.append("- [git] %s" % subject)
            for text in entry["notes"]:
                lines.append("- [记录] %s" % text)
            lines.append("")
    else:
        lines.append("（无）")
        lines.append("")
    lines += [
        "### 数量汇总",
        "",
        "| 指标 | 数值 |",
        "| --- | --- |",
        "| commit 总数 | %d |" % git_stats["count"],
        "| 代码新增 | %d 行 |" % git_stats["added"],
        "| 代码删除 | %d 行 |" % git_stats["deleted"],
        "| 改动文件数 | %d |" % git_stats["files"],
        "| 活跃天数 | %d |" % git_stats["active_days"],
        "| 工作记录条目 | %d（已完成 %d / 未完成 %d / 未标注 %d） |" % (
            len(notes["done"]) + len(notes["undone"]) + len(notes["unlabeled"]),
            len(notes["done"]), len(notes["undone"]), len(notes["unlabeled"]),
        ),
        "| 合并去重丢弃的重复条目 | %d |" % meta.get("merged_away", 0),
        "",
    ]

    if raw_output:
        lines += ["## 附录：模型原始输出（未能解析为 JSON）", "", "```text", raw_output, "```", ""]

    return "\n".join(lines).rstrip() + "\n"


# ---------- 自检用假客户端 ----------

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
    """假客户端：beta 层抛 TypeError，messages 层第一次抛、第二次成功"""

    def __init__(self):
        self.state = {"calls": 0}
        self.beta = _FakeBeta()
        self.messages = _FakeMessages(self.state)


# ---------- 自检 ----------

def _git_env():
    """自检用的 git 环境：固定作者/提交者身份，避免依赖本机配置"""
    env = dict(os.environ)
    env["GIT_AUTHOR_NAME"] = "自检用户"
    env["GIT_AUTHOR_EMAIL"] = "selftest@example.com"
    env["GIT_COMMITTER_NAME"] = "自检用户"
    env["GIT_COMMITTER_EMAIL"] = "selftest@example.com"
    env["GIT_CONFIG_GLOBAL"] = os.devnull
    env["GIT_CONFIG_SYSTEM"] = os.devnull
    return env


def _run_git_checked(repo, args, env):
    """自检里执行 git，失败就抛 AssertionError"""
    proc = subprocess.run(
        ["git"] + args, cwd=str(repo), env=env,
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    assert proc.returncode == 0, "git %s 失败：%s" % (" ".join(args), proc.stderr)
    return proc


def run_self_test():
    """离线自检：git 造仓库解析、工作记录解析、合并去重、离线骨架、模型兜底；
    不联网、不需要 API key。git 不可用时跳过 git 相关断言并打印提示。"""
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)

        # --- 工作记录解析：三种日期标题 + 勾选状态 ---
        notes_text = (
            "# 工作记录\n"
            "\n"
            "## 2026-10-26\n"
            "- [x] 完成登录接口联调\n"
            "- [ ] 推进压测方案\n"
            "\n"
            "## 10.27\n"
            "- [x] 修复空指针崩溃\n"
            "- [x] 补充接口文档\n"
            "- [ ] 准备评审材料（进行中）\n"
            "\n"
            "## 周三\n"
            "- [ ] 梳理下周计划\n"
            "\n"
            "## 本周总结\n"
            "- 整体进展顺利\n"
        )
        reference = datetime(2026, 10, 28).date()
        notes = parse_notes(notes_text, reference_date=reference)
        assert len(notes["groups"]) == 4, [g["label"] for g in notes["groups"]]
        assert notes["groups"][0]["date"] == "2026-10-26"
        assert notes["groups"][1]["date"] == "2026-10-27"
        # 周三 → 参考日 2026-10-28（周三）往前最近的周三
        assert notes["groups"][2]["date"] == "2026-10-28", notes["groups"][2]
        assert notes["groups"][3]["date"] == ""
        assert notes["done"] == ["完成登录接口联调", "修复空指针崩溃", "补充接口文档"], notes["done"]
        assert "推进压测方案" in notes["undone"]
        assert "准备评审材料（进行中）" in notes["undone"]
        assert "梳理下周计划" in notes["undone"]
        assert "整体进展顺利" in notes["unlabeled"]

        # --- 模型回复 JSON 解析兜底 ---
        good = (
            "```json\n"
            '{"summary": "本周进展顺利", "done": ["完成登录"], "in_progress": ["压测"],'
            ' "next_plan": ["出报告"], "risks": ["人手紧张"], "help_needed": ["需要测试同学"]}\n'
            "```"
        )
        parsed = parse_model_json(good)
        assert parsed is not None
        assert parsed["summary"] == "本周进展顺利"
        assert parsed["done"] == ["完成登录"]
        assert parsed["help_needed"] == ["需要测试同学"]
        assert parse_model_json("这周做了很多事，就不一一列举了。") is None
        assert parse_model_json("") is None
        assert parse_model_json('{"done": "不是数组"}')["done"] == []

        # --- 分类与模块的确定性规则 ---
        assert classify_commit("feat(auth): 新增登录") == "feat"
        assert classify_commit("fix: 修复空指针") == "fix"
        assert classify_commit("docs: 更新 README") == "docs"
        assert classify_commit("refactor: 拆分服务") == "refactor"
        assert classify_commit("test: 补充用例") == "test"
        assert classify_commit("chore: 升级依赖") == "chore"
        assert classify_commit("新增导出功能") == "feat"
        assert classify_commit("修复登录超时") == "fix"
        assert classify_commit("#1022 新增导出功能") == "feat"
        assert classify_commit("【紧急】修复登录超时") == "fix"
        assert classify_commit("随便写点什么") == "其他"
        assert module_of("src/app.py") == "src"
        assert module_of("docs/api.md") == "docs"
        assert module_of("README.md") == "根目录"
        assert normalize_item("fix: 修复空指针") == normalize_item("修复空指针")

        # --- git 采集（git 不可用时跳过） ---
        if git_available():
            env = _git_env()
            repo = root / "repo"
            repo.mkdir()
            _run_git_checked(repo, ["init", "-q"], env)

            app_path = repo / "src" / "app.py"
            app_path.parent.mkdir(parents=True, exist_ok=True)
            app_path.write_text("line1\nline2\nline3\n", encoding="utf-8")
            _run_git_checked(repo, ["add", "."], env)
            env1 = dict(env)
            env1["GIT_AUTHOR_DATE"] = "2026-10-26T10:00:00+08:00"
            env1["GIT_COMMITTER_DATE"] = "2026-10-26T10:00:00+08:00"
            _run_git_checked(repo, [
                "-c", "user.name=自检用户", "-c", "user.email=selftest@example.com",
                "commit", "-q", "-m", "feat: 新增登录接口",
            ], env1)

            readme_path = repo / "README.md"
            readme_path.write_text("title\nintro\n", encoding="utf-8")
            _run_git_checked(repo, ["add", "."], env)
            env2 = dict(env)
            env2["GIT_AUTHOR_DATE"] = "2026-10-27T09:00:00+08:00"
            env2["GIT_COMMITTER_DATE"] = "2026-10-27T09:00:00+08:00"
            _run_git_checked(repo, [
                "-c", "user.name=自检用户", "-c", "user.email=selftest@example.com",
                "commit", "-q", "-m", "docs: 补充文档",
            ], env2)

            app_path.write_text("line1\nline2\nline3 fixed\nline4\n", encoding="utf-8")
            _run_git_checked(repo, ["add", "."], env)
            env3 = dict(env)
            env3["GIT_AUTHOR_DATE"] = "2026-10-27T15:00:00+08:00"
            env3["GIT_COMMITTER_DATE"] = "2026-10-27T15:00:00+08:00"
            _run_git_checked(repo, [
                "-c", "user.name=自检用户", "-c", "user.email=selftest@example.com",
                "commit", "-q", "-m", "fix: 修复空指针",
            ], env3)

            commits = collect_git_commits(repo)
            assert len(commits) == 3, [c["subject"] for c in commits]
            assert commits[0]["subject"] == "fix: 修复空指针"   # git log 倒序
            assert commits[0]["author"] == "自检用户"
            assert DATE_IN_TEXT_RE.search(commits[0]["date"])
            assert commits[0]["added"] == 2 and commits[0]["deleted"] == 1, commits[0]
            assert [item["path"] for item in commits[0]["files"]] == ["src/app.py"]

            git_stats = aggregate_git(commits)
            assert git_stats["count"] == 3
            assert git_stats["types"]["feat"] == 1
            assert git_stats["types"]["docs"] == 1
            assert git_stats["types"]["fix"] == 1
            assert git_stats["types"]["其他"] == 0
            assert git_stats["added"] == 3 + 2 + 2, git_stats
            assert git_stats["deleted"] == 1, git_stats
            assert git_stats["modules"]["src"]["commits"] == 2
            assert git_stats["modules"]["src"]["added"] == 5
            assert git_stats["modules"]["根目录"]["commits"] == 1
            assert git_stats["active_days"] == 2, git_stats["days"]
            assert git_stats["days"] == {"2026-10-26": 1, "2026-10-27": 2}, git_stats["days"]

            # author 过滤
            filtered = collect_git_commits(repo, author="不存在的作者")
            assert filtered == []

            # 空仓库（没有 commit）不崩
            empty_repo = root / "empty"
            empty_repo.mkdir()
            _run_git_checked(empty_repo, ["init", "-q"], env)
            assert collect_git_commits(empty_repo) == []

            # --- 合并去重：notes 与 git 重复条目按日期归并 ---
            merged_days, merged_away = merge_by_day(commits, notes)
            assert "2026-10-26" in merged_days and "2026-10-27" in merged_days
            assert "修复空指针崩溃" in merged_away or "修复空指针" in merged_away
            day_27 = merged_days["2026-10-27"]
            assert "docs: 补充文档" in day_27["git"]
            assert "补充接口文档" in day_27["notes"]

            # --- 离线骨架周报 ---
            skeleton = skeleton_analysis(git_stats, notes)
            assert skeleton["summary"] and "3 次提交" in skeleton["summary"], skeleton["summary"]
            assert any("新功能" in item for item in skeleton["done"]), skeleton["done"]
            assert any("缺陷修复" in item for item in skeleton["done"])
            assert "推进压测方案" in skeleton["next_plan"]
            assert "梳理下周计划" in skeleton["next_plan"]
            assert "准备评审材料（进行中）" in skeleton["in_progress"]

            meta = {
                "report_type": "weekly",
                "period": "2026-10-22 ~ 2026-10-28",
                "source": "git（%s） + 工作记录.md" % repo.name,
                "mode": "离线确定性模式（--offline）",
                "merged_away": len(merged_away),
            }
            report = build_report(skeleton, git_stats, notes, merged_days, meta,
                                  generated_at="2026-10-28 18:00:00")
            assert report.startswith("# 工作周报（2026-10-22 ~ 2026-10-28）")
            assert "## 概述" in report and "## 数据附录" in report
            assert "### 分类统计表" in report
            assert "### 模块统计表" in report
            assert "### 每日分布" in report
            assert "2026-10-26" in report and "src" in report
            out_path = root / "weekly-report.md"
            out_path.write_text(report, encoding="utf-8")
            assert out_path.read_text(encoding="utf-8").startswith("# 工作周报")

            # 模型输出解析失败时原文进附录
            report_raw = build_report(skeleton, git_stats, notes, merged_days, meta,
                                      raw_output="自由发挥的周报",
                                      generated_at="2026-10-28 18:00:00")
            assert "附录：模型原始输出" in report_raw
            assert "自由发挥的周报" in report_raw
        else:
            print("[提示] 本机没有 git，已跳过 git 采集与合并去重相关断言。")

        # --- 无 git 数据时骨架也能出（只用工作记录） ---
        empty_stats = aggregate_git([])
        assert empty_stats["count"] == 0 and empty_stats["active_days"] == 0
        notes_only = skeleton_analysis(empty_stats, notes)
        assert notes_only["next_plan"], notes_only["next_plan"]
        assert notes_only["done"], notes_only["done"]
        empty_report = build_report(
            notes_only, empty_stats, notes, {}, {
                "report_type": "daily", "period": "2026-10-28",
                "source": "工作记录.md", "mode": "离线确定性模式（--offline）",
                "merged_away": 0,
            },
            generated_at="2026-10-28 18:00:00",
        )
        assert empty_report.startswith("# 工作日报")
        assert "（无 git 数据）" in empty_report

        # --- call_model 三级降级（离线假客户端） ---
        fake = _FakeClient()
        result = call_model(fake, "fake-model", "system", [{"role": "user", "content": "hi"}])
        assert result["ok"] is True
        assert fake.state["calls"] == 2

        # --- parse_commit_lines：从文本文件解析 commit 列表 ---
        parsed_commits = parse_commit_lines(
            "# 注释行\n"
            "2026-10-27 | feat: 新增登录接口 | +120 -30 | src/auth/login.py,src/auth/api.py\n"
            "2026-10-28 | 修复金额计算\n"
            "只有说明没有日期\n"
        )
        assert len(parsed_commits) == 3, parsed_commits
        assert parsed_commits[0]["date"] == "2026-10-27", parsed_commits[0]
        assert parsed_commits[0]["added"] == 120 and parsed_commits[0]["deleted"] == 30
        assert len(parsed_commits[0]["files"]) == 2
        assert parsed_commits[1]["subject"] == "修复金额计算", parsed_commits[1]
        commit_stats = aggregate_git(parsed_commits)
        assert commit_stats["count"] == 3
        assert commit_stats["types"]["feat"] == 1 and commit_stats["types"]["fix"] == 1

        # --- UTF-8 BOM 容忍（Windows 常见） ---
        bom_notes = parse_notes("\ufeff# 工作记录\n## 2026-10-28\n- [x] BOM 容忍测试\n")
        assert bom_notes["done"] == ["BOM 容忍测试"], bom_notes
        bom_commits = parse_commit_lines("\ufeff# 注释\n2026-10-28 | feat: BOM 提交 | +1 -1 | src/bom.py\n")
        assert len(bom_commits) == 1 and bom_commits[0]["subject"] == "feat: BOM 提交", bom_commits

    print("SELF-TEST OK")


# ---------- CLI ----------

def build_parser():
    parser = argparse.ArgumentParser(
        description="周报/日报生成助手：采集 git 提交与工作记录，做分类统计与合并去重，生成中文周报/日报",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            "  python3 agent.py --repo ~/projects/myapp --days 7 --output 周报.md\n"
            "  python3 agent.py --repo . --notes 工作记录.md --type weekly\n"
            "  python3 agent.py --notes 工作记录.md --offline\n"
            "  python3 agent.py --commits commits.txt --notes 工作记录.md --offline\n"
            "  python3 agent.py --repo . --model qwen3.8-max\n"
        ),
    )
    parser.add_argument(
        "--repo", default="",
        help="git 仓库路径（默认当前目录；不是 git 仓库时只用 --notes / --commits 的数据）",
    )
    parser.add_argument(
        "--notes", default="",
        help="工作记录 markdown 文件（按 '## 日期' 分组、'- [x] / - [ ]' 记条目）",
    )
    parser.add_argument(
        "--commits", default="",
        help="commit 列表文本文件：每行 '日期 | 提交说明 | +新增 -删除 | 文件列表（逗号分隔）'，"
             "各字段都可省略（'#' 开头为注释行）；用于没有 git 仓库时提供提交数据，可与 --repo 同时用",
    )
    parser.add_argument(
        "--days", type=int, default=0,
        help="统计最近多少天（默认：weekly=7 天，daily=1 天）",
    )
    parser.add_argument(
        "--author", default="",
        help="只统计某个作者的 commit（git --author 过滤）",
    )
    parser.add_argument(
        "--type", choices=["weekly", "daily"], default="weekly",
        help="报告类型：weekly 周报 / daily 日报（默认 weekly）",
    )
    parser.add_argument(
        "--output", default=DEFAULT_OUTPUT,
        help="报告写入路径（默认 %s）" % DEFAULT_OUTPUT,
    )
    parser.add_argument(
        "--offline", action="store_true",
        help="纯确定性模式：直接生成骨架周报，不调用模型、不需要 API key",
    )
    parser.add_argument(
        "--model", default=DEFAULT_MODEL,
        help="模型名称（默认 %s）；走第三方兼容网关时用 --model 指定网关认得的模型名" % DEFAULT_MODEL,
    )
    parser.add_argument(
        "--self-test", action="store_true",
        help="离线自检：不联网、不需要 API key，跑全部确定性逻辑后打印 SELF-TEST OK",
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

    if not args.repo and not args.notes and not args.commits:
        parser.error("请至少提供 --repo / --notes / --commits 之一作为数据来源")
    if args.days < 0:
        parser.error("--days 不能是负数")

    days = args.days if args.days > 0 else (DEFAULT_DAYS if args.type == "weekly" else 1)

    # ---- git 采集 ----
    commits = []
    git_stats = aggregate_git([])
    source_parts = []
    if args.repo:
        repo_path = Path(args.repo).expanduser()
        if not repo_path.exists():
            print("错误：仓库路径不存在：%s" % repo_path, file=sys.stderr)
            sys.exit(1)
        if not git_available():
            print("[提示] 本机没有 git，跳过 git 采集，只用工作记录的数据。")
        else:
            try:
                commits = collect_git_commits(repo_path, days=days, author=args.author or None)
            except RuntimeError as exc:
                print("错误：%s（请确认 %s 是 git 仓库）" % (exc, repo_path), file=sys.stderr)
                sys.exit(1)
            git_stats = aggregate_git(commits)
            source_parts.append("git（%s，最近 %d 天）" % (repo_path, days))
            print("[git] commit %d 次，新增 %d 行、删除 %d 行，活跃 %d 天"
                  % (git_stats["count"], git_stats["added"], git_stats["deleted"], git_stats["active_days"]))

    # ---- commit 列表文件（没有 git 仓库时的提交数据来源） ----
    if args.commits:
        commits_path = Path(args.commits).expanduser()
        if not commits_path.exists():
            print("错误：找不到 commit 列表文件：%s" % commits_path, file=sys.stderr)
            sys.exit(1)
        try:
            commits_text = commits_path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            print("错误：读取 commit 列表失败：%s" % exc, file=sys.stderr)
            sys.exit(1)
        file_commits = parse_commit_lines(commits_text)
        commits = commits + file_commits
        git_stats = aggregate_git(commits)
        source_parts.append("%s（commit 列表 %d 条）" % (commits_path.name, len(file_commits)))
        print("[commits] 从 %s 读到 %d 条 commit 记录，累计 %d 条"
              % (commits_path.name, len(file_commits), len(commits)))

    # ---- 工作记录解析 ----
    notes = parse_notes("", reference_date=datetime.now().date())
    if args.notes:
        notes_path = Path(args.notes).expanduser()
        if not notes_path.exists():
            print("错误：找不到工作记录文件：%s" % notes_path, file=sys.stderr)
            sys.exit(1)
        try:
            notes_text = notes_path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            print("错误：读取工作记录失败：%s" % exc, file=sys.stderr)
            sys.exit(1)
        notes = parse_notes(notes_text, reference_date=datetime.now().date())
        source_parts.append(notes_path.name)
        print("[记录] 条目 %d 项（已完成 %d / 未完成 %d / 未标注 %d）"
              % (len(notes["done"]) + len(notes["undone"]) + len(notes["unlabeled"]),
                 len(notes["done"]), len(notes["undone"]), len(notes["unlabeled"])))

    if not commits and not (notes["done"] or notes["undone"] or notes["unlabeled"]):
        print("错误：没有采集到任何数据（git commit 与工作记录条目都是空）。", file=sys.stderr)
        sys.exit(1)

    merged_days, merged_away = merge_by_day(commits, notes)
    print("[合并] 按日归并 %d 天，丢弃重复条目 %d 条" % (len(merged_days), len(merged_away)))

    period = "%s ~ %s" % (
        (datetime.now().date() - timedelta(days=max(days - 1, 0))).isoformat(),
        datetime.now().date().isoformat(),
    )

    raw_output = None
    if args.offline:
        analysis = skeleton_analysis(git_stats, notes)
        mode = "离线确定性模式（--offline）"
        print("[离线] 使用确定性骨架，未调用模型")
    else:
        if not os.environ.get("ANTHROPIC_API_KEY") and not os.environ.get("ANTHROPIC_AUTH_TOKEN"):
            print(
                "提示：没有检测到 ANTHROPIC_API_KEY 或 ANTHROPIC_AUTH_TOKEN，无法调用模型。\n"
                "请先设置 API key：\n"
                "    macOS / Linux：export ANTHROPIC_API_KEY=你的key\n"
                "    Windows PowerShell：$env:ANTHROPIC_API_KEY=\"你的key\"\n"
                "也可以加 --offline 生成确定性骨架周报（不需要 key）。",
                file=sys.stderr,
            )
            sys.exit(1)
        mode = "模型生成（%s）" % args.model
        print("[模型] 模型：%s" % args.model)
        try:
            analysis, raw_output = analyze_with_model(
                git_stats, notes, merged_days, args.model, args.type, period
            )
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
            print("错误：模型服务返回错误（%s）：%s" % (exc.status_code, exc.message), file=sys.stderr)
            sys.exit(1)
        if analysis is None:
            print("[提示] 模型输出不是合法 JSON，已改用确定性骨架。")
            analysis = skeleton_analysis(git_stats, notes)

    meta = {
        "report_type": args.type,
        "period": period,
        "source": " + ".join(source_parts) or "（无）",
        "mode": mode,
        "merged_away": len(merged_away),
    }
    report = build_report(analysis, git_stats, notes, merged_days, meta, raw_output=raw_output)
    output_path = Path(args.output).expanduser()
    try:
        if output_path.parent and not output_path.parent.exists():
            output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(report, encoding="utf-8")
    except OSError as exc:
        print("错误：报告写入失败：%s" % exc, file=sys.stderr)
        sys.exit(1)

    print("[完成] 报告已写入：%s" % output_path)
    print("[完成] commit %d 次、完成 %d 条、计划 %d 条"
          % (git_stats["count"], len(analysis.get("done") or []), len(analysis.get("next_plan") or [])))
    if raw_output:
        print("[提示] 模型输出不是合法 JSON，原文已放入报告附录。")


if __name__ == "__main__":
    main()
