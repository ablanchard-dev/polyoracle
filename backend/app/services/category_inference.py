"""v0.7.6 B11 — category inference for Polymarket markets.

The gamma API returns ``category`` in some responses but not others (older
markets, certain slugs, conditional markets). This module provides a
deterministic fallback :

1. Use the gamma-supplied category if present.
2. Otherwise, infer from the slug + question text via keyword matching.
3. Otherwise, return "Unknown" — explicit, never silent.

Result is one of: Crypto, Sports, Finance, Politics, Economics, Culture,
Weather, Mentions, Tech, Geopolitics, Other, Unknown. The naming
matches B12's ``FEE_RATES`` keys (lowercased).
"""

from __future__ import annotations

import re
from typing import Optional

# Keyword -> canonical category. Order matters: first match wins.
# Multi-word patterns match anywhere in the text via simple substring.
CATEGORY_KEYWORDS: list[tuple[str, list[str]]] = [
    ("Crypto", [
        "bitcoin", "btc", "ethereum", "eth ", "ethereum-",
        "crypto", "solana", "sol-up", "sol-down", "doge",
        "memecoin", "memecoins", "stablecoin", "altcoin",
        "ether", "blockchain", "nft", "bnb",
        "xrp", "cardano", "ada", "avalanche", "avax",
        "polkadot", "polygon", "matic", "litecoin", "ltc",
        # R1 V2 audit fix (2026-05-27) — 595 condition_ids restaient
        # "unknown" car les slugs Polymarket dynamiques utilisent
        # `<coin>-updown-{5m,15m,4h}-<epoch>` (la version concaténée
        # n'était pas matchée par 'sol-up' alone).
        "btc-updown", "eth-updown", "sol-updown", "xrp-updown",
        "bnb-updown", "doge-updown", "hype-updown", "ada-updown",
        "avax-updown", "sui-updown", "matic-updown", "ltc-updown",
        "shib-updown", "pepe-updown", "wif-updown", "bonk-updown",
        # Form courte question text ("Up or Down")
        "up or down",
        # Hyperliquid (token HYPE)
        "hyperliquid", "hype",
        # Long-form question text patterns
        "bitcoin up", "ethereum up", "solana up",
    ]),
    ("Sports", [
        "nfl", "nba", "mlb", "soccer", "football", "basketball",
        "baseball", "hockey", "nhl", "mma", "ufc", "boxing",
        "tennis", "golf", "f1 ", "formula", "olympics", "world cup",
        "champions league", "playoff", "super bowl", "superbowl",
        "winner", "championship", "o/u ", "over/under",
        "both teams to score", "vs.", "saudi club", "fc ",
        "match", "qualification",
    ]),
    ("Geopolitics", [
        "russia", "ukraine", "putin", "china", "taiwan", "israel",
        "palestine", "gaza", "iran", "north korea", "kim jong",
        "war ", "war-", "nato", "ceasefire", "sanction",
        "election", "primary", "summit", "treaty",
    ]),
    ("Politics", [
        "president", "governor", "senate", "house", "congress",
        "biden", "harris", "trump", "republican", "democrat",
        "speaker", "supreme court", "scotus", "vp pick",
        "approval rating", "vote", "ballot", "campaign",
    ]),
    ("Economics", [
        "fed ", "fed-", "fomc", "rate hike", "rate cut",
        "interest rate", "inflation", "cpi", "ppi", "gdp",
        "unemployment", "jobs report", "recession", "yield curve",
        "consumer", "retail sales",
    ]),
    ("Finance", [
        "stock", "ipo", "earnings", "dividend", "bond ", "bond-",
        "treasury", "tesla", "apple", "msft", "nvda", "amzn",
        "googl", "meta", "spy", "qqq", "nasdaq", "dow",
        "s&p", "sp500", "crude oil", "wti", "brent", "gold price",
        "silver price", "commodity", "futures",
    ]),
    ("Tech", [
        "ai ", "ai-", "openai", "chatgpt", "anthropic", "google",
        "microsoft", "amazon", "spacex", "tesla product",
        "iphone", "ipad", "android", "app store", "tweet",
        "meta launch", "vr ", "ar ",
    ]),
    ("Culture", [
        "oscar", "grammy", "emmy", "golden globe", "tony award",
        "movie", "box office", "kpop", "song", "album",
        "celebrity", "kardashian", "swift", "beyoncé",
    ]),
    ("Weather", [
        "weather", "hurricane", "tropical storm", "tornado",
        "wildfire", "drought", "blizzard", "temperature",
        "snowfall", "rainfall",
    ]),
    ("Mentions", [
        "mention", "tweet", "tweets count", "twitter mention",
        "social media",
    ]),
]

CANONICAL_CATEGORIES = {
    "crypto", "sports", "finance", "politics", "economics", "culture",
    "weather", "mentions", "tech", "geopolitics", "other", "unknown",
}


def _normalize(text: Optional[str]) -> str:
    if not text:
        return ""
    return text.lower()


def _looks_canonical(category: Optional[str]) -> Optional[str]:
    """Map a possibly-cased / aliased category string to one of the
    canonical names. Returns the canonical form or None if not recognized."""
    if category is None:
        return None
    norm = category.strip().lower()
    if not norm:
        return None
    if norm in CANONICAL_CATEGORIES:
        return norm.capitalize()
    # Common aliases
    aliases = {
        "geopolitical": "Geopolitics",
        "macro": "Economics",
        "macroeconomics": "Economics",
        "election": "Politics",
        "general": "Other",
    }
    return aliases.get(norm)


def infer_category(
    *,
    gamma_category: Optional[str] = None,
    slug: Optional[str] = None,
    question: Optional[str] = None,
) -> str:
    """Return one canonical category, with deterministic fallback chain.

    Priority:
    1. ``gamma_category`` if it normalizes to a canonical value.
    2. Keyword match on slug.
    3. Keyword match on question.
    4. ``"Unknown"`` (never silently masked).
    """
    canon = _looks_canonical(gamma_category)
    if canon:
        return canon

    haystack_slug = _normalize(slug)
    haystack_q = _normalize(question)
    for category, keywords in CATEGORY_KEYWORDS:
        for kw in keywords:
            if kw in haystack_slug:
                return category
    for category, keywords in CATEGORY_KEYWORDS:
        for kw in keywords:
            if kw in haystack_q:
                return category
    return "Unknown"


def backfill_categories_in_db(session, *, batch: int = 1000) -> dict:
    """Walk the ``market`` table and infer categories for rows where it's
    NULL or empty. Updates in place. Returns a count summary.
    """
    from sqlmodel import select
    from app.models.market import Market

    updated = 0
    inferred_dist: dict[str, int] = {}
    rows = list(session.exec(
        select(Market).where((Market.category.is_(None)) | (Market.category == ""))
    ))
    for m in rows:
        cat = infer_category(
            gamma_category=m.category,
            slug=m.raw_slug,
            question=m.question,
        )
        if cat != (m.category or "") and cat != "Unknown":
            m.category = cat
            session.add(m)
            updated += 1
        inferred_dist[cat] = inferred_dist.get(cat, 0) + 1
    if updated:
        session.commit()
    return {
        "scanned": len(rows),
        "updated": updated,
        "distribution_after_inference": inferred_dist,
    }
