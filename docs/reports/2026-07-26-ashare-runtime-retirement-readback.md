# A股 observation runtime 与 legacy front 退役读回

> 验证日期：2026-07-26 CST
> 代码锚点：`eb2e18a6c38b1f5c1139679a8e910c6923fa3edb`
> 性质：服务器安装态、simulation-only、未激活

## 结论

- 专用 observation Python runtime 已安装为
  `/opt/investment/tools/venvs/tradingagent-observation-py312-pyyaml603-v1`。
  解释器 leaf 为 `root:root 0555`、regular、`nlink=1`；PyYAML 6.0.3 由精确
  wheel SHA-256 锁定，真实入口导入和 runtime audit 均通过。
- `tradingagent-ashare-observation.service` 已按主线字节安装，但保持
  `inactive/static`；对应 timer 仍为 `not-found`，没有 enable 或 start。
- legacy front 的两份 drop-in 已备份后移出 active systemd 目录。front base unit
  已前滚为主线字节，所有 active `8082`、`SHAREDSIGNALS_API_URL`、
  `/opt/tradingagent` 和 `marketgraph` service identity 引用均为零。
- front 保持 `inactive/disabled`，并存在 `/run/systemd/system` runtime mask；
  8787 未监听。旧 8082 listener 仍由其原系统 owner 保留，但 TA 不再引用或回退到
  它；TradingDatas 18082 未被修改。
- UID/GID 987 在验收后进程数为零；`REAL_TRADING_ENABLED=false`。本次没有启动
  observation、front、timer、broker、模拟成交或真实交易。

## 失败关闭与修正

第一版 `063249e…` 错误假定 observation 入口是 stdlib-only。fresh server smoke
在 unit/front 修改前因缺少 PyYAML 阻断。该失败 runtime 已从 active tools 路径
移入对应只读证据目录，未被激活。后续 `eb2e18a…` 增加精确版本、wheel hash 和
启动前真实导入/版本审计。

第二次 smoke 发现既有 state root 为 `0750`，不满足 worker 合同的精确 `0700`。
在证明 UID 987 无进程后，只把
`/var/lib/tradingagent/ashare-observation` 收紧为 `0700`；runtime/log root 已是
`0700`。随后完整 runtime audit 通过。

## 权威证据

- 失败关闭证据：
  `/opt/investment/release-evidence/tradingagent/20260726T113353Z-ta-runtime-retirement-063249e`
- 最终 runtime、unit、drop-in 退役和 readback：
  `/opt/investment/release-evidence/tradingagent/20260726T114404Z-ta-runtime-retirement-eb2e18a`
- front base unit 前滚与字节一致性：
  `/opt/investment/release-evidence/tradingagent/20260726T114546Z-ta-front-base-forwardfix-eb2e18a`

每个证据目录均保存排序后的 `evidence.sha256`。证据不包含 token 内容、token
hash 或 Authorization header。

## 未授权和下一停止线

本轮只消除了旧 Python parent 与旧 front 8082 配置依赖。不可据此推断：

- immutable release 已切为 `current`；
- observation worker 或 timer 已激活；
- 动态每日 manifest rollover 已完成；
- current observation 已成为历史 PIT、特征、预测或交易证据；
- 自动模拟盘或真实交易已经开始。

下一步仍须先实现并验证按最新完成交易日生成 immutable manifest 的 rollover，
再用 disabled worker 做新鲜手工 one-shot、失败恢复和幂等读回；timer 继续关闭。
