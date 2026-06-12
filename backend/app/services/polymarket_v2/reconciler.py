"""PnL Reconciler — résout les fills Route A/B copiés contre la résolution réelle.

paper=live faithful :
  - Le settlement d'un binaire Polymarket est DÉTERMINISTE : un BUY filled à
    price p, tenu jusqu'à résolution, paie 1/p−1 par $ si l'outcome gagne, −1
    sinon. Identique paper et live (pas de mid-price, pas d'optimisme).
  - La résolution vient de la VRAIE résolution marché (resolvedmarketrecord, ou
    Gamma outcomePrices≥0.999 via MarketResolutionScanner) — jamais d'estimation.
  - Le seul écart paper/live RESTANT est en amont : la simulation de fill maker
    (Bernoulli) — c.-à-d. *si* et *à quel prix* on aurait fillé. Le reconciler
    calcule le PnL CONDITIONNEL au fill ; il n'invente aucun prix.

Modèle BUY-only (cohérent avec le re-audit validé qui ne comptait que les BUY).
Les SELL fills sont enregistrés mais non-PnL (status resolved, pnl=None).
Positions persistées (positions.jsonl) → survit aux restarts (les marchés
non-crypto résolvent en heures/jours).
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Optional


class PnLReconciler:
    def __init__(self, positions_path, gamma_client=None, scanner=None,
                 db_path: Optional[str] = None):
        self.positions_path = Path(positions_path)
        self.gamma = gamma_client
        self.scanner = scanner
        self.db_path = db_path
        self.positions: list[dict] = []
        self._resolution_cache: dict[str, str] = {}  # cid -> winner (positive only)
        self._load()

    def _load(self):
        if self.positions_path.exists():
            try:
                for line in self.positions_path.read_text().splitlines():
                    line = line.strip()
                    if line:
                        self.positions.append(json.loads(line))
            except Exception:
                pass

    def _persist(self):
        try:
            tmp = self.positions_path.with_suffix(".jsonl.tmp")
            tmp.write_text("\n".join(json.dumps(p) for p in self.positions) + "\n")
            tmp.replace(self.positions_path)
        except Exception:
            pass

    def record_fill(self, *, route, wallet, condition_id, token_id, outcome,
                    side, entry_price, size, notional, ts, fee=0.0):
        try:
            ep = float(entry_price)
        except Exception:
            return
        if ep <= 0:
            return
        try:
            fee_v = max(0.0, float(fee))
        except Exception:
            fee_v = 0.0
        self.positions.append({
            "ts": ts, "route": route, "wallet": wallet,
            "condition_id": condition_id, "token_id": token_id,
            "outcome": (outcome or "").strip().lower(),
            "side": (side or "").upper(),
            "entry_price": ep, "size": float(size), "notional": float(notional),
            "fee": fee_v,  # taker fee USDC, paid win or lose (0 for maker)
            "status": "open", "won": None, "pnl": None,
        })
        self._persist()

    def _lookup_resolution(self, cid: str) -> Optional[str]:
        """Winning outcome name (lower) si résolu, sinon None. DB puis Gamma."""
        if not cid:
            return None
        if cid in self._resolution_cache:
            return self._resolution_cache[cid]
        winner = None
        if self.db_path:
            try:
                c = sqlite3.connect("file:%s?mode=ro" % self.db_path, uri=True, timeout=10)
                row = c.execute(
                    "SELECT winning_outcome_name FROM resolvedmarketrecord WHERE condition_id=?",
                    (cid,)).fetchone()
                c.close()
                if row and row[0]:
                    winner = row[0].strip().lower()
            except Exception:
                pass
        if winner is None and self.gamma is not None and self.scanner is not None:
            try:
                raw = self.gamma.fetch_market_by_condition(cid)
                if raw:
                    _, name = self.scanner.extract_winning_outcome(raw)
                    if name:
                        winner = name.strip().lower()
            except Exception:
                pass
        if winner is not None:
            self._resolution_cache[cid] = winner
        return winner

    def resolve_pending(self, max_lookups: int = 60) -> dict:
        """Résout les marchés des positions ouvertes (cap max_lookups Gamma/cycle),
        puis ferme les positions dont le marché a résolu. Retourne un résumé."""
        seen = set()
        open_cids = []
        for p in self.positions:
            cid = p.get("condition_id")
            if (p["status"] == "open" and cid and cid not in seen
                    and cid not in self._resolution_cache):
                seen.add(cid)
                open_cids.append(cid)
        n_new = 0
        for cid in open_cids[:max_lookups]:
            if self._lookup_resolution(cid) is not None:
                n_new += 1
        closed = 0
        for p in self.positions:
            if p["status"] != "open":
                continue
            w = self._resolution_cache.get(p.get("condition_id"))
            if w is None:
                continue
            if p["side"] == "BUY":
                won = (p["outcome"] == w)
                fee = p.get("fee", 0.0) or 0.0  # taker fee paid win or lose
                gross = p["notional"] * (1.0 / p["entry_price"] - 1.0) if won else -p["notional"]
                p["won"] = won
                p["pnl"] = round(gross - fee, 4)
            else:
                p["won"] = None
                p["pnl"] = None  # SELL non modélisé (re-audit BUY-only)
            p["status"] = "resolved"
            closed += 1
        if closed:
            self._persist()
        return {"resolved_new_markets": n_new, "closed_positions": closed,
                "open_markets_pending": len(open_cids)}

    def report(self) -> dict:
        agg: dict[str, dict] = {}
        for p in self.positions:
            rk = p.get("route") or "?"
            a = agg.setdefault(rk, {"open": 0, "resolved": 0, "buy_resolved": 0,
                                    "wins": 0, "pnl": 0.0})
            if p["status"] == "open":
                a["open"] += 1
            else:
                a["resolved"] += 1
                if p.get("pnl") is not None:
                    a["buy_resolved"] += 1
                    a["pnl"] += p["pnl"]
                    if p.get("won"):
                        a["wins"] += 1
        for a in agg.values():
            a["wr"] = round(a["wins"] / a["buy_resolved"], 3) if a["buy_resolved"] else None
            a["pnl"] = round(a["pnl"], 3)
        return agg
