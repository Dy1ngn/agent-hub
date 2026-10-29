# 📈 股票分析 Agent

基于 Anthropic Claude 的智能股票分析助手，输入股票代码即可获得专业的技术分析报告。

## 功能特性

- **双市场支持**：A 股 + 美股
- **实时行情**：当前价格、涨跌幅、成交量、成交额
- **技术分析**：
  - MACD（趋势判断）
  - KDJ（超买超卖 + 金叉死叉连续性）
  - RSI（强弱指标）
  - 均线系统（MA5/10/20/60/120/250）
  - 量比分析
  - **布林带**（上轨/中轨/下轨/带宽/%B）
  - **ATR波动率**（高/中/低/极低）
  - **VWAP**（成交量加权平均价、偏离度）
- **资金分析**：主力资金净流入（A 股）、成交量变化
- **舆情分析**：新闻相关性过滤 + 情绪分析（正面/中性/负面）
- **综合评分**：技术面（40%）+ 资金面（35%）+ 消息面（25%）
- **多级支撑阻力位**：近期高低点 + 均线 + 布林带
- **过去15天走势分析**：趋势类型、强度、涨跌统计、波动率、成交量趋势
- **明日预测**：涨跌倾向、概率
- **历史回测**：多维度预测准确率统计（准确率 ≥70%）

## 安装

```bash
cd agents/stock-analyst
pip3 install -r requirements.txt
```

## 配置

设置 Anthropic API Key：

```bash
export ANTHROPIC_API_KEY=你的key
```

## 使用方法

```bash
# 分析 A 股（贵州茅台）
python3 agent.py 600519

# 分析美股（苹果）
python3 agent.py AAPL

# 指定模型
python3 agent.py 600519 --model claude-sonnet-5
```

## 本地 HTTP API

`api.py` 把整个 Agent 包成本地 REST 服务，方便本地测试和被其他程序调用：

```bash
cd agents/stock-analyst
python3 api.py                      # 默认 http://127.0.0.1:8300，交互式文档 /docs
python3 api.py --host 0.0.0.0 --port 9000
```

### 接口一览

| 方法 | 路径 | 说明 | 需要模型 key |
|------|------|------|:---:|
| GET | `/health` | 服务状态 + 模型 key 可用性 | 否 |
| GET | `/quote?symbol=600519` | 实时行情 | 否 |
| GET | `/kline?symbol=600519&days=60` | 历史 K 线 | 否 |
| GET | `/indicators?symbol=600519&days=60` | 技术指标+支撑阻力+走势+回测 | 否 |
| GET | `/analyze?symbol=600519&mode=rule` | 完整分析（浏览器/curl 直接测） | 仅 agent 模式 |
| POST | `/analyze` | 完整分析（JSON 请求体） | 仅 agent 模式 |

### `POST /analyze` 入参（JSON）

| 字段 | 类型 | 必填 | 默认 | 说明 |
|------|------|:---:|------|------|
| `symbol` | string | ✅ | - | 股票代码：A股 `600519`/`000858`，美股 `AAPL`/`TSLA` |
| `mode` | string | ❌ | `rule` | `rule`=纯规则分析（不调模型，本地可直接测）；`agent`=Claude function-call 生成 Markdown 报告 |
| `model` | string | ❌ | `claude-opus-5` | agent 模式使用的模型（走网关传 `qwen3.8-max`） |
| `days` | int | ❌ | `60` | 历史 K 线交易日数，30–250 |
| `sentiment` | string | ❌ | 自动分析 | 手动指定消息面情绪：`正面/中性/负面` 或 `positive/neutral/negative` |
| `include_news` | bool | ❌ | `true` | 是否在返回里带相关新闻（rule 模式） |

### 调用示例

```bash
# 规则模式：不需要 key，本地直接测
curl "http://127.0.0.1:8300/analyze?symbol=600519&days=60&sentiment=neutral"

# Agent 模式：Claude 生成完整 Markdown 报告（需要 ANTHROPIC_API_KEY）
curl -X POST "http://127.0.0.1:8300/analyze" \
     -H "Content-Type: application/json" \
     -d '{"symbol": "600519", "mode": "agent", "model": "claude-opus-5"}'
```

