#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
多语言翻译助手（translator）—— 占位符保护 + 分批翻译 + 术语表 的批量翻译 agent

确定性部分先把不该翻译的内容（Markdown 代码块、行内代码、链接 URL、图片、
占位符 {name} / %s / <tag> / {{var}}）抽成 \x00N\x00 占位符，按段落/句子边界
分批（每批不超过 --batch-chars），再让模型只输出译文；译后无损还原占位符，
并按术语表强制统一术语。JSON 只翻字符串 value、保留 key 与结构，CSV 可按列
选择翻译。最后生成中文 Markdown 翻译报告。除 anthropic SDK 外只用标准库。

用法：
    python3 agent.py <文件或目录...> --to 英语 [选项]
    python3 agent.py --text "要翻译的文本" --to 英语

示例：
    python3 agent.py ./docs --to 英语 --from 中文 --out-dir ./translated
    python3 agent.py data.json --to 日语 --glossary 术语表.json
    python3 agent.py table.csv --to 英语 --columns 名称,备注
    python3 agent.py --text "你好，世界" --to 英语 --offline

环境要求：
    1. 调用模型翻译需要设置 API key（--offline 与 --self-test 不需要）：
           macOS / Linux：      export ANTHROPIC_API_KEY=你的key
           Windows PowerShell：$env:ANTHROPIC_API_KEY="你的key"
       走第三方 Anthropic 兼容网关时，改设 ANTHROPIC_BASE_URL 与
       ANTHROPIC_AUTH_TOKEN，并用 --model 指定网关支持的模型名。
    2. 安装 SDK：
           pip3 install -r requirements.txt
