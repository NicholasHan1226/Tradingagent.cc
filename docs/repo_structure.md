# Three-Repo Architecture

TradingAgent is one of three independent finance repositories:

| Repository | Role |
| --- | --- |
| SharedSignals | Provider collection, validation, direct database writes, API/read-model output |
| MarketGraph | Research graph, macro/cross-market evidence, read-only APIs |
| TradingAgent | Strategy evaluation, signal queues, simulated/shadow ledgers, notifications |

## Data Boundary

TradingAgent does not collect market data directly. Production strategy,
screening, simulation and review code must use
`shared.data.reader.TradingagentDataReader`, which prefers the SharedSignals
HTTP API (`SHAREDSIGNALS_API_URL`) and fails closed when data is missing.

Direct SharedSignals SQLite reads are only for explicit local tests or emergency
diagnostics. They require `TRADINGAGENT_ALLOW_SHARED_SIGNALS_SQLITE=1` or the
market-specific diagnostic switch, plus `SHARED_SIGNALS_DB` when a non-default
database path is needed.

## MarketGraph Boundary

MarketGraph provides optional research evidence through its public API/read
model. TradingAgent must not use MarketGraph as a market-data collector and
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
