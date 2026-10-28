#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
面试准备助手（interview-prep）—— 题库抽题 + 岗位画像 + 复习计划 + 模拟面试

内置 42 道分类题库（算法与数据结构 / 系统设计 / 编程语言 / 项目深挖 / 行为STAR / HR反问），
按 --type / --count / JD 权重抽题（固定随机种子保证可复现），渲染 STAR / PREP 答题框架骨架，
读写练习记录并按 1/2/4/7/15 天间隔安排复习；--mock 时让模型逐题模拟追问并四维打分。
除 anthropic SDK 外只用标准库。

用法：
    python3 agent.py [--jd 岗位描述.txt] [--type 技术|行为|产品|综合] [--count 10]
                     [--mock] [--record 练习记录.json] [--output 面试准备.md] [--offline]

示例：
    python3 agent.py --jd 岗位描述.txt --type 技术 --count 12
    python3 agent.py --type 行为 --output 行为面准备.md --offline
    python3 agent.py --jd 岗位描述.txt --mock --record 练习记录.json
    python3 agent.py --type 综合 --seed 7 --model qwen3.8-max

环境要求：
    1. 调用模型（--mock）需要设置 API key（--offline 与 --self-test 不需要）：
           macOS / Linux：        export ANTHROPIC_API_KEY=你的key
           Windows PowerShell：   $env:ANTHROPIC_API_KEY="你的key"
    2. 安装依赖：
           pip3 install -r requirements.txt
    3. 走第三方 Anthropic 兼容网关时，用 --model 指定网关认得的模型名，例如：
           python3 agent.py --mock --model qwen3.8-max
