#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
长文摘要助手 —— 确定性抽取 + 模型生成 的中文文档摘要 agent

支持 .txt / .md / .html / .json / .csv（装了 pypdf 时还支持 .pdf）。
流程：先做完全确定性的文本清洗、结构解析、抽取式摘要（TF×位置×标题命中×句长惩罚
+ TextRank-lite）和中英文关键词统计，再按 --level 让模型生成一句话概括 / 要点列表 /
详细摘要，最后合并成一份中文 Markdown 摘要报告。除 anthropic SDK 外只用标准库。

用法：
    python3 agent.py <文件...> [选项]

示例：
    python3 agent.py 年度报告.md --level detailed --keywords 12 --output 摘要.md
    python3 agent.py 说明.html 数据.csv --level brief
    python3 agent.py 长文.txt --offline          # 不调模型，纯抽取式摘要

环境要求：
    1. 生成式摘要需要设置 API key（--offline 与 --self-test 不需要 key）：
           macOS / Linux：      export ANTHROPIC_API_KEY=你的key
           Windows PowerShell： $env:ANTHROPIC_API_KEY="你的key"
       也可以用 ANTHROPIC_AUTH_TOKEN 代替 ANTHROPIC_API_KEY。
    2. 安装依赖：pip3 install -r requirements.txt
    3. 走第三方 Anthropic 兼容网关时，用 --model 指定网关支持的模型名，
       例如 --model qwen3.8-max，并自行设置 ANTHROPIC_BASE_URL。
    4. PDF 需额外安装 pypdf（pip3 install pypdf）；没装也不影响其它格式。
