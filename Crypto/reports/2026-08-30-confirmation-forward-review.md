# 两日确认独立前向登记：候选验收记录

2026-08-30，北京时间。范围仅research/simulation；无收益提升或运行安装声明。

## 交付与证据

- 新分支 `codex/crypto-confirmation-forward` 从#610精确 `6a798cad199b0889ee524634b10fba7759fd1494` 建立，旧#606/#608/#610不改。
- [计划](2026-08-30-confirmation-forward-plan.md)冻结2026-09-01至2026-11-30 UTC的90日窗口；候选仅 `confirmation_only`，不再扫描历史参数。
- [实际登记](2026-08-30-confirmation-forward-registration.json)：`registered_at=2026-08-30T07:54:59Z`，绑定九个计算源文件。
- 计划SHA256：`05c09709ec11b5662f2887dc3c1cc21626f30651ad38e97cbd83e18ced1668ac`。
- 登记SHA256：`33028f6306b89d0f411fdd5e58f2403f3d6c53201c0a0923c226f39d7ee49e21`。
- [同秒CLI读回](2026-08-30-confirmation-forward-status.json)：`registered_not_started`，`results=null`，`runtime_installed=false`、`data_collection_installed=false`。
- 读回SHA256：`9e5a29927a5f5604db20265d916aea06bc6973be7bcd4de5555a91abfe0fc83f`。
- 实际CLI传入不存在的 `/nonexistent/confirmation-forward-must-not-read.json` 仍正常返回封存状态，未调用网络或读取行情。

## 验证

六模块初次最终检查119 passed / 12.68s（新增登记产物绑定回归后见下方追加终验）。
登记产物绑定回归加入后，完整六模块终验 **120 passed / 12.54s**，包括仓内实际登记与同秒状态完整复算。
独立只读协作者先完成四模块81 passed / 5.77s、无阻断；其后主助手新增了显式row symbol错配拒绝，
并在到期回归中用陷阱证明不进入任何旧analyze或模型fit路径，这些已包含于119项主测试。
此前新测试一次使用了不存在的ledger.notional，改为quantity×fill_price独立重算；不是放松业务断言。

```bash
REAL_TRADING_ENABLED=false PYTHONDONTWRITEBYTECODE=1 python3 -B -m pytest -q \
  -p no:cacheprovider tests/test_crypto_confirmation_forward_research.py \
  tests/test_crypto_cost_aware_trend_research.py tests/test_crypto_slow_trend_risk_replay.py \
  tests/test_crypto_slow_trend_research.py tests/test_crypto_research_accounting.py \
  tests/test_crypto_ten_symbol_research_loop.py
```

主要覆盖：注册start等号拒绝、end前不读input、不允许CLI覆盖时钟、登记/hash/源码/计划漂移拒绝、
全10币151日预热及估值覆盖、坏时间/重复/乱序/NaN/错币拒绝、缺价不滑窗不延期、
1x/2x成本逐腿费用/滑点/现金/仓位归零对账、未来行情不影响此前交易、原实验不计算、
Decimal上下文固定、既有文件不可覆盖、sim-only与无晋级标记。

普通lane验证因worktree不是固定market-crypto拒绝；精确七路径的isolated-candidate验证通过。
后者只证明所选父提交的祖先关系与写域，不是Controller派单、current-main同步、独立接受或发布证据。
只读检查4747009至当时main `5a74edf3381703f4ee6a1a5c880d59638a409d9d` 的差异仅其它市场/对应合同文档与测试，
不修改这些主线文件；本候选不重写研究栈，也不据此声称完整主线兼容验证。

本仓CI仅针对main的push/PR；stacked draft不自动跑该工作流。手工dispatch只接受exact-main，
本候选不得借此绕过。局部pytest不等于全仓CI。

## 剩余层级与边界

本地登记是本地时钟+可复算摘要，不是外部签名。开始前的远端PR/commit留证单独核对；
不因创建PR声称TD数据已就绪、每日消费者运行或PIT成立。
尚无本窗口真实行情、收益、定时采集、每日运行、PIT receipt或生产部署证据。
所有到期结果只能是固定窗口离线研究；全部修订输入/结果需保留，不得择优报告。
自动Champion替换、资本/订单权限、风险扩张、实盘、observer/timer/release/systemd均未触及。

TA另有现场修复任务独占运行/发布写域；已告知本研究不集中SSH/API取数、不占运维窗口。
TD套息仍在合同owner决策入口#395，缺真实perp/mark/schedule时继续data_unavailable；本批不绕过。
回滚仅停止调用该新增入口，旧报告、登记、账户与账本不删除不迁移。

## 推送与最后读回

实现与登记提交 `80164aab721a1be6173cfa2228e99eaa11acbdd4` 已推送并从远端ref读回一致。
[草稿PR #612](https://github.com/NicholasHan1226/Tradingagent.cc/pull/612) 的GitHub `createdAt=2026-08-30T07:56:52Z`，
早于固定窗口开始；API读回draft/open、base为#610分支、无CI checks，没有合并/部署。
最终独立审查补验 **83 passed / 5.79s**，九源hash和实际登记/状态一致；提交后单模块 **36 passed / 1.89s**。
浏览器实际检查该实现commit的GitHub计划Preview与README锚点：中文层级、日期、限制及命令代码块可读，链接正确。
这些留证不证明未来数据首次可用、每日运行或收益。
