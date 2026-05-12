"use client";

// A-T0 strict cutover marker panel (Phase A 2026-05-11).
// Shows current cutover_at + trades_after_cutover. Button to mark a new cutover
// (with confirm dialog — overwrites previous, paper_capital NOT reset by this
// button, separate operator decision).

import { useEffect, useState } from "react";
import { StrictCutoverStatus, getStrictCutoverStatus, postStrictCutover } from "@/lib/api";

export default function StrictCutoverPanel() {
  const [status, setStatus] = useState<StrictCutoverStatus | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [lastAction, setLastAction] = useState<string | null>(null);

  async function load() {
    try {
      const r = await getStrictCutoverStatus();
      setStatus(r);
      setError(null);
    } catch (e: any) {
      setError(e?.message || String(e));
    }
  }

  useEffect(() => {
    load();
    const t = setInterval(load, 5000);
    return () => clearInterval(t);
  }, []);

  async function handleCutover() {
    if (!confirm(
      "Marquer un nouveau STRICT CUTOVER ?\n\n" +
      "Tous les paper trades ouverts AVANT ce moment seront marqués comme " +
      "legacy (non-décisionnels pour Phase B baseline). " +
      "paper_capital n'est PAS reset par ce bouton (faire séparément si besoin)."
    )) return;
    setBusy(true);
    setLastAction(null);
    try {
      const r = await postStrictCutover();
      setLastAction(`Cutover marked: ${r.strict_cutover_at} (capital=${r.paper_capital_preserved}€)`);
      await load();
    } catch (e: any) {
      setLastAction(`ERROR: ${e?.message || String(e)}`);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="rounded-lg border border-amber-500/30 bg-amber-500/5 p-4">
      <div className="mb-3 flex items-center justify-between">
        <div>
          <h2 className="text-sm font-semibold text-amber-200">A-T0 — Strict Cutover</h2>
          <p className="text-xs text-amber-200/60">
            Marqueur baseline post-PAPER_LIVE_STRICT. Filtre paper_pnl sur trades opened ≥ cutover.
          </p>
        </div>
        <button
          onClick={handleCutover}
          disabled={busy}
          className="rounded bg-amber-500/20 px-3 py-1 text-xs font-semibold text-amber-100 hover:bg-amber-500/30 disabled:opacity-50"
        >
          {busy ? "..." : "MARK CUTOVER NOW"}
        </button>
      </div>

      {error && <p className="text-xs text-red-400">Error: {error}</p>}

      {status && (
        <div className="grid grid-cols-3 gap-3">
          <div>
            <p className="text-xs text-zinc-500">cutover_at</p>
            <p className={`text-xs font-mono ${status.strict_cutover_at ? "text-zinc-100" : "text-zinc-600"}`}>
              {status.strict_cutover_at ?? "(none — pre-cutover mode)"}
            </p>
          </div>
          <div>
            <p className="text-xs text-zinc-500">Trades post-cutover</p>
            <p className="text-lg font-mono text-zinc-100">{status.trades_after_cutover}</p>
          </div>
          {status.paper_capital !== undefined && (
            <div>
              <p className="text-xs text-zinc-500">paper_capital</p>
              <p className="text-lg font-mono text-zinc-100">{status.paper_capital}€</p>
            </div>
          )}
        </div>
      )}

      {lastAction && (
        <p className={`mt-2 text-xs ${
          lastAction.startsWith("ERROR") ? "text-red-400" : "text-emerald-400"
        }`}>
          {lastAction}
        </p>
      )}
    </div>
  );
}
