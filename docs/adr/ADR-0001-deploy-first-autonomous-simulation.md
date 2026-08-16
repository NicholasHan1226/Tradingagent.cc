# ADR-0001: Deploy-first autonomous research and simulation

- Status: accepted
- Date: 2026-08-16

## Context

TradingAgent is intended to learn and improve continuously from real data and simulated/shadow outcomes. Hosted GitHub Actions may be unavailable, and the owner cannot act as a recurring approval step for ordinary model, factor or strategy promotion.

The project already has evidence-bound evaluation and promotion mechanisms. Requiring additional manual review or broad engineering completion before every simulated promotion would reduce sample accumulation and slow the feedback loop without improving the underlying evidence.

## Decision

1. Running data, observation, evaluation and simulation loops take priority over non-essential refactoring and tooling work.
2. GitHub Actions is optional and is not a runtime or release authority.
3. Research/shadow/simulation evolution is evidence-driven and autonomous.
4. When the frozen promotion contract yields `promotion_evidence_ready=true`, the simulated Champion may be promoted automatically and must emit an auditable promotion receipt.
5. Failure of one market, dataset, model family or auxiliary research lane must not become a global stop condition unless it violates a shared hard authority.
6. Runtime correctness, data provenance, capital integrity and execution atomicity remain fail-closed; autonomy is not permission to fabricate missing evidence.
7. Large structural refactors are deferred unless they solve a demonstrated reliability, correctness, security or operating-cost problem.
8. Real-money execution remains a separate authorization boundary from autonomous research/simulation. `REAL_TRADING_ENABLED=false` is not a promotion gate for simulation; it is a boundary against unintended external capital side effects.

## History and authority

- Current machine facts stay in runtime state, ledgers, receipts and fresh readbacks.
- Durable design decisions stay in `docs/adr/`.
- Dated production evidence and incident analysis stay in `docs/reports/`.
- `STATUS.md` should converge toward a short current summary rather than an append-only historical log.
- Ordinary development history is preserved by Git and is not duplicated across README/STATUS/ROADMAP.

## Consequences

The system can continue accumulating data and improving simulated strategies without waiting for a person or a hosted CI runner. Hard evidence and capital-integrity constraints remain in place, while documentation and engineering work become supporting activities rather than global gates.
