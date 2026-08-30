# Crypto 收益核算、低换手基准与前向验证（2026-08-30）

范围：research/simulation-only；不修改 K10、资本、订单、原观测链、旧报告或实盘开关。
候选基于 GitHub main `80966e9a44f952c9917c54a877f50c6c30bd57cb`，独立分支
`codex/crypto-research-accounting-baselines`。本报告不构成盈利、部署或晋级证明。

## 交付增量

1. `research_accounting.py`：固定币数量的线性合约两腿核算、真实执行名义金额
   计费、逐事件资金费、总投入资本分母、完全抵押及采样标记价保证金筛查。
   现货与合约同方向同幅价格变化的等数量对冲不产生虚假残差。
2. 原基差模型 v2 修正空头倒数公式和费用；原 funding 模型 v2 明确降为旧代理
   复现，新增 `evaluate_settled_carry`。旧报告不覆盖；代理输入不足以证明或否定
   真实套息盈利。
3. 一个预声明低换手基准：十币种、20/60 日均线、20 日波动率缩仓、日频 long/flat、
   每币目标最多 10%，与同期间现金／持有 BTC 比较，计入两边费用和终端清仓。
   这是受机构研究启发的本地基准，不是 Man 原文策略的完整复现。
4. 研究循环 v3 保留自动重估，取消历史最优正收益格子的 `auto_promote`，
   增加已评估格子数、未完整登记的历史试验数声明，以及冻结基准。
   v1/v2 artifacts 不变；没有新增 timer/service，也没有把研究报告接入资金。

## 本轮真实数据证据

- 正式 API：Crypto catalog `v1-c3011487473156b0`，240 datasets；首次15秒
  catalog 请求超时，第二次40秒上限请求成功。没有绕过认证或改权限。
- BTC spot 历史最早探针返回 `2026-01-30T07:35:00Z`；state=ready、quality=valid、
  degraded=false。receipt：
  `receipt:3b8981c996c7226f32cf722ca486cdd5f8963d23df6c7184b8919dd04807fda7`。
  此两行探针不代表完整历史已验证；返回 cursor 也不当成全量读取。
- BTC funding 探针返回 2026-08-30 00:00 UTC 费率 `0.00008283`，前次结算
  `2026-08-29T16:00:00.002Z`，说明结算可带毫秒。receipt：
  `receipt:96d228c44f30fc3c621a3744082ef6fd9ba23542186d766e6c5540f6e32e0b00`。
  funding 可读不等于两腿套息可计算。
- BTC/ETH perp catalog 只有 funding_rate/open_interest/premium_index，未发现
  实际合约成交价或标记价格历史，funding schema 也不含结算 mark。
  真实套息：`data_unavailable`，净收益 null；不得用 premium 代替缺失价格。
- 旧已验证 raw cache（8月6日至15日，每币2571行）仅覆盖8个完整日线输入，
  不足60日特征窗口；该次研究正确输出不足，不输出0收益冒充可用基准。

## 历史基准正式实验

正式取数只经过 TradingDatas catalog/query，按 UTC 每日 00:05 open 和23:55
bar close 的精确时点查询，每批最多100个时点，不采集新的交易所行情。未使用
的日内缺口不影响日线计算；缺必需价格不前填，缺特征只使该币空仓，缺组合
估值日不得跳日拼接。回看历史选择最长连续可估值段，同长取最早，不按收益选段。

实际取数窗口为2月1日至8月30日（右端不含），每币缺2个请求时点，形成208个
共同完整日线输入。60日预热后，最长连续可估值段为 **4月2日至8月28日（右端
不含），148天**；不能把结果写成截至8月30日的实时账户收益。

| 口径 | 148天费用后收益 | 研究期末权益（初始10,000 USDT） | 日收盘最大回撤 | 买卖腿数 |
|---|---:|---:|---:|---:|
| 固定低换手趋势 | +3.5995% | 10,359.9547 | 15.4541% | 66 |
| 持有 BTC | +17.5196% | 11,751.9572 | 28.6891% | 2 |
| 现金（零利息） | 0% | 10,000 | 0% | 0 |

