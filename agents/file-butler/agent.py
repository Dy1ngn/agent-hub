#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
本地文件管家 —— 基于 Anthropic function-call（tool use）的文件管理 agent

你授权一个文件夹给它，然后用自然语言下命令，它会自己决定调用哪些文件工具
（列目录 / 读文件 / 搜索内容 / 写文件）来完成任务。不使用向量库，
全部通过直接的文件操作工具实现。

用法：
    python3 agent.py <授权文件夹> "<命令>"

示例：
    python3 agent.py ~/Documents/notes "把这个文件夹下所有 md 文件，提取标题汇总输出 report.md"
    python3 agent.py ./项目资料 "找出所有提到'合同'的文件，列一个清单保存为 清单.txt"

环境要求：
    1. 设置 ANTHROPIC_API_KEY 环境变量：
           export ANTHROPIC_API_KEY=你的key
    2. 安装 SDK：
           pip3 install -r requirements.txt
"""

import argparse
import fnmatch
import json
import os
import re
import sys
from pathlib import Path

import anthropic

# 默认模型（可用 --model 参数或 FILE_BUTLER_MODEL 环境变量更换）
DEFAULT_MODEL = os.environ.get("FILE_BUTLER_MODEL", "claude-opus-5")

MAX_FILE_CHARS = 200_000   # 单个文件最多读入多少字符，防止撑爆上下文
MAX_LIST_ENTRIES = 500     # 列表工具最多返回多少条
MAX_SEARCH_MATCHES = 200   # 搜索工具最多返回多少条匹配
MAX_ITERATIONS = 30        # 最多允许多少轮工具调用，防止死循环


class SandboxError(Exception):
    """出于安全原因拒绝的操作（路径越界、目标不存在等）"""


class FileSandbox:
    """把所有文件操作严格限制在用户授权的文件夹内"""

    def __init__(self, root):
        self.root = Path(root).expanduser().resolve()
        if not self.root.is_dir():
            raise FileNotFoundError("文件夹不存在：%s" % self.root)

    def resolve(self, rel_path):
        """把模型给的相对路径解析成绝对路径，并确认没有逃出授权文件夹"""
        candidate = (self.root / rel_path).resolve() if rel_path else self.root
        # Path.resolve() 会展开符号链接和 ../ ，再校验前缀就能防住越界
        if candidate != self.root and self.root not in candidate.parents:
            raise SandboxError("路径超出授权文件夹范围，已拒绝：%s" % rel_path)
        return candidate

    def rel(self, path):
        """显示用的相对路径"""
        try:
            return str(path.relative_to(self.root))
        except ValueError:
            return str(path)

    # ---------- 四个工具的实现 ----------

    def list_files(self, path="", pattern="*", recursive=False):
        base = self.resolve(path)
        if not base.is_dir():
            raise SandboxError("不是文件夹：%s" % path)
        entries = []
        iterator = base.rglob("*") if recursive else base.iterdir()
        for p in sorted(iterator):
            if p.name.startswith("."):
                continue  # 跳过隐藏文件/文件夹
            if p.is_file() and not fnmatch.fnmatch(p.name, pattern):
                continue
            if p.is_dir():
                entries.append("[文件夹] %s/" % self.rel(p))
            else:
                entries.append("[文件] %s (%d 字节)" % (self.rel(p), p.stat().st_size))
            if len(entries) >= MAX_LIST_ENTRIES:
                entries.append("……（条目过多，已截断，请缩小范围或加过滤条件）")
                break
        return "\n".join(entries) if entries else "（空）"

    def read_file(self, path):
        p = self.resolve(path)
        if not p.is_file():
            raise SandboxError("文件不存在：%s" % path)
        try:
            text = p.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            raise SandboxError("不是 UTF-8 文本文件（可能是二进制），无法读取：%s" % path)
        if len(text) > MAX_FILE_CHARS:
            text = text[:MAX_FILE_CHARS] + "\n……（文件过大，仅展示前 %d 字符）" % MAX_FILE_CHARS
        return text

    def write_file(self, path, content):
        p = self.resolve(path)
        if p.is_dir():
            raise SandboxError("目标是文件夹，不能写入：%s" % path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return "已写入：%s（%d 字符）" % (self.rel(p), len(content))

    def search_files(self, pattern, path=""):
        if not pattern:
            raise SandboxError("搜索内容不能为空")
        try:
            rx = re.compile(pattern)
        except re.error as e:
            raise SandboxError("正则表达式有误：%s" % e)
        base = self.resolve(path)
        if base.is_file():
            files = [base]
        else:
            files = sorted(
                p for p in base.rglob("*")
                if p.is_file() and not p.name.startswith(".")
            )
        matches = []
        for f in files:
            try:
                text = f.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue  # 跳过二进制/读不了的文件
            for lineno, line in enumerate(text.splitlines(), 1):
                if rx.search(line):
                    matches.append("%s:%d: %s" % (self.rel(f), lineno, line.strip()[:200]))
                    if len(matches) >= MAX_SEARCH_MATCHES:
                        return "\n".join(matches) + "\n……（匹配过多，已截断）"
        return "\n".join(matches) if matches else "没有找到匹配的内容"


# ---------- 给模型看的工具定义（function-call 的"菜单"） ----------

TOOLS = [
    {
        "name": "list_files",
        "description": (
            "列出授权文件夹里的文件和子文件夹。做任何事之前建议先调用它了解文件夹结构。"
            "可以用 pattern 过滤文件名（如 *.md），recursive 设为 true 可以递归列出所有层级。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "要列出的子文件夹（相对授权文件夹），留空表示根目录",
                },
                "pattern": {
                    "type": "string",
                    "description": "文件名过滤模式，如 *.md；默认 * 表示全部",
                },
                "recursive": {
                    "type": "boolean",
                    "description": "是否递归列出子文件夹中的内容，默认 false",
                },
            },
            "required": [],
        },
    },
    {
        "name": "read_file",
        "description": "读取授权文件夹内某个文本文件的完整内容。",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "要读取的文件路径（相对授权文件夹）",
                },
            },
            "required": ["path"],
        },
    },
    {
        "name": "search_files",
        "description": (
            "用正则表达式在文件内容中搜索，返回 文件:行号: 内容 的匹配列表。"
            "适合'找出所有包含某关键词的文件'这类任务。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": "正则表达式，如 合同、^# （匹配标题行）",
                },
                "path": {
                    "type": "string",
                    "description": "限定搜索范围的子文件夹或单个文件（相对授权文件夹），留空表示全部",
                },
            },
            "required": ["pattern"],
        },
    },
    {
        "name": "write_file",
        "description": (
            "把文本内容写入授权文件夹内的文件（会覆盖已有内容，父文件夹会自动创建）。"
            "生成报告、清单等结果文件时使用。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "要写入的文件路径（相对授权文件夹），如 report.md",
                },
                "content": {
                    "type": "string",
                    "description": "要写入的完整文本内容",
                },
            },
            "required": ["path", "content"],
        },
    },
]


def dispatch(sandbox, name, args):
    """根据模型选择的工具名，调用对应的沙盒方法"""
    if name == "list_files":
        return sandbox.list_files(
            args.get("path", ""),
            args.get("pattern", "*"),
            bool(args.get("recursive", False)),
        )
    if name == "read_file":
        return sandbox.read_file(args.get("path", ""))
    if name == "search_files":
        return sandbox.search_files(args.get("pattern", ""), args.get("path", ""))
    if name == "write_file":
        return sandbox.write_file(args.get("path", ""), args.get("content", ""))
    raise SandboxError("未知工具：%s" % name)


SYSTEM_PROMPT_TEMPLATE = """你是「本地文件管家」，一个帮用户管理文件的助手。
你当前被授权的文件夹是：{root}（你只能操作这个文件夹内的文件，无法访问其他任何位置）。

