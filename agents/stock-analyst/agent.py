#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
股票分析 Agent —— 基于 Anthropic function-call 的智能股票分析助手

输入股票代码，输出：
- 今日行情分析
- 技术指标分析（MACD/KDJ/RSI/均线）
- 资金流向分析
- 舆情分析（基于新闻）
- 综合评分与明日涨跌预测

用法：
    python3 agent.py <股票代码>

示例：
    python3 agent.py 600519      # A 股：贵州茅台
    python3 agent.py AAPL        # 美股：苹果

环境要求：
    1. 设置 ANTHROPIC_API_KEY 环境变量：
           export ANTHROPIC_API_KEY=你的key
    2. 安装 SDK：
           pip3 install -r requirements.txt
"""

import argparse
import json
import os
import sys

import anthropic

from data_fetcher import (
    fetch_realtime_quote,
    fetch_history_kline,
    fetch_fund_flow,
    fetch_sector_info,
    fetch_news,
    filter_relevant_news,
    analyze_news_sentiment,
    fetch_sector_performance,
)
from analyzer import (
    analyze_technical_indicators,
    calc_technical_score,
    calc_fund_score,
    calc_sentiment_score,
    calc_comprehensive_score,
    predict_tomorrow,
    calc_support_resistance,
    backtest_prediction,
)

# 默认模型
DEFAULT_MODEL = os.environ.get("STOCK_ANALYST_MODEL", "claude-opus-5")

MAX_ITERATIONS = 15  # 最大工具调用轮数


# ========== 工具定义 ==========

TOOLS = [
    {
        "name": "fetch_realtime_quote",
        "description": (
            "获取股票的实时行情数据，包括当前价格、涨跌幅、成交量、成交额、开盘价、最高价、最低价等。"
            "A 股和美股都支持。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "symbol": {
                    "type": "string",
                    "description": "股票代码，A股如 600519，美股如 AAPL",
                },
            },
            "required": ["symbol"],
        },
    },
    {
        "name": "fetch_history_kline",
        "description": (
            "获取股票的历史 K 线数据，用于计算技术指标。"
            "返回最近 N 个交易日的开盘价、收盘价、最高价、最低价、成交量。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "symbol": {
                    "type": "string",
                    "description": "股票代码",
                },
                "days": {
                    "type": "integer",
                    "description": "获取最近多少个交易日的数据，默认 60",
                },
            },
            "required": ["symbol"],
        },
    },
    {
        "name": "analyze_technical_indicators",
        "description": (
            "基于历史 K 线数据计算技术指标，包括 MACD、KDJ、RSI、移动均线、量比等。"
            "需要先调用 fetch_history_kline 获取数据。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "klines": {
                    "type": "array",
                    "description": "K 线数据数组，每项包含 date/open/high/low/close/volume",
                    "items": {
                        "type": "object",
                        "properties": {
                            "date": {"type": "string"},
                            "open": {"type": "number"},
                            "high": {"type": "number"},
                            "low": {"type": "number"},
                            "close": {"type": "number"},
                            "volume": {"type": "number"},
                        },
                    },
                },
            },
            "required": ["klines"],
        },
    },
    {
        "name": "fetch_fund_flow",
        "description": (
            "获取股票的资金流向数据，包括主力资金净流入等。"
            "A 股支持详细数据，美股暂不支持。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "symbol": {
                    "type": "string",
                    "description": "股票代码",
                },
            },
            "required": ["symbol"],
        },
    },
    {
        "name": "fetch_sector_info",
        "description": "获取股票所属板块信息。",
        "input_schema": {
            "type": "object",
            "properties": {
                "symbol": {
                    "type": "string",
                    "description": "股票代码",
                },
            },
            "required": ["symbol"],
        },
    },
    {
        "name": "fetch_news",
        "description": (
            "获取股票相关的新闻，用于舆情分析。"
            "返回新闻标题、来源、时间等信息。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "symbol": {
                    "type": "string",
                    "description": "股票代码",
                },
            },
            "required": ["symbol"],
        },
    },
    {
        "name": "calculate_comprehensive_score",
        "description": (
            "计算综合评分和明日涨跌预测。"
            "需要提供技术面评分（0-100）、资金面评分（0-100）、消息面评分（0-100）。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "tech_score": {
                    "type": "integer",
                    "description": "技术面评分（0-100）",
                },
                "fund_score": {
                    "type": "integer",
                    "description": "资金面评分（0-100）",
                },
                "sentiment_score": {
                    "type": "integer",
                    "description": "消息面评分（0-100）",
                },
            },
            "required": ["tech_score", "fund_score", "sentiment_score"],
        },
    },
    {
        "name": "calculate_support_resistance",
        "description": (
            "基于历史 K 线数据计算多级支撑位和阻力位。"
            "返回包括近期高低点、均线、布林带等多个维度的支撑阻力位。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "klines": {
                    "type": "array",
                    "description": "K 线数据数组",
                    "items": {
                        "type": "object",
                    },
                },
            },
            "required": ["klines"],
        },
    },
    {
        "name": "filter_relevant_news",
        "description": (
            "过滤新闻，只保留与股票相关的新闻。"
            "基于股票名称和代码进行关键词匹配。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "news": {
                    "type": "array",
                    "description": "原始新闻列表",
                    "items": {
                        "type": "object",
                    },
                },
                "stock_name": {
                    "type": "string",
                    "description": "股票名称",
                },
                "symbol": {
                    "type": "string",
                    "description": "股票代码",
                },
            },
            "required": ["news", "stock_name", "symbol"],
        },
    },
    {
        "name": "analyze_news_sentiment",
        "description": (
            "分析新闻情绪，返回情绪倾向和关键情绪词。"
            "基于正面/负面关键词统计。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "news": {
                    "type": "array",
                    "description": "新闻列表",
                    "items": {
                        "type": "object",
                    },
                },
            },
            "required": ["news"],
        },
    },
    {
        "name": "backtest_prediction",
        "description": (
            "历史预测回测，基于过去N天的数据验证预测准确率。"
            "返回准确率和详细回测记录。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "klines": {
                    "type": "array",
                    "description": "K 线数据数组",
                    "items": {
                        "type": "object",
                    },
                },
                "lookback": {
                    "type": "integer",
                    "description": "回测天数，默认 20",
                },
            },
            "required": ["klines"],
        },
    },
    {
        "name": "analyze_recent_trend",
        "description": (
            "分析股票过去N天的走势，包括趋势类型、强度、涨跌统计、波动率等。"
            "返回详细的走势分析和每日详情。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "klines": {
                    "type": "array",
                    "description": "K 线数据数组",
                    "items": {
                        "type": "object",
                    },
                },
                "days": {
                    "type": "integer",
                    "description": "分析天数，默认 15",
                },
            },
            "required": ["klines"],
        },
    },
]


# ========== 工具调度 ==========


def dispatch(name: str, args: dict) -> any:
    """根据工具名调用对应的函数"""
    if name == "fetch_realtime_quote":
        return fetch_realtime_quote(args["symbol"])
    elif name == "fetch_history_kline":
        return fetch_history_kline(args["symbol"], args.get("days", 60))
    elif name == "analyze_technical_indicators":
        return analyze_technical_indicators(args["klines"])
    elif name == "fetch_fund_flow":
        return fetch_fund_flow(args["symbol"])
    elif name == "fetch_sector_info":
        return fetch_sector_info(args["symbol"])
    elif name == "fetch_news":
        return fetch_news(args["symbol"])
    elif name == "calculate_comprehensive_score":
        tech = args["tech_score"]
        fund = args["fund_score"]
        sentiment = args["sentiment_score"]
        score = calc_comprehensive_score(tech, fund, sentiment)
        prediction = predict_tomorrow(score)
        return {
            "comprehensive_score": score,
            "prediction": prediction,
            "breakdown": {
                "tech_score": tech,
                "fund_score": fund,
                "sentiment_score": sentiment,
                "weights": "技术面40% + 资金面35% + 消息面25%"
            }
        }
    elif name == "calculate_support_resistance":
        return calc_support_resistance(args["klines"])
    elif name == "filter_relevant_news":
        return filter_relevant_news(args["news"], args["stock_name"], args["symbol"])
    elif name == "analyze_news_sentiment":
        return analyze_news_sentiment(args["news"])
    elif name == "backtest_prediction":
        return backtest_prediction(args["klines"], args.get("lookback", 20))
    elif name == "analyze_recent_trend":
        return analyze_recent_trend(args["klines"], args.get("days", 15))
    else:
        return {"error": f"未知工具：{name}"}


# ========== 系统提示词 ==========

SYSTEM_PROMPT = """你是「股票分析助手」，一个专业的股票技术分析师。

