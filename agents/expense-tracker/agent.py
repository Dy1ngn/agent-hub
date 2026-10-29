#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
自然语言记账与开销分析助手（expense-tracker）

用一句话记一笔账（例如“午饭 35 元”），确定性规则自动解析金额、日期、分类、
商户摘要和支付方式，写入本地 JSON 账本（原子写、自动去重）；再按日/周/月/年做
统计，输出带 ASCII 图表的中文 Markdown 开销报告（含预算对比与异常消费提示）。
除 anthropic 外只用标准库。

用法：
    python3 agent.py add "午饭 35 元" "打车￥45.5 昨天 支付宝" --book 账本.json
    python3 agent.py report --month 2026-10 --budget 预算.csv --output 月报.md
    python3 agent.py list --category 餐饮
    python3 agent.py export --csv 账本.csv
    python3 agent.py add --file 记账文本.txt        # 每行一条，批量导入

环境要求：
    1. 只有使用 --ask-model（让模型归一化模糊记账）时才需要 API key：
           export ANTHROPIC_API_KEY=你的key        # macOS / Linux
           $env:ANTHROPIC_API_KEY="你的key"         # Windows PowerShell
    2. 安装依赖：pip3 install -r requirements.txt
    3. 走第三方 Anthropic 兼容网关时，用 --model 指定网关认得的模型名，例如：
           python3 agent.py add --file 明细.txt --ask-model --model qwen3.8-max
       （--offline 与 --self-test 完全离线，不需要 key、不需要网络）
