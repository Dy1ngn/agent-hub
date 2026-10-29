#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
简历诊断与 JD 匹配助手 —— 确定性体检 + 模型润色 两段式简历优化 agent

先用完全确定性的规则给简历做“体检”：章节结构完整度、要点动词、量化成果、
日期与联系方式规范、错别字、无关信息，再按岗位描述（JD）做技能关键词匹配打分，
得到四维评分卡与优化路线图；最后把最差的要点交给模型做“差 → 好”逐条改写示例。
除 anthropic SDK 外只用标准库。

用法：
    python3 agent.py <简历.txt|md> [--jd 职位描述.txt] [--role 目标岗位]
    python3 agent.py 简历.md --jd jd.txt --output 诊断报告.md
    python3 agent.py 简历.txt --offline        # 只做确定性诊断，不调用模型

示例：
    python3 agent.py 简历.txt --jd 数据分析师JD.txt --role 数据分析师 --output 诊断.md
    python3 agent.py 简历.md --offline
    python3 agent.py 简历.txt --jd jd.txt --model qwen3.8-max

环境要求：
    1. 调用模型需要设置 API key（--offline 与 --self-test 不需要 key）：
           export ANTHROPIC_API_KEY=你的key          # macOS / Linux
           $env:ANTHROPIC_API_KEY="你的key"           # Windows PowerShell
       也支持 ANTHROPIC_AUTH_TOKEN（第三方兼容网关常用）。
    2. 安装依赖：
           python3 -m pip install -r requirements.txt   # Windows 用 py -3 -m pip
    3. 走第三方 Anthropic 兼容网关时，用 --model 指定网关认得的模型名，例如：
           python3 agent.py 简历.txt --model qwen3.8-max
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

# 默认模型（可用 --model 参数或 RESUME_REVIEWER_MODEL 环境变量更换）
DEFAULT_MODEL = os.environ.get("RESUME_REVIEWER_MODEL", "claude-opus-5")

DEFAULT_OUTPUT = "review-report.md"
MAX_INPUT_CHARS = 40000       # 简历/JD 最多读入多少字符，超出截断并注明
MAX_REWRITES = 8              # 最多让模型改写多少条要点
LONG_BULLET_LIMIT = 60        # 要点超过多少字符算过长
MIN_BULLETS_PER_BLOCK = 3     # 每段经历少于多少条要点算偏少
MAX_BULLETS_PER_BLOCK = 8     # 每段经历多于多少条要点算偏多
RICH_SKILL_TARGET = 15        # 无 JD 时按多少个技能关键词算满丰富度

SEVERITIES = ("严重", "警告", "建议")
SEVERITY_ORDER = {"建议": 1, "警告": 2, "严重": 3}

# ---------- 章节识别 ----------

SECTION_TYPES = [
    ("basic", "基本信息", (
        "基本信息", "个人信息", "个人资料", "联系方式", "联系方法",
        "personal information", "contact", "basic information",
    )),
    ("education", "教育背景", (
        "教育背景", "教育经历", "教育情况", "学习经历", "教育",
        "education", "educational background", "academic background",
    )),
    ("experience", "工作经历", (
        "工作经历", "工作经验", "职业经历", "实习经历", "实习经验", "工作",
        "work experience", "experience", "employment", "internship",
    )),
    ("project", "项目经历", (
        "项目经历", "项目经验", "项目作品", "作品集", "项目",
        "projects", "project experience", "portfolio",
    )),
    ("skill", "技能", (
        "专业技能", "技能清单", "技能特长", "技能关键词", "核心技能", "技能",
        "skills", "technical skills", "core skills", "key skills",
    )),
    ("certificate", "证书荣誉", (
        "证书", "资格证书", "资质证书", "荣誉", "获奖", "奖项", "证书荣誉",
        "certifications", "certificates", "awards", "honors",
    )),
    ("summary", "自我评价", (
        "自我评价", "自我介绍", "个人总结", "个人优势", "职业目标", "自我评价",
        "summary", "profile", "objective", "about me",
    )),
]

SECTION_NAME = dict((key, name) for key, name, _ in SECTION_TYPES)
# 结构评分权重：核心章节权重大，合计 25 分
SECTION_WEIGHTS = {
    "basic": 3, "education": 4, "experience": 5,
    "project": 3, "skill": 4, "certificate": 3, "summary": 3,
}
KEY_SECTIONS = ("education", "experience", "skill")   # 缺这些要警告

HEADING_PREFIX_RE = re.compile(r"^#{1,6}\s*")
HEADING_SUFFIX_RE = re.compile(r"\s*#{1,6}$")
BRACKET_RE = re.compile(r"^[【\[（(]+|[】\]）)]+$")
NUMBERING_RE = re.compile(r"^([一二三四五六七八九十]+|\d+|[IVXivx]+)[、.．)）:：]\s*")

BULLET_RE = re.compile(
    r"^\s*([-*+•·▪◦→※✦]|\(\d+\)|（\d+）|\d+[.、)]|[①②③④⑤⑥⑦⑧⑨⑩])\s*"
)

# ---------- 强/弱动词表 ----------

STRONG_VERBS_EN = [
    "led", "built", "designed", "optimized", "improved", "increased", "reduced",
    "launched", "developed", "implemented", "managed", "created", "delivered",
    "migrated", "refactored", "scaled", "automated", "analyzed", "architected",
    "owned", "drove", "established", "expanded", "generated", "mentored",
    "negotiated", "pioneered", "rebuilt", "streamlined", "spearheaded",
    "standardized", "accelerated", "achieved", "coordinated", "cut", "deployed",
    "doubled", "eliminated", "engineered", "executed", "grew", "hired",
    "introduced", "maintained", "organized", "outperformed", "predicted",
    "prioritized", "produced", "promoted", "redesigned", "resolved", "revamped",
    "saved", "secured", "simplified", "solved", "strengthened", "surpassed",
    "tested", "trained", "transformed", "upgraded", "won",
]

STRONG_VERBS_CN = [
    "负责", "主导", "牵头", "搭建", "构建", "建立", "优化", "提升", "提高", "降低",
    "推动", "落地", "完成", "设计", "开发", "实现", "重构", "改进", "改善", "增长",
    "节省", "缩短", "覆盖", "整合", "迁移", "维护", "保障", "支撑", "协调", "组织",
    "输出", "交付", "梳理", "沉淀", "推广", "上线", "迭代", "修复", "排查", "分析",
    "评估", "预测", "识别", "挖掘", "打通", "制定", "规划", "管理", "带领", "独立",
    "联合", "从零到一", "从0到1", "超额", "连续", "统筹", "重塑", "创办", "攻克",
]

WEAK_VERBS_CN = [
    "参与", "协助", "帮助", "配合", "了解", "学习", "熟悉", "接触", "做一些",
    "做了", "帮忙", "打杂", "跟随", "尝试", "开始",
]

# ---------- 量化与日期正则 ----------

QUANT_PATTERNS = (
    ("百分比", re.compile(r"\d+(?:\.\d+)?\s*%")),
    ("金额", re.compile(r"[¥$]?\s?\d+(?:\.\d+)?\s*(?:万|亿|千)?\s*(?:元|美元|美金|人民币|USD|CNY|RMB|¥|\$)")),
    ("倍数", re.compile(r"\d+(?:\.\d+)?\s*倍")),
    ("时长", re.compile(r"\d+(?:\.\d+)?\s*(?:个)?(?:天|周|月|年|小时|分钟|秒|毫秒|ms|s|h)\b")),
    ("数量", re.compile(r"\d+(?:\.\d+)?\+?")),
)

DATE_STYLE_PATTERNS = (
    ("点分（2026.10）", re.compile(r"(?:19|20)\d{2}\.\s?\d{1,2}")),
    ("短横线（2026-10）", re.compile(r"(?:19|20)\d{2}-\d{1,2}")),
    ("斜杠（2026/10）", re.compile(r"(?:19|20)\d{2}/\d{1,2}")),
    ("中文（2026年10月）", re.compile(r"(?:19|20)\d{2}\s*年\s*\d{1,2}\s*月?")),
)

DATE_ANY_RE = re.compile(r"(?:19|20)\d{2}\s*[.\-/年]\s?\d{1,2}")

# ---------- 联系方式 / 无关信息 / 错别字 ----------