"""

import argparse
import csv
import io
import json
import math
import os
import re
import sys
import tempfile
from collections import Counter, OrderedDict
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path

import anthropic

# 默认模型（可用 --model 参数或 DOC_SUMMARIZER_MODEL 环境变量更换）
DEFAULT_MODEL = os.environ.get("DOC_SUMMARIZER_MODEL", "claude-opus-5")

DEFAULT_OUTPUT = "summary.md"
DEFAULT_KEYWORDS = 10
CHUNK_LIMIT = 8000          # 超长文本分块时每块的字符上限
MAX_BULLET_LEN = 40         # 要点单条最大长度（模型输出超长会被截断）

# 详细程度 → 要点条数 / 抽取句数 / 详细摘要目标字数
LEVELS = {
    "brief": {"label": "简要", "points": 3, "sentences": 3, "summary_chars": 200},
    "standard": {"label": "标准", "points": 5, "sentences": 6, "summary_chars": 600},
    "detailed": {"label": "详细", "points": 8, "sentences": 10, "summary_chars": 1200},
}

EXT_KINDS = {
    ".txt": "text", ".text": "text", ".log": "text",
    ".md": "markdown", ".markdown": "markdown",
    ".html": "html", ".htm": "html",
    ".json": "json",
    ".csv": "csv", ".tsv": "csv",
    ".pdf": "pdf",
}

# ---------- 正则 ----------

HEADING_MARK_RE = re.compile(r"^【H(\d)】(.*)$")            # 清洗后保留的标题层级标记
MD_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
FENCE_RE = re.compile(r"^\s*(```|~~~)")
TABLE_SEP_RE = re.compile(r"^\s*\|?[\s:\-\|]+\|?\s*$")
HR_RE = re.compile(r"^\s*([-*_]\s*){3,}$")
LIST_RE = re.compile(r"^(\s*)([-*+]|\d+[.)])\s+")
QUOTE_RE = re.compile(r"^\s*>\s?")
LINK_RE = re.compile(r"!?\[([^\]]*)\]\([^)]*\)")
INLINE_CODE_RE = re.compile(r"`+([^`]*)`+")
EMPH_RE = re.compile(r"(\*\*|__|\*|_|~~)")
HTML_TAG_RE = re.compile(r"<[^>]+>")
SENT_END_RE = re.compile(r"(?<=[。！？；!?;])")
EN_SENT_RE = re.compile(r"(?<=[.。])\s+(?=[A-Z\u4e00-\u9fff])")
HAN_RUN_RE = re.compile(r"[\u4e00-\u9fff]+")
WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9_\-]*")
PLAIN_HEADING_RE = re.compile(
    r"^(第[一二三四五六七八九十百\d]+[章节部分篇]|[一二三四五六七八九十]+、|\d+(\.\d+)*[、.])\s*\S{1,30}$"
)
BREAK_LINE_RE = re.compile(r"[\r\t\u00a0]+")

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


# ---------- 文件读取 ----------

def decode_bytes(data):
    """按 utf-8 → utf-8-sig → gbk → latin-1 的顺序解码，返回 (文本, 编码名)"""
    for encoding in ("utf-8", "utf-8-sig", "gbk", "latin-1"):
        try:
            return data.decode(encoding), encoding
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode("utf-8", errors="replace"), "utf-8(容错)"


def read_pdf_text(path):
    """读取 PDF 文本；没装 pypdf 时抛出带中文提示的 RuntimeError"""
    try:
        import pypdf  # 可选依赖，不写进 requirements.txt
    except ImportError:
        raise RuntimeError(
            "读取 PDF 需要额外安装 pypdf：pip3 install pypdf；"
            "也可以先用其它工具把 PDF 转成 .txt / .md 再喂给本工具。"
        )
    reader = pypdf.PdfReader(str(path))
    parts = []
    for page in reader.pages:
        parts.append(page.extract_text() or "")
    return "\n\n".join(parts)


def read_document(path):
    """读入一个文档，返回 (原文, 格式)"""
    file_path = Path(path).expanduser()
    if not file_path.exists():
        raise RuntimeError("找不到文件：%s" % path)
    if not file_path.is_file():
        raise RuntimeError("不是普通文件：%s" % path)
    kind = EXT_KINDS.get(file_path.suffix.lower(), "text")
    if kind == "pdf":
        return read_pdf_text(file_path), kind
    data = file_path.read_bytes()
    if b"\x00" in data[:4096]:
        raise RuntimeError("二进制文件无法摘要：%s" % path)
    text, _ = decode_bytes(data)
    return text, kind


# ---------- 文本清洗 ----------

class _HTMLTextExtractor(HTMLParser):
    """把 HTML 压成纯文本：跳过 script/style，块级标签换行，实体自动转义"""

    SKIP_TAGS = {"script", "style", "noscript", "template", "svg"}
    BLOCK_TAGS = {
        "p", "div", "br", "section", "article", "header", "footer", "main",
        "h1", "h2", "h3", "h4", "h5", "h6", "li", "tr", "table", "ul", "ol",
        "blockquote", "pre", "hr", "form", "figure", "figcaption",
    }

    def __init__(self):
        HTMLParser.__init__(self, convert_charrefs=True)
        self.parts = []
        self._skip_depth = 0
        self._heading_level = 0

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP_TAGS:
            self._skip_depth += 1
            return
        if self._skip_depth:
            return
        if tag in self.BLOCK_TAGS:
            self.parts.append("\n")
        match = re.match(r"h([1-6])$", tag or "")
        if match:
            self._heading_level = int(match.group(1))
            self.parts.append("【H%d】" % self._heading_level)

    def handle_endtag(self, tag):
        if tag in self.SKIP_TAGS:
            if self._skip_depth:
                self._skip_depth -= 1
            return
        if self._skip_depth:
            return
        if tag in self.BLOCK_TAGS:
            self.parts.append("\n")

    def handle_startendtag(self, tag, attrs):
        if not self._skip_depth and tag in self.BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self._skip_depth:
            self.parts.append(data)


def clean_html(html_text):
    """HTML 去标签：用 HTMLParser 抽出正文，标题保留层级标记"""
    parser = _HTMLTextExtractor()
    try:
        parser.feed(html_text)
        parser.close()
    except Exception:
        # 极端不规范的 HTML 直接退化成正则去标签
        return HTML_TAG_RE.sub("\n", html_text)
    return "".join(parser.parts)


def clean_markdown(md_text):
    """Markdown 去符号：保留标题层级（写成【H1】这类标记），去掉强调/链接/表格线等"""
    lines = md_text.split("\n")
    if lines and lines[0].strip() == "---":          # 头部 YAML frontmatter 整块丢掉
        for index in range(1, min(len(lines), 40)):
            if lines[index].strip() == "---":
                lines = lines[index + 1:]
                break
    out = []
    in_fence = False
    for line in lines:
        raw = line.rstrip()
        if FENCE_RE.match(raw):
            in_fence = not in_fence          # 围栏本身丢掉，围栏内代码当普通文本保留
            continue
        if in_fence:
            out.append(raw)
            continue
        if not raw.strip():
            out.append("")
            continue
        if HR_RE.match(raw):
            out.append("")
            continue
        heading = MD_HEADING_RE.match(raw)
        if heading:
            level = len(heading.group(1))
            title = EMPH_RE.sub("", INLINE_CODE_RE.sub(r"\1", heading.group(2))).strip()
            out.append("")
            out.append("【H%d】%s" % (level, title))
            out.append("")
            continue
        if TABLE_SEP_RE.match(raw) and "|" in raw:
            continue
        text = raw
        text = QUOTE_RE.sub("", text)
        text = LIST_RE.sub(lambda m: "%s• " % m.group(1), text)
        text = LINK_RE.sub(r"\1", text)                       # [文字](链接) → 文字
        text = INLINE_CODE_RE.sub(r"\1", text)                # `代码` → 代码
        text = text.replace("|", " ")                          # 表格竖线
        text = EMPH_RE.sub("", text)                           # **加粗** / *斜体*
        out.append(text.strip() if text.strip() else "")
    return "\n".join(out)


def flatten_json(data, prefix=""):
    """把 JSON 递归拍平成 '键：值' 文本行"""
    lines = []
    if isinstance(data, dict):
        for key in sorted(data.keys(), key=str):
            name = "%s.%s" % (prefix, key) if prefix else str(key)
            lines.extend(flatten_json(data[key], name))
    elif isinstance(data, list):
        for index, value in enumerate(data):
            lines.extend(flatten_json(value, "%s[%d]" % (prefix, index)))
    else:
        lines.append("%s：%s" % (prefix, data))
    return lines


def flatten_csv(text):
    """把 CSV/TSV 拍平成 '表头: 值；表头: 值' 文本行"""
    sample = text[:2048]
    delimiter = "\t" if "\t" in sample and sample.count("\t") > sample.count(",") else ","
    reader = csv.reader(io.StringIO(text), delimiter=delimiter)
    rows = [row for row in reader if any(cell.strip() for cell in row)]
    if not rows:
        return ""
    header = [cell.strip() for cell in rows[0]]
    body = rows[1:] if len(rows) > 1 else []
    lines = []
    if not body:                      # 只有一行时按普通文本处理
        return "，".join(header)
    for row in body:
        pairs = []
        for index, cell in enumerate(row):
            name = header[index] if index < len(header) else "列%d" % (index + 1)
            pairs.append("%s：%s" % (name, cell.strip()))
        lines.append("；".join(pairs))
    return "\n".join(lines)


def normalize_whitespace(text):
    """压缩空行与行内空白"""
    text = BREAK_LINE_RE.sub(" ", text)
    text = re.sub(r"[ \u3000]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def mark_plain_headings(text):
    """纯文本里形如 '一、xxx' / '1. xxx' / '第x章 xxx' 的短行视作标题"""
    out = []
    for line in text.split("\n"):
        stripped = line.strip()
        if stripped and len(stripped) <= 32 and PLAIN_HEADING_RE.match(stripped):
            out.append("")
            out.append("【H1】%s" % re.sub(r"^(第[一二三四五六七八九十百\d]+[章节部分篇]|[一二三四五六七八九十]+、|\d+(\.\d+)*[、.])\s*", "", stripped))
            out.append("")
        else:
            out.append(line)
    return "\n".join(out)


def clean_document(text, kind):
    """按格式清洗出统一的正文：标题写成【Hn】标记，其余去符号"""
    if kind == "html":
        text = clean_html(text)
        text = normalize_whitespace(text)
        return mark_plain_headings(text)
    if kind == "markdown":
        text = clean_markdown(text)
        return normalize_whitespace(text)
    if kind == "json":
        try:
            data = json.loads(text)
        except ValueError:
            data = None
        if data is not None:
            text = "\n".join(flatten_json(data))
        return normalize_whitespace(mark_plain_headings(text))
    if kind == "csv":
        text = flatten_csv(text)
        return normalize_whitespace(text)
    text = HTML_TAG_RE.sub(" ", text)          # 纯文本里混入的小标签顺手清掉
    return normalize_whitespace(mark_plain_headings(text))


# ---------- 结构解析 ----------

def split_paragraphs(text):
    """按空行切段落"""
    return [block.strip() for block in re.split(r"\n\s*\n", text) if block.strip()]


def split_sentences(text):
    """切句子；返回的每一条都是原文的连续子串（便于校验来源）"""
    sentences = []
    for line in text.split("\n"):
        line = line.strip()
        if not line:
            continue
        for piece in SENT_END_RE.split(line):
            for sub in EN_SENT_RE.split(piece):
                sub = sub.strip()
                if sub:
                    sentences.append(sub)
    return sentences


def parse_sections(text):
    """解析标题层级树：返回 [{title, level, chars, paragraphs, sentences}]"""
    sections = []
    current = {"title": "（全文）", "level": 1, "chars": 0, "paragraphs": 0, "sentences": 0}
    started = False
    for block in split_paragraphs(text):
        heading = HEADING_MARK_RE.match(block.strip())
        if heading and len(block.strip()) <= 60:
            if started:
                sections.append(current)
            current = {
                "title": heading.group(2).strip() or "（无标题）",
                "level": int(heading.group(1)),
                "chars": 0,
                "paragraphs": 0,
                "sentences": 0,
            }
            started = True
            continue
        current["chars"] += len(block)
        current["paragraphs"] += 1
        current["sentences"] += len(split_sentences(block))
    if started or current["chars"] or current["paragraphs"]:
        sections.append(current)
    return sections


def render_structure_tree(sections):
    """渲染章节结构树（含字数分布）"""
    total = sum(sec["chars"] for sec in sections) or 1
    lines = []
    for sec in sections:
        indent = "    " * max(0, sec["level"] - 1)
        ratio = sec["chars"] * 100.0 / total
        lines.append(
            "%s├─ %s（%d 字，占 %.1f%%，%d 段 / %d 句）"
            % (indent, sec["title"], sec["chars"], ratio, sec["paragraphs"], sec["sentences"])
        )
    lines.append("")
    lines.append("合计：%d 个章节，%d 字" % (len(sections), sum(sec["chars"] for sec in sections)))
    return "\n".join(lines)


# ---------- 分词与关键词 ----------

def sent_tokens(sentence):
    """句子的特征词：英文单词 + 中文二字词（用于打分和相似度）"""
    tokens = set()
    for word in WORD_RE.findall(sentence):
        lowered = word.lower()
        if len(lowered) >= 2 and lowered not in EN_STOPWORDS:
            tokens.add(lowered)
    for run in HAN_RUN_RE.findall(sentence):
        for index in range(len(run) - 1):
            gram = run[index:index + 2]
            if gram not in ZH_STOPWORDS:
                tokens.add(gram)
    return tokens


def term_frequency(text):
    """全文词频（英文单词 + 中文 2-gram）"""
    counter = Counter()
    for sentence in split_sentences(text):
        counter.update(sent_tokens(sentence))
    return counter


def keyword_candidates(text):
    """中英文混合候选词：英文单词 + 中文 2/3/4-gram"""
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
    return counter


def extract_keywords(text, top_n=10):
    """取 top 关键词，去掉互相包含的冗余候选，返回 [(词, 次数)]"""
    counter = keyword_candidates(text)
    ordered = sorted(counter.items(), key=lambda kv: (-kv[1], -len(kv[0]), kv[0]))
    chosen = []
    for word, count in ordered:
        if any(word in kept for kept in chosen):
            continue                                   # 被已选长词覆盖
        if any(kept in word and counter[kept] == count for kept in chosen):
            continue                                   # 只是已选词的扩展且频次相同
        chosen.append(word)
        if len(chosen) >= top_n:
            break
    return [(word, counter[word]) for word in chosen]


# ---------- 抽取式摘要 ----------

def analyze_document(text):
    """统计正文：句子、段落、章节归属（供打分使用）"""
    records = []
    sections = []
    section_words = set()
    section_title = "（全文）"
    para_index = -1
    for block in split_paragraphs(text):
        para_index += 1
        heading = HEADING_MARK_RE.match(block.strip())
        if heading and len(block.strip()) <= 60:
            section_title = heading.group(2).strip() or "（无标题）"
            section_words = sent_tokens(section_title)
            sections.append(section_title)
            continue
        for offset, sentence in enumerate(split_sentences(block)):
            records.append({
                "text": sentence,
                "para": para_index,
                "section": section_title,
                "heading_words": set(section_words),
                "first_in_para": offset == 0,
            })
    return {"sentences": records, "sections": sections}


def score_sentences(analysis, tf):
    """TF × 位置权重 × 标题词命中 × 句长惩罚"""
    max_tf = max(tf.values()) if tf else 1
    scores = []
    for index, item in enumerate(analysis["sentences"]):
        tokens = sent_tokens(item["text"])
        if not tokens:
            scores.append(0.0)
            continue
        base = sum(tf.get(token, 0) for token in tokens) / float(len(tokens) * max_tf)
        position = 1.0 / (1.0 + 0.08 * index)             # 越靠前越重要
        if item["first_in_para"]:
            position *= 1.2
        hits = len(tokens & item["heading_words"])
        heading_bonus = 1.0 + 0.25 * min(hits, 3)
        length = len(item["text"])
        if length < 8:
            penalty = 0.4
        elif length < 15:
            penalty = 0.8
        elif length > 220:
            penalty = 0.85
        else:
            penalty = 1.0
        scores.append(base * position * heading_bonus * penalty)
    return scores


def textrank_scores(sentences, damping=0.85, iterations=40):
    """TextRank-lite：句子相似度矩阵 + 幂迭代"""
    total = len(sentences)
    if total == 0:
        return []
    token_sets = [sent_tokens(sentence) for sentence in sentences]
    sim = [[0.0] * total for _ in range(total)]
    for i in range(total):
        for j in range(i + 1, total):
            left, right = token_sets[i], token_sets[j]
            if not left or not right:
                continue
            value = len(left & right) / math.sqrt(len(left) * len(right))
            sim[i][j] = sim[j][i] = value
    out_sum = [sum(sim[i]) or 1.0 for i in range(total)]
    ranks = [1.0] * total
    for _ in range(iterations):
        updated = [
            (1.0 - damping)
            + damping * sum(sim[i][j] / out_sum[j] * ranks[j] for j in range(total) if j != i)
            for i in range(total)
        ]
        delta = max(abs(updated[i] - ranks[i]) for i in range(total))
        ranks = updated
        if delta < 1e-6:
            break
    return ranks


def _normalize(values):
    top = max(values) if values else 0.0
    if top <= 0:
        return [0.0 for _ in values]
    return [value / top for value in values]


def extractive_summary(text, max_sentences=6):
    """抽取式摘要：TF/位置打分 + TextRank 合并取 top 句，按原文顺序返回"""
    analysis = analyze_document(text)
    records = analysis["sentences"]
    if not records:
        return []
    sentences = [item["text"] for item in records]
    if len(sentences) <= max_sentences:
        return sentences
    tf_scores = _normalize(score_sentences(analysis, term_frequency(text)))
    tr_scores = _normalize(textrank_scores(sentences))
    combined = [
        (0.55 * tf_scores[i] + 0.45 * tr_scores[i], i) for i in range(len(sentences))
    ]
    top = sorted(combined, key=lambda item: (-item[0], item[1]))[:max_sentences]
    picked = sorted(index for _, index in top)
    return [sentences[index] for index in picked]


# ---------- 超长文本分块（map-reduce） ----------

def _split_long_paragraph(paragraph, limit):
    pieces = []
    current = ""
    for sentence in split_sentences(paragraph):
        if current and len(current) + len(sentence) + 1 > limit:
            pieces.append(current)
            current = ""
        current = (current + " " + sentence) if current else sentence
        if len(current) >= limit:
            pieces.append(current)
            current = ""
    if current:
        pieces.append(current)
    return pieces or [paragraph[:limit]]


def split_chunks(text, limit=CHUNK_LIMIT):
    """把正文切成每块不超过 limit 字符的块（尽量按段落边界）"""
    chunks = []
    current = ""
    for paragraph in split_paragraphs(text):
        pieces = _split_long_paragraph(paragraph, limit) if len(paragraph) > limit else [paragraph]
        for piece in pieces:
            if current and len(current) + len(piece) + 2 > limit:
                chunks.append(current.strip())
                current = ""
            current = (current + "\n\n" + piece) if current else piece
    if current.strip():
        chunks.append(current.strip())
    return chunks


def map_reduce_summary(text, max_sentences=6, limit=CHUNK_LIMIT):
    """分块摘要再合并：每块先抽句，合并后再抽一次，句数不超过 max_sentences"""
    chunks = split_chunks(text, limit)
    if len(chunks) <= 1:
        return extractive_summary(text, max_sentences)
    per_chunk = max(2, int(math.ceil(max_sentences / float(len(chunks)))))
    mapped = []
    for chunk in chunks:
        mapped.extend(extractive_summary(chunk, per_chunk))
    if not mapped:
        return []
    return extractive_summary("\n".join(mapped), max_sentences)


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


SUMMARY_SYSTEM_PROMPT = """你是一名资深中文编辑，擅长把长文压缩成准确、克制的摘要。
用户会给出清洗后的正文和已有的抽取式句子，请据此撰写摘要。

