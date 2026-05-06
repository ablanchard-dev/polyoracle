"""v0.5.6 Phase B — Adversarial / model-selection-bias filter.

Two checks at signal time:

1. ``market_similarity_score(market, wallet)``: how similar is the
   incoming market to the markets the wallet historically had edge on?
   Computed as the Jaccard-like overlap of market metadata (category,
   slug-token bag) with the wallet's positive-EV history.

2. ``copy_allowed_if_market_matches_edge(market, wallet, threshold)``:
   binary gate combining (a) above + the wallet's category EV being
   positive on this market's category.

Used by the (paper-only) signal engine to prevent us copying a wallet
into bets that look nothing like the bets where its edge was measured.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class SimilarityVerdict:
    similarity_score: float  # 0..1
    category_match: bool
    slug_overlap: float
    rationale: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "similarity_score": self.similarity_score,
            "category_match": self.category_match,
            "slug_overlap": self.slug_overlap,
            "rationale": self.rationale,
        }


def _slug_tokens(slug: str | None) -> set[str]:
    if not slug:
        return set()
    return {t for t in slug.lower().replace("_", "-").split("-") if t and len(t) >= 3}


def market_similarity_score(
    market_category: str | None,
    market_slug: str | None,
    wallet_category_edge_map: dict[str, dict[str, Any]] | None,
    wallet_slug_history: list[str] | None,
) -> SimilarityVerdict:
    cat = (market_category or "").strip() or "Uncategorised"
    cat_info = (wallet_category_edge_map or {}).get(cat)
    cat_match = cat_info is not None and (cat_info.get("ev") or 0) > 0

    market_tokens = _slug_tokens(market_slug)
    if not market_tokens or not wallet_slug_history:
        slug_overlap = 0.0
    else:
        history_tokens: set[str] = set()
        for slug in wallet_slug_history:
            history_tokens |= _slug_tokens(slug)
        if not history_tokens:
            slug_overlap = 0.0
        else:
            inter = market_tokens & history_tokens
            union = market_tokens | history_tokens
            slug_overlap = len(inter) / len(union) if union else 0.0

    similarity = 0.5 * (1.0 if cat_match else 0.0) + 0.5 * min(1.0, slug_overlap * 4.0)
    rationale = "OK" if similarity >= 0.5 else "LOW_SIMILARITY"
    return SimilarityVerdict(
        similarity_score=round(similarity, 4),
        category_match=cat_match,
        slug_overlap=round(slug_overlap, 4),
        rationale=rationale,
    )


def copy_allowed_if_market_matches_edge(
    market_category: str | None,
    market_slug: str | None,
    wallet_category_edge_map: dict[str, dict[str, Any]] | None,
    wallet_slug_history: list[str] | None,
    *,
    similarity_threshold: float = 0.5,
) -> tuple[bool, SimilarityVerdict]:
    verdict = market_similarity_score(
        market_category,
        market_slug,
        wallet_category_edge_map,
        wallet_slug_history,
    )
    return verdict.similarity_score >= similarity_threshold, verdict
