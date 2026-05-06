"""Unit tests for DenseTradeAuditor — pagination logic, dedupe, normalisation,
and the post-audit recompute. No real network — the tests use a mocked
``httpx.Client`` so they're fast and deterministic.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from app.services.dense_trade_auditor import (
    DenseAuditConfig,
    DenseAuditState,
    DenseTradeAuditor,
    normalize_paginated_trades,
    rebuild_wallet_trade_history,
    recompute_wallet_metrics,
)


def _trade_row(wallet: str, cid: str, oi: int, side: str, size: float, price: float, ts: int, tx: str = "") -> dict[str, Any]:
    return {
        "proxyWallet": wallet,
        "conditionId": cid,
        "outcomeIndex": oi,
        "side": side,
        "size": size,
        "price": price,
        "timestamp": ts,
        "transactionHash": tx,
    }


def test_normalize_filters_invalid_rows() -> None:
    rows = [
        _trade_row("0xabc", "0xcid", 0, "BUY", 100, 0.5, 1700000000, "0xtx1"),
        # Missing wallet
        {"conditionId": "0xcid", "outcomeIndex": 0, "side": "BUY", "size": 1, "price": 0.5, "timestamp": 1},
        # Bad side
        _trade_row("0xabc", "0xcid", 0, "REDEEM", 1, 0.5, 1, "0xtx2"),
        # Missing cid
        {"proxyWallet": "0xabc", "outcomeIndex": 0, "side": "BUY", "size": 1, "price": 0.5, "timestamp": 1},
    ]
    out = normalize_paginated_trades(rows)
    assert len(out) == 1
    assert out[0]["wallet"] == "0xabc"
    assert out[0]["cid"] == "0xcid"


def test_recompute_wallet_metrics_winning_position() -> None:
    trades = [_trade_row("0xa", "0xcid", 0, "BUY", 100, 0.40, 1700000000)]
    by_wallet = rebuild_wallet_trade_history(normalize_paginated_trades(trades))
    res = {"0xcid": {"winning_outcome_index": 0}}
    out = recompute_wallet_metrics(by_wallet, res)
    assert "0xa" in out
    m = out["0xa"]
    assert m.sample_size == 1
    assert m.n_winners == 1
    assert m.win_rate == 1.0
    # return_per_dollar at price 0.40 = (1-0.4)/0.4 = 1.5
    assert m.expected_value == pytest.approx(1.5)


def test_recompute_wallet_metrics_losing_position() -> None:
    trades = [_trade_row("0xa", "0xcid", 0, "BUY", 100, 0.40, 1700000000)]
    by_wallet = rebuild_wallet_trade_history(normalize_paginated_trades(trades))
    res = {"0xcid": {"winning_outcome_index": 1}}  # outcome 0 lost
    out = recompute_wallet_metrics(by_wallet, res)
    m = out["0xa"]
    assert m.n_losers == 1
    assert m.expected_value == pytest.approx(-1.0)


# ---------------- pagination logic ----------------


class _MockClient:
    """Returns canned responses for /trades requests."""

    def __init__(self, pages: list[list[dict[str, Any]]]):
        self.pages = pages
        self.requests = 0

    def get(self, url: str, params: dict[str, Any] | None = None) -> "httpx.Response":  # type: ignore[override]
        self.requests += 1
        offset = (params or {}).get("offset", 0)
        page_size = (params or {}).get("limit", 1000)
        page_idx = offset // page_size
        body = self.pages[page_idx] if page_idx < len(self.pages) else []
        return httpx.Response(status_code=200, json=body)

    def close(self) -> None:
        pass

    def __enter__(self) -> "_MockClient":
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.close()


def _full_page() -> list[dict[str, Any]]:
    return [
        _trade_row(f"0x{k:040x}", "0xcid", 0, "BUY", 1.0, 0.5, 1700000000 + k, tx=f"0xtx{k}")
        for k in range(1000)
    ]


def test_pagination_stops_on_empty_page() -> None:
    pages = [_full_page(), []]  # second page empty
    cfg = DenseAuditConfig(sleep_ms=0, max_pages_per_market=10)
    auditor = DenseTradeAuditor(cfg, client=_MockClient(pages))  # type: ignore[arg-type]
    state = DenseAuditState()
    new, cov = auditor.fetch_market_trades_paginated("0xcid", state)
    assert cov.stopped_early_reason == "EMPTY_PAGE"
    assert cov.pages_fetched == 2
    assert len(new) == 1000


def test_pagination_stops_on_partial_page() -> None:
    # Second page must have NEW trades (not subset of first) so it doesn't
    # trigger ALL_DUPLICATES before PARTIAL_PAGE — use a different ts range.
    partial = [
        _trade_row(f"0xb{k:039x}", "0xcid", 0, "BUY", 1.0, 0.5, 1800000000 + k, tx=f"0xtx_p{k}")
        for k in range(50)
    ]
    pages = [_full_page(), partial]
    cfg = DenseAuditConfig(sleep_ms=0, max_pages_per_market=10)
    auditor = DenseTradeAuditor(cfg, client=_MockClient(pages))  # type: ignore[arg-type]
    state = DenseAuditState()
    new, cov = auditor.fetch_market_trades_paginated("0xcid", state)
    assert cov.stopped_early_reason == "PARTIAL_PAGE"
    assert cov.pages_fetched == 2
    assert len(new) == 1050


def test_pagination_stops_on_all_duplicates() -> None:
    page = _full_page()
    pages = [page, page]  # second page identical → all duplicates after dedupe
    cfg = DenseAuditConfig(sleep_ms=0, max_pages_per_market=10)
    auditor = DenseTradeAuditor(cfg, client=_MockClient(pages))  # type: ignore[arg-type]
    state = DenseAuditState()
    new, cov = auditor.fetch_market_trades_paginated("0xcid", state)
    assert cov.stopped_early_reason == "ALL_DUPLICATES"
    assert cov.pages_fetched == 2
    assert len(new) == 1000  # only the first page's trades count


def _unique_page(seed: int) -> list[dict[str, Any]]:
    """1000 unique trades, distinct from any other seed."""
    return [
        _trade_row(
            f"0x{seed:02x}{k:038x}",
            "0xcid",
            0,
            "BUY",
            1.0,
            0.5,
            1700000000 + seed * 10000 + k,
            tx=f"0xtx_s{seed}_k{k}",
        )
        for k in range(1000)
    ]


def test_pagination_caps_at_max_pages() -> None:
    pages = [_unique_page(s) for s in range(10)]
    cfg = DenseAuditConfig(sleep_ms=0, max_pages_per_market=3)
    auditor = DenseTradeAuditor(cfg, client=_MockClient(pages))  # type: ignore[arg-type]
    state = DenseAuditState()
    new, cov = auditor.fetch_market_trades_paginated("0xcid", state)
    assert cov.stopped_early_reason == "MAX_PAGES"
    assert cov.pages_fetched == 3


def test_global_budget_stops_pagination() -> None:
    pages = [_unique_page(s) for s in range(10)]
    cfg = DenseAuditConfig(sleep_ms=0, max_pages_per_market=10, max_total_requests_per_run=2)
    auditor = DenseTradeAuditor(cfg, client=_MockClient(pages))  # type: ignore[arg-type]
    state = DenseAuditState()
    new, cov = auditor.fetch_market_trades_paginated("0xcid", state)
    assert cov.stopped_early_reason == "GLOBAL_BUDGET"
    assert state.total_requests <= 2


def test_audit_markets_resumes_from_checkpoint(tmp_path) -> None:  # type: ignore[no-untyped-def]
    pages = [_full_page()]
    cfg = DenseAuditConfig(sleep_ms=0, max_pages_per_market=1, market_limit=10)
    cp = tmp_path / "checkpoint.txt"
    cp.write_text("0xa\n0xb\n", encoding="utf-8")
    auditor = DenseTradeAuditor(cfg, client=_MockClient(pages))  # type: ignore[arg-type]
    state = auditor.audit_markets(["0xa", "0xb", "0xc"], checkpoint_path=cp)
    # Only 0xc should be processed.
    assert len(state.coverages) == 1
    assert state.coverages[0].market_id == "0xc"
    written = cp.read_text(encoding="utf-8")
    assert "0xc" in written
