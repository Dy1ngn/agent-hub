#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
提示词体检与优化助手 —— 确定性体检 + 模型改写 两段式提示词优化 agent

先用完全确定性的规则给提示词做一次“体检”（五要素、模糊词、结构、占位符一致性），
再按固定公式算出五维评分卡，套用【角色/任务/背景/约束/输出格式/示例】模板生成
结构化版本，最后把原文交给模型产出优化版提示词、逐条改动说明与使用建议，
输出一份中文 Markdown 报告。除 anthropic SDK 外只用标准库。

用法：
    python3 agent.py <提示词.txt> [--goal 任务目标] [--style 简洁|详细|结构化]
    python3 agent.py --text "你是一名……" --output 优化报告.md
    python3 agent.py prompt.txt --offline        # 只做确定性体检，不调用模型

示例：
    python3 agent.py prompt.txt --goal "生成小红书推广文案" --style 结构化 --output 报告.md
    python3 agent.py --text "帮我写个文案" --offline
    python3 agent.py prompt.txt --model qwen3.8-max

环境要求：
    1. 调用模型需要设置 API key（--offline 与 --self-test 不需要 key）：
           export ANTHROPIC_API_KEY=你的key          # macOS / Linux
           $env:ANTHROPIC_API_KEY="你的key"           # Windows PowerShell
       也支持 ANTHROPIC_AUTH_TOKEN（第三方兼容网关常用）。
    2. 安装依赖：
           python3 -m pip install -r requirements.txt   # Windows 用 py -3 -m pip
    3. 走第三方 Anthropic 兼容网关时，用 --model 指定网关认得的模型名，例如：
           python3 agent.py prompt.txt --model qwen3.8-max
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

# 默认模型（可用 --model 参数或 PROMPT_OPTIMIZER_MODEL 环境变量更换）
DEFAULT_MODEL = os.environ.get("PROMPT_OPTIMIZER_MODEL", "claude-opus-5")

DEFAULT_OUTPUT = "prompt-report.md"
DEFAULT_STYLE = "详细"
STYLE_CHOICES = ("简洁", "详细", "结构化")

MAX_PROMPT_CHARS = 20000      # 提示词最多读入多少字符，超出截断并注明
MAX_EVIDENCE_PER_ELEMENT = 3  # 每个要素最多展示几条证据

SEVERITIES = ("严重", "警告", "建议")

# ---------- 五要素体检规则 ----------

ELEMENT_SPECS = [
    {
        "key": "role",
        "name": "角色",
        "desc": "有没有告诉模型“你是谁”",
        "strong": [
            r"你(是|来)(一|1)?(名|位|个)",
            r"假设你是",
            r"扮演",
            r"担任.{0,8}角色",
            r"作为(一|1)?(名|位|个)",
            r"资深.{0,8}(专家|工程师|顾问|分析师|经理|设计师|编辑|翻译|助手|律师|医生|教师)",
            r"\bact\s+as\b",
            r"\byou\s+are\s+(a|an|the)\b",
            r"\bprompt\s+engineer\b",
            r"人设",
        ],
        "weak": ["角色", "身份", "persona", "role", "专家", "助手"],
        "hint": "开头加一句“你是一名资深 XX 专家”，把角色、经验年限、口吻写清楚。",
    },
    {
        "key": "task",
        "name": "任务",
        "desc": "有没有一句明确的祈使指令",
        "strong": [
            r"^(请|帮我|替我|需要你|你要|你的任务|任务[:：]|目标[:：]|目标是)",
            r"^(生成|撰写|编写|写作|写一|总结|概括|分析|翻译|改写|润色|优化|提取|整理|回答|给出|输出|评估|设计|实现|计算|检查|对比|推荐|解释|改写|分类|转换|评审|校对|提炼)",
            r"^(write|generate|create|summarize|analyze|translate|extract|answer|review|explain|list|design|compare|rewrite|classify|convert|rewrite|optimize)\b",
        ],
        "weak": ["任务", "目标", "需要", "想要", "希望", "帮忙", "协助", "文案", "内容", "报告", "总结", "task"],
        "hint": "用一句祈使句写清“做什么 + 做到什么程度”，例如“请为一款便携咖啡机撰写 3 条小红书推广文案”。",
    },
    {
        "key": "constraint",
        "name": "约束",
        "desc": "有没有边界条件和禁止事项",
        "strong": [
            r"不要",
            r"不能",
            r"禁止",
            r"必须",
            r"只能",
            r"不得",
            r"不超过",
            r"不能超过",
            r"务必",
            r"限制",
            r"至少",
            r"避免",
            r"不允许",
            r"\bdo\s+not\b",
            r"\bmust\b",
            r"\bnever\b",
            r"\balways\b",
            r"\bdon'?t\b",
            r"\bavoid\b",
            r"\bat\s+least\b",
            r"\bno\s+more\s+than\b",
        ],
        "weak": ["尽量", "最好", "建议", "注意", "应该", "合适", "should", "prefer", "please"],
        "hint": "补 2~4 条硬约束：不要做什么、必须包含什么、数量/字数上限、语气与合规红线。",
    },
    {
        "key": "format",
        "name": "输出格式",
        "desc": "有没有规定结果长什么样",
        "strong": [
            r"输出格式",
            r"返回格式",
            r"格式[:：]",
            r"\bjson\b",
            r"\bmarkdown\b",
            r"\bcsv\b",
            r"\byaml\b",
            r"表格",
            r"列表",
            r"分点",
            r"字数",
            r"分段",
            r"以.{0,8}形式",
            r"用.{0,6}(表格|列表|分点|段落)",
            r"```",
            r"标题.{0,4}正文",
            r"\btable\b",
            r"\bbullet\b",
        ],
        "weak": ["输出", "返回", "给出", "format", "output", "结果"],
        "hint": "写明输出载体（Markdown / JSON / 表格 / 分点）、条数、字数上限和字段名。",
    },
    {
        "key": "example",
        "name": "示例",
        "desc": "有没有输入输出样例或参照",
        "strong": [
            r"例如",
            r"比如",
            r"示例",
            r"样例",
            r"例子",
            r"参考[:：]",
            r"e\.g\.",
            r"\bfor\s+example\b",
            r"\bfor\s+instance\b",
            r"```",
            r"\binput\b",
            r"\boutput\b",
            r"输入[:：]",
            r"输出[:：]",
        ],
        "weak": ["如：", "比方", "譬如", "像这样", "参照"],
        "hint": "给 1~2 组 input → output 样例（可以用 ``` 代码块包起来），让模型照着格式走。",
    },
]

ELEMENT_PATTERN_MAP = dict((spec["key"], spec) for spec in ELEMENT_SPECS)

# ---------- 模糊词词表与替换建议 ----------

