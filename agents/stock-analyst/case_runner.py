#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
股票分析 Agent —— 评测入口（源码模式，AEP 内置智能体）

平台每个用例起一个独立进程跑本文件（不再依赖本地 HTTP API / api.py）：
    python3 case_runner.py --case <用例JSON> --output <输出目录>/report.json [--mode rule|agent]

用例输入（AEP 用例 JSON 的 input 字段）：
    content : 股票代码（如 600519 / AAPL），或 JSON：{"symbol": "...", "days": 60, "seed": 7,
              "trend": "up|down|sideways|volatile", "sentiment": "正面|中性|负面", "fund_score": 60}
    params  : 与上面 JSON 同义的字段，优先级更高；params.mode = rule（默认）| agent

模式：
    rule  —— 纯确定性分析（analyzer.py 源码直接调用，K 线由 seed/trend 生成或用例自带），不调模型
    agent —— 走 agent.py 的 function-call 循环（模型 + 工具），行情/新闻数据用用例夹具注入

输出（平台契约）：{ findings: [...], suggestions: [...], summary: "Markdown 报告" }
findings 定位约定（与历史 API 模式一致，金标可按序号 ±容差 匹配）：
    file = 股票代码，line = 信号序号（固定顺序），category = 信号分组
    （ma/macd/kdj/rsi/bollinger/volume/atr/vwap/trend/prediction/sentiment）
