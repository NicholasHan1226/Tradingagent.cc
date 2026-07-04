# TradingAgent Front

TradingAgent 的前端看版层。它负责把自动化交易系统的结果展示给用户：
实时收益、机会漏斗、持仓、决策结果、风险边界和复盘信息。

本目录不是交易执行系统，也不是账户控制台。任何 agent 接手这里时，默认只做
展示、读取和可视化，不得触发交易动作。

## 一句话定位

`front/` 是 TradingAgent 的用户界面和只读快照 API 包装层：

```text
浏览器页面
  ↓
TradingAgent Front
  ↓ 只读
/api/trading-agent/snapshot
  ↓ 只读
TradingAgent signals / positions / review / risk
```

## 展示目标

首页优先展示三件事：

- 实时收益：模拟盘当前收益、目标差、回撤和收益曲线。
- 机会漏斗：从发现机会到进入持仓/放弃机会的动态筛选过程。
- 当前结果：用户现在该关注什么，哪些机会在推进，哪些风险已被挡住。

主题页按结果分类：

- 收益：收益曲线、目标、贡献来源。
- 机会：当前机会、错过/放弃原因、筛选进度。
- 持仓：模拟盘持仓和未来实盘接入口状态。
- 决策：研究、交易、风控形成的判断及结果。
- 风险：回撤、敞口、风险节省、边界状态。
- 复盘：关闭机会、执行结果和复盘线索。

## 当前实现

- UI：React + TypeScript + Vite。
- 图表：Recharts，生产构建中拆成独立 `charts` chunk。
- 本地开发：Vite dev server 会挂载只读快照 API。
- 生产 API：`src/server/tradingAgentSnapshotHttp.ts` 可独立启动 Node 只读服务。
- 读取入口：`GET /api/trading-agent/snapshot`。
- 浏览器客户端：`src/api/tradingAgentIntegration.ts`。
- TradingAgent 读取器：`src/server/tradingAgentSnapshot.ts`。
- 真实数据适配：`src/api/tradingAgentReadModel.ts` 和
  `src/adapters/tradingAgentReadModel.ts`。
- fallback 数据：`src/data/dashboard.ts`，只用于接口不可用或字段暂缺时展示。

## 数据边界

可以读取：

- `../signals/{pending,filled,cancelled,expired,failed,partial}/*.json`
- `../signals/positions/*.json`
- `../shared/accounting/position_plan.jsonl`
- `../shared/review/daily/daily_brief.jsonl`
- `../shared/review/attribution/*.jsonl`
- `../shared/risk/risk_limits.yaml`

不得执行：

- 写入或移动 `signals/` 队列文件。
- claim / cancel / expire / fill 任何 signal。
- 调用执行器、下单路由、邮件发送、webhook、账户回调。
- 读取或暴露账号凭据、2FA、私钥、资金权限。
- 把模拟盘、影子盘、实盘混成一个收益数字。

## 生产形态

首个生产版本建议和 TradingAgent 放在同一台杭州服务器：

```text
8.138.181.177
  Nginx / HTTPS
    /                         -> front/dist
    /api/trading-agent/snapshot -> 127.0.0.1:8787

  Node snapshot API
    bind 127.0.0.1:8787
    read /opt/investment/TradingAgent
```

推荐生产源码路径：

```text
/opt/investment/TradingAgent/front
```

前端默认使用同源接口：

```bash
VITE_TRADING_AGENT_SNAPSHOT_URL=/api/trading-agent/snapshot
```

详细部署和 Nginx 示例见 [docs/integration.md](docs/integration.md)。

## 本地运行

```bash
npm ci
npm run dev
```

本地页面：

```text
http://127.0.0.1:5173/
```

## 验证

修改本目录后至少运行：

```bash
npm run lint
npm test -- --run
npm run build
npm run build:api
```

## 当前缺口

- 持仓真实读模型还不完整，当前可能显示为空或 fallback。
- 收益曲线仍需要后端输出更稳定的日内/日级收益序列。
- 机会漏斗需要后端补充每个机会的阶段变化时间线，才能做到更真实的动态流动。
- 实盘只保留未来接入口；未验证账户授权前，前端不得展示为已接入。

