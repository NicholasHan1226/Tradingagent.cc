# TradingAgent 后续 Backlog

> 本文件只记录 2026-07-12 范围冻结后明确移出的工作。它不表示已授权发布、外部写入或实盘。

## 1. 生产发布与下个交易日启用

- 获 Nicholas 单独授权后，备份并核对生产文件、环境变量和现有 crontab。
- 分市场初始化两个 fresh 50,000 CNY production-side simulated root，验证 freeze manifest、checksum、execution lineage 和首次 MTM reconcile。
- 只安装已审计的 sim-only cron；运行 opening/preopen/ops、A股 15:32 daily MTM、样本和看板 smoke。
- 分开报告生产文件、runtime、cron 与首个真实市场样本；任一层失败则回滚任务启用，不删除 append-only 事实。

## 2. 进化终端深化

- 在只读看板深化 champion/challenger、按风格样本、拒绝分布、校准、N_eff、逐日 MTM 回撤和 MG paired ablation。
- 保持 SampleJournal/KPI 唯一 authority；不得恢复自动 promotion、自动风险扩张或风格独立资金。

## 3. TradingCopilot 后续实盘辅助

- V1 已建立用户申报资金/持仓、关注股、个股正反证据、建议强度、条件门和人工意图账本；下一步接入 TradingAgent 正式 A 股观察，而不是扩大演示股票。
- 后续可评估券商只读持仓对账，但必须与手工申报来源并列并有 freshness/identity；不得静默覆盖用户状态。
- Nicholas 明确授权前，不发真实邮件、不操作同花顺、不接 broker。独立 broker automation gateway 属于更晚的单独项目。

## 4. 长期统计方法

- 以更多独立交易日、决策 cluster、市场状态和完整回合验证费用后 expectancy、校准、稳定性与逐日 MTM 回撤。
- CNFutures Sharpe 仅在有同频净收益序列后计算；再评估 DSR、多重比较、换月/夜盘/极端状态分层。
- 任何长期统计升级不得用 label-cell 数替代独立样本，也不得据短期盈利自动晋级。

## 5. 旧数据客户端退役债务

- 按消费者批次移除旧 `TradingagentDataReader` 的隐式 localhost 配置；每批同时迁移 adapter、screening、research、wrapper/runtime-test、环境变量、测试和文档，不做长期双轨。
- 旧 `SharedSignalsAPIClient` 的类级缓存键尚未绑定 `base_url` 与访问身份，存在不同 endpoint 同查询串用缓存的隔离风险。该客户端应随旧专用端点一起退役；若退役前必须继续使用，则先补 endpoint/identity 绑定与跨端点负例。
- 退役完成前，服务器旁路测试必须显式把 `SHAREDSIGNALS_API_URL` 与 `MARKETGRAPH_API_URL` 设为空，禁止测试进程读取本机现役服务。

## 6. TradingDatas catalog parity 加固

- 当前 declared-impaired dataset 无论上游 effective state 为何都会保持
  `REJECT/weight=0/research_snapshot_eligible=false`，不会进入研究快照；后续应把可
  “诚实记账”的状态收紧到 TradingDatas 冻结的已知 impaired state 白名单。遇到
  未知 state 必须整体阻断并要求 catalog/manifest 复核，避免状态枚举漂移被静默
  归入 impaired。该项是 P2 合同可观测性加固，不阻断当前 sim-only 候选。
