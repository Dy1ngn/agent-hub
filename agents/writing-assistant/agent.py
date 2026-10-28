#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
写作润色助手（writing-assistant）—— 文本体检 + 模式化改写 的写作辅助 agent

确定性部分先对原文做「体检」：中英文混合字数/词数、句数、平均句长、最长句、
段落数、重复词 top5（中文 2-gram + 英文单词）、连接词/口头禅计数、可读性启发式
评分；再按模式（polish/rewrite/expand/shorten/formal/casual）让模型改写，
要求返回 JSON（改写稿 + 修改点清单 + 三项评分）；最后用 difflib 生成
「原文 ↔ 修改稿」逐句对照表，输出中文 Markdown 写作报告。
除 anthropic SDK 外只用标准库。

用法：
    python3 agent.py <文件> [--mode polish] [选项]
    python3 agent.py --text "要改写的文本" [选项]

示例：
    python3 agent.py 文章.md --mode polish --tone 温和 --audience 产品经理
    python3 agent.py 邮件.txt --mode formal --output 邮件润色报告.md
    python3 agent.py --text "其实我觉得这个方案可能还行" --mode shorten
    python3 agent.py 文章.md --mode rewrite --model qwen3.8-max

环境要求：
    1. 调用模型改写需要设置 API key（--offline 与 --self-test 不需要）：
           macOS / Linux：      export ANTHROPIC_API_KEY=你的key
           Windows PowerShell：$env:ANTHROPIC_API_KEY="你的key"
       走第三方 Anthropic 兼容网关时，改设 ANTHROPIC_BASE_URL 与
       ANTHROPIC_AUTH_TOKEN，并用 --model 指定网关支持的模型名。
    2. 安装 SDK：
           pip3 install -r requirements.txt
