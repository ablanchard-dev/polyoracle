"""v0.5.7 — DenseTradeAuditor.

Goal: deepen `/trades?market=` pagination so we don't miss 80-95 % of
wallets on the busiest markets. v0.5.5 capped at 5000 trades per
market; v0.5.7 caps at 200 pages × 1000 = up to 200k trades per market,
within a global 200k-request safety budget.

Pagination rules per market:

* Stop when a page returns < ``page_size`` rows (last page).
* Stop when a page returns 0 new trades after dedupe (auto-stop).
* Cap at ``max_pages_per_market`` (200) regardless.
* Cap globally at ``max_total_requests_per_run`` (200 000).

Resume support: each completed market id is appended to a JSONL
``checkpoint`` file; restart skips already-completed markets.

The module is **read-only against the network** — never sends trades,
never connects to wallets, never enables paper/live execution.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable

import httpx

logger = logging.getLogger(__name__)

DATA_API_DEFAULT = "https://data-api.polymarket.com"


@dataclass
class DenseAuditConfig:
    market_limit: int = 3000
    page_size: int = 1000
    max_pages_per_market: int = 200
    max_total_requests_per_run: int = 200_000
    sleep_ms: int = 130
    resume_enabled: bool = True
    checkpoint_every: int = 50
    request_timeout_s: float = 20.0
    data_api_base: str = DATA_API_DEFAULT


@dataclass
class MarketCoverage:
    market_id: str
    pages_fetched: int
    trades_fetched: int
    new_trades_after_dedupe: int
    stopped_early_reason: str  # "EMPTY_PAGE" | "PARTIAL_PAGE" | "ALL_DUPLICATES"
                               # | "MAX_PAGES" | "GLOBAL_BUDGET" | "ERROR"
    duration_s: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "market_id": self.market_id,
            "pages_fetched": self.pages_fetched,
            "trades_fetched": self.trades_fetched,
            "new_trades_after_dedupe": self.new_trades_after_dedupe,
            "stopped_early_reason": self.stopped_early_reason,
            "duration_s": self.duration_s,
        }


@dataclass
class DenseAuditState:
    """In-memory state carried across markets in a single run."""

    seen_trade_keys: set[str] = field(default_factory=set)
    total_requests: int = 0
    total_trades: int = 0
    total_new_trades: int = 0
    coverages: list[MarketCoverage] = field(default_factory=list)
    started_at: float = field(default_factory=time.time)


def _trade_key(trade: dict[str, Any]) -> str:
    """Deterministic key for dedupe — combination of ts/proxy/cid/oi/size."""
    return (
        f"{trade.get('transactionHash') or ''}|"
        f"{trade.get('proxyWallet') or trade.get('user') or ''}|"
        f"{trade.get('conditionId') or trade.get('market') or ''}|"
        f"{trade.get('outcomeIndex')}|{trade.get('timestamp')}|"
        f"{trade.get('size')}|{trade.get('price')}"
    ).lower()


def normalize_paginated_trades(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Strip incoming rows to fields used downstream and lowercase the cid."""
    out: list[dict[str, Any]] = []
    for r in rows:
        cid = r.get("conditionId") or r.get("market")
        oi_raw = r.get("outcomeIndex")
        side = (r.get("side") or "").upper()
        if not cid or oi_raw is None or side not in {"BUY", "SELL"}:
            continue
        try:
            oi = int(oi_raw)
        except (TypeError, ValueError):
            continue
        try:
            size = float(r.get("size") or 0)
            price = float(r.get("price") or 0)
            ts = int(r.get("timestamp") or 0)
        except (TypeError, ValueError):
            continue
        proxy = (r.get("proxyWallet") or r.get("user") or "").lower()
        if not proxy:
            continue
        out.append(
            {
                "cid": str(cid).lower(),
                "oi": oi,
                "side": side,
                "size": size,
                "price": price,
                "ts": ts,
                "wallet": proxy,
                "tx": (r.get("transactionHash") or "").lower(),
            }
        )
    return out