只输出一个 JSON 对象，不要任何解释文字，不要使用 Markdown 代码围栏，字段为：
- headline：一句话概括，40 字以内
- points：要点数组，每条 20~40 字，条数按用户要求
- summary：连贯的详细摘要段落

要求：不要编造原文没有的事实，数字必须与原文一致；全部用中文。"""


def build_model_prompt(text, level, extractive):
    """拼装用户提示词：正文 + 抽取式参考句"""
    config = LEVELS[level]
    excerpt = text[:12000]
    material = "\n".join("%d. %s" % (index + 1, line) for index, line in enumerate(extractive))
    return (
        "详细程度：%s\n"
        "要点条数：请输出 %d 条\n"
        "详细摘要目标字数：%d 字左右\n\n"
        "===== 抽取式参考句（确定性算法给出，可参考但不必照抄）=====\n%s\n\n"
        "===== 清洗后的正文 =====\n%s\n"
        % (config["label"], config["points"], config["summary_chars"], material, excerpt)
    )


def clip(text, limit):
    """截断超长文本并补省略号"""
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[:limit - 1] + "…"


def parse_summary_json(text):
    """尽力把模型回复解析成 {headline, points, summary}；解析不出来返回 None"""
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
    headline = str(data.get("headline") or "").strip()
    summary = str(data.get("summary") or "").strip()
    raw_points = data.get("points")
    points = []
    if isinstance(raw_points, list):
        for item in raw_points:
            value = str(item or "").strip()
            if value:
                points.append(clip(value, MAX_BULLET_LEN))
    if not headline and not points and not summary:
        return None
    return {"headline": headline, "points": points, "summary": summary}


def generate_summary(client, model, text, level, extractive):
    """调模型生成摘要，返回 (解析结果或 None, 模型原文)"""
    prompt = build_model_prompt(text, level, extractive)
    response = call_model(
        client, model, SUMMARY_SYSTEM_PROMPT,
        [{"role": "user", "content": prompt}],
        max_tokens=4000,
    )
    raw = response_text(response)
    return parse_summary_json(raw), raw


# ---------- 报告 ----------

def build_report(infos, sections, analysis, keywords, extractive, generated, level,
                 offline, model_name, raw_model_text, chunked, total_chars,
                 generated_at=None):
    """生成中文 Markdown 摘要报告（generated 为模型解析结果，可为 None）"""
    config = LEVELS[level]
    if not generated_at:
        generated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    lines = []
    lines.append("# 文档摘要报告")
    lines.append("")
    lines.append(
        "> 生成时间：%s ｜ 详细程度：%s ｜ 模式：%s"
        % (generated_at, config["label"], "抽取式（--offline）" if offline else "生成式（模型 %s）" % model_name)
    )
    if chunked:
        lines.append(">")
        lines.append("> 正文超过 %d 字符，已按 map-reduce 分块摘要后合并。" % CHUNK_LIMIT)
    lines.append("")

    lines.append("## 文档信息")
    lines.append("")
    lines.append("| 文件 | 格式 | 字符数 |")
    lines.append("| --- | --- | --- |")
    for info in infos:
        lines.append("| %s | %s | %d |" % (info["name"], info["kind"], info["chars"]))
    lines.append("| **合计** | — | **%d** |" % total_chars)
    lines.append("")
    sentences = analysis["sentences"]
    lines.append(
        "正文共 %d 个段落、%d 个句子、%d 个章节。"
        % (sum(sec["paragraphs"] for sec in sections), len(sentences), len(sections))
    )
    lines.append("")

    headline = generated["headline"] if generated else ""
    if not headline and sentences:
        headline = clip(sentences[0]["text"], 60)
    lines.append("## 一句话概括")
    lines.append("")
    lines.append(headline or "（原文太短，无法概括）")
    lines.append("")

    points = list(generated["points"]) if generated and generated["points"] else [
        clip(sentence, MAX_BULLET_LEN) for sentence in extractive[:config["points"]]
    ]
    lines.append("## 要点")
    lines.append("")
    if points:
        for point in points:
            lines.append("- %s" % point)
    else:
        lines.append("（没有可用要点）")
    lines.append("")

    summary = generated["summary"] if generated else ""
    if not summary:
        summary = "\n\n".join(extractive) or "（原文太短，正文即摘要）"
    lines.append("## 详细摘要")
    lines.append("")
    for paragraph in summary.split("\n"):
        if paragraph.strip():
            lines.append(paragraph.strip())
            lines.append("")

    lines.append("## 关键词")
    lines.append("")
    if keywords:
        lines.append("| 关键词 | 出现次数 |")
        lines.append("| --- | --- |")
        for word, count in keywords:
            lines.append("| %s | %d |" % (word, count))
    else:
        lines.append("（未提取到关键词）")
    lines.append("")

    lines.append("## 章节结构")
    lines.append("")
    lines.append("```text")
    lines.append(render_structure_tree(sections))
    lines.append("```")
    lines.append("")

    lines.append("## 抽取式原句")
    lines.append("")
    if extractive:
        for index, sentence in enumerate(extractive, 1):
            lines.append("%d. %s" % (index, sentence))
    else:
        lines.append("（原文没有可抽取的句子）")
    lines.append("")

    if raw_model_text and not generated:
        lines.append("## 附录：模型原始输出（未能解析为 JSON）")
        lines.append("")
        lines.append("```text")
        lines.append(raw_model_text)
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


def run_self_test():
    """离线自检：造样本、跑全部确定性逻辑、assert 校验，不联网、不需要 API key"""
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)

        # --- HTML 清洗：去标签、跳过 script、保留标题层级 ---
        html = (
            "<html><head><script>var x = '<b>坏内容</b>';</script></head>"
            "<body><h1>年度总结</h1><p>人工智能正在改变<b>软件</b>研发流程。</p>"
            "<p>机器学习让测试更加高效，人工智能让评审更加智能。</p>"
            "<style>p{color:red}</style></body></html>"
        )
        cleaned_html = clean_document(html, "html")
        assert "年度总结" in cleaned_html
        assert "【H1】年度总结" in cleaned_html
        assert "人工智能正在改变软件研发流程" in cleaned_html
        assert "坏内容" not in cleaned_html, "script 内容应被跳过"
        assert "<" not in cleaned_html and ">" not in cleaned_html
        assert "color" not in cleaned_html, "style 内容应被跳过"

        # --- Markdown 清洗：去符号、保留标题层级 ---
        md = (
            "# 项目说明\n\n"
            "这是**加粗**的`代码`与[链接文字](https://example.com)。\n\n"
            "## 设计思路\n\n"
            "- 第一条要点\n"
            "- 第二条要点\n\n"
            "| 列A | 列B |\n| --- | --- |\n| 1 | 2 |\n\n"
            "```python\nprint(1)\n```\n"
        )
        cleaned_md = clean_document(md, "markdown")
        assert "【H1】项目说明" in cleaned_md
        assert "【H2】设计思路" in cleaned_md
        assert "**" not in cleaned_md and "](" not in cleaned_md and "`" not in cleaned_md
        assert "加粗" in cleaned_md and "代码" in cleaned_md and "链接文字" in cleaned_md
        assert "• 第一条要点" in cleaned_md
        assert "print(1)" in cleaned_md, "围栏内的代码文本应保留"

        # --- JSON / CSV 拍平 ---
        cleaned_json = clean_document('{"title": "标题", "items": [1, 2], "meta": {"a": "甲"}}', "json")
        assert "title：标题" in cleaned_json
        assert "items[0]：1" in cleaned_json
        assert "meta.a：甲" in cleaned_json
        cleaned_csv = clean_document("名称,数量\n苹果,3\n梨,5", "csv")
        assert "名称：苹果；数量：3" in cleaned_csv
        assert "名称：梨；数量：5" in cleaned_csv

        # --- 结构解析：章节层级树与字数分布 ---
        sections = parse_sections(cleaned_md)
        titles = [sec["title"] for sec in sections]
        assert titles.count("项目说明") == 1
        assert "设计思路" in titles
        levels = dict((sec["title"], sec["level"]) for sec in sections)
        assert levels["项目说明"] == 1 and levels["设计思路"] == 2
        assert all(sec["chars"] >= 0 for sec in sections)
        tree = render_structure_tree(sections)
        assert "项目说明" in tree and "设计思路" in tree and "合计" in tree
        assert "字，占" in tree

        # --- 句子切分与抽取式摘要：句子必须来自原文 ---
        body = (
            "人工智能正在改变软件研发流程。机器学习模型可以自动定位缺陷。\n\n"
            "代码评审是保障质量的重要环节。评审可以发现潜在的逻辑错误。\n\n"
            "自动化测试让发布更加快速。测试覆盖率是衡量质量的关键指标。\n\n"
            "文档写作帮助新人快速上手。好的文档能显著降低沟通成本。\n\n"
            "持续集成把构建和测试串成流水线。流水线失败时要第一时间修复。"
        )
        cleaned_body = clean_document(body, "text")
        sentences = split_sentences(cleaned_body)
        assert len(sentences) == 10, "句子切分数量不对：%d" % len(sentences)
        extractive = extractive_summary(cleaned_body, 4)
        assert len(extractive) == 4
        for sentence in extractive:
            assert sentence in cleaned_body, "抽取句不是原文子串：%s" % sentence
        extractive_all = extractive_summary(cleaned_body, 99)
        assert len(extractive_all) == 10

        # --- TextRank 与打分函数可用 ---
        ranks = textrank_scores(sentences)
        assert len(ranks) == len(sentences) and all(rank > 0 for rank in ranks)
        tf = term_frequency(cleaned_body)
        assert tf["测试"] >= 2

        # --- 关键词：中英文混合统计 ---
        kw_text = (
            "人工智能与机器学习正在改变软件研发。人工智能让机器学习更高效。"
            "机器学习需要数据，人工智能需要算法。Kubernetes 部署 Kubernetes 集群。"
        ) * 3
        keywords = extract_keywords(kw_text, 6)
        words = [word for word, _ in keywords]
        assert "人工智能" in words, "关键词没命中：%s" % words
        assert "机器学习" in words
        assert "kubernetes" in words
        assert all(count >= 1 for _, count in keywords)

        # --- 分块 map-reduce ---
        long_parts = []
        for index in range(150):
            long_parts.append(
                "第 %d 章介绍了分布式系统的可靠性设计，可靠性是系统设计的核心目标之一，"
                "需要从冗余、容灾、监控、告警、演练等多个方面持续投入，才能在故障发生时把影响降到最低。" % (index + 1)
            )
        long_text = "\n\n".join(long_parts)
        chunks = split_chunks(long_text, CHUNK_LIMIT)
        assert len(chunks) >= 2, "超长文本应被切成多块"
        assert all(len(chunk) <= CHUNK_LIMIT for chunk in chunks)
        merged = map_reduce_summary(long_text, 5, CHUNK_LIMIT)
        assert len(merged) <= 5
        for sentence in merged:
            assert sentence in long_text, "合并摘要里的句子应来自原文"

        # --- 模型回复 JSON 解析兜底 ---
        good = (
            "```json\n"
            '{"headline":"一句话概括","points":["要点一","要点二"],"summary":"详细摘要正文"}\n'
            "```"
        )
        parsed = parse_summary_json(good)
        assert parsed["headline"] == "一句话概括" and len(parsed["points"]) == 2
        assert parsed["summary"] == "详细摘要正文"
        assert parse_summary_json("这不是 JSON") is None
        assert parse_summary_json("{}") is None
        assert parse_summary_json("") is None
        weird = '{"headline":"h","points":["%s"],"summary":"s"}' % ("长" * 80)
        coerced = parse_summary_json(weird)
        assert coerced and len(coerced["points"][0]) <= MAX_BULLET_LEN

        # --- 报告结构（含未解析的模型原文进附录） ---
        analysis = analyze_document(cleaned_body)
        body_sections = parse_sections(cleaned_body)
        report = build_report(
            [{"name": "样本.txt", "kind": "text", "chars": len(cleaned_body)}],
            body_sections,
            analysis,
            extract_keywords(cleaned_body, 5),
            extractive,
            parsed,
            "standard",
            True,
            DEFAULT_MODEL,
            "",
            False,
            len(cleaned_body),
        )
        for heading in (
            "# 文档摘要报告", "## 文档信息", "## 一句话概括", "## 要点",
            "## 详细摘要", "## 关键词", "## 章节结构", "## 抽取式原句",
        ):
            assert heading in report, "报告缺少：%s" % heading
        assert "一句话概括" in report
        assert "样本.txt" in report

        raw_report = build_report(
            [{"name": "样本.txt", "kind": "text", "chars": len(cleaned_body)}],
            body_sections, analysis, [], extractive, None, "brief", False,
            DEFAULT_MODEL, "自由格式的模型回复", False, len(cleaned_body),
        )
        assert "附录：模型原始输出（未能解析为 JSON）" in raw_report
        assert "自由格式的模型回复" in raw_report

        # --- call_model 逐级降级（离线假客户端） ---
        fake = _FakeClient()
        result = call_model(fake, "fake-model", "system", [{"role": "user", "content": "hi"}])
        assert result["ok"] is True
        assert fake.state["calls"] == 2

        # --- 落盘 ---
        output_path = root / "summary.md"
        output_path.write_text(report, encoding="utf-8")
        assert output_path.read_text(encoding="utf-8").startswith("# 文档摘要报告")

    print("SELF-TEST OK")


# ---------- CLI ----------

def build_parser():
    parser = argparse.ArgumentParser(
        description="长文摘要助手：确定性抽取式摘要 + 模型生成式摘要，输出中文 Markdown 报告",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            "  python3 agent.py 年度报告.md --level detailed --output 摘要.md\n"
            "  python3 agent.py 说明.html 数据.csv --level brief\n"
            "  python3 agent.py 长文.txt --offline        # 纯抽取式，不调模型\n"
            "  python3 agent.py 报告.pdf --model qwen3.8-max   # 走第三方兼容网关\n"
        ),
    )
    parser.add_argument("files", nargs="*", help="要摘要的文件，可以给多个（.txt/.md/.html/.json/.csv，装了 pypdf 还支持 .pdf）")
    parser.add_argument(
        "--level",
        choices=["brief", "standard", "detailed"],
        default="standard",
        help="摘要详细程度：brief 简要 / standard 标准 / detailed 详细（默认 standard）",
    )
    parser.add_argument(
        "--keywords",
        type=int,
        default=DEFAULT_KEYWORDS,
        metavar="N",
        help="关键词最多提取多少个（默认 %d）" % DEFAULT_KEYWORDS,
    )
    parser.add_argument(
        "--output",
        default=DEFAULT_OUTPUT,
        help="摘要报告写入路径（默认 %s）" % DEFAULT_OUTPUT,
    )
    parser.add_argument(
        "--offline",
        action="store_true",
        help="纯确定性模式：只做清洗、抽取式摘要和关键词统计，不调用模型（不需要 API key）",
    )
    parser.add_argument(
        "--model",
        default=DEFAULT_MODEL,
        help="生成式摘要使用的模型（默认 %s）" % DEFAULT_MODEL,
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

    if not args.files:
        parser.error("请至少提供一个要摘要的文件")

    if args.keywords < 1:
        parser.error("--keywords 必须大于 0")

    infos = []
    texts = []
    for file_name in args.files:
        try:
            raw_text, kind = read_document(file_name)
        except RuntimeError as exc:
            print("错误：%s" % exc, file=sys.stderr)
            sys.exit(1)
        except OSError as exc:
            print("错误：读取 %s 失败：%s" % (file_name, exc), file=sys.stderr)
            sys.exit(1)
        cleaned = clean_document(raw_text, kind)
        if not cleaned.strip():
            print("提示：%s 清洗后没有可用文本，已跳过。" % file_name)
            continue
        infos.append({"name": Path(file_name).name, "kind": kind, "chars": len(cleaned)})
        texts.append(cleaned)

    if not texts:
        print("错误：没有任何可摘要的文本内容。", file=sys.stderr)
        sys.exit(1)

    corpus = "\n\n".join(texts)
    total_chars = sum(info["chars"] for info in infos)
    config = LEVELS[args.level]
    chunked = len(corpus) > CHUNK_LIMIT

    print("[读取] %d 个文件，共 %d 字符，详细程度 %s"
          % (len(infos), total_chars, config["label"]))

    extractive = (
        map_reduce_summary(corpus, config["sentences"])
        if chunked
        else extractive_summary(corpus, config["sentences"])
    )
    keywords = extract_keywords(corpus, args.keywords)
    analysis = analyze_document(corpus)
    sections = parse_sections(corpus)
    print("[抽取] 抽取式句子 %d 条，关键词 %d 个，章节 %d 个"
          % (len(extractive), len(keywords), len(sections)))

    generated = None
    raw_model_text = ""
    if args.offline:
        print("[模式] --offline：跳过模型，只用抽取式结果")
    else:
        if not os.environ.get("ANTHROPIC_API_KEY") and not os.environ.get("ANTHROPIC_AUTH_TOKEN"):
            print(
                "提示：没有检测到 ANTHROPIC_API_KEY 或 ANTHROPIC_AUTH_TOKEN，无法调用模型生成摘要。\n"
                "请先设置：\n"
                "  macOS / Linux：      export ANTHROPIC_API_KEY=你的key\n"
                "  Windows PowerShell： $env:ANTHROPIC_API_KEY=\"你的key\"\n"
                "也可以加 --offline 使用纯确定性抽取式摘要。",
                file=sys.stderr,
            )
            sys.exit(1)
        print("[模型] %s 生成中……" % args.model)
        try:
            generated, raw_model_text = generate_summary(
                anthropic.Anthropic(), args.model, corpus, args.level, extractive
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
        if generated:
            print("[模型] 生成成功")
        else:
            print("[模型] 返回内容不是合法 JSON，已回退到抽取式结果（原文进报告附录）")

    report = build_report(
        infos, sections, analysis, keywords, extractive, generated,
        args.level, args.offline, args.model, raw_model_text,
        chunked, total_chars,
    )

    output_path = Path(args.output).expanduser()
    try:
        if output_path.parent and not output_path.parent.exists():
            output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(report, encoding="utf-8")
    except OSError as exc:
        print("错误：报告写入失败：%s" % exc, file=sys.stderr)
        sys.exit(1)

    print("[完成] 摘要报告已写入：%s" % output_path)
    if raw_model_text and not generated:
        print("[提示] 模型原始输出已放入报告附录，可人工查看。")


if __name__ == "__main__":
    main()
