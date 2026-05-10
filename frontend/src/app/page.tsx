"use client";

import { AlertTriangle, Database, HardDrive, Pause, Play, Square, Zap } from "lucide-react";
import dynamic from "next/dynamic";
import { useEffect, useState } from "react";

import { ControlButton } from "@/components/ControlButton";
import { MetricCard } from "@/components/MetricCard";
import {
  AppSettings,
  BotLoopStatus,
  BotStatus,
  EdgeReport,
  Market,
  PaperPerformance,
  Signal,
  StorageStatus,
  WalletAudit,
  getAuditedWallets,
  getBotLoopStatus,
  getBotStatus,
  getEdgeReport,
  getMarkets,
  getPaperPerformance,
  getSettings,
  getSignals,
  getStorageStatus,
  postBotAction
} from "@/lib/api";

const PnlChart = dynamic(() => import("@/charts/PnlChart").then((mod) => mod.PnlChart), { ssr: false });

const money = new Intl.NumberFormat("en-US", { style: "currency", currency: "USD", maximumFractionDigits: 0 });

function marketDataSourceSummary(rows: Market[]): string {
  const counts = rows.reduce<Record<string, number>>((acc, row) => {
    const key = row.data_source ?? "unknown";
    acc[key] = (acc[key] ?? 0) + 1;
    return acc;
  }, {});
  return Object.entries(counts)
    .map(([source, count]) => `${source} (${count})`)
    .join(", ");
}