PHONE_RE = re.compile(r"(?<!\d)(?:\+?86[-\s]?)?(1[3-9]\d)(\d{4})(\d{4})(?!\d)")
PHONE_SLOPPY_RE = re.compile(r"(?<!\d)1[3-9]\d(?:[-\s]\d{4}){2}(?!\d)")
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")

IRRELEVANT_WORDS = (
    "身高", "体重", "婚姻", "已婚", "未婚", "政治面貌", "籍贯", "户口", "民族",
    "健康状况", "性别", "出生年月", "宗教", "血型", "星座", "生肖",
)

TYPO_TABLE = (
    ("做为", "作为"),
    ("帐号", "账号"),
    ("登陆", "登录"),
    ("必竟", "毕竟"),
    ("既使", "即使"),
    ("按装", "安装"),
    ("重迭", "重叠"),
    ("松驰", "松弛"),
    ("防碍", "妨碍"),
    ("以经", "已经"),
    ("定单", "订单"),
    ("告戒", "告诫"),
    ("甘败下风", "甘拜下风"),
    ("一如继往", "一如既往"),
    ("迫不急待", "迫不及待"),
)

# ---------- JD 技能词表 ----------

TECH_SKILLS = [
    "Python", "Java", "JavaScript", "TypeScript", "Go", "C++", "C#", "PHP", "Ruby",
    "Rust", "Scala", "Kotlin", "Swift", "Shell", "SQL", "MySQL", "PostgreSQL",
    "Oracle", "SQL Server", "Redis", "MongoDB", "Elasticsearch", "ClickHouse",
    "Hive", "Hadoop", "Spark", "Flink", "Kafka", "RabbitMQ", "Airflow",
    "TensorFlow", "PyTorch", "scikit-learn", "Pandas", "NumPy", "OpenCV",
    "NLP", "LLM", "RAG", "Transformer", "AIGC", "大模型", "微调",
    "Docker", "Kubernetes", "Linux", "Git", "CI/CD", "Jenkins", "Ansible",
    "Terraform", "AWS", "Azure", "GCP", "阿里云", "微服务", "分布式", "高并发",
    "Spring", "Spring Boot", "Django", "Flask", "FastAPI", "Node.js", "React",
    "Vue", "Angular", "Next.js", "微信小程序", "Android", "iOS", "爬虫",
    "ETL", "数据仓库", "数据治理", "数据建模", "推荐系统", "搜索引擎",
    "性能优化", "架构设计", "系统设计", "接口设计", "数据库设计", "自动化测试",
    "持续集成", "安全审计", "运维", "监控", "日志分析",
]

CN_SKILLS = [
    "数据分析", "数据挖掘", "数据可视化", "机器学习", "深度学习", "自然语言处理",
    "计算机视觉", "用户增长", "产品运营", "内容运营", "新媒体运营", "活动策划",
    "品牌营销", "市场推广", "市场调研", "竞品分析", "用户研究", "需求分析",
    "需求评审", "项目管理", "产品设计", "产品规划", "原型设计", "交互设计",
    "视觉设计", "文案撰写", "创意策划", "商务谈判", "商务拓展", "渠道管理",
    "销售管理", "客户关系", "大客户", "供应链", "采购管理", "库存管理",
    "财务分析", "预算管理", "成本控制", "风险控制", "合规", "审计", "人力资源",
    "招聘", "培训", "绩效管理", "薪酬福利", "组织发展", "团队管理", "跨部门协作",
    "战略规划", "经营分析", "质量管理", "流程优化", "标准化", "复盘", "汇报",
    "A/B 测试", "A/B测试", "用户画像", "指标体系", "标签体系", "增长黑客",
    "私域运营", "社群运营", "直播", "短视频", "电商运营", "跨境电商", "供应链管理",
]

TOOL_SKILLS = [
    "Excel", "PowerPoint", "PPT", "Word", "Tableau", "Power BI", "FineBI",
    "SPSS", "SAS", "MATLAB", "Stata", "Figma", "Sketch", "Axure", "Xmind",
    "Photoshop", "Illustrator", "Premiere", "After Effects", "Canva",
    "Jira", "Confluence", "Notion", "Trello", "Slack", "Teambition",
    "SAP", "Salesforce", "HubSpot", "金蝶", "用友", "WordPress", "Shopify",
    "Selenium", "Postman", "Swagger", "Grafana", "Kibana", "Prometheus",
    "Streamlit", "Metabase", "Superset", "Visio", "Project", "SPSS Modeler",
]

# 词表权重：技术词 > 中文技能词 > 工具词
SKILL_KINDS = (
    ("技术", TECH_SKILLS, 1.0),
    ("技能", CN_SKILLS, 0.8),
    ("工具", TOOL_SKILLS, 0.6),
)

# 可迁移技能提示：缺失技能 → 可用哪些已有技能佐证
TRANSFERABLE_HINTS = {
    "Power BI": ("Tableau", "Excel", "数据可视化", "FineBI"),
    "Tableau": ("Excel", "数据可视化", "Power BI"),
    "数据可视化": ("Tableau", "Excel", "Power BI", "FineBI"),
    "机器学习": ("Python", "数据分析", "数据挖掘", "SQL"),
    "深度学习": ("Python", "机器学习", "PyTorch", "TensorFlow"),
    "自然语言处理": ("Python", "机器学习", "大模型", "LLM"),
    "Kubernetes": ("Docker", "Linux", "运维", "CI/CD"),
    "Docker": ("Linux", "运维", "CI/CD", "Kubernetes"),
    "AWS": ("阿里云", "运维", "Linux", "Docker"),
    "Spark": ("Hadoop", "Hive", "Python", "SQL"),
    "Flink": ("Kafka", "Spark", "Java", "实时计算"),
    "项目管理": ("跨部门协作", "团队管理", "Jira", "需求分析"),
    "用户增长": ("数据分析", "产品运营", "A/B 测试", "用户画像"),
    "产品运营": ("内容运营", "用户增长", "数据分析", "活动策划"),
    "财务分析": ("Excel", "数据分析", "预算管理", "成本控制"),
    "SQL": ("Excel", "数据分析", "Python"),
    "Python": ("SQL", "数据分析", "自动化测试"),
}


# ---------- 数据结构 ----------

class Issue:
    """一条诊断问题"""

    def __init__(self, severity, rule_id, name, line, detail, fix):
        self.severity = severity
        self.rule_id = rule_id
        self.name = name
        self.line = line
        self.detail = detail
        self.fix = fix


# ---------- 工具函数 ----------

def clamp(value, low=0, high=100):
    """把分数限制在 [low, high]"""
    return max(low, min(high, int(round(value))))


def read_text_file(path):
    """读取文本文件，容忍 BOM 与个别坏字节，返回 (文本, 是否截断)"""
    raw = Path(path).expanduser().read_text(encoding="utf-8-sig", errors="replace")
    if len(raw) > MAX_INPUT_CHARS:
        return raw[:MAX_INPUT_CHARS], True
    return raw, False


def split_lines(text):
    """按行拆分，保留行号（1 开始）"""
    return [(index, line) for index, line in enumerate(text.splitlines(), 1)]


def excerpt(line, limit=80):
    """截取一行作为展示"""
    stripped = line.strip()
    if len(stripped) <= limit:
        return stripped
    return stripped[:limit] + "…"


def bullet_text(line):
    """去掉要点前缀标记，返回要点正文"""
    return BULLET_RE.sub("", line.strip()).strip()


def normalize_heading(line):
    """把一行规整成候选标题：去 markdown 标记、括号、编号、首尾标点"""
    text = line.strip()
    text = HEADING_PREFIX_RE.sub("", text)
    text = HEADING_SUFFIX_RE.sub("", text)
    text = BRACKET_RE.sub("", text)
    text = NUMBERING_RE.sub("", text)
    return text.strip(" ：:、．.-—–\t　").lower()


# ---------- 章节解析 ----------

def match_section(heading):
    """把标题映射到章节类型，认不出返回 None"""
    for key, _, aliases in SECTION_TYPES:
        for alias in aliases:
            if heading == alias.lower():
                return key
    return None


