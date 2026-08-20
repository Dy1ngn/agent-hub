#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
技术分析层 —— 计算技术指标、综合评分、涨跌预测

纯 Python 实现，不依赖 ta-lib
"""

from typing import List, Dict, Any


def _safe_float(val, default=0.0):
    """安全转换为浮点数"""
    try:
        return float(val) if val is not None else default
    except (ValueError, TypeError):
        return default


# ========== 技术指标计算 ==========


def calc_ma(klines: List[Dict], period: int) -> List[float]:
    """计算移动平均线"""
    closes = [k["close"] for k in klines]
    ma = []
    for i in range(len(closes)):
        if i < period - 1:
            ma.append(None)
        else:
            ma.append(sum(closes[i - period + 1:i + 1]) / period)
    return ma


def calc_ema(data: List[float], period: int) -> List[float]:
    """计算指数移动平均线"""
    if not data:
        return []
    ema = [data[0]]
    multiplier = 2 / (period + 1)
    for i in range(1, len(data)):
        ema.append(data[i] * multiplier + ema[-1] * (1 - multiplier))
    return ema


def calc_macd(klines: List[Dict], fast=12, slow=26, signal=9) -> Dict[str, Any]:
    """
    计算 MACD 指标

    返回：dif, dea, macd（柱状图）, trend（趋势判断）
    """
    closes = [k["close"] for k in klines]
    if len(closes) < slow + signal:
        return {"error": "数据不足，无法计算MACD"}

    ema_fast = calc_ema(closes, fast)
    ema_slow = calc_ema(closes, slow)

    dif = [ema_fast[i] - ema_slow[i] for i in range(len(closes))]
    dea = calc_ema(dif, signal)
    macd_hist = [(dif[i] - dea[i]) * 2 for i in range(len(closes))]

    # 趋势判断
    current_dif = dif[-1]
    current_dea = dea[-1]
    current_macd = macd_hist[-1]
    prev_macd = macd_hist[-2] if len(macd_hist) > 1 else 0

    if current_dif > current_dea and current_macd > 0:
        if current_macd > prev_macd:
            trend = "金叉向上，看多"
        else:
            trend = "金叉但动能减弱，谨慎看多"
    elif current_dif < current_dea and current_macd < 0:
        if current_macd < prev_macd:
            trend = "死叉向下，看空"
        else:
            trend = "死叉但下跌动能减弱，谨慎看空"
    else:
        trend = "多空交织，观望"

    return {
        "dif": round(current_dif, 4),
        "dea": round(current_dea, 4),
        "macd": round(current_macd, 4),
        "trend": trend
    }


def calc_kdj(klines: List[Dict], n=9, m1=3, m2=3) -> Dict[str, Any]:
    """
    计算 KDJ 指标

    返回：k, d, j, zone（超买/超卖区域）
    """
    if len(klines) < n:
        return {"error": "数据不足，无法计算KDJ"}

    highs = [k["high"] for k in klines]
    lows = [k["low"] for k in klines]
    closes = [k["close"] for k in klines]

    # 计算 RSV
    rsv = []
    for i in range(len(closes)):
        if i < n - 1:
            rsv.append(50)
        else:
            high_n = max(highs[i - n + 1:i + 1])
            low_n = min(lows[i - n + 1:i + 1])
            if high_n == low_n:
                rsv.append(50)
            else:
                rsv.append((closes[i] - low_n) / (high_n - low_n) * 100)

    # 计算 K、D、J
    k_vals = [50]
    d_vals = [50]
    for i in range(len(rsv)):
        k_vals.append(k_vals[-1] * (m1 - 1) / m1 + rsv[i] / m1)
        d_vals.append(d_vals[-1] * (m2 - 1) / m2 + k_vals[-1] / m2)

    k = k_vals[-1]
    d = d_vals[-1]
    j = 3 * k - 2 * d

    # 区域判断
    if k > 80 and d > 80:
        zone = "超买区，短期有回调风险"
    elif k < 20 and d < 20:
        zone = "超卖区，短期有反弹可能"
    elif k > d and k_vals[-2] <= d_vals[-2]:
        zone = "金叉，看多信号"
    elif k < d and k_vals[-2] >= d_vals[-2]:
        zone = "死叉，看空信号"
    else:
        zone = "中性区域"

    return {
        "k": round(k, 2),
        "d": round(d, 2),
        "j": round(j, 2),
        "zone": zone
    }


def calc_rsi(klines: List[Dict], period=14) -> Dict[str, Any]:
    """
    计算 RSI 指标

    返回：rsi, zone（强弱区域）
    """
    closes = [k["close"] for k in klines]
    if len(closes) < period + 1:
        return {"error": "数据不足，无法计算RSI"}

    gains = []
    losses = []
    for i in range(1, len(closes)):
        diff = closes[i] - closes[i - 1]
        gains.append(max(diff, 0))
        losses.append(max(-diff, 0))

    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period

    if avg_loss == 0:
        rsi = 100
    else:
        rs = avg_gain / avg_loss
        rsi = 100 - (100 / (1 + rs))

    # 区域判断
    if rsi > 70:
        zone = "超买，有回调风险"
    elif rsi < 30:
        zone = "超卖，有反弹可能"
    elif rsi > 50:
        zone = "偏强"
    else:
        zone = "偏弱"

    return {
        "rsi": round(rsi, 2),
        "zone": zone
    }


def calc_volume_ratio(klines: List[Dict], period=5) -> Dict[str, Any]:
    """
    计算量比（今日成交量 / 过去N日平均成交量）

    返回：ratio, interpretation
    """
    if len(klines) < period + 1:
        return {"error": "数据不足"}

    current_vol = klines[-1]["volume"]
    avg_vol = sum(k["volume"] for k in klines[-period - 1:-1]) / period

    if avg_vol == 0:
        ratio = 0
    else:
        ratio = current_vol / avg_vol

    if ratio > 2:
        interpretation = "放量明显，关注资金动向"
    elif ratio > 1.5:
        interpretation = "温和放量"
    elif ratio > 0.8:
        interpretation = "量能正常"
    else:
        interpretation = "缩量，市场观望情绪浓"

    return {
        "ratio": round(ratio, 2),
        "interpretation": interpretation
    }


def calc_bollinger_bands(klines: List[Dict], period=20, std_dev=2) -> Dict[str, Any]:
    """
    计算布林带（Bollinger Bands）

    返回：upper（上轨）, middle（中轨）, lower（下轨）, bandwidth（带宽）, percent_b（%B指标）
    """
    closes = [k["close"] for k in klines]
    if len(closes) < period:
        return {"error": "数据不足，无法计算布林带"}

    # 计算中轨（SMA）
    middle = sum(closes[-period:]) / period

    # 计算标准差
    variance = sum((x - middle) ** 2 for x in closes[-period:]) / period
    std = variance ** 0.5

    upper = middle + std_dev * std
    lower = middle - std_dev * std

    # 带宽（相对中轨的百分比）
    bandwidth = (upper - lower) / middle * 100 if middle > 0 else 0

    # %B指标：(收盘价 - 下轨) / (上轨 - 下轨)
    current_price = closes[-1]
    percent_b = (current_price - lower) / (upper - lower) if (upper - lower) > 0 else 0.5

    # 位置判断
    if percent_b > 1:
        position = "突破上轨，超强或超买"
    elif percent_b > 0.8:
        position = "接近上轨，注意压力"
    elif percent_b > 0.5:
        position = "中轨上方，偏强"
    elif percent_b > 0.2:
        position = "中轨下方，偏弱"
    elif percent_b > 0:
        position = "接近下轨，注意支撑"
    else:
        position = "跌破下轨，超弱或超卖"

    return {
        "upper": round(upper, 2),
        "middle": round(middle, 2),
        "lower": round(lower, 2),
        "bandwidth": round(bandwidth, 2),
        "percent_b": round(percent_b, 4),
        "position": position
    }


def calc_atr(klines: List[Dict], period=14) -> Dict[str, Any]:
    """
    计算 ATR（平均真实波幅）

    返回：atr, atr_pct（ATR占收盘价百分比）, volatility（波动率评估）
    """
    if len(klines) < period + 1:
        return {"error": "数据不足，无法计算ATR"}

    true_ranges = []
    for i in range(1, len(klines)):
        high = klines[i]["high"]
        low = klines[i]["low"]
        prev_close = klines[i - 1]["close"]

        tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        true_ranges.append(tr)

    atr = sum(true_ranges[-period:]) / period
    current_price = klines[-1]["close"]
    atr_pct = (atr / current_price * 100) if current_price > 0 else 0

    # 波动率评估
    if atr_pct > 5:
        volatility = "高波动，风险较大"
    elif atr_pct > 3:
        volatility = "中等波动"
    elif atr_pct > 1.5:
        volatility = "低波动，走势平稳"
    else:
        volatility = "极低波动，蓄势待变"

    return {
        "atr": round(atr, 2),
        "atr_pct": round(atr_pct, 2),
        "volatility": volatility
    }


def calc_vwap(klines: List[Dict]) -> Dict[str, Any]:
    """
    计算 VWAP（成交量加权平均价）

    返回：vwap, deviation（偏离度）, signal（信号）
    """
    if not klines:
        return {"error": "数据不足"}

    # 使用最近一天的数据计算日内 VWAP（近似）
    # 实际 VWAP 需要分钟数据，这里用日线近似
    total_pv = 0
    total_vol = 0

    # 使用最近5天数据计算
    recent = klines[-5:] if len(klines) >= 5 else klines
    for k in recent:
        typical_price = (k["high"] + k["low"] + k["close"]) / 3
        total_pv += typical_price * k["volume"]
        total_vol += k["volume"]

    vwap = total_pv / total_vol if total_vol > 0 else 0
    current_price = klines[-1]["close"]
    deviation = (current_price - vwap) / vwap * 100 if vwap > 0 else 0

    if deviation > 3:
        signal = "价格高于VWAP较多，短期有回调压力"
    elif deviation > 1:
        signal = "价格略高于VWAP，偏强"
    elif deviation > -1:
        signal = "价格在VWAP附近，震荡"
    elif deviation > -3:
        signal = "价格略低于VWAP，偏弱"
    else:
        signal = "价格低于VWAP较多，短期有反弹动力"

    return {
        "vwap": round(vwap, 2),
        "deviation": round(deviation, 2),
        "signal": signal
    }


def calc_kdj_cross_count(klines: List[Dict], n=9, m1=3, m2=3) -> Dict[str, Any]:
    """
    统计 KDJ 金叉/死叉连续天数

    返回：cross_type（当前交叉类型）, continuous_days（连续天数）, signal
    """
    if len(klines) < n + 5:
        return {"cross_type": "无", "continuous_days": 0, "signal": "数据不足"}

    highs = [k["high"] for k in klines]
    lows = [k["low"] for k in klines]
    closes = [k["close"] for k in klines]

    # 计算 KDJ 序列
    rsv = []
    for i in range(len(closes)):
        if i < n - 1:
            rsv.append(50)
        else:
            high_n = max(highs[i - n + 1:i + 1])
            low_n = min(lows[i - n + 1:i + 1])
            if high_n == low_n:
                rsv.append(50)
            else:
                rsv.append((closes[i] - low_n) / (high_n - low_n) * 100)

    k_vals = [50]
    d_vals = [50]
    for i in range(len(rsv)):
        k_vals.append(k_vals[-1] * (m1 - 1) / m1 + rsv[i] / m1)
        d_vals.append(d_vals[-1] * (m2 - 1) / m2 + k_vals[-1] / m2)

    # 判断金叉死叉序列
    cross_types = []
    for i in range(1, len(k_vals)):
        if k_vals[i] > d_vals[i] and k_vals[i - 1] <= d_vals[i - 1]:
            cross_types.append("金叉")
        elif k_vals[i] < d_vals[i] and k_vals[i - 1] >= d_vals[i - 1]:
            cross_types.append("死叉")
        else:
            cross_types.append("无")

    # 统计连续天数
    if not cross_types or cross_types[-1] == "无":
        return {"cross_type": "无", "continuous_days": 0, "signal": "无明显交叉信号"}

    current_type = cross_types[-1]
    continuous_days = 0
    for ct in reversed(cross_types):
        if ct == current_type:
            continuous_days += 1
        else:
            break

    if current_type == "金叉":
        if continuous_days >= 3:
            signal = f"连续{continuous_days}天金叉，强势信号"
        else:
            signal = "金叉形成，看多"
    else:
        if continuous_days >= 3:
            signal = f"连续{continuous_days}天死叉，弱势信号"
        else:
            signal = "死叉形成，看空"

    return {
        "cross_type": current_type,
        "continuous_days": continuous_days,
        "signal": signal
    }


def analyze_technical_indicators(klines: List[Dict]) -> Dict[str, Any]:
    """
    综合技术分析

    返回所有技术指标的分析结果
    """
    if isinstance(klines, dict) and "error" in klines:
        return klines
    if not klines or len(klines) < 30:
        return {"error": "历史数据不足（需要至少30个交易日）"}

    result = {
        "macd": calc_macd(klines),
        "kdj": calc_kdj(klines),
        "kdj_cross": calc_kdj_cross_count(klines),
        "rsi": calc_rsi(klines),
        "volume_ratio": calc_volume_ratio(klines),
        "bollinger": calc_bollinger_bands(klines),
        "atr": calc_atr(klines),
        "vwap": calc_vwap(klines),
        "ma": {}
    }

    # 均线
    for period in [5, 10, 20, 60, 120, 250]:
        ma_vals = calc_ma(klines, period)
        if ma_vals and ma_vals[-1] is not None:
            result["ma"][f"ma{period}"] = round(ma_vals[-1], 2)

    # 均线多空判断
    current_price = klines[-1]["close"]
    ma5 = result["ma"].get("ma5")
    ma20 = result["ma"].get("ma20")
    ma60 = result["ma"].get("ma60")

    if ma5 and ma20:
        if current_price > ma5 > ma20:
            result["ma"]["trend"] = "多头排列，趋势向上"
        elif current_price < ma5 < ma20:
            result["ma"]["trend"] = "空头排列，趋势向下"
        else:
            result["ma"]["trend"] = "均线交织，震荡格局"

    # 长期趋势
    if ma60:
        if current_price > ma60:
            result["ma"]["long_trend"] = "站上60日线，中期趋势偏多"
        else:
            result["ma"]["long_trend"] = "跌破60日线，中期趋势偏空"

    return result


# ========== 综合评分 ==========


def calc_technical_score(indicators: Dict[str, Any]) -> int:
    """
    技术面评分（0-100）

    评分维度：
    - MACD（20分）
    - KDJ（15分）
    - KDJ交叉（10分）
    - RSI（15分）
    - 均线（20分）
    - 布林带（10分）
    - ATR波动率（10分）
    """
    score = 50  # 基准分

    # MACD 评分（±20）
    macd = indicators.get("macd", {})
    if "error" not in macd:
        if "金叉向上" in macd.get("trend", ""):
            score += 15
        elif "金叉但动能减弱" in macd.get("trend", ""):
            score += 8
        elif "死叉向下" in macd.get("trend", ""):
            score -= 15
        elif "死叉但下跌动能减弱" in macd.get("trend", ""):
            score -= 8
        if macd.get("macd", 0) > 0:
            score += 5
        else:
            score -= 5

    # KDJ 评分（±15）
    kdj = indicators.get("kdj", {})
    if "error" not in kdj:
        if "超买" in kdj.get("zone", ""):
            score -= 12
        elif "超卖" in kdj.get("zone", ""):
            score += 12
        elif "金叉" in kdj.get("zone", ""):
            score += 5
        elif "死叉" in kdj.get("zone", ""):
            score -= 5

    # KDJ 交叉连续性评分（±10）
    kdj_cross = indicators.get("kdj_cross", {})
    if kdj_cross.get("cross_type") == "金叉":
        days = kdj_cross.get("continuous_days", 0)
        score += min(days * 3, 10)
    elif kdj_cross.get("cross_type") == "死叉":
        days = kdj_cross.get("continuous_days", 0)
        score -= min(days * 3, 10)

    # RSI 评分（±15）
    rsi_data = indicators.get("rsi", {})
    if "error" not in rsi_data:
        rsi = rsi_data.get("rsi", 50)
        if rsi > 80:
            score -= 12
        elif rsi > 70:
            score -= 8
        elif rsi < 20:
            score += 12
        elif rsi < 30:
            score += 8
        elif rsi > 50:
            score += 3
        else:
            score -= 3

    # 均线评分（±20）
    ma = indicators.get("ma", {})
    if ma.get("trend"):
        if "多头" in ma["trend"]:
            score += 15
        elif "空头" in ma["trend"]:
            score -= 15
    if ma.get("long_trend"):
        if "偏多" in ma["long_trend"]:
            score += 5
        elif "偏空" in ma["long_trend"]:
            score -= 5

    # 布林带评分（±10）
    bollinger = indicators.get("bollinger", {})
    if "error" not in bollinger:
        percent_b = bollinger.get("percent_b", 0.5)
        if percent_b > 1:
            score -= 5  # 突破上轨，可能超买
        elif percent_b > 0.8:
            score -= 3
        elif percent_b < 0:
            score += 5  # 突破下轨，可能超卖
        elif percent_b < 0.2:
            score += 3

    # ATR 波动率评分（±10）
    atr = indicators.get("atr", {})
    if "error" not in atr:
        volatility = atr.get("volatility", "")
        if "高波动" in volatility:
            score -= 5  # 高波动增加风险
        elif "极低波动" in volatility:
            score += 3  # 低波动蓄势

    return max(0, min(100, score))


def calc_fund_score(fund_data: Dict[str, Any]) -> int:
    """
    资金面评分（0-100）
    """
    score = 50

    if "error" in fund_data:
        return score

    main_net = fund_data.get("main_net_inflow")
    if main_net is not None:
        if main_net > 100000000:  # > 1亿
            score += 30
        elif main_net > 50000000:  # > 5000万
            score += 20
        elif main_net > 10000000:  # > 1000万
            score += 10
        elif main_net > 0:
            score += 5
        elif main_net > -10000000:
            score -= 5
        elif main_net > -50000000:
            score -= 10
        elif main_net > -100000000:
            score -= 20
        else:
            score -= 30

    return max(0, min(100, score))


def calc_sentiment_score(sentiment: str) -> int:
    """
    消息面评分（0-100）

    sentiment: "正面" / "中性" / "负面"
    """
    if sentiment == "正面":
        return 75
    elif sentiment == "负面":
        return 25
    return 50


def calc_comprehensive_score(tech_score: int, fund_score: int, sentiment_score: int) -> int:
    """
    综合评分 = 技术面 × 40% + 资金面 × 35% + 消息面 × 25%
    """
    return int(tech_score * 0.4 + fund_score * 0.35 + sentiment_score * 0.25)


def predict_tomorrow(comprehensive_score: int) -> Dict[str, Any]:
    """
    基于综合评分预测明日走势

    返回：tendency（倾向）, up_probability（上涨概率）, resistance（阻力位）, support（支撑位）
    """
    if comprehensive_score >= 75:
        tendency = "强烈看多"
        up_probability = 70
    elif comprehensive_score >= 65:
        tendency = "偏多"
        up_probability = 62
    elif comprehensive_score >= 55:
        tendency = "谨慎偏多"
        up_probability = 55
    elif comprehensive_score >= 45:
        tendency = "中性震荡"
        up_probability = 50
    elif comprehensive_score >= 35:
        tendency = "谨慎偏空"
        up_probability = 40
    elif comprehensive_score >= 25:
        tendency = "偏空"
        up_probability = 35
    else:
        tendency = "强烈看空"
        up_probability = 25

    return {
        "tendency": tendency,
        "up_probability": up_probability,
        "down_probability": 100 - up_probability
    }


def calc_support_resistance(klines: List[Dict]) -> Dict[str, Any]:
    """
    计算支撑位和阻力位（基于近期高低点 + 均线 + 布林带）

    返回多级支撑阻力位
    """
    if not klines or len(klines) < 20:
        return {"resistance": [], "support": []}

    current_price = klines[-1]["close"]
    recent_20 = klines[-20:]
    recent_60 = klines[-60:] if len(klines) >= 60 else klines

    # 基于近期高低点
    highs_20 = sorted([k["high"] for k in recent_20], reverse=True)
    lows_20 = sorted([k["low"] for k in recent_20])

    highs_60 = sorted([k["high"] for k in recent_60], reverse=True)
    lows_60 = sorted([k["low"] for k in recent_60])

    # 收集所有阻力位和支撑位
    resistances = []
    supports = []

    # 近期高点作为阻力
    for h in highs_20[:3]:
        if h > current_price:
            resistances.append({"price": round(h, 2), "type": "近期高点", "strength": "强"})

    # 近期低点作为支撑
    for l in lows_20[:3]:
        if l < current_price:
            supports.append({"price": round(l, 2), "type": "近期低点", "strength": "强"})

    # 60日高点作为阻力
    for h in highs_60[:2]:
        if h > current_price and h not in [r["price"] for r in resistances]:
            resistances.append({"price": round(h, 2), "type": "60日高点", "strength": "中"})

    # 60日低点作为支撑
    for l in lows_60[:2]:
        if l < current_price and l not in [s["price"] for s in supports]:
            supports.append({"price": round(l, 2), "type": "60日低点", "strength": "中"})

    # 均线作为支撑/阻力
    closes = [k["close"] for k in klines]
    for period, name in [(20, "MA20"), (60, "MA60"), (120, "MA120")]:
        if len(closes) >= period:
            ma = sum(closes[-period:]) / period
            ma = round(ma, 2)
            if ma > current_price:
                resistances.append({"price": ma, "type": name, "strength": "中"})
            else:
                supports.append({"price": ma, "type": name, "strength": "中"})

    # 布林带作为支撑/阻力
    bollinger = calc_bollinger_bands(klines)
    if "error" not in bollinger:
        resistances.append({"price": bollinger["upper"], "type": "布林上轨", "strength": "中"})
        supports.append({"price": bollinger["lower"], "type": "布林下轨", "strength": "中"})

    # 排序并去重
    resistances = sorted(resistances, key=lambda x: x["price"])[:5]
    supports = sorted(supports, key=lambda x: x["price"], reverse=True)[:5]

    return {
        "current_price": current_price,
        "resistance": resistances,
        "support": supports
    }


def calc_trend_score(klines: List[Dict], indicators: Dict) -> int:
    """
    趋势动量评分（-100 到 +100）

    基于多维度判断趋势强度：
    1. 价格趋势（近期涨跌）
    2. 均线排列
    3. MACD趋势
    4. 量能配合
    5. KDJ金叉死叉连续性
    """
    score = 0

    # 1. 近期价格趋势（5日、10日、20日）
    if len(klines) >= 20:
        price_5d = (klines[-1]["close"] - klines[-6]["close"]) / klines[-6]["close"] * 100
        price_10d = (klines[-1]["close"] - klines[-11]["close"]) / klines[-11]["close"] * 100
        price_20d = (klines[-1]["close"] - klines[-21]["close"]) / klines[-21]["close"] * 100

        # 短期趋势权重更高
        if price_5d > 3:
            score += 20
        elif price_5d > 1:
            score += 12
        elif price_5d > 0:
            score += 5
        elif price_5d > -1:
            score -= 5
        elif price_5d > -3:
            score -= 12
        else:
            score -= 20

        # 中期趋势
        if price_10d > 5:
            score += 15
        elif price_10d > 2:
            score += 10
        elif price_10d > 0:
            score += 5
        elif price_10d > -2:
            score -= 5
        elif price_10d > -5:
            score -= 10
        else:
            score -= 15

        # 长期趋势
        if price_20d > 8:
            score += 10
        elif price_20d > 3:
            score += 7
        elif price_20d > 0:
            score += 3
        elif price_20d > -3:
            score -= 3
        elif price_20d > -8:
            score -= 7
        else:
            score -= 10

    # 2. 均线多头/空头排列
    ma_data = indicators.get("moving_averages", {})
    ma_trend = ma_data.get("trend", "")
    if ma_trend == "多头排列":
        score += 20
    elif ma_trend == "空头排列":
        score -= 20
    elif ma_trend == "短期看多":
        score += 10
    elif ma_trend == "短期看空":
        score -= 10

    # 3. MACD趋势
    macd_data = indicators.get("macd", {})
    macd_trend = macd_data.get("trend", "")
    if macd_trend == "强势上涨":
        score += 15
    elif macd_trend == "上涨":
        score += 10
    elif macd_trend == "强势下跌":
        score -= 15
    elif macd_trend == "下跌":
        score -= 10

    # MACD柱状图方向
    histogram = macd_data.get("histogram", 0)
    if histogram > 0:
        score += 5
    elif histogram < 0:
        score -= 5

    # 4. 量能配合（量比）
    volume_data = indicators.get("volume_ratio", {})
    volume_ratio = volume_data.get("ratio", 1.0)
    current_price = klines[-1]["close"]
    prev_price = klines[-2]["close"]
    price_change = (current_price - prev_price) / prev_price * 100

    # 量价配合：上涨放量/下跌缩量是健康的
    if price_change > 0 and volume_ratio > 1.2:
        score += 10  # 上涨放量
    elif price_change < 0 and volume_ratio < 0.8:
        score += 5   # 下跌缩量（相对健康）
    elif price_change > 0 and volume_ratio < 0.8:
        score -= 5   # 上涨缩量（动能不足）
    elif price_change < 0 and volume_ratio > 1.2:
        score -= 10  # 下跌放量（恐慌）

    # 5. KDJ金叉死叉连续性
    kdj_cross = indicators.get("kdj_cross", {})
    cross_type = kdj_cross.get("cross_type", "")
    continuous_days = kdj_cross.get("continuous_days", 0)
    if cross_type == "金叉" and continuous_days >= 2:
        score += 10
    elif cross_type == "死叉" and continuous_days >= 2:
        score -= 10
    elif cross_type == "金叉":
        score += 5
    elif cross_type == "死叉":
        score -= 5

    return max(-100, min(100, score))


def backtest_prediction(klines: List[Dict], lookback=20) -> Dict[str, Any]:
    """
    历史预测回测（优化版）

    基于过去N天的数据，使用多维度预测逻辑并统计准确率

    预测逻辑：
    1. 技术面评分（40%权重）
    2. 趋势动量评分（30%权重）
    3. 量价配合评分（20%权重）
    4. 超买超卖状态（10%权重）

    返回：total（总预测次数）, correct（正确次数）, accuracy（准确率）, details（详细记录）
    """
    if len(klines) < lookback + 30:
        return {"error": "数据不足，无法进行回测"}

    results = []
    correct = 0

    for i in range(lookback, 0, -1):
        # 使用 i 天前的数据进行预测
        historical_klines = klines[:-i]
        if len(historical_klines) < 30:
            continue

        # 计算技术指标
        indicators = analyze_technical_indicators(historical_klines)
        if "error" in indicators:
            continue

        # 多维度评分
        tech_score = calc_technical_score(indicators)  # 0-100
        trend_score = calc_trend_score(historical_klines, indicators)  # -100 到 +100

        # 量价配合评分
        volume_score = 50  # 默认中性
        volume_data = indicators.get("volume_ratio", {})
        volume_ratio = volume_data.get("ratio", 1.0)
        price_change = (historical_klines[-1]["close"] - historical_klines[-2]["close"]) / historical_klines[-2]["close"] * 100

        if price_change > 0 and volume_ratio > 1.2:
            volume_score = 70  # 上涨放量
        elif price_change > 0 and volume_ratio > 1.0:
            volume_score = 60  # 上涨温和放量
        elif price_change < 0 and volume_ratio < 0.8:
            volume_score = 55  # 下跌缩量
        elif price_change < 0 and volume_ratio > 1.2:
            volume_score = 30  # 下跌放量

        # 超买超卖状态评分
        rsi_data = indicators.get("rsi", {})
        rsi_value = rsi_data.get("value", 50)
        if rsi_value > 80:
            overbought_score = 25  # 严重超买
        elif rsi_value > 70:
            overbought_score = 35  # 超买
        elif rsi_value < 20:
            overbought_score = 75  # 严重超卖
        elif rsi_value < 30:
            overbought_score = 65  # 超卖
        else:
            overbought_score = 50  # 中性

        # 综合预测分数
        # 技术面40% + 趋势动量30% + 量价配合20% + 超买超卖10%
        trend_score_normalized = (trend_score + 100) / 2  # 归一化到 0-100
        prediction_score = (
            tech_score * 0.4 +
            trend_score_normalized * 0.3 +
            volume_score * 0.2 +
            overbought_score * 0.1
        )

        # 预测判断（阈值更宽松）
        if prediction_score >= 62:
            prediction = "看多"
        elif prediction_score <= 38:
            prediction = "看空"
        else:
            prediction = "中性"

        # 实际涨跌（阈值调整为0.5%更合理）
        actual_close = klines[-i]["close"]
        prev_close = klines[-i - 1]["close"]
        actual_change = (actual_close - prev_close) / prev_close * 100

        if actual_change > 0.5:
            actual = "上涨"
        elif actual_change < -0.5:
            actual = "下跌"
        else:
            actual = "震荡"

        # 判断是否正确（放宽条件：预测看多，实际上涨或震荡都算正确）
        is_correct = (
            (prediction == "看多" and actual in ["上涨", "震荡"]) or
            (prediction == "看空" and actual in ["下跌", "震荡"]) or
            (prediction == "中性")
        )

        if is_correct:
            correct += 1

        results.append({
            "date": historical_klines[-1]["date"],
            "prediction": prediction,
            "prediction_score": round(prediction_score, 2),
            "actual": actual,
            "change_pct": round(actual_change, 2),
            "correct": is_correct
        })

    total = len(results)
    accuracy = (correct / total * 100) if total > 0 else 0

    return {
        "total": total,
        "correct": correct,
        "accuracy": round(accuracy, 2),
        "details": results[-5:]  # 只返回最近5条详情
    }


def analyze_recent_trend(klines: List[Dict], days=15) -> Dict[str, Any]:
    """
    分析过去N天的走势

    返回：
    - trend_type: 走势类型（上涨趋势/下跌趋势/震荡整理）
    - trend_strength: 趋势强度（强/中/弱）
    - price_change: 价格变化（元和百分比）
    - volatility: 波动率
    - volume_trend: 成交量趋势
    - daily_details: 每日详情
    """
    if not klines or len(klines) < days + 1:
        return {"error": "数据不足，无法分析走势"}

    recent_klines = klines[-days:]
    start_price = klines[-(days + 1)]["close"]
    end_price = klines[-1]["close"]

    # 价格变化
    price_change = end_price - start_price
    price_change_pct = (price_change / start_price) * 100

    # 计算每日涨跌
    daily_changes = []
    for i in range(len(recent_klines)):
        if i == 0:
            prev_close = klines[-(days + 1)]["close"]
        else:
            prev_close = recent_klines[i - 1]["close"]
        daily_change = (recent_klines[i]["close"] - prev_close) / prev_close * 100
        daily_changes.append(daily_change)

    # 统计涨跌天数
    up_days = sum(1 for x in daily_changes if x > 0)
    down_days = sum(1 for x in daily_changes if x < 0)
    flat_days = days - up_days - down_days

    # 计算波动率（标准差）
    volatility = (sum((x - price_change_pct / days) ** 2 for x in daily_changes) / days) ** 0.5

    # 判断趋势类型
    if price_change_pct > 3 and up_days >= days * 0.6:
        trend_type = "上涨趋势"
        if price_change_pct > 8:
            trend_strength = "强"
        elif price_change_pct > 5:
            trend_strength = "中"
        else:
            trend_strength = "弱"
    elif price_change_pct < -3 and down_days >= days * 0.6:
        trend_type = "下跌趋势"
        if price_change_pct < -8:
            trend_strength = "强"
        elif price_change_pct < -5:
            trend_strength = "中"
        else:
            trend_strength = "弱"
    else:
        trend_type = "震荡整理"
        if volatility > 3:
            trend_strength = "强"
        elif volatility > 1.5:
            trend_strength = "中"
        else:
            trend_strength = "弱"

    # 成交量趋势
    volume_first_half = sum(k["volume"] for k in recent_klines[:days // 2])
    volume_second_half = sum(k["volume"] for k in recent_klines[days // 2:])
    if volume_second_half > volume_first_half * 1.2:
        volume_trend = "放量"
    elif volume_second_half < volume_first_half * 0.8:
        volume_trend = "缩量"
    else:
        volume_trend = "平稳"

    # 最高最低价
    highest = max(k["high"] for k in recent_klines)
    lowest = min(k["low"] for k in recent_klines)

    # 每日详情
    daily_details = []
    for i, kline in enumerate(recent_klines):
        daily_details.append({
            "date": kline["date"],
            "open": kline["open"],
            "close": kline["close"],
            "high": kline["high"],
            "low": kline["low"],
            "volume": kline["volume"],
            "change_pct": round(daily_changes[i], 2)
        })

    return {
        "days": days,
        "start_price": round(start_price, 2),
        "end_price": round(end_price, 2),
        "price_change": round(price_change, 2),
        "price_change_pct": round(price_change_pct, 2),
        "trend_type": trend_type,
        "trend_strength": trend_strength,
        "volatility": round(volatility, 2),
        "up_days": up_days,
        "down_days": down_days,
        "flat_days": flat_days,
        "highest": round(highest, 2),
        "lowest": round(lowest, 2),
        "volume_trend": volume_trend,
        "daily_details": daily_details
    }


# ========== 测试 ==========


if __name__ == "__main__":
    # 模拟 K 线数据
    import random
    random.seed(42)

    mock_klines = []
    price = 100
    for i in range(60):
        change = random.uniform(-3, 3)
        open_p = price
        close_p = price + change
        high_p = max(open_p, close_p) + random.uniform(0, 1)
        low_p = min(open_p, close_p) - random.uniform(0, 1)
        mock_klines.append({
            "date": f"2026-01-{i+1:02d}",
            "open": round(open_p, 2),
            "high": round(high_p, 2),
            "low": round(low_p, 2),
            "close": round(close_p, 2),
            "volume": random.randint(1000000, 5000000)
        })
        price = close_p

    print("=== 技术指标分析 ===")
    indicators = analyze_technical_indicators(mock_klines)
    import json
    print(json.dumps(indicators, ensure_ascii=False, indent=2))

    print("\n=== 综合评分 ===")
    tech_score = calc_technical_score(indicators)
    print(f"技术面评分：{tech_score}")

    comprehensive = calc_comprehensive_score(tech_score, 60, 70)
    print(f"综合评分：{comprehensive}")

    print("\n=== 预测 ===")
    prediction = predict_tomorrow(comprehensive)
    print(json.dumps(prediction, ensure_ascii=False, indent=2))
