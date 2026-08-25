# 因子候选台账（备选考核因子唯一明细入口：A股 C 系列 / 加密 K 系列）

- 建立：2026-08-25。服务目标（Nicholas 2026-08-25 澄清后口径）：**A股、加密
  各自独立**达到备选考核因子 150 个。汇总计数与冻结口径见 ladder doc
  「因子与策略库记分板」节；本文件只管逐条明细与状态流转，两处数字必须对账
  一致。
- research_only / not_promotion_evidence。加密侧加严条款：历史回填无 PIT 证明
  的读数一律非证据身份，判定只能走 receipt-bound 滚动评估。

## 状态机与升格规则（冻结）

```
seeded → preregistered → testing → concluded(KEEP/FAIL) 
                              ↘ parked（附原因与重启条件）
```

- **seeded**：数据面可行或有明确来源 + 方向假设 + 操作化边界三要素齐备才准入册；
  缺任一要素的念头不入册（防注水）。
- **seeded→preregistered**：覆盖率测定完成 + 冻结定义 PR 先于任何分桶收益合入
  主线（家族既有纪律）。
- **testing→concluded**：只按各自预注册门柱判定；FAIL 是合格产出。
- 同信息再参数化不计新条目；无判定路径不入册（北向 #41 / 停牌判例）。
- 每班补充 ≥5 条 seeded（三源：面板降级行升格评估 / 新数据轴探针 / 滚动面扩展）。

## 在册条目（23 条，2026-08-25 晨建账）

| ID | 名称 | 数据源 | 状态 | 入口锚点 | 下一步 |
|---|---|---|---|---|---|
| C01 | absorption balanced 承接桶 | moneyflow | testing | #438 接线 | 滚动样本积累 vs 观察名单复判线 |
| C02 | block near_flat 平价过户桶 | blocktrade | testing | #438 side-table | 小样本累积（11 结局时 4 例） |
| C03 | turnover shrink/normal 滚动 | daily_basic | testing | #444 接线 | 滚动读出面自然验证 |
| C04 | chips underwater/mid/profit 滚动 | cyq_perf | testing | #447 接线 | 2026 滚动 11 结局全 underwater 一致 |
| C05 | macro ante 发布窗滚动 | 宏观日历 | testing | #515 接线 | 滚动样本 vs 观察名单项 |
| C06 | valuation low_le25 前瞻门 | dailybasic | testing | #536/#537 | **首读 n≥30 日=最早合法判定点** |
| C07 | holdertype incentive 前瞻门 | share_float | testing | #536/#537 | 同上；质量选择效应混杂在档 |
| C08 | holdertrade 净减持排除屏滚动 | stk_holdertrade | seeded | 面板 #29 | 镜像 #456 五处接线 |
| C09 | repurchase active 滚动 | 回购公告 | seeded | 面板 #30 | 同上 |
| C10 | pledge high 负向滚动 | pledge_stat | seeded | 面板 #31 | 接线由班次容量决定 |
| C11 | register natural_heavy H1 | top10_holders | preregistered | #541+引擎 ba60554 | #541 合入后推引擎开 PR→一次性读数 |
| C12 | register fin_inst_heavy | top10_holders | preregistered | 同上（预降级描述性） | 随 #18 读数 |
| C13 | register mixed_other | top10_holders | preregistered | 同上 | 随 #18 读数 |
| C14 | limit_list_d 日内结构（封板时长/open_times）×解禁入场 | limit_list_d | seeded | 数据面 #7 素材 | 六日缺口补采（on-demand 重执行待安全窗）；历史已可立项 |
| C15 | cyq winner_rate 分布 ×解禁 | cyq_perf | seeded | HV 已证自洽（0.250 vs 0.120） | 覆盖率测定→预注册 |
| C16 | cb_share 公告流条件层 | cb_share | seeded | 观察项 | 可行性探针 |
| C17 | major_news resource_budget 条件层 | major_news | seeded | 观察项 | 可行性探针 |
| C18 | crypto 情绪四端点 ×加密模拟盘 | crypto lane | seeded | #320 | lane 冻结解除后立项 |
| C19 | pb 轴估值分位 | dailybasic(pb) | seeded | #17 工程注记（2 缺失 vs pe 90） | 覆盖率测定→独立预注册 |
| C20 | 宏观前瞻日历 forward_capable | 宏观日历 | seeded | 信号库存快照 | 生产探针判定后决定立项 |
| C21 | top_inst 净卖确认层 | top_inst | parked | #36 FAIL 反向 | 重预注册方可反向使用 |
| C22 | rise_dev 投机热度毒性回避 | top_list | parked | #35 全场最差桶 −182.1 | 同上 |
| C23 | 大宗深折价回避屏 | blocktrade | parked | n=16 覆盖 4% | 覆盖不足；重预注册+扩样 |

### Parked 队列补充（不占上表编号，重启需独立预注册）

deep_dd 中期反转（#15 均值腿仅 +4.9bps）、insider 年份结构修正后再审
（2018–2020 占 85%）、holdertype placement 正向使用（#16 反转）、
valuation high_ge75 负向（#17 双腿矛盾）、holdernum U 形全景利用、
deleverage×shrink 联合格(n30)、expansion×mid 联合格(n66)、reserve20
调度微调（#16 sweep 唯一正增量）。队列评估标准见 #536 冻结条款，
禁止逐案即时追认。

## 加密线在册条目（K 系列，8 条，2026-08-25 午建账）

| ID | 名称 | 数据源 | 状态 | 入口锚点 | 下一步 |
|---|---|---|---|---|---|
| K01 | TS 动量入场滚动冠军扭亏评估 | 40 币观察器回执 | testing | rolling-evaluation entry-001→003 | MVP-2 回合累积；downweight 趋势确认则机械降权 |
| K02 | CS 相对强弱留观格（l576_k5_h288 等） | 40 币 5m 网格 | seeded | cross-sectional-prescreen | 只经 receipt-bound 滚动裁决，不以回填为据 |
| K03 | funding carry 结构性风险溢价转策略 | perp funding | seeded | 08-18 forty-symbol 预筛 | maker 假设判定→PIT 数据面→滚动验证 |
| K04 | basis carry / cash-and-carry 转策略 | 期现 basis | seeded | 08-18 预筛+maker 判定节 | 同上 |
| K05 | OI 三族正式路径复算 | open_interest 5m | seeded | 08-16 OI prescreen | 18083 release 门禁恢复后走 catalog/query 重跑 |
| K06 | CS 离散度门控族重评 | 40 币 5m 网格 | parked | 门控从未激活（登记缺陷） | 新预登记修正阈值语义后方可重评 |
| K07 | sentiment 四端点 ×币价 | TD#320 情绪源 | seeded | tradingdatas issue #320 | lane 冻结解除后立项 |
| K08 | exit shadow 读数面 | delayed_paper_exit_shadow | seeded | Crypto/delayed_paper_exit_shadow.py | 接入滚动读出 |

## 变更日志

- 2026-08-25：建账 A股 C 系列 23 条（testing 7 / preregistered 3 / seeded 10 /
  parked 3）；同日午间双轨化修订——目标改为 A股/加密各自 150，新增加密
  K 系列 8 条（testing 1 / seeded 5 / parked 1 / infra-seeded 计入 seeded），
  对账记分板「备选考核因子 A股 23 / 加密 8」。
