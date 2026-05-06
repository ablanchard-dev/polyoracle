# POLYORACLE Roadmap

## Principle

POLYORACLE follows this sequence:

Observer -> Understand -> Simulate -> Measure -> Prove -> Execute.

Live trading is not a v0 priority. The system must prove that an edge survives copy delay, spread, slippage, liquidity limits and risk controls before live execution is considered.

## v0.2 Zero-Cost / Local-First (done)

- SQLite default storage.
- Local `data/` folder for snapshots, exports and logs.
- File exports for trades and signals.
- Mock data enabled by default.
- Postgres, Redis and Docker optional.
- Edge Validation skeleton.
- Settings and storage status endpoints.
- Tests running without Docker, keys or paid services.

## v0.3 Public Data Integration (done)

- Normalize Gamma API markets/events/tags/outcomes.
- Add CLOB public orderbook snapshots.
- Persist market snapshots locally.
- UI dashboard wiring.

## v0.4 Smart-Money Audit Bot (done)

- `SmartWalletAuditor`: discover top 50–100 wallets, audit each one with a 12-dimension SmartWalletScore (PnL, ROI, sample size, consistency, copyability, timing, exit quality, category specialization, liquidity quality, risk management, recent activity, confidence) and explicit penalties. Tiers: ELITE / STRONG / WATCH / WEAK / IGNORE / SUSPICIOUS / INSUFFICIENT_DATA. Auto watchlist + auto blacklist for suspicious profiles.
- `TradeAuditEngine`: ingest public trades, normalize, persist, classify decisions IGNORE / WATCH / SIGNAL / PAPER_TRADE; detect large trades, repeated accumulation, multi-wallet clusters and smart-money moves.
- `OrderbookAnalyzer`: best bid/ask, midpoint, spread, depth, imbalance, slippage estimation, fill price, quality (EXCELLENT / GOOD / ACCEPTABLE / BAD / UNTRADABLE).
- `CopyableEdgeEngine`: spread/slippage/copy-delay/late-entry/price-deterioration penalties, copyable edge classification (STRONG_EDGE / WEAK_EDGE / NO_EDGE / NEGATIVE_EDGE).
- `SignalEngine` extended with smart money signal types (SMART_WALLET_ENTRY, WHALE_ENTRY, SMART_CONSENSUS, REPEATED_ACCUMULATION, MULTI_WALLET_CLUSTER, EXIT_ALERT, LATE_ENTRY_RISK …) and SignalDecision (PAPER_TRADE / WATCH / REJECT / NEED_MORE_DATA).
- `PaperTradingEngine` extended with `maybe_auto_trade`, exposure tracking, daily/weekly PnL, take-profit / stop-loss / max-holding-time auto evaluation, manual close endpoint.
- `RiskEngine` extended with all v0.4 reason codes (LOW_SIGNAL_SCORE, LOW_CONFIDENCE, LOW_LIQUIDITY, WIDE_SPREAD, BAD_ORDERBOOK, NO_COPYABLE_EDGE, LATE_ENTRY, TOO_MUCH_EXPOSURE, DAILY_LOSS_LIMIT, WEEKLY_LOSS_LIMIT, KILL_SWITCH, INSUFFICIENT_DATA, WALLET_NOT_RELIABLE) and a persistent NoTradeDecision log.
- `BotLoop` orchestration: market scan → top wallet refresh → wallet audits → trade audits → cluster detection → signal generation → paper trade execution → position evaluation → status persistence. Modes OFF / RESEARCH / PAPER / SEMI_AUTO / LIVE (LIVE blocked).
- `EdgeValidationEngine` extended with strategy comparison (naive copy / filtered smart money / whale only / consensus / market opportunity), wallet/category breakdown and the no-trade decision log.
- New endpoints under `/wallets`, `/trades`, `/paper`, `/edge`, `/bot`.
- New UI pages: Smart Wallets, Trade Audit, Markets, Signals, Paper Trading, Edge Validation, Control Room.
- 35 backend tests passing.

