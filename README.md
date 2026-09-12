# Sienner

A portfolio evaluation tool. Given a hypothetical allocation — tickers and weights —
it answers three questions:

1. **Backtest** — what would this allocation have done over a historical period?
2. **Optimize** — what weights does the math suggest, given a set of constraints?
3. **Simulate** — what is the distribution of forward outcomes?

## Scope boundary

Sienner evaluates possibilities, not reality. It does not know what you own.

There is no position tracking, no cost basis, no transaction ledger, no broker
integration, no trade execution. If a feature requires knowing actual holdings, it
does not belong in this repository — that is a separate tool.

This boundary is strict and is the main thing to check a proposed feature against.

## Stack

| Layer | Choice |
|---|---|
| Backend | Python, FastAPI |
| Numerical | numpy, pandas, scipy |
| Validation | pandera |
| Database | PostgreSQL |
| Frontend | React, Recharts |
| Market data | Financial Modeling Prep (FMP) |
| Testing | pytest |
| Deployment | Docker Compose, VPS behind Caddy |

No alternative frontend framework. No CSS-in-JS. No LLM features. No auth until
single-user stops being true.

---

## Design decisions

These are settled. Where a decision rests on an assumption, the assumption is named
here and must be a configurable parameter in code rather than a literal buried in a
function.

### The stored unit is a daily return, not an adjusted price

Adjusted close prices are **not immutable**. Every split or dividend rewrites the
entire historical adjusted series retroactively. A split in 2027 changes the adjusted
close of every prior day, which silently invalidates anything cached on top of it.

Sienner therefore stores **raw bars plus corporate actions**, and derives a daily
total-return series as the unit every downstream module consumes:

```
r_t = (C_t · k_t + D_t) / C_{t-1} − 1
```

- `C_t` — raw close on day `t`
- `k_t` — split ratio effective on day `t` (1.0 if no split)
- `D_t` — cash dividend with **ex-date** `t` (0 if none)

Once computed, `r_t` never changes. A future corporate action does not alter the
relationship between two adjacent historical closes. This is the immutability
property the cache design depends on, and it holds here where it does not hold for
adjusted prices.

Equity curves are a cumulative product of returns, computed per request. Cheap.

**Assumption baked in:** dividends are reinvested at the close on the ex-date, with
no tax and no cash drag. This is the standard total-return convention. It is wrong
for a taxable account, and the UI must say so.

### Cache contract

Raw bars are immutable once the day closes; fetch once, never re-fetch. Corporate
actions are append-only. The tool must work fully offline for any ticker-range
already cached.

The schema distinguishes three states that would otherwise look identical:

- **not fetched** — we have never asked
- **fetched, no bar exists** — market holiday, or before listing
- **fetched, bar quarantined** — data present but failed validation

This is what the coverage ledger is for. It is also what makes a later backfill job
idempotent rather than a rewrite.

### Universe

US-listed equities and ETFs on NYSE, Nasdaq, and NYSE American. USD only. One
trading calendar.

Everything else is rejected at ingestion with an error naming the ticker and the
reason. Not a coverage limitation — each excluded class breaks an assumption already
committed to above:

- **Crypto** trades 24/7, so holiday-gap validation becomes meaningless
- **Mutual funds** are forward-priced NAV, not traded bars, with different
  adjustment semantics
- **Non-US listings** need a per-exchange calendar and an FX series, and once
  currencies are converted every downstream metric needs a stated base currency

Widening later is additive: a calendar per exchange, plus an FX layer. The provider
abstraction does not change.

### Validation, in two tiers

Every DataFrame crossing a process boundary — provider output, cached read, backtest
input, metric output — has a declared pandera schema. Failures name the ticker, the
date, and the rule.

**Hard reject:** nulls in required columns, non-positive prices, duplicate dates,
non-monotonic dates.

**Quarantine and report:** single-day absolute log return above a configurable
threshold (default 0.5), and gaps longer than one trading day that do not match the
market calendar.

Quarantined rows are stored with a flag and surfaced in the API response. They are
never silently dropped, and they never silently propagate into a backtest.

Rationale for two tiers: with the adjustment done correctly, a very large single-day
move is either bad data or a real event. Hard-failing on the real ones would block
legitimate backtests.

