#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
日志排查助手 —— 流式扫描日志、确定性聚合统计、再由 Claude 产出根因假设

它先把日志「读薄」：逐行流式扫描（支持 .gz、自动识别 utf-8/gbk），识别多种
日志格式，归一化消息签名后做聚合统计（级别分布、Top 错误签名、错误时间分布、
nginx 状态码与慢请求、跨文件时间线）。统计结果既可以直接看（--stats-only，
不花 token），也可以把「统计摘要」交给 Claude 分析根因、给排查命令，最后写成
一份 Markdown 报告。

用法：
    python3 agent.py <日志文件...> [选项]

示例：
    python3 agent.py app.log --stats-only
    python3 agent.py logs/*.log access.log --level-min ERROR --top 20
    python3 agent.py app.log --since 2026-10-20 --output 排查报告.md
    python3 agent.py app.log.gz --grep "payment" --stats-only

环境要求：
    1. 只用模型分析时才需要设置 ANTHROPIC_API_KEY：
           export ANTHROPIC_API_KEY=你的key
    2. 安装 SDK：
           pip3 install -r requirements.txt

安全边界：
    - 只读取你命令行里明确给出的文件，不递归扫描目录、不写日志目录
    - 报告只会写到 --output 指定的位置（默认当前目录 log-report.md）
    - 发给模型的是聚合后的统计摘要（≤20000 字符），不是日志全文

参数降级兼容：call_model() 会按「能力从高到低」逐级降级，官方 API 与第三方
Anthropic 兼容网关都能用（网关不支持 thinking/fallbacks 时自动退到基础调用）。
"""

import argparse
import datetime
import gzip
import heapq
import io
import json
import os
import re
import sys
import tempfile
from collections import Counter
from pathlib import Path

import anthropic

# 默认模型（可用 --model 参数或 LOG_DETECTIVE_MODEL 环境变量更换）
DEFAULT_MODEL = os.environ.get("LOG_DETECTIVE_MODEL", "claude-opus-5")

MAX_LINES_DEFAULT = 500_000   # 默认最多扫描多少行
MAX_SAMPLE_CHARS = 300        # 样例原文最多保留多少字符
MAX_SIGNATURES = 200_000      # 最多保留多少个不同签名，防止内存爆炸
MAX_MODEL_CHARS = 20_000      # 发给模型的统计摘要上限
MAX_TIMELINE = 50             # 报告里的跨文件时间线条数
MAX_TIMELINE_BUFFER = 400     # 时间线内部缓冲条数（堆容量）
MAX_SLOW_REQUESTS = 10        # nginx 最慢请求展示条数
CONT_LINE_LIMIT = 5           # 一条错误最多拼接几行后续堆栈行
SAMPLE_PREVIEW_BYTES = 64_000  # 编码嗅探读多少字节

# 级别优先级（数值越大越严重），用于 --level-min 过滤
LEVEL_RANK = {"DEBUG": 10, "INFO": 20, "WARN": 30, "ERROR": 40, "UNKNOWN": 35}

# 不同日志系统对级别的写法 → 统一成四档
LEVEL_MAP = {
    "DEBUG": "DEBUG", "TRACE": "DEBUG", "VERBOSE": "DEBUG", "FINE": "DEBUG",
    "INFO": "INFO", "NOTICE": "INFO", "INFORMATION": "INFO",
    "WARN": "WARN", "WARNING": "WARN",
    "ERROR": "ERROR", "ERR": "ERROR", "SEVERE": "ERROR",
    "CRITICAL": "ERROR", "FATAL": "ERROR", "ALERT": "ERROR", "EMERG": "ERROR",
}

LEVEL_NAMES = ["ERROR", "WARN", "INFO", "DEBUG", "UNKNOWN"]

# ---------------- 各日志格式的正则 ----------------

LEVEL_PATTERN = r"DEBUG|TRACE|INFO|NOTICE|WARN|WARNING|ERROR|CRITICAL|FATAL|SEVERE"

# Python logging：2026-10-20 12:00:01,123 ERROR module.name: message
RE_PYLOG = re.compile(
    r"^(?P<date>\d{4}-\d{2}-\d{2})[ T](?P<time>\d{2}:\d{2}:\d{2})"
    r"(?:[.,](?P<frac>\d{1,6}))?\s+\[?(?P<level>" + LEVEL_PATTERN + r")\]?"
    r"\s+(?P<logger>[\w\.\-/]*?)\s*:?\s(?P<msg>.*)$"
)

# ISO8601：2026-10-20T12:00:01.123Z ERROR message（也支持 +08:00）
RE_ISO = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}"
    r"(?:[.,]\d{1,6})?(?:Z|[+-]\d{2}:?\d{2})?)\s+"
    r"\[?(?P<level>" + LEVEL_PATTERN + r")\]?\s+(?P<msg>.*)$"
)

# Java / log4j：12:00:01.123 [thread-1] ERROR com.x.Y - message
RE_LOG4J = re.compile(
    r"^(?P<time>\d{2}:\d{2}:\d{2})(?:[.,](?P<frac>\d{1,6}))?\s+"
    r"\[(?P<thread>[^\]]*)\]\s+\[?(?P<level>" + LEVEL_PATTERN + r")\]?\s+"
    r"(?P<logger>[^\s\-]+)\s+-\s+(?P<msg>.*)$"
)

# nginx / apache access：1.2.3.4 - - [20/Oct/2026:12:00:01 +0800] "GET /x HTTP/1.1" 500 123 0.032
RE_ACCESS = re.compile(
    r"^(?P<ip>\S+)\s+\S+\s+\S+\s+\[(?P<ts>[^\]]+)\]\s+"
    r'"(?P<request>[^"]*)"\s+(?P<status>\d{3})\s+(?P<bytes>\d+|-)'
    r'(?:\s+"[^"]*"\s+"[^"]*")?'
    r"(?:\s+(?P<rt>[\d.]+))?"
)

# 消息签名归一化用的正则
RE_URL = re.compile(r"https?://[^\s\"'<>]+")
RE_UUID = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
)
RE_IP = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b")
RE_TS_FULL = re.compile(
    r"\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?(?:Z|[+-]\d{2}:?\d{2})?\b"
)
RE_TIME_ONLY = re.compile(r"\b\d{1,2}:\d{2}:\d{2}(?:[.,]\d+)?\b")
RE_HEX = re.compile(r"\b0x[0-9a-fA-F]+\b|\b[0-9a-fA-F]{12,}\b")
RE_QUOTED_PATH = re.compile(r'"([^"\r\n]*[/\\][^"\r\n]*)"')
RE_NUM = re.compile(r"\b\d+(?:\.\d+)?\b")
RE_WS = re.compile(r"\s+")

MONTHS = {
    "Jan": 1, "Feb": 2, "Mar": 3, "Apr": 4, "May": 5, "Jun": 6,
    "Jul": 7, "Aug": 8, "Sep": 9, "Oct": 10, "Nov": 11, "Dec": 12,
}

FORMAT_LABELS = {
    "python": "Python logging",
    "iso": "ISO8601",
    "log4j": "Java/log4j",
    "access": "nginx/apache access",
    "json": "JSON lines",
    "plain": "未结构化错误行",
}


# ---------------- 时间处理 ----------------


def _build_dt(date_part, hour, minute, second, frac):
    """把日期/时间各段拼成 naive datetime，frac 支持 1~9 位小数"""
    if frac is None or frac == "":
        micro = 0
    else:
        micro = int((frac + "000000")[:6])
    return datetime.datetime(
        date_part.year, date_part.month, date_part.day,
        int(hour), int(minute), int(second), micro,
    )


def parse_iso_ts(text):
    """解析 ISO8601 时间戳，忽略时区偏移（按日志里的墙上时间聚合）"""
    m = re.match(
        r"^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2}):(\d{2})"
        r"(?:[.,](\d{1,9}))?(?:Z|[+-]\d{2}:?\d{2})?$",
        text.strip(),
    )
    if not m:
        return None
    try:
        date_part = datetime.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        return _build_dt(date_part, m.group(4), m.group(5), m.group(6), m.group(7))
    except ValueError:
        return None


def parse_access_ts(text):
    """解析 nginx 的 20/Oct/2026:12:00:01 +0800"""
    m = re.match(
        r"^(\d{1,2})/([A-Za-z]{3})/(\d{4}):(\d{2}):(\d{2}):(\d{2})(?:\s+[+-]\d{4})?$",
        text.strip(),
    )
    if not m:
        return None
    month = MONTHS.get(m.group(2).title())
    if not month:
        return None
    try:
        return datetime.datetime(
            int(m.group(3)), month, int(m.group(1)),
            int(m.group(4)), int(m.group(5)), int(m.group(6)),
        )
    except ValueError:
        return None


def parse_bound(text):
    """解析 --since / --until 的取值：YYYY-MM-DD 或 YYYY-MM-DD HH:MM[:SS]"""
    text = text.strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.datetime.strptime(text, fmt)
        except ValueError:
            continue
    parsed = parse_iso_ts(text)
    if parsed is not None:
        return parsed
    raise ValueError("时间格式无法识别：%s（应为 YYYY-MM-DD 或 YYYY-MM-DD HH:MM:SS）" % text)


def parse_bucket(text):
    """把 1m / 5m / 1h / 30s 解析成秒数"""
    m = re.match(r"^(\d+)([smh])$", text.strip().lower())
    if not m:
        raise ValueError("--bucket 只支持 数字+单位（s/m/h），如 1m、5m、1h")
    unit = {"s": 1, "m": 60, "h": 3600}[m.group(2)]
    seconds = int(m.group(1)) * unit
    if seconds <= 0:
        raise ValueError("--bucket 必须大于 0")
    return seconds


def _dt_to_epoch(dt):
    """naive datetime → epoch 秒（本地时区，仅用于分桶和排序）"""
    return int(dt.timestamp())


def _bucket_label(epoch_seconds):
    return datetime.datetime.fromtimestamp(epoch_seconds).strftime("%Y-%m-%d %H:%M:%S")


def _fmt_dt(dt):
    if dt is None:
        return "（无）"
    return dt.strftime("%Y-%m-%d %H:%M:%S")


# ---------------- 消息签名归一化 ----------------


def normalize_message(text):
    """把易变的部分换成占位符，得到可聚合的「签名」"""
    s = text or ""
    s = RE_URL.sub("<url>", s)
    s = RE_QUOTED_PATH.sub("<path>", s)
    s = RE_UUID.sub("<uuid>", s)
    s = RE_IP.sub("<ip>", s)
    s = RE_TS_FULL.sub("<ts>", s)
    s = RE_TIME_ONLY.sub("<ts>", s)
    s = RE_HEX.sub("<hex>", s)
    s = RE_NUM.sub("<num>", s)
    s = RE_WS.sub(" ", s).strip()
    return s[:200]


# ---------------- 流式读取 ----------------


def detect_encoding(head_bytes):
    """先试 utf-8，再兜底 gbk；都失败就 utf-8 + replace"""
    if not head_bytes:
        return "utf-8"
    for enc in ("utf-8", "gbk"):
        try:
            head_bytes.decode(enc)
            return enc
        except UnicodeDecodeError:
            continue
    return "utf-8"


def open_log_stream(path):
    """打开日志为文本流，.gz 自动解压；返回 (stream, kind, encoding)"""
    path_str = str(path)
    is_gz = path_str.lower().endswith(".gz")
    raw = gzip.open(path_str, "rb") if is_gz else open(path_str, "rb")
    try:
        head = raw.read(SAMPLE_PREVIEW_BYTES)
        encoding = detect_encoding(head)
        raw.seek(0)
        stream = io.TextIOWrapper(raw, encoding=encoding, errors="replace", newline=None)
    except Exception:
        raw.close()
        raise
    return stream, ("gzip" if is_gz else "plain"), encoding


# ---------------- 单行解析 ----------------


def _normalize_level(raw_level):
    if not raw_level:
        return "UNKNOWN"
    return LEVEL_MAP.get(raw_level.strip().upper(), "UNKNOWN")


def _new_entry(fmt, level, ts, msg, raw_line, file_label, lineno):
    return {
        "format": fmt,
        "level": level,
        "ts": ts,
        "msg": (msg or "").strip(),
        "sample": raw_line[:MAX_SAMPLE_CHARS],
        "file": file_label,
        "line": lineno,
        "extra": {},
    }


def guess_level(text):
    """兜底：靠关键词猜级别，不是错误/告警就返回 None"""
    upper = text.upper()
    for kw in ("FATAL", "CRITICAL", "ERROR", "EXCEPTION", "TRACEBACK"):
        if kw in upper:
            return "ERROR"
    if "WARN" in upper:
        return "WARN"
    return None


def _level_for_status(status):
    if status >= 500:
        return "ERROR"
    if status >= 400:
        return "WARN"
    return "INFO"


def _looks_like_json(line):
    stripped = line.strip()
    return len(stripped) > 2 and stripped[0] == "{" and stripped[-1] == "}"


def parse_json_line(line, file_label, lineno):
    """JSON lines：字段名做多路兼容"""
    try:
        obj = json.loads(line)
    except (ValueError, TypeError):
        return None
    if not isinstance(obj, dict):
        return None

    ts = None
    for key in ("time", "timestamp", "ts", "@timestamp", "datetime", "date", "eventTime"):
        value = obj.get(key)
        if isinstance(value, (int, float)):
            try:
                ts = datetime.datetime.fromtimestamp(float(value))
            except (OverflowError, OSError, ValueError):
                ts = None
        elif isinstance(value, str) and value.strip():
            ts = parse_iso_ts(value)
        if ts is not None:
            break

    raw_level = None
    for key in ("level", "lvl", "severity", "log_level", "loglevel", "levelname"):
        value = obj.get(key)
        if isinstance(value, str):
            raw_level = value
            break
    level = _normalize_level(raw_level)

    msg = None
    for key in ("msg", "message", "text", "event", "log", "body"):
        value = obj.get(key)
        if isinstance(value, str):
            msg = value
            break
    if msg is None:
        msg = json.dumps(obj, ensure_ascii=False)

    service = ""
    for key in ("service", "logger", "name", "app", "component", "module"):
        value = obj.get(key)
        if isinstance(value, str):
            service = value
            break

    if level == "UNKNOWN":
        guessed = guess_level(msg)
        if guessed:
            level = guessed

    entry = _new_entry("json", level, ts, msg, line, file_label, lineno)
    entry["extra"]["service"] = service
    return entry


def parse_line(line, file_label, lineno, default_date):
    """按「最具体 → 最宽松」的顺序尝试各格式，返回 entry 或 None"""
    stripped = line.rstrip("\r\n")
    if not stripped.strip():
        return None

    # 1) Python logging
    m = RE_PYLOG.match(stripped)
    if m:
        ts = None
        try:
            date_part = datetime.date(
                int(m.group("date")[0:4]), int(m.group("date")[5:7]), int(m.group("date")[8:10])
            )
            ts = _build_dt(date_part, m.group("time")[0:2], m.group("time")[3:5],
                           m.group("time")[6:8], m.group("frac"))
        except (ValueError, IndexError):
            ts = None
        return _new_entry(
            "python", _normalize_level(m.group("level")), ts,
            m.group("msg"), stripped, file_label, lineno,
        )

    # 2) ISO8601
    m = RE_ISO.match(stripped)
    if m:
        return _new_entry(
            "iso", _normalize_level(m.group("level")), parse_iso_ts(m.group("ts")),
            m.group("msg"), stripped, file_label, lineno,
        )

    # 3) Java / log4j（只有时间没有日期 → 用 default_date 当天）
    m = RE_LOG4J.match(stripped)
    if m:
        ts = None
        try:
            ts = _build_dt(default_date, m.group("time")[0:2], m.group("time")[3:5],
                           m.group("time")[6:8], m.group("frac"))
        except (ValueError, IndexError):
            ts = None
        entry = _new_entry(
            "log4j", _normalize_level(m.group("level")), ts,
            m.group("msg"), stripped, file_label, lineno,
        )
        entry["extra"]["logger"] = m.group("logger")
        entry["extra"]["thread"] = m.group("thread")
        return entry

    # 4) nginx / apache access
    m = RE_ACCESS.match(stripped)
    if m:
        status = int(m.group("status"))
        ts = parse_access_ts(m.group("ts"))
        request = m.group("request") or ""
        parts = request.split()
        method = parts[0] if parts else ""
        path = parts[1] if len(parts) > 1 else request
        rt = None
        if m.group("rt"):
            try:
                rt = float(m.group("rt"))
            except ValueError:
                rt = None
        entry = _new_entry(
            "access", _level_for_status(status), ts,
            "%s %s -> %d" % (method, path, status), stripped, file_label, lineno,
        )
        entry["extra"].update({
            "ip": m.group("ip"), "status": status, "method": method,
            "path": path, "rt": rt,
        })
        return entry

    # 5) JSON lines
    if _looks_like_json(stripped):
        entry = parse_json_line(stripped, file_label, lineno)
        if entry is not None:
            return entry

    # 6) 兜底：关键词命中才算错误行，否则整行忽略
    level = guess_level(stripped)
    if level:
        return _new_entry("plain", level, None, stripped, stripped, file_label, lineno)
    return None


# ---------------- 聚合 ----------------


def new_stats(bucket_seconds):
    return {
        "files": [],
        "total_lines": 0,        # 实际读进来的行数（含空行/未能识别行）
        "matched_lines": 0,      # 参与统计的行数
        "levels": Counter(),
        "formats": Counter(),
        "first_ts": None,
        "last_ts": None,
        "timestamps_missing": 0,
        "signatures": {},
        "sig_overflow": 0,
        "buckets": Counter(),
        "bucket_seconds": bucket_seconds,
        "timeline": [],
        "nginx": {
            "total": 0,
            "errors": 0,
            "status": Counter(),
            "slow": [],
            "buckets": Counter(),
        },
        "truncated": False,
        "unparsed_lines": 0,
        "date_from_mtime": 0,   # 多少次时间戳的日期是按文件修改日期补的
        "notes": [],
    }


def _note_date_source(stats):
    stats["date_from_mtime"] += 1


def _record_timeline(stats, entry):
    """用最小堆保留最早的一批事件，避免为时间线缓存全量数据"""
    ts = entry["ts"]
    if ts is None:
        return
    item = (
        -_dt_to_epoch(ts),
        entry["file"],
        entry["line"],
        entry["level"],
        entry["msg"][:120],
    )
    heap = stats["timeline"]
    if len(heap) < MAX_TIMELINE_BUFFER:
        heapq.heappush(heap, item)
    elif item > heap[0]:
        heapq.heapreplace(heap, item)


def _entry_signature(entry):
    """生成签名：access 日志保留真实状态码（否则 500/502/504 会被一起归一化掉）"""
    if entry["format"] == "access":
        extra = entry["extra"]
        path = normalize_message(extra.get("path", "") or "")
        signature = "%s %s" % (extra.get("method", ""), path)
        status = extra.get("status")
        if status is not None:
            signature += " -> %d" % status
        return signature.strip()[:200]
    return normalize_message(entry["msg"] or entry["sample"])


def record_entry(stats, entry, level_min_rank):
    level = entry["level"] or "UNKNOWN"
    if LEVEL_RANK.get(level, 0) < level_min_rank:
        return

    stats["matched_lines"] += 1
    stats["levels"][level] += 1
    stats["formats"][entry["format"]] += 1

    ts = entry["ts"]
    if ts is None:
        stats["timestamps_missing"] += 1
    else:
        if stats["first_ts"] is None or ts < stats["first_ts"]:
            stats["first_ts"] = ts
        if stats["last_ts"] is None or ts > stats["last_ts"]:
            stats["last_ts"] = ts

    # nginx 专有统计
    if entry["format"] == "access":
        nginx = stats["nginx"]
        status = entry["extra"].get("status")
        rt = entry["extra"].get("rt")
        nginx["total"] += 1
        if status is not None:
            nginx["status"][status] += 1
            if status >= 400:
                nginx["errors"] += 1
        if rt is not None:
            slow = nginx["slow"]
            record = (rt, entry["extra"].get("method", ""), entry["extra"].get("path", ""),
                      entry["file"], entry["line"])
            if len(slow) < MAX_SLOW_REQUESTS:
                heapq.heappush(slow, record)
            elif record > slow[0]:
                heapq.heapreplace(slow, record)

    # 只对错误/告警做签名聚合和时间分桶
    if level not in ("ERROR", "WARN"):
        return

    signature = _entry_signature(entry)
    sigs = stats["signatures"]
    record = sigs.get(signature)
    if record is None:
        if len(sigs) >= MAX_SIGNATURES:
            stats["sig_overflow"] += 1
            return
        sigs[signature] = {
            "count": 1,
            "level": level,
            "first_ts": ts,
            "last_ts": ts,
            "sample": entry["sample"][:MAX_SAMPLE_CHARS],
            "file": entry["file"],
            "line": entry["line"],
        }
    else:
        record["count"] += 1
        if ts is not None:
            if record["first_ts"] is None or ts < record["first_ts"]:
                record["first_ts"] = ts
            if record["last_ts"] is None or ts > record["last_ts"]:
                record["last_ts"] = ts
        if record["level"] == "WARN" and level == "ERROR":
            record["level"] = "ERROR"

    if ts is not None:
        bucket_seconds = stats["bucket_seconds"]
        bucket_start = (_dt_to_epoch(ts) // bucket_seconds) * bucket_seconds
        stats["buckets"][bucket_start] += 1
        if entry["format"] == "access":
            stats["nginx"]["buckets"][bucket_start] += 1

    _record_timeline(stats, entry)


def scan_file(path, stats, options, remaining_lines):
    """流式扫描一个文件，返回 (本次读了多少行, 是否因为行数上限被截断)"""
    file_label = os.path.basename(path)
    default_date = None
    try:
        default_date = datetime.datetime.fromtimestamp(os.path.getmtime(path)).date()
    except OSError:
        default_date = datetime.datetime.now().date()

    used = 0
    hit_limit = False
    last_entry = None
    cont_left = 0
    fmt_counter = Counter()
    file_errors = 0

    stream, kind, encoding = open_log_stream(path)
    try:
        for raw in stream:
            if used >= remaining_lines:
                hit_limit = True
                break
            used += 1
            stats["total_lines"] += 1
            line = raw.rstrip("\r\n")

            # 堆栈续行：紧跟错误行、以空白开头的行并进上一条样例，不单独成条
            if line[:1] in (" ", "\t") and last_entry is not None and cont_left < CONT_LINE_LIMIT:
                cont_left += 1
                if len(last_entry["sample"]) < MAX_SAMPLE_CHARS:
                    last_entry["sample"] = (
                        last_entry["sample"] + "\n" + line.strip()
                    )[:MAX_SAMPLE_CHARS]
                continue

            if options["grep"] is not None and not options["grep"].search(line):
                stats["unparsed_lines"] += 1
                continue

            entry = parse_line(line, file_label, used, default_date)
            if entry is None:
                if line.strip():
                    stats["unparsed_lines"] += 1
                continue

            # 时间范围过滤（无时间戳的行不参与过滤，保留）
            ts = entry["ts"]
            if ts is not None:
                if options["since"] is not None and ts < options["since"]:
                    continue
                if options["until"] is not None and ts > options["until"]:
                    continue
            if entry["format"] == "log4j" and ts is not None:
                _note_date_source(stats)

            last_entry = entry
            cont_left = 0
            # 同一文件里出现过完整日期后，后续只有时间的 log4j 行沿用这个日期，
            # 比一律用文件修改日期更接近真实；一次都没出现才退回修改日期。
            if entry["format"] in ("python", "iso", "access") and entry["ts"] is not None:
                default_date = entry["ts"].date()
            fmt_counter[entry["format"]] += 1
            if entry["level"] in ("ERROR", "WARN"):
                file_errors += 1
            record_entry(stats, entry, options["level_min_rank"])
    finally:
        try:
            stream.close()
        except Exception:
            pass

    stats["files"].append({
        "path": str(path),
        "kind": kind,
        "encoding": encoding,
        "lines": used,
        "errors": file_errors,
        "formats": dict(fmt_counter),
        "truncated": hit_limit,
    })
    return used, hit_limit


def scan_paths(paths, options):
    """扫描全部文件，产出统计结果"""
    stats = new_stats(options["bucket_seconds"])
    budget = options["max_lines"]
    for path in paths:
        if budget <= 0:
            stats["truncated"] = True
            break
        used, hit_limit = scan_file(path, stats, options, budget)
        budget -= used
        if hit_limit:
            stats["truncated"] = True
            break

    if stats["truncated"]:
        stats["notes"].append(
            "扫描行数达到 --max-lines %d 上限，后续内容已截断，统计结果只代表已扫描部分。"
            % options["max_lines"]
        )
    if stats["date_from_mtime"]:
        stats["notes"].append(
            "有 %d 条 log4j 风格日志只有时间没有日期，已按对应文件的修改日期补全，跨天时可能不准。"
            % stats["date_from_mtime"]
        )
    if stats["timestamps_missing"]:
        stats["notes"].append(
            "有 %d 条错误行没有时间戳，未参与时间分桶和时间线统计。" % stats["timestamps_missing"]
        )
    if stats["sig_overflow"]:
        stats["notes"].append(
            "不同签名超过 %d 个，其余错误已合并计数、不再单独列出。" % MAX_SIGNATURES
        )
    return stats


# ---------------- 报告渲染 ----------------


def top_signatures(stats, limit):
    items = list(stats["signatures"].items())
    items.sort(key=lambda kv: (-kv[1]["count"], kv[0]))
    return items[:limit]


def top_buckets(stats, limit=5):
    if not stats["buckets"]:
        return []
    items = sorted(stats["buckets"].items(), key=lambda kv: (-kv[1], kv[0]))
    peak = items[0][1] or 1
    return [(start, count, count * 100.0 / peak) for start, count in items[:limit]]


def _md_escape_cell(text):
    return (text or "").replace("|", "\\|").replace("\n", " ")


def _md_code_block(text, lang=""):
    fence = "```"
    body = (text or "").replace("```", "'''")
    return "%s%s\n%s\n%s" % (fence, lang, body, fence)


def render_overview(stats, options):
    lines = []
    lines.append("| 项目 | 值 |")
    lines.append("| --- | --- |")
    lines.append("| 扫描文件数 | %d |" % len(stats["files"]))
    lines.append("| 总行数（已读取） | %d |" % stats["total_lines"])
    lines.append("| 参与统计的行数 | %d |" % stats["matched_lines"])
    lines.append("| 未识别/被过滤的行数 | %d |" % stats["unparsed_lines"])
    lines.append("| ERROR / WARN | %d / %d |" % (
        stats["levels"].get("ERROR", 0), stats["levels"].get("WARN", 0)))
    span = "（无可用时间戳）"
    if stats["first_ts"] is not None and stats["last_ts"] is not None:
        seconds = max(0, _dt_to_epoch(stats["last_ts"]) - _dt_to_epoch(stats["first_ts"]))
        span = "%s → %s（约 %s）" % (
            _fmt_dt(stats["first_ts"]), _fmt_dt(stats["last_ts"]), _human_span(seconds))
    lines.append("| 时间跨度 | %s |" % span)
    lines.append("| 错误签名种类 | %d |" % len(stats["signatures"]))
    bucket_seconds = stats["bucket_seconds"]
    lines.append("| 分桶粒度 | %s |" % _bucket_desc(bucket_seconds))
    if options["grep"] is not None:
        lines.append("| 行过滤正则 | `%s` |" % _md_escape_cell(options["grep_text"]))
    lines.append("")
    return lines


def _bucket_desc(seconds):
    if seconds % 3600 == 0:
        return "%d 小时" % (seconds // 3600)
    if seconds % 60 == 0:
        return "%d 分钟" % (seconds // 60)
    return "%d 秒" % seconds


def _human_span(seconds):
    if seconds < 60:
        return "%d 秒" % seconds
    if seconds < 3600:
        return "%d 分 %d 秒" % (seconds // 60, seconds % 60)
    if seconds < 86400:
        return "%d 小时 %d 分" % (seconds // 3600, (seconds % 3600) // 60)
    return "%d 天 %d 小时" % (seconds // 86400, (seconds % 86400) // 3600)


def render_levels(stats):
    total = sum(stats["levels"].values()) or 1
    lines = ["| 级别 | 条数 | 占比 |", "| --- | ---: | ---: |"]
    for name in LEVEL_NAMES:
        count = stats["levels"].get(name, 0)
        lines.append("| %s | %d | %.1f%% |" % (name, count, count * 100.0 / total))
    lines.append("| **合计** | **%d** | 100%% |" % sum(stats["levels"].values()))
    lines.append("")
    if stats["formats"]:
        lines.append("识别到的日志格式：")
        lines.append("")
        lines.append("| 格式 | 条数 |")
        lines.append("| --- | ---: |")
        for key, count in stats["formats"].most_common():
            lines.append("| %s | %d |" % (FORMAT_LABELS.get(key, key), count))
        lines.append("")
    return lines


def render_signatures(stats, limit):
    items = top_signatures(stats, limit)
    lines = []
    if not items:
        return ["没有识别到错误/告警签名。", ""]
    for index, (signature, info) in enumerate(items, 1):
        lines.append("### %d. `%s` — %d 次" % (
            index, _md_escape_cell(signature), info["count"]))
        lines.append("")
        lines.append("- 级别：%s" % info["level"])
        lines.append("- 首次：%s" % _fmt_dt(info["first_ts"]))
        lines.append("- 末次：%s" % _fmt_dt(info["last_ts"]))
        lines.append("- 首次出现位置：`%s:%d`" % (info["file"], info["line"]))
        lines.append("- 样例原文：")
        lines.append("")
        lines.append(_md_code_block(info["sample"], "text"))
        lines.append("")
    return lines


def render_buckets(stats, limit=5):
    items = top_buckets(stats, limit)
    if not items:
        return ["没有可用于分桶的时间戳（或没有错误行），本节跳过。", ""]
    lines = ["| 时间桶（%s） | 错误条数 | 相对峰值 |" % _bucket_desc(stats["bucket_seconds"]),
             "| --- | ---: | ---: |"]
    for start, count, ratio in items:
        lines.append("| %s | %d | %.0f%% |" % (_bucket_label(start), count, ratio))
    lines.append("")
    lines.append("完整时间桶序列（按时间正序，最多 40 个）：")
    lines.append("")
    ordered = sorted(stats["buckets"].items())
    lines.append("| 时间桶 | 错误条数 |")
    lines.append("| --- | ---: |")
    for start, count in ordered[:40]:
        lines.append("| %s | %d |" % (_bucket_label(start), count))
    if len(ordered) > 40:
        lines.append("| ……（共 %d 个桶，已截断） | |" % len(ordered))
    lines.append("")
    return lines


def render_nginx(stats):
    nginx = stats["nginx"]
    if not nginx["total"]:
        return []
    lines = ["| 项目 | 值 |", "| --- | --- |"]
    lines.append("| 请求总数 | %d |" % nginx["total"])
    lines.append("| 错误请求（4xx+5xx） | %d |" % nginx["errors"])
    rate = nginx["errors"] * 100.0 / nginx["total"] if nginx["total"] else 0.0
    lines.append("| 错误率 | %.2f%% |" % rate)
    lines.append("")
    classes = Counter()
    for status, count in nginx["status"].items():
        classes["%dxx" % (status // 100)] += count
    lines.append("| 状态码分类 | 次数 |")
    lines.append("| --- | ---: |")
    for name in ("2xx", "3xx", "4xx", "5xx"):
        if classes.get(name):
            lines.append("| %s | %d |" % (name, classes[name]))
    lines.append("")
    lines.append("出现最多的状态码：")
    lines.append("")
    lines.append("| 状态码 | 次数 |")
    lines.append("| ---: | ---: |")
    for status, count in nginx["status"].most_common(10):
        lines.append("| %d | %d |" % (status, count))
    lines.append("")

    if nginx["slow"]:
        lines.append("最慢的 %d 个请求：" % min(MAX_SLOW_REQUESTS, len(nginx["slow"])))
        lines.append("")
        lines.append("| 耗时(s) | 方法 | 路径 | 位置 |")
        lines.append("| ---: | --- | --- | --- |")
        for rt, method, path, file_label, lineno in sorted(nginx["slow"], reverse=True):
            lines.append("| %.3f | %s | `%s` | `%s:%d` |" % (
                rt, method, _md_escape_cell(path), file_label, lineno))
        lines.append("")
    return lines


def render_timeline(stats):
    events = sorted(stats["timeline"], reverse=True)[:MAX_TIMELINE]
    if not events:
        return ["没有带时间戳的事件可供排序。", ""]
    lines = ["| 时间 | 级别 | 文件:行 | 内容 |", "| --- | --- | --- | --- |"]
    for neg_epoch, file_label, lineno, level, msg in events:
        ts = datetime.datetime.fromtimestamp(-neg_epoch)
        lines.append("| %s | %s | `%s:%d` | %s |" % (
            _fmt_dt(ts), level, file_label, lineno, _md_escape_cell(msg)))
    lines.append("")
    return lines


def render_file_table(stats):
    lines = ["| 文件 | 类型 | 编码 | 行数 | 错误/告警 | 格式 |",
             "| --- | --- | --- | ---: | ---: | --- |"]
    for info in stats["files"]:
        formats = ", ".join(
            "%s×%d" % (FORMAT_LABELS.get(k, k), v) for k, v in sorted(info["formats"].items())
        ) or "-"
        lines.append("| `%s` | %s | %s | %d | %d | %s |" % (
            info["path"], info["kind"], info["encoding"], info["lines"],
            info["errors"], _md_escape_cell(formats)))
    lines.append("")
    return lines


def build_model_summary(stats, options):
    """把统计摘要压成 ≤20000 字符的文本，交给模型分析（不发原始全文）"""
    parts = []
    parts.append("# 日志统计摘要（自动生成，非原始全文）")
    parts.append("")
    parts.append("扫描文件：%d 个；总行数 %d；参与统计 %d 行；ERROR %d；WARN %d" % (
        len(stats["files"]), stats["total_lines"], stats["matched_lines"],
        stats["levels"].get("ERROR", 0), stats["levels"].get("WARN", 0)))
    if stats["first_ts"] is not None:
        parts.append("时间跨度：%s → %s" % (_fmt_dt(stats["first_ts"]), _fmt_dt(stats["last_ts"])))
    parts.append("识别格式：%s" % ", ".join(
        "%s×%d" % (FORMAT_LABELS.get(k, k), v) for k, v in stats["formats"].most_common()))
    if options["grep"] is not None:
        parts.append("行过滤正则：%s" % options["grep_text"])
    parts.append("")
    parts.append("## 级别分布")
    parts.append(", ".join("%s=%d" % (name, stats["levels"].get(name, 0)) for name in LEVEL_NAMES))
    parts.append("")

    parts.append("## Top 错误签名（按次数降序）")
    for index, (signature, info) in enumerate(top_signatures(stats, 15), 1):
        parts.append("%d. [%s ×%d] %s" % (index, info["level"], info["count"], signature))
        parts.append("   首次 %s / 末次 %s / 位置 %s:%d" % (
            _fmt_dt(info["first_ts"]), _fmt_dt(info["last_ts"]), info["file"], info["line"]))
        parts.append("   样例：%s" % info["sample"].replace("\n", " ⏎ ")[:MAX_SAMPLE_CHARS])
    parts.append("")

    buckets = top_buckets(stats, 5)
    parts.append("## 错误爆发时间桶（相对峰值）")
    if buckets:
        for start, count, ratio in buckets:
            parts.append("- %s：%d 条（峰值的 %.0f%%）" % (_bucket_label(start), count, ratio))
    else:
        parts.append("- 无时间戳，跳过")
    parts.append("")

    nginx = stats["nginx"]
    if nginx["total"]:
        parts.append("## 访问日志（nginx/apache）")
        parts.append("请求 %d，4xx+5xx %d，错误率 %.2f%%" % (
            nginx["total"], nginx["errors"],
            nginx["errors"] * 100.0 / nginx["total"]))
        parts.append("状态码分布：%s" % ", ".join(
            "%d×%d" % (s, c) for s, c in nginx["status"].most_common(10)))
        if nginx["slow"]:
            parts.append("最慢请求：")
            for rt, method, path, file_label, lineno in sorted(nginx["slow"], reverse=True)[:5]:
                parts.append("- %.3fs %s %s (%s:%d)" % (rt, method, path, file_label, lineno))
        parts.append("")

    if stats["notes"]:
        parts.append("## 统计口径提示")
        for note in stats["notes"]:
            parts.append("- %s" % note)
        parts.append("")

    parts.append("## 原始文件清单")
    for info in stats["files"]:
        parts.append("- %s（%s，%d 行）" % (info["path"], info["kind"], info["lines"]))

    text = "\n".join(parts)
    if len(text) > MAX_MODEL_CHARS:
        text = text[:MAX_MODEL_CHARS - 40] + "\n……（统计摘要过长，已截断）\n"
    return text


def build_report(stats, options, model_analysis, model_summary_sent=None):
    lines = []
    lines.append("# 日志排查报告")
    lines.append("")
    lines.append("生成时间：%s" % datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    lines.append("")
    lines.append("## 一、概览")
    lines.append("")
    lines.extend(render_overview(stats, options))
    lines.append("### 输入文件")
    lines.append("")
    lines.extend(render_file_table(stats))
    lines.append("## 二、级别分布")
    lines.append("")
    lines.extend(render_levels(stats))
    lines.append("## 三、Top 错误签名")
    lines.append("")
    lines.extend(render_signatures(stats, options["top"]))
    lines.append("## 四、错误时间分布")
    lines.append("")
    lines.extend(render_buckets(stats))
    nginx_section = render_nginx(stats)
    if nginx_section:
        lines.append("## 五、访问日志（nginx/apache）")
        lines.append("")
        lines.extend(nginx_section)
        timeline_title = "## 六、跨文件时间线"
    else:
        timeline_title = "## 五、跨文件时间线"
    lines.append(timeline_title)
    lines.append("")
    lines.extend(render_timeline(stats))
    lines.append("## 七、模型分析" if nginx_section else "## 六、模型分析")
    lines.append("")
    if model_analysis:
        lines.append(model_analysis.strip())
    else:
        lines.append("（本次使用 --stats-only，跳过了模型分析。统计结果不受影响。）")
    lines.append("")
    if stats["notes"]:
        lines.append("---")
        lines.append("")
        lines.append("### 统计口径与已知限制")
        lines.append("")
        for note in stats["notes"]:
            lines.append("- %s" % note)
        lines.append("")
    if model_summary_sent:
        lines.append("---")
        lines.append("")
        lines.append("> 发给模型的是上面这份统计摘要（%d 字符，不含日志全文）。" % model_summary_sent)
        lines.append("")
    return "\n".join(lines)


def write_report(path, content):
    target = Path(path).expanduser()
    if target.parent and str(target.parent) not in ("", "."):
        target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return target


# ---------------- 终端摘要 ----------------


def print_summary(stats, options):
    print("=" * 60)
    print("扫描完成：%d 个文件，读入 %d 行，参与统计 %d 行" % (
        len(stats["files"]), stats["total_lines"], stats["matched_lines"]))
    print("级别：ERROR %d / WARN %d / INFO %d / DEBUG %d / UNKNOWN %d" % (
        stats["levels"].get("ERROR", 0), stats["levels"].get("WARN", 0),
        stats["levels"].get("INFO", 0), stats["levels"].get("DEBUG", 0),
        stats["levels"].get("UNKNOWN", 0)))
    if stats["first_ts"] is not None:
        print("时间跨度：%s → %s" % (_fmt_dt(stats["first_ts"]), _fmt_dt(stats["last_ts"])))
    print("错误签名：%d 种" % len(stats["signatures"]))
    nginx = stats["nginx"]
    if nginx["total"]:
        print("访问日志：%d 请求，错误率 %.2f%%，最慢 %.3fs" % (
            nginx["total"], nginx["errors"] * 100.0 / nginx["total"],
            max(nginx["slow"])[0] if nginx["slow"] else 0.0))
    for note in stats["notes"]:
        print("提示：%s" % note)
    print("-" * 60)
    print("Top 错误签名（最多 10 条）：")
    items = top_signatures(stats, 10)
    if not items:
        print("  （没有识别到错误/告警行）")
    for index, (signature, info) in enumerate(items, 1):
        print("  %2d. [%s ×%d] %s" % (index, info["level"], info["count"], signature[:120]))
        print("      位置 %s:%d，首次 %s" % (
            info["file"], info["line"], _fmt_dt(info["first_ts"])))
    print("=" * 60)


# ---------------- 模型分析 ----------------

SYSTEM_PROMPT = """你是资深 SRE / 后端工程师，擅长从日志统计里定位线上故障根因。
你拿到的是一份由确定性脚本聚合出的日志统计摘要（不是原始全文），可能有多文件混合、
多日志格式、部分行被截断。请基于这些统计做判断，不要凭空编造摘要里没有的事实；
不确定的地方明确指出不确定性。

必须严格按下面四个 Markdown 小节输出，不要写前言和客套话：
### 1. 最可能的根因假设
按可能性从高到低列出（最多 4 条），每条一行标题 + 「依据：」说明它对应摘要里的哪些
签名、时间桶、状态码或时间线证据。

### 2. 关键证据
引用摘要里的日志原文/签名（用反引号包住），说明它能支持什么结论。

### 3. 下一步排查命令
给出可直接复制执行的命令，分「Linux/macOS」与「PowerShell」两小段，用 ```bash 和
```powershell 代码块。命令要针对本次统计里的实际文件名、关键词、时间范围，不要写
泛泛的占位符。

### 4. 还需要补充什么信息
列出为了确诊还需要哪些日志、指标或配置（最多 5 条）。
"""


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


def extract_text(response):
    chunks = []
    for block in getattr(response, "content", []) or []:
        if getattr(block, "type", "") == "text":
            chunks.append(block.text)
    return "".join(chunks).strip()


def run_model_analysis(stats, options):
    """返回 (分析 Markdown, 发给模型的字符数)；调用失败抛给上层处理"""
    summary = build_model_summary(stats, options)
    client = anthropic.Anthropic()
    user_content = (
        "请分析下面这份日志统计摘要，给出根因假设与排查命令。\n\n"
        "```text\n%s\n```\n" % summary
    )
    response = call_model(
        client,
        options["model"],
        SYSTEM_PROMPT,
        [{"role": "user", "content": user_content}],
    )
    text = extract_text(response)
    if not text:
        text = "（模型没有返回文本内容）"
    return text, len(summary)


# ---------------- 自检（不联网、不需要 API key） ----------------


APP_LOG = """2026-10-20 12:00:01,123 INFO app.start: starting service
2026-10-20 12:00:02,456 DEBUG app.db: connect attempts=3
2026-10-20 12:00:05,789 WARNING app.cache: cache miss ratio 0.42
2026-10-20 12:00:10,000 ERROR app.api: request failed: user_id=12345 code=500
Traceback (most recent call last):
  File "/srv/app/api.py", line 42, in handler
  RuntimeError: upstream timeout after 30s
2026-10-20 12:01:11,250 ERROR app.api: request failed: user_id=67890 code=500
2026-10-20 12:02:00,000 INFO app.start: health ok
"""

ISO_LOG = """2026-10-20T12:00:00.500Z INFO started
2026-10-20T20:00:01.000+08:00 ERROR payment failed order=8f3c9b1e-2a44-4c11-9f1d-1a2b3c4d5e6f status=502
2026-10-20T20:00:02.000+08:00 WARN retry #2
"""

JAVA_LOG = """12:00:01.123 [thread-1] ERROR com.x.Y - NullPointerException at OrderService.java:88
12:00:03.500 [thread-2] INFO com.x.Z - order loaded in 12ms
12:00:04.000 [thread-1] WARN com.x.Y - slow query 1500ms
"""

ACCESS_LOG = """1.2.3.4 - - [20/Oct/2026:12:00:01 +0800] "GET /api/users/12345 HTTP/1.1" 500 123 0.832
1.2.3.4 - - [20/Oct/2026:12:00:02 +0800] "GET /api/orders HTTP/1.1" 200 512 0.031
5.6.7.8 - - [20/Oct/2026:12:00:03 +0800] "POST /api/orders HTTP/1.1" 503 0 2.140
5.6.7.8 - - [20/Oct/2026:12:00:04 +0800] "GET /static/app.js HTTP/1.1" 304 0 0.005
5.6.7.8 - - [20/Oct/2026:12:00:05 +0800] "GET /api/missing HTTP/1.1" 404 9 0.010
"""

JSON_LOG = """{"time":"2026-10-20T12:00:01Z","level":"error","msg":"db connection refused host=10.0.0.5","service":"orders"}
{"time":"2026-10-20T12:00:02Z","level":"info","msg":"request handled","service":"orders"}
{"time":"2026-10-20T12:00:03Z","level":"warn","msg":"retry scheduled attempt=3","service":"gateway"}
"""

PLAIN_LOG = """just a normal line without level
Something bad happened: ERROR while flushing buffer
CRITICAL: disk full at /var/log/app
unrelated info here
"""

GZ_LOG = """2026-10-21 03:00:00,001 ERROR rotated.old: legacy failure code=17
2026-10-21 03:00:01,002 INFO rotated.old: done
"""


def _selftest_options(**overrides):
    options = {
        "level_min_rank": 0,
        "grep": None,
        "grep_text": "",
        "max_lines": MAX_LINES_DEFAULT,
        "top": 15,
        "bucket_seconds": 60,
        "since": None,
        "until": None,
        "model": DEFAULT_MODEL,
        "stats_only": True,
    }
    options.update(overrides)
    return options


def self_test():
    """在临时目录造多种格式日志，跑通全部确定性逻辑"""
    checks = []

    def check(name, condition, detail=""):
        if not condition:
            raise AssertionError("检查项「%s」不通过 %s" % (name, detail))
        checks.append(name)

    with tempfile.TemporaryDirectory(prefix="log-detective-selftest-") as tmp:
        tmpdir = Path(tmp)
        files = {
            "app.log": (APP_LOG, None),
            "iso.log": (ISO_LOG, None),
            "java.log": (JAVA_LOG, None),
            "access.log": (ACCESS_LOG, None),
            "events.jsonl": (JSON_LOG, None),
            "plain.log": (PLAIN_LOG, None),
            "app.old.log.gz": (GZ_LOG, "gz"),
            "gbk.log": (None, "gbk"),
        }
        for name, (content, kind) in files.items():
            target = tmpdir / name
            if kind == "gz":
                with gzip.open(str(target), "wb") as fh:
                    fh.write(content.encode("utf-8"))
            elif kind == "gbk":
                gbk_text = "2026-10-20 12:00:09,000 ERROR gbk.mod: 中文乱码测试\n"
                target.write_bytes(gbk_text.encode("gbk"))
            else:
                target.write_text(content, encoding="utf-8")

        paths = [str(tmpdir / name) for name in files]

        # 1) 基础聚合
        stats = scan_paths(paths, _selftest_options())
        check("总行数", stats["total_lines"] == 30, "got %d" % stats["total_lines"])
        expected_levels = {"ERROR": 12, "WARN": 5, "INFO": 8, "DEBUG": 1}
        actual_levels = {k: stats["levels"].get(k, 0) for k in expected_levels}
        check("级别计数", actual_levels == expected_levels, str(actual_levels))
        check("参与统计行数", stats["matched_lines"] == 26, "got %d" % stats["matched_lines"])
        for fmt in ("python", "iso", "log4j", "access", "json", "plain"):
            check("识别格式 %s" % fmt, stats["formats"].get(fmt, 0) > 0)
        check("文件数", len(stats["files"]) == 8)
        check("gz 识别", any(f["kind"] == "gzip" for f in stats["files"]))
        check("gbk 兜底", any(f["encoding"] == "gbk" for f in stats["files"]))
        check("时间跨度", stats["first_ts"] is not None and stats["last_ts"] > stats["first_ts"])

        # 2) 签名归一化：只有 user_id 不同的两行应聚成同一签名
        merged = [v for k, v in stats["signatures"].items() if "request failed" in k]
        check("签名归一化聚合", any(v["count"] == 2 for v in merged), str([v["count"] for v in merged]))
        check("数字归一化", all("<num>" in k for k in
                                [sig for sig, _ in stats["signatures"].items() if "request failed" in sig]))
        norm_a = normalize_message("order 12345 user 10.0.0.5 id "
                                   "8f3c9b1e-2a44-4c11-9f1d-1a2b3c4d5e6f at 2026-10-20 12:00:01,123")
        norm_b = normalize_message("order 999 user 10.0.0.9 id "
                                   "11111111-2222-3333-4444-555555555555 at 2026-11-01 08:09:10,456")
        check("归一化结果一致", norm_a == norm_b, "%r != %r" % (norm_a, norm_b))
        for placeholder in ("<num>", "<ip>", "<uuid>", "<ts>"):
            check("占位符 %s" % placeholder, placeholder in norm_a, norm_a)

        # 3) 时间分桶
        buckets = top_buckets(stats, 5)
        check("时间分桶有结果", bool(buckets))
        check("分桶相对峰值", buckets[0][2] == 100.0)
        check("分桶时间正确", _bucket_label(buckets[0][0]).startswith("2026-10-20 12:0"))

        # 4) nginx 专有统计
        nginx = stats["nginx"]
        check("nginx 请求数", nginx["total"] == 5, "got %d" % nginx["total"])
        check("nginx 错误数", nginx["errors"] == 3, "got %d" % nginx["errors"])
        check("nginx 状态码分布", nginx["status"][500] == 1 and nginx["status"][503] == 1)
        check("最慢请求", abs(max(nginx["slow"])[0] - 2.140) < 1e-9)
        check("慢请求上限", len(nginx["slow"]) <= MAX_SLOW_REQUESTS)

        # 5) 时间线：最多 50 条且按时间正序
        timeline = sorted(stats["timeline"], reverse=True)[:MAX_TIMELINE]
        check("时间线上限", len(timeline) <= MAX_TIMELINE)
        epochs = [-item[0] for item in timeline]
        check("时间线有序", epochs == sorted(epochs))

        # 6) 行数上限
        truncated = scan_paths([str(tmpdir / "app.log")],
                              _selftest_options(max_lines=3))
        check("max-lines 截断", truncated["total_lines"] == 3)
        check("截断标记", truncated["truncated"] is True)
        check("截断提示", any("已截断" in note for note in truncated["notes"]))

        # 7) level-min 过滤
        errors_only = scan_paths(paths, _selftest_options(level_min_rank=LEVEL_RANK["ERROR"]))
        check("level-min 只留 ERROR", errors_only["levels"].get("WARN", 0) == 0
              and errors_only["levels"].get("INFO", 0) == 0)
        check("level-min 计数不变", errors_only["levels"].get("ERROR", 0) == 12)

        # 8) grep 过滤
        payment = scan_paths(paths, _selftest_options(grep=re.compile("payment")))
        check("grep 过滤", payment["total_lines"] == 30
              and payment["levels"].get("ERROR", 0) == 1)

        # 9) since / until 过滤（只对有时间戳的行生效；无时间戳的行按设计保留）
        since_day = scan_paths(
            [str(tmpdir / "iso.log"), str(tmpdir / "access.log"),
             str(tmpdir / "app.old.log.gz")],
            _selftest_options(since=parse_bound("2026-10-21")),
        )
        check("since 过滤", since_day["levels"].get("ERROR", 0) == 1
              and since_day["levels"].get("INFO", 0) == 1, str(since_day["levels"]))
        no_ts = scan_paths([str(tmpdir / "plain.log")],
                           _selftest_options(since=parse_bound("2026-10-21")))
        check("无时间戳行保留", no_ts["levels"].get("ERROR", 0) == 2, str(no_ts["levels"]))
        only_gz = scan_paths([str(tmpdir / "app.old.log.gz")], _selftest_options(
            since=parse_bound("2026-10-21 00:00:00"), until=parse_bound("2026-10-21 23:59:59")))
        check("until 过滤", only_gz["levels"].get("ERROR", 0) == 1)

        # 10) 模型摘要长度与报告生成
        summary = build_model_summary(stats, _selftest_options())
        check("模型摘要长度", 0 < len(summary) <= MAX_MODEL_CHARS, "len=%d" % len(summary))
        check("模型摘要含签名", "request failed" in summary or "<num>" in summary)
        report = build_report(stats, _selftest_options(), "### 1. 根因假设\n测试用模型分析。", len(summary))
        for heading in ("# 日志排查报告", "## 一、概览", "## 二、级别分布",
                        "## 三、Top 错误签名", "## 四、错误时间分布",
                        "## 五、访问日志", "## 六、跨文件时间线", "## 七、模型分析"):
            check("报告含 %s" % heading, heading in report)
        report_no_model = build_report(stats, _selftest_options(), "")
        check("stats-only 报告", "跳过" in report_no_model)
        out_path = tmpdir / "sub" / "report.md"
        written = write_report(str(out_path), report)
        check("报告落盘", written.is_file() and written.stat().st_size > 1000,
              "size=%d" % (written.stat().st_size if written.is_file() else 0))

        # 11) 时区忽略、log4j 补日期、边界解析
        check("时区忽略", parse_iso_ts("2026-10-20T20:00:01+08:00")
              == datetime.datetime(2026, 10, 20, 20, 0, 1))
        check("分桶粒度解析", parse_bucket("5m") == 300 and parse_bucket("1h") == 3600)
        check("时间边界解析", parse_bound("2026-10-20 08:00") ==
              datetime.datetime(2026, 10, 20, 8, 0))
        check("log4j 补日期", stats["date_from_mtime"] >= 3, str(stats["date_from_mtime"]))

    print("已通过 %d 项检查：" % len(checks))
    for name in checks:
        print("  [OK] %s" % name)
    print("SELF-TEST OK")


# ---------------- 入口 ----------------


def build_parser():
    parser = argparse.ArgumentParser(
        description="日志排查助手：流式扫描日志 → 确定性聚合统计 → Claude 分析根因",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            "  py -3 agent.py app.log --stats-only\n"
            "  py -3 agent.py app.log access.log --level-min ERROR --top 20\n"
            "  py -3 agent.py app.log.gz --since 2026-10-20 --output 排查报告.md\n"
            "  py -3 agent.py --self-test\n"
        ),
    )
    parser.add_argument("logs", nargs="*", help="日志文件路径（支持 .gz，可给多个）")
    parser.add_argument(
        "--level-min", default=None,
        choices=["ERROR", "WARN", "INFO", "DEBUG"],
        help="只统计不低于该级别的行（默认全部）",
    )
    parser.add_argument("--grep", default=None, help="只处理匹配该正则的行（Python 正则）")
    parser.add_argument(
        "--max-lines", type=int, default=MAX_LINES_DEFAULT,
        help="最多扫描多少行（默认 %d）" % MAX_LINES_DEFAULT,
    )
    parser.add_argument("--top", type=int, default=15, help="展示前 N 条错误签名（默认 15）")
    parser.add_argument(
        "--bucket", default="1m",
        help="错误时间分桶粒度，如 1m、5m、1h（默认 1m）",
    )
    parser.add_argument("--since", default=None, help="起始时间，YYYY-MM-DD 或 YYYY-MM-DD HH:MM:SS")
    parser.add_argument("--until", default=None, help="结束时间，同上")
    parser.add_argument("--output", default="log-report.md", help="报告输出路径（默认 log-report.md）")
    parser.add_argument("--stats-only", action="store_true", help="只出统计，不调用模型（不花 token）")
    parser.add_argument(
        "--model", default=DEFAULT_MODEL,
        help="模型名（默认 %s，走网关可换 qwen3.8-max）" % DEFAULT_MODEL,
    )
    parser.add_argument("--self-test", action="store_true", help="跑内置自检（不联网、不需要 API key）")
    return parser


def configure_output():
    """输出被重定向/管道捕获时用 utf-8，避免 Windows 控制台编码把中文弄成乱码；
    真实终端里保持控制台本身的编码，中文照常显示。"""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            if not stream.isatty():
                reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            pass


def main():
    configure_output()
    parser = build_parser()
    args = parser.parse_args()

    if args.self_test:
        try:
            self_test()
        except AssertionError as e:
            print("自检失败：%s" % e, file=sys.stderr)
            sys.exit(1)
        return

    if not args.logs:
        parser.error("请至少给一个日志文件路径（或用 --self-test 跑自检）")

    # 参数前置校验，错误直接给中文提示
    try:
        bucket_seconds = parse_bucket(args.bucket)
        since = parse_bound(args.since) if args.since else None
        until = parse_bound(args.until) if args.until else None
    except ValueError as e:
        print("错误：%s" % e, file=sys.stderr)
        sys.exit(1)
    if args.top <= 0:
        print("错误：--top 必须大于 0", file=sys.stderr)
        sys.exit(1)
    if args.max_lines <= 0:
        print("错误：--max-lines 必须大于 0", file=sys.stderr)
        sys.exit(1)
    if since and until and since > until:
        print("错误：--since 不能晚于 --until", file=sys.stderr)
        sys.exit(1)

    try:
        grep_rx = re.compile(args.grep) if args.grep else None
    except re.error as e:
        print("错误：--grep 正则表达式有误：%s" % e, file=sys.stderr)
        sys.exit(1)

    paths = []
    for raw_path in args.logs:
        p = Path(raw_path).expanduser()
        if not p.is_file():
            print("错误：文件不存在或不是普通文件：%s" % raw_path, file=sys.stderr)
            sys.exit(1)
        paths.append(str(p))

    options = {
        "level_min_rank": LEVEL_RANK[args.level_min] if args.level_min else 0,
        "grep": grep_rx,
        "grep_text": args.grep or "",
        "max_lines": args.max_lines,
        "top": args.top,
        "bucket_seconds": bucket_seconds,
        "since": since,
        "until": until,
        "model": args.model,
        "stats_only": args.stats_only,
    }

    print("日志排查助手：共 %d 个文件，模型 %s" % (len(paths), args.model))
    print("扫描中……（逐行流式读取，上限 %d 行）" % args.max_lines)
    stats = scan_paths(paths, options)

    model_analysis = ""
    model_summary_len = 0
    model_error = None
    if not args.stats_only:
        print("统计分析完成，正在请求模型分析（统计摘要 %d 字符）……" % len(
            build_model_summary(stats, options)))
        try:
            model_analysis, model_summary_len = run_model_analysis(stats, options)
        except anthropic.AuthenticationError:
            model_error = "API key 无效或没有权限，请检查 ANTHROPIC_API_KEY。"
        except anthropic.APIConnectionError:
            model_error = "无法连接 Anthropic API，请检查网络或代理设置。"
        except anthropic.RateLimitError:
            model_error = "请求太频繁被限流，请稍后再试（或用 --stats-only 先看统计）。"
        except anthropic.APIStatusError as e:
            model_error = "API 返回错误（%s）：%s" % (e.status_code, e.message)
        except Exception as e:  # 兜底：任何意外都不能丢掉统计结果
            model_error = "模型调用出现意外错误：%s" % e
        if model_error:
            model_analysis = (
                "**模型分析不可用**：%s\n\n"
                "上面的统计结果不受影响，可以先按 Top 错误签名和时间分布自行排查。" % model_error
            )

    report = build_report(stats, options, model_analysis, model_summary_len)
    try:
        written = write_report(args.output, report)
    except OSError as e:
        print("错误：报告写入失败（%s）：%s" % (args.output, e), file=sys.stderr)
        sys.exit(1)

    print_summary(stats, options)
    if model_error:
        print("警告：模型分析失败，报告里已标注「模型分析不可用」。", file=sys.stderr)
    print("报告已写入：%s（%d 字符）" % (written, len(report)))
    print("提示：加 --stats-only 可以只出统计、不花 token。")

    if model_error:
        # 统计结果和报告都已落盘，再按约定以非零码提示模型侧失败
        print("错误：%s" % model_error, file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
