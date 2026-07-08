# TradingAgent Front Cloudflare Deployment

This document records the Cloudflare migration shape for the TradingAgent front
layer. It is a deployment preparation note, not a trading runtime change.

## Current Status

The active browser-facing dashboard is served from the TradingAgent production
host through `/opt/investment/tradingagent/front`. Cloudflare Pages is a tested
historical deployment and rollback option, not the active production source of
truth.

The last recorded Cloudflare Pages shape was:

| Item | Value |
| --- | --- |
| Pages project | `tradingagent-front` |
| Production URL | `https://tradingagent-front.pages.dev` |
| First deployment URL | `https://d29e260e.tradingagent-front.pages.dev` |
| Latest checked deployment URL | `https://30a39766.tradingagent-front.pages.dev` |
| Historical custom dashboard domain | `https://dashboard.tradingagent.cc` |
| Historical DNS status | `dashboard.tradingagent.cc` was tested as a proxied CNAME to `tradingagent-front.pages.dev` |
| Tunnel | `tradingagent-front-api` (`88b5a0af-35fe-438d-b294-2d1b441631ca`) |
| API domain | `https://api.tradingagent.cc/api/trading-agent/snapshot` |

When the Cloudflare path is re-enabled, the dashboard page is public on
Cloudflare and the snapshot API connects through Cloudflare Tunnel to the
TradingAgent server's local read-only snapshot service on `127.0.0.1:8787`.

The Pages Function at `/api/trading-agent/snapshot` remains the same-origin
read-only proxy shape for that rollback path. It uses the Cloudflare Pages
server-side variable `TRADING_AGENT_SNAPSHOT_UPSTREAM_URL`.

## Cloudflare Target Shape

Move the browser-facing dashboard away from the mainland Alibaba Cloud Nginx
public entry and onto Cloudflare:

```text
Browser
  |
  | HTTPS
  v
Cloudflare Pages
  |
  | read-only snapshot fetch
  v
Cloudflare Tunnel
  |
  | private origin connection
  v
TradingAgent snapshot API on 127.0.0.1:8787
```

The snapshot API remains read-only. It must not expose TradingAgent execution,
order mutation, callback, account, credential, 2FA, email-sending, or queue
write routes.

## Pages Frontend

Cloudflare Pages should deploy the static Vite build from this directory.

Recommended Pages settings:

| Setting | Value |
| --- | --- |
| Project root | `front/` when deploying from the TradingAgent repository root, or this directory when deploying the front repository directly |
| Build command | `npm ci && npm run build` |
| Build output directory | `dist` |
| Production branch | `main` |

The included `wrangler.jsonc` only records the static output directory and
project name. It intentionally does not include account IDs, API tokens, zone
IDs, tunnel credentials, or secrets.

For the browser build, use the same-origin API path:

```bash
VITE_TRADING_AGENT_SNAPSHOT_URL=/api/trading-agent/snapshot
```

`VITE_*` values are public browser configuration. Do not place API tokens,
server-only bearer tokens, local filesystem paths, account IDs, or credentials
in Vite variables.

Configure the upstream URL as a Cloudflare Pages environment variable, not a
browser variable:

```bash
TRADING_AGENT_SNAPSHOT_UPSTREAM_URL=https://api.tradingagent.cc/api/trading-agent/snapshot
```

If the origin API later requires a bearer token, set it only as a Pages secret:

```bash
TRADING_AGENT_SNAPSHOT_API_TOKEN=<server-only token>
```

## Snapshot API Through Tunnel

Preferred first backend shape:

1. Keep the Node snapshot API on the TradingAgent host.
2. Keep it bound to `127.0.0.1:8787`.
3. Run Cloudflare Tunnel on the same host or a trusted private network peer.
4. Publish only the snapshot route through a dedicated hostname such as
   `api.tradingagent.cc`.
5. Keep the API token, if enabled, server-side at the tunnel/proxy layer.

The origin service remains:

```bash
FINANCE_WORKSPACE_ROOT=/opt/investment/tradingagent \
TRADING_AGENT_SNAPSHOT_HOST=127.0.0.1 \
TRADING_AGENT_SNAPSHOT_PORT=8787 \
TRADING_AGENT_SNAPSHOT_CORS_ORIGINS=https://dashboard.tradingagent.cc,https://tradingagent.cc \
node dist-server/server/tradingAgentSnapshotHttp.js
```

Current Tunnel route shape:

```text
api.tradingagent.cc -> http://127.0.0.1:8787
```

If token auth is enabled with `TRADING_AGENT_SNAPSHOT_API_TOKEN`, the browser
must not know that token. Use a server-side proxy that injects
`Authorization: Bearer <server-only-token>`, or keep the tunnel hostname private
behind Cloudflare Access and use a Worker proxy for the public dashboard.

