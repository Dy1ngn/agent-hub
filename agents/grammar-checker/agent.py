#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
中英文校对助手（grammar-checker）—— 规则表 + 模型语义校对 的两段式文字校对 agent

先跑一遍完全确定性的规则表（的/地/得误用、在/再误用、做/作误用、重复词、
重复标点、中英文标点混用、中英文之间缺空格、全角/半角混用、行尾空白、
句首小写、双空格、重复单词、拼写误用、there/their、超长句、残留标记），
再把文本分批交给模型做语义级校对（语法、搭配、表达不地道），最后合并去重，
生成按级别分组的中文 Markdown 校对报告。除 anthropic SDK 外只用标准库。

用法：
    python3 agent.py <文件或目录...> [选项]

示例：
    python3 agent.py 文章.md --lang 中文 --output 校对报告.md
    python3 agent.py ./docs --min-level 警告
    python3 agent.py 文章.md --offline            # 只跑规则，不调用模型、不需要 key
    python3 agent.py 文章.md --model qwen3.8-max  # 走第三方兼容网关时指定模型名

环境要求：
    1. 模型语义校对需要设置 API key（--offline 与 --self-test 不需要）：
           macOS / Linux：      export ANTHROPIC_API_KEY=你的key
           Windows PowerShell：$env:ANTHROPIC_API_KEY="你的key"
       走第三方 Anthropic 兼容网关时，改设 ANTHROPIC_BASE_URL 与
       ANTHROPIC_AUTH_TOKEN，并用 --model 指定网关支持的模型名。
    2. 安装 SDK：
           pip3 install -r requirements.txt
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

# 默认模型（可用 --model 参数或 GRAMMAR_CHECKER_MODEL 环境变量更换）
DEFAULT_MODEL = os.environ.get("GRAMMAR_CHECKER_MODEL", "claude-opus-5")

DEFAULT_OUTPUT = "proofread-report.md"
DEFAULT_MAX_FILES = 200           # 单次最多校对多少个文件
DEFAULT_MAX_FILE_CHARS = 200_000  # 单个文件最多读入多少字符
SEMANTIC_BATCH_CHARS = 20_000     # 发给模型的每一批文本上限（字符）
MAX_FINDINGS_PER_RULE = 30        # 单文件单条规则最多报多少处，避免刷屏

LEVELS = ("提示", "警告", "错误")
LEVEL_ORDER = {"提示": 1, "警告": 2, "错误": 3}
LANG_CHOICES = ("auto", "中文", "英文")

LONG_SENT_CN = 60   # 中文句子超过多少字算超长
LONG_SENT_EN = 25   # 英文句子超过多少词算超长

# 递归扫描时直接忽略的目录
IGNORED_DIRS = {".git", "node_modules", "__pycache__", "dist", "build", "venv", ".venv"}

# ---------- 正则与词表 ----------

CJK_RE = re.compile(r"[\u4e00-\u9fff]")
TRAILING_SPACE_RE = re.compile(r"[ \t]+$")
MULTI_SPACE_RE = re.compile(r" {2,}")
REPEAT_DOT_RE = re.compile(r"(?<!\.)\.\.(?!\.)")
REPEAT_PUNCT_CN_RE = re.compile(r"([？！。，；：、])\1+")
REPEAT_PUNCT_EN_RE = re.compile(r"([!?;:,])\1+")
MIXED_PUNCT_RES = (
    re.compile(r"[\u4e00-\u9fff][,?!;:]"),
    re.compile(r"[,?!;:][\u4e00-\u9fff]"),
    re.compile(r"[\u4e00-\u9fff]\.(?![0-9])"),
    re.compile(r"(?<![0-9.])\.[\u4e00-\u9fff]"),
)
SPACE_BETWEEN_RES = (
    re.compile(r"[\u4e00-\u9fff][A-Za-z]"),
    re.compile(r"[A-Za-z][\u4e00-\u9fff]"),
)
FULLWIDTH_ALNUM_RE = re.compile(r"[\uff10-\uff19\uff21-\uff3a\uff41-\uff5a\u3000]")
REPEAT_WORD_EN_RE = re.compile(r"\b([A-Za-z]+)\s+\1\b", re.IGNORECASE)
SENTENCE_LOWER_RE = re.compile(r"(?:^|[.!?][\"')\]]?\s+)([a-z])")
SENT_SPLIT_RE = re.compile(r"[。！？；!?;]")
MARKER_RE = re.compile(r"\b(TODO|FIXME|XXX|HACK)\b")
WORD_RE = re.compile(r"[A-Za-z0-9'\-]+")

# 「的 + 动词」启发式：前面是常见副词/形容词，后面是常见动词
DE_ADVERB = (
    "越来越", "非常", "十分", "比较", "更加", "真的", "快速", "慢慢", "迅速", "认真",
    "仔细", "努力", "积极", "热情", "安静", "大声", "小声", "高兴", "开心", "难过",
    "伤心", "愤怒", "生气", "勇敢", "坚强", "独立", "自觉", "主动", "直接", "间接",
    "公开", "正式", "好好", "匆匆", "悄悄", "默默", "渐渐", "不断", "持续", "经常",
    "偶尔", "总是", "一直", "马上", "立刻", "及时", "准确", "小心", "耐心", "专心",
    "用心", "细心", "大胆", "果断", "坚决", "彻底", "完全", "真正", "特别",
)
DE_VERB = (
    "跑", "走", "说", "看", "做", "写", "学", "吃", "喝", "玩", "笑", "哭", "睡",
    "飞", "唱", "跳", "打", "查", "改", "修", "建", "画", "拍", "照", "演", "讲",
    "答", "问", "选", "挑", "试", "读", "用", "拿", "放", "等", "找", "给", "送",
    "收", "记", "忘", "变", "想", "听", "来", "去", "开", "关",
)
# 「地 + 名词」启发式：后面跟的是明确名词时多半该用「的」
DI_NOUN = (
    "态度", "问题", "情况", "样子", "方式", "方法", "结果", "原因", "东西", "事情",
    "内容", "环境", "条件", "水平", "能力", "质量", "标准", "要求", "目标", "计划",
    "方案", "现象", "本质", "特征", "特点", "优点", "缺点", "价值", "意义", "作用",
    "影响", "效果", "效率", "速度", "声音", "颜色", "味道", "感觉", "印象", "经验",
    "知识", "技术", "生活", "工作", "时间", "过程", "阶段", "时期",
)


def _alt(words):
    """把词表拼成正则 alternation，长词在前避免被短词截断"""
    return "|".join(sorted((re.escape(w) for w in words), key=len, reverse=True))


DE_MISUSE_RE = re.compile(r"(?:%s)的(?:%s)" % (_alt(DE_ADVERB), _alt(DE_VERB)))
DI_MISUSE_RE = re.compile(r"地(?:%s)" % _alt(DI_NOUN))