def parse_sections(text):
    """识别简历章节，返回 (章节列表, 是否识别到过标题)"""
    sections = []
    current = None
    seen_heading = False
    lines = split_lines(text)
    for index, (lineno, raw) in enumerate(lines):
        stripped = raw.strip()
        if not stripped:
            continue
        heading = normalize_heading(stripped)
        # 标题下划线写法：下一行是 === 或 ---
        underline_next = False
        if index + 1 < len(lines):
            nxt = lines[index + 1][1].strip()
            if nxt and re.match(r"^(={2,}|-{2,})$", nxt):
                underline_next = True
        key = match_section(heading) if (len(stripped) <= 16 or underline_next) else None
        if key:
            seen_heading = True
            current = {"key": key, "name": SECTION_NAME[key], "title": stripped,
                       "line": lineno, "content": [], "bullets": []}
            sections.append(current)
            continue
        if current is None:
            current = {"key": "unknown", "name": "未命名", "title": "（未识别到章节标题）",
                       "line": 0, "content": [], "bullets": []}
            sections.append(current)
        current["content"].append((lineno, stripped))
        if BULLET_RE.match(stripped):
            current["bullets"].append((lineno, bullet_text(stripped)))
    return sections, seen_heading


def split_experience_blocks(section):
    """把经历类章节按条目标题行拆成若干段经历，统计每段要点数"""
    blocks = []
    current = None
    for lineno, line in section["content"]:
        if BULLET_RE.match(line):
            if current is None:
                current = {"title": "（未标注标题的经历）", "line": lineno, "bullets": []}
                blocks.append(current)
            current["bullets"].append((lineno, bullet_text(line)))
        else:
            current = {"title": excerpt(line, 50), "line": lineno, "bullets": []}
            blocks.append(current)
    return blocks


def experience_bullets(sections):
    """收集经历/项目章节里的全部要点，返回 [(行号, 要点, 章节名)]"""
    result = []
    for section in sections:
        if section["key"] in ("experience", "project"):
            for lineno, content in section["bullets"]:
                result.append((lineno, content, section["name"]))
    return result


def all_bullets(sections):
    """收集全部章节的要点"""
    result = []
    for section in sections:
        for lineno, content in section["bullets"]:
            result.append((lineno, content, section["name"]))
    return result


# ---------- 量化与日期 ----------

def quant_kinds(text):
    """返回文本命中的量化类型列表"""
    found = []
    for name, pattern in QUANT_PATTERNS:
        if pattern.search(text):
            found.append(name)
    return found


def has_quantification(text):
    """判断一条要点是否有量化成果"""
    return bool(quant_kinds(text))


def date_styles(text):
    """统计全文出现的日期写法风格，返回 [(风格名, 样例)]"""
    found = []
    for name, pattern in DATE_STYLE_PATTERNS:
        match = pattern.search(text)
        if match:
            found.append((name, match.group(0)))
    return found


# ---------- 规则检查（每条返回 [(行号, 说明), ...]）----------

def check_bullet_verbs(ctx):
    """要点是否以强动词开头"""
    hits = []
    for lineno, content, where in ctx["experience_bullets"]:
        first = content[:20]
        strong = False
        for verb in STRONG_VERBS_CN:
            if first.startswith(verb):
                strong = True
                break
        if not strong:
            first_word = re.match(r"[A-Za-z]+", first)
            if first_word and first_word.group(0).lower() in STRONG_VERBS_EN:
                strong = True
        if strong:
            continue
        weak = False
        for verb in WEAK_VERBS_CN:
            if first.startswith(verb):
                weak = True
                break
        if weak:
            hits.append((lineno, "要点以弱动词开头「%s」，成果感偏弱：%s" % (first[:4], excerpt(content))))
        else:
            hits.append((lineno, "要点没有以动词开头，读起来像职责清单：%s" % excerpt(content)))
    return hits


def check_quantification(ctx):
    """逐条要点判定有没有量化成果"""
    hits = []
    for lineno, content, where in ctx["experience_bullets"]:
        if not has_quantification(content):
            hits.append((lineno, "要点没有量化成果（数字/百分比/金额/倍数/时长）：%s" % excerpt(content)))
    return hits


def check_bullet_count(ctx):
    """每段经历的要点条数是否合适"""
    hits = []
    for block in ctx["blocks"]:
        count = len(block["bullets"])
        if count < MIN_BULLETS_PER_BLOCK:
            hits.append((block["line"],
                         "「%s」只有 %d 条要点，偏少（建议 %d~%d 条）"
                         % (block["title"], count, MIN_BULLETS_PER_BLOCK, MAX_BULLETS_PER_BLOCK)))
        elif count > MAX_BULLETS_PER_BLOCK:
            hits.append((block["line"],
                         "「%s」有 %d 条要点，偏多（建议 %d~%d 条，合并同类项）"
                         % (block["title"], count, MIN_BULLETS_PER_BLOCK, MAX_BULLETS_PER_BLOCK)))
    return hits


def check_first_person(ctx):
    """第一人称开头的要点"""
    hits = []
    for lineno, content, where in ctx["all_bullets"]:
        if content.startswith("我"):
            hits.append((lineno, "要点以第一人称「我」开头：%s" % excerpt(content)))
    return hits


def check_mixed_language(ctx):
    """中英混排严重、时态与大小写容易混乱的要点"""
    hits = []
    for lineno, content, where in ctx["all_bullets"]:
        cjk = len(re.findall(r"[\u4e00-\u9fff]", content))
        english_words = re.findall(r"[A-Za-z]{2,}", content)
        if cjk >= 4 and len(english_words) >= 3:
            hits.append((lineno, "中英混排较多（%d 个英文词），注意时态与大小写统一：%s"
                         % (len(english_words), excerpt(content))))
    return hits


def check_typos(ctx):
    """常见错别字/误用"""
    hits = []
    for lineno, line in split_lines(ctx["text"]):
        for wrong, right in TYPO_TABLE:
            if wrong in line:
                hits.append((lineno, "疑似错别字「%s」，应为「%s」：%s" % (wrong, right, excerpt(line))))
    return hits


def check_long_bullet(ctx):
    """过长要点"""
    hits = []
    for lineno, content, where in ctx["all_bullets"]:
        if len(content) > LONG_BULLET_LIMIT:
            hits.append((lineno, "要点过长（%d 字符 > %d），建议拆成 2 条或砍掉次要信息：%s"
                         % (len(content), LONG_BULLET_LIMIT, excerpt(content))))
    return hits


def check_date_format(ctx):
    """日期写法是否一致"""
    styles = ctx["date_styles"]
    if len(styles) <= 1:
        return []
    detail = "简历里同时出现 %s 等 %d 种日期写法" % (
        "、".join("%s：`%s`" % (name, sample) for name, sample in styles), len(styles))
    return [(0, detail + "，建议统一成一种（推荐 `2026.10` 或 `2026年10月`）")]


def check_contact(ctx):
    """联系方式缺失或写法不规范"""
    hits = []
    text = ctx["text"]
    sloppy = PHONE_SLOPPY_RE.search(text)
    has_phone = bool(PHONE_RE.search(text)) or bool(sloppy)
    has_email = bool(EMAIL_RE.search(text))
    if not has_phone and not has_email:
        hits.append((0, "没有识别到手机号和邮箱，招聘方无法联系到你"))
    elif not has_email:
        hits.append((0, "缺少邮箱（建议用姓名拼音邮箱，避免昵称邮箱）"))
    elif not has_phone:
        hits.append((0, "缺少手机号（建议写 11 位连续数字或 +86 形式）"))
    if sloppy:
        hits.append((0, "手机号写法不规范「%s」，建议写成连续 11 位数字或 +86-138xxxx1234" % sloppy.group(0)))
    return hits


def check_irrelevant(ctx):
    """与求职无关的信息"""
    hits = []
    for lineno, line in split_lines(ctx["text"]):
        found = [word for word in IRRELEVANT_WORDS if word in line]
        if found:
            hits.append((lineno, "含无关信息 %s（一般不需要写进简历）：%s"
                         % ("、".join(found), excerpt(line))))
    return hits


# ---------- 规则表 ----------

