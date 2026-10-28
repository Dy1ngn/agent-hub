#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
会议纪要助手 —— 把会议逐字稿整理成结构化中文纪要

先做完全确定性的解析：三种逐字稿写法的发言人切分、发言统计、时间戳会议时段、
议题切分、决议/待办/未决问题抽取（含负责人与截止时间），再让模型把这些材料组织成
正式纪要（背景、议题讨论、决议、待办表、风险），最后输出中文 Markdown 纪要文件。
除 anthropic SDK 外只用标准库。

用法：
    python3 agent.py <逐字稿.txt> [选项]
    python3 agent.py --text "张三：我们讨论一下上线安排。李四：下周可以。"

示例：
    python3 agent.py 会议记录.txt --title 支付模块评审 --date 2026-10-28
    python3 agent.py --text "张三：决定下周灰度。李四：我负责监控。" --offline
    python3 agent.py 会议记录.txt --model qwen3.8-max --output 纪要.md

环境要求：
    1. 需要模型的路径（不带 --offline）要设置 API key：
           export ANTHROPIC_API_KEY=你的key        # macOS / Linux
           $env:ANTHROPIC_API_KEY="你的key"         # Windows PowerShell
       也支持 ANTHROPIC_AUTH_TOKEN。--offline 与 --self-test 不需要 key、不联网。
    2. 安装 SDK：
           pip3 install -r requirements.txt
    3. 走第三方兼容网关时（如阿里云等），用 --model 指定网关认得的模型名，
       例如 --model qwen3.8-max；代码里的 call_model() 会自动逐级降级参数。

支持的逐字稿写法（可混用，容忍空行与时间戳）：
    张三：今天讨论上线安排。
    [12:03] 李四：我这边压测已经完成。
    王五> 我认为可以下周灰度。
