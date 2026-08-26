# earnings 两臂正式判定前外部文献三角互洽（research_only）

- 日期：2026-08-26。性质：判定前解读锚定（#573 宏观 ante 同款先例），
  服务 2026-08-31 计划运行导出后的正式判定撰写；不移动任何冻结门柱、
  不改变任何预注册标准、不作部署候选。
- 对象：跟踪器三信号中尚未正式判定的 `earnings_pos`（披露正向漂移）与
  `earnings_neg`（负向释然反弹）两臂；判定输入=labeled_outcomes 的
  post_return_bps（净=毛−15bps 往返），家族冻结标准不变。

## 文献锚点（中国 A 股直接证据为主）

**正向漂移臂（PEAD 家族）——存在性证据强，但有三条衰减/反转警示：**

1. 存在性与幅度：[Is PEAD a Priced Risk Factor? Chinese Evidence]
   (https://doi.org/10.14738/abr.56.3299)（2000–2008，按意外度多空
   60 天 7.92% 超额）；[Limited Attention and PEAD: China]
   (https://econjournals.com/index.php/ijefi/article/download/10817/pdf/26415)
   （意外越大事后超额越高；归因有限注意——不足三成 A 股投资者依据公告
   决策）；[Investor Limited Attention, Opinion Divergence, and PEAD]
   (https://doi.org/10.1080/1540496x.2022.2079975)（2000–2020，意见分歧
   越大漂移越强）。中国样本的 PEAD 普遍报告比成熟市场更大。
2. 警示一（近期衰减）：[A Simple Earnings Surprise Measure (ORJ)]
   (https://www.sciencedirect.com/science/article/abs/pii/S1057521924003922)
   （2011–2023，48 季）：经典 SUE 类度量已弱化，注意力中介正负向不对称。
   我们的方向映射走「事前业绩预告→实际披露」而非 SUE，结构不同，但
   「近十年信号变薄」的可能性必须在解读时纳入。
3. 警示二（正向过度反应分支）：[News Shock, Limited Institutional
   Attention and Stock Market Response](https://ideas.repec.org/a/eee/asieco/v100y2025ics1049007825001174.html)
   （JAE 2025）：中国市场对**好消息过度反应、坏消息反应不足**——若该分
   支主导，正向披露后的结构是回吐而非延续。两条分支对 earnings_pos 判
   定方向相反，正好构成互证：KEEP 偏向主流漂移读法，FAIL 不必然否定机
   制（可能是衰减或过度反应分支），需看月度画像再落笔。
4. 注意力截面：[Heterogeneous Investor Attention and PEAD]
   (https://ideas.repec.org/a/eee/ecmode/v110y2022ics0264999322000426.html)
   ——散户注意力↑漂移、机构注意力↓；临近 52 周高点更强。可对照我们已
   有的事前换手率标签做后续描述性互证（不新开面板）。

**负向释然臂（超跌修正家族）——机制同源性成立但时点更挑剔：**

5. [The Unusual Trading Volume and Earnings Surprises in China]
   (https://www.mdpi.com/1911-8074/13/10/244)：卖空约束下事前缩量预示
   负面基本面，价格过冲后于披露后出现**正向修正**——「过冲在披露处、修
  正在披露后」与我们 neg 臂入场窗同构。
6. [Market Responses to Earnings Seasonality]
   (https://papers.ssrn.com/sol3/papers.cfm?abstract_id=3928167)：预期驱
   动的反转——历史低预期季度公告日反而获得更高异常收益，支持「悲观定价
   兑现即利空出尽」读法。
7. 机构侧：[Institutional Investors and PEAD in China]
   (https://ideas.repec.org/a/taf/acctbr/v51y2021i2p206-236.html)：机构
   持仓放大而非套利掉漂移，且公告后 Q4 出现价格反转——提示漂移类结构的
   日历敏感性。

## 对周一判定的解读矩阵（不预支任何结论）

| 判定 | 与文献的关系 | 解读要点 |
|---|---|---|
| pos=KEEP | 合主流中国 PEAD | 看月度画像是否集中早期年份（衰减检验） |
| pos=FAIL | 与衰减/正向过度反应分支相容 | 非机制否证；记方向分支证据 |
| neg=KEEP | 合卖空约束过冲-修正机制 | 时点敏感，看首周 vs 全窗贡献 |
| neg=FAIL | 释然时点假设弱于文献时点 | 可能窗口错位而非无效应 |
| 任一 GRAY | 样本/时序问题 | 按 extend-once 规则，只延一次 |

## 边界声明

本文只做解读锚定；所有判定仍以 8·31 CI 导出 + 冻结家族标准 +
`event_milestone_judgment.py` 复算为准；观察名单≠部署候选；反向使用需
独立重新预注册。文献数字均为各自样本口径，不可与本管道读数直接比大小。