RULE_SPECS = [
    {"id": "RR001", "name": "要点开头动词", "severity": "建议", "scope": "经历",
     "desc": "要点应以强动词开头（主导/搭建/优化/提升……），弱动词和名词开头削弱成果感",
     "fix": "把「参与/协助/负责了」改成具体动作：主导、搭建、优化、提升，并补上结果。",
     "check": check_bullet_verbs, "quant": False},
    {"id": "RR002", "name": "量化成果", "severity": "警告", "scope": "经历",
     "desc": "每条要点都要有数字、百分比、金额、倍数或时长等量化成果",
     "fix": "给每条要点补一个可核对的数字：规模、提升幅度、节省成本、覆盖人数、耗时。",
     "check": check_quantification, "quant": True},
    {"id": "RR003", "name": "要点数量", "severity": "建议", "scope": "经历",
     "desc": "每段经历要点数应在 3~8 条之间，太少显得单薄，太多抓不住重点",
     "fix": "把同类要点合并，每段经历保留最能体现价值的 3~8 条。",
     "check": check_bullet_count, "quant": False},
    {"id": "RR004", "name": "第一人称", "severity": "建议", "scope": "全部",
     "desc": "简历要点不应用「我」开头",
     "fix": "删掉句首的「我」，直接用动词开头，如「主导搭建……」。",
     "check": check_first_person, "quant": False},
    {"id": "RR005", "name": "中英混排", "severity": "建议", "scope": "全部",
     "desc": "中英混排过多会造成时态、大小写混乱",
     "fix": "统一语言风格：术语保留英文并首次给出中文，其余用中文表述，时态统一用过去式或现在式。",
     "check": check_mixed_language, "quant": False},
    {"id": "RR006", "name": "错别字", "severity": "警告", "scope": "全部",
     "desc": "常见错别字与误用（做为/帐号/登陆……）",
     "fix": "逐条替换错别字，提交前用输入法校对或让工具通读一遍。",
     "check": check_typos, "quant": False},
    {"id": "RR007", "name": "要点过长", "severity": "建议", "scope": "全部",
     "desc": "单条要点超过 60 字符会淹没重点",
     "fix": "一条要点只讲一件事，拆成两条或砍掉背景描述，只留动作 + 结果。",
     "check": check_long_bullet, "quant": False},
    {"id": "RR008", "name": "日期格式", "severity": "警告", "scope": "全部",
     "desc": "日期写法应全文统一（2026.10 / 2026-10 / 2026年10月 三选一）",
     "fix": "全文统一成一种日期写法，并保证起止月份格式一致。",
     "check": check_date_format, "quant": False},
    {"id": "RR009", "name": "联系方式", "severity": "严重", "scope": "全部",
     "desc": "必须有手机号与邮箱，且写法规范",
     "fix": "在简历顶部写「手机：138xxxx1234　邮箱：name@example.com」，不要用昵称邮箱。",
     "check": check_contact, "quant": False},
    {"id": "RR010", "name": "无关信息", "severity": "建议", "scope": "全部",
     "desc": "身高/体重/婚姻/政治面貌等信息与求职无关",
     "fix": "删掉这些字段，把版面留给项目成果与技能证据。",
     "check": check_irrelevant, "quant": False},
]

RULES = RULE_SPECS


def analyze_resume(text, sections, blocks, exp_bullets, bullets, dstyles):
    """跑全部确定性规则，返回 (Issue 列表, 统计信息)"""
    ctx = {
        "text": text,
        "sections": sections,
        "blocks": blocks,
        "experience_bullets": exp_bullets,
        "all_bullets": bullets,
        "date_styles": dstyles,
    }
    issues = []
    for rule in RULES:
        try:
            hits = rule["check"](ctx)
        except Exception:
            hits = []
        for lineno, detail in hits:
            issues.append(Issue(rule["severity"], rule["id"], rule["name"], lineno, detail, rule["fix"]))
    issues.sort(key=lambda issue: (-SEVERITY_ORDER[issue.severity], issue.rule_id, issue.line))
    return issues


# ---------- JD 关键词提取与匹配 ----------

def find_skill_keywords(text):
    """从 JD（或简历）里提取技能关键词，返回 [{word, kind, weight, count}]"""
    found = []
    seen = set()
    for kind, words, weight in SKILL_KINDS:
        for word in words:
            key = word.lower()
            if key in seen:
                continue
            if _contains_skill(text, word):
                seen.add(key)
                found.append({"word": word, "kind": kind, "weight": weight,
                              "count": _count_skill(text, word)})
    return found


def _skill_pattern(word):
    """英文词用词边界匹配，中文词直接子串匹配"""
    if re.match(r"^[\u4e00-\u9fff]", word):
        return re.compile(re.escape(word))
    return re.compile(r"(?<![A-Za-z0-9+#])%s(?![A-Za-z0-9+#])" % re.escape(word), re.IGNORECASE)


def _contains_skill(text, word):
    return bool(_skill_pattern(word).search(text))


def _count_skill(text, word):
    return len(_skill_pattern(word).findall(text))


def match_jd(keywords, resume_text):
    """把 JD 关键词与简历对比，返回 (命中表, 缺失表, 匹配度 0-100)"""
    hits = []
    misses = []
    total_weight = 0.0
    hit_weight = 0.0
    for item in keywords:
        total_weight += item["weight"]
        if _contains_skill(resume_text, item["word"]):
            hit_weight += item["weight"]
            hits.append(dict(item))
        else:
            misses.append(dict(item))
    score = round(hit_weight / total_weight * 100, 1) if total_weight else 0.0
    return hits, misses, score


def transferable_hints(misses, hits):
    """给缺失技能提可迁移技能提示：已有相近技能怎么补"""
    owned = set(item["word"] for item in hits)
    hints = []
    for item in misses:
        related = TRANSFERABLE_HINTS.get(item["word"], ())
        evidence = [skill for skill in related if skill in owned]
        if evidence:
            hints.append({
                "skill": item["word"],
                "hint": "你已具备 %s，可在简历里补一条用它支撑 %s 的经历，或用 1~2 周补齐差距。"
                        % ("、".join(evidence), item["word"]),
            })
        elif related:
            hints.append({
                "skill": item["word"],
                "hint": "没有直接证据，可以先学 %s 打底，再补 %s。" % ("、".join(related[:2]), item["word"]),
            })
        else:
            hints.append({
                "skill": item["word"],
                "hint": "JD 明确要求但简历没有证据，要么补一段相关经历，要么在技能区写清对 %s 的掌握程度。"
                        % item["word"],
            })
    return hints


# ---------- 评分卡 ----------

def score_card(sections, issues, exp_bullets, match_score, skill_richness):
    """四维评分卡：结构 25 / 内容 30 / 量化 25 / 匹配 20"""
    found_keys = set(section["key"] for section in sections)
    structure = sum(SECTION_WEIGHTS[key] for key in found_keys if key in SECTION_WEIGHTS)

    content = 30
    for issue in issues:
        if issue.rule_id == "RR002":
            continue  # 量化单列一维
        if issue.severity == "严重":
            content -= 4
        elif issue.severity == "警告":
            content -= 2
        else:
            content -= 1
    content = clamp(content, 0, 30)

    if exp_bullets:
        quant_count = sum(1 for _, content_text, _ in exp_bullets if has_quantification(content_text))
        quant = clamp(25.0 * quant_count / len(exp_bullets), 0, 25)
    else:
        quant = 0

    if match_score is None:
        matched = clamp(20.0 * min(1.0, skill_richness / float(RICH_SKILL_TARGET)), 0, 20)
    else:
        matched = clamp(20.0 * match_score / 100.0, 0, 20)

    total = clamp(structure + content + quant + matched, 0, 100)
    return {
        "结构": clamp(structure, 0, 25),
        "内容": content,
        "量化": quant,
        "匹配": matched,
        "总分": total,
    }


SCORE_DIMS = ("结构", "内容", "量化", "匹配")
SCORE_LIMITS = {"结构": 25, "内容": 30, "量化": 25, "匹配": 20, "总分": 100}


