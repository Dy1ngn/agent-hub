#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
幻灯片生成助手 —— 大纲提炼 + Marp 幻灯片生成 的 agent

输入一个主题（--topic）或一份现有文档（--from 文档.md），先做完全确定性的文档提炼
（按标题切章节、每章取要点句、词频启发式提取关键词），再排成 Marp 幻灯片结构
（封面 / 目录 / 章节内容页 / 小结 / Q&A），自动控制每页 ≤5 个要点、每要点 ≤30 字，
超出自动拆页，最后（可选）让模型按页产出标题、要点、演讲备注与版式。
输出 slides.md，可直接用 Marp CLI 导出 PDF / PPTX。除 anthropic SDK 外只用标准库。

用法：
    python3 agent.py --topic "主题" [选项]
    python3 agent.py --from 文档.md [选项]

示例：
    python3 agent.py --topic "Kubernetes 上手" --slides 12 --audience "后端工程师"
    python3 agent.py --from 方案.md --topic "方案评审" --output 评审slides.md
    python3 agent.py --topic "季度汇报" --offline

环境要求：
    1. 模型生成每页内容需要设置 API key（--offline 与 --self-test 不需要 key）：
           macOS / Linux：      export ANTHROPIC_API_KEY=你的key
           Windows PowerShell： $env:ANTHROPIC_API_KEY="你的key"
       也可以用 ANTHROPIC_AUTH_TOKEN 代替 ANTHROPIC_API_KEY。
    2. 安装依赖：pip3 install -r requirements.txt
    3. 走第三方 Anthropic 兼容网关时，用 --model 指定网关支持的模型名，
       例如 --model qwen3.8-max，并自行设置 ANTHROPIC_BASE_URL。