"""

import argparse
import csv
import hashlib
import json
import os
import re
import sys
import tempfile
from collections import OrderedDict
from datetime import date, datetime, timedelta

import anthropic

# 默认模型（可用 --model 参数或 EXPENSE_TRACKER_MODEL 环境变量更换）
DEFAULT_MODEL = os.environ.get("EXPENSE_TRACKER_MODEL", "claude-opus-5")

DEFAULT_BOOK = "账本.json"
DEFAULT_REPORT = "expense-report.md"
CHART_WIDTH = 16          # ASCII 柱状图的条形宽度（格数）
TOP_MERCHANTS = 5         # 报告里展示多少个高频商户

# ---------- 分类词典（每类 20+ 关键词，按顺序匹配，先命中先得） ----------

CATEGORY_ORDER = ["餐饮", "交通", "购物", "居住", "娱乐", "医疗", "教育", "人情", "其他"]

CATEGORY_KEYWORDS = OrderedDict([
    ("餐饮", [
        "早饭", "早餐", "午饭", "午餐", "晚饭", "晚餐", "夜宵", "外卖", "快餐",
        "餐厅", "饭店", "食堂", "小吃", "火锅", "烧烤", "咖啡", "奶茶", "饮品",
        "甜点", "蛋糕", "面包", "零食", "水果", "买菜", "聚餐", "便当", "盒饭",
        "自助餐", "日料", "西餐", "面馆", "麻辣烫", "下午茶", "便利店",
    ]),
    ("交通", [
        "地铁", "公交", "打车", "出租车", "滴滴", "网约车", "加油", "汽油",
        "停车", "过路费", "高铁", "火车", "机票", "飞机", "汽车票", "船票",
        "骑行", "单车", "共享单车", "车票", "交通卡", "etc", "ETC", "年检",
        "保养", "修车", "违章", "顺风车", "大巴", "长途车", "轮渡",
    ]),
    ("购物", [
        "衣服", "服饰", "鞋子", "鞋", "背包", "包包", "化妆品", "护肤", "美妆",
        "电子产品", "手机", "电脑", "家电", "超市", "日用品", "洗漱用品", "洗护",
        "京东", "淘宝", "拼多多", "网购", "电商", "商场", "专柜", "数码",
        "配件", "家具", "家居", "眼镜", "首饰", "母婴", "玩具", "代购", "批发",
    ]),
    ("居住", [
        "房租", "水费", "电费", "燃气", "燃气费", "物业", "物业费", "宽带",
        "网费", "家政", "维修", "装修", "房贷", "供暖", "取暖", "空调",
        "清洁", "纸巾", "垃圾", "保洁", "搬家", "门锁", "押金", "水电",
    ]),
    ("娱乐", [
        "电影", "演唱会", "游戏", "会员", "视频会员", "音乐", "健身", "瑜伽",
        "旅游", "门票", "景点", "酒店", "住宿", "KTV", "剧本杀", "密室",
        "展览", "漫展", "运动", "球场", "游泳", "滑雪", "网吧", "手办",
        "盲盒", "派对", "桌游", "演出", "剧院", "游乐园",
    ]),
    ("医疗", [
        "医院", "药店", "买药", "挂号", "门诊", "体检", "牙科", "眼科",
        "理疗", "按摩", "推拿", "疫苗", "住院", "手术", "检查费", "配镜",
        "隐形眼镜", "营养品", "维生素", "中医", "拔罐", "康复", "器械", "药品",
    ]),
    ("教育", [
        "学费", "书籍", "教材", "课程", "网课", "培训", "考试", "报名费",
        "文具", "补习", "家教", "讲座", "考研", "留学", "语言班", "电子书",
        "论文", "教辅", "网校", "认证", "习题", "知识付费", "学习", "书店",
    ]),
    ("人情", [
        "红包", "礼物", "礼金", "随份子", "份子钱", "婚礼", "生日", "聚会",
        "请客", "孝敬", "慈善", "捐款", "压岁钱", "伴手礼", "满月", "乔迁",
        "探望", "慰问", "送礼", "人情", "答谢", "答谢宴", "祝寿", "压岁",
    ]),
])

# ---------- 支付方式关键词 ----------

PAY_KEYWORDS = [
    ("微信支付", "微信"), ("微信", "微信"), ("支付宝", "支付宝"),
    ("花呗", "花呗"), ("白条", "白条"), ("云闪付", "云闪付"),
    ("信用卡", "信用卡"), ("储蓄卡", "储蓄卡"), ("银行卡", "银行卡"),
    ("刷卡", "银行卡"), ("现金", "现金"), ("纸币", "现金"),
]

# ---------- 解析正则 ----------

AMOUNT_PATTERNS = [
    (re.compile(r"[¥￥]\s*(\d+(?:\.\d+)?)"), "人民币"),
    (re.compile(r"\$\s*(\d+(?:\.\d+)?)"), "美元"),
    (re.compile(r"(\d+(?:\.\d+)?)\s*(?:元|块|块钱|圆)"), "人民币"),
    (re.compile(r"(\d+(?:\.\d+)?)\s*(?:人民币|RMB|rmb)"), "人民币"),
    (re.compile(r"(\d+(?:\.\d+)?)\s*(?:美元|美金|刀)"), "美元"),
]
# 兜底：既不属于日期也不带单位的裸数字
BARE_AMOUNT_RE = re.compile(
    r"(?<![\d.:/\-月])(\d+(?:\.\d+)?)(?![\d.:/\-月日号点分年倍折])"
)
CN_NUM_RE = re.compile(r"[零〇一二三四五六七八九十百千两]+\s*(?:元|块|圆)")
ABS_DATE_RE = re.compile(r"(\d{4})[-/年.](\d{1,2})[-/月.](\d{1,2})日?")
CN_DATE_RE = re.compile(r"(\d{1,2})月(\d{1,2})[日号]?")
DAY_RE = re.compile(r"(\d{1,2})[号日]")
WEEKDAY_RE = re.compile(r"(?:周|星期)([一二三四五六日天])")
TODAY_RE = re.compile(r"今天|今日")
YESTERDAY_RE = re.compile(r"昨天|昨日")
DAY_BEFORE_RE = re.compile(r"前天")

WEEKDAY_MAP = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6}


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


# ---------- 自然语言解析 ----------

def parse_amount(text):
    """解析金额，返回 (金额或 None, 命中片段或 None, 提示或 None)。"""
    for pattern, _currency in AMOUNT_PATTERNS:
        matched = pattern.search(text)
        if matched:
            return float(matched.group(1)), matched.group(0), None
    bare = BARE_AMOUNT_RE.search(text)
    if bare:
        return (
            float(bare.group(1)),
            bare.group(0),
            "金额未带单位，已按「%s 元」记账，请确认" % bare.group(1),
        )
    if CN_NUM_RE.search(text):
        return None, None, "暂不支持中文数字金额，请改用阿拉伯数字（如 35 元）"
    return None, None, "未能解析出金额，请补上数字（如 35 元）"


def _weekday_before(today, target):
    """最近一个不晚于今天的星期 X（今天就是该星期几时返回今天）。"""
    delta = (today.weekday() - target) % 7
    return today - timedelta(days=delta)


def _recent_day(today, day):
    """最近一个不晚于今天的「X 号」。"""
    for month_offset in (0, -1):
        year, month = today.year, today.month + month_offset
        while month <= 0:
            month += 12
            year -= 1
        try:
            candidate = date(year, month, day)
        except ValueError:
            continue
        if candidate <= today:
            return candidate
    return None


def parse_date(text, today=None):
    """解析日期，返回 (date 或 None, 命中片段或 None, 提示或 None)。"""
    today = today or date.today()

    matched = ABS_DATE_RE.search(text)
    if matched:
        try:
            return date(int(matched.group(1)), int(matched.group(2)), int(matched.group(3))), matched.group(0), None
        except ValueError:
            return None, matched.group(0), "日期「%s」不合法，请用 2026-10-27 这种写法" % matched.group(0)

    matched = CN_DATE_RE.search(text)
    if matched:
        month, day = int(matched.group(1)), int(matched.group(2))
        year = today.year
        try:
            candidate = date(year, month, day)
        except ValueError:
            return None, matched.group(0), "日期「%s」不合法" % matched.group(0)
        if candidate > today:
            candidate = date(year - 1, month, day)
        return candidate, matched.group(0), None

    matched = TODAY_RE.search(text)
    if matched:
        return today, matched.group(0), None
    matched = DAY_BEFORE_RE.search(text)
    if matched:
        return today - timedelta(days=2), matched.group(0), None
    matched = YESTERDAY_RE.search(text)
    if matched:
        return today - timedelta(days=1), matched.group(0), None

    matched = WEEKDAY_RE.search(text)
    if matched:
        target = WEEKDAY_MAP.get(matched.group(1))
        if target is not None:
            return _weekday_before(today, target), matched.group(0), None

    matched = DAY_RE.search(text)
    if matched:
        candidate = _recent_day(today, int(matched.group(1)))
        if candidate:
            return candidate, matched.group(0), None
        return None, matched.group(0), "日期「%s」不合法" % matched.group(0)

    return None, None, "未识别到日期，按今天（%s）记账" % today.isoformat()


def parse_pay_method(text):
    """解析支付方式，返回 (规范名或 None, 命中片段或 None)。"""
    for keyword, canonical in PAY_KEYWORDS:
        matched = re.search(re.escape(keyword), text)
        if matched:
            return canonical, matched.group(0)
    return None, None


def match_category(text):
    """按词典给文本分类，返回 (分类或 None, 命中关键词或 None)。"""
    lowered = text.lower()
    for category in CATEGORY_ORDER[:-1]:
        for keyword in CATEGORY_KEYWORDS[category]:
            if keyword.lower() in lowered:
                return category, keyword
    return None, None


def parse_entry(text, today=None):
    """把一条自然语言记账解析成结构化记录（解析不出的字段为 None 并给提示）。"""
    today = today or date.today()
    raw = text.strip()
    hints = []
    work = raw

    parsed_date, date_text, date_hint = parse_date(work, today)
    if date_hint:
        hints.append(date_hint)
    if date_text:
        work = work.replace(date_text, " ", 1)

    amount, amount_text, amount_hint = parse_amount(work)
    if amount_hint:
        hints.append(amount_hint)
    if amount_text:
        work = work.replace(amount_text, " ", 1)

    pay_method, pay_text = parse_pay_method(work)
    if pay_text:
        work = work.replace(pay_text, " ", 1)

    category, keyword = match_category(raw)

    merchant = re.sub(r"^[，,、。.;；:：\-\s]+", "", work)
    merchant = re.sub(r"\s+", " ", merchant).strip()
    merchant = re.sub(r"[，,、。.;；:：]+$", "", merchant).strip()

    if category is None:
        hints.append("未识别到分类关键词，已归入「其他」")
    if not merchant:
        hints.append("未识别到商户/摘要，用记账原文代替")

    return {
        "raw": raw,
        "amount": amount,
        "date": parsed_date.isoformat() if parsed_date else None,
        "category": category,
        "merchant": merchant or None,
        "pay_method": pay_method,
        "hints": hints,
    }


# ---------- 账本存储（JSON + 原子写） ----------

def entry_key(entry):
    """用于去重的业务键：同一天同一笔消费只记一次。"""
    return (
        entry.get("date", ""),
        round(float(entry.get("amount") or 0), 2),
        entry.get("category") or "",
        (entry.get("merchant") or "").strip(),
        entry.get("pay_method") or "",
        (entry.get("note") or "").strip(),
    )


def make_entry(parsed, today=None):
    """把解析结果变成账本记录（带 id 与时间戳）。"""
    today = today or date.today()
    entry = {
        "date": parsed.get("date") or today.isoformat(),
        "amount": round(float(parsed.get("amount") or 0), 2),
        "category": parsed.get("category") or "其他",
        "merchant": parsed.get("merchant") or parsed.get("raw") or "",
        "pay_method": parsed.get("pay_method"),
        "note": parsed.get("raw", ""),
        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    digest = hashlib.sha1("|".join(str(part) for part in entry_key(entry)).encode("utf-8")).hexdigest()
    entry["id"] = digest[:12]
    return entry


def load_book(path):
    """读账本，文件不存在时返回空账本。"""
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8") as handle:
        data = json.load(handle)
    if isinstance(data, dict):
        entries = data.get("entries", [])
    elif isinstance(data, list):
        entries = data
    else:
        entries = []
    return [item for item in entries if isinstance(item, dict)]


def save_book(path, entries):
    """原子写账本：先写临时文件再 os.replace，避免写一半损坏。"""
    payload = {
        "version": 1,
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "entries": entries,
    }
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    handle, temp_path = tempfile.mkstemp(dir=directory, suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2)
        os.replace(temp_path, path)
    except BaseException:
        if os.path.exists(temp_path):
            os.remove(temp_path)
        raise


def add_entries(book_path, parsed_list, today=None):
    """把解析结果去重后追加进账本，返回 (新增记录, 跳过的重复数, 无金额条数)。"""
    today = today or date.today()
    existing = load_book(book_path)
    seen = set(entry_key(entry) for entry in existing)
    added, skipped, no_amount = [], 0, 0
    for parsed in parsed_list:
        if parsed.get("amount") is None:
            no_amount += 1
            continue
        entry = make_entry(parsed, today)
        key = entry_key(entry)
        if key in seen:
            skipped += 1
            continue
        seen.add(key)
        added.append(entry)
    if added:
        save_book(book_path, existing + added)
    return added, skipped, no_amount


# ---------- 统计 ----------

def month_of(iso_date):
    return (iso_date or "")[:7]


def filter_month(entries, month):
    return [entry for entry in entries if month_of(entry.get("date", "")) == month]


def days_in_month(month):
    year, mon = int(month[:4]), int(month[5:7])
    if mon == 12:
        nxt = date(year + 1, 1, 1)
    else:
        nxt = date(year, mon + 1, 1)
    return (nxt - date(year, mon, 1)).days


def previous_month(month):
    year, mon = int(month[:4]), int(month[5:7])
    year, mon = (year - 1, 12) if mon == 1 else (year, mon - 1)
    return "%04d-%02d" % (year, mon)


def summarize(entries, period_days):
    """确定性统计：总额、笔数、分类、商户、日均、频次、最大单笔、每日分布。"""
    period_days = max(int(period_days or 1), 1)
    total = sum(float(entry.get("amount") or 0) for entry in entries)
    count = len(entries)

    by_category = OrderedDict()
    for category in CATEGORY_ORDER:
        by_category[category] = {"count": 0, "amount": 0.0}
    for entry in entries:
        bucket = by_category.setdefault(entry.get("category") or "其他", {"count": 0, "amount": 0.0})
        bucket["count"] += 1
        bucket["amount"] += float(entry.get("amount") or 0)

    merchant_totals = OrderedDict()
    for entry in entries:
        name = (entry.get("merchant") or "").strip() or "（未填）"
        merchant_totals[name] = merchant_totals.get(name, 0.0) + float(entry.get("amount") or 0)
    top_merchants = sorted(merchant_totals.items(), key=lambda kv: (-kv[1], kv[0]))[:TOP_MERCHANTS]

    daily = OrderedDict()
    for entry in sorted(entries, key=lambda item: item.get("date") or ""):
        day = entry.get("date") or "（无日期）"
        daily[day] = round(daily.get(day, 0.0) + float(entry.get("amount") or 0), 2)

    max_entry = None
    for entry in entries:
        if max_entry is None or float(entry.get("amount") or 0) > float(max_entry.get("amount") or 0):
            max_entry = entry

    return {
        "total": round(total, 2),
        "count": count,
        "by_category": by_category,
        "top_merchants": top_merchants,
        "daily": daily,
        "daily_avg": round(total / period_days, 2),
        "record_days": len(daily),
        "frequency": round(count / period_days, 2),
        "avg_per_record": round(total / count, 2) if count else 0.0,
        "max_entry": max_entry,
    }


def read_budget(path):
    """读预算表（CSV：category,limit；也接受 中文表头 类别,额度）。"""
    rows = []
    with open(path, "r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.reader(handle)
        header = next(reader, None)
        for line in reader:
            if not line or len(line) < 2:
                continue
            try:
                rows.append({"category": line[0].strip(), "limit": float(line[1])})
            except ValueError:
                continue
    return rows


def budget_check(by_category, budget_rows):
    """预算对比：返回每行 {分类,已花,预算,差额,是否超支}，超支排在前面。"""
    result = []
    for row in budget_rows:
        category = row["category"]
        spent = round(float(by_category.get(category, {}).get("amount", 0.0)), 2)
        limit = round(float(row["limit"]), 2)
        result.append({
            "category": category,
            "spent": spent,
            "limit": limit,
            "diff": round(limit - spent, 2),
            "over": spent > limit,
        })
    result.sort(key=lambda item: (not item["over"], item["category"]))
    return result


def detect_anomalies(summary, budget_rows):
    """确定性异常消费提示：超预算、超常大额、单日爆发。"""
    notes = []
    for row in budget_check(summary["by_category"], budget_rows):
        if row["over"]:
            notes.append("「%s」超预算：已花 %.2f 元，超出 %.2f 元" % (
                row["category"], row["spent"], row["spent"] - row["limit"]))
    if summary["count"] and summary["max_entry"]:
        avg = summary["avg_per_record"]
        biggest = summary["max_entry"]
        if avg > 0 and float(biggest.get("amount") or 0) > 3 * avg:
            notes.append("单笔大额：%s（%s）花了 %.2f 元，是平均单笔 %.2f 元的 %.1f 倍" % (
                biggest.get("merchant") or "（未填）", biggest.get("date") or "无日期",
                float(biggest.get("amount") or 0), avg, float(biggest.get("amount") or 0) / avg))
    for day, amount in summary["daily"].items():
        if summary["daily_avg"] > 0 and amount > 2 * summary["daily_avg"]:
            notes.append("%s 单日消费 %.2f 元，超过日均 %.2f 元的 2 倍" % (
                day, amount, summary["daily_avg"]))
    return notes


# ---------- ASCII 图表 ----------

def render_bar_chart(items, width=CHART_WIDTH):
    """水平柱状图：items 为 (标签, 数值, 附注) 列表。"""
    if not items:
        return "（没有数据）"
    max_value = max(float(value) for _label, value, _note in items) or 1.0
    label_width = max(len(str(label)) for label, _value, _note in items)
    lines = []
    for label, value, note in items:
        blocks = int(round(float(value) / max_value * width))
        lines.append("%s | %s %s" % (str(label).ljust(label_width), "█" * blocks, note))
    return "\n".join(lines)


def render_trend_chart(daily, width=CHART_WIDTH):
    """每日消费趋势柱状图。"""
    items = [(day, amount, "%.2f 元" % amount) for day, amount in daily.items()]
    return render_bar_chart(items, width)


# ---------- 报告 ----------

def _money(value):
    return "%.2f" % float(value or 0)


def build_report(summary, month, budget_rows, anomalies, model_note, raw_appendix,
                 entries, generated_at=None, prev_total=None):
    """把统计结果渲染成中文 Markdown 开销报告。"""
    generated_at = generated_at or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    lines = [
        "# 开销报告（%s）" % month,
        "",
        "生成时间：%s" % generated_at,
        "",
        "## 本月概览",
        "",
        "| 指标 | 数值 |",
        "| --- | --- |",
        "| 总支出 | %s 元 |" % _money(summary["total"]),
        "| 记账笔数 | %d 笔 |" % summary["count"],
        "| 有消费的天数 | %d 天 |" % summary["record_days"],
    ]
    if prev_total:
        change = (summary["total"] - prev_total) / prev_total * 100
        lines.append("| 环比上月 | %s 元（%+.2f%%） |" % (_money(prev_total), change))
    lines.extend([
        "| 日均消费 | %s 元 |" % _money(summary["daily_avg"]),
        "| 平均单笔 | %s 元 |" % _money(summary["avg_per_record"]),
        "| 消费频次 | %s 笔/天 |" % _money(summary["frequency"]),
    ])
    if summary["max_entry"]:
        biggest = summary["max_entry"]
        lines.append("| 最大单笔 | %s 元（%s，%s） |" % (
            _money(biggest.get("amount")), biggest.get("merchant") or "（未填）",
            biggest.get("date") or "无日期"))
    lines.append("")

    lines.extend(["## 分类占比", "", "| 分类 | 笔数 | 金额（元） | 占比 |", "| --- | --- | --- | --- |"])
    total = summary["total"] or 1.0
    for category in CATEGORY_ORDER:
        bucket = summary["by_category"].get(category)
        if not bucket or bucket["count"] == 0:
            continue
        lines.append("| %s | %d | %s | %.1f%% |" % (
            category, bucket["count"], _money(bucket["amount"]), bucket["amount"] / total * 100))
    lines.append("")

    category_items = []
    for category in CATEGORY_ORDER:
        bucket = summary["by_category"].get(category)
        if bucket and bucket["count"]:
            category_items.append((
                category, bucket["amount"],
                "%s 元 / %.1f%%" % (_money(bucket["amount"]), bucket["amount"] / total * 100),
            ))
    lines.extend(["### 分类柱状图", "", "```text", render_bar_chart(category_items), "```", ""])

    lines.extend(["## 每日趋势", "", "```text", render_trend_chart(summary["daily"]), "```", ""])

    lines.append("## 预算对比")
    lines.append("")
    if budget_rows:
        lines.extend(["| 分类 | 已花（元） | 预算（元） | 差额（元） | 状态 |", "| --- | --- | --- | --- | --- |"])
        for row in budget_check(summary["by_category"], budget_rows):
            lines.append("| %s | %s | %s | %s | %s |" % (
                row["category"], _money(row["spent"]), _money(row["limit"]),
                _money(row["diff"]), "超支" if row["over"] else "正常"))
    else:
        lines.append("（本次未提供预算表，用 `--budget 预算.csv` 可做超支告警，格式：`category,limit`）")
    lines.append("")

    lines.append("## 异常消费提示")
    lines.append("")
    if anomalies:
        for note in anomalies:
            lines.append("- %s" % note)
    else:
        lines.append("- 未发现超预算或异常大额消费。")
    lines.append("")

    if summary["top_merchants"]:
        lines.extend(["## 高频商户", ""])
        for name, amount in summary["top_merchants"]:
            lines.append("- %s：%s 元" % (name, _money(amount)))
        lines.append("")

    lines.extend([
        "## 明细",
        "",
        "| 日期 | 金额（元） | 分类 | 商户/摘要 | 支付方式 | 备注 |",
        "| --- | --- | --- | --- | --- | --- |",
    ])
    for entry in sorted(entries, key=lambda item: (item.get("date") or "", item.get("id") or "")):
        lines.append("| %s | %s | %s | %s | %s | %s |" % (
            entry.get("date") or "无日期", _money(entry.get("amount")),
            entry.get("category") or "其他", entry.get("merchant") or "（未填）",
            entry.get("pay_method") or "未填", (entry.get("note") or "").replace("|", "丨")))
    if not entries:
        lines.append("| （无记录） | | | | | |")
    lines.append("")

    if model_note:
        lines.extend(["## 模型解读", "", model_note, ""])
    if raw_appendix:
        lines.extend([
            "## 附录：模型原始输出（未能解析为 JSON）",
            "",
            "```text",
            raw_appendix,
            "```",
            "",
        ])
    return "\n".join(lines).rstrip() + "\n"


# ---------- 模型归一化（可选） ----------

ASK_MODEL_SYSTEM = """你是记账助手。用户会给出若干条自然语言记账原文，请把每条归一化成结构化记录。
输出要求：
1. 只输出一个 JSON 数组，不要任何解释文字，不要 Markdown 代码围栏。
2. 数组元素字段：amount（数字）、category（只能是 餐饮/交通/购物/居住/娱乐/医疗/教育/人情/其他 之一）、
   merchant（商户或简短摘要）、date（YYYY-MM-DD）。
