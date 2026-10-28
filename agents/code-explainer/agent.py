#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
代码讲解 / 学习助手（code-explainer）—— 结构提取 + 复杂度启发式 + 模型分层讲解

先把代码做完全确定性的分析：语言检测、结构提取（Python 走 ast，其它语言走正则）、
圈复杂度近似、术语扫描，再让模型生成分层讲解（总体作用、逐段讲解、坑点、改进建议、
自测题），最后合并成一份中文 Markdown 讲解文档。除 anthropic SDK 外只用标准库。

用法：
    python3 agent.py <代码文件...> [--lang auto|python|javascript|go|java]
                     [--level 入门|中级|深入] [--output 讲解.md] [--offline]

示例：
    python3 agent.py ./src/app.py --output 讲解.md
    python3 agent.py ./src/utils.js --lang javascript --level 入门
    python3 agent.py ./main.go --offline              # 只做确定性分析，不调模型
    python3 agent.py ./Demo.java --model qwen3.8-max  # 走第三方兼容网关时指定模型名

环境要求：
    1. 调用模型生成讲解时需要设置 API key（--offline 与 --self-test 不需要）：
           macOS / Linux：        export ANTHROPIC_API_KEY=你的key
           Windows PowerShell：   $env:ANTHROPIC_API_KEY="你的key"
    2. 安装依赖：
           pip3 install -r requirements.txt
    3. 走第三方 Anthropic 兼容网关时，用 --model 指定网关认得的模型名，例如：
           python3 agent.py app.py --model qwen3.8-max
