# ADR-0002 — Market-specific evolution program

- Status: accepted
- Date: 2026-08-16

## Context

TradingAgent contains materially different market lanes. A-share already has a SampleJournal/KPI scientific evidence path and simulation-only automatic Champion promotion. Crypto has continuous delayed-paper/factor evaluation but its legacy promotion scorecard remains read-only/manual-review. CNFutures is intentionally paused. A single generic `sim → shadow → real → scaled` ladder therefore cannot describe or authorize all three markets.

The repository also has no reliable GitHub Actions budget guarantee and the owner cannot act as a routine approval gate. Normal research, simulation, merge, deployment and simulation-lifecycle work must continue from machine evidence without waiting for human confirmation.

## Decision

1. `docs/EVOLUTION_PROGRAM.md` is the planning authority for system-level and market-specific evolution goals.
2. Each market advances independently through evidence-based phases. Missing evidence in one market does not block safe work in another.
3. Phase exits use multiple evidence axes: data/PIT integrity, sample maturity, post-cost economics, risk, robustness/OOS, multiple-testing awareness, execution realism, reproducibility and operational health.
4. Daily/weekly review thresholds are diagnostics only and cannot promote/demote lifecycle state or authorize live transition.
5. Simulation/shadow lifecycle may promote, demote, retire and rollback automatically when the market-specific scientific authority is ready. Automatic risk expansion and automatic live transition remain disabled.
6. GitHub Actions is optional supplemental validation and cannot be the only merge/deploy gate. When Actions is unavailable, deterministic local/server checks plus fresh readback are sufficient for normal authorized work.
7. Real-money execution remains a separate explicit authority boundary and is not granted by this ADR.

## Consequences

- The old global `shared/review/goals.yaml` must be treated as compatibility review configuration until market-specific diagnostics replace it; it is not lifecycle authority.
- A-share should harden its existing automatic evolution loop rather than restore manual promotion.
- Crypto should migrate from the retired manual-review scorecard to a simulation-only evidence-bound Challenger/Champion registry.
- CNFutures remains preserve-only until its strategic pause is explicitly lifted.
- Planning documents must not duplicate current runtime truth; current state still requires fresh readback/receipts/ledgers.