VAGUE_WORDS_ZH = [
    "一些", "某些", "若干", "尽量", "可能", "大概", "差不多", "比较好", "较好", "不错",
    "合适", "相关", "等等", "适当", "尽快", "大量", "足够", "常见", "充分", "合理",
    "有效", "显著", "更好", "最佳", "尽可能", "某种程度", "一般来说", "比较好用",
]

VAGUE_WORDS_EN = [
    "some", "many", "several", "maybe", "might", "nice", "good", "great", "relevant",
    "appropriate", "etc", "various", "certain", "often", "usually", "quickly", "soon",
    "best", "better", "lots", "plenty", "adequate", "sufficient", "significant",
    "reasonable", "effective", "robust", "seamless", "user-friendly", "a few",
]

VAGUE_SUGGESTIONS = {
    "一些": "改成明确数量，如“3 条”“5 个”",
    "某些": "列出具体对象，或写“所有”",
    "若干": "改成具体数字",
    "尽量": "改成硬性要求“必须”，做不到就写例外条件",
    "可能": "去掉，或写清判断条件",
    "大概": "改成确切数值或范围，如“300~500 字”",
    "差不多": "改成可核对的标准",
    "比较好": "改成可度量指标，如“准确率 ≥ 90%”",
    "较好": "改成可度量指标",
    "不错": "改成可度量指标",
    "合适": "写清判断标准，如“字数在 200~300 之间即为合适”",
    "相关": "列出具体技能/字段名",
    "等等": "改成完整枚举，或写“以下 N 项”",
    "适当": "写清程度，如“每次不超过 2 处”",
    "尽快": "改成明确时限，如“24 小时内”",
    "大量": "改成具体数量级",
    "足够": "改成具体阈值",
    "常见": "列出你认定的常见场景",
    "充分": "写清“充分”的判定标准",
    "合理": "写清合理性标准",
    "有效": "写清有效性指标",
    "显著": "写清提升幅度，如“提升 20% 以上”",
    "更好": "写成对比指标",
    "最佳": "写清评价维度与权重",
    "尽可能": "改成硬约束或写明取舍顺序",
    "某种程度": "量化程度",
    "一般来说": "去掉，直接给规则",
    "比较好用": "写清使用体验的判定标准",
    "some": "改为具体数量，如 3 items",
    "many": "改为具体数量或范围",
    "several": "改为具体数字",
    "maybe": "删除，或写清触发条件",
    "might": "删除，改成确定表述",
    "nice": "改为可度量指标",
    "good": "改为可度量指标，如 accuracy ≥ 90%",
    "great": "改为可度量指标",
    "relevant": "列出具体字段/技能名",
    "appropriate": "写清判断标准",
    "etc": "改为完整枚举",
    "various": "列出具体种类",
    "certain": "写清是哪些",
    "often": "改为频率，如 每周 2 次",
    "usually": "写清概率或规则",
    "quickly": "改为时限，如 5 分钟内",
    "soon": "改为明确时间点",
    "best": "写清评价维度",
    "better": "写成对比指标",
    "lots": "改为具体数量",
    "plenty": "改为具体数量",
    "adequate": "写清阈值",
    "sufficient": "写清阈值",
    "significant": "写清幅度，如 +20%",
    "reasonable": "写清判断标准",
    "effective": "写清有效性指标",
    "robust": "写清容错场景",
    "seamless": "写清交互细节",
    "user-friendly": "写清可用性指标",
    "a few": "改为具体数字",
}

VAGUE_DEFAULT_SUGGESTION = "改成可核对的量化表述（数量、比例、时限、阈值）"

# ---------- 结构体检阈值 ----------

SHORT_PROMPT_CHARS = 30        # 低于多少字符算过短
LONG_PROMPT_CHARS = 6000       # 高于多少字符算过长
GOOD_PROMPT_RANGE = (50, 3000)  # 评分友好的长度区间
MIXED_LANGUAGE_RATIO = 0.3     # 中英混排行占比超过多少算混杂
MAX_MULTI_SIGNS = 3            # 问号/祈使句达到多少算多任务混杂

CJK_RE = re.compile(r"[\u4e00-\u9fff]")
EN_WORD_RE = re.compile(r"[A-Za-z]{2,}")
NUMBERED_LINE_RE = re.compile(r"^\s*(\d+\s*[.、)）]|[一二三四五六七八九十]+\s*[、.)）])")
BULLET_LINE_RE = re.compile(r"^\s*([-*+•·]|\(\d+\))")
SEPARATOR_RE = re.compile(r"^\s*(#{1,6}\s+\S|---+\s*$|\*\*\*+\s*$|===+\s*$|──+\s*$)")
DOUBLE_BRACE_RE = re.compile(r"\{\{\s*([A-Za-z_][\w\-. ]*)\s*\}\}")
SINGLE_BRACE_RE = re.compile(r"(?<!\{)\{([A-Za-z_][\w\-.]*)\}(?!\})")


# ---------- 数据结构 ----------

class Issue:
    """一条体检问题"""

    def __init__(self, severity, category, line, detail, suggestion):
        self.severity = severity
        self.category = category
        self.line = line
        self.detail = detail
        self.suggestion = suggestion

    def key(self):
        return (self.category, self.line, self.detail)


# ---------- 工具函数 ----------

def clamp(value, low=0, high=100):
    """把分数限制在 [low, high]"""
    return max(low, min(high, int(round(value))))


def read_text_file(path):
    """读取文本文件，容忍 BOM 与个别坏字节，返回 (文本, 是否截断)"""
    raw = Path(path).expanduser().read_text(encoding="utf-8-sig", errors="replace")
    if len(raw) > MAX_PROMPT_CHARS:
        return raw[:MAX_PROMPT_CHARS], True
    return raw, False


def split_lines(text):
    """按行拆分，保留行号（1 开始）"""
    return [(index, line) for index, line in enumerate(text.splitlines(), 1)]


def excerpt(line, limit=90):
    """截取一行作为证据展示"""
    stripped = line.strip()
    if len(stripped) <= limit:
        return stripped
    return stripped[:limit] + "…"


# ---------- 五要素体检 ----------

def detect_elements(text):
    """检测五要素（角色/任务/约束/输出格式/示例），返回 有/弱/无 + 证据行"""
    result = {}
    lines = split_lines(text)
    for spec in ELEMENT_SPECS:
        strong_hits = []
        weak_hits = []
        for lineno, line in lines:
            stripped = line.strip()
            if not stripped:
                continue
            matched = False
            for pattern in spec["strong"]:
                if re.search(pattern, stripped, re.IGNORECASE):
                    strong_hits.append((lineno, excerpt(stripped)))
                    matched = True
                    break
            if matched:
                continue
            for pattern in spec["weak"]:
                if re.search(pattern, stripped, re.IGNORECASE):
                    weak_hits.append((lineno, excerpt(stripped)))
                    break
        if strong_hits:
            status = "有"
            evidence = strong_hits
        elif weak_hits:
            status = "弱"
            evidence = weak_hits
        else:
            status = "无"
            evidence = []
        result[spec["key"]] = {
            "name": spec["name"],
            "desc": spec["desc"],
            "status": status,
            "evidence": evidence[:MAX_EVIDENCE_PER_ELEMENT],
            "hint": spec["hint"],
        }
    return result


