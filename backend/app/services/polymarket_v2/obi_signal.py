"""OBI Signal — Order Book Imbalance compute pour Polymarket V2.

Formule : OBI_t = (bid_topN_qty - ask_topN_qty) / (bid_topN_qty + ask_topN_qty)
Source : Cont, Stoikov, Talreja (2014) — "A Stochastic Model for Order Book Dynamics"
Référence Polymarket : OBI > 0.65 prédit direction 15-30min, ~58% accuracy
(cf. la littérature sur l'order-book imbalance / OBI comme signal de flux)

Data source : CLOB API GET https://clob.polymarket.com/book?token_id=<token_id>
Cache 30s par token_id pour éviter de spammer l'API.

Phase B = OBSERVATION : on calcule OBI sur chaque trade cohort qui PASS les
lean_gates, on logue dans events.jsonl. Pas de filtre. Mesure accuracy en
post-hoc via analyzer script (lookahead 15-30min sur price history Gamma).

Output : OBISignal(value: float ∈ [-1,+1], side_aligned: bool, raw_book_age_ms)
"""
from __future__ import annotations

import json
import time
import urllib.request
from dataclasses import dataclass
from typing import Optional


CLOB_BOOK_URL = "https://clob.polymarket.com/book?token_id={token_id}"

# OBI thresholds (sourcés Cont 2014, validés Polymarket Medium ILLUMINATION)
OBI_BULLISH_THRESHOLD = 0.65   # > 0.65 : bid pressure → predict UP
OBI_BEARISH_THRESHOLD = -0.65  # < -0.65 : ask pressure → predict DOWN

# Cache TTL — book change vite mais on évite rate-limit
CACHE_TTL_S = 30.0

# Top N niveaux à sommer pour OBI multi-level (Cont 2014 = top 5)
TOP_N_LEVELS = 5


@dataclass
class OBISignal:
    value: float                # ∈ [-1, +1]
    side_aligned: Optional[bool]  # True si OBI confirme direction du trade source
    raw_book_age_ms: int        # age book server-side
    bullish: bool               # OBI > BULLISH_THRESHOLD
    bearish: bool               # OBI < BEARISH_THRESHOLD
    bid_qty_sum: float
    ask_qty_sum: float
    error: Optional[str] = None


