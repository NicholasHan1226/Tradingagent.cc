# TradingAgent current status

Observed at: 2026-09-04T21:35:00+08:00. This replaceable snapshot separates
source, configuration, runtime and market evidence. Simulation only; no live
trading, new capital, risk expansion or strategy promotion.

## 2026-09-04 21:35 CST three-surface garden

- Local canonical `main` fast-forwarded `33f6c90f` → `53734f2b` (`#639`).
  `git rev-list --left-right --count HEAD...origin/main` = `0 0`; worktree clean.
- GitHub `origin/main` = `53734f2b3e1bb38841a8535c11250540caf71650`. The 13
  commits local had lacked are ashare catalog/minute bindings (`#631`/`#632`)
  plus crypto health/forty-symbol observer/deploy pin (`#634`–`#639`). They
  are already on the immutable production pointer below; this round did not
  merge extra feature PRs.
- Open leftovers: `#630` (in-session clocks) is `CONFLICTING` against main —
  not merged. Drafts `#606`/`#608`/`#610`/`#612` kept. No `automerge-m0`
  hygiene candidates besides this page.
- Production files: `marketgraph-main`
  `/opt/investment/releases/tradingagent/current` → `53734f2b` with matching
  `.source-sha`. No TradingAgentSource checkout. This round did not switch
  releases. `REAL_TRADING_ENABLED=false` on 26 `tradingagent-*.service` units.
  `tradingagent-front-api` is `active/running`.
- Runtime health (not gardening): failed oneshots with `ExecMainStatus=2` —
  `ashare-minute-scale500-paper` (15:07), `ashare-minute-scale500-session`
  (09:36), `ashare-minute-session` (09:18), `crypto-ten-symbol-factor-research`
  (21:29), `crypto-ten-symbol-observation` (21:28). Not restarted.
- Worktrees: removed 4 extra trees under `TradingAgent-worktrees/` (2 ancestors
  of origin/main, 2 patch-equivalent). Kept canonical plus
  `.worktrees/market-{ashare,cnfutures,crypto}`. Local patch-equivalent
  branches `codex/cnfutures-td-major2-consumer-20260830` and
  `codex/ashare-td-major2-consumers-20260830` retained after worktree removal.

The 2026-08-31 private-API / account-relocation snapshot below is historical
and is not a current main/release pointer.

## Source and release layers

| Layer | Current verification | Boundary |
|---|---|---|
| 本地主线 | resolve with `git rev-parse HEAD origin/main` | isolated delivery checkout; user research checkout preserved |
| GitHub 主线 | resolve with `git rev-parse HEAD origin/main` and matching CI | remote source is separate from the effective runtime |

