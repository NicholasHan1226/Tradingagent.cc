# TradingAgent 当前状态

> 最后更新：2026-07-19 CST。本文件只记录当前候选、主线、服务器与外部依赖的分层事实；长期规则见 [AGENTS.md](AGENTS.md)，运行与回滚见 [docs/operations.md](docs/operations.md)。历史事故、旧候选和作废证据从 Git 与服务器只读证据目录审计，不在这里维护流水账。

## 当前结论

TradingAgent A股 V1 已形成一个本地集成候选，仍是 **fixture/mock-first、simulation-only、无真实交易权限**。集成候选代码冻结锚点为 `f1a0387`，位于 `TradingAgent/.worktrees/ta-v1-integrated-release` 的 `codex/ta-v1-integrated-release` 分支；状态文档提交本身由所在 Git 提交识别，不把自引用 commit hash 写入文件。

该候选合并了原 `ta-v1-data-client` 候选与本地主板 fail-closed 修复，并撤回了旧 `SharedSignalsAPIClient` 对 V1 的错误 ownership。A股 current-v1 只允许显式配置、显式 transport 的 `GET /v1/catalog` 与 `POST /v1/query`；旧 reader 只按 `active-compatibility` / `retirement-pending` 服务非 A 股兼容与法证路径，旧 A股 writer/wrapper 保持 `hard-blocked`，都不得拥有或自动接线 V1。

## 五层事实

| 层级 | 2026-07-19 当前事实 | 不能据此推断 |
|---|---|---|
| 本地集成候选 | `codex/ta-v1-integrated-release`；代码锚点 `f1a0387`；本文件提交后以 `git rev-parse HEAD` 和 clean status 为最终字节证据 | 不等于 GitHub、主线或服务器 |
| GitHub 候选 | 集成分支尚未 push，尚无对应 PR/CI；Draft PR #2 仅对应旧源候选 `codex/ta-v1-data-client@de57a71` | PR #2 不代表本集成候选 |
| Git 主线 | `origin/main@3b3aab41bcf1fee046da169f6fd582b4f2818cba`；尚未合入本集成候选 | 本地 main 的额外提交不等于远端主线 |
| 服务器现役 | 只读 readback：`/opt/investment/tradingagent@6c12fbed29db925019f85a6016774626f63b857a`；`tradingagent-front-api.service=active`，PID `1043`，仅监听 `127.0.0.1:8787`，`/healthz` 为 200；`127.0.0.1:18787` 无监听 | 现役代码未切换，候选未部署、未激活 |
| 外部能力 | 未连接 live SharedSignals、真实 DeepSeek、broker、邮件、同花顺、GUI、Cloudflare 控制面或公开 API | 不能声称真实数据闭环、真实模型可用或真实交易 |

服务器只读快照同时记录：现役 Git status 有 49 条既有运行/回滚资产，内容摘要为 `2ac8dc6a...74f9`；systemd unit 摘要为 `9128f159...2307`；marketgraph 用户 crontab 摘要为 `af3605a8...fc9a`。这些值只用于本次发布前后差异比较，不能成为候选能力或生产激活凭证。

## 当前候选能力