# 「在 / 再」常见误用
ZAI_MISUSE_RES = (
    (
        re.compile(
            r"在(?:见|也(?:不会|不能|不是|没有|不行|不要)|说(?:一遍|一次)|"
            r"做(?:一遍|一次)|来(?:一遍|一次)|试(?:一遍|一次)|看(?:一遍|一次)|"
            r"想(?:一遍|一次)|三(?:考虑|强调|要求|确认|检查|道歉|叮嘱|声明))"
        ),
        "再",
    ),
    (
        re.compile(
            r"再(?:家|学校|公司|单位|路上|床上|桌子|屋里|屋外|门口|外面|里面|"
            r"这里|那里|办公室|教室|厨房|客厅|卧室|车上|国内|国外|老家|乡下|"
            r"城市|农村|北京|上海|广州|深圳)"
        ),
        "在",
    ),
)

# 「做 / 作」常见误用：左边是错的，右边是该用的词
ZUO_MISUSE_MAP = {
    "做用": "作用", "做业": "作业", "做者": "作者", "做品": "作品", "做息": "作息",
    "做文": "作文", "做为": "作为", "做风": "作风", "做废": "作废", "做弊": "作弊",
    "做家": "作家", "作饭": "做饭", "作梦": "做梦", "作事": "做事", "作活": "做活",
    "作工": "做工", "作买卖": "做买卖",
}

# 重复词（常见虚词/助词连续重复），「的的确确」是固定语，要先排除
REPEAT_CN_RE = re.compile(
    r"(的的|了了|是是|在在|和和|很很|都都|就就|也也|会会|能能|要要|把把|"
    r"被被|着着|过过|还还|又又|却却|但但|而而|或或|与与|及及)"
)
REPEAT_CN_EXCLUDE = ("的的确确",)

# 常见英文拼写误用表（错的 → 对的）
MISSPELLINGS = {
    "teh": "the",
    "recieve": "receive",
    "seperate": "separate",
    "occured": "occurred",
    "definately": "definitely",
    "wich": "which",
    "thier": "their",
    "freind": "friend",
    "goverment": "government",
    "beleive": "believe",
    "enviroment": "environment",
    "neccessary": "necessary",
    "accomodate": "accommodate",
    "tommorow": "tomorrow",
    "begining": "beginning",
    "existance": "existence",
    "arguement": "argument",
    "wierd": "weird",
    "untill": "until",
    "wether": "whether",
    "truely": "truly",
    "calender": "calendar",
    "independant": "independent",
    "knowlege": "knowledge",
    "occassion": "occasion",
    "reccomend": "recommend",
    "succesful": "successful",
    "vaccuum": "vacuum",
    "embarass": "embarrass",
    "harrass": "harass",
    "publically": "publicly",
}
MISSPELLING_PATTERNS = tuple(
    (re.compile(r"\b%s\b" % re.escape(wrong), re.IGNORECASE), wrong, right)
    for wrong, right in sorted(MISSPELLINGS.items())
)

# there / their 启发式
THEIR_IS_RE = re.compile(
    r"\btheir\s+(is|are|was|were|will|has|have|had|seems|appears|exists|remains)\b",
    re.IGNORECASE,
)
THERE_OWN_RE = re.compile(
    r"\bthere\s+(own|children|parents|house|car|job|lives|friends|name|time|work|"
    r"school|money|ideas|problems|opinions|hands|eyes|heads|hearts|goals|plans|"
    r"views|rights|duties)\b",
    re.IGNORECASE,
)

# 句子首字母小写检查要跳过的缩写（句点不是句末）
SENTENCE_ABBREVIATIONS = (
    "e.g", "i.e", "etc", "vs", "no", "mr", "mrs", "ms", "dr", "st", "fig",
    "approx", "resp", "cf", "al", "jr", "sr", "inc", "ltd", "co", "dept",
    "est", "vol", "pp", "op", "ca", "min", "max", "avg",
)


# ---------- 修复建议文案 ----------

FIX_DE_DI = (
    "修饰动词用「地」，修饰名词用「的」，补充说明用「得」：\n\n"
    "```text\n"
    "快速地跑（不是「快速的跑」）\n"
    "认真的态度（不是「认真地态度」）\n"
    "跑得很快（不是「跑的很快」）\n"
    "```"
)
FIX_ZAI = (
    "表示重复/又一次用「再」，表示地点/正在用「在」：\n\n"
    "```text\n"
    "再见、再说一遍、再试一次（不是「在见」）\n"
    "在家、在学校、正在学习（不是「再家」）\n"
    "```"
)
FIX_ZUO = (
    "「作」多用于较抽象、书面的合成词，「做」多用于具体动作：\n\n"
    "```text\n"
    "作用、作业、作者、作品、作为（不是「做用」「做业」）\n"
    "做饭、做事、做梦、做活（不是「作饭」「作事」）\n"
    "```"
)
FIX_REPEAT_WORD = (
    "删掉重复的那个字；中文虚词一般不叠用（「的的确确」这类固定语除外）：\n\n"
    "```text\n"
    "这个问题需要解决（不是「这个的问题」）\n"
    "我马上就去（不是「我马上就就去」）\n"
    "```"
)
FIX_REPEAT_PUNCT = (
    "标点不要连写，问号/叹号最多用一个；省略号统一用「……」或「...」：\n\n"
    "```text\n"
    "真的吗？（不是「真的吗？？」）\n"
    "好。（不是「好。。」）\n"
    "```"
)
FIX_MIXED_PUNCT = (
    "中文句子里用全角标点，英文句子里用半角标点，不要混着用：\n\n"
    "```text\n"
    "今天天气很好，我们去散步。（不是「今天天气很好,我们去散步.」）\n"
    "It works, so we shipped it.（不是「It works，so we shipped it。」）\n"
    "```"
)
FIX_SPACE_BETWEEN = (
    "中文与英文/数字之间加一个半角空格，阅读节奏更好：\n\n"
    "```text\n"
    "我们用 Python 写脚本（不是「我们用Python写脚本」）\n"
    "```"
)
FIX_FULLWIDTH = (
    "数字和字母统一用半角，中文标点才用全角；全角空格（U+3000）不要出现在行内：\n\n"
    "```text\n"
    "型号是 ABC-2026（不是「型号是ＡＢＣ-２０２６」）\n"
    "```"
)
FIX_MULTI_SPACE = (
    "删掉多余的空格，词与词之间只留一个半角空格：\n\n"
    "```text\n"
    "你好 世界（不是「你好  世界」）\n"
    "```"
)
FIX_TRAILING_SPACE = (
    "删掉行尾的空格/制表符：\n\n"
    "```bash\n"
    "sed -i '' 's/[[:space:]]*$//' 文章.md   # macOS\n"
    "```"
)
FIX_SENTENCE_CASE = (
    "每个句子的首字母大写：\n\n"
    "```text\n"
    "This is fine. Another sentence.（不是「This is fine. another sentence.」）\n"
    "```"
)
FIX_DOUBLE_SPACE = (
    "英文单词之间只留一个空格：\n\n"
    "```text\n"
    "Hello world（不是「Hello  world」）\n"
    "```"
)
FIX_REPEAT_WORD_EN = (
    "删掉重复的那个单词：\n\n"
    "```text\n"
    "This is a test.（不是「This is is a test.」）\n"
    "```"
)
FIX_SPELLING = (
    "按正确拼写替换；拿不准时查词典确认：\n\n"
    "```text\n"
    "receive（不是 recieve）\n"
    "separate（不是 seperate）\n"
    "```"
)
FIX_THEIR_THERE = (
    "there 表示「那里有」，their 表示「他们的」：\n\n"
    "```text\n"
    "There is a problem.（不是「Their is a problem.」）\n"
    "their own car（不是「there own car」）\n"
    "```"
)
FIX_LONG_SENT = (
    "长句拆短：一句只说一件事，中间用句号断开：\n\n"
    "```text\n"
    "今天天气很好。我们决定去山里徒步。（合成一句会读得很累）\n"
    "```"
)
FIX_MARKER = (
    "要么补完内容并删掉标记，要么改成能跟踪的任务：\n\n"
    "```text\n"
    "TODO(张三, 2026-11): 这一节要补数据来源，见 issue#123\n"
    "```"
)


