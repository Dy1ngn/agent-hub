---
name: file-butler
description: 本地文件管家：授权一个文件夹后，用自然语言命令让它列出/读取/搜索/写入其中的文件，自动完成整理、汇总、生成报告等任务。基于 Claude function-call，不使用向量库。
---

# 本地文件管家（file-butler）

一个帮你管理本地文件的 agent。你**授权一个文件夹**给它，然后用一句自然语言下命令，它会自己决定调用哪些文件工具来完成任务——不需要你写脚本、记命令。

## 它能做什么

- **列目录**：看清文件夹里有什么
- **读文件**：读取文本文件内容
- **搜内容**：用关键词/正则在所有文件里找匹配
- **写文件**：生成报告、清单等结果文件

典型命令示例：

- 「把这个文件夹下所有 md 文件，提取标题汇总输出 report.md」
- 「找出所有提到『合同』的文件，列一个清单保存为 清单.txt」
- 「把 notes 文件夹里的文件按类型统计一下数量，写成 统计.md」

## 怎么用

```bash
# 1. 设置 API key（只需一次，建议写进 ~/.zshrc）
export ANTHROPIC_API_KEY=你的key

# 2. 安装依赖
pip3 install -r requirements.txt

# 3. 运行：第一个参数是授权的文件夹，第二个参数是命令
python3 agent.py ~/Documents/notes "把所有 md 文件提取标题汇总输出 report.md"
```

想省点费用可以换模型：`python3 agent.py 文件夹 "命令" --model claude-sonnet-5`

## 实现方式

基于 **Anthropic function-call（tool use）**：把「列目录/读文件/搜索/写文件」封装成 4 个工具，Claude 读懂你的命令后自主决定调用顺序，循环执行直到完成。**不使用向量库**，全部是直接的本地文件操作。

## 安全说明

所有文件操作被**严格限制在你授权的文件夹内**——它会校验每个路径，拒绝任何试图逃出该文件夹的访问（包括 `../` 和符号链接）。它看不到、也碰不到你电脑上的其他文件。

## 文件清单

- `agent.py` — agent 主程序（工具封装 + function-call 循环）
- `requirements.txt` — Python 依赖
