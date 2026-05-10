"use client";

import { useEffect, useState } from "react";

type CohortWallet = {
  address: string;
  candidate_status: string;
  win_rate: number | null;
  wr_bucket: string;
  resolved_winning: number | null;
  resolved_losing: number | null;
  sample_wl: number;
  recent_activity_score: number | null;
  composite_score: number | null;
  best_category: string | null;
  tradable_now: boolean;
};

type CohortResponse = {
  current_tier: string;
  current_capital: number;
  allowed_elite_buckets: string[];
  allowed_strong_buckets: string[];
  max_open_positions: number;
  max_total_exposure: number;
  counts: {
    total_elite: number;
    total_strong: number;
    elite_by_bucket: Record<string, number>;
    strong_by_bucket: Record<string, number>;
  };
  tradable_now: number;
  wallets: CohortWallet[];
};

const API_BASE = process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8000";

const bucketColor: Record<string, string> = {
  GOLD: "text-yellow-300",
  SILVER: "text-slate-300",
  BRONZE: "text-amber-700",
  REGULAR: "text-slate-500",
  UNKNOWN: "text-slate-600",
};

export function CohortPanel() {
  const [data, setData] = useState<CohortResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [filter, setFilter] = useState<"tradable" | "all">("tradable");

  useEffect(() => {
    let cancelled = false;
    async function load() {
      try {
        const res = await fetch(`${API_BASE}/wallets/cohort?limit=300`);
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const json: CohortResponse = await res.json();
        if (!cancelled) setData(json);
      } catch (err) {
        if (!cancelled) setError(err instanceof Error ? err.message : "Failed to load cohort");
      }
    }
    load();
    const id = window.setInterval(load, 15_000);
    return () => { cancelled = true; window.clearInterval(id); };
  }, []);

  if (error) {
    return <div className="rounded border border-danger bg-danger/10 p-3 text-sm text-red-100">Cohort load failed: {error}</div>;
  }
  if (!data) {
    return <div className="rounded border border-line bg-panel p-4 text-sm text-slate-400">Loading cohort…</div>;
  }

  const wallets = filter === "tradable" ? data.wallets.filter((w) => w.tradable_now) : data.wallets;

  return (
    <section className="rounded border border-line bg-panel p-4">
      <header className="mb-4 flex flex-wrap items-start justify-between gap-3">
        <div>
          <h2 className="text-lg font-semibold">Cohorte active (P6 — 12-tier)</h2>
          <p className="text-xs text-slate-400">
            Source : MFWR 2026-05-04 reclass v2 — 621 ELITE / 821 STRONG / 237k DROPPED.
            Filtre auto : <span className="font-semibold text-accent">{data.current_tier}</span> tier (capital ${data.current_capital.toFixed(2)}).
          </p>
        </div>
        <div className="flex gap-2 text-xs">
          <button
            onClick={() => setFilter("tradable")}
            className={`rounded px-3 py-1 ${filter === "tradable" ? "bg-accent/20 text-accent" : "bg-slate-800 text-slate-400 hover:bg-slate-700"}`}
          >Tradable now ({data.tradable_now})</button>
          <button
            onClick={() => setFilter("all")}
            className={`rounded px-3 py-1 ${filter === "all" ? "bg-accent/20 text-accent" : "bg-slate-800 text-slate-400 hover:bg-slate-700"}`}
          >Tous ({data.counts.total_elite + data.counts.total_strong})</button>
        </div>
      </header>

      <div className="mb-4 grid grid-cols-2 gap-3 md:grid-cols-4">
        <Stat label="ELITE GOLD" value={data.counts.elite_by_bucket.GOLD} tone="gold" />
        <Stat label="ELITE SILVER" value={data.counts.elite_by_bucket.SILVER} tone="silver" />
        <Stat label="ELITE BRONZE" value={data.counts.elite_by_bucket.BRONZE} tone="bronze" />
        <Stat label="STRONG GOLD" value={data.counts.strong_by_bucket.GOLD} tone="gold" />
      </div>

      <div className="overflow-x-auto">
        <table className="w-full min-w-[820px] text-left text-sm">
          <thead className="text-xs uppercase text-slate-400">
            <tr>
              <th className="pb-2">Wallet</th>
              <th className="pb-2">Status</th>
              <th className="pb-2">Bucket</th>
              <th className="pb-2 text-right">WR</th>
              <th className="pb-2 text-right">W</th>
              <th className="pb-2 text-right">L</th>
              <th className="pb-2 text-right">Sample</th>
              <th className="pb-2 text-right">Activity</th>
              <th className="pb-2 text-right">Composite</th>
              <th className="pb-2">Tradable</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-line">
            {wallets.slice(0, 80).map((w) => (
              <tr key={w.address} className={w.tradable_now ? "" : "opacity-60"}>
                <td className="py-2 font-mono text-xs text-slate-200">{w.address.slice(0, 10)}…{w.address.slice(-4)}</td>
                <td className="py-2 text-xs uppercase">{w.candidate_status}</td>
                <td className={`py-2 text-xs font-semibold ${bucketColor[w.wr_bucket] ?? "text-slate-500"}`}>{w.wr_bucket}</td>
                <td className="py-2 text-right">{w.win_rate != null ? (w.win_rate * 100).toFixed(1) + "%" : "—"}</td>
                <td className="py-2 text-right">{w.resolved_winning ?? 0}</td>
                <td className="py-2 text-right">{w.resolved_losing ?? 0}</td>
                <td className="py-2 text-right">{w.sample_wl}</td>
                <td className="py-2 text-right">{w.recent_activity_score?.toFixed(0) ?? "—"}</td>
                <td className="py-2 text-right">{w.composite_score?.toFixed(1) ?? "—"}</td>
                <td className="py-2">
                  {w.tradable_now
                    ? <span className="rounded bg-accent/15 px-2 py-1 text-xs text-accent">YES</span>
                    : <span className="rounded bg-slate-800 px-2 py-1 text-xs text-slate-500">no</span>
                  }
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        {wallets.length > 80 && (
          <div className="mt-2 text-xs text-slate-500">… {wallets.length - 80} additional rows truncated</div>
        )}
      </div>
    </section>
  );
}

function Stat({ label, value, tone }: { label: string; value: number; tone: "gold" | "silver" | "bronze" }) {
  const c = tone === "gold" ? "text-yellow-300" : tone === "silver" ? "text-slate-200" : "text-amber-700";
  return (
    <div className="rounded border border-line bg-slate-900 p-3">
      <div className="text-xs uppercase tracking-wide text-slate-400">{label}</div>
      <div className={`mt-1 text-2xl font-semibold ${c}`}>{value}</div>
    </div>
  );
}