# ---------- 数据结构 ----------

class Finding:
    """一条校对发现"""

    __slots__ = ("file", "line", "level", "rule_id", "title", "detail", "suggestion")

    def __init__(self, file, line, level, rule_id, title, detail, suggestion):
        self.file = file
        self.line = line
        self.level = level
        self.rule_id = rule_id
        self.title = title
        self.detail = detail
        self.suggestion = suggestion

    def key(self):
        return (self.file, self.line, self.rule_id, self.title.strip().lower())


class SourceText:
    """一个已载入内存、可校对的文本文件"""

    __slots__ = ("path", "rel", "text", "lines", "truncated")

    def __init__(self, path, rel, text, truncated):
        self.path = path
        self.rel = rel
        self.text = text
        self.lines = text.splitlines()
        self.truncated = truncated


class TextContext:
    """规则函数运行时能看到的文本信息"""

    __slots__ = ("path", "rel", "text", "lines", "space_between")

    def __init__(self, source, space_between=True):
        self.path = source.path
        self.rel = source.rel
        self.text = source.text
        self.lines = source.lines
        self.space_between = space_between


# ---------- 工具函数 ----------

def has_cjk(text):
    return bool(CJK_RE.search(text))


def make_context(text, rel="文本", space_between=True):
    """由一段文本构造规则上下文（自检里大量使用）"""
    return TextContext(SourceText(None, rel, text, False), space_between=space_between)


# ---------- 规则实现（每个函数返回 [(行号, 说明), ...]）----------

def check_de_di_misuse(ctx):
    out = []
    for lineno, line in enumerate(ctx.lines, 1):
        match = DE_MISUSE_RE.search(line)
        if match:
            out.append((
                lineno,
                "「%s」疑似「的/地」误用：修饰动词一般用「地」（如「快速地跑」）。" % match.group(0),
            ))
            continue
        match = DI_MISUSE_RE.search(line)
        if match:
            out.append((
                lineno,
                "「%s」疑似「地/的」误用：后面是名词时一般用「的」（如「认真的态度」）。" % match.group(0),
            ))
    return out


def check_zai_misuse(ctx):
    out = []
    for lineno, line in enumerate(ctx.lines, 1):
        for pattern, right in ZAI_MISUSE_RES:
            match = pattern.search(line)
            if match:
                out.append((
                    lineno,
                    "「%s」疑似「在/再」误用，这里多半应该用「%s」。" % (match.group(0), right),
                ))
                break
    return out


def check_zuo_misuse(ctx):
    out = []
    for lineno, line in enumerate(ctx.lines, 1):
        for wrong, right in sorted(ZUO_MISUSE_MAP.items()):
            if wrong in line:
                out.append((
                    lineno,
                    "「%s」疑似「做/作」误用，规范写法是「%s」。" % (wrong, right),
                ))
                break
    return out


def check_repeat_cn_word(ctx):
    out = []
    for lineno, line in enumerate(ctx.lines, 1):
        probe = line
        for token in REPEAT_CN_EXCLUDE:
            probe = probe.replace(token, "　" * len(token))
        match = REPEAT_CN_RE.search(probe)
        if match:
            out.append((
                lineno,
                "「%s」是同一个字连续重复，疑似多敲了一个字。" % match.group(0),
            ))
    return out


def check_repeat_punct(ctx):
    out = []
    for lineno, line in enumerate(ctx.lines, 1):
        match = REPEAT_PUNCT_CN_RE.search(line) or REPEAT_PUNCT_EN_RE.search(line) or REPEAT_DOT_RE.search(line)
        if match:
            out.append((
                lineno,
                "标点「%s」连续重复，中文标点/问号叹号都不需要连写。" % match.group(0),
            ))
    return out


def check_mixed_punct(ctx):
    out = []
    for lineno, line in enumerate(ctx.lines, 1):
        if not has_cjk(line):
            continue
        for pattern in MIXED_PUNCT_RES:
            match = pattern.search(line)
            if match:
                out.append((
                    lineno,
                    "中文句子里出现了半角标点「%s」，与全角标点混用。" % match.group(0),
                ))
                break
    return out


def check_space_between(ctx):
    if not ctx.space_between:
        return []
    out = []
    for lineno, line in enumerate(ctx.lines, 1):
        for pattern in SPACE_BETWEEN_RES:
            match = pattern.search(line)
            if match:
                out.append((
                    lineno,
                    "「%s」中英文/数字之间缺少空格。" % match.group(0),
                ))
                break
    return out


def check_fullwidth_alnum(ctx):
    out = []
    for lineno, line in enumerate(ctx.lines, 1):
        match = FULLWIDTH_ALNUM_RE.search(line)
        if match:
            token = match.group(0)
            name = "全角空格" if token == "\u3000" else "全角字符「%s」" % token
            out.append((
                lineno,
                "%s 出现在数字/字母位置，全角与半角混用会让排版和搜索都出问题。" % name,
            ))
    return out


def check_multi_space(ctx):
    out = []
    for lineno, line in enumerate(ctx.lines, 1):
        if not has_cjk(line):
            continue
        match = MULTI_SPACE_RE.search(line)
        if match:
            out.append((
                lineno,
                "行内出现连续 %d 个空格，多半是排版残留。" % len(match.group(0)),
            ))
    return out


def check_trailing_space(ctx):
    out = []
    for lineno, line in enumerate(ctx.lines, 1):
        if TRAILING_SPACE_RE.search(line):
            out.append((
                lineno,
                "行尾有多余的空格/制表符，diff 里会显示成无意义的改动。",
            ))
    return out


