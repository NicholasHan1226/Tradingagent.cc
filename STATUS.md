# TradingAgent 当前状态

Observed at: 2026-09-05 13:17 Asia/Shanghai。Simulation only；不启用真实交易、
新资本或风险扩张。本页是可替换摘要，历史失败不能被配置一致掩盖。

## 源码、发布和实际进程

| 层面 | 核验入口 |
|---|---|
| 本地主线 | 现场读取仓库，不将文档自身的提交写成永久当前值 |
| GitHub 主线 | 刷新远端后与本地分别核对 |

实时源码核验使用 `git rev-parse HEAD origin/main`；以下为上述观察时间的发布快照。

- 最近已合主线：`074e9c2e10129eb8d2692a57977dae5938c73655`（#642）。
  本机主仓与 GitHub 一致且干净。
- 精确主线 CI `33945833456` 成功，既有生产发布 `33946442878` 成功。
  `current`、部署标记、前端 PID 1656394 的实际工作目录均为该 SHA；`/healthz` 通过。
- 发布工具按 DEPLOYMENT 的首次受控重装条款更新，自更新逻辑与已接受主线一致，
  旧工具保存在服务器 `/root/garden-20260905/tradingagent-release-before-refresh`。
- 40 币、G5 与 A股发布管理任务配置指向本版本；未运行的任务不算已完成新版本执行。

## 未通过项与正在修复范围

- A股 minute session、scale500 session/paper 的失败来自 9 月 4 日交易时段；
  分别调查输入、分钟数据合同和上游 session 失败传播，不通过 reset-failed 抹除。
- G5 acceptance 与 rolling evaluation 的最近失败发生在本轮部署前；
  需要区分历史状态、重复快照读取竞争与报告计算成本。
- 旧十币 observation/factor research 仍自然运行在较早 release 并持续失败；
  前端和 40 币发布成功不代表这些任务已经更新。其运行归属与修复单独验收。
- 本轮修复包含显式分钟合同重绑、仅对并发 head 前进的有界重读、十币发布绑定及防止回退丢失任务覆盖。
- G5 验收在单次调用内复用已完整验证的归档语义回放，每次仍读取全量字节并核对整套文件身份；
  合成测试约 4 倍提速，篡改与跨文件并发变化必须拒绝。生产整轮耗时仍须部署后验证。
- 修复和测试不代表策略有效、下一开市日执行已恢复或历史失败已消失。

## Crypto track

G5 delayed-paper service 的发布配置与实际运行结果分别核验；本轮仍保留历史失败。
latest continuous 288-bar segment 仅衡量 runtime maturity；历史断点
 do not block safe-segment simulation，但不能被解释为策略有效或允许风险扩张。

## Copilot and paused scopes

TradingCopilot 仍为独立人工辅助边界，不授予自动交易权限。

## 研究、工作树与暂停范围

#606/#608/#610/#612 仍为冻结、堆叠的研究草稿。保留原始负向证据及预注册确认窗口；
不提前读取确认期结果，不因为 CI 通过自动发布策略。
A股、Crypto 的长期市场 lane 保留；CNFutures 暂停，保留隔离树和历史合同。
Mini 的控制面归属与职责见根 [AGENTS.md](AGENTS.md)，不能用本机缺目录替代验收。
脏改动、独有研究及运行事实保留，临时任务工作树在安全核验后回收。

## 当前入口与历史

规划见 [EVOLUTION_PROGRAM](docs/EVOLUTION_PROGRAM.md)，
运行与恢复见 [operations](docs/operations.md)，
发布见 [DEPLOYMENT](docs/DEPLOYMENT.md)。各市场的真实数据、样本、资本、
模拟执行和评价必须分别由自己的机器事实验证。

原先 234 行历史快照保留于
[本轮前的固定 Git 版本](https://github.com/NicholasHan1226/Tradingagent.cc/blob/074e9c2e10129eb8d2692a57977dae5938c73655/STATUS.md)。
它们仅证明原观察时间，不作为当前 release 或任务成功证据。