- Existing accepted runtime: PR [616](https://github.com/NicholasHan1226/Tradingagent.cc/pull/616),
  immutable release 752845b79381532838e1fb223e9b105afe6d16b0.
  Candidate CI33315398287 and exact-main CI33315864586 passed 6,211 Python
  tests, one skip, 266 subtests and 429 frontend tests. Deployment33316330837
  actually published the artifact on Aug-30. Package SHA256:
  8c4c6c1c5c36249d7d77622aa6de9764c6fecae21406a0bbd795097682e2c65a.
- Aug-31 effective-runtime readback binds the front process, unit and current
  to that release without blockers. No application release switch in this
  configuration/account-relocation batch. Resolve GitHub main afresh; this
  document is not a self-updating main pointer.
- Ordinary server source was last verified at 5cd9649, not the active release.
  It was not synchronized or reset. Research/user worktrees preserved.
- Nine release bindings/timers were restored in PR616's 117-second window.
  This batch does not stop or change A-share/Crypto simulators.

## Private API: origin restriction and reader activation

Nicholas explicitly authorized restricting the named API to localhost and
relocating the original A-share account without resetting capital.

- Before correction, a real non-loopback connection returned snapshot HTTP200
  through nginx. External direct-origin requests currently meet an Aliyun
  Beaver/ICP HTTP403; that is **not** nginx authorization proof.
- Only the exact /api/trading-agent/snapshot location in the TA nginx site was
  changed: allow127.0.0.1/::1, deny all. No real-IP rewriting was present.
  Listener, static root, health route, other sites, DNS and credentials unchanged.
- Candidate and installed nginx syntax checks passed. **29 actual origin checks
  passed**: all three TA hosts, forged forwarding headers and normalized paths
  denied; unmatched variants returned only byte-identical static HTML.
  Localhost snapshot/no-store, direct backend, healthz and static page retained.
- Installed site SHA256:
  a131695acce0f3d275bceb365e885e71478522cd1c61b275a2517fbfa71fddd6.
  Private backup/request matrix: /var/tmp/ta-private-ingress-20260831.qLFAz9/.
- After origin denial proof, the accepted front unit's two runtime-reader
  environment entries were installed, with no other unit field changes.
  Unit SHA256: da08828150c1e75c7c42ff77473d64bcc5e9ad35666d36c307c3b24d3c26e538.
  The process is active. First response was pending, then A-share correctly
  showed Aug-28 as dated. Crypto initially returned an isolated validation
  failure. Some direct reads outside and inside the front mount namespace were
  healthy, but the natural 00:46 cache refresh reproduced the failure. A bounded
  diagnostic under the same clean child environment identifies
  round_trip_health_core_incomplete. This is unresolved source/health-state
  handling, not proven credential failure or proven capital corruption. The
  failing Crypto entry remains isolated; no validation or permission was relaxed.
  A later natural refresh at00:52 did return Crypto ready (market slot00:40,
  5,949 completions), also visible in the actual browser. This proves successful
  deployed-reader consumption, not resolution of intermittent incomplete states.
- Local browser acceptance used the actual deployed API through a loopback SSH
  tunnel, not a replay fixture. At1280x720 it showed dated A-share3188/1197/1991,
  no horizontal overflow, per-market filtering, no All Markets money, and no
  runtime observation panel in live mode. Public static files were not changed.
- Public static root remains the old 69ca4475 build. This does not claim the
  public page is updated, remote single-user access configured, or historical
  Pages/Tunnel routes audited. IPv6 has no active listener and was not opened.
  Remote snapshot access remains unavailable.

## A-share: original account relocated, trading entry still unwired

The original ashare-capital-v1 is byte-identically relocated from its Aug-24
recovery directory to /var/lib/tradingagent/ashare-canonical:

- Capital relative path: shared/logs/capital/ashare/.
- Execution relative path:
  shared/logs/execution_lineages/ashare-sim-fresh-20260712-v1/,
  derived from the verified snapshot, not a newly selected lineage.
- **43 files**, including 32 reconcile sources, copied without byte changes.
  Canonical replay checks all **33 events**, the JSON-normalized projection,
  execution manifest/outbox hashes, empty trade/receipt files and zero pending
  actions. The actual service identity replays the target successfully.
- Cash/equity remains **50,000 CNY**, generation1, zero positions, reservations,
  fills and PnL. Account timestamp remains **2026-07-22**: a relocated dated
  account, not new funding or current market PnL.
- Events SHA256: a9459349fada47b5fcfd3ba5a9013a9c08cadea0e6fef3fee0adb8012af132e6.
  Latest SHA256: e65d4becc7013aa7b63a53edc2ac5f17853c93b236c05eaf8ecd18ae125d32ef.
  Head: 1f0be2d18d63e6e162f07b7d4c59934d6f3577f01eea4d0a1c8dfbcbd6755d2b.
- Originals retained, root-owned and immutable. Target data subtrees are owned
  by tradingagent only, directories0700/files0600, with root-owned parents.
  Actual append-open/no-write probes prove the new identity can write the target,
  the old identity cannot write either copy, and front's mount namespace rejects
  writes with EROFS. No bytes were written by the probes.
- Private receipt: /var/lib/tradingagent/ashare-canonical/migration-receipt.json.
  Metadata backup: /var/tmp/ta-account-migration-20260831.llaxnm8p/.
  No source writer was open; no legacy scheduler restarted or active service
  redirected to the relocated root.

compose_capital_backed_paper_runtime remains test-only/network-closed.
Production quote/calendar/Champion adapters and the capital-backed entry are
**unwired**. Keep canonicalAccountConnected=false. Relocation and write
readiness cannot be promoted into full-chain simulated-trading success.

## A-share coverage and next natural session

Latest natural evidence remains Aug-28: **1,197 accepted / 3,188 active**,
1,991 missing at the persisted slot; four counterfactual books have zero fills.
These are research books, not four capital authorities. Preserved bundle SHA256:
53b1d9e5de51476403fd008f02642edd524e9988be5bb31c24761bd9554e291e.

Aug-30 service-identity preopen preparation (isolated roots, not natural runtime)
proved Aug-31 open: **3,193 source / 3,191 age-eligible / 3,187 prior closes**.
Four missing closes: 000635.SZ, 000711.SZ, 002274.SZ, 002586.SZ; two listings
pending individually. Existing stocks' clocks were not reset. Baseline30/30.
Four existing timers still schedule Aug-31 initialization at09:18 and delayed
bar attempts at09:42. Monday execution/fills are not yet proven.

## Crypto track

The G5 delayed-paper service was bound to the accepted752845b release in the
Aug-30 deployment; this batch did not change its binding or scheduler.
Full forty-symbol coverage and a latest continuous 288-bar segment constrain
runtime maturity and later authority claims; they do not block safe-segment simulation.

PR616's actual rolling evaluation completed Aug-30 22:17:02: 2,170 events,
two history segments, 73 contiguous slots, 40 symbols and85 bars per symbol.
Its116 resolved counterfactual trips are not account fills or runtime returns;
tradeable PIT, capital, execution and promotion remain false. Daily wrapper
uses the canonical store and unique output path; no new timer added.

G5 health service last checked here exited successfully Aug-31 00:34:47.
Independent read-only health also succeeded under the front mount namespace.
Neither establishes48h stability or profitability. Research drafts606/608/610/612
remain outside this delivery.

TD's last authenticated consumer readback remains the Aug-30 record, not refreshed
in this batch: rt_min and broker_recommend consumable then; rt_min_daily major3
producer/readback still unproven then. TA does not deploy TD or wait on that
dataset for independent work.

## Copilot and paused scopes

No Copilot publication or account-state writes were performed. CNFutures and
prediction markets remain paused; no research draft was merged or deployed.

## Lessons and remaining work

- Gate the dependent stock/dataset/window, not the whole changing universe.
  Preserve valid observations and rejection evidence without inventing fills.
- A localhost backend, injected token, CORS or edge403 does not authenticate
  a remote browser. Test the real proxy peer boundary and legitimate control.
- Policy, ownership and an old ledger do not prove runtime account connection.
  Preserve timestamps and verify execution-root/head identity.
- Read-only means no bootstrap, repair or lock-file creation. Validate under the
  actual service sandbox, not only the same UID in an ordinary shell.
- Next: resolve Crypto incomplete-core display semantics and implement bounded
  capital-backed adapters while existing stock samples keep accumulating.
  No global maturity wait, capital reset, legacy fallback or automatic live mode.

Rollback preserves all account bytes and new append-only facts. Disable/revert
the reader if necessary, retaining nginx restrictions. Account rollback requires
fencing target writers and checking head continuity; never overwrite the target
with the original or recursively unfreeze the recovery tree.