"""

import argparse
import csv
import io
import json
import os
import re
import sys
import tempfile
from datetime import datetime
from pathlib import Path

import anthropic

# 默认模型（可用 --model 参数或 TRANSLATOR_MODEL 环境变量更换）
DEFAULT_MODEL = os.environ.get("TRANSLATOR_MODEL", "claude-opus-5")

DEFAULT_OUT_DIR = "./translated"
DEFAULT_REPORT_NAME = "translation-report.md"
DEFAULT_BATCH_CHARS = 6000     # 每批不超过多少字符
DEFAULT_MAX_FILES = 500        # 单次最多翻译多少个文件
DEFAULT_MAX_FILE_CHARS = 500_000

PLACEHOLDER_FMT = "\x00%d\x00"  # 占位符格式：NUL + 序号 + NUL
TOKEN_RE = re.compile(r"\x00\s*(\d+)\s*\x00")

IGNORED_DIRS = {".git", "node_modules", "__pycache__", "dist", "build", "venv", ".venv"}

JSON_SUFFIXES = {".json"}
CSV_SUFFIXES = {".csv", ".tsv"}
TEXT_SUFFIXES = {
    ".md", ".markdown", ".txt", ".text", ".rst", ".html", ".htm", ".xml",
    ".yaml", ".yml", ".po", ".strings", ".srt", ".vtt",
}

CJK_RE = re.compile(r"[\u4e00-\u9fff]")
EN_WORD_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9'\-]*")

# 不该被翻译的片段，按顺序抽取（顺序很重要：先整体后局部）
PROTECT_PATTERNS = (
    ("代码块", re.compile(r"```[\s\S]*?```")),
    ("行内代码", re.compile(r"`[^`\n]*`")),
    ("图片", re.compile(r"!\[[^\]\n]*\]\([^)\n]*\)")),
    ("HTML 标签", re.compile(r"</?[A-Za-z][A-Za-z0-9:_-]*(?:\s[^<>\n]*)?/?>")),
    ("双花括号占位符", re.compile(r"\{\{[^{}\n]*\}\}")),
    ("花括号占位符", re.compile(r"\{[A-Za-z_][A-Za-z0-9_]*\}")),
    ("百分号占位符", re.compile(r"%(?:\([A-Za-z_][A-Za-z0-9_]*\))?[sdif%]")),
    ("美元占位符", re.compile(r"\$\{[A-Za-z_][A-Za-z0-9_]*\}")),
)
LINK_URL_RE = re.compile(r"\]\([^)\n]*\)")

PARA_SPLIT_RE = re.compile(r"(\n{2,})")
SENT_SPLIT_RE = re.compile(r"([。！？；!?;]\s*|\n)")


# ---------- 占位符保护 ----------

class TextProtector:
    """把不该翻译的片段抽成 \x00N\x00 占位符，译后无损还原"""

    __slots__ = ("tokens",)

    def __init__(self):
        self.tokens = []

    def add(self, snippet):
        """登记一段原文，返回它的占位符"""
        self.tokens.append(snippet)
        return PLACEHOLDER_FMT % (len(self.tokens) - 1)

    def protect(self, text):
        """按顺序把代码、链接、图片、占位符抽走，返回可安全翻译的文本"""
        for _name, pattern in PROTECT_PATTERNS:
            text = pattern.sub(lambda m: self.add(m.group(0)), text)

        def _protect_link(match):
            whole = match.group(0)
            inner = whole[2:-1]
            return "](" + self.add(inner) + ")"

        return LINK_URL_RE.sub(_protect_link, text)

    def restore(self, text):
        """把占位符还原成原文；容忍模型对占位符做的轻微变形（加空格）"""
        def _restore(match):
            index = int(match.group(1))
            if 0 <= index < len(self.tokens):
                return self.tokens[index]
            return match.group(0)

        return TOKEN_RE.sub(_restore, text)

    def unbalanced(self, text):
        """判断文本里是否有被切断的占位符（NUL 数量为奇数）"""
        return text.count("\x00") % 2 == 1


# ---------- 术语表 ----------

def load_glossary(path):
    """读取术语表 JSON：{"源词": "译法"}，也支持 {"terms": {...}} 写法；文件缺失/损坏时按空表处理"""
    try:
        raw = Path(path).expanduser().read_text(encoding="utf-8")
        data = json.loads(raw)
    except (OSError, ValueError) as exc:
        print("[提示] 术语表不可用（%s），按空术语表继续。" % exc)
        return {}
    if isinstance(data, dict) and isinstance(data.get("terms"), dict):
        data = data["terms"]
    if not isinstance(data, dict):
        raise ValueError("术语表必须是 JSON 对象：{\"源词\": \"译法\"}")
    glossary = {}
    for key, value in data.items():
        key = str(key).strip()
        if key:
            glossary[key] = str(value)
    return glossary


def apply_glossary(text, protector, glossary):
    """译前把术语表源词抽成占位符（值写目标译法），保证术语不被译走"""
    for src in sorted(glossary, key=len, reverse=True):
        if src in text:
            text = text.replace(src, protector.add(glossary[src]))
    return text


def glossary_fixup(text, glossary):
    """译后兜底：漏网的源词统一替换成术语表译法"""
    for src in sorted(glossary, key=len, reverse=True):
        if src in text:
            text = text.replace(src, glossary[src])
    return text


# ---------- 分批切分 ----------

def _split_keep(text, regex):
    """按正则切分但保留分隔符，所有片段拼回 == 原文"""
    parts = regex.split(text)
    return [part for part in parts if part != ""]


def _hard_split(text, limit):
    """长片段硬切；尽量不切断 \x00N\x00 占位符"""
    pieces = []
    start = 0
    total = len(text)
    while start < total:
        end = min(start + limit, total)
        if end < total:
            tail = text[start:end]
            if tail.count("\x00") % 2 == 1:
                cut = text.rfind("\x00", start, end)
                if cut > start:
                    end = cut
        pieces.append(text[start:end])
        start = end
    return pieces


def split_units(text, limit):
    """把文本切成「段落 → 句子 → 硬切」的单元，每个单元不超过 limit"""
    units = []
    for para in _split_keep(text, PARA_SPLIT_RE):
        if len(para) <= limit:
            units.append(para)
            continue
        for sent in _split_keep(para, SENT_SPLIT_RE):
            if len(sent) <= limit:
                units.append(sent)
            else:
                units.extend(_hard_split(sent, limit))
    return units


def split_batches(text, limit):
    """贪心打包成每批不超过 limit 的批次，所有批次拼回 == 原文"""
    if limit < 1:
        raise ValueError("批大小必须大于 0")
    if not text:
        return []
    batches = []
    current = ""
    for unit in split_units(text, limit):
        if current and len(current) + len(unit) > limit:
            batches.append(current)
            current = ""
        current += unit
    if current:
        batches.append(current)
    return batches


# ---------- 统计 ----------

def count_chars(text):
    """字符数（含空白与换行）"""
    return len(text)


def count_words(text):
    """词数：中文字数 + 英文单词数"""
    return len(CJK_RE.findall(text)) + len(EN_WORD_RE.findall(text))


# ---------- JSON / CSV 翻译 ----------

def walk_json(value, fn):
    """递归遍历 JSON：只对字符串 value 应用 fn，key 与结构保持不变"""
    if isinstance(value, dict):
        return dict((key, walk_json(item, fn)) for key, item in value.items())
    if isinstance(value, list):
        return [walk_json(item, fn) for item in value]
    if isinstance(value, str):
        return fn(value)
    return value


def collect_json_strings(value, out):
    """按遍历顺序收集非空字符串 value"""
    if isinstance(value, dict):
        for _key, item in value.items():
            collect_json_strings(item, out)
    elif isinstance(value, list):
        for item in value:
            collect_json_strings(item, out)
    elif isinstance(value, str) and value.strip():
        out.append(value)


def translate_json_text(text, translate_cells):
    """翻译 JSON 文件：只翻字符串 value，保留 key 与结构。

    translate_cells(list[str]) -> (list[str], 失败批号列表)
    """
    data = json.loads(text)
    strings = []
    collect_json_strings(data, strings)
    results, failed = translate_cells(strings)
    iterator = iter(results)

    def _replace(item):
        if isinstance(item, str) and item.strip():
            return next(iterator)
        return item

    new_data = walk_json(data, _replace)
    return json.dumps(new_data, ensure_ascii=False, indent=2) + "\n", {
        "cells": len(strings),
        "failed": failed,
    }


def parse_column_spec(spec, header):
    """把 --columns 解析成 0-based 列序号集合；空值表示全部列"""
    if not spec:
        return None
    targets = set()
    for raw in spec.split(","):
        item = raw.strip()
        if not item:
            continue
        if item.isdigit():
            targets.add(int(item))
        elif header and item in header:
            targets.add(header.index(item))
        else:
            raise ValueError("在表头里找不到列：%s" % item)
    return targets


def translate_csv_text(text, translate_cells, columns=None, delimiter=","):
    """翻译 CSV/TSV：表头保留不译，可按 --columns 选择要翻译的列"""
    rows = list(csv.reader(io.StringIO(text), delimiter=delimiter))
    if not rows:
        return "", {"cells": 0, "failed": []}
    header = rows[0]
    targets = parse_column_spec(columns, header)

    todo = []
    positions = []
    for ridx, row in enumerate(rows):
        if ridx == 0:
            continue  # 表头不翻译，保持列名稳定
        for cidx, cell in enumerate(row):
            if cell.strip() and (targets is None or cidx in targets):
                todo.append(cell)
                positions.append((ridx, cidx))
    results, failed = translate_cells(todo)
    for (ridx, cidx), value in zip(positions, results):
        rows[ridx][cidx] = value

    buffer = io.StringIO()
    writer = csv.writer(buffer, delimiter=delimiter, lineterminator="\n")
    writer.writerows(rows)
    return buffer.getvalue(), {"cells": len(todo), "failed": failed}


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


TRANSLATE_SYSTEM_PROMPT = """你是一名专业译者。用户会给出需要翻译的文本。
文本里的 \\x00N\\x00 形式的占位符（NUL 字符包住数字）代表代码、链接、变量名、
标签等不能翻译也不能改动的内容。

