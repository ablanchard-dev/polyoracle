"use client";

import { useEffect, useState } from "react";
import { EdgeReport, getEdgeReport } from "@/lib/api";

const conclusionTone: Record<string, string> = {
  EDGE_CONFIRMED: "text-accent",
  EDGE_WEAK: "text-amber-300",
  EDGE_NOT_PROVEN: "text-slate-400",
  EDGE_NEGATIVE: "text-danger",
  INSUFFICIENT_DATA: "text-slate-400"
};

export default function EdgePage() {
  const [report, setReport] = useState<EdgeReport | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    getEdgeReport().then(setReport).catch((err) => setError(err instanceof Error ? err.message : "load failed"));
  }, []);

  return (
    <main className="mx-auto max-w-7xl px-5 py-6">
      <header className="mb-6">
        <h1 className="text-2xl font-semibold">Edge Validation</h1>
        <p className="text-sm text-slate-400">Compare strategies, watch the no-trade decision log, measure if a real edge survives copy delay, spread and slippage.</p>
      </header>
      {error ? <div className="mb-4 rounded border border-danger bg-danger/10 p-3 text-sm text-red-100">{error}</div> : null}
      {!report ? <div className="rounded border border-line bg-panel p-4 text-sm text-slate-400">Loading...</div> : null}
      {report ? (
        <>
          <div className="mb-6 rounded border border-line bg-panel p-4">
            <div className="text-xs uppercase tracking-wide text-slate-400">Conclusion</div>
            <div className={`mt-2 text-3xl font-semibold ${conclusionTone[report.conclusion] ?? "text-slate-300"}`}>{report.conclusion}</div>
            <p className="mt-2 text-sm text-slate-400">{report.note}</p>
          </div>

          <div className="grid gap-6 lg:grid-cols-2">
            <section className="rounded border border-line bg-panel p-4">
              <h2 className="mb-3 text-lg font-semibold">Metrics</h2>
              <dl className="grid gap-2 text-sm">
                {Object.entries(report.metrics).map(([key, value]) => (
                  <div key={key} className="flex justify-between border-b border-line py-1 text-slate-300">
                    <dt className="text-xs uppercase text-slate-400">{key}</dt>
                    <dd>{typeof value === "number" ? value.toFixed(4) : String(value)}</dd>
                  </div>
                ))}
              </dl>
            </section>

            <section className="rounded border border-line bg-panel p-4">
              <h2 className="mb-3 text-lg font-semibold">Strategies</h2>
              {report.strategies ? (
                <div className="space-y-2 text-sm">
                  {Object.entries(report.strategies).map(([name, metrics]) => (
                    <div key={name} className="rounded border border-line bg-slate-900 p-3">
                      <div className="text-xs uppercase tracking-wide text-accent">{name}</div>
                      <div className="mt-1 grid grid-cols-3 gap-2 text-xs text-slate-300">
                        {Object.entries(metrics).map(([metricName, metricValue]) => (
                          <div key={metricName}>
                            <div className="uppercase text-slate-500">{metricName}</div>
                            <div>{typeof metricValue === "number" ? metricValue.toFixed(4) : String(metricValue)}</div>
                          </div>
                        ))}
                      </div>
                    </div>
                  ))}
                </div>
              ) : null}
            </section>
          </div>

          <section className="mt-6 rounded border border-line bg-panel p-4">
            <h2 className="mb-3 text-lg font-semibold">No-trade decision log</h2>
            {!report.no_trade_decision_log || report.no_trade_decision_log.length === 0 ? (
              <div className="text-sm text-slate-500">No saved-loss entries yet.</div>
            ) : (
              <ul className="space-y-2 text-sm">
                {report.no_trade_decision_log.map((entry) => (
                  <li key={entry.id} className="rounded border border-line bg-slate-900 p-3">
                    <div className="flex items-center justify-between">
                      <span className="font-semibold text-amber-300">{entry.reason_code}</span>
                      <span className="text-xs text-slate-500">{entry.created_at}</span>
                    </div>
                    <div className="mt-1 text-xs text-slate-400">{entry.market_id ?? "—"} · {entry.signal_id ?? "—"}</div>
                  </li>
                ))}
              </ul>
            )}
          </section>
        </>
      ) : null}
    </main>
  );
}
