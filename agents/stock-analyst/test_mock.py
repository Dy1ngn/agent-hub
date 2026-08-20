#!/usr/bin/env python3
"""
完整测试：评分逻辑 + 新功能
"""

import random
import sys
from analyzer import (
    calc_support_resistance,
    calc_comprehensive_score,
    predict_tomorrow,
    backtest_prediction,
    analyze_recent_trend,
    analyze_technical_indicators,
    calc_technical_score
)

# 生成模拟 K 线数据
random.seed(42)

mock_klines = []
price = 100
for i in range(120):
    change = random.gauss(0, 2)
    price = max(50, price * (1 + change / 100))
    volume = random.randint(1000000, 5000000)

    mock_klines.append({
        "date": f"2026-{7 + i // 30:02d}-{1 + i % 30:02d}",
        "open": round(price * (1 - random.uniform(0, 0.02)), 2),
        "high": round(price * (1 + random.uniform(0, 0.03)), 2),
        "low": round(price * (1 - random.uniform(0, 0.03)), 2),
        "close": round(price, 2),
        "volume": volume
    })

print("=" * 60)
print("股票分析 Agent 完整测试（v1.2.0）")
print("=" * 60)

# 测试1: 多级支撑阻力位
print("\n【测试1】多级支撑阻力位")
print("-" * 40)
sr = calc_support_resistance(mock_klines)
print(f"当前价格: {sr['current_price']}")
print(f"阻力位数量: {len(sr['resistance'])}")
print(f"支撑位数量: {len(sr['support'])}")
if sr['resistance']:
    print(f"第一阻力位: {sr['resistance'][0]['price']} ({sr['resistance'][0]['type']})")
if sr['support']:
    print(f"第一支撑位: {sr['support'][0]['price']} ({sr['support'][0]['type']})")

# 测试2: 综合评分
print("\n【测试2】综合评分")
print("-" * 40)
test_cases = [
    (90, 80, 70, "强烈看多"),
    (50, 50, 50, "中性"),
    (20, 20, 20, "强烈看空"),
]
for tech, fund, sentiment, expected in test_cases:
    score = calc_comprehensive_score(tech, fund, sentiment)
    prediction = predict_tomorrow(score)
    print(f"技术面={tech}, 资金面={fund}, 消息面={sentiment} → 综合={score}, 预测={prediction['tendency']}")

# 测试3: 优化后的回测准确率
print("\n【测试3】优化后的回测准确率")
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

# 测试4: 过去15天走势分析
print("\n【测试4】过去15天走势分析")
print("-" * 40)
trend = analyze_recent_trend(mock_klines, days=15)
if "error" not in trend:
    print(f"走势类型: {trend['trend_type']}")
    print(f"趋势强度: {trend['trend_strength']}")
    print(f"价格变化: {trend['price_change']} ({trend['price_change_pct']}%)")
    print(f"上涨/下跌天数: {trend['up_days']}/{trend['down_days']}")
    print(f"波动率: {trend['volatility']}%")
    print(f"成交量趋势: {trend['volume_trend']}")

print("\n" + "=" * 60)
print("所有测试完成！")
print("=" * 60)
