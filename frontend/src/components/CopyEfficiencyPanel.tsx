"use client";

// M1 copy_efficiency dashboard panel (Phase A 2026-05-11).
// Polls /edge/copy-efficiency every 10s. Window selector (24h/7d/30d).
// Displays: global ratio + classification, per-category breakdown with
// breach flags, top/bottom wallets by ratio.

import { useEffect, useState } from "react";
import { CopyEfficiencyReport, getCopyEfficiency } from "@/lib/api";

const WINDOWS = ["24h", "7d", "30d"] as const;

function classificationColor(c: string): string {
  switch (c) {
    case "EXCELLENT": return "text-emerald-400";
    case "ACCEPTABLE": return "text-green-400";
    case "DEGRADED": return "text-amber-400";
    case "CRITICAL": return "text-red-400";
    case "NO_VALID_RATIO": return "text-zinc-500";
    default: return "text-zinc-400";
  }
}

function ratioColor(ratio: number | null, threshold: number = 0.7): string {
  if (ratio === null) return "text-zinc-500";
  if (ratio >= 0.9) return "text-emerald-400";
  if (ratio >= threshold) return "text-green-400";
  if (ratio >= 0.5) return "text-amber-400";
  return "text-red-400";
}

export default function CopyEfficiencyPanel() {
  const [window, setWindow] = useState<typeof WINDOWS[number]>("24h");
  const [data, setData] = useState<CopyEfficiencyReport | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let active = true;
    async function load() {
      try {
        const r = await getCopyEfficiency(window);
        if (!active) return;
        setData(r);
        setError(null);
      } catch (e: any) {
        if (!active) return;
        setError(e?.message || String(e));
      }
    }
    load();
    const t = setInterval(load, 10000);
    return () => {
      active = false;
      clearInterval(t);
    };
  }, [window]);

  return (
    <div className="rounded-lg border border-zinc-800 bg-zinc-900/40 p-4">
      <div className="mb-3 flex items-center justify-between">
        <div>
          <h2 className="text-sm font-semibold text-zinc-100">M1 — Copy Efficiency</h2>
          <p className="text-xs text-zinc-500">bot_pnl / source_wallet_counterfactual_pnl</p>
        </div>
        <div className="flex gap-1">
          {WINDOWS.map((w) => (
            <button
              key={w}
              onClick={() => setWindow(w)}
              className={`rounded px-2 py-0.5 text-xs ${
                window === w
                  ? "bg-zinc-700 text-zinc-100"
                  : "bg-zinc-800 text-zinc-400 hover:bg-zinc-700"
              }`}
            >
              {w}
            </button>
          ))}
        </div>
      </div>

      {error && <p className="text-xs text-red-400">Error: {error}</p>}

      {data && (
        <div className="space-y-3">
          {/* Global ratio + classification */}
          <div className="grid grid-cols-3 gap-3">
            <div>
              <p className="text-xs text-zinc-500">Global ratio</p>
              <p className={`text-2xl font-mono ${ratioColor(data.global_ratio)}`}>
                {data.global_ratio !== null ? data.global_ratio.toFixed(3) : "—"}
              </p>
            </div>
            <div>
              <p className="text-xs text-zinc-500">Classification</p>
              <p className={`text-sm font-semibold ${classificationColor(data.classification)}`}>
                {data.classification}
              </p>
            </div>
            <div>
              <p className="text-xs text-zinc-500">Sample</p>
              <p className="text-sm font-mono text-zinc-200">
                {data.sample_size} <span className="text-zinc-500">(excl. {data.sample_size_excluded})</span>
              </p>
            </div>
          </div>

          {/* Bot vs source totals */}
          <div className="grid grid-cols-3 gap-3 border-t border-zinc-800 pt-3">
            <div>
              <p className="text-xs text-zinc-500">Bot PnL</p>
              <p className={`text-sm font-mono ${data.bot_pnl_total >= 0 ? "text-green-400" : "text-red-400"}`}>
                {data.bot_pnl_total.toFixed(2)}
              </p>
            </div>
            <div>
              <p className="text-xs text-zinc-500">Source counterfact.</p>
              <p className="text-sm font-mono text-zinc-200">{data.source_counterfactual_total.toFixed(2)}</p>
            </div>
            <div>
              <p className="text-xs text-zinc-500">Bot &gt; source</p>
              <p className="text-sm font-mono text-zinc-200">{data.bot_outperforms_source_count}</p>
            </div>
          </div>

          {/* By category */}
          {Object.keys(data.by_category).length > 0 && (
            <div className="border-t border-zinc-800 pt-3">
              <p className="mb-2 text-xs font-semibold text-zinc-400">Par catégorie</p>
              <div className="space-y-1">
                {Object.entries(data.by_category).map(([cat, m]) => (
                  <div
                    key={cat}
                    className={`flex items-center justify-between text-xs ${
                      m.breached ? "text-red-400" : "text-zinc-300"
                    }`}
                  >
                    <span className="font-mono">{cat}</span>
                    <span className="flex gap-3">
                      <span>n={m.n}</span>
                      <span className={ratioColor(m.ratio, m.threshold)}>
                        {m.ratio !== null ? m.ratio.toFixed(3) : "—"} / ≥{m.threshold}
                      </span>
                      {m.breached && <span className="text-red-500">⚠ BREACH</span>}
                    </span>
                  </div>
                ))}
              </div>
            </div>
          )}

          {/* Threshold breaches */}
          {data.threshold_breaches.length > 0 && (
            <div className="border-t border-red-500/30 pt-3">
              <p className="mb-2 text-xs font-semibold text-red-400">⚠ Threshold breaches (n≥50)</p>
              {data.threshold_breaches.map((b) => (
                <div key={b.category} className="text-xs text-red-300">
                  <span className="font-mono">{b.category}</span>: ratio {b.ratio.toFixed(3)} &lt; {b.threshold} (n={b.sample_size})
                </div>
              ))}
            </div>
          )}

          {/* Top/Bottom wallets */}
          {(data.by_wallet_top.length > 0 || data.by_wallet_bottom.length > 0) && (
            <div className="grid grid-cols-2 gap-3 border-t border-zinc-800 pt-3">
              {data.by_wallet_top.length > 0 && (
                <div>
                  <p className="mb-1 text-xs font-semibold text-emerald-400">Top wallets</p>
                  {data.by_wallet_top.slice(0, 5).map((w) => (
                    <div key={w.wallet} className="text-xs">
                      <span className="font-mono text-zinc-400">{w.wallet.slice(0, 10)}…</span>
                      <span className={`ml-2 ${ratioColor(w.ratio)}`}>{w.ratio.toFixed(2)}</span>
                      <span className="ml-2 text-zinc-500">n={w.n}</span>
                    </div>
                  ))}
                </div>
              )}
              {data.by_wallet_bottom.length > 0 && (
                <div>
                  <p className="mb-1 text-xs font-semibold text-red-400">Bottom wallets</p>
                  {data.by_wallet_bottom.slice(0, 5).map((w) => (
                    <div key={w.wallet} className="text-xs">
                      <span className="font-mono text-zinc-400">{w.wallet.slice(0, 10)}…</span>
                      <span className={`ml-2 ${ratioColor(w.ratio)}`}>{w.ratio.toFixed(2)}</span>
                      <span className="ml-2 text-zinc-500">n={w.n}</span>
                    </div>
                  ))}
                </div>
              )}
            </div>
          )}
        </div>
      )}
    </div>
  );
}
