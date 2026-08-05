#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
sync_agents.py — 自动同步 README.md 中的 Agent 列表

工作方式：
1. 扫描顶层 agents/ 目录下的每个子目录（一个子目录 = 一个 Agent）
2. 读取每个 Agent 的介绍文件 agents/<目录名>/README.md：
   - 优先提取 frontmatter 里的 name / description 字段
   - 没有 frontmatter 时，自动解析正文的第一个 # 大标题和第一段正文
3. 重新生成 README.md 里 AGENTS:START 与 AGENTS:END 标记之间的列表内容
4. 只有内容真正变化时才写回文件，避免产生无意义的改动

触发方式（见 .claude/settings.json 中的 hook 配置）：
- PostToolUse hook：Claude Code 写入或修改 agents/**/README.md 后自动运行
- SessionStart hook：每次会话开始时自动运行一次（兜底，覆盖手动增删的情况）

也可以随时手动运行：
    python3 scripts/sync_agents.py
"""

import json
import os
import re
import sys
from urllib.parse import quote

# README 中自动维护区块的边界标记（请勿修改 README 里的这两行）
START_MARK = "<!-- AGENTS:START -->"
END_MARK = "<!-- AGENTS:END -->"

# 如果 README.md 意外丢失，会用这个最小模板自动重建
README_TEMPLATE = """# Agent Hub

> 我的私人 Agent 代码仓库。

## Agent 列表

{placeholder}
"""

# 列表中描述的展示长度上限（保持表格整洁）
DESC_MAX_LEN = 100


def warn(msg):
    """把警告输出到标准错误（不会干扰 hook，也不会写进 Claude 的上下文）"""
    print(f"[sync_agents] {msg}", file=sys.stderr)


def project_root():
    """项目根目录：优先用 Claude Code 注入的环境变量，手动运行时按脚本位置推算"""
    root = os.environ.get("CLAUDE_PROJECT_DIR")
    if root:
        return os.path.abspath(root)
    # 脚本位于 <根目录>/scripts/ 下，向上一级就是项目根目录
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def split_frontmatter(text):
    """把文件拆成 (frontmatter 行列表, 正文)；没有 frontmatter 时返回 ([], 原文)"""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return [], text
    for idx in range(1, len(lines)):
        if lines[idx].strip() in ("---", "..."):
            return lines[1:idx], "\n".join(lines[idx + 1:])
    # 没有找到结尾的 ---，视为没有 frontmatter
    return [], text


def parse_frontmatter(fm_lines):
    """解析 frontmatter（简单的 key: value 形式，支持 | / > 多行块）"""
    meta = {}
    i = 0
    while i < len(fm_lines):
        line = fm_lines[i]
        m = re.match(r"^([A-Za-z_][A-Za-z0-9_-]*)\s*:\s*(.*)$", line)
        if not m:
            i += 1
            continue
        key = m.group(1).lower()
        value = m.group(2).strip()
        if value in ("|", ">", "|-", ">-", "|+", ">+"):
            # 多行文本块写法：收集后续缩进的行
            block = []
            i += 1
            while i < len(fm_lines):
                cur = fm_lines[i]
                if cur.strip() == "":
                    block.append("")
                    i += 1
                    continue
                if cur[0] not in (" ", "\t"):
                    break
                block.append(cur.strip())
                i += 1
            joiner = "\n" if value[0] == "|" else " "
            meta[key] = joiner.join(block).strip()
            continue
        # 去掉成对的引号
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        meta[key] = value
        i += 1
    return meta


def extract_from_body(body):
    """从正文提取 (第一个 # 大标题, 第一段正文)；找不到则为 None"""
    title = None
    for line in body.splitlines():
        m = re.match(r"^#\s+(.+?)\s*$", line.strip())
        if m:
            title = m.group(1).strip()
            break
    # 第一段非空、非标题的连续文字
    para = []
    started = False
    for raw in body.splitlines():
        s = raw.strip()
        if not s:
            if started:
                break
            continue
        if s.startswith("#"):
            if started:
                break
            continue
        started = True
        para.append(s)
    desc = " ".join(para) if para else None
    return title, desc


def clean_inline(text):
    """清理不适合放进列表表格的 Markdown：去掉图片，链接只保留文字"""
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", text)      # 移除图片
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)  # 链接 → 链接文字
    return re.sub(r"\s+", " ", text).strip()


def truncate(text, limit=DESC_MAX_LEN):
    """超长描述截断，保持列表整洁"""
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "……"