你的任务是分析用户给出的股票代码，输出完整的分析报告，包括：

1. **今日行情**：当前价格、涨跌幅、成交量等基础数据
2. **技术指标分析**：
   - MACD（趋势判断）
   - KDJ（超买超卖 + 金叉死叉连续性）
   - RSI（强弱指标）
   - 均线系统（MA5/10/20/60/120/250）
   - 量比分析
   - 布林带（Bollinger Bands）：上轨/中轨/下轨/带宽/%B
   - ATR（平均真实波幅）：波动率评估
   - VWAP（成交量加权平均价）：价格偏离度
3. **资金分析**：主力资金流向（A股）、成交量变化
4. **舆情分析**：基于相关新闻判断市场情绪（过滤不相关新闻）
5. **综合评分**：技术面（40%）+ 资金面（35%）+ 消息面（25%）
6. **支撑阻力位**：多级支撑阻力（近期高低点 + 均线 + 布林带）
7. **过去15天走势分析**：趋势类型、强度、涨跌统计、波动率、成交量趋势
8. **明日预测**：涨跌倾向、概率
9. **历史回测**：预测准确率统计（使用多维度预测逻辑）

工作流程：
1. 先调用 fetch_realtime_quote 获取实时行情
2. 调用 fetch_history_kline 获取历史数据（建议 120 天以上）
3. 调用 analyze_technical_indicators 计算技术指标
4. 调用 fetch_fund_flow 获取资金流向
5. 调用 fetch_news 获取新闻
6. 调用 filter_relevant_news 过滤不相关新闻
7. 调用 analyze_news_sentiment 分析新闻情绪
8. 根据以上数据，给出技术面、资金面、消息面评分
9. 调用 calculate_comprehensive_score 计算综合评分
10. 调用 calculate_support_resistance 计算多级支撑/阻力位
11. 调用 analyze_recent_trend 分析过去15天走势
12. 调用 backtest_prediction 进行历史回测（使用多维度预测逻辑）
13. 输出完整的分析报告

