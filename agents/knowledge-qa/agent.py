#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
本地知识库问答（knowledge-qa）—— 倒排索引 + BM25 检索，带来源引用的中文问答

把一个本地目录（.md / .txt / .json / .csv）当成知识库：先做完全确定性的分段、
分词、倒排索引与 BM25 打分，再把命中的片段交给模型生成带引用的中文回答。
不使用向量库，除 anthropic SDK 外只用标准库；索引缓存在知识库目录的 .kb_index.json。

用法：
    python3 agent.py --kb 知识库目录 --ask "问题" [--top-k 5] [--rebuild] [--offline]

示例：
    python3 agent.py --kb ./docs --ask "如何安装依赖"
    python3 agent.py --kb ./docs --ask "配置项有哪些" --top-k 8 --output 答案.md
    python3 agent.py --kb ./docs                    # 不带 --ask 进入交互问答（输入 exit 退出）
    python3 agent.py --kb ./docs --ask "如何部署" --offline   # 只检索不调模型，打印命中原文片段

环境要求：
    1. 调用模型回答时需要设置 API key（--offline 与 --self-test 不需要）：
           macOS / Linux：        export ANTHROPIC_API_KEY=你的key
           Windows PowerShell：   $env:ANTHROPIC_API_KEY="你的key"
    2. 安装依赖：
           pip3 install -r requirements.txt
    3. 走第三方 Anthropic 兼容网关时，用 --model 指定网关认得的模型名，例如：
           python3 agent.py --kb ./docs --ask "..." --model qwen3.8-max