"""

import argparse
import difflib
import json
import os
import re
import sys
import tempfile
from datetime import datetime
from pathlib import Path

import anthropic

# 默认模型（可用 --model 参数或 WRITING_ASSISTANT_MODEL 环境变量更换）
DEFAULT_MODEL = os.environ.get("WRITING_ASSISTANT_MODEL", "claude-opus-5")

DEFAULT_OUTPUT = "writing-report.md"
DEFAULT_MAX_CHARS = 200_000  # 单次最多读入多少字符，超出截断并注明

MODES = ("polish", "rewrite", "expand", "shorten", "formal", "casual")

# 模式 → 改写要求模板
MODE_PROMPTS = {
    "polish": (
        "润色：在不改变原意、篇幅和段落结构的前提下修正语病、统一语气、"
        "删掉冗余与口头禅，让表达更干净顺滑。事实、数据、专有名词必须原样保留。"
    ),
    "rewrite": (
        "重写：可以调整结构、句式和用词，让逻辑更清晰、表达更有力，"
        "但必须保留原文的全部事实与信息点，不允许新增原文没有的结论。"
    ),
    "expand": (
        "扩写：补充细节、例子、背景与过渡句，让内容更丰满，"
        "篇幅约为原文的 1.5 到 2 倍；补充的内容必须合理，不得编造具体数据或事实。"
    ),
    "shorten": (
        "缩写：压缩到原文一半左右，删掉重复、次要和铺垫内容，"
        "保留关键结论、数据与行动项，句子要短。"
    ),
    "formal": (
        "改成正式书面语：用词规范、句式完整、少用人称与感叹，"
        "适合报告、公文、商务邮件；避免网络用语与口语化表达。"
    ),
    "casual": (
        "改成轻松口语：句子短、语气亲切自然，可以适度使用口语与语气词，"
        "适合社交媒体、内部沟通或产品文案；事实与数据仍要准确。"
    ),
}

# 连接词 / 口头禅词表
FILLER_WORDS = (
    "然后", "其实", "就是", "非常", "可能", "基本上", "大概", "感觉",
    "我觉得", "有点", "反正", "总之", "另外", "不过", "所以", "但是",
    "而且", "好像", "差不多", "应该说",
)

SCORE_LABELS = {"clarity": "清晰度", "concision": "简洁度", "tone": "语气契合"}

SENT_FIND_RE = re.compile(r"[^。！？；!?;\n]+[。！？；!?;]*")
PARA_SPLIT_RE = re.compile(r"\n\s*\n")
CJK_RE = re.compile(r"[\u4e00-\u9fff]")
CJK_RUN_RE = re.compile(r"[\u4e00-\u9fff]+")
EN_WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9'\-]*")

LONG_WORD_LEN = 13      # 英文单词超过多少字母算长难词
LONG_SENT_AVG = 20      # 平均句长超过多少字开始扣分


# ---------- 文本统计（全部确定性）----------

def split_sentences(text):
    """按中英文句末标点与换行切句，保留句末标点，返回去掉空白的句子列表"""
    return [seg.strip() for seg in SENT_FIND_RE.findall(text) if seg.strip()]


def split_paragraphs(text):
    """按空行分段，返回非空段落列表"""
    return [seg.strip() for seg in PARA_SPLIT_RE.split(text) if seg.strip()]


def count_cjk(text):
    """中文字数"""
    return len(CJK_RE.findall(text))


def count_en_words(text):
    """英文单词数（含数字词）"""
    return len(EN_WORD_RE.findall(text))


def repeated_terms(text, top=5):
    """重复词 top5：中文按 2-gram、英文按单词（小写），取出现 ≥2 次的"""
    counts = {}
    for run in CJK_RUN_RE.findall(text):
        for offset in range(len(run) - 1):
            gram = run[offset:offset + 2]
            counts[gram] = counts.get(gram, 0) + 1
    for word in EN_WORD_RE.findall(text):
        word = word.lower()
        if len(word) >= 2:
            counts[word] = counts.get(word, 0) + 1
    repeats = [(term, times) for term, times in counts.items() if times >= 2]
    repeats.sort(key=lambda item: (-item[1], item[0]))
    return repeats[:top]


def filler_counts(text):
    """连接词/口头禅计数，返回 {词: 次数}（含 0 次）"""
    return dict((word, text.count(word)) for word in FILLER_WORDS)


def readability_score(stats):
    """可读性启发式评分（0-100）：句长、口头禅、重复词、长难词都会扣分"""
    score = 100.0
    avg = stats["avg_sentence_len"]
    if avg > LONG_SENT_AVG:
        score -= min(30.0, (avg - LONG_SENT_AVG) * 1.5)
    score -= min(25.0, stats["filler_total"] * 2.5)
    score -= min(15.0, stats["repeat_total"] * 1.5)
    score -= min(15.0, stats["long_word_ratio"] * 30.0)
    return max(0.0, min(100.0, round(score, 1)))


def text_stats(text):
    """统计一段文本，返回可直接渲染进报告的字典"""
    sentences = split_sentences(text)
    paragraphs = split_paragraphs(text)
    cjk = count_cjk(text)
    en = count_en_words(text)
    sentence_chars = [len(sentence) for sentence in sentences]
    total_sentence_chars = sum(sentence_chars)
    avg_len = round(total_sentence_chars / len(sentences), 1) if sentences else 0.0
    longest = max(sentences, key=len) if sentences else ""
    fillers = filler_counts(text)
    filler_total = sum(fillers.values())
    repeats = repeated_terms(text)
    repeat_total = sum(times for _term, times in repeats)
    en_words = EN_WORD_RE.findall(text)
    long_words = [word for word in en_words if len(word) >= LONG_WORD_LEN]
    long_word_ratio = round(len(long_words) / len(en_words), 3) if en_words else 0.0

    stats = {
        "chars": len(text),
        "cjk_chars": cjk,
        "en_words": en,
        "words": cjk + en,
        "sentences": len(sentences),
        "avg_sentence_len": avg_len,
        "longest_sentence": longest,
        "longest_sentence_chars": len(longest),
        "paragraphs": len(paragraphs),
        "repeats": repeats,
        "repeat_total": repeat_total,
        "fillers": fillers,
        "filler_total": filler_total,
        "long_word_ratio": long_word_ratio,
    }
    stats["readability"] = readability_score(stats)
    return stats


# ---------- 对照渲染（全部确定性）----------

def _escape_cell(text):
    """转义 Markdown 表格里的竖线与换行"""
    return text.replace("|", "\\|").replace("\n", "<br>")


def render_diff(original, rewritten):
    """生成「原文 ↔ 修改稿」逐句对照表与 unified diff，返回 (表格文本, diff 文本)"""
    old = split_sentences(original)
    new = split_sentences(rewritten)
    matcher = difflib.SequenceMatcher(None, old, new)
    rows = []
    number = 0
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            for offset in range(i2 - i1):
                number += 1
                rows.append((number, old[i1 + offset], new[j1 + offset], "无变化"))
        elif tag == "replace":
            span = max(i2 - i1, j2 - j1)
            for offset in range(span):
                number += 1
                left = old[i1 + offset] if i1 + offset < i2 else "（无）"
                right = new[j1 + offset] if j1 + offset < j2 else "（无）"
                rows.append((number, left, right, "改写"))
        elif tag == "delete":
            for offset in range(i1, i2):
                number += 1
                rows.append((number, old[offset], "（删除）", "删除"))
        elif tag == "insert":
            for offset in range(j1, j2):
                number += 1
                rows.append((number, "（新增）", new[offset], "新增"))

    lines = [
        "| 序号 | 原文 | 修改稿 | 说明 |",
        "| --- | --- | --- | --- |",
    ]
    for number, left, right, kind in rows:
        lines.append("| %d | %s | %s | %s |" % (
            number, _escape_cell(left), _escape_cell(right), kind,
        ))
    if not rows:
        lines.append("| - | （空） | （空） | - |")

    diff_lines = list(difflib.unified_diff(
        old, new, fromfile="原文", tofile="修改稿", lineterm="",
    ))
    return "\n".join(lines), "\n".join(diff_lines)


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
    """把模型响应里的文本块拼成一段字符串"""
    parts = []
    for block in getattr(response, "content", []) or []:
        if getattr(block, "type", "") == "text":
            parts.append(getattr(block, "text", ""))
    return "\n".join(parts).strip()


REVIEW_SYSTEM_PROMPT = """你是一名资深中文写作编辑。用户会给出一段原文、改写模式、语气和目标读者。
请严格按改写要求处理原文，然后只输出一个 JSON 对象，不要任何解释、不要 Markdown 代码围栏：

