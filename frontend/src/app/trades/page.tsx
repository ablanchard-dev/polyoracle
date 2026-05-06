"use client";

import { useEffect, useState } from "react";
import {
  SmartMoneyEvent,
  TradeAudit,
  TradeCluster,
  getAuditedTrades,
  getSmartMoneyEvents,
  getTradeClusters,
  runTradeAudit
} from "@/lib/api";

const decisionTone: Record<string, string> = {
  PAPER_TRADE: "text-accent",
  SIGNAL: "text-emerald-300",
  WATCH: "text-sky-300",
  IGNORE: "text-slate-500"
};

export default function TradeAuditPage() {
  const [trades, setTrades] = useState<TradeAudit[]>([]);
  const [clusters, setClusters] = useState<TradeCluster[]>([]);
  const [events, setEvents] = useState<SmartMoneyEvent[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function load() {
    try {
      const [auditedRows, clusterRows, eventRows] = await Promise.all([
        getAuditedTrades(200),
        getTradeClusters(50),
        getSmartMoneyEvents(100)
      ]);
      setTrades(auditedRows);
      setClusters(clusterRows);
      setEvents(eventRows);
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load trades");
    }
  }

  async function audit() {
    setBusy(true);
    try {
      await runTradeAudit();
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Audit failed");
    } finally {
      setBusy(false);
    }
  }

  useEffect(() => {
    load();
  }, []);

  return (
    <main className="mx-auto max-w-7xl px-5 py-6">
      <header className="mb-6 flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-semibold">Trade Audit</h1>
          <p className="text-sm text-slate-400">Each trade is filtered through wallet, orderbook, spread, slippage and copyable edge checks.</p>
        </div>
        <button
          onClick={audit}
          disabled={busy}
          className="rounded border border-accent bg-accent/15 px-4 py-2 text-sm font-medium text-accent transition hover:bg-accent/25 disabled:opacity-60"
        >
          {busy ? "Auditing..." : "Run trade audit"}
        </button>
      </header>

      {error ? <div className="mb-4 rounded border border-danger bg-danger/10 p-3 text-sm text-red-100">{error}</div> : null}

      <section className="rounded border border-line bg-panel p-4">
        <h2 className="mb-3 text-lg font-semibold">Audited trades</h2>
        <div className="overflow-x-auto">
          <table className="w-full min-w-[1100px] text-left text-sm">
            <thead className="text-xs uppercase text-slate-400">
              <tr>
                <th className="pb-3">Wallet</th>
                <th className="pb-3">Tier</th>
                <th className="pb-3">Market</th>
                <th className="pb-3">Side</th>
                <th className="pb-3">Price</th>
                <th className="pb-3">Notional</th>
                <th className="pb-3">Spread</th>
                <th className="pb-3">Edge</th>
                <th className="pb-3">Quality</th>
                <th className="pb-3">Decision</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-line">
              {trades.length === 0 ? (
                <tr><td colSpan={10} className="py-6 text-center text-slate-500">No audits yet — run the trade audit.</td></tr>
              ) : null}
              {trades.map((trade) => (
                <tr key={trade.id}>
                  <td className="py-3 pr-3 font-mono text-xs text-slate-200">{trade.wallet_address}</td>
                  <td className="py-3 text-xs uppercase">{trade.wallet_tier}</td>
                  <td className="py-3 pr-3 text-xs">{trade.market_id}</td>
                  <td className="py-3">{trade.side}</td>
                  <td className="py-3">{trade.price.toFixed(3)}</td>
                  <td className="py-3">${trade.notional_usd.toFixed(0)}</td>
                  <td className="py-3">{(trade.estimated_spread * 100).toFixed(2)}%</td>
                  <td className="py-3">{trade.copyable_edge.toFixed(4)}</td>
                  <td className="py-3 text-xs uppercase">{trade.orderbook_quality}</td>
                  <td className={`py-3 font-semibold ${decisionTone[trade.decision] ?? "text-slate-400"}`}>{trade.decision}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </section>

      <section className="mt-6 grid gap-6 lg:grid-cols-2">
        <div className="rounded border border-line bg-panel p-4">
          <h2 className="mb-3 text-lg font-semibold">Trade clusters</h2>
          {clusters.length === 0 ? <div className="text-sm text-slate-500">No clusters detected yet.</div> : null}
          <ul className="space-y-3">
            {clusters.map((cluster) => (
              <li key={cluster.id} className="rounded border border-line bg-slate-900 p-3 text-sm">
                <div className="font-semibold">{cluster.market_id} · {cluster.side}</div>
                <div className="text-xs text-slate-400">{cluster.wallet_count} wallets · ${cluster.notional_usd.toFixed(0)} · score {cluster.average_wallet_score.toFixed(1)}</div>
              </li>
            ))}
          </ul>
        </div>

        <div className="rounded border border-line bg-panel p-4">
          <h2 className="mb-3 text-lg font-semibold">Smart-money events</h2>
          {events.length === 0 ? <div className="text-sm text-slate-500">No smart-money events yet.</div> : null}
          <ul className="space-y-3">
            {events.map((event) => (
              <li key={event.id} className="rounded border border-line bg-slate-900 p-3 text-sm">
                <div className="text-xs uppercase text-accent">{event.event_type}</div>
                <div className="mt-1">{event.summary}</div>
                <div className="mt-1 text-xs text-slate-400">${event.notional_usd.toFixed(0)} · confidence {event.confidence.toFixed(1)}</div>
              </li>
            ))}
          </ul>
        </div>
      </section>
    </main>
  );
}