# ---------- 模糊词检测 ----------

def find_vague_words(text):
    """定位中文/英文模糊词，返回 [{word, kind, line, excerpt, suggestion}]"""
    hits = []
    for lineno, line in split_lines(text):
        stripped = line.strip()
        if not stripped:
            continue
        lowered = stripped.lower()
        for word in VAGUE_WORDS_ZH:
            start = 0
            while True:
                index = stripped.find(word, start)
                if index < 0:
                    break
                hits.append({
                    "word": word,
                    "kind": "中文",
                    "line": lineno,
                    "excerpt": excerpt(stripped),
                    "suggestion": VAGUE_SUGGESTIONS.get(word, VAGUE_DEFAULT_SUGGESTION),
                })
                start = index + len(word)
        for word in VAGUE_WORDS_EN:
            if re.search(r"\b%s\b" % re.escape(word), lowered):
                hits.append({
                    "word": word,
                    "kind": "英文",
                    "line": lineno,
                    "excerpt": excerpt(stripped),
                    "suggestion": VAGUE_SUGGESTIONS.get(word, VAGUE_DEFAULT_SUGGESTION),
                })
    return hits


def vague_density(vague_hits, text):
    """模糊词密度：每 100 字出现多少个模糊词"""
    length = max(1, len(text.strip()))
    return round(len(vague_hits) * 100.0 / length, 2)


# ---------- 结构体检 ----------

def analyze_structure(text):
    """统计长度、段落、列表、分隔标记、多任务混杂、语言混杂、占位符一致性"""
    lines = split_lines(text)
    nonempty = [(no, line) for no, line in lines if line.strip()]
    paragraphs = 0
    blank_run = True
    for _, line in lines:
        if line.strip():
            if blank_run:
                paragraphs += 1
            blank_run = False
        else:
            blank_run = True

    numbered = [no for no, line in nonempty if NUMBERED_LINE_RE.match(line.strip())]
    bullets = [no for no, line in nonempty if BULLET_LINE_RE.match(line.strip())]
    separators = [no for no, line in nonempty if SEPARATOR_RE.match(line)]

    question_count = text.count("？") + text.count("?")

    imperative_lines = []
    task_spec = ELEMENT_PATTERN_MAP["task"]
    for no, line in nonempty:
        stripped = line.strip()
        for pattern in task_spec["strong"]:
            if re.search(pattern, stripped, re.IGNORECASE):
                imperative_lines.append(no)
                break

    mixed_lines = []
    for no, line in nonempty:
        if CJK_RE.search(line) and EN_WORD_RE.search(line):
            mixed_lines.append(no)
    mixed_ratio = (len(mixed_lines) / float(len(nonempty))) if nonempty else 0.0

    double_vars = sorted(set(DOUBLE_BRACE_RE.findall(text)))
    single_vars = sorted(set(SINGLE_BRACE_RE.findall(text)))

    return {
        "char_count": len(text.strip()),
        "line_count": len(lines),
        "nonempty_line_count": len(nonempty),
        "paragraph_count": paragraphs,
        "numbered_list_count": len(numbered),
        "bullet_list_count": len(bullets),
        "separator_lines": separators,
        "has_separator": bool(separators),
        "question_count": question_count,
        "imperative_lines": imperative_lines,
        "multi_task": question_count >= MAX_MULTI_SIGNS or len(imperative_lines) >= MAX_MULTI_SIGNS,
        "mixed_language": mixed_ratio >= MIXED_LANGUAGE_RATIO,
        "mixed_line_count": len(mixed_lines),
        "mixed_ratio": round(mixed_ratio, 2),
        "double_brace_vars": double_vars,
        "single_brace_vars": single_vars,
        "placeholder_conflict": bool(double_vars and single_vars),
    }


# ---------- 评分卡 ----------

def element_score(status):
    """要素状态转分值：有 100 / 弱 50 / 无 0"""
    return {"有": 100, "弱": 50, "无": 0}[status]


def compute_scores(elements, vague_hits, structure):
    """五维评分：清晰度/完整性/可执行性/格式/约束 + 总分（全部确定性公式）"""
    vague_count = len(vague_hits)
    statuses = dict((key, elements[key]["status"]) for key in elements)

    # 清晰度：模糊词越多、任务混杂、中英混杂、长度异常越扣分
    clarity = 100 - 8 * vague_count
    if structure["multi_task"]:
        clarity -= 15
    if structure["mixed_language"]:
        clarity -= 10
    if structure["char_count"] < SHORT_PROMPT_CHARS:
        clarity -= 20
    if structure["char_count"] > LONG_PROMPT_CHARS:
        clarity -= 10
    clarity = clamp(clarity)

    # 完整性：五要素命中率（有 1 分、弱 0.5 分、无 0 分）
    ratio = sum(element_score(statuses[key]) for key in (
        "role", "task", "constraint", "format", "example")) / 500.0
    completeness = clamp(ratio * 100)

    # 可执行性：任务 40% + 约束 35% + 输出格式 25%
    executability = clamp(
        0.40 * element_score(statuses["task"])
        + 0.35 * element_score(statuses["constraint"])
        + 0.25 * element_score(statuses["format"])
    )

    # 格式：段落、列表、分隔标记、长度区间
    fmt = 50
    if structure["paragraph_count"] >= 2:
        fmt += 15
    if structure["numbered_list_count"] or structure["bullet_list_count"]:
        fmt += 15
    if structure["has_separator"]:
        fmt += 10
    if GOOD_PROMPT_RANGE[0] <= structure["char_count"] <= GOOD_PROMPT_RANGE[1]:
        fmt += 10
    fmt = clamp(fmt)

    # 约束：约束要素 70% + 模糊词扣分 30%
    constraint = clamp(
        0.70 * element_score(statuses["constraint"])
        + 0.30 * max(0, 100 - 10 * vague_count)
    )

    total = clamp(
        0.25 * clarity + 0.25 * completeness + 0.20 * executability
        + 0.15 * fmt + 0.15 * constraint
    )
    return {
        "清晰度": clarity,
        "完整性": completeness,
        "可执行性": executability,
        "格式": fmt,
        "约束": constraint,
        "总分": total,
    }


