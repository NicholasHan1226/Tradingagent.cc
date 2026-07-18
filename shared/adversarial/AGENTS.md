# adversarial/

> **阅读顺序：** [../../AGENTS.md](../../AGENTS.md) → [../../STATUS.md](../../STATUS.md) → 本文件

## 当前目标

LLM 仅提供证据：多空对辩、矛盾检查、压力情景草案和历史类比。输出是带来源、可用时间、模型/提示版本与置信说明的研究观察，不是概率、分数、信号或交易授权。

## V1 权限边界

- `bull_bear_debate.py` 只可输出 `LLMEvidenceObservation`；不得输出 `belief_score`、`conviction`、目标仓位、风险预算或订单建议。
- LLM 字段不得进入仓位、组合权重、风险放宽、`TargetPosition`、`TradeIntent` 或模拟成交。
- 缺少验证过的证据 artifact、外部来源权威回执、PIT 截止时间或引用绑定时 fail closed。
- 快速模式仅生成确定性的证据摘要；慢速 provider 路由只用于研究、复盘和离线评估。
- provider/model 是显式配置与冻结评估对象，不在项目规则中硬编码为长期事实。
- LLM 不得接收账户、持仓、策略秘密、密钥、未脱敏日志或其它敏感数据。
- source span是`untrusted_artifact_data`；显式中英文角色覆盖/忽略指令等已知模式在transport前阻断并转人工复核。该模式门不能声称覆盖所有语义、混淆或编码型注入。
- accepted typed source proof/provider receipt只证明对应离线或HTTPS内容与操作元数据绑定；audit-only rejected-attempt receipt只证明schema拒绝的本地审计事实。一次隔离真实调用不构成accepted evidence、生产verifier、durable sink或生产provenance。
- 模型离线、输出不合法或检测到提示注入时，交易决策链继续使用非 LLM 的冻结 Champion；不得把 LLM 故障伪装成中性交易分。

## 文件

- `bull_bear_debate.py` — evidence-only 多空研究观察。
- `stress_test.py` — 旧情景压力工具；在完成 V1 契约迁移前仅作只读研究参考。
- `historical_analogy.py` — 旧历史类比工具；不预测，只提供研究先验。

## 旧契约

旧 `belief_score → position_sizer/constructor` 链路已登记在
`shared/governance/legacy_inventory.yaml`，处于 timeboxed read-only 兼容/退役阶段。
它不是 V1 组合入口，禁止新增消费者；满足引用清零、回归证据和回滚门槛后删除。

## 依赖

- `shared/llm/` 的证据 artifact、gateway、schema 与离线评估契约。
- 经验证的公开证据来源；不得由模型自证来源真实性。