def improvement_points(dim, sections, issues, exp_bullets, match_score, misses):
    """给某个评分维度列提升点"""
    points = []
    found_keys = set(section["key"] for section in sections)
    if dim == "结构":
        for key in ("education", "experience", "project", "skill", "basic", "certificate", "summary"):
            if key not in found_keys:
                points.append("补上「%s」章节。" % SECTION_NAME[key])
        if not points:
            points.append("章节齐全，可以再压缩篇幅（应届 1 页、3 年以上 2 页）。")
    elif dim == "内容":
        counts = {}
        for issue in issues:
            if issue.rule_id == "RR002":
                continue
            counts[issue.rule_id] = counts.get(issue.rule_id, 0) + 1
        for rule in RULES:
            if rule["quant"]:
                continue
            if rule["id"] in counts:
                points.append("%s %d 处：%s" % (rule["name"], counts[rule["id"]], rule["fix"]))
        if not points:
            points.append("内容层面没有明显硬伤，继续保持「动词 + 动作 + 结果」的写法。")
    elif dim == "量化":
        if exp_bullets:
            quant_count = sum(1 for _, content_text in exp_bullets if has_quantification(content_text))
            points.append("目前 %d 条经历要点里 %d 条有量化，目标是全部都有数字结果。"
                          % (len(exp_bullets), quant_count))
        points.append("每条要点至少带一个数字：规模、提升幅度、耗时、覆盖量、成本。")
    elif dim == "匹配":
        if match_score is None:
            points.append("没有提供 JD，匹配分按技能丰富度估算；提供 JD 后能给出命中/缺失技能表。")
        elif misses:
            points.append("缺失技能 %s：要么补相关经历，要么在技能区写清掌握程度。"
                          % "、".join(item["word"] for item in misses[:5]))
        else:
            points.append("JD 技能全部命中，重点是把每项技能绑定到具体项目证据上。")
    return points


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


REWRITE_SYSTEM_PROMPT = """你是一名资深简历教练与招聘专家，熟悉技术、产品、运营、财务等岗位的简历筛选标准。

用户会给你几条待优化的简历要点，可能还会附上目标岗位和 JD 关键词。请完成三件事：
1. 逐条给出改写示例：改写前（原句）、改写后（带量化成果的强动词句式）、改写理由。
   改写后要满足：强动词开头、有动作、有量化结果、不出现“我”、单句不超过 50 字。
   如果原句信息不足，可以在改写里用【补数字】占位，提示求职者填真实数据，不要编造。
2. 总结简历亮点与风险。
3. 针对 JD 给出匹配解释与补强建议。

输出要求：
1. 只输出一个 JSON 对象，不要输出解释文字，不要使用 Markdown 代码围栏。
2. 对象字段：
   - rewrites：数组，每项 {"before": "改写前", "after": "改写后", "reason": "理由"}
   - summary：字符串，亮点与风险总结
   - match_advice：字符串，JD 匹配解释与补强建议
3. 全部用中文。
"""


def build_model_user_prompt(bad_bullets, role, jd_summary):
    """拼接发给模型的改写任务"""
    lines = []
    for index, (lineno, content) in enumerate(bad_bullets, 1):
        lines.append("%d.（第 %d 行）%s" % (index, lineno, content))
    return (
        "【目标岗位】%s\n"
        "【JD 技能关键词】%s\n"
        "【待改写要点】\n%s\n"
        "请按系统提示输出 JSON。"
    ) % (role or "（未指定）", jd_summary or "（未提供 JD）", "\n".join(lines))


def parse_rewrites_json(text):
    """把模型回复解析成改写结果字典；解析不出来返回 None（调用方兜底）"""
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
    rewrites = []
    raw_rewrites = data.get("rewrites")
    if isinstance(raw_rewrites, list):
        for item in raw_rewrites:
            if not isinstance(item, dict):
                continue
            entry = {
                "before": str(item.get("before") or "").strip(),
                "after": str(item.get("after") or "").strip(),
                "reason": str(item.get("reason") or "").strip(),
            }
            if entry["before"] or entry["after"]:
                rewrites.append(entry)
    summary = str(data.get("summary") or "").strip()
    match_advice = str(data.get("match_advice") or "").strip()
    if not rewrites and not summary and not match_advice:
        return None
    return {"rewrites": rewrites, "summary": summary, "match_advice": match_advice}


def rewrite_with_model(bad_bullets, role, jd_summary, model, verbose=True):
    """调用模型做改写，返回 (结果字典或 None, 模型原始输出或 None)"""
    client = anthropic.Anthropic()
    if verbose:
        print("[模型改写] 正在请求模型：%s（要点 %d 条）" % (model, len(bad_bullets)))
    response = call_model(
        client,
        model,
        REWRITE_SYSTEM_PROMPT,
        [{"role": "user", "content": build_model_user_prompt(bad_bullets, role, jd_summary)}],
        max_tokens=8000,
    )
    raw = response_text(response)
    parsed = parse_rewrites_json(raw)
    if parsed is None and verbose:
        print("[模型改写] 返回内容不是合法 JSON，原文已放入报告附录。")
    return parsed, raw


def offline_rewrites(bad_bullets):
    """离线模式：按规则给每条要点生成改写指导（不做语言润色）"""
    rewrites = []
    for lineno, content in bad_bullets:
        reasons = []
        after_parts = []
        if content.startswith("我"):
            reasons.append("去掉句首第一人称")
            content_body = content[1:].lstrip("，,、 ")
        else:
            content_body = content
        first = content_body[:20]
        strong = any(first.startswith(verb) for verb in STRONG_VERBS_CN)
        if not strong:
            reasons.append("换强动词开头（主导/搭建/优化/提升）")
            after_parts.append("【强动词】")
        if not has_quantification(content):
            reasons.append("补量化结果")
            after_parts.append("【补数字：规模或提升幅度】")
        if len(content) > LONG_BULLET_LIMIT:
            reasons.append("拆成两条、砍背景")
        after = "%s%s" % ("".join(after_parts), content_body[:40]) if after_parts else content_body[:40]
        rewrites.append({
            "before": content,
            "after": "建议改写为：%s…" % after,
            "reason": "；".join(reasons) if reasons else "句式本身可以，继续核对数字真实性",
        })
    return rewrites


# ---------- 报告 ----------

def score_table(scores):
    lines = ["| 维度 | 得分 | 满分 |", "| --- | --- | --- |"]
    for dim in SCORE_DIMS:
        lines.append("| %s | %d | %d |" % (dim, scores[dim], SCORE_LIMITS[dim]))
    lines.append("| **总分** | **%d** | %d |" % (scores["总分"], SCORE_LIMITS["总分"]))
    return "\n".join(lines)


def keyword_table(items, resume_text=None):
    """渲染技能表；传入 resume_text 时统计简历里的出现次数，否则次数列写 —"""
    lines = ["| 技能 | 类别 | 权重 | 简历出现次数 |", "| --- | --- | --- | --- |"]
    for item in items:
        if resume_text is not None:
            count_cell = str(_count_skill(resume_text, item["word"]))
        else:
            count_cell = "—"
        lines.append("| %s | %s | %.1f | %s |"
                     % (cell(item["word"]), item["kind"], item["weight"], count_cell))
    return "\n".join(lines)


def cell(text):
    """把文本塞进 Markdown 表格单元格前转义竖线"""
    return str(text).replace("|", "\\|").replace("\n", " ")


def issue_section(issues, severity):
    rows = [issue for issue in issues if issue.severity == severity]
    if not rows:
        return "（无）\n"
    parts = []
    for index, issue in enumerate(rows, 1):
        location = "第 %d 行" % issue.line if issue.line else "全文"
        parts.append("%d. **[%s] %s**（%s）\n   - 问题：%s\n   - 修复：%s"
                     % (index, issue.rule_id, issue.name, location, issue.detail, issue.fix))
    return "\n".join(parts) + "\n"


