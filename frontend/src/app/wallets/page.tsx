"use client";

import { useEffect, useState } from "react";
import { UniversePanel } from "@/components/UniversePanel";
import {
  CandidateRow,
  DiscoveryAuditReport,
  MarketFirstReport,
  MarketFirstWallet,
  TopWallet,
  ValidationReport,
  WalletAudit,
  getAuditedWallets,
  getDiscoveryAuditLatest,
  getMarketFirstReport,
  getMarketFirstTopWallets,
  getTopWallets,
  getValidationLatest,
  postWalletBlacklist,
  postWalletWatch,
  runDiscoveryAudit,
  runMarketFirstDiscovery,
  runValidation,
  runWalletAuditBatch
} from "@/lib/api";

const tierColor: Record<string, string> = {
  ELITE: "text-accent",
  STRONG: "text-emerald-300",
  WATCH: "text-sky-300",
  WEAK: "text-amber-300",
  IGNORE: "text-slate-400",
  SUSPICIOUS: "text-danger",
  INSUFFICIENT_DATA: "text-slate-500"
};

const conclusionTone: Record<string, string> = {
  DISCOVERY_GOOD: "text-accent",
  DISCOVERY_VOLUME_BIASED: "text-amber-300",
  DISCOVERY_TOO_MUCH_MOCK: "text-warning",
  DISCOVERY_INSUFFICIENT_DATA: "text-slate-400",
  DISCOVERY_API_UNRELIABLE: "text-danger",
  DISCOVERY_NEEDS_ALTERNATIVE_METHOD: "text-amber-300",
  MARKET_FIRST_GOOD: "text-accent",
  MARKET_FIRST_DISCOVERY_READY: "text-emerald-300",
  MARKET_FIRST_NEEDS_MORE_MARKETS: "text-amber-300",
  MARKET_FIRST_NO_ELITE_FOUND: "text-amber-300",
  MARKET_FIRST_API_LIMITED: "text-warning",
  MARKET_FIRST_INSUFFICIENT_DATA: "text-slate-400"
};

const statusTone: Record<string, string> = {
  STRONG_AND_ACTIVE: "text-accent",
  HISTORICAL_ELITE_ACTIVE: "text-accent",
  HISTORICAL_ELITE_INACTIVE: "text-amber-300",
  RECENTLY_ACTIVE_UNPROVEN: "text-sky-300",
  WATCH_ONLY: "text-slate-300",
  IGNORE: "text-slate-500"
};

const winRateConfidenceTone: Record<string, string> = {
  HIGH: "text-accent",
  MEDIUM: "text-emerald-300",
  LOW: "text-amber-300",
  INSUFFICIENT_DATA: "text-slate-500"
};

const money = new Intl.NumberFormat("en-US", { style: "currency", currency: "USD", maximumFractionDigits: 0 });

type DiscoveryReport = DiscoveryAuditReport | { conclusion: string; rationale?: string; csv_path: string; json_path: string };

function isFullReport(report: DiscoveryReport | null): report is DiscoveryAuditReport {
  return !!report && "audited_wallets_count" in report;
}

type MarketFirstView = MarketFirstReport | { available: false; message: string; exports: Record<string, unknown> };

function isMarketFirstReport(report: MarketFirstView | null): report is MarketFirstReport {
  return !!report && "wallets_discovered" in report;
}

type ValidationView = ValidationReport | { available: false; message: string; exports: Record<string, unknown> };

function isValidationReport(report: ValidationView | null): report is ValidationReport {
  return !!report && "rows" in report;
}

const candidateStatusTone: Record<string, string> = {
  ELITE: "text-accent",
  STRONG: "text-emerald-300",
  CANDIDATE_ELITE: "text-sky-300",
  BIASED_SAMPLE: "text-warning",
  FAILED_VALIDATION: "text-danger",
  DROPPED: "text-slate-500"
};