"""

import argparse
import json
import os
import random
import re
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

import anthropic

# 默认模型（可用 --model 参数或 INTERVIEW_PREP_MODEL 环境变量更换）
DEFAULT_MODEL = os.environ.get("INTERVIEW_PREP_MODEL", "claude-opus-5")

DEFAULT_OUTPUT = "interview-prep.md"
DEFAULT_COUNT = 10
DEFAULT_SEED = 42
REVIEW_INTERVALS = (1, 2, 4, 7, 15)      # 复习间隔（天）
SCORE_RANGE = (1, 5)                     # 自评分范围
MAX_JD_CHARS = 20_000

QUESTION_TYPES = ("技术", "行为", "产品", "综合")
DIFFICULTIES = ("基础", "进阶", "挑战")
CATEGORIES = ("算法与数据结构", "系统设计", "编程语言", "项目深挖", "行为STAR", "HR反问")

# 每种面试类型覆盖哪些题库分类
TYPE_CATEGORIES = {
    "技术": ("算法与数据结构", "系统设计", "编程语言", "项目深挖"),
    "行为": ("行为STAR", "项目深挖", "HR反问"),
    "产品": ("项目深挖", "系统设计", "行为STAR", "HR反问"),
    "综合": CATEGORIES,
}

# 难度权重（抽题时用）
DIFFICULTY_WEIGHTS = {"基础": 1.0, "进阶": 1.2, "挑战": 0.8}

# ---------- 内置题库（≥40 题） ----------
# 字段：id、分类、难度、标签、题目、考察点、回答框架（STAR / PREP / 要点）

_RAW_QUESTIONS = (
    # 算法与数据结构
    ("Q001", "算法与数据结构", "基础", ("数组", "哈希"),
     "手写「两数之和」，并说明为什么用哈希表而不是双重循环。",
     "哈希表换时间的思路、边界条件、复杂度分析", "要点"),
    ("Q002", "算法与数据结构", "基础", ("链表", "递归"),
     "反转单链表，分别写迭代和递归两种版本。",
     "指针操作的准确性、递归的终止条件、空间复杂度对比", "要点"),
    ("Q003", "算法与数据结构", "进阶", ("树", "BFS"),
     "按层打印二叉树，并判断一棵二叉树是否对称。",
     "队列的使用、递归与迭代的取舍、空节点处理", "要点"),
    ("Q004", "算法与数据结构", "进阶", ("动态规划",),
     "讲解爬楼梯 / 0-1 背包问题的状态定义与转移方程。",
     "状态定义、转移方程、初始条件、空间压缩", "要点"),
    ("Q005", "算法与数据结构", "进阶", ("排序", "分治"),
     "手写快速排序，分析平均与最坏时间复杂度，如何避免最坏情况。",
     "分区逻辑、随机 pivot、递归深度与栈溢出风险", "要点"),
    ("Q006", "算法与数据结构", "挑战", ("图", "拓扑排序"),
     "用拓扑排序判断任务依赖是否有环，并举一个实际应用场景。",
     "入度统计、队列遍历、环检测、工程里的调度应用", "要点"),
    ("Q007", "算法与数据结构", "挑战", ("字符串", "滑动窗口"),
     "求字符串的最小覆盖子串，讲清滑动窗口的收缩条件。",
     "窗口合法性维护、双指针、复杂度 O(n) 的原因", "要点"),
    # 系统设计
    ("Q010", "系统设计", "基础", ("容量估算", "短链"),
     "设计一个短链服务：先做容量估算，再画出核心链路。",
     "读写比、QPS 估算、发号器方案、302 跳转", "要点"),
    ("Q011", "系统设计", "进阶", ("缓存", "Redis"),
     "如何设计缓存？缓存穿透、击穿、雪崩分别怎么防。",
     "空值缓存、互斥锁重建、过期时间打散、多级缓存", "要点"),
    ("Q012", "系统设计", "进阶", ("消息队列", "可靠性"),
     "用消息队列削峰填谷，怎么保证消息不丢、不重复。",
     "生产端确认、持久化、消费位点提交、幂等消费", "要点"),
    ("Q013", "系统设计", "进阶", ("分库分表", "扩容"),
     "什么情况下要分库分表？如何平滑扩容？",
     "垂直/水平拆分、分片键选择、双写迁移、数据校验", "要点"),
    ("Q014", "系统设计", "挑战", ("一致性", "事务"),
     "如何保证订单与库存的一致性？对比本地消息表与分布式事务。",
     "最终一致性、消息表、TCC、对账兜底", "要点"),
    ("Q015", "系统设计", "挑战", ("高并发", "秒杀"),
     "设计一个秒杀系统，讲清如何挡住超额流量。",
     "多层限流、库存预扣、异步化、防刷与兜底", "要点"),
    ("Q016", "系统设计", "基础", ("监控", "告警"),
     "一个服务上线后如何做监控告警？看哪些指标？",
     "RED/USE 指标、日志与链路追踪、告警分级、值班机制", "要点"),
    # 编程语言
    ("Q020", "编程语言", "基础", ("python", "并发"),
     "Python 的 GIL 是什么？对多线程和多进程有什么影响？",
     "GIL 的作用、IO 密集 vs CPU 密集、multiprocessing 与协程", "要点"),
    ("Q021", "编程语言", "基础", ("java", "JVM"),
     "JVM 的垃圾回收机制是什么？常见收集器有什么区别？",
     "可达性分析、分代假设、CMS/G1/ZGC 的取舍", "要点"),
    ("Q022", "编程语言", "进阶", ("python", "装饰器"),
     "实现一个带参数的装饰器，并说明上下文管理器的两种写法。",
     "闭包与三层嵌套、functools.wraps、__enter__/__exit__、contextmanager", "要点"),
    ("Q023", "编程语言", "进阶", ("go", "调度"),
     "Goroutine 的调度模型是怎样的？channel 满了会怎样？",
     "GMP 模型、抢占式调度、无缓冲 vs 有缓冲 channel、select", "要点"),
    ("Q024", "编程语言", "进阶", ("java", "并发容器"),
     "ConcurrentHashMap 如何保证线程安全？和 Hashtable 的区别？",
     "分段/桶级锁、CAS、size 计算、读写锁取舍", "要点"),
    ("Q025", "编程语言", "挑战", ("线程池", "并发"),
     "线程池参数怎么定？队列满了又拒绝了怎么办？",
     "核心线程数、队列容量、拒绝策略、监控与动态调整", "要点"),
    ("Q026", "编程语言", "挑战", ("内存", "排查"),
     "线上出现内存泄漏，你的排查步骤是什么？",
     "dump 与快照对比、支配树、GC 日志、常见泄漏模式", "要点"),
    # 项目深挖
    ("Q030", "项目深挖", "基础", ("项目", "表达"),
     "介绍一个你最有成就感的项目：背景、你的角色、结果。",
     "结构化表达、个人贡献区分、结果量化", "STAR"),
    ("Q031", "项目深挖", "进阶", ("难点",),
     "这个项目里最难的技术难点是什么？你是怎么解决的？",
     "难点定位、备选方案对比、落地过程、结果验证", "STAR"),
    ("Q032", "项目深挖", "进阶", ("取舍",),
     "项目里做过最艰难的一次技术取舍是什么？",
     "约束条件、方案权衡、放弃项、事后是否后悔", "STAR"),
    ("Q033", "项目深挖", "进阶", ("指标",),
     "你的项目带来了哪些可量化的结果？数据从哪来？",
     "指标口径、前后对比、数据可信度、归因", "要点"),
    ("Q034", "项目深挖", "挑战", ("故障", "复盘"),
     "讲一次你经历的线上故障：影响、处理、复盘结论。",
     "时间线、止血动作、根因、改进项与跟进", "STAR"),
    ("Q035", "项目深挖", "挑战", ("推动", "跨团队"),
     "推动过一次跨团队的技术改造吗？怎么让别人配合你？",
     "共识建立、里程碑拆解、风险预判、结果沉淀", "STAR"),
    ("Q036", "项目深挖", "基础", ("角色",),
     "你在这个团队里的角色是什么？和上下游怎么分工？",
     "职责边界、协作方式、冲突处理", "要点"),
    # 行为 STAR
    ("Q040", "行为STAR", "基础", ("协作", "冲突"),
     "和同事在技术方案上意见不合时，你会怎么处理？",
     "对事不对人、数据/原型说服、升级机制", "STAR"),
    ("Q041", "行为STAR", "基础", ("压力", "排期"),
     "时间紧、任务重时你怎么安排优先级？举个例子。",
     "优先级框架、砍需求沟通、交付节奏", "STAR"),
    ("Q042", "行为STAR", "进阶", ("失败", "成长"),
     "讲一次失败的经历：发生了什么、你学到了什么。",
     "复盘深度、责任承担、行为改变", "STAR"),
    ("Q043", "行为STAR", "进阶", ("主动",),
     "举一个你主动发现问题并推动解决的例子。",
     "主动性、问题定义、推动落地、收益", "STAR"),
    ("Q044", "行为STAR", "进阶", ("学习",),
     "讲一次你快速学习新技术并用到工作里的经历。",
     "学习方法、验证方式、产出与分享", "STAR"),
    ("Q045", "行为STAR", "挑战", ("向上管理",),
     "和上级意见冲突时你怎么处理？举真实例子。",
     "尊重与坦诚、换位、留痕与执行", "STAR"),
    ("Q046", "行为STAR", "挑战", ("带人",),
     "带过新人或指导他人吗？效果如何？",
     "培养方法、反馈机制、可衡量的成长", "STAR"),
    # HR 反问
    ("Q050", "HR反问", "基础", ("动机",),
     "你为什么想加入我们团队？",
     "岗位匹配度、公司/团队了解程度、真诚度", "PREP"),
    ("Q051", "HR反问", "基础", ("规划",),
     "你未来三年的职业规划是什么？",
     "目标清晰度、与岗位一致性、成长路径", "PREP"),
    ("Q052", "HR反问", "进阶", ("离职",),
     "你为什么离开上一家公司？",
     "正向表达、不抱怨、与新机会的关联", "PREP"),
    ("Q053", "HR反问", "进阶", ("薪资",),
     "你的期望薪资是多少？依据是什么？",
     "市场调研、能力对标、弹性空间", "PREP"),
    ("Q054", "HR反问", "进阶", ("反问",),
     "你有什么想问我们的？请准备 3 个高质量反问。",
     "业务、团队、成长三个维度的提问", "要点"),
    ("Q055", "HR反问", "挑战", ("自我认知",),
     "你最大的缺点是什么？它怎么影响过你的工作？",
     "真实但可控、改进措施、不套话", "PREP"),
    ("Q056", "HR反问", "挑战", ("节奏",),
     "你如何看待加班与工作节奏？",
     "价值观澄清、效率观、边界与承诺", "PREP"),
)


def build_bank():
    """把内置题库元组转成结构化条目"""
    bank = []
    for raw in _RAW_QUESTIONS:
        bank.append({
            "id": raw[0],
            "category": raw[1],
            "difficulty": raw[2],
            "tags": list(raw[3]),
            "question": raw[4],
            "focus": raw[5],
            "framework": raw[6],
        })
    return bank


QUESTIONS = build_bank()

# ---------- JD 关键词解析 ----------

# 技能词表：中文用子串匹配，英文用词边界匹配
SKILL_WORDS = [
    # 编程语言
    "Python", "Java", "Go", "Golang", "C++", "JavaScript", "TypeScript", "Rust", "PHP", "Ruby", "Kotlin", "Swift",
    # 框架与库
    "Spring", "SpringBoot", "Django", "Flask", "FastAPI", "React", "Vue", "Angular", "Node.js", "Express",
    "TensorFlow", "PyTorch", "Pandas", "NumPy", "Spark", "Flink", "Hadoop",
    # 中间件与数据
    "MySQL", "PostgreSQL", "MongoDB", "Redis", "Kafka", "RabbitMQ", "RocketMQ", "Elasticsearch", "ClickHouse",
    "SQL", "NoSQL", "Oracle",
    # 工程与基础设施
    "Docker", "Kubernetes", "K8s", "Linux", "Git", "CI/CD", "Jenkins", "Nginx", "AWS", "阿里云", "微服务",
    "分布式", "高并发", "高可用", "性能优化", "系统设计", "架构", "中间件", "消息队列", "缓存", "数据库",
    "大数据", "机器学习", "深度学习", "数据分析", "数据仓库", "推荐系统", "风控", "支付", "订单",
    # 产品 / 通用
    "产品设计", "需求分析", "用户研究", "项目管理", "跨团队", "复盘", "测试", "运维", "监控", "安全",
]

# 职责动词
DUTY_VERBS = (
    "负责", "设计", "开发", "维护", "优化", "推动", "落地", "搭建", "主导", "参与",
    "协调", "分析", "解决", "保障", "输出", "沉淀", "规划", "评审", "联调", "上线", "调研", "迭代",
)

# 技能词 → 提权的题目分类
SKILL_CATEGORY_HINTS = {
    "算法与数据结构": ("算法", "数据结构", "leetcode", "刷题", "排序", "链表", "树", "图", "动态规划"),
    "系统设计": ("架构", "分布式", "高并发", "高可用", "微服务", "系统设计", "中间件", "消息队列", "缓存",
                 "分库分表", "秒杀", "监控", "kafka", "redis", "mysql", "postgresql", "mongodb"),
    "编程语言": ("python", "java", "go", "golang", "c++", "javascript", "typescript", "rust", "php",
                 "spring", "django", "flask", "fastapi", "jvm", "kotlin", "swift"),
    "项目深挖": ("项目", "经验", "负责", "主导", "推动", "落地", "成果", "指标", "业务", "交付"),
    "行为STAR": ("团队", "协作", "沟通", "跨团队", "带人", "冲突", "复盘"),
    "HR反问": ("文化", "价值观", "成长", "规划", "团队氛围"),
}


def extract_skills(text):
    """从 JD 里提取技能词（中文子串 + 英文词边界），返回 [(技能, 次数)]"""
    lowered = text.lower()
    hits = {}
    for skill in SKILL_WORDS:
        if re.search(r"[A-Za-z]", skill):
            pattern = r"(?<![a-z0-9_+#.])%s(?![a-z0-9_+#.])" % re.escape(skill.lower())
            count = len(re.findall(pattern, lowered))
        else:
            count = text.count(skill)
        if count > 0:
            hits[skill] = hits.get(skill, 0) + count
    return sorted(hits.items(), key=lambda item: (-item[1], item[0]))


def extract_years(text):
    """提取年限要求，如「3-5 年」「3 年以上」"""
    match = re.search(r"(\d+)\s*[-~～到至]\s*(\d+)\s*年", text)
    if match:
        return "%s-%s 年" % (match.group(1), match.group(2))
    match = re.search(r"(\d+)\s*年以上", text)
    if match:
        return "%s 年以上" % match.group(1)
    match = re.search(r"(\d+)\s*年", text)
    if match:
        return "%s 年" % match.group(1)
    return "未提及"


def extract_verbs(text):
    """提取职责动词及出现次数"""
    found = []
    for verb in DUTY_VERBS:
        count = text.count(verb)
        if count > 0:
            found.append((verb, count))
    return sorted(found, key=lambda item: (-item[1], item[0]))


def parse_jd(text):
    """解析 JD → 岗位画像：技能要求表、年限、职责动词、分类权重"""
    skills = extract_skills(text)
    years = extract_years(text)
    verbs = extract_verbs(text)
    lowered = text.lower()

    weights = dict((category, 1.0) for category in CATEGORIES)
    for category, hints in SKILL_CATEGORY_HINTS.items():
        for hint in hints:
            if re.search(r"[A-Za-z]", hint):
                if re.search(r"(?<![a-z0-9_+#.])%s(?![a-z0-9_+#.])" % re.escape(hint), lowered):
                    weights[category] += 0.5
            elif hint in text:
                weights[category] += 0.5
    for verb, _count in verbs:
        weights["项目深挖"] += 0.1
        break
    return {
        "skills": skills,
        "years": years,
        "verbs": verbs,
        "weights": weights,
        "length": len(text),
    }


def category_weights(profile, type_name):
    """岗位画像 + 面试类型 → 各分类抽题权重"""
    weights = dict(profile.get("weights") or {})
    for category in CATEGORIES:
        weights.setdefault(category, 1.0)
    for category in TYPE_CATEGORIES[type_name]:
        weights[category] += 1.0
    if type_name == "行为":
        weights["行为STAR"] += 1.0
        weights["HR反问"] += 0.5
    if type_name == "产品":
        weights["项目深挖"] += 1.0
    return weights


# ---------- 出题策略 ----------

def question_weight(question, weights):
    """单题权重 = 分类权重 × 难度权重"""
    return weights.get(question["category"], 1.0) * DIFFICULTY_WEIGHTS.get(question["difficulty"], 1.0)


def weighted_pick(rng, candidates, weights):
    """按权重抽一题（可复现：只用传入的 rng）"""
    total = sum(question_weight(item, weights) for item in candidates)
    threshold = rng.random() * total
    running = 0.0
    for item in candidates:
        running += question_weight(item, weights)
        if running >= threshold:
            return item
    return candidates[-1]


def select_questions(bank, type_name, count, weights=None, seed=DEFAULT_SEED):
    """按类型/数量/权重抽题；先每类保底 1 题，再按权重补足，固定种子可复现"""
    rng = random.Random(seed)
    weights = weights or dict((category, 1.0) for category in CATEGORIES)
    categories = TYPE_CATEGORIES[type_name]
    pool = [item for item in bank if item["category"] in categories]

    selected = []
    used = set()
    for category in categories:                       # 覆盖率：每类至少 1 题
        candidates = [item for item in pool if item["category"] == category and item["id"] not in used]
        if not candidates:
            continue
        pick = weighted_pick(rng, candidates, weights)
        selected.append(pick)
        used.add(pick["id"])

    remaining = [item for item in pool if item["id"] not in used]
    while len(selected) < count and remaining:
        pick = weighted_pick(rng, remaining, weights)
        selected.append(pick)
        used.add(pick["id"])
        remaining = [item for item in remaining if item["id"] != pick["id"]]

    return selected[:count] if count < len(selected) else selected


def check_coverage(selected, type_name):
    """覆盖率检查：该类型涉及的每个分类都要至少 1 题"""
    categories = TYPE_CATEGORIES[type_name]
    covered = set(item["category"] for item in selected)
    return [category for category in categories if category not in covered]


# ---------- 答题框架 ----------

FRAMEWORKS = {
    "STAR": (
        ("- 情境（Situation）", "当时的背景、约束是什么？一句话交代清楚。"),
        ("- 任务（Task）", "你要达成的目标 / 你承担的责任是什么？"),
        ("- 行动（Action）", "你具体做了什么？为什么这么做？（这是重点，讲 2-4 步）"),
        ("- 结果（Result）", "带来了什么可衡量的结果？如果有数据一定要给。"),
    ),
    "PREP": (
        ("- 观点（Point）", "先亮明你的答案或立场，一句话。"),
        ("- 理由（Reason）", "为什么这么想？给出 2 个以内的核心理由。"),
        ("- 例子（Example）", "用一个真实经历或具体数据支撑。"),
        ("- 重申（Point）", "回到观点收尾，并说明与这个岗位的关系。"),
    ),
    "要点": (
        ("- 结论先行", "第一句话给出结论或方案，再展开细节。"),
        ("- 分点展开", "按 2-4 个要点展开，每个要点一句话解释。"),
        ("- 举例/数据", "至少给一个具体例子、边界条件或数据。"),
        ("- 收尾权衡", "说清适用范围、代价和你会怎么选。"),
    ),
}


def render_framework(question):
    """把题目渲染成答题框架骨架（Markdown 行列表）"""
    lines = []
    for title, hint in FRAMEWORKS[question["framework"]]:
        lines.append("  %s：%s" % (title, hint))
    return lines


# ---------- 练习记录与复习计划 ----------

def parse_date(text):
    return datetime.strptime(text, "%Y-%m-%d").date()


def load_record(path):
    """读练习记录 JSON：[{id, date, score, note}]；文件不存在返回空表"""
    record_path = Path(path).expanduser()
    if not record_path.is_file():
        return []
    with open(str(record_path), "r", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, list):
        raise ValueError("练习记录应该是 JSON 数组")
    records = []
    for item in data:
        if not isinstance(item, dict) or "id" not in item or "date" not in item:
            continue
        records.append({
            "id": str(item["id"]),
            "date": str(item["date"]),
            "score": int(item.get("score", 3)),
            "note": str(item.get("note", "")),
        })
    return records


def save_record(path, records):
    """把练习记录写回 JSON（按日期排序，格式统一）"""
    ordered = sorted(records, key=lambda item: (item["date"], item["id"]))
    record_path = Path(path).expanduser()
    with open(str(record_path), "w", encoding="utf-8") as handle:
        json.dump(ordered, handle, ensure_ascii=False, indent=2)
    return record_path


def append_record(records, question_id, date_text, score, note=""):
    """追加一条练习记录（校验自评分范围）"""
    if not (SCORE_RANGE[0] <= score <= SCORE_RANGE[1]):
        raise ValueError("自评分必须在 %d-%d 之间" % SCORE_RANGE)
    records.append({
        "id": str(question_id),
        "date": date_text,
        "score": int(score),
        "note": note,
    })
    return records


def review_schedule(base_date, intervals=REVIEW_INTERVALS):
    """按 1/2/4/7/15 天间隔生成复习日期列表"""
    return [base_date + timedelta(days=days) for days in intervals]


def next_due_date(dates, intervals=REVIEW_INTERVALS):
    """根据某题的练习日期列表算下次复习日：第 n 次复习后按第 n 个间隔排"""
    ordered = sorted(parse_date(item) for item in dates)
    if not ordered:
        return None
    index = min(len(ordered) - 1, len(intervals) - 1)
    return ordered[-1] + timedelta(days=intervals[index])


def due_today(records, today, bank_ids=(), intervals=REVIEW_INTERVALS):
    """今日该复习的题目清单：[{id, last_date, due_date, overdue_days}]"""
    grouped = {}
    for record in records:
        grouped.setdefault(record["id"], []).append(record["date"])
    due = []
    for question_id, dates in grouped.items():
        due_date = next_due_date(dates, intervals)
        if due_date is not None and due_date <= today:
            due.append({
                "id": question_id,
                "last_date": sorted(dates)[-1],
                "due_date": due_date.strftime("%Y-%m-%d"),
                "overdue_days": (today - due_date).days,
            })
    due.sort(key=lambda item: (item["due_date"], item["id"]))
    return due


def progress_stats(records):
    """进步曲线统计：按日期的平均自评分、总次数、覆盖题数、提升幅度"""
    if not records:
        return {
            "total": 0, "covered": 0, "avg": 0.0,
            "by_date": [], "improvement": 0.0,
        }
    by_date = {}
    for record in records:
        by_date.setdefault(record["date"], []).append(record["score"])
    curve = []
    for date_text in sorted(by_date):
        scores = by_date[date_text]
        curve.append({
            "date": date_text,
            "count": len(scores),
            "avg": round(sum(scores) / float(len(scores)), 2),
        })
    all_scores = [record["score"] for record in records]
    improvement = 0.0
    if len(curve) >= 2:
        improvement = round(curve[-1]["avg"] - curve[0]["avg"], 2)
    return {
        "total": len(records),
        "covered": len(set(record["id"] for record in records)),
        "avg": round(sum(all_scores) / float(len(all_scores)), 2),
        "by_date": curve,
        "improvement": improvement,
    }


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


MOCK_SYSTEM_PROMPT = """你是一位严格的面试官，正在做模拟面试。用户会给出一道面试题、考察点和回答框架。
请做三件事：
1. 针对这道题出一个追问（面试官会顺着候选人的回答继续挖）。
2. 假设候选人给出了中等水平的回答，按四个维度打分（整数 1-5）：内容、结构、证据、表达。
3. 给 2-3 条具体可执行的改进建议。