SCORE_DIMS = ("清晰度", "完整性", "可执行性", "格式", "约束")


def improvement_points(dim, elements, vague_hits, structure, scores):
    """给某个评分维度列 1~3 条提升点"""
    points = []
    if dim == "清晰度":
        if vague_hits:
            points.append("把 %d 处模糊词换成可核对的数字/阈值/时限（见模糊词清单）。" % len(vague_hits))
        if structure["multi_task"]:
            points.append("一次只交代一个主任务，其余拆成多条提示词或分步执行。")
        if structure["mixed_language"]:
            points.append("统一语言：同一条提示词里尽量只用一种语言写指令。")
        if not points:
            points.append("保持现在的写法，新增内容时继续用具体数字替代形容词。")
    elif dim == "完整性":
        missing = [elements[key]["name"] for key in ("role", "task", "constraint", "format", "example")
                   if elements[key]["status"] != "有"]
        if missing:
            points.append("补齐要素：%s。" % "、".join(missing))
        else:
            points.append("五要素齐全，可以再补背景信息（用户、场景、成功标准）。")
    elif dim == "可执行性":
        if elements["task"]["status"] != "有":
            points.append("把任务改成一句祈使句，写清交付物与数量。")
        if elements["constraint"]["status"] != "有":
            points.append("补充硬约束（禁止事项、字数/条数上限、合规红线）。")
        if elements["format"]["status"] != "有":
            points.append("规定输出载体与字段，例如“Markdown 表格：技能 | 证据 | 建议”。")
    elif dim == "格式":
        if structure["paragraph_count"] < 2:
            points.append("按“角色 / 任务 / 约束 / 输出格式”分段，段落之间空一行。")
        if not (structure["numbered_list_count"] or structure["bullet_list_count"]):
            points.append("约束与要求用编号列表逐条列出让模型逐条对照。")
        if not structure["has_separator"]:
            points.append("用 ### 或 --- 分隔大块内容，减少串行误读。")
    elif dim == "约束":
        if elements["constraint"]["status"] != "有":
            points.append("写明“不要做什么”和“必须包含什么”。")
        if vague_hits:
            points.append("删掉约束里的模糊表述，全部换成可校验条件。")
    return points


# ---------- 模板重排（结构化版本） ----------

def classify_lines(text):
    """把原文逐行归入 角色/任务/背景/约束/输出格式/示例 六类"""
    categories = dict((key, []) for key in ("role", "task", "background", "constraint", "format", "example"))
    order = ("role", "task", "format", "constraint", "example")
    in_fence = False
    fence_buf = []
    for lineno, raw in split_lines(text):
        line = raw.strip()
        if line.startswith("```"):
            if in_fence:
                fence_buf.append(raw)
                categories["example"].append((lineno, "\n".join(fence_buf)))
                fence_buf = []
                in_fence = False
            else:
                in_fence = True
                fence_buf = [raw]
            continue
        if in_fence:
            fence_buf.append(raw)
            continue
        if not line:
            continue
        best_key = None
        best_hits = 0
        for key in order:
            hits = 0
            for pattern in ELEMENT_PATTERN_MAP[key]["strong"]:
                if re.search(pattern, line, re.IGNORECASE):
                    hits += 1
            if hits > best_hits:
                best_key = key
                best_hits = hits
        if best_key is not None:
            categories[best_key].append((lineno, line))
        else:
            categories["background"].append((lineno, line))
    if fence_buf:
        categories["example"].append((0, "\n".join(fence_buf)))
    return categories


STRUCTURE_TEMPLATE_TITLE = {
    "role": "角色",
    "task": "任务",
    "background": "背景",
    "constraint": "约束",
    "format": "输出格式",
    "example": "示例",
}


def render_structured(text, goal=""):
    """按【角色/任务/背景/约束/输出格式/示例】模板重排原文，得到结构化版本"""
    categories = classify_lines(text)
    parts = []
    for key in ("role", "task", "background", "constraint", "format", "example"):
        title = STRUCTURE_TEMPLATE_TITLE[key]
        body_lines = [line for _, line in categories[key]]
        if key == "task" and goal:
            body_lines.insert(0, "目标：%s" % goal)
        if body_lines:
            body = "\n".join(body_lines)
        else:
            body = "（原文未提供，建议补充：%s）" % ELEMENT_PATTERN_MAP.get(key, {}).get(
                "hint", "补充这一段内容，让提示词更完整")
        parts.append("【%s】\n%s" % (title, body))
    return "\n\n".join(parts)


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
    """从模型响应里提取纯文本"""
    parts = []
    for block in getattr(response, "content", []) or []:
        if getattr(block, "type", "") == "text":
            parts.append(getattr(block, "text", ""))
    return "\n".join(parts).strip()


OPTIMIZE_SYSTEM_PROMPT = """你是一名资深提示词工程师（Prompt Engineer），擅长把粗糙的提示词改写成结构清晰、可执行、可验证的提示词。

用户会给你一段待优化的提示词，可能还会附带任务目标。请完成三件事：
1. 给出优化后的完整提示词：保留原意，补全缺失要素（角色、任务、约束、输出格式、示例），把模糊表述换成量化表述。
2. 列出逐条改动说明：每条写清改动类型、改动前、改动后、为什么这么改。
3. 给出使用建议：如何配合系统提示、如何迭代、常见踩坑。

输出要求：
1. 只输出一个 JSON 对象，不要输出解释文字，不要使用 Markdown 代码围栏。
2. 对象字段：
   - optimized：字符串，优化后的完整提示词
   - changes：数组，每项 {"type": "改动类型", "before": "改动前", "after": "改动后", "why": "原因"}
   - tips：字符串数组，使用建议
3. 所有内容用中文（示例代码/字段名可保留英文）。
"""

STYLE_HINTS = {
    "简洁": "优化后的提示词要短，控制在原文 80% 字数以内，只留必要要素。",
    "详细": "优化后的提示词要素齐全，允许适度展开背景和示例。",
    "结构化": "优化后的提示词必须按【角色】【任务】【背景】【约束】【输出格式】【示例】分块排版。",
}


def build_model_user_prompt(text, goal, style, elements, vague_hits):
    """拼接发给模型的用户提示"""
    issues = []
    for spec in ELEMENT_SPECS:
        item = elements[spec["key"]]
        if item["status"] != "有":
            issues.append("%s：%s（%s）" % (item["name"], item["status"], item["hint"]))
    vague_preview = "、".join(sorted(set(hit["word"] for hit in vague_hits)))[:200] or "（无）"
    return (
        "【待优化提示词】\n%s\n\n"
        "【任务目标】%s\n"
        "【期望风格】%s（%s）\n"
        "【体检发现的缺项】\n%s\n"
        "【检测到的模糊词】%s\n"
        "请按系统提示输出 JSON。"
    ) % (
        text,
        goal or "（未指定）",
        style,
        STYLE_HINTS.get(style, ""),
        "\n".join(issues) if issues else "（五要素齐全）",
        vague_preview,
    )