export default function DashboardPage() {
  const [status, setStatus] = useState<BotStatus | null>(null);
  const [loopStatus, setLoopStatus] = useState<BotLoopStatus | null>(null);
  const [markets, setMarkets] = useState<Market[]>([]);
  const [signals, setSignals] = useState<Signal[]>([]);
  const [settings, setSettings] = useState<AppSettings | null>(null);
  const [storage, setStorage] = useState<StorageStatus | null>(null);
  const [edge, setEdge] = useState<EdgeReport | null>(null);
  const [perf, setPerf] = useState<PaperPerformance | null>(null);
  const [topAudits, setTopAudits] = useState<WalletAudit[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function load() {
    try {
      const [s, loop, mkt, sig, st, store, edgeData, performance, audits] = await Promise.all([
        getBotStatus(),
        getBotLoopStatus(),
        getMarkets(),
        getSignals(),
        getSettings(),
        getStorageStatus(),
        getEdgeReport(),
        getPaperPerformance(),
        getAuditedWallets(10)
      ]);
      setStatus(s);
      setLoopStatus(loop);
      setMarkets(mkt);
      setSignals(sig);
      setSettings(st);
      setStorage(store);
      setEdge(edgeData);
      setPerf(performance);
      setTopAudits(audits);
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Unable to load API data");
    }
  }

  async function runAction(action: "start" | "pause" | "stop" | "kill-switch") {
    // Confirmation modals for destructive actions (G — defensive UX 2026-05-06).
    if (action === "kill-switch") {
      if (!confirm("KILL SWITCH active : ferme TOUTES les positions paper et bloque le redémarrage du bot jusqu'à reset manuel.\n\nConfirmer ?")) return;
    } else if (action === "stop") {
      if (!confirm("STOP : interrompt le polling. Positions ouvertes non fermées.\n\nConfirmer ?")) return;
    }
    setBusy(true);
    try {
      const result = await postBotAction(action);
      setStatus(result.status);
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Action failed");
    } finally {
      setBusy(false);
    }
  }

  useEffect(() => {
    load();
    const id = window.setInterval(load, 10_000);
    return () => window.clearInterval(id);
  }, []);

  const activeSignals = signals.filter((signal) => signal.status !== "rejected" && (signal.decision ?? "") !== "REJECT").length;
  const mode = status?.mode ?? "LOADING";

  return (
    <main className="min-h-screen">
      <section className="border-b border-line bg-[#0c0f15]">
        <div className="mx-auto flex max-w-7xl flex-col gap-5 px-5 py-6 lg:flex-row lg:items-center lg:justify-between">
          <div>
            <div className="text-sm font-semibold uppercase tracking-wide text-accent">POLYORACLE v0.7.8 P6 — smart-money copy-trading</div>
            <h1 className="mt-2 text-3xl font-semibold text-white">Trading Research Cockpit</h1>
          </div>
          <div className="flex flex-wrap gap-3">
            <ControlButton label="START" icon={Play} disabled={busy || status?.kill_switch_active} onClick={() => runAction("start")} />
            <ControlButton label="PAUSE" icon={Pause} disabled={busy} onClick={() => runAction("pause")} />
            <ControlButton label="STOP" icon={Square} disabled={busy} onClick={() => runAction("stop")} />
            <ControlButton label="KILL SWITCH" icon={Zap} tone="danger" disabled={busy} onClick={() => runAction("kill-switch")} />
          </div>
        </div>
      </section>

      <section className="mx-auto max-w-7xl px-5 py-6">
        {error ? (
          <div className="mb-5 flex items-center gap-2 rounded border border-danger bg-danger/10 p-3 text-sm text-red-100">
            <AlertTriangle size={16} />
            <span>{error}</span>
          </div>
        ) : null}

        <div className="grid gap-4 md:grid-cols-2 lg:grid-cols-4">
          <MetricCard label="Mode" value={mode} tone={mode === "LIVE" ? "danger" : "good"} />
          <MetricCard label="Paper Capital" value={money.format(perf?.capital ?? status?.paper_capital ?? 0)} />
          <MetricCard label="Realized PnL" value={money.format(perf?.realized_pnl ?? status?.paper_pnl ?? 0)} tone={(perf?.realized_pnl ?? 0) >= 0 ? "good" : "danger"} />
          <MetricCard label="Exposure" value={`${(((perf?.exposure ?? status?.exposure ?? 0) as number) * 100).toFixed(1)}%`} tone="warning" />
          <MetricCard label="Bot State" value={status?.kill_switch_active ? "KILLED" : status?.running ? "RUNNING" : status?.paused ? "PAUSED" : "IDLE"} tone={status?.kill_switch_active ? "danger" : "normal"} />
          <MetricCard label="Wallets audited" value={`${loopStatus?.wallets_audited ?? 0}`} />
          <MetricCard label="Trades audited" value={`${loopStatus?.trades_audited ?? 0}`} />
          <MetricCard label="Signals" value={`${activeSignals}`} tone="good" />
          <MetricCard label="Paper trades" value={`${loopStatus?.paper_trades_opened ?? 0}`} />
          <MetricCard label="Rejected signals" value={`${loopStatus?.rejected_signals ?? 0}`} tone="warning" />
          <MetricCard label="Edge" value={edge?.conclusion ?? "LOADING"} tone={edge?.conclusion === "EDGE_CONFIRMED" ? "good" : "warning"} />
          <MetricCard label="Live" value={status?.live_enabled ? "ENABLED" : "BLOCKED"} tone={status?.live_enabled ? "danger" : "good"} />
        </div>

        {markets.length > 0 ? (
          <div className="mt-4 rounded border border-line bg-slate-900 p-3 text-xs text-slate-400">
            Data source: {marketDataSourceSummary(markets)}. Mock fallback engages when a public Polymarket endpoint is unreachable or rate-limited.
          </div>
        ) : null}

        {status?.live_blocked_reason ? (
          <div className="mt-5 rounded border border-line bg-panel p-4 text-sm text-slate-300">
            Live trading guard: <span className="font-semibold text-accent">{status.live_blocked_reason}</span>
          </div>
        ) : null}

        <div className="mt-6 grid gap-6 lg:grid-cols-[1.1fr_0.9fr]">
          <PnlChart />

          <div className="rounded border border-line bg-panel p-4">
            <div className="mb-3 text-sm font-semibold text-slate-200">Risk Alerts</div>
            <div className="space-y-3 text-sm">
              <div className="rounded border border-line bg-slate-900 p-3">Live execution disabled until compliance and manual confirmation are configured.</div>
              <div className="rounded border border-line bg-slate-900 p-3">Wallet filter capital-tier-aware: NANO→SMALL = ELITE GOLD+SILVER (wr≥95%); ≥$10k adds BRONZE. STRONG GOLD overflow only ≥$1k.</div>
              <div className="rounded border border-line bg-slate-900 p-3">Kill switch prevents restart until operator review.</div>
            </div>
          </div>
        </div>

        <div className="mt-6 grid gap-6 lg:grid-cols-2">
          <div className="rounded border border-line bg-panel p-4">
            <div className="mb-4 flex items-center gap-2">
              <HardDrive size={18} className="text-accent" />
              <h2 className="text-lg font-semibold">Top audited wallets</h2>
            </div>
            <ul className="space-y-2 text-sm">
              {topAudits.length === 0 ? <li className="text-slate-500">No audits yet — open Smart Wallets to run the auditor.</li> : null}
              {topAudits.map((wallet) => (
                <li key={wallet.address} className="flex items-center justify-between rounded border border-line bg-slate-900 p-2">
                  <span className="font-mono text-xs text-slate-200">{wallet.address}</span>
                  <span className="text-xs uppercase text-accent">{wallet.tier}</span>
                  <span className="text-sm">{wallet.smart_score.toFixed(1)}</span>
                </li>
              ))}
            </ul>
          </div>

          <div className="rounded border border-line bg-panel p-4">
            <div className="mb-4 flex items-center gap-2">
              <Database size={18} className="text-accent" />
              <h2 className="text-lg font-semibold">Storage Status</h2>
            </div>
            <div className="space-y-3 text-sm">
              <div className="rounded border border-line bg-slate-900 p-3">SQLite path: {storage?.sqlite_path}</div>
              <div className="rounded border border-line bg-slate-900 p-3">Data root: {storage?.data_root}</div>
              <div className="rounded border border-line bg-slate-900 p-3">Postgres optional: {storage?.postgres_enabled ? "enabled" : "disabled"}</div>
              <div className="rounded border border-line bg-slate-900 p-3">Redis optional: {storage?.redis_enabled ? "enabled" : "disabled"}</div>
            </div>
          </div>
        </div>

        <div className="mt-6 grid gap-6 lg:grid-cols-2">
          <div className="rounded border border-line bg-panel p-4">
            <div className="mb-4 flex items-center justify-between">
              <h2 className="text-lg font-semibold">Top Markets</h2>
              <span className="text-xs text-slate-400">Polymarket Gamma + mock fallback</span>
            </div>
            <div className="overflow-x-auto">
              <table className="w-full min-w-[640px] text-left text-sm">
                <thead className="text-xs uppercase text-slate-400">
                  <tr>
                    <th className="pb-3">Market</th>
                    <th className="pb-3">YES</th>
                    <th className="pb-3">Volume 24h</th>
                    <th className="pb-3">Spread</th>
                    <th className="pb-3">Score</th>
                    <th className="pb-3">Source</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-line">
                  {markets.slice(0, 8).map((market) => (
                    <tr key={market.id}>
                      <td className="py-3 pr-4 text-slate-100">{market.question}</td>
                      <td className="py-3">{market.yes_price?.toFixed(2)}</td>
                      <td className="py-3">{money.format(market.volume_24h)}</td>
                      <td className="py-3">{(market.spread * 100).toFixed(1)}%</td>
                      <td className="py-3 font-semibold text-accent">{market.opportunity_score.toFixed(1)}</td>
                      <td className="py-3 text-xs uppercase text-slate-500">{market.data_source}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>

          <div className="rounded border border-line bg-panel p-4">
            <div className="mb-4 flex items-center justify-between">
              <h2 className="text-lg font-semibold">Signals</h2>
              <span className="text-xs text-slate-400">{signals.length} total</span>
            </div>
            <div className="space-y-3">
              {signals.slice(0, 6).map((signal) => (
                <div key={signal.id} className="rounded border border-line bg-slate-900 p-3">
                  <div className="flex items-center justify-between gap-3">
                    <div className="text-sm font-semibold">{signal.signal_type}</div>
                    <div className={signal.status === "rejected" ? "text-sm text-danger" : "text-sm text-accent"}>{signal.score.toFixed(0)}</div>
                  </div>
                  <div className="mt-2 text-sm text-slate-300">{signal.reason}</div>
                  <div className="mt-2 text-xs uppercase text-slate-500">{signal.outcome} · {signal.action} · {signal.decision ?? signal.status}</div>
                </div>
              ))}
            </div>
          </div>
        </div>
      </section>
    </main>
  );
}
