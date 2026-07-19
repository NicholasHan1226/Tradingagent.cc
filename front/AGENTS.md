# TradingAgent Front

本目录是 TradingAgent 的前端看版和只读快照 API，不是交易执行入口。

接手本目录时，先读：

1. `README.md` — 产品定位、目录边界和运行方式。
2. `docs/integration.md` — 只读接口、生产部署和 Nginx 形态。
3. 上层 `../AGENTS.md` — TradingAgent 的交易、队列、回调和实盘边界。

## 边界

- 可以读取 TradingAgent 的信号、持仓、复盘、风险和收益展示数据。
- 不得写入 `signals/`、不得修改队列状态、不得触发成交、撤单、回调、邮件发送或账户操作。
- 前端默认展示模拟盘；实盘只保留连接状态和未来接入口，未验证真实账户与授权前不得展示为已接入。
- 系统仅供 Nicholas 个人内部使用。生产部署时，前端静态文件和只读 API 可以放在同一台服务器，但 API 必须只监听 `127.0.0.1`。`tradingagent.cc`可经Cloudflare提供远程入口，但必须启用Access或等价单用户认证；禁止匿名公网转发和API直出。
- 如果需要新增 API，只能新增只读展示接口；任何执行、下单、回调、账户和资金相关接口都不属于本目录。

## 生产路径建议

- 源码目录：`/opt/investment/tradingagent/front`
- 前端构建输出：`front/dist`
- 只读 API 构建输出：`front/dist-server`
- API 内部监听：`127.0.0.1:8787`
- 浏览器访问：服务器本机 `http://127.0.0.1:<internal-port>/`，或经过单用户认证的 `tradingagent.cc`；域名可用不等于匿名公开。

## 验证

修改本目录后，至少运行：

```bash
npm run lint
npm test -- --run
npm run build
npm run build:api
```