def check_sentence_case(ctx):
    out = []
    for lineno, line in enumerate(ctx.lines, 1):
        if has_cjk(line):
            continue
        hit = None
        for match in SENTENCE_LOWER_RE.finditer(line):
            before = line[:match.start()].rstrip()
            if before:
                tail = before[-8:].lower()
                if any(tail.endswith(abbrev + ".") or tail.endswith(abbrev + " .") for abbrev in SENTENCE_ABBREVIATIONS):
                    continue
            hit = match.group(1)
            break
        if hit:
            out.append((
                lineno,
                "句子首字母「%s」是小写，英文句子开头应大写。" % hit,
            ))
    return out


def check_double_space_en(ctx):
    out = []
    for lineno, line in enumerate(ctx.lines, 1):
        if has_cjk(line):
            continue
        match = MULTI_SPACE_RE.search(line)
        if match:
            out.append((
                lineno,
                "英文行内出现连续 %d 个空格。" % len(match.group(0)),
            ))
    return out


def check_repeat_word_en(ctx):
    out = []
    for lineno, line in enumerate(ctx.lines, 1):
        if has_cjk(line):
            continue
        match = REPEAT_WORD_EN_RE.search(line)
        if match:
            out.append((
                lineno,
                "单词「%s」连续重复了两次，多半是手误。" % match.group(1),
            ))
    return out


def check_spelling(ctx):
    out = []
    for lineno, line in enumerate(ctx.lines, 1):
        for pattern, wrong, right in MISSPELLING_PATTERNS:
            match = pattern.search(line)
            if not match:
                continue
            typed = match.group(0)
            suggestion = right
            if typed[:1].isupper():
                suggestion = right[:1].upper() + right[1:]
            if typed.isupper():
                suggestion = right.upper()
            out.append((
                lineno,
                "「%s」是常见拼写误用，正确拼写是「%s」。" % (typed, suggestion),
            ))
            break
    return out


def check_their_there(ctx):
    out = []
    for lineno, line in enumerate(ctx.lines, 1):
        match = THEIR_IS_RE.search(line)
        if match:
            out.append((
                lineno,
                "「%s」疑似 there/their 误用：表示「有」应该用 there。" % match.group(0),
            ))
            continue
        match = THERE_OWN_RE.search(line)
        if match:
            out.append((
                lineno,
                "「%s」疑似 there/their 误用：表示「他们的」应该用 their。" % match.group(0),
            ))
    return out


def check_long_sentence(ctx):
    out = []
    for lineno, line in enumerate(ctx.lines, 1):
        for seg in SENT_SPLIT_RE.split(line):
            seg = seg.strip()
            if not seg:
                continue
            if has_cjk(seg):
                if len(seg) > LONG_SENT_CN:
                    out.append((
                        lineno,
                        "这个句子约 %d 个字符，超过 %d 字，一口气读不完，建议拆成短句。" % (len(seg), LONG_SENT_CN),
                    ))
                    break
            else:
                words = WORD_RE.findall(seg)
                if len(words) > LONG_SENT_EN:
                    out.append((
                        lineno,
                        "这个英文句子约 %d 个词，超过 %d 词，建议拆成短句。" % (len(words), LONG_SENT_EN),
                    ))
                    break
    return out


def check_todo_marker(ctx):
    out = []
    for lineno, line in enumerate(ctx.lines, 1):
        match = MARKER_RE.search(line)
        if match:
            out.append((
                lineno,
                "残留了 %s 标记，正文里应该补完或改写成任务。" % match.group(1),
            ))
    return out


# ---------- 规则表 ----------

RULE_SPECS = [
    {
        "id": "GR001",
        "name": "的/地/得误用",
        "level": "提示",
        "category": "中文",
        "desc": "「快速的跑」式误用（的+动词、地+名词）",
        "fix": FIX_DE_DI,
        "check": check_de_di_misuse,
    },
    {
        "id": "GR002",
        "name": "在/再误用",
        "level": "警告",
        "category": "中文",
        "desc": "「在见」「在也不会」「再家」等常见误用",
        "fix": FIX_ZAI,
        "check": check_zai_misuse,
    },
    {
        "id": "GR003",
        "name": "做/作误用",
        "level": "警告",
        "category": "中文",
        "desc": "「做用」「作饭」等合成词用字错误",
        "fix": FIX_ZUO,
        "check": check_zuo_misuse,
    },
    {
        "id": "GR004",
        "name": "重复词",
        "level": "警告",
        "category": "中文",
        "desc": "虚词/助词连续重复，如「的的」「了了」",
        "fix": FIX_REPEAT_WORD,
        "check": check_repeat_cn_word,
    },
    {
        "id": "GR005",
        "name": "重复标点",
        "level": "提示",
        "category": "通用",
        "desc": "「？？」「！！」「。。」等标点连写",
        "fix": FIX_REPEAT_PUNCT,
        "check": check_repeat_punct,
    },
    {
        "id": "GR006",
        "name": "中英文标点混用",
        "level": "警告",
        "category": "中文",
        "desc": "中文句子里出现半角 , . ? ! : ;",
        "fix": FIX_MIXED_PUNCT,
        "check": check_mixed_punct,
    },
    {
        "id": "GR007",
        "name": "中英文之间缺空格",
        "level": "提示",
        "category": "中文",
        "desc": "「用Python写」式缺少半角空格（--no-space-between 可关闭）",
        "fix": FIX_SPACE_BETWEEN,
        "check": check_space_between,
    },
    {
        "id": "GR008",
        "name": "全角/半角混用",
        "level": "提示",
        "category": "通用",
        "desc": "数字/字母位置出现全角字符或全角空格",
        "fix": FIX_FULLWIDTH,
        "check": check_fullwidth_alnum,
    },
    {
        "id": "GR009",
        "name": "连续空格",
        "level": "提示",
        "category": "中文",
        "desc": "中文行内出现两个及以上空格",
        "fix": FIX_MULTI_SPACE,
        "check": check_multi_space,
    },
    {
        "id": "GR010",
        "name": "行尾空白",
        "level": "提示",
        "category": "通用",
        "desc": "行尾有多余的空格/制表符",
        "fix": FIX_TRAILING_SPACE,
        "check": check_trailing_space,
    },
    {
        "id": "GR011",
        "name": "句子首字母小写",
        "level": "警告",
        "category": "英文",
        "desc": "英文句子开头字母没有大写",
        "fix": FIX_SENTENCE_CASE,
        "check": check_sentence_case,
    },
    {
        "id": "GR012",
        "name": "英文双空格",
        "level": "提示",
        "category": "英文",
        "desc": "英文单词之间出现连续空格",
        "fix": FIX_DOUBLE_SPACE,
        "check": check_double_space_en,
    },
    {
        "id": "GR013",
        "name": "重复单词",
        "level": "警告",
        "category": "英文",
        "desc": "「the the」式相邻重复单词",
        "fix": FIX_REPEAT_WORD_EN,
        "check": check_repeat_word_en,
    },
    {
        "id": "GR014",
        "name": "常见拼写误用",
        "level": "警告",
        "category": "英文",
        "desc": "teh→the、recieve→receive 等 30 组误用",
        "fix": FIX_SPELLING,
        "check": check_spelling,
    },
    {
        "id": "GR015",
        "name": "there/their 误用",
        "level": "警告",
        "category": "英文",
        "desc": "「their is」「there own」等启发式误用",
        "fix": FIX_THEIR_THERE,
        "check": check_their_there,
    },
    {
        "id": "GR016",
        "name": "超长句",
        "level": "提示",
        "category": "通用",
        "desc": "中文句子超过 60 字或英文句子超过 25 词",
        "fix": FIX_LONG_SENT,
        "check": check_long_sentence,
    },
    {
        "id": "GR017",
        "name": "残留标记",
        "level": "提示",
        "category": "通用",
        "desc": "TODO / FIXME / XXX / HACK 未清理",
        "fix": FIX_MARKER,
        "check": check_todo_marker,
    },
]