## v0.4.1 Stability check (done)

- Idempotent audit and signal IDs (no growth across cycles).
- Paper-trade dedupe by `signal_id` and `(market, outcome, wallet)`.
- Real audit context fed to RiskEngine (no more 0–100 scores in USD slots).
- NoTradeDecision log captures every rejection with a documented reason code.
- `/markets/tradable`, `/signals/active`, `/signals/decisions` endpoints.
- Control-room UI surfaces per-cycle metrics + rejection breakdown + recent no-trade log.
- 5 smoke tests verifying endpoint coverage and dedupe.

## v0.4.2 Real-data discovery audit (done)

- `/leaderboard` is dead (404). Replaced with `/trades`-aggregated discovery (real `polymarket_data_recent_trades`).
- Strict no-silent-mock-fallback: when `mock_data_enabled=false` the discovery returns `data_source="unavailable"` rather than synthesising data.
- New `WinRateEngine`: groups wallet trades by (market, outcome), checks Gamma resolutions (`condition_id` query), counts only resolved markets where the wallet was net long on a known outcome. Confidence buckets: 0–9 INSUFFICIENT_DATA, 10–29 LOW, 30–99 MEDIUM, 100+ HIGH. Never invents a win or a loss.
- `win_rate_score` integrated into `SmartWalletScore` capped at 10%, weighted by confidence (LOW × 0.3, MEDIUM × 0.7, HIGH × 1.0). INSUFFICIENT_DATA contributes 0.
- `DiscoveryAuditService` runs the full pipeline, writes `data/exports/wallet_discovery_audit.json` and `wallet_discovery_top100.csv`, derives a `DISCOVERY_*` verdict.
- Empirical finding: live discovery is currently `DISCOVERY_VOLUME_BIASED` — recent trades come from currently-active 5-minute BTC up/down markets, so all 20 audited wallets returned `win_rate=None` (zero resolved markets in their window).
- 9 tests covering all of the above.

## v0.5 Market-first discovery (done)

- New `MarketResolutionScanner`: walks Gamma `closed=true` markets, validates every row, emits a fixed `rejection_reason` enum (`NO_CONDITION_ID`, `NOT_CLOSED`, `UNKNOWN_WINNING_OUTCOME`, `LOW_DATA_QUALITY`, …) so audits never silently drop markets.
- New `MarketFirstDiscoveryService`: for each usable market, fetches `/trades?market={conditionId}`, aggregates trades into per-wallet, per-outcome positions, and counts a *win* only when the wallet was net long the winning outcome at resolution. A wallet net-flat or net-short never moves the win rate.
- New `MarketFirstWalletScore` (0-100, capped) — 25% resolved win rate (gated by confidence: HIGH ×1.0, MEDIUM ×0.85, LOW ×0.4, INSUFFICIENT_DATA ×0), 20% sample size, 15% category specialization, 15% consistency, 10% recent activity, 10% liquidity quality, with explicit penalties for tiny sample, mostly-unresolved positions, suspicious high-win-rate small-sample profiles, and copying-too-large positions.
- Tier rules locked: ELITE/STRONG require sample ≥ `MARKET_FIRST_MIN_SAMPLE_FOR_STRONG` AND confidence ∈ {MEDIUM, HIGH}. INSUFFICIENT_DATA tier is reserved for sample below `MARKET_FIRST_MIN_SAMPLE_FOR_WATCH`.
- Composite score = 70% market-first + 20% recent activity + 10% copyability. Status tags: `STRONG_AND_ACTIVE`, `HISTORICAL_ELITE_INACTIVE`, `RECENTLY_ACTIVE_UNPROVEN`, `WATCH_ONLY`, `IGNORE`.
- `DataClient.fetch_market_trades(condition_id, limit, offset)` (paginated) and `GammaClient.fetch_closed_markets(...)` added.
- Endpoints under `/discovery/market-first/*` (status/run/report/wallets/markets/rejected-markets/export) and `/wallets/market-first/*` (top, by-address) and `/wallets/{address}/category-winrate`.
- Persistent SQLite tables `MarketFirstWalletRecord` and `ResolvedMarketRecord` so the latest run survives across restarts.
- 4 exports: `data/exports/market_first_discovery_report.json`, `market_first_wallets.csv`, `market_first_markets.csv`, `market_first_rejected_markets.csv`.
- UI: a "Market-first discovery (v0.5)" block at the top of `/wallets` with conclusion, tier breakdown, top wallets table (Tier / Status / Score / Composite / Win rate / Sample / Confidence / Best category / Recent). Old v0.4.2 audit kept below as overlay.
- Empirical first run (180 days, 80 markets): 3930 wallets discovered, 0 ELITE/STRONG, 23 WEAK candidates with promising but LOW-confidence win rates; conclusion `MARKET_FIRST_NEEDS_MORE_MARKETS` — exactly what the bot should say.
- 8 new tests, 57 backend tests in total.

