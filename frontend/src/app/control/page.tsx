"use client";

import { Pause, Play, RefreshCw, Square, Zap } from "lucide-react";
import { useEffect, useState } from "react";
import { ControlButton } from "@/components/ControlButton";
import { CapitalTierPanel } from "@/components/CapitalTierPanel";
import StrictCutoverPanel from "@/components/StrictCutoverPanel";
import {
  BotLoopStatus,
  BotStatus,
  CycleSummary,
  PollingStatus,
  PromotionCandidatesResponse,
  ReclassResult,
  getBotLoopStatus,
  getBotStatus,
  getNoTradeLog,
  getPollingStatus,
  getPromotionCandidates,
  getSignalDecisions,
  postBotAction,
  postBotMode,
  runAuditOnce,
  runReclass,
  startAudit,
  startPolling,
  stopAudit,
  stopPolling
} from "@/lib/api";

export default function ControlRoomPage() {
  const [status, setStatus] = useState<BotStatus | null>(null);
  const [loopStatus, setLoopStatus] = useState<BotLoopStatus | null>(null);
  const [polling, setPolling] = useState<PollingStatus | null>(null);
  const [decisions, setDecisions] = useState<Record<string, number>>({});
  const [noTradeLog, setNoTradeLog] = useState<Array<{ reason_code: string; created_at: string; market_id: string | null; signal_id: string | null; details: string | null }>>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  // v0.7.8 P6 — manual weekly reclass
  const [reclassResult, setReclassResult] = useState<ReclassResult | null>(null);
  const [reclassBusy, setReclassBusy] = useState(false);
  const [reclassError, setReclassError] = useState<string | null>(null);
  const [promotionCandidates, setPromotionCandidates] = useState<PromotionCandidatesResponse | null>(null);

  async function load() {
    try {
      const [s, l, p, d, log] = await Promise.all([
        getBotStatus(),
        getBotLoopStatus(),
        getPollingStatus().catch(() => null),
        getSignalDecisions(),
        getNoTradeLog(20)
      ]);
      setStatus(s);
      setLoopStatus(l);
      setPolling(p);
      setDecisions(d);
      setNoTradeLog(log);
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "load failed");
    }
  }

  useEffect(() => {
    load();
    const id = window.setInterval(load, 5_000);
    return () => window.clearInterval(id);
  }, []);

  async function call<T>(fn: () => Promise<T>, msg: string) {
    setBusy(true);
    setMessage(null);
    try {
      await fn();
      setMessage(msg);
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : "action failed");
    } finally {
      setBusy(false);
    }
  }

  const summary: CycleSummary | null =
    loopStatus?.last_cycle_summary && Object.keys(loopStatus.last_cycle_summary).length > 0
      ? (loopStatus.last_cycle_summary as CycleSummary)
      : null;

  async function handleReclass(dryRun: boolean) {
    if (!dryRun) {
      if (!confirm("Reclass NON-dry-run : modifie les candidate_status en DB de façon permanente (621 ELITE / 821 STRONG actuels).\n\nConfirmer ?")) return;
    }
    setReclassBusy(true);
    setReclassError(null);
    try {
      const r = await runReclass(dryRun);
      setReclassResult(r);
      // refresh promotion candidates after reclass
      try {
        const pc = await getPromotionCandidates();
        setPromotionCandidates(pc);
      } catch {}
    } catch (err) {
      setReclassError(err instanceof Error ? err.message : "reclass failed");
    } finally {
      setReclassBusy(false);
    }
  }

  async function handleLoadPromotionCandidates() {
    try {
      const pc = await getPromotionCandidates();
      setPromotionCandidates(pc);
    } catch (err) {
      setReclassError(err instanceof Error ? err.message : "promotion candidates fetch failed");
    }
  }

  return (
    <main className="mx-auto max-w-7xl px-5 py-6">
      <header className="mb-6 flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="text-2xl font-semibold">Control Room</h1>
          <p className="text-sm text-slate-400">Mode, kill switch, one-cycle bot loop runner, last-cycle metrics and rejection breakdown.</p>
        </div>
        <div className="flex flex-wrap gap-3">
          <ControlButton label="START" icon={Play} disabled={busy} onClick={() => call(() => postBotAction("start"), "bot started")} />
          <ControlButton label="PAUSE" icon={Pause} disabled={busy} onClick={() => call(() => postBotAction("pause"), "bot paused")} />
          <ControlButton label="STOP" icon={Square} disabled={busy} onClick={() => {
            if (!confirm("STOP : interrompt le polling. Positions ouvertes non fermées.\n\nConfirmer ?")) return;
            call(() => postBotAction("stop"), "bot stopped");
          }} />
          <ControlButton label="KILL SWITCH" icon={Zap} tone="danger" disabled={busy} onClick={() => {
            if (!confirm("KILL SWITCH: This will flatten ALL open paper positions immediately. Continue?")) return;
            call(() => postBotAction("kill-switch"), "kill switch activated");
          }} />
        </div>
      </header>

      {error ? <div className="mb-4 rounded border border-danger bg-danger/10 p-3 text-sm text-red-100">{error}</div> : null}
      {message ? <div className="mb-4 rounded border border-line bg-slate-900 p-3 text-sm text-accent">{message}</div> : null}

      <section className="grid gap-4 md:grid-cols-2 lg:grid-cols-3">
        <Stat label="Bot Mode" value={status?.mode ?? "—"} />
        <Stat label="Live" value={status?.live_enabled ? "ENABLED" : "BLOCKED"} tone={status?.live_enabled ? "danger" : "good"} />
        <Stat label="Kill Switch" value={status?.kill_switch_active ? "ACTIVE" : "OFF"} tone={status?.kill_switch_active ? "danger" : "good"} />
        <Stat label="Loop running" value={loopStatus?.running ? "YES" : "NO"} tone={loopStatus?.running ? "good" : "warn"} />
        <Stat label="Cycles" value={`${loopStatus?.cycles_count ?? 0}`} />
        <Stat label="Errors" value={`${loopStatus?.errors_count ?? 0}`} tone={(loopStatus?.errors_count ?? 0) > 0 ? "warn" : "good"} />
        <Stat label="Markets scanned (total)" value={`${loopStatus?.markets_scanned ?? 0}`} />
        <Stat label="Wallets audited (total)" value={`${loopStatus?.wallets_audited ?? 0}`} />
        <Stat label="Trades audited (total)" value={`${loopStatus?.trades_audited ?? 0}`} />
        <Stat label="Signals (total)" value={`${loopStatus?.signals_generated ?? 0}`} />
        <Stat label="Paper trades (total)" value={`${loopStatus?.paper_trades_opened ?? 0}`} />
        <Stat label="Rejected (total)" value={`${loopStatus?.rejected_signals ?? 0}`} />
      </section>

      <section className="mt-6">
        <CapitalTierPanel />
      </section>

      <section className="mt-6">
        <StrictCutoverPanel />
      </section>

      <section className="mt-6 rounded border border-line bg-panel p-4">
        <div className="mb-3 flex items-center justify-between">
          <h2 className="text-lg font-semibold">Wallet polling</h2>
          <span className={`rounded px-2 py-1 text-xs font-mono ${polling?.running ? "bg-accent/15 text-accent" : "bg-slate-800 text-slate-400"}`}>
            {polling?.running ? "RUNNING" : "STOPPED"}
          </span>
        </div>
        <div className="mb-3 grid gap-3 text-sm md:grid-cols-3 lg:grid-cols-6">
          <Stat label="Pool size" value={`${polling?.cohort_size ?? 0}`} />
          <Stat label="Interval (s)" value={`${polling?.interval_seconds ?? 0}`} />
          <Stat label="Rate cap (calls/s)" value={`${polling?.rate_limit_calls_per_sec ?? 0}`} />
          <Stat label="Trades detected" value={`${polling?.trades_detected_total ?? 0}`} tone="good" />
          <Stat label="Paper trades opened" value={`${polling?.paper_trades_opened ?? 0}`} tone="good" />
          <Stat label="Errors" value={`${polling?.polling_errors ?? 0}`} tone={(polling?.polling_errors ?? 0) > 0 ? "warn" : "good"} />
        </div>
        <div className="flex flex-wrap gap-3">
          <button onClick={() => call(startPolling, "polling started")} disabled={busy || polling?.running} className="rounded border border-accent px-4 py-2 text-sm text-accent hover:bg-accent/15 disabled:opacity-40">Start polling</button>
          <button onClick={() => call(stopPolling, "polling stopped")} disabled={busy || !polling?.running} className="rounded border border-line px-4 py-2 text-sm hover:bg-slate-800 disabled:opacity-40">Stop polling</button>
          <span className="ml-auto text-xs text-slate-500">
            last poll: {polling?.last_poll_at ?? "—"} · started: {polling?.started_at ?? "—"}
          </span>
        </div>
      </section>

      <section className="mt-6 grid gap-4 md:grid-cols-2">
        <div className="rounded border border-line bg-panel p-4">
          <h2 className="mb-3 text-lg font-semibold">Bot mode</h2>
          <div className="flex flex-wrap gap-3">
            <button onClick={() => call(() => postBotMode("research"), "mode RESEARCH")} disabled={busy} className="rounded border border-line px-4 py-2 text-sm hover:bg-slate-800">Research</button>
            <button onClick={() => call(() => postBotMode("paper"), "mode PAPER")} disabled={busy} className="rounded border border-accent px-4 py-2 text-sm text-accent hover:bg-accent/15">Paper</button>
            <button onClick={() => call(() => postBotMode("off"), "mode OFF")} disabled={busy} className="rounded border border-line px-4 py-2 text-sm hover:bg-slate-800">Off</button>
          </div>
        </div>
        <div className="rounded border border-line bg-panel p-4">
          <h2 className="mb-3 text-lg font-semibold">Audit loop</h2>
          <div className="flex flex-wrap gap-3">
            <button onClick={() => call(startAudit, "audit started")} disabled={busy} className="rounded border border-line px-4 py-2 text-sm hover:bg-slate-800">Start loop</button>
            <button onClick={() => call(stopAudit, "audit stopped")} disabled={busy} className="rounded border border-line px-4 py-2 text-sm hover:bg-slate-800">Stop loop</button>
            <button onClick={() => call(runAuditOnce, "ran one cycle")} disabled={busy} className="flex items-center gap-2 rounded border border-accent px-4 py-2 text-sm text-accent hover:bg-accent/15">
              <RefreshCw size={14} /> Run once
            </button>
          </div>
          {loopStatus?.last_error ? <div className="mt-3 rounded border border-warning bg-warning/10 p-2 text-xs text-amber-200">last error: {loopStatus.last_error}</div> : null}
          {loopStatus?.last_cycle_at ? <div className="mt-3 text-xs text-slate-400">Last cycle: {loopStatus.last_cycle_at} ({loopStatus.last_cycle_duration_ms.toFixed(0)} ms)</div> : null}
        </div>
      </section>

      <section className="mt-6 grid gap-4 lg:grid-cols-3">
        <div className="rounded border border-line bg-panel p-4">
          <h2 className="mb-3 text-lg font-semibold">Last cycle</h2>
          {!summary ? <div className="text-sm text-slate-500">No cycle yet — click Run once.</div> : (
            <ul className="space-y-1 text-sm text-slate-200">
              <li>Markets scanned: <strong>{summary.markets_scanned}</strong></li>
              <li>Wallets audited: <strong>{summary.wallets_audited}</strong></li>
              <li>Trades audited: <strong>{summary.trades_audited}</strong></li>
              <li>Signals generated: <strong>{summary.signals_generated}</strong></li>
              <li>Paper trades opened: <strong className="text-accent">{summary.paper_trades_opened}</strong></li>
              <li>Rejected signals: <strong className="text-warning">{summary.rejected_signals}</strong></li>
              <li>Duration: <strong>{summary.duration_ms.toFixed(0)} ms</strong></li>
              {summary.last_error ? <li className="text-danger">Error: {summary.last_error}</li> : null}
            </ul>
          )}
        </div>

        <div className="rounded border border-line bg-panel p-4">
          <h2 className="mb-3 text-lg font-semibold">Rejection reasons (last cycle)</h2>
          {summary && Object.keys(summary.rejection_reasons ?? {}).length > 0 ? (
            <ul className="space-y-1 text-sm">
              {Object.entries(summary.rejection_reasons ?? {})
                .sort((a, b) => b[1] - a[1])
                .map(([code, count]) => (
                  <li key={code} className="flex items-center justify-between rounded border border-line bg-slate-900 px-3 py-1">
                    <span className="font-mono text-xs text-amber-300">{code}</span>
                    <span className="font-semibold">{count}</span>
                  </li>
                ))}
            </ul>
          ) : (
            <div className="text-sm text-slate-500">No rejections yet.</div>
          )}
        </div>

        <div className="rounded border border-line bg-panel p-4">
          <h2 className="mb-3 text-lg font-semibold">Signal decisions (cumulative)</h2>
          {Object.keys(decisions).length === 0 ? <div className="text-sm text-slate-500">No signals yet.</div> : (
            <ul className="space-y-1 text-sm">
              {Object.entries(decisions)
                .sort((a, b) => b[1] - a[1])
                .map(([code, count]) => (
                  <li key={code} className="flex items-center justify-between rounded border border-line bg-slate-900 px-3 py-1">
                    <span className="font-mono text-xs text-sky-300">{code}</span>
                    <span className="font-semibold">{count}</span>
                  </li>
                ))}
            </ul>
          )}
        </div>
      </section>

      <section className="mt-6 rounded border border-line bg-panel p-4">
        <div className="mb-3 flex items-center justify-between">
          <h2 className="text-lg font-semibold">Cohort reclass (v0.7.8 P6)</h2>
          <span className="text-xs text-slate-500">Manual trigger; weekly cron also runs Sunday 02:00 UTC</span>
        </div>
        <div className="flex flex-wrap gap-3">
          <button
            onClick={() => {
              if (!confirm("Run weekly reclass? This will promote/demote wallets in the cohort. Continue?")) return;
              handleReclass(false);
            }}
            disabled={reclassBusy}
            className="rounded border border-accent px-4 py-2 text-sm text-accent hover:bg-accent/15 disabled:opacity-40"
          >
            {reclassBusy ? "Running…" : "Run weekly reclass"}
          </button>
          <button
            onClick={() => handleReclass(true)}
            disabled={reclassBusy}
            className="rounded border border-line px-4 py-2 text-sm hover:bg-slate-800 disabled:opacity-40"
          >
            Dry run
          </button>
          <button
            onClick={handleLoadPromotionCandidates}
            disabled={reclassBusy}
            className="rounded border border-line px-4 py-2 text-sm hover:bg-slate-800 disabled:opacity-40"
          >
            View promotion candidates
          </button>
        </div>
        {reclassError && (
          <div className="mt-3 rounded border border-danger bg-danger/10 p-2 text-sm text-red-300">
            {reclassError}
          </div>
        )}
        {reclassResult && (
          <div className="mt-3 grid gap-3 text-sm md:grid-cols-3 lg:grid-cols-6">
            <Stat label="Cohort before" value={`${reclassResult.cohort_before}`} />
            <Stat label="Cohort after" value={`${reclassResult.cohort_after}`} tone="good" />
            <Stat label="Promoted" value={`${reclassResult.promoted_count}`} tone="good" />
            <Stat label="Demoted" value={`${reclassResult.demoted_count}`} tone={reclassResult.demoted_count > 0 ? "warn" : "good"} />
            <Stat label="Unchanged" value={`${reclassResult.unchanged}`} />
            <Stat
              label="Rolled back"
              value={reclassResult.rolled_back ? "YES" : "no"}
              tone={reclassResult.rolled_back ? "danger" : "good"}
            />
          </div>
        )}
        {reclassResult?.errors && reclassResult.errors.length > 0 && (
          <div className="mt-3 rounded border border-warning bg-warning/10 p-2 text-xs text-amber-300">
            {reclassResult.errors.join(" · ")}
          </div>
        )}
        {reclassResult && (
          <details className="mt-3 text-xs">
            <summary className="cursor-pointer text-slate-400">Summary markdown</summary>
            <pre className="mt-2 max-h-64 overflow-auto rounded border border-line bg-slate-950 p-2 font-mono text-xs text-slate-300">
              {reclassResult.summary_md}
            </pre>
          </details>
        )}
        {promotionCandidates && (
          <div className="mt-4">
            <div className="mb-2 text-sm font-semibold">
              Promotion candidates ({promotionCandidates.count})
              <span className="ml-2 text-xs font-normal text-slate-400">
                STRONG wallets ≥70 W+L, ≥0.90 wr — close to ELITE bar
              </span>
            </div>
            {promotionCandidates.candidates.length === 0 ? (
              <div className="text-xs text-slate-500">No promotion candidates currently.</div>
            ) : (
              <ul className="max-h-72 space-y-1 overflow-auto text-xs">
                {promotionCandidates.candidates.slice(0, 30).map((c) => (
                  <li key={c.address} className="flex items-center justify-between rounded border border-line bg-slate-900 px-2 py-1">
                    <span className="font-mono text-slate-300">{c.address.slice(0, 14)}…</span>
                    <span className="text-slate-400">
                      W+L={c.wins_plus_losses} · wr={(c.win_rate * 100).toFixed(1)}% · {c.trades_to_elite} to ELITE
                    </span>
                  </li>
                ))}
              </ul>
            )}
          </div>
        )}
      </section>

      <section className="mt-6 rounded border border-line bg-panel p-4">
        <h2 className="mb-3 text-lg font-semibold">Recent no-trade entries</h2>
        {noTradeLog.length === 0 ? <div className="text-sm text-slate-500">No saved-loss entries yet.</div> : (
          <ul className="space-y-2 text-sm">
            {noTradeLog.slice(0, 10).map((entry, idx) => (
              <li key={`${entry.signal_id}-${idx}`} className="rounded border border-line bg-slate-900 p-2">
                <div className="flex items-center justify-between">
                  <span className="font-mono text-xs text-amber-300">{entry.reason_code}</span>
                  <span className="text-xs text-slate-500">{entry.created_at}</span>
                </div>
                <div className="mt-1 text-xs text-slate-400">market: {entry.market_id ?? "—"} · signal: {entry.signal_id ?? "—"}</div>
                {entry.details ? <div className="mt-1 truncate text-xs text-slate-500">{entry.details}</div> : null}
              </li>
            ))}
          </ul>
        )}
      </section>
    </main>
  );
}

function Stat({ label, value, tone }: { label: string; value: string; tone?: "good" | "danger" | "warn" }) {
  const toneClass = tone === "good" ? "text-accent" : tone === "danger" ? "text-danger" : tone === "warn" ? "text-warning" : "text-slate-100";
  return (
    <div className="rounded border border-line bg-panel p-4">
      <div className="text-xs uppercase tracking-wide text-slate-400">{label}</div>
      <div className={`mt-2 text-2xl font-semibold ${toneClass}`}>{value}</div>
    </div>
  );
}
