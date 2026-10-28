#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
目标拆解与计划生成助手（todo-planner）

把一段目标描述确定性地拆成里程碑 / 任务 / 子任务，生成 WBS 编号（1 / 1.1 / 1.1.1）、
依赖表与循环依赖检测、按工作日排布的日程与甘特式 ASCII 图、按目标类型定制的风险
清单，最后汇总成中文 Markdown 计划文件；可选让模型给出更细的结构化拆解（解析
失败自动退回确定性结果）。除 anthropic 外只用标准库。

用法：
    python3 agent.py --goal "一个月内学会 Python 并做两个小项目"
    python3 agent.py --goal "..." --days 30 --start 2026-10-28 --style 激进
    python3 agent.py --from 目标.md --output 计划.md --offline

环境要求：
    1. 默认流程会调用模型增强拆解，需要设置 key：
           export ANTHROPIC_API_KEY=你的key        # macOS / Linux
           $env:ANTHROPIC_API_KEY="你的key"         # Windows PowerShell
       加 --offline 走纯确定性模式，不需要 key、不需要网络。
    2. 安装依赖：pip3 install -r requirements.txt
    3. 走第三方 Anthropic 兼容网关时，用 --model 指定网关认得的模型名，例如：
           python3 agent.py --goal "..." --model qwen3.8-max