"""

import argparse
import json
import os
import re
import sys
import tempfile
from collections import Counter, OrderedDict
from datetime import datetime
from pathlib import Path

import anthropic

# 默认模型（可用 --model 参数或 SLIDE_MAKER_MODEL 环境变量更换）
DEFAULT_MODEL = os.environ.get("SLIDE_MAKER_MODEL", "claude-opus-5")

DEFAULT_OUTPUT = "slides.md"
DEFAULT_SLIDES = 12
MAX_BULLETS_PER_PAGE = 5      # 每页最多几个要点
MAX_BULLET_CHARS = 30         # 每个要点最多多少字
PAGE_TARGET_TOLERANCE = 2     # 页数与 --slides 相差多少算"偏离目标"

LAYOUTS = ("content", "chart", "quote")
LAYOUT_LABELS = {"content": "图文", "chart": "图表", "quote": "金句"}

# 没有文档输入时的确定性大纲模板
TEMPLATE_SECTIONS = (
    "背景与目标",
    "现状与问题",
    "核心概念",
    "方案与实践",
    "案例分析",
    "总结与展望",
)
TEMPLATE_BULLETS = {
    "背景与目标": ["为什么讲{topic}", "{audience}最关心的问题", "本次分享的目标"],
    "现状与问题": ["当前做法与瓶颈", "典型问题清单", "问题带来的影响"],
    "核心概念": ["关键概念一图讲清", "概念之间的关系", "常见误解澄清"],
    "方案与实践": ["整体思路与步骤", "落地的关键动作", "实践中的取舍"],
    "案例分析": ["真实场景还原", "改了什么、效果如何", "可复用的经验"],
    "总结与展望": ["三条核心结论", "下一步计划", "给{audience}的建议"],
}

# ---------- 正则 ----------

MD_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
LIST_RE = re.compile(r"^(\s*)([-*+]|\d+[.)])\s+")
FENCE_RE = re.compile(r"^\s*(```|~~~)")
EMPH_RE = re.compile(r"(\*\*|__|\*|_|~~)")
LINK_RE = re.compile(r"!?\[([^\]]*)\]\([^)]*\)")
INLINE_CODE_RE = re.compile(r"`+([^`]*)`+")
SENT_END_RE = re.compile(r"(?<=[。！？；!?;])")
HAN_RUN_RE = re.compile(r"[\u4e00-\u9fff]+")
WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9_\-]*")
DIGIT_RE = re.compile(r"\d")

ZH_STOPWORDS = set("""
我们 你们 他们 这个 那个 一个 什么 怎么 因为 所以 但是 如果 就是 没有 自己 这样 这些 那些
以及 通过 进行 对于 关于 并且 或者 一些 一样 一直 已经 可以 可能 需要 应该 非常 其中 方面
然后 现在 时候 之后 之前 以下 以上 其实 尤其 由于 因此 不过 而且 同时 相关 主要 重要 基本
""".split())

EN_STOPWORDS = set("""
the a an and or of to in for on with is are was were be been this that it as at by from
not no but if then than so such can will would should may might must have has had do does did
we you they he she i its our your their there here what which who when where how why all any
""".split())


# ---------- 文档提炼 ----------

def strip_inline(text):
    """去掉行内 Markdown 符号"""
    text = LINK_RE.sub(r"\1", text)
    text = INLINE_CODE_RE.sub(r"\1", text)
    text = EMPH_RE.sub("", text)
    return text.strip()


def split_sentences(text):
    """切句子，保留标点"""
    sentences = []
    for line in text.split("\n"):
        line = line.strip()
        if not line:
            continue
        for piece in SENT_END_RE.split(line):
            piece = piece.strip()
            if piece:
                sentences.append(piece)
    return sentences


def extract_doc_outline(text):
    """从文档提炼大纲：按标题切章节，每章取要点句，返回 [{title, level, bullets, keywords}]"""
    sections = []
    current = {"title": "（前言）", "level": 1, "bullets": []}
    in_fence = False
    lines = text.split("\n")
    if lines and lines[0].strip() == "---":          # 头部 YAML frontmatter 整块丢掉
        for index in range(1, min(len(lines), 40)):
            if lines[index].strip() == "---":
                lines = lines[index + 1:]
                break
    for raw_line in lines:
        line = raw_line.rstrip()
        if FENCE_RE.match(line):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        heading = MD_HEADING_RE.match(line.strip())
        if heading:
            sections.append(current)
            current = {"title": strip_inline(heading.group(2)) or "（无标题）", "level": len(heading.group(1)), "bullets": []}
            continue
        stripped = line.strip()
        if not stripped:
            continue
        if re.match(r"^[-=_*]{2,}$", stripped):
            continue                                  # 单独的分隔线不当要点
        listed = LIST_RE.sub("", stripped)
        if listed != stripped:                       # 列表项 → 直接作为要点
            current["bullets"].append(strip_inline(listed))
        else:                                        # 段落 → 取首句作为要点
            plain = strip_inline(stripped)
            sentences = split_sentences(plain)
            current["bullets"].append(sentences[0] if sentences else plain)
    sections.append(current)
    sections = [section for section in sections if section["title"] != "（前言）" or section["bullets"]]
    for section in sections:
        section["keywords"] = [word for word, _ in extract_keywords(" ".join(section["bullets"]), 5)]
    return sections


def extract_keywords(text, top_n=8):
    """中英文混合词频启发式：英文单词 + 中文 2/3/4-gram，去掉停用词与互相包含的冗余词"""
    counter = Counter()
    for run in HAN_RUN_RE.findall(text):
        for size in (2, 3, 4):
            for index in range(0, len(run) - size + 1):
                gram = run[index:index + size]
                if gram not in ZH_STOPWORDS:
                    counter[gram] += 1
    for word in WORD_RE.findall(text):
        lowered = word.lower()
        if len(lowered) >= 2 and lowered not in EN_STOPWORDS:
            counter[lowered] += 1
    ordered = sorted(counter.items(), key=lambda item: (-item[1], -len(item[0]), item[0]))
    chosen = []
    for word, count in ordered:
        if any(word in kept for kept in chosen):
            continue
        if any(kept in word and counter[kept] == count for kept in chosen):
            continue
        chosen.append(word)
        if len(chosen) >= top_n:
            break
    return [(word, counter[word]) for word in chosen]


def template_sections(topic, audience):
    """没有文档输入时的确定性大纲模板"""
    topic = topic or "主题"
    audience = audience or "听众"
    sections = []
    for title in TEMPLATE_SECTIONS:
        bullets = [
            bullet.replace("{topic}", topic).replace("{audience}", audience)
            for bullet in TEMPLATE_BULLETS[title]
        ]
        sections.append({"title": title, "level": 2, "bullets": bullets, "keywords": []})
    return sections


# ---------- 排版与拆页 ----------

def shorten_bullet(text, limit=MAX_BULLET_CHARS):
    """要点截断到 limit 字，超出加省略号"""
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[:limit - 1] + "…"


def enforce_bullet_limit(pages):
    """要点密度检查：超过每页上限的自动拆页（拆出的页标题加"（续N）"）"""
    result = []
    for page in pages:
        bullets = [shorten_bullet(item) for item in page["bullets"] if item.strip()]
        if len(bullets) <= MAX_BULLETS_PER_PAGE:
            new_page = dict(page)
            new_page["bullets"] = bullets
            result.append(new_page)
            continue
        chunks = [
            bullets[index:index + MAX_BULLETS_PER_PAGE]
            for index in range(0, len(bullets), MAX_BULLETS_PER_PAGE)
        ]
        for offset, chunk in enumerate(chunks):
            title = page["title"] if offset == 0 else "%s（续%d）" % (page["title"], offset)
            notes = page["notes"] if offset == 0 else "承接上一页继续讲%s。" % page["title"]
            new_page = dict(page)
            new_page["title"] = title
            new_page["bullets"] = chunk
            new_page["notes"] = notes
            result.append(new_page)
    return result


def guess_layout(bullets, title):
    """按内容猜版式：数字密集 → 图表；短句像金句 → 金句；其余图文"""
    joined = " ".join(bullets)
    digits = len(DIGIT_RE.findall(joined))
    if digits >= 6 or "%" in joined:
        return "chart"
    if len(bullets) == 1 and len(bullets[0]) <= MAX_BULLET_CHARS and "：" not in bullets[0]:
        return "quote"
    return "content"


def build_content_pages(sections, chunk_size):
    """把章节要点按 chunk_size 切成内容页"""
    pages = []
    for section in sections:
        if not section["bullets"]:
            continue                       # 没有要点的章节（如文档大标题）不再单开内容页
        bullets = section["bullets"]
        for index in range(0, len(bullets), chunk_size):
            chunk = bullets[index:index + chunk_size]
            offset = index // chunk_size
            title = section["title"] if offset == 0 else "%s（续%d）" % (section["title"], offset)
            pages.append({
                "title": title,
                "bullets": chunk,
                "notes": "本页讲「%s」，重点讲第一条：%s。控制在 2 分钟内。"
                         % (section["title"], shorten_bullet(chunk[0], 20)),
                "layout": guess_layout(chunk, section["title"]),
                "kind": "content",
            })
    return pages


def build_deck(topic, sections, target_slides, audience, keywords):
    """组装整套幻灯片：封面、目录、内容页、小结、Q&A"""
    content_target = max(1, (target_slides or DEFAULT_SLIDES) - 4)
    best = None
    for chunk_size in (5, 4, 3):
        pages = build_content_pages(sections, chunk_size)
        score = abs(len(pages) - content_target)
        if best is None or score < best[0]:
            best = (score, chunk_size, pages)
    content_pages = best[2]

    deck = []
    deck.append({
        "title": topic,
        "bullets": [
            "面向：%s" % (audience or "通用听众"),
            "日期：%s" % datetime.now().strftime("%Y-%m-%d"),
        ],
        "notes": "开场：自我介绍，说明今天围绕「%s」展开，全篇约 %d 页，预计讲 %d 分钟。"
                 % (topic, target_slides or (len(content_pages) + 4), max(5, len(content_pages) * 2)),
        "layout": "quote",
        "kind": "cover",
    })
    toc_bullets = [shorten_bullet(section["title"], MAX_BULLET_CHARS) for section in sections][:MAX_BULLETS_PER_PAGE]
    deck.append({
        "title": "目录",
        "bullets": toc_bullets or ["（目录为空）"],
        "notes": "用 30 秒过一遍目录，告诉大家重点在中间两章。",
        "layout": "content",
        "kind": "toc",
    })
    deck.extend(content_pages)

    summary_bullets = []
    for section in sections:
        if section["bullets"]:
            summary_bullets.append(shorten_bullet(section["bullets"][0], MAX_BULLET_CHARS))
    for word, _ in (keywords or [])[:2]:
        summary_bullets.append("关键词：%s" % shorten_bullet(word, MAX_BULLET_CHARS - 3))
    deck.append({
        "title": "小结",
        "bullets": summary_bullets[:MAX_BULLETS_PER_PAGE] or ["（小结待补充）"],
        "notes": "回顾全篇结论，重申最想让听众记住的三件事。",
        "layout": "content",
        "kind": "summary",
    })
    deck.append({
        "title": "Q&A",
        "bullets": ["感谢聆听", "欢迎提问与交流"],
        "notes": "预留 5 分钟提问；没人提问就抛一个引导问题收尾。",
        "layout": "quote",
        "kind": "qa",
    })
    return enforce_bullet_limit(deck)


# ---------- Marp 渲染与结构校验 ----------

def render_marp(deck, topic):
    """渲染成标准 Marp Markdown：头部指令 + --- 分页 + 页脚页码 + 演讲备注"""
    total = len(deck)
    lines = ["---", "marp: true", "theme: default", "paginate: true", "---", ""]
    for index, page in enumerate(deck, 1):
        if index > 1:
            lines.append("---")
            lines.append("")
        lines.append("<!-- _footer: %s · 第 %d/%d 页 -->" % (topic, index, total))
        lines.append("")
        lines.append("# %s" % page["title"])
        lines.append("")
        for bullet in page["bullets"]:
            lines.append("- %s" % bullet)
        lines.append("")
        if page.get("notes"):
            lines.append("<!-- notes: %s -->" % page["notes"])
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def parse_pages(markdown):
    """按 Marp 结构解析页面：剥掉头部 front matter 后按 --- 切页"""
    text = re.sub(r"\A---\n.*?\n---\n", "", markdown, count=1, flags=re.S)
    return [page for page in re.split(r"\n---\n", text) if page.strip()]


def count_page_separators(markdown):
    """页面分隔符数量（不含 front matter 的两条 ---）"""
    return max(0, len(parse_pages(markdown)) - 1)


def validate_deck(deck, target_slides):
    """结构校验：页数与目标差异、每页要点数、要点长度，返回中文提示列表"""
    hints = []
    total = len(deck)
    if target_slides and abs(total - target_slides) > PAGE_TARGET_TOLERANCE:
        hints.append(
            "页数 %d 与 --slides 目标 %d 相差 %d 页，可调 --slides 或给更细的大纲。"
            % (total, target_slides, abs(total - target_slides))
        )
    for index, page in enumerate(deck, 1):
        if len(page["bullets"]) > MAX_BULLETS_PER_PAGE:
            hints.append("第 %d 页「%s」要点 %d 个，超过每页 %d 个上限（应已被自动拆页）。"
                         % (index, page["title"], len(page["bullets"]), MAX_BULLETS_PER_PAGE))
        for bullet in page["bullets"]:
            if len(bullet) > MAX_BULLET_CHARS:
                hints.append("第 %d 页要点超过 %d 字：%s" % (index, MAX_BULLET_CHARS, bullet))
    return hints


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


DECK_SYSTEM_PROMPT = """你是一名资深的幻灯片策划。用户会给你主题、听众和大纲，请按页产出幻灯片内容。

