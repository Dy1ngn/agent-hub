#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
单元测试生成助手 —— ast 静态分析 + 测试骨架生成 + 模型补全断言 的两段式测试生成 agent

先完全确定性地用 ast 分析被测模块（函数/方法签名、docstring 的 Args/Returns/Raises、
raise 的异常、yield/async 特性、类与导入、被测函数之间的调用关系），据此规划用例
（典型路径 / 边界值 / 异常路径 / 返回值类型）并生成可直接运行的 pytest / unittest 测试骨架；
再可选让模型补全真实断言与测试数据，合并进骨架（解析失败就退回纯骨架，仍然语法正确可运行）。
除 anthropic SDK 外只用标准库，Python 3.9 语法兼容。

用法：
    python3 agent.py <py 文件或目录> [选项]

示例：
    python3 agent.py ./src --output-dir tests
    python3 agent.py ./src/app.py --framework unittest --coverage 公开函数
    python3 agent.py ./src --run --output 测试报告.md

环境要求：
    1. 模型补全断言需要设置 ANTHROPIC_API_KEY 或 ANTHROPIC_AUTH_TOKEN：
           export ANTHROPIC_API_KEY=你的key
       （Windows PowerShell：$env:ANTHROPIC_API_KEY="你的key"）
       （--offline 与 --self-test 不需要 key）
    2. 安装 SDK：
           pip3 install -r requirements.txt
    3. 走第三方 Anthropic 兼容网关时，用 --model 指定网关侧的模型名，
       例如：python3 agent.py ./src --model qwen3.8-max --offline
