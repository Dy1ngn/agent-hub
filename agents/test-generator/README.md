---
name: test-generator
description: 用 ast 分析被测函数签名与异常，生成 pytest/unittest 测试骨架与用例清单，可选模型补全断言。
---

# 单元测试生成助手（test-generator）

一个本地命令行测试生成 agent：先用 `ast` 把被测模块拆开看清楚（函数/方法签名、docstring 的 Args/Returns/Raises、`raise` 的异常、`yield`/`async` 特性、类与导入、被测函数之间的调用关系），据此确定性地规划用例并生成能直接运行的 pytest / unittest 测试骨架；再可选让模型补全真实断言与测试数据，合并进骨架（模型输出解析失败就退回纯骨架，仍然语法正确可运行）。除 `anthropic` 外只用标准库。

## 它能做什么

- **ast 静态分析**：`extract_params()` 拿到参数名、类型注解、默认值、是否必填；`parse_docstring()` 解析 Google / Sphinx 两种风格的 Args、Returns、Raises；`extract_raises()` 抓函数体里的 `raise` 与 `except`；`has_yield()` / `is_async` 认出生成器与协程；`ModuleInfo` 汇总类、方法、导入与调用关系
- **用例规划**：每个被测对象固定产出四类用例——典型路径、边界值（空/0/None/空串/空列表，按类型注解挑）、异常路径（`raise` 了什么就测什么）、返回值类型断言；自动生成「用例清单」表格（用例名/类型/断言点/所属对象）
- **测试骨架生成**：`tests/test_<模块>.py`，含 `sys.path` 注入、被测模块 import、`pytest.raises` / `assertRaises` 异常用例、`pytest.mark.parametrize` 参数化占位（unittest 模式用循环替代）、中文 docstring 用例名；`async` 函数自动包一层 `asyncio.run`，生成器自动 `list()` 包装
- **两种框架**：`--framework pytest`（默认）或 `--framework unittest`（生成 `TestCase` 类，文件尾带 `unittest.main()`，可以直接 `python3 test_x.py` 跑）
- **语法自检**：生成的测试文件先过一遍 `ast.parse`，不合法直接报警告，不会静默写坏文件
- **已有测试检测**：目标目录已有同名 `test_<模块>.py` 时默认跳过并提示，加 `--force` 才覆盖
- **模型补全**：把骨架 + 签名交给模型，要求返回 JSON `{"tests":[{"name":"","code":"","reason":""}]}`；`merge_model_tests()` 按名字替换骨架里的用例（自动对齐缩进），解析失败或合并后语法不合法就退回纯骨架，原文进报告附录
- **可选跑测试**：`--run` 用 subprocess 跑一遍（带超时保护），解析通过/失败/错误/跳过数量写进报告
- **离线自检**：`--self-test` 在临时目录造样本，跑通全部确定性逻辑，不联网也不需要 API key

## 怎么用

### 第一步：安装依赖

```powershell
# Windows PowerShell
cd agents/test-generator
py -3 -m pip install -r requirements.txt
```

```bash
# macOS / Linux
cd agents/test-generator
python3 -m pip install -r requirements.txt
```

想用 `--framework pytest` 跑测试的话再装一下 pytest：`python3 -m pip install pytest`。

### 第二步：设置 API key（只在要模型补全断言时需要）

```powershell
# Windows PowerShell（当前窗口有效）
$env:ANTHROPIC_API_KEY="你的key"
```

```bash
# macOS / Linux
export ANTHROPIC_API_KEY=你的key
```

走第三方 Anthropic 兼容网关时，用 `--model` 指定网关侧的模型名即可，例如 `--model qwen3.8-max`。
只想生成骨架的话跳过这一步，直接加 `--offline`。

### 第三步：生成测试

```powershell
# Windows PowerShell
py -3 agent.py .\src --output-dir .\tests --offline
```

```bash
# macOS / Linux
python3 agent.py ./src --output-dir tests --output 测试计划.md
```

生成的测试文件在 `--output-dir`（默认 `tests/`），测试计划报告默认写到 `test-plan-report.md`。

## 常用示例