class OBISignalEngine:
    """OBI compute avec cache REST per token_id."""

    def __init__(self, top_n: int = TOP_N_LEVELS, cache_ttl_s: float = CACHE_TTL_S,
                 timeout_s: float = 3.0):
        self.top_n = top_n
        self.cache_ttl_s = cache_ttl_s
        self.timeout_s = timeout_s
        # token_id -> (OBISignal, fetched_at_ts)
        self._cache: dict[str, tuple[OBISignal, float]] = {}
        # Stats
        self.stats: dict[str, int] = dict(
            queries=0, cache_hits=0, api_calls=0, api_errors=0,
            bullish=0, bearish=0, neutral=0,
        )

    def _fetch_book(self, token_id: str) -> Optional[dict]:
        """REST GET CLOB book. Retourne dict ou None si erreur."""
        url = CLOB_BOOK_URL.format(token_id=token_id)
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": "PolyV2-OBI/1.0",
                "Accept": "application/json",
            })
            with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
                return json.loads(resp.read())
        except Exception:
            self.stats["api_errors"] += 1
            return None

    def _compute_obi(self, book: dict) -> tuple[float, float, float]:
        """Returns (obi, bid_qty_sum, ask_qty_sum). top_n niveaux chaque côté."""
        bids = book.get("bids") or []
        asks = book.get("asks") or []
        # Top N par côté (book renvoyé déjà trié par price desc/asc selon API)
        bid_qty = 0.0
        for b in bids[:self.top_n]:
            try: bid_qty += float(b.get("size", 0))
            except (TypeError, ValueError): pass
        ask_qty = 0.0
        for a in asks[:self.top_n]:
            try: ask_qty += float(a.get("size", 0))
            except (TypeError, ValueError): pass
        denom = bid_qty + ask_qty
        if denom <= 0:
            return 0.0, bid_qty, ask_qty
        return (bid_qty - ask_qty) / denom, bid_qty, ask_qty

    def compute(self, token_id: str, side: str) -> OBISignal:
        """Compute OBI pour token, retourne OBISignal annoté avec side_aligned.

        side : "BUY" → bullish OBI confirme. "SELL" → bearish OBI confirme.
        """
        self.stats["queries"] += 1
        now = time.time()
        # Cache check
        cached = self._cache.get(token_id)
        if cached and (now - cached[1]) < self.cache_ttl_s:
            self.stats["cache_hits"] += 1
            sig = cached[0]
            # Re-annoter side_aligned car le side peut différer entre 2 lookups
            side_aligned = self._compute_side_aligned(sig.value, side)
            return OBISignal(
                value=sig.value, side_aligned=side_aligned,
                raw_book_age_ms=sig.raw_book_age_ms,
                bullish=sig.bullish, bearish=sig.bearish,
                bid_qty_sum=sig.bid_qty_sum, ask_qty_sum=sig.ask_qty_sum,
                error=sig.error,
            )

        # API fetch
        self.stats["api_calls"] += 1
        book = self._fetch_book(token_id)
        if book is None:
            sig = OBISignal(
                value=0.0, side_aligned=None, raw_book_age_ms=-1,
                bullish=False, bearish=False, bid_qty_sum=0, ask_qty_sum=0,
                error="api_unavailable",
            )
            return sig

        # Book age — auto-detect sec vs ms.
        # Fix 2026-05-26 : seuil 1e11 (= mars 1973 en ms) garantit qu'on
        # promeut s→ms uniquement pour des timestamps en seconde modernes.
        # 1e12 (ancien seuil) valait ~2001 en ms et fonctionnait par hasard
        # en 2026 ; échouerait silencieusement si Polymarket change format.
        try:
            ts_str = book.get("timestamp", "0")
            ts_int = int(ts_str)
            if ts_int > 0 and ts_int < 10**11:
                # < 1973 en ms → c'est en secondes, promote
                ts_int *= 1000
            book_age_ms = int(now * 1000) - ts_int if ts_int > 0 else -1
        except (TypeError, ValueError):
            book_age_ms = -1

        obi_val, bid_qty, ask_qty = self._compute_obi(book)
        bullish = obi_val > OBI_BULLISH_THRESHOLD
        bearish = obi_val < OBI_BEARISH_THRESHOLD
        if bullish:
            self.stats["bullish"] += 1
        elif bearish:
            self.stats["bearish"] += 1
        else:
            self.stats["neutral"] += 1

        side_aligned = self._compute_side_aligned(obi_val, side)

        sig = OBISignal(
            value=obi_val, side_aligned=side_aligned,
            raw_book_age_ms=book_age_ms,
            bullish=bullish, bearish=bearish,
            bid_qty_sum=bid_qty, ask_qty_sum=ask_qty,
        )
        self._cache[token_id] = (sig, now)
        # Cleanup cache (bounded)
        if len(self._cache) > 500:
            # Drop oldest
            oldest = min(self._cache.items(), key=lambda x: x[1][1])
            del self._cache[oldest[0]]
        return sig

    @staticmethod
    def _compute_side_aligned(obi: float, side: str) -> Optional[bool]:
        """True si OBI confirme la direction du side du trade.
        BUY = wallet long YES → bullish OBI confirme (les bids sont chargés
        sur ce token).
        SELL = wallet sort/short → bearish OBI confirme.
        """
        s = (side or "").upper()
        if s == "BUY":
            if obi > OBI_BULLISH_THRESHOLD: return True
            if obi < OBI_BEARISH_THRESHOLD: return False
            return None  # neutral
        if s == "SELL":
            if obi < OBI_BEARISH_THRESHOLD: return True
            if obi > OBI_BULLISH_THRESHOLD: return False
            return None
        return None

    def report(self) -> dict:
        q = self.stats["queries"] or 1
        return {
            **self.stats,
            "cache_hit_rate_pct": 100.0 * self.stats["cache_hits"] / q,
            "cache_size": len(self._cache),
        }


# ─── CLI smoke test ──────────────────────────────────────────────────
if __name__ == "__main__":
    """Smoke : fetch OBI sur un token de marché actif."""
    import sys
    # Test token_id : 1er trade visible sur RTDS
    test_token = sys.argv[1] if len(sys.argv) > 1 else None
    if not test_token:
        print("Usage: python -m app.services.polymarket_v2.obi_signal <token_id>")
        sys.exit(1)
    eng = OBISignalEngine()
    sig = eng.compute(test_token, side="BUY")
    print(f"OBI = {sig.value:+.3f}")
    print(f"  bid_qty(top5)={sig.bid_qty_sum:.1f}  ask_qty(top5)={sig.ask_qty_sum:.1f}")
    print(f"  bullish={sig.bullish}  bearish={sig.bearish}")
    print(f"  side_aligned(BUY)={sig.side_aligned}")
    print(f"  book_age_ms={sig.raw_book_age_ms}")
    print(f"Report: {eng.report()}")