1. **固定 SS V1 consumer 合同**：八字段 QueryRequest、完整 Catalog/Query envelope、逐数据集 evidence gate、nullable source proof 与 fail-closed；无 SS DB、`/tushare`、`/source_status`、provider 专用 route 或本地文件 fallback。
2. **递归证据快照**：Catalog/Query 数据及 freshness、quality、lineage 内部保存 canonical JSON 快照；调用方修改原 payload、返回副本或缓存副本不能改变后续 Gate 判断。嵌套 `failed/error/invalid/unavailable` 统一拒绝。
3. **三层 A股 Universe**：只有沪深主板普通股可进入个股分析、候选、预测、模拟仓位与订单；创业板、科创板个股无权限时不分析、不交易，其指数及行业汇总只作市场环境和行业宽度参考。
4. **小资金决策与组合**：50,000 CNY simulated authority、100 股整数约束、15% 单票、90% 总敞口、最多 8 仓、最低经济订单、no-trade band、费用/滑点、T+1、现金与六维投资论点风险门。
5. **研究与自动模拟闭环候选**：Opportunity Radar/Ledger、多期限未校准 forecast、三风格 shadow router、Decision Ledger、RunBundle、label maturity、counterfactual 与网络关闭的 automatic day fixture。它们不证明预测有效，也不是已安装 scheduler。
6. **LLM evidence sidecar**：DeepSeek transport 默认关闭，固定 evidence-only schema、accepted/rejected/invocation Journal 与 fail-closed provenance；LLM 禁止输出订单、仓位、目标权重或风险预算。2026-07-18 的旧单次 canary 仅证明请求到达 provider 后被本地 schema 拒绝，没有 accepted evidence，不能复用为当前候选或生产凭证。
7. **只读前端**：`front/` 是唯一前端入口，只显示模拟状态与证据缺口，不写资本、订单或交易权限。线上页面恢复不在本阶段范围。

## 本轮验证状态

- SS V1 client/evidence/research/runtime/架构相关：`242 passed`；Ruff check 与相关格式检查通过。
- 交易安全只读终审锚定 `525065c`：P0=0、P1=0、P2=0；专项 `389 passed`。之后唯一代码变化是 SS envelope/evidence P1 修复，正在进行独立 fresh 复核。
- 前端：43 个测试文件、`276 passed`；`npm run lint` 与 `npm run build:all` 通过。
- 第一次集成全量基线：`3065 passed / 1 failed`；唯一失败是已修复的旧 provenance 断言。最终冻结字节的完整后端全量尚待重跑，因此当前仍不可 merge。
- 候选清单：上一冻结字节为 `1513 passed`；最终文档与证据修订后必须再跑。

上述证据只属于标注的本地字节和只读层；旧候选的 `3059 passed`、Draft PR #2、旧 GitHub Actions 或旧服务器 canary 均不替代当前集成候选验证。

## 明确未完成或未授权

- SS 上游尚未向本任务提供冻结的真实 internal handoff、catalog version、dataset IDs、service token、receipt authority、跨页语义与 runtime readback；因此只允许 fixture/mock。
- 未安装或启用 live paper scheduler/cron；仓库 crontab 只是设计模板，旧 A股 wrapper 与 funnel writer 继续 hard-blocked。
- 未调用真实 DeepSeek；会话中曾暴露的旧 credential 不写入 Git、不装载进现役服务，也不构成后续网络授权。
- 未连接 broker、真实账户、真实邮件、同花顺、公开 ingress 或真实交易；`REAL_TRADING_ENABLED=false`。
- `tradingagent.cc` 的单用户 Access 门未在本轮恢复或验证，禁止把候选接入匿名公网入口。

## 本阶段剩余门禁

1. 完成 final SS/document/trading-safety fresh review，关闭全部 P0/P1；
2. 在最终冻结字节重跑后端全量、候选清单、前端、静态检查与 secret scan；
3. push 独立集成分支，创建对应 PR，等待干净 CI，再以普通 merge commit 合入 `origin/main` 并 readback；
4. 只在服务器创建 detached、隔离根、network-disabled、sim-only sidecar，监听 `127.0.0.1:18787`；验证后停止，确保现役 HEAD、service、8787、cron、unit 与运行资产无变化；
5. 回填最终主线/sidecar事实，清理仅限已合并、clean、明确归属的旧 worktree/branch；保留所有 dirty、unmerged、运行与证据资产。

第一阶段真正出口仍需真实 SS V1 后连续 20 个交易日自动模拟闭环，以及随后 60–120 个交易日冻结 OOS/多状态样本。月收益 20% 只作为概率分布上尾指标，不是强制交易、满仓或 PASS 条件；任何模型晋级、风险扩张或 live transition 仍需 Nicholas 单独批准。