"""

import argparse
import ast
import json
import os
import re
import subprocess
import sys
import tempfile
import textwrap
from datetime import datetime
from pathlib import Path

import anthropic

# 默认模型（可用 --model 参数或 TEST_GENERATOR_MODEL 环境变量更换）
DEFAULT_MODEL = os.environ.get("TEST_GENERATOR_MODEL", "claude-opus-5")

DEFAULT_OUTPUT_DIR = "tests"
DEFAULT_REPORT = "test-plan-report.md"
MAX_FILES = 100              # 一次最多处理多少个 py 文件
RUN_TIMEOUT = 120            # --run 跑测试的超时秒数
MAX_CASES_PER_FUNC = 12      # 单个函数最多规划多少条用例

CASE_TYPES = ("典型路径", "边界值", "异常路径", "返回值类型")
FRAMEWORKS = ("pytest", "unittest")
COVERAGE_MODES = ("全部", "公开函数")

# 内置异常名（生成 pytest.raises(...) 时优先用真实名字，认不出的一律降级成 Exception）
BUILTIN_EXCEPTIONS = {
    "Exception", "BaseException", "ValueError", "TypeError", "KeyError", "IndexError",
    "AttributeError", "RuntimeError", "NotImplementedError", "ZeroDivisionError",
    "ArithmeticError", "AssertionError", "ImportError", "ModuleNotFoundError",
    "IOError", "OSError", "FileNotFoundError", "PermissionError", "StopIteration",
    "OverflowError", "RecursionError", "UnicodeError", "UnicodeDecodeError",
    "UnicodeEncodeError", "LookupError", "NameError", "UnboundLocalError",
    "StopAsyncIteration", "TimeoutError", "ConnectionError", "BlockingIOError",
}

# 标注类型 → 占位值 / 边界值（占位值取“正常输入”，边界值取空值/零值）
TYPE_PLACEHOLDERS = (
    ("bool", "True"),
    ("int", "1"),
    ("float", "1.0"),
    ("str", '"测试"'),
    ("bytes", 'b"测试"'),
    ("dict", '{"键": 1}'),
    ("mapping", '{"键": 1}'),
    ("list", "[1, 2]"),
    ("sequence", "[1, 2]"),
    ("set", "{1, 2}"),
    ("tuple", "(1, 2)"),
    ("frozenset", "frozenset({1, 2})"),
)
TYPE_BOUNDARIES = (
    ("bool", "False"),
    ("int", "0"),
    ("float", "0.0"),
    ("str", '""'),
    ("bytes", 'b""'),
    ("dict", "{}"),
    ("mapping", "{}"),
    ("list", "[]"),
    ("sequence", "[]"),
    ("set", "set()"),
    ("tuple", "()"),
    ("frozenset", "frozenset()"),
)


# ---------- ast 分析 ----------

def expr_text(node):
    """把 AST 表达式还原成源码文本（Python 3.9 自带 ast.unparse）"""
    if node is None:
        return ""
    try:
        return ast.unparse(node)
    except Exception:
        return ""


def parse_docstring(text):
    """解析 docstring：摘要 + Google 风格（Args/Returns/Raises/Yields）+ Sphinx 风格（:param:）"""
    result = {"summary": "", "args": {}, "returns": "", "yields": "", "raises": {}}
    if not text:
        return result
    lines = [line.rstrip() for line in text.strip().splitlines()]
    summary_lines = []
    for line in lines:
        if not line.strip():
            break
        summary_lines.append(line.strip())
    result["summary"] = " ".join(summary_lines)

    section = None
    pending_key = None
    for line in lines:
        stripped = line.strip()
        header = re.match(r"^(Args|Arguments|Parameters|Returns|Return|Yields|Raises|Raises:)\s*:\s*$",
                          stripped)
        if header:
            section = header.group(1).lower()
            pending_key = None
            continue
        if not stripped:
            continue
        if not line.startswith((" ", "\t")):
            # 顶格的新段落 → 当前小节结束
            section = None
            pending_key = None
            continue
        if section in ("args", "arguments", "parameters"):
            entry = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)\s*(\([^)]*\))?\s*:\s*(.*)$", stripped)
            if entry:
                result["args"][entry.group(1)] = (entry.group(3) or "").strip()
                pending_key = entry.group(1)
            elif pending_key:
                result["args"][pending_key] = (
                    result["args"][pending_key] + " " + stripped).strip()
        elif section in ("returns", "return"):
            result["returns"] = (result["returns"] + " " + stripped).strip()
        elif section == "yields":
            result["yields"] = (result["yields"] + " " + stripped).strip()
        elif section == "raises":
            entry = re.match(r"^([A-Za-z_][A-Za-z0-9_.]*)\s*:\s*(.*)$", stripped)
            if entry:
                result["raises"][entry.group(1)] = (entry.group(2) or "").strip()
            else:
                result["returns"] = result["returns"]

    # Sphinx 风格：:param name: xxx / :return: xxx / :raises Exc: xxx
    for line in lines:
        param = re.match(r"^:param\s+(?:[\w\[\],. ]+\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*:\s*(.*)$",
                         line.strip())
        if param:
            result["args"][param.group(1)] = param.group(2).strip()
        ret = re.match(r"^:returns?\s*:\s*(.*)$", line.strip())
        if ret:
            result["returns"] = ret.group(1).strip()
        raise_entry = re.match(r"^:raises\s+([A-Za-z_][A-Za-z0-9_.]*)\s*:\s*(.*)$", line.strip())
        if raise_entry:
            result["raises"][raise_entry.group(1)] = raise_entry.group(2).strip()
    return result


def extract_params(node):
    """提取函数参数：名字、类型注解、默认值、是否必填"""
    args = node.args
    positional = list(getattr(args, "posonlyargs", []) or []) + list(args.args)
    defaults = list(args.defaults)
    # 默认值从右往左对齐到位置参数
    default_map = {}
    for offset, arg in enumerate(reversed(positional)):
        if offset < len(defaults):
            default_map[arg.arg] = defaults[len(defaults) - 1 - offset]
    params = []
    for arg in positional:
        params.append({
            "name": arg.arg,
            "annotation": expr_text(arg.annotation),
            "default": expr_text(default_map.get(arg.arg)),
            "kind": "位置参数",
            "required": arg.arg not in default_map,
        })
    if args.vararg:
        params.append({"name": args.vararg.arg, "annotation": expr_text(args.vararg.annotation),
                       "default": "", "kind": "*args", "required": False})
    for arg, default in zip(args.kwonlyargs, args.kw_defaults):
        params.append({
            "name": arg.arg,
            "annotation": expr_text(arg.annotation),
            "default": expr_text(default),
            "kind": "关键字参数",
            "required": default is None,
        })
    if args.kwarg:
        params.append({"name": args.kwarg.arg, "annotation": expr_text(args.kwarg.annotation),
                       "default": "", "kind": "**kwargs", "required": False})
    return params


def extract_raises(node):
    """提取函数体里 raise 的异常类型，以及 except 捕获的异常类型"""
    found = []
    for child in ast.walk(node):
        if isinstance(child, ast.Raise) and child.exc is not None:
            exc = child.exc
            if isinstance(exc, ast.Call):
                exc = exc.func
            if isinstance(exc, ast.Name):
                found.append(exc.id)
            elif isinstance(exc, ast.Attribute):
                found.append(exc.attr)
        elif isinstance(child, ast.ExceptHandler):
            if isinstance(child.type, ast.Name):
                found.append(child.type.id)
            elif isinstance(child.type, ast.Tuple):
                for item in child.type.elts:
                    if isinstance(item, ast.Name):
                        found.append(item.id)
    seen = []
    for name in found:
        if name not in seen:
            seen.append(name)
    return seen


def extract_calls(node):
    """提取函数体里调用的其它函数名（用于被测函数之间的调用关系）"""
    names = []
    for child in ast.walk(node):
        if isinstance(child, ast.Call):
            func = child.func
            if isinstance(func, ast.Name):
                names.append(func.id)
            elif isinstance(func, ast.Attribute):
                names.append(func.attr)
    seen = []
    for name in names:
        if name not in seen:
            seen.append(name)
    return seen


def has_yield(node):
    for child in ast.walk(node):
        if isinstance(child, (ast.Yield, ast.YieldFrom)):
            return True
    return False


class FunctionInfo(object):
    """一个被测函数/方法"""

    def __init__(self, node, class_name=None):
        self.name = node.name
        self.class_name = class_name
        self.lineno = getattr(node, "lineno", 0)
        self.params = extract_params(node)
        self.returns = expr_text(node.returns)
        self.docstring = ast.get_docstring(node) or ""
        self.doc = parse_docstring(self.docstring)
        self.raises = extract_raises(node)
        for name in self.doc["raises"]:
            if name not in self.raises:
                self.raises.append(name)
        self.is_async = isinstance(node, ast.AsyncFunctionDef)
        self.is_generator = has_yield(node)
        self.is_private = self.name.startswith("_")
        self.calls = extract_calls(node)
        self.decorators = [expr_text(item) for item in node.decorator_list]

    @property
    def qualname(self):
        return "%s.%s" % (self.class_name, self.name) if self.class_name else self.name

    @property
    def signature(self):
        parts = []
        for param in self.params:
            text = param["name"]
            if param["annotation"]:
                text += ": %s" % param["annotation"]
            if param["default"]:
                text += "=%s" % param["default"]
            parts.append(text)
        sig = "%s(%s)" % (self.name, ", ".join(parts))
        if self.returns:
            sig += " -> %s" % self.returns
        return sig

    @property
    def summary(self):
        return self.doc["summary"] or "（无 docstring）"


class ClassInfo(object):
    """一个被测类"""

    def __init__(self, node):
        self.name = node.name
        self.lineno = getattr(node, "lineno", 0)
        self.bases = [expr_text(base) for base in node.bases]
        self.docstring = ast.get_docstring(node) or ""
        self.methods = []
        for child in node.body:
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                self.methods.append(FunctionInfo(child, class_name=self.name))
        self.is_private = self.name.startswith("_")

    @property
    def summary(self):
        return (self.docstring.strip().splitlines() or ["（无 docstring）"])[0].strip()


class ModuleInfo(object):
    """一个被测模块"""

    def __init__(self, path):
        self.path = Path(path)
        self.name = self.path.stem
        self.rel = str(self.path)
        self.source = self.path.read_text(encoding="utf-8")
        self.imports = []
        self.functions = []
        self.classes = []
        self.call_graph = {}
        self._analyze()

    def _analyze(self):
        tree = ast.parse(self.source, filename=str(self.path))
        for node in tree.body:
            if isinstance(node, ast.Import):
                for alias in node.names:
                    self.imports.append(alias.name)
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    self.imports.append(node.module)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                self.functions.append(FunctionInfo(node))
            elif isinstance(node, ast.ClassDef):
                self.classes.append(ClassInfo(node))
        known = set(func.name for func in self.functions)
        for klass in self.classes:
            for method in klass.methods:
                known.add(method.name)
        for func in self.functions:
            self.call_graph[func.qualname] = [c for c in func.calls if c in known]
        for klass in self.classes:
            for method in klass.methods:
                self.call_graph[method.qualname] = [c for c in method.calls if c in known]

    @property
    def all_functions(self):
        """函数 + 方法，统一成 FunctionInfo 列表"""
        result = list(self.functions)
        for klass in self.classes:
            result.extend(klass.methods)
        return result


def collect_modules(paths):
    """收集要生成测试的 py 文件，返回 (模块列表, 备注)"""
    files = []
    notes = []
    seen = set()
    for raw in paths:
        target = Path(raw).expanduser()
        if not target.exists():
            raise FileNotFoundError("路径不存在：%s" % raw)
        if target.is_file():
            if target.suffix != ".py":
                notes.append("跳过非 Python 文件：%s" % target)
                continue
            files.append(target)
        else:
            for root, dirs, names in os.walk(str(target)):
                dirs[:] = [d for d in dirs if d not in
                           {".git", "__pycache__", "tests", "venv", ".venv", "node_modules"}]
                for name in sorted(names):
                    if name.endswith(".py") and not name.startswith("test_"):
                        files.append(Path(root) / name)

    modules = []
    for file_path in files:
        real = str(file_path.resolve())
        if real in seen:
            continue
        seen.add(real)
        if len(modules) >= MAX_FILES:
            notes.append("模块数量超过 %d，其余不再处理" % MAX_FILES)
            break
        try:
            modules.append(ModuleInfo(file_path))
        except SyntaxError as exc:
            notes.append("跳过 %s：语法错误（第 %s 行）：%s"
                         % (file_path, getattr(exc, "lineno", "?"), exc.msg))
        except (OSError, UnicodeDecodeError, ValueError) as exc:
            notes.append("跳过 %s：%s" % (file_path, exc))
    return modules, notes


# ---------- 用例规划 ----------

def _type_hint(param):
    return (param.get("annotation") or "").lower()


def placeholder_value(param):
    """按类型注解挑一个“典型值”占位"""
    if param.get("default"):
        return param["default"]
    hint = _type_hint(param)
    for key, value in TYPE_PLACEHOLDERS:
        if key in hint:
            return value
    return "None"


def boundary_value(param):
    """按类型注解挑一个边界值：空串 / 0 / None / 空列表 / 空字典"""
    hint = _type_hint(param)
    for key, value in TYPE_BOUNDARIES:
        if key in hint:
            return value
    return "None"


def return_assert_lines(returns, var="result"):
    """根据返回值注解生成断言行"""
    text = (returns or "").strip()
    lowered = text.lower()
    if not text or lowered == "none":
        return ["assert %s is None" % var] if lowered == "none" else [
            "assert %s is not None" % var]
    for key, type_name in (("int", "int"), ("float", "float"), ("str", "str"),
                           ("bool", "bool"), ("list", "list"), ("dict", "dict"),
                           ("tuple", "tuple"), ("set", "set")):
        if lowered == key or lowered.startswith(key + "[") or lowered.startswith(key + "("):
            return ["assert isinstance(%s, %s)" % (var, type_name)]
    return ["assert %s is not None" % var]


def safe_name(text):
    return re.sub(r"\W", "_", str(text)).strip("_").lower() or "case"


def plan_cases(module, coverage):
    """确定性规划用例：典型路径 / 边界值 / 异常路径 / 返回值类型"""
    cases = []
    skipped = []
    for func in module.all_functions:
        if coverage == "公开函数" and func.is_private:
            skipped.append(func.qualname)
            continue
        if coverage == "公开函数" and func.class_name and func.class_name.startswith("_"):
            skipped.append(func.qualname)
            continue
        group_cases = []
        params = [p for p in func.params if p["name"] not in ("self", "cls")]
        call_args = ", ".join(placeholder_value(p) for p in params)

        group_cases.append({
            "case_type": "典型路径",
            "func": func,
            "args": call_args,
            "params": params,
            "assert_point": "返回值非 None，且与 %s 的语义一致" % func.qualname,
            "doc": "典型路径：%s 正常输入应返回预期结果" % func.summary,
        })

        for param in params[:4]:
            group_cases.append({
                "case_type": "边界值",
                "func": func,
                "args": ", ".join(
                    boundary_value(p) if p["name"] == param["name"] else placeholder_value(p)
                    for p in params),
                "params": params,
                "boundary_param": param["name"],
                "boundary_value": boundary_value(param),
                "assert_point": "参数 %s 取边界值 %s 时不崩溃，返回值类型正确"
                                % (param["name"], boundary_value(param)),
                "doc": "边界值：参数 %s = %s（空/0/None/空串/空列表）"
                       % (param["name"], boundary_value(param)),
            })

        seen_exc = []
        for exc in func.raises:
            if exc in seen_exc:
                continue
            seen_exc.append(exc)
            group_cases.append({
                "case_type": "异常路径",
                "func": func,
                "args": ", ".join("None" for _ in params) if params else "",
                "params": params,
                "exc": exc,
                "assert_point": "输入非法时抛出 %s" % exc,
                "doc": "异常路径：%s 应抛出 %s" % (func.qualname, exc),
            })

        group_cases.append({
            "case_type": "返回值类型",
            "func": func,
            "args": call_args,
            "params": params,
            "assert_point": "返回值类型为 %s" % (func.returns or "非 None"),
            "doc": "返回值类型：%s 返回 %s" % (func.qualname, func.returns or "非 None 值"),
        })

        for index, case in enumerate(group_cases[:MAX_CASES_PER_FUNC]):
            if case["case_type"] == "典型路径":
                suffix = "normal"
            elif case["case_type"] == "边界值":
                suffix = "boundary_%s" % safe_name(case.get("boundary_param", "value"))
            elif case["case_type"] == "异常路径":
                suffix = "raises_%s" % safe_name(case.get("exc", "error"))
            else:
                suffix = "return_type"
            case["name"] = "test_%s_%s" % (safe_name(func.qualname), suffix)
            if any(c["name"] == case["name"] for c in cases):
                case["name"] = "%s_%d" % (case["name"], index + 1)
            cases.append(case)
    return cases, skipped


# ---------- 测试骨架生成 ----------

def import_target_lines(module, test_dir):
    """生成 sys.path 注入 + 导入被测模块的代码行"""
    rel = os.path.relpath(str(module.path.parent), str(test_dir))
    lines = [
        "sys.path.insert(0, os.path.normpath(os.path.join("
        "os.path.dirname(os.path.abspath(__file__)), %r)))" % rel,
    ]
    if re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", module.name):
        lines.append("")
        lines.append("import %s as target" % module.name)
    else:
        # 模块名不是合法标识符时用 importlib 按文件路径加载
        lines.append("import importlib.util")
        lines.append("")
        lines.append("_SPEC = importlib.util.spec_from_file_location(")
        lines.append('    "%s", os.path.join(os.path.dirname(os.path.abspath(__file__)), %r)'
                     % (safe_name(module.name), os.path.join(rel, module.path.name)))
        lines.append(")")
        lines.append("target = importlib.util.module_from_spec(_SPEC)")
        lines.append("_SPEC.loader.exec_module(target)")
    return lines


def class_init_args(module, class_name):
    """被测类构造参数占位"""
    for klass in module.classes:
        if klass.name != class_name:
            continue
        for method in klass.methods:
            if method.name == "__init__":
                params = [p for p in method.params if p["name"] not in ("self", "cls")]
                return ", ".join(placeholder_value(p) for p in params)
    return ""


def render_case_body(case, framework, module):
    """渲染单条用例的函数体（不含 def 行），pytest 与 unittest 两套写法"""
    func = case["func"]
    call = "target.%s(%s)" % (func.qualname, case["args"])
    if func.class_name:
        instance_expr = "target.%s(%s)" % (
            func.class_name, class_init_args(module, func.class_name))
        call = "instance.%s(%s)" % (func.name, case["args"])
    if func.is_generator:
        call = "list(%s)" % call
    elif func.is_async:
        call = "asyncio.run(%s)" % call

    lines = []
    if case["case_type"] == "异常路径":
        exc_name = case["exc"] if case["exc"] in BUILTIN_EXCEPTIONS else "Exception"
        if framework == "pytest":
            lines.append("with pytest.raises(%s):" % exc_name)
        else:
            lines.append("with self.assertRaises(%s):" % exc_name)
        if func.class_name:
            lines.append("    instance = %s" % instance_expr)
        lines.append("    %s" % call)
        if case["exc"] not in BUILTIN_EXCEPTIONS:
            lines.append("# 注：%s 不是内置异常名，已降级为 Exception 断言" % case["exc"])
        return lines

    body = []
    if func.class_name:
        body.append("instance = %s" % instance_expr)
    body.append("result = %s" % call)

    if case["case_type"] == "边界值":
        # 边界值允许抛 TypeError/ValueError，但不允许其它未预期崩溃
        if framework == "pytest":
            skip_line = 'pytest.skip("边界输入抛出 TypeError/ValueError，需按语义补全精确预期")'
        else:
            skip_line = 'self.skipTest("边界输入抛出 TypeError/ValueError，需按语义补全精确预期")'
        wrapped = ["try:"]
        wrapped.extend("    %s" % line for line in body)
        wrapped.append("except (TypeError, ValueError):")
        wrapped.append("    %s" % skip_line)
        wrapped.extend(return_assert_lines(func.returns))
        return wrapped

    lines.extend(body)
    lines.extend(return_assert_lines(func.returns))
    return lines


def render_test_file(module, cases, framework, test_file_path):
    """生成完整的测试文件源码"""
    test_dir = str(Path(test_file_path).parent)
    groups = []
    for case in cases:
        key = case["func"].class_name or case["func"].qualname
        if key not in [g[0] for g in groups]:
            groups.append((key, []))
        for item in groups:
            if item[0] == key:
                item[1].append(case)

    needs_async = any(case["func"].is_async for case in cases)
    lines = [
        "# -*- coding: utf-8 -*-",
        '"""%s 的单元测试（由 test-generator 生成）' % module.name,
        "",
        "被测对象：%s" % "、".join(sorted(set(c["func"].qualname for c in cases))),
        "用例类型：%s" % " / ".join(CASE_TYPES),
        "",
        "提示：断言与测试数据是确定性骨架，可用模型补全后再精化。",
        '"""',
        "",
        "import os",
        "import sys",
    ]
    if needs_async:
        lines.append("import asyncio")
    if framework == "pytest":
        lines.append("")
        lines.append("import pytest")
    else:
        lines.append("")
        lines.append("import unittest")
    lines.append("")
    lines.extend(import_target_lines(module, test_dir))
    lines.append("")

    for key, group_cases in groups:
        func = group_cases[0]["func"]
        class_name = func.class_name
        test_class = "Test%s" % safe_name(key).title().replace("_", "")
        lines.append("")
        if framework == "unittest":
            lines.append("class %s(unittest.TestCase):" % test_class)
        else:
            lines.append("class %s:" % test_class)
        if class_name:
            lines.append('    """针对 %s 的测试"""' % class_name)
        else:
            lines.append('    """针对 %s 的测试"""' % func.qualname)
        lines.append("")
        used_names = set()
        for case in group_cases:
            name = case["name"]
            while name in used_names:
                name = "%s_x" % name
            used_names.add(name)
            lines.append("    def %s(self):" % name)
            lines.append('        """%s"""' % case["doc"].replace('"""', "'''"))
            for body_line in render_case_body(case, framework, module):
                if body_line.startswith("#"):
                    lines.append("        %s" % body_line)
                else:
                    lines.extend("        %s" % line for line in textwrap.indent(body_line, "").splitlines())
            lines.append("")
        # 参数化占位用例
        params = [p for p in group_cases[0]["params"] if p["name"] not in ("self", "cls")]
        if params:
            names = ", ".join(p["name"] for p in params)
            sample = ", ".join(placeholder_value(p) for p in params)
            boundary = ", ".join(boundary_value(p) for p in params)
            if framework == "pytest":
                lines.append('    @pytest.mark.parametrize("%s", [(%s), (%s)])'
                             % (names, sample, boundary))
                lines.append("    def test_%s_param_cases(self, %s):"
                             % (safe_name(func.qualname), names))
            else:
                lines.append("    def test_%s_param_cases(self):" % safe_name(func.qualname))
            lines.append('        """参数化占位用例：批量验证不同输入组合"""')
            if framework == "unittest":
                lines.append("        for %s in [(%s), (%s)]:" % (names, sample, boundary))
                indent = "            "
            else:
                indent = "        "
            call = "target.%s(%s)" % (func.qualname, names)
            if func.class_name:
                call = "instance.%s(%s)" % (func.name, names)
                lines.append("%sinstance = target.%s(%s)"
                             % (indent, func.class_name, class_init_args(module, func.class_name)))
            if func.is_generator:
                call = "list(%s)" % call
            elif func.is_async:
                call = "asyncio.run(%s)" % call
            lines.append("%stry:" % indent)
            lines.append("%s    result = %s" % (indent, call))
            lines.append("%sexcept (TypeError, ValueError):" % indent)
            if framework == "pytest":
                lines.append('%s    pytest.skip("占位输入抛异常，需补全真实输入组合")' % indent)
            else:
                lines.append('%s    self.skipTest("占位输入抛异常，需补全真实输入组合")' % indent)
            lines.append("%sassert result is not None" % indent)
            lines.append("")
    if framework == "unittest":
        lines.append("")
        lines.append('if __name__ == "__main__":')
        lines.append("    unittest.main()")
    return "\n".join(lines).rstrip() + "\n"