def parse_optimization_json(text):
    """把模型回复解析成优化结果字典；解析不出来返回 None（调用方兜底）"""
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
    optimized = str(data.get("optimized") or "").strip()
    changes = []
    raw_changes = data.get("changes")
    if isinstance(raw_changes, list):
        for item in raw_changes:
            if not isinstance(item, dict):
                continue
            change = {
                "type": str(item.get("type") or "改写").strip(),
                "before": str(item.get("before") or "").strip(),
                "after": str(item.get("after") or "").strip(),
                "why": str(item.get("why") or "").strip(),
            }
            if change["before"] or change["after"]:
                changes.append(change)
    tips = []
    raw_tips = data.get("tips")
    if isinstance(raw_tips, list):
        for tip in raw_tips:
            if not isinstance(tip, str):
                continue
            text_tip = tip.strip()
            if text_tip:
                tips.append(text_tip)
    if not optimized and not changes and not tips:
        return None
    return {"optimized": optimized, "changes": changes, "tips": tips}


def optimize_with_model(text, goal, style, model, elements, vague_hits, verbose=True):
    """调用模型做优化，返回 (结果字典或 None, 模型原始输出或 None)"""
    client = anthropic.Anthropic()
    if verbose:
        print("[模型优化] 正在请求模型：%s" % model)
    response = call_model(
        client,
        model,
        OPTIMIZE_SYSTEM_PROMPT,
        [{"role": "user", "content": build_model_user_prompt(text, goal, style, elements, vague_hits)}],
        max_tokens=8000,
    )
    raw = response_text(response)
    parsed = parse_optimization_json(raw)
    if parsed is None and verbose:
        print("[模型优化] 返回内容不是合法 JSON，原文已放入报告附录。")
    return parsed, raw


# ---------- 问题清单 ----------

def collect_issues(elements, vague_hits, structure, truncated=False):
    """汇总确定性体检发现的问题，按 严重/警告/建议 分级"""
    issues = []
    for spec in ELEMENT_SPECS:
        item = elements[spec["key"]]
        if item["status"] == "无":
            issues.append(Issue("严重", "要素缺失", 0,
                                "缺少要素「%s」：%s" % (item["name"], item["desc"]),
                                item["hint"]))
        elif item["status"] == "弱":
            issues.append(Issue("警告", "要素偏弱", item["evidence"][0][0] if item["evidence"] else 0,
                                "要素「%s」只有间接表述，证据：%s" % (
                                    item["name"],
                                    item["evidence"][0][1] if item["evidence"] else "（无）"),
                                item["hint"]))

    if vague_hits:
        for hit in vague_hits[:20]:
            issues.append(Issue("建议", "模糊表述", hit["line"],
                                "第 %d 行出现模糊词「%s」：%s" % (hit["line"], hit["word"], hit["excerpt"]),
                                hit["suggestion"]))

    if structure["multi_task"]:
        issues.append(Issue("警告", "结构", 0,
                            "疑似多任务混杂：问号 %d 处、祈使句 %d 句，模型容易只做其中一件。"
                            % (structure["question_count"], len(structure["imperative_lines"])),
                            "拆成多条提示词，或明确“第一步/第二步”的执行顺序。"))
    if structure["mixed_language"]:
        issues.append(Issue("建议", "结构", 0,
                            "中英混排行占比 %d%%（%d 行），语气与时态容易不一致。"
                            % (structure["mixed_ratio"] * 100, structure["mixed_line_count"]),
                            "统一用中文或英文写指令，术语第一次出现时括号注明原文。"))
    if structure["placeholder_conflict"]:
        issues.append(Issue("警告", "占位符", 0,
                            "占位符写法不一致：{{%s}} 与 {%s} 混用。"
                            % ("、".join(structure["double_brace_vars"][:3]),
                               "、".join(structure["single_brace_vars"][:3])),
                            "统一成一种写法（推荐 {{变量名}}），避免模板引擎解析失败。"))
    if structure["char_count"] < SHORT_PROMPT_CHARS:
        issues.append(Issue("严重", "结构", 0,
                            "提示词过短（%d 字符），几乎无法约束模型输出。" % structure["char_count"],
                            "补齐五要素，至少写清任务、约束与输出格式。"))
    elif structure["char_count"] > LONG_PROMPT_CHARS:
        issues.append(Issue("建议", "结构", 0,
                            "提示词偏长（%d 字符），重点容易被淹没。" % structure["char_count"],
                            "把背景、示例移到文件里用占位符引用，正文只留指令。"))
    if not structure["has_separator"] and structure["paragraph_count"] >= 2:
        issues.append(Issue("建议", "结构", 0,
                            "没有 ### 或 --- 之类的分隔标记，长提示词段落边界不清。",
                            "用 ### 小标题或 --- 分隔大块内容。"))
    if truncated:
        issues.append(Issue("警告", "输入", 0,
                            "输入超过 %d 字符已被截断，体检只覆盖前一部分。" % MAX_PROMPT_CHARS,
                            "拆分提示词后再体检，或提高 MAX_PROMPT_CHARS。"))
    return issues


# ---------- 报告 ----------

def render_issue_section(issues, severity):
    rows = [issue for issue in issues if issue.severity == severity]
    if not rows:
        return "（无）\n"
    parts = []
    for index, issue in enumerate(rows, 1):
        location = "第 %d 行" % issue.line if issue.line else "全文"
        parts.append("%d. **[%s]**（%s）\n   - 问题：%s\n   - 建议：%s"
                     % (index, issue.category, location, issue.detail, issue.suggestion))
    return "\n".join(parts) + "\n"


def score_table(scores):
    lines = ["| 维度 | 得分（0-100） |", "| --- | --- |"]
    for dim in SCORE_DIMS:
        lines.append("| %s | %d |" % (dim, scores[dim]))
    lines.append("| **总分** | **%d** |" % scores["总分"])
    return "\n".join(lines)


def compare_table(before_scores, after_scores):
    lines = ["| 维度 | 优化前 | 优化后 | 变化 |", "| --- | --- | --- | --- |"]
    for dim in list(SCORE_DIMS) + ["总分"]:
        delta = after_scores[dim] - before_scores[dim]
        sign = "+" if delta > 0 else ""
        lines.append("| %s | %d | %d | %s%d |" % (dim, before_scores[dim], after_scores[dim], sign, delta))
    return "\n".join(lines)