{
  "rewritten": "改写后的完整文本",
  "changes": [
    {"type": "修改类型", "before": "原文片段", "after": "修改后片段", "reason": "为什么这么改"}
  ],
  "score": {"clarity": 85, "concision": 80, "tone": 90}
}

要求：
1. rewritten 必须是完整改写稿，不要截断、不要加标题或说明。
2. changes 只列最有代表性的修改点（最多 10 条），before/after 用原文里的短片段。
3. score 三项都是 0 到 100 的整数，评价的是改写后的文本。
"""


def build_user_prompt(text, mode, tone="", audience=""):
    """拼装改写请求：模式要求 + 语气 + 读者 + 原文"""
    parts = ["改写模式：%s" % MODE_PROMPTS[mode]]
    if tone:
        parts.append("语气要求：%s" % tone)
    if audience:
        parts.append("目标读者：%s" % audience)
    parts.append("")
    parts.append("原文如下：")
    parts.append("")
    parts.append(text)
    return "\n".join(parts)


def _strip_fence(text):
    content = text.strip()
    if content.startswith("```"):
        content = re.sub(r"^```[A-Za-z]*\s*", "", content)
        content = re.sub(r"\s*```$", "", content)
    return content


def parse_review_json(text):
    """把模型回复解析成 {"rewritten","changes","score"}；失败返回 None"""
    if not text:
        return None
    content = _strip_fence(text)
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
    rewritten = data.get("rewritten")
    if not isinstance(rewritten, str) or not rewritten.strip():
        return None

    changes = []
    raw_changes = data.get("changes")
    if isinstance(raw_changes, list):
        for item in raw_changes[:20]:
            if not isinstance(item, dict):
                continue
            changes.append({
                "type": str(item.get("type") or "调整").strip() or "调整",
                "before": str(item.get("before") or ""),
                "after": str(item.get("after") or ""),
                "reason": str(item.get("reason") or "").strip() or "（模型未说明原因）",
            })

    score = {}
    raw_score = data.get("score")
    if isinstance(raw_score, dict):
        for key in ("clarity", "concision", "tone"):
            try:
                value = float(raw_score.get(key))
            except (TypeError, ValueError):
                continue
            score[key] = max(0.0, min(100.0, value))

    return {"rewritten": rewritten, "changes": changes, "score": score}


def review_text(text, model, mode, tone="", audience="", client=None, verbose=True):
    """调模型改写一段文本，返回 (结果字典, 是否走了兜底)

    兜底策略：模型回复解析不出 JSON 时，rewritten 直接用原文，报告里标注。
    """
    call_client = client if client is not None else anthropic.Anthropic()
    if verbose:
        print("[改写] 模式 %s，模型 %s" % (mode, model))
    prompt = build_user_prompt(text, mode, tone, audience)
    response = call_model(
        call_client,
        model,
        REVIEW_SYSTEM_PROMPT,
        [{"role": "user", "content": prompt}],
        max_tokens=8000,
    )
    parsed = parse_review_json(response_text(response))
    if parsed is None:
        if verbose:
            print("[改写] 模型回复不是合法 JSON，改写稿保留原文（已在报告标注）。")
        return {
            "rewritten": text,
            "changes": [],
            "score": {},
            "raw": response_text(response),
        }, True
    parsed["raw"] = ""
    return parsed, False


# ---------- 报告 ----------

def _fmt_repeats(repeats):
    if not repeats:
        return "（无）"
    return "、".join("%s×%d" % (term, times) for term, times in repeats)


def _fmt_fillers(fillers):
    items = [(word, times) for word, times in fillers.items() if times > 0]
    if not items:
        return "（无）"
    return "、".join("%s×%d" % (word, times) for word, times in items)


def build_report(stats, original, rewritten, changes, score, mode, tone, audience,
                 model, offline, fallback=False, raw_text="", generated_at=None):
    """生成中文 Markdown 写作报告"""
    now = generated_at or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    table, diff_text = render_diff(original, rewritten)
    longest = stats["longest_sentence"]
    if len(longest) > 40:
        longest = longest[:40] + "……"

    lines = [
        "# 写作润色报告",
        "",
        "- 生成时间：%s" % now,
        "- 改写模式：%s%s" % (mode, "（%s）" % MODE_PROMPTS[mode].split("：", 1)[0] if mode in MODE_PROMPTS else ""),
        "- 语气：%s · 目标读者：%s" % (tone or "未指定", audience or "未指定"),
        "- 运行模式：%s" % ("离线（--offline，未调用模型，改写稿=原文）" if offline else "模型改写（%s）" % model),
        "",
        "## 评分卡",
        "",
        "| 指标 | 数值 |",
        "| --- | --- |",
        "| 字符数 | %d |" % stats["chars"],
        "| 词数 | 中文字 %d + 英文词 %d = %d |" % (stats["cjk_chars"], stats["en_words"], stats["words"]),
        "| 句数 | %d |" % stats["sentences"],
        "| 平均句长 | %s 字 |" % stats["avg_sentence_len"],
        "| 最长句 | %s 字（%s） |" % (stats["longest_sentence_chars"], _escape_cell(longest) or "（空）"),
        "| 段落数 | %d |" % stats["paragraphs"],
        "| 重复词 top5 | %s |" % _escape_cell(_fmt_repeats(stats["repeats"])),
        "| 连接词/口头禅 | 共 %d 次（%s） |" % (stats["filler_total"], _escape_cell(_fmt_fillers(stats["fillers"]))),
        "| 可读性启发式评分 | %s / 100 |" % stats["readability"],
        "| 模型评分 | %s |" % (
            "、".join("%s %s" % (SCORE_LABELS[key], score[key]) for key in ("clarity", "concision", "tone") if key in score)
            or "（无）"
        ),
        "",
    ]

    if fallback:
        lines.append("> 注意：模型回复未能解析成约定 JSON，本报告的改写稿直接使用原文，修改对照为空。")
        lines.append("")

    lines.extend([
        "## 修改对照",
        "",
        "逐句对照（中文按句切分，机器生成）：",
        "",
        table,
        "",
        "<details><summary>展开 unified diff</summary>",
        "",
        "```diff",
        diff_text or "（无差异）",
        "```",
        "",
        "</details>",
        "",
    ])

    lines.append("## 修改点清单")
    lines.append("")
    if changes:
        lines.append("| 类型 | 原文 | 修改后 | 原因 |")
        lines.append("| --- | --- | --- | --- |")
        for change in changes:
            lines.append("| %s | %s | %s | %s |" % (
                _escape_cell(change["type"]),
                _escape_cell(change["before"] or "（无）"),
                _escape_cell(change["after"] or "（无）"),
                _escape_cell(change["reason"]),
            ))
    else:
        lines.append("（模型未提供修改点清单）")
    lines.append("")

    lines.extend([
        "## 改写稿",
        "",
        rewritten or "（空）",
        "",
    ])

    if raw_text:
        lines.append("## 附录：模型原文（未能解析为 JSON）")
        lines.append("")
        lines.append("```text")
        lines.append(raw_text)
        lines.append("```")
        lines.append("")

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


def _make_response(text):
    """构造一个只有文本块的假模型响应"""

    class _Block:
        type = "text"

    _Block.text = text

    class _Response:
        content = [_Block()]

    return _Response()


class _StubClient:
    """极简客户端：beta / thinking / fallbacks 参数都抛 TypeError，最基础的调用返回给定文本"""

    def __init__(self, text):
        self.text = text
        self.state = {"calls": 0}
        self.beta = self
        self.messages = self

    def create(self, **kwargs):
        self.state["calls"] += 1
        if "betas" in kwargs or "thinking" in kwargs or "fallbacks" in kwargs:
            raise TypeError("网关不认识新参数")
        return _make_response(self.text)


GOOD_REVIEW_JSON = (
    '{"rewritten": "第一句没变。第二句改好了。",'
    ' "changes": [{"type": "删冗余", "before": "其实我觉得", "after": "", "reason": "多余的口头禅"}],'
    ' "score": {"clarity": 80, "concision": 70, "tone": 90}}'
)


def run_self_test():
    """离线自检：造样本、跑全部确定性逻辑、assert 校验，不联网、不需要 API key"""
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)

        # --- 统计函数数值必须准确 ---
        sample = "今天天气很好。今天天气很好。\n\n我们去公园散步。\n"
        stats = text_stats(sample)
        assert stats["sentences"] == 3
        assert stats["paragraphs"] == 2
        assert stats["cjk_chars"] == 6 * 2 + 7
        assert stats["en_words"] == 0
        assert stats["words"] == 19
        assert stats["chars"] == len(sample)
        assert stats["avg_sentence_len"] == 7.3   # (7 + 7 + 8) / 3，句子含句末标点
        assert stats["longest_sentence"] == "我们去公园散步。"
        assert stats["longest_sentence_chars"] == 8
        assert set(term for term, _times in stats["repeats"]) == {"今天", "天天", "天气", "气很", "很好"}
        assert stats["repeat_total"] == 10
        assert stats["filler_total"] == 0
        assert 0.0 <= stats["readability"] <= 100.0

        mixed = "他写了 Python 脚本，然后其实还有 Go 版本。\n"
        mixed_stats = text_stats(mixed)
        assert mixed_stats["cjk_chars"] > 0
        assert mixed_stats["en_words"] == 2  # Python、Go
        assert mixed_stats["fillers"]["然后"] == 1
        assert mixed_stats["fillers"]["其实"] == 1

        clean = "今天天气很好。我们去公园散步。"
        messy = "然后其实然后其实然后其实就是非常可能就是非常可能" * 2 + "的一段很长很长很长很长很长很长很长很长很长很长的话。"
        clean_score = text_stats(clean)["readability"]
        messy_stats = text_stats(messy)
        assert messy_stats["filler_total"] > 5
        assert messy_stats["readability"] < clean_score, "啰嗦文本的可读性评分必须更低"
        assert 0.0 <= messy_stats["readability"] <= 100.0

        empty_stats = text_stats("")
        assert empty_stats["sentences"] == 0 and empty_stats["avg_sentence_len"] == 0.0
        assert empty_stats["longest_sentence"] == ""
        assert empty_stats["repeats"] == []

        # --- 修改对照渲染：表格与 diff 都要有 ---
        original = "第一句没变。第二句要改。"
        rewritten = "第一句没变。第二句改好了。"
        table, diff_text = render_diff(original, rewritten)
        assert "| 原文 | 修改稿 |" in table
        assert "第一句没变。" in table
        assert "第二句要改。" in table and "第二句改好了。" in table
        assert "-第二句要改。" in diff_text and "+第二句改好了。" in diff_text

        # 删除的句子要在对照表里标出来
        table_del, diff_del = render_diff("只有一句。又有一句。", "只有一句。")
        assert "（删除）" in table_del and "又有一句。" in table_del
        assert "-又有一句。" in diff_del

        # 新增句子也要能在对照表里看到
        table2, diff2 = render_diff("只有一句。", "只有一句。又加了一句。")
        assert "（新增）" in table2 and "又加了一句。" in table2
        assert "+又加了一句。" in diff2

        # 空文本不崩
        table3, diff3 = render_diff("", "")
        assert "| 原文 | 修改稿 |" in table3
        assert diff3 == "" or isinstance(diff3, str)

        # --- 模式模板：六个模式都要有且各不相同 ---
        assert set(MODE_PROMPTS.keys()) == set(MODES)
        templates = list(MODE_PROMPTS.values())
        assert len(set(templates)) == len(templates), "模式模板不能重复"
        for keyword, mode in (
            ("润色", "polish"), ("重写", "rewrite"), ("扩写", "expand"),
            ("缩写", "shorten"), ("正式", "formal"), ("口语", "casual"),
        ):
            assert keyword in MODE_PROMPTS[mode], "模式 %s 的模板缺少关键词 %s" % (mode, keyword)

        prompt = build_user_prompt("原文内容", "formal", "稳重", "高管")
        assert "正式书面语" in prompt
        assert "语气要求：稳重" in prompt
        assert "目标读者：高管" in prompt
        assert "原文内容" in prompt
        prompt_no_tone = build_user_prompt("原文内容", "casual")
        assert "轻松口语" in prompt_no_tone

        # --- 模型回复 JSON 解析与兜底 ---
        parsed = parse_review_json(GOOD_REVIEW_JSON)
        assert parsed and parsed["rewritten"] == "第一句没变。第二句改好了。"
        assert parsed["changes"][0]["type"] == "删冗余"
        assert parsed["score"] == {"clarity": 80.0, "concision": 70.0, "tone": 90.0}
        assert parse_review_json("这不是 JSON") is None
        assert parse_review_json("{bad json") is None
        assert parse_review_json('{"rewritten": ""}') is None
        assert parse_review_json('{"changes": []}') is None
        weird = (
            '{"rewritten": "ok", "changes": [{"type": 1}, "junk", {"before": "a", "after": "b"}],'
            ' "score": {"clarity": "x", "concision": 250, "tone": -5}}'
        )
        weird_parsed = parse_review_json(weird)
        assert weird_parsed and len(weird_parsed["changes"]) == 2
        assert weird_parsed["score"]["concision"] == 100.0
        assert weird_parsed["score"]["tone"] == 0.0
        assert "clarity" not in weird_parsed["score"]
        assert parse_review_json("```json\n%s\n```" % GOOD_REVIEW_JSON)["rewritten"]

        # --- 用假客户端走完整改写流程 ---
        stub_good = _StubClient(GOOD_REVIEW_JSON)
        result, fallback = review_text(
            "原文", "fake-model", "polish", client=stub_good, verbose=False,
        )
        assert fallback is False
        assert result["rewritten"] == "第一句没变。第二句改好了。"
        assert result["changes"]

        stub_bad = _StubClient("我觉得这样写就挺好的，不用改。")
        result2, fallback2 = review_text(
            "原文保持原样。", "fake-model", "polish", client=stub_bad, verbose=False,
        )
        assert fallback2 is True
        assert result2["rewritten"] == "原文保持原样。", "解析失败时改写稿必须退回原文"
        assert result2["changes"] == []
        assert result2["raw"]

        # --- 报告结构完整 ---
        report = build_report(
            stats, sample, "今天天气很好。今天天气很好。我们去公园散步。",
            [{"type": "合句", "before": "今天天气很好。今天天气很好。", "after": "今天天气很好。", "reason": "重复句合并"}],
            {"clarity": 80, "concision": 70, "tone": 90},
            "polish", "温和", "产品经理", "claude-opus-5", False,
            generated_at="2026-01-01 00:00:00",
        )
        assert report.startswith("# 写作润色报告")
        for heading in ("## 评分卡", "## 修改对照", "## 修改点清单", "## 改写稿"):
            assert heading in report, "报告缺少：%s" % heading
        assert "| 字符数 |" in report and "| 重复词 top5 |" in report
        assert "| 原文 | 修改稿 |" in report
        assert "合句" in report and "重复句合并" in report
        assert "今天天气很好。我们去公园散步。" in report
        assert "模型评分" in report and "清晰度 80" in report

        report_offline = build_report(
            stats, sample, sample, [], {}, "casual", "", "", "claude-opus-5", True,
            generated_at="2026-01-01 00:00:00",
        )
        assert "离线（--offline" in report_offline
        assert "（模型未提供修改点清单）" in report_offline

        report_fallback = build_report(
            stats, sample, sample, [], {}, "polish", "", "", "claude-opus-5", False,
            fallback=True, raw_text="自由格式的模型回复",
            generated_at="2026-01-01 00:00:00",
        )
        assert "改写稿直接使用原文" in report_fallback
        assert "附录：模型原文（未能解析为 JSON）" in report_fallback
        assert "自由格式的模型回复" in report_fallback

        # --- 报告落盘 ---
        output_path = root / "writing-report.md"
        output_path.write_text(report, encoding="utf-8")
        assert output_path.read_text(encoding="utf-8").startswith("# 写作润色报告")

        # --- 文件读取：截断与编码 ---
        file_path = root / "文章.md"
        file_path.write_text(sample, encoding="utf-8")
        text = file_path.read_text(encoding="utf-8")
        assert text == sample

        # --- call_model 逐级降级（离线假客户端）---
        fake = _FakeClient()
        call_result = call_model(fake, "fake-model", "system", [{"role": "user", "content": "hi"}])
        assert call_result["ok"] is True
        assert fake.state["calls"] == 2

    print("SELF-TEST OK")


# ---------- CLI ----------

def build_parser():
    parser = argparse.ArgumentParser(
        description="写作润色助手：先做文本体检，再按模式改写，输出评分卡、逐句对照与改写稿的中文报告",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            "  py -3 agent.py 文章.md --mode polish --tone 温和 --audience 产品经理\n"
            "  py -3 agent.py 邮件.txt --mode formal --output 邮件润色报告.md\n"
            "  py -3 agent.py --text \"其实我觉得这个方案可能还行\" --mode shorten\n"
            "  py -3 agent.py 文章.md --mode rewrite --model qwen3.8-max\n"
        ),
    )
    parser.add_argument("path", nargs="?", help="要润色的文本文件（与 --text 二选一）")
    parser.add_argument(
        "--text",
        default=None,
        metavar="文本",
        help="直接改写一段文本（不用给文件）",
    )
    parser.add_argument(
        "--mode",
        choices=list(MODES),
        default="polish",
        help="改写模式：polish 润色 / rewrite 重写 / expand 扩写 / shorten 缩写 / formal 正式 / casual 口语（默认 polish）",
    )
    parser.add_argument(
        "--tone",
        default="",
        metavar="语气",
        help="目标语气，例如 温和 / 坚定 / 幽默（可不填）",
    )
    parser.add_argument(
        "--audience",
        default="",
        metavar="读者",
        help="目标读者，例如 产品经理 / 高管 / 开发者（可不填）",
    )
    parser.add_argument(
        "--output",
        default=DEFAULT_OUTPUT,
        help="报告写入路径（默认 %s）" % DEFAULT_OUTPUT,
    )
    parser.add_argument(
        "--offline",
        action="store_true",
        help="纯确定性模式：只做文本体检与对照，不调用模型、不需要 API key",
    )
    parser.add_argument(
        "--model",
        default=DEFAULT_MODEL,
        help="改写使用的模型（默认 %s）" % DEFAULT_MODEL,
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

    if args.text is not None and args.path:
        parser.error("--text 与文件路径二选一，不要同时使用")
    if args.text is None and not args.path:
        parser.error("请提供一个文本文件，或用 --text 直接给一段文本")

    truncated = False
    if args.text is not None:
        text = args.text
        source_label = "--text 输入"
    else:
        file_path = Path(args.path).expanduser()
        if not file_path.exists() or not file_path.is_file():
            print("错误：文件不存在或不是普通文件：%s" % args.path, file=sys.stderr)
            sys.exit(1)
        try:
            raw = file_path.read_bytes()
        except OSError as exc:
            print("错误：文件读取失败：%s" % exc, file=sys.stderr)
            sys.exit(1)
        if b"\x00" in raw[:4096]:
            print("错误：这是二进制文件，只能处理 UTF-8 文本。", file=sys.stderr)
            sys.exit(1)
        try:
            text = raw.decode("utf-8", "strict")
        except UnicodeDecodeError:
            print("错误：文件不是 UTF-8 编码，请先转码。", file=sys.stderr)
            sys.exit(1)
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        if len(text) > DEFAULT_MAX_CHARS:
            text = text[:DEFAULT_MAX_CHARS]
            truncated = True
        source_label = file_path.name

    if not text.strip():
        print("错误：文本是空的，没有可润色的内容。", file=sys.stderr)
        sys.exit(1)

    print("[体检] 来源：%s" % source_label)
    stats = text_stats(text)
    print("[体检] 字符 %d，词 %d，句 %d，段 %d，可读性 %s/100"
          % (stats["chars"], stats["words"], stats["sentences"],
             stats["paragraphs"], stats["readability"]))
    if truncated:
        print("[体检] 文本超过 %d 字符，已截断处理。" % DEFAULT_MAX_CHARS)

    fallback = False
    raw_text = ""
    if args.offline:
        print("[改写] 已跳过（--offline），改写稿=原文，报告仍包含体检与对照。")
        rewritten = text
        changes = []
        score = {}
    else:
        if not os.environ.get("ANTHROPIC_API_KEY") and not os.environ.get("ANTHROPIC_AUTH_TOKEN"):
            print(
                "提示：没有检测到 ANTHROPIC_API_KEY 或 ANTHROPIC_AUTH_TOKEN，无法调用模型改写。\n"
                "请先设置（macOS / Linux）：export ANTHROPIC_API_KEY=你的key\n"
                "Windows PowerShell：$env:ANTHROPIC_API_KEY=\"你的key\"\n"
                "也可以加 --offline 只做文本体检与对照，不需要 key。",
                file=sys.stderr,
            )
            sys.exit(1)
        try:
            result, fallback = review_text(
                text, args.model, args.mode, args.tone, args.audience, verbose=True,
            )
        except anthropic.AuthenticationError:
            print("错误：API key 无效或没有权限，请检查 ANTHROPIC_API_KEY。", file=sys.stderr)
            sys.exit(1)
        except anthropic.APIConnectionError:
            print("错误：无法连接 API，请检查网络后重试。", file=sys.stderr)
            sys.exit(1)
        except anthropic.RateLimitError:
            print("错误：请求太频繁被限流，请稍后再试。", file=sys.stderr)
            sys.exit(1)
        except anthropic.APIStatusError as exc:
            print("错误：API 返回错误（%s）：%s" % (exc.status_code, exc.message), file=sys.stderr)
            sys.exit(1)
        rewritten = result["rewritten"]
        changes = result["changes"]
        score = result["score"]
        raw_text = result.get("raw", "")
        print("[改写] 修改点 %d 条，改写稿 %d 字符" % (len(changes), len(rewritten)))

    report = build_report(
        stats, text, rewritten, changes, score,
        args.mode, args.tone, args.audience, args.model, args.offline,
        fallback=fallback, raw_text=raw_text,
    )
    output_path = Path(args.output).expanduser()
    try:
        if output_path.parent and not output_path.parent.exists():
            output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(report, encoding="utf-8")
    except OSError as exc:
        print("错误：报告写入失败：%s" % exc, file=sys.stderr)
        sys.exit(1)

    print("[完成] 报告已写入：%s" % output_path)
    print("[完成] 评分卡、逐句对照、修改点清单与改写稿都在报告里。")
    if fallback:
        print("[提示] 模型回复未能解析成 JSON，改写稿保留原文，详见报告附录。")


if __name__ == "__main__":
    main()
