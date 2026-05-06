"use client";

import { useEffect, useState } from "react";
import { Signal, getSignals } from "@/lib/api";

const decisionTone: Record<string, string> = {
  PAPER_TRADE: "text-accent",
  WATCH: "text-sky-300",
  REJECT: "text-danger",
  NEED_MORE_DATA: "text-amber-300"
};

export default function SignalsPage() {
  const [signals, setSignals] = useState<Signal[]>([]);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    getSignals().then(setSignals).catch((err) => setError(err instanceof Error ? err.message : "load failed"));
  }, []);

  return (
    <main className="mx-auto max-w-7xl px-5 py-6">
      <header className="mb-6">
        <h1 className="text-2xl font-semibold">Signals</h1>
        <p className="text-sm text-slate-400">Smart-wallet entries, clusters and rejection reasons. Rejected signals are kept for the no-trade decision log.</p>
      </header>
      {error ? <div className="mb-4 rounded border border-danger bg-danger/10 p-3 text-sm text-red-100">{error}</div> : null}
      <div className="space-y-3">
        {signals.length === 0 ? <div className="rounded border border-line bg-panel p-4 text-sm text-slate-500">No signals yet.</div> : null}
        {signals.map((signal) => (
          <div key={signal.id} className="rounded border border-line bg-panel p-4">
            <div className="flex flex-wrap items-center gap-3">
              <span className="text-sm font-semibold uppercase tracking-wide text-accent">{signal.signal_type}</span>
              <span className="text-xs uppercase text-slate-400">{signal.market_id}</span>
              <span className={`ml-auto text-sm font-semibold ${decisionTone[signal.decision ?? ""] ?? "text-slate-300"}`}>
                {signal.decision ?? signal.status}
              </span>
              <span className="text-sm">Score {signal.score.toFixed(0)}</span>
            </div>
            <div className="mt-2 text-sm text-slate-300">{signal.reason}</div>
            <div className="mt-2 grid gap-3 text-xs text-slate-400 md:grid-cols-4">
              <div>Outcome: {signal.outcome}</div>
              <div>Confidence: {(signal.confidence * 100).toFixed(0)}%</div>
              <div>Edge: {signal.copyable_edge?.toFixed(4) ?? "—"}</div>
              <div>Proposed size: ${signal.proposed_size_usd?.toFixed(2) ?? "0"}</div>
            </div>
          </div>
        ))}
      </div>
    </main>
  );
}