## v0.5.1 Candidate validation (done)

Confirms the v0.5 short-list is real, not a sampling artefact:

- New `MarketFirstDiscoveryService.run_with_temporal_split(...)` — splits the usable resolved markets into oldest-70% (discovery) and newest-30% (validation) by ``end_date``, runs aggregation three times (full / discovery / validation) without re-fetching trades.
- New `CandidateValidationService` — re-runs the market-first pipeline at a wider scale (default 365 days / 300 markets), cross-references the result with the previous v0.5 run snapshot, and produces a `candidate_status` per wallet from a fixed enum:
  * `ELITE` — sample ≥ 100 + HIGH confidence + win rate ≥ 0.65 in BOTH partitions + no bias warnings.
  * `STRONG` — sample ≥ 30 + MEDIUM/HIGH confidence + |discovery − validation| ≤ 0.20 + validation ≥ 0.55.
  * `CANDIDATE_ELITE` — high win rate but sample still 10–29; promising, not validated.
  * `BIASED_SAMPLE` — single-event-slug correlation ≥ 80% on ≥ 5 winning markets, OR single-category concentration ≥ 95% (when the category is meaningful).
  * `FAILED_VALIDATION` — discovery win rate ≥ 0.70, validation win rate < 0.50.
  * `DROPPED` — sample big enough to judge, win rate gone.
- Anti-bias sanity helpers: `detect_short_window_crypto_dominance` (5-min BTC up/down loops), `detect_loser_presence` (survivor bias check), `detect_category_concentration` (skips when category is `Uncategorised`), `detect_correlated_markets` (collapses slugs to first 4 hyphen-tokens so e.g. `espresso-fdv-above-100m-…`, `…-200m-…`, `…-700m-…` group as one event).
- Per-(wallet, market) dedupe locked in: a wallet trading the same market 10 times still counts as 1 resolved entry.
- Endpoints `POST/GET /discovery/market-first/validate` + `GET /discovery/market-first/validate/export`.
- Exports `candidate_elite_validation_report.json`, `candidate_elite_wallets.csv`, plus `market_first_discovery_report_previous.json` snapshot.
- UI: a "Candidate validation (v0.5.1)" block in `/wallets` shows status histogram, the validated wallets table (Address / Status / Previous / Expanded / Discovery / Validation / Recommendation), and the bias flags from the run.
- Empirical 365-day / 300-market run: **5 of the 23 v0.5 candidates promoted to STRONG**, 2 brand-new STRONG wallets emerged in the expansion (7 STRONG total), 14 stay CANDIDATE_ELITE, 4 DROPPED. Zero ELITE because no wallet has reached ≥ 100 resolved markets yet — bot stays strict.
- 6 new tests, 63 backend tests in total.

## v0.5.2 Risk modes + strict ELITE (done)