"""

import argparse
import json
import os
import re
import sys
import tempfile
from datetime import datetime
from pathlib import Path

import anthropic

# 默认模型（可用 --model 参数或 MEETING_MINUTES_MODEL 环境变量更换）
DEFAULT_MODEL = os.environ.get("MEETING_MINUTES_MODEL", "claude-opus-5")
DEFAULT_OUTPUT = "minutes.md"
MAX_TRANSCRIPT_CHARS = 20000    # 发给模型的逐字稿上限
MAX_ITEMS = 30                  # 决议/待办/未决问题最多列多少条
MAX_SEGMENT_FALLBACK = 20       # 无议题标记时最多切多少段

# ---------- 词表与正则 ----------

DECISION_WORDS = ("决定", "决议", "结论", "确认")
TODO_WORDS = ("负责", "跟进", "下周", "截止", "DL", "落地", "安排")
OPEN_WORDS = ("待定", "未决", "还需要", "下次讨论", "有争议")
OPINION_WORDS = ("认为", "觉得", "建议", "支持", "反对", "担心", "主张")
RISK_WORDS = ("风险", "担心", "隐患", "阻塞", "卡点", "不确定")

BRACKET_TIME_RE = re.compile(r"^[\[（(]\s*(\d{1,2}:\d{2}(?::\d{2})?)\s*[\]）)]\s*(.*)$", re.S)
BARE_TIME_RE = re.compile(r"^(\d{1,2}:\d{2}(?::\d{2})?)\s+(.*)$", re.S)
SPEAKER_COLON_RE = re.compile(
    r"^([\u4e00-\u9fa5A-Za-z][\u4e00-\u9fa5A-Za-z0-9_·\- ]{0,19})\s*[:：]\s*(.+)$", re.S
)
SPEAKER_ARROW_RE = re.compile(
    r"^([\u4e00-\u9fa5A-Za-z][\u4e00-\u9fa5A-Za-z0-9_·\- ]{0,19})\s*>\s*(.+)$", re.S
)
SPEAKER_BLACKLIST = {"http", "https", "ftp", "www", "todo", "note"}

TOPIC_MARKER_RE = re.compile(
    r"^\s*(?:议题|议程|主题|topic)\s*[一二三四五六七八九十百\d]*\s*[:：#.\-—、]?\s*(.+?)\s*$",
    re.I,
)
DISCUSS_MARKER_RE = re.compile(r"^\s*讨论\s*[:：]\s*(.+?)\s*$")
ABOUT_MARKER_RE = re.compile(r"^\s*关于(.{1,30}?)的?(?:讨论|事项|问题|进展)?\s*$")

OWNER_RES = (
    re.compile(r"负责人\s*[:：]?\s*([\u4e00-\u9fa5A-Za-z]{2,6})"),
    re.compile(r"由\s*([\u4e00-\u9fa5A-Za-z]{2,6}?)\s*(?:来)?(?:负责|跟进|牵头|对接|主责)"),
    re.compile(r"(?<![一-龥A-Za-z])([一-龥]{2,3})\s*(?:来)?(?:负责|跟进|牵头|对接|主责)"),
)
DUE_RES = (
    re.compile(r"\d{4}\s*[-/年]\s*\d{1,2}\s*[-/月]\s*\d{1,2}\s*日?"),
    re.compile(r"\d{1,2}\s*月\s*\d{1,2}\s*日(?:之前|前|内)?"),
    re.compile(r"(?:下?周|星期)[一二三四五六日天](?:之前|前|内|以前)?"),
    re.compile(r"(?:今天|明天|后天|本周|下周|月底)(?:之前|前|内)?"),
)


# ---------- 逐字稿解析 ----------

def split_timestamp(line):
    """拆掉行首时间戳（[12:03] 或 12:03 形式），返回 (时间或None, 剩余文本)"""
    match = BRACKET_TIME_RE.match(line)
    if match:
        return match.group(1), match.group(2).strip()
    match = BARE_TIME_RE.match(line)
    if match:
        rest = match.group(2)
        # 只有后面还跟着"说话人：/说话人>"时才认定是时间戳，避免误伤正文里的"10:30 截止"
        if SPEAKER_COLON_RE.match(rest) or SPEAKER_ARROW_RE.match(rest):
            return match.group(1), rest.strip()
    return None, line


def match_speaker(line):
    """识别 '说话人: 内容' / '说话人：内容' / '说话人> 内容'，返回 (说话人, 内容)"""
    for pattern in (SPEAKER_COLON_RE, SPEAKER_ARROW_RE):
        match = pattern.match(line)
        if not match:
            continue
        speaker = match.group(1).strip()
        content = match.group(2).strip()
        if not speaker or speaker.lower() in SPEAKER_BLACKLIST:
            continue
        if speaker.isdigit():
            continue
        return speaker, content
    return None, None


def match_topic(line):
    """识别议题标记行：'议题：XXX' / '议题一：XXX' / '讨论：XXX' / '关于XXX'"""
    match = TOPIC_MARKER_RE.match(line)
    if match:
        title = match.group(1).strip(" ：:、")
        return title or "（未命名议题）"
    match = DISCUSS_MARKER_RE.match(line)
    if match:
        title = match.group(1).strip(" ：:、")
        return title or "（未命名议题）"
    match = ABOUT_MARKER_RE.match(line)
    if match and not re.search(r"[。！？!?]", line):
        return line.strip()
    return None


def parse_transcript(text):
    """把逐字稿切成条目列表，每条是 {"kind": "utt"/"topic", ...}。
    说话人切换前的空行、纯文本续行都能容忍：续行并回上一条发言。"""
    items = []
    paragraph = 0          # 段落号，空行 +1，用于无议题标记时的切段
    last_index = None
    seen_blank = False
    for raw_line in (text or "").splitlines():
        line = raw_line.strip()
        if not line:
            if not seen_blank and items:
                paragraph += 1
            seen_blank = True
            continue
        seen_blank = False
        time_str, rest = split_timestamp(line)
        topic_title = match_topic(rest)
        if topic_title is not None:
            items.append({"kind": "topic", "title": topic_title, "para": paragraph})
            last_index = None
            continue
        speaker, content = match_speaker(rest)
        if speaker is not None:
            items.append({
                "kind": "utt",
                "speaker": speaker,
                "text": content,
                "time": time_str,
                "para": paragraph,
            })
            last_index = len(items) - 1
            continue
        if last_index is not None:
            # 说话人没变的续行
            items[last_index]["text"] += "\n" + rest
        else:
            items.append({
                "kind": "utt",
                "speaker": "（未署名）",
                "text": rest,
                "time": time_str,
                "para": paragraph,
            })
            last_index = len(items) - 1
    return items


def utterances_of(items):
    """取全部发言人条目"""
    return [item for item in items if item["kind"] == "utt"]


# ---------- 确定性统计 ----------

def _char_len(text):
    """统计字数：忽略所有空白字符"""
    return len(re.sub(r"\s+", "", text or ""))


def speaker_stats(utterances):
    """发言人统计：发言次数、字数、占比、平均单次长度、最长发言"""
    stats = {}
    total_chars = 0
    for item in utterances:
        speaker = item["speaker"]
        length = _char_len(item["text"])
        total_chars += length
        entry = stats.setdefault(speaker, {
            "count": 0, "chars": 0, "longest": 0, "longest_text": "",
        })
        entry["count"] += 1
        entry["chars"] += length
        if length > entry["longest"]:
            entry["longest"] = length
            entry["longest_text"] = item["text"].strip()
    for entry in stats.values():
        entry["avg"] = round(entry["chars"] / entry["count"], 1) if entry["count"] else 0
        entry["share"] = round(entry["chars"] * 100.0 / total_chars, 1) if total_chars else 0.0
    return stats


def _time_minutes(time_str):
    """'12:03' / '12:03:45' 换算成当天第几分钟"""
    if not time_str:
        return None
    parts = time_str.split(":")
    try:
        hour = int(parts[0])
        minute = int(parts[1])
        second = int(parts[2]) if len(parts) > 2 else 0
    except (ValueError, IndexError):
        return None
    if not (0 <= hour <= 23 and 0 <= minute <= 59 and 0 <= second <= 59):
        return None
    return hour * 60 + minute + (1 if second >= 30 else 0)


def meeting_span(utterances):
    """从时间戳解析会议时段与时长（分钟）"""
    minutes = []
    for item in utterances:
        value = _time_minutes(item.get("time"))
        if value is not None:
            minutes.append(value)
    if not minutes:
        return {"start": "", "end": "", "duration": 0}
    start = min(minutes)
    end = max(minutes)
    return {
        "start": "%02d:%02d" % (start // 60, start % 60),
        "end": "%02d:%02d" % (end // 60, end % 60),
        "duration": end - start,
    }


def split_topics(items):
    """议题切分：优先按议题标记行；没有标记行就按"说话人切换 + 空行"切段"""
    segments = []
    current = None
    has_marker = False
    for item in items:
        if item["kind"] == "topic":
            has_marker = True
            current = {"title": item["title"], "utterances": []}
            segments.append(current)
        else:
            if current is None:
                current = {"title": "", "utterances": []}
                segments.append(current)
            current["utterances"].append(item)
    segments = [seg for seg in segments if seg["utterances"]]

    if has_marker:
        for seg in segments:
            if not seg["title"]:
                seg["title"] = "（未命名议题）"
        return _finish_segments(segments)

    # 回退：说话人切换 + 空行（段落号变化）才切段
    fallback = []
    for seg in segments:
        bucket = []
        for item in seg["utterances"]:
            if bucket:
                prev = bucket[-1]
                same_para = item["para"] == prev["para"]
                same_speaker = item["speaker"] == prev["speaker"]
                if not same_para and not same_speaker:
                    fallback.append({"title": "", "utterances": bucket})
                    bucket = []
            bucket.append(item)
        if bucket:
            fallback.append({"title": "", "utterances": bucket})
    fallback = fallback[:MAX_SEGMENT_FALLBACK]
    for index, seg in enumerate(fallback, 1):
        if not seg["title"]:
            first = seg["utterances"][0]["text"].strip().replace("\n", " ")
            snippet = first[:16] + ("…" if len(first) > 16 else "")
            seg["title"] = "议题 %d：%s" % (index, snippet)
    return _finish_segments(fallback)


def _finish_segments(segments):
    """给每个议题补上参与人、时长、字数占比"""
    total_chars = sum(_char_len(item["text"]) for seg in segments for item in seg["utterances"])
    for seg in segments:
        speakers = []
        chars = 0
        minutes = []
        for item in seg["utterances"]:
            if item["speaker"] not in speakers:
                speakers.append(item["speaker"])
            chars += _char_len(item["text"])
            value = _time_minutes(item.get("time"))
            if value is not None:
                minutes.append(value)
        seg["participants"] = speakers
        seg["chars"] = chars
        seg["share"] = round(chars * 100.0 / total_chars, 1) if total_chars else 0.0
        if minutes:
            seg["duration"] = max(minutes) - min(minutes)
        else:
            seg["duration"] = None
    return segments


# ---------- 决议 / 待办 / 未决问题 ----------

def _hit(text, words):
    return any(word in text for word in words)


def extract_owner(text):
    """从一句话里抽负责人：'负责人：张三' / '张三负责' / '由李四跟进'"""
    for pattern in OWNER_RES:
        match = pattern.search(text)
        if match:
            name = match.group(1).strip()
            if name and not name.isdigit():
                return name
    return ""


def extract_due(text):
    """从一句话里抽截止时间"""
    for pattern in DUE_RES:
        match = pattern.search(text)
        if match:
            return match.group(0).strip()
    return ""


def extract_decisions(utterances):
    """决议：命中'决定/决议/结论/确认'的发言"""
    results = []
    for item in utterances:
        if _hit(item["text"], DECISION_WORDS):
            results.append({"text": item["text"].strip(), "speaker": item["speaker"]})
    return results[:MAX_ITEMS]


def extract_todos(utterances):
    """待办：命中'负责/跟进/下周/截止/DL/落地/安排'的发言，带负责人与时间"""
    results = []
    for item in utterances:
        if _hit(item["text"], TODO_WORDS):
            text = item["text"].strip()
            results.append({
                "task": text,
                "owner": extract_owner(text),
                "due": extract_due(text),
                "speaker": item["speaker"],
            })
    return results[:MAX_ITEMS]


def extract_opens(utterances):
    """未决问题：命中'待定/未决/还需要/下次讨论/有争议'的发言"""
    results = []
    for item in utterances:
        if _hit(item["text"], OPEN_WORDS):
            results.append({"text": item["text"].strip(), "speaker": item["speaker"]})
    return results[:MAX_ITEMS]


def extract_opinions(utterances):
    """观点句：命中'认为/觉得/建议/支持/反对/担心'等词的句子片段"""
    opinions = []
    for item in utterances:
        for piece in re.split(r"[。！？!?\n]+", item["text"]):
            piece = piece.strip()
            if piece and _hit(piece, OPINION_WORDS):
                opinions.append("%s：%s" % (item["speaker"], piece))
    return opinions[:MAX_ITEMS]


def extract_risks(utterances):
    """风险信号：命中'风险/担心/隐患/阻塞/卡点/不确定'的句子片段"""
    risks = []
    for item in utterances:
        for piece in re.split(r"[。！？!?\n]+", item["text"]):
            piece = piece.strip()
            if piece and _hit(piece, RISK_WORDS):
                risks.append("%s：%s" % (item["speaker"], piece))
    return risks[:MAX_ITEMS]


def collect_facts(items):
    """把逐字稿的全部确定性抽取结果收集成一份数据"""
    utterances = utterances_of(items)
    return {
        "utterances": utterances,
        "stats": speaker_stats(utterances),
        "span": meeting_span(utterances),
        "topics": split_topics(items),
        "decisions": extract_decisions(utterances),
        "todos": extract_todos(utterances),
        "opens": extract_opens(utterances),
        "opinions": extract_opinions(utterances),
        "risks": extract_risks(utterances),
    }


def deterministic_minutes(facts, title, date):
    """离线/降级纪要：直接用确定性抽取结果拼出与模型同构的结构"""
    utterances = facts["utterances"]
    background = " ".join(item["text"].strip() for item in utterances[:2])[:200] or "（逐字稿过短，未识别到背景）"
    topics = []
    for seg in facts["topics"]:
        discussion = " ".join(item["text"].strip() for item in seg["utterances"][:3])
        topics.append({
            "title": seg["title"],
            "discussion": discussion[:300],
            "opinion": "；".join(
                "%s：%s" % (item["speaker"], item["text"].strip()[:40])
                for item in seg["utterances"]
                if _hit(item["text"], OPINION_WORDS)
            )[:300],
        })
    return {
        "background": background,
        "topics": topics,
        "decisions": [item["text"] for item in facts["decisions"]],
        "todos": [
            {"task": item["task"], "owner": item["owner"], "due": item["due"]}
            for item in facts["todos"]
        ],
        "risks": facts["risks"] or [item["text"] for item in facts["opens"]],
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


MINUTES_SYSTEM_PROMPT = """你是一名专业的会议纪要撰写人。用户会给出会议逐字稿和确定性抽取结果（发言统计、议题切分、决议、待办、未决问题）。
请把材料整理成正式的中文会议纪要内容，只输出一个 JSON 对象，不要输出解释文字，不要使用 Markdown 代码围栏：
{
  "background": "会议背景与目的，2-4 句",
  "topics": [{"title": "议题名称", "discussion": "讨论要点", "opinion": "主要观点与分歧"}],
  "decisions": ["决议，逐条"],
  "todos": [{"task": "待办事项", "owner": "负责人，未知留空", "due": "截止时间，未知留空"}],
  "risks": ["风险或待协调事项"]
}
要求：只依据逐字稿内容，不要编造人名和数据；语言正式、简洁、可直接归档。"""


def build_prompt(facts, title, date):
    """拼模型提示词：逐字稿 + 确定性抽取结果"""
    transcript = "\n".join(
        "[%s] %s：%s" % (item.get("time") or "--:--", item["speaker"], item["text"].strip())
        for item in facts["utterances"]
    )
    if len(transcript) > MAX_TRANSCRIPT_CHARS:
        transcript = transcript[:MAX_TRANSCRIPT_CHARS] + "\n……（逐字稿过长已截断）"
    lines = [
        "【会议信息】",
        "主题：%s" % (title or "（未提供）"),
        "日期：%s" % (date or "（未提供）"),
        "",
        "【逐字稿】",
        transcript or "（空）",
        "",
        "【确定性抽取结果，供参考】",
        "议题：%s" % "；".join(seg["title"] for seg in facts["topics"]),
        "决议候选：%s" % ("；".join(item["text"][:40] for item in facts["decisions"]) or "无"),
        "待办候选：%s" % ("；".join(
            "%s（负责人：%s，截止：%s）" % (item["task"][:30], item["owner"] or "未知", item["due"] or "未知")
            for item in facts["todos"]) or "无"),
        "未决问题：%s" % ("；".join(item["text"][:40] for item in facts["opens"]) or "无"),
        "",
        "只输出 JSON 对象。",
    ]
    return "\n".join(lines)


def response_text(response):
    """提取模型响应里的文本块"""
    parts = []
    for block in getattr(response, "content", []) or []:
        if getattr(block, "type", "") == "text":
            parts.append(getattr(block, "text", ""))
    return "\n".join(parts).strip()


def parse_model_json(text):
    """尽力把模型回复解析成纪要 JSON 对象；解析不出来返回 None（调用方负责兜底）"""
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

    topics = []
    raw_topics = data.get("topics")
    if isinstance(raw_topics, list):
        for item in raw_topics:
            if not isinstance(item, dict):
                continue
            title = str(item.get("title") or "").strip()
            topics.append({
                "title": title or "（未命名议题）",
                "discussion": str(item.get("discussion") or "").strip(),
                "opinion": str(item.get("opinion") or "").strip(),
            })
    todos = []
    raw_todos = data.get("todos")
    if isinstance(raw_todos, list):
        for item in raw_todos:
            if not isinstance(item, dict):
                continue
            task = str(item.get("task") or "").strip()
            if not task:
                continue
            todos.append({
                "task": task,
                "owner": str(item.get("owner") or "").strip(),
                "due": str(item.get("due") or "").strip(),
            })
    decisions = [str(x).strip() for x in (data.get("decisions") or []) if str(x).strip()]
    risks = [str(x).strip() for x in (data.get("risks") or []) if str(x).strip()]
    return {
        "background": str(data.get("background") or "").strip(),
        "topics": topics,
        "decisions": decisions,
        "todos": todos,
        "risks": risks,
    }


def analyze_with_model(facts, model, title, date):
    """调用模型生成纪要内容，返回 (analysis, raw_text 或 None)"""
    client = anthropic.Anthropic()
    prompt = build_prompt(facts, title, date)
    response = call_model(
        client,
        model,
        MINUTES_SYSTEM_PROMPT,
        [{"role": "user", "content": prompt}],
        max_tokens=6000,
    )
    text = response_text(response)
    parsed = parse_model_json(text)
    if parsed is None:
        return None, text or "（模型没有返回文本）"
    return parsed, None


# ---------- 报告渲染 ----------

def truncate(text, limit=120):
    text = (text or "").replace("\n", " ").strip()
    return text[:limit] + ("……" if len(text) > limit else "")


def build_report(facts, analysis, meta, raw_output=None, generated_at=None):
    """生成中文 Markdown 会议纪要"""
    generated_at = generated_at or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    span = facts["span"]
    speakers = list(facts["stats"].keys())
    if span["start"]:
        time_line = "%s - %s（%d 分钟）" % (span["start"], span["end"], span["duration"])
    else:
        time_line = "（逐字稿没有时间戳）"

    lines = [
        "# 会议纪要：%s" % (meta["title"] or "未命名会议"),
        "",
        "## 会议信息",
        "",
        "| 字段 | 内容 |",
        "| --- | --- |",
        "| 会议主题 | %s |" % (meta["title"] or "（未提供）"),
        "| 会议日期 | %s |" % (meta["date"] or "（未提供）"),
        "| 会议时段 | %s |" % time_line,
        "| 参与人 | %s |" % ("、".join(speakers) or "（无）"),
        "| 发言条数 | %d |" % len(facts["utterances"]),
        "| 数据来源 | %s |" % meta["source"],
        "| 生成时间 | %s |" % generated_at,
        "| 生成方式 | %s |" % meta["mode"],
        "",
        "## 会议背景",
        "",
        analysis.get("background") or "（无）",
        "",
        "## 议题与讨论",
        "",
    ]

    topics = analysis.get("topics") or []
    if not topics:
        lines.append("（未识别到议题）")
    for index, topic in enumerate(topics, 1):
        seg = facts["topics"][index - 1] if index - 1 < len(facts["topics"]) else None
        lines.append("### 议题 %d：%s" % (index, topic.get("title") or "（未命名议题）"))
        lines.append("")
        if seg:
            duration = ("%d 分钟" % seg["duration"]) if seg.get("duration") is not None else "（无时间戳）"
            lines.append("- 参与人：%s" % ("、".join(seg["participants"]) or "（无）"))
            lines.append("- 时长：%s；发言占比 %.1f%%" % (duration, seg.get("share", 0.0)))
            lines.append("")
        if topic.get("discussion"):
            lines += ["**讨论要点**：", "", topic["discussion"], ""]
        if topic.get("opinion"):
            lines += ["**主要观点**：", "", topic["opinion"], ""]
        if not topic.get("discussion") and not topic.get("opinion"):
            lines += ["（本议题暂无细化内容）", ""]

    lines += ["## 决议", ""]
    decisions = analysis.get("decisions") or []
    if decisions:
        lines += ["%d. %s" % (index, item) for index, item in enumerate(decisions, 1)]
    else:
        lines.append("（本次会议没有明确决议）")
    lines.append("")

    lines += [
        "## 待办事项",
        "",
        "| 序号 | 事项 | 负责人 | 截止时间 |",
        "| --- | --- | --- | --- |",
    ]
    todos = analysis.get("todos") or []
    if todos:
        for index, item in enumerate(todos, 1):
            lines.append("| %d | %s | %s | %s |" % (
                index,
                truncate(item.get("task", ""), 80),
                item.get("owner") or "（未指定）",
                item.get("due") or "（未定）",
            ))
    else:
        lines.append("| - | （本次会议没有待办） | - | - |")
    lines.append("")

    lines += [
        "## 发言统计",
        "",
        "| 发言人 | 发言次数 | 字数 | 发言占比 | 平均单次长度 | 最长发言 |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for speaker, entry in sorted(facts["stats"].items(), key=lambda kv: -kv[1]["chars"]):
        lines.append("| %s | %d | %d | %.1f%% | %.1f 字 | %d 字：%s |" % (
            speaker,
            entry["count"],
            entry["chars"],
            entry["share"],
            entry["avg"],
            entry["longest"],
            truncate(entry["longest_text"], 30),
        ))
    lines.append("")

    lines += ["## 未决问题", ""]
    opens = facts["opens"]
    if opens:
        lines += ["%d. %s（%s）" % (index, item["text"], item["speaker"])
                  for index, item in enumerate(opens, 1)]
    else:
        lines.append("（没有识别到未决问题）")
    lines.append("")

    risks = analysis.get("risks") or []
    if risks:
        lines += ["## 风险与待协调事项", ""]
        lines += ["%d. %s" % (index, item) for index, item in enumerate(risks, 1)]
        lines.append("")

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

SAMPLE_TRANSCRIPT = (
    "[09:30] 张三：各位好，今天讨论支付模块上线安排。\n"
    "\n"
    "李四> 我这边压测已经完成，结果正常。\n"
    "[09:41] 王五: 我认为可以下周灰度，风险可控。\n"
    "\n"
    "议题一：灰度方案\n"
    "张三：决定下周一开始灰度，张三负责盯监控。\n"
    "[09:55] 李四：由王五跟进客户通知，10月30日前完成。\n"
    "王五：还有个问题待定：回滚方案还需要下次讨论。\n"
)


def run_self_test():
    """离线自检：三种逐字稿格式、统计、议题、决议/待办、模型兜底，不联网、不需要 key"""
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)

        # --- 三种逐字稿格式解析（说话人: / [12:03] 说话人：/ 说话人>） ---
        items = parse_transcript(SAMPLE_TRANSCRIPT)
        utterances = utterances_of(items)
        assert len(utterances) == 6, [(u["speaker"], u["text"]) for u in utterances]
        speakers = [u["speaker"] for u in utterances]
        assert speakers == ["张三", "李四", "王五", "张三", "李四", "王五"], speakers
        assert utterances[0]["time"] == "09:30"
        assert utterances[2]["time"] == "09:41"      # 裸时间戳 + 半角冒号
        assert utterances[4]["time"] == "09:55"
        assert "压测已经完成" in utterances[1]["text"]
        assert utterances[1]["time"] is None

        # --- 续行合并、空行容忍 ---
        multiline = "张三：第一行\n第二行还是张三\n\n李四：新发言\n"
        multi_items = parse_transcript(multiline)
        multi_utts = utterances_of(multi_items)
        assert len(multi_utts) == 2, multi_utts
        assert "第二行还是张三" in multi_utts[0]["text"]
        assert multi_utts[0]["text"].count("\n") == 1

        # --- 发言人统计数值 ---
        facts = collect_facts(items)
        stats = facts["stats"]
        assert set(stats) == {"张三", "李四", "王五"}, stats
        assert stats["张三"]["count"] == 2
        assert stats["李四"]["count"] == 2
        assert stats["王五"]["count"] == 2
        expected_chars = sum(
            _char_len(u["text"]) for u in utterances if u["speaker"] == "张三"
        )
        assert stats["张三"]["chars"] == expected_chars
        assert stats["张三"]["avg"] == round(expected_chars / 2, 1)
        assert abs(sum(entry["share"] for entry in stats.values()) - 100.0) < 1.0
        assert stats["王五"]["longest"] == max(
            _char_len(u["text"]) for u in utterances if u["speaker"] == "王五"
        )

        # --- 时间戳解析会议时段与时长 ---
        span = facts["span"]
        assert span["start"] == "09:30", span
        assert span["end"] == "09:55", span
        assert span["duration"] == 25, span
        no_ts = meeting_span(parse_transcript("张三：没有时间戳的发言。\n"))
        assert no_ts["duration"] == 0 and no_ts["start"] == ""

        # --- 议题切分：标记行切出 2 段，各有参与人与占比 ---
        topics = facts["topics"]
        assert len(topics) == 2, [seg["title"] for seg in topics]
        assert topics[1]["title"] == "灰度方案", topics[1]["title"]
        assert topics[1]["participants"] == ["张三", "李四", "王五"]
        assert topics[1]["share"] > 0
        assert topics[0]["share"] > 0

        # --- 无议题标记时按"说话人切换 + 空行"切段 ---
        plain = (
            "张三：第一段开场。\n"
            "\n"
            "李四：第二段发言。\n"
            "\n"
            "王五> 第三段发言。\n"
        )
        seg_items = parse_transcript(plain)
        seg_topics = split_topics(seg_items)
        assert len(seg_topics) == 3, [seg["title"] for seg in seg_topics]
        assert seg_topics[0]["title"].startswith("议题 1")
        assert seg_topics[2]["participants"] == ["王五"]

        # --- 决议 / 待办抽取（含负责人与时间） ---
        decisions = facts["decisions"]
        assert any("决定下周一开始灰度" in item["text"] for item in decisions), decisions
        todos = facts["todos"]
        assert todos, todos
        owner_line = [item for item in todos if "跟进客户通知" in item["task"]]
        assert owner_line and owner_line[0]["owner"] == "王五", owner_line
        assert owner_line[0]["due"] == "10月30日前", owner_line
        self_line = [item for item in todos if "盯监控" in item["task"]]
        assert self_line and self_line[0]["owner"] == "张三", self_line
        assert extract_owner("负责人：赵六 跟进") == "赵六"
        assert extract_owner("这个事情需要尽快推进，没有指定人") == ""
        assert extract_due("10月30日前完成") == "10月30日前"
        assert extract_due("下周三前给结论") == "下周三前"

        # --- 未决问题 ---
        opens = facts["opens"]
        assert any("回滚方案还需要下次讨论" in item["text"] for item in opens), opens

        # --- 模型回复 JSON 解析兜底 ---
        good = (
            "```json\n"
            '{"background": "评审上线", "topics": [{"title": "灰度", "discussion": "按周灰度",'
            ' "opinion": "王五支持"}], "decisions": ["下周一灰度"],'
            ' "todos": [{"task": "盯监控", "owner": "张三", "due": "下周一"}], "risks": ["回滚"]}\n'
            "```"
        )
        parsed = parse_model_json(good)
        assert parsed is not None
        assert parsed["background"] == "评审上线"
        assert parsed["topics"][0]["title"] == "灰度"
        assert parsed["todos"][0]["owner"] == "张三"
        assert parsed["decisions"] == ["下周一灰度"]
        assert parse_model_json("先说结论：可以做。然后省略一千字。") is None
        assert parse_model_json("") is None
        assert parse_model_json('{"topics": "不是数组", "decisions": []}')["topics"] == []

        # --- 解析失败时用确定性抽取结果兜底 ---
        fallback = deterministic_minutes(facts, "支付模块评审", "2026-10-28")
        assert fallback["background"]
        assert fallback["topics"] and fallback["topics"][0]["title"]
        assert fallback["decisions"], fallback["decisions"]
        assert fallback["todos"], fallback["todos"]

        # --- 报告渲染（含模型原文附录） ---
        meta = {
            "title": "支付模块评审",
            "date": "2026-10-28",
            "source": "会议记录.txt",
            "mode": "模型分析（claude-opus-5）",
        }
        report_raw = build_report(
            facts, fallback, meta, raw_output="自由格式意见",
            generated_at="2026-10-28 12:00:00",
        )
        assert report_raw.startswith("# 会议纪要：支付模块评审")
        assert "## 发言统计" in report_raw
        assert "## 未决问题" in report_raw
        assert "附录：模型原始输出" in report_raw
        assert "自由格式意见" in report_raw
        assert "王五" in report_raw and "灰度方案" in report_raw

        # --- 文件读取路径 + 落盘 ---
        transcript_path = root / "会议记录.txt"
        transcript_path.write_text(SAMPLE_TRANSCRIPT, encoding="utf-8")
        file_items = parse_transcript(transcript_path.read_text(encoding="utf-8"))
        assert len(utterances_of(file_items)) == 6
        out_path = root / "minutes.md"
        out_path.write_text(report_raw, encoding="utf-8")
        assert out_path.read_text(encoding="utf-8").startswith("# 会议纪要")

        # --- call_model 三级降级（离线假客户端） ---
        fake = _FakeClient()
        result = call_model(fake, "fake-model", "system", [{"role": "user", "content": "hi"}])
        assert result["ok"] is True
        assert fake.state["calls"] == 2

    print("SELF-TEST OK")


# ---------- CLI ----------

def build_parser():
    parser = argparse.ArgumentParser(
        description="会议纪要助手：解析逐字稿做发言人统计、议题切分、决议与待办抽取，生成中文会议纪要",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            "  python3 agent.py 会议记录.txt --title 支付模块评审 --date 2026-10-28\n"
            "  python3 agent.py --text \"张三：决定下周灰度。李四：我负责监控。\" --offline\n"
            "  python3 agent.py 会议记录.txt --model qwen3.8-max --output 纪要.md\n"
        ),
    )
    parser.add_argument(
        "transcript", nargs="?",
        help="逐字稿文本文件（支持 '说话人: 内容'、'[12:03] 说话人：内容'、'说话人> 内容'）",
    )
    parser.add_argument(
        "--text", default="",
        help="直接把逐字稿内容作为参数传入（可与文件参数同时用，内容拼在文件之后）",
    )
    parser.add_argument(
        "--title", default="",
        help="会议主题（不给则用文件名或'未命名会议'）",
    )
    parser.add_argument(
        "--date", default="",
        help="会议日期，如 2026-10-28（不给则留空）",
    )
    parser.add_argument(
        "--output", default=DEFAULT_OUTPUT,
        help="纪要写入路径（默认 %s）" % DEFAULT_OUTPUT,
    )
    parser.add_argument(
        "--offline", action="store_true",
        help="纯确定性模式：只做解析、统计与规则抽取，不调用模型、不需要 API key",
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

    if not args.transcript and not args.text.strip():
        parser.error("请提供逐字稿文件，或用 --text 直接传入逐字稿内容")

    parts = []
    source = "--text 参数"
    if args.transcript:
        path = Path(args.transcript).expanduser()
        if not path.exists():
            print("错误：找不到逐字稿文件：%s" % path, file=sys.stderr)
            sys.exit(1)
        try:
            parts.append(path.read_text(encoding="utf-8", errors="replace"))
        except OSError as exc:
            print("错误：读取逐字稿失败：%s" % exc, file=sys.stderr)
            sys.exit(1)
        source = path.name
    if args.text.strip():
        parts.append(args.text)
        if args.transcript:
            source = "%s + --text 参数" % path.name
    text = "\n".join(parts)

    items = parse_transcript(text)
    facts = collect_facts(items)
    if not facts["utterances"]:
        print("错误：没有解析到任何发言人发言，请检查逐字稿格式。", file=sys.stderr)
        sys.exit(1)
    print("[解析] 发言 %d 条，参与人 %d 位，议题 %d 个"
          % (len(facts["utterances"]), len(facts["stats"]), len(facts["topics"])))

    title = args.title or (Path(args.transcript).stem if args.transcript else "未命名会议")
    raw_output = None
    if args.offline:
        analysis = deterministic_minutes(facts, title, args.date)
        mode = "离线确定性模式（--offline）"
        print("[离线] 使用确定性模式，未调用模型")
    else:
        if not os.environ.get("ANTHROPIC_API_KEY") and not os.environ.get("ANTHROPIC_AUTH_TOKEN"):
            print(
                "提示：没有检测到 ANTHROPIC_API_KEY 或 ANTHROPIC_AUTH_TOKEN，无法调用模型。\n"
                "请先设置 API key：\n"
                "    macOS / Linux：export ANTHROPIC_API_KEY=你的key\n"
                "    Windows PowerShell：$env:ANTHROPIC_API_KEY=\"你的key\"\n"
                "也可以加 --offline 使用纯确定性模式（不需要 key）。",
                file=sys.stderr,
            )
            sys.exit(1)
        mode = "模型分析（%s）" % args.model
        print("[模型] 模型：%s" % args.model)
        try:
            analysis, raw_output = analyze_with_model(facts, args.model, title, args.date)
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
            print("[提示] 模型输出不是合法 JSON，已改用确定性抽取结果生成纪要。")
            analysis = deterministic_minutes(facts, title, args.date)

    meta = {"title": title, "date": args.date, "source": source, "mode": mode}
    report = build_report(facts, analysis, meta, raw_output=raw_output)
    output_path = Path(args.output).expanduser()
    try:
        if output_path.parent and not output_path.parent.exists():
            output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(report, encoding="utf-8")
    except OSError as exc:
        print("错误：纪要写入失败：%s" % exc, file=sys.stderr)
        sys.exit(1)

    print("[完成] 纪要已写入：%s" % output_path)
    print("[完成] 发言 %d 条、议题 %d 个、决议 %d 条、待办 %d 条、未决问题 %d 条"
          % (
              len(facts["utterances"]),
              len(analysis.get("topics") or facts["topics"]),
              len(analysis.get("decisions") or []),
              len(analysis.get("todos") or []),
              len(facts["opens"]),
          ))
    if raw_output:
        print("[提示] 模型输出不是合法 JSON，原文已放入纪要附录。")


if __name__ == "__main__":
    main()