RULES = RULE_SPECS  # 别名，便于阅读


def rules_for_lang(lang):
    """按 --lang 过滤规则：auto 返回全部，中文/英文 只留对应类别加通用"""
    if lang == "auto":
        return list(RULES)
    return [rule for rule in RULES if rule["category"] in (lang, "通用")]


def analyze_source(source, rules, space_between=True):
    """对单个文件执行一组规则，返回 Finding 列表"""
    ctx = TextContext(source, space_between=space_between)
    findings = []
    for rule in rules:
        try:
            hits = rule["check"](ctx)
        except Exception:
            # 单条规则内部出错不能拖垮整次校对
            hits = []
        for lineno, detail in hits[:MAX_FINDINGS_PER_RULE]:
            findings.append(Finding(
                source.rel,
                lineno,
                rule["level"],
                rule["id"],
                rule["name"],
                detail,
                rule["fix"],
            ))
    return findings


def analyze_text(text, rules=None, rel="文本", space_between=True):
    """直接对一段文本跑规则（自检与库调用都用它）"""
    source = SourceText(None, rel, text, False)
    return analyze_source(source, rules if rules is not None else list(RULES), space_between)


# ---------- 文件收集 ----------

def _iter_directory(root):
    for dirpath, dirnames, filenames in os.walk(str(root)):
        dirnames[:] = sorted(d for d in dirnames if d not in IGNORED_DIRS)
        for name in sorted(filenames):
            path = Path(dirpath) / name
            yield path, path.relative_to(root).as_posix()


def load_source(path, rel, max_chars):
    """读取一个文件；返回 (SourceText 或 None, 跳过原因)"""
    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            raw = handle.read(max_chars * 4 + 8)
    except OSError as exc:
        return None, "读取失败：%s" % exc
    if b"\x00" in raw[:4096]:
        return None, "二进制文件"
    over_limit = size > len(raw)
    try:
        text = raw.decode("utf-8", "ignore" if over_limit else "strict")
    except UnicodeDecodeError:
        return None, "非 UTF-8 文本"
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    truncated = over_limit or len(text) > max_chars
    if len(text) > max_chars:
        text = text[:max_chars]
    return SourceText(path, rel, text, truncated), ""


def collect_sources(paths, max_files=DEFAULT_MAX_FILES, max_file_chars=DEFAULT_MAX_FILE_CHARS):
    """展开路径、过滤、读取。返回 (sources, skipped)"""
    sources = []
    skipped = []
    seen = set()
    for raw_path in paths:
        path = Path(raw_path).expanduser()
        if not path.exists():
            raise FileNotFoundError("路径不存在：%s" % raw_path)
        if path.is_file():
            candidates = [(path, path.name)]
        else:
            candidates = list(_iter_directory(path))
        for file_path, rel in candidates:
            if len(sources) >= max_files:
                skipped.append((rel, "达到 --max-files=%d 上限，未校对" % max_files))
                continue
            try:
                resolved = file_path.resolve()
            except OSError:
                resolved = file_path
            key = os.path.normcase(str(resolved))
            if key in seen:
                continue
            seen.add(key)
            source, reason = load_source(file_path, rel, max_file_chars)
            if source is None:
                skipped.append((rel, reason))
                continue
            sources.append(source)
    return sources, skipped


# ---------- 模型语义校对 ----------

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


SEMANTIC_SYSTEM_PROMPT = """你是一名资深中英文文字校对编辑。用户会给出若干文本，每行前面有行号。
请只找真实存在的语言问题：语法错误、用词不当、搭配不地道、指代不清、语序别扭、
表述啰嗦或歧义。规则能查出来的排版问题（标点、空格、拼写）不要重复报告。

输出要求：
1. 只输出一个 JSON 数组，不要输出任何解释文字，不要使用 Markdown 代码围栏。
2. 数组每个元素包含字段：
   - line：整数，绝对行号（以每行前面的行号为准）
   - severity：只能是 错误 / 警告 / 提示 之一
   - type：问题类型短语，如「语法」「搭配」「语序」
   - problem：问题是什么、为什么不好
   - suggestion：建议怎么改，可以直接给出改后的句子
3. 没有发现问题就输出 []。
"""

SEVERITY_ALIASES = {
    "错误": "错误", "error": "错误", "严重": "错误", "critical": "错误", "major": "错误",
    "警告": "警告", "warning": "警告", "warn": "警告",
    "提示": "提示", "info": "提示", "hint": "提示", "建议": "提示", "minor": "提示",
}


def render_numbered(start_line, lines):
    body = "\n".join("%6d | %s" % (start_line + offset, line) for offset, line in enumerate(lines))
    return "```\n%s\n```\n\n" % body


def build_semantic_batches(sources, limit=SEMANTIC_BATCH_CHARS):
    """把文件切成不超过 limit 字符的批次，每行带绝对行号"""
    segments = []
    for source in sources:
        lines = source.lines
        if not lines:
            continue
        chunk = []
        chunk_size = 0
        start_line = 1
        for offset, line in enumerate(lines):
            rendered = "%6d | %s" % (offset + 1, line[:4000])
            if chunk and chunk_size + len(rendered) + 1 > limit - 400:
                segments.append({
                    "label": "%s（第 %d 行起）" % (source.rel, start_line),
                    "content": render_numbered(start_line, chunk),
                })
                chunk = []
                chunk_size = 0
                start_line = offset + 1
            chunk.append(line[:4000])
            chunk_size += len(rendered) + 1
        if chunk:
            segments.append({
                "label": "%s（第 %d 行起）" % (source.rel, start_line),
                "content": render_numbered(start_line, chunk),
            })

    batches = []
    current = []
    current_size = 0
    for segment in segments:
        content = segment["content"]
        if current and current_size + len(content) > limit:
            batches.append({
                "label": "、".join(item["label"] for item in current)[:120],
                "content": "".join(item["content"] for item in current),
            })
            current = []
            current_size = 0
        current.append(segment)
        current_size += len(content)
    if current:
        batches.append({
            "label": "、".join(item["label"] for item in current)[:120],
            "content": "".join(item["content"] for item in current),
        })
    return batches


