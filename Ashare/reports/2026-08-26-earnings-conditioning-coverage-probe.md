# earnings 两臂 × 既有冻结标签族覆盖探针（research_only）

- 日期：2026-08-26。性质：C14 体例纯标签计数探针（#585 同款先例）；
  零收益计算、零新桶定义、不占面板编号（候选台账修订版 #560 仍
  OPEN）。目的：8·31 原始臂判定落地后，若任一臂 KEEP，「漂移有无条
  件层可做」是下一自然问题；本探针先行摸清既有基础设施的附着成本。
- 数据：彩排导出 labeled_outcomes（pos 165 / neg 206，ID 天然唯一零剔
  除）；身份自 ID 段提取（symbol=[2]，event_date 与入场日一致）；标签
  全部来自各研究模块既有冻结批量助手；缺档如实计为未标注。

## 覆盖表（计数层）

| 家族 | pos 覆盖 | pos 主要分布 | neg 覆盖 | neg 主要分布 |
|---|---|---|---|---|
| valuation | 100% | mid40/high36/loss36/low36/sh17 | 100% | **loss125**/mid44/high24/low13 |
| turnover | 100% | normal154/surge7/shrink4 | 100% | normal181/shrink21/surge4 |
| macro 发布窗 | 78.2% | outside103/**ante13**/sd8/post5 | 84.0% | outside143/**ante14**/sd13/post3 |
| chips | 100% | mid65/profit56/underwater44 | 99.5% | **underwater82**/mid77/profit46 |
| pledge | 100% | no_snap85/mid40/low29/high11 | 100% | no_snap99/mid56/low31/high20 |
| holdernum | 100% | **expand65**/stable52/contract47 | 100% | **contract81**/stable79/expand44 |
| topinst | 100% | no_listing142/netbuy12/netsell6 | 100% | no_listing176/nb8/ns7 |
| toplist | 100% | no_listing142/rise14/sell8 | 100% | no_listing176/sell21/rise9 |
| blocktrade | 100% | none157/flat6/deep2 | 100% | none201/flat4/deep1 |
| absorption | 100% | balanced128/outflow24/inflow13 | 100% | balanced149/outflow41/inflow16 |
| repurchase | 100% | no_rec158/active5/done2 | 100% | no_rec200/active3/done3 |
| holdertrade | 100% | no_rec161/nsell3/nbuy1 | 100% | no_rec199/nsell6/nbuy1 |

结构性排除（未计算）：holdertype 批次构成与 unlock supply 为解禁批次
专属语义，对 earnings 无对象。无现成批量助手需适配工作：dividend 姿态
（私有单点函数）、价格结构 r63（逐条接口）、margin 自融。

## 计数层结构观察（非读数、不作判定输入）

1. **neg 臂估值缺失高度集中**（loss_or_missing 125/206=61%，pos 仅
   22%）——负向预告不成比例地来自亏损股（pe_ttm 无定义），经济上自洽；
   neg 臂未来任何估值条件化必须把 loss_or_missing 当独立桶而非缺档剔除，
   否则会静默丢掉六成样本。
2. **holdernum 轴两臂变化真实且方向相反**（pos 以 expand 为主 65 vs
   neg 以 contract 为主 81）——股东户数轴是未来条件化候选中变异最健
   康的一条。
3. macro ante 两臂各有 13/14 例——观察名单项 rule[ante] 未来可在
   earnings 臂上做跨信号对照（独立预注册前提）。
4. 事件窗活动类家族普遍稀疏（repurchase active ≤5、holdertrade ≤6、
   blocktrade deep ≤2）——当前 n≈200 下不具备条件化门柱资格。

## 判定后条件化启动规则（2026-08-26 冻结）

本节在**任何 earnings 条件化分桶收益计算发生之前**冻结（#536/#588
同款预注册先例），目的：判定夜之后不存在「看过收益画像再挑家族」的
自由裁量空间。冻结依据仅为上文计数层分布，不含任何收益率信息。

1. **激活条件**：仅周一 8·31 正式排期导出的判定结果可激活本节；
   彩排数据不激活。某臂 KEEP → 该臂条件化可立项；GRAY → 随延长
   期样本重判后再议；FAIL → 该臂全部条目作废归档，双 FAIL 则整节
   作废。
2. **立项优先序（冻结；依据＝覆盖率 × 桶间变异 × 细胞规模）**：
   - 第一：**holdernum**——唯一两臂全桶过门柱（pos 65/52/47、
     neg 81/79/44）且变异方向跨臂相反的家族；
   - 第二：**chips**——同样全桶过门柱（pos 65/56/44、neg 82/77/46）；
   - 第三：**valuation**——pos 四主桶 36–40 均匀可用，但 neg 臂
     loss_or_missing 须按覆盖表观察①独立成桶，且 high24/low13 两桶
     低于门柱只能观察级；
   - 第四：**macro[ante]**——ante 仅 13/14 例，只允许跨信号对照的
     观察级登记，永不具备判定门柱；
   - turnover surge/shrink、absorption inflow/outflow 及事件窗活动类
     （repurchase/holdertrade/blocktrade/topinst/toplist）：观察级，
     无门柱资格；
   - 需适配器的 dividend/r63/margin 自融三族：暂缓——前两个立项
     周期由上述四族覆盖，适配工作等轮到时再做（避免无人使用的
     预制件）。
3. **多假设上限**：判定后首个研究周期内最多同时实例化 **2 个**
   条件化研究（按上序取前二可行者），其余排队。每项研究仍须各自
   走完整「抓取/接线→独立预注册→引擎→读数」链路——本节只冻结
   资格与顺序，不替代单项预注册。
4. **细胞门柱**：沿用家族门柱惯例 n≥30（独立事件粒度）；不足 30
   的细胞一律观察级记录、不进入任何判定；细胞读数复用评估器
   ``judge()`` 同一冻结标准（净＝毛−15bps 往返、日期排序对分两半
   一致性），且正式读数只绑定正式排期导出（与原始臂同一纪律）。
5. **编号与台账**：面板编号分配与候选台账登记留给台账 owner
   （#560 重建落地后），本文档不占号、不预设编号。
6. **可推翻性**：项目级预注册决定，Nicholas 可零成本推翻；但推翻
   须发生在该臂任何条件化分桶收益计算之前——计算发生后推翻不改写
   已产生的读数，只能重新预注册。

## 边界声明

纯标签计数；先于任何 earnings 分桶收益计算合入主线；research_only /
not_promotion_evidence；不移动门柱、不产生部署候选。启动规则冻结的
是「判定后可以做什么」的程序边界，不预测、不承诺任何判定结果。