def build_report(resume_path, text, sections, blocks, issues, scores, rewrite_data,
                 raw_output, hits, misses, match_score, jd_summary, role, offline,
                 model, truncated=False, generated_at=None):
    """生成中文 Markdown 诊断报告"""
    generated_at = generated_at or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    out = []
    out.append("# 简历诊断报告")
    out.append("")
    out.append("> 生成时间：%s　|　简历：%s　|　目标岗位：%s　|　模式：%s　|　模型：%s"
               % (generated_at, resume_path, role or "（未指定）",
                  "离线诊断（不调用模型）" if offline else "诊断 + 模型改写", model))
    out.append("")

    # 评分卡
    out.append("## 一、评分卡")
    out.append("")
    out.append(score_table(scores))
    out.append("")
    out.append("评分口径：结构=各章节权重之和（工作经历 5、教育/技能 4、其余 3，满分 25）；"
               "内容=30 分减去非量化规则的问题扣分（严重 -4、警告 -2、建议 -1）；"
               "量化=25×有量化要点占比；匹配=20×JD 关键词加权覆盖率"
               "（无 JD 时按技能关键词丰富度估算，满分 20）。")
    out.append("")
    exp_pairs = [(no, content) for block in blocks for no, content in block["bullets"]]
    for dim in SCORE_DIMS:
        points = improvement_points(dim, sections, issues, exp_pairs, match_score, misses)
        out.append("- **%s（%d/%d）**：%s"
                   % (dim, scores[dim], SCORE_LIMITS[dim], "".join(points)))
    out.append("")

    # 结构体检
    out.append("## 二、简历结构体检")
    out.append("")
    out.append("| 章节 | 状态 | 位置 | 要点数 |")
    out.append("| --- | --- | --- | --- |")
    found_keys = set()
    for key, name, _ in SECTION_TYPES:
        section = next((item for item in sections if item["key"] == key), None)
        if section:
            found_keys.add(key)
            out.append("| %s | 已包含 | 第 %d 行 | %d |" % (name, section["line"], len(section["bullets"])))
        else:
            out.append("| %s | **缺失** | — | 0 |" % name)
    for section in sections:
        if section["key"] == "unknown":
            out.append("| %s | 待整理 | 第 %d 行 | %d |"
                       % (section["title"], section["line"], len(section["bullets"])))
    out.append("")
    missing_core = [SECTION_NAME[key] for key in KEY_SECTIONS if key not in found_keys
                    and not (key == "experience" and "project" in found_keys)]
    if missing_core:
        out.append("**结构警告**：缺少关键章节 %s，建议补齐（教育背景 / 工作或项目经历 / 技能）。"
                   % "、".join(missing_core))
    else:
        out.append("**结构结论**：关键章节（教育、经历、技能）齐全。")
    out.append("")
    out.append("### 每段经历的要点数")
    out.append("")
    out.append("| 经历 | 起始行 | 要点数 | 评价 |")
    out.append("| --- | --- | --- | --- |")
    for block in blocks:
        count = len(block["bullets"])
        if count < MIN_BULLETS_PER_BLOCK:
            verdict = "偏少"
        elif count > MAX_BULLETS_PER_BLOCK:
            verdict = "偏多"
        else:
            verdict = "合适"
        out.append("| %s | %d | %d | %s |" % (cell(block["title"]), block["line"], count, verdict))
    out.append("")

    # 问题清单
    out.append("## 三、问题清单")
    out.append("")
    out.append("### 严重")
    out.append("")
    out.append(issue_section(issues, "严重"))
    out.append("### 警告")
    out.append("")
    out.append(issue_section(issues, "警告"))
    out.append("### 建议")
    out.append("")
    out.append(issue_section(issues, "建议"))

    # 改写示例
    out.append("## 四、逐条改写示例（差 → 好）")
    out.append("")
    if rewrite_data and rewrite_data.get("rewrites"):
        for index, item in enumerate(rewrite_data["rewrites"], 1):
            out.append("### %d. 改写示例" % index)
            out.append("")
            out.append("- **改写前**：%s" % (item["before"] or "（无）"))
            out.append("- **改写后**：%s" % (item["after"] or "（无）"))
            out.append("- **理由**：%s" % (item["reason"] or "（无）"))
            out.append("")
    elif offline:
        out.append("（离线模式下只给改写指导，不调用模型润色；带【补数字】的地方请填真实数据。）")
        out.append("")
    else:
        out.append("（模型未返回可解析的改写示例，原始输出见附录。）")
        out.append("")
    if rewrite_data and rewrite_data.get("summary"):
        out.append("### 亮点与风险总结")
        out.append("")
        out.append(rewrite_data["summary"])
        out.append("")

    # JD 匹配
    out.append("## 五、JD 匹配分析")
    out.append("")
    if match_score is None:
        out.append("未提供 JD，跳过关键词匹配；下面给出简历里识别到的技能关键词，供对照 JD 使用。")
        out.append("")
        if hits:
            out.append(keyword_table(hits, text))
            out.append("")
    else:
        out.append("- **匹配度（加权覆盖率）**：%.1f / 100" % match_score)
        out.append("- **JD 关键词**：%s" % (jd_summary or "（见下表）"))
        out.append("")
        out.append("### 命中技能")
        out.append("")
        out.append(keyword_table(hits, text) if hits else "（无命中）")
        out.append("")
        out.append("### 缺失技能")
        out.append("")
        out.append(keyword_table(misses) if misses else "（无缺失，JD 技能全部命中）")
        out.append("")
        out.append("### 可迁移技能提示")
        out.append("")
        hints = transferable_hints(misses, hits)
        if hints:
            for hint in hints:
                out.append("- **%s**：%s" % (hint["skill"], hint["hint"]))
        else:
            out.append("- （暂无缺失技能，重点是把命中技能绑定到项目证据上。）")
        out.append("")
        if rewrite_data and rewrite_data.get("match_advice"):
            out.append("### 模型给出的匹配解释")
            out.append("")
            out.append(rewrite_data["match_advice"])
            out.append("")

    # 路线图
    out.append("## 六、优化路线图")
    out.append("")
    def summarize(issue_list):
        """按规则合并同类问题，避免同一条修复建议重复刷屏"""
        merged = {}
        order = []
        for issue in issue_list:
            if issue.name not in merged:
                merged[issue.name] = {"count": 0, "fix": issue.fix}
                order.append(issue.name)
            merged[issue.name]["count"] += 1
        return ["%s ×%d：%s" % (name, merged[name]["count"], merged[name]["fix"])
                for name in order]

    severe = [issue for issue in issues if issue.severity == "严重"]
    warns = [issue for issue in issues if issue.severity == "警告"]
    advice = [issue for issue in issues if issue.severity == "建议"]
    out.append("**第一阶段（今天就改，1 小时内）**")
    out.append("")
    severe_lines = summarize(severe)
    for line in severe_lines[:5]:
        out.append("- %s" % line)
    if not severe_lines:
        out.append("- 没有严重问题，先把警告项清掉。")
    out.append("")
    out.append("**第二阶段（本周内）**")
    out.append("")
    warn_lines = summarize(warns)
    for line in warn_lines[:5]:
        out.append("- %s" % line)
    if not warn_lines:
        out.append("- 给每条经历要点补量化数字。")
    out.append("")
    out.append("**第三阶段（长期积累）**")
    out.append("")
    advice_lines = summarize(advice)
    for line in advice_lines[:5]:
        out.append("- %s" % line)
    if not advice_lines:
        out.append("- 持续更新项目成果，每季度复盘一次简历。")
    out.append("")

    # 附录
    out.append("## 附录：统计明细")
    out.append("")
    total_bullets = sum(len(section["bullets"]) for section in sections)
    out.append("- 简历长度：%d 字符 / %d 行" % (len(text), len(text.splitlines())))
    out.append("- 识别到的章节：%d 个（含未命名 %d 个）"
               % (len(sections), sum(1 for section in sections if section["key"] == "unknown")))
    out.append("- 要点总数：%d 条" % total_bullets)
    out.append("- 问题总数：%d 条（严重 %d、警告 %d、建议 %d）"
               % (len(issues),
                  sum(1 for issue in issues if issue.severity == "严重"),
                  sum(1 for issue in issues if issue.severity == "警告"),
                  sum(1 for issue in issues if issue.severity == "建议")))
    if truncated:
        out.append("- 输入超过 %d 字符已截断，诊断只覆盖前一部分" % MAX_INPUT_CHARS)
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


RESUME_SAMPLE = """张三
手机：13800001234　邮箱：zhangsan@example.com

## 基本信息
姓名：张三　性别：男　身高：175cm　婚姻状况：未婚

## 教育背景
2018.09-2022.06　某大学　计算机科学与技术　本科

## 工作经历
某科技有限公司 | 数据分析师 | 2022-07 - 至今
- 负责日常数据报表开发，完成 3 张核心报表，覆盖 5 个业务线
- 参与用户增长分析，效果差不多比较好
- 我主导搭建指标体系，把口径不一致问题从 12 个降到 2 个
- 协助团队做数据看板，做为支撑工具
- 优化 SQL 查询性能，平均响应时间从 3.2 秒降到 0.8 秒，提升 75%
- 负责数据质量监控，负责异常排查，负责报表维护
- 搭建调度任务体系，把人工取数耗时从 2 小时缩短到 10 分钟
- 推动跨部门指标对齐，组织 6 场评审会，覆盖 4 个部门
- 这是一条非常长的要点，用来测试超过六十个字符的超长要点检测规则，它会一直写下去，直到明显超过限制为止，方便验证规则确实生效

## 项目经历
用户画像项目 | 2021年3月-2021年6月
- 设计用户标签体系，产出 48 个标签

## 技能
- 熟悉 Python、SQL、Excel、Tableau、机器学习
- 了解数据分析、数据可视化、A/B 测试

## 证书
- 英语六级

## 自我评价
本人工作认真负责。
"""