def parse_model_findings(text):
    """尽力把模型回复解析成 Finding 列表；解析不出来返回 None"""
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
    findings = []
    for item in data:
        if not isinstance(item, dict):
            continue
        problem = str(item.get("problem") or "").strip()
        if not problem:
            continue
        try:
            line = int(item.get("line") or 0)
        except (TypeError, ValueError):
            line = 0
        severity = str(item.get("severity") or "").strip()
        level = SEVERITY_ALIASES.get(severity, SEVERITY_ALIASES.get(severity.lower(), "提示"))
        kind = str(item.get("type") or "语义").strip() or "语义"
        suggestion = str(item.get("suggestion") or "").strip() or "（模型未给出改法）"
        findings.append(Finding(
            "（模型）",
            max(1, line),
            level,
            "MODEL",
            "[模型] %s" % kind,
            problem,
            suggestion,
        ))
    return findings or None


def semantic_check(sources, model, batch_chars=SEMANTIC_BATCH_CHARS, verbose=True):
    """分批调用模型做语义校对，返回 (findings, raw_outputs)"""
    client = anthropic.Anthropic()
    batches = build_semantic_batches(sources, batch_chars)
    findings = []
    raw_outputs = []
    total = len(batches)
    for index, batch in enumerate(batches, 1):
        if verbose:
            print("[语义校对] 第 %d/%d 批：%s" % (index, total, batch["label"]))
        prompt = "请校对下面的文本：\n\n%s" % batch["content"]
        response = call_model(
            client,
            model,
            SEMANTIC_SYSTEM_PROMPT,
            [{"role": "user", "content": prompt}],
            max_tokens=8000,
        )
        text = response_text(response)
        parsed = parse_model_findings(text)
        if parsed is None:
            raw_outputs.append({"label": "第 %d 批（%s）" % (index, batch["label"]), "text": text})
            if verbose:
                print("[语义校对] 第 %d 批没有返回可解析的 JSON，已原样放入报告附录。" % index)
            continue
        findings.extend(parsed)
    return findings, raw_outputs


# ---------- 报告 ----------

def dedupe_findings(findings):
    seen = set()
    result = []
    for finding in findings:
        key = finding.key()
        if key in seen:
            continue
        seen.add(key)
        result.append(finding)
    return result


def sort_findings(findings):
    return sorted(
        findings,
        key=lambda f: (-LEVEL_ORDER.get(f.level, 0), f.file.lower(), f.line),
    )


def filter_by_level(findings, min_level):
    threshold = LEVEL_ORDER.get(min_level, 1)
    return [f for f in findings if LEVEL_ORDER.get(f.level, 0) >= threshold]


def _indent(text, spaces=2):
    prefix = " " * spaces
    return "\n".join(prefix + line if line else line for line in text.splitlines())


def render_finding(index, finding):
    rule_label = "模型语义校对" if finding.rule_id == "MODEL" else "规则 %s %s" % (finding.rule_id, finding.title)
    return "\n".join([
        "### %d. [%s] %s" % (index, finding.level, finding.title),
        "",
        "位置：`%s:%d` · %s" % (finding.file, finding.line, rule_label),
        "",
        "- 问题：%s" % finding.detail,
        "- 建议：",
        "",
        _indent(finding.suggestion, 2),
        "",
    ])


def render_section(title, findings):
    lines = ["## %s" % title, ""]
    if not findings:
        lines.append("（无）")
        lines.append("")
        return lines
    for index, finding in enumerate(findings, 1):
        lines.append(render_finding(index, finding))
    return lines


def build_report(sources, findings, raw_outputs, min_level, lang, model, offline,
                 skipped=None, generated_at=None):
    """生成完整的 Markdown 校对报告文本"""
    skipped = skipped or []
    filtered = sort_findings(dedupe_findings(filter_by_level(findings, min_level)))
    counts = dict((level, 0) for level in LEVELS)
    for finding in filtered:
        counts[finding.level] = counts.get(finding.level, 0) + 1

    total_lines = sum(len(source.lines) for source in sources)
    now = generated_at or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    lang_label = {"auto": "自动（中英混合）", "中文": "中文", "英文": "英文"}.get(lang, lang)

    lines = [
        "# 校对报告",
        "",
        "- 生成时间：%s" % now,
        "- 语言模式：%s" % lang_label,
        "- 运行模式：%s" % ("仅规则检查（--offline）" if offline else "规则检查 + 模型语义校对（%s）" % model),
        "- 最小展示级别：%s" % min_level,
        "",
        "## 概览",
        "",
        "- 校对文件：%d 个，共 %d 行" % (len(sources), total_lines),
        "- 问题合计：%d 个（错误 %d、警告 %d、提示 %d）"
        % (len(filtered), counts["错误"], counts["警告"], counts["提示"]),
        "- 去重前发现：%d 条（已按级别排序）" % len(findings),
        "",
    ]
    lines.extend(render_section("错误", [f for f in filtered if f.level == "错误"]))
    lines.extend(render_section("警告", [f for f in filtered if f.level == "警告"]))
    lines.extend(render_section("提示", [f for f in filtered if f.level == "提示"]))

    lines.append("## 附录：文件清单")
    lines.append("")
    lines.append("| 文件 | 行数 | 状态 |")
    lines.append("| --- | --- | --- |")
    for source in sources:
        status = "已截断（超过 %d 字符）" % DEFAULT_MAX_FILE_CHARS if source.truncated else "正常"
        lines.append("| `%s` | %d | %s |" % (source.rel, len(source.lines), status))
    if not sources:
        lines.append("| （无） | 0 | - |")
    lines.append("")
    if skipped:
        lines.append("已跳过：")
        lines.append("")
        for rel, reason in skipped[:100]:
            lines.append("- `%s`：%s" % (rel, reason))
        if len(skipped) > 100:
            lines.append("- ……（其余 %d 个跳过项省略）" % (len(skipped) - 100))
        lines.append("")

    if raw_outputs:
        lines.append("## 附录：模型补充意见（未能解析为 JSON）")
        lines.append("")
        lines.append("以下内容来自模型，但不符合约定的 JSON 格式，原样保留供人工参考。")
        lines.append("")
        for item in raw_outputs:
            lines.append("### %s" % item["label"])
            lines.append("")
            lines.append("```text")
            lines.append(item["text"] or "（模型没有返回任何内容）")
            lines.append("```")
            lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def render_rules_table():
    lines = [
        "中英文校对内置规则（不需要模型）：",
        "",
        "ID      级别  类别  说明",
        "------  ----  ----  ------------------------------------------",
    ]
    for rule in RULES:
        lines.append("%-6s  %-4s  %-4s  %s" % (
            rule["id"],
            rule["level"],
            rule["category"],
            rule["desc"],
        ))
    lines.append("")
    lines.append("共 %d 条。用 --lang 可按语言筛选，用 --no-space-between 可关闭 GR007。" % len(RULES))
    return "\n".join(lines)


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


