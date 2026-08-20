#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
数据获取层 —— 支持 A 股（新浪财经）和美股（Yahoo Finance）

所有函数返回 dict，失败时返回 {"error": "错误信息"}
"""

import json
import re
import time
from datetime import datetime, timedelta

import requests
import yfinance as yf

# 请求超时（秒）
TIMEOUT = 10

# 最大重试次数
MAX_RETRIES = 3


def _request_with_retry(url, params=None, headers=None):
    """带重试的请求"""
    for i in range(MAX_RETRIES):
        try:
            resp = requests.get(url, params=params, headers=headers, timeout=TIMEOUT)
            resp.raise_for_status()
            return resp
        except requests.RequestException as e:
            if i == MAX_RETRIES - 1:
                raise
            time.sleep(1)
    return None


def _detect_market(symbol):
    """检测股票市场：A股返回 'a'，美股返回 'us'"""
    # A 股：纯数字，6位
    if re.match(r'^\d{6}$', symbol):
        return 'a'
    # 美股：字母开头
    if re.match(r'^[A-Za-z]', symbol):
        return 'us'
    # 默认尝试 A 股
    return 'a'


def _get_a_stock_prefix(symbol):
    """获取 A 股代码前缀（sh/sz）"""
    if symbol.startswith('6'):
        return 'sh'
    elif symbol.startswith('0') or symbol.startswith('3'):
        return 'sz'
    elif symbol.startswith('8') or symbol.startswith('4'):
        return 'bj'  # 北交所
    return 'sh'


# ========== A 股数据 ==========


def fetch_a_stock_realtime(symbol):
    """
    获取 A 股实时行情（新浪财经 API）

    返回字段：symbol, name, price, change, change_pct, open, high, low,
              volume, amount, prev_close, turnover_rate, pe_ratio
    """
    prefix = _get_a_stock_prefix(symbol)
    code = f"{prefix}{symbol}"
    url = f"https://hq.sinajs.cn/list={code}"
    headers = {"Referer": "https://finance.sina.com.cn"}

    try:
        resp = _request_with_retry(url, headers=headers)
        text = resp.text

        # 解析新浪行情数据
        # 格式：var hq_str_sh600519="贵州茅台,1850.00,...";
        match = re.search(r'"(.+)"', text)
        if not match:
            return {"error": f"无法解析行情数据：{text[:200]}"}

        fields = match.group(1).split(',')
        if len(fields) < 32:
            return {"error": "行情数据字段不足"}

        name = fields[0]
        open_price = float(fields[1]) if fields[1] else 0
        prev_close = float(fields[2]) if fields[2] else 0
        price = float(fields[3]) if fields[3] else 0
        high = float(fields[4]) if fields[4] else 0
        low = float(fields[5]) if fields[5] else 0
        volume = float(fields[8]) if fields[8] else 0  # 成交量（股）
        amount = float(fields[9]) if fields[9] else 0  # 成交额（元）

        change = price - prev_close if prev_close > 0 else 0
        change_pct = (change / prev_close * 100) if prev_close > 0 else 0

        return {
            "symbol": symbol,
            "name": name,
            "price": price,
            "change": round(change, 2),
            "change_pct": round(change_pct, 2),
            "open": open_price,
            "high": high,
            "low": low,
            "volume": volume,
            "amount": amount,
            "prev_close": prev_close,
            "market": "A股"
        }
    except Exception as e:
        return {"error": f"获取A股行情失败：{e}"}


def fetch_a_stock_kline(symbol, days=60):
    """
    获取 A 股历史 K 线数据（新浪财经 API）

    返回：list of dict，每项包含 date, open, high, low, close, volume
    """
    prefix = _get_a_stock_prefix(symbol)
    url = f"https://quotes.sina.cn/cn/api/jsonp_v2.php/var%20_k=/CN_MarketDataService.getKLineData"
    params = {
        "symbol": f"{prefix}{symbol}",
        "scale": "240",  # 日K
        "ma": "no",
        "datalen": days
    }

    try:
        resp = _request_with_retry(url, params=params)
        text = resp.text

        # 解析 JSONP 响应
        match = re.search(r'\[(.+)\]', text, re.DOTALL)
        if not match:
            return {"error": "无法解析K线数据"}

        data = json.loads(f"[{match.group(1)}]")
        klines = []
        for item in data:
            klines.append({
                "date": item.get("day", ""),
                "open": float(item.get("open", 0)),
                "high": float(item.get("high", 0)),
                "low": float(item.get("low", 0)),
                "close": float(item.get("close", 0)),
                "volume": float(item.get("volume", 0))
            })
        return klines
    except Exception as e:
        return {"error": f"获取A股K线失败：{e}"}


def fetch_a_stock_fund_flow(symbol):
    """
    获取 A 股资金流向（东方财富 API）

    返回字段：main_net_inflow（主力净流入）, main_pct, huge_net_inflow, big_net_inflow,
              mid_net_inflow, small_net_inflow, north_net_inflow
    """
    # 东方财富资金流向接口
    prefix = "1." if symbol.startswith('6') else "0."
    code = f"{prefix}{symbol}"
    url = f"https://push2.eastmoney.com/api/qt/stock/fflow/kline/get"
    params = {
        "secid": code,
        "fields1": "f1,f2,f3,f7",
        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f62,f63,f64,f65",
        "klt": "101",  # 日线
        "lmt": "1",    # 最近1天
    }

    try:
        resp = _request_with_retry(url, params=params)
        data = resp.json()

        if not data.get("data") or not data["data"].get("klines"):
            return {"error": "无资金流向数据"}

        kline = data["data"]["klines"][-1]
        fields = kline.split(",")

        # 字段：日期,主力净流入,小单净流入,中单净流入,大单净流入,超大单净流入
        main_net = float(fields[1]) if len(fields) > 1 else 0

        return {
            "main_net_inflow": main_net,  # 主力净流入（元）
            "main_net_inflow_display": f"{main_net / 10000:.2f} 万" if abs(main_net) < 100000000 else f"{main_net / 100000000:.2f} 亿",
        }
    except Exception as e:
        return {"error": f"获取资金流向失败：{e}"}


def fetch_a_stock_sector(symbol):
    """
    获取 A 股所属板块信息（简化版，返回板块名称）

    注：完整板块数据需要更复杂的 API，这里返回基础信息
    """
    # 这里简化处理，实际可通过东方财富 API 获取
    # 返回一个基础结构，让 Claude 通过新闻分析补充
    return {
        "sector": "未知板块",
        "note": "板块信息需结合新闻分析"
    }


# ========== 美股数据 ==========


def fetch_us_stock_realtime(symbol):
    """
    获取美股实时行情（yfinance）

    返回字段：symbol, name, price, change, change_pct, open, high, low,
              volume, prev_close, market_cap
    """
    try:
        ticker = yf.Ticker(symbol)
        info = ticker.info

        if not info or "regularMarketPrice" not in info:
            # 尝试从历史数据获取
            hist = ticker.history(period="1d")
            if hist.empty:
                return {"error": "无行情数据"}
            price = hist["Close"].iloc[-1]
            prev_close = info.get("previousClose", price)
        else:
            price = info.get("regularMarketPrice", 0)
            prev_close = info.get("previousClose", 0)

        change = price - prev_close if prev_close > 0 else 0
        change_pct = (change / prev_close * 100) if prev_close > 0 else 0

        return {
            "symbol": symbol,
            "name": info.get("shortName", symbol),
            "price": round(price, 2),
            "change": round(change, 2),
            "change_pct": round(change_pct, 2),
            "open": info.get("regularMarketOpen", 0),
            "high": info.get("regularMarketDayHigh", 0),
            "low": info.get("regularMarketDayLow", 0),
            "volume": info.get("regularMarketVolume", 0),
            "prev_close": prev_close,
            "market_cap": info.get("marketCap", 0),
            "market": "美股",
            "currency": info.get("currency", "USD")
        }
    except Exception as e:
        return {"error": f"获取美股行情失败：{e}"}


def fetch_us_stock_kline(symbol, days=60):
    """
    获取美股历史 K 线数据（yfinance）

    返回：list of dict，每项包含 date, open, high, low, close, volume
    """
    try:
        ticker = yf.Ticker(symbol)
        hist = ticker.history(period=f"{days}d")

        if hist.empty:
            return {"error": "无K线数据"}

        klines = []
        for date, row in hist.iterrows():
            klines.append({
                "date": date.strftime("%Y-%m-%d"),
                "open": round(row["Open"], 2),
                "high": round(row["High"], 2),
                "low": round(row["Low"], 2),
                "close": round(row["Close"], 2),
                "volume": int(row["Volume"])
            })
        return klines
    except Exception as e:
        return {"error": f"获取美股K线失败：{e}"}


def fetch_us_stock_fund_flow(symbol):
    """
    美股资金流向（Yahoo Finance 不直接提供，返回提示）
    """
    return {
        "note": "美股资金流向数据暂不支持，可参考成交量变化和机构持仓变动",
        "main_net_inflow": None,
        "main_net_inflow_display": "暂无数据"
    }


def fetch_us_stock_sector(symbol):
    """美股板块信息"""
    return {
        "sector": "未知板块",
        "note": "板块信息需结合新闻分析"
    }


# ========== 新闻数据 ==========


def fetch_a_stock_news(symbol):
    """
    获取 A 股相关新闻（新浪财经）

    返回：list of dict，每项包含 title, source, time, url
    """
    url = f"https://feed.mix.sina.com.cn/api/roll/get"
    params = {
        "pageid": "153",
        "lid": "2516",
        "k": symbol,
        "num": 10,
        "page": 1,
    }

    try:
        resp = _request_with_retry(url, params=params)
        data = resp.json()

        result = data.get("result", {})
        articles = result.get("data", [])

        news = []
        for item in articles:
            news.append({
                "title": item.get("title", ""),
                "source": item.get("media_name", ""),
                "time": item.get("ctime", ""),
                "url": item.get("url", ""),
            })
        return news
    except Exception as e:
        return {"error": f"获取A股新闻失败：{e}"}


def fetch_us_stock_news(symbol):
    """
    获取美股相关新闻（Yahoo Finance）

    返回：list of dict，每项包含 title, source, time, url
    """
    url = f"https://query1.finance.yahoo.com/v1/finance/search"
    params = {
        "q": symbol,
        "quotesCount": 0,
        "newsCount": 10,
    }
    headers = {"User-Agent": "Mozilla/5.0"}

    try:
        resp = _request_with_retry(url, params=params, headers=headers)
        data = resp.json()

        news_items = data.get("news", [])
        news = []
        for item in news_items:
            news.append({
                "title": item.get("title", ""),
                "source": item.get("publisher", ""),
                "time": datetime.fromtimestamp(item.get("providerPublishTime", 0)).strftime("%Y-%m-%d %H:%M") if item.get("providerPublishTime") else "",
                "url": item.get("link", ""),
            })
        return news
    except Exception as e:
        return {"error": f"获取美股新闻失败：{e}"}


def fetch_news(symbol):
    """统一接口：获取新闻"""
    market = _detect_market(symbol)
    if market == 'a':
        return fetch_a_stock_news(symbol)
    else:
        return fetch_us_stock_news(symbol)


# ========== 统一接口 ==========


def fetch_realtime_quote(symbol):
    """统一接口：获取实时行情"""
    market = _detect_market(symbol)
    if market == 'a':
        return fetch_a_stock_realtime(symbol)
    else:
        return fetch_us_stock_realtime(symbol)


def fetch_history_kline(symbol, days=60):
    """统一接口：获取历史 K 线"""
    market = _detect_market(symbol)
    if market == 'a':
        return fetch_a_stock_kline(symbol, days)
    else:
        return fetch_us_stock_kline(symbol, days)


def fetch_fund_flow(symbol):
    """统一接口：获取资金流向"""
    market = _detect_market(symbol)
    if market == 'a':
        return fetch_a_stock_fund_flow(symbol)
    else:
        return fetch_us_stock_fund_flow(symbol)


def fetch_sector_info(symbol):
    """统一接口：获取板块信息"""
    market = _detect_market(symbol)
    if market == 'a':
        return fetch_a_stock_sector(symbol)
    else:
        return fetch_us_stock_sector(symbol)


def filter_relevant_news(news, stock_name, symbol):
    """
    过滤相关新闻，只保留与股票相关的新闻

    参数：
        news: 新闻列表
        stock_name: 股票名称
        symbol: 股票代码

    返回：过滤后的新闻列表
    """
    if isinstance(news, dict) and "error" in news:
        return news
    if not news:
        return []

    # 关键词列表
    keywords = [stock_name, symbol]

    # 添加常见别名
    name_aliases = {
        "贵州茅台": ["茅台", "茅台酒"],
        "五粮液": ["五粮液酒"],
        "宁德时代": ["宁德"],
        "比亚迪": ["BYD"],
        "中国平安": ["平安"],
        "招商银行": ["招行"],
        "苹果": ["Apple", "AAPL"],
        "特斯拉": ["Tesla", "TSLA"],
        "英伟达": ["NVIDIA", "NVDA"],
    }

    if stock_name in name_aliases:
        keywords.extend(name_aliases[stock_name])

    filtered = []
    for item in news:
        title = item.get("title", "")
        # 检查标题是否包含关键词
        if any(kw in title for kw in keywords if kw):
            filtered.append(item)

    # 如果过滤后太少，返回原始新闻（最多5条）
    if len(filtered) < 3:
        return news[:5]

    return filtered


def analyze_news_sentiment(news):
    """
    分析新闻情绪

    返回：sentiment（正面/中性/负面）, score（0-100）, keywords（关键情绪词）
    """
    if not news:
        return {"sentiment": "中性", "score": 50, "keywords": []}

    # 情绪关键词
    positive_words = ["上涨", "增长", "突破", "利好", "看多", "反弹", "回升", "新高", "涨停", "大涨", "强势", "买入", "增持"]
    negative_words = ["下跌", "下降", "跌破", "利空", "看空", "回调", "新低", "跌停", "大跌", "弱势", "卖出", "减持", "风险"]

    positive_count = 0
    negative_count = 0
    found_keywords = []

    for item in news:
        title = item.get("title", "")
        for word in positive_words:
            if word in title:
                positive_count += 1
                found_keywords.append(f"+{word}")
        for word in negative_words:
            if word in title:
                negative_count += 1
                found_keywords.append(f"-{word}")

    # 计算情绪分数
    total = positive_count + negative_count
    if total == 0:
        sentiment = "中性"
        score = 50
    elif positive_count > negative_count:
        sentiment = "正面"
        score = 50 + min((positive_count - negative_count) * 10, 30)
    elif negative_count > positive_count:
        sentiment = "负面"
        score = 50 - min((negative_count - positive_count) * 10, 30)
    else:
        sentiment = "中性"
        score = 50

    return {
        "sentiment": sentiment,
        "score": score,
        "keywords": list(set(found_keywords))[:5],
        "positive_count": positive_count,
        "negative_count": negative_count
    }


def fetch_sector_performance(symbol):
    """
    获取板块表现（A股）

    返回：sector（板块名称）, sector_change（板块涨跌幅）, rank（个股在板块中的排名）
    """
    # 这里简化处理，实际需要更复杂的板块数据
    # 返回基础结构，让 Claude 通过其他数据补充
    return {
        "sector": "未知板块",
        "sector_change": None,
        "rank": None,
        "note": "板块数据需结合实时行情分析"
    }


# 测试
if __name__ == "__main__":
    print("=== 测试 A 股（600519 贵州茅台）===")
    print(json.dumps(fetch_realtime_quote("600519"), ensure_ascii=False, indent=2))

    print("\n=== 测试美股（AAPL）===")
    print(json.dumps(fetch_realtime_quote("AAPL"), ensure_ascii=False, indent=2))

    print("\n=== 测试 K 线 ===")
    klines = fetch_history_kline("600519", 5)
    if isinstance(klines, list):
        for k in klines[-3:]:
            print(k)
    else:
        print(klines)