JD_SAMPLE = """岗位：数据分析师
要求：
- 熟练掌握 SQL 和 Python，有数据分析经验
- 熟悉 Tableau 或 Power BI，能做数据可视化
- 了解机器学习，有 A/B 测试经验者优先
- 良好的沟通与跨部门协作能力
"""


def run_self_test():
    """离线自检：用 tempfile 造简历与 JD 样本，覆盖全部确定性逻辑，不联网、不需要 API key"""
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        resume_path = root / "resume.md"
        resume_path.write_text(RESUME_SAMPLE, encoding="utf-8")
        jd_path = root / "jd.txt"
        jd_path.write_text(JD_SAMPLE, encoding="utf-8")

        # --- 读取 ---
        text, truncated = read_text_file(resume_path)
        assert not truncated
        assert "数据分析师" in text
        jd_text, _ = read_text_file(jd_path)

        # --- 章节识别 ---
        sections, seen = parse_sections(text)
        assert seen
        keys = set(section["key"] for section in sections)
        for expected in ("basic", "education", "experience", "project", "skill",
                         "certificate", "summary"):
            assert expected in keys, "章节未识别：%s" % expected
        exp_section = next(section for section in sections if section["key"] == "experience")
        assert exp_section["line"] > 0
        assert len(exp_section["bullets"]) >= 8
        unknown_sections, seen_unknown = parse_sections("一段没有标题的文本\n- 要点一\n")
        assert not seen_unknown
        assert unknown_sections and unknown_sections[0]["key"] == "unknown"

        # --- 经历块与要点 ---
        blocks = split_experience_blocks(exp_section)
        assert len(blocks) == 1
        assert len(blocks[0]["bullets"]) >= 9   # >8，应触发“偏多”
        proj_section = next(section for section in sections if section["key"] == "project")
        proj_blocks = split_experience_blocks(proj_section)
        assert len(proj_blocks[0]["bullets"]) == 1   # <3，应触发“偏少”
        exp_bullets = experience_bullets(sections)
        bullets = all_bullets(sections)
        assert len(exp_bullets) >= 10
        assert len(bullets) > len(exp_bullets)

        # --- 量化统计 ---
        assert has_quantification("完成 3 张核心报表，覆盖 5 个业务线")
        assert has_quantification("提升 75%")
        assert has_quantification("耗时从 2 小时缩短到 10 分钟")
        assert not has_quantification("参与用户增长分析")
        kinds = quant_kinds("节省成本 20 万元，效率提升 2 倍，周期 3 周")
        assert "金额" in kinds and "倍数" in kinds and "时长" in kinds

        # --- 动词规则 ---
        verb_hits = check_bullet_verbs({
            "text": text, "sections": sections, "blocks": blocks,
            "experience_bullets": exp_bullets, "all_bullets": bullets,
            "date_styles": [],
        })
        assert verb_hits, "动词规则未命中"
        assert any("参与" in detail for _, detail in verb_hits)
        assert not any("负责日常数据报表开发" in detail for _, detail in verb_hits), "强动词要点被误报"
        assert any(detail.startswith("要点没有以动词开头") for _, detail in verb_hits)

        # --- 日期不一致 ---
        styles = date_styles(text)
        assert len(styles) >= 3
        date_hits = check_date_format({
            "text": text, "sections": sections, "blocks": blocks,
            "experience_bullets": exp_bullets, "all_bullets": bullets,
            "date_styles": styles,
        })
        assert date_hits and "种日期写法" in date_hits[0][1]
        single_styles = date_styles("2020年1月-2021年2月")
        assert len(single_styles) == 1
        assert not check_date_format({
            "text": "x", "sections": [], "blocks": [],
            "experience_bullets": [], "all_bullets": [], "date_styles": single_styles,
        })

        # --- 全量规则 ---
        issues = analyze_resume(text, sections, blocks, exp_bullets, bullets, styles)
        rule_ids = set(issue.rule_id for issue in issues)
        for expected in ("RR001", "RR002", "RR003", "RR004", "RR005",
                         "RR006", "RR007", "RR008", "RR010"):
            assert expected in rule_ids, "规则未命中：%s" % expected
        assert any("身高" in issue.detail for issue in issues)
        assert any("做为" in issue.detail for issue in issues)
        assert any("第一人称" in issue.detail for issue in issues)
        assert any(issue.rule_id == "RR007" for issue in issues)
        assert issues[0].severity in SEVERITIES
        assert all(issue.fix for issue in issues)

        # --- 联系方式 ---
        contact_ctx = {
            "text": "简历正文，没有任何电话邮箱", "sections": [], "blocks": [],
            "experience_bullets": [], "all_bullets": [], "date_styles": [],
        }
        contact_hits = check_contact(contact_ctx)
        assert contact_hits and "无法联系" in contact_hits[0][1]
        sloppy_ctx = dict(contact_ctx, text="电话：138 0000 1234　邮箱：a@b.com")
        assert any("写法不规范" in detail for _, detail in check_contact(sloppy_ctx))

        # --- JD 提取与匹配 ---
        keywords = find_skill_keywords(jd_text)
        words = set(item["word"] for item in keywords)
        for expected in ("SQL", "Python", "Tableau", "机器学习", "数据分析"):
            assert expected in words, "JD 关键词未提取：%s" % expected
        hits, misses, match_score = match_jd(keywords, text)
        hit_words = set(item["word"] for item in hits)
        miss_words = set(item["word"] for item in misses)
        assert "SQL" in hit_words and "Python" in hit_words and "Tableau" in hit_words
        assert "机器学习" in hit_words
        assert "Power BI" in miss_words
        assert hit_words.isdisjoint(miss_words)
        assert hit_words | miss_words == words
        total_weight = sum(item["weight"] for item in keywords)
        hit_weight = sum(item["weight"] for item in hits)
        assert abs(match_score - round(hit_weight / total_weight * 100, 1)) < 0.01
        assert 0 < match_score < 100
        hints = transferable_hints(misses, hits)
        assert hints and all(hint["hint"] for hint in hints)
        assert any("Tableau" in hint["hint"] for hint in hints), "可迁移提示未给出已有技能证据"

        # --- 评分卡 ---
        scores = score_card(sections, issues, exp_bullets, match_score, 0)
        for dim in SCORE_DIMS:
            assert 0 <= scores[dim] <= SCORE_LIMITS[dim]
        assert scores["总分"] == clamp(
            scores["结构"] + scores["内容"] + scores["量化"] + scores["匹配"], 0, 100)
        weak_scores = score_card([], [], [], None, 0)
        assert weak_scores["总分"] < scores["总分"]
        assert weak_scores["匹配"] == 0
        rich_scores = score_card([], [], [], None, RICH_SKILL_TARGET)
        assert rich_scores["匹配"] == 20

        # --- 模型 JSON 解析兜底 ---
        good_json = (
            "```json\n"
            '{"rewrites":[{"before":"参与分析","after":"主导分析，覆盖 5 条业务线",'
            '"reason":"强动词 + 量化"}],"summary":"亮点：量化充分","match_advice":"补 Power BI"}\n'
            "```"
        )
        parsed = parse_rewrites_json(good_json)
        assert parsed and parsed["rewrites"][0]["after"].startswith("主导")
        assert parsed["summary"] and parsed["match_advice"]
        assert parse_rewrites_json("随便一段话") is None
        assert parse_rewrites_json("{oops") is None
        assert parse_rewrites_json('{"rewrites":"x"}') is None
        weird = '{"rewrites":[{"before":"a"}],"summary":"","match_advice":""}'
        weird_parsed = parse_rewrites_json(weird)
        assert weird_parsed and weird_parsed["rewrites"][0]["before"] == "a"

        # --- call_model 三级降级（离线假客户端：第一层 TypeError，第二层成功） ---
        fake = _FakeClient()
        result = call_model(fake, "fake-model", "system", [{"role": "user", "content": "hi"}])
        assert result["ok"] is True
        assert fake.state["calls"] == 2

        # --- 离线改写指导 ---
        bad = [(no, content) for no, content, _ in exp_bullets if not has_quantification(content)]
        offline_items = offline_rewrites(bad[:3])
        assert len(offline_items) == min(3, len(bad))
        assert all(item["reason"] for item in offline_items)
        assert any("补数字" in item["after"] or "强动词" in item["after"] for item in offline_items)

        # --- 报告渲染 ---
        report = build_report(
            "resume.md", text, sections, blocks, issues, scores, parsed, "",
            hits, misses, match_score, "SQL、Python、Tableau", "数据分析师", False,
            "claude-opus-5", generated_at="2026-01-01 00:00:00",
        )
        for heading in ("# 简历诊断报告", "## 一、评分卡", "## 二、简历结构体检",
                        "## 三、问题清单", "## 四、逐条改写示例（差 → 好）",
                        "## 五、JD 匹配分析", "## 六、优化路线图", "## 附录：统计明细"):
            assert heading in report, "报告缺少：%s" % heading
        assert "Power BI" in report
        assert "主导分析" in report
        assert "缺少关键章节" not in report

        offline_report = build_report(
            "resume.md", text, sections, blocks, issues,
            score_card(sections, issues, exp_bullets, None, 8),
            {"rewrites": offline_rewrites(bad[:2]), "summary": "", "match_advice": ""},
            "自由格式输出", [], [], None, "", "数据分析师", True, "claude-opus-5",
            generated_at="2026-01-01 00:00:00",
        )
        assert "离线诊断" in offline_report
        assert "未提供 JD" in offline_report
        assert "附录：模型原始输出" in offline_report

        # --- 缺章节警告 ---
        short_text = "张三\n## 工作经历\n- 负责销售，完成 100 万业绩\n"
        short_sections, _ = parse_sections(short_text)
        short_report = build_report(
            "short.md", short_text, short_sections,
            split_experience_blocks(short_sections[0]) if short_sections else [],
            [], score_card(short_sections, [], [], None, 0), None, "",
            [], [], None, "", "销售", True, "claude-opus-5",
            generated_at="2026-01-01 00:00:00",
        )
        assert "缺少关键章节" in short_report

        # --- 报告落盘 ---
        output_path = root / "review-report.md"
        output_path.write_text(report, encoding="utf-8")
        assert output_path.read_text(encoding="utf-8").startswith("# 简历诊断报告")

    print("SELF-TEST OK")