# 每条规则一条能命中它的样本（自检逐条验证）
RULE_SAMPLES = {
    "GR001": "他快速的跑向终点。\n她的认真地态度让人佩服。\n",
    "GR002": "我们在见吧，我在也不会来了，我们在家再家等你。\n",
    "GR003": "这项做用很明显，我要作饭了。\n",
    "GR004": "这个的问题需要解决，我马上就就去处理。\n",
    "GR005": "这是什么？？真的！！好。。\n",
    "GR006": "今天天气很好,我们去散步吧?好吧。\n",
    "GR007": "我们用Python写脚本。\n",
    "GR008": "价格是２０２６元，型号ＡＢＣ。\n",
    "GR009": "你好  世界。\n",
    "GR010": "末尾有空格的行。  \n",
    "GR011": "this is a sentence. another one here.\n",
    "GR012": "Hello  world, this is a test.\n",
    "GR013": "This is is a test of the the system.\n",
    "GR014": "I recieve teh package tomorrow.\n",
    "GR015": "Their is a problem with there own car.\n",
    "GR016": "这是一段用于测试超长句子检测规则的示例文本，" * 3 + "读起来非常累。\n",
    "GR017": "TODO: 这一节待补充。\n",
}

CLEAN_SAMPLE = (
    "今天天气很好，我们去公园散步。\n"
    "她认真地学习，跑得很快，美丽的风景让人心情愉快。\n"
    "We finished the project on time. Their team did a great job.\n"
    "There is nothing to worry about.\n"
    "请把报告发给所有人。\n"
)


def run_self_test():
    """离线自检：造样本、跑全部确定性逻辑、assert 校验，不联网、不需要 API key"""
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)

        # --- 每条规则都要能命中 ---
        for rule in RULES:
            sample = RULE_SAMPLES.get(rule["id"])
            assert sample, "规则 %s 缺少自检样本" % rule["id"]
            findings = analyze_text(sample)
            hit_ids = set(f.rule_id for f in findings)
            assert rule["id"] in hit_ids, "规则 %s 未命中样本：%s" % (rule["id"], sorted(hit_ids))
            for finding in findings:
                assert finding.file
                assert finding.line >= 1
                assert finding.level in LEVELS
                assert finding.detail
                assert finding.suggestion
        assert len(RULES) >= 15

        # --- 干净样本零误报（开着全部规则，含 GR007）---
        clean_findings = analyze_text(CLEAN_SAMPLE, space_between=True)
        assert not clean_findings, "干净样本出现误报：%s" % [
            (f.rule_id, f.line, f.detail) for f in clean_findings
        ]

        # --- 的/地/得：两条启发式都能单独命中 ---
        both = analyze_text("他快速的跑。\n她的认真地态度很好。\n")
        assert sum(1 for f in both if f.rule_id == "GR001") == 2

        # --- 在/再：两个方向都能命中 ---
        zai = analyze_text("在见。\n再家。\n")
        assert sum(1 for f in zai if f.rule_id == "GR002") == 2

        # --- 重复标点：三种写法都能命中，省略号不算 ---
        punct = analyze_text("好。。\n好??\nWait...\n")
        assert sum(1 for f in punct if f.rule_id == "GR005") == 2

        # --- GR007 开关 ---
        space_text = "我们用Python写脚本。\n"
        assert [f for f in analyze_text(space_text, space_between=True) if f.rule_id == "GR007"]
        assert not [f for f in analyze_text(space_text, space_between=False) if f.rule_id == "GR007"]

        # --- 级别过滤 ---
        all_findings = []
        for rule in RULES:
            all_findings.extend(analyze_text(RULE_SAMPLES[rule["id"]]))
        warnings_only = filter_by_level(all_findings, "警告")
        assert warnings_only
        assert all(LEVEL_ORDER[f.level] >= 2 for f in warnings_only)
        assert len(warnings_only) < len(all_findings)
        errors_only = filter_by_level(all_findings, "错误")
        assert all(f.level == "错误" for f in errors_only)

        # --- 语言筛选 ---
        cn_rules = rules_for_lang("中文")
        en_rules = rules_for_lang("英文")
        assert all(rule["category"] in ("中文", "通用") for rule in cn_rules)
        assert all(rule["category"] in ("英文", "通用") for rule in en_rules)
        assert len(rules_for_lang("auto")) == len(RULES)
        assert len(cn_rules) < len(RULES) and len(en_rules) < len(RULES)

        # --- 文件收集：目录递归、二进制跳过 ---
        (root / "sub").mkdir()
        (root / "a.md").write_text(RULE_SAMPLES["GR005"], encoding="utf-8")
        (root / "sub" / "b.txt").write_text(RULE_SAMPLES["GR014"], encoding="utf-8")
        (root / "clean.md").write_text(CLEAN_SAMPLE, encoding="utf-8")
        (root / "blob.bin").write_bytes(b"\x00\x01\x02binary")
        sources, skipped = collect_sources([str(root)])
        rels = [s.rel for s in sources]
        assert "a.md" in rels and "sub/b.txt" in rels and "clean.md" in rels
        assert "blob.bin" not in rels
        assert any("二进制" in reason for _, reason in skipped)

        # --- 文件级校对 + 报告结构 ---
        file_findings = []
        for source in sources:
            file_findings.extend(analyze_source(source, list(RULES)))
        report = build_report(
            sources, file_findings, [], "提示", "auto", "claude-opus-5", True,
            skipped=skipped, generated_at="2026-01-01 00:00:00",
        )
        assert report.startswith("# 校对报告")
        for heading in ("## 概览", "## 错误", "## 警告", "## 提示", "## 附录：文件清单"):
            assert heading in report, "报告缺少：%s" % heading
        assert "a.md" in report and "sub/b.txt" in report
        assert "位置：" in report and "问题：" in report and "建议：" in report
        assert "- 运行模式：仅规则检查（--offline）" in report

        report_warn = build_report(
            sources, file_findings, [], "警告", "auto", "claude-opus-5", True,
            generated_at="2026-01-01 00:00:00",
        )
        assert "- 最小展示级别：警告" in report_warn
        assert "### 1. [提示]" not in report_warn

        report_model = build_report(
            sources, file_findings,
            [{"label": "第 1 批（a.md）", "text": "自由格式意见"}],
            "提示", "中文", "qwen3.8-max", False,
            generated_at="2026-01-01 00:00:00",
        )
        assert "模型语义校对（qwen3.8-max）" in report_model
        assert "模型补充意见（未能解析为 JSON）" in report_model
        assert "自由格式意见" in report_model

        # --- 模型 JSON 解析与兜底 ---
        good = (
            "```json\n"
            '[{"line":3,"severity":"warning","type":"搭配","problem":"搭配不地道",'
            '"suggestion":"改为更自然的说法"}]\n'
            "```"
        )
        parsed = parse_model_findings(good)
        assert parsed and len(parsed) == 1
        assert parsed[0].line == 3 and parsed[0].level == "警告"
        assert parsed[0].rule_id == "MODEL"
        assert parse_model_findings("这不是 JSON") is None
        assert parse_model_findings("[not json") is None
        assert parse_model_findings("[]") is None
        weird = '[{"line":"x","severity":"未知","type":"","problem":"p","suggestion":""}]'
        coerced = parse_model_findings(weird)
        assert coerced and coerced[0].line == 1 and coerced[0].level == "提示"
        assert coerced[0].suggestion

        # --- 批次切分：每批不超过上限 ---
        batches = build_semantic_batches(sources, 2000)
        assert batches
        for batch in batches:
            assert len(batch["content"]) <= 2000 + 500
        assert "```" in batches[0]["content"]

        # --- call_model 逐级降级（离线假客户端）---
        fake = _FakeClient()
        result = call_model(fake, "fake-model", "system", [{"role": "user", "content": "hi"}])
        assert result["ok"] is True
        assert fake.state["calls"] == 2

        # --- 规则表与报告落盘 ---
        table = render_rules_table()
        assert all(rule["id"] in table for rule in RULES)
        assert "GR001" in table and "GR017" in table

        output_path = root / "proofread-report.md"
        output_path.write_text(report, encoding="utf-8")
        assert output_path.read_text(encoding="utf-8").startswith("# 校对报告")

    print("SELF-TEST OK")