## Worker Proxy Option

Use a Worker when the public dashboard should keep a same-origin API path or
when token injection is required.

Recommended route:

```text
https://dashboard.tradingagent.cc/api/trading-agent/snapshot
  -> Worker
  -> https://api.tradingagent.cc/api/trading-agent/snapshot
  -> Tunnel
  -> http://127.0.0.1:8787/api/trading-agent/snapshot
```

Worker responsibilities:

- Forward only `GET` and `OPTIONS` for `/api/trading-agent/snapshot`.
- Reject every other method and path.
- Inject a server-side bearer token only from Worker secrets.
- Set `Cache-Control: no-store`.
- Return only the snapshot response; do not add mutation routes.

Do not implement trading execution, queue mutation, callbacks, account control,
or email sending in the Worker.

## DNS

Recommended target split:

| Hostname | Cloudflare target | Purpose |
| --- | --- | --- |
| `dashboard.tradingagent.cc` | Cloudflare Pages custom domain | Dashboard UI |
| `tradingagent.cc` | Cloudflare Pages custom domain or redirect to dashboard | Public dashboard entry |
| `www.tradingagent.cc` | Redirect or Pages custom domain | Convenience alias |
| `api.tradingagent.cc` | Cloudflare Tunnel public hostname | Read-only snapshot API |

`dashboard.tradingagent.cc` has already been moved away from the mainland
Alibaba Cloud A-record entry. `api.tradingagent.cc` is routed through the
Cloudflare Tunnel record. Keep the old Nginx server available during the
migration window so rollback is fast. The apex and `www` records are still left
on the previous Alibaba Cloud A-record route until they are intentionally moved.

## CORS

When the frontend calls a separate API hostname, configure the Node snapshot API
with only the deployed dashboard origins:

```bash
TRADING_AGENT_SNAPSHOT_CORS_ORIGINS=https://dashboard.tradingagent.cc,https://tradingagent.cc
```

Do not use `*` for this dashboard. If a Worker provides the same-origin route,
CORS can stay narrower because the browser fetches `/api/trading-agent/snapshot`
from the same host.

## Security Boundary

Keep these boundaries explicit:

- The front layer is a read-only display surface.
- The snapshot API reads display-ready TradingAgent state only.
- The API should stay bound to `127.0.0.1` on the origin host.
- Browser configuration can contain only public URLs.
- Tokens live only in server-side service configuration, Cloudflare Tunnel
  private configuration, or Worker secrets.
- Cloudflare Access may protect `api.tradingagent.cc` during testing.
- No Cloudflare rule should expose `signals/`, local files, order queues,
  execution routes, account callbacks, email endpoints, or credentials.

## Deployment Checklist

1. Build locally with `npm run build` and `npm run build:api`.
2. Confirm the origin API health on the host:
   `curl http://127.0.0.1:8787/healthz`.
3. Confirm the origin snapshot returns JSON and no mutation surface.
4. Create the Pages project with output directory `dist`. Done:
   `tradingagent-front`.
5. Set `VITE_TRADING_AGENT_SNAPSHOT_URL=/api/trading-agent/snapshot`.
6. Deploy the Pages Function proxy. Done.
7. Set DNS/custom domains in Cloudflare. Done for `dashboard.tradingagent.cc`.
8. Verify the public dashboard loads from Pages. Done.
9. Create the Tunnel public hostname for `api.tradingagent.cc`. Done.
10. Set `TRADING_AGENT_SNAPSHOT_UPSTREAM_URL` in Cloudflare Pages. Done.
11. Verify the browser snapshot call returns JSON through Cloudflare. Done.
12. Keep the Alibaba Cloud Nginx route available until the Cloudflare route has
    been checked from normal browsers.

## Rollback

Rollback should not require changing TradingAgent execution code.

Fast rollback:

1. Point dashboard DNS back to the previous Alibaba Cloud Nginx entry, or pause
   the Pages custom domain route.
2. Keep the existing Nginx route serving `front/dist`.
3. Keep the Node snapshot API on `127.0.0.1:8787`.
4. Reset `VITE_TRADING_AGENT_SNAPSHOT_URL` to the same-origin
   `/api/trading-agent/snapshot` shape for the Nginx build.

Backend rollback:

1. Disable or remove the Tunnel public hostname.
2. Disable the Worker route if one was added.
3. Leave the local `tradingagent-front-api.service` running for the old Nginx
   route.

## Not Covered Yet

- No Cloudflare Access policy is configured yet for `api.tradingagent.cc`.
- `tradingagent.cc` and `www.tradingagent.cc` still point to the previous
  Alibaba Cloud A-record route.
- The snapshot currently returns the live API envelope, but several data
  domains may still be empty until TradingAgent writes richer performance,
  holdings, signal timeline, and decision records.