class DenseTradeAuditor:
    def __init__(
        self,
        config: DenseAuditConfig | None = None,
        client: httpx.Client | None = None,
    ) -> None:
        self.config = config or DenseAuditConfig()
        self._owns_client = client is None
        self.client = client or httpx.Client(timeout=self.config.request_timeout_s)

    def close(self) -> None:
        if self._owns_client:
            self.client.close()

    def __enter__(self) -> "DenseTradeAuditor":
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.close()

    # ------------ pagination ------------

    def fetch_market_trades_paginated(
        self,
        market_id: str,
        state: DenseAuditState,
    ) -> tuple[list[dict[str, Any]], MarketCoverage]:
        """Page through ``/trades?market=`` for one market.

        Mutates ``state`` to track global request count + dedupe set.
        Returns (new_normalised_trades, coverage).
        """
        cfg = self.config
        new_trades: list[dict[str, Any]] = []
        pages_fetched = 0
        trades_fetched = 0
        new_after_dedupe = 0
        stopped_reason = "MAX_PAGES"
        page_started_at = time.time()
        for page in range(cfg.max_pages_per_market):
            if state.total_requests >= cfg.max_total_requests_per_run:
                stopped_reason = "GLOBAL_BUDGET"
                break
            offset = page * cfg.page_size
            try:
                response = self.client.get(
                    f"{cfg.data_api_base}/trades",
                    params={"market": market_id, "limit": cfg.page_size, "offset": offset},
                )
                state.total_requests += 1
                if response.status_code != 200:
                    stopped_reason = "ERROR"
                    break
                rows = response.json()
                if not isinstance(rows, list):
                    stopped_reason = "ERROR"
                    break
            except Exception:
                stopped_reason = "ERROR"
                break
            pages_fetched += 1
            trades_fetched += len(rows)
            if not rows:
                stopped_reason = "EMPTY_PAGE"
                break
            normalised = normalize_paginated_trades(rows)
            page_new = 0
            for t in normalised:
                k = f"{t['tx']}|{t['wallet']}|{t['cid']}|{t['oi']}|{t['ts']}|{t['size']}|{t['price']}"
                if k in state.seen_trade_keys:
                    continue
                state.seen_trade_keys.add(k)
                new_trades.append(t)
                page_new += 1
            new_after_dedupe += page_new
            if page_new == 0 and pages_fetched > 1:
                stopped_reason = "ALL_DUPLICATES"
                break
            if len(rows) < cfg.page_size:
                stopped_reason = "PARTIAL_PAGE"
                break
            time.sleep(cfg.sleep_ms / 1000)
        coverage = MarketCoverage(
            market_id=market_id,
            pages_fetched=pages_fetched,
            trades_fetched=trades_fetched,
            new_trades_after_dedupe=new_after_dedupe,
            stopped_early_reason=stopped_reason,
            duration_s=round(time.time() - page_started_at, 3),
        )
        state.total_trades += trades_fetched
        state.total_new_trades += new_after_dedupe
        state.coverages.append(coverage)
        return new_trades, coverage

    def audit_markets(
        self,
        market_ids: Iterable[str],
        *,
        on_market_done: Any = None,
        checkpoint_path: Path | None = None,
        wallet_writer: Any = None,
    ) -> DenseAuditState:
        """Iterate ``market_ids``, fetch trades, optionally persist via the
        provided ``wallet_writer`` callback ``(wallet, trade) -> None``.

        Resumes by skipping any market already listed in ``checkpoint_path``.
        """
        state = DenseAuditState()
        already_done: set[str] = set()
        if checkpoint_path and checkpoint_path.exists() and self.config.resume_enabled:
            with checkpoint_path.open(encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if line:
                        already_done.add(line)
        ck_handle = checkpoint_path.open("a", encoding="utf-8") if checkpoint_path else None
        try:
            processed = 0
            for cid in market_ids:
                cid_lower = cid.lower()
                if cid_lower in already_done:
                    continue
                if state.total_requests >= self.config.max_total_requests_per_run:
                    logger.info("dense audit: global request budget reached, stopping")
                    break
                trades, _coverage = self.fetch_market_trades_paginated(cid_lower, state)
                if wallet_writer:
                    for t in trades:
                        wallet_writer(t["wallet"], t)
                if ck_handle:
                    ck_handle.write(cid_lower + "\n")
                    ck_handle.flush()
                processed += 1
                if on_market_done:
                    on_market_done(processed, state)
                if processed % self.config.checkpoint_every == 0:
                    if ck_handle:
                        ck_handle.flush()
                if processed >= self.config.market_limit:
                    break
        finally:
            if ck_handle:
                ck_handle.close()
        return state


# ----------------- post-audit recompute -----------------


@dataclass
class WalletDenseRecompute:
    address: str
    sample_size: int
    n_winners: int
    n_losers: int
    win_rate: float | None
    average_entry_price: float | None
    expected_value: float | None
    total_notional_usd: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "address": self.address,
            "sample_size": self.sample_size,
            "n_winners": self.n_winners,
            "n_losers": self.n_losers,
            "win_rate": self.win_rate,
            "average_entry_price": self.average_entry_price,
            "expected_value": self.expected_value,
            "total_notional_usd": self.total_notional_usd,
        }