- New `services/risk_mode.py` registry: three frozen `RiskModeProfile` instances (SAFE, AGGRESSIVE, FULL_PAPER) with allowed wallet statuses, sample/confidence gates, four exposure caps (per-trade, per-wallet, per-market, total), open-positions cap, daily-trades cap, and a hard `live_allowed=False` on every profile.
- New `services/risk_mode_service.py` + `RiskModeState` SQLModel for the persisted active mode (default `SAFE`, configurable via `.env` `RISK_MODE` and `/risk/mode` endpoints).
- `RiskEngine.validate_for_mode(...)` consumes the active profile and walks: kill switch → status → sample → confidence → open-positions cap → daily-trades cap → wallet/total/market exposure → spread / liquidity / orderbook / copyable-edge / signal-score / late-entry, with a `details` payload written into the no-trade decision log.
- `PaperTradingEngine.maybe_auto_trade(profile=None)` — falls back to `get_active_profile(session)` when none is passed; new helpers `wallet_exposure(addr)`, `daily_trade_count()`, `lookup_candidate_status(addr)`, `lookup_wallet_record(addr)`.
- `CandidateValidationService` now writes the validated `candidate_status` back onto `MarketFirstWalletRecord.candidate_status`, so the risk gate has a single SQLite source of truth.
- BotLoop's `_build_audit_context` enriches the context with `candidate_status`, `win_rate_confidence`, `market_sample_size` from the wallet record so the profile can be applied without an extra lookup.
- New `MarketFirstDiscoveryService.reclassify_existing_records()` — re-runs `_classify_tier` over all persisted records and rewrites the JSON+CSV exports under the active suffix. Used to apply the strict ELITE rule without re-running the 11-min discovery.
- Strict ELITE rule: `_classify_tier` now requires `confidence == "HIGH"` (= sample ≥ 100 by construction) for ELITE. MEDIUM-confidence wallets cap out at STRONG.
- `EdgeValidationEngine.compare_strategies` adds `risk_mode_safe`, `risk_mode_aggressive`, `risk_mode_full_paper` projections that walk the persisted audits through each profile filter and report what would have been accepted.
- New endpoints `GET/POST /risk/mode`, `POST /risk/mode/{name}`, `GET /risk/mode/limits`, `GET /risk/profiles`.
- New UI: `RiskModePanel` (radio selector, current limits, FULL_PAPER warning banner) wired into `/control`.
- 13 new risk-mode tests + 1 strict-ELITE test → 77 backend tests in total. Frontend build green.
- Empirical 730d / 1000m re-classification: 22 ELITE + 30 STRONG (after dropping 1 MEDIUM-confidence outlier from ELITE → STRONG). OOS validation on the same sample (700/300 split): 5 of 22 strict-ELITE and 23 of 30 strict-STRONG kept ELITE/STRONG status, 0 FAILED_VALIDATION, 0 BIASED_SAMPLE, avg gap 4.2%. Live mode comparison invariant SAFE ≤ AGGRESSIVE ≤ FULL_PAPER → 5 ≤ 15 ≤ 41.

## v0.5.3 Strict ELITE rule + OUTLIER_FLAGGED (done)

- `CandidateValidationService._derive_status` now requires `validation_win_rate ≥ 0.70` AND `|discovery_win_rate − validation_win_rate| ≤ 0.15` for ELITE, on top of the v0.5.2 sample/confidence gates. A wallet that meets the sample/confidence gates but fails the gap or validation-WR threshold lands in the new `OUTLIER_FLAGGED` status — it is **not** allowed in SAFE / AGGRESSIVE / FULL_PAPER.
- Two new tests pinning the rule (`test_outlier_flagged_when_validation_gap_exceeds_15pct`, `test_outlier_flagged_when_validation_wr_below_70pct`).
- `ValidationReport` carries an `outlier_flagged` count and the recommendation text dedicated to that status.

