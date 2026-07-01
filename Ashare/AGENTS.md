# tradingagent/Ashare

> **阅读顺序：** [AGENTS.md](../AGENTS.md) → [STATUS.md](../STATUS.md) → 本文件

## 目标
A股交易全闭环 (T+1, Hermes同花顺执行)。

## 约束
- T+1: 当天买不能当天卖
- 集合竞价: 9:15-9:25 (单独策略)
- 连续竞价: 9:30-11:30, 13:00-14:57 (主策略)
- 收盘竞价: 14:57-15:00 (单独策略)
- 涨跌停: 10%限制

## 资金
- 初始20,000元, 集中2-3只
- 盘前1小时资金规划
- 闲置资金尾盘买逆回购(204001)

## 执行
- 模拟盘: UI自动化 (a_share_simulated_trade_executor)
- 实盘: Hermes桌面控制同花顺 (a_share_tonghuashun_execution)
- 5-10分钟级别自动化

## 现有代码
- 144个工具待整理迁移 (因子28/复盘27/组合18/执行16/筛选11/风控10/通知14)
- 路径: /opt/investment/Ashare/tools/a_share_*.py