"""

import argparse
import ast
import json
import os
import re
import sys
import tempfile
from datetime import datetime
from pathlib import Path

import anthropic

# 默认模型（可用 --model 参数或 CODE_EXPLAINER_MODEL 环境变量更换）
DEFAULT_MODEL = os.environ.get("CODE_EXPLAINER_MODEL", "claude-opus-5")

DEFAULT_OUTPUT = "explanation.md"
LEVELS = ("入门", "中级", "深入")
LANGS = ("auto", "python", "javascript", "go", "java")
LANG_LABELS = {
    "python": "Python",
    "javascript": "JavaScript / TypeScript",
    "go": "Go",
    "java": "Java",
    "unknown": "无法确定（按通用类 C 语法处理）",
}
MAX_FILE_CHARS = 200_000     # 单文件最多读入字符数
TOP_COMPLEX = 3              # 标注多少个高复杂度函数
GLOSSARY_MIN = 60            # 内置术语表至少要这么多条

EXT_LANG = {
    ".py": "python", ".pyw": "python",
    ".js": "javascript", ".jsx": "javascript", ".ts": "javascript",
    ".tsx": "javascript", ".mjs": "javascript", ".cjs": "javascript",
    ".go": "go",
    ".java": "java",
}

# 圈复杂度关键词：if / for / while / case / and / or / 三元 等
COMPLEX_KEYWORD_RE = re.compile(
    r"(?<![\w.])(if|elif|else\s+if|for|while|case|catch|except|and|or)(?![\w])",
    re.IGNORECASE,
)
COMPLEX_OP_RE = re.compile(r"(&&|\|\|)|\?[^\n?:]{0,60}:")
FUNC_CALL_RE = re.compile(r"\b([A-Za-z_]\w*)\s*\(")
KEYWORD_CALLS = {
    "if", "for", "while", "switch", "catch", "return", "sizeof", "typeof",
    "print", "printf", "println", "range", "len", "int", "str", "float",
    "list", "dict", "set", "tuple", "super", "isinstance", "getattr",
}

# ---------- 内置术语表（中英对照 + 一句话解释） ----------

GLOSSARY = [
    ("closure", "闭包", "函数捕获并记住定义时所在作用域的变量"),
    ("decorator", "装饰器", "不改原函数代码就能给它包一层额外行为"),
    ("generator", "生成器", "按需产出元素、不一次性占用内存的迭代器"),
    ("coroutine", "协程", "可以挂起恢复的协作式并发函数"),
    ("idempotent", "幂等", "同一个操作执行一次和多次效果相同"),
    ("race condition", "竞态", "结果取决于多个任务的执行顺序"),
    ("deadlock", "死锁", "多个任务互相等待对方持有的资源"),
    ("recursion", "递归", "函数调用自身，必须有终止条件"),
    ("memoization", "记忆化", "缓存函数结果避免重复计算"),
    ("serialization", "序列化", "把内存对象转成可存储或传输的格式"),
    ("deserialization", "反序列化", "把字节流还原成内存对象"),
    ("concurrency", "并发", "多个任务在时间上交替推进"),
    ("parallelism", "并行", "多个任务真正同时执行"),
    ("thread", "线程", "进程内的执行单元，共享内存"),
    ("process", "进程", "资源隔离的执行单位"),
    ("mutex", "互斥锁", "保证同一时刻只有一个任务进入临界区"),
    ("semaphore", "信号量", "限制同时访问资源的任务数量"),
    ("callback", "回调", "把函数当参数传给别人，在适当时机调用"),
    ("promise", "期约", "表示未来才有的值的异步对象"),
    ("async", "异步", "不阻塞等待结果的编程方式"),
    ("await", "等待", "挂起当前协程直到异步结果就绪"),
    ("exception", "异常", "打断正常流程的错误对象"),
    ("traceback", "堆栈回溯", "报错时的调用链信息"),
    ("inheritance", "继承", "子类复用父类的属性和方法"),
    ("polymorphism", "多态", "同一接口表现出不同实现"),
    ("encapsulation", "封装", "隐藏内部细节只暴露必要接口"),
    ("abstraction", "抽象", "抽取共性、忽略无关细节"),
    ("interface", "接口", "约定能力而不规定实现"),
    ("mixin", "混入", "复用一组方法而不走继承"),
    ("singleton", "单例", "保证一个类只有一个实例"),
    ("factory", "工厂", "把对象创建逻辑集中到一处"),
    ("observer", "观察者", "状态变化时自动通知订阅者"),
    ("adapter", "适配器", "把不兼容的接口转换成可用接口"),
    ("facade", "门面", "用一个简单入口封装一堆子系统"),
    ("proxy", "代理", "替目标对象接收调用并做转发或增强"),
    ("iterator", "迭代器", "统一遍历集合的对象"),
    ("comprehension", "推导式", "用表达式快速构造列表/字典/集合"),
    ("lambda", "匿名函数", "一次性使用的小函数"),
    ("hash", "哈希", "把任意数据映射成固定长度的值"),
    ("index", "索引", "加速查找的数据结构"),
    ("cache", "缓存", "用空间换时间的加速手段"),
    ("buffer", "缓冲区", "临时存放数据的内存区域"),
    ("stream", "流", "按序处理数据而不必全部载入"),
    ("pipeline", "管道", "把处理步骤串成一条流水线"),
    ("refactor", "重构", "不改变行为的前提下改善结构"),
    ("regression", "回归", "改动导致原有功能变坏"),
    ("unit test", "单元测试", "针对最小函数/类的自动化测试"),
    ("mock", "模拟对象", "测试里替代真实依赖的假实现"),
    ("stub", "桩", "返回固定值的假实现"),
    ("dependency injection", "依赖注入", "由外部提供依赖而不是内部硬创建"),
    ("framework", "框架", "规定代码结构的大套件"),
    ("middleware", "中间件", "在请求处理链上插入的通用处理层"),
    ("handler", "处理器", "真正处理某个请求/事件的函数"),
    ("router", "路由", "把请求分发给对应处理器"),
    ("endpoint", "端点", "一个可访问的接口地址"),
    ("request", "请求", "客户端发出的一次调用"),
    ("response", "响应", "服务端返回的结果"),
    ("session", "会话", "一段时间内的连续交互上下文"),
    ("token", "令牌", "代表身份或权限的字符串"),
    ("payload", "载荷", "请求或消息里携带的数据"),
    ("schema", "模式", "对数据结构的约束描述"),
    ("validation", "校验", "检查输入是否符合约定"),
    ("sanitization", "净化", "清洗掉危险输入内容"),
    ("escaping", "转义", "把特殊字符变成安全形式"),
    ("injection", "注入", "把恶意输入拼进解释器导致越权执行"),
    ("xss", "跨站脚本", "在页面里注入脚本窃取信息"),
    ("csrf", "跨站请求伪造", "借用户身份伪造请求"),
    ("sql injection", "SQL 注入", "把 SQL 片段拼进查询改变语义"),
    ("garbage collection", "垃圾回收", "自动回收不再使用的内存"),
    ("memory leak", "内存泄漏", "不再使用的内存迟迟不释放"),
    ("stack overflow", "栈溢出", "调用层级过深或局部变量过大"),
    ("heap", "堆", "动态分配的内存区域"),
    ("stack", "栈", "保存调用帧的后进先出内存区"),
    ("pointer", "指针", "保存内存地址的变量"),
    ("reference", "引用", "指向另一个对象的别名"),
    ("deep copy", "深拷贝", "递归复制出完全独立的对象"),
    ("shallow copy", "浅拷贝", "只复制顶层，内部仍共享"),
    ("mutable", "可变", "创建后内容还能改"),
    ("immutable", "不可变", "创建后内容不能改"),
    ("scope", "作用域", "名字可见的代码范围"),
    ("shadowing", "遮蔽", "内层同名变量盖住外层变量"),
    ("currying", "柯里化", "把多参数函数变成单参数函数链"),
    ("pure function", "纯函数", "相同输入必得相同输出且无副作用"),
    ("side effect", "副作用", "函数对外部状态造成的改动"),
    ("throughput", "吞吐量", "单位时间处理的请求数"),
    ("latency", "延迟", "一次请求从发出到完成的时间"),
    ("profiling", "性能剖析", "测量各部分耗时定位瓶颈"),
    ("benchmark", "基准测试", "可重复的性能对比测试"),
    ("pagination", "分页", "把结果按页返回"),
    ("sharding", "分片", "把数据拆到多个节点"),
    ("replication", "复制", "把数据同步到多个副本"),
    ("consistency", "一致性", "多副本之间数据是否相同"),
    ("eventual consistency", "最终一致性", "暂时不一致但最终会收敛"),
    ("transaction", "事务", "要么全成功要么全回滚的一组操作"),
    ("rollback", "回滚", "撤销未完成的改动"),
    ("migration", "迁移", "演进数据库结构或数据位置"),
    ("orm", "对象关系映射", "用对象操作代替手写 SQL"),
    ("daemon", "守护进程", "后台长期运行的进程"),
    ("container", "容器", "轻量级隔离的运行环境"),
    ("ci", "持续集成", "每次提交都自动构建和测试"),
    ("lint", "代码检查", "静态扫描风格和低级错误"),
    ("type hint", "类型提示", "给参数和返回值标注类型"),
    ("duck typing", "鸭子类型", "只看行为不看类型"),
    ("reflection", "反射", "运行时检查和调用类型信息"),
    ("annotation", "注解", "附加在代码上的元数据"),
]

# ---------- 语言检测 ----------

SHEBANG_PY_RE = re.compile(r"^#!.*\bpython\b")
SHEBANG_NODE_RE = re.compile(r"^#!.*\bnode\b")
GO_HINTS = (
    re.compile(r"^\s*package\s+\w+", re.M),
    re.compile(r"\bfunc\s+\w+\s*\("),
    re.compile(r":="),
    re.compile(r"\bfmt\.\w+\("),
)
JAVA_HINTS = (
    re.compile(r"^\s*(public\s+|final\s+|abstract\s+)*class\s+\w+", re.M),
    re.compile(r"\bSystem\.out\.print"),
    re.compile(r"^\s*import\s+[\w.]+(\.\*)?\s*;", re.M),
    re.compile(r"\bpublic\s+static\s+void\s+main\b"),
)
JS_HINTS = (
    re.compile(r"\brequire\s*\("),
    re.compile(r"^\s*import\s+.+?from\s+['\"]", re.M),
    re.compile(r"=>"),
    re.compile(r"\bfunction\s*\*?\s*\w*\s*\("),
    re.compile(r"\bexport\s+(default|const|function|class)\b"),
)
PY_HINTS = (
    re.compile(r"^\s*def\s+\w+\s*\(", re.M),
    re.compile(r"^\s*class\s+\w+\s*[(:]?", re.M),
    re.compile(r"^\s*import\s+\w+", re.M),
    re.compile(r"\bif\s+__name__\s*=="),
    re.compile(r"^\s*from\s+\w+[\w.]*\s+import\s+", re.M),
)


def detect_language(path, text):
    """扩展名 + 内容启发式（shebang、import 语法、花括号密度）判断语言"""
    ext = Path(path).suffix.lower()
    declared = EXT_LANG.get(ext)

    scores = {"python": 0, "javascript": 0, "go": 0, "java": 0}
    if declared:
        scores[declared] += 2
    first_line = text.splitlines()[0] if text.strip() else ""
    if SHEBANG_PY_RE.match(first_line):
        scores["python"] += 5
    if SHEBANG_NODE_RE.match(first_line):
        scores["javascript"] += 5
    for regex in PY_HINTS:
        if regex.search(text):
            scores["python"] += 1
    for regex in JS_HINTS:
        if regex.search(text):
            scores["javascript"] += 1
    for regex in GO_HINTS:
        if regex.search(text):
            scores["go"] += 1
    for regex in JAVA_HINTS:
        if regex.search(text):
            scores["java"] += 1

    line_count = max(len(text.splitlines()), 1)
    brace_density = (text.count("{") + text.count("}")) / float(line_count)
    if brace_density > 0.15:
        scores["javascript"] += 1
        scores["java"] += 1
        scores["go"] += 1
    if ":" in text and re.search(r"\bfunc\s+\w+\s*\([^)]*\)\s*[\w\[\]*]*\s*\{", text):
        scores["go"] += 1

    best = max(scores, key=lambda key: scores[key])
    if scores[best] <= 0:
        return "unknown"
    return best


# ---------- 结构提取：Python（ast） ----------

def expr_str(node):
    """把 AST 表达式还原成源码字符串（3.9 有 ast.unparse，失败再退化）"""
    if node is None:
        return ""
    try:
        return ast.unparse(node)
    except Exception:
        return getattr(node, "id", "") or getattr(node, "attr", "") or ""


def format_arguments(args):
    """手写参数串：位置参数、默认值、*args、关键字参数、**kwargs"""
    positional = list(getattr(args, "posonlyargs", [])) + list(args.args)
    defaults = list(args.defaults)
    pad = len(positional) - len(defaults)
    parts = []
    for index, arg in enumerate(positional):
        piece = arg.arg
        if getattr(arg, "annotation", None) is not None:
            piece += ": " + expr_str(arg.annotation)
        if index >= pad:
            piece += "=" + expr_str(defaults[index - pad])
        parts.append(piece)
    if args.vararg is not None:
        piece = "*" + args.vararg.arg
        if getattr(args.vararg, "annotation", None) is not None:
            piece += ": " + expr_str(args.vararg.annotation)
        parts.append(piece)
    elif args.kwonlyargs:
        parts.append("*")
    for arg, default in zip(args.kwonlyargs, args.kw_defaults):
        piece = arg.arg
        if getattr(arg, "annotation", None) is not None:
            piece += ": " + expr_str(arg.annotation)
        if default is not None:
            piece += "=" + expr_str(default)
        parts.append(piece)
    if args.kwarg is not None:
        piece = "**" + args.kwarg.arg
        if getattr(args.kwarg, "annotation", None) is not None:
            piece += ": " + expr_str(args.kwarg.annotation)
        parts.append(piece)
    return ", ".join(parts)


def collect_calls(node):
    """收集函数体里调用到的名字（依赖调用）"""
    names = []
    for child in ast.walk(node):
        if isinstance(child, ast.Call):
            name = expr_str(child.func)
            if name:
                base = name.split(".")[-1].split("(")[0]
                if base and base not in KEYWORD_CALLS and base not in names:
                    names.append(base)
    return names


def py_function_entry(node, parent="", text_lines=None):
    """把一个 FunctionDef/AsyncFunctionDef 变成结构条目（含嵌套函数）"""
    is_async = isinstance(node, ast.AsyncFunctionDef)
    prefix = "async def" if is_async else "def"
    returns = expr_str(node.returns)
    signature = "%s %s(%s)" % (prefix, node.name, format_arguments(node.args))
    if returns:
        signature += " -> " + returns
    entry = {
        "name": node.name,
        "qualified": (parent + "." + node.name) if parent else node.name,
        "kind": "函数",
        "signature": signature,
        "params": format_arguments(node.args),
        "returns": returns or "（未标注）",
        "decorators": [expr_str(dec) for dec in node.decorator_list],
        "docstring": ast.get_docstring(node) or "",
        "start": getattr(node, "lineno", 1),
        "end": getattr(node, "end_lineno", getattr(node, "lineno", 1)),
        "calls": collect_calls(node),
        "nested": [],
        "is_async": is_async,
    }
    for child in node.body:
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
            entry["nested"].append(py_function_entry(child, entry["qualified"], text_lines))
    return entry


def py_class_entry(node):
    """把一个 ClassDef 变成结构条目（含方法与嵌套类）"""
    bases = [expr_str(base) for base in node.bases]
    entry = {
        "name": node.name,
        "kind": "类",
        "bases": bases,
        "decorators": [expr_str(dec) for dec in node.decorator_list],
        "docstring": ast.get_docstring(node) or "",
        "start": getattr(node, "lineno", 1),
        "end": getattr(node, "end_lineno", getattr(node, "lineno", 1)),
        "methods": [],
        "nested_classes": [],
    }
    for child in node.body:
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
            entry["methods"].append(py_function_entry(child, node.name))
        elif isinstance(child, ast.ClassDef):
            entry["nested_classes"].append(py_class_entry(child))
    return entry


def extract_python_structure(text):
    """用 ast 提取模块 docstring、导入、函数/类、全局变量、入口保护"""
    tree = ast.parse(text)
    info = {
        "module_docstring": ast.get_docstring(tree) or "",
        "imports": [],
        "functions": [],
        "classes": [],
        "globals": [],
        "has_main_guard": False,
    }
    for node in tree.body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                info["imports"].append({
                    "text": "import %s" % alias.name + (" as %s" % alias.asname if alias.asname else ""),
                    "module": alias.name,
                    "line": getattr(node, "lineno", 1),
                })
        elif isinstance(node, ast.ImportFrom):
            names = ", ".join(alias.name for alias in node.names)
            module = node.module or ""
            info["imports"].append({
                "text": "from %s import %s" % (module, names),
                "module": module,
                "line": getattr(node, "lineno", 1),
            })
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            info["functions"].append(py_function_entry(node))
        elif isinstance(node, ast.ClassDef):
            info["classes"].append(py_class_entry(node))
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                name = expr_str(target)
                if name:
                    info["globals"].append({
                        "name": name,
                        "value": expr_str(node.value),
                        "line": getattr(node, "lineno", 1),
                    })
        elif isinstance(node, ast.AnnAssign):
            name = expr_str(node.target)
            if name:
                info["globals"].append({
                    "name": name,
                    "value": expr_str(node.value),
                    "line": getattr(node, "lineno", 1),
                })
        elif isinstance(node, ast.If):
            test = expr_str(node.test)
            if "__name__" in test and "__main__" in test:
                info["has_main_guard"] = True
    return info


# ---------- 结构提取：其它语言（正则） ----------

JS_IMPORT_RE = re.compile(
    r"^\s*import\s+(?:[\w*{},\s]+\s+from\s+)?['\"]([^'\"]+)['\"]|^\s*(?:const|let|var)\s+[\w{},\s]*=\s*require\s*\(\s*['\"]([^'\"]+)['\"]",
    re.M,
)
GO_IMPORT_RE = re.compile(r"^\s*(?:import\s+)?[\w.]*\s*\"([^\"]+)\"|^\s*import\s+(?:\w+\s+)?\"([^\"]+)\"", re.M)
JAVA_IMPORT_RE = re.compile(r"^\s*import\s+(static\s+)?([\w.]+(?:\.\*)?)\s*;", re.M)
JS_FUNC_RE = re.compile(
    r"^\s*(export\s+)?(default\s+)?(async\s+)?function\s*\*?\s*([A-Za-z_$][\w$]*)\s*\(([^)]*)\)",
    re.M,
)
JS_ARROW_RE = re.compile(
    r"^\s*(export\s+)?(const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(async\s+)?\(([^)]*)\)\s*=>",
    re.M,
)
JS_CLASS_RE = re.compile(r"^\s*(export\s+)?(abstract\s+)?class\s+([A-Za-z_$][\w$]*)", re.M)
GO_FUNC_RE = re.compile(r"^func\s+(?:\((\w+)\s+\*?(\w+)\)\s*)?([A-Za-z_]\w*)\s*\(([^)]*)\)\s*([^{]*)", re.M)
GO_TYPE_RE = re.compile(r"^type\s+([A-Za-z_]\w*)\s+(struct|interface|\w+)", re.M)
JAVA_CLASS_RE = re.compile(
    r"^\s*(?:public\s+|private\s+|protected\s+|static\s+|final\s+|abstract\s+)*(class|interface|enum|record)\s+(\w+)",
    re.M,
)
JAVA_METHOD_RE = re.compile(
    r"^\s*(?:public\s+|private\s+|protected\s+|static\s+|final\s+|synchronized\s+|native\s+|abstract\s+)*"
    r"(?:[\w<>\[\],.\?\s]+?)\s+(\w+)\s*\(([^)]*)\)\s*(?:throws\s+[\w,.\s]+)?\s*\{",
    re.M,
)
GENERIC_CALL_RE = re.compile(r"\b([A-Za-z_][\w.]*)\s*\(")


def extract_block(lines, start_idx):
    """从起始行按花括号配平截取函数体，返回 (块行列表, 结束行号)"""
    depth = 0
    opened = False
    block = []
    for index in range(start_idx, len(lines)):
        line = lines[index]
        block.append(line)
        for char in line:
            if char == "{":
                depth += 1
                opened = True
            elif char == "}":
                depth -= 1
        if opened and depth <= 0:
            return block, index + 1
    return block, len(lines)


def generic_entry(name, params, body_lines, start, end, lang, extra=""):
    """通用函数条目：参数、返回（能看出来就写）、依赖调用"""
    calls = []
    for match in FUNC_CALL_RE.finditer("\n".join(body_lines)):
        base = match.group(1).split(".")[-1]
        if base not in KEYWORD_CALLS and base != name and base not in calls:
            calls.append(base)
    returns = "（未标注）"
    if lang == "go" and extra.strip():
        returns = extra.strip()
    elif lang in ("javascript", "java"):
        ret_match = re.search(r"\breturn\s+([^\n;]+);?", "\n".join(body_lines))
        if ret_match:
            returns = "（推断）" + ret_match.group(1).strip()[:40]
    return {
        "name": name,
        "qualified": name,
        "kind": "函数",
        "signature": "%s(%s)" % (name, params.strip()),
        "params": params.strip() or "（无参数）",
        "returns": returns,
        "decorators": [],
        "docstring": "",
        "start": start,
        "end": end,
        "calls": calls[:20],
        "nested": [],
        "is_async": False,
    }


def extract_generic_structure(text, lang):
    """JS / Go / Java：正则提取导入、类、函数"""
    lines = text.splitlines()
    info = {
        "module_docstring": "",
        "imports": [],
        "functions": [],
        "classes": [],
        "globals": [],
        "has_main_guard": False,
    }

    if lang == "go":
        for match in GO_IMPORT_RE.finditer(text):
            module = match.group(1) or match.group(2)
            if module:
                info["imports"].append({
                    "text": 'import "%s"' % module,
                    "module": module,
                    "line": text[:match.start()].count("\n") + 1,
                })
    elif lang == "java":
        for match in JAVA_IMPORT_RE.finditer(text):
            info["imports"].append({
                "text": "import %s%s;" % ("static " if match.group(1) else "", match.group(2)),
                "module": match.group(2),
                "line": text[:match.start()].count("\n") + 1,
            })
    else:
        for match in JS_IMPORT_RE.finditer(text):
            module = match.group(1) or match.group(2)
            if module:
                info["imports"].append({
                    "text": match.group(0).strip(),
                    "module": module,
                    "line": text[:match.start()].count("\n") + 1,
                })

    seen_functions = set()

    def add_function(name, params, start_idx, extra=""):
        if name in seen_functions or not name:
            return
        block, end = extract_block(lines, start_idx)
        if name not in KEYWORD_CALLS:
            seen_functions.add(name)
        info["functions"].append(
            generic_entry(name, params, block, start_idx + 1, end, lang, extra)
        )

    if lang == "go":
        for match in GO_FUNC_RE.finditer(text):
            start_idx = text[:match.start()].count("\n")
            add_function(match.group(3), match.group(4), start_idx, match.group(5) or "")
        for match in GO_TYPE_RE.finditer(text):
            info["classes"].append({
                "name": match.group(1),
                "kind": match.group(2) if match.group(2) in ("struct", "interface") else "类型",
                "bases": [],
                "decorators": [],
                "docstring": "",
                "start": text[:match.start()].count("\n") + 1,
                "end": text[:match.start()].count("\n") + 1,
                "methods": [],
                "nested_classes": [],
            })
    elif lang == "java":
        for match in JAVA_CLASS_RE.finditer(text):
            info["classes"].append({
                "name": match.group(2),
                "kind": match.group(1),
                "bases": [],
                "decorators": [],
                "docstring": "",
                "start": text[:match.start()].count("\n") + 1,
                "end": text[:match.start()].count("\n") + 1,
                "methods": [],
                "nested_classes": [],
            })
        for match in JAVA_METHOD_RE.finditer(text):
            name = match.group(1)
            if name in ("if", "for", "while", "switch", "catch", "return", "new"):
                continue
            start_idx = text[:match.start()].count("\n")
            add_function(name, match.group(2), start_idx)
    else:
        for match in JS_FUNC_RE.finditer(text):
            start_idx = text[:match.start()].count("\n")
            add_function(match.group(4), match.group(5), start_idx)
        for match in JS_ARROW_RE.finditer(text):
            start_idx = text[:match.start()].count("\n")
            add_function(match.group(3), match.group(5), start_idx)
        for match in JS_CLASS_RE.finditer(text):
            info["classes"].append({
                "name": match.group(3),
                "kind": "类",
                "bases": [],
                "decorators": [],
                "docstring": "",
                "start": text[:match.start()].count("\n") + 1,
                "end": text[:match.start()].count("\n") + 1,
                "methods": [],
                "nested_classes": [],
            })

    info["has_main_guard"] = bool(
        re.search(r"if\s*\(?\s*require\.main|public\s+static\s+void\s+main|func\s+main\s*\(\s*\)", text)
    )
    return info


def extract_structure(text, lang):
    """按语言分派结构提取；Python 语法错误时退回正则提取，不崩溃"""
    if lang == "python":
        try:
            return extract_python_structure(text), ""
        except SyntaxError as exc:
            info = extract_generic_structure(text, "javascript")
            return info, "Python 语法解析失败（%s），已退回正则提取，结构可能不完整。" % exc
    return extract_generic_structure(text, lang), ""


# ---------- 复杂度启发式 ----------

def estimate_complexity(body_text):
    """圈复杂度近似：1 + if/for/while/case/and/or/三元 计数"""
    score = 1
    score += len(COMPLEX_KEYWORD_RE.findall(body_text))
    score += len(COMPLEX_OP_RE.findall(body_text))
    return score


def nesting_depth(lines, brace_based):
    """嵌套深度：缩进栈启发式（Python）或花括号深度启发式（其它语言）"""
    if brace_based:
        depth = 0
        max_depth = 0
        for line in lines:
            depth += line.count("{") - line.count("}")
            max_depth = max(max_depth, depth)
        return max(0, max_depth)
    stack = []
    max_depth = 0
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        indent = len(line) - len(line.lstrip(" \t"))
        while stack and indent < stack[-1]:
            stack.pop()
        if not stack or indent > stack[-1]:
            stack.append(indent)
        max_depth = max(max_depth, len(stack))
    return max(0, max_depth)


def function_metrics(entry, source_lines, lang):
    """给函数条目补上复杂度、长度、嵌套深度"""
    start = max(entry["start"] - 1, 0)
    end = min(entry["end"], len(source_lines))
    body_lines = source_lines[start:end]
    body_text = "\n".join(body_lines)
    entry["complexity"] = estimate_complexity(body_text)
    entry["length"] = max(end - start, 1)
    entry["param_count"] = 0 if entry["params"] in ("（无参数）", "") else len(
        [p for p in entry["params"].split(",") if p.strip()]
    )
    entry["depth"] = nesting_depth(body_lines, brace_based=(lang != "python"))
    return entry


def collect_entries(info):
    """拍平函数条目（含嵌套函数、类方法）"""
    entries = []
    for function in info.get("functions", []):
        entries.append(function)
        entries.extend(function.get("nested", []))
    for cls in info.get("classes", []):
        entries.extend(cls.get("methods", []))
        for nested in cls.get("nested_classes", []):
            entries.extend(nested.get("methods", []))
    return entries


def top_complex_functions(entries, limit=TOP_COMPLEX):
    """按复杂度降序取前 N 个（长度作次序参考）"""
    ordered = sorted(entries, key=lambda e: (-e["complexity"], -e["length"], e["name"]))
    return ordered[:limit]


# ---------- 术语扫描 ----------

def find_terms(text):
    """扫描代码里出现的内置术语，返回 [(英文, 中文, 解释, 次数)]"""
    lowered = text.lower()
    hits = []
    for en, zh, hint in GLOSSARY:
        count = 0
        if re.search(r"[A-Za-z]", en):
            count += len(re.findall(r"(?<![a-z0-9_])%s(?![a-z0-9_])" % re.escape(en.lower()), lowered))
        if zh in text:
            count += text.count(zh)
        if count > 0:
            hits.append((en, zh, hint, count))
    hits.sort(key=lambda item: (-item[3], item[0]))
    return hits


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


EXPLAIN_SYSTEM_PROMPT = """你是一名耐心的编程讲师，正在给一位水平为「{level}」的读者讲解一段代码。
请只依据给出的代码和结构摘要讲解，不要编造代码里没有的行为。

