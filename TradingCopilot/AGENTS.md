# TradingAgent / TradingCopilot

> 阅读顺序：[../AGENTS.md](../AGENTS.md) → [../STATUS.md](../STATUS.md) → 本文件。

## 定位

- TradingCopilot 是 TA 成熟过程中的过渡性 A 股实盘辅助、解释、观察与人工接管领域，不是第二套量化交易系统，也不是 Finance 的终局交易产品。长期方向是 TradingAgent 在逐市场明确授权后接管决策与执行，Copilot 退回监控、解释和人工接管面。
- 它可以读取 TradingAgent 已发布的只读研究、候选、风险与市场证据，结合用户申报的资金、持仓、关注股和人工决定形成行动卡。
- 它不拥有量化 Champion、模型晋级、资本预约、量化订单、BrokerAdapter、真实交易或风险扩张 authority。
- 它不是 TradingAgent 的必经下游，不扩展到 Crypto 或预测市场；这些市场的量化闭环属于各自隔离的 TA/Quant Core lane。
- 未获得 TradingAgent 正式覆盖的股票必须显示 `analysis_unavailable`；演示数据必须显示 `demo_fixture`，不得冒充实时分析。

## 共享底座与领域所有权

- Quant Core 与 TradingCopilot 只共享不可变、可追溯、无资金/订单 authority 的底层证据与纯计算能力：TradingDatas 行情/基础资料、A股规则与成本口径、PIT observation/features、公告新闻舆情、市场状态、模型/校准/OOS结果和个股只读投影。
- 预测训练、Kronos Challenger、样本外评估与校准属于 TradingAgent learning/research plane，计算一次后分别生成只读投影；不得在 `front/` 或 TradingCopilot 状态服务中建立第二套正式模型、特征、回测或校准 authority。前端确定性基线只可用于 `demo_fixture` 视觉验收。
- 候选排名、Champion、组合优化、策略仓位、量化资本/预约、硬风险放行、订单/outbox/fill/reconcile、SampleJournal、策略KPI与模型晋级仅属于 Quant Core。Copilot 最多读取解释投影，不能修改或继承这些 authority。
- 用户申报资金/持仓、关注列表、个人约束、行动卡、人工意图和人工计划复盘仅属于 TradingCopilot；不能回流为量化样本、收益、模型标签、晋级或风险扩张依据。
- 机器可读责任表为 [contracts/shared_capability_boundary.v1.json](contracts/shared_capability_boundary.v1.json)。新增能力必须先归入且只能归入一个 owner；跨域共享必须通过只读投影，不得共享可写状态。

## 唯一写入范围

- 用户申报账户：总资金、可用现金和更新时间。
- 用户申报持仓：代码、名称、数量、可卖数量、成本与更新时间。
- 关注列表和人工决策：加入计划、继续观察、暂不交易；计划必须保存理由、触发、失效条件和可选风险上限，事后实际动作与复盘必须独立记录。
- 上述状态只能写入 TradingCopilot 独立 namespace；不得写入 `shared/logs/capital/**`、execution lineage、outbox、SampleJournal、量化 Decision Ledger 或 `signals/`。

## 前端与执行边界

- 唯一网页入口仍为 `../front/`；本目录定义领域合同和状态边界，不建立第二套生产看板。
- 网页写操作只允许进入 TradingCopilot 状态接口。状态接口必须使用 `ETag/If-Match` 防止多标签覆盖，并在读取/写入前验证 append-only state/event hash 链。浏览器 fallback 必须明确标记为未同步草稿。按钮产生人工意图记录，不代表已下单、已成交或已发送到券商。
- 正式个股投影必须同时存在独立 `stock_projection_receipt`，绑定 exact projection SHA、有效期、verifier 与上游 receipt；页面不得信任投影内自报的 ready/score。普通 TradingAgent confidence 只能显示为未评分观察。A股 T+1、一手、板块涨跌停、ST、停牌和复权口径缺失时，人工计划入口保持阻断。
- 当前不连接券商、不发送邮件/消息、不操作同花顺。未来券商只读同步和执行适配必须分别立项、验证并由 Nicholas 明确授权。

## 账户与绩效隔离

- `user_declared` 账户与 `ashare-capital-v1` 模拟量化账户完全分离。
- 用户申报持仓不是 broker-verified 持仓；页面和API必须保留来源标签。
- TradingCopilot 的个人盈亏、人工决策和行为复盘不得进入量化策略样本、策略收益或模型晋级统计。