def build_report(text, elements, vague_hits, structure, scores, issues,
                 structured_md, model_data, raw_output, goal, style, offline,
                 model, truncated=False, generated_at=None):
    """生成中文 Markdown 体检报告"""
    generated_at = generated_at or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    out = []
    out.append("# 提示词体检与优化报告")
    out.append("")
    out.append("> 生成时间：%s　|　模式：%s　|　模型：%s"
               % (generated_at, "离线体检（不调用模型）" if offline else "体检 + 模型优化", model))
    out.append("")
    out.append("## 一、概览")
    out.append("")
    out.append("| 项目 | 数值 |")
    out.append("| --- | --- |")
    out.append("| 提示词长度 | %d 字符 / %d 行（非空 %d 行） |"
               % (structure["char_count"], structure["line_count"], structure["nonempty_line_count"]))
    out.append("| 段落数 | %d |" % structure["paragraph_count"])
    out.append("| 编号列表 / 项目符号 | %d / %d 行 |"
               % (structure["numbered_list_count"], structure["bullet_list_count"]))
    out.append("| 模糊词 | %d 处（密度 %.2f 个/百字） |"
               % (len(vague_hits), vague_density(vague_hits, text)))
    out.append("| 中英混排行 | %d 行（占比 %d%%） |"
               % (structure["mixed_line_count"], structure["mixed_ratio"] * 100))
    out.append("| 占位符 | 双花括号 %d 个 / 单花括号 %d 个 |"
               % (len(structure["double_brace_vars"]), len(structure["single_brace_vars"])))
    out.append("| 任务目标 | %s |" % (goal or "（未指定）"))
    out.append("| 期望风格 | %s |" % style)
    out.append("")
    out.append("## 二、五要素体检表")
    out.append("")
    out.append("| 要素 | 状态 | 说明 | 证据 |")
    out.append("| --- | --- | --- | --- |")
    for spec in ELEMENT_SPECS:
        item = elements[spec["key"]]
        evidence = "；".join("第 %d 行：%s" % (no, text_) for no, text_ in item["evidence"]) or "（无）"
        out.append("| %s | %s | %s | %s |" % (item["name"], item["status"], item["desc"], evidence))
    out.append("")
    out.append("### 提升要点")
    out.append("")
    for spec in ELEMENT_SPECS:
        item = elements[spec["key"]]
        if item["status"] != "有":
            out.append("- **%s（%s）**：%s" % (item["name"], item["status"], item["hint"]))
    if all(elements[spec["key"]]["status"] == "有" for spec in ELEMENT_SPECS):
        out.append("- 五要素齐全，继续保持；可再补充背景与成功标准。")
    out.append("")
    out.append("## 三、评分卡")
    out.append("")
    out.append(score_table(scores))
    out.append("")
    out.append("评分口径：清晰度=100-8×模糊词数-多任务扣分-混杂扣分-长度扣分；"
               "完整性=五要素命中率（有 1、弱 0.5、无 0）；"
               "可执行性=任务 40%+约束 35%+输出格式 25%；"
               "格式=段落/列表/分隔标记/长度区间加分；"
               "约束=约束要素 70%+模糊词扣分 30%；"
               "总分=清晰度 25%+完整性 25%+可执行性 20%+格式 15%+约束 15%。")
    out.append("")
    for dim in SCORE_DIMS:
        points = improvement_points(dim, elements, vague_hits, structure, scores)
        if not points:
            points = ["这一维暂无明显问题，保持现状即可。"]
        out.append("- **%s（%d 分）**：%s" % (dim, scores[dim], "".join(points)))
    out.append("")

    improved_text = ""
    improved_label = ""
    if model_data and model_data.get("optimized"):
        improved_text = model_data["optimized"]
        improved_label = "模型优化版"
    elif structured_md:
        improved_text = re.sub(r"^【.*?】\n", "", structured_md, flags=re.M)
        improved_label = "结构化版本（离线）"
    if improved_text:
        improved_elements = detect_elements(improved_text)
        improved_structure = analyze_structure(improved_text)
        improved_scores = compute_scores(improved_elements, find_vague_words(improved_text), improved_structure)
        out.append("### 优化前后对比（对照%s）" % improved_label)
        if not (model_data and model_data.get("optimized")):
            out.append("")
            out.append("> 离线模式下“优化后”只是结构化重排后的重新评分，变化来自排版与长度，不代表模型改写效果。")
        out.append("")
        out.append(compare_table(scores, improved_scores))
        out.append("")

    out.append("## 四、问题清单")
    out.append("")
    out.append("### 严重")
    out.append("")
    out.append(render_issue_section(issues, "严重"))
    out.append("### 警告")
    out.append("")
    out.append(render_issue_section(issues, "警告"))
    out.append("### 建议")
    out.append("")
    out.append(render_issue_section(issues, "建议"))
    out.append("## 五、结构化版本（模板重排）")
    out.append("")
    out.append("下面是把原文按【角色/任务/背景/约束/输出格式/示例】模板重新排版的结果，可直接作为新提示词起点：")
    out.append("")
    out.append("```markdown")
    out.append(structured_md)
    out.append("```")
    out.append("")
    out.append("## 六、优化版本")
    out.append("")
    if model_data and model_data.get("optimized"):
        out.append("```markdown")
        out.append(model_data["optimized"])
        out.append("```")
        out.append("")
    elif offline:
        out.append("（离线模式未调用模型；上面的「结构化版本」即为本次可用的优化结果，接入模型后可得到逐条改写说明。）")
        out.append("")
    else:
        out.append("（模型未返回可解析的优化文本，原始输出见附录。）")
        out.append("")
    out.append("## 七、改动对照")
    out.append("")
    if model_data and model_data.get("changes"):
        out.append("| 类型 | 改动前 | 改动后 | 原因 |")
        out.append("| --- | --- | --- | --- |")
        for change in model_data["changes"]:
            out.append("| %s | %s | %s | %s |" % (
                change["type"].replace("|", "/"),
                change["before"].replace("|", "/").replace("\n", " ")[:80] or "（无）",
                change["after"].replace("|", "/").replace("\n", " ")[:80] or "（无）",
                change["why"].replace("|", "/").replace("\n", " ")[:120] or "（无）",
            ))
        out.append("")
    else:
        out.append("（本次没有结构化改动清单；离线模式下请对照「问题清单」逐条修改。）")
        out.append("")
    out.append("## 八、使用建议")
    out.append("")
    tips = list(model_data.get("tips") or []) if model_data else []
    default_tips = [
        "把角色、任务、约束、输出格式固定成模板，只替换每次变化的变量部分。",
        "模糊词改成数字与阈值后，用 3~5 个真实输入回归验证输出是否稳定。",
        "提示词里给出的示例要贴近真实数据，别用明显假的例子。",
        "改完一次只调一个变量（角色 / 约束 / 示例），便于定位是哪一条改动起了作用。",
    ]
    for index, tip in enumerate(tips + default_tips, 1):
        out.append("%d. %s" % (index, tip))
    out.append("")
    if raw_output:
        out.append("## 附录：模型原始输出（未能解析为 JSON）")
        out.append("")
        out.append("```text")
        out.append(raw_output[:6000])
        out.append("```")
        out.append("")
    return "\n".join(out)


