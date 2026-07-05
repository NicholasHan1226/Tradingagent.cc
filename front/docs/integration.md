# TradingAgent Front Integration

## Result

The front layer reserves one direct read-only integration endpoint:

`GET /api/trading-agent/snapshot`

This endpoint is the server boundary between TradingAgent and the browser UI.
It is designed for simulated-account display first. Live account status remains
gated and must not trigger execution from the front layer.

## Current TradingAgent Surfaces

| Front result | Preferred source | Fallback / supporting source | Status |
| --- | --- | --- | --- |
| Current opportunities | `signals/pending/*.json` | `signals/{claimed,running,filled,cancelled,expired,failed,partial}/*.json` | Ready |
| Positions | `signals/positions/*.json` | `shared/accounting/position_plan.jsonl` | Partial |
| Performance | `shared/review/daily/daily_brief.jsonl` return fields | `shared/review/*/style_performance.jsonl` simulated PnL series | Partial |
| Decisions | daily review and attribution JSONL files | strategy version history | Partial |
| Risk | `shared/risk/risk_limits.yaml` | PM risk report JSONL | Ready |
| Live readiness | execution schemas and filled signal writeback | manual authorization state | Gated |

## Read-Only Contract

The browser fetches `TradingAgentReadModelSnapshot` through
`createTradingAgentSnapshotClient()`.

The server can wrap a local reader with `getTradingAgentSnapshotResponse()`.
The response must use `Cache-Control: no-store`.

Display-ready fields used by the homepage:

- `performance[]`: `day`, `simulated`, `target`, `benchmark`, `opportunity`.
  The local reader accepts daily review aliases such as
  `simulated_return_pct`, `return_pct`, `pnl_pct`, `target_return_pct`,
  `benchmark_return_pct`, and `opportunity_gap_pct`.
- If daily review return fields are absent, the reader can build a real
  simulated return series from `shared/review/*/style_performance.jsonl` by
  summing `pnl` per date and normalizing it against the simulated ledger
  capital base from `shared/logs/sim_ledger/*/*/positions.json`.
- Trade journals and position cost are not valid performance sources by
  themselves. When only those files exist, `domains.performance.status` remains
  `empty` with a message explaining the missing PnL / return series.
- `signals[]`: `symbol`, `market`, `status`, `impact`, `confidence`,
  `reason`, `next`, `steps`, plus optional funnel fields `stage`,
  `stageTimes`, and `stageLatencyMinutes`.
- Signal stage timestamps can be supplied as `discovered_at`, `scored_at`,
  `debated_at`, `risk_checked_at`, and `triggered_at`. The reader maps existing
  status and timestamps into `发现 / 评分 / 风控 / 待执行 / 成交 / 错过 / 拒绝`
  so the animated funnel reflects only real read-only file state.
- The homepage trading funnel is designed to animate real stage movement. If
  the API only exposes completed simulated-ledger trade journals, the UI will
  show a completed-trade replay instead of inventing upstream drop-off. To show
  a true screening funnel, upstream records should include one row per
  opportunity with its latest `status`, current `stage`, and available stage
  timestamps before execution.
- `holdings[]`: `symbol`, `market`, `weight`, `pnl`, `risk`, and `role`.

## Same-Server Production Deployment

The preferred first production shape is to keep the dashboard frontend and the
read-only snapshot API on the TradingAgent production server. The browser does
not read the filesystem directly. It loads a static Vite build through Nginx and
fetches one same-origin snapshot route:

`GET /api/trading-agent/snapshot`

Recommended production shape on the Hangzhou host:

1. Nginx serves `front/dist` as the frontend.
2. Nginx proxies `/api/trading-agent/snapshot` to
   `127.0.0.1:8787/api/trading-agent/snapshot`.
3. The Node snapshot service reads the verified TradingAgent workspace.
4. The API returns only display-ready snapshot JSON.

Current production deployment:

- Host: `8.138.181.177`
- Workspace: `/opt/investment/tradingagent`
- Front source: `/opt/investment/tradingagent/front`
- Node runtime: `/opt/investment/tools/node-v24.4.1/bin/node`
- Service: `tradingagent-front-api.service`
- Nginx site: `/etc/nginx/sites-available/tradingagent-front`
- Internal API: `127.0.0.1:8787`
- Public server names: `dashboard.tradingagent.cc`, `tradingagent.cc`,
  `www.tradingagent.cc`
- DNS status: the Nginx site is ready, but the domain A records must point to
  `8.138.181.177` before normal browser access reaches this server.

When the frontend and API share the same domain, the frontend can use the
same-origin route:

`VITE_TRADING_AGENT_SNAPSHOT_URL=/api/trading-agent/snapshot`

Do not put secrets, local filesystem paths, execution tokens, account
credentials, or order mutation routes in Vite environment variables. `VITE_*`
values are public browser configuration.

## Hosted Snapshot API

The repository includes a standalone Node API server for the production
snapshot route:

```bash
npm run build:api
FINANCE_WORKSPACE_ROOT=/opt/investment/tradingagent \
TRADING_AGENT_SNAPSHOT_HOST=127.0.0.1 \
TRADING_AGENT_SNAPSHOT_PORT=8787 \
TRADING_AGENT_SNAPSHOT_CORS_ORIGINS=https://dashboard.tradingagent.cc \
/opt/investment/tools/node-v24.4.1/bin/node dist-server/server/tradingAgentSnapshotHttp.js
```

Routes:

- `GET /healthz`
- `GET /api/trading-agent/snapshot`
- `OPTIONS /api/trading-agent/snapshot`

Security boundary:

- Keep the API bound to `127.0.0.1` behind a reverse proxy when possible.
- Use HTTPS at the proxy layer.
- Require `Authorization: Bearer <token>` when the endpoint is not fully
  private. If a token is enabled and the browser uses a same-origin route,
  inject the token at the proxy layer; do not send it from browser JavaScript.
- Allow only the dashboard origin in
  `TRADING_AGENT_SNAPSHOT_CORS_ORIGINS`.
- Never expose TradingAgent execution, order mutation, callback, account,
  credential, or 2FA routes through this API.
- Never put `TRADING_AGENT_SNAPSHOT_API_TOKEN` in a `VITE_*` variable.

Frontend configuration:

```bash
VITE_TRADING_AGENT_SNAPSHOT_URL=/api/trading-agent/snapshot
npm run build
```

If the frontend and API share the same domain and path, the frontend can also
omit `VITE_TRADING_AGENT_SNAPSHOT_URL` and use the same-origin
`/api/trading-agent/snapshot` fallback.

## Nginx Shape

Example route shape for the production server:

```nginx
server {
  listen 443 ssl;
  server_name dashboard.tradingagent.cc;

  root /opt/investment/tradingagent/front/dist;
  index index.html;

  location / {
    try_files $uri /index.html;
  }

  location /api/trading-agent/snapshot {
    proxy_pass http://127.0.0.1:8787/api/trading-agent/snapshot;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header Authorization "Bearer server-only-token";
  }
}
```

If the internal API is bound to `127.0.0.1` and only reachable through the same
server Nginx process, the token can be omitted by leaving
`TRADING_AGENT_SNAPSHOT_API_TOKEN` unset. If the token is set, Nginx must inject
the `Authorization` header as shown above.

## Production Service Shape

Keep the API as a local service and let Nginx handle the public HTTPS surface.
One practical `systemd` shape:

```ini
[Unit]
Description=TradingAgent front snapshot API
After=network.target

[Service]
User=marketgraph
Group=marketgraph
WorkingDirectory=/opt/investment/tradingagent/front
Environment=FINANCE_WORKSPACE_ROOT=/opt/investment/tradingagent
Environment=TRADING_AGENT_SNAPSHOT_HOST=127.0.0.1
Environment=TRADING_AGENT_SNAPSHOT_PORT=8787
Environment=TRADING_AGENT_SNAPSHOT_CORS_ORIGINS=https://dashboard.tradingagent.cc
ExecStart=/opt/investment/tools/node-v24.4.1/bin/node /opt/investment/tradingagent/front/dist-server/server/tradingAgentSnapshotHttp.js
Restart=on-failure
RestartSec=3
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=full
ReadWritePaths=/opt/investment/tradingagent

[Install]
WantedBy=multi-user.target
```

Production verification:

- `curl http://127.0.0.1:8787/healthz` returns `ok`.
- `curl http://127.0.0.1:8787/api/trading-agent/snapshot` returns JSON when
  the service is bound to localhost and token auth is unset.
- `curl --resolve dashboard.tradingagent.cc:80:8.138.181.177 http://dashboard.tradingagent.cc/` returns the React app before DNS is switched.
- The public dashboard route loads the React app.
- The public `/api/trading-agent/snapshot` route returns JSON through Nginx.
- The snapshot response reports simulated display data and does not expose
  execution, account, credential, callback, or mutation routes.

Rollback:

- Keep the previous `front/dist` and `front/dist-server` build directories or
  redeploy the previous Git commit.
- Restart only the local snapshot API service after rolling back server files.
- Nginx can be reverted independently because it only serves static files and
  proxies the read-only route.

The route may read:

- `signals/{pending,claimed,running,filled,cancelled,expired,failed,partial}/*.json`
- `signals/positions/*.json`
- `shared/accounting/position_plan.jsonl`
- `shared/review/daily/daily_brief.jsonl`
- `shared/review/*/style_performance.jsonl`
- `shared/review/attribution/*.jsonl`
- `shared/logs/sim_ledger/*/*/{positions.json,trade_journal.jsonl}`
- `shared/logs/local_sim/local_sim_trades.jsonl`
- `shared/risk/risk_limits.yaml`

The route must not:

- write to `signals/`
- claim, cancel, expire, fill, or mutate signal cards
- import execution routers as action surfaces
- send orders, emails, webhooks, or account callbacks
- merge different account layers into one result number

## Current Gap

The local Vite dev, preview runtimes, Cloudflare Pages route, and server-side
read-only API now use the same snapshot contract. The React app uses local
preview data only when the endpoint is unavailable. If the endpoint is
available but a domain returns an empty array, the UI must show a real empty
state instead of substituting sample returns, opportunities, or holdings.

The production boundary is currently Cloudflare Pages for the static frontend
plus Cloudflare Tunnel to the server-side snapshot API. The same read-only rule
must be preserved: no execution, callback, or order mutation routes belong to
this dashboard.

The data gap is now narrower: server-local simulated ledger positions and trade
journals feed the homepage holdings and signal funnel, and
`shared/review/*/style_performance.jsonl` can feed a real simulated return curve
when present with simulated ledger capital. `midday_review.jsonl`, strategy/factor attribution JSONL,
`risk_limits.yaml`, richer per-signal stage records, and normalized
mark-to-market return series still need upstream data before the UI should
present them as complete panels. The frontend must not infer returns from trade
notional or cost basis.