# ---------- CLI ----------

def build_parser():
    parser = argparse.ArgumentParser(
        description="中英文校对助手：先跑确定性规则表，再交给模型做语义校对，生成中文 Markdown 校对报告",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            "  py -3 agent.py 文章.md --lang 中文 --output 校对报告.md\n"
            "  py -3 agent.py ./docs --min-level 警告\n"
            "  py -3 agent.py 文章.md --offline\n"
            "  py -3 agent.py 文章.md --model qwen3.8-max\n"
        ),
    )
    parser.add_argument("paths", nargs="*", help="要校对的文件或目录，可以给多个")
    parser.add_argument(
        "--lang",
        choices=list(LANG_CHOICES),
        default="auto",
        help="校对语言：auto=中英都查（默认），中文 / 英文=只跑对应规则",
    )
    parser.add_argument(
        "--output",
        default=DEFAULT_OUTPUT,
        help="报告写入路径（默认 %s）" % DEFAULT_OUTPUT,
    )
    parser.add_argument(
        "--min-level",
        choices=list(LEVELS),
        default="提示",
        help="报告里展示的最低级别（默认 提示，即全部展示）",
    )
    parser.add_argument(
        "--offline",
        action="store_true",
        help="纯确定性模式：只跑规则，不调用模型、不需要 API key",
    )
    parser.add_argument(
        "--model",
        default=DEFAULT_MODEL,
        help="语义校对使用的模型（默认 %s）" % DEFAULT_MODEL,
    )
    space_group = parser.add_mutually_exclusive_group()
    space_group.add_argument(
        "--space-between",
        dest="space_between",
        action="store_true",
        help="检查中英文之间缺空格（默认开启，即 GR007）",
    )
    space_group.add_argument(
        "--no-space-between",
        dest="space_between",
        action="store_false",
        help="关闭中英文之间缺空格检查（不报 GR007）",
    )
    parser.set_defaults(space_between=True)
    parser.add_argument(
        "--list-rules",
        action="store_true",
        help="打印全部内置规则后退出",
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

    if args.list_rules:
        print(render_rules_table())
        return

    if not args.paths:
        parser.error("请至少提供一个要校对的文件或目录路径")

    try:
        sources, skipped = collect_sources(args.paths)
    except FileNotFoundError as exc:
        print("错误：%s" % exc, file=sys.stderr)
        sys.exit(1)

    if not sources:
        print("错误：没有可校对的文本文件（可能都是二进制，或路径下没有文件）。", file=sys.stderr)
        sys.exit(1)

    rules = rules_for_lang(args.lang)
    print("[规则检查] %d 个文件，规则 %d 条，语言 %s" % (len(sources), len(rules), args.lang))
    findings = []
    for source in sources:
        findings.extend(analyze_source(source, rules, space_between=args.space_between))
    print("[规则检查] 完成，发现 %d 条" % len(findings))

    semantic_findings = []
    raw_outputs = []
    if args.offline:
        print("[模型校对] 已跳过（--offline）")
    else:
        if not os.environ.get("ANTHROPIC_API_KEY") and not os.environ.get("ANTHROPIC_AUTH_TOKEN"):
            print(
                "提示：没有检测到 ANTHROPIC_API_KEY 或 ANTHROPIC_AUTH_TOKEN，无法调用模型做语义校对。\n"
                "请先设置（macOS / Linux）：export ANTHROPIC_API_KEY=你的key\n"
                "Windows PowerShell：$env:ANTHROPIC_API_KEY=\"你的key\"\n"
                "也可以加 --offline 只跑规则检查，不需要 key。",
                file=sys.stderr,
            )
            sys.exit(1)
        print("[模型校对] 模型：%s" % args.model)
        try:
            semantic_findings, raw_outputs = semantic_check(sources, args.model)
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
        print("[模型校对] 完成，发现 %d 条" % len(semantic_findings))

    all_findings = list(findings) + list(semantic_findings)
    report = build_report(
        sources,
        all_findings,
        raw_outputs,
        args.min_level,
        args.lang,
        args.model,
        args.offline,
        skipped=skipped,
    )

    output_path = Path(args.output).expanduser()
    try:
        if output_path.parent and not output_path.parent.exists():
            output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(report, encoding="utf-8")
    except OSError as exc:
        print("错误：报告写入失败：%s" % exc, file=sys.stderr)
        sys.exit(1)

    kept = sort_findings(dedupe_findings(filter_by_level(all_findings, args.min_level)))
    counts = dict((level, 0) for level in LEVELS)
    for finding in kept:
        counts[finding.level] = counts.get(finding.level, 0) + 1
    print("[完成] 报告已写入：%s" % output_path)
    print("[完成] 文件 %d 个，问题 %d 个（错误 %d、警告 %d、提示 %d）"
          % (len(sources), len(kept), counts["错误"], counts["警告"], counts["提示"]))
    if raw_outputs:
        print("[提示] 有 %d 批模型输出不是合法 JSON，已原样放入报告附录。" % len(raw_outputs))


if __name__ == "__main__":
    main()
