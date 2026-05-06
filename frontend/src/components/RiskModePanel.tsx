"use client";

import { useEffect, useState } from "react";
import { RiskProfile, getRiskMode, getRiskProfiles, setRiskMode } from "@/lib/api";

const modeTone: Record<string, string> = {
  SAFE: "border-emerald-500/60 text-emerald-200 bg-emerald-500/10",
  AGGRESSIVE: "border-amber-500/60 text-amber-200 bg-amber-500/10",
  FULL_PAPER: "border-red-500/60 text-red-200 bg-red-500/10"
};

export function RiskModePanel() {
  const [active, setActive] = useState<string>("SAFE");
  const [profile, setProfile] = useState<RiskProfile | null>(null);
  const [profiles, setProfiles] = useState<RiskProfile[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function load() {
    try {
      const [current, all] = await Promise.all([getRiskMode(), getRiskProfiles()]);
      setActive(current.mode);
      setProfile(current.profile);
      setProfiles(all.profiles);
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load risk mode");
    }
  }

  useEffect(() => {
    load();
  }, []);

  async function pick(name: "SAFE" | "AGGRESSIVE" | "FULL_PAPER") {
    if (name === active) return;
    if (name === "FULL_PAPER" && !confirm("Switch to FULL_PAPER mode? This enables maximum aggregate risk exposure. Continue?")) return;
    setBusy(true);
    try {
      await setRiskMode(name, "ui");
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to change risk mode");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="rounded border border-line bg-panel p-4">
      <div className="mb-3 flex items-center justify-between">
        <div>
          <h2 className="text-lg font-semibold">Risk mode</h2>
          <p className="text-xs text-slate-400">SAFE / AGGRESSIVE / FULL_PAPER. The kill switch and exposure caps always apply.</p>
        </div>
        <div className={`rounded border px-3 py-1 text-xs font-semibold uppercase ${modeTone[active] ?? "border-line text-slate-300"}`}>{active}</div>
      </div>
      {error ? <div className="mb-3 rounded border border-danger bg-danger/10 p-2 text-xs text-red-100">{error}</div> : null}

      <div className="mb-4 flex flex-wrap gap-2">
        {(["SAFE", "AGGRESSIVE", "FULL_PAPER"] as const).map((name) => (
          <button
            key={name}
            disabled={busy}
            onClick={() => pick(name)}
            className={`rounded border px-3 py-1 text-sm transition ${active === name ? modeTone[name] : "border-line bg-slate-800 text-slate-200 hover:bg-slate-700"} disabled:opacity-60`}
          >
            {name}
          </button>
        ))}
      </div>

      {active === "FULL_PAPER" ? (
        <div className="mb-3 rounded border border-red-500/60 bg-red-500/10 p-2 text-xs text-red-100">
          FULL MODE = paper only by default. High aggregate risk. No live execution.
        </div>
      ) : null}

      {profile ? (
        <div className="grid gap-2 text-xs text-slate-300 md:grid-cols-2">
          <div>Allowed statuses: <span className="font-mono text-slate-100">{profile.allowed_statuses.join(", ")}</span></div>
          <div>Min sample size: <span className="font-mono text-slate-100">{profile.min_sample_size}</span></div>
          <div>Confidence required: <span className="font-mono text-slate-100">{profile.require_medium_high_confidence ? "MEDIUM/HIGH" : "any"}</span></div>
          <div>Max risk / trade: <span className="font-mono text-slate-100">{(profile.max_risk_per_trade * 100).toFixed(1)}%</span></div>
          <div>Max wallet exposure: <span className="font-mono text-slate-100">{(profile.max_wallet_exposure * 100).toFixed(1)}%</span></div>
          <div>Max market exposure: <span className="font-mono text-slate-100">{(profile.max_market_exposure * 100).toFixed(1)}%</span></div>
          <div>Max total exposure: <span className="font-mono text-slate-100">{(profile.max_total_exposure * 100).toFixed(1)}%</span></div>
          <div>Max open positions: <span className="font-mono text-slate-100">{profile.max_open_positions ?? "∞"}</span></div>
          <div>Daily trade cap: <span className="font-mono text-slate-100">{profile.no_daily_trade_count_limit ? "no cap" : profile.max_daily_trades}</span></div>
          <div>Live allowed: <span className="font-mono text-slate-100">{profile.live_allowed ? "yes" : "BLOCKED"}</span></div>
        </div>
      ) : null}

      {profiles.length > 0 ? (
        <details className="mt-4 text-xs text-slate-400">
          <summary className="cursor-pointer">Compare all profiles</summary>
          <div className="mt-2 grid gap-2 md:grid-cols-3">
            {profiles.map((p) => (
              <div key={p.name} className={`rounded border ${modeTone[p.name]?.split(" ")[0] ?? "border-line"} p-2`}>
                <div className="font-semibold">{p.name}</div>
                <div className="mt-1 text-[11px]">{p.description}</div>
                <ul className="mt-1 space-y-0.5">
                  <li>statuses: {p.allowed_statuses.join(", ")}</li>
                  <li>min sample: {p.min_sample_size}</li>
                  <li>max risk/trade: {(p.max_risk_per_trade * 100).toFixed(1)}%</li>
                  <li>max total exp: {(p.max_total_exposure * 100).toFixed(0)}%</li>
                  <li>max open: {p.max_open_positions ?? "∞"}</li>
                  <li>daily cap: {p.max_daily_trades ?? "no cap"}</li>
                </ul>
              </div>
            ))}
          </div>
        </details>
      ) : null}
    </div>
  );
}
