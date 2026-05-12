"use client";

// M5 throughput classifier dashboard panel (Phase A 2026-05-11).
// Polls /observability/utilization every 5s. Window selector (6h/24h/7d).
// Shows the 5-bound classification + reasoning + key metrics + rejection breakdown.

import { useEffect, useState } from "react";
import { UtilizationMetrics, getUtilization } from "@/lib/api";

const WINDOWS_H = [6, 24, 168] as const;

function boundColor(b: string): string {
  switch (b) {
    case "balanced": return "text-emerald-400";
    case "slot-bound": return "text-amber-400";
    case "capital-bound": return "text-amber-400";
    case "opportunity-bound": return "text-orange-400";
    case "latency-bound": return "text-red-400";
    case "risk-gate-bound": return "text-red-400";
    case "UNKNOWN": return "text-zinc-500";
    default: return "text-zinc-400";
  }
}

function pct(n: number): string {
  return `${(n * 100).toFixed(1)}%`;
}

export default function UtilizationPanel() {
  const [windowH, setWindowH] = useState<number>(24);
  const [data, setData] = useState<UtilizationMetrics | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let active = true;
    async function load() {
      try {
        const r = await getUtilization(windowH);
        if (!active) return;
        setData(r);
        setError(null);
      } catch (e: any) {
        if (!active) return;
        setError(e?.message || String(e));
      }
    }
    load();
    const t = setInterval(load, 5000);
    return () => {
      active = false;
      clearInterval(t);
    };
  }, [windowH]);

  return (
    <div className="rounded-lg border border-zinc-800 bg-zinc-900/40 p-4">
      <div className="mb-3 flex items-center justify-between">
        <div>
          <h2 className="text-sm font-semibold text-zinc-100">M5 — Throughput Classifier</h2>
          <p className="text-xs text-zinc-500">5-bound classification (slot/capital/opp/latency/risk-gate)</p>
        </div>
        <div className="flex gap-1">
          {WINDOWS_H.map((h) => (
            <button
              key={h}
              onClick={() => setWindowH(h)}
              className={`rounded px-2 py-0.5 text-xs ${
                windowH === h
                  ? "bg-zinc-700 text-zinc-100"
                  : "bg-zinc-800 text-zinc-400 hover:bg-zinc-700"
              }`}
            >
              {h < 24 ? `${h}h` : h === 24 ? "24h" : "7d"}
            </button>
          ))}
        </div>
      </div>

      {error && <p className="text-xs text-red-400">Error: {error}</p>}

      {data && (
        <div className="space-y-3">
          {/* Bound classification */}
          <div>
            <p className="text-xs text-zinc-500">Classification</p>
            <p className={`text-lg font-semibold ${boundColor(data.bound_classification)}`}>
              {data.bound_classification}
            </p>
            <p className="mt-1 text-xs text-zinc-500">{data.bound_reasoning}</p>
          </div>

          {/* Key metrics grid */}
          <div className="grid grid-cols-3 gap-3 border-t border-zinc-800 pt-3">
            <div>
              <p className="text-xs text-zinc-500">Open / Max</p>
              <p className="text-sm font-mono text-zinc-200">
                {data.open_positions_now} / {data.max_open_positions}
              </p>
            </div>
            <div>
              <p className="text-xs text-zinc-500">Util avg / p95</p>
              <p className="text-sm font-mono text-zinc-200">
                {pct(data.max_pos_utilization_avg)} / {pct(data.max_pos_utilization_p95)}
              </p>
            </div>
            <div>
              <p className="text-xs text-zinc-500">Slot block rate</p>
              <p className="text-sm font-mono text-zinc-200">{pct(data.slot_block_rate)}</p>
            </div>
            <div>
              <p className="text-xs text-zinc-500">Capital util avg</p>
              <p className="text-sm font-mono text-zinc-200">{pct(data.capital_utilization_avg)}</p>
            </div>
            <div>
              <p className="text-xs text-zinc-500">Idle capital</p>
              <p className="text-sm font-mono text-zinc-200">{pct(data.idle_capital_pct)}</p>
            </div>
            <div>
              <p className="text-xs text-zinc-500">Fresh signals/h</p>
              <p className="text-sm font-mono text-zinc-200">{data.fresh_signal_rate_per_hour.toFixed(1)}</p>
            </div>
            <div>
              <p className="text-xs text-zinc-500">Stale rate</p>
              <p className="text-sm font-mono text-zinc-200">{pct(data.stale_backfill_rate)}</p>
            </div>
            <div>
              <p className="text-xs text-zinc-500">Risk-gate rate</p>
              <p className="text-sm font-mono text-zinc-200">{pct(data.risk_gate_rate)}</p>
            </div>
            <div>
              <p className="text-xs text-zinc-500">Sample (rejects)</p>
              <p className="text-sm font-mono text-zinc-200">{data.sample_size}</p>
            </div>
          </div>

          {/* Rejection breakdown */}
          {Object.keys(data.rejection_breakdown).length > 0 && (
            <div className="border-t border-zinc-800 pt-3">
              <p className="mb-2 text-xs font-semibold text-zinc-400">Rejection breakdown</p>
              <div className="grid grid-cols-2 gap-x-3 gap-y-0.5">
                {Object.entries(data.rejection_breakdown)
                  .sort((a, b) => b[1] - a[1])
                  .slice(0, 12)
                  .map(([code, count]) => (
                    <div key={code} className="flex items-center justify-between text-xs">
                      <span className={`font-mono ${
                        code.startsWith("SHADOW_BYPASS_") ? "text-amber-400" : "text-zinc-300"
                      }`}>
                        {code}
                      </span>
                      <span className="text-zinc-500">{count}</span>
                    </div>
                  ))}
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