"""

import argparse
import json
import os
import re
import sys
import tempfile
from collections import OrderedDict
from datetime import date, datetime, timedelta

import anthropic

# 默认模型（可用 --model 参数或 TODO_PLANNER_MODEL 环境变量更换）
DEFAULT_MODEL = os.environ.get("TODO_PLANNER_MODEL", "claude-opus-5")

DEFAULT_OUTPUT = "plan.md"
DEFAULT_DAYS = 30           # 默认计划窗口（天）
DEFAULT_TASK_DAYS = {"稳妥": 2, "激进": 1}   # 任务里没写工期时的默认工期
MAX_DAILY_ROWS = 45         # 「每日待办」最多展示多少天
MAX_TASK_DAYS = 15          # 单个任务工期上限（天）

STYLES = ("稳妥", "激进")
GOAL_TYPES = ("学习", "开发", "活动", "其他")

# ---------- 正则 ----------

MILESTONE_RE = re.compile(
    r"(第[一二三四五六七八九十0-9]+阶段|阶段[一二三四五六七八九十0-9]+|"
    r"里程碑[一二三四五六七八九十0-9]*|M[0-9]+|Phase\s*[0-9]+)"
)
LIST_ITEM_RE = re.compile(r"^\s*(?:[-*•·–]|\d+[.、)）]|[（(]\d+[)）])\s*(.+)$")
SEGMENT_SPLIT_RE = re.compile(r"[；;。！!？?、\n]")
DURATION_RE = re.compile(r"(\d+)\s*(?:天|日)")
SUBTASK_SPLIT_RE = re.compile(r"[、，,；;]")

TIME_WORD_RE = re.compile(
    r"((?:[0-9]+|[一二三四五六七八九十两半几]+)\s*个?\s*(?:月|周|天|日)|"
    r"月底|月初|年底|年末|月末|本周|下周|下个月|本月|"
    r"[0-9]+\s*月\s*[0-9]+\s*[日号]|截止|之前|以内|期限|deadline|最晚)"
)
BUDGET_WORD_RE = re.compile(
    r"(预算|成本|费用|资金|开销|花费|不超过\s*[\d.]+\s*元|[\d.]+\s*元以内)"
)
MANPOWER_WORD_RE = re.compile(
    r"(人手|团队|同事|伙伴|兼职|外包|人力|资源|一个人|两个人|三个人|志愿者|导师|老师)"
)

DEPEND_AFTER_RE = re.compile(r"(然后|之后|随后|接着|基于|完成后|之后再|再进行|依赖|前提)")
DEPEND_FIRST_RE = re.compile(r"^\s*(先|首先|第一步|前置|先决)")
CONSTRAINT_LINE_RE = re.compile(r"^\s*(约束|限制|要求|前提|条件|注意事项|说明)\s*[：:]?")
COMPLETE_THEN_RE = re.compile(r"完成(.{2,12}?)之?后")

TYPE_KEYWORDS = OrderedDict([
    ("学习", ["学习", "复习", "考试", "课程", "掌握", "阅读", "背", "练习", "考研",
              "雅思", "托福", "技能", "入门", "看书", "单词", "刷题", "作业"]),
    ("开发", ["开发", "系统", "程序", "上线", "项目", "代码", "部署", "测试", "接口",
              "架构", "产品", "功能", "模块", "数据库", "前端", "后端", "爬虫", "重构"]),
    ("活动", ["活动", "举办", "比赛", "演出", "发布会", "婚礼", "展览", "策划",
              "运动会", "晚会", "团建", "路演", "沙龙", "庆典", "开幕", "志愿者"]),
])

# ---------- 风险清单模板（按目标类型） ----------

RISK_TEMPLATES = {
    "学习": [
        ("进度风险", "学习节奏被打断，计划拖期", "把大目标切成每周可验收的小任务，留 20% 缓冲"),
        ("动力风险", "中期动力下降导致弃坑", "设置里程碑奖励与固定学习时段"),
        ("方法风险", "方法低效、只输入不输出", "每个阶段都要有可见产出（笔记/练习/小项目）"),
        ("资源风险", "资料太杂、选择成本高", "开工前锁定 1~2 套主教材，其余做补充"),
    ],
    "开发": [
        ("范围风险", "需求蔓延、越做越大", "先冻结最小可用范围，超出的进「以后再说」清单"),
        ("技术风险", "关键技术点提前验证不足", "第一周就做技术验证（spike）与原型"),
        ("质量风险", "测试不足、上线翻车", "每个里程碑都要有验收用例与回归检查"),
        ("环境风险", "环境/权限/依赖出问题", "提前准备开发、测试、生产三套环境"),
    ],
    "活动": [
        ("时间风险", "场地、嘉宾、物料排期冲突", "倒排时间表，关键节点提前 1 周锁死"),
        ("预算风险", "费用超支", "预算分档：必选/可选/应急，留 15% 机动"),
        ("人员风险", "关键人临时缺席", "每个关键岗位设 A/B 角"),
        ("现场风险", "流程出岔子、设备故障", "提前彩排，准备纸质流程单与备用设备"),
    ],
    "其他": [
        ("目标风险", "目标模糊、验收标准不清", "开工前把目标改写成可衡量的验收条件"),
        ("进度风险", "任务估算偏乐观", "默认工期取中位数，缓冲放到里程碑之间"),
        ("资源风险", "人手/预算/信息不足", "列出所需资源清单并确认到位时间"),
        ("复盘风险", "做完不复盘、下次重犯", "每个里程碑结束留 30 分钟复盘"),
    ],
}


# ---------- 模型调用（三级降级） ----------

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
    """从模型响应里提取纯文本。"""
    parts = []
    for block in getattr(response, "content", []) or []:
        if getattr(block, "type", "") == "text":
            parts.append(getattr(block, "text", ""))
    return "\n".join(parts).strip()


def require_api_key():
    """模型路径前置检查：没有 key 就给中文提示并退出。"""
    if os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"):
        return
    print(
        "提示：没有检测到 ANTHROPIC_API_KEY / ANTHROPIC_AUTH_TOKEN 环境变量，无法调用模型。\n"
        "请先设置（macOS / Linux）：\n"
        "    export ANTHROPIC_API_KEY=你的key\n"
        "Windows PowerShell：\n"
        '    $env:ANTHROPIC_API_KEY="你的key"\n'
        "或者加 --offline 使用纯确定性模式（不需要 key）。",
        file=sys.stderr,
    )
    sys.exit(1)


# ---------- 目标解析 ----------

def detect_goal_type(text):
    """按关键词判断目标类型：学习 / 开发 / 活动 / 其他。"""
    scores = []
    for goal_type, keywords in TYPE_KEYWORDS.items():
        score = sum(text.count(keyword) for keyword in keywords)
        scores.append((score, goal_type))
    scores.sort(key=lambda item: (-item[0], item[1]))
    if scores[0][0] == 0:
        return "其他"
    return scores[0][1]


def _extract_constraints(text):
    """抽取时间/预算/人力约束片段。"""
    constraints = []
    for pattern, label in ((TIME_WORD_RE, "时间"), (BUDGET_WORD_RE, "预算"),
                           (MANPOWER_WORD_RE, "人力")):
        for matched in pattern.finditer(text):
            snippet = matched.group(0).strip()
            item = "%s约束：%s" % (label, snippet)
            if item not in constraints:
                constraints.append(item)
    # 同类别里只有孤立关键词（如「预算」）且还有更长描述时，丢掉关键词避免重复
    filtered = []
    for item in constraints:
        label, snippet = item.split("：", 1)
        if len(snippet) <= 2 and any(
                other.startswith(label + "：") and other != item for other in constraints):
            continue
        filtered.append(item)
    return filtered


def _split_task_text(line):
    """列表项 / 顿号、分号并列项 → 任务文本列表。"""
    matched = LIST_ITEM_RE.match(line)
    if matched:
        return [matched.group(1).strip()]
    return [part.strip() for part in SEGMENT_SPLIT_RE.split(line) if part.strip()]


def _task_with_subtasks(text):
    """「任务：A、B、C」这种写法拆成主任务 + 子任务。"""
    for separator in ("：", ":"):
        if separator in text:
            head, tail = text.split(separator, 1)
            parts = [part.strip() for part in SUBTASK_SPLIT_RE.split(tail) if part.strip()]
            if head.strip() and len(parts) >= 2:
                return {"title": head.strip(), "desc": tail.strip(), "subtasks": parts}
    return {"title": text.strip(), "desc": "", "subtasks": []}


def parse_goal_text(text):
    """把目标描述拆成 {标题, 类型, 约束, 里程碑[任务[子任务]]}。"""
    title = ""
    for line in text.splitlines():
        if line.strip():
            title = line.strip()
            break
    title = re.split(r"[。！？!?；;\n]", title)[0].strip()[:60] or "未命名目标"

    milestones = []
    current = None
    lines = [raw.strip() for raw in text.splitlines() if raw.strip()]
    # 约束只从目标句与「约束/要求…」行里抽，避免把任务里的工期当成约束
    constraint_lines = [line for line in lines if CONSTRAINT_LINE_RE.match(line)]
    if lines:
        constraint_lines.insert(0, lines[0])
    constraints = _extract_constraints("\n".join(constraint_lines))

    for line_no, line in enumerate(lines):
        if line_no == 0:
            continue  # 第一行是目标描述本身，不当任务（下面有兜底拆分）
        if MILESTONE_RE.search(line):
            marker = MILESTONE_RE.search(line).group(0)
            name = line
            for separator in ("：", ":", "——", "—", "-"):
                if separator in name:
                    name = name.split(separator, 1)[1].strip()
                    break
            if name == line:
                name = marker
            current = {"title": name or marker, "tasks": []}
            milestones.append(current)
            remainder = line.replace(marker, "", 1).strip(" ：:—-")
            if remainder and remainder != current["title"]:
                for piece in _split_task_text(remainder):
                    current["tasks"].append(_task_with_subtasks(piece))
            continue

        if CONSTRAINT_LINE_RE.match(line):
            continue  # 约束行只进约束清单（_extract_constraints 已抓取关键词）

        pieces = _split_task_text(line)
        if not pieces:
            continue
        if current is None:
            current = {"title": "总体推进", "tasks": []}
            milestones.append(current)
        for piece in pieces:
            if not piece:
                continue
            current["tasks"].append(_task_with_subtasks(piece))

    if not any(milestone["tasks"] for milestone in milestones):
        # 只有一句目标、没有展开行时，把目标句拆成任务，避免空计划
        if not milestones:
            milestones = [{"title": "总体推进", "tasks": []}]
        for piece in _split_task_text(lines[0] if lines else "完成目标"):
            milestones[0]["tasks"].append(_task_with_subtasks(piece))

    if not milestones:
        milestones = [{"title": "总体推进", "tasks": []}]

    return {
        "title": title,
        "goal_type": detect_goal_type(text),
        "constraints": constraints,
        "milestones": milestones,
    }


# ---------- WBS ----------

def build_wbs(milestones):
    """生成 WBS 编号：里程碑 1、任务 1.1、子任务 1.1.1，返回扁平节点表。"""
    nodes = []
    for m_index, milestone in enumerate(milestones, 1):
        nodes.append({
            "code": str(m_index), "depth": 0, "kind": "里程碑",
            "title": milestone.get("title", "阶段%d" % m_index),
            "desc": "", "subtasks": [], "milestone": m_index,
        })
        for t_index, task in enumerate(milestone.get("tasks", []), 1):
            code = "%d.%d" % (m_index, t_index)
            nodes.append({
                "code": code, "depth": 1, "kind": "任务",
                "title": task.get("title", "任务%d" % t_index),
                "desc": task.get("desc", ""), "subtasks": task.get("subtasks", []),
                "milestone": m_index,
            })
            for s_index, subtask in enumerate(task.get("subtasks", []), 1):
                nodes.append({
                    "code": "%s.%d" % (code, s_index), "depth": 2, "kind": "子任务",
                    "title": subtask, "desc": "", "subtasks": [], "milestone": m_index,
                })
    return nodes


def task_nodes(wbs_nodes):
    """只取任务层（WBS 第 2 层），排期与依赖都以它为单位。"""
    return [node for node in wbs_nodes if node["depth"] == 1]


def render_wbs_tree(wbs_nodes):
    """Markdown 缩进树。"""
    lines = []
    for node in wbs_nodes:
        indent = "  " * node["depth"]
        suffix = "（%s）" % node["kind"] if node["depth"] == 0 else ""
        lines.append("%s- **%s** %s%s" % (indent, node["code"], node["title"], suffix))
        if node["desc"] and node["depth"] == 1:
            lines.append("%s  - 说明：%s" % (indent, node["desc"]))
    return "\n".join(lines)


# ---------- 依赖 ----------

def estimate_days(node, style):
    """任务工期：文本里写明「X 天」就按它，否则按风格给默认值。"""
    text = "%s %s" % (node.get("title", ""), node.get("desc", ""))
    matched = DURATION_RE.search(text)
    if matched:
        return max(1, min(int(matched.group(1)), MAX_TASK_DAYS))
    return DEFAULT_TASK_DAYS.get(style, 2)


def build_dependencies(tasks):
    """依赖启发式：先/然后/基于/完成X后 → 前置任务依赖。返回 [(前, 后), ...]。"""
    edges = []
    for index, task in enumerate(tasks):
        text = "%s %s" % (task["title"], task.get("desc", ""))
        if index > 0 and DEPEND_AFTER_RE.search(text):
            edges.append((tasks[index - 1]["code"], task["code"]))
        matched = COMPLETE_THEN_RE.search(text)
        if matched:
            keyword = matched.group(1).strip()
            for other in tasks:
                if other["code"] == task["code"]:
                    continue
                if keyword in other["title"] or other["title"] in keyword:
                    edges.append((other["code"], task["code"]))
                    break
        if DEPEND_FIRST_RE.search(text) and index + 1 < len(tasks):
            edges.append((task["code"], tasks[index + 1]["code"]))
    deduped = []
    for edge in edges:
        if edge not in deduped and edge[0] != edge[1]:
            deduped.append(edge)
    return deduped


def detect_cycle(codes, edges):
    """深度优先找环，返回 [A, B, A] 这样的环路径；没有环返回 None。"""
    graph = OrderedDict((code, []) for code in codes)
    for start, end in edges:
        graph.setdefault(start, []).append(end)
        graph.setdefault(end, [])
    color = dict((code, 0) for code in graph)   # 0 未访问 1 栈上 2 已完成
    stack = []

    def visit(node):
        color[node] = 1
        stack.append(node)
        for neighbor in graph.get(node, []):
            if color.get(neighbor, 0) == 1:
                start_at = stack.index(neighbor)
                return stack[start_at:] + [neighbor]
            if color.get(neighbor, 0) == 0:
                found = visit(neighbor)
                if found:
                    return found
        stack.pop()
        color[node] = 2
        return None

    for code in graph:
        if color[code] == 0:
            found = visit(code)
            if found:
                return found
    return None


def topological_order(tasks, edges):
    """拓扑排序（稳定：同层按声明顺序）；有环就退回声明顺序。"""
    codes = [task["code"] for task in tasks]
    if detect_cycle(codes, edges):
        return list(tasks)
    indegree = dict((code, 0) for code in codes)
    graph = dict((code, []) for code in codes)
    for start, end in edges:
        if start in indegree and end in indegree:
            graph[start].append(end)
            indegree[end] += 1
    order_index = dict((code, index) for index, code in enumerate(codes))
    ready = [code for code in codes if indegree[code] == 0]
    ordered = []
    by_code = dict((task["code"], task) for task in tasks)
    while ready:
        ready.sort(key=lambda code: order_index[code])   # 稳定：先来的先排
        code = ready.pop(0)
        ordered.append(by_code[code])
        for neighbor in graph[code]:
            indegree[neighbor] -= 1
            if indegree[neighbor] == 0:
                ready.append(neighbor)
    if len(ordered) < len(tasks):
        return list(tasks)
    return ordered


# ---------- 工作日排程 ----------

def is_workday(day, skip_weekends=True):
    if not skip_weekends:
        return True
    return day.weekday() < 5


def next_workday(day, skip_weekends=True):
    """找到不早于 day 的第一个工作日。"""
    while not is_workday(day, skip_weekends):
        day += timedelta(days=1)
    return day


def workdays_from(start, count, skip_weekends=True):
    """从 start 起连续取 count 个工作日，返回日期列表（可跨月）。"""
    day = next_workday(start, skip_weekends)
    result = []
    while len(result) < count:
        if is_workday(day, skip_weekends):
            result.append(day)
        day += timedelta(days=1)
    return result


def workday_delta(start, end, skip_weekends=True):
    """区间 [start, end] 内的工作日数量。"""
    count = 0
    day = start
    while day <= end:
        if is_workday(day, skip_weekends):
            count += 1
        day += timedelta(days=1)
    return count


def schedule_tasks(tasks, start, style="稳妥", skip_weekends=True, window_days=None):
    """按依赖拓扑序把任务工期排到工作日日历，返回 (排程表, 提示列表)。"""
    planned = []
    for task in tasks:
        item = dict(task)
        item["days"] = int(task.get("days") or 0) or estimate_days(task, style)
        planned.append(item)

    edges = build_dependencies(planned)
    ordered = topological_order(planned, edges)
    end_by_code = {}
    cursor = start
    notes = []

    for task in ordered:
        earliest = cursor
        for source, target in edges:
            if target == task["code"] and source in end_by_code:
                earliest = max(earliest, next_workday(end_by_code[source] + timedelta(days=1), skip_weekends))
        span = workdays_from(earliest, task["days"], skip_weekends)
        if not span:
            span = [earliest]
        task["start"] = span[0]
        task["end"] = span[-1]
        task["dates"] = span
        end_by_code[task["code"]] = task["end"]
        cursor = next_workday(task["end"] + timedelta(days=1), skip_weekends)

    if window_days:
        window_end = start + timedelta(days=window_days)
        overflow = [task for task in ordered if task["end"] > window_end]
        if overflow:
            notes.append("有 %d 个任务排到了 %d 天窗口之外（最后结束于 %s），可加 --style 激进或延长 --days"
                         % (len(overflow), window_days, max(task["end"] for task in ordered)))
    return planned, notes


def render_gantt(tasks, skip_weekends=True):
    """按周分组的甘特式 ASCII 图。"""
    dated = [task for task in tasks if task.get("start") and task.get("end")]
    if not dated:
        return "（没有可排布的任务）"
    first = min(task["start"] for task in dated)
    last = max(task["end"] for task in dated)
    week_starts = []
    cursor = first - timedelta(days=first.weekday())
    while cursor <= last:
        week_starts.append(cursor)
        cursor += timedelta(days=7)

    def week_index(day):
        return (day - week_starts[0]).days // 7

    label_width = max(len("%s %s" % (task["code"], task["title"])) for task in dated)
    label_width = min(label_width, 24)
    header = "任务".ljust(label_width) + " | " + " | ".join(
        start.strftime("%m-%d") for start in week_starts)
    lines = [header, "-" * len(header)]
    for task in dated:
        label = ("%s %s" % (task["code"], task["title"]))
        label = label[:label_width].ljust(label_width)
        cells = ["   " for _ in week_starts]
        for day in task.get("dates", []):
            cells[week_index(day)] = "███"
        lines.append("%s | %s    %s ~ %s（%d 个工作日）" % (
            label, " ".join(cells).rstrip(),
            task["start"].strftime("%m-%d"), task["end"].strftime("%m-%d"),
            len(task.get("dates", []))))
    return "\n".join(lines)


def daily_todo(tasks, start, skip_weekends=True):
    """每天要做哪些任务（按区间覆盖统计）。"""
    if not tasks:
        return []
    last = max(task["end"] for task in tasks if task.get("end"))
    days = []
    day = start
    while day <= last:
        if is_workday(day, skip_weekends):
            running = [task for task in tasks
                       if task.get("start") and task.get("start") <= day <= task["end"]]
            if running:
                days.append((day, running))
        day += timedelta(days=1)
    return days


# ---------- 风险清单 ----------

def risk_checklist(goal_type):
    """按目标类型取风险检查表，附「应对预案」占位。"""
    rows = RISK_TEMPLATES.get(goal_type, RISK_TEMPLATES["其他"])
    result = []
    for category, risk, plan in rows:
        result.append({
            "category": category, "risk": risk,
            "possibility": "中", "impact": "中", "plan": plan,
        })
    return result


# ---------- 模型拆解（可选） ----------

PLAN_SYSTEM = """你是资深项目计划师。用户会给出一个目标，请拆解成结构化计划。
输出要求：
1. 只输出一个 JSON 对象，不要解释文字，不要 Markdown 代码围栏。
2. 对象结构：
   {"milestones":[{"title":"阶段名","tasks":[{"title":"任务名","desc":"一两句说明",
     "days":1,"depends_on":["前置任务名"],"deliverable":"交付物"}]}],
    "risks":[{"risk":"风险描述","plan":"应对预案"}]}
