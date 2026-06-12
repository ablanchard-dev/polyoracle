# Polyoracle

> Local-first **smart-money copy-trading research bot** for Polymarket — full-stack
> (FastAPI + Next.js). It scans the market, audits the best wallets, scores each
> opportunity through a strict copyable-edge filter, and paper-trades only the rare
> high-conviction signals.

![Python](https://img.shields.io/badge/Python-3.11%2B-3776AB?logo=python&logoColor=white)
![FastAPI](https://img.shields.io/badge/FastAPI-backend-009688?logo=fastapi&logoColor=white)
![Next.js](https://img.shields.io/badge/Next.js-frontend-000000?logo=nextdotjs&logoColor=white)
![Tests](https://img.shields.io/badge/tests-800%2B-success)
![License](https://img.shields.io/badge/License-MIT-green)

> **Status : research / paper.** Live trading is locked by default — an edge must first
> be proven to survive copy delay, spread, slippage, liquidity and risk controls. The
> value here is the **engineering and the honest methodology**, not a profit claim.

**Stack** — FastAPI · SQLModel · SQLite · Next.js / React · pytest (800+ tests).

## Core Philosophy

POLYORACLE does not assume that copying top wallets is profitable. It must test whether an edge survives copy delay, spread, slippage, liquidity, position sizing and risk controls.

> Observe massively. Filter sévèrement. Trade only when the edge is clear.

```text
Observe -> Understand -> Simulate -> Measure -> Prove -> Execute
```

Live trading is blocked by default and is not a v0 priority.

## Zero-Cost / Local-First

POLYORACLE runs on a single PC with no paid service:

- SQLite by default.
- Local `data/` folder for snapshots, exports and logs.
- Real Polymarket public APIs (Gamma + CLOB public + Data API) with mock fallback when offline / rate-limited.
- No API key required.
- No Docker required.
- PostgreSQL, Redis and Docker remain optional.
- No paid charting or market data dependency.

Default local settings:

```env
LOCAL_FIRST=true
STORAGE_BACKEND=sqlite
SQLITE_PATH=./data/polyoracle.db
DUCKDB_ENABLED=false
POSTGRES_ENABLED=false
REDIS_ENABLED=false
LIVE_ENABLED=false
PAPER_TRADING_ENABLED=true
MOCK_DATA_ENABLED=true
POLYMARKET_PUBLIC_ENABLED=true
MARKET_FETCH_LIMIT=100
ORDERBOOK_SNAPSHOT_ENABLED=true

# v0.4 paper auto trading
PAPER_CAPITAL=1000
PAPER_MAX_RISK_PER_TRADE=0.01
PAPER_MAX_EXPOSURE=0.20
PAPER_MAX_MARKET_EXPOSURE=0.05
PAPER_MAX_DAILY_LOSS=0.03
PAPER_MAX_WEEKLY_LOSS=0.08
MIN_SIGNAL_SCORE=75
MIN_CONFIDENCE_SCORE=60
MIN_COPYABLE_EDGE=60
MAX_SPREAD_PCT=0.03
MIN_LIQUIDITY_SCORE=50
AUTO_PAPER_TRADE_ENABLED=true

# v0.4 bot loop tuning
TOP_WALLETS_TARGET=100
TOP_WALLETS_MIN=50
MAX_WALLET_TRADES_PER_AUDIT=500
MAX_MARKETS_PER_CYCLE=100
MAX_TRADES_PER_CYCLE=1000
AUDIT_INTERVAL_SECONDS=120
WALLET_REFRESH_INTERVAL_MINUTES=30
MARKET_SCAN_INTERVAL_SECONDS=60
ORDERBOOK_FETCH_LIMIT=50
TRADE_AUDIT_ENABLED=true
AUTO_WATCHLIST_ENABLED=true
```

## Safety Model

Modes:

- `OFF`: nothing runs except health/status.
- `RESEARCH`: read-only analysis (default).
- `PAPER`: simulated execution. Auto paper trades only when wallet tier ≥ STRONG, signal decision = PAPER_TRADE, copyable edge, spread, orderbook quality and exposure all pass.
- `SEMI_AUTO`: future proposal mode with human validation.
- `LIVE`: disabled by default. The BotLoop refuses `LIVE`. ExecutionEngine still requires compliance + manual confirmation.

There is no VPN, bypass, geographic circumvention or hard-coded private key logic.

## Run Locally Without Docker

Fast Windows start:

```bat
scripts\start-all.cmd
```

Backend only:

```bat
scripts\start-backend.cmd
```

Frontend only:

```bat
scripts\start-frontend.cmd
```

Backend:

```bash
cd polyoracle/backend
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python -m app.main
```

Alternative:

```bash
uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

Frontend:

```bash
cd polyoracle/frontend
npm install
npm run dev
```

Open:

- Dashboard: `http://localhost:3000`
- API: `http://localhost:8000`
- Health: `http://localhost:8000/health` → `{"status":"ok","version":"0.4.0"}`

## v0.5.3 / v0.5.4 — strict ELITE rule + ValidatedPaperUniverse

The strict ELITE rule from v0.5.2 was tightened further: a wallet is now ELITE only if **all** of the following hold simultaneously, otherwise it falls into the new `OUTLIER_FLAGGED` status (or one of the existing rejection statuses):

- `expanded_sample ≥ 100`
- `win_rate_confidence == "HIGH"`
- `validation_sample ≥ 10`
- `validation_win_rate ≥ 0.70`
- `|discovery_win_rate − validation_win_rate| ≤ 0.15`
- not `BIASED_SAMPLE`, not `FAILED_VALIDATION`, not survivor-bias-flagged
- recent activity above the inactive threshold

The 3-year / 3000-market run + OOS validation produced (146 990 wallets evaluated):

| Status | Count |
|---|---:|
| ELITE | 52 |
| STRONG | 161 |
| CANDIDATE_ELITE | 2 033 |
| OUTLIER_FLAGGED | 4 |
| BIASED_SAMPLE | 114 |
| FAILED_VALIDATION | 2 |
| DROPPED | 144 624 |

The merged **ValidatedPaperUniverse** (730d + 1095d) lives in `data/exports/validated_paper_universe_latest.csv`. After re-applying the strict gap ≤ 15% / val_wr ≥ 70% gate at merge time (so legacy reports predating `OUTLIER_FLAGGED` get demoted automatically):

| Metric | Count |
|---|---:|
| Universe entries | **216** |
| ELITE | **52** |
| STRONG | **164** |
| Allowed in SAFE (gap ≤ 0.10 + val_wr ≥ 0.80) | **50** |
| Allowed in AGGRESSIVE | 216 |
| Allowed in FULL_PAPER | 216 |
| Excluded — OUTLIER_FLAGGED | 5 (incl. legacy `0xa66790e2…`) |
| Excluded — BIASED_SAMPLE | 161 |
| Excluded — FAILED_VALIDATION | 2 |
| Excluded — CANDIDATE_ELITE / sample-thin | 2 862 |
| Source split | 1095d only: 142 / 1095d+730d: 71 / 730d only: 3 |

Top 3 by sample (all `allowed_safe=True`):

1. `0xb6fa57039ea79185895500dbd0067c288594abcf` — sample 1235, wr 94.8%, val 136m@94.1%, gap 0.8%
2. `0x30cecdf29f069563ea21b8ae94492e41e53a6b2b` — sample 1126, wr 95.8%, val 132m@91.7%, gap 4.7%
3. `0xe8dd7741ccb12350957ec71e9ee332e0d1e6ec86` — sample 913, wr 96.1%, val 99m@92.9%, gap 3.5%

Endpoints:

- `POST /discovery/universe/merge` — rebuild the universe by merging the latest 730d + 1095d validation reports (or any list of `(label, path)` tuples passed in the body).
- `GET  /discovery/universe/latest` — current summary (counts, exclusions, top addresses, latest CSV path).

UI: a green `Validated Paper Universe (v0.5.4)` panel at the top of `/wallets` shows the live counts, an "Awaiting operator signal" warning, and a rebuild button.

## v0.5.2 risk modes — SAFE / AGGRESSIVE / FULL_PAPER

Three named profiles drive the paper-auto-trade gate. Active profile is read from `RiskModeState` (default `SAFE`, configurable via `.env` `RISK_MODE` and the `/risk/mode` endpoint or the UI selector in `/control`). Live execution is permanently blocked on every profile.

| Profile | Allowed status | Min sample | Confidence | Risk/trade | Wallet | Market | Total | Open | Daily |
|---|---|---:|---|---:|---:|---:|---:|---:|---:|
| **SAFE** | ELITE / STRONG | 30 | MEDIUM/HIGH required | 1% | 5% | 3% | 15% | 5 | 10 |
| **AGGRESSIVE** | ELITE / STRONG / CANDIDATE_ELITE | 20 | any | 2% | 8% | 5% | 25% | 15 | no cap |
| **FULL_PAPER** | ELITE / STRONG / CANDIDATE_ELITE | 10 | any | 1% | 10% | 7% | 40% | ∞ | no cap |

Live comparison on the top-50 strict ELITE/STRONG wallets (the bot offered the SAME 50 signals to each profile):

| Mode | Opened | Rejected | Exposure | Top reason |
|---|---:|---:|---:|---|
| SAFE | 5 | 45 | 5.00% | open-positions cap (5) hit |
| AGGRESSIVE | 15 | 35 | 15.00% | open-positions cap (15) hit |
| FULL_PAPER | 41 | 9 | 41.00% | total-exposure cap (40%) hit |

Invariant locked in: SAFE ≤ AGGRESSIVE ≤ FULL_PAPER. The kill switch and exposure caps still apply in all three modes; the only thing FULL_PAPER removes is the daily/per-mode trade-count cap.

The strict ELITE rule was tightened in v0.5.2: a wallet now needs **HIGH** win-rate confidence (i.e. resolved-market sample ≥ 100) to land in ELITE. A 100% win rate on sample 99 / MEDIUM confidence stays in STRONG. The 730-day re-classification pass moved 1 borderline wallet from ELITE→STRONG, giving the final tier breakdown:

| Tier | Count |
|---|---:|
| ELITE | 22 |
| STRONG | 30 |
| WATCH | 94 |
| WEAK | 867 |
| IGNORE | 343 |
| INSUFFICIENT_DATA | 75 156 |

OOS validation on the same 730d sample (700/300 temporal split):

- **5 of 22 strict ELITE** kept ELITE/STRONG status; **0** failed validation; **0** flagged BIASED_SAMPLE; the rest fell to CANDIDATE_ELITE because their newest-30%-of-markets sample was too small (< 10 markets in the validation window) — not because they lost their edge.
- **23 of 30 strict STRONG** kept ELITE/STRONG status; **0** failed validation; **0** flagged BIASED_SAMPLE.
- Survivor gap |discovery − validation|: avg 4.2%, max 22.9% (one outlier `0xa66790e2…` 88.1% disc → 65.2% val on small validation sample).
- Survivor bias: OK (93% of markets show losing wallets).
- 5-min crypto dominance: 0%.

Endpoints:

- `GET  /risk/mode` — current profile + available modes
- `POST /risk/mode` (body `{"mode": "SAFE", "by": "ui"}`) or `POST /risk/mode/{name}`
- `GET  /risk/mode/limits` — limits of the active profile
- `GET  /risk/profiles` — full registry

EdgeValidationEngine extended with `risk_mode_safe`, `risk_mode_aggressive`, `risk_mode_full_paper` projections so the strategy comparator now shows what each profile would have accepted on the existing audit set.

## v0.5.1 candidate validation — out-of-sample check

Once v0.5 has produced a short-list of CANDIDATE_ELITE wallets, run the validation pass to see which of them survive a larger sample AND a temporal out-of-sample split:

```bash
curl -X POST http://localhost:8000/discovery/market-first/validate \
  -H "Content-Type: application/json" \
  -d '{"days_back": 365, "max_markets": 300, "split_ratio": 0.7}'
```

Live result (300 usable resolved markets, 22 152 wallets aggregated, 0 API errors):

| Metric | Value |
|---|---|
| Markets scanned / usable | 600 / 300 |
| Discovery / validation split | 210 / 90 |
| v0.5 candidates re-evaluated | 23 / 23 |
| ELITE | 0 |
| STRONG (validated out-of-sample) | **7** |
| CANDIDATE_ELITE | 125 |
| BIASED_SAMPLE | 21 |
| FAILED_VALIDATION | 0 |
| DROPPED | 21 999 |
| Survivor bias | OK (93% of markets show losing wallets) |
| 5-min crypto dominance | OK (0%) |

Of the 23 candidates v0.5 surfaced:

- **5 promoted to STRONG** (held up across the 70/30 temporal split): `0xa2d19a…`, `0xa3e9a7…`, `0xe4c6fd…`, `0xf6cbda…`, `0x8a9777…`. All have validation win rate ≥ 80% on samples ≥ 15 markets, and discovery vs validation gap ≤ 0.10.
- **14 stay CANDIDATE_ELITE** because their sample is still 14-29 resolved markets — promising, not yet provable.
- **4 DROPPED** (those whose v0.5 win rate was already ~47% — confirmed no edge).

Plus **2 new STRONG wallets** the expansion surfaced that v0.5 had missed (`0x3e27b4fc…` 60 markets @ 98% / val 100%, `0x1c22d715…` 32 markets @ 100% / val 100%).

The bot stays strict: 0 ELITE because none of the 7 STRONG wallets have ≥ 100 resolved markets yet. Honest answer rather than inflated claim.

Exports (every run overwrites these):

- `data/exports/candidate_elite_validation_report.json`
- `data/exports/candidate_elite_wallets.csv`
- `data/exports/market_first_discovery_report_previous.json` (snapshot of the v0.5 baseline used for the comparison)

## v0.5 market-first discovery — primary path

POLYORACLE no longer trusts the recent `/trades` feed as a primary signal of skill. The v0.5 pipeline starts from markets that have already resolved, walks the wallets that traded each market, and only credits a wallet with a *win* if it was net long the winning outcome at resolution.

```bash
curl -X POST http://localhost:8000/discovery/market-first/run \
  -H "Content-Type: application/json" \
  -d '{"days_back": 180, "max_markets": 80, "trades_per_market": 600}'
```

What you'll see (from a real run, 80 resolved markets, ~50s):

| Metric | Value |
|---|---|
| Markets scanned / usable / rejected | 80 / 80 / 0 |
| Wallets discovered | 3 930 |
| Wallets with reliable (MEDIUM/HIGH) win rate | 0 |
| Tier breakdown | 0 ELITE / 0 STRONG / 0 WATCH / 23 WEAK / 1 IGNORE / 3906 INSUFFICIENT_DATA |
| Status breakdown | 1540 RECENTLY_ACTIVE_UNPROVEN / 2390 IGNORE |
| Avg / median win rate | 73.6% / 100% |
| Avg resolved-market sample / wallet | 1.26 |
| Conclusion | `MARKET_FIRST_NEEDS_MORE_MARKETS` |

Honest reading: with only 80 resolved markets in window, the average wallet was seen on ~1 market — not enough to clear the 30-resolved-market bar required for STRONG. The 23 WEAK candidates have win rates ≥85% on samples 12-23, but `LOW` confidence keeps them out of STRONG by design (we never invent ELITE).

To raise the sample size, run with a larger window:

```bash
curl -X POST http://localhost:8000/discovery/market-first/run \
  -H "Content-Type: application/json" \
  -d '{"days_back": 365, "max_markets": 300}'
```

Exports:

- `data/exports/market_first_discovery_report.json` — full report with conclusion, warnings, top wallets.
- `data/exports/market_first_wallets.csv` — every audited wallet, ranked by `market_first_score`.
- `data/exports/market_first_markets.csv` — every market scanned with usable/rejected flag.
- `data/exports/market_first_rejected_markets.csv` — only rejected, with reason code.

## v0.4.2 discovery audit — what we learned

Run a real-data discovery audit to check whether POLYORACLE actually finds smart wallets:

```bash
curl -X POST http://localhost:8000/wallets/discovery/audit \
  -H "Content-Type: application/json" \
  -d '{"limit": 100, "batch_size": 25}'
```

Results from a live run (20 wallets audited via real APIs):

| Metric | Value |
|---|---|
| Data source | `polymarket_data_recent_trades` |
| Tier breakdown | 15 WATCH / 4 WEAK / 1 INSUFFICIENT_DATA / 0 ELITE / 0 STRONG |
| Win rate (avg / median) | None / None — every wallet had 0 resolved markets in their recent trade window |
| Conclusion | `DISCOVERY_VOLUME_BIASED` |

Three findings:

1. **The legacy `/leaderboard` endpoint is dead** (returns 404 in production). Discovery now uses `/trades` (recent global trade feed), aggregated by `proxyWallet` and ranked by recent trade notional.
2. **Recent-trade discovery is volume-biased**, not skill-biased. Top wallets are simply the ones trading the most right now (typically $50–$1000 notional bursts on 5-minute BTC up/down markets), not historically skilled traders.
3. **Win rate cannot be validated from this source alone**: the markets these wallets touch are currently active (not resolved), so the WinRateEngine returns INSUFFICIENT_DATA for all of them. The engine is correct — the data is the limit.

Recommended next step (not coded yet): a **market-first discovery** that walks recent *resolved* markets and surfaces the wallets that consistently held the winning outcome. See ROADMAP v0.5.

Exports of every audit:

- `data/exports/wallet_discovery_audit.json` — full report (warnings, rationale, audited wallets with sub-scores).
- `data/exports/wallet_discovery_top100.csv` — ranked CSV with score, tier, sub-scores and win-rate columns.

## How to test POLYORACLE v0.4.1 (stability check)

A short procedure to validate the bot end-to-end without any paid service:

1. Make sure no other project is leaking a `DATABASE_URL` env var: `unset DATABASE_URL` (Windows PowerShell: `Remove-Item Env:DATABASE_URL`). If `DATABASE_URL` points elsewhere, POLYORACLE will respect it and your audits will silently land in the wrong DB.
2. Start the stack:

   ```bat
   scripts\start-all.cmd
   ```

3. Open `http://localhost:3000/control`.
4. Click **Run once**. The dashboard shows: markets scanned, wallets audited, trades audited, signals generated, paper trades opened, rejected signals, last cycle duration, and a per-reason rejection breakdown (LOW_SIGNAL_SCORE / NOT_PAPER_DECISION / WIDE_SPREAD / NO_COPYABLE_EDGE / LATE_ENTRY / TOO_MUCH_EXPOSURE / WALLET_NOT_RELIABLE / BAD_ORDERBOOK / LOW_LIQUIDITY / KILL_SWITCH / INSUFFICIENT_DATA).
5. Click **Mode → Research**, then **Run once**. Rejected signals stay at 0 (Research never trades) and `paper_trades_opened` is 0.
6. Click **Mode → Paper**, then **Run once**. The bot opens at most one paper trade per signal (signal-id and market-outcome dedupe). A second **Run once** must keep the position count flat.
7. Open the pages:
   - **Control Room**: per-cycle metrics, rejection breakdown and recent no-trade entries.
   - **Smart Wallets** (`/wallets`): top wallets and audited tiers.
   - **Trade Audit** (`/trades`): every audited trade with quality, decision and warnings.
   - **Signals** (`/signals`): each smart-money signal with proposed size and copyable edge.
   - **Paper Trading** (`/paper`): open/closed positions, capital, daily PnL, exposure.
   - **Edge Validation** (`/edge`): strategy comparison + no-trade log.
8. Logs:
   - SQLite: `data/polyoracle.db` (tables `tradeauditrecord`, `walletaudit`, `notradedecision`, …).
   - Audit text log: `data/logs/audit.log`.
   - Backend stdout/stderr: `backend/backend.dev.log`, `backend/backend.dev.err.log`.
9. Real vs mock data: every market/wallet/trade response carries a `data_source` field — `polymarket_gamma`, `polymarket_clob_public`, `polymarket_data` (real APIs) or `mock` (fallback when offline / rate-limited / `POLYMARKET_PUBLIC_ENABLED=false`). The dashboard top banner aggregates the breakdown.
10. To stop everything cleanly: kill the script. To wipe state: `del data\polyoracle.db` and re-run.

## Running the audit bot

1. Start backend + frontend (see above).
2. Open `http://localhost:3000/control`.
3. Click **Mode → Paper** (or stay in **Research** to never open trades).
4. Click **Run once** to trigger a single audit cycle (or **Start loop** for continuous scheduling).
5. Open **Smart Wallets** → **Run audit batch (50)** to populate the wallet tier table.
6. Open **Trade Audit** → **Run trade audit** to ingest and score recent public trades.
7. Open **Signals** to see PAPER_TRADE / WATCH / REJECT decisions.
8. Open **Paper Trading** to see auto-opened paper positions and PnL.
9. Open **Edge Validation** to see strategy comparison and the no-trade decision log.

You can also drive everything via API:

```bash
curl -X POST http://localhost:8000/bot/mode/paper
curl -X POST http://localhost:8000/wallets/audit/run-batch -H "Content-Type: application/json" -d '{"limit":50}'
curl -X POST http://localhost:8000/trades/audit/run -H "Content-Type: application/json" -d '{}'
curl -X POST http://localhost:8000/bot/audit/run-once
curl http://localhost:8000/paper/performance
curl http://localhost:8000/edge/report
```

## Endpoints (v0.4 additions)

Markets (added in v0.4.1):

- `GET  /markets/tradable` — markets passing spread + liquidity filters.

Signals (added in v0.4.1):

- `GET  /signals/active` — non-rejected signals.
- `GET  /signals/decisions` — decision-code histogram.

Wallet discovery + win rate (added in v0.4.2):

- `POST /wallets/discovery/audit` — run a one-shot discovery audit (writes CSV + JSON exports, returns conclusion).
- `GET  /wallets/discovery/audit` — latest report.
- `GET  /wallets/discovery/export` — paths to CSV/JSON.
- `GET  /wallets/{address}/winrate` — strict resolved-market win rate for a wallet.
- `GET  /wallets/winrate/top` — wallets with reliable (MEDIUM/HIGH) confidence win rates, sorted desc.
- `GET  /wallets/winrate/summary` — aggregates from the latest audit.

Market-first discovery (added in v0.5):

- `POST /discovery/market-first/run` — run a fresh audit, write CSV + JSON exports.
- `GET  /discovery/market-first/run` — latest report (idempotent helper).
- `GET  /discovery/market-first/status` — last-run summary + export paths.
- `GET  /discovery/market-first/report` — full latest report.
- `GET  /discovery/market-first/wallets` — top wallets persisted from the latest run.
- `GET  /discovery/market-first/markets` — markets that fed the run.
- `GET  /discovery/market-first/rejected-markets` — only the rejected ones with reason code.
- `GET  /discovery/market-first/export` — export file paths.
- `GET  /wallets/market-first/top` — top wallets from the latest run.
- `GET  /wallets/market-first/{address}` — single-wallet record from the latest run.
- `GET  /wallets/{address}/category-winrate` — per-category win rate breakdown.

Candidate validation (added in v0.5.1):

- `POST /discovery/market-first/validate` — run a fresh validation pass with temporal split + anti-bias checks.
- `GET  /discovery/market-first/validate` — latest validation report.
- `GET  /discovery/market-first/validate/export` — CSV/JSON export paths.

Wallets:

- `GET  /wallets/top?limit=100`
- `GET  /wallets/audited`
- `GET  /wallets/watchlist`
- `POST /wallets/watchlist`
- `POST /wallets/blacklist`
- `GET  /wallets/{address}`
- `GET  /wallets/{address}/audit`
- `GET  /wallets/{address}/trades`
- `POST /wallets/audit/run`
- `POST /wallets/audit/run-batch`
- `GET  /wallets/audit/report`
- `GET  /wallets/stats`

Trades:

- `GET  /trades/recent`
- `GET  /trades/audited`
- `GET  /trades/clusters`
- `GET  /trades/large`
- `GET  /trades/smart-money`
- `POST /trades/audit/run`
- `GET  /trades/stats`

Paper trading:

- `GET  /paper/positions`
- `GET  /paper/trades`
- `POST /paper/reset`
- `POST /paper/close/{position_id}`
- `GET  /paper/report`
- `GET  /paper/performance`

Edge validation:

- `GET  /edge/report`
- `GET  /edge/metrics`
- `GET  /edge/strategies`
- `GET  /edge/wallets`
- `GET  /edge/categories`
- `GET  /edge/no-trade-log`

Bot loop:

- `GET  /bot/loop/status`
- `POST /bot/mode/research`
- `POST /bot/mode/paper`
- `POST /bot/mode/off`
- `POST /bot/audit/start`
- `POST /bot/audit/stop`
- `POST /bot/audit/run-once`
- `POST /bot/kill-switch`

## Public Polymarket Data

POLYORACLE uses free public endpoints first:

- Gamma API for active markets and market details.
- CLOB public API for orderbooks and midpoints.
- Data API for the public leaderboard, wallet positions and wallet activity (used by the SmartWalletAuditor and TradeAuditEngine).
- Mock fallback remains enabled by default.

No account, paid API key or wallet is required. If a public API fails or rate limits, POLYORACLE falls back to local mock data, marks records as `data_source=mock` and keeps the dashboard usable.

## Tests

```bash
cd polyoracle/backend
pytest
```

An extensive automated test suite (**800+ tests**) covers the conservative win-rate engine, wallet classification, the capital/risk allocator, the paper-trading engine, the discovery + out-of-sample validation pipeline, and the safety gates — kill switch, exposure caps and the permanent live-execution lock (e.g. a 100% win rate on a thin sample / MEDIUM confidence can never become ELITE; SAFE ≤ AGGRESSIVE ≤ FULL_PAPER is an enforced invariant).

Frontend build:

```bash
cd polyoracle/frontend
npm run build
```

## Live Readiness

Live remains out of scope until at least:

- 30 days of paper trading.
- 100 to 300 simulated trades.
- Positive expectancy after spread and slippage.
- Profit factor above 1.3.
- Reasonable drawdown.
- Filtered smart-money strategy must beat naive copy.
- Risk engine, kill switch and no-trade log validated.
- Compliance explicitly allows live, manual confirmation per order.