工作方式：
- 用户用自然语言下命令，你通过调用工具（list_files / read_file / search_files / write_file）完成任务
- 动手前先用 list_files 了解文件夹结构，不要凭空猜测文件名
- 需要生成的结果文件（报告、清单等）一律写在授权文件夹内
- 工具报错时，根据错误信息调整做法，不要反复用同样的参数重试
- 全部用中文交流；任务完成后，用两三句话总结你做了什么、结果文件在哪里
"""


def call_model(client, model, system, tools, messages):
    """调用模型，按"能力从高到低"逐级降级，兼容官方 API 和第三方兼容网关：
    1. 官方最新能力：adaptive thinking + refusal 自动兜底
    2. 普通端点 + adaptive thinking
    3. 最基础的调用（什么新参数都不带，兼容各类 Anthropic 兼容网关）
    """
    base = dict(model=model, max_tokens=16000, system=system, tools=tools, messages=messages)
    try:
        return client.beta.messages.create(
            thinking={"type": "adaptive"},
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            **base,
        )
    except (TypeError, anthropic.BadRequestError, anthropic.NotFoundError):
        pass
    try:
        return client.messages.create(thinking={"type": "adaptive"}, **base)
    except (TypeError, anthropic.BadRequestError, anthropic.NotFoundError):
        pass
    return client.messages.create(**base)


def run_agent(sandbox, command, model, verbose=True):
    """agent 主循环：发请求 → 执行模型选中的工具 → 把结果喂回去 → 直到模型给出最终答复"""
    client = anthropic.Anthropic()
    system = SYSTEM_PROMPT_TEMPLATE.format(root=sandbox.root)
    messages = [{"role": "user", "content": command}]

    response = None
    for _ in range(MAX_ITERATIONS):
        response = call_model(client, model, system, TOOLS, messages)

        # 安全策略拒绝（极少数情况）：提示用户，而不是当作普通回复
        if response.stop_reason == "refusal":
            return "模型出于安全策略拒绝了这次请求，请检查命令内容后重试。"

        # 打印模型这一轮想说的话（进度说明）
        for block in response.content:
            if block.type == "text" and block.text.strip() and verbose:
                print(block.text)

        tool_uses = [b for b in response.content if b.type == "tool_use"]

        # 模型没有再调用工具 → 任务结束
        if response.stop_reason == "end_turn" or not tool_uses:
            break

        # 把模型的完整回复（含 tool_use 块）记入对话历史
        messages.append({"role": "assistant", "content": response.content})

        # 逐个执行工具，收集结果
        tool_results = []
        for tu in tool_uses:
            args_preview = json.dumps(tu.input, ensure_ascii=False)
            if len(args_preview) > 100:
                args_preview = args_preview[:100] + "…"
            if verbose:
                print("  🔧 %s(%s)" % (tu.name, args_preview))
            try:
                result = dispatch(sandbox, tu.name, tu.input)
                tool_results.append(
                    {"type": "tool_result", "tool_use_id": tu.id, "content": result}
                )
            except (SandboxError, FileNotFoundError, OSError) as e:
                if verbose:
                    print("  ⚠️ %s" % e)
                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": tu.id,
                        "content": "错误：%s" % e,
                        "is_error": True,
                    }
                )

        messages.append({"role": "user", "content": tool_results})
    else:
        return "已达到最大执行轮数（%d），任务未完成。可以把命令拆得更小再试。" % MAX_ITERATIONS

    # 取最后一轮的文本回复作为最终答案
    final_text = ""
    if response is not None:
        final_text = "".join(b.text for b in response.content if b.type == "text")
    return final_text.strip() or "（模型没有返回文字说明）"


def main():
    parser = argparse.ArgumentParser(
        description="本地文件管家：授权一个文件夹，用自然语言命令管理里面的文件",
    )
    parser.add_argument("folder", help="授权的文件夹路径，如 ~/Documents/notes")
    parser.add_argument(
        "command",
        help="自然语言命令，如：把所有 md 文件提取标题汇总输出 report.md",
    )
    parser.add_argument(
        "--model",
        default=DEFAULT_MODEL,
        help="使用的模型（默认 %s，省钱可换 claude-sonnet-5）" % DEFAULT_MODEL,
    )
    args = parser.parse_args()

    if not os.environ.get("ANTHROPIC_API_KEY") and not os.environ.get("ANTHROPIC_AUTH_TOKEN"):
        print(
            "提示：没有检测到 ANTHROPIC_API_KEY 环境变量。\n"
            "请先设置再运行：export ANTHROPIC_API_KEY=你的key",
            file=sys.stderr,
        )

    try:
        sandbox = FileSandbox(args.folder)
    except FileNotFoundError as e:
        print("错误：%s" % e, file=sys.stderr)
        sys.exit(1)

    print("📁 授权文件夹：%s" % sandbox.root)
    print("🤖 模型：%s" % args.model)
    print("📝 命令：%s" % args.command)
    print("-" * 50)

    try:
        answer = run_agent(sandbox, args.command, args.model)
    except anthropic.AuthenticationError:
        print("错误：API key 无效或没有权限，请检查 ANTHROPIC_API_KEY。", file=sys.stderr)
        sys.exit(1)
    except anthropic.APIConnectionError:
        print("错误：无法连接 Anthropic API，请检查网络。", file=sys.stderr)
        sys.exit(1)
    except anthropic.RateLimitError:
        print("错误：请求太频繁被限流，请稍后再试。", file=sys.stderr)
        sys.exit(1)
    except anthropic.APIStatusError as e:
        print("错误：API 返回错误（%s）：%s" % (e.status_code, e.message), file=sys.stderr)
        sys.exit(1)

    print("-" * 50)
    print("✅ %s" % answer)


if __name__ == "__main__":
    main()
