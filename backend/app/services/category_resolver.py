"""Category resolver with fallback chain (Phase A B-fix — 2026-05-11).

Bug fix: at NANO/small capital ($200 USD), `UNKNOWN_CATEGORY` gate rejects
markets without a category. But many Market rows have `category=NULL` even
though the data exists elsewhere (ResolvedMarketRecord, slug, question).
Result: 811/1318 (62%) of paper trades were rejected unnecessarily.

P0.1 (review Round 8 — 2026-05-12): the previous version called
`session.get(ResolvedMarketRecord, market_id)` which uses the primary key
(numeric market_id). When the caller passes a hex `condition_id` (common in
the paper-trade pipeline — `PaperTrade.market_id` stores the hex form), the
lookup silently misses. M1 v2 had the same bug. We now apply the same
fallback used by `copy_efficiency_v3._resolve_market_record()`: try by PK
first, then by `ResolvedMarketRecord.condition_id == market_id`.

This helper provides a SINGLE source-of-truth for category resolution at
runtime, applying a deterministic fallback chain:

  1. Market.category (canonical, if not null/"Unknown"/"Uncategorised")
  2. ResolvedMarketRecord.category — try by PK, then by condition_id
  3. infer_category() on Market.slug / Market.question (= keyword-based
     inference, reuses the existing service)
  4. "Unknown" (legitimate reject — gate keeps small-capital safety)

No bypass, no threshold lowering — just better data resolution.
"""

from __future__ import annotations

from typing import Optional

from sqlmodel import Session, select

from app.services.category_inference import infer_category

# Categories considered "unknown" / not informative (case-insensitive)
_UNKNOWN_LABELS = frozenset({
    "", "unknown", "uncategorised", "uncategorized",
    "uncategorised_markets", "none", "null",
})


def _is_unknown(category: Optional[str]) -> bool:
    """Return True if the category is null/empty/uninformative."""
    if not category:
        return True
    return category.strip().lower() in _UNKNOWN_LABELS


def _resolve_market_record(session: Session, market_id: Optional[str]):
    """Look up a ResolvedMarketRecord by primary key, then by condition_id.

    Why two-step: `PaperTrade.market_id` and the audit pipeline use the hex
    condition_id form, but `ResolvedMarketRecord` is keyed by the numeric
    market_id. A direct `session.get(RMR, hex_id)` returns None silently.
    M1 v2 had this same bug; P0.1 (Round 8) ports the fix here.
    """
    if not market_id:
        return None
    try:
        from app.models.wallet import ResolvedMarketRecord
    except Exception:
        return None
    try:
        rmr = session.get(ResolvedMarketRecord, market_id)
    except Exception:
        rmr = None
    if rmr is not None:
        return rmr
    try:
        return session.exec(
            select(ResolvedMarketRecord).where(
                ResolvedMarketRecord.condition_id == market_id
            )
        ).first()
    except Exception:
        return None


def resolve_category_with_fallback(
    session: Session, market_id: Optional[str], *, hint_category: Optional[str] = None,
) -> tuple[str, str]:
    """Resolve a market category using a 4-level fallback chain.

    Args:
        session: SQLAlchemy session (lazy imports inside to avoid cycles).
        market_id: The market id (Market.id == ResolvedMarketRecord.market_id).
        hint_category: Caller may pass a category they already have (e.g.
            audit_context.category). Used as the FIRST attempt.

    Returns:
        (resolved_category, source) where source is one of:
        "hint", "market_db", "resolved_market_record", "inferred", "unknown".
    """
    # 1. Hint from caller (audit_context.category is typically the start point)
    if not _is_unknown(hint_category):
        return (hint_category, "hint")  # type: ignore[return-value]

    if not market_id:
        return ("Unknown", "unknown")

    # 2. Market.category direct lookup
    try:
        from app.models.market import Market
        market_row = session.get(Market, market_id)
    except Exception:
        market_row = None

    if market_row is not None and not _is_unknown(market_row.category):
        return (market_row.category or "Unknown", "market_db")

    # 3. ResolvedMarketRecord.category — two-step lookup (PK then condition_id)
    #    P0.1: previously used `session.get(RMR, market_id)` only, which missed
    #    hex-form condition_id callers. Now uses `_resolve_market_record()`.
    rmr = _resolve_market_record(session, market_id)

    if rmr is not None and not _is_unknown(rmr.category):
        return (rmr.category or "Unknown", "resolved_market_record")

    # 4. Inference from slug/question (reuse existing service)
    slug = getattr(market_row, "raw_slug", None) if market_row else None
    question = getattr(market_row, "question", None) if market_row else None
    if not question and rmr is not None:
        question = getattr(rmr, "question", None)
    if slug or question:
        inferred = infer_category(
            gamma_category=None,
            slug=slug,
            question=question,
        )
        if inferred != "Unknown":
            return (inferred, "inferred")

    return ("Unknown", "unknown")
