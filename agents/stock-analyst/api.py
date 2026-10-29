#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
股票分析 Agent —— 本地 HTTP API

把 stock-analyst 包成一个本地 REST 接口，方便本地测试和被其他程序调用。

启动：
    python3 api.py                      # 默认 http://127.0.0.1:8300
    python3 api.py --host 0.0.0.0 --port 9000

测试：
    # 健康检查（不需要模型 key）
    curl "http://127.0.0.1:8300/health"

    # 快速行情 / 指标（不需要模型 key）
    curl "http://127.0.0.1:8300/quote?symbol=600519"
    curl "http://127.0.0.1:8300/indicators?symbol=600519&days=60"

    # 完整分析：规则模式（不需要模型 key，本地可直接测）
    curl "http://127.0.0.1:8300/analyze?symbol=600519"

    # 完整分析：Agent 模式（Claude function-call 生成 Markdown 报告，需要 key）
    curl -X POST "http://127.0.0.1:8300/analyze" \
         -H "Content-Type: application/json" \
         -d '{"symbol": "600519", "mode": "agent", "model": "claude-opus-5"}'

交互式文档：浏览器打开 http://127.0.0.1:8300/docs

模型 key（只有 mode=agent 需要）：
    export ANTHROPIC_API_KEY=你的key                # 官方 API
    # 或走兼容网关：
    export ANTHROPIC_BASE_URL=https://your-gateway
    export ANTHROPIC_AUTH_TOKEN=你的token