只输出一个 JSON 对象，不要解释文字，不要 Markdown 代码围栏：
{"follow_up": "追问内容", "scores": {"内容": 3, "结构": 3, "证据": 3, "表达": 3}, "suggestions": ["建议1", "建议2"]}
"""

MOCK_DIMENSIONS = ("内容", "结构", "证据", "表达")


def parse_mock_json(text):
    """尽力把模型模拟回复解析成 JSON；解析不出来返回 None（原文进报告附录）"""
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
    follow_up = data.get("follow_up")
    if not isinstance(follow_up, str) or not follow_up.strip():
        return None
    scores = {}
    raw_scores = data.get("scores")
    if isinstance(raw_scores, dict):
        for dimension in MOCK_DIMENSIONS:
            value = raw_scores.get(dimension)
            try:
                value = int(value)
            except (TypeError, ValueError):
                continue
            if SCORE_RANGE[0] <= value <= SCORE_RANGE[1]:
                scores[dimension] = value
    suggestions = []
    raw_suggestions = data.get("suggestions")
    if isinstance(raw_suggestions, list):
        for item in raw_suggestions:
            if isinstance(item, str) and item.strip():
                suggestions.append(item.strip())
    return {
        "follow_up": follow_up.strip(),
        "scores": scores,
        "suggestions": suggestions,
    }


def mock_interview(question, model):
    """模拟面试：出追问 + 四维打分 + 改进建议；返回 (parsed, raw_text)"""
    client = anthropic.Anthropic()
    user = "题目：%s\n分类：%s / 难度：%s\n考察点：%s\n回答框架：%s" % (
        question["question"], question["category"], question["difficulty"],
        question["focus"], question["framework"],
    )
    response = call_model(
        client, model, MOCK_SYSTEM_PROMPT,
        [{"role": "user", "content": user}],
        max_tokens=2000,
    )
    raw = response_text(response)
    return parse_mock_json(raw), raw


# ---------- 报告 ----------

def build_report(selected, profile, records, today, mock_results, raw_outputs, meta):
    """生成中文 Markdown：岗位画像、题目清单、复习计划、自评表、模拟记录"""
    lines = ["# 面试准备报告", ""]
    lines.append("- 面试类型：%s ｜ 题目数：%d ｜ 随机种子：%d" % (meta["type"], len(selected), meta["seed"]))
    if meta["jd_file"]:
        lines.append("- 岗位描述：%s" % meta["jd_file"])
    lines.append("- 生成时间：%s ｜ 模式：%s" % (
        meta["generated_at"], "离线（不调模型）" if meta["offline"] else "模型模拟面试"))
    lines.append("")

    lines.append("## 一、岗位画像")
    lines.append("")
    if profile and profile.get("skills"):
        lines.append("| 技能要求 | 出现次数 |")
        lines.append("| --- | --- |")
        for skill, count in profile["skills"][:15]:
            lines.append("| %s | %d |" % (skill, count))
        lines.append("")
        lines.append("- 年限要求：%s" % profile["years"])
        if profile["verbs"]:
            lines.append("- 职责动词：%s" % "、".join(
                "%s(%d)" % (verb, count) for verb, count in profile["verbs"][:10]))
        lines.append("- 抽题权重（分类 → 权重）：%s" % "、".join(
            "%s=%.1f" % (category, profile["weights"][category])
            for category in TYPE_CATEGORIES[meta["type"]]))
    else:
        lines.append("（没有提供 --jd，按默认权重抽题；给一份岗位描述可以生成技能要求表与权重。）")
    lines.append("")

    lines.append("## 二、题目清单")
    lines.append("")
    for index, question in enumerate(selected, 1):
        lines.append("### %d. [%s / %s] %s" % (
            index, question["category"], question["difficulty"], question["question"]))
        lines.append("")
        lines.append("- 题目 id：`%s` ｜ 标签：%s" % (
            question["id"], "、".join(question["tags"]) or "—"))
        lines.append("- 考察点：%s" % question["focus"])
        lines.append("- 回答框架（%s）：" % question["framework"])
        for line in render_framework(question):
            lines.append(line)
        lines.append("")

    lines.append("## 三、复习计划")
    lines.append("")
    lines.append("复习间隔按 %s 天递进：同一道题练习后第 %s 天各复习一次；"
                 "第 n 次复习完成后，下次复习按第 n 个间隔顺延。" % (
                     "/".join(str(x) for x in REVIEW_INTERVALS),
                     "/".join(str(x) for x in REVIEW_INTERVALS)))
    lines.append("")
    if records:
        due = due_today(records, today)
        lines.append("### 今日该复习（%s）" % today.strftime("%Y-%m-%d"))
        lines.append("")
        if due:
            lines.append("| 题目 id | 上次练习 | 应复习日 | 逾期天数 |")
            lines.append("| --- | --- | --- | --- |")
            for item in due:
                lines.append("| `%s` | %s | %s | %d |" % (
                    item["id"], item["last_date"], item["due_date"], item["overdue_days"]))
        else:
            lines.append("（今天没有到期的复习任务。）")
        lines.append("")
        stats = progress_stats(records)
        lines.append("### 进步曲线")
        lines.append("")
        lines.append("| 日期 | 练习次数 | 平均自评分 |")
        lines.append("| --- | --- | --- |")
        for item in stats["by_date"]:
            lines.append("| %s | %d | %.2f |" % (item["date"], item["count"], item["avg"]))
        lines.append("")
        lines.append("- 累计练习 %d 次，覆盖 %d 道题，平均自评分 %.2f，曲线首尾差 %+0.2f" % (
            stats["total"], stats["covered"], stats["avg"], stats["improvement"]))
    else:
        lines.append("（还没有练习记录。用 --record 指向一个 JSON 文件，"
                     "每次练习后写入 {id, date, score, note}，这里就会出现复习清单和进步曲线。）")
    lines.append("")

    lines.append("## 四、自评表模板")
    lines.append("")
    lines.append("练习后复制一行填写，写回 --record 指定的 JSON 文件即可：")
    lines.append("")
    lines.append("```json")
    lines.append('{"id": "Q001", "date": "%s", "score": 3, "note": "复杂度分析说漏了空间复杂度"}'
                 % today.strftime("%Y-%m-%d"))
    lines.append("```")
    lines.append("")
    lines.append("| 题目 id | 日期 | 自评分（1-5） | 备注 |")
    lines.append("| --- | --- | --- | --- |")
    for question in selected[:10]:
        lines.append("| `%s` | %s |  |  |" % (question["id"], today.strftime("%Y-%m-%d")))
    lines.append("")

    lines.append("## 五、模拟问答记录")
    lines.append("")
    if mock_results:
        for question, parsed in mock_results:
            lines.append("### `%s` %s" % (question["id"], question["question"]))
            lines.append("")
            if parsed:
                lines.append("**面试官追问**：%s" % parsed["follow_up"])
                lines.append("")
                if parsed["scores"]:
                    lines.append("| 维度 | 得分（1-5） |")
                    lines.append("| --- | --- |")
                    for dimension in MOCK_DIMENSIONS:
                        if dimension in parsed["scores"]:
                            lines.append("| %s | %d |" % (dimension, parsed["scores"][dimension]))
                    lines.append("")
                if parsed["suggestions"]:
                    lines.append("**改进建议**：")
                    for item in parsed["suggestions"]:
                        lines.append("- %s" % item)
                    lines.append("")
            else:
                lines.append("（这道题的模型回复没有解析成约定 JSON，原文见附录。）")
                lines.append("")
    else:
        lines.append("（本次没有开启 --mock。加 --mock 会逐题模拟面试官追问，"
                     "按 内容/结构/证据/表达 四维打分并给改进建议。）")
        lines.append("")

    if raw_outputs:
        lines.append("## 附录：模型原始回复（未能解析为 JSON）")
        lines.append("")
        for item in raw_outputs:
            lines.append("### %s" % item["label"])
            lines.append("")
            lines.append("```text")
            lines.append(item["text"] or "（模型没有返回任何内容）")
            lines.append("```")
            lines.append("")

    return "\n".join(lines).rstrip() + "\n"


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


SAMPLE_JD = """高级后端工程师（3-5 年）
岗位职责：
1. 负责核心交易系统的架构设计与开发，推动服务拆分与微服务化；
2. 负责高并发场景的性能优化，主导缓存与消息队列（Kafka、Redis）方案落地；
3. 参与 MySQL 分库分表、监控告警体系建设，保障系统高可用；
4. 与产品、测试跨团队协作，完成需求分析、评审与上线。
任职要求：
1. 3 年以上 Java 或 Go 开发经验，熟悉 Spring；
2. 熟悉分布式、缓存、消息队列，有大数据经验优先；
3. 有较强的沟通与复盘意识。
"""


def run_self_test():
    """离线自检：造样本、跑全部确定性逻辑、assert 校验，不联网、不需要 API key"""
    bank = QUESTIONS

    # --- 题库：规模、分类、字段完整性 ---
    assert len(bank) >= 40, "题库不足 40 题：%d" % len(bank)
    categories = set(item["category"] for item in bank)
    assert set(CATEGORIES).issubset(categories), "题库分类不全：%s" % (set(CATEGORIES) - categories)
    for item in bank:
        assert item["id"].startswith("Q")
        assert item["difficulty"] in DIFFICULTIES
        assert item["question"] and item["focus"] and item["tags"]
        assert item["framework"] in FRAMEWORKS
    ids = [item["id"] for item in bank]
    assert len(ids) == len(set(ids)), "题目 id 重复"
    assert all(len([q for q in bank if q["category"] == c]) >= 6 for c in CATEGORIES), "每类至少 6 题"

    # --- 题目筛选（按类型） ---
    for type_name, cats in TYPE_CATEGORIES.items():
        picked = select_questions(bank, type_name, 10, seed=1)
        assert picked, type_name
        assert all(item["category"] in cats for item in picked), "%s 混入了不该有的分类" % type_name
        assert len(picked) == min(10, len([q for q in bank if q["category"] in cats]))

    # --- JD 关键词画像 ---
    profile = parse_jd(SAMPLE_JD)
    skill_names = [name for name, _ in profile["skills"]]
    for expected in ("Java", "Go", "Spring", "Redis", "Kafka", "MySQL", "微服务", "分布式"):
        assert expected in skill_names, "JD 技能词缺失：%s" % expected
    assert profile["years"] == "3-5 年", profile["years"]
    verb_names = [name for name, _ in profile["verbs"]]
    assert "负责" in verb_names and "推动" in verb_names and "主导" in verb_names
    assert profile["weights"]["系统设计"] > profile["weights"]["HR反问"], "架构类 JD 应提高系统设计权重"
    assert parse_jd("随便一段没有技能词的文本")["skills"] == []
    assert parse_jd("随便一段没有技能词的文本")["years"] == "未提及"

    # --- 抽题：可复现、覆盖率、数量 ---
    weights = category_weights(profile, "综合")
    picked_a = select_questions(bank, "综合", 12, weights, seed=7)
    picked_b = select_questions(bank, "综合", 12, weights, seed=7)
    assert [q["id"] for q in picked_a] == [q["id"] for q in picked_b], "同种子应可复现"
    picked_c = select_questions(bank, "综合", 12, weights, seed=8)
    assert [q["id"] for q in picked_a] != [q["id"] for q in picked_c], "不同种子应有差异"
    assert len(picked_a) == 12
    assert check_coverage(picked_a, "综合") == [], "每类至少 1 题"
    assert check_coverage(select_questions(bank, "技术", 8, weights, seed=3), "技术") == []
    small = select_questions(bank, "技术", 3, weights, seed=3)
    assert len(small) == 3

    # --- 答题框架渲染 ---
    star = [q for q in bank if q["framework"] == "STAR"][0]
    star_lines = render_framework(star)
    assert any("情境" in line for line in star_lines)
    assert any("行动" in line for line in star_lines)
    prep = [q for q in bank if q["framework"] == "PREP"][0]
    prep_lines = render_framework(prep)
    assert any("观点" in line for line in prep_lines) and any("重申" in line for line in prep_lines)
    point = [q for q in bank if q["framework"] == "要点"][0]
    assert render_framework(point)

    # --- 练习记录读写 ---
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        record_path = root / "练习记录.json"
        assert load_record(record_path) == []
        records = []
        append_record(records, "Q001", "2026-01-01", 3, "第一次")
        append_record(records, "Q001", "2026-01-02", 4, "复习")
        append_record(records, "Q010", "2026-01-02", 2, "")
        try:
            append_record(records, "Q001", "2026-01-03", 9)
            raise AssertionError("超范围自评分应被拒绝")
        except ValueError:
            pass
        saved = save_record(record_path, records)
        assert saved.is_file()
        loaded = load_record(saved)
        assert len(loaded) == 3
        assert loaded[0]["id"] in ("Q001", "Q010")
        assert any(item["note"] == "复习" for item in loaded)

        # --- 复习间隔（1/2/4/7/15 天） ---
        schedule = review_schedule(parse_date("2026-01-01"))
        assert [d.strftime("%Y-%m-%d") for d in schedule] == [
            "2026-01-02", "2026-01-03", "2026-01-05", "2026-01-08", "2026-01-16"]
        assert next_due_date(["2026-01-01"]) == parse_date("2026-01-02"), "第 1 次练习 → +1 天"
        assert next_due_date(["2026-01-01", "2026-01-02"]) == parse_date("2026-01-04"), "第 2 次 → +2 天"
        assert next_due_date(["2026-01-01", "2026-01-02", "2026-01-04"]) == parse_date("2026-01-08"), "第 3 次 → +4 天"
        assert next_due_date([]) is None

        due = due_today(loaded, parse_date("2026-01-05"))
        due_ids = [item["id"] for item in due]
        assert "Q001" in due_ids and "Q010" in due_ids, due
        assert all(item["overdue_days"] >= 0 for item in due)
        assert due_today(loaded, parse_date("2026-01-01")) == [], "没到期不应列入"

        # --- 进步曲线统计 ---
        stats = progress_stats(loaded)
        assert stats["total"] == 3 and stats["covered"] == 2
        assert stats["by_date"][0]["date"] == "2026-01-01"
        assert stats["avg"] == round((3 + 4 + 2) / 3.0, 2)
        assert stats["improvement"] == round((4 + 2) / 2.0 - 3, 2)
        assert progress_stats([])["total"] == 0

    # --- 模型回复 JSON 解析兜底 ---
    good = (
        "```json\n"
        '{"follow_up": "如果数据量再大 10 倍呢？", '
        '"scores": {"内容": 4, "结构": 3, "证据": 3, "表达": 5}, '
        '"suggestions": ["补充复杂度分析", "给一个真实数据例子"]}\n'
        "```"
    )
    parsed = parse_mock_json(good)
    assert parsed and parsed["follow_up"].startswith("如果数据量")
    assert parsed["scores"] == {"内容": 4, "结构": 3, "证据": 3, "表达": 5}
    assert len(parsed["suggestions"]) == 2
    assert parse_mock_json("不是 JSON") is None
    assert parse_mock_json("{bad json") is None
    assert parse_mock_json('{"scores": {"内容": 4}}') is None, "没有 follow_up 应返回 None"
    coerced = parse_mock_json(
        '{"follow_up": "追问", "scores": {"内容": "4", "结构": 9}, "suggestions": ["好建议", 3]}'
    )
    assert coerced and coerced["scores"] == {"内容": 4} and coerced["suggestions"] == ["好建议"]

    # --- 报告结构（离线 + 模拟 + 解析失败兜底） ---
    today = parse_date("2026-01-05")
    meta = {
        "type": "综合", "seed": 7, "jd_file": "岗位描述.txt", "offline": True,
        "generated_at": "2026-01-05 09:00:00",
    }
    report = build_report(picked_a, profile, [], today, [], [], meta)
    assert report.startswith("# 面试准备报告")
    for heading in ("## 一、岗位画像", "## 二、题目清单", "## 三、复习计划",
                    "## 四、自评表模板", "## 五、模拟问答记录"):
        assert heading in report, "报告缺少：%s" % heading
    assert "Java" in report and "3-5 年" in report
    assert "回答框架" in report and "考察点" in report

    records = [
        {"id": "Q001", "date": "2026-01-01", "score": 3, "note": ""},
        {"id": "Q001", "date": "2026-01-02", "score": 4, "note": ""},
    ]
    report_record = build_report(picked_a, profile, records, today, [], [], meta)
    assert "今日该复习" in report_record and "进步曲线" in report_record

    mock_results = [(picked_a[0], parsed), (picked_a[1], None)]
    report_mock = build_report(
        picked_a, profile, records, today, mock_results,
        [{"label": "Q002 模拟回复", "text": "自由格式"}],
        dict(meta, offline=False),
    )
    assert "面试官追问" in report_mock
    assert "改进建议" in report_mock
    assert "附录：模型原始回复" in report_mock and "自由格式" in report_mock

    # --- response_text 与 call_model 三级降级 ---
    assert response_text(_FakeResponse("你好")) == "你好"
    fake = _FakeClient()
    result = call_model(fake, "fake-model", "system", [{"role": "user", "content": "hi"}])
    assert result["ok"] is True
    assert fake.state["calls"] == 2, "第一层抛 TypeError、第二层成功，共调用 2 次"

    print("SELF-TEST OK")


# ---------- CLI ----------

def build_parser():
    parser = argparse.ArgumentParser(
        description="面试准备助手：内置分类题库 + JD 岗位画像 + 可复现抽题 + 复习计划 + 模拟面试",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            "  python3 agent.py --jd 岗位描述.txt --type 技术 --count 12\n"
            "  python3 agent.py --type 行为 --output 行为面准备.md --offline\n"
            "  python3 agent.py --jd 岗位描述.txt --mock --record 练习记录.json\n"
            "  python3 agent.py --type 综合 --seed 7 --model qwen3.8-max\n"
        ),
    )
    parser.add_argument("--jd", default="", help="岗位描述文件（.txt），用于解析技能词、年限、职责动词并加权抽题")
    parser.add_argument("--type", default="综合", choices=QUESTION_TYPES,
                        help="面试类型：技术 / 行为 / 产品 / 综合，默认综合")
    parser.add_argument("--count", type=int, default=DEFAULT_COUNT, help="出题数量，默认 10")
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED,
                        help="随机种子，保证同样参数抽出同样题目，默认 42")
    parser.add_argument("--mock", action="store_true",
                        help="模拟面试：逐题让模型出追问、按内容/结构/证据/表达打分并给建议")
    parser.add_argument("--record", default="",
                        help="练习记录 JSON 路径（读 + 写），用于复习清单与进步曲线")
    parser.add_argument("--output", default=DEFAULT_OUTPUT,
                        help="准备文档输出路径，默认 interview-prep.md")
    parser.add_argument("--offline", action="store_true",
                        help="离线模式：只出题与渲染框架骨架，不调模型")
    parser.add_argument("--model", default=DEFAULT_MODEL,
                        help="模型名，默认 %s；走第三方兼容网关时用 --model 指定网关认得的模型名" % DEFAULT_MODEL)
    parser.add_argument("--self-test", action="store_true", help="离线自检：不联网、不需要 API key")
    return parser


def require_api_key():
    """需要模型的路径必须先有 key，否则打印中文提示并退出"""
    if not os.environ.get("ANTHROPIC_API_KEY") and not os.environ.get("ANTHROPIC_AUTH_TOKEN"):
        print(
            "提示：没有检测到 ANTHROPIC_API_KEY 或 ANTHROPIC_AUTH_TOKEN，无法进行模拟面试。\n"
            "请先设置（二选一）：\n"
            "  macOS / Linux：      export ANTHROPIC_API_KEY=你的key\n"
            "  Windows PowerShell： $env:ANTHROPIC_API_KEY=\"你的key\"\n"
            "也可以加 --offline 只出题和答题框架（不需要 key、不联网）。",
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

    if args.count < 1:
        parser.error("--count 必须大于 0")

    profile = {"skills": [], "years": "未提及", "verbs": [], "weights": {}}
    if args.jd:
        jd_path = Path(args.jd).expanduser()
        if not jd_path.is_file():
            print("错误：岗位描述文件不存在：%s" % jd_path, file=sys.stderr)
            sys.exit(1)
        try:
            jd_text = jd_path.read_text(encoding="utf-8", errors="replace")[:MAX_JD_CHARS]
        except OSError as exc:
            print("错误：岗位描述读取失败：%s" % exc, file=sys.stderr)
            sys.exit(1)
        profile = parse_jd(jd_text)
        print("[岗位画像] 技能 %d 个，年限 %s，职责动词 %d 个" % (
            len(profile["skills"]), profile["years"], len(profile["verbs"])))

    weights = category_weights(profile, args.type)
    selected = select_questions(QUESTIONS, args.type, args.count, weights, seed=args.seed)
    if not selected:
        print("错误：该面试类型下没有可出的题目。", file=sys.stderr)
        sys.exit(1)

    missing = check_coverage(selected, args.type)
    print("[出题] %d 题（类型 %s，种子 %d）%s" % (
        len(selected), args.type, args.seed,
        "" if not missing else "，注意：%s 没抽到题" % "、".join(missing)))

    if args.mock and not args.offline:
        require_api_key()

    records = []
    if args.record:
        try:
            records = load_record(args.record)
            print("[练习记录] 已读入 %d 条：%s" % (len(records), args.record))
        except (OSError, ValueError) as exc:
            print("错误：练习记录读取失败：%s" % exc, file=sys.stderr)
            sys.exit(1)

    mock_results = []
    raw_outputs = []
    if args.mock and not args.offline:
        print("[模拟面试] 模型：%s" % args.model)
        for question in selected:
            try:
                parsed, raw = mock_interview(question, args.model)
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
            if parsed is None:
                raw_outputs.append({"label": "%s 模拟回复" % question["id"], "text": raw})
                print("[提示] %s 的模型回复不是合法 JSON，原文已放入附录。" % question["id"])
            mock_results.append((question, parsed))

    today = datetime.now().date()
    meta = {
        "type": args.type,
        "seed": args.seed,
        "jd_file": args.jd,
        "offline": args.offline or not args.mock,
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    report = build_report(selected, profile, records, today, mock_results, raw_outputs, meta)

    output_path = Path(args.output).expanduser()
    try:
        if output_path.parent and not output_path.parent.exists():
            output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(report, encoding="utf-8")
    except OSError as exc:
        print("错误：准备文档写入失败：%s" % exc, file=sys.stderr)
        sys.exit(1)
    print("[完成] 准备文档已写入：%s" % output_path)

    if args.record and records:
        try:
            save_record(args.record, records)
        except OSError as exc:
            print("错误：练习记录写入失败：%s" % exc, file=sys.stderr)
            sys.exit(1)


if __name__ == "__main__":
    main()
