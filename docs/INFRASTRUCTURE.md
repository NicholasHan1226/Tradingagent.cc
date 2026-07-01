# Infrastructure

## 服务器
| 角色 | IP | 规格 | 职责 |
|------|-----|------|------|
| 杭州 (主) | 8.138.181.177 | 2核3.4GB/99GB | 所有系统 (除A股实盘) |
| 新加坡 | 47.82.153.58 | 30GB | 境外RSS采集 (495源) |
| Mac Mini | 本地 | — | A股实盘执行 (Hermes同花顺) |

## 域名
- tradingagent.cc — 统一域名
- dashboard.tradingagent.cc — Cloudflare前端看板 (未来)
- api.tradingagent.cc — API反代 (未来)

## 邮件
- 交易类: notice@tradingagent.cc → tradingadviser@coze.email
- 系统类: notice@tradingagent.cc → soc@coze.email

## 环境
- Python: 3.12.3 (venv /opt/marketgraph/venv)
- OS: Ubuntu 24.04
- 无DuckDB/Redis (3.4GB RAM限制, 未来扩展)
- SQLite: marketdata.sqlite(75MB) + reference_index.sqlite(5MB) + rss_collector.db

## 网络
- Nginx :80 → 127.0.0.1:8080 (API server)
- RSSHub :1200 (Node.js)
- Mihomo :7890/:7891 (Clash代理, Binance/Polymarket走代理)
- 新加坡 → rsync → 杭州 staging (每5min)

## API Keys
- 详见 .env (不在此文档记录值)
- Tushare/Firecrawl/Tavily/DeepSeek 4个key

## Git Repositories
- SharedSignals: https://github.com/NicholasHan1226/SharedSignals.git
- MarketGraph: https://github.com/NicholasHan1226/MarketGraph.git
- TradingAgent: https://github.com/NicholasHan1226/Tradingagent.cc.git