# ---------- 模型补全与合并 ----------

TESTS_SYSTEM_PROMPT = """你是一名资深 Python 测试工程师。
用户会给出一个已经生成好的测试骨架（含被测函数签名、docstring、异常与用例规划），
请为其中的用例补全真实的测试数据与断言。

输出要求：
1. 只输出一个 JSON 对象，形如 {"tests": [{"name": "", "code": "", "reason": ""}]}，
   不要输出任何解释文字，不要使用 Markdown 代码围栏。
2. name 必须与骨架里的测试方法名完全一致（如 test_add_normal）。
3. code 必须是完整的测试方法源码：一行 def 行 + 函数体，保持骨架里 self 的写法，
   不要输出 class，不要输出 import。
4. reason 用一句话说明你补了什么断言。
5. 无法确定预期的用例不要编造，宁可少给几条。
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


def response_text(response):
    parts = []
    for block in getattr(response, "content", []) or []:
        if getattr(block, "type", "") == "text":
            parts.append(getattr(block, "text", ""))
    return "\n".join(parts).strip()


def parse_tests_json(text):
    """把模型回复解析成 {"tests": [...]}；解析不出来返回 None（退回纯骨架）"""
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
    raw_tests = data.get("tests")
    if not isinstance(raw_tests, list):
        return None
    tests = []
    for item in raw_tests:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        code = textwrap.dedent(str(item.get("code") or ""))
        reason = str(item.get("reason") or "").strip()
        if not name or not code.strip():
            continue
        tests.append({"name": name, "code": code, "reason": reason})
    return tests or None


def merge_model_tests(source, tests, framework):
    """把模型补全的用例合并进骨架；任何一步不合法都退回纯骨架"""
    original = source
    if not tests:
        return source, []
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return original, ["骨架解析失败，已忽略模型补全的用例"]
    source_lines = source.splitlines()
    segments = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            segments.setdefault(node.name, (node.lineno, node.end_lineno))

    replacements = {}
    extras = []
    notes = []
    for test in tests:
        name = test["name"]
        code = textwrap.dedent(str(test["code"])).strip("\n")
        try:
            snippet = ast.parse(code)
        except SyntaxError:
            notes.append("模型用例 %s 语法不合法，已丢弃" % name)
            continue
        nodes = [node for node in snippet.body
                 if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))]
        if len(nodes) != 1 or nodes[0].name != name:
            notes.append("模型用例 %s 内容与名字不符，已丢弃" % name)
            continue
        if name in segments:
            replacements[name] = code
            notes.append("已用模型补全替换 %s" % name)
        else:
            extras.append(code)
            notes.append("模型新增用例 %s（骨架里没有同名用例）" % name)

    if replacements:
        inserts = []
        for name, (start, end) in segments.items():
            if name not in replacements:
                continue
            # 按原函数所在位置的缩进级别重新缩进，保证类里的方法不会被顶格替换
            indent = re.match(r"[ \t]*", source_lines[start - 1]).group(0)
            code = textwrap.indent(replacements[name], indent)
            inserts.append((start, end, code.splitlines()))
        inserts.sort(key=lambda item: item[0], reverse=True)
        for start, end, code_lines in inserts:
            source_lines[start - 1:end] = code_lines
        source = "\n".join(source_lines) + "\n"

    if extras:
        if framework == "unittest":
            block = ["", "", "class TestModelCases(unittest.TestCase):",
                     '    """模型补全的额外用例"""', ""]
            for code in extras:
                block.extend(textwrap.indent(code, "    ").splitlines())
                block.append("")
            source = source.rstrip() + "\n" + "\n".join(block) + "\n"
        else:
            source = source.rstrip() + "\n\n\n" + "\n\n".join(extras) + "\n"

    try:
        ast.parse(source)
    except SyntaxError as exc:
        return original, ["合并后语法不合法（第 %s 行），已退回纯骨架：%s"
                          % (getattr(exc, "lineno", "?"), exc.msg)]
    return source, notes


# ---------- 已有测试检测 ----------

def should_write(path, force):
    """目标已存在时：默认跳过，--force 才覆盖"""
    if not path.exists():
        return True, "新建"
    if force:
        return True, "覆盖已有文件"
    return False, "已存在同名测试文件，已跳过（加 --force 可覆盖）"


# ---------- 运行与输出解析 ----------

def parse_test_output(text):
    """解析 pytest / unittest 的输出，统计通过/失败/错误/跳过数量"""
    text = text or ""
    stats = {"passed": 0, "failed": 0, "errors": 0, "skipped": 0, "total": 0}
    # pytest 风格：3 passed, 1 failed, 2 skipped in 0.51s
    passed_match = re.search(r"(\d+)\s+passed", text)
    if passed_match:
        stats["passed"] = int(passed_match.group(1))
    for key, pattern in (
        ("failed", r"(\d+)\s+failed"),
        ("errors", r"(\d+)\s+errors?"),
        ("skipped", r"(\d+)\s+skipped"),
    ):
        match = re.search(pattern, text)
        if match:
            stats[key] = int(match.group(1))
    # unittest 风格：Ran 5 tests in 0.01s / FAILED (failures=2, errors=1)
    ran = re.search(r"Ran (\d+) tests?", text)
    if ran:
        stats["total"] = int(ran.group(1))
    for key, pattern in (
        ("failed", r"failures?=(\d+)"),
        ("errors", r"errors?=(\d+)"),
        ("skipped", r"skipped=(\d+)"),
    ):
        match = re.search(pattern, text)
        if match:
            stats[key] = int(match.group(1))
    if stats["total"] and not passed_match:
        stats["passed"] = max(0, stats["total"] - stats["failed"]
                              - stats["errors"] - stats["skipped"])
    if not stats["total"]:
        stats["total"] = stats["passed"] + stats["failed"] + stats["errors"] + stats["skipped"]
    return stats


def run_tests(framework, test_dir, timeout=RUN_TIMEOUT):
    """用 subprocess 跑测试（带超时保护），返回（统计, 原始输出）"""
    if framework == "pytest":
        cmd = [sys.executable, "-m", "pytest", str(test_dir), "-q"]
    else:
        # 顶层目录设成测试目录自身，避免目录名不是合法包名时无法发现用例
        cmd = [sys.executable, "-m", "unittest", "discover",
               "-s", str(test_dir), "-t", str(test_dir), "-v"]
    try:
        proc = subprocess.run(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            timeout=timeout, encoding="utf-8", errors="replace",
        )
        output = proc.stdout or ""
        if proc.returncode != 0 and "No module named" in output:
            hint = "pip install pytest" if framework == "pytest" else "换用 --framework pytest"
            return None, "测试框架不可用：%s\n请先安装：%s" % (output.strip(), hint)
        return parse_test_output(output), output
    except subprocess.TimeoutExpired:
        return None, "运行超时（%d 秒），已终止" % timeout
    except OSError as exc:
        return None, "无法启动测试进程：%s" % exc


# ---------- 报告 ----------

def build_report(modules, files_info, cases, skipped_funcs, model_notes,
                 raw_model_text, model, offline, run_stats, run_output, notes):
    """生成中文 Markdown 测试计划报告"""
    lines = ["# 单元测试生成报告", ""]
    lines.append("> 由 test-generator 生成；确定性 ast 分析 + 测试骨架%s。"
                 % ("（--offline 离线模式，未调用模型）" if offline else " + 模型补全断言"))
    lines.append("")
    lines.append("## 概览")
    lines.append("")
    lines.append("| 指标 | 数值 |")
    lines.append("| --- | --- |")
    lines.append("| 被测模块数 | %d |" % len(modules))
    lines.append("| 被测对象（函数/方法） | %d |" % sum(len(m.all_functions) for m in modules))
    lines.append("| 生成测试文件 | %d |" % len(files_info))
    lines.append("| 规划用例数 | %d |" % len(cases))
    type_counts = {}
    for case in cases:
        type_counts[case["case_type"]] = type_counts.get(case["case_type"], 0) + 1
    for case_type in CASE_TYPES:
        lines.append("| %s用例 | %d |" % (case_type, type_counts.get(case_type, 0)))
    lines.append("")

    lines.append("## 被测对象清单")
    lines.append("")
    for module in modules:
        lines.append("### %s" % module.rel)
        lines.append("")
        lines.append("| 对象 | 类型 | 签名 | 摘要 | raise | 特性 | 调用关系 |")
        lines.append("| --- | --- | --- | --- | --- | --- | --- |")
        rows = []
        for func in module.functions:
            rows.append((func, "函数"))
        for klass in module.classes:
            for method in klass.methods:
                rows.append((method, "方法"))
        for func, kind in rows:
            features = []
            if func.is_async:
                features.append("async")
            if func.is_generator:
                features.append("生成器")
            if func.is_private:
                features.append("私有")
            calls = module.call_graph.get(func.qualname) or []
            lines.append("| `%s` | %s | `%s` | %s | %s | %s | %s |" % (
                func.qualname, kind, func.signature, func.summary,
                "、".join(func.raises) or "—",
                "、".join(features) or "—",
                "→ " + "、".join(calls) if calls else "—",
            ))
        lines.append("")

    lines.append("## 用例清单")
    lines.append("")
    lines.append("| 用例名 | 类型 | 断言点 | 所属对象 |")
    lines.append("| --- | --- | --- | --- |")
    for case in cases:
        lines.append("| `%s` | %s | %s | `%s` |" % (
            case["name"], case["case_type"], case["assert_point"], case["func"].qualname))
    lines.append("")

    lines.append("## 覆盖率计划")
    lines.append("")
    planned = sorted(set(case["func"].qualname for case in cases))
    total = sum(len(m.all_functions) for m in modules)
    lines.append("- 计划覆盖对象：%d / %d（预估 %.0f%%）"
                 % (len(planned), total, (100.0 * len(planned) / total) if total else 0))
    lines.append("- 覆盖对象：%s" % ("、".join(planned) or "无"))
    lines.append("- 跳过的对象：%s" % ("、".join(skipped_funcs) or "无"))
    lines.append("- 每个对象至少包含：典型路径 1 条、返回值类型 1 条、边界值若干条、异常路径若干条。")
    lines.append("")

    lines.append("## 生成文件")
    lines.append("")
    lines.append("| 文件 | 用例数 | 语法自检 | 处理方式 |")
    lines.append("| --- | --- | --- | --- |")
    for info in files_info:
        lines.append("| `%s` | %d | %s | %s |" % (
            info["path"], info["cases"], "通过" if info["syntax_ok"] else "失败", info["action"]))
    lines.append("")

    lines.append("## 运行结果")
    lines.append("")
    if run_stats:
        lines.append("| 通过 | 失败 | 错误 | 跳过 | 合计 |")
        lines.append("| --- | --- | --- | --- | --- |")
        lines.append("| %d | %d | %d | %d | %d |" % (
            run_stats["passed"], run_stats["failed"], run_stats["errors"],
            run_stats["skipped"], run_stats["total"]))
        lines.append("")
        if run_output:
            lines.append("```text")
            lines.append(run_output[-4000:])
            lines.append("```")
            lines.append("")
    else:
        if run_output:
            lines.append("测试没有正常完成：")
            lines.append("")
            lines.append("```text")
            lines.append(run_output[-2000:])
            lines.append("```")
            lines.append("")
        else:
            lines.append("本次没有加 `--run`，未执行测试；生成后可自行运行：")
            lines.append("`python3 -m pytest tests -q`")
            lines.append("")

    if model_notes:
        lines.append("## 模型补全说明")
        lines.append("")
        for note in model_notes:
            lines.append("- %s" % note)
        lines.append("")

    if notes:
        lines.append("## 备注")
        lines.append("")
        for note in notes:
            lines.append("- %s" % note)
        lines.append("")

    if raw_model_text and not model_notes:
        lines.append("## 附录：模型原始输出（未能解析成 JSON）")
        lines.append("")
        lines.append("```text")
        lines.append(raw_model_text[:8000])
        lines.append("```")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


# ---------- 自检 ----------

class _FakeMessages:
    """假的消息接口：第一次抛 TypeError（模拟网关不认 thinking），第二次成功"""

    def __init__(self, outer):
        self.outer = outer

    def create(self, **kwargs):
        self.outer["calls"] += 1
        if self.outer["calls"] == 1:
            raise TypeError("网关可能不认识 thinking 参数")
        return {"ok": True, "level": 3, "kwargs": kwargs}


class _FakeBetaMessages:
    """假的 beta 接口：永远抛 TypeError（模拟网关不认 beta 参数）"""

    def __init__(self, outer):
        self.outer = outer

    def create(self, **kwargs):
        self.outer["calls"] += 1
        raise TypeError("网关不认识 beta 参数")


class _FakeBeta:
    def __init__(self, outer):
        self.messages = _FakeBetaMessages(outer)


class _FakeClient:
    def __init__(self):
        self.state = {"calls": 0}
        self.beta = _FakeBeta(self.state)
        self.messages = _FakeMessages(self.state)


SAMPLE_MODULE = '''"""示例模块：自检用的被测对象"""
import math


def add(a: int, b: int = 0) -> int:
    """把两个数相加

    Args:
        a (int): 第一个加数
        b (int): 第二个加数

    Returns:
        int: 两数之和

    Raises:
        TypeError: 参数不是数字时
    """
    if not isinstance(a, (int, float)):
        raise TypeError("a 必须是数字")
    return a + b


def divide(a: float, b: float) -> float:
    """做除法

    Args:
        a (float): 被除数
        b (float): 除数

    Returns:
        float: 商

    Raises:
        ValueError: 除数为 0 时
    """
    if b == 0:
        raise ValueError("除数不能为 0")
    return a / b


def count_items(items: list) -> int:
    """统计个数

    Args:
        items (list): 输入列表

    Returns:
        int: 个数
    """
    total = add(0)
    for item in items:
        total = add(total, 1)
    return total


def pairs(values: list):
    """成对产出元素

    Args:
        values (list): 输入列表

    Yields:
        tuple: 二元组
    """
    for value in values:
        yield (value, value)


async def fetch(url: str) -> str:
    """异步获取内容

    Args:
        url (str): 地址

    Returns:
        str: 内容
    """
    return url


def _helper(x):
    """内部辅助函数"""
    return x


class Calculator:
    """一个简单计算器"""

    def __init__(self, base: int = 0):
        self.base = base

    def plus(self, value: int) -> int:
        """加上一个数

        Args:
            value (int): 加数

        Returns:
            int: 结果
        """
        return add(self.base, value)

    def reset(self) -> None:
        """重置状态"""
        self.base = 0
'''


def run_self_test():
    """离线自检：造样本、跑全部确定性逻辑、assert 校验，不联网、不需要 API key"""
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        (root / "pkg").mkdir()
        (root / "pkg" / "sample.py").write_text(SAMPLE_MODULE, encoding="utf-8")

        # --- ast 提取：签名、docstring、异常、yield/async、类、调用关系 ---
        module = ModuleInfo(root / "pkg" / "sample.py")
        by_name = dict((f.name, f) for f in module.all_functions)
        assert set(["add", "divide", "count_items", "pairs", "fetch", "plus"]).issubset(by_name)

        add = by_name["add"]
        assert [p["name"] for p in add.params] == ["a", "b"]
        assert add.params[0]["annotation"] == "int" and add.params[0]["required"] is True
        assert add.params[1]["default"] == "0" and add.params[1]["required"] is False
        assert add.returns == "int"
        assert "TypeError" in add.raises, "raise 的异常没有提取到"
        assert add.doc["args"].get("a"), "docstring Args 解析失败"
        assert "和" in add.doc["returns"] or add.doc["returns"], "docstring Returns 解析失败"
        assert "TypeError" in add.doc["raises"], "docstring Raises 解析失败"

        assert "ValueError" in by_name["divide"].raises
        assert by_name["pairs"].is_generator is True
        assert by_name["fetch"].is_async is True
        assert by_name["reset"].returns == "None" or by_name["reset"].returns == "None"

        classes = dict((c.name, c) for c in module.classes)
        assert "Calculator" in classes
        assert [m.name for m in classes["Calculator"].methods] == ["__init__", "plus", "reset"]
        assert classes["Calculator"].bases == []
        assert "math" in module.imports
        assert "add" in module.call_graph["count_items"], "调用关系没有提取到"

        # --- 用例规划：四种类型都要有，边界值/异常路径要有具体样本 ---
        cases, skipped = plan_cases(module, "全部")
        names = [c["name"] for c in cases]
        assert len(names) == len(set(names)), "用例名有重复：%s" % names
        assert set(c["case_type"] for c in cases) == set(CASE_TYPES), "用例类型不全"
        add_cases = [c for c in cases if c["func"].name == "add"]
        assert any(c["case_type"] == "典型路径" for c in add_cases)
        assert any(c["case_type"] == "异常路径" and c["exc"] == "TypeError" for c in add_cases)
        assert any(c["case_type"] == "返回值类型" for c in add_cases)
        boundary = [c for c in cases if c["case_type"] == "边界值"]
        assert boundary, "没有规划边界值用例"
        assert any(c.get("boundary_value") == "[]" for c in boundary), "空列表边界值没有覆盖"
        assert any(c.get("boundary_value") == '""' for c in boundary), "空串边界值没有覆盖"
        assert any(c.get("boundary_value") == "0" for c in boundary), "0 边界值没有覆盖"
        assert any(c.get("boundary_value") == "None" for c in boundary), "None 边界值没有覆盖"
        assert all(c["assert_point"] for c in cases)

        public_cases, public_skipped = plan_cases(module, "公开函数")
        assert "_helper" not in [c["func"].name for c in public_cases]
        assert any("_helper" in item for item in public_skipped), "私有函数应当被跳过"
        assert len(public_cases) < len(cases)

        # --- 骨架生成：两种框架都要语法正确、结构齐全 ---
        test_path = root / "tests" / "test_sample.py"
        test_path.parent.mkdir(parents=True, exist_ok=True)
        pytest_code = render_test_file(module, cases, "pytest", test_path)
        unittest_code = render_test_file(module, cases, "unittest", test_path)
        ast.parse(pytest_code)
        ast.parse(unittest_code)
        assert "import sample as target" in pytest_code
        assert "sys.path.insert" in pytest_code, "缺 sys.path 注入"
        assert "pytest.raises(TypeError)" in pytest_code, "异常用例没有用 pytest.raises"
        assert "pytest.mark.parametrize" in pytest_code, "缺参数化占位"
        assert "unittest.TestCase" in unittest_code
        assert "assertRaises" in unittest_code
        assert "asyncio.run(target.fetch" in pytest_code, "async 函数应当被 asyncio.run 包装"
        assert "list(target.pairs" in pytest_code, "生成器应当被 list() 包装"
        assert '"""' in pytest_code and "def test_" in pytest_code
        # 生成的文件要能真正 import 被测模块
        test_path.write_text(pytest_code, encoding="utf-8")
        assert test_path.read_text(encoding="utf-8").startswith("# -*- coding: utf-8 -*-")

        # --- 模型回复解析兜底 ---
        good = parse_tests_json(
            '```json\n{"tests": [{"name": "test_add_normal", "code": '
            '"def test_add_normal(self):\\n    assert add(1, 2) == 3", "reason": "补了真实断言"}]}\n```'
        )
        assert good and good[0]["name"] == "test_add_normal" and "assert" in good[0]["code"]
        assert parse_tests_json("这不是 JSON") is None
        assert parse_tests_json("") is None
        assert parse_tests_json('{"tests": "oops"}') is None
        assert parse_tests_json('{"tests": [{"name": "x"}]}') is None

        # --- 模型用例合并：成功替换、非法退回纯骨架 ---
        merged, merge_notes = merge_model_tests(
            pytest_code,
            [{"name": "test_add_normal",
              "code": "def test_add_normal(self):\n    assert target.add(1, 2) == 3",
              "reason": "补了真实断言"}],
            "pytest",
        )
        ast.parse(merged)
        assert "target.add(1, 2) == 3" in merged
        assert merge_notes, "合并应当有说明"

        broken, broken_notes = merge_model_tests(
            pytest_code,
            [{"name": "test_add_normal", "code": "def test_add_normal(self:\n  pass", "reason": "坏代码"}],
            "pytest",
        )
        assert broken == pytest_code, "语法不合法的模型代码必须退回纯骨架"
        assert broken_notes

        # 骨架里没有同名用例时：追加成额外用例（unittest 里放进新的 TestCase）
        extra, extra_notes = merge_model_tests(
            pytest_code,
            [{"name": "test_zzz_extra", "code": "def test_zzz_extra(self):\n    assert True",
              "reason": "新增用例"}],
            "unittest",
        )
        ast.parse(extra)
        assert "TestModelCases" in extra, "额外用例应当追加进新的 TestCase"
        assert extra_notes

        fallback = parse_tests_json("我建议补几条断言，但不是 JSON")
        merged_fallback, _ = merge_model_tests(pytest_code, fallback, "pytest")
        assert merged_fallback == pytest_code, "解析失败时应当原样保留骨架"

        # --- 已有测试检测：--force 才覆盖 ---
        write, reason = should_write(Path("/不存在的目录/x.py"), False)
        assert write is True and "新建" in reason
        write, reason = should_write(test_path, False)
        assert write is False and "跳过" in reason
        write, reason = should_write(test_path, True)
        assert write is True and "覆盖" in reason

        # --- 测试输出解析 ---
        stats = parse_test_output("3 passed, 1 failed, 2 skipped in 0.51s")
        assert stats["passed"] == 3 and stats["failed"] == 1 and stats["skipped"] == 2
        stats = parse_test_output("Ran 5 tests in 0.01s\n\nOK")
        assert stats["total"] == 5 and stats["passed"] == 5
        stats = parse_test_output("Ran 10 tests in 0.000s\n\nOK (skipped=2)")
        assert stats["total"] == 10 and stats["skipped"] == 2 and stats["passed"] == 8
        stats = parse_test_output("Ran 4 tests in 0.01s\n\nFAILED (failures=1, errors=1)")
        assert stats["failed"] == 1 and stats["errors"] == 1 and stats["passed"] == 2
        assert parse_test_output("")["total"] == 0

        # --- 报告生成 ---
        files_info = [{"path": str(test_path), "cases": len(cases),
                       "syntax_ok": True, "action": "新建"}]
        report = build_report(
            [module], files_info, cases, public_skipped, ["已用模型补全替换 test_add_normal"],
            "", DEFAULT_MODEL, True, None, "", ["示例备注"],
        )
        assert report.startswith("# 单元测试生成报告")
        for heading in ("## 概览", "## 被测对象清单", "## 用例清单",
                        "## 覆盖率计划", "## 生成文件", "## 运行结果"):
            assert heading in report, "报告缺少 %s" % heading
        assert "test_add_normal" in report and "TypeError" in report

        run_report = build_report(
            [module], files_info, cases, [], [], "模型说了一段散文", DEFAULT_MODEL, False,
            {"passed": 3, "failed": 1, "errors": 0, "skipped": 2, "total": 6},
            "3 passed, 1 failed, 2 skipped in 0.51s", [],
        )
        assert "附录：模型原始输出" in run_report and "模型说了一段散文" in run_report
        assert "| 3 | 1 | 0 | 2 | 6 |" in run_report

        # --- call_model 逐级降级（离线假客户端） ---
        fake = _FakeClient()
        result = call_model(fake, "fake-model", "system", [{"role": "user", "content": "hi"}])
        assert result["ok"] is True
        assert fake.state["calls"] == 2, "降级调用次数应该是 2，实际 %s" % fake.state["calls"]

        # --- 语法错误模块应当被跳过并记备注 ---
        (root / "pkg" / "bad.py").write_text("def broken(:\n    pass\n", encoding="utf-8")
        modules, module_notes = collect_modules([str(root / "pkg")])
        assert [m.name for m in modules] == ["sample"], "语法错误模块应被跳过"
        assert any("语法错误" in note for note in module_notes)

    print("SELF-TEST OK")