def collect_agents(root):
    """扫描 agents/ 下的一级子目录，返回每个 agent 的信息列表"""
    agents_dir = os.path.join(root, "agents")
    agents = []
    if not os.path.isdir(agents_dir):
        return agents
    for entry in sorted(os.listdir(agents_dir)):
        if entry.startswith("."):
            continue
        subdir = os.path.join(agents_dir, entry)
        if not os.path.isdir(subdir):
            continue
        readme_path = os.path.join(subdir, "README.md")
        # 没有介绍文件：仍然列出这个 agent，但标注缺失
        if not os.path.isfile(readme_path):
            agents.append({
                "name": entry,
                "dir": entry,
                "description": "（缺少 README.md 介绍文件）",
            })
            continue
        try:
            with open(readme_path, "r", encoding="utf-8") as f:
                text = f.read()
        except OSError as e:
            warn(f"读取介绍文件失败，已跳过：agents/{entry}/README.md（{e}）")
            continue
        fm_lines, body = split_frontmatter(text)
        meta = parse_frontmatter(fm_lines)
        body_title, body_desc = extract_from_body(body)
        # 名称优先级：frontmatter.name > 正文大标题 > 目录名
        name = meta.get("name") or body_title or entry
        # 描述优先级：frontmatter.description > 正文第一段 > 占位
        raw_desc = meta.get("description") or body_desc or "（暂无描述）"
        agents.append({
            "name": name,
            "dir": entry,
            "description": truncate(clean_inline(raw_desc)),
        })
    agents.sort(key=lambda a: a["name"].lower())
    return agents


def cell(text):
    """表格单元格转义：竖线和换行会破坏 Markdown 表格"""
    return str(text).replace("|", "\\|").replace("\r", " ").replace("\n", " ").strip()


def link_text(name):
    """链接文字：去掉会破坏 Markdown 链接结构的方括号"""
    return name.replace("[", "").replace("]", "").replace("|", "\\|")


def render_section(agents):
    """生成 README 中自动维护区块的完整内容（含边界标记）"""
    lines = [
        START_MARK,
        "",
        "> 本区块由 `scripts/sync_agents.py` 自动维护，请勿手动编辑。",
        "",
    ]
    if not agents:
        lines.append(
            "暂无 Agent。在 `agents/` 目录下创建 agent 文件夹（内含 README.md 介绍）后，这里会自动列出。"
        )
    else:
        lines.append(f"共 {len(agents)} 个 Agent：")
        lines.append("")
        lines.append("| Agent | 描述 |")
        lines.append("| --- | --- |")
        for a in agents:
            target = "agents/" + quote(a["dir"]) + "/"
            lines.append(
                f"| [{link_text(a['name'])}]({target}) | {cell(a['description'])} |"
            )
    lines += ["", END_MARK]
    return "\n".join(lines)


def main():
    root = project_root()

    # 读取 hook 通过标准输入传入的 JSON（手动运行时可能没有输入）
    hook_event = None
    try:
        if not sys.stdin.isatty():
            hook_event = json.load(sys.stdin)
    except (ValueError, OSError):
        hook_event = None

    # 快速跳过：如果是文件写入事件，但改动的不是 agents 下的 README.md，直接退出
    if isinstance(hook_event, dict) and hook_event.get("tool_name") in (
        "Write", "Edit", "MultiEdit",
    ):
        file_path = (hook_event.get("tool_input") or {}).get("file_path") or ""
        agents_dir = os.path.join(root, "agents") + os.sep
        target = os.path.abspath(os.path.expanduser(file_path)) if file_path else ""
        base = os.path.basename(target)
        if not (target.startswith(agents_dir) and base.lower() == "readme.md"):
            return 0

    # 读取 README.md；如果文件丢失则按模板重建
    readme_path = os.path.join(root, "README.md")
    try:
        with open(readme_path, "r", encoding="utf-8") as f:
            readme = f.read()
    except OSError:
        warn("未找到 README.md，已按模板重建")
        readme = README_TEMPLATE.replace(
            "{placeholder}", START_MARK + "\n\n" + END_MARK
        )

    # 检查边界标记是否存在且顺序正确
    if (
        START_MARK not in readme
        or END_MARK not in readme
        or readme.index(START_MARK) > readme.index(END_MARK)
    ):
        warn("README.md 中缺少 AGENTS 标记区块，本次跳过更新（请检查 README 是否完整）")
        return 0

    agents = collect_agents(root)
    section = render_section(agents)
    pattern = re.compile(
        re.escape(START_MARK) + r".*?" + re.escape(END_MARK), re.DOTALL
    )
    new_readme = pattern.sub(lambda _m: section, readme, count=1)

    # 只在内容真正变化时写回，避免无意义的文件改动
    if new_readme != readme:
        try:
            with open(readme_path, "w", encoding="utf-8") as f:
                f.write(new_readme)
        except OSError as e:
            warn(f"写入 README.md 失败：{e}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