"""

import argparse
import os
import sys
from datetime import datetime
from typing import Any, Dict, List, Optional

# Windows 控制台 GBK 编码保护
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

import agent as agent_core
from analyzer import (
    analyze_recent_trend,
    analyze_technical_indicators,
    backtest_prediction,
    calc_comprehensive_score,
    calc_fund_score,
    calc_sentiment_score,
    calc_support_resistance,
    calc_technical_score,
    predict_tomorrow,
)
from data_fetcher import (
    analyze_news_sentiment,
    fetch_fund_flow,
    fetch_history_kline,
    fetch_news,
    fetch_realtime_quote,
    filter_relevant_news,
)

RISK_NOTE = "所有分析仅供参考，不构成投资建议。股票预测本质是概率分析，无法保证准确。"

# 情绪入参归一化（中英文都接受）
SENTIMENT_MAP = {
    "正面": "正面", "positive": "正面", "pos": "正面", "利好": "正面",
    "中性": "中性", "neutral": "中性", "neu": "中性",
    "负面": "负面", "negative": "负面", "neg": "负面", "利空": "负面",
}

app = FastAPI(
    title="Stock Analyst API",
    version="1.0.0",
    description="股票分析 Agent 的本地 HTTP 接口：A股/美股行情、技术指标、综合评分与明日预测。",
)


# ========== 入参模型 ==========


class AnalyzeRequest(BaseModel):
    """POST /analyze 的请求体"""

    symbol: str = Field(..., description="股票代码：A股如 600519 / 000858，美股如 AAPL / TSLA",
                        min_length=1, max_length=12, examples=["600519"])
    mode: str = Field("rule", description="分析模式：rule=纯规则分析（不调模型，本地可直接测）；agent=Claude function-call 生成完整报告")
    model: Optional[str] = Field(None, description="agent 模式使用的模型，默认 claude-opus-5（走网关可传 qwen3.8-max）")
    days: int = Field(60, description="历史 K 线交易日数（30-250，指标计算需要至少 30）", ge=30, le=250)
    sentiment: Optional[str] = Field(
        None,
        description="手动指定消息面情绪（正面/中性/负面，或 positive/neutral/negative）；不传则自动抓新闻做情绪分析",
        examples=["中性"],
    )
    include_news: bool = Field(True, description="是否在返回里带相关新闻（rule 模式）")


# ========== 工具函数 ==========


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _normalize_sentiment(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    key = value.strip().lower()
    mapped = SENTIMENT_MAP.get(key) or SENTIMENT_MAP.get(value.strip())
    if mapped is None:
        raise HTTPException(
            status_code=422,
            detail="sentiment 只能是：正面/中性/负面（或 positive/neutral/negative）",
        )
    return mapped


def _rule_analysis(symbol: str, days: int, sentiment: Optional[str], include_news: bool) -> Dict[str, Any]:
    """纯规则分析：不调用大模型，全部确定性计算，本地可直接测试"""
    quote = fetch_realtime_quote(symbol)
    if isinstance(quote, dict) and quote.get("error"):
        raise HTTPException(status_code=502, detail=f"行情获取失败：{quote['error']}")

    klines = fetch_history_kline(symbol, days=days)
    if isinstance(klines, dict) and klines.get("error"):
        raise HTTPException(status_code=502, detail=f"历史 K 线获取失败：{klines['error']}")
    if not klines or len(klines) < 30:
        raise HTTPException(status_code=502, detail="历史数据不足（需要至少 30 个交易日），请增大 days 或稍后重试")

    indicators = analyze_technical_indicators(klines)
    tech_score = calc_technical_score(indicators)

    fund_data = fetch_fund_flow(symbol)
    fund_score = calc_fund_score(fund_data)

    # 消息面：手动指定优先，否则抓新闻自动分析
    news_raw = fetch_news(symbol)
    news = filter_relevant_news(news_raw, quote.get("name", ""), symbol) if isinstance(news_raw, list) else []
    if sentiment is not None:
        sentiment_label = sentiment
        sentiment_info: Dict[str, Any] = {"sentiment": sentiment_label, "source": "manual"}
    else:
        sentiment_info = analyze_news_sentiment(news) if news else {"sentiment": "中性", "score": 50, "keywords": []}
        sentiment_label = sentiment_info.get("sentiment", "中性")
        sentiment_info["source"] = "auto"
    sentiment_score = calc_sentiment_score(sentiment_label)

    comprehensive = calc_comprehensive_score(tech_score, fund_score, sentiment_score)

    result: Dict[str, Any] = {
        "symbol": symbol,
        "name": quote.get("name"),
        "market": quote.get("market"),
        "mode": "rule",
        "generated_at": _now(),
        "quote": quote,
        "kline_days": len(klines),
        "indicators": indicators,
        "scores": {
            "technical": tech_score,
            "fund": fund_score,
            "sentiment": sentiment_score,
            "comprehensive": comprehensive,
            "weights": {"technical": 0.4, "fund": 0.35, "sentiment": 0.25},
        },
        "fund_flow": fund_data,
        "sentiment": sentiment_info,
        "prediction": predict_tomorrow(comprehensive),
        "support_resistance": calc_support_resistance(klines),
        "recent_trend": analyze_recent_trend(klines, days=15),
        "backtest": backtest_prediction(klines, lookback=20),
        "risk_note": RISK_NOTE,
    }
    if include_news:
        result["news"] = news[:5] if isinstance(news, list) else news
    return result


# ========== 路由 ==========


@app.get("/", tags=["meta"])
def index():
    return {
        "service": "stock-analyst API",
        "version": "1.0.0",
        "docs": "/docs",
        "endpoints": {
            "GET /health": "服务状态与模型 key 可用性",
            "GET /quote?symbol=600519": "实时行情",
            "GET /kline?symbol=600519&days=60": "历史 K 线",
            "GET /indicators?symbol=600519&days=60": "技术指标+支撑阻力+走势+回测",
            "GET /analyze?symbol=600519&mode=rule": "完整分析（GET，方便浏览器/curl 测）",
            "POST /analyze": "完整分析（POST JSON，字段见 /docs）",
        },
    }


@app.get("/health", tags=["meta"])
def health():
    llm_ready = bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))
    return {
        "status": "ok",
        "time": _now(),
        "default_model": agent_core.DEFAULT_MODEL,
        "llm_ready": llm_ready,
        "llm_hint": "" if llm_ready else "未检测到 ANTHROPIC_API_KEY / ANTHROPIC_AUTH_TOKEN，只能使用 mode=rule",
    }


@app.get("/quote", tags=["data"])
def quote(symbol: str = Query(..., description="股票代码", min_length=1, max_length=12)):
    return fetch_realtime_quote(symbol)


@app.get("/kline", tags=["data"])
def kline(
    symbol: str = Query(..., description="股票代码", min_length=1, max_length=12),
    days: int = Query(60, description="交易日数", ge=5, le=250),
):
    return {"symbol": symbol, "days": days, "klines": fetch_history_kline(symbol, days=days)}


@app.get("/indicators", tags=["data"])
def indicators(
    symbol: str = Query(..., description="股票代码", min_length=1, max_length=12),
    days: int = Query(60, description="交易日数", ge=30, le=250),
):
    klines = fetch_history_kline(symbol, days=days)
    if isinstance(klines, dict) and klines.get("error"):
        raise HTTPException(status_code=502, detail=f"历史 K 线获取失败：{klines['error']}")
    if not klines or len(klines) < 30:
        raise HTTPException(status_code=502, detail="历史数据不足（需要至少 30 个交易日）")
    return {
        "symbol": symbol,
        "generated_at": _now(),
        "kline_days": len(klines),
        "indicators": analyze_technical_indicators(klines),
        "support_resistance": calc_support_resistance(klines),
        "recent_trend": analyze_recent_trend(klines, days=15),
        "backtest": backtest_prediction(klines, lookback=20),
    }


def _run_analyze(symbol: str, mode: str, model: Optional[str], days: int,
                 sentiment: Optional[str], include_news: bool) -> Dict[str, Any]:
    symbol = symbol.strip().upper() if symbol.strip().isalpha() else symbol.strip()
    sentiment_label = _normalize_sentiment(sentiment)

    if mode == "rule":
        return _rule_analysis(symbol, days, sentiment_label, include_news)

    # mode == "agent"
    if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
        raise HTTPException(
            status_code=503,
            detail="agent 模式需要模型 key：请设置 ANTHROPIC_API_KEY（或网关的 ANTHROPIC_BASE_URL + ANTHROPIC_AUTH_TOKEN）；"
                   "本地无 key 测试请用 mode=rule",
        )
    use_model = model or agent_core.DEFAULT_MODEL
    try:
        report = agent_core.run_agent(symbol, use_model, verbose=False)
    except Exception as e:  # SDK 各类错误统一转成 502，信息带回前端
        raise HTTPException(status_code=502, detail=f"模型调用失败：{type(e).__name__}: {e}")

    return {
        "symbol": symbol,
        "mode": "agent",
        "model": use_model,
        "generated_at": _now(),
        "report": report,
        "risk_note": RISK_NOTE,
    }


@app.post("/analyze", tags=["analyze"])
def analyze_post(req: AnalyzeRequest):
    return _run_analyze(req.symbol, req.mode.lower(), req.model, req.days, req.sentiment, req.include_news)


@app.get("/analyze", tags=["analyze"])
def analyze_get(
    symbol: str = Query(..., description="股票代码", min_length=1, max_length=12),
    mode: str = Query("rule", description="rule=纯规则（不调模型）；agent=Claude 报告"),
    model: Optional[str] = Query(None, description="agent 模式使用的模型"),
    days: int = Query(60, description="历史 K 线交易日数", ge=30, le=250),
    sentiment: Optional[str] = Query(None, description="手动指定消息面情绪：正面/中性/负面"),
    include_news: bool = Query(True, description="是否返回相关新闻"),
):
    return _run_analyze(symbol, mode.lower(), model, days, sentiment, include_news)


@app.exception_handler(HTTPException)
async def http_exception_handler(request, exc):
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": True, "status_code": exc.status_code, "detail": exc.detail, "time": _now()},
    )


# ========== 入口 ==========


def main():
    parser = argparse.ArgumentParser(description="股票分析 Agent 本地 HTTP API")
    parser.add_argument("--host", default="127.0.0.1", help="监听地址（默认 127.0.0.1）")
    parser.add_argument("--port", type=int, default=8300, help="监听端口（默认 8300）")
    args = parser.parse_args()

    try:
        import uvicorn
    except ImportError:
        print("缺少 uvicorn：pip3 install uvicorn fastapi", file=sys.stderr)
        sys.exit(1)

    print(f"🚀 Stock Analyst API: http://{args.host}:{args.port}  （交互式文档 /docs）")
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