# ---------- 自检 ----------

class _FakeMessages:
    def __init__(self, outer):
        self.outer = outer

    def create(self, **kwargs):
        self.outer["calls"] += 1
        if self.outer["calls"] == 1:
            raise TypeError("网关可能不认识 thinking 参数")
        return {"ok": True, "kwargs": kwargs}


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
    """离线自检：用 tempfile 造样本，覆盖全部确定性逻辑，不联网、不需要 API key"""
    strong_text = (
        "你是一名资深电商文案专家。\n"
        "任务：请为一款便携咖啡机撰写小红书推广文案。\n"
        "背景：目标用户是 25-35 岁上班族，客单价 399 元。\n"
        "约束：不要夸大功效，必须包含产品参数，字数不超过 300 字。\n"
        "输出格式：以 Markdown 列表输出，包含标题、正文、话题标签。\n"
        "示例：例如标题可以写「通勤 5 分钟，喝到手冲级咖啡」。\n"
    )
    weak_text = (
        "关于新品上市的推广文案，尽量写得有意思一些，可能需要几个亮点，整体感觉要比较合适。\n"
    )
    none_text = "天气。\n"

    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        strong_path = root / "strong.txt"
        strong_path.write_text(strong_text, encoding="utf-8")
        weak_path = root / "weak.txt"
        weak_path.write_text(weak_text, encoding="utf-8")
        none_path = root / "none.txt"
        none_path.write_text(none_text, encoding="utf-8")

        # --- 读取 ---
        loaded, truncated = read_text_file(strong_path)
        assert not truncated
        assert loaded.strip() == strong_text.strip()

        # --- 五要素：强样本全部命中 ---
        strong_elements = detect_elements(loaded)
        for spec in ELEMENT_SPECS:
            assert strong_elements[spec["key"]]["status"] == "有", \
                "强样本要素未命中：%s" % spec["key"]
            assert strong_elements[spec["key"]]["evidence"], "应有证据行"

        # --- 五要素：弱样本 / 空样本 ---
        weak_elements = detect_elements(weak_text)
        assert weak_elements["role"]["status"] == "无"
        assert weak_elements["task"]["status"] == "弱"
        assert weak_elements["constraint"]["status"] == "弱"
        assert weak_elements["format"]["status"] == "无"
        assert weak_elements["example"]["status"] == "无"
        none_elements = detect_elements(none_text)
        assert all(item["status"] == "无" for item in none_elements.values())

        # --- 模糊词：计数与定位 ---
        vague_hits = find_vague_words(weak_text)
        words = set(hit["word"] for hit in vague_hits)
        for expected in ("尽量", "一些", "可能", "合适"):
            assert expected in words, "模糊词漏检：%s" % expected
        assert all(hit["line"] == 1 for hit in vague_hits)
        assert all(hit["suggestion"] for hit in vague_hits)
        assert not find_vague_words(strong_text) or all(h["word"] for h in find_vague_words(strong_text))
        assert vague_density(vague_hits, weak_text) > 0

        # --- 结构体检 ---
        strong_structure = analyze_structure(strong_text)
        assert strong_structure["char_count"] > 50
        assert strong_structure["paragraph_count"] >= 1
        assert not strong_structure["placeholder_conflict"]
        assert not strong_structure["multi_task"]

        multi_text = "第一件事怎么做？第二件事怎么做？第三件事呢？请生成报告。请输出表格。请给出结论。\n"
        multi_structure = analyze_structure(multi_text)
        assert multi_structure["multi_task"] is True

        placeholder_text = "变量 {{user_name}} 与 {user_age} 一起用。\n"
        placeholder_structure = analyze_structure(placeholder_text)
        assert placeholder_structure["placeholder_conflict"] is True
        assert "user_name" in placeholder_structure["double_brace_vars"]
        assert "user_age" in placeholder_structure["single_brace_vars"]

        mixed_structure = analyze_structure("这是中文行 but with English words mixed in\n" * 4)
        assert mixed_structure["mixed_language"] is True

        # --- 评分公式 ---
        strong_scores = compute_scores(strong_elements, find_vague_words(strong_text), strong_structure)
        weak_scores = compute_scores(weak_elements, vague_hits, analyze_structure(weak_text))
        none_scores = compute_scores(none_elements, [], analyze_structure(none_text))
        for dim in list(SCORE_DIMS) + ["总分"]:
            assert 0 <= strong_scores[dim] <= 100
            assert 0 <= weak_scores[dim] <= 100
            assert 0 <= none_scores[dim] <= 100
        assert strong_scores["总分"] > weak_scores["总分"] > none_scores["总分"]
        assert strong_scores["完整性"] == 100
        assert none_scores["完整性"] == 0
        # 确定性：同输入两次计算必须完全一致
        assert compute_scores(strong_elements, find_vague_words(strong_text), strong_structure) == strong_scores

        # --- 问题清单分级 ---
        issues = collect_issues(weak_elements, vague_hits, analyze_structure(weak_text))
        severities = set(issue.severity for issue in issues)
        assert "严重" in severities or "警告" in severities
        assert any(issue.category == "模糊表述" for issue in issues)
        strong_issues = collect_issues(strong_elements, [], strong_structure)
        assert not [issue for issue in strong_issues if issue.severity == "严重"]

        placeholder_issues = collect_issues(
            strong_elements, [], analyze_structure(placeholder_text + strong_text))
        assert any(issue.category == "占位符" for issue in placeholder_issues)

        # --- 模板渲染 ---
        structured = render_structured(strong_text, goal="小红书推广")
        for title in ("【角色】", "【任务】", "【背景】", "【约束】", "【输出格式】", "【示例】"):
            assert title in structured, "结构化版本缺少：%s" % title
        assert "资深电商文案专家" in structured
        assert "小红书推广" in structured
        empty_structured = render_structured(none_text)
        assert "（原文未提供" in empty_structured

        # --- 模型 JSON 解析兜底 ---
        good_json = (
            "```json\n"
            '{"optimized":"新的提示词","changes":[{"type":"补约束","before":"写好点",'
            '"after":"字数不超过 300 字","why":"可核对"}],"tips":["分段排版"]}\n'
            "```"
        )
        parsed = parse_optimization_json(good_json)
        assert parsed and parsed["optimized"] == "新的提示词"
        assert parsed["changes"][0]["type"] == "补约束"
        assert parsed["tips"] == ["分段排版"]
        assert parse_optimization_json("这不是 JSON") is None
        assert parse_optimization_json("{bad json") is None
        assert parse_optimization_json('{"changes":"not-a-list"}') is None
        assert parse_optimization_json("[]") is None
        weird = '{"optimized":"","changes":[{"before":"a","after":"","why":""}],"tips":[1,"x",null]}'
        weird_parsed = parse_optimization_json(weird)
        assert weird_parsed and weird_parsed["changes"][0]["before"] == "a"
        assert weird_parsed["tips"] == ["x"]

        # --- call_model 三级降级（离线假客户端：第一层 TypeError，第二层成功） ---
        fake = _FakeClient()
        result = call_model(fake, "fake-model", "system", [{"role": "user", "content": "hi"}])
        assert result["ok"] is True
        assert fake.state["calls"] == 2

        # --- 报告渲染 ---
        report = build_report(
            strong_text, strong_elements, [], strong_structure, strong_scores,
            strong_issues, structured, parsed, "", "小红书推广", "结构化", False,
            "claude-opus-5", generated_at="2026-01-01 00:00:00",
        )
        for heading in ("# 提示词体检与优化报告", "## 一、概览", "## 二、五要素体检表",
                        "## 三、评分卡", "## 四、问题清单", "## 五、结构化版本（模板重排）",
                        "## 六、优化版本", "## 七、改动对照", "## 八、使用建议"):
            assert heading in report, "报告缺少：%s" % heading
        assert "新的提示词" in report
        assert "补约束" in report
        assert "优化前后对比" in report

        offline_report = build_report(
            weak_text, weak_elements, vague_hits, analyze_structure(weak_text), weak_scores,
            issues, render_structured(weak_text), None, "自由格式输出", "", "简洁", True,
            "claude-opus-5", generated_at="2026-01-01 00:00:00",
        )
        assert "离线体检" in offline_report
        assert "附录：模型原始输出" in offline_report
        assert "自由格式输出" in offline_report

        # --- 报告落盘 ---
        output_path = root / "prompt-report.md"
        output_path.write_text(report, encoding="utf-8")
        assert output_path.read_text(encoding="utf-8").startswith("# 提示词体检与优化报告")

    print("SELF-TEST OK")