3. 里程碑 2~4 个，每个里程碑 2~5 个任务；days 是正整数；depends_on 只填同阶段或更早任务名。"""


def parse_plan_json(text):
    """把模型回复解析成 {milestones, risks}；失败返回 None。"""
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
    milestones = []
    for item in data.get("milestones") or []:
        if not isinstance(item, dict):
            continue
        tasks = []
        for task in item.get("tasks") or []:
            if not isinstance(task, dict):
                continue
            title = str(task.get("title") or "").strip()
            if not title:
                continue
            try:
                days = int(task.get("days") or 1)
            except (TypeError, ValueError):
                days = 1
            tasks.append({
                "title": title,
                "desc": str(task.get("desc") or ""),
                "days": max(1, min(days, MAX_TASK_DAYS)),
                "depends_on": [str(dep) for dep in (task.get("depends_on") or [])],
                "deliverable": str(task.get("deliverable") or ""),
                "subtasks": [],
            })
        if tasks:
            milestones.append({"title": str(item.get("title") or "阶段"), "tasks": tasks})
    if not milestones:
        return None
    risks = []
    for item in data.get("risks") or []:
        if isinstance(item, dict) and item.get("risk"):
            risks.append({"risk": str(item.get("risk")),
                          "plan": str(item.get("plan") or "（待补充）")})
    return {"milestones": milestones, "risks": risks}


def refine_with_model(goal_text, model):
    """让模型拆解目标，返回 (解析结果或 None, 原始文本)。"""
    client = anthropic.Anthropic()
    response = call_model(client, model, PLAN_SYSTEM, [{"role": "user", "content": goal_text}],
                          max_tokens=4000)
    raw = response_text(response)
    return parse_plan_json(raw), raw


def merge_plan(detected, model_plan):
    """模型结果可用就用模型的，否则退回确定性解析结果。"""
    if not model_plan:
        return {
            "milestones": detected["milestones"],
            "risks": risk_checklist(detected["goal_type"]),
            "source": "确定性解析",
        }
    milestones = []
    for milestone in model_plan["milestones"]:
        tasks = []
        for task in milestone["tasks"]:
            tasks.append({
                "title": task["title"],
                "desc": task.get("desc", ""),
                "subtasks": task.get("subtasks", []),
                "days": task.get("days", 1),
                "depends_on": task.get("depends_on", []),
                "deliverable": task.get("deliverable", ""),
            })
        milestones.append({"title": milestone["title"], "tasks": tasks})
    risks = risk_checklist(detected["goal_type"])
    for risk in model_plan.get("risks", []):
        risks.append({"category": "模型补充", "risk": risk["risk"],
                      "possibility": "待评估", "impact": "待评估", "plan": risk["plan"]})
    return {"milestones": milestones, "risks": risks, "source": "模型拆解"}


# ---------- 计划报告 ----------

def build_plan_report(detected, merged, wbs_nodes, edges, schedule_tasks_list,
                      notes, style, start, days, model_raw, generated_at=None):
    """把拆解结果渲染成中文 Markdown 计划文件。"""
    generated_at = generated_at or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    tasks = task_nodes(wbs_nodes) if wbs_nodes else []
    by_code = dict((task["code"], task) for task in schedule_tasks_list)

    lines = [
        "# 目标拆解计划：%s" % detected["title"],
        "",
        "生成时间：%s　|　计划窗口：%d 天　|　开始日期：%s　|　风格：%s　|　拆解来源：%s"
        % (generated_at, days, start.isoformat(), style, merged.get("source", "确定性解析")),
        "",
        "## 目标与约束",
        "",
        "> %s" % detected["title"],
        "",
        "- 目标类型：%s" % detected["goal_type"],
    ]
    if detected["constraints"]:
        for constraint in detected["constraints"]:
            lines.append("- %s" % constraint)
    else:
        lines.append("- （未识别到明确约束，按默认窗口 %d 天排布）" % days)
    lines.append("")

    lines.extend(["## 里程碑", ""])
    lines.extend(["| 里程碑 | 任务数 | 起止日期 |", "| --- | --- | --- |"])
    for node in wbs_nodes:
        if node["depth"] != 0:
            continue
        children = [item for item in tasks if item["milestone"] == node["milestone"]]
        starts = [by_code[item["code"]]["start"] for item in children if item["code"] in by_code]
        ends = [by_code[item["code"]]["end"] for item in children if item["code"] in by_code]
        span = "%s ~ %s" % (min(starts).isoformat(), max(ends).isoformat()) if starts else "（无任务）"
        lines.append("| %d. %s | %d | %s |" % (node["milestone"], node["title"], len(children), span))
    lines.append("")

    lines.extend(["## WBS 树与任务表", "", "```text", render_wbs_tree(wbs_nodes), "```", ""])
    lines.extend(["| 代码 | 任务 | 说明 | 工期（工作日） | 交付物 | 起止 |",
                  "| --- | --- | --- | --- | --- | --- |"])
    for node in wbs_nodes:
        if node["depth"] != 1:
            continue
        planned = by_code.get(node["code"], {})
        lines.append("| %s | %s | %s | %s | %s | %s |" % (
            node["code"], node["title"], (node["desc"] or "—").replace("|", "丨")[:40],
            planned.get("days", "—"), (planned.get("deliverable") or "—").replace("|", "丨")[:30],
            "%s ~ %s" % (planned["start"].isoformat(), planned["end"].isoformat())
            if planned.get("start") else "—"))
    lines.append("")

    lines.extend(["## 依赖关系", ""])
    if edges:
        lines.extend(["| 前置任务 | 后续任务 | 依据 |", "| --- | --- | --- |"])
        titles = dict((node["code"], node["title"]) for node in wbs_nodes)
        for source, target in edges:
            lines.append("| %s %s | %s %s | 文本中的先后关系 |" % (
                source, titles.get(source, ""), target, titles.get(target, "")))
        codes = [node["code"] for node in wbs_nodes]
        cycle = detect_cycle(codes, edges)
        if cycle:
            lines.append("")
            lines.append("**注意：检测到循环依赖 %s，已退回按声明顺序排程。**" % " → ".join(cycle))
    else:
        lines.append("（未识别到任务间依赖，按里程碑顺序排布）")
    lines.append("")

    lines.extend(["## 甘特图（按周分组）", "", "```text",
                  render_gantt(schedule_tasks_list), "```", ""])

    lines.extend(["## 风险清单", "", "| 类别 | 风险 | 可能性 | 影响 | 应对预案 |",
                  "| --- | --- | --- | --- | --- |"])
    for risk in merged.get("risks", []):
        lines.append("| %s | %s | %s | %s | %s |" % (
            risk.get("category", "通用"), risk.get("risk", ""),
            risk.get("possibility", "中"), risk.get("impact", "中"),
            (risk.get("plan") or "（待补充）").replace("|", "丨")))
    lines.append("")

    lines.extend(["## 每日待办", ""])
    days_list = daily_todo(schedule_tasks_list, start)
    shown = days_list[:MAX_DAILY_ROWS]
    for day, running in shown:
        lines.append("### %s（%s）" % (day.isoformat(), "一二三四五六日"[day.weekday()]))
        lines.append("")
        for task in running:
            marker = "开始" if task["start"] == day else ("收尾" if task["end"] == day else "推进")
            lines.append("- [%s] **%s %s**（%s ~ %s）" % (
                marker, task["code"], task["title"],
                task["start"].isoformat(), task["end"].isoformat()))
        lines.append("")
    if len(days_list) > MAX_DAILY_ROWS:
        lines.append("（仅展示前 %d 个工作日，完整排程见甘特图）" % MAX_DAILY_ROWS)
        lines.append("")

    if notes:
        lines.extend(["## 提示", ""])
        for note in notes:
            lines.append("- %s" % note)
        lines.append("")
    if model_raw:
        lines.extend(["## 附录：模型原始输出（未能解析为 JSON）", "", "```text", model_raw, "```", ""])

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
    """离线自检：解析、WBS、依赖、排程、甘特图、报告、兜底逻辑全跑一遍。"""
    goal = "\n".join([
        "一个月内系统学习 Python 基础",
        "第一阶段：打基础",
        "- 整理笔记：变量、函数、列表",
        "- 学习基础语法与数据结构，3 天",
        "- 完成课后练习，然后做小测",
        "第二阶段：做练习",
        "- 先复习前一阶段内容，2 天",
        "- 完成复习后做综合练习，4 天",
        "- 背熟常用标准库",
        "约束：一个月内完成，预算不超过 500 元，需要一位老师指导",
    ])

    # --- 目标解析 ---
    detected = parse_goal_text(goal)
    assert detected["title"].startswith("一个月内系统学习 Python"), detected
    assert detected["goal_type"] == "学习", detected
    assert len(detected["milestones"]) == 2, detected["milestones"]
    assert detected["milestones"][0]["title"] == "打基础", detected["milestones"]
    assert detected["milestones"][1]["title"] == "做练习"
    titles = [task["title"] for milestone in detected["milestones"] for task in milestone["tasks"]]
    assert "学习基础语法与数据结构，3 天" in titles or any("基础语法" in t for t in titles), titles
    assert any("背熟常用标准库" in t for t in titles), titles
    # 顿号并列项拆成子任务
    note_task = [task for milestone in detected["milestones"] for task in milestone["tasks"]
                 if task["title"].startswith("整理笔记")][0]
    assert note_task["subtasks"] == ["变量", "函数", "列表"], note_task
    # 约束抽取
    assert any("时间" in c for c in detected["constraints"]), detected["constraints"]
    assert any("预算" in c for c in detected["constraints"]), detected["constraints"]
    assert any("人力" in c for c in detected["constraints"]), detected["constraints"]

    # 并列句也要能拆
    assert detect_goal_type("举办一场校园运动会") == "活动"
    assert detect_goal_type("开发一个记账系统并上线") == "开发"
    assert detect_goal_type("随便看看") == "其他"
    simple = parse_goal_text("调研需求；准备材料；提交报告")
    assert sum(len(m["tasks"]) for m in simple["milestones"]) == 3, simple

    # --- WBS 编号与层级树 ---
    wbs = build_wbs(detected["milestones"])
    codes = [node["code"] for node in wbs]
    assert "1" in codes and "1.1" in codes and "1.1.1" in codes, codes
    tree = render_wbs_tree(wbs)
    assert "**1**" in tree and "**1.1**" in tree and "**1.1.1**" in tree
    assert "  - **1.1**" in tree and "    - **1.1.1**" in tree

    tasks = task_nodes(wbs)
    assert all(node["depth"] == 1 for node in tasks)
    assert len(tasks) == 6, [task["title"] for task in tasks]

    # --- 工期估算 ---
    style_task = [task for task in tasks if "3 天" in task["title"]][0]
    assert estimate_days(style_task, "稳妥") == 3
    plain_task = [task for task in tasks if "背熟" in task["title"]][0]
    assert estimate_days(plain_task, "稳妥") == 2
    assert estimate_days(plain_task, "激进") == 1

    # --- 依赖与循环检测 ---
    edges = build_dependencies(tasks)
    assert any(edge[0] == "1.2" and edge[1] == "1.3" for edge in edges), edges  # 完成练习后小测
    assert any(edge[0] == "2.1" and edge[1] == "2.2" for edge in edges), edges  # 先复习再练习
    assert detect_cycle([task["code"] for task in tasks], edges) is None

    cycle = detect_cycle(["A", "B"], [("A", "B"), ("B", "A")])
    assert cycle and cycle[0] == cycle[-1] and set(cycle) == {"A", "B"}, cycle
    assert detect_cycle(["A", "B", "C"], [("A", "B"), ("B", "C")]) is None

    # --- 工作日排程（含跨月） ---
    assert date(2026, 10, 30).weekday() == 4          # 2026-10-30 是周五（自校验）
    five = workdays_from(date(2026, 10, 30), 5)
    assert five == [date(2026, 10, 30), date(2026, 11, 2), date(2026, 11, 3),
                    date(2026, 11, 4), date(2026, 11, 5)], five
    assert next_workday(date(2026, 10, 31)) == date(2026, 11, 2)   # 周六 → 下周一
    assert workday_delta(date(2026, 10, 30), date(2026, 11, 5)) == 5
    assert len(workdays_from(date(2026, 11, 1), 1)) == 1

    schedule, notes = schedule_tasks(tasks, date(2026, 10, 30), "稳妥", True, 30)
    assert schedule and all(item.get("start") and item.get("end") for item in schedule)
    for item in schedule:
        assert all(day.weekday() < 5 for day in item["dates"]), item
        assert item["end"] >= item["start"]
        assert len(item["dates"]) == item["days"], item
    # 依赖生效：1.3 必须在 1.2 结束后才开始
    item1 = [item for item in schedule if item["code"] == "1.2"][0]
    item2 = [item for item in schedule if item["code"] == "1.3"][0]
    assert item2["start"] > item1["end"], (item1, item2)
    assert any("窗口之外" in note for note in notes) or not notes

    schedule_no_skip, _ = schedule_tasks(tasks[:1], date(2026, 10, 31), "激进", False, None)
    assert schedule_no_skip[0]["start"] == date(2026, 10, 31)   # 不跳周末

    # --- 甘特图 ---
    gantt = render_gantt(schedule)
    assert "█" in gantt and "任务" in gantt
    assert "1.1" in gantt and "10-30" in gantt
    assert render_gantt([]) == "（没有可排布的任务）"

    # --- 每日待办 ---
    days_list = daily_todo(schedule, date(2026, 10, 30))
    assert days_list and days_list[0][0] == date(2026, 10, 30)
    assert all(day.weekday() < 5 for day, _running in days_list)

    # --- 风险清单 ---
    for goal_type in GOAL_TYPES:
        rows = risk_checklist(goal_type)
        assert len(rows) >= 4
        for row in rows:
            assert row["risk"] and row["plan"]
    assert risk_checklist("学习")[0]["category"] == "进度风险"

    # --- 模型回复 JSON 解析兜底 ---
    good = (
        "```json\n"
        '{"milestones":[{"title":"阶段一","tasks":[{"title":"任务A","desc":"d",'
        '"days":3,"depends_on":[],"deliverable":"报告"}]}],'
        '"risks":[{"risk":"风险1","plan":"预案1"}]}'
        "\n```"
    )
    parsed = parse_plan_json(good)
    assert parsed and parsed["milestones"][0]["tasks"][0]["days"] == 3
    assert parsed["risks"][0]["risk"] == "风险1"
    assert parse_plan_json("这不是 JSON") is None
    assert parse_plan_json("{bad json") is None
    assert parse_plan_json('{"milestones":[]}') is None
    weird = '{"milestones":[{"title":"X","tasks":[{"title":"T","days":"很多"}]}]}'
    coerced = parse_plan_json(weird)
    assert coerced and coerced["milestones"][0]["tasks"][0]["days"] == 1

    merged_ok = merge_plan(detected, parsed)
    assert merged_ok["source"] == "模型拆解"
    assert merged_ok["milestones"][0]["title"] == "阶段一"
    assert any(risk["category"] == "模型补充" for risk in merged_ok["risks"])

    merged_fallback = merge_plan(detected, None)
    assert merged_fallback["source"] == "确定性解析"
    assert merged_fallback["milestones"] == detected["milestones"]

    # --- 报告结构 ---
    report = build_plan_report(
        detected, merged_fallback, wbs, edges, schedule, notes,
        "稳妥", date(2026, 10, 30), 30, "", generated_at="2026-10-28 12:00:00",
    )
    for heading in (
        "# 目标拆解计划：", "## 目标与约束", "## 里程碑", "## WBS 树与任务表",
        "## 依赖关系", "## 甘特图（按周分组）", "## 风险清单", "## 每日待办",
    ):
        assert heading in report, "计划缺少：%s" % heading
    assert "1.1.1" in report and "█" in report
    assert "确定性解析" in report

    raw_report = build_plan_report(
        detected, merged_fallback, wbs, edges, schedule, [],
        "稳妥", date(2026, 10, 30), 30, "自由格式模型意见",
        generated_at="2026-10-28 12:00:00",
    )
    assert "未能解析为 JSON" in raw_report
    assert "自由格式模型意见" in raw_report

    # --- call_model 三级降级（离线假客户端） ---
    fake = _FakeClient()
    result = call_model(fake, "fake-model", "system", [{"role": "user", "content": "hi"}])
    assert result["ok"] is True
    assert fake.state["calls"] == 2

    print("SELF-TEST OK")


# ---------- CLI ----------

def build_parser():
    parser = argparse.ArgumentParser(
        description="目标拆解与计划生成：里程碑、WBS、依赖、工作日排程、甘特图、风险清单，输出中文计划",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            '  python3 agent.py --goal "一个月内学会 Python 并做两个小项目"\n'
            "  python3 agent.py --goal \"...\" --days 30 --start 2026-10-28 --style 激进\n"
            "  python3 agent.py --from 目标.md --output 计划.md --offline\n"
        ),
    )
    parser.add_argument("--goal", default=None, help="目标描述（中文自然语言）")
    parser.add_argument("--from", dest="from_file", metavar="目标文件",
                        help="从文本文件读目标描述（每行一条）")
    parser.add_argument("--days", type=int, default=DEFAULT_DAYS,
                        help="计划窗口天数（默认 %d）" % DEFAULT_DAYS)
    parser.add_argument("--start", default=None,
                        help="开始日期 YYYY-MM-DD（默认今天）")
    parser.add_argument("--style", choices=list(STYLES), default="稳妥",
                        help="排期风格：稳妥留缓冲 / 激进压工期（默认 稳妥）")
    parser.add_argument("--skip-weekends", dest="skip_weekends", action="store_true",
                        default=True, help="排程跳过周末（默认开启）")
    parser.add_argument("--no-skip-weekends", dest="skip_weekends", action="store_false",
                        help="排程不跳周末")
    parser.add_argument("--output", default=DEFAULT_OUTPUT,
                        help="计划写入路径（默认 %s）" % DEFAULT_OUTPUT)
    parser.add_argument("--model", default=DEFAULT_MODEL,
                        help="模型名（默认 %s）" % DEFAULT_MODEL)
    parser.add_argument("--offline", action="store_true",
                        help="纯确定性模式：不调用模型、不需要 API key")
    parser.add_argument("--self-test", action="store_true",
                        help="离线自检：不联网、不需要 API key")
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

    goal_text = (args.goal or "").strip()
    if args.from_file:
        try:
            with open(args.from_file, "r", encoding="utf-8") as handle:
                goal_text = (goal_text + "\n" + handle.read()).strip()
        except OSError as exc:
            print("错误：读取目标文件失败：%s" % exc, file=sys.stderr)
            sys.exit(1)
    if not goal_text:
        parser.error("请用 --goal 或 --from 提供目标描述（或用 --self-test 自检）")
    if args.days < 1:
        parser.error("--days 必须大于 0")

    if args.start:
        try:
            start = datetime.strptime(args.start, "%Y-%m-%d").date()
        except ValueError:
            parser.error("--start 格式应为 YYYY-MM-DD，例如 2026-10-28")
    else:
        start = date.today()

    detected = parse_goal_text(goal_text)
    print("[解析] 目标类型：%s，里程碑 %d 个，任务 %d 个，约束 %d 条"
          % (detected["goal_type"], len(detected["milestones"]),
             sum(len(m["tasks"]) for m in detected["milestones"]),
             len(detected["constraints"])))

    merged = None
    raw_appendix = None
    if not args.offline:
        require_api_key()
        print("[模型拆解] 模型：%s" % args.model)
        try:
            model_plan, raw_appendix = refine_with_model(goal_text, args.model)
        except (anthropic.AuthenticationError, anthropic.APIConnectionError,
                anthropic.APIStatusError) as exc:
            print("[提示] 模型拆解失败（%s），退回确定性解析。" % exc.__class__.__name__)
            model_plan, raw_appendix = None, None
        merged = merge_plan(detected, model_plan)
        if model_plan is None:
            print("[提示] 模型输出不是合法 JSON，原文放进计划附录，主体用确定性解析结果。")
    else:
        merged = merge_plan(detected, None)

    wbs_nodes = build_wbs(merged["milestones"])
    tasks = task_nodes(wbs_nodes)
    edges = build_dependencies(tasks)
    cycle = detect_cycle([node["code"] for node in wbs_nodes], edges)
    if cycle:
        print("[警告] 检测到循环依赖：%s（已退回按声明顺序排程）" % " → ".join(cycle))
    schedule, notes = schedule_tasks(tasks, start, args.style, args.skip_weekends, args.days)

    report = build_plan_report(
        detected, merged, wbs_nodes, edges, schedule, notes,
        args.style, start, args.days, raw_appendix,
    )
    output_path = os.path.abspath(args.output)
    parent = os.path.dirname(output_path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as handle:
        handle.write(report)

    print("[完成] 计划已写入：%s" % output_path)
    if schedule:
        print("[完成] 任务 %d 个，排程 %s ~ %s"
              % (len(schedule),
                 min(item["start"] for item in schedule),
                 max(item["end"] for item in schedule)))
    for note in notes:
        print("[提示] %s" % note)


if __name__ == "__main__":
    main()