### Backtest semantics

- **Rebalancing:** none (buy-and-hold), monthly, quarterly, or annually. Executed at
  the close of the last trading day of the calendar period, at that day's price.
  Zero transaction cost, fractional shares. Weight drift between rebalance dates is
  intended behaviour, not a bug.
- **Weights** must sum to 1.0 within 1e-6. Rejected rather than silently normalised.
  Negative weights rejected until Stage 4. Maximum 20 tickers.
- **Misaligned inception dates:** truncate the window to the shortest common history
  across the requested tickers and return the effective start date in the response,
  so the UI can show *requested 2005-01-01, ran from 2013-06-19 (limited by XYZ)*.
  No silent shortening, no zero-filling. *(Provisional — revisit at Stage 1c.)*
- **Holes inside the window** after truncation: forward-fill up to 5 trading days
  (halts are real), surfaced as a warning. Beyond 5 trading days, reject.
- **Equity curve** indexed to 100 at the effective start. No dollar amounts — the
  tool does not know your balance.

### Metrics

Total return, CAGR, volatility, max drawdown, Sharpe ratio.

Annualisation at 252 trading days. The risk-free rate is a configurable constant,
**default 0.0**, displayed in the UI next to the Sharpe figure. At `rf = 0` the
number is a return-over-volatility ratio and the UI should say so rather than imply
otherwise. Fetching a T-bill series is a second data pipeline for a second-order
metric; deferred.

### Bias

The lookahead surface in a weights-and-prices backtester is small, but two real
biases are not:

- **Survivorship.** FMP does not return delisted tickers, so any allocation that can
  be typed in is one that survived. Not solvable at this stage — document it in the
  UI.
- **In-sample optimisation.** Stage 4 must not optimise on a period and then report
  performance on that same period. Train/test separation is designed in from the
  start of the optimiser, not retrofitted.

The backtester must never use information unavailable at the simulated point in
time, and there are tests that would catch a violation.

---

## Schema

Five tables. Names are indicative.

- `securities` — ticker, exchange, listing status, first and last available date
- `daily_bars` — raw OHLCV per ticker-date. Immutable. Never back-adjusted.
- `corporate_actions` — splits and cash dividends, ex-dated. Append-only.
- `fetch_coverage` — which ticker-ranges have been successfully retrieved, and when
- `quarantine` — rows that failed a soft validation rule, with the rule that fired

Daily returns are computed on read in Stage 1b. Materialising them (and rolling
metrics, and correlation matrices) is a later optimisation — the door is left open
for dbt, but nothing is designed around it yet.

---

## Stages

Each stage ends with something openable and usable. No stage begins before the
previous one is deployed and working.

### Stage 1a — walking skeleton

One ticker. Raw closes only, no adjustment, no actions table. Postgres with
`daily_bars` and `fetch_coverage`. FMP provider behind the `MarketDataProvider`
interface. FastAPI endpoint, React page with a single line chart, total return only.
Docker Compose, Caddy, live on the VPS.

**Usable:** type a ticker, see a line — labelled *price return, dividends not
included*.

**Condition that makes the split cheap:** the return-series contract is decided here,
not in 1b. The provider interface returns raw bars; the layer above returns a daily
return series; consumers see only that. In 1a the return computation is a stub with
`k = 1, D = 0`. Filling it in later changes no signature downstream.

**Why deployment comes first:** Compose + Caddy + Postgres + secrets on a box is the
only part of Stage 1 with unknown-unknowns in it. Hitting that friction while the
payload is one ticker means every failure is unambiguously an infra failure.

### Stage 1b — correct data layer

`corporate_actions` table, split and dividend fetch, the real total-return
computation, pandera schemas, `quarantine` table.

Tests that matter here:

- a known-split fixture (NVDA 10:1, June 2024)
- reconciliation against FMP's own `adjClose` within tolerance
- a property test that appends a synthetic future split and asserts every historical
  daily return is byte-identical

**Usable:** the same chart, now correct — and visibly different for anything paying a
dividend, which is the demonstration that it worked.

### Stage 1c — backtest

Multi-ticker, weights, rebalancing frequencies, truncation to shortest common
history, forward-fill rules, full metric set, golden-file test with hand-computed
metrics.

**Usable:** the tool described at the top of this file.