"""

import argparse
import json
import math
import os
import re
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

import anthropic

# 默认模型（可用 --model 参数或 KNOWLEDGE_QA_MODEL 环境变量更换）
DEFAULT_MODEL = os.environ.get("KNOWLEDGE_QA_MODEL", "claude-opus-5")

CACHE_NAME = ".kb_index.json"      # 索引缓存文件名（写在知识库目录里）
CACHE_VERSION = 2                  # 缓存结构版本，变了就整体重建
KB_EXTS = (".md", ".markdown", ".txt", ".json", ".csv")
SKIP_DIRS = {".git", ".svn", ".hg", "__pycache__", "node_modules", ".venv", "venv"}
SKIP_FILES = {CACHE_NAME, ".DS_Store"}

MAX_SEGMENT_CHARS = 800     # 单个片段最大字符数（超长段落按句再切）
BM25_K1 = 1.5
BM25_B = 0.75
DEFAULT_TOP_K = 5
DEFAULT_MIN_SCORE = 0.5     # 最高分低于该阈值视为"知识库里没有相关内容"
MAX_FILE_CHARS = 2_000_000  # 单文件最多读入字符数，超出截断
SNIPPET_CHARS = 300         # 离线模式下每个片段打印的最大字符数

# 同义词小词表（查询扩展用，可选）
SYNONYMS = {
    "安装": ["install", "pip", "setup", "依赖", "requirements"],
    "依赖": ["requirements", "package", "install", "包"],
    "配置": ["config", "configuration", "setting", "环境变量", "env"],
    "部署": ["deploy", "deployment", "上线", "发布", "release"],
    "错误": ["error", "异常", "exception", "失败", "报错"],
    "性能": ["performance", "优化", "latency", "吞吐", "慢"],
    "测试": ["test", "unittest", "pytest", "单测", "用例"],
    "数据库": ["database", "sql", "mysql", "sqlite", "表"],
    "接口": ["api", "endpoint", "路由", "http"],
    "日志": ["log", "logging", "trace", "排查"],
}

SENT_SPLIT_RE = re.compile(r"(?<=[。！？；!?;.])")
CJK_RE = re.compile(r"[\u4e00-\u9fff]+")
WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9_+#.-]*")
HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
FENCE_RE = re.compile(r"^\s*(```|~~~)")


# ---------- 数据结构 ----------

class KbFile:
    """知识库里的一个文本文件"""

    __slots__ = ("rel", "path", "lines", "mtime", "size", "truncated")

    def __init__(self, rel, path, lines, mtime, size, truncated):
        self.rel = rel
        self.path = path
        self.lines = lines
        self.mtime = mtime
        self.size = size
        self.truncated = truncated


class Segment:
    """知识库分段：一段带来源定位的文本"""

    __slots__ = ("file", "start", "end", "breadcrumb", "text")

    def __init__(self, file, start, end, breadcrumb, text):
        self.file = file
        self.start = start
        self.end = end
        self.breadcrumb = breadcrumb
        self.text = text

    def to_dict(self):
        return {
            "file": self.file,
            "start": self.start,
            "end": self.end,
            "breadcrumb": self.breadcrumb,
            "text": self.text,
        }

    @staticmethod
    def from_dict(data):
        return Segment(
            data["file"],
            int(data["start"]),
            int(data["end"]),
            data.get("breadcrumb", ""),
            data["text"],
        )

    @property
    def source(self):
        return "%s:%d-%d" % (self.file, self.start, self.end)


# ---------- 分词 ----------

def tokenize(text):
    """中文按 2-gram、英文按单词切分，返回 token 列表"""
    tokens = []
    if not text:
        return tokens
    for match in CJK_RE.finditer(text):
        run = match.group(0)
        if len(run) == 1:
            tokens.append(run)
        else:
            for i in range(len(run) - 1):
                tokens.append(run[i:i + 2])
    for match in WORD_RE.finditer(text):
        tokens.append(match.group(0).lower())
    return tokens


def expand_query(query, enabled=True):
    """查询扩展：命中同义词表就补充相关词，返回（原词表, 扩展词表）"""
    base = tokenize(query)
    if not enabled:
        return base, []
    extras = []
    for key, words in SYNONYMS.items():
        if key in query:
            for word in words:
                for token in tokenize(word):
                    if token not in base and token not in extras:
                        extras.append(token)
    return base, extras


# ---------- 知识库加载 ----------

def scan_kb_files(kb_dir):
    """递归扫描知识库目录，返回 {相对路径: (mtime, size)}（用于缓存失效判断）"""
    root = Path(kb_dir)
    meta = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(root).as_posix()
        if any(part in SKIP_DIRS for part in path.relative_to(root).parts[:-1]):
            continue
        if path.name in SKIP_FILES or path.name.startswith("."):
            continue
        if path.suffix.lower() not in KB_EXTS:
            continue
        try:
            stat = path.stat()
        except OSError:
            continue
        meta[rel] = (stat.st_mtime, stat.st_size)
    return meta


def load_kb(kb_dir):
    """递归读取 .md/.txt/.json/.csv，跳过二进制与 .git 等目录，返回 KbFile 列表"""
    root = Path(kb_dir)
    if not root.is_dir():
        raise FileNotFoundError("知识库目录不存在：%s" % kb_dir)
    files = []
    for rel in sorted(scan_kb_files(root)):
        path = root / rel
        try:
            raw = path.read_bytes()
        except OSError:
            continue
        if b"\x00" in raw[:4096]:        # 含 NUL 的按二进制处理
            continue
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            try:
                text = raw.decode("gbk")
            except UnicodeDecodeError:
                continue                 # 解不开的编码直接跳过
        truncated = False
        if len(text) > MAX_FILE_CHARS:
            text = text[:MAX_FILE_CHARS]
            truncated = True
        stat = path.stat()
        files.append(KbFile(rel, path, text.splitlines(), stat.st_mtime, stat.st_size, truncated))
    return files


# ---------- 分段 ----------

def split_sentences(text):
    """按中英文句子边界切分；单句过长时硬切"""
    pieces = []
    for piece in SENT_SPLIT_RE.split(text):
        piece = piece.strip()
        if piece:
            pieces.append(piece)
    return pieces or ([text.strip()] if text.strip() else [])


def _group_units(units, max_chars):
    """把 (行号, 句子) 单元按 max_chars 贪心打包成片段"""
    chunks = []
    cur = []
    cur_len = 0
    for lineno, sentence in units:
        if cur and cur_len + len(sentence) + 1 > max_chars:
            chunks.append(cur)
            cur = []
            cur_len = 0
        if len(sentence) > max_chars:      # 单句超长：硬切
            if cur:
                chunks.append(cur)
                cur = []
                cur_len = 0
            for i in range(0, len(sentence), max_chars):
                chunks.append([(lineno, sentence[i:i + max_chars])])
            continue
        cur.append((lineno, sentence))
        cur_len += len(sentence) + 1
    if cur:
        chunks.append(cur)
    return chunks


def make_segment(file, breadcrumb, items):
    """把若干 (行号, 文本) 打成一个片段，行号取首尾"""
    text = " ".join(sentence for _, sentence in items).strip()
    return Segment(file, items[0][0], items[-1][0], breadcrumb, text)


def split_block(file, breadcrumb, items, max_chars=MAX_SEGMENT_CHARS):
    """按空行段落切好后，超长段落再按句切到不超过 max_chars"""
    if not items:
        return []
    total = sum(len(text) for _, text in items) + len(items)
    if total <= max_chars:
        return [make_segment(file, breadcrumb, items)]
    units = []
    for lineno, text in items:
        for sentence in split_sentences(text):
            units.append((lineno, sentence))
    return [make_segment(file, breadcrumb, chunk) for chunk in _group_units(units, max_chars)]


def segment_markdown(file, lines, max_chars=MAX_SEGMENT_CHARS):
    """Markdown：按标题层级切段，片段带上标题面包屑"""
    sections = []          # [(breadcrumb, [(行号, 行)])]
    headings = {}          # 层级 -> 标题
    current_items = []
    current_crumb = ""
    in_fence = False

    def flush():
        if current_items:
            sections.append((current_crumb, list(current_items)))

    for idx, line in enumerate(lines, 1):
        if FENCE_RE.match(line):
            in_fence = not in_fence
        heading = None if in_fence else HEADING_RE.match(line)
        if heading:
            flush()
            current_items = []
            level = len(heading.group(1))
            title = heading.group(2).strip() or "（无标题）"
            headings[level] = title
            for deeper in [lv for lv in headings if lv > level]:
                del headings[deeper]
            current_crumb = " > ".join(headings[lv] for lv in sorted(headings))
        current_items.append((idx, line))
    flush()

    segments = []
    for crumb, items in sections:
        segments.extend(split_block(file, crumb, items, max_chars))
    return segments


def segment_text(file, lines, max_chars=MAX_SEGMENT_CHARS):
    """纯文本：按空行段落切，超长段落再按句切"""
    segments = []
    block = []
    for idx, line in enumerate(lines, 1):
        if line.strip():
            block.append((idx, line))
        else:
            segments.extend(split_block(file, "", block, max_chars))
            block = []
    segments.extend(split_block(file, "", block, max_chars))
    return segments


def segment_file(kb_file, max_chars=MAX_SEGMENT_CHARS):
    """按文件类型分段；Markdown 走标题切，其余走段落切"""
    ext = Path(kb_file.rel).suffix.lower()
    if ext in (".md", ".markdown"):
        return segment_markdown(kb_file.rel, kb_file.lines, max_chars)
    return segment_text(kb_file.rel, kb_file.lines, max_chars)


def segment_files(files, max_chars=MAX_SEGMENT_CHARS):
    segments = []
    for kb_file in files:
        segments.extend(segment_file(kb_file, max_chars))
    return segments


# ---------- 倒排索引与 BM25 ----------

def build_index(segments):
    """纯 Python 倒排索引 + 文档长度统计"""
    postings = {}
    doc_len = []
    for doc_id, segment in enumerate(segments):
        tokens = tokenize(segment.text)
        doc_len.append(max(len(tokens), 1))
        tf = {}
        for token in tokens:
            tf[token] = tf.get(token, 0) + 1
        for token, count in tf.items():
            postings.setdefault(token, []).append([doc_id, count])
    total = sum(doc_len) or 1
    return {
        "version": CACHE_VERSION,
        "segments": [segment.to_dict() for segment in segments],
        "postings": postings,
        "doc_len": doc_len,
        "avgdl": total / float(len(doc_len) or 1),
    }


def bm25_search(query, index, top_k=DEFAULT_TOP_K, expand=True):
    """BM25 打分，返回按得分降序的 [(得分, Segment, 命中词)]"""
    segments = [Segment.from_dict(item) for item in index["segments"]]
    if not segments:
        return []
    base, extras = expand_query(query, expand)
    doc_len = index["doc_len"]
    avgdl = index["avgdl"] or 1.0
    n_docs = len(segments)
    scores = {}
    matched = {}
    for weight, terms in ((1.0, base), (0.6, extras)):
        for term in terms:
            postings = index["postings"].get(term)
            if not postings:
                continue
            df = len(postings)
            idf = math.log(1.0 + (n_docs - df + 0.5) / (df + 0.5))
            for doc_id, tf in postings:
                norm = tf * (BM25_K1 + 1) / (
                    tf + BM25_K1 * (1 - BM25_B + BM25_B * doc_len[doc_id] / avgdl)
                )
                scores[doc_id] = scores.get(doc_id, 0.0) + weight * idf * norm
                if weight == 1.0:
                    matched.setdefault(doc_id, set()).add(term)
    results = []
    for doc_id, score in scores.items():
        results.append((score, segments[doc_id], sorted(matched.get(doc_id, set()))))
    results.sort(key=lambda item: (-item[0], item[1].file, item[1].start))
    return results[:top_k]


def select_hits(results, min_score=DEFAULT_MIN_SCORE):
    """过滤：得分低于阈值的丢掉；返回 (命中的, 是否判定为无命中)"""
    hits = [item for item in results if item[0] >= min_score and item[2]]
    return hits, not hits


# ---------- 索引缓存 ----------

def save_cache(kb_dir, index, files_meta):
    """把索引写进知识库目录的 .kb_index.json，带上文件 mtime 便于失效检测"""
    payload = dict(index)
    payload["files"] = dict(
        (rel, {"mtime": meta[0], "size": meta[1]}) for rel, meta in files_meta.items()
    )
    cache_path = Path(kb_dir) / CACHE_NAME
    with open(str(cache_path), "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False)


def load_cache(kb_dir):
    """读缓存；结构不对或读不动都返回 None"""
    cache_path = Path(kb_dir) / CACHE_NAME
    if not cache_path.is_file():
        return None
    try:
        with open(str(cache_path), "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    if payload.get("version") != CACHE_VERSION:
        return None
    for key in ("segments", "postings", "doc_len", "files"):
        if key not in payload:
            return None
    return payload


def cache_is_valid(cache, files_meta):
    """缓存里的文件清单（含 mtime、size）必须与当前扫描结果完全一致"""
    cached = cache.get("files") or {}
    if set(cached.keys()) != set(files_meta.keys()):
        return False
    for rel, meta in files_meta.items():
        entry = cached.get(rel) or {}
        if int(entry.get("mtime", -1)) != int(meta[0]):
            return False
        if int(entry.get("size", -1)) != int(meta[1]):
            return False
    return True


def build_index_from_files(files, files_meta):
    segments = segment_files(files)
    index = build_index(segments)
    return index, segments


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
    parts = []
    for block in getattr(response, "content", []) or []:
        if getattr(block, "type", "") == "text":
            parts.append(getattr(block, "text", ""))
    return "\n".join(parts).strip()


QA_SYSTEM_PROMPT = """你是一名本地知识库问答助手。用户会给出若干从知识库检索到的片段，每个片段都标注了来源。
要求：
1. 只依据片段回答，绝对不要使用片段之外的知识；片段不足以回答时，明确说"根据现有资料无法回答这个问题"。
2. 回答中引用来源时，引用格式固定为 [来源: 文件名:起始行-结束行]，不要改写这个格式。
3. 回答使用简体中文，条理清楚，先给结论再给依据。
4. 只输出一个 JSON 对象，不要输出任何解释文字，不要使用 Markdown 代码围栏，格式：
{"answer": "带引用的中文回答", "citations": [{"file": "文件名", "start": 1, "end": 2}], "confidence": "高|中|低"}
"""

CONFIDENCE_LEVELS = ("高", "中", "低")


def parse_answer_json(text):
    """尽力把模型回复解析成问答 JSON；解析不出来返回 None（原文留给报告附录）"""
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
    answer = data.get("answer")
    if not isinstance(answer, str) or not answer.strip():
        return None
    citations = []
    raw_citations = data.get("citations")
    if isinstance(raw_citations, list):
        for item in raw_citations:
            if not isinstance(item, dict):
                continue
            file_name = item.get("file")
            if not isinstance(file_name, str) or not file_name:
                continue
            try:
                start_line = max(int(item.get("start", 1)), 1)
                end_line = max(int(item.get("end", start_line)), start_line)
            except (TypeError, ValueError):
                continue
            citations.append({"file": file_name, "start": start_line, "end": end_line})
    confidence = data.get("confidence")
    if confidence not in CONFIDENCE_LEVELS:
        confidence = "低"
    return {"answer": answer.strip(), "citations": citations, "confidence": confidence}


def build_user_message(question, hits):
    lines = ["问题：%s" % question, "", "检索到的知识库片段："]
    for rank, (score, segment, _) in enumerate(hits, 1):
        lines.append("")
        lines.append("片段 %d（检索得分 %.3f）" % (rank, score))
        lines.append("[来源: %s:%d-%d]" % (segment.file, segment.start, segment.end))
        if segment.breadcrumb:
            lines.append("标题路径：%s" % segment.breadcrumb)
        lines.append(segment.text)
    return "\n".join(lines)


def ask_model(question, hits, model):
    """模型问答；返回 (parsed, raw_text)，解析失败时 parsed 为 None"""
    client = anthropic.Anthropic()
    response = call_model(
        client,
        model,
        QA_SYSTEM_PROMPT,
        [{"role": "user", "content": build_user_message(question, hits)}],
        max_tokens=4000,
    )
    raw = response_text(response)
    return parse_answer_json(raw), raw


# ---------- 报告与输出 ----------

def build_report(question, hits, no_hit, parsed, raw_text, meta):
    """生成中文 Markdown 报告：答案 + 引用清单 + 检索得分表"""
    lines = ["# 知识库问答报告", ""]
    lines.append("- 提问：%s" % question)
    lines.append("- 知识库：%s" % meta["kb"])
    lines.append("- 检索片段数：%d（top-k=%d，BM25 k1=%.2f b=%.2f）" % (
        len(hits), meta["top_k"], BM25_K1, BM25_B))
    lines.append("- 生成时间：%s" % meta["generated_at"])
    lines.append("")

    lines.append("## 答案")
    lines.append("")
    if no_hit:
        lines.append("知识库里没有相关内容：检索得分低于阈值 %.2f，没有任何片段能回答这个问题。" % meta["min_score"])
        lines.append("")
        lines.append("可以试试换关键词、扩大 --top-k，或确认知识库目录里确实有相关文档。")
    elif meta["offline"]:
        lines.append("（离线模式：不调用模型，以下是检索命中的原文片段）")
        lines.append("")
        for rank, (score, segment, matched) in enumerate(hits, 1):
            lines.append("### 片段 %d：%s" % (rank, segment.source))
            lines.append("")
            if segment.breadcrumb:
                lines.append("标题路径：%s" % segment.breadcrumb)
                lines.append("")
            snippet = segment.text
            if len(snippet) > SNIPPET_CHARS * 3:
                snippet = snippet[:SNIPPET_CHARS * 3] + "……"
            lines.append("> %s" % snippet.replace("\n", "\n> "))
            lines.append("")
    elif parsed:
        lines.append(parsed["answer"])
        lines.append("")
        lines.append("置信度：%s" % parsed["confidence"])
    else:
        lines.append("模型回复没有解析成约定的 JSON，原文放在文末附录；下面是检索命中的片段原文：")
        lines.append("")
        for rank, (score, segment, matched) in enumerate(hits, 1):
            lines.append("> 片段 %d [%s]：%s" % (rank, segment.source, segment.text[:SNIPPET_CHARS]))
    lines.append("")

    lines.append("## 引用清单")
    lines.append("")
    if parsed and parsed["citations"]:
        lines.append("| # | 来源 | 标题路径 | 原文摘录 |")
        lines.append("| --- | --- | --- | --- |")
        for idx, citation in enumerate(parsed["citations"], 1):
            snippet = ""
            crumb = ""
            for _, segment, _ in hits:
                if (segment.file == citation["file"]
                        and segment.start <= citation["start"] <= segment.end):
                    snippet = segment.text[:80].replace("|", "\\|")
                    crumb = segment.breadcrumb
                    break
            lines.append("| %d | [%s:%d-%d] | %s | %s |" % (
                idx, citation["file"], citation["start"], citation["end"],
                crumb or "—", snippet or "—"))
    elif no_hit:
        lines.append("（无命中，没有可引用的片段）")
    else:
        lines.append("| # | 来源 | 标题路径 | 原文摘录 |")
        lines.append("| --- | --- | --- | --- |")
        for idx, (_, segment, _) in enumerate(hits, 1):
            lines.append("| %d | [%s] | %s | %s |" % (
                idx, segment.source, segment.breadcrumb or "—",
                segment.text[:80].replace("|", "\\|")))
    lines.append("")

    lines.append("## 检索得分表")
    lines.append("")
    if hits:
        lines.append("| 排名 | 得分 | 来源 | 标题路径 | 命中词 |")
        lines.append("| --- | --- | --- | --- | --- |")
        for rank, (score, segment, matched) in enumerate(hits, 1):
            lines.append("| %d | %.3f | %s | %s | %s |" % (
                rank, score, segment.source, segment.breadcrumb or "—",
                "、".join(matched) or "—"))
    else:
        lines.append("没有任何片段达到阈值 %.2f。" % meta["min_score"])
    lines.append("")

    if raw_text and not parsed:
        lines.append("## 附录：模型原始回复（未能解析为 JSON）")
        lines.append("")
        lines.append("```text")
        lines.append(raw_text or "（模型没有返回任何内容）")
        lines.append("```")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def print_console(question, hits, no_hit, parsed, meta):
    """终端输出：答案 + 引用 + 得分表（Markdown 形式）"""
    print("=" * 60)
    print("问：%s" % question)
    print("=" * 60)
    if no_hit:
        print("知识库里没有相关内容（最高得分低于阈值 %.2f）。" % meta["min_score"])
    elif meta["offline"]:
        print("（离线模式：命中 %d 个片段，原文如下）" % len(hits))
        for rank, (score, segment, matched) in enumerate(hits, 1):
            snippet = segment.text[:SNIPPET_CHARS].replace("\n", " ")
            print("  %d. [%.3f] [%s] %s" % (rank, score, segment.source, snippet))
    elif parsed:
        print(parsed["answer"])
        print()
        print("置信度：%s" % parsed["confidence"])
        print("引用：")
        for citation in parsed["citations"]:
            print("  - [%s:%d-%d]" % (citation["file"], citation["start"], citation["end"]))
    else:
        print("模型回复没有解析成约定的 JSON，报告附录里保留了原文。")
    print("-" * 60)
    print("检索得分表：")
    for rank, (score, segment, matched) in enumerate(hits, 1):
        print("  %d. %.3f  %s  %s  命中：%s" % (
            rank, score, segment.source,
            (segment.breadcrumb or "—")[:24],
            "、".join(matched) or "—"))
    print()


# ---------- 索引装配（缓存优先） ----------

def prepare_index(kb_dir, rebuild=False):
    """返回 (index, segments, used_cache)；缓存失效或 --rebuild 时重建并回写"""
    files_meta = scan_kb_files(kb_dir)
    cache = None if rebuild else load_cache(kb_dir)
    if cache and cache_is_valid(cache, files_meta):
        segments = [Segment.from_dict(item) for item in cache["segments"]]
        return cache, segments, True
    files = load_kb(kb_dir)
    index, segments = build_index_from_files(files, files_meta)
    try:
        save_cache(kb_dir, index, files_meta)
    except OSError:
        pass                      # 只读目录不影响检索
    return index, segments, False


def answer_question(kb_dir, question, args, index):
    """检索 + 生成回答，返回 Markdown 报告文本"""
    results = bm25_search(question, index, top_k=args.top_k, expand=not args.no_expand)
    hits, no_hit = select_hits(results, args.min_score)
    meta = {
        "kb": str(Path(kb_dir).expanduser()),
        "top_k": args.top_k,
        "min_score": args.min_score,
        "offline": args.offline,
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    parsed = None
    raw_text = ""
    if not args.offline and not no_hit:
        parsed, raw_text = ask_model(question, hits, args.model)
    report = build_report(question, hits, no_hit, parsed, raw_text, meta)
    print_console(question, hits, no_hit, parsed, meta)
    return report


# ---------- 自检 ----------

class _FakeMessages:
    def __init__(self, outer):
        self.outer = outer

    def create(self, **kwargs):
        self.outer["calls"] += 1
        return {"ok": True, "kwargs": kwargs}


class _FakeBetaMessages:
    def __init__(self, outer):
        self.outer = outer

    def create(self, **kwargs):
        self.outer["calls"] += 1
        raise TypeError("网关不认识 beta 参数")


class _FakeBeta:
    def __init__(self, outer):
        self.messages = _FakeBetaMessages(outer)


class _FakeClient:
    """假客户端：第一层抛 TypeError，第二层成功"""

    def __init__(self):
        self.state = {"calls": 0}
        self.beta = _FakeBeta(self.state)
        self.messages = _FakeMessages(self.state)


class _FakeBlock:
    type = "text"

    def __init__(self, text):
        self.text = text


class _FakeResponse:
    def __init__(self, text):
        self.content = [_FakeBlock(text)]


def run_self_test():
    """离线自检：造样本、跑全部确定性逻辑、assert 校验，不联网、不需要 API key"""
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)

        # --- 造一个 5 段小知识库（含 Markdown、纯文本、JSON、CSV、二进制与隐藏目录）---
        (root / "guide.md").write_text(
            "# 安装指南\n"                                   # 1
            "\n"                                             # 2
            "本项目是一个 Python 命令行工具。\n"                 # 3
            "\n"                                             # 4
            "## 依赖安装\n"                                   # 5
            "\n"                                             # 6
            "如何安装依赖？使用 pip 安装依赖即可。\n"            # 7
            "\n"                                             # 8
            "```bash\n"                                      # 9
            "# 不是标题（在代码块里）\n"                       # 10
            "pip install -r requirements.txt\n"               # 11
            "```\n"                                          # 12
            "\n"                                             # 13
            "## 常见问题\n"                                   # 14
            "\n"                                             # 15
            "安装失败时先升级 pip 再重试。\n",                  # 16
            encoding="utf-8",
        )
        long_para = "这是一段很长的说明文字。" * 60            # 远超 800 字符
        (root / "notes.txt").write_text(
            "数据库备份说明。\n"
            "\n"
            + long_para + "\n"
            "\n"
            "部署流程见运维手册。\n",
            encoding="utf-8",
        )
        (root / "data.json").write_text('{"服务": "网关", "端口": 8080}\n', encoding="utf-8")
        (root / "table.csv").write_text("名称,版本\napi,1.0\n", encoding="utf-8")
        (root / "blob.bin").write_bytes(b"\x00\x01\x02binary\x00")
        (root / ".git").mkdir()
        (root / ".git" / "config.txt").write_text("git 配置不应被索引\n", encoding="utf-8")
        (root / ".hidden.txt").write_text("隐藏文件不应被索引\n", encoding="utf-8")

        # --- 知识库加载：只留 4 个文本文件 ---
        files = load_kb(root)
        rels = [f.rel for f in files]
        assert sorted(rels) == ["data.json", "guide.md", "notes.txt", "table.csv"], rels
        assert "blob.bin" not in rels
        assert not any(".git" in rel for rel in rels)
        assert not any(rel.startswith(".") for rel in rels)

        # --- 分段：Markdown 标题面包屑 + 行号 ---
        segments = segment_files(files)
        assert len(segments) >= 5, "至少应切出 5 段，实际 %d" % len(segments)
        install_seg = None
        for segment in segments:
            if "使用 pip 安装依赖" in segment.text:
                install_seg = segment
                break
        assert install_seg is not None, "没有切出安装依赖片段"
        assert install_seg.file == "guide.md"
        assert install_seg.breadcrumb == "安装指南 > 依赖安装", install_seg.breadcrumb
        assert install_seg.start <= 7 <= install_seg.end, (install_seg.start, install_seg.end)
        for segment in segments:
            assert 1 <= segment.start <= segment.end
            assert segment.end <= len(
                [f for f in files if f.rel == segment.file][0].lines
            ), "片段行号越界：%s" % segment.source
            assert segment.text.strip(), "片段不能为空"

        # 代码块里的 "# 不是标题" 不能当成标题切
        assert not any("不是标题（在代码块里）" in (s.breadcrumb or "") for s in segments)

        # 纯文本按空行段落切，超长段落再按句切到 <=800 字符
        note_segments = [s for s in segments if s.file == "notes.txt"]
        assert len(note_segments) >= 3, "notes.txt 应至少切成 3 段"
        long_segments = [s for s in note_segments if "很长的说明文字" in s.text]
        assert long_segments, "长段落没有被切出来"
        assert all(len(s.text) <= MAX_SEGMENT_CHARS for s in long_segments), "超长片段未按句切"
        assert all(s.start == s.end or s.start < s.end for s in long_segments)

        # --- 倒排索引与 BM25 排序 ---
        index = build_index(segments)
        assert index["postings"], "倒排索引不能为空"
        assert len(index["doc_len"]) == len(segments)
        results = bm25_search("如何安装依赖", index, top_k=5)
        assert results, "BM25 应该有结果"
        assert results[0][1].file == "guide.md", "最相关片段应排第一：%s" % results[0][1].source
        assert "使用 pip 安装依赖" in results[0][1].text
        scores = [score for score, _, _ in results]
        assert scores == sorted(scores, reverse=True), "得分应单调不增"
        assert scores[0] > scores[-1], "首尾得分应拉开差距"
        assert results[0][2], "应记录命中词"

        # --- 查询扩展 ---
        base, extras = expand_query("如何配置数据库", True)
        assert base and extras, "应有原词与扩展词"
        assert "如何配置数据库" == "如何配置数据库"
        base2, extras2 = expand_query("如何配置数据库", False)
        assert extras2 == [] and base2 == base

        # --- 索引缓存：写读、命中、mtime 失效 ---
        save_cache(root, index, scan_kb_files(root))
        cache_path = root / CACHE_NAME
        assert cache_path.is_file(), "缓存文件应写入知识库目录"
        loaded = load_cache(root)
        assert loaded and loaded["version"] == CACHE_VERSION
        assert cache_is_valid(loaded, scan_kb_files(root))
        index2, segments2, used_cache = prepare_index(root, rebuild=False)
        assert used_cache, "缓存有效时应直接复用"
        assert len(segments2) == len(segments)
        results2 = bm25_search("如何安装依赖", index2, top_k=5)
        assert results2[0][1].source == results[0][1].source, "缓存前后检索结果应一致"

        # 改动文件 mtime → 缓存必须失效并重建
        target = root / "notes.txt"
        os.utime(str(target), (time.time() + 10, time.time() + 10))
        index3, _, used_cache3 = prepare_index(root, rebuild=False)
        assert not used_cache3, "mtime 变化后不应继续用缓存"
        assert cache_is_valid(load_cache(root), scan_kb_files(root)), "重建后缓存应再次有效"

        # --rebuild 强制重建
        _, _, used_cache4 = prepare_index(root, rebuild=True)
        assert not used_cache4, "--rebuild 应强制重建"

        # --- 无命中兜底 ---
        results_none = bm25_search("量子纠缠与暗物质实验", index, top_k=5)
        hits_none, no_hit = select_hits(results_none, DEFAULT_MIN_SCORE)
        assert no_hit and not hits_none, "无关问题应判定为无命中"
        hits_high, no_high = select_hits(results, 10_000.0)
        assert no_high and not hits_high, "阈值过高也应判定为无命中"

        # --- 模型回复 JSON 解析兜底 ---
        good = (
            "```json\n"
            '{"answer": "先安装依赖 [来源: guide.md:5-11]。", '
            '"citations": [{"file": "guide.md", "start": 5, "end": 11}], '
            '"confidence": "高"}\n'
            "```"
        )
        parsed = parse_answer_json(good)
        assert parsed and parsed["confidence"] == "高"
        assert parsed["citations"][0]["file"] == "guide.md"
        assert parsed["citations"][0]["start"] == 5
        assert parse_answer_json("这不是 JSON") is None
        assert parse_answer_json("{not json") is None
        assert parse_answer_json('{"citations": []}') is None, "没有 answer 应返回 None"
        weird = '{"answer": "ok", "citations": [{"file": "a.md", "start": "x", "end": 3}], "confidence": "未知"}'
        coerced = parse_answer_json(weird)
        assert coerced and coerced["citations"] == [] and coerced["confidence"] == "低"

        # --- 报告生成：得分表、引用清单、无命中与解析失败兜底 ---
        meta = {
            "kb": str(root), "top_k": 5, "min_score": DEFAULT_MIN_SCORE,
            "offline": False, "generated_at": "2026-01-01 00:00:00",
        }
        hits, no_hit = select_hits(results, DEFAULT_MIN_SCORE)
        report = build_report("如何安装依赖", hits, no_hit, parsed, "", meta)
        assert report.startswith("# 知识库问答报告")
        assert "## 答案" in report and "## 引用清单" in report and "## 检索得分表" in report
        assert "[guide.md:5-11]" in report
        assert "置信度：高" in report

        report_none = build_report("量子问题", [], True, None, "", meta)
        assert "知识库里没有相关内容" in report_none

        report_raw = build_report("如何安装依赖", hits, False, None, "自由格式回复", meta)
        assert "附录：模型原始回复" in report_raw and "自由格式回复" in report_raw

        report_offline = build_report("如何安装依赖", hits, False, None, "", dict(meta, offline=True))
        assert "原文片段" in report_offline

        # --- response_text 提取 ---
        assert response_text(_FakeResponse("你好")) == "你好"
        assert response_text({"content": []}) == ""

        # --- call_model 逐级降级（离线假客户端） ---
        fake = _FakeClient()
        result = call_model(fake, "fake-model", "system", [{"role": "user", "content": "hi"}])
        assert result["ok"] is True
        assert fake.state["calls"] == 2, "第一层抛 TypeError、第二层成功，共调用 2 次"

        # --- 离线问答走通（不调模型）---
        args = argparse.Namespace(
            top_k=3, min_score=DEFAULT_MIN_SCORE, offline=True,
            no_expand=False, model="fake-model",
        )
        offline_report = answer_question(root, "如何安装依赖", args, index)
        assert "检索得分表" in offline_report and "guide.md" in offline_report

    print("SELF-TEST OK")


# ---------- CLI ----------

def build_parser():
    parser = argparse.ArgumentParser(
        description="本地知识库问答：倒排索引 + BM25 检索命中片段，再让模型基于片段生成带引用的中文回答",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            "  python3 agent.py --kb ./docs --ask \"如何安装依赖\"\n"
            "  python3 agent.py --kb ./docs --ask \"配置项有哪些\" --top-k 8 --output 答案.md\n"
            "  python3 agent.py --kb ./docs                # 交互问答，输入 exit 退出\n"
            "  python3 agent.py --kb ./docs --ask \"如何部署\" --offline\n"
        ),
    )
    parser.add_argument("--kb", default=".", help="知识库目录（递归读取 .md/.txt/.json/.csv），默认当前目录")
    parser.add_argument("--ask", default="", help="要提问的问题；不带这个参数就进入交互问答")
    parser.add_argument("--top-k", type=int, default=DEFAULT_TOP_K, help="检索返回的片段数量，默认 5")
    parser.add_argument("--min-score", type=float, default=DEFAULT_MIN_SCORE,
                        help="命中判定的最低 BM25 得分，低于它视为知识库里没有相关内容，默认 0.5")
    parser.add_argument("--rebuild", action="store_true", help="忽略缓存强制重建索引")
    parser.add_argument("--no-expand", action="store_true", help="关闭同义词查询扩展")
    parser.add_argument("--offline", action="store_true", help="离线模式：只检索不调模型，打印命中的原文片段")
    parser.add_argument("--model", default=DEFAULT_MODEL,
                        help="模型名，默认 %s；走第三方兼容网关时用 --model 指定网关认得的模型名" % DEFAULT_MODEL)
    parser.add_argument("--output", default="", help="把 Markdown 报告写到该文件（不给就只打印到终端）")
    parser.add_argument("--self-test", action="store_true", help="离线自检：不联网、不需要 API key")
    return parser


def require_api_key():
    """需要模型的路径必须先有 key，否则打印中文提示并退出"""
    if not os.environ.get("ANTHROPIC_API_KEY") and not os.environ.get("ANTHROPIC_AUTH_TOKEN"):
        print(
            "提示：没有检测到 ANTHROPIC_API_KEY 或 ANTHROPIC_AUTH_TOKEN，无法调用模型回答。\n"
            "请先设置（二选一）：\n"
            "  macOS / Linux：      export ANTHROPIC_API_KEY=你的key\n"
            "  Windows PowerShell： $env:ANTHROPIC_API_KEY=\"你的key\"\n"
            "也可以加 --offline 只做本地检索（不需要 key、不联网）。",
            file=sys.stderr,
        )
        sys.exit(1)


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

    if args.top_k < 1:
        parser.error("--top-k 必须大于 0")
    if args.min_score < 0:
        parser.error("--min-score 不能为负数")

    kb_dir = Path(args.kb).expanduser()
    if not kb_dir.is_dir():
        print("错误：知识库目录不存在：%s" % kb_dir, file=sys.stderr)
        sys.exit(1)

    try:
        index, segments, used_cache = prepare_index(kb_dir, rebuild=args.rebuild)
    except (OSError, ValueError) as exc:
        print("错误：知识库索引构建失败：%s" % exc, file=sys.stderr)
        sys.exit(1)

    print("[索引] 片段 %d 个，来源 %d 个文件（%s）" % (
        len(segments),
        len(set(segment.file for segment in segments)),
        "命中缓存" if used_cache else "已重建",
    ))

    def run_once(question):
        report = answer_question(kb_dir, question, args, index)
        if args.output:
            output_path = Path(args.output).expanduser()
            try:
                if output_path.parent and not output_path.parent.exists():
                    output_path.parent.mkdir(parents=True, exist_ok=True)
                output_path.write_text(report, encoding="utf-8")
                print("[完成] 报告已写入：%s" % output_path)
            except OSError as exc:
                print("错误：报告写入失败：%s" % exc, file=sys.stderr)
                sys.exit(1)

    if not args.offline:
        require_api_key()

    if args.ask:
        run_once(args.ask)
        return

    print("[交互] 请输入问题（输入 exit / quit / 退出 结束）：")
    while True:
        try:
            question = input("问> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if question.lower() in ("exit", "quit", "退出", "q"):
            break
        if not question:
            continue
        run_once(question)


if __name__ == "__main__":
    main()