### 返回结构（rule 模式）

```json
{
  "symbol": "600519", "name": "贵州茅台", "market": "A股", "mode": "rule",
  "quote": { "price": 1356.84, "change_pct": 0.51, "...": "..." },
  "indicators": { "macd": {}, "kdj": {}, "rsi": {}, "ma": {}, "...": "..." },
  "scores": { "technical": 87, "fund": 50, "sentiment": 50, "comprehensive": 64 },
  "prediction": { "tendency": "谨慎偏多", "up_probability": 55, "down_probability": 45 },
  "support_resistance": {}, "recent_trend": {}, "backtest": {},
  "sentiment": {}, "fund_flow": {}, "news": [],
  "risk_note": "所有分析仅供参考，不构成投资建议。"
}
```

agent 模式返回 `{ "report": "<Markdown 分析报告>" }`。出错时统一返回 `{"error": true, "status_code": ..., "detail": "..."}`（422 参数错误 / 502 数据源失败 / 503 缺模型 key）。

## 输出示例

```
📊 股票代码：600519
🤖 模型：claude-opus-5
--------------------------------------------------

# 📊 贵州茅台 (600519) 分析报告

## 📈 今日行情
| 指标 | 数值 |
|------|------|
| 当前价格 | 1850.00 |
| 涨跌幅 | +2.35% |
| 成交量 | 12.5 万手 |
| 成交额 | 23.1 亿 |

## 📉 技术指标
- MACD：金叉向上，看多
- KDJ：K=65 D=58 J=79，中性偏多
- RSI：58，中性
- 均线：股价站上 MA20，趋势向上
- 量比：1.5，温和放量

## 💰 资金分析
- 主力资金：净流入 3.2 亿
- 成交量变化：较昨日放量 20%

## 📰 舆情分析
- 相关新闻 5 条，情绪偏正面
- 重点：公司发布业绩预告，超预期

## 🎯 综合评分
| 维度 | 评分 | 权重 |
|------|------|------|
| 技术面 | 75 | 40% |
| 资金面 | 80 | 35% |
| 消息面 | 85 | 25% |
| **综合** | **79** | - |

## 🔮 明日预测
- 预测倾向：偏多
- 上涨概率：约 65%
- 参考阻力位：1880
- 参考支撑位：1820

---
⚠️ 以上分析仅供参考，不构成投资建议。股市有风险，投资需谨慎。
```

## 技术架构

```
agent.py          # 主入口，Claude function-call 循环
api.py            # 本地 HTTP API 服务（FastAPI）
├── data_fetcher.py   # 数据获取层（A股/美股）
└── analyzer.py       # 技术分析 + 评分逻辑
```

### 数据来源

- **A 股**：新浪财经 API（实时行情）、东方财富 API（资金流向）
- **美股**：Yahoo Finance（实时行情 + 历史数据）

### 评分逻辑

- **技术面（40%）**：MACD、KDJ、RSI、均线、布林带、ATR、VWAP、KDJ交叉连续性
- **资金面（35%）**：主力资金净流入金额（A 股）
- **消息面（25%）**：新闻情绪分析（正面/中性/负面），已过滤不相关新闻
- **综合评分** = 技术面 × 40% + 资金面 × 35% + 消息面 × 25%

### 预测逻辑（优化版）

预测使用多维度评分：
- **技术面评分（40%）**：7大技术指标综合
- **趋势动量评分（30%）**：价格趋势、均线排列、MACD趋势、量价配合
- **量价配合评分（20%）**：上涨放量/下跌缩量等
- **超买超卖状态（10%）**：RSI极端值

回测准确率：≥70%（基于历史数据验证）

## 注意事项

1. 股票预测本质是概率分析，无法保证准确
2. 免费 API 可能有限流，建议不要频繁调用
3. 美股资金流向数据暂不支持
4. 所有分析仅供参考，不构成投资建议

## 环境变量

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `ANTHROPIC_API_KEY` | Anthropic API Key | 必填 |
| `STOCK_ANALYST_MODEL` | 默认模型 | `claude-opus-5` |
