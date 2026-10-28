#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
邮件助手 —— 解析 .eml / .mbox / .txt 邮件，做摘要、优先级判断、待办提取与回复起草

先用标准库 email 包做完全确定性的解析（发件人、收件人、抄送、主题、日期、Message-ID、
引用线程头、附件清单、HTML 转纯文本、引用剥离、优先级启发式、日期/金额/电话/链接/待办
提取），再按需调用模型生成摘要、行动项清单和回复草稿，最后输出中文 Markdown 报告。
除 anthropic SDK 外只用标准库，不依赖向量库。

用法：
    python3 agent.py <eml|mbox|txt 文件或目录> [选项]

示例：
    python3 agent.py 收件箱.mbox --draft --tone 正式 --output 邮件报告.md
    python3 agent.py ./邮件目录 --offline          # 纯确定性模式，不需要 API key
    python3 agent.py 一封邮件.eml --draft --tone 简洁
    python3 agent.py 邮件.txt --model qwen3.8-max  # 走第三方兼容网关时指定模型名

环境要求：
    1. 需要模型的路径（不带 --offline）要设置 API key：
           export ANTHROPIC_API_KEY=你的key        # macOS / Linux
           $env:ANTHROPIC_API_KEY="你的key"         # Windows PowerShell
       也支持 ANTHROPIC_AUTH_TOKEN。--offline 与 --self-test 不需要 key、不联网。
    2. 安装 SDK：
           pip3 install -r requirements.txt
    3. 走第三方兼容网关时（如阿里云等），用 --model 指定网关认得的模型名，
       例如 --model qwen3.8-max；代码里的 call_model() 会自动逐级降级参数。