### Stage 2 — risk and correlation

Correlation matrix over a configurable window, rolling volatility and drawdown,
per-asset contribution to return and to risk. Frontend: correlation heatmap, rolling
metric charts.

### Stage 3 — historical scenarios

Named windows (2008 GFC, March 2020, 2022 rate shock, dot-com). Drawdown, recovery
time, worst day per scenario. Frontend: scenario comparison.

### Stage 4 — optimiser

Mean-variance via scipy. Configurable constraints: per-asset min/max weight, target
return or target volatility, long-only vs long-short. Efficient frontier.

Failure cases handled explicitly and never returned as a silently wrong answer:
infeasible constraints, singular covariance matrix, optimiser non-convergence.

### Stage 5 — Monte Carlo

Forward path simulation under selectable return-generating assumptions: IID normal
(labelled in the UI as the naive baseline), bootstrap resampling, block bootstrap.
Cross-asset correlation structure preserved. Percentile outcomes, probability of
loss, probability of hitting a target. Frontend: fan chart, outcome histogram.

---

## Deferred, not designed out

- **Saved allocations.** Everything is request-scoped through Stage 1. Persistence is
  one migration and touches nothing in the price layer. Needed by Stages 3 and 4.
- **Orchestration.** Fetch logic lives in a standalone callable job module with no
  FastAPI imports. That is the seam a Prefect or Airflow wrapper attaches to later.
- **Derived tables.** dbt, if per-request computation becomes the bottleneck.
- **T-bill risk-free series.**
- **Wider universe.** Additional exchange calendars, FX layer.

---

## Open questions

1. **FMP plan rate limit, and whether bulk/batch endpoints are available.** Decides
   whether backfilling 20 tickers × 20 years is one call or four hundred, and whether
   inline cache-miss fetch is tolerable or needs a queue at 1a.
2. **VPS: reuse the existing box or provision a separate one?** Blocks 1a.
3. **Default rebalance frequency in the UI.** Not needed until 1c.

### Spike required before writing 1b

FMP's actual field semantics are not resolvable by discussion. Twenty minutes against
the live key, answering:

- Does `adjClose` adjust for splits only, or splits and dividends? (If the series
  diverges from a self-computed one by roughly the cumulative dividend yield, it is
  split-only.)
- Are dividends ex-dated or pay-dated?
- Is `adjDividend` already split-adjusted? Applying a split correction to an
  already-corrected dividend is quietly wrong on exactly the tickers that split.

---

## Running Stage 1a

```bash
cp .env.example .env          # set POSTGRES_PASSWORD and SIENNER_FMP_API_KEY
docker compose up --build     # http://localhost
```

Caddy is the only published port. The backend is reachable at `backend:8000`
inside the Compose network and nowhere else. `backend/migrations/001_stage1a.sql`
is applied by the Postgres entrypoint on an empty data directory only — to
re-apply it, `docker compose down -v`.

To lift to the VPS: set `SITE_ADDRESS` to the hostname in `.env` and Caddy
provisions TLS on its own. There is no certbot step.

Backend without containers:

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e 'backend[dev]'
cd backend && pytest                       # 67 tests, no key and no network needed
uvicorn sienner.main:app --reload          # degrades to in-memory storage if
                                           # Postgres is unreachable, and says so
                                           # at /api/health
cd frontend && npm install && npm run dev  # proxies /api to :8000
```

### What Stage 1a is not

- **No adjustment.** `sienner/returns.py` computes `r_t` with `k = 1, D = 0`.
  The signature is already the Stage 1b one, and passing it real corporate
  actions raises rather than silently ignoring them.
- **No `adjClose`.** The FMP adapter drops it. It is a Stage 1b reconciliation
  input, never a source of truth.
- **Universe check is shape-only.** `sienner/universe.py` cannot tell `BRK.B`
  from `VOD.L` without an exchange lookup, so it allowlists US class suffixes
  and rejects the rest. The securities table in Stage 1b replaces this.
- **No pandera.** Hard-reject rules are enforced in `providers/base.py` and in
  CHECK constraints; the soft-reject (quarantine) tier does not exist yet.
- **Test fixtures are shaped, not recorded.** See
  `backend/tests/fixtures/README.md`. Re-record them during the spike.
