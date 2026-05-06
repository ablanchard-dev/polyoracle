"use client";

import { useEffect, useState } from "react";
import { UniverseSummary, getUniverseLatest, mergeUniverse } from "@/lib/api";

const tierTone = "text-emerald-300";

export function UniversePanel() {
  const [summary, setSummary] = useState<UniverseSummary | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function load() {
    try {
      const next = await getUniverseLatest();
      setSummary(next);
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load universe summary");
    }
  }

  useEffect(() => {
    load();
  }, []);

  async function rebuild() {
    setBusy(true);
    try {
      const next = await mergeUniverse();
      setSummary(next);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Universe merge failed");
    } finally {
      setBusy(false);
    }
  }

  const ready = summary && summary.total_entries !== undefined && summary.total_entries > 0;
  return (
    <div className="rounded border border-emerald-500/40 bg-panel p-4">
      <div className="mb-3 flex flex-wrap items-center justify-between gap-3">
        <div>
          <h2 className="text-lg font-semibold">Validated Paper Universe (v0.5.4)</h2>
          <p className="text-xs text-slate-400">
            Single source of truth for "wallets the bot is allowed to copy". Merges 730d + 1095d OOS-validated runs and excludes outliers, biased samples, failed validations and candidates.
          </p>
        </div>
        <button
          onClick={rebuild}
          disabled={busy}
          className="rounded border border-emerald-400 bg-emerald-400/15 px-4 py-2 text-sm font-medium text-emerald-200 transition hover:bg-emerald-400/25 disabled:opacity-60"
        >
          {busy ? "Merging..." : "Rebuild from validation reports"}
        </button>
      </div>
      {error ? <div className="mb-3 rounded border border-danger bg-danger/10 p-2 text-xs text-red-100">{error}</div> : null}
      {!ready ? (
        <div className="text-sm text-slate-500">{summary?.message ?? "No universe built yet."}</div>
      ) : (
        <>
          <div className="grid gap-4 md:grid-cols-2 lg:grid-cols-4">
            <Stat label="Sources" value={(summary.sources ?? []).join(" + ") || "—"} />
            <Stat label="Total entries" value={`${summary.total_entries ?? 0}`} tone={tierTone} />
            <Stat label="ELITE" value={`${summary.elite_count ?? 0}`} tone="text-accent" />
            <Stat label="STRONG" value={`${summary.strong_count ?? 0}`} tone="text-emerald-300" />
            <Stat label="Allowed SAFE" value={`${summary.allowed_safe_count ?? 0}`} />
            <Stat label="Allowed AGGRESSIVE" value={`${summary.allowed_aggressive_count ?? 0}`} />
            <Stat label="Allowed FULL_PAPER" value={`${summary.allowed_full_paper_count ?? 0}`} />
            <Stat label="Excluded total" value={`${(summary.excluded_outlier_count ?? 0) + (summary.excluded_biased_count ?? 0) + (summary.excluded_failed_count ?? 0) + (summary.excluded_candidate_count ?? 0)}`} tone="text-warning" />
          </div>
          <div className="mt-3 grid gap-1 text-xs text-slate-400 md:grid-cols-2">
            <div>OUTLIER_FLAGGED excluded : <span className="font-mono text-amber-300">{summary.excluded_outlier_count ?? 0}</span></div>
            <div>BIASED_SAMPLE excluded : <span className="font-mono text-amber-300">{summary.excluded_biased_count ?? 0}</span></div>
            <div>FAILED_VALIDATION excluded : <span className="font-mono text-amber-300">{summary.excluded_failed_count ?? 0}</span></div>
            <div>CANDIDATE_ELITE excluded : <span className="font-mono text-slate-300">{summary.excluded_candidate_count ?? 0}</span></div>
          </div>
          <div className="mt-3 text-xs text-slate-500">
            generated_at: <span className="font-mono">{summary.generated_at ?? "—"}</span>
            <br />
            csv: <span className="font-mono">{summary.csv_path ?? "—"}</span>
            <br />
            latest alias: <span className="font-mono">{summary.latest_csv_path ?? "—"}</span>
          </div>
          <div className="mt-3 rounded border border-amber-500/40 bg-amber-500/10 p-2 text-xs text-amber-100">
            Paper 7d run status: <strong>not started</strong>. Awaiting operator signal — do not auto-launch.
          </div>
        </>
      )}
    </div>
  );
}

function Stat({ label, value, tone }: { label: string; value: string; tone?: string }) {
  return (
    <div className="rounded border border-line bg-slate-900 p-3">
      <div className="text-xs uppercase tracking-wide text-slate-400">{label}</div>
      <div className={`mt-1 text-lg font-semibold ${tone ?? "text-slate-100"}`}>{value}</div>
    </div>
  );
}
