import json
from datetime import datetime
from typing import Any

from app.models.market import Market


def safe_float(value: Any, default: float = 0.0) -> float:
    if value is None or value == "":
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def parse_jsonish_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
            return decoded if isinstance(decoded, list) else []
        except json.JSONDecodeError:
            return []
    return []


def parse_datetime(value: Any) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def normalize_gamma_market(payload: dict[str, Any]) -> Market:
    outcomes = parse_jsonish_list(payload.get("outcomes"))
    prices = [safe_float(item) for item in parse_jsonish_list(payload.get("outcomePrices"))]
    token_ids = parse_jsonish_list(payload.get("clobTokenIds"))

    yes_price = None
    no_price = None
    for index, outcome in enumerate(outcomes):
        normalized = str(outcome).lower()
        if index < len(prices) and normalized == "yes":
            yes_price = prices[index]
        if index < len(prices) and normalized == "no":
            no_price = prices[index]

    if yes_price is None and prices:
        yes_price = prices[0]
    if no_price is None and len(prices) > 1:
        no_price = prices[1]

    spread = safe_float(payload.get("spread"))
    best_bid = safe_float(payload.get("bestBid"), default=-1)
    best_ask = safe_float(payload.get("bestAsk"), default=-1)
    if spread == 0 and best_bid >= 0 and best_ask >= 0:
        spread = max(0.0, best_ask - best_bid)

    # v0.7.7 B11.1 — infer category at ingestion when gamma metadata
    # doesn't supply one. Falls back through slug / question keywords.
    # Source flag stored in `data_source` suffix so we can tell
    # gamma-supplied vs inferred apart in audits.
    from app.services.category_inference import infer_category as _infer_b11_1
    raw_question = payload.get("question") or payload.get("title") or payload.get("slug") or "Unknown market"
    raw_category = payload.get("category")
    inferred_category = _infer_b11_1(
        gamma_category=raw_category,
        slug=payload.get("slug"),
        question=raw_question,
    )
    if raw_category and str(raw_category).strip():
        category_source = "metadata"
    elif inferred_category != "Unknown":
        category_source = "inferred"
    else:
        category_source = "unknown"
    data_source_with_flag = f"polymarket_gamma+{category_source}"

    return Market(
        id=str(payload.get("id") or payload.get("conditionId") or payload.get("slug")),
        question=str(raw_question),
        category=inferred_category,  # B11.1: never NULL post-ingestion
        yes_price=yes_price,
        no_price=no_price,
        volume_24h=safe_float(payload.get("volume24hr") or payload.get("volume24h")),
        volume_7d=safe_float(payload.get("volume1wk") or payload.get("volume7d")),
        spread=spread,
        liquidity=safe_float(payload.get("liquidityNum") or payload.get("liquidity")),
        open_interest=safe_float(payload.get("openInterest") or payload.get("open_interest")),
        deadline=parse_datetime(payload.get("endDate") or payload.get("end_date")),
        clob_token_ids=json.dumps(token_ids) if token_ids else None,
        data_source=data_source_with_flag,
        raw_slug=payload.get("slug"),
        active=bool(payload.get("active", True)),
    )
