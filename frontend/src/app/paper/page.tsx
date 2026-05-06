"use client";

import { useEffect, useState } from "react";
import {
  PaperPerformance,
  PaperPosition,
  getPaperPerformance,
  getPaperPositions,
  getPaperTrades
} from "@/lib/api";

const money = new Intl.NumberFormat("en-US", { style: "currency", currency: "USD", maximumFractionDigits: 2 });

export default function PaperPage() {
  const [positions, setPositions] = useState<PaperPosition[]>([]);
  const [trades, setTrades] = useState<PaperPosition[]>([]);
  const [perf, setPerf] = useState<PaperPerformance | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function load() {
    try {
      const [openRows, allRows, performance] = await Promise.all([
        getPaperPositions(),
        getPaperTrades(),
        getPaperPerformance()
      ]);
      setPositions(openRows);
      setTrades(allRows);
      setPerf(performance);
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "load failed");
    }
  }

  useEffect(() => {
    load();
    const id = window.setInterval(load, 10_000);
    return () => window.clearInterval(id);
  }, []);

  return (
    <main className="mx-auto max-w-7xl px-5 py-6">
      <header className="mb-6">
        <h1 className="text-2xl font-semibold">Paper Trading</h1>
        <p className="text-sm text-slate-400">Auto paper-trades opened only when signal decision is PAPER_TRADE and risk filters pass.</p>
      </header>
      {error ? <div className="mb-4 rounded border border-danger bg-danger/10 p-3 text-sm text-red-100">{error}</div> : null}

      <div className="grid gap-4 md:grid-cols-2 lg:grid-cols-4">
        <Stat label="Capital" value={money.format(perf?.capital ?? 0)} />
        <Stat label="Realized PnL" value={money.format(perf?.realized_pnl ?? 0)} tone={(perf?.realized_pnl ?? 0) >= 0 ? "good" : "bad"} />
        <Stat label="Daily PnL" value={money.format(perf?.daily_pnl ?? 0)} />
        <Stat label="Weekly PnL" value={money.format(perf?.weekly_pnl ?? 0)} />
        <Stat label="Open positions" value={`${perf?.open_positions ?? 0}`} />
        <Stat label="Closed trades" value={`${perf?.closed_trades ?? 0}`} />
        <Stat label="Win rate" value={`${((perf?.win_rate ?? 0) * 100).toFixed(0)}%`} />
        <Stat label="Auto trading" value={perf?.auto_enabled ? "ON" : "OFF"} tone={perf?.auto_enabled ? "good" : "warn"} />
      </div>

      <section className="mt-6 rounded border border-line bg-panel p-4">
        <h2 className="mb-3 text-lg font-semibold">Open positions</h2>
        <PositionTable rows={positions} />
      </section>

      <section className="mt-6 rounded border border-line bg-panel p-4">
        <h2 className="mb-3 text-lg font-semibold">All paper trades</h2>
        <PositionTable rows={trades} />
      </section>
    </main>
  );
}

function Stat({ label, value, tone }: { label: string; value: string; tone?: "good" | "bad" | "warn" }) {
  const toneClass = tone === "good" ? "text-accent" : tone === "bad" ? "text-danger" : tone === "warn" ? "text-warning" : "text-slate-100";
  return (
    <div className="rounded border border-line bg-panel p-4">
      <div className="text-xs uppercase tracking-wide text-slate-400">{label}</div>
      <div className={`mt-2 text-2xl font-semibold ${toneClass}`}>{value}</div>
    </div>
  );
}

function PositionTable({ rows }: { rows: PaperPosition[] }) {
  if (rows.length === 0) {
    return <div className="text-sm text-slate-500">No paper trades yet.</div>;
  }
  return (
    <div className="overflow-x-auto">
      <table className="w-full min-w-[960px] text-left text-sm">
        <thead className="text-xs uppercase text-slate-400">
          <tr>
            <th className="pb-3">Market</th>
            <th className="pb-3">Outcome</th>
            <th className="pb-3">Side</th>
            <th className="pb-3">Qty</th>
            <th className="pb-3">Entry</th>
            <th className="pb-3">Notional</th>
            <th className="pb-3">Status</th>
            <th className="pb-3">PnL</th>
            <th className="pb-3">Auto</th>
            <th className="pb-3">Wallet</th>
          </tr>
        </thead>
        <tbody className="divide-y divide-line">
          {rows.map((row) => (
            <tr key={row.id}>
              <td className="py-3 text-xs">{row.market_id}</td>
              <td className="py-3">{row.outcome}</td>
              <td className="py-3">{row.side}</td>
              <td className="py-3">{row.quantity.toFixed(0)}</td>
              <td className="py-3">{row.average_price.toFixed(3)}</td>
              <td className="py-3">${row.notional_usd?.toFixed(2)}</td>
              <td className="py-3 text-xs uppercase">{row.status}</td>
              <td className={`py-3 ${row.realized_pnl >= 0 ? "text-accent" : "text-danger"}`}>{row.realized_pnl.toFixed(2)}</td>
              <td className="py-3 text-xs">{row.auto ? "YES" : "—"}</td>
              <td className="py-3 font-mono text-xs text-slate-400">{row.wallet_address ?? "—"}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
