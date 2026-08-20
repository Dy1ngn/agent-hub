#!/usr/bin/env python3
"""
测试新增功能：优化后的回测准确率 + 过去15天走势分析
"""

import random
import sys
from analyzer import (
    analyze_technical_indicators,
    calc_technical_score,
    calc_trend_score,
    backtest_prediction,
    analyze_recent_trend
)

# 生成模拟 K 线数据（模拟真实股票走势）
random.seed(42)

mock_klines = []
price = 100

# 生成120天的K线数据
for i in range(120):
    # 模拟趋势：前30天上涨，中间40天震荡，后50天先跌后涨
    if i < 30:
        trend = 0.3  # 上涨趋势
    elif i < 70:
        trend = -0.1  # 震荡
    elif i < 90:
        trend = -0.4  # 下跌
    else:
        trend = 0.5  # 上涨

    change = random.gauss(trend, 1.5)  # 均值为趋势，标准差1.5

    price = max(50, price * (1 + change / 100))
    volume = random.randint(1000000, 5000000) * (1 + random.gauss(0, 0.3))

    mock_klines.append({
        "date": f"2026-{7 + i // 30:02d}-{1 + i % 30:02d}",
        "open": round(price * (1 - random.uniform(0, 0.02)), 2),
        "high": round(price * (1 + random.uniform(0, 0.03)), 2),
        "low": round(price * (1 - random.uniform(0, 0.03)), 2),
        "close": round(price, 2),
        "volume": int(volume)
    })

print("=" * 60)
print("测试新增功能：优化后的回测准确率 + 过去15天走势分析")
print("=" * 60)

# 测试1: 趋势动量评分
print("\n【测试1】趋势动量评分")
print("-" * 40)
indicators = analyze_technical_indicators(mock_klines)
if "error" not in indicators:
    trend_score = calc_trend_score(mock_klines, indicators)
    print(f"趋势动量评分: {trend_score}")
    if trend_score > 0:
        print(f"判断: 看多趋势 (+{trend_score})")
    elif trend_score < 0:
        print(f"判断: 看空趋势 ({trend_score})")
    else:
        print(f"判断: 中性趋势")
else:
    print(f"错误: {indicators['error']}")

# 测试2: 优化后的回测准确率
print("\n【测试2】优化后的回测准确率")
print("-" * 40)
backtest = backtest_prediction(mock_klines, lookback=20)
if "error" not in backtest:
    print(f"总预测次数: {backtest['total']}")
    print(f"正确次数: {backtest['correct']}")
    print(f"准确率: {backtest['accuracy']}%")

    if backtest['accuracy'] >= 70:
        print("✅ 准确率达标 (>=70%)")
    else:
        print("⚠️  准确率未达标 (<70%)")

    print("\n最近5天回测详情:")
    for detail in backtest.get('details', []):
        status = "✅" if detail['correct'] else "❌"
        print(f"  {detail['date']}: 预测={detail['prediction']} (分数={detail['prediction_score']}), "
              f"实际={detail['actual']} ({detail['change_pct']}%), {status}")
else:
    print(f"错误: {backtest['error']}")

# 测试3: 过去15天走势分析
print("\n【测试3】过去15天走势分析")
print("-" * 40)
trend = analyze_recent_trend(mock_klines, days=15)
if "error" not in trend:
    print(f"起始价格: {trend['start_price']}")
    print(f"结束价格: {trend['end_price']}")
    print(f"价格变化: {trend['price_change']} ({trend['price_change_pct']}%)")
    print(f"走势类型: {trend['trend_type']}")
    print(f"趋势强度: {trend['trend_strength']}")
    print(f"波动率: {trend['volatility']}%")
    print(f"上涨天数: {trend['up_days']}")
    print(f"下跌天数: {trend['down_days']}")
    print(f"平盘天数: {trend['flat_days']}")
    print(f"最高价: {trend['highest']}")
    print(f"最低价: {trend['lowest']}")
    print(f"成交量趋势: {trend['volume_trend']}")

    print("\n每日详情:")
    for day in trend.get('daily_details', [])[-5:]:  # 只显示最近5天
        print(f"  {day['date']}: 收盘={day['close']}, 涨跌={day['change_pct']}%")
else:
    print(f"错误: {trend['error']}")

print("\n" + "=" * 60)
print("测试完成！")
print("=" * 60)