# ---------- CLI ----------

def build_parser():
    parser = argparse.ArgumentParser(
        description="单元测试生成：ast 分析被测对象、生成 pytest/unittest 测试骨架，可选让模型补全断言",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            "  python3 agent.py ./src --output-dir tests\n"
            "  python3 agent.py ./src/app.py --framework unittest --coverage 公开函数\n"
            "  python3 agent.py ./src --run --output 测试报告.md\n"
        ),
    )
    parser.add_argument("paths", nargs="*", help="被测 Python 文件或目录，可以给多个")
    parser.add_argument(
        "--output-dir",
        default=DEFAULT_OUTPUT_DIR,
        help="测试文件输出目录（默认 %s）" % DEFAULT_OUTPUT_DIR,
    )
    parser.add_argument(
        "--framework",
        choices=list(FRAMEWORKS),
        default="pytest",
        help="测试框架（默认 pytest）",
    )
    parser.add_argument(
        "--coverage",
        choices=list(COVERAGE_MODES),
        default="公开函数",
        help="覆盖范围：全部（含下划线开头的私有函数）或 公开函数（默认）",
    )
    parser.add_argument(
        "--run",
        action="store_true",
        help="生成后顺带跑一遍测试（subprocess + 超时保护），把结果写进报告",
    )
    parser.add_argument(
        "--output",
        default=DEFAULT_REPORT,
        help="测试计划报告写入路径（默认 %s）" % DEFAULT_REPORT,
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="目标目录已有同名测试文件时覆盖（默认跳过）",
    )
    parser.add_argument(
        "--offline",
        action="store_true",
        help="纯确定性模式：只生成骨架、不调用模型、不需要 API key",
    )
    parser.add_argument(
        "--model",
        default=DEFAULT_MODEL,
        help="补全断言使用的模型（默认 %s）" % DEFAULT_MODEL,
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

    if not args.paths:
        parser.error("请至少提供一个被测 Python 文件或目录")

    try:
        modules, notes = collect_modules(args.paths)
    except FileNotFoundError as exc:
        print("错误：%s" % exc, file=sys.stderr)
        sys.exit(1)

    if not modules:
        print("错误：没有可分析的 Python 模块（请确认路径下有 .py 文件）。", file=sys.stderr)
        sys.exit(1)

    print("[分析] 模块 %d 个，被测对象 %d 个，框架 %s，覆盖范围 %s"
          % (len(modules), sum(len(m.all_functions) for m in modules),
             args.framework, args.coverage))

    all_cases = []
    skipped_funcs = []
    files_info = []
    output_dir = Path(args.output_dir).expanduser()

    for module in modules:
        cases, skipped = plan_cases(module, args.coverage)
        all_cases.extend(cases)
        skipped_funcs.extend(skipped)
        if not cases:
            notes.append("%s 里没有符合条件的被测对象" % module.rel)
            continue
        test_name = "test_%s.py" % safe_name(module.name)
        test_path = output_dir / test_name
        ok, action = should_write(test_path, args.force)
        code = render_test_file(module, cases, args.framework, test_path)
        try:
            ast.parse(code)
            syntax_ok = True
        except SyntaxError:
            syntax_ok = False
            notes.append("%s 生成结果语法自检未通过" % test_path)
        seen_funcs = {}
        for case in cases:
            seen_funcs.setdefault(case["func"].qualname, case["func"])
        signatures = [
            {"name": func.qualname, "signature": func.signature,
             "doc": func.docstring, "raises": func.raises}
            for func in list(seen_funcs.values())[:20]
        ]
        info = {"path": str(test_path), "cases": len(cases),
                "syntax_ok": syntax_ok, "action": action, "signatures": signatures}
        if not ok:
            files_info.append(info)
            print("[跳过] %s（%s）" % (test_path, action))
            continue
        try:
            test_path.parent.mkdir(parents=True, exist_ok=True)
            test_path.write_text(code, encoding="utf-8")
        except OSError as exc:
            notes.append("写入 %s 失败：%s" % (test_path, exc))
            continue
        files_info.append(info)
        print("[生成] %s（%d 条用例，语法自检 %s，%s）"
              % (test_path, len(cases), "通过" if syntax_ok else "失败", action))

    model_notes = []
    raw_model_text = ""
    if args.offline:
        print("[离线] --offline 模式，跳过模型补全")
    elif not files_info:
        print("[跳过] 没有新生成的测试文件，跳过模型补全")
    else:
        if not os.environ.get("ANTHROPIC_API_KEY") and not os.environ.get("ANTHROPIC_AUTH_TOKEN"):
            print(
                "提示：没有检测到 ANTHROPIC_API_KEY / ANTHROPIC_AUTH_TOKEN，无法让模型补全断言。\n"
                "请先设置：\n"
                "  macOS / Linux：export ANTHROPIC_API_KEY=你的key\n"
                "  Windows PowerShell：$env:ANTHROPIC_API_KEY=\"你的key\"\n"
                "或者加 --offline 生成纯骨架（不需要 key）。",
                file=sys.stderr,
            )
            sys.exit(1)
        print("[补全] 模型：%s" % args.model)
        client = anthropic.Anthropic()
        try:
            for info in files_info:
                test_path = Path(info["path"])
                skeleton = test_path.read_text(encoding="utf-8")
                payload = {
                    "module": info["path"],
                    "framework": args.framework,
                    "signatures": info.get("signatures", []),
                    "skeleton": skeleton[:20000],
                }
                messages = [{
                    "role": "user",
                    "content": "请补全下面测试骨架中的用例断言：\n```json\n%s\n```\n"
                               "按要求输出 JSON 对象。"
                               % json.dumps(payload, ensure_ascii=False, indent=2),
                }]
                response = call_model(client, args.model, TESTS_SYSTEM_PROMPT, messages,
                                      max_tokens=8000)
                text = response_text(response)
                raw_model_text = text
                tests = parse_tests_json(text)
                if not tests:
                    model_notes.append("%s：模型输出不是合法 JSON，已保留纯骨架" % test_path)
                    continue
                merged, merge_notes = merge_model_tests(skeleton, tests, args.framework)
                if merged != skeleton and merged:
                    test_path.write_text(merged, encoding="utf-8")
                model_notes.extend(["%s：%s" % (test_path.name, note) for note in merge_notes])
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
            print("错误：API 返回错误（%s）：%s" % (exc.status_code, exc.message), file=sys.stderr)
            sys.exit(1)

    run_stats = None
    run_output = ""
    if args.run and files_info:
        print("[运行] %s 测试（超时 %d 秒）..." % (args.framework, RUN_TIMEOUT))
        run_stats, run_output = run_tests(args.framework, str(output_dir))
        if run_stats:
            print("[运行] 通过 %d、失败 %d、错误 %d、跳过 %d"
                  % (run_stats["passed"], run_stats["failed"],
                     run_stats["errors"], run_stats["skipped"]))
        else:
            print("[运行] 测试没有正常结束：%s" % run_output)

    report = build_report(
        modules, files_info, all_cases, skipped_funcs, model_notes, raw_model_text,
        args.model, args.offline, run_stats, run_output, notes,
    )
    report_path = Path(args.output).expanduser()
    try:
        if report_path.parent and not report_path.parent.exists():
            report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(report, encoding="utf-8")
    except OSError as exc:
        print("错误：报告写入失败：%s" % exc, file=sys.stderr)
        sys.exit(1)

    print("[完成] 测试文件 %d 个，用例 %d 条，报告已写入：%s"
          % (len(files_info), len(all_cases), report_path))
    if notes:
        print("[提示] 有 %d 条备注，详见报告「备注」小节" % len(notes))


if __name__ == "__main__":
    main()
