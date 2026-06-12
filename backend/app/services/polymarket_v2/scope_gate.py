"""Scope gate R4 — restriction post-audit R1 V2 (2026-05-27).

Audit R1 V2 a établi :
  - OBI directional 65.5% accuracy crypto (CI lo 59.1%, N=229).
  - OBI directional 66.1% accuracy crypto × p_90_100 (CI lo 58.8%, N=174).
  - Bayes FAIL_neg massif p_70+ (35-43%) → désactivé.
  - Maker rebate 20% rend break-even crypto possible (51.7% maker vs 72.7% taker).

Conclusion : ne trader QUE le sous-univers où l'edge est prouvé.

Scope :
  - `category ∈ {"crypto", "finance"}` (seules catégories validées).
  - `price ∈ [0.70, 1.00)` (zone OBI ≥ 56% accuracy).
  - `|obi_value| ≥ 0.65` (signal directional only — neutre exclu).

Toute autre combinaison → REJECT pré-gate. Le but est de tester
empiriquement si la conjonction (crypto+highprice+OBI) tient en runtime.

API publique : `scope_check(signal, obi_value, market_category)`.
"""
from __future__ import annotations

from typing import Tuple

from app.services.polymarket_v2.lean_gates import CopySignal


# Catégories prouvées par l'audit R1 V2 — Crypto et Finance only.
# (Tous les autres : Sports/Politics/Geopolitics/Tech/Culture/Weather/
# Mentions/Economics/Other/Unknown → REJECT.)
SCOPE_CATEGORIES = frozenset({"crypto", "finance"})

# Zone de prix où OBI maintient ≥ 56% accuracy (R1 V2 ladder).
# < 0.70 : OBI drift sous 50%. ≥ 1.00 : edge case price clipping.
SCOPE_PRICE_MIN = 0.70
SCOPE_PRICE_MAX = 1.00

# Seuil OBI directional — Cont 2014, sourcé module obi_signal.
# < 0.65 = neutral book, pas de signal de pression ordrebook.
SCOPE_OBI_THRESHOLD = 0.65


def scope_check(
    signal: CopySignal,
    obi_value: float,
    market_category: str,
) -> Tuple[bool, str]:
    """Vérifie si un signal tombe dans le scope R1 V2.

    Args:
        signal: CopySignal candidat (déjà construit par on_rtds).
        obi_value: OBI value [-1, +1]. Passer 0.0 si pas calculable (sera
            traité comme neutral → REJECT SCOPE_OBI_NEUTRAL).
        market_category: catégorie résolue (case-insensitive). Use
            category_resolver.resolve_category_with_fallback en amont.

    Returns:
        (passed: bool, reason: str). reason = "OK" si pass, sinon code
        explicite (SCOPE_CATEGORY / SCOPE_PRICE / SCOPE_OBI_NEUTRAL).
    """
    cat_norm = (market_category or "").strip().lower()
    if cat_norm not in SCOPE_CATEGORIES:
        return False, f"SCOPE_CATEGORY ({cat_norm or 'unknown'})"

    if signal.price < SCOPE_PRICE_MIN or signal.price >= SCOPE_PRICE_MAX:
        return False, f"SCOPE_PRICE ({signal.price:.4f})"

    if abs(obi_value) < SCOPE_OBI_THRESHOLD:
        return False, f"SCOPE_OBI_NEUTRAL ({obi_value:+.2f})"

    return True, "OK"


# ─── CLI smoke test ──────────────────────────────────────────────────
if __name__ == "__main__":
    import time as _t
    from dataclasses import dataclass

    def _mk(price: float):
        return CopySignal(
            trader="0xabc", token_id="42", condition_id="0xmkt",
            side="BUY", notional_usd=10.0, price=price,
            ts_ms=int(_t.time() * 1000),
        )

    cases = [
        # (signal_price, obi, category, expected_pass)
        (0.95, 0.80, "crypto", True),   # ideal
        (0.95, 0.80, "Crypto", True),   # case-insensitive
        (0.95, 0.80, "sports", False),  # wrong category
        (0.50, 0.80, "crypto", False),  # price too low
        (1.05, 0.80, "crypto", False),  # price too high
        (0.95, 0.30, "crypto", False),  # OBI neutral
        (0.95, -0.80, "crypto", True),  # bearish OBI directional OK
        (0.85, 0.65, "finance", True),  # edge: exactly threshold
        (0.70, 0.66, "finance", True),  # edge: exactly price floor
        (0.99, 0.99, "Unknown", False), # unknown cat
    ]
    print(f"{'price':>6} {'obi':>6} {'cat':>10} {'expected':>8}  result  reason")
    print("-" * 70)
    for px, obi, cat, expected in cases:
        ok, reason = scope_check(_mk(px), obi, cat)
        verdict = "OK" if ok == expected else "FAIL"
        print(f"{px:>6.2f} {obi:>+6.2f} {cat:>10} {str(expected):>8}  {verdict}    {reason}")