输出要求：
1. 只输出一个 JSON 对象：{"translation": "译文"}。不要解释，不要用 Markdown 代码围栏。
2. 所有占位符必须原样保留：数量不变、顺序不变、内容不变，不要翻译占位符本身。
3. 只输出译文本身，不要加译注、注释、原文或任何说明。
"""

TRANSLATE_MANY_SYSTEM_PROMPT = """你是一名专业译者。用户会给出一个 JSON 字符串数组，数组里是若干条待翻译文本。
文本里的 \\x00N\\x00 形式的占位符（NUL 字符包住数字）代表代码、变量名、标签等不能翻译也不能改动的内容。

输出要求：
1. 只输出一个 JSON 对象：{"translations": ["译文1", "译文2", ...]}。
2. translations 必须与输入数组长度相同、顺序一致。
3. 所有占位符原样保留，不要翻译占位符本身，不要加任何解释。
"""


def _strip_fence(text):
    content = text.strip()
    if content.startswith("```"):
        content = re.sub(r"^```[A-Za-z]*\s*", "", content)
        content = re.sub(r"\s*```$", "", content)
    return content


def _extract_json_object(text):
    content = _strip_fence(text)
    start = content.find("{")
    end = content.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(content[start:end + 1])
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def parse_translation_json(text):
    """解析 {"translation": "..."}；失败返回 None（调用方保留原文）"""
    data = _extract_json_object(text)
    if data is None:
        return None
    value = data.get("translation")
    if not isinstance(value, str) or not value.strip():
        return None
    return value


def parse_translations_json(text, expected):
    """解析 {"translations": [...]}；数量不符或失败返回 None"""
    data = _extract_json_object(text)
    if data is None:
        return None
    values = data.get("translations")
    if not isinstance(values, list) or len(values) != expected:
        return None
    if not all(isinstance(item, str) for item in values):
        return None
    return values


# ---------- 翻译流程 ----------

class Translator:
    """把「保护 → 分批 → 调模型 → 还原 → 术语表兜底」串起来"""

    __slots__ = ("model", "from_lang", "to_lang", "glossary", "batch_chars",
                 "offline", "client", "verbose", "call_count", "failed_batches",
                 "columns")

    def __init__(self, model, from_lang, to_lang, glossary=None,
                 batch_chars=DEFAULT_BATCH_CHARS, offline=False, client=None,
                 verbose=True, columns=None):
        self.model = model
        self.from_lang = from_lang
        self.to_lang = to_lang
        self.glossary = glossary or {}
        self.batch_chars = batch_chars
        self.offline = offline
        self.client = client
        self.verbose = verbose
        self.call_count = 0
        self.failed_batches = []
        self.columns = columns  # 仅 CSV 模式使用：要翻译的列

    def _request(self, system, prompt):
        client = self.client if self.client is not None else anthropic.Anthropic()
        self.call_count += 1
        response = call_model(
            client,
            self.model,
            system,
            [{"role": "user", "content": prompt}],
            max_tokens=8000,
        )
        return response_text(response)

    def _lang_hint(self):
        return "从%s翻译成%s" % (self.from_lang, self.to_lang)

    def _translate_batch(self, protected_text):
        """翻译一批文本，返回译文；失败返回 None"""
        if self.offline:
            return protected_text
        prompt = "请把下面的文本%s：\n\n%s" % (self._lang_hint(), protected_text)
        return parse_translation_json(self._request(TRANSLATE_SYSTEM_PROMPT, prompt))

    def _translate_many(self, protected_items):
        """批量翻译短文本，返回 (结果列表, 失败批号列表)"""
        results = list(protected_items)
        batches = []
        current_idx = []
        current_texts = []
        size = 0
        for index, item in enumerate(protected_items):
            if current_texts and size + len(item) > self.batch_chars:
                batches.append((current_idx, current_texts))
                current_idx, current_texts, size = [], [], 0
            current_idx.append(index)
            current_texts.append(item)
            size += len(item) + 4
        if current_texts:
            batches.append((current_idx, current_texts))

        failed = []
        for number, (indices, texts) in enumerate(batches, 1):
            if self.offline:
                continue
            payload = json.dumps(texts, ensure_ascii=False)
            prompt = "请把下面 JSON 数组里的每条文本%s，按顺序返回：\n\n%s" % (
                self._lang_hint(), payload,
            )
            translated = parse_translations_json(
                self._request(TRANSLATE_MANY_SYSTEM_PROMPT, prompt), len(texts),
            )
            if translated is None:
                failed.append(number)
                continue
            for position, index in enumerate(indices):
                results[index] = translated[position]
        return results, failed

    def translate_cells(self, items):
        """翻译一批短文本（JSON 值 / CSV 单元格），逐条保护占位符与术语"""
        protected_items = []
        protectors = []
        for item in items:
            protector = TextProtector()
            protected = protector.protect(item)
            if self.glossary:
                protected = apply_glossary(protected, protector, self.glossary)
            protected_items.append(protected)
            protectors.append(protector)
        results, failed = self._translate_many(protected_items)
        out = []
        for protector, value in zip(protectors, results):
            text = protector.restore(value)
            if self.glossary:
                text = glossary_fixup(text, self.glossary)
            out.append(text)
        return out, failed

    def translate_plain(self, text):
        """翻译一段纯文本/Markdown，返回 (译文, 统计信息)"""
        protector = TextProtector()
        protected = protector.protect(text)
        if self.glossary:
            protected = apply_glossary(protected, protector, self.glossary)
        batches = split_batches(protected, self.batch_chars)
        pieces = []
        failed = []
        for number, batch in enumerate(batches, 1):
            if self.verbose:
                print("[翻译] 第 %d/%d 批（%d 字符）" % (number, len(batches), len(batch)))
            translated = self._translate_batch(batch)
            if translated is None:
                failed.append(number)
                pieces.append(batch)
                if self.verbose:
                    print("[翻译] 第 %d 批解析失败，保留原文。" % number)
            else:
                pieces.append(translated)
        self.failed_batches.extend(failed)
        translated_text = protector.restore("".join(pieces))
        if self.glossary:
            translated_text = glossary_fixup(translated_text, self.glossary)
        return translated_text, {"batches": len(batches), "failed": failed}


def translate_file(text, rel, translator):
    """按文件类型分派翻译，返回 (译文, 统计信息)；JSON 解析失败时按纯文本处理"""
    suffix = Path(rel).suffix.lower()
    if suffix in JSON_SUFFIXES:
        try:
            return translate_json_text(text, translator.translate_cells)
        except ValueError as exc:
            translated, meta = translator.translate_plain(text)
            meta = dict(meta)
            meta["note"] = "JSON 解析失败（%s），已按纯文本翻译" % exc
            return translated, meta
    if suffix in CSV_SUFFIXES:
        delimiter = "\t" if suffix == ".tsv" else ","
        return translate_csv_text(text, translator.translate_cells, translator.columns, delimiter)
    return translator.translate_plain(text)


# ---------- 文件收集 ----------

def _kind_for(rel):
    suffix = Path(rel).suffix.lower()
    if suffix in JSON_SUFFIXES:
        return "json"
    if suffix in CSV_SUFFIXES:
        return "csv"
    return "text"


def _iter_directory(root):
    for dirpath, dirnames, filenames in os.walk(str(root)):
        dirnames[:] = sorted(d for d in dirnames if d not in IGNORED_DIRS)
        for name in sorted(filenames):
            path = Path(dirpath) / name
            yield path, path.relative_to(root).as_posix()


def load_file(path, rel, max_chars=DEFAULT_MAX_FILE_CHARS):
    """读取一个文件；返回 (文本 或 None, 跳过原因)"""
    try:
        with path.open("rb") as handle:
            raw = handle.read(max_chars * 4 + 8)
    except OSError as exc:
        return None, "读取失败：%s" % exc
    if b"\x00" in raw[:4096]:
        return None, "二进制文件"
    try:
        text = raw.decode("utf-8", "strict")
    except UnicodeDecodeError:
        return None, "非 UTF-8 文本"
    return text.replace("\r\n", "\n").replace("\r", "\n"), ""


def collect_inputs(paths, max_files=DEFAULT_MAX_FILES):
    """展开路径、过滤、读取。返回 [(path, rel, kind, text), ...], skipped"""
    items = []
    skipped = []
    seen = set()
    for raw_path in paths:
        path = Path(raw_path).expanduser()
        if not path.exists():
            raise FileNotFoundError("路径不存在：%s" % raw_path)
        candidates = [(path, path.name)] if path.is_file() else list(_iter_directory(path))
        for file_path, rel in candidates:
            if len(items) >= max_files:
                skipped.append((rel, "达到 --max-files=%d 上限，未翻译" % max_files))
                continue
            try:
                resolved = file_path.resolve()
            except OSError:
                resolved = file_path
            key = os.path.normcase(str(resolved))
            if key in seen:
                continue
            seen.add(key)
            text, reason = load_file(file_path, rel)
            if text is None:
                skipped.append((rel, reason))
                continue
            items.append((file_path, rel, _kind_for(rel), text))
    return items, skipped


# ---------- 报告 ----------

def build_report(entries, from_lang, to_lang, glossary, offline, model,
                 skipped=None, generated_at=None):
    """生成中文 Markdown 翻译报告"""
    skipped = skipped or []
    total_chars = sum(entry["chars"] for entry in entries)
    total_words = sum(entry["words"] for entry in entries)
    total_batches = sum(entry["batches"] for entry in entries)
    failed_total = sum(len(entry["failed"]) for entry in entries)
    now = generated_at or datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    lines = [
        "# 翻译报告",
        "",
        "- 生成时间：%s" % now,
        "- 源语言：%s · 目标语言：%s" % (from_lang, to_lang),
        "- 运行模式：%s" % ("离线（--offline，未调用模型，译文保留原文）" if offline else "模型翻译（%s）" % model),
        "- 术语表：%d 条%s" % (len(glossary), "（译前锁定 + 译后兜底）" if glossary else ""),
        "",
        "## 统计概览",
        "",
        "- 文件：%d 个（字符 %d，词 %d）" % (len(entries), total_chars, total_words),
        "- 批次：%d 个，失败 %d 个（失败批保留原文）" % (total_batches, failed_total),
        "- 输出目录内的本报告：%s" % DEFAULT_REPORT_NAME,
        "",
        "## 文件清单",
        "",
        "| 输入 | 输出 | 类型 | 字符 | 词 | 批次 | 状态 |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for entry in entries:
        if entry["failed"]:
            status = "部分失败（%d 批保留原文）" % len(entry["failed"])
        else:
            status = "成功"
        lines.append("| `%s` | `%s` | %s | %d | %d | %d | %s |" % (
            entry["input"], entry["output"], entry["kind"],
            entry["chars"], entry["words"], entry["batches"], status,
        ))
    if not entries:
        lines.append("| （无） | - | - | 0 | 0 | 0 | - |")
    lines.append("")

    lines.append("## 批次明细")
    lines.append("")
    lines.append("| 文件 | 批次总数 | 失败批 | 说明 |")
    lines.append("| --- | --- | --- | --- |")
    for entry in entries:
        note = entry.get("note") or ""
        failed_text = "、".join(str(n) for n in entry["failed"]) or "无"
        lines.append("| `%s` | %d | %s | %s |" % (
            entry["input"], entry["batches"], failed_text, note or "-",
        ))
    if not entries:
        lines.append("| （无） | 0 | 无 | - |")
    lines.append("")

    lines.append("## 失败批")
    lines.append("")
    if failed_total:
        lines.append("以下批次的模型回复没能解析成约定 JSON，该批译文保留原文（占位符已还原）：")
        lines.append("")
        for entry in entries:
            for number in entry["failed"]:
                lines.append("- `%s` 第 %d 批" % (entry["input"], number))
        lines.append("")
    else:
        lines.append("（无）")
        lines.append("")

    if skipped:
        lines.append("## 附录：跳过的文件")
        lines.append("")
        for rel, reason in skipped[:100]:
            lines.append("- `%s`：%s" % (rel, reason))
        if len(skipped) > 100:
            lines.append("- ……（其余 %d 个跳过项省略）" % (len(skipped) - 100))
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


def run_self_test():
    """离线自检：造样本、跑全部确定性逻辑、assert 校验，不联网、不需要 API key"""
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)

        # --- 占位符保护：抽取后还原必须与原文逐字符一致 ---
        tricky = (
            "# 标题\n\n"
            "调用 `run()` 函数，参数是 {name} 和 %s，模板写成 {{var}}。\n\n"
            "```python\nprint('hello {world}')\n```\n\n"
            "看这张图 ![示意图](https://example.com/a.png) 和链接 [文档](https://example.com/doc)。\n\n"
            "支持 <tag attr=\"x\">标签</tag> 与 <br/>。\n"
        )
        protector = TextProtector()
        protected = protector.protect(tricky)
        assert "\x00" in protected
        assert "```" not in protected
        assert "https://example.com/a.png" not in protected
        assert protector.restore(protected) == tricky, "占位符还原必须与原文逐字符一致"

        # 还原要能容忍占位符被加空格的轻微变形
        mangled = protected.replace("\x000\x00", "\x00 0 \x00", 1)
        assert protector.restore(mangled) == tricky

        # --- 术语表：译前锁定 + 译后兜底 ---
        glossary = {"Kubernetes": "K8s 容器平台", "API": "接口"}
        p2 = TextProtector()
        text2 = "使用 Kubernetes 管理 API"
        locked = apply_glossary(p2.protect(text2), p2, glossary)
        result = glossary_fixup(p2.restore(locked), glossary)
        assert "K8s 容器平台" in result
        assert "Kubernetes" not in result
        assert glossary_fixup("部署 Kubernetes 和别的 API", glossary) == "部署 K8s 容器平台 和别的 接口"

        glossary_path = root / "术语表.json"
        glossary_path.write_text(
            json.dumps({"terms": {"机器学习": "Machine Learning"}}, ensure_ascii=False),
            encoding="utf-8",
        )
        assert load_glossary(glossary_path) == {"机器学习": "Machine Learning"}
        flat_path = root / "flat.json"
        flat_path.write_text(json.dumps({"数据库": "Database"}), encoding="utf-8")
        assert load_glossary(flat_path) == {"数据库": "Database"}

        # --- 分批切分：每批不超限、合并后完整、不切断占位符 ---
        long_text = "\n\n".join(
            "这是第 %d 段，包含若干句子。句子用来测试按句断行！还有英文 sentence %d." % (i, i)
            for i in range(12)
        )
        batches = split_batches(long_text, 200)
        assert batches
        assert all(len(batch) <= 200 for batch in batches), "有批次超长"
        assert "".join(batches) == long_text, "批次合并后必须与原文一致"

        # 硬切长片段时不能把 \x00N\x00 占位符切成两半
        token_units = [
            "占位符" + (PLACEHOLDER_FMT % index) + "很长很长很长很长很长很长"
            for index in range(30)
        ]
        token_text = "".join(token_units)
        token_batches = split_batches(token_text, 40)
        assert "".join(token_batches) == token_text
        assert all(len(batch) <= 40 for batch in token_batches)
        for batch in token_batches:
            assert batch.count("\x00") % 2 == 0, "批次切断了占位符：%r" % batch[:60]

        # 占位符正好横跨切点时要回退切点，保证占位符完整
        hard_unit = "开" * 48 + (PLACEHOLDER_FMT % 0) + "长" * 200
        hard_batches = split_batches(hard_unit, 50)
        assert "".join(hard_batches) == hard_unit
        assert all(len(batch) <= 50 for batch in hard_batches)
        assert all(batch.count("\x00") % 2 == 0 for batch in hard_batches)

        assert split_batches("", 100) == []
        try:
            split_batches("x", 0)
            assert False, "批大小为 0 应当报错"
        except ValueError:
            pass

        # --- 统计 ---
        assert count_chars("你好 world") == len("你好 world")
        # 5 个中文字（你/好/第/二/句）+ 1 个英文单词（world）
        assert count_words("你好 world，第二句") == 6
        assert count_words("") == 0

        # --- JSON：只翻字符串 value，保留 key 与结构 ---
        source_json = {
            "name": "你好",
            "nested": {"desc": "世界", "count": 3, "ok": True, "none": None},
            "items": ["第一条", 2, "第二条"],
        }
        source_text = json.dumps(source_json, ensure_ascii=False, indent=2)

        def fake_cells(items):
            return ["T(%s)" % item for item in items], []

        translated_json, meta = translate_json_text(source_text, fake_cells)
        data = json.loads(translated_json)
        assert data["name"] == "T(你好)"
        assert data["nested"]["desc"] == "T(世界)"
        assert data["nested"]["count"] == 3 and data["nested"]["ok"] is True
        assert data["nested"]["none"] is None
        assert data["items"] == ["T(第一条)", 2, "T(第二条)"]
        assert meta["cells"] == 4
        assert list(data.keys()) == ["name", "nested", "items"], "key 与顺序必须保留"

        try:
            translate_json_text("{bad json", fake_cells)
            assert False, "坏 JSON 应当抛错"
        except ValueError:
            pass

        # --- CSV：表头不译、按列选择 ---
        source_csv = "名称,备注,价格\n苹果,好吃,3\n香蕉,便宜,2\n"

        def csv_cells(items):
            return ["X(%s)" % item for item in items], []

        out_all, meta_all = translate_csv_text(source_csv, csv_cells, None)
        rows = list(csv.reader(io.StringIO(out_all)))
        assert rows[0] == ["名称", "备注", "价格"], "表头必须保留"
        assert rows[1] == ["X(苹果)", "X(好吃)", "X(3)"]
        assert meta_all["cells"] == 6

        out_col, meta_col = translate_csv_text(source_csv, csv_cells, "名称")
        rows_col = list(csv.reader(io.StringIO(out_col)))
        assert rows_col[1] == ["X(苹果)", "好吃", "3"]
        assert meta_col["cells"] == 2

        out_idx, _meta_idx = translate_csv_text(source_csv, csv_cells, "2")
        rows_idx = list(csv.reader(io.StringIO(out_idx)))
        assert rows_idx[1] == ["苹果", "好吃", "X(3)"]

        try:
            translate_csv_text(source_csv, csv_cells, "不存在的列")
            assert False, "不存在的列应当报错"
        except ValueError:
            pass

        # --- 模型回复 JSON 解析与兜底 ---
        assert parse_translation_json('{"translation": "Hello"}') == "Hello"
        assert parse_translation_json('```json\n{"translation": "你好"}\n```') == "你好"
        assert parse_translation_json("这不是 JSON") is None
        assert parse_translation_json('{"translation": ""}') is None
        assert parse_translation_json('{"other": "x"}') is None
        multi = '{"translations": ["a", "b"]}'
        assert parse_translations_json(multi, 2) == ["a", "b"]
        assert parse_translations_json(multi, 3) is None
        assert parse_translations_json('{"translations": ["a", 1]}', 2) is None

        # --- 用假客户端走完整翻译流程：解析失败保留原文 ---
        stub_bad = _StubClient("模型说了一堆没用的解释")
        translator = Translator(
            "fake-model", "中文", "英语", glossary=None,
            batch_chars=1000, offline=False, client=stub_bad, verbose=False,
        )
        out_text, meta_plain = translator.translate_plain("你好，世界。\n")
        assert out_text == "你好，世界。\n", "解析失败时必须保留原文"
        assert meta_plain["failed"] == [1]

        stub_good = _StubClient('{"translation": "Hello, world."}')
        translator2 = Translator(
            "fake-model", "中文", "英语", glossary=None,
            batch_chars=1000, offline=False, client=stub_good, verbose=False,
        )
        out_text2, meta2 = translator2.translate_plain("你好，世界。\n")
        assert out_text2 == "Hello, world.", "译文应当是模型返回的内容"
        assert meta2["failed"] == []

        # 占位符在模型输出里必须被还原
        stub_keep = _StubClient('{"translation": "Call \\u00000\\u0000 now."}')
        translator3 = Translator(
            "fake-model", "中文", "英语", glossary=None,
            batch_chars=1000, offline=False, client=stub_keep, verbose=False,
        )
        out_text3, _meta3 = translator3.translate_plain("调用 `run()` 现在。\n")
        assert "`run()`" in out_text3, "占位符必须被还原成原文"

        # --- 离线模式：不联网也能产出译文与统计 ---
        offline = Translator(
            "fake-model", "中文", "英语", glossary={"你好": "Hello"},
            batch_chars=1000, offline=True, client=None, verbose=False,
        )
        out_off, meta_off = offline.translate_plain("你好，世界。\n")
        assert out_off == "Hello，世界。\n"
        assert meta_off["batches"] == 1 and meta_off["failed"] == []
        assert offline.call_count == 0

        # --- 文件级流程：文本 + JSON + CSV，保持相对目录结构 ---
        (root / "docs").mkdir()
        (root / "docs" / "note.md").write_text(
            "# 说明\n\n调用 `run()` 传入 {name}。\n", encoding="utf-8"
        )
        (root / "data.json").write_text(source_text, encoding="utf-8")
        (root / "table.csv").write_text(source_csv, encoding="utf-8")
        (root / "blob.bin").write_bytes(b"\x00\x01binary")

        items, skipped = collect_inputs([str(root)])
        rels = [item[1] for item in items]
        assert "docs/note.md" in rels and "data.json" in rels and "table.csv" in rels
        assert "blob.bin" not in rels
        assert any("二进制" in reason for _, reason in skipped)

        out_dir = root / "translated"
        translator4 = Translator(
            "fake-model", "中文", "英语", glossary=None,
            batch_chars=6000, offline=True, client=None, verbose=False,
        )
        translator4.columns = None
        entries = []
        for file_path, rel, kind, text in items:
            translated, meta = translate_file(text, rel, translator4)
            out_path = out_dir / rel
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(translated, encoding="utf-8")
            entries.append({
                "input": rel,
                "output": rel,
                "kind": {"json": "JSON", "csv": "CSV", "text": "文本"}.get(kind, kind),
                "chars": count_chars(text),
                "words": count_words(text),
                "batches": meta.get("batches", 0),
                "failed": meta.get("failed", []),
                "note": meta.get("note", ""),
            })
        assert (out_dir / "docs" / "note.md").exists(), "必须保持相对目录结构"
        assert (out_dir / "data.json").exists()

        report = build_report(
            entries, "中文", "英语", glossary, True, "claude-opus-5",
            skipped=skipped, generated_at="2026-01-01 00:00:00",
        )
        assert report.startswith("# 翻译报告")
        for heading in ("## 统计概览", "## 文件清单", "## 批次明细", "## 失败批"):
            assert heading in report, "报告缺少：%s" % heading
        assert "docs/note.md" in report and "data.json" in report
        assert "术语表：2 条" in report
        assert "离线（--offline" in report

        report_fail = build_report(
            [{"input": "a.md", "output": "a.md", "kind": "文本", "chars": 10,
              "words": 5, "batches": 2, "failed": [2], "note": ""}],
            "中文", "英语", {}, False, "qwen3.8-max",
            generated_at="2026-01-01 00:00:00",
        )
        assert "部分失败（1 批保留原文）" in report_fail
        assert "`a.md` 第 2 批" in report_fail
        assert "模型翻译（qwen3.8-max）" in report_fail

        report_path = root / "translation-report.md"
        report_path.write_text(report, encoding="utf-8")
        assert report_path.read_text(encoding="utf-8").startswith("# 翻译报告")

        # --- call_model 逐级降级（离线假客户端）---
        fake = _FakeClient()
        result = call_model(fake, "fake-model", "system", [{"role": "user", "content": "hi"}])
        assert result["ok"] is True
        assert fake.state["calls"] == 2

    print("SELF-TEST OK")


# ---------- CLI ----------

def build_parser():
    parser = argparse.ArgumentParser(
        description="多语言翻译助手：占位符保护 + 分批翻译 + 术语表，输出译文文件与中文 Markdown 翻译报告",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            "  py -3 agent.py ./docs --to 英语 --from 中文 --out-dir ./translated\n"
            "  py -3 agent.py data.json --to 日语 --glossary 术语表.json\n"
            "  py -3 agent.py table.csv --to 英语 --columns 名称,备注\n"
            "  py -3 agent.py --text \"你好，世界\" --to 英语 --offline\n"
        ),
    )
    parser.add_argument("paths", nargs="*", help="要翻译的文件或目录，可以给多个")
    parser.add_argument(
        "--text",
        default=None,
        metavar="文本",
        help="直接翻译一段文本（不用给文件路径），结果打印到终端并写入报告",
    )
    parser.add_argument(
        "--to",
        default=None,
        metavar="语言",
        help="目标语言（必填），例如 英语 / 日语 / 中文 / 法语",
    )
    parser.add_argument(
        "--from",
        dest="from_lang",
        default="自动识别",
        metavar="语言",
        help="源语言（默认 自动识别）",
    )
    parser.add_argument(
        "--out-dir",
        default=DEFAULT_OUT_DIR,
        help="译文输出目录，保持相对目录结构（默认 %s）" % DEFAULT_OUT_DIR,
    )
    parser.add_argument(
        "--glossary",
        default=None,
        metavar="JSON",
        help="术语表文件：{\"源词\": \"译法\"}，译前锁定、译后兜底",
    )
    parser.add_argument(
        "--columns",
        default=None,
        metavar="列",
        help="CSV 只翻译这些列（列名或 0 起序号，逗号分隔；默认全部数据列，表头不译）",
    )
    parser.add_argument(
        "--batch-chars",
        type=int,
        default=DEFAULT_BATCH_CHARS,
        help="每批最多多少字符（默认 %d）" % DEFAULT_BATCH_CHARS,
    )
    parser.add_argument(
        "--offline",
        action="store_true",
        help="纯确定性模式：不调用模型、不需要 API key，译文保留原文（只做术语表替换）",
    )
    parser.add_argument(
        "--model",
        default=DEFAULT_MODEL,
        help="翻译使用的模型（默认 %s）" % DEFAULT_MODEL,
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

    if args.to is None:
        parser.error("请用 --to 指定目标语言，例如 --to 英语")
    if args.batch_chars < 200:
        parser.error("--batch-chars 不能小于 200")
    if args.text is None and not args.paths:
        parser.error("请提供要翻译的文件/目录，或用 --text 直接给一段文本")
    if args.text is not None and args.paths:
        parser.error("--text 与文件路径二选一，不要同时使用")

    if not args.offline:
        if not os.environ.get("ANTHROPIC_API_KEY") and not os.environ.get("ANTHROPIC_AUTH_TOKEN"):
            print(
                "提示：没有检测到 ANTHROPIC_API_KEY 或 ANTHROPIC_AUTH_TOKEN，无法调用模型翻译。\n"
                "请先设置（macOS / Linux）：export ANTHROPIC_API_KEY=你的key\n"
                "Windows PowerShell：$env:ANTHROPIC_API_KEY=\"你的key\"\n"
                "也可以加 --offline 做纯确定性试跑（译文保留原文，只生成报告）。",
                file=sys.stderr,
            )
            sys.exit(1)

    glossary = {}
    if args.glossary:
        try:
            glossary = load_glossary(args.glossary)
        except (OSError, ValueError) as exc:
            print("错误：术语表读取失败：%s" % exc, file=sys.stderr)
            sys.exit(1)

    try:
        if args.text is not None:
            items = [(None, "输入文本", "text", args.text)]
            skipped = []
        else:
            items, skipped = collect_inputs(args.paths)
    except FileNotFoundError as exc:
        print("错误：%s" % exc, file=sys.stderr)
        sys.exit(1)

    if not items:
        print("错误：没有可翻译的文本文件（可能都是二进制，或路径下没有文件）。", file=sys.stderr)
        sys.exit(1)

    translator = Translator(
        args.model,
        args.from_lang,
        args.to,
        glossary=glossary,
        batch_chars=args.batch_chars,
        offline=args.offline,
        verbose=True,
    )
    translator.columns = args.columns

    out_dir = Path(args.out_dir).expanduser()
    entries = []
    try:
        for _file_path, rel, kind, text in items:
            print("[翻译] %s（%s）" % (rel, {"json": "JSON", "csv": "CSV", "text": "文本"}.get(kind, kind)))
            translated, meta = translate_file(text, rel, translator)
            if args.text is not None:
                out_rel = "translated.txt"
                print("")
                print(translated)
            else:
                out_rel = rel
                out_path = out_dir / rel
                out_path.parent.mkdir(parents=True, exist_ok=True)
                out_path.write_text(translated, encoding="utf-8")
            entries.append({
                "input": rel,
                "output": out_rel,
                "kind": {"json": "JSON", "csv": "CSV", "text": "文本"}.get(kind, kind),
                "chars": count_chars(text),
                "words": count_words(text),
                "batches": meta.get("batches", 0),
                "failed": meta.get("failed", []),
                "note": meta.get("note", ""),
            })
            if args.text is not None:
                (out_dir / out_rel).parent.mkdir(parents=True, exist_ok=True)
                (out_dir / out_rel).write_text(translated, encoding="utf-8")
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
    except ValueError as exc:
        print("错误：%s" % exc, file=sys.stderr)
        sys.exit(1)

    report = build_report(
        entries, args.from_lang, args.to, glossary, args.offline, args.model,
        skipped=skipped,
    )
    report_path = out_dir / DEFAULT_REPORT_NAME
    try:
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(report, encoding="utf-8")
    except OSError as exc:
        print("错误：报告写入失败：%s" % exc, file=sys.stderr)
        sys.exit(1)

    total_chars = sum(entry["chars"] for entry in entries)
    total_words = sum(entry["words"] for entry in entries)
    total_batches = sum(entry["batches"] for entry in entries)
    failed_total = sum(len(entry["failed"]) for entry in entries)
    print("[完成] 译文目录：%s" % out_dir)
    print("[完成] 翻译报告：%s" % report_path)
    print("[完成] 文件 %d 个，字符 %d，词 %d，批次 %d，失败批次 %d"
          % (len(entries), total_chars, total_words, total_batches, failed_total))
    if args.offline:
        print("[提示] --offline 模式未调用模型，译文保留原文（仅术语表替换）。")


if __name__ == "__main__":
    main()