def rebuild_wallet_trade_history(
    trades: Iterable[dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {}
    for t in trades:
        wallet = t.get("wallet")
        if not wallet:
            continue
        out.setdefault(wallet, []).append(t)
    return out


def recompute_wallet_metrics(
    trades_by_wallet: dict[str, list[dict[str, Any]]],
    market_resolutions: dict[str, dict[str, Any]],
) -> dict[str, WalletDenseRecompute]:
    """For each wallet, aggregate (cid, oi) positions and compute observed
    EV from real entry prices on resolved markets.
    """
    out: dict[str, WalletDenseRecompute] = {}
    for wallet, rows in trades_by_wallet.items():
        positions: dict[tuple[str, int], dict[str, float]] = {}
        for r in rows:
            cid, oi = r["cid"], r["oi"]
            bucket = positions.setdefault(
                (cid, oi),
                {"buys_size": 0.0, "buys_notional": 0.0, "sells_size": 0.0, "sells_notional": 0.0},
            )
            if r["side"] == "BUY":
                bucket["buys_size"] += r["size"]
                bucket["buys_notional"] += r["size"] * r["price"]
            else:
                bucket["sells_size"] += r["size"]
                bucket["sells_notional"] += r["size"] * r["price"]
        winners: list[float] = []
        losers: list[float] = []
        notionals: list[float] = []
        prices: list[tuple[float, float]] = []  # (price, notional weight)
        for (cid, oi), pos in positions.items():
            net = pos["buys_size"] - pos["sells_size"]
            if net <= 0 or pos["buys_size"] <= 0:
                continue
            avg_price = pos["buys_notional"] / pos["buys_size"]
            if avg_price <= 0 or avg_price >= 1:
                continue
            res = market_resolutions.get(cid)
            if not res:
                continue
            woi = res.get("winning_outcome_index")
            if woi is None:
                continue
            if oi == int(woi):
                winners.append((1 - avg_price) / avg_price)
            else:
                losers.append(-1.0)
            notionals.append(pos["buys_notional"])
            prices.append((avg_price, pos["buys_notional"]))
        sample = len(winners) + len(losers)
        if sample == 0:
            continue
        wr = len(winners) / sample
        avg_payoff = sum(winners) / len(winners) if winners else None
        avg_loss = sum(losers) / len(losers) if losers else None
        ev: float | None = None
        if avg_payoff is not None and avg_loss is not None:
            ev = wr * avg_payoff + (1 - wr) * avg_loss
        elif winners and not losers and avg_payoff is not None:
            ev = avg_payoff
        elif losers and not winners and avg_loss is not None:
            ev = avg_loss
        total_w = sum(w for _, w in prices)
        avg_entry = sum(p * w for p, w in prices) / total_w if total_w > 0 else None
        out[wallet] = WalletDenseRecompute(
            address=wallet,
            sample_size=sample,
            n_winners=len(winners),
            n_losers=len(losers),
            win_rate=round(wr, 4),
            average_entry_price=round(avg_entry, 4) if avg_entry is not None else None,
            expected_value=round(ev, 4) if ev is not None else None,
            total_notional_usd=round(sum(notionals), 2),
        )
    return out


def export_dense_audit_report(
    state: DenseAuditState,
    json_path: Path,
) -> None:
    payload = {
        "generated_at": datetime.now(UTC).isoformat(),
        "total_markets_audited": len(state.coverages),
        "total_requests": state.total_requests,
        "total_trades_fetched": state.total_trades,
        "total_new_trades_after_dedupe": state.total_new_trades,
        "duration_s": round(time.time() - state.started_at, 1),
        "coverages": [c.to_dict() for c in state.coverages],
    }
    json_path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
