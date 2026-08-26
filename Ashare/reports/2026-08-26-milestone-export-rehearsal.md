# 里程碑判定链路端到端彩排（research_only）

- 日期：2026-08-26（早班）。性质：工程验证记录，不含任何正式判定。
- 目的：在 2026-08-31（周一）计划运行首次导出 `labeled_outcomes` /
  `prewindow_samples` 之前，用真实生产数据验证「跟踪器状态导出 ↔ 里程碑
  判定评估器」全链路，避免正式读数当晚才发现对接缺陷。

## 彩排方法

1. 基座：run9 工件（`tracker-report-9`，2026-08-25T22:19Z 成功运行）中的
   生产 `sample_journal.jsonl` + `signal_tracker_ledger.jsonl` **副本**
   （577 行；原件不动，追加只发生在副本）。
2. 本地缓存 `/tmp/ashare_event_research`（数据面新鲜度 08-25 晨）离线跑
   post-#570 跟踪器，命令与 CI 完全同形：`--since 20260401 --signals
   lockup,earnings_pos,earnings_neg --expanded`，纯计算 40 秒。
3. 对产出状态 JSON 做结构校验（只看字段与计数），再走评估器 CLI 全部
   preset。

## 发现并修复的缺陷（#582）

`event_milestone_judgment.py` 的 earnings 两臂 preset 被错误接到
`prewindow_samples`（事前窗口描述性导出，字段为 `pre_return_bps`）而非判
定输入 `labeled_outcomes`（事后毛收益 `post_return_bps`）。后果有两层：

- `earnings_neg` 直接抽取失败——#570 导出里 prewindow 只填 positive 臂，
  negative 臂恒为空；
- `earnings_pos` 抽取"成功"也是假象——行内没有 `post_return_bps` 字段，
  后续 `judge()` 必然崩溃。

合成测试夹具两键形状一致所以未暴露；真实数据彩排当场抓住。修复=统一改
读 `labeled_outcomes`；新增回归测试钉住「prewindow 键缺失/半缺时判定照常
工作」（即周一导出的真实形状）。prewindow 只填 positive 臂是 #570 的描
述性导出设计，不属于判定输入，本修复不改动跟踪器。

## 结构校验结论（08-25 缓存快照下的计数，仅供工程确认）

- `labeled_outcomes` 三臂齐全：lockup 197 / earnings_pos 165 /
  earnings_neg 206 行；每行 `{event_id, event_date, regime, ratio_bucket,
  post_return_bps}` 字段完整，regime 与 ratio_bucket 取值域正常。
- `prewindow_samples`：earnings_pos 165 行、另两臂空——与 #570 设计一致。
- 评估器抽取：`lockup_rule` n=100；三 preset 全部可走完 CLI 判定流程；
  其中 lockup_rule 判定行与 #571 已入档读数逐位一致（链路无损的直接证
  据）。

## 边界声明

- 正式里程碑判定仍绑定 2026-08-31 计划运行的 CI 导出；本文所有计数与机
  械验证结果不构成判定依据，不移动任何门柱，不作部署候选。
- 彩排中顺带出现的中途读数仅用于确认工具能跑通，未写入任何观察名单或决
  策文档。
- 工程注记：macOS 上 `/tmp` 是符号链接，journal 安全守卫会拒绝
  （`JournalSafetyError: journal path or parent is a symlink`）；本地副本
  路径须用 `/private/tmp/...`。run9 之后副本 journal 一日自然增长 186 行
  （577→763，去重键无冲突），断点续传链幂等性旁证。