```bash
# 1) 给整个 src 目录生成 pytest 骨架，纯离线、不花钱
python3 agent.py ./src --output-dir tests --offline

# 2) 只测公开接口，用 unittest 框架（不依赖 pytest 也能跑）
python3 agent.py ./src --framework unittest --coverage 公开函数

# 3) 生成后立刻跑一遍，把通过/失败统计写进报告
python3 agent.py ./src --framework unittest --run --output 测试报告.md

# 4) 走第三方兼容网关让模型补全真实断言，并覆盖已有测试文件
python3 agent.py ./src --model qwen3.8-max --force
```

## 实现方式

1. **收集模块**：`collect_modules()` 递归找 `.py`（跳过 `tests`、`.git`、`__pycache__` 等目录和 `test_*.py`），语法错误的模块记备注并跳过。
2. **ast 分析**：`ModuleInfo` 调用 `extract_params()` / `parse_docstring()` / `extract_raises()` / `extract_calls()` / `has_yield()`，产出 `FunctionInfo`、`ClassInfo`，并记录函数之间的调用关系（`call_graph`）。
3. **用例规划**：`plan_cases()` 按 典型路径 / 边界值 / 异常路径 / 返回值类型 四类铺开，`placeholder_value()` 给正常输入占位，`boundary_value()` 给空值/零值，`--coverage` 控制是否包含下划线开头的私有函数。
4. **骨架生成**：`render_test_file()` 分组生成测试类，`render_case_body()` 生成每条用例的函数体（异常路径用 `pytest.raises` / `assertRaises`，边界值用 `try/except` 跳过策略，async/生成器自动包装），`import_target_lines()` 负责 `sys.path` 注入与被测模块导入（模块名不合法时自动改用 `importlib`）。
5. **模型补全**：`generate` 部分把骨架交给 `call_model()`（三级降级，兼容官方 API 与第三方网关），`parse_tests_json()` 兜底解析，`merge_model_tests()` 按名字替换并保持缩进，合并后 `ast.parse` 复检，任何一步失败都退回纯骨架。
6. **运行与报告**：`run_tests()` 用 subprocess 跑测试（超时保护、缺框架时给中文提示），`parse_test_output()` 解析 pytest/unittest 两种输出，`build_report()` 生成中文 Markdown（概览、被测对象清单、用例清单、覆盖率计划、生成文件、运行结果）。

## 文件清单

- `agent.py` —— 全部实现：ast 分析、用例规划、骨架生成、模型补全与合并、运行统计、报告生成、离线自检
- `README.md` —— 本说明文件
- `requirements.txt` —— 依赖清单（只有 anthropic SDK）

## 常见问题

**Q：不给 API key 能用吗？**
A：能。`--offline` 就是纯确定性模式：ast 分析、用例规划、骨架生成、报告全部本地完成，生成的测试文件语法正确、可直接运行。不加 `--offline` 又没 key 时会打印中文提示并退出。

**Q：生成的测试断言看起来很“骨架”，能直接用吗？**
A：骨架里的断言是按返回值类型注解自动生成的（`isinstance(result, float)` 这类），边界值用例允许抛 `TypeError/ValueError` 后跳过。想要真实业务断言，去掉 `--offline` 让模型补全，或自己改测试数据——报告的「用例清单」已经写明每条用例该断言什么。

**Q：为什么 pytest 模式下有的用例被 skip 了？**
A：边界值与参数化占位用例在输入导致 `TypeError/ValueError` 时会主动 `pytest.skip`，因为精确预期需要业务语义，模型补全或人工确认后再收紧。

**Q：我的模块放在包里（`src/pkg/mod.py`），能 import 对吗？**
A：能。生成的测试文件用 `os.path.relpath` 算出被测模块目录并注入 `sys.path`，所以 `tests/` 放在项目根、被测模块在任意子目录都能正确 import；模块名不是合法标识符时会自动改用 `importlib` 按文件路径加载。

**Q：`--run` 报“测试框架不可用”怎么办？**
A：pytest 需要单独安装（`python3 -m pip install pytest`）；不想装就用 `--framework unittest`，unittest 是标准库自带的，生成的文件末尾有 `unittest.main()`，也可以直接 `python3 tests/test_x.py` 运行。

**Q：已有 tests/ 目录会被覆盖吗？**
A：默认不会。发现同名 `test_<模块>.py` 会跳过并在报告里标注，确认要覆盖时加 `--force`。