export default function WalletsPage() {
  const [top, setTop] = useState<TopWallet[]>([]);
  const [audited, setAudited] = useState<WalletAudit[]>([]);
  const [discovery, setDiscovery] = useState<DiscoveryReport | null>(null);
  const [marketFirst, setMarketFirst] = useState<MarketFirstView | null>(null);
  const [marketFirstWallets, setMarketFirstWallets] = useState<MarketFirstWallet[]>([]);
  const [validation, setValidation] = useState<ValidationView | null>(null);
  const [busy, setBusy] = useState(false);
  const [discoveryBusy, setDiscoveryBusy] = useState(false);
  const [marketFirstBusy, setMarketFirstBusy] = useState(false);
  const [validationBusy, setValidationBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function load() {
    try {
      const [topRows, auditedRows, discoveryRow, mfReport, mfWallets, valReport] = await Promise.all([
        getTopWallets(100),
        getAuditedWallets(100),
        getDiscoveryAuditLatest(),
        getMarketFirstReport(),
        getMarketFirstTopWallets(50),
        getValidationLatest()
      ]);
      setTop(topRows);
      setAudited(auditedRows);
      setDiscovery(discoveryRow);
      setMarketFirst(mfReport);
      setMarketFirstWallets(mfWallets);
      setValidation(valReport);
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load wallets");
    }
  }

  useEffect(() => {
    load();
  }, []);

  async function runAudit() {
    setBusy(true);
    try {
      await runWalletAuditBatch(50);
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Audit failed");
    } finally {
      setBusy(false);
    }
  }

  async function runDiscovery() {
    setDiscoveryBusy(true);
    try {
      const report = await runDiscoveryAudit(100, 25);
      setDiscovery(report);
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Discovery audit failed");
    } finally {
      setDiscoveryBusy(false);
    }
  }

  async function runMarketFirst() {
    setMarketFirstBusy(true);
    try {
      const report = await runMarketFirstDiscovery(90, 60, 1000);
      setMarketFirst(report);
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Market-first discovery failed");
    } finally {
      setMarketFirstBusy(false);
    }
  }

  async function runCandidateValidation() {
    setValidationBusy(true);
    try {
      const report = await runValidation(365, 300, 0.7);
      setValidation(report);
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Candidate validation failed");
    } finally {
      setValidationBusy(false);
    }
  }

  async function watch(address: string) {
    await postWalletWatch(address, "manual");
  }

  async function blacklist(address: string) {
    await postWalletBlacklist(address, "manual");
  }

  const auditedWalletsLookup: Record<string, Record<string, unknown>> = {};
  if (isFullReport(discovery) && discovery.audited_wallets) {
    for (const row of discovery.audited_wallets) {
      const addr = String(row.address ?? "");
      if (addr) {
        auditedWalletsLookup[addr] = row;
      }
    }
  }

  return (
    <main className="mx-auto max-w-7xl px-5 py-6">
      <header className="mb-6 flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-semibold">Smart Wallets</h1>
          <p className="text-sm text-slate-400">Top 100 wallets discovered + audited. Discovery uses /trades (real) with mock fallback.</p>
        </div>
        <button
          onClick={runAudit}
          disabled={busy}
          className="rounded border border-accent bg-accent/15 px-4 py-2 text-sm font-medium text-accent transition hover:bg-accent/25 disabled:opacity-60"
        >
          {busy ? "Auditing..." : "Run audit batch (50)"}
        </button>
      </header>

      {error ? <div className="mb-4 rounded border border-danger bg-danger/10 p-3 text-sm text-red-100">{error}</div> : null}

      <section className="mb-6">
        <UniversePanel />
      </section>

      <section className="mb-6 rounded border border-accent bg-panel p-4">
        <div className="mb-3 flex flex-wrap items-center justify-between gap-3">
          <div>
            <h2 className="text-lg font-semibold">Market-first discovery (v0.5)</h2>
            <p className="text-xs text-slate-400">
              Walk recent resolved Polymarket markets, audit who actually held the winning outcome, score on resolved-market win rate.
            </p>
          </div>
          <button
            onClick={runMarketFirst}
            disabled={marketFirstBusy}
            className="rounded border border-accent bg-accent/15 px-4 py-2 text-sm font-medium text-accent transition hover:bg-accent/25 disabled:opacity-60"
          >
            {marketFirstBusy ? "Running..." : "Run market-first discovery"}
          </button>
        </div>
        {!marketFirst ? (
          <div className="text-sm text-slate-500">No market-first run yet.</div>
        ) : isMarketFirstReport(marketFirst) ? (
          <>
            <div className="grid gap-4 md:grid-cols-2 lg:grid-cols-4">
              <DiscoveryStat label="Conclusion" value={marketFirst.conclusion} tone={conclusionTone[marketFirst.conclusion] ?? "text-slate-200"} />
              <DiscoveryStat label="Markets scanned" value={`${marketFirst.markets_scanned}`} />
              <DiscoveryStat label="Markets usable" value={`${marketFirst.markets_usable}`} />
              <DiscoveryStat label="Markets rejected" value={`${marketFirst.markets_rejected}`} tone="text-warning" />
              <DiscoveryStat label="Wallets discovered" value={`${marketFirst.wallets_discovered}`} />
              <DiscoveryStat label="Reliable WR (MEDIUM/HIGH)" value={`${marketFirst.wallets_with_medium_high_confidence}`} tone="text-accent" />
              <DiscoveryStat
                label="ELITE / STRONG / WATCH"
                value={`${marketFirst.tier_breakdown?.ELITE ?? 0} / ${marketFirst.tier_breakdown?.STRONG ?? 0} / ${marketFirst.tier_breakdown?.WATCH ?? 0}`}
                tone="text-accent"
              />
              <DiscoveryStat
                label="Avg / median win rate"
                value={`${marketFirst.average_win_rate != null ? (marketFirst.average_win_rate * 100).toFixed(1) + "%" : "—"} / ${marketFirst.median_win_rate != null ? (marketFirst.median_win_rate * 100).toFixed(1) + "%" : "—"}`}
              />
            </div>
            {marketFirst.rationale ? <p className="mt-3 text-sm text-slate-300">{marketFirst.rationale}</p> : null}
            {marketFirst.warnings.length > 0 ? (
              <ul className="mt-3 space-y-1 text-xs text-amber-200">
                {marketFirst.warnings.map((w) => (<li key={w}>· {w}</li>))}
              </ul>
            ) : null}
            {Object.keys(marketFirst.rejection_reasons || {}).length > 0 ? (
              <div className="mt-3 text-xs text-slate-400">
                Rejected reasons:&nbsp;
                {Object.entries(marketFirst.rejection_reasons).map(([code, count]) => (
                  <span key={code} className="mr-3 font-mono">{code}={count}</span>
                ))}
              </div>
            ) : null}
            <div className="mt-3 text-xs text-slate-500">
              Exports: <span className="font-mono">{marketFirst.csv_paths.wallets}</span> ·{" "}
              <span className="font-mono">{marketFirst.csv_paths.markets}</span> ·{" "}
              <span className="font-mono">{marketFirst.json_path}</span>
            </div>
          </>
        ) : (
          <div className="text-sm text-slate-500">{marketFirst.message}</div>
        )}
      </section>

      <section className="mb-6 rounded border border-emerald-500/40 bg-panel p-4">
        <div className="mb-3 flex flex-wrap items-center justify-between gap-3">
          <div>
            <h2 className="text-lg font-semibold">Candidate validation (v0.5.1)</h2>
            <p className="text-xs text-slate-400">
              Confirm v0.5 candidates on a larger sample (365 days / 300 markets) and a 70/30 temporal out-of-sample split. Anti-bias filters: survivor bias, single-event slug correlation, single-category concentration, 5-minute crypto dominance.
            </p>
          </div>
          <button
            onClick={runCandidateValidation}
            disabled={validationBusy}
            className="rounded border border-emerald-400 bg-emerald-400/15 px-4 py-2 text-sm font-medium text-emerald-200 transition hover:bg-emerald-400/25 disabled:opacity-60"
          >
            {validationBusy ? "Running..." : "Run candidate validation"}
          </button>
        </div>
        {!validation ? (
          <div className="text-sm text-slate-500">No validation run yet.</div>
        ) : isValidationReport(validation) ? (
          <>
            <div className="grid gap-4 md:grid-cols-2 lg:grid-cols-4">
              <DiscoveryStat label="Markets usable" value={`${validation.markets_usable}`} />
              <DiscoveryStat label="Discovery / validation split" value={`${validation.discovery_markets} / ${validation.validation_markets}`} />
              <DiscoveryStat label="Wallets evaluated" value={`${validation.wallets_in_full}`} />
              <DiscoveryStat label="v0.5 candidates kept" value={`${validation.expanded_kept} / ${validation.previous_candidates}`} />
              <DiscoveryStat label="ELITE / STRONG" value={`${validation.elite} / ${validation.strong}`} tone="text-accent" />
              <DiscoveryStat label="CANDIDATE_ELITE" value={`${validation.candidate_elite}`} tone="text-sky-300" />
              <DiscoveryStat label="BIASED_SAMPLE" value={`${validation.biased_sample}`} tone="text-warning" />
              <DiscoveryStat label="FAILED_VALIDATION" value={`${validation.failed_validation}`} tone="text-danger" />
            </div>
            <div className="mt-3 grid gap-2 text-xs text-slate-400 md:grid-cols-3">
              <div>Survivor bias: {validation.survivor_bias_warning ? "FLAG" : "OK"} ({(validation.survivor_bias_loser_market_share * 100).toFixed(0)}% markets with losers)</div>
              <div>5-min crypto dominance: {validation.short_window_crypto_dominance ? "FLAG" : "OK"} ({(validation.short_window_crypto_share * 100).toFixed(1)}%)</div>
              <div>API errors: {validation.api_errors}</div>
            </div>
            {validation.top_validated.length > 0 ? (
              <div className="mt-4 overflow-x-auto rounded border border-line bg-slate-900 p-3">
                <h3 className="mb-2 text-sm font-semibold text-emerald-200">Validated wallets (out-of-sample)</h3>
                <table className="w-full min-w-[1100px] text-left text-xs">
                  <thead className="text-[10px] uppercase text-slate-400">
                    <tr>
                      <th className="pb-2">Address</th>
                      <th className="pb-2">Status</th>
                      <th className="pb-2">Previous</th>
                      <th className="pb-2">Expanded</th>
                      <th className="pb-2">Discovery</th>
                      <th className="pb-2">Validation</th>
                      <th className="pb-2">Recommendation</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-line">
                    {validation.top_validated.map((row) => (
                      <tr key={row.address}>
                        <td className="py-2 pr-3 font-mono text-[11px] text-slate-200">{row.address}</td>
                        <td className={`py-2 font-semibold uppercase ${candidateStatusTone[row.candidate_status] ?? "text-slate-300"}`}>{row.candidate_status}</td>
                        <td className="py-2">{row.previous_sample ?? "—"}m@{row.previous_win_rate != null ? `${(row.previous_win_rate * 100).toFixed(0)}%` : "—"}</td>
                        <td className="py-2">{row.expanded_sample}m@{row.expanded_win_rate != null ? `${(row.expanded_win_rate * 100).toFixed(0)}%` : "—"}</td>
                        <td className="py-2">{row.discovery_sample}m@{row.discovery_win_rate != null ? `${(row.discovery_win_rate * 100).toFixed(0)}%` : "—"}</td>
                        <td className="py-2 font-semibold">{row.validation_sample}m@{row.validation_win_rate != null ? `${(row.validation_win_rate * 100).toFixed(0)}%` : "—"}</td>
                        <td className="py-2 text-slate-300">{row.final_recommendation}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            ) : null}
            <div className="mt-3 text-xs text-slate-500">
              Exports: <span className="font-mono">{validation.csv_path}</span> · <span className="font-mono">{validation.json_path}</span>
            </div>
          </>
        ) : (
          <div className="text-sm text-slate-500">{validation.message}</div>
        )}
      </section>

      {marketFirstWallets.length > 0 ? (
        <section className="mb-6 rounded border border-line bg-panel p-4">
          <h2 className="mb-3 text-lg font-semibold">Market-first top wallets</h2>
          <div className="overflow-x-auto">
            <table className="w-full min-w-[1100px] text-left text-sm">
              <thead className="text-xs uppercase text-slate-400">
                <tr>
                  <th className="pb-3">Address</th>
                  <th className="pb-3">Tier</th>
                  <th className="pb-3">Status</th>
                  <th className="pb-3">Score</th>
                  <th className="pb-3">Composite</th>
                  <th className="pb-3">Win rate</th>
                  <th className="pb-3">Sample</th>
                  <th className="pb-3">Confidence</th>
                  <th className="pb-3">Best category</th>
                  <th className="pb-3">Recent</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-line">
                {marketFirstWallets.map((wallet) => (
                  <tr key={wallet.address}>
                    <td className="py-3 pr-4 font-mono text-xs text-slate-200">{wallet.address}</td>
                    <td className={`py-3 font-semibold ${tierColor[wallet.tier] ?? "text-slate-300"}`}>{wallet.tier}</td>
                    <td className={`py-3 text-xs uppercase ${statusTone[wallet.status] ?? "text-slate-400"}`}>{wallet.status}</td>
                    <td className="py-3">{wallet.market_first_score.toFixed(1)}</td>
                    <td className="py-3 font-semibold">{wallet.composite_score.toFixed(1)}</td>
                    <td className="py-3">{wallet.resolved_market_win_rate != null ? `${(wallet.resolved_market_win_rate * 100).toFixed(0)}%` : "—"}</td>
                    <td className="py-3">{wallet.resolved_markets_traded}</td>
                    <td className={`py-3 text-xs uppercase ${winRateConfidenceTone[wallet.win_rate_confidence] ?? "text-slate-500"}`}>{wallet.win_rate_confidence}</td>
                    <td className="py-3 text-xs text-slate-300">{wallet.best_category ?? "—"}</td>
                    <td className="py-3">{wallet.recent_activity_score.toFixed(0)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>
      ) : null}

      <section className="mb-6 rounded border border-line bg-panel p-4">
        <div className="mb-3 flex items-center justify-between gap-3">
          <div>
            <h2 className="text-lg font-semibold">Discovery Audit (v0.4.2 — recent /trades)</h2>
            <p className="text-xs text-slate-400">Volume-biased fallback path. Kept as overlay only.</p>
          </div>
          <button
            onClick={runDiscovery}
            disabled={discoveryBusy}
            className="rounded border border-line bg-slate-800 px-4 py-2 text-sm transition hover:bg-slate-700 disabled:opacity-60"
          >
            {discoveryBusy ? "Running..." : "Run discovery audit (100)"}
          </button>
        </div>
        {!discovery ? (
          <div className="text-sm text-slate-500">No discovery audit run yet.</div>
        ) : (
          <div className="grid gap-4 md:grid-cols-2 lg:grid-cols-4">
            <DiscoveryStat
              label="Conclusion"
              value={discovery.conclusion}
              tone={conclusionTone[discovery.conclusion] ?? "text-slate-200"}
            />
            {isFullReport(discovery) ? (
              <>
                <DiscoveryStat label="Data source" value={discovery.data_source ?? "—"} />
                <DiscoveryStat label="Top discovered" value={`${discovery.top_wallets_count}`} />
                <DiscoveryStat label="Audited" value={`${discovery.audited_wallets_count}`} />
                <DiscoveryStat
                  label="ELITE / STRONG"
                  value={`${discovery.tier_breakdown?.ELITE ?? 0} / ${discovery.tier_breakdown?.STRONG ?? 0}`}
                  tone="text-accent"
                />
                <DiscoveryStat
                  label="Suspicious / Insufficient"
                  value={`${discovery.suspicious_count} / ${discovery.insufficient_data_count}`}
                  tone="text-warning"
                />
                <DiscoveryStat
                  label="Avg / median win rate"
                  value={`${discovery.average_win_rate != null ? (discovery.average_win_rate * 100).toFixed(1) + "%" : "—"} / ${discovery.median_win_rate != null ? (discovery.median_win_rate * 100).toFixed(1) + "%" : "—"}`}
                />
                <DiscoveryStat
                  label="Reliable WR / Insufficient WR"
                  value={`${discovery.wallets_with_reliable_win_rate_count} / ${discovery.wallets_with_insufficient_win_rate_count}`}
                />
              </>
            ) : null}
          </div>
        )}
        {discovery && "rationale" in discovery && discovery.rationale ? (
          <p className="mt-3 text-sm text-slate-300">{discovery.rationale}</p>
        ) : null}
        {isFullReport(discovery) && discovery.warnings.length > 0 ? (
          <ul className="mt-3 space-y-1 text-xs text-amber-200">
            {discovery.warnings.map((w) => (
              <li key={w}>· {w}</li>
            ))}
          </ul>
        ) : null}
        {discovery && "csv_path" in discovery ? (
          <div className="mt-3 text-xs text-slate-500">
            Exports: <span className="font-mono">{discovery.csv_path}</span> · <span className="font-mono">{discovery.json_path}</span>
          </div>
        ) : null}
      </section>

      <section className="rounded border border-line bg-panel p-4">
        <h2 className="mb-3 text-lg font-semibold">Audited</h2>
        <div className="overflow-x-auto">
          <table className="w-full min-w-[1200px] text-left text-sm">
            <thead className="text-xs uppercase text-slate-400">
              <tr>
                <th className="pb-3">Address</th>
                <th className="pb-3">Tier</th>
                <th className="pb-3">Score</th>
                <th className="pb-3">PnL</th>
                <th className="pb-3">ROI</th>
                <th className="pb-3">Win rate</th>
                <th className="pb-3">Sample</th>
                <th className="pb-3">Confidence</th>
                <th className="pb-3">Unresolved</th>
                <th className="pb-3">Trades</th>
                <th className="pb-3">Copyable</th>
                <th className="pb-3">Source</th>
                <th className="pb-3">Actions</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-line">
              {audited.length === 0 ? (
                <tr>
                  <td colSpan={13} className="py-6 text-center text-slate-500">
                    No audits yet. Click "Run audit batch" or "Run discovery audit" above.
                  </td>
                </tr>
              ) : null}
              {audited.map((wallet) => {
                const wrEntry = auditedWalletsLookup[wallet.address] || {};
                const winRate = (wrEntry.resolved_market_win_rate as number | null) ?? null;
                const winSample = (wrEntry.market_sample_size as number) ?? 0;
                const winConf = (wrEntry.win_rate_confidence as string) ?? "INSUFFICIENT_DATA";
                const unresolved = (wrEntry.unresolved_markets_count as number) ?? 0;
                return (
                  <tr key={wallet.address}>
                    <td className="py-3 pr-4 font-mono text-xs text-slate-200">{wallet.address}</td>
                    <td className={`py-3 font-semibold ${tierColor[wallet.tier] ?? "text-slate-300"}`}>{wallet.tier}</td>
                    <td className="py-3">{wallet.smart_score.toFixed(1)}</td>
                    <td className="py-3">{money.format(wallet.pnl)}</td>
                    <td className="py-3">{(wallet.roi * 100).toFixed(1)}%</td>
                    <td className="py-3">{winRate != null ? `${(winRate * 100).toFixed(0)}%` : "—"}</td>
                    <td className="py-3">{winSample}</td>
                    <td className={`py-3 text-xs uppercase ${winRateConfidenceTone[winConf] ?? "text-slate-500"}`}>{winConf}</td>
                    <td className="py-3">{unresolved}</td>
                    <td className="py-3">{wallet.sample_size}</td>
                    <td className="py-3">{wallet.copyability.toFixed(0)}</td>
                    <td className="py-3 text-xs uppercase text-slate-500">{wallet.data_source}</td>
                    <td className="py-3">
                      <div className="flex gap-2">
                        <button onClick={() => watch(wallet.address)} className="rounded border border-line px-2 py-1 text-xs text-accent hover:bg-slate-800">
                          Watch
                        </button>
                        <button onClick={() => blacklist(wallet.address)} className="rounded border border-danger px-2 py-1 text-xs text-red-200 hover:bg-danger/20">
                          Block
                        </button>
                      </div>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </section>

      <section className="mt-6 rounded border border-line bg-panel p-4">
        <h2 className="mb-3 text-lg font-semibold">Top wallets discovered ({top.length})</h2>
        <div className="overflow-x-auto">
          <table className="w-full min-w-[720px] text-left text-sm">
            <thead className="text-xs uppercase text-slate-400">
              <tr>
                <th className="pb-3">Address</th>
                <th className="pb-3">Volume estimated</th>
                <th className="pb-3">Markets</th>
                <th className="pb-3">Trades observed</th>
                <th className="pb-3">Source</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-line">
              {top.map((wallet) => (
                <tr key={wallet.address}>
                  <td className="py-3 pr-4 font-mono text-xs text-slate-200">{wallet.address}</td>
                  <td className="py-3">{money.format(wallet.volume)}</td>
                  <td className="py-3">{wallet.market_count}</td>
                  <td className="py-3">{(wallet as TopWallet & { trades_observed?: number }).trades_observed ?? "—"}</td>
                  <td className="py-3 text-xs uppercase text-slate-500">{wallet.data_source}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </section>
    </main>
  );
}

function DiscoveryStat({ label, value, tone }: { label: string; value: string; tone?: string }) {
  return (
    <div className="rounded border border-line bg-slate-900 p-3">
      <div className="text-xs uppercase tracking-wide text-slate-400">{label}</div>
      <div className={`mt-1 text-lg font-semibold ${tone ?? "text-slate-100"}`}>{value}</div>
    </div>
  );
}