"""

import argparse
import json
import os
import re
import random
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))

RISK_NOTE = "所有分析仅供参考，不构成投资建议。股票预测本质是概率分析，无法保证准确。"


def ensure_utf8():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass


# ---------- 夹具数据 ----------

def build_klines(seed=42, trend="sideways", days=120):
    """确定性合成 K 线：同 seed+trend 永远同一份数据（评测可复现）"""
    rnd = random.Random(seed)
    klines = []
    price = 100.0
    drift = {"up": 0.006, "down": -0.006, "sideways": 0.0, "volatile": 0.0}.get(trend, 0.0)
    vol_scale = 3.0 if trend == "volatile" else 1.5
    for i in range(days):
        change = rnd.gauss(drift * 100, vol_scale)
        price = max(5.0, price * (1 + change / 100))
        open_p = round(price * (1 - rnd.uniform(0, 0.01)), 2)
        close_p = round(price, 2)
        klines.append({
            "date": "2026-%02d-%02d" % (1 + (i // 28), 1 + (i % 28)),
            "open": open_p,
            "high": round(max(open_p, close_p) * (1 + rnd.uniform(0, 0.02)), 2),
            "low": round(min(open_p, close_p) * (1 - rnd.uniform(0, 0.02)), 2),
            "close": close_p,
            "volume": int(rnd.uniform(1_000_000, 5_000_000)),
        })
    return klines


def fixture_bundle(symbol, klines, sentiment, seed):
    """agent 模式用的工具夹具（模型只消费 JSON，形状自由）"""
    last = klines[-1] if klines else {}
    return {
        "quote": {
            "symbol": symbol,
            "name": "示例股票",
            "price": last.get("close", 0),
            "change_pct": round((last.get("close", 0) - klines[-2]["close"]) / klines[-2]["close"] * 100, 2) if len(klines) > 1 else 0,
            "volume": last.get("volume", 0),
            "date": last.get("date", ""),
        },
        "klines": klines,
        "fund_flow": {
            "main_net_inflow": (seed % 7 - 3) * 1e7,
            "main_net_inflow_pct": round((seed % 17 - 8) / 10, 2),
            "retail_net_inflow": (seed % 5 - 2) * 1e7,
        },
        "news": [
            {"title": "%s 发布季度业绩预告" % symbol, "time": "2026-10-20", "sentiment": sentiment},
            {"title": "%s 所在行业政策面平稳" % symbol, "time": "2026-10-18", "sentiment": "中性"},
            {"title": "%s 机构调研热度上升" % symbol, "time": "2026-10-15", "sentiment": sentiment},
        ],
        "sector": {"name": "示例板块", "change_pct": round((seed % 9) - 4, 2)},
    }


def clean_agent_report(text):
    """去掉模型误输出的工具调用标记（<function_calls>/<invoke>/<parameter> 等），只留报告正文"""
    if not isinstance(text, str):
        text = str(text or "")
    cut = re.search(r"<(function_calls|invoke|parameter)\b", text)
    if cut:
        text = text[:cut.start()]
    lines = [ln for ln in text.splitlines() if not ln.lstrip().startswith("<")]
    return "\n".join(lines).strip()


# ---------- 规则模式（确定性源码分析） ----------

def severity_of(text):
    if any(k in text for k in ["风险", "超买", "超卖", "背离", "高波动", "负面", "看空", "看跌", "暴跌", "回落", "危机", "空头", "偏空"]):
        return "high"
    if any(k in text for k in ["压力", "注意", "谨慎", "中性", "支撑", "分歧", "放缓", "震荡", "观望", "交织"]):
        return "medium"
    return "low"


def rule_analysis(symbol, klines, sentiment="中性", fund_score=50):
    """直接调用 analyzer.py 的源码做确定性分析，返回 (findings, suggestions, 摘要段落)"""
    from analyzer import (
        analyze_recent_trend,
        analyze_technical_indicators,
        backtest_prediction,
        calc_comprehensive_score,
        calc_sentiment_score,
        calc_support_resistance,
        calc_technical_score,
        predict_tomorrow,
    )

    if not isinstance(klines, list) or len(klines) < 30:
        return [], [], {"error": "历史数据不足（需要至少30个交易日），已跳过技术分析"}

    indicators = analyze_technical_indicators(klines)
    if isinstance(indicators, dict) and "error" in indicators:
        return [], [], {"error": indicators["error"]}

    trend = analyze_recent_trend(klines, days=min(15, len(klines) - 1))
    sr = calc_support_resistance(klines)
    backtest = backtest_prediction(klines, lookback=min(20, len(klines) // 3))

    tech_score = calc_technical_score(indicators)
    sentiment_score = calc_sentiment_score(sentiment)
    fund_score = max(0, min(100, int(fund_score)))
    total = calc_comprehensive_score(tech_score, fund_score, sentiment_score)
    prediction = predict_tomorrow(total)

    signals = [
        ("ma", "MA", [indicators.get("ma", {}).get("trend"), indicators.get("ma", {}).get("long_trend")]),
        ("macd", "MACD", [indicators.get("macd", {}).get("trend")]),
        ("kdj", "KDJ", [indicators.get("kdj", {}).get("zone"), indicators.get("kdj_cross", {}).get("signal")]),
        ("rsi", "RSI", [indicators.get("rsi", {}).get("zone")]),
        ("bollinger", "BOLL", [indicators.get("bollinger", {}).get("position")]),
        ("volume", "量比", [indicators.get("volume_ratio", {}).get("interpretation")]),
        ("atr", "ATR", [indicators.get("atr", {}).get("volatility")]),
        ("vwap", "VWAP", [indicators.get("vwap", {}).get("signal")]),
        ("trend", "近期走势", [trend.get("trend_type"), trend.get("trend_strength")]),
        ("prediction", "预测", ["%s，上涨概率 %s%%" % (prediction.get("tendency"), prediction.get("up_probability"))]),
        ("sentiment", "消息面", ["%s（%s 分）" % (sentiment, sentiment_score)]),
    ]

    findings = []
    for index, (category, label, parts) in enumerate(signals, start=1):
        text = "，".join([str(p) for p in parts if p])
        if not text:
            continue
        findings.append({
            "file": symbol,
            "line": index,
            "category": category,
            "severity": severity_of(text),
            "title": "%s：%s" % (label, text),
        })

    suggestions = []
    support = sr.get("support") or []
    resistance = sr.get("resistance") or []
    if support:
        suggestions.append("下方支撑位参考 %s" % "、".join(["%.2f" % s.get("price", 0) for s in support[:2]]))
    if resistance:
        suggestions.append("上方压力位参考 %s" % "、".join(["%.2f" % s.get("price", 0) for s in resistance[:2]]))
    if backtest.get("accuracy") is not None:
        suggestions.append("近 %s 次回测准确率 %s%%，预测结论请结合仓位控制" % (backtest.get("total"), backtest.get("accuracy")))
    suggestions.append(RISK_NOTE)

    segments = {
        "scores": {"tech": tech_score, "fund": fund_score, "sentiment": sentiment_score, "total": total},
        "prediction": prediction,
        "trend": trend,
        "support_resistance": sr,
        "backtest": backtest,
        "indicators": indicators,
    }
    return findings, suggestions, segments


def render_summary(symbol, findings, suggestions, segments):
    if "error" in segments:
        return "# 股票分析报告：%s\n\n## 结论\n\n%s\n\n## 风险提示\n\n%s\n" % (symbol, segments["error"], RISK_NOTE)
    scores = segments["scores"]
    pred = segments["prediction"]
    trend = segments["trend"]
    sr = segments["support_resistance"]
    bt = segments["backtest"]
    ind = segments["indicators"]
    lines = [
        "# 股票分析报告：%s" % symbol,
        "",
        "## 行情概览",
        "",
        "- 近期走势：%s（%s，%s）" % (trend.get("trend_type", "—"), trend.get("trend_strength", "—"), trend.get("price_change_pct", "—")),
        "- 当前价格：%.2f" % sr.get("current_price", 0),
        "",
        "## 技术指标",
        "",
        "- MA：%s / %s" % (ind.get("ma", {}).get("trend", "—"), ind.get("ma", {}).get("long_trend", "—")),
        "- MACD：%s" % ind.get("macd", {}).get("trend", "—"),
        "- KDJ：%s（%s）" % (ind.get("kdj", {}).get("zone", "—"), ind.get("kdj_cross", {}).get("signal", "—")),
        "- RSI：%s" % ind.get("rsi", {}).get("zone", "—"),
        "- 布林：%s" % ind.get("bollinger", {}).get("position", "—"),
        "- 量比：%s" % ind.get("volume_ratio", {}).get("interpretation", "—"),
        "- 波动：%s / %s" % (ind.get("atr", {}).get("volatility", "—"), ind.get("vwap", {}).get("signal", "—")),
        "",
        "## 综合评分",
        "",
        "- 技术面 %s · 资金面 %s · 消息面 %s → 综合 %s（满分 100）" % (scores["tech"], scores["fund"], scores["sentiment"], scores["total"]),
        "",
        "## 明日预测",
        "",
        "- 倾向：%s，上涨概率 %s%%，下跌概率 %s%%" % (pred.get("tendency"), pred.get("up_probability"), pred.get("down_probability")),
        "",
        "## 信号清单",
        "",
    ]
    for finding in findings:
        lines.append("- [%s] %s（%s）" % (finding["severity"], finding["title"], finding["category"]))
    lines += ["", "## 建议", ""]
    for suggestion in suggestions:
        lines.append("- %s" % suggestion)
    lines += ["", "## 风险提示", "", RISK_NOTE, ""]
    return "\n".join(lines)


# ---------- agent 模式（function-call + 模型） ----------

def agent_analysis(symbol, model, klines, sentiment, seed, verbose=True):
    import agent as agent_core

    bundle = fixture_bundle(symbol, klines, sentiment, seed)
    agent_core.fetch_realtime_quote = lambda sym: bundle["quote"]
    agent_core.fetch_history_kline = lambda sym, days=60: bundle["klines"]
    agent_core.fetch_fund_flow = lambda sym: bundle["fund_flow"]
    agent_core.fetch_news = lambda sym: bundle["news"]
    agent_core.fetch_sector_info = lambda sym: bundle["sector"]
    agent_core.fetch_sector_performance = lambda sym: bundle["sector"]
    agent_core.filter_relevant_news = lambda news, name, sym: news
    agent_core.analyze_news_sentiment = lambda news: sentiment

    # 评测模式：限制工具轮数与单次生成长度（推理模型长输出会拖慢 100 次调用的用例组），
    # 只在本评测入口收敛，不改 agent.py 的日常行为。
    agent_core.MAX_ITERATIONS = min(getattr(agent_core, "MAX_ITERATIONS", 15), 2)

    def capped_call_model(client, mod, system, tools, messages):
        import anthropic

        system = (system or "") + "\n（评测模式：报告控制在 400 字以内；工具调用请使用工具调用能力，不要手写工具调用标记；最终回答是 Markdown 报告。）"
        base = dict(model=mod, max_tokens=2500, system=system, tools=tools, messages=messages)
        try:
            return client.beta.messages.create(**base)
        except (TypeError, anthropic.BadRequestError, anthropic.NotFoundError):
            pass
        try:
            return client.messages.create(thinking={"type": "adaptive"}, **base)
        except (TypeError, anthropic.BadRequestError, anthropic.NotFoundError):
            pass
        return client.messages.create(**base)

    agent_core.call_model = capped_call_model

    report = agent_core.run_agent(symbol, model=model, verbose=verbose)
    return {"summary": report if isinstance(report, str) else str(report)}


# ---------- 用例入口 ----------

def parse_input(case):
    content = ((case.get("input") or {}).get("content") or "").strip()
    params = (case.get("input") or {}).get("params") or {}
    data = {}
    if content:
        try:
            parsed = json.loads(content)
            if isinstance(parsed, dict):
                data.update(parsed)
            else:
                data["symbol"] = content
        except ValueError:
            data["symbol"] = content
    data.update({k: v for k, v in params.items() if v is not None})

    symbol = str(data.get("symbol") or "").strip()
    if not symbol and content and not content.lstrip().startswith("{"):
        symbol = content.strip()
    # content 是长说明文字时，抠出里面的标的（6 位 A 股代码 / $TICKER / 大写美股代码）
    if len(symbol) > 40:
        m = re.search(r"(?<!\d)(\d{6})(?!\d)", symbol) or re.search(r"\$([A-Za-z]{1,5})\b", symbol) or re.search(r"\b([A-Z]{2,5})\b", symbol)
        if m:
            symbol = m.group(1).upper()
    try:
        seed = int(data.get("seed") or 42)
    except (TypeError, ValueError):
        seed = 42
    try:
        days = int(data.get("days") or 120)
    except (TypeError, ValueError):
        days = 120
    days = max(0, min(500, days))
    trend = str(data.get("trend") or "sideways")
    sentiment = str(data.get("sentiment") or "中性")
    try:
        fund_score = float(data.get("fund_score") if data.get("fund_score") is not None else 50)
    except (TypeError, ValueError):
        fund_score = 50
    mode = str(data.get("mode") or "rule").lower()
    klines = data.get("klines") if isinstance(data.get("klines"), list) else None
    return symbol, mode, klines, seed, days, trend, sentiment, fund_score


def main():
    ensure_utf8()
    parser = argparse.ArgumentParser(description="股票分析 Agent 评测入口（源码模式）")
    parser.add_argument("--case", default=os.environ.get("AEP_CASE_FILE", ""), help="用例 JSON 路径")
    parser.add_argument("--output", default="", help="report.json 输出路径（默认 $AEP_OUTPUT_DIR/report.json）")
    parser.add_argument("--mode", choices=["rule", "agent"], default="", help="覆盖用例里的模式")
    parser.add_argument("--model", default=os.environ.get("STOCK_ANALYST_MODEL", "mimo-v2.6-flash"))
    args = parser.parse_args()

    out_path = args.output or os.path.join(os.environ.get("AEP_OUTPUT_DIR", "."), "report.json")
    case = {}
    if args.case and os.path.exists(args.case):
        with open(args.case, "r", encoding="utf-8") as handle:
            case = json.load(handle)

    symbol, mode, klines, seed, days, trend, sentiment, fund_score = parse_input(case)
    if args.mode:
        mode = args.mode

    report = {"findings": [], "suggestions": [], "summary": ""}
    try:
        if not symbol:
            report["summary"] = "# 股票分析报告\n\n## 结论\n\n未提供股票代码（input.content 为空），无法分析。\n\n## 风险提示\n\n%s\n" % RISK_NOTE
        else:
            if klines is None:
                klines = build_klines(seed=seed, trend=trend, days=days)
            if mode == "agent":
                agent_part = agent_analysis(symbol, args.model, klines, sentiment, seed)
                summary = clean_agent_report(agent_part["summary"])
                if len(summary) < 80:
                    # 模型只回了工具调用标记/极短文本：补一份确定性分析，保证对话可用
                    findings, suggestions, segments = rule_analysis(symbol, klines, sentiment, fund_score)
                    note = "> （模型报告不完整，以下为确定性分析补充）\n\n"
                    summary = summary + ("\n\n" if summary else "") + note + render_summary(symbol, findings, suggestions, segments)
                report["summary"] = summary
                report["suggestions"] = [RISK_NOTE]
            else:
                findings, suggestions, segments = rule_analysis(symbol, klines, sentiment, fund_score)
                report["findings"] = findings
                report["suggestions"] = suggestions
                report["summary"] = render_summary(symbol, findings, suggestions, segments)
    except Exception as exc:  # 评测要求优雅降级：任何异常都要落一份可分析的报告
        report["summary"] = "# 股票分析报告：%s\n\n## 结论\n\n分析过程出现异常，已优雅降级：%s\n\n## 风险提示\n\n%s\n" % (symbol or "（空）", exc, RISK_NOTE)
        report["suggestions"] = [RISK_NOTE]

    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2)
    print("[完成] 分析报告已写入：%s" % out_path)


if __name__ == "__main__":
    main()