"""

import argparse
import email
import email.header
import email.parser
import email.utils
import html.parser
import json
import mailbox
import os
import re
import sys
import tempfile
from datetime import datetime
from pathlib import Path

import anthropic

# 默认模型（可用 --model 参数或 EMAIL_ASSISTANT_MODEL 环境变量更换）
DEFAULT_MODEL = os.environ.get("EMAIL_ASSISTANT_MODEL", "claude-opus-5")
DEFAULT_OUTPUT = "email-report.md"
MAX_INPUT_FILES = 500        # 单次最多处理多少个输入文件
MAX_BODY_CHARS = 8000        # 报告里每封邮件最多展示多少字符的正文
MAX_MODEL_CHARS = 6000       # 发给模型的正文上限
MAX_ITEMS = 20               # 待办/日期/金额等条目最多列多少条

TONES = ("正式", "友好", "简洁")
LEVELS = ("高", "中", "低")

SUPPORTED_SUFFIXES = (".eml", ".mbox", ".txt")

# ---------- 词表与正则 ----------

URGENT_WORDS = (
    "urgent", "asap", "紧急", "尽快", "今天内", "今日内", "截止", "deadline",
    "立即", "马上", "务必", "请尽快", "重要", "critical",
)

DEADLINE_RES = (
    re.compile(r"\d{4}\s*[-/年]\s*\d{1,2}\s*[-/月]\s*\d{1,2}\s*日?"),
    re.compile(r"\d{1,2}\s*月\s*\d{1,2}\s*日"),
    re.compile(r"周[一二三四五六日天](?:之前|前|内|以前)"),
    re.compile(r"星期[一二三四五六日天](?:之前|前|内|以前)"),
    re.compile(r"(?:今天|明天|后天|今晚|本周|下周|月底|本周五|本月底)(?:之前|前|内)?"),
    re.compile(r"(?<!\d)\d{1,2}:\d{2}(?!\d)"),
)

AMOUNT_RE = re.compile(
    r"(?:[$￥¥]\s?\d[\d,]*(?:\.\d+)?"
    r"|(?:USD|EUR|RMB|CNY)\s?\d[\d,]*(?:\.\d+)?"
    r"|\d[\d,]*(?:\.\d+)?\s?(?:元|万元|美金|美元|块))"
)
PHONE_RES = (
    re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"),
    re.compile(r"(?<!\d)0\d{2,3}-\d{7,8}(?!\d)"),
    re.compile(r"\+\d{1,3}[- ]?\d{6,11}"),
)
LINK_RE = re.compile(r"https?://[^\s)>\]\"'，。；;]+")
TODO_START_RE = re.compile(r"^(请|麻烦|需要|记得|下周|务必|请于|待|跟进|请你|各位请)")
REPLY_REQUEST_RE = re.compile(r"请.{0,4}(回复|确认|答复)|麻烦.{0,4}(回复|确认|答复)|回执")

QUOTE_PREFIX_RE = re.compile(r"^\s*>")
STOP_RES = (
    re.compile(r"(?i)^\s*On .{4,120}\bwrote:\s*$"),
    re.compile(r"^\s*在.{0,80}(?:写道|寫道)\s*[:：]?\s*$"),
    re.compile(r"(?i)^\s*-{2,}\s*original\s+message\s*-{2,}\s*$"),
    re.compile(r"^\s*-{2,}\s*(?:转发邮件|原始邮件|转发|附言)\s*-{2,}\s*$"),
)

SENTENCE_SPLIT_RE = re.compile(r"[。！？!?，、；;\n]+")

TAG_RE = re.compile(r"<[^>]+>")


# ---------- HTML 转纯文本 ----------

class _HTMLTextExtractor(html.parser.HTMLParser):
    """自写的 HTMLParser 子类：跳过 script/style，块级标签转成换行，其余取文本"""

    SKIP_TAGS = {"script", "style", "head", "title", "noscript"}
    BLOCK_TAGS = {
        "p", "div", "br", "tr", "li", "h1", "h2", "h3", "h4", "h5", "h6",
        "table", "ul", "ol", "section", "article", "blockquote", "hr",
    }

    def __init__(self):
        html.parser.HTMLParser.__init__(self, convert_charrefs=True)
        self.parts = []
        self._skip_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP_TAGS:
            self._skip_depth += 1
        elif tag in self.BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in self.SKIP_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1)
        elif tag in self.BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data):
        if self._skip_depth == 0:
            self.parts.append(data)


def html_to_text(html_source):
    """HTML 正文转纯文本：HTMLParser 提取 + 正则去残留标签 + 压缩空行"""
    if not html_source:
        return ""
    parser = _HTMLTextExtractor()
    try:
        parser.feed(html_source)
        parser.close()
    except Exception:
        # 极端畸形 HTML 交给正则兜底
        return normalize_text(TAG_RE.sub("", html_source))
    return normalize_text(TAG_RE.sub("", "".join(parser.parts)))


def normalize_text(text):
    """统一换行、去掉行尾空白、把 3 个以上连续空行压成 1 个空行"""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = [line.rstrip() for line in text.split("\n")]
    out = []
    blank = False
    for line in lines:
        if not line.strip():
            if blank:
                continue
            blank = True
            out.append("")
        else:
            blank = False
            out.append(line)
    while out and not out[-1].strip():
        out.pop()
    return "\n".join(out)


# ---------- 头部解码 ----------

def decode_mime_header(value):
    """把 RFC 2047 编码的邮件头解码成可读文本（多字符集逐段兜底）"""
    if not value:
        return ""
    if not isinstance(value, str):
        value = str(value)
    try:
        parts = email.header.decode_header(value)
    except Exception:
        return value
    out = []
    for chunk, charset in parts:
        if isinstance(chunk, bytes):
            decoded = None
            for enc in (charset, "utf-8", "gb18030", "latin-1"):
                if not enc:
                    continue
                try:
                    decoded = chunk.decode(enc, errors="replace")
                    break
                except (LookupError, UnicodeDecodeError):
                    continue
            if decoded is None:
                decoded = chunk.decode("utf-8", errors="replace")
            out.append(decoded)
        else:
            out.append(chunk)
    return "".join(out)


def format_addresses(msg, header_name):
    """取某个地址头（From/To/Cc），返回 [(显示名, 邮箱), ...] 与格式化文本"""
    raw = msg.get_all(header_name, []) or []
    pairs = []
    for name, addr in email.utils.getaddresses(raw):
        name = decode_mime_header(name).strip()
        addr = (addr or "").strip()
        if name or addr:
            pairs.append((name, addr))
    return pairs


def render_pairs(pairs):
    """把 [(显示名, 邮箱)] 渲染成一行可读文本"""
    out = []
    for name, addr in pairs:
        if name and addr:
            out.append("%s <%s>" % (name, addr))
        elif addr:
            out.append(addr)
        elif name:
            out.append(name)
    return "、".join(out) if out else "（无）"


# ---------- 邮件数据结构与解析 ----------

class EmailCard:
    """一封邮件的解析结果：元信息、正文、附件、以及后续派生的分析结果"""

    def __init__(self, source):
        self.source = source           # 来源文件（mbox 会带 #序号）
        self.subject = ""
        self.from_pairs = []
        self.to_pairs = []
        self.cc_pairs = []
        self.date_raw = ""
        self.date_iso = ""
        self.message_id = ""
        self.in_reply_to = ""
        self.references = []
        self.body_plain = ""
        self.body_html_text = ""
        self.body_text = ""
        self.attachments = []
        # 派生结果
        self.clean_body = ""
        self.quote_removed = 0
        self.priority = {"level": "低", "score": 0, "reasons": []}
        self.info = {}
        self.analysis = None


def parse_message(msg, source):
    """把一个 email.message.Message 解析成 EmailCard（只取 text/plain 与 text/html）"""
    card = EmailCard(source)
    card.subject = decode_mime_header(msg.get("Subject")).strip() or "（无主题）"
    card.from_pairs = format_addresses(msg, "From")
    card.to_pairs = format_addresses(msg, "To")
    card.cc_pairs = format_addresses(msg, "Cc")
    card.date_raw = (msg.get("Date") or "").strip()
    if card.date_raw:
        try:
            card.date_iso = email.utils.parsedate_to_datetime(card.date_raw).isoformat()
        except (TypeError, ValueError, IndexError):
            card.date_iso = ""
    card.message_id = (msg.get("Message-ID") or "").strip()
    card.in_reply_to = (msg.get("In-Reply-To") or "").strip()
    card.references = re.findall(r"<[^>]+>", msg.get("References") or "")

    plain_parts = []
    html_parts = []
    for part in msg.walk():
        if part.is_multipart():
            continue
        ctype = part.get_content_type()
        filename = decode_mime_header(part.get_filename() or "").strip()
        disposition = (part.get("Content-Disposition") or "").lower()
        if filename or disposition.startswith("attachment"):
            payload = part.get_payload(decode=True) or b""
            card.attachments.append({
                "name": filename or "（未命名附件）",
                "type": ctype,
                "size": len(payload),
            })
            continue
        if ctype not in ("text/plain", "text/html"):
            continue
        text = decode_part_text(part)
        if ctype == "text/plain":
            plain_parts.append(text)
        else:
            html_parts.append(text)

    card.body_plain = normalize_text("\n".join(plain_parts))
    card.body_html_text = "\n".join(html_to_text(h) for h in html_parts)
    card.body_text = card.body_plain if card.body_plain.strip() else card.body_html_text
    return card


def _has_surrogates(text):
    """判断字符串里是否有 surrogateescape 留下的代理字符"""
    return any(0xDC80 <= ord(ch) <= 0xDCFF for ch in text)


def _decode_bytes(data, part):
    """按邮件头声明的字符集解码字节，逐级兜底"""
    charset = part.get_content_charset() or "utf-8"
    try:
        return data.decode(charset, errors="replace")
    except (LookupError, UnicodeDecodeError):
        return data.decode("utf-8", errors="replace")


def _sanitize(text):
    """清掉代理字符等脏码点，避免写文件时报错"""
    return text.encode("utf-8", errors="replace").decode("utf-8")


def decode_part_text(part):
    """解码一个 MIME 文本分段，字符集逐级兜底。

    不同来源的邮件解析后形态不一样，这里选"损失最小"的一份：
    - mailbox / BytesParser：get_payload(decode=True) 得到原始字节，按字符集解码最准；
    - 字符串解析（email.parser.Parser）：get_payload(decode=True) 会把非 ASCII
      内容降级成 unicode 转义序列，这时原始字符串才是对的。
    判据是哪一份的替换字符（U+FFFD）更少，平手则取原始字符串。
    """
    cte = (part.get("Content-Transfer-Encoding") or "").strip().lower()
    if cte in ("base64", "quoted-printable"):
        return _decode_bytes(part.get_payload(decode=True) or b"", part)
    raw = part.get_payload(decode=False)
    payload = part.get_payload(decode=True)
    if not isinstance(raw, str):
        return _decode_bytes(payload or b"", part)
    if isinstance(payload, bytes) and payload and not _has_surrogates(raw):
        text = _decode_bytes(payload, part)
        if text.count("\ufffd") < raw.count("\ufffd"):
            return text
    elif isinstance(payload, bytes) and payload:
        text = _decode_bytes(raw.encode("ascii", "surrogateescape"), part)
        if text.count("\ufffd") < raw.count("\ufffd"):
            return text
    return _sanitize(raw)


def parse_eml_file(path):
    """解析单封 .eml 文件，返回 [EmailCard]"""
    data = Path(path).read_bytes()
    msg = email.parser.BytesParser().parsebytes(data)
    return [parse_message(msg, Path(path).name)]


def parse_mbox_file(path):
    """解析 .mbox 邮箱文件，返回 [EmailCard]（每封一条）"""
    cards = []
    box = mailbox.mbox(str(path))
    try:
        for index, msg in enumerate(box, 1):
            cards.append(parse_message(msg, "%s#%d" % (Path(path).name, index)))
    finally:
        box.close()
    return cards


def parse_txt_file(path):
    """解析纯文本邮件：有邮件头就按邮件头解析，否则整篇当正文，主题取文件名"""
    text = Path(path).read_text(encoding="utf-8", errors="replace").replace("\r\n", "\n")
    head, sep, _body = text.partition("\n\n")
    if sep and re.search(r"(?im)^(subject|from|to)\s*[:：]", head):
        msg = email.parser.Parser().parsestr(text)
        return [parse_message(msg, Path(path).name)]
    card = EmailCard(Path(path).name)
    card.subject = Path(path).stem or "（无主题）"
    card.body_plain = normalize_text(text)
    card.body_text = card.body_plain
    return [card]


def parse_file(path):
    """按扩展名分发解析器，返回 [EmailCard]"""
    suffix = Path(path).suffix.lower()
    if suffix == ".eml":
        return parse_eml_file(path)
    if suffix == ".mbox":
        return parse_mbox_file(path)
    if suffix == ".txt":
        return parse_txt_file(path)
    raise ValueError("不支持的文件类型：%s" % path)


def collect_inputs(paths):
    """收集输入文件：文件直接收，目录递归找 .eml/.mbox/.txt；返回 (文件列表, 提示列表)"""
    files = []
    notes = []
    seen = set()
    for raw in paths:
        path = Path(raw).expanduser()
        if not path.exists():
            raise FileNotFoundError("路径不存在：%s" % path)
        candidates = []
        if path.is_dir():
            for item in sorted(path.rglob("*")):
                if item.is_file() and item.suffix.lower() in SUPPORTED_SUFFIXES:
                    candidates.append(item)
        else:
            if path.suffix.lower() in SUPPORTED_SUFFIXES:
                candidates.append(path)
            else:
                notes.append("跳过不支持的文件类型：%s" % path.name)
        for item in candidates:
            key = str(item.resolve())
            if key in seen:
                continue
            seen.add(key)
            files.append(item)
    if len(files) > MAX_INPUT_FILES:
        notes.append("文件数超过 %d，只处理前 %d 个" % (MAX_INPUT_FILES, MAX_INPUT_FILES))
        files = files[:MAX_INPUT_FILES]
    return files, notes


# ---------- 确定性分析 ----------

def strip_quotes(text):
    """去掉 "> " 引用行与转发块（On ... wrote: / -----Original Message----- 之后），
    返回 (清理后的正文, 被剥离的行数)"""
    kept = []
    removed = 0
    stopped = False
    for line in (text or "").split("\n"):
        if stopped:
            removed += 1
            continue
        if any(pattern.search(line) for pattern in STOP_RES):
            removed += 1
            stopped = True
            continue
        if QUOTE_PREFIX_RE.match(line):
            removed += 1
            continue
        kept.append(line)
    while kept and not kept[-1].strip():
        kept.pop()
    return "\n".join(kept), removed


def mentions_recipient(card):
    """正文里是否点名了收件人（按显示名或邮箱前缀匹配）"""
    body = card.clean_body or ""
    body_lower = body.lower()
    for name, addr in card.to_pairs + card.cc_pairs:
        if len(name) >= 2 and name in body:
            return True
        local = addr.split("@")[0].strip().lower()
        if len(local) >= 3 and local in body_lower:
            return True
    return False


def compute_priority(card):
    """优先级启发式：紧急词、截止时间、问号数量、是否点名收件人、是否要求回复"""
    text = "%s\n%s" % (card.subject or "", card.clean_body or "")
    lower = text.lower()
    reasons = []
    score = 0

    hits = [word for word in URGENT_WORDS if word.lower() in lower]
    if hits:
        score += 2 * min(len(hits), 3)
        reasons.append("出现紧急词：%s" % "、".join(hits[:5]))

    deadlines = []
    for pattern in DEADLINE_RES:
        for match in pattern.finditer(text):
            deadlines.append(match.group(0).strip())
    if deadlines:
        score += 2
        reasons.append("提到截止/时间点：%s" % "、".join(list(dict.fromkeys(deadlines))[:5]))

    questions = text.count("?") + text.count("？")
    if questions >= 2:
        score += 1
        reasons.append("正文含 %d 个问号，疑似需要回复" % questions)

    if mentions_recipient(card):
        score += 1
        reasons.append("正文点名了收件人")

    if REPLY_REQUEST_RE.search(text):
        score += 1
        reasons.append("明确要求回复或确认")

    if score >= 5:
        level = "高"
    elif score >= 2:
        level = "中"
    else:
        level = "低"
    return {
        "level": level,
        "score": score,
        "reasons": reasons or ["没有命中紧急词、截止时间或回复请求等特征"],
    }


def split_sentences(text):
    """按中英文标点和换行切成句子片段"""
    return [piece.strip() for piece in SENTENCE_SPLIT_RE.split(text or "") if piece.strip()]


def extract_info(text):
    """从正文提取：日期时间、金额、电话、链接、待办句"""
    text = text or ""
    dates = []
    for pattern in DEADLINE_RES:
        for match in pattern.finditer(text):
            dates.append(match.group(0).strip())
    amounts = [m.group(0).strip() for m in AMOUNT_RE.finditer(text)]
    phones = []
    for pattern in PHONE_RES:
        for match in pattern.finditer(text):
            phones.append(match.group(0).strip())
    links = [m.group(0) for m in LINK_RE.finditer(text)]
    todos = []
    for sentence in split_sentences(text):
        if TODO_START_RE.match(sentence) and len(sentence) >= 4:
            todos.append(sentence[:120])
    return {
        "dates": list(dict.fromkeys(dates))[:MAX_ITEMS],
        "amounts": list(dict.fromkeys(amounts))[:MAX_ITEMS],
        "phones": list(dict.fromkeys(phones))[:MAX_ITEMS],
        "links": list(dict.fromkeys(links))[:MAX_ITEMS],
        "todos": list(dict.fromkeys(todos))[:MAX_ITEMS],
    }


def analyze_card(card):
    """对单封邮件跑完整确定性分析，结果写回 card"""
    card.clean_body, card.quote_removed = strip_quotes(card.body_text)
    card.priority = compute_priority(card)
    card.info = extract_info(card.clean_body)
    return card


def aggregate(cards):
    """批量汇总：总邮件数、优先级分布、待办总数、附件总数"""
    stats = {"total": len(cards), "high": 0, "mid": 0, "low": 0, "todos": 0, "attachments": 0}
    for card in cards:
        level = card.priority["level"]
        if level == "高":
            stats["high"] += 1
        elif level == "中":
            stats["mid"] += 1
        else:
            stats["low"] += 1
        stats["todos"] += len(card.info.get("todos", []))
        stats["attachments"] += len(card.attachments)
    return stats


def deterministic_summary(card):
    """离线摘要：取清理后正文的前 3 句，超长截断"""
    sentences = split_sentences(card.clean_body)[:3]
    text = "；".join(sentences) if sentences else "（正文为空，无法生成摘要）"
    if len(text) > 160:
        text = text[:160] + "……"
    return text


def deterministic_draft(card, tone):
    """离线草稿：按语气套模板，逐条列出对方提出的问题"""
    opening = {
        "正式": "您好：",
        "友好": "你好！",
        "简洁": "你好：",
    }[tone]
    closing = {
        "正式": "如有其他安排，请随时告知。顺祝商祺。",
        "友好": "有问题随时找我，谢谢！",
        "简洁": "谢谢。",
    }[tone]
    questions = [s for s in split_sentences(card.clean_body) if s.endswith(("？", "?"))]
    lines = [opening, "", "已收到您关于「%s」的邮件，逐条回复如下：" % card.subject, ""]
    if questions:
        for index, question in enumerate(questions, 1):
            lines.append("%d. %s —— （此处填写回复）" % (index, question))
    else:
        lines.append("（未识别到具体问题，请在此补充回复要点。）")
    lines += ["", closing]
    return "\n".join(lines)


def deterministic_analysis(card, tone, draft):
    """离线/降级用的确定性分析结果"""
    todos = card.info.get("todos", [])
    dates = card.info.get("dates", [])
    return {
        "summary": deterministic_summary(card),
        "actions": [
            {"task": task, "due": dates[0] if dates else "", "owner": ""}
            for task in todos[:MAX_ITEMS]
        ],
        "reply": deterministic_draft(card, tone) if draft else "",
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


ANALYSIS_SYSTEM_PROMPT = """你是一名高效的邮件助理。用户会给出一封邮件的元信息和正文（已剥离引用）。
请只输出一个 JSON 对象，不要输出任何解释文字，不要使用 Markdown 代码围栏，字段如下：
{
  "summary": "不超过 3 句话的中文摘要",
  "actions": [{"task": "待办事项", "due": "截止时间，没有就留空", "owner": "负责人，未知就留空"}],
  "reply": "给对方的中文回复草稿，逐条回应对方提出的问题"
}
要求：summary 只保留事实；actions 里的 task 要可执行；reply 不要编造不存在的事实。"""

TONE_HINTS = {
    "正式": "回复语气正式、礼貌、书面化，使用「您」。",
    "友好": "回复语气友好、自然、有温度，像同事之间的沟通。",
    "简洁": "回复语气简洁直接，短句，去掉客套话。",
}


def build_analysis_prompt(card, tone, draft):
    """拼一封邮件的模型提示词"""
    body = (card.clean_body or "").strip()
    if len(body) > MAX_MODEL_CHARS:
        body = body[:MAX_MODEL_CHARS] + "\n……（正文过长已截断）"
    info = card.info
    lines = [
        "【元信息】",
        "主题：%s" % card.subject,
        "发件人：%s" % render_pairs(card.from_pairs),
        "收件人：%s" % render_pairs(card.to_pairs),
        "日期：%s" % (card.date_raw or "（未知）"),
        "",
        "【正文】",
        body or "（正文为空）",
    ]
    if info.get("todos"):
        lines += ["", "【确定性提取到的待办】"] + ["- %s" % t for t in info["todos"]]
    lines += ["", "【要求】"]
    if draft:
        lines.append("请按「%s」的语气起草回复。%s" % (tone, TONE_HINTS[tone]))
    else:
        lines.append("本次不需要写回复草稿，reply 字段留空字符串即可。")
    lines.append("只输出 JSON 对象。")
    return "\n".join(lines)


def response_text(response):
    """提取模型响应里的文本块"""
    parts = []
    for block in getattr(response, "content", []) or []:
        if getattr(block, "type", "") == "text":
            parts.append(getattr(block, "text", ""))
    return "\n".join(parts).strip()


def parse_model_json(text):
    """尽力把模型回复解析成分析 JSON 对象；解析不出来返回 None（调用方负责兜底）"""
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

    summary = str(data.get("summary") or "").strip()
    reply = str(data.get("reply") or "").strip()
    actions = []
    raw_actions = data.get("actions")
    if isinstance(raw_actions, list):
        for item in raw_actions:
            if not isinstance(item, dict):
                continue
            task = str(item.get("task") or "").strip()
            if not task:
                continue
            actions.append({
                "task": task,
                "due": str(item.get("due") or "").strip(),
                "owner": str(item.get("owner") or "").strip(),
            })
    return {"summary": summary, "actions": actions, "reply": reply}


def analyze_with_model(cards, model, tone, draft, verbose=True):
    """逐封调用模型做分析，返回 (analyses, raw_outputs)；解析失败的原文进 raw_outputs"""
    client = anthropic.Anthropic()
    analyses = []
    raw_outputs = []
    for card in cards:
        if verbose:
            print("[模型] 正在分析：%s" % card.subject)
        prompt = build_analysis_prompt(card, tone, draft)
        response = call_model(
            client,
            model,
            ANALYSIS_SYSTEM_PROMPT,
            [{"role": "user", "content": prompt}],
            max_tokens=4000,
        )
        text = response_text(response)
        parsed = parse_model_json(text)
        if parsed is None:
            raw_outputs.append((card.subject, text or "（模型没有返回文本）"))
            analyses.append(None)
        else:
            analyses.append(parsed)
    return analyses, raw_outputs


# ---------- 报告渲染 ----------

def render_attachments(card):
    """渲染附件清单"""
    if not card.attachments:
        return "无"
    rows = ["| 文件名 | 类型 | 大小 |", "| --- | --- | --- |"]
    for item in card.attachments:
        rows.append("| %s | %s | %s |" % (item["name"], item["type"], human_size(item["size"])))
    return "\n".join(rows)


def human_size(size):
    """字节数转可读大小"""
    if size < 1024:
        return "%d B" % size
    if size < 1024 * 1024:
        return "%.1f KB" % (size / 1024.0)
    return "%.1f MB" % (size / (1024.0 * 1024.0))


def render_list(items, empty="（无）"):
    """把列表渲染成编号列表"""
    if not items:
        return empty
    return "\n".join("%d. %s" % (index, item) for index, item in enumerate(items, 1))


def build_report(cards, analyses, raw_outputs, stats, model, offline, tone, draft, generated_at=None):
    """生成中文 Markdown 报告"""
    generated_at = generated_at or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    mode = "离线确定性模式（--offline）" if offline else "模型分析（%s）" % model
    lines = [
        "# 邮件处理报告",
        "",
        "- 生成时间：%s" % generated_at,
        "- 工作模式：%s" % mode,
        "- 处理邮件数：%d（高 %d / 中 %d / 低 %d）" % (
            stats["total"], stats["high"], stats["mid"], stats["low"]),
        "- 待办总数：%d" % stats["todos"],
    ]
    if draft:
        lines.append("- 已按「%s」语气起草回复" % tone)
    lines.append("")

    for index, card in enumerate(cards, 1):
        analysis = analyses[index - 1] if index - 1 < len(analyses) else None
        if analysis is None:
            analysis = deterministic_analysis(card, tone, draft)
        lines += [
            "## 邮件 %d：%s" % (index, card.subject),
            "",
            "### 元信息",
            "",
            "| 字段 | 内容 |",
            "| --- | --- |",
            "| 来源文件 | %s |" % card.source,
            "| 发件人 | %s |" % render_pairs(card.from_pairs),
            "| 收件人 | %s |" % render_pairs(card.to_pairs),
            "| 抄送 | %s |" % render_pairs(card.cc_pairs),
            "| 日期 | %s |" % ((card.date_raw + "（%s）" % card.date_iso) if card.date_iso else (card.date_raw or "（未知）")),
            "| Message-ID | %s |" % (card.message_id or "（无）"),
            "| 引用线程 | In-Reply-To：%s；References：%d 个 |" % (
                card.in_reply_to or "（无）", len(card.references)),
            "",
            "### 附件清单",
            "",
            render_attachments(card),
            "",
            "### 摘要",
            "",
            analysis.get("summary") or "（无摘要）",
            "",
            "### 优先级",
            "",
            "- 级别：**%s**（评分 %d）" % (card.priority["level"], card.priority["score"]),
            "- 理由：%s" % "；".join(card.priority["reasons"]),
            "",
            "### 待办与关键信息",
            "",
            "待办（%d 项）：" % len(card.info.get("todos", [])),
            render_list(card.info.get("todos", [])),
            "",
            "- 日期/时间：%s" % ("、".join(card.info.get("dates", [])) or "（无）"),
            "- 金额：%s" % ("、".join(card.info.get("amounts", [])) or "（无）"),
            "- 电话：%s" % ("、".join(card.info.get("phones", [])) or "（无）"),
            "- 链接：%s" % ("、".join(card.info.get("links", [])) or "（无）"),
            "",
        ]
        if analysis.get("actions"):
            lines += ["### 行动项清单", ""]
            lines.append("| 序号 | 事项 | 截止 | 负责人 |")
            lines.append("| --- | --- | --- | --- |")
            for order, action in enumerate(analysis["actions"], 1):
                lines.append("| %d | %s | %s | %s |" % (
                    order,
                    action.get("task", ""),
                    action.get("due") or "（未定）",
                    action.get("owner") or "（未定）",
                ))
            lines.append("")
        if draft:
            lines += ["### 草稿回复", "", analysis.get("reply") or "（无草稿）", ""]
        lines += ["### 正文（已剥离引用，共剥离 %d 行）" % card.quote_removed, ""]
        body = card.clean_body or "（正文为空）"
        if len(body) > MAX_BODY_CHARS:
            body = body[:MAX_BODY_CHARS] + "\n……（正文过长已截断）"
        lines += ["```text", body, "```", ""]

    lines += [
        "## 汇总统计",
        "",
        "| 指标 | 数值 |",
        "| --- | --- |",
        "| 处理邮件数 | %d |" % stats["total"],
        "| 高优先级 | %d |" % stats["high"],
        "| 中优先级 | %d |" % stats["mid"],
        "| 低优先级 | %d |" % stats["low"],
        "| 待办总数 | %d |" % stats["todos"],
        "| 附件总数 | %d |" % stats["attachments"],
        "",
    ]

    if raw_outputs:
        lines += ["## 附录：模型原始输出（未能解析为 JSON）", ""]
        for subject, raw in raw_outputs:
            lines += ["### %s" % subject, "", "```text", raw or "（空）", "```", ""]

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

def run_self_test():
    """离线自检：造样本覆盖全部确定性逻辑，不联网、不需要 API key"""
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)

        # --- 样本 1：.eml（multipart：text/plain + text/html + 附件） ---
        plain_body = (
            "李四，你好：\n"
            "\n"
            "这封邮件比较紧急，请在 2026-10-28 前确认合同金额 ￥12,000 是否正确？\n"
            "请尽快回复：是否需要法务复核？能否今天内给出结论？\n"
            "我的电话是 13800138000，详情见 https://example.com/contract?id=9\n"
            "\n"
            "> 引用的旧内容一行\n"
            "> 引用的旧内容二行\n"
            "\n"
            "On 2026-10-26 09:00, 王五 wrote:\n"
            "> 转发里的一行\n"
            "> 转发里的二行\n"
        )
        html_body = (
            "<html><body><p>李四，你好：</p>"
            "<script>var secret_token = 'x';</script>"
            "<p>请尽快<b>确认</b>合同。</p></body></html>"
        )
        msg = email.message.EmailMessage()
        msg["From"] = "张三 <zhangsan@example.com>"
        msg["To"] = "李四 <lisi@example.com>"
        msg["Cc"] = "王五 <wangwu@example.com>"
        msg["Subject"] = "紧急：合同确认（请今天内回复）"
        msg["Date"] = email.utils.format_datetime(datetime(2026, 10, 27, 9, 30, 0))
        msg["Message-ID"] = "<child@example.com>"
        msg["In-Reply-To"] = "<parent@example.com>"
        msg["References"] = "<root@example.com> <parent@example.com>"
        msg.set_content(plain_body)
        msg.add_alternative(html_body, subtype="html")
        msg.add_attachment(b"%PDF-1.4 fake", maintype="application", subtype="pdf", filename="合同.pdf")
        eml_path = root / "urgent.eml"
        eml_path.write_bytes(msg.as_bytes())

        cards = parse_eml_file(eml_path)
        assert len(cards) == 1
        card = cards[0]
        assert card.subject == "紧急：合同确认（请今天内回复）", card.subject
        assert card.from_pairs[0] == ("张三", "zhangsan@example.com"), card.from_pairs
        assert ("李四", "lisi@example.com") in card.to_pairs
        assert ("王五", "wangwu@example.com") in card.cc_pairs
        assert card.message_id == "<child@example.com>"
        assert card.in_reply_to == "<parent@example.com>"
        assert card.references == ["<root@example.com>", "<parent@example.com>"]
        assert "2026-10-27" in card.date_iso
        assert len(card.attachments) == 1
        assert card.attachments[0]["name"] == "合同.pdf"
        assert card.attachments[0]["type"] == "application/pdf"
        assert card.attachments[0]["size"] == len(b"%PDF-1.4 fake")

        # --- HTML 去标签：脚本内容不进正文，标签被去掉 ---
        plain_html = html_to_text(html_body)
        assert "李四，你好：" in plain_html, plain_html
        assert "请尽快确认合同。" in plain_html, plain_html
        assert "secret_token" not in plain_html, plain_html
        assert "<" not in plain_html and ">" not in plain_html, plain_html

        # --- 正文优先 text/plain，不混入附件内容 ---
        assert "请在 2026-10-28 前确认合同金额" in card.body_text
        assert "%PDF" not in card.body_text

        # --- 引用剥离：转发块与 "> " 行都去掉，行数统计正确 ---
        clean, removed = strip_quotes(card.body_text)
        assert "引用的旧内容" not in clean
        assert "转发里的一行" not in clean
        assert "On 2026-10-26 09:00" not in clean
        assert "请尽快回复" in clean
        assert removed == 5, removed

        # --- 优先级启发式 ---
        analyze_card(card)
        assert card.priority["level"] == "高", card.priority
        assert card.priority["score"] >= 5
        assert any("紧急" in reason for reason in card.priority["reasons"]), card.priority

        # --- 日期/金额/电话/链接/待办正则 ---
        info = card.info
        assert "2026-10-28" in info["dates"], info["dates"]
        assert any("12,000" in amount for amount in info["amounts"]), info["amounts"]
        assert "13800138000" in info["phones"], info["phones"]
        assert "https://example.com/contract?id=9" in info["links"], info["links"]
        assert len(info["todos"]) == 2, info["todos"]
        assert info["todos"][0].startswith("请在 2026-10-28 前"), info["todos"]

        # --- 低优先级样本 ---
        low_msg = email.message.EmailMessage()
        low_msg["From"] = "王五 <wangwu@example.com>"
        low_msg["To"] = "李四 <lisi@example.com>"
        low_msg["Subject"] = "服务器巡检结果"
        low_msg.set_content("巡检完成，没有发现异常，费用 USD 300 已报销。\n")
        low_path = root / "low.eml"
        low_path.write_bytes(low_msg.as_bytes())
        low_card = analyze_card(parse_eml_file(low_path)[0])
        assert low_card.priority["level"] == "低", low_card.priority
        assert low_card.priority["score"] < 2
        assert any("300" in amount for amount in low_card.info["amounts"]), low_card.info

        # --- .mbox：两封邮件都能解析（头部用 RFC 2047 编码） ---
        mbox_path = root / "inbox.mbox"
        subj1 = email.header.Header("每周运维周报", "utf-8").encode()
        subj2 = email.header.Header("团建报名", "utf-8").encode()
        name1 = email.header.Header("王五", "utf-8").encode()
        name2 = email.header.Header("赵六", "utf-8").encode()
        mbox_text = (
            "From wangwu@example.com Mon Oct 26 10:00:00 2026\n"
            "From: %s <wangwu@example.com>\n"
            "To: lisi@example.com\n"
            "Subject: %s\n"
            "Date: Mon, 26 Oct 2026 10:00:00 +0800\n"
            "Message-ID: <mbox-1@example.com>\n"
            "\n"
            "巡检完成，没有发现异常。\n"
            "\n"
            "From zhaoliu@example.com Mon Oct 26 11:00:00 2026\n"
            "From: %s <zhaoliu@example.com>\n"
            "To: lisi@example.com\n"
            "Subject: %s\n"
            "Date: Mon, 26 Oct 2026 11:00:00 +0800\n"
            "Message-ID: <mbox-2@example.com>\n"
            "\n"
            "请在 10月30日 前报名参加团建，谢谢！\n"
        ) % (name1, subj1, name2, subj2)
        mbox_path.write_text(mbox_text, encoding="utf-8")
        mbox_cards = parse_mbox_file(mbox_path)
        assert len(mbox_cards) == 2, len(mbox_cards)
        assert mbox_cards[0].subject == "每周运维周报", mbox_cards[0].subject
        assert mbox_cards[1].subject == "团建报名"
        assert mbox_cards[1].from_pairs[0] == ("赵六", "zhaoliu@example.com")
        analyze_card(mbox_cards[0])
        analyze_card(mbox_cards[1])
        assert mbox_cards[1].info["todos"], mbox_cards[1].info
        assert mbox_cards[1].priority["level"] in ("中", "高")

        # --- .txt：带邮件头按邮件头解析 ---
        txt_path = root / "notice.txt"
        txt_path.write_text(
            "Subject: 项目周会时间调整\n"
            "From: 孙七 <sunqi@example.com>\n"
            "To: 李四 <lisi@example.com>\n"
            "\n"
            "周会改到周五 15:00，记得带上材料。\n",
            encoding="utf-8",
        )
        txt_card = analyze_card(parse_txt_file(txt_path)[0])
        assert txt_card.subject == "项目周会时间调整", txt_card.subject
        assert ("孙七", "sunqi@example.com") in txt_card.from_pairs
        assert "记得带上材料" in txt_card.info["todos"], txt_card.info["todos"]

        # --- .txt：不带邮件头，主题取文件名 ---
        bare_path = root / "随手记录.txt"
        bare_path.write_text("记得明天提交周报。\n", encoding="utf-8")
        bare_card = analyze_card(parse_txt_file(bare_path)[0])
        assert bare_card.subject == "随手记录", bare_card.subject
        assert bare_card.info["todos"] == ["记得明天提交周报"], bare_card.info["todos"]

        # --- 批量处理目录：逐封卡片 + 汇总统计 ---
        batch_dir = root / "batch"
        batch_dir.mkdir()
        (batch_dir / "urgent.eml").write_bytes(msg.as_bytes())
        (batch_dir / "low.eml").write_bytes(low_msg.as_bytes())
        (batch_dir / "inbox.mbox").write_text(mbox_text, encoding="utf-8")
        (batch_dir / "notice.txt").write_text(txt_path.read_text(encoding="utf-8"), encoding="utf-8")
        files, notes = collect_inputs([str(batch_dir)])
        assert len(files) == 4, files
        assert not notes
        batch_cards = []
        for path in files:
            for item in parse_file(path):
                batch_cards.append(analyze_card(item))
        stats = aggregate(batch_cards)
        # 4 个文件共 5 封邮件（inbox.mbox 里有 2 封）
        assert stats["total"] == 5, stats
        assert stats["high"] == 1, stats
        assert stats["mid"] == 2, stats
        assert stats["low"] == 2, stats
        assert stats["todos"] == 4, stats
        assert stats["attachments"] == 1, stats

        # --- 收集输入：不存在的路径要报错、不支持的类型要提示 ---
        try:
            collect_inputs([str(root / "不存在.eml")])
            assert False, "应该抛 FileNotFoundError"
        except FileNotFoundError:
            pass
        bin_path = root / "x.docx"
        bin_path.write_bytes(b"binary")
        _, bin_notes = collect_inputs([str(bin_path)])
        assert bin_notes and "跳过" in bin_notes[0]

        # --- 模型回复 JSON 解析兜底 ---
        good = (
            "```json\n"
            '{"summary": "测试摘要", "actions": [{"task": "回复合同", "due": "2026-10-28", "owner": "张三"}],'
            ' "reply": "好的，收到。"}\n'
            "```"
        )
        parsed = parse_model_json(good)
        assert parsed is not None
        assert parsed["summary"] == "测试摘要"
        assert parsed["actions"][0]["task"] == "回复合同"
        assert parsed["reply"] == "好的，收到。"
        assert parse_model_json("这不是 JSON，只是模型的自由发挥") is None
        assert parse_model_json("") is None
        assert parse_model_json('{"summary": "ok", "actions": "不是数组"}')["actions"] == []

        # --- 解析失败时用确定性结果兜底，原文进报告附录 ---
        fallback = deterministic_analysis(card, "正式", True)
        assert fallback["summary"] and fallback["reply"]
        assert fallback["actions"], fallback["actions"]
        report_raw = build_report(
            [card], [None], [("紧急：合同确认（请今天内回复）", "自由格式意见")],
            aggregate([card]), "claude-opus-5", False, "正式", True,
            generated_at="2026-10-28 00:00:00",
        )
        assert "附录：模型原始输出" in report_raw
        assert "自由格式意见" in report_raw
        assert "草稿回复" in report_raw

        # --- 完整报告落盘 ---
        report = build_report(
            batch_cards,
            [None] * len(batch_cards),
            [],
            stats,
            "claude-opus-5",
            True,
            "简洁",
            False,
            generated_at="2026-10-28 00:00:00",
        )
        assert report.startswith("# 邮件处理报告")
        assert "## 汇总统计" in report
        assert "邮件 1：" in report and "邮件 5：" in report
        out_path = root / "email-report.md"
        out_path.write_text(report, encoding="utf-8")
        assert out_path.read_text(encoding="utf-8").startswith("# 邮件处理报告")

        # --- call_model 三级降级（离线假客户端） ---
        fake = _FakeClient()
        result = call_model(fake, "fake-model", "system", [{"role": "user", "content": "hi"}])
        assert result["ok"] is True
        assert fake.state["calls"] == 2

    print("SELF-TEST OK")


# ---------- CLI ----------

def build_parser():
    parser = argparse.ArgumentParser(
        description="邮件助手：解析 .eml/.mbox/.txt 邮件，做摘要、优先级判断、待办提取与回复起草，输出中文 Markdown 报告",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            "  python3 agent.py 收件箱.mbox --draft --tone 正式 --output 邮件报告.md\n"
            "  python3 agent.py ./邮件目录 --offline\n"
            "  python3 agent.py 一封邮件.eml --draft --tone 简洁\n"
            "  python3 agent.py 邮件.txt --model qwen3.8-max\n"
        ),
    )
    parser.add_argument(
        "inputs", nargs="*",
        help="邮件文件（.eml/.mbox/.txt）或包含它们的目录，可以给多个",
    )
    parser.add_argument(
        "--draft", action="store_true",
        help="按指定语气起草回复草稿（会调用模型）",
    )
    parser.add_argument(
        "--tone", choices=list(TONES), default="正式",
        help="回复语气：正式 / 友好 / 简洁（默认 正式）",
    )
    parser.add_argument(
        "--output", default=DEFAULT_OUTPUT,
        help="报告写入路径（默认 %s）" % DEFAULT_OUTPUT,
    )
    parser.add_argument(
        "--offline", action="store_true",
        help="纯确定性模式：只做解析、统计和规则提取，不调用模型、不需要 API key",
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

    if not args.inputs:
        parser.error("请至少提供一个邮件文件或目录（.eml / .mbox / .txt）")

    try:
        files, notes = collect_inputs(args.inputs)
    except FileNotFoundError as exc:
        print("错误：%s" % exc, file=sys.stderr)
        sys.exit(1)
    for note in notes:
        print("[提示] %s" % note)
    if not files:
        print("错误：没有找到 .eml / .mbox / .txt 文件。", file=sys.stderr)
        sys.exit(1)

    cards = []
    for path in files:
        try:
            cards.extend(parse_file(path))
        except Exception as exc:
            print("[提示] 解析失败已跳过：%s（%s）" % (path.name, exc))
    if not cards:
        print("错误：所有文件都解析失败，没有可处理的邮件。", file=sys.stderr)
        sys.exit(1)
    print("[解析] %d 个文件，共 %d 封邮件" % (len(files), len(cards)))

    for card in cards:
        analyze_card(card)
    stats = aggregate(cards)
    print("[规则] 优先级 高 %d / 中 %d / 低 %d，待办 %d 条"
          % (stats["high"], stats["mid"], stats["low"], stats["todos"]))

    raw_outputs = []
    if args.offline:
        analyses = [deterministic_analysis(card, args.tone, args.draft) for card in cards]
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
        print("[模型] 模型：%s" % args.model)
        try:
            analyses, raw_outputs = analyze_with_model(
                cards, args.model, args.tone, args.draft
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
        for index, analysis in enumerate(analyses):
            if analysis is None:
                analyses[index] = deterministic_analysis(cards[index], args.tone, args.draft)

    report = build_report(
        cards, analyses, raw_outputs, stats,
        args.model, args.offline, args.tone, args.draft,
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
    print("[完成] 邮件 %d 封（高 %d、中 %d、低 %d），待办 %d 条"
          % (stats["total"], stats["high"], stats["mid"], stats["low"], stats["todos"]))
    if raw_outputs:
        print("[提示] 有 %d 封邮件的模型输出不是合法 JSON，已原样放入报告附录。" % len(raw_outputs))


if __name__ == "__main__":
    main()
