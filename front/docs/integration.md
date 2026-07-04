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
| Current opportunities | `TradingAgent/signals/pending/*.json` | other `signals/*/*.json` buckets | Ready |
| Positions | `TradingAgent/signals/positions/*.json` | `TradingAgent/shared/accounting/position_plan.jsonl` | Partial |
| Performance | `TradingAgent/shared/review/daily/daily_brief.jsonl` | `TradingAgent/signals/filled/*.json` | Ready for read model |
| Decisions | daily review and attribution JSONL files | strategy version history | Partial |
| Risk | `TradingAgent/shared/risk/risk_limits.yaml` | PM risk report JSONL | Ready |
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
- `signals[]`: `symbol`, `market`, `status`, `impact`, `confidence`,
  `reason`, `next`, `steps`, plus optional funnel fields `stage`,
  `stageTimes`, and `stageLatencyMinutes`.
- Signal stage timestamps can be supplied as `discovered_at`, `scored_at`,
  `debated_at`, `risk_checked_at`, and `triggered_at`. These drive the
  animated opportunity funnel and should reflect the real pipeline path.
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
FINANCE_WORKSPACE_ROOT=/opt/investment/TradingAgent \
TRADING_AGENT_SNAPSHOT_HOST=127.0.0.1 \
TRADING_AGENT_SNAPSHOT_PORT=8787 \
TRADING_AGENT_SNAPSHOT_CORS_ORIGINS=https://dashboard.tradingagent.cc \
TRADING_AGENT_SNAPSHOT_API_TOKEN=server-only-token \
npm run start:api
```

Routes:

- `GET /healthz`
- `GET /api/trading-agent/snapshot`
- `OPTIONS /api/trading-agent/snapshot`

Security boundary:

- Keep the API bound to `127.0.0.1` behind a reverse proxy when possible.
- Use HTTPS at the proxy layer.
- Require `Authorization: Bearer <token>` when the endpoint is not fully
  private.
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

  root /opt/investment/TradingAgent/front/dist;
  index index.html;

  location / {
    try_files $uri /index.html;
  }

  location /api/trading-agent/snapshot {
    proxy_pass http://127.0.0.1:8787/api/trading-agent/snapshot;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
  }
}
```

The route may read:

- `TradingAgent/signals/{pending,filled,cancelled,expired,failed,partial}/*.json`
- `TradingAgent/signals/positions/*.json`
- `TradingAgent/shared/accounting/position_plan.jsonl`
- `TradingAgent/shared/review/daily/daily_brief.jsonl`
- `TradingAgent/shared/review/attribution/*.jsonl`
- `TradingAgent/shared/risk/risk_limits.yaml`

The route must not:

- write to `signals/`
- claim, cancel, expire, fill, or mutate signal cards
- import execution routers as action surfaces
- send orders, emails, webhooks, or account callbacks
- merge simulated, shadow, and live results into one number

## Current Gap

The local Vite dev and preview runtimes now mount the read-only endpoint and
the React app prefers that snapshot when it is available. The browser still
falls back to mock display data if the endpoint is unavailable or if a domain
does not yet expose usable rows.

The remaining production gap is the hosted server boundary: a production
runtime still needs to mount the same endpoint and point it at the verified
TradingAgent workspace root. That production mount must keep the same
read-only rule and must not expose execution, callback, or order mutation
routes to the dashboard.