# ---------- CLI ----------

def build_parser():
    parser = argparse.ArgumentParser(
        description="提示词体检与优化助手：确定性体检（五要素/模糊词/结构）+ 模型改写，输出中文 Markdown 报告",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            "  python3 agent.py prompt.txt --goal \"生成小红书文案\" --style 结构化\n"
            "  python3 agent.py --text \"帮我写个文案\" --offline\n"
            "  python3 agent.py prompt.txt --model qwen3.8-max --output 优化报告.md\n"
        ),
    )
    parser.add_argument("prompt_file", nargs="?", help="提示词文件路径（.txt/.md），与 --text 二选一")
    parser.add_argument("--text", default="", help="直接给出提示词文本，不用文件")
    parser.add_argument("--goal", default="", help="这次提示词要完成的任务目标，写进报告和模型提示")
    parser.add_argument("--style", choices=list(STYLE_CHOICES), default=DEFAULT_STYLE,
                        help="优化风格：简洁 / 详细 / 结构化（默认 %s）" % DEFAULT_STYLE)
    parser.add_argument("--output", default=DEFAULT_OUTPUT,
                        help="报告写入路径（默认 %s）" % DEFAULT_OUTPUT)
    parser.add_argument("--model", default=DEFAULT_MODEL,
                        help="优化使用的模型（默认 %s）" % DEFAULT_MODEL)
    parser.add_argument("--offline", action="store_true",
                        help="只做确定性体检与模板重排，不调用模型（不需要 API key）")
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

    if not args.prompt_file and not args.text.strip():
        parser.error("请提供提示词文件路径，或用 --text \"提示词内容\" 直接给出提示词")

    truncated = False
    if args.prompt_file:
        path = Path(args.prompt_file).expanduser()
        if not path.is_file():
            print("错误：找不到提示词文件：%s" % path, file=sys.stderr)
            sys.exit(1)
        try:
            text, truncated = read_text_file(path)
        except OSError as exc:
            print("错误：读取提示词失败：%s" % exc, file=sys.stderr)
            sys.exit(1)
    else:
        text = args.text
        if len(text) > MAX_PROMPT_CHARS:
            text = text[:MAX_PROMPT_CHARS]
            truncated = True

    if not text.strip():
        print("错误：提示词内容为空。", file=sys.stderr)
        sys.exit(1)

    print("[体检] 提示词长度 %d 字符，风格 %s" % (len(text), args.style))
    elements = detect_elements(text)
    vague_hits = find_vague_words(text)
    structure = analyze_structure(text)
    scores = compute_scores(elements, vague_hits, structure)
    issues = collect_issues(elements, vague_hits, structure, truncated=truncated)
    structured_md = render_structured(text, args.goal)
    print("[体检] 五要素：%s，模糊词 %d 处，总分 %d"
          % ("、".join("%s %s" % (elements[key]["name"], elements[key]["status"])
                      for key in ("role", "task", "constraint", "format", "example")),
             len(vague_hits), scores["总分"]))

    model_data = None
    raw_output = ""
    if args.offline:
        print("[离线模式] 跳过模型优化，只输出确定性体检结果与结构化版本。")
    else:
        if not os.environ.get("ANTHROPIC_API_KEY") and not os.environ.get("ANTHROPIC_AUTH_TOKEN"):
            print(
                "提示：没有检测到 ANTHROPIC_API_KEY / ANTHROPIC_AUTH_TOKEN，无法调用模型做优化。\n"
                "请先设置：\n"
                "  macOS / Linux：export ANTHROPIC_API_KEY=你的key\n"
                "  Windows PowerShell：$env:ANTHROPIC_API_KEY=\"你的key\"\n"
                "或者加 --offline 只做确定性体检。",
                file=sys.stderr,
            )
            sys.exit(1)
        try:
            model_data, raw_output = optimize_with_model(
                text, args.goal, args.style, args.model, elements, vague_hits)
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

    report = build_report(
        text, elements, vague_hits, structure, scores, issues,
        structured_md, model_data, raw_output, args.goal, args.style,
        args.offline, args.model, truncated=truncated,
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
    print("[完成] 总分 %d（清晰度 %d、完整性 %d、可执行性 %d、格式 %d、约束 %d），问题 %d 条"
          % (scores["总分"], scores["清晰度"], scores["完整性"], scores["可执行性"],
             scores["格式"], scores["约束"], len(issues)))
    if raw_output and model_data is None:
        print("[提示] 模型输出不是合法 JSON，已原样放入报告附录。")


if __name__ == "__main__":
    main()