评分标准：
- 技术面（40%）：MACD、KDJ、RSI、均线、布林带、ATR
- 资金面（35%）：主力资金净流入/流出金额
- 消息面（25%）：新闻情绪分析
- 消息面：新闻情绪（正面/中性/负面）

注意事项：
- 所有分析仅供参考，不构成投资建议
- 必须在报告末尾添加风险提示
- 用中文输出，格式清晰美观
- 如果数据获取失败，说明原因并基于可用数据分析
"""


# ========== 模型调用（兼容降级）==========


def call_model(client, model, system, tools, messages):
    """
    调用模型，按"能力从高到低"逐级降级，兼容官方 API 和第三方兼容网关
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


# ========== 主循环 ==========


def run_agent(symbol: str, model: str, verbose: bool = True) -> str:
    """agent 主循环"""
    client = anthropic.Anthropic()
    system = SYSTEM_PROMPT
    messages = [{"role": "user", "content": f"请分析股票 {symbol}，给出完整的分析报告。"}]

    response = None
    for iteration in range(MAX_ITERATIONS):
        response = call_model(client, model, system, TOOLS, messages)

        # 安全策略拒绝
        if response.stop_reason == "refusal":
            return "模型出于安全策略拒绝了这次请求，请检查股票代码后重试。"

        # 打印模型的回复
        for block in response.content:
            if block.type == "text" and block.text.strip() and verbose:
                print(block.text)

        tool_uses = [b for b in response.content if b.type == "tool_use"]

        # 没有工具调用，任务完成
        if response.stop_reason == "end_turn" or not tool_uses:
            break

        # 记录模型回复
        messages.append({"role": "assistant", "content": response.content})

        # 执行工具
        tool_results = []
        for tu in tool_uses:
            args_preview = json.dumps(tu.input, ensure_ascii=False)
            if len(args_preview) > 100:
                args_preview = args_preview[:100] + "…"
            if verbose:
                print(f"  🔧 {tu.name}({args_preview})")

            try:
                result = dispatch(tu.name, tu.input)
                # 截断过长的结果
                result_str = json.dumps(result, ensure_ascii=False)
                if len(result_str) > 50000:
                    result_str = result_str[:50000] + "\n……（数据过长，已截断）"
                tool_results.append(
                    {"type": "tool_result", "tool_use_id": tu.id, "content": result_str}
                )
            except Exception as e:
                if verbose:
                    print(f"  ⚠️ {e}")
                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": tu.id,
                        "content": f"错误：{e}",
                        "is_error": True,
                    }
                )

        messages.append({"role": "user", "content": tool_results})
    else:
        return f"已达到最大执行轮数（{MAX_ITERATIONS}），任务未完成。"

    # 最终答案
    final_text = ""
    if response is not None:
        final_text = "".join(b.text for b in response.content if b.type == "text")
    return final_text.strip() or "（模型没有返回分析结果）"


# ========== 入口 ==========


def main():
    parser = argparse.ArgumentParser(
        description="股票分析 Agent：输入股票代码，输出完整分析报告",
    )
    parser.add_argument("symbol", help="股票代码，如 600519（A股）、AAPL（美股）")
    parser.add_argument(
        "--model",
        default=DEFAULT_MODEL,
        help=f"使用的模型（默认 {DEFAULT_MODEL}）",
    )
    args = parser.parse_args()

    if not os.environ.get("ANTHROPIC_API_KEY") and not os.environ.get("ANTHROPIC_AUTH_TOKEN"):
        print(
            "提示：没有检测到 ANTHROPIC_API_KEY 环境变量。\n"
            "请先设置再运行：export ANTHROPIC_API_KEY=你的key",
            file=sys.stderr,
        )

    symbol = args.symbol.upper() if args.symbol.isalpha() else args.symbol

    print(f"📊 股票代码：{symbol}")
    print(f"🤖 模型：{args.model}")
    print("-" * 50)

    try:
        answer = run_agent(symbol, args.model)
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
        print(f"错误：API 返回错误（{e.status_code}）：{e.message}", file=sys.stderr)
        sys.exit(1)

    print("-" * 50)
    print(f"✅ 分析完成")


if __name__ == "__main__":
    main()
