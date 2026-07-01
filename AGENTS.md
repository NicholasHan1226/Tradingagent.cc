# Tradings/

## 目标
 交易模拟盘/影子盘, 高频训练策略, 每日2次复盘。

## 现有代码
- /opt/investment//tools/ (约20个工具)

## Projects 工作区同步补充

本仓库位于 `/Users/nicholashan/Projects/Finance/TradingAgent` 时，按 Projects 工作区统一同步规则执行：

- 仓库地址、remote 名称和默认分支以本仓库内 `git remote -v`、`git branch --show-current` 和项目文档为准，不从其它项目继承。
- 开发前检查 `git status -sb`、`git remote -v`、当前分支和是否落后远端；工作树不干净时先判断改动来源，不得覆盖并发 agent、cron、桌面自动化或 Nicholas 的改动。
- 涉及交易 agent 行为、邮件/API、部署、配置、数据契约、风控边界、服务器路径、定时任务或协作流程的变更，必须同步更新核心文档，例如 `README.md`、`docs/data_contract.md`、`docs/email_setup.md`、`docs/INFRASTRUCTURE.md`、`docs/repo_structure.md` 或对应市场/模块文档。
- 涉及真实资金、实盘执行、账号凭据、2FA、私钥、邮件发送通道或生产服务器的操作，必须先说明授权边界、回退方式和验证方式；研究、模拟盘和影子盘不得被汇报成实盘结果。
- 提交时只暂存本次审计过的文件；数据库、缓存、日志、staging、密钥、本机运行产物和交易临时输出默认不提交，除非项目文档明确要求并已审计。
- 从旧 `Desktop/Investment` 或其它 iCloud 管理目录迁移时，优先使用当前 Projects 下真实 clone；旧目录只作为对照和补漏来源，不直接搬运 `.git`。