趋势比 BTC 少13.9200个百分点，手续费45.7860 USDT，成交名义金额总和/初始
研究现金为4.5786。虽然历史净收益为正，但未跑赢 BTC，15.45%回撤仍偏高。
这里只评估原始基准，未模拟现役账户的日亏/连亏/7%回撤暂停完整状态机，也没有
可执行盘口和交易所过滤器，因此不允许接管资金。不能看完结果再修改同一计划。

[机器报告](2026-08-30-research-accounting-baselines.json) 由正式 API 实验生成，
含逐腿 ledger、日权益、50份分页 receipt 及返回行 hash；完整 report SHA：
`1519a1e0c4d12f046c33fe325eba51125a4274ef8a4b84df3ce21c8617e996c6`；
冻结计划 SHA：`c08c42a22f092ee11aa250c83c1eff995de6d9c2c2ef1c95a6d4d016e49bf0be`。
本地独立按全部交易腿重算现金/数量/手续费，期末现金、零持仓和收益与报告一致；
同时重验 report、plan、两份 ledger hash。没有套用旧带缺陷的 carry 模型。

## 冻结验证与未完成层

- 前向窗口固定 `2026-08-31T00:00:00Z` 至 `2026-11-29T00:00:00Z`；结束前
  不输出该策略前向收益。到期缺90天完整估值则明确失败、不延期挑结果。
- 当前观测 store 尚不足60日特征历史；CLI 历史数据与观测 store 不被自动拼接。
  缺少 first-seen/PIT 和正式 registry authority 时，即使到期描述性净值为正，
  仍不自动晋级。此实现不声称完整的自主模拟 Champion 生命周期已验收。
- 套息缺实际 perp 成交价、mark 与独立完整结算日程；新增数据须由 TD 正式交付。
- K10 冻结定义、窗口与一次性读数保持不变。
- 未修改或切换生产 release，也未安装/启动自动调度。原 Controller 任务已归档；
  派单失败，未擅自恢复任务或改写 AUTODEV_STATE。候选需正式交付验收后才能合并发布。

## 验证与回滚

- 精确路径 isolated lane 结构校验通过，against frozen base behind=0；该结果不
  冒充已归档 Controller 的派单/接受或当前主线同步。
- 本轮6组针对性/邻接回归100 passed（78.45秒），包含公式、费用、毫秒结算、
  缺资金费、margin spike、无杠杆、日线时序、缺必需点、固定前向读数、batch100、
  cursor/metadata拒收、旧v1/v2目录不变、同输入幂等、实盘硬闸。
- Kimi CLI 0.38.0 smoke成功，但130秒只读交叉审计超时，无可接受的独立报告；
  不计作验收通过。不使用外部协作者产物作为代码或合并依据。
- `git diff --check` 通过。远端CI、合并、immutable release、自然调度及真实收益
  各自独立，不从本地测试推断。
- 回滚：停止调用新研究入口并恢复已验证代码，保留全部旧/新研究产物；不清空、
  重置或迁移任何账户、观测、资金与订单事实。

## 研究来源

- [Man Group: In Crypto We Trend](https://www.man.com/insights/in-crypto-we-trend)：
  日线趋势、风险缩放、成本与币种流动性；不是可复制实盘收益保证。
- [Common Risk Factors in Cryptocurrency](https://onlinelibrary.wiley.com/doi/10.1111/jofi.13119)：
  市场、规模、动量因子和复现入口；多空学术组合不等于本地只做多策略。
- [BIS: Crypto carry](https://www.bis.org/publications/working-paper-1087-crypto-carry)：
  套息机制及保证金风险，不作为当前收益率报价。
- [Bybit P&L calculation](https://www.bybit.com/en/help-center/article/FAQ-Profit-Loss-Calculation)：
  USDT 线性合约固定数量盈亏及费用口径。