# ---------- CLI ----------

def build_parser():
    parser = argparse.ArgumentParser(
        description="简历诊断与 JD 匹配：确定性体检（结构/动词/量化/日期/联系方式）+ 模型改写，输出中文 Markdown 报告",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            "  python3 agent.py 简历.txt --jd 数据分析师JD.txt --role 数据分析师\n"
            "  python3 agent.py 简历.md --offline\n"
            "  python3 agent.py 简历.txt --jd jd.txt --model qwen3.8-max --output 诊断.md\n"
        ),
    )
    parser.add_argument("resume", nargs="?", help="简历文件路径（.txt / .md）")
    parser.add_argument("--jd", default="", help="职位描述文件路径（可选，提供后做技能匹配打分）")
    parser.add_argument("--output", default=DEFAULT_OUTPUT,
                        help="报告写入路径（默认 %s）" % DEFAULT_OUTPUT)
    parser.add_argument("--role", default="", help="目标岗位名称，写进报告和模型提示")
    parser.add_argument("--model", default=DEFAULT_MODEL,
                        help="改写使用的模型（默认 %s）" % DEFAULT_MODEL)
    parser.add_argument("--offline", action="store_true",
                        help="只做确定性诊断与改写指导，不调用模型（不需要 API key）")
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

    if not args.resume:
        parser.error("请提供简历文件路径（.txt / .md）")

    resume_path = Path(args.resume).expanduser()
    if not resume_path.is_file():
        print("错误：找不到简历文件：%s" % resume_path, file=sys.stderr)
        sys.exit(1)
    try:
        text, truncated = read_text_file(resume_path)
    except OSError as exc:
        print("错误：读取简历失败：%s" % exc, file=sys.stderr)
        sys.exit(1)
    if not text.strip():
        print("[警告] 简历文件内容为空，按空简历继续生成体检报告。", file=sys.stderr)

    jd_text = ""
    if args.jd:
        jd_path = Path(args.jd).expanduser()
        if not jd_path.is_file():
            print("错误：找不到 JD 文件：%s" % jd_path, file=sys.stderr)
            sys.exit(1)
        try:
            jd_text, _ = read_text_file(jd_path)
        except OSError as exc:
            print("错误：读取 JD 失败：%s" % exc, file=sys.stderr)
            sys.exit(1)

    print("[诊断] 简历 %s，长度 %d 字符" % (resume_path.name, len(text)))
    sections, _ = parse_sections(text)
    blocks = []
    for section in sections:
        if section["key"] in ("experience", "project"):
            blocks.extend(split_experience_blocks(section))
    exp_bullets = experience_bullets(sections)
    bullets = all_bullets(sections)
    styles = date_styles(text)
    issues = analyze_resume(text, sections, blocks, exp_bullets, bullets, styles)

    hits, misses, match_score = ([], [], None)
    keywords = []
    if jd_text:
        keywords = find_skill_keywords(jd_text)
        hits, misses, match_score = match_jd(keywords, text)
        print("[匹配] JD 关键词 %d 个，命中 %d 个，缺失 %d 个，匹配度 %.1f"
              % (len(keywords), len(hits), len(misses), match_score))
    else:
        print("[匹配] 未提供 JD，匹配分按技能丰富度估算。")

    skill_words = find_skill_keywords(text)
    scores = score_card(sections, issues, exp_bullets, match_score, len(skill_words))
    print("[诊断] 章节 %d 个，要点 %d 条，问题 %d 条" % (len(sections), len(bullets), len(issues)))

    bad_bullets = [
        (lineno, content) for lineno, content, _ in exp_bullets
        if not has_quantification(content) or content.startswith("我")
    ][:MAX_REWRITES]

    rewrite_data = None
    raw_output = ""
    if args.offline:
        print("[离线模式] 跳过模型改写，只输出确定性诊断结果与改写指导。")
        if bad_bullets:
            rewrite_data = {"rewrites": offline_rewrites(bad_bullets),
                            "summary": "", "match_advice": ""}
    else:
        if not os.environ.get("ANTHROPIC_API_KEY") and not os.environ.get("ANTHROPIC_AUTH_TOKEN"):
            print(
                "提示：没有检测到 ANTHROPIC_API_KEY / ANTHROPIC_AUTH_TOKEN，无法调用模型做改写。\n"
                "请先设置：\n"
                "  macOS / Linux：export ANTHROPIC_API_KEY=你的key\n"
                "  Windows PowerShell：$env:ANTHROPIC_API_KEY=\"你的key\"\n"
                "或者加 --offline 只做确定性诊断。",
                file=sys.stderr,
            )
            sys.exit(1)
        if not bad_bullets:
            print("[模型改写] 没有需要改写的要点，跳过模型调用。")
        else:
            jd_summary = "、".join(item["word"] for item in keywords[:20])
            try:
                rewrite_data, raw_output = rewrite_with_model(
                    bad_bullets, args.role, jd_summary, args.model)
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
                print("错误：模型服务返回错误（%s）：%s" % (exc.status_code, exc.message),
                      file=sys.stderr)
                sys.exit(1)

    jd_summary = "、".join(item["word"] for item in keywords[:20])
    report = build_report(
        resume_path.name, text, sections, blocks, issues, scores,
        rewrite_data, raw_output, hits, misses, match_score, jd_summary,
        args.role, args.offline, args.model, truncated=truncated,
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
    print("[完成] 总分 %d（结构 %d/25、内容 %d/30、量化 %d/25、匹配 %d/20），问题 %d 条"
          % (scores["总分"], scores["结构"], scores["内容"], scores["量化"],
             scores["匹配"], len(issues)))
    if raw_output and rewrite_data is None:
        print("[提示] 模型输出不是合法 JSON，已原样放入报告附录。")


if __name__ == "__main__":
    main()