只输出一个 JSON 数组，不要解释、不要 Markdown 代码围栏，数组每个元素是一 页：
- title：页标题，12 字以内
- bullets：要点数组，每页 3~5 条，每条不超过 30 字，用短句，不要句号结尾
- notes：这一页的演讲备注（中文，30~80 字）
- layout：只能是 content / chart / quote 之一（数据多用 chart，观点金句用 quote）

第 1 页是封面、第 2 页是目录，中间是内容页，倒数第 2 页是小结，最后一页是 Q&A。
全部用中文，不要编造用户没提供的数据。"""


def build_deck_prompt(topic, audience, sections, target_slides):
    """拼装模型提示词"""
    parts = [
        "主题：%s" % topic,
        "听众：%s" % (audience or "通用听众"),
        "目标页数：%d" % (target_slides or DEFAULT_SLIDES),
        "",
        "大纲：",
    ]
    for index, section in enumerate(sections, 1):
        parts.append("%d. %s" % (index, section["title"]))
        for bullet in section["bullets"][:5]:
            parts.append("   - %s" % bullet)
    return "\n".join(parts)


def parse_deck_json(text):
    """尽力把模型回复解析成页面数组；解析不出来返回 None"""
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
    pages = []
    for item in data:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "").strip()
        raw_bullets = item.get("bullets")
        bullets = []
        if isinstance(raw_bullets, list):
            for raw in raw_bullets:
                value = str(raw or "").strip()
                if value:
                    bullets.append(shorten_bullet(value))
        if not title and not bullets:
            continue
        layout = str(item.get("layout") or "content").strip()
        if layout not in LAYOUTS:
            layout = "content"
        pages.append({
            "title": title or "（无标题）",
            "bullets": bullets,
            "notes": str(item.get("notes") or "").strip(),
            "layout": layout,
            "kind": "model",
        })
    return pages or None


def generate_deck_with_model(client, model, topic, audience, sections, target_slides):
    """调模型按页生成内容，返回 (页面列表或 None, 模型原文)"""
    response = call_model(
        client, model, DECK_SYSTEM_PROMPT,
        [{"role": "user", "content": build_deck_prompt(topic, audience, sections, target_slides)}],
        max_tokens=8000,
    )
    raw = response_text(response)
    return parse_deck_json(raw), raw


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

        # --- 文档提炼：章节与要点数量 ---
        doc = (
            "# Kubernetes 上手\n\n"
            "## 为什么用 Kubernetes\n\n"
            "- 容器编排标准化\n"
            "- 声明式配置好管理\n"
            "- 社区生态成熟\n\n"
            "Kubernetes 让发布变得可重复。\n\n"
            "## 核心概念\n\n"
            "- Pod 是最小调度单位\n"
            "- Service 负责服务发现\n"
            "- Deployment 管理版本\n"
            "- ConfigMap 管配置\n"
            "- Secret 管密钥\n\n"
            "## 部署实践\n\n"
            "- 先小范围灰度\n"
            "- 再全量发布\n"
        )
        sections = extract_doc_outline(doc)
        titles = [section["title"] for section in sections]
        assert len(sections) == 4, "章节数不对：%d" % len(sections)
        assert titles == ["Kubernetes 上手", "为什么用 Kubernetes", "核心概念", "部署实践"], titles
        assert sections[0]["level"] == 1 and sections[1]["level"] == 2
        assert len(sections[0]["bullets"]) == 0
        assert len(sections[1]["bullets"]) == 4, "要点数不对：%s" % sections[1]["bullets"]
        assert len(sections[2]["bullets"]) == 5
        assert len(sections[3]["bullets"]) == 2
        assert any("Kubernetes 让发布变得可重复" in item for item in sections[1]["bullets"])
        keywords = extract_keywords(doc * 3, 5)
        words = [word for word, _ in keywords]
        assert "kubernetes" in words, "关键词没命中：%s" % words

        # --- 排版：要点截断与自动拆页 ---
        assert len(shorten_bullet("一" * 40)) == MAX_BULLET_CHARS
        assert shorten_bullet("一" * 40).endswith("…")
        assert shorten_bullet("短句") == "短句"
        crowded = [{
            "title": "要点过多的页",
            "bullets": ["要点%d" % index for index in range(12)],
            "notes": "备注",
            "layout": "content",
            "kind": "content",
        }]
        split_pages = enforce_bullet_limit(crowded)
        assert len(split_pages) == 3, "自动拆页结果不对：%d" % len(split_pages)
        assert all(len(page["bullets"]) <= MAX_BULLETS_PER_PAGE for page in split_pages)
        assert split_pages[0]["title"] == "要点过多的页"
        assert split_pages[1]["title"] == "要点过多的页（续1）"
        assert len(split_pages[0]["bullets"]) == 5 and len(split_pages[2]["bullets"]) == 2

        # --- 整套幻灯片与 Marp 渲染 ---
        topic = "Kubernetes 上手"
        deck = build_deck(topic, sections, 12, "后端工程师", keywords)
        assert deck[0]["kind"] == "cover" and deck[0]["title"] == topic
        assert deck[1]["kind"] == "toc"
        assert deck[-1]["kind"] == "qa" and deck[-2]["kind"] == "summary"
        markdown = render_marp(deck, topic)
        assert markdown.startswith("---\nmarp: true\ntheme: default\npaginate: true\n---\n")
        pages = parse_pages(markdown)
        assert len(pages) == len(deck), "页数与渲染结果不一致"
        assert count_page_separators(markdown) == len(deck) - 1, "真页面分隔符应为页数减一"
        assert markdown.count("\n---\n") == len(deck), "分隔符数量应等于页数（含 front matter 收尾）"
        for index, page_text in enumerate(pages, 1):
            assert "第 %d/%d 页" % (index, len(deck)) in page_text, "页脚缺页码"
            assert topic in page_text, "页脚缺主题名"
            assert "<!-- notes: " in page_text, "缺演讲备注"
        for page in deck:
            assert len(page["bullets"]) <= MAX_BULLETS_PER_PAGE
            assert all(len(bullet) <= MAX_BULLET_CHARS for bullet in page["bullets"])
        assert "## " not in markdown, "Marp 页面正文应使用一级标题"

        # --- 结构校验：页数目标差异提示 ---
        hints = validate_deck(deck, 12)
        assert not any("超过每页" in hint or "要点超过" in hint for hint in hints), "正常 deck 不应有要点超限提示"
        far_hints = validate_deck(deck, 30)
        assert any("与 --slides 目标" in hint for hint in far_hints), "页数偏离应有提示"
        bad_page = [{"title": "超限", "bullets": ["要点%d" % i for i in range(6)], "notes": "", "layout": "content", "kind": "content"}]
        bad_hints = validate_deck(bad_page, 2)
        assert any("超过每页" in hint for hint in bad_hints), "要点超限应有提示"
        long_hints = validate_deck(
            [{"title": "长句", "bullets": ["超" * 60], "notes": "", "layout": "content", "kind": "content"}], 1
        )
        assert any("要点超过" in hint for hint in long_hints)

        # --- 只给主题时的确定性大纲模板 ---
        template = template_sections("季度汇报", "管理层")
        assert [section["title"] for section in template] == list(TEMPLATE_SECTIONS)
        assert all(len(section["bullets"]) == 3 for section in template)
        assert "为什么讲季度汇报" in template[0]["bullets"]
        assert "管理层最关心的问题" in template[0]["bullets"]
        template_deck = build_deck("季度汇报", template, 10, "管理层", [])
        template_md = render_marp(template_deck, "季度汇报")
        assert len(parse_pages(template_md)) == len(template_deck)

        # --- 模型回复 JSON 解析兜底 ---
        good = (
            "```json\n"
            '[{"title":"封面","bullets":["面向：后端工程师"],"notes":"开场","layout":"quote"},'
            ' {"title":"要点","bullets":["要点一","要点二"],"notes":"讲解","layout":"content"}]\n'
            "```"
        )
        parsed = parse_deck_json(good)
        assert parsed and len(parsed) == 2
        assert parsed[0]["layout"] == "quote" and parsed[1]["bullets"] == ["要点一", "要点二"]
        assert parse_deck_json("这不是 JSON") is None
        assert parse_deck_json("[]") is None
        assert parse_deck_json("") is None
        weird = '[{"title":"页","bullets":["%s"],"notes":"","layout":"不存在"}]' % ("长" * 80)
        coerced = parse_deck_json(weird)
        assert coerced and len(coerced[0]["bullets"][0]) <= MAX_BULLET_CHARS
        assert coerced[0]["layout"] == "content", "非法 layout 应回退成 content"

        # 模型解析失败时仍输出合法 Marp
        fallback_deck = parsed if parsed else deck
        fallback_md = render_marp(fallback_deck, topic)
        assert len(parse_pages(fallback_md)) == len(fallback_deck)
        assert count_page_separators(fallback_md) == len(fallback_deck) - 1

        # --- call_model 逐级降级（离线假客户端） ---
        fake = _FakeClient()
        result = call_model(fake, "fake-model", "system", [{"role": "user", "content": "hi"}])
        assert result["ok"] is True
        assert fake.state["calls"] == 2

        # --- 落盘 ---
        output_path = root / "slides.md"
        output_path.write_text(markdown, encoding="utf-8")
        saved = output_path.read_text(encoding="utf-8")
        assert saved.startswith("---\nmarp: true")
        assert len(parse_pages(saved)) == len(deck)

    print("SELF-TEST OK")


# ---------- CLI ----------

def build_parser():
    parser = argparse.ArgumentParser(
        description="幻灯片生成助手：从主题或文档提炼大纲，生成可直接导出 PDF/PPTX 的 Marp 幻灯片",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            "  python3 agent.py --topic \"Kubernetes 上手\" --slides 12 --audience \"后端工程师\"\n"
            "  python3 agent.py --from 方案.md --topic \"方案评审\" --output 评审slides.md\n"
            "  python3 agent.py --topic \"季度汇报\" --offline\n"
            "  python3 agent.py --topic \"技术分享\" --model qwen3.8-max\n"
        ),
    )
    parser.add_argument("--topic", help="幻灯片主题（不给时用 --from 文档的第一个标题）")
    parser.add_argument("--slides", type=int, default=DEFAULT_SLIDES, metavar="N",
                        help="目标页数（默认 %d，与实际相差超过 %d 页会提示）" % (DEFAULT_SLIDES, PAGE_TARGET_TOLERANCE))
    parser.add_argument("--audience", default="通用听众", help="听众是谁，影响措辞与备注（默认 通用听众）")
    parser.add_argument("--from", dest="from_doc", metavar="文档", help="从现有文档提炼大纲（支持 .md / .txt）")
    parser.add_argument("--output", default=DEFAULT_OUTPUT, help="Marp 幻灯片写入路径（默认 %s）" % DEFAULT_OUTPUT)
    parser.add_argument("--offline", action="store_true", help="纯确定性模式：用文档提炼/大纲模板直接排版，不调用模型")
    parser.add_argument("--model", default=DEFAULT_MODEL, help="生成每页内容使用的模型（默认 %s）" % DEFAULT_MODEL)
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

    if not args.topic and not args.from_doc:
        parser.error("请给 --topic 主题，或用 --from 指定要提炼的文档")
    if args.slides < 5:
        parser.error("--slides 不能小于 5（封面、目录、内容、小结、Q&A 至少各一页）")

    sections = []
    if args.from_doc:
        file_path = Path(args.from_doc).expanduser()
        if not file_path.exists():
            print("错误：找不到文档：%s" % args.from_doc, file=sys.stderr)
            sys.exit(1)
        try:
            text = file_path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            text = file_path.read_bytes().decode("utf-8", errors="replace")
        except OSError as exc:
            print("错误：读取文档失败：%s" % exc, file=sys.stderr)
            sys.exit(1)
        sections = extract_doc_outline(text)
        print("[提炼] %s：%d 个章节，%d 个要点"
              % (args.from_doc, len(sections), sum(len(s["bullets"]) for s in sections)))
        if not args.topic:
            args.topic = sections[0]["title"] if sections else file_path.stem

    topic = args.topic or "未命名主题"
    if not sections:
        sections = template_sections(topic, args.audience)
        print("[大纲] 未提供文档，使用确定性大纲模板（%d 个章节）" % len(sections))

    keywords = extract_keywords(
        " ".join([topic] + [bullet for section in sections for bullet in section["bullets"]]), 8
    )
    deck = build_deck(topic, sections, args.slides, args.audience, keywords)

    raw_model_text = ""
    used_model_pages = False
    if args.offline:
        print("[模式] --offline：跳过模型，直接用确定性排版")
    else:
        if not os.environ.get("ANTHROPIC_API_KEY") and not os.environ.get("ANTHROPIC_AUTH_TOKEN"):
            print(
                "提示：没有检测到 ANTHROPIC_API_KEY 或 ANTHROPIC_AUTH_TOKEN，无法调用模型生成每页内容。\n"
                "请先设置：\n"
                "  macOS / Linux：      export ANTHROPIC_API_KEY=你的key\n"
                "  Windows PowerShell： $env:ANTHROPIC_API_KEY=\"你的key\"\n"
                "也可以加 --offline 使用确定性大纲直接排版。",
                file=sys.stderr,
            )
            sys.exit(1)
        print("[模型] %s 生成每页内容……" % args.model)
        try:
            model_pages, raw_model_text = generate_deck_with_model(
                anthropic.Anthropic(), args.model, topic, args.audience, sections, args.slides
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
        if model_pages:
            deck = enforce_bullet_limit(model_pages)
            used_model_pages = True
            print("[模型] 生成成功，共 %d 页" % len(deck))
        else:
            print("[模型] 返回内容不是合法 JSON，已回退到确定性大纲（仍然输出合法 Marp）")

    markdown = render_marp(deck, topic)
    output_path = Path(args.output).expanduser()
    try:
        if output_path.parent and not output_path.parent.exists():
            output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(markdown, encoding="utf-8")
    except OSError as exc:
        print("错误：幻灯片写入失败：%s" % exc, file=sys.stderr)
        sys.exit(1)

    print("[大纲] 共 %d 页（目标 %d 页）：" % (len(deck), args.slides))
    for index, page in enumerate(deck, 1):
        print("  %2d. %s（%d 个要点，%s）"
              % (index, page["title"], len(page["bullets"]), LAYOUT_LABELS.get(page["layout"], page["layout"])))
    for hint in validate_deck(deck, args.slides):
        print("[提示] %s" % hint)
    print("[完成] Marp 幻灯片已写入：%s" % output_path)
    print("[提示] 可用 marp-cli 导出：npx @marp-team/marp-cli %s --pdf" % output_path)
    if raw_model_text and not used_model_pages:
        print("[提示] 模型原文未解析成功，本次按确定性大纲排版；可加 --offline 明确跳过模型。")


if __name__ == "__main__":
    main()
