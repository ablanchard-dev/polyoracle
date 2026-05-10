"use client";

import { useEffect, useState } from "react";
import { getBotStatus } from "@/lib/api";

// Mirror backend CAPITAL_TIER_RULES (capital_allocator.py — 12-tier refactor 2026-05-06).
// Single source of truth in Python; this is a UI-side mirror for display only.
type Tier = {
  name: string;
  maxCapital: number;
  maxPos: number;
  eliteBuckets: string[];
  strongBuckets: string[];
  maxExpo: number;
};

const TIER_RULES: Tier[] = [
  { name: "NANO",       maxCapital: 200,        maxPos: 12,  eliteBuckets: ["GOLD","SILVER"], strongBuckets: [],       maxExpo: 0.60 },
  { name: "TINY",       maxCapital: 250,        maxPos: 18,  eliteBuckets: ["GOLD","SILVER"], strongBuckets: [],       maxExpo: 0.65 },
  { name: "MICRO",      maxCapital: 500,        maxPos: 22,  eliteBuckets: ["GOLD","SILVER"], strongBuckets: [],       maxExpo: 0.70 },
  { name: "SMALL",      maxCapital: 1000,       maxPos: 32,  eliteBuckets: ["GOLD","SILVER"], strongBuckets: [],       maxExpo: 0.70 },
  { name: "MEDIUM",     maxCapital: 2000,       maxPos: 50,  eliteBuckets: ["GOLD","SILVER"], strongBuckets: [],       maxExpo: 0.75 },
  { name: "LARGE",      maxCapital: 4000,       maxPos: 75,  eliteBuckets: ["GOLD","SILVER"], strongBuckets: [],       maxExpo: 0.78 },
  { name: "XL",         maxCapital: 8000,       maxPos: 110, eliteBuckets: ["GOLD","SILVER"], strongBuckets: [],       maxExpo: 0.80 },
  { name: "XXL",        maxCapital: 10000,      maxPos: 150, eliteBuckets: ["GOLD","SILVER"], strongBuckets: [],       maxExpo: 0.82 },
  { name: "ELITE_OPEN", maxCapital: 32000,      maxPos: 200, eliteBuckets: ["GOLD","SILVER","BRONZE"], strongBuckets: ["GOLD"], maxExpo: 0.85 },
  { name: "GIGA",       maxCapital: 64000,      maxPos: 300, eliteBuckets: ["GOLD","SILVER","BRONZE"], strongBuckets: ["GOLD"], maxExpo: 0.87 },
  { name: "HUGE",       maxCapital: 128000,     maxPos: 400, eliteBuckets: ["GOLD","SILVER","BRONZE"], strongBuckets: ["GOLD"], maxExpo: 0.88 },
  { name: "INST",       maxCapital: Number.POSITIVE_INFINITY, maxPos: 500, eliteBuckets: ["GOLD","SILVER","BRONZE"], strongBuckets: ["GOLD"], maxExpo: 0.90 },
];

function resolveTier(capital: number): Tier {
  return TIER_RULES.find((r) => capital < r.maxCapital) ?? TIER_RULES[TIER_RULES.length - 1];
}

function nextTier(current: Tier): Tier | null {
  const idx = TIER_RULES.findIndex((r) => r.name === current.name);
  if (idx < 0 || idx >= TIER_RULES.length - 1) return null;
  return TIER_RULES[idx + 1];
}

function bucketDot(bucket: string, allowed: boolean): string {
  if (!allowed) return "text-slate-700";
  return bucket === "GOLD" ? "text-yellow-300" : bucket === "SILVER" ? "text-slate-300" : "text-amber-700";
}

export function CapitalTierPanel() {
  const [capital, setCapital] = useState<number>(0);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    async function load() {
      try {
        const s = await getBotStatus();
        if (!cancelled) setCapital(s.paper_capital ?? 0);
      } catch (err) {
        if (!cancelled) setError(err instanceof Error ? err.message : "Failed to load");
      }
    }
    load();
    const id = window.setInterval(load, 10_000);
    return () => { cancelled = true; window.clearInterval(id); };
  }, []);

  const tier = resolveTier(capital);
  const next = nextTier(tier);
  const tierMin = TIER_RULES.findIndex((r) => r.name === tier.name) === 0
    ? 0
    : TIER_RULES[TIER_RULES.findIndex((r) => r.name === tier.name) - 1].maxCapital;
  const progressPct = next
    ? Math.min(100, ((capital - tierMin) / (tier.maxCapital - tierMin)) * 100)
    : 100;

  return (
    <section className="rounded border border-line bg-panel p-4">
      <header className="mb-3 flex items-center justify-between">
        <div>
          <h2 className="text-lg font-semibold">Capital Tier</h2>
          <p className="text-xs text-slate-400">12-tier refactor 2026-05-06 — wallet filter par bucket WR.</p>
        </div>
        <div className="text-right">
          <div className="text-xs uppercase tracking-wide text-slate-400">Current capital</div>
          <div className="text-2xl font-semibold text-accent">${capital.toFixed(2)}</div>
        </div>
      </header>
      {error && <div className="mb-3 rounded border border-danger bg-danger/10 p-2 text-xs text-red-100">{error}</div>}

      <div className="mb-4 flex items-center justify-between rounded border border-accent/40 bg-accent/5 p-3">
        <div>
          <div className="text-xs uppercase tracking-wide text-slate-400">Tier actif</div>
          <div className="text-2xl font-semibold text-accent">{tier.name}</div>
        </div>
        <div className="text-right text-sm">
          <div>max_pos: <span className="font-semibold text-slate-100">{tier.maxPos}</span></div>
          <div>max_expo: <span className="font-semibold text-slate-100">{(tier.maxExpo * 100).toFixed(0)}%</span></div>
        </div>
      </div>

      <div className="mb-4">
        <div className="mb-2 text-xs uppercase tracking-wide text-slate-400">Wallet filter</div>
        <div className="grid grid-cols-2 gap-3 text-sm">
          <div className="rounded border border-line bg-slate-900 p-2">
            <div className="text-xs text-slate-400">ELITE buckets</div>
            <div className="mt-1 flex gap-2">
              {(["GOLD", "SILVER", "BRONZE"] as const).map((b) => (
                <span key={b} className={`text-xs font-semibold ${bucketDot(b, tier.eliteBuckets.includes(b))}`}>
                  ● {b}
                </span>
              ))}
            </div>
          </div>
          <div className="rounded border border-line bg-slate-900 p-2">
            <div className="text-xs text-slate-400">STRONG overflow</div>
            <div className="mt-1 flex gap-2">
              {tier.strongBuckets.length === 0 ? (
                <span className="text-xs text-slate-500">— off —</span>
              ) : (
                tier.strongBuckets.map((b) => (
                  <span key={b} className="text-xs font-semibold text-yellow-300">● {b}</span>
                ))
              )}
            </div>
          </div>
        </div>
      </div>

      {next && (
        <div>
          <div className="mb-1 flex items-center justify-between text-xs">
            <span className="text-slate-400">Next tier: <span className="font-semibold text-slate-200">{next.name}</span></span>
            <span className="text-slate-400">${tier.maxCapital.toLocaleString()}</span>
          </div>
          <div className="h-2 w-full rounded bg-slate-800">
            <div
              className="h-2 rounded bg-accent transition-all"
              style={{ width: `${progressPct.toFixed(1)}%` }}
            />
          </div>
          <div className="mt-1 text-right text-xs text-slate-500">{progressPct.toFixed(1)}%</div>
        </div>
      )}
    </section>
  );
}
