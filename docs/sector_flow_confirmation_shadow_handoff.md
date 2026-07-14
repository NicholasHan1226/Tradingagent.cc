# Sector Flow Confirmation Shadow 接手包

## 边界与基线

- 当前 replay worktree: `/private/tmp/tradingagent-sector-flow-v4-replay-ece93a1`
- 当前 detached base: `ece93a1712851cb8aaee9469c125eecbfeb8357d`
- 历史 source worktree: `/Users/nicholashan/Projects/Finance/.worktrees/tradingagent-sector-flow-v2`（branch `codex/ashare-sector-flow-v2`，base `9b243208a2584867df4431336d26af7cb9da1c6f`）；只作语义来源，不是当前候选身份。
- 仅本地未提交候选；禁止 commit、push、deploy、生产/cron/账户/真实交易写入。
- 冲突区保持零 diff：`shared/orchestrator.py`、`shared/wrappers/`、`shared/capital/`、`shared/risk/`、`shared/runtime_test/*sample_ops.py`、`shared/review/projection_generation.py` 和既有 forecast worktree。

## 改动面

- `shared/screening/condition_generator.py`: 保持 rotation trigger 算法不变，把 symbol-scoped moneyflow 明确为个股资金确认。
- `shared/screening/sector_flow_confirmation.py`: 构建相同 base snapshot/decision/pair identity 的 feature off/on 配对，在任何 trim/coercion 前验证 scope、请求/快照 sector ID、snapshot ID 与 taxonomy 均为原生非空 string，再验证 finite/integer、PIT chronology，以及 canonical payload 与声明 source SHA 的 constant-time binding，输出 before/after 消费回执。
- `tests/test_ashare_moneyflow_scoring.py`: 防止再次把个股资金称为板块资金。
- `tests/test_sector_flow_confirmation.py`: 覆盖合格、缺失、未来、坏 SHA、错 scope，以及 identity 的 bool/int/float/list/mapping/None/empty 类型矩阵，并验证非法输入不能形成合法 paired identity、始终零消费/零门禁绕过。
- `shared/screening/AGENTS.md`、`docs/data_contract.md`、`STATUS.md`: 同步长期语义、字段合同和本地候选状态。

## 当前消费结论

`sector_flow_confirmation` 目前只计算影子观察，不接入 decision consumer：

```json
{
  "consumer": "shadow_observation_only",
  "consumed": false,
  "changed_candidate_membership": false,
  "changed_ranking": false,
  "changed_playbook": false,
  "changed_strategy": false,
  "changed_execution_eligibility": false,
  "execution_gate_bypassed": false
}
```

因此本候选纠正错误语义并建立可审计特征合同，但不会改变候选、排序、策略或执行结果。

## 后续接口需求（本轮不实现）

上游需提供 immutable sector flow snapshot：`scope=sector`、请求/快照两侧非空且匹配的 canonical `sector_id`、taxonomy、snapshot ID、finite `net_inflow_cny`、类型级原生正整数 rank、带时区 event/availability clocks 和 content-bound source SHA。scope 与全部 identity 字段只接受原生非空 string，不允许把 bool、number、list、mapping 或 `None` 转成字符串；非法 identity 不生成 pair SHA。资金值只接受原生 int/float，拒绝 bool 和 numeric string；rank 不接受 bool、float 或 numeric string；两者都不做隐式 coercion。source SHA 的 canonical payload 字段顺序与集合由 data contract 固定，不能只验 64-hex。若未来要让特征影响决策，decision consumer 必须显式读取 paired record、记录 before/after candidate/rank/playbook/strategy/execution identity 差异，并继续经过现有数据、资本、风险和执行门禁；不得把 `confirmation=true` 当作放行条件。

## 验收命令

```bash
python3 -m pytest tests/test_sector_flow_confirmation.py tests/test_ashare_moneyflow_scoring.py -q
python3 -m pytest tests/test_ashare_moneyflow_scoring.py tests/test_sector_flow_confirmation.py tests/test_data_reader.py -q
python3 -m ruff check shared/screening/condition_generator.py shared/screening/sector_flow_confirmation.py tests/test_ashare_moneyflow_scoring.py tests/test_sector_flow_confirmation.py
python3 -m compileall -q shared/screening/condition_generator.py shared/screening/sector_flow_confirmation.py
git diff --check
```

最终交付时以本轮新鲜命令输出为准，不以本文中的命令列表代替实际验证。

旧 aggregate `18de...`、`5e5a...`、v3 `c8fea3d8...`、9b v4 `120063b2...` 与 ece93a 首轮 `cc6a043e...` 均不能作为当前通过证据；其中 cc6a 已因 identity 隐式字符串转换的 fresh P1 明确作废。v5 在修复前取得新增类型矩阵 `40 failed / 22 deselected`，修复后为 `40 passed / 22 deselected`；最终代码上 rank16/core21/expanded107 分别为 `16 / 21 / 107 passed`（各 `46 deselected`），完整 feature/core/expanded 为 `62 / 67 / 153 passed`，仓外 Fresh 为 `8 passed`，独立包为 `64 passed`。Ruff 0.15.14、compile、diff、docs、禁止路径与 hygiene 以本轮新 manifest 中的证据为准。最终 aggregate 使用路径排序后的精确 8 文件 `SHA-256 + path` 清单重算，包含 3 个 Git untracked 文件；当前只冻结候选等待新的 fresh 独立 review。