## v0.5.4 ValidatedPaperUniverse (done)

- New `services/validated_paper_universe.py` — merges N validation reports (e.g. 730d + 1095d) into a single ranked CSV (`validated_paper_universe_latest.csv`) plus JSON snapshot, with per-row `allowed_safe / allowed_aggressive / allowed_full_paper` flags.
- Inclusion: ELITE (sample ≥ 100, validation_sample ≥ 10) and STRONG (sample ≥ 30, validation_sample ≥ 5) only.
- Exclusion: OUTLIER_FLAGGED, BIASED_SAMPLE, FAILED_VALIDATION, CANDIDATE_ELITE, DROPPED, INSUFFICIENT_DATA.
- Belt-and-braces: the merge re-applies the strict gap ≤ 0.15 / val_wr ≥ 0.70 gate at merge time so legacy reports predating `OUTLIER_FLAGGED` get demoted automatically (caught a real production case: `0xa66790e2…` was tagged ELITE in the 730d report and would have leaked into AGGRESSIVE/FULL_PAPER without this filter).
- `allowed_safe = True` only when `tier == "ELITE"` AND `gap ≤ 0.10` AND `val_wr ≥ 0.80`. `allowed_aggressive` and `allowed_full_paper = True` for every retained ELITE/STRONG.
- Same wallet seen in multiple runs collapses to a single row carrying the combined `discovery_run_source` (e.g. `1095d+730d`) and the strongest tier across runs.
- Endpoints `GET /discovery/universe/latest` + `POST /discovery/universe/merge`.
- Frontend `UniversePanel` component at the top of `/wallets` with live counts, an "Awaiting operator signal" banner ("Paper 7d run not started"), and a rebuild button.
- 4 new tests including a regression test for the legacy ELITE failing-strict-gap case.
- Empirical run on 730d + 1095d:
  - **216 wallets** in the merged universe (52 ELITE + 164 STRONG, 1 demoted from legacy 730d).
  - **50 allowed in SAFE**, 216 allowed in AGGRESSIVE / FULL_PAPER.
  - **5 OUTLIER_FLAGGED, 161 BIASED_SAMPLE, 2 FAILED_VALIDATION, 2 862 CANDIDATE_ELITE / sample-thin** explicitly excluded.

## v0.6 Cockpit & Telemetry (next)

Discovery first, then cockpit polish:

- **Market-first discovery (Alternative A)**: walk recent *resolved* Gamma markets, fetch `/holders` per market, audit those wallets. This is the path that should produce a measurable win rate.
- **Niche-market discovery (Alternative B)**: pick mid-volume markets where inefficiency is plausible and audit consistent winners on them.
- **Cluster discovery (Alternative C)**: detect wallets entering the same market within a short window — flag coordinated groups.
- **ROI/sample discovery (Alternative D)**: rank wallets by ROI on resolved markets, with sample-size + drawdown sanity checks; win rate stays a sub-metric.

Plus the routine cockpit work that v0.4.1 deferred:

- Wallet detail page with score breakdown, daily stats and trade timeline.
- Local WebSocket for live updates of audits / signals / paper trades.
- Persistent settings (mode, thresholds) writable from `/settings`.
- Wallet copy-cohort comparator (compare 3 wallets in one view).
- Time-series charts for paper PnL, exposure and signal volume.
- Export buttons (CSV / JSON) for audited trades and signals.
- Background scheduler for `BotLoop.run_once` at `AUDIT_INTERVAL_SECONDS` cadence.

## Live Readiness Criteria

Live remains blocked unless all are true:

- At least 30 days paper trading.
- 100 to 300 simulated trades.
- Positive expectancy after spread and slippage.
- Profit factor above 1.3.
- Reasonable drawdown.
- Filtered smart-money strategy beats naive copy in `/edge/strategies`.
- Risk engine, no-trade log and kill switch validated.
- Compliance explicitly allows live; user manually confirms every live activation.
