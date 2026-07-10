# Three-Repo Architecture

TradingAgent is one of three independent finance repositories:

| Repository | Role |
| --- | --- |
| SharedSignals | Provider collection, validation, direct database writes, HTTP API output |
| MarketGraph | Research graph, macro/cross-market evidence, read-only APIs |
| TradingAgent | Strategy evaluation, signal queues, simulated/shadow ledgers, notifications |

## Data Boundary

TradingAgent does not collect market data directly. Production strategy,
screening, simulation and review code must use
`shared.data.reader.TradingagentDataReader`, which prefers the SharedSignals
HTTP API (`SHAREDSIGNALS_API_URL`) and fails closed when data is missing.

Production never opens SharedSignals SQLite files. Tests that need a local read
model inject an isolated reader directly and cannot enable fallback through
environment variables.

## MarketGraph Boundary

MarketGraph provides optional research evidence through its public API.
TradingAgent must not use MarketGraph as a market-data collector and
must not depend on MarketGraph internal provider scripts or runtime cache paths.

## Trading Boundary

TradingAgent owns:

- Candidate generation, strategy scoring, capital planning and risk gates.
- Simulated/shadow signal queues.
- Server-local simulated fills and ledgers.
- Daily/weekly review and dashboard snapshots.
- Future controlled broker integration, default disabled.

SharedSignals rows are data, not trading signals. MarketGraph research is
evidence, not execution authority.