3. 金额无法判断就填 0，分类无法判断就填「其他」，顺序必须与输入一致。"""


def parse_normalize_json(text):
    """把模型回复解析成归一化记录列表；解析失败返回 None。"""
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
    records = []
    for item in data:
        if not isinstance(item, dict):
            continue
        try:
            amount = float(item.get("amount") or 0)
        except (TypeError, ValueError):
            amount = 0.0
        records.append({
            "amount": round(amount, 2),
            "category": item.get("category") if item.get("category") in CATEGORY_ORDER else "其他",
            "merchant": (str(item.get("merchant") or "") or None),
            "date": item.get("date") if re.match(r"^\d{4}-\d{2}-\d{2}$", str(item.get("date") or "")) else None,
        })
    return records or None


def normalize_with_model(parsed_list, model):
    """让模型归一化模糊记账，返回 (归一化记录或 None, 原始文本)。"""
    client = anthropic.Anthropic()
    lines = ["%d. %s" % (index + 1, parsed.get("raw", "")) for index, parsed in enumerate(parsed_list)]
    response = call_model(
        client, model, ASK_MODEL_SYSTEM, [{"role": "user", "content": "\n".join(lines)}]
    )
    raw = response_text(response)
    return parse_normalize_json(raw), raw


def merge_normalized(parsed_list, normalized):
    """把模型归一化结果合并回规则解析结果（只补空、不覆盖已解析字段）。"""
    merged = []
    for index, parsed in enumerate(parsed_list):
        item = dict(parsed)
        if normalized and index < len(normalized):
            guess = normalized[index]
            if item.get("amount") is None and guess.get("amount"):
                item["amount"] = guess["amount"]
            if not item.get("category"):
                item["category"] = guess.get("category")
            if not item.get("merchant") and guess.get("merchant"):
                item["merchant"] = guess.get("merchant")
            if not item.get("date") and guess.get("date"):
                item["date"] = guess.get("date")
        merged.append(item)
    return merged


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
    """离线自检：临时账本 + 多条样本，覆盖全部确定性逻辑，不联网、不需要 key。"""
    today = date(2026, 10, 28)  # 星期三

    # --- 自然语言解析：金额 ---
    entry = parse_entry("午饭 35 元", today)
    assert entry["amount"] == 35.0, entry
    assert entry["category"] == "餐饮", entry
    assert entry["date"] is None, entry          # 没写日期 → None + 提示，入库时按今天
    assert any("未识别到日期" in hint for hint in entry["hints"]), entry["hints"]
    assert entry["merchant"] == "午饭", entry
    assert make_entry(entry, today)["date"] == "2026-10-28"

    entry = parse_entry("咖啡 ￥12.5", today)
    assert entry["amount"] == 12.5, entry
    assert entry["category"] == "餐饮", entry

    entry = parse_entry("coffee $12.5", today)
    assert entry["amount"] == 12.5, entry

    entry = parse_entry("打车去机场 45.5块 昨天 支付宝", today)
    assert entry["amount"] == 45.5, entry
    assert entry["date"] == "2026-10-27", entry
    assert entry["category"] == "交通", entry
    assert entry["pay_method"] == "支付宝", entry
    assert "机场" in (entry["merchant"] or ""), entry

    entry = parse_entry("午饭三十五元", today)
    assert entry["amount"] is None, entry
    assert any("中文数字" in hint for hint in entry["hints"]), entry["hints"]

    entry = parse_entry("买菜 20", today)
    assert entry["amount"] == 20.0, entry
    assert any("未带单位" in hint for hint in entry["hints"]), entry["hints"]

    # --- 自然语言解析：日期 ---
    assert parse_entry("交房租 3000 元 10月27日", today)["date"] == "2026-10-27"
    assert parse_entry("交房租 3000 元 2026-10-27", today)["date"] == "2026-10-27"
    assert parse_entry("交房租 3000 元 27号", today)["date"] == "2026-10-27"
    assert parse_entry("交房租 3000 元 前天", today)["date"] == "2026-10-26"
    assert parse_entry("交房租 3000 元 周一", today)["date"] == "2026-10-26"  # 最近的周一
    assert parse_entry("交房租 3000 元 3月1日", today)["date"] == "2026-03-01"

    # --- 自然语言解析：分类与支付方式 ---
    assert parse_entry("电影票 88 元 微信", today)["category"] == "娱乐"
    assert parse_entry("挂号费 50 元", today)["category"] == "医疗"
    assert parse_entry("随份子 200 元", today)["category"] == "人情"
    assert parse_entry("网课 199 元", today)["category"] == "教育"
    assert parse_entry("房租 3000 元 银行卡", today)["category"] == "居住"
    assert parse_entry("买衣服 500 元", today)["category"] == "购物"
    assert parse_entry("打车 30 元 信用卡", today)["pay_method"] == "信用卡"
    assert parse_entry("晚饭 40 元 现金", today)["pay_method"] == "现金"
    weird = parse_entry("随便花了一笔 66 元", today)
    assert weird["category"] is None and any("其他" in hint for hint in weird["hints"])

    # --- 账本读写：原子写、去重、id ---
    with tempfile.TemporaryDirectory() as temp_dir:
        book_path = os.path.join(temp_dir, "账本.json")
        parsed_list = [
            parse_entry("午饭 35 元", today),
            parse_entry("晚饭 50 元", today),
            parse_entry("午饭 35 元", today),  # 重复
            parse_entry("午餐三顿", today),        # 解析不出金额 → 应跳过
        ]
        added, skipped, no_amount = add_entries(book_path, parsed_list, today)
        assert len(added) == 2, added
        assert skipped == 1
        assert no_amount == 1
        assert os.path.exists(book_path)
        loaded = load_book(book_path)
        assert len(loaded) == 2
        assert all(item.get("id") for item in loaded)

        added2, skipped2, _ = add_entries(book_path, parsed_list, today)
        assert added2 == [] and skipped2 == 3  # 两条重复 + 一条无金额不计

        # --- 统计 ---
        entries = load_book(book_path)
        entries.append(make_entry(parse_entry("打车 45.5 元 昨天 支付宝", today), today))
        entries.append(make_entry(parse_entry("买衣服 800 元 2026-10-20", today), today))
        summary = summarize(entries, days_in_month("2026-10"))
        assert summary["count"] == 4
        assert summary["total"] == 35.0 + 50.0 + 45.5 + 800.0
        assert summary["by_category"]["餐饮"]["count"] == 2
        assert summary["by_category"]["餐饮"]["amount"] == 85.0
        assert summary["by_category"]["交通"]["amount"] == 45.5
        assert summary["max_entry"]["amount"] == 800.0
        assert summary["record_days"] == 3
        assert abs(summary["daily_avg"] - summary["total"] / 31) < 0.01
        assert summary["daily"]["2026-10-28"] == 85.0

        month_entries = filter_month(entries, "2026-10")
        assert len(month_entries) == 4
        assert filter_month(entries, "2026-09") == []
        assert previous_month("2026-01") == "2025-12"
        assert days_in_month("2026-10") == 31
        assert days_in_month("2026-02") == 28

        # --- 预算对比与超支告警 ---
        budget_rows = [
            {"category": "餐饮", "limit": 100.0},
            {"category": "购物", "limit": 500.0},
        ]
        checks = budget_check(summary["by_category"], budget_rows)
        assert checks[0]["category"] == "购物" and checks[0]["over"] is True
        assert checks[1]["category"] == "餐饮" and checks[1]["over"] is False
        anomalies = detect_anomalies(summary, budget_rows)
        assert any("超预算" in note and "购物" in note for note in anomalies), anomalies

        budget_path = os.path.join(temp_dir, "预算.csv")
        with open(budget_path, "w", encoding="utf-8", newline="") as handle:
            handle.write("category,limit\n餐饮,100\n购物,500\n")
        assert read_budget(budget_path) == [
            {"category": "餐饮", "limit": 100.0},
            {"category": "购物", "limit": 500.0},
        ]

        # --- ASCII 图表 ---
        chart = render_bar_chart([("餐饮", 85.0, "85.00 元"), ("购物", 800.0, "800.00 元")])
        assert "█" in chart
        assert "餐饮" in chart and "购物" in chart
        assert chart.index("餐饮") < chart.index("购物")
        trend = render_trend_chart(summary["daily"])
        assert "2026-10-28" in trend and "█" in trend
        assert render_bar_chart([]) == "（没有数据）"

        # --- 报告结构 ---
        report = build_report(
            summary, "2026-10", budget_rows, anomalies, "", None, entries,
            generated_at="2026-10-28 12:00:00",
        )
        for heading in (
            "# 开销报告（2026-10）", "## 本月概览", "## 分类占比", "## 每日趋势",
            "## 预算对比", "## 异常消费提示", "## 明细",
        ):
            assert heading in report, "报告缺少：%s" % heading
        assert "█" in report
        assert "800.00" in report

        # --- 导出 CSV ---
        csv_path = os.path.join(temp_dir, "账本.csv")
        with open(csv_path, "w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(["date", "amount", "category", "merchant", "pay_method", "note"])
            for item in entries:
                writer.writerow([
                    item.get("date"), item.get("amount"), item.get("category"),
                    item.get("merchant"), item.get("pay_method") or "", item.get("note"),
                ])
        with open(csv_path, "r", encoding="utf-8", newline="") as handle:
            rows = list(csv.reader(handle))
        assert rows[0][0] == "date" and len(rows) == len(entries) + 1

    # --- 模型回复 JSON 解析兜底 ---
    good = (
        "```json\n"
        '[{"amount":35,"category":"餐饮","merchant":"午饭","date":"2026-10-28"}]\n'
        "```"
    )
    parsed = parse_normalize_json(good)
    assert parsed and parsed[0]["amount"] == 35.0 and parsed[0]["category"] == "餐饮"
    assert parse_normalize_json("这不是 JSON") is None
    assert parse_normalize_json("[not json") is None
    assert parse_normalize_json("[]") is None
    weird_json = '[{"amount":"很多","category":"外星","merchant":"","date":"2026/10/28"}]'
    coerced = parse_normalize_json(weird_json)
    assert coerced and coerced[0]["amount"] == 0.0
    assert coerced[0]["category"] == "其他"
    assert coerced[0]["date"] is None

    merged = merge_normalized(
        [parse_entry("不明支出 12 元", today)], parsed
    )
    assert merged[0]["category"] == "餐饮"

    raw_report = build_report(
        summarize([], 31), "2026-10", [], [], "", "自由格式模型意见",
        [], generated_at="2026-10-28 12:00:00",
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
        description="自然语言记账助手：一句话记账、本地 JSON 账本、开销统计与中文 Markdown 月报",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            '  python3 agent.py add "午饭 35 元" "打车￥45.5 昨天 支付宝"\n'
            "  python3 agent.py report --month 2026-10 --budget 预算.csv\n"
            "  python3 agent.py list --category 餐饮\n"
            "  python3 agent.py add --file 记账文本.txt\n"
            "  python3 agent.py report --file 记账文本.txt --month 2026-10\n"
        ),
    )
    parser.add_argument(
        "command", nargs="?", choices=["add", "report", "list", "export"],
        help="子命令：add 记账 / report 月报 / list 查看 / export 导出 CSV",
    )
    parser.add_argument(
        "texts", nargs="*",
        help="add 时的记账原文，可以给多条，例如 \"午饭 35 元\"",
    )
    parser.add_argument("--book", default=DEFAULT_BOOK,
                        help="账本 JSON 路径（默认 %s）" % DEFAULT_BOOK)
    parser.add_argument("--file", dest="file_path", metavar="文本文件",
                        help="批量导入：每行一条记账原文（add 时写入账本；report 时先导入账本再出报告）")
    parser.add_argument("--month", default=None,
                        help="统计月份，格式 YYYY-MM（默认当月）")
    parser.add_argument("--budget", metavar="预算CSV",
                        help="预算表 CSV（category,limit 两列），用于超支告警")
    parser.add_argument("--output", default=DEFAULT_REPORT,
                        help="报告写入路径（默认 %s）" % DEFAULT_REPORT)
    parser.add_argument("--category", default=None,
                        help="list 时只看某个分类，如 餐饮")
    parser.add_argument("--csv", dest="csv_path", metavar="导出CSV",
                        help="export 时导出的 CSV 路径")
    parser.add_argument("--ask-model", action="store_true",
                        help="让模型归一化模糊记账（需要 API key）")
    parser.add_argument("--model", default=DEFAULT_MODEL,
                        help="模型名（默认 %s）" % DEFAULT_MODEL)
    parser.add_argument("--offline", action="store_true",
                        help="纯确定性模式：不调用模型、不需要 API key")
    parser.add_argument("--self-test", action="store_true",
                        help="离线自检：不联网、不需要 API key")
    return parser


def _print_entries(entries):
    if not entries:
        print("（账本里没有记录）")
        return
    print("%-4s %-12s %-10s %-8s %-20s %-8s %s" % (
        "序号", "日期", "金额", "分类", "商户/摘要", "支付", "备注"))
    for index, entry in enumerate(entries, 1):
        print("%-4d %-12s %-10s %-8s %-20s %-8s %s" % (
            index,
            entry.get("date") or "无日期",
            "%.2f" % float(entry.get("amount") or 0),
            entry.get("category") or "其他",
            (entry.get("merchant") or "（未填）")[:18],
            entry.get("pay_method") or "未填",
            entry.get("note") or "",
        ))


def _read_lines(path):
    with open(path, "r", encoding="utf-8") as handle:
        return [line.strip() for line in handle if line.strip()]


def cmd_add(args, parser):
    texts = list(args.texts)
    if args.file_path:
        try:
            texts.extend(_read_lines(args.file_path))
        except OSError as exc:
            print("错误：读取记账文本失败：%s" % exc, file=sys.stderr)
            sys.exit(1)
    if not texts:
        parser.error("add 需要至少一条记账原文，或用 --file 批量导入")

    today = date.today()
    parsed_list = [parse_entry(text, today) for text in texts]

    raw_appendix = None
    if args.ask_model and not args.offline:
        require_api_key()
        print("[模型归一化] 模型：%s" % args.model)
        try:
            normalized, raw_appendix = normalize_with_model(parsed_list, args.model)
        except anthropic.AuthenticationError:
            print("错误：API key 无效或没有权限，请检查 ANTHROPIC_API_KEY。", file=sys.stderr)
            sys.exit(1)
        except anthropic.APIConnectionError:
            print("错误：无法连接模型服务，请检查网络或网关地址。", file=sys.stderr)
            sys.exit(1)
        except anthropic.APIStatusError as exc:
            print("错误：模型返回错误（%s）：%s" % (exc.status_code, exc.message), file=sys.stderr)
            sys.exit(1)
        if normalized is None:
            print("[提示] 模型输出不是合法 JSON，已按规则解析结果记账。")
        else:
            parsed_list = merge_normalized(parsed_list, normalized)

    for parsed in parsed_list:
        for hint in parsed["hints"]:
            print("[提示] %s：%s" % (parsed["raw"][:20], hint))

    added, skipped, no_amount = add_entries(args.book, parsed_list, today)
    print("[记账] 写入 %d 条到 %s（重复跳过 %d 条、缺少金额跳过 %d 条）"
          % (len(added), args.book, skipped, no_amount))
    for entry in added:
        print("  + %s %8.2f 元  %-4s %s" % (
            entry["date"], entry["amount"], entry["category"], entry["merchant"]))
    if raw_appendix:
        print("[提示] 模型原始输出未解析成功，可查看报告附录或换 --model 重试。")


def cmd_report(args, parser):
    month = args.month or date.today().strftime("%Y-%m")
    if not re.match(r"^\d{4}-\d{2}$", month):
        parser.error("--month 格式应为 YYYY-MM，例如 2026-10")
    if not 1 <= int(month[5:7]) <= 12:
        parser.error("--month 月份必须在 01-12 之间，例如 2026-10")
    if args.file_path:
        # 批量导入：先把自然语言记账行解析进账本（与 add --file 同一套解析），再出报告
        try:
            texts = _read_lines(args.file_path)
        except OSError as exc:
            print("错误：读取记账文本失败：%s" % exc, file=sys.stderr)
            sys.exit(1)
        today = date.today()
        parsed_list = [parse_entry(text, today) for text in texts]
        added, skipped, no_amount = add_entries(args.book, parsed_list, today)
        print("[记账] 从 %s 导入 %d 条到 %s（重复跳过 %d 条、缺少金额跳过 %d 条）"
              % (args.file_path, len(added), args.book, skipped, no_amount))
    entries = load_book(args.book)
    month_entries = filter_month(entries, month)
    if not entries:
        print("错误：账本 %s 里没有记录，先用 add 记几笔。" % args.book, file=sys.stderr)
        sys.exit(1)
    if not month_entries:
        print("[提示] %s 没有记账记录，报告仍会生成（其余月份：%s）" % (
            month, "、".join(sorted(set(month_of(e["date"]) for e in entries)) or ["无"])))

    summary = summarize(month_entries, days_in_month(month))
    prev_total = summarize(filter_month(entries, previous_month(month)), days_in_month(previous_month(month)))["total"]
    budget_rows = []
    if args.budget:
        try:
            budget_rows = read_budget(args.budget)
        except OSError as exc:
            print("错误：读取预算表失败：%s" % exc, file=sys.stderr)
            sys.exit(1)
    anomalies = detect_anomalies(summary, budget_rows)

    model_note = ""
    raw_appendix = None
    if args.ask_model and not args.offline:
        require_api_key()
        print("[模型解读] 模型：%s" % args.model)
        try:
            response = call_model(
                anthropic.Anthropic(), args.model,
                "你是个人财务助手。下面是一份月度开销统计，请用中文给出 3 句以内解读"
                "（消费结构、值得注意的点），只输出纯文本，不要标题和列表符号。",
                [{"role": "user", "content": json.dumps({
                    "month": month,
                    "total": summary["total"],
                    "count": summary["count"],
                    "by_category": dict(summary["by_category"]),
                    "top_merchants": summary["top_merchants"],
                }, ensure_ascii=False)}],
                max_tokens=1000,
            )
            model_note = response_text(response)
            if not model_note:
                raw_appendix = "（模型没有返回任何内容）"
        except (anthropic.AuthenticationError, anthropic.APIConnectionError, anthropic.APIStatusError) as exc:
            print("[提示] 模型解读失败（%s），报告按确定性统计生成。" % exc.__class__.__name__)

    report = build_report(summary, month, budget_rows, anomalies, model_note, raw_appendix,
                          month_entries, prev_total=prev_total or None)
    output_path = os.path.abspath(args.output)
    parent = os.path.dirname(output_path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as handle:
        handle.write(report)
    print("[完成] 报告已写入：%s" % output_path)
    print("[完成] %s 共 %d 笔、合计 %.2f 元，日均 %.2f 元" % (
        month, summary["count"], summary["total"], summary["daily_avg"]))
    if anomalies:
        print("[提醒] 发现 %d 条异常消费提示，详见报告。" % len(anomalies))


def cmd_list(args, parser):
    entries = load_book(args.book)
    if args.category:
        entries = [entry for entry in entries if entry.get("category") == args.category]
    if args.month:
        entries = filter_month(entries, args.month)
    _print_entries(sorted(entries, key=lambda item: (item.get("date") or "", item.get("id") or "")))
    total = sum(float(entry.get("amount") or 0) for entry in entries)
    print("合计：%d 笔，%.2f 元" % (len(entries), total))


def cmd_export(args, parser):
    entries = load_book(args.book)
    if not entries:
        print("错误：账本 %s 里没有记录，无法导出。" % args.book, file=sys.stderr)
        sys.exit(1)
    csv_path = args.csv_path or os.path.splitext(args.book)[0] + ".csv"
    parent = os.path.dirname(os.path.abspath(csv_path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(csv_path, "w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["date", "amount", "category", "merchant", "pay_method", "note"])
        for entry in sorted(entries, key=lambda item: (item.get("date") or "", item.get("id") or "")):
            writer.writerow([
                entry.get("date"), entry.get("amount"), entry.get("category"),
                entry.get("merchant"), entry.get("pay_method") or "", entry.get("note") or "",
            ])
    print("[完成] 已导出 %d 条到 %s" % (len(entries), csv_path))


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

    if not args.command:
        parser.error("请指定子命令：add / report / list / export（或用 --self-test 自检）")

    if args.command == "add":
        cmd_add(args, parser)
    elif args.command == "report":
        cmd_report(args, parser)
    elif args.command == "list":
        cmd_list(args, parser)
    elif args.command == "export":
        cmd_export(args, parser)


if __name__ == "__main__":
    main()