输出要求：
1. 只输出一个 JSON 对象，不要输出解释文字，不要使用 Markdown 代码围栏。
2. JSON 结构：
{{"overview": "总体作用的中文说明",
  "sections": [{{"part": "讲解哪一部分（如某函数名）",
                 "explain": "这一部分怎么工作的中文讲解",
                 "pitfalls": ["容易踩的坑"],
                 "suggestions": ["改进建议"]}}],
  "quiz": [{{"q": "自测题", "a": "参考答案"}}]}}
3. sections 按代码阅读顺序组织，至少覆盖主要函数/类；quiz 恰好 3 道，由浅入深。
4. 全部用简体中文，讲解要具体到代码行为，不要空话。
"""

EXPLAIN_USER_TEMPLATE = """语言：{lang}
讲解深度：{level}
文件：{name}（{lines} 行）

【结构摘要】
{structure}

【代码】
```{code_lang}
{code}
```
"""


def parse_explain_json(text):
    """尽力把模型回复解析成讲解 JSON；解析不出来返回 None（原文进报告附录）"""
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
    overview = data.get("overview")
    if not isinstance(overview, str) or not overview.strip():
        return None
    sections = []
    raw_sections = data.get("sections")
    if isinstance(raw_sections, list):
        for item in raw_sections:
            if not isinstance(item, dict):
                continue
            part = item.get("part")
            explain = item.get("explain")
            if not isinstance(part, str) or not isinstance(explain, str):
                continue
            sections.append({
                "part": part.strip(),
                "explain": explain.strip(),
                "pitfalls": [str(x) for x in item.get("pitfalls", []) if str(x).strip()],
                "suggestions": [str(x) for x in item.get("suggestions", []) if str(x).strip()],
            })
    quiz = []
    raw_quiz = data.get("quiz")
    if isinstance(raw_quiz, list):
        for item in raw_quiz:
            if not isinstance(item, dict):
                continue
            question = item.get("q")
            answer = item.get("a")
            if isinstance(question, str) and isinstance(answer, str) and question.strip():
                quiz.append({"q": question.strip(), "a": answer.strip()})
    return {"overview": overview.strip(), "sections": sections, "quiz": quiz}


def summarize_structure(info, entries, lang):
    """给模型看的结构摘要（也是离线骨架讲解的素材）"""
    lines = []
    if info.get("module_docstring"):
        lines.append("模块说明：%s" % info["module_docstring"].splitlines()[0])
    if info.get("imports"):
        lines.append("导入：%s" % "、".join(item["text"] for item in info["imports"][:20]))
    if info.get("globals"):
        lines.append("全局变量：%s" % "、".join(
            "%s=%s" % (item["name"], item["value"][:20]) for item in info["globals"][:10]))
    for entry in entries:
        lines.append("- %s：%s（第 %d-%d 行，复杂度 %d，参数 %d 个）" % (
            entry["kind"], entry["signature"], entry["start"], entry["end"],
            entry["complexity"], entry["param_count"]))
    return "\n".join(lines)


def explain_with_model(name, text, info, entries, lang, level, model):
    """模型讲解；返回 (parsed, raw_text)，解析失败时 parsed 为 None"""
    client = anthropic.Anthropic()
    system = EXPLAIN_SYSTEM_PROMPT.format(level=level)
    code = text[:60_000]
    user = EXPLAIN_USER_TEMPLATE.format(
        lang=LANG_LABELS.get(lang, lang),
        level=level,
        name=name,
        lines=len(text.splitlines()),
        structure=summarize_structure(info, entries, lang),
        code_lang=lang,
        code=code,
    )
    response = call_model(
        client, model, system,
        [{"role": "user", "content": user}],
        max_tokens=6000,
    )
    raw = response_text(response)
    return parse_explain_json(raw), raw


# ---------- 骨架讲解（离线可用） ----------

def render_skeleton_table(name, entries, lang):
    """按函数逐个生成"签名/参数/返回/复杂度/依赖调用"结构化说明表格"""
    lines = []
    lines.append("### %s 的函数骨架" % name)
    lines.append("")
    if not entries:
        lines.append("（没有识别到函数级结构，可看上方结构表与代码原文）")
        lines.append("")
        return "\n".join(lines)
    lines.append("| 函数 | 签名 | 参数 | 返回 | 复杂度 | 依赖调用 |")
    lines.append("| --- | --- | --- | --- | --- | --- |")
    for entry in entries:
        calls = "、".join(entry["calls"][:6]) or "（无）"
        lines.append("| `%s` | `%s` | %s | %s | %d | %s |" % (
            entry["qualified"],
            entry["signature"][:80],
            (entry["params"] or "（无参数）")[:60],
            (entry["returns"] or "（未标注）")[:30],
            entry["complexity"],
            calls[:60],
        ))
    lines.append("")
    lines.append("逐个说明：")
    lines.append("")
    for entry in entries:
        lines.append("#### `%s`（第 %d-%d 行）" % (entry["qualified"], entry["start"], entry["end"]))
        lines.append("")
        lines.append("- **签名**：`%s`" % entry["signature"])
        lines.append("- **参数**：%s（%d 个）" % (entry["params"] or "无", entry["param_count"]))
        lines.append("- **返回**：%s" % (entry["returns"] or "（未标注）"))
        lines.append("- **复杂度**：%d（圈复杂度近似），函数长度 %d 行，嵌套深度 %d" % (
            entry["complexity"], entry["length"], entry["depth"]))
        lines.append("- **依赖调用**：%s" % ("、".join(entry["calls"][:10]) or "（无）"))
        if entry.get("decorators"):
            lines.append("- **装饰器**：%s" % "、".join(entry["decorators"]))
        if entry.get("docstring"):
            lines.append("- **自带说明**：%s" % entry["docstring"].splitlines()[0])
        if entry.get("nested"):
            lines.append("- **嵌套函数**：%s" % "、".join(
                "%s（第 %d 行）" % (n["name"], n["start"]) for n in entry["nested"]))
        lines.append("")
    return "\n".join(lines)


# ---------- 报告 ----------

def build_report(name, lang, text, info, entries, term_hits, parsed, raw_text, meta, note=""):
    """生成中文 Markdown 讲解：概览、结构表、逐段讲解、复杂度提示、术语词典、自测题"""
    lines = ["# 代码讲解：%s" % name, ""]
    lines.append("- 文件：`%s`" % name)
    lines.append("- 语言：%s（检测方式：%s）" % (LANG_LABELS.get(lang, lang), meta["detect"]))
    lines.append("- 规模：%d 行，函数 %d 个，类 %d 个，导入 %d 处" % (
        len(text.splitlines()), len(entries), len(info.get("classes", [])),
        len(info.get("imports", []))))
    lines.append("- 讲解深度：%s ｜ 生成时间：%s" % (meta["level"], meta["generated_at"]))
    if note:
        lines.append("- 提示：%s" % note)
    lines.append("")

    lines.append("## 一、概览")
    lines.append("")
    if parsed:
        lines.append(parsed["overview"])
    else:
        docstring = info.get("module_docstring") or ""
        if docstring:
            lines.append("**模块自带说明**：%s" % docstring.strip())
            lines.append("")
        lines.append("**确定性分析**：这个文件包含 %d 个函数、%d 个类；主要入口见下方结构表。" % (
            len(entries), len(info.get("classes", []))))
        if meta["offline"]:
            lines.append("")
            lines.append("（离线模式：不调用模型，以下为结构化骨架讲解；加模型可得完整讲解与自测题）")
    lines.append("")

    lines.append("## 二、结构表")
    lines.append("")
    lines.append("| 项目 | 内容 |")
    lines.append("| --- | --- |")
    if info.get("module_docstring"):
        lines.append("| 模块说明 | %s |" % info["module_docstring"].splitlines()[0].replace("|", "\\|"))
    lines.append("| 入口保护 | %s |" % ("有 `if __name__ == '__main__'` 入口" if info.get("has_main_guard") else "无"))
    if info.get("imports"):
        lines.append("| 导入 | %s |" % "、".join(
            "`%s`" % item["text"].replace("|", "\\|") for item in info["imports"][:12]))
    if info.get("globals"):
        lines.append("| 全局变量 | %s |" % "、".join(
            "`%s`" % item["name"] for item in info["globals"][:12]))
    for cls in info.get("classes", []):
        bases = "（%s）" % "、".join(cls["bases"]) if cls.get("bases") else ""
        lines.append("| %s | `%s`%s，第 %d-%d 行 |" % (
            cls["kind"], cls["name"], bases, cls["start"], cls["end"]))
    lines.append("")

    if info.get("classes"):
        lines.append("### 类与方法")
        lines.append("")
        lines.append("| 类 | 方法 | 装饰器 | 说明 |")
        lines.append("| --- | --- | --- | --- |")
        for cls in info.get("classes", []):
            methods = "、".join("`%s`" % m["name"] for m in cls.get("methods", [])) or "（无方法）"
            decorators = "、".join(cls.get("decorators", [])) or "—"
            lines.append("| `%s` | %s | %s | %s |" % (
                cls["name"], methods, decorators,
                (cls.get("docstring") or "—").splitlines()[0][:40].replace("|", "\\|")))
        lines.append("")

    lines.append("## 三、逐段讲解")
    lines.append("")
    if parsed and parsed["sections"]:
        for index, section in enumerate(parsed["sections"], 1):
            lines.append("### %d. %s" % (index, section["part"]))
            lines.append("")
            lines.append(section["explain"])
            lines.append("")
            if section["pitfalls"]:
                lines.append("**坑点**：")
                for item in section["pitfalls"]:
                    lines.append("- %s" % item)
                lines.append("")
            if section["suggestions"]:
                lines.append("**改进建议**：")
                for item in section["suggestions"]:
                    lines.append("- %s" % item)
                lines.append("")
    else:
        if parsed is None and not meta["offline"] and raw_text:
            lines.append("模型回复没有解析成约定的 JSON，这里给出确定性的骨架讲解（模型原文见文末附录）。")
            lines.append("")
        lines.append(render_skeleton_table(name, entries, lang))

    lines.append("## 四、复杂度提示")
    lines.append("")
    tops = top_complex_functions(entries)
    if tops:
        lines.append("| 排名 | 函数 | 圈复杂度近似 | 函数长度 | 参数个数 | 嵌套深度 | 位置 |")
        lines.append("| --- | --- | --- | --- | --- | --- | --- |")
        for index, entry in enumerate(tops, 1):
            lines.append("| %d | `%s` | %d | %d 行 | %d | %d | 第 %d-%d 行 |" % (
                index, entry["qualified"], entry["complexity"], entry["length"],
                entry["param_count"], entry["depth"], entry["start"], entry["end"]))
        lines.append("")
        lines.append("复杂度启发式：1 + if/for/while/case/and/or/三元出现次数。排名靠前的函数往往最难读，"
                     "优先把它们拆小（每个函数只做一件事）、把深嵌套改成提前返回。")
    else:
        lines.append("（没有识别到函数，跳过复杂度排序）")
    lines.append("")

    lines.append("## 五、术语小词典")
    lines.append("")
    if term_hits:
        lines.append("| 术语 | 中文 | 出现次数 | 解释 |")
        lines.append("| --- | --- | --- | --- |")
        for en, zh, hint, count in term_hits[:25]:
            lines.append("| %s | %s | %d | %s |" % (en, zh, count, hint))
    else:
        lines.append("（代码里没有命中内置术语表，换个大点的文件试试）")
    lines.append("")

    lines.append("## 六、自测题")
    lines.append("")
    if parsed and parsed["quiz"]:
        for index, item in enumerate(parsed["quiz"], 1):
            lines.append("**%d. %s**" % (index, item["q"]))
            lines.append("")
            lines.append("> 参考答案：%s" % item["a"])
            lines.append("")
    else:
        lines.append("（离线模式不生成自测题；去掉 --offline 即可让模型按代码出 3 道题）")
        lines.append("")

    if raw_text and not parsed:
        lines.append("## 附录：模型原始回复（未能解析为 JSON）")
        lines.append("")
        lines.append("```text")
        lines.append(raw_text or "（模型没有返回任何内容）")
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


SAMPLE_PY = '''"""订单处理示例模块"""

import os
import json

MAX_RETRY = 3
DEBUG = False


def retry_key(name):
    return name.lower()


@retry_key
def load_config(path):
    """读取配置"""
    with open(path) as handle:
        return json.load(handle)


class OrderService:
    """订单服务"""

    def __init__(self, config):
        self.config = config

    def submit(self, order, retry=0):
        def validate(item):
            if not item:
                return False
            return True

        if retry > MAX_RETRY or not validate(order):
            return None
        for key in order:
            if key == "id" or key == "sku":
                continue
        return order


if __name__ == "__main__":
    print("入口")
'''


def run_self_test():
    """离线自检：造样本、跑全部确定性逻辑、assert 校验，不联网、不需要 API key"""
    assert len(GLOSSARY) >= GLOSSARY_MIN, "内置术语表不足 %d 条" % GLOSSARY_MIN

    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        py_path = root / "sample.py"
        py_path.write_text(SAMPLE_PY, encoding="utf-8")
        (root / "legacy.txt").write_text(SAMPLE_PY, encoding="utf-8")
        (root / "app.js").write_text(
            "const os = require('os');\n"
            "export function add(a, b) {\n"
            "  if (a > 0 && b > 0) {\n"
            "    return a + b;\n"
            "  }\n"
            "  return 0;\n"
            "}\n"
            "const mul = (a, b) => a * b;\n"
            "export class Store {}\n",
            encoding="utf-8",
        )
        (root / "main.go").write_text(
            "package main\n\n"
            "import \"fmt\"\n\n"
            "func main() {\n"
            "\tfmt.Println(\"hi\")\n"
            "}\n\n"
            "func Add(a int, b int) int {\n"
            "\tif a > 0 || b > 0 {\n"
            "\t\treturn a + b\n"
            "\t}\n"
            "\treturn 0\n"
            "}\n",
            encoding="utf-8",
        )
        (root / "Demo.java").write_text(
            "import java.util.List;\n\n"
            "public class Demo {\n"
            "    public static void main(String[] args) {\n"
            "        System.out.println(\"hi\");\n"
            "    }\n"
            "    private int add(int a, int b) {\n"
            "        if (a > 0 && b > 0) {\n"
            "            return a + b;\n"
            "        }\n"
            "        return 0;\n"
            "    }\n"
            "}\n",
            encoding="utf-8",
        )

        # --- 语言检测：扩展名 + 内容启发式 ---
        py_text = py_path.read_text(encoding="utf-8")
        assert detect_language(str(py_path), py_text) == "python"
        assert detect_language(str(root / "legacy.txt"), py_text) == "python", "应靠内容识别出 Python"
        js_text = (root / "app.js").read_text(encoding="utf-8")
        assert detect_language(str(root / "app.js"), js_text) == "javascript"
        go_text = (root / "main.go").read_text(encoding="utf-8")
        assert detect_language(str(root / "main.go"), go_text) == "go"
        java_text = (root / "Demo.java").read_text(encoding="utf-8")
        assert detect_language(str(root / "Demo.java"), java_text) == "java"
        assert detect_language("x.unknown", "毫无特征的纯文本") == "unknown"

        # --- Python 结构提取：模块 docstring、导入、类/函数/装饰器/嵌套函数/全局变量/入口 ---
        info = extract_python_structure(py_text)
        assert "订单处理示例模块" in info["module_docstring"]
        assert any(item["module"] == "os" for item in info["imports"])
        function_names = [f["name"] for f in info["functions"]]
        assert "load_config" in function_names and "retry_key" in function_names
        load_config = [f for f in info["functions"] if f["name"] == "load_config"][0]
        assert load_config["decorators"] == ["retry_key"], load_config["decorators"]
        assert "读取配置" in load_config["docstring"]
        assert load_config["returns"] == "（未标注）"
        assert "json.load" in load_config["calls"] or "load" in load_config["calls"]
        classes = info["classes"]
        assert classes and classes[0]["name"] == "OrderService"
        assert "订单服务" in classes[0]["docstring"]
        method_names = [m["name"] for m in classes[0]["methods"]]
        assert "submit" in method_names and "__init__" in method_names
        submit = [m for m in classes[0]["methods"] if m["name"] == "submit"][0]
        assert submit["nested"] and submit["nested"][0]["name"] == "validate", "应识别嵌套函数"
        assert submit["start"] < submit["end"]
        global_names = [g["name"] for g in info["globals"]]
        assert "MAX_RETRY" in global_names and "DEBUG" in global_names
        assert info["has_main_guard"] is True

        # --- 其它语言结构提取 ---
        js_info = extract_generic_structure(js_text, "javascript")
        js_funcs = [f["name"] for f in js_info["functions"]]
        assert "add" in js_funcs and "mul" in js_funcs, js_funcs
        assert any("os" in item["module"] for item in js_info["imports"])
        assert js_info["classes"] and js_info["classes"][0]["name"] == "Store"

        go_info = extract_generic_structure(go_text, "go")
        go_funcs = [f["name"] for f in go_info["functions"]]
        assert "Add" in go_funcs and "main" in go_funcs, go_funcs
        assert any("fmt" in item["module"] for item in go_info["imports"])
        add_entry = [f for f in go_info["functions"] if f["name"] == "Add"][0]
        assert add_entry["returns"] == "int", add_entry["returns"]

        java_info = extract_generic_structure(java_text, "java")
        assert java_info["classes"] and java_info["classes"][0]["name"] == "Demo"
        assert java_info["has_main_guard"] is True

        # --- 复杂度启发式 ---
        entries = collect_entries(info)
        for entry in entries:
            function_metrics(entry, py_text.splitlines(), "python")
        assert entries
        simple = [e for e in entries if e["name"] == "retry_key"][0]
        complex_one = [e for e in entries if e["name"] == "submit"][0]
        assert simple["complexity"] == 1, simple["complexity"]
        assert complex_one["complexity"] > simple["complexity"], "分支多的函数复杂度应更高"
        assert complex_one["param_count"] == 3, "submit(self, order, retry=0) 共 3 个参数（含 self）"
        assert complex_one["depth"] >= 3, "嵌套深度应体现 if/for 的嵌套"
        assert complex_one["length"] >= 5
        tops = top_complex_functions(entries)
        assert tops and tops[0]["name"] == "submit", [e["name"] for e in tops]
        assert len(tops) <= TOP_COMPLEX
        assert estimate_complexity("if a and b or c:\n    x = 1 if y else 2") > 1

        # JS 也走同一套启发式
        js_entries = collect_entries(js_info)
        for entry in js_entries:
            function_metrics(entry, js_text.splitlines(), "javascript")
        js_add = [e for e in js_entries if e["name"] == "add"][0]
        assert js_add["complexity"] >= 2 and js_add["param_count"] == 2

        # --- 术语扫描 ---
        glossary_sample = "def handler():\n    # closure 和 decorator 的例子，保证幂等\n    pass\n"
        term_hits = find_terms(glossary_sample)
        hit_names = [item[0] for item in term_hits]
        assert "closure" in hit_names and "decorator" in hit_names and "idempotent" in hit_names
        assert all(item[3] >= 1 for item in term_hits)
        assert find_terms("纯数字 12345 没有术语") == []

        # --- 模型回复 JSON 解析兜底 ---
        good = (
            "```json\n"
            '{"overview": "这是一个订单服务示例。", '
            '"sections": [{"part": "submit", "explain": "提交订单。", '
            '"pitfalls": ["retry 可能死循环"], "suggestions": ["加退避"]}], '
            '"quiz": [{"q": "复杂度怎么算？", "a": "分支数加一"}]}\n'
            "```"
        )
        parsed = parse_explain_json(good)
        assert parsed and parsed["overview"].startswith("这是一个订单")
        assert parsed["sections"][0]["part"] == "submit"
        assert parsed["quiz"][0]["a"] == "分支数加一"
        assert parse_explain_json("完全不是 JSON") is None
        assert parse_explain_json("{broken") is None
        assert parse_explain_json('{"sections": []}') is None, "没有 overview 应返回 None"
        coerced = parse_explain_json(
            '{"overview": "ok", "sections": [{"part": "p", "explain": "e", "pitfalls": ["x"], "suggestions": []}], '
            '"quiz": [{"q": "q"}]}'
        )
        assert coerced and coerced["quiz"] == [] and coerced["sections"][0]["pitfalls"] == ["x"]

        # --- 骨架讲解表格 ---
        skeleton = render_skeleton_table("sample.py", entries, "python")
        for column in ("签名", "参数", "返回", "复杂度", "依赖调用"):
            assert column in skeleton, "骨架表缺少列：%s" % column
        assert "submit" in skeleton and "validate" in skeleton

        # --- 报告结构（离线 + 解析失败兜底）---
        meta = {
            "level": "入门", "offline": True, "detect": "扩展名 + 内容启发式",
            "generated_at": "2026-01-01 00:00:00",
        }
        report = build_report(
            "sample.py", "python", py_text, info, entries, term_hits,
            None, "", meta,
        )
        for heading in ("## 一、概览", "## 二、结构表", "## 三、逐段讲解",
                        "## 四、复杂度提示", "## 五、术语小词典", "## 六、自测题"):
            assert heading in report, "报告缺少：%s" % heading
        assert "OrderService" in report and "submit" in report
        assert "closure" in report

        report_model = build_report(
            "sample.py", "python", py_text, info, entries, term_hits,
            parsed, "", dict(meta, offline=False),
        )
        assert "订单服务示例" in report_model
        assert "**坑点**" in report_model and "参考答案" in report_model

        report_raw = build_report(
            "sample.py", "python", py_text, info, entries, term_hits,
            None, "自由格式讲解", dict(meta, offline=False),
        )
        assert "附录：模型原始回复" in report_raw and "自由格式讲解" in report_raw
        assert "骨架讲解" in report_raw or "函数骨架" in report_raw

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
        description="代码讲解助手：确定性提取结构与复杂度，再让模型生成中文分层讲解与自测题",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            "  python3 agent.py ./src/app.py --output 讲解.md\n"
            "  python3 agent.py ./src/utils.js --lang javascript --level 入门\n"
            "  python3 agent.py ./main.go --offline\n"
            "  python3 agent.py ./Demo.java --model qwen3.8-max\n"
        ),
    )
    parser.add_argument("paths", nargs="*", help="要讲解的代码文件（可给多个）")
    parser.add_argument("--lang", default="auto", choices=LANGS,
                        help="代码语言，默认 auto 自动检测")
    parser.add_argument("--level", default="中级", choices=LEVELS,
                        help="讲解深度：入门 / 中级 / 深入，默认中级")
    parser.add_argument("--output", default=DEFAULT_OUTPUT,
                        help="讲解文档输出路径，默认 explanation.md")
    parser.add_argument("--offline", action="store_true",
                        help="离线模式：只做确定性分析与骨架讲解，不调模型")
    parser.add_argument("--model", default=DEFAULT_MODEL,
                        help="模型名，默认 %s；走第三方兼容网关时用 --model 指定网关认得的模型名" % DEFAULT_MODEL)
    parser.add_argument("--self-test", action="store_true", help="离线自检：不联网、不需要 API key")
    return parser


def require_api_key():
    """需要模型的路径必须先有 key，否则打印中文提示并退出"""
    if not os.environ.get("ANTHROPIC_API_KEY") and not os.environ.get("ANTHROPIC_AUTH_TOKEN"):
        print(
            "提示：没有检测到 ANTHROPIC_API_KEY 或 ANTHROPIC_AUTH_TOKEN，无法调用模型生成讲解。\n"
            "请先设置（二选一）：\n"
            "  macOS / Linux：      export ANTHROPIC_API_KEY=你的key\n"
            "  Windows PowerShell： $env:ANTHROPIC_API_KEY=\"你的key\"\n"
            "也可以加 --offline 只做确定性分析（不需要 key、不联网）。",
            file=sys.stderr,
        )
        sys.exit(1)


def analyze_file(path, lang_arg):
    """读文件 → 语言检测 → 结构提取 → 复杂度 → 术语扫描"""
    raw = Path(path).read_bytes()
    if b"\x00" in raw[:4096]:
        raise ValueError("看起来是二进制文件，无法讲解")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        text = raw.decode("gbk", errors="replace")
    truncated = False
    if len(text) > MAX_FILE_CHARS:
        text = text[:MAX_FILE_CHARS]
        truncated = True
    lang = detect_language(path, text) if lang_arg == "auto" else lang_arg
    if lang == "unknown":
        lang = "javascript"      # 按通用类 C 语法提取
        detected_label = "unknown"
    else:
        detected_label = lang
    info, note = extract_structure(text, lang)
    entries = collect_entries(info)
    source_lines = text.splitlines()
    for entry in entries:
        function_metrics(entry, source_lines, lang)
    term_hits = find_terms(text)
    return {
        "path": str(path),
        "name": Path(path).name,
        "text": text,
        "lang": lang,
        "detected": detected_label,
        "info": info,
        "entries": entries,
        "term_hits": term_hits,
        "note": note + ("（文件过长已截断到 %d 字符）" % MAX_FILE_CHARS if truncated else ""),
    }


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

    if not args.paths:
        parser.error("请至少提供一个要讲解的代码文件")

    if not args.offline:
        require_api_key()

    reports = []
    for path in args.paths:
        file_path = Path(path).expanduser()
        if not file_path.is_file():
            print("错误：文件不存在：%s" % file_path, file=sys.stderr)
            sys.exit(1)
        try:
            analysis = analyze_file(file_path, args.lang)
        except ValueError as exc:
            print("错误：%s：%s" % (file_path, exc), file=sys.stderr)
            sys.exit(1)
        except SyntaxError as exc:
            print("错误：%s 不是合法的 Python 代码：%s" % (file_path, exc), file=sys.stderr)
            sys.exit(1)
        except OSError as exc:
            print("错误：读取失败：%s" % exc, file=sys.stderr)
            sys.exit(1)

        print("[分析] %s：%s，函数 %d 个，类 %d 个，术语命中 %d 个" % (
            analysis["name"], LANG_LABELS.get(analysis["lang"], analysis["lang"]),
            len(analysis["entries"]), len(analysis["info"].get("classes", [])),
            len(analysis["term_hits"]),
        ))

        parsed = None
        raw_text = ""
        if not args.offline:
            print("[讲解] 模型：%s（深度：%s）" % (args.model, args.level))
            try:
                parsed, raw_text = explain_with_model(
                    analysis["name"], analysis["text"], analysis["info"],
                    analysis["entries"], analysis["lang"], args.level, args.model,
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
            if parsed is None:
                print("[提示] 模型回复不是约定的 JSON，已回退到骨架讲解并把原文放进附录。")

        meta = {
            "level": args.level,
            "offline": args.offline,
            "detect": "自动检测（扩展名 + 内容启发式）" if args.lang == "auto" else "命令行指定 --lang",
            "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        reports.append(build_report(
            analysis["name"], analysis["lang"], analysis["text"], analysis["info"],
            analysis["entries"], analysis["term_hits"], parsed, raw_text, meta,
            note=analysis["note"],
        ))

    output_path = Path(args.output).expanduser()
    try:
        if output_path.parent and not output_path.parent.exists():
            output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text("\n\n---\n\n".join(reports), encoding="utf-8")
    except OSError as exc:
        print("错误：讲解文档写入失败：%s" % exc, file=sys.stderr)
        sys.exit(1)

    print("[完成] 讲解文档已写入：%s" % output_path)
    if args.offline:
        print("[完成] 离线模式：只包含确定性结构分析与骨架讲解。")


if __name__ == "__main__":
    main()
