# 三仓库架构 (Three-Repo Architecture)

## 概览
投资系统拆分为三个独立 Git 仓库, 各自独立部署、独立版本、独立演进。
仓库间通过共享数据文件 (SQLite/CSV/NDJSON) 通信, 不做直接代码 import。

```
┌─────────────────┐    SQLite/CSV    ┌─────────────────┐
│  SharedSignals  │ ───────────────▶ │   MarketGraph   │
│  数据采集+存储   │                  │  战略研究 (只读)  │
└─────────────────┘                  └─────────────────┘
        │                                     │
        │ SQLite/CSV                     研究结论 (CSV)
        ▼                                     ▼
┌─────────────────┐    共享模块调用     ┌─────────────────┐
│     Tradings    │ ◀────────────────  │   (消费研究结论)  │
│  交易全闭环      │                    │                 │
└─────────────────┘                    └─────────────────┘
```

## 仓库清单
| 仓库 | 地址 | 职责 |
|------|------|------|
| SharedSignals | https://github.com/NicholasHan1226/SharedSignals.git | 数据采集 + 存储 |
| MarketGraph | https://github.com/NicholasHan1226/MarketGraph.git | 战略研究 |
| Tradings | https://github.com/NicholasHan1226/Tradingagent.cc.git | 交易线 |

---

## 1. SharedSignals — 数据采集与存储
**地址**: https://github.com/NicholasHan1226/SharedSignals.git
**本地路径**: /opt/investment/SharedSignals/

### 职责
统一采集所有外部数据源, 去重入库, 供研究线和交易线共享读取。
不分析、不分类、不做交易决策。

### 文件结构
- `collectors/` — 各数据源采集器
  - Tushare (14接口): A股行情/财务/资金/期货/港股
  - Binance (4接口): 加密货币行情
  - Polymarket (3接口): 预测市场
  - RSS (883源) + Tavily + agents → 事件采集
- `storage/` — 数据库 schema 和管理
  - SQLite: marketdata.sqlite (75MB) + reference_index.sqlite (5MB)
  - 未来: DuckDB (analytics), 当前 SQLite
- `bridge/` — staging→DB 归并桥 (runtime_bridge)
  - 6 streams: event_candidates / sentiment_signals / collection_runs / ...
- `reference/` — 参考数据
  - stock_master / source_registry / entity_map / market_calendar
- `memory/` — 采集层记忆
- `patrol.py` — 巡查 (来源健康 / 数据新鲜度)
- `heal.py` — 自愈 (切换备用源 / 补采)

### 输出
- SQLite (主) + CSV (缓存) + NDJSON (staging)
- 消费方: MarketGraph (只读), Tradings (直接读)

---

## 2. MarketGraph — 战略研究
**地址**: https://github.com/NicholasHan1226/MarketGraph.git
**本地路径**: /opt/investment/MarketGraph/

### 职责
宏观战略研究层。复刻桥水 All Weather 框架:
regime 检测、因果影响引擎、风险平价分配、前向验证。
不做快事件、不做交易。

### 文件结构 (10个领域模块)
- `00-Shared-Kernel/` — 共享内核 (契约/引用/内存)
- `01-Asset-Universe/` — 资产主数据
- `02-Industry-Enterprise-Graph/` — 行业企业图谱
- `03-Events-Signals/` — 事件信号采集
- `04-Macro-CrossAsset/` — 宏观跨资产 (regime)
- `05-Impact-Propagation/` — 因果影响引擎
  - 事件 → 受影响板块/个股排序
  - 方向 (受益/受损) + 置信度 + 因果链 + 历史兑现率
- `06-Portfolio-Risk/` — 组合风控
- `07-Review-Calibration/` — 复盘校准 (前向OOS验证)
- `08-Market-Interfaces/` — 市场接口 (MCP 只读)
- `09-AllWeather/` — All Weather 风险平价分配

### 对外能力
- MCP 工具: regime / impact_query / all_weather / news_brief / decision_draft
- 契约表: causal_truth_table / market_knowledge_packages
- 只读研究, 不执行交易

### 输入
- SharedSignals 的 SQLite + CSV (只读)

### 输出
- 研究结论 (CSV) → Tradings 消费
- 不回传交易结果 (保持研究独立)

---

## 3. Tradings — 交易线
**地址**: https://github.com/NicholasHan1226/Tradingagent.cc.git
**本地路径**: /opt/investment/Tradings/

### 职责
交易全闭环。覆盖多个市场, 共享通用模块 + 市场特定逻辑。

### 文件结构
- `shared/` — 跨市场共享模块 (约20个工具)
  - 筛选 → 对抗 → 风控 → 组合 → 执行 → 复盘
  - 权重式打分, 不设硬门禁
- 市场特定:
  - `Ashare/` — A股 (含 Hermes 同花顺自动化执行)
  - `HK/` — 港股
  - `US/` — 美股
  - `Crypto/` — 加密货币
  - `PM/` — 组合管理

### 输入
- SharedSignals 原始数据 (直接读 SQLite/CSV)
- MarketGraph 研究结论 (CSV)

### 输出
- 交易信号 / 模拟盘 / 影子盘记录
- 不回传研究层 (保持研究独立)

---

## 通信机制

### 当前 (基于文件)
| 通道 | 格式 | 方向 |
|------|------|------|
| 主数据库 | SQLite (marketdata.sqlite) | SharedSignals → 所有 |
| 缓存 | CSV | SharedSignals → 所有 |
| 事件staging | NDJSON → CSV (bridge) | SharedSignals → 所有 |
| 研究结论 | CSV | MarketGraph → Tradings |

**关键约束**: 仓库间不做直接代码 import。所有数据交换通过文件。

### 未来扩展
| 通道 | 用途 | 状态 |
|------|------|------|
| Redis pub/sub | 实时事件推送 (替代文件轮询) | 规划中 |
| DuckDB | 分析型查询 (替代 SQLite) | 规划中 |
| 多租户 MCP | 多账户隔离的只读研究接口 | 规划中 |

---

## 边界原则
1. **采集一次, 两线共享** — SharedSignals 是唯一采集入口, 不重复
2. **MarketGraph 不做交易** — 只做战略研究, 输出结论
3. **Tradings 不回传** — 交易结果不回研究层, 保持 OOS 纯度
4. **文件通信** — 仓库间无代码依赖, 通过 SQLite/CSV 交换
5. **独立部署** — 三个仓库独立 git, 独立版本, 独立 CI

---

## 服务器分布
| 仓库 | 杭州 (8.138.181.177) | 新加坡 (47.82.153.58) | Mac Mini |
|------|:---:|:---:|:---:|
| SharedSignals | ✓ (主) | ✓ (RSS节点) | — |
| MarketGraph | ✓ | — | — |
| Tradings | ✓ (模拟/影子) | — | ✓ (A股实盘) |
