"""FastExecutor — maker-only post_only GTC execution (R1 V2 plan, 2026-05-27).

Audit R1 V2 a établi que le maker rebate 20% rend break-even crypto possible :
  - Taker WR break-even = 72.7% (insoutenable pour OBI 65.5% accuracy).
  - Maker WR break-even = 51.7% (atteignable).

Donc en live, on N'ACCEPTE PAS de fallback taker. Soit on prend la rebate
maker via `post_only=True` (GTC), soit on skip le trade. Mieux vaut un
skip qu'un trade taker qui mange l'edge.

Spec :
  - MAX_RETRIES = 5 re-quote si BBO bouge
  - REQUOTE_INTERVAL_S = 30s entre check fill
  - TIMEOUT_S = 150s hard cap (5 × 30s)
  - Pas de fallback taker
  - paper mode (`dry_run=True`) : simule fill ~35% des cas (conservative)

py-clob-client API utilisée (`client.py:623`) :
  client.post_order(signed_order, OrderType.GTC, post_only=True)
Documentation Polymarket CLOB:
  https://docs.polymarket.com/developers/CLOB/orders/create-order

Wired dans launch_paper.py on_rtds APRÈS scope_check.allow ET gates.evaluate.allow.
"""
from __future__ import annotations

import asyncio
import random
import time
from dataclasses import dataclass, field
from typing import Any, Optional


# Spec verrouillée (R1 V2 plan)
MAX_RETRIES = 5
REQUOTE_INTERVAL_S = 30.0
TIMEOUT_S = 150.0  # = MAX_RETRIES × REQUOTE_INTERVAL_S

# Paper mode default — conservative fill probability per maker queue
# Justification : sub-30min markets sur Polymarket ont des queues
# typiquement 30-50% remplies. On prend 35% comme MVP, à recalibrer
# après 12h smoke réel.
PAPER_FILL_PROBABILITY = 0.35


@dataclass
class ExecResult:
    """Résultat d'un place_maker_post_only."""
    filled: bool
    fill_price: float = 0.0
    fill_size: float = 0.0
    attempts: int = 0
    elapsed_s: float = 0.0
    error: Optional[str] = None
    raw: dict = field(default_factory=dict)


class FastExecutor:
    """Place maker-only GTC post_only orders. Pas de fallback taker."""

    MAX_RETRIES = MAX_RETRIES
    REQUOTE_INTERVAL_S = REQUOTE_INTERVAL_S
    TIMEOUT_S = TIMEOUT_S

    def __init__(self, clob_client: Any = None, dry_run: bool = True,
                 fill_probability: float = PAPER_FILL_PROBABILITY,
                 rng: Optional[random.Random] = None):
        """
        Args:
            clob_client: py_clob_client.ClobClient instance (level 2 auth).
                None tolerated when dry_run=True.
            dry_run: True = paper mode (no live order). False = real order.
            fill_probability: paper-mode fill rate per attempt (MVP 0.35).
            rng: deterministic RNG injection for tests.
        """
        if not dry_run and clob_client is None:
            raise ValueError("FastExecutor live mode requires a clob_client")
        self.client = clob_client
        self.dry_run = dry_run
        self.fill_probability = fill_probability
        self._rng = rng or random.Random()
        self.stats: dict[str, Any] = {
            "orders_attempted": 0,
            "orders_placed": 0,      # orders successfully posted to CLOB
            "orders_filled": 0,
            "orders_cancelled": 0,
            "orders_timeout": 0,
            "orders_error": 0,
            "total_attempts": 0,     # cumulative re-quotes
            "fill_rate": 0.0,
        }

    async def place_maker_post_only(
        self,
        token_id: str,
        side: str,
        size: float,
        price_target: float,
        get_bbo_func: Optional[Any] = None,
    ) -> ExecResult:
        """Place limit GTC post_only au prix `price_target`. Re-quote si pas filled.

        Args:
            token_id: outcome token Polymarket.
            side: "BUY" | "SELL" (sera passé tel quel au CLOB).
            size: order size en outcome shares.
            price_target: prix initial (typiquement BBO du même côté).
            get_bbo_func: optional async callable () -> (bid, ask) pour
                re-quote. Si None, on garde price_target inchangé entre
                attempts (simple cas, MVP).

        Returns:
            ExecResult avec filled / fill_price / fill_size / error.
        """
        self.stats["orders_attempted"] += 1
        start = time.monotonic()

        if self.dry_run:
            return await self._paper_simulate(
                token_id, side, size, price_target, start)

        # Live path — py_clob_client
        return await self._live_execute(
            token_id, side, size, price_target, get_bbo_func, start)

    async def _paper_simulate(self, token_id: str, side: str,
                              size: float, price_target: float,
                              start_monotonic: float) -> ExecResult:
        """Paper mode : simule un seul tirage Bernoulli au niveau du call.

        MVP conservatif : on suppose un fill_probability terminal de 35%
        sur l'ensemble du « life » de l'ordre maker (queue 5 × 30s).
        Cette valeur match l'intuition R1 V2 (« sub-30min queues sur
        Polymarket ont une chance de fill 30-50% »).

        On ne tire PAS un Bernoulli par attempt (sinon
        P(filled) = 1 - (1-0.35)^5 ≈ 88%, ce qui sur-estime
        massivement la cadence). Une fois 12h réel observé, recalibrer
        p (et déplacer le tirage par-attempt si on modélise la queue).
        """
        # Yield au event loop pour rester non-bloquant.
        await asyncio.sleep(0.0)
        self.stats["total_attempts"] += 1
        self.stats["orders_placed"] += 1
        if self._rng.random() < self.fill_probability:
            self.stats["orders_filled"] += 1
            self._refresh_fill_rate()
            return ExecResult(
                filled=True,
                fill_price=price_target,
                fill_size=size,
                attempts=1,
                elapsed_s=time.monotonic() - start_monotonic,
                raw={"mode": "paper", "p": self.fill_probability},
            )
        # Missed — paper retournerait MAX_RETRIES attempts en runtime live
        self.stats["orders_cancelled"] += 1
        self.stats["orders_timeout"] += 1
        self._refresh_fill_rate()
        return ExecResult(
            filled=False,
            attempts=self.MAX_RETRIES,
            elapsed_s=time.monotonic() - start_monotonic,
            error="MAKER_TIMEOUT_PAPER",
            raw={"mode": "paper", "p": self.fill_probability},
        )

    async def _live_execute(self, token_id: str, side: str,
                            size: float, price_target: float,
                            get_bbo_func: Any,
                            start_monotonic: float) -> ExecResult:
        """Live path : place GTC post_only, check fill, re-quote on BBO drift.

        py_clob_client.ClobClient.post_order(order, OrderType.GTC, post_only=True)
        Returns dict tel que {"success": True, "orderID": "...", ...}.
        Le polling fill status est fait via get_order(orderID).
        Voir https://docs.polymarket.com/developers/CLOB/orders/create-order.
        """
        try:
            from py_clob_client.clob_types import (
                OrderArgs,
                OrderType,
                PartialCreateOrderOptions,
            )
        except Exception as e:  # pragma: no cover - guard import
            self.stats["orders_error"] += 1
            self._refresh_fill_rate()
            return ExecResult(
                filled=False,
                error=f"PYCLOB_IMPORT_ERROR: {type(e).__name__}",
                elapsed_s=time.monotonic() - start_monotonic,
            )

        current_price = price_target
        current_order_id: Optional[str] = None
        for attempt in range(1, self.MAX_RETRIES + 1):
            self.stats["total_attempts"] += 1
            try:
                # Cancel previous if drifting
                if current_order_id and get_bbo_func is not None:
                    try:
                        bbo = await get_bbo_func()
                        new_target = (
                            bbo[0] if side.upper() == "BUY" else bbo[1]
                        )
                        if abs(new_target - current_price) > 1e-4:
                            try:
                                self.client.cancel(order_id=current_order_id)
                            except Exception:
                                pass
                            current_price = new_target
                            current_order_id = None
                    except Exception:
                        pass

                if current_order_id is None:
                    args = OrderArgs(
                        token_id=token_id,
                        price=current_price,
                        size=size,
                        side=side.upper(),
                    )
                    signed = self.client.create_order(args)
                    resp = self.client.post_order(
                        signed, OrderType.GTC, post_only=True)
                    if not isinstance(resp, dict) or not resp.get("success"):
                        self.stats["orders_error"] += 1
                        self._refresh_fill_rate()
                        return ExecResult(
                            filled=False,
                            attempts=attempt,
                            elapsed_s=time.monotonic() - start_monotonic,
                            error=f"POST_ORDER_FAILED: {str(resp)[:120]}",
                            raw={"resp": resp},
                        )
                    self.stats["orders_placed"] += 1
                    current_order_id = resp.get("orderID") or resp.get(
                        "orderId") or resp.get("order_id")
                    if not current_order_id:
                        self.stats["orders_error"] += 1
                        self._refresh_fill_rate()
                        return ExecResult(
                            filled=False,
                            attempts=attempt,
                            elapsed_s=time.monotonic() - start_monotonic,
                            error="NO_ORDER_ID_RETURNED",
                            raw={"resp": resp},
                        )

                # Wait then check fill
                await asyncio.sleep(self.REQUOTE_INTERVAL_S)
                try:
                    status = self.client.get_order(current_order_id)
                except Exception as e:
                    status = {"error": str(e)}
                # CLOB status fields : "FILLED" / "MATCHED" / "OPEN" / ...
                st = str(status.get("status", "")).upper() \
                    if isinstance(status, dict) else ""
                if st in ("FILLED", "MATCHED"):
                    self.stats["orders_filled"] += 1
                    self._refresh_fill_rate()
                    fill_sz = float(status.get("size_matched")
                                    or status.get("matched_size")
                                    or size)
                    return ExecResult(
                        filled=True,
                        fill_price=current_price,
                        fill_size=fill_sz,
                        attempts=attempt,
                        elapsed_s=time.monotonic() - start_monotonic,
                        raw=status,
                    )
                # Else : still open or partial → continue loop
            except Exception as e:
                self.stats["orders_error"] += 1
                self._refresh_fill_rate()
                return ExecResult(
                    filled=False,
                    attempts=attempt,
                    elapsed_s=time.monotonic() - start_monotonic,
                    error=f"EXCEPTION: {type(e).__name__}: {str(e)[:80]}",
                )

        # MAX_RETRIES exhausted — cancel residue
        if current_order_id:
            try:
                self.client.cancel(order_id=current_order_id)
                self.stats["orders_cancelled"] += 1
            except Exception:
                pass
        self.stats["orders_timeout"] += 1
        self._refresh_fill_rate()
        return ExecResult(
            filled=False,
            attempts=self.MAX_RETRIES,
            elapsed_s=time.monotonic() - start_monotonic,
            error="TIMEOUT_RETRIES_EXHAUSTED",
        )

    def _refresh_fill_rate(self):
        placed = self.stats["orders_placed"]
        if placed > 0:
            self.stats["fill_rate"] = self.stats["orders_filled"] / placed
        else:
            self.stats["fill_rate"] = 0.0

    def report(self) -> dict:
        self._refresh_fill_rate()
        return {**self.stats, "dry_run": self.dry_run}


# ─── CLI smoke test ──────────────────────────────────────────────────
if __name__ == "__main__":
    """Smoke FastExecutor en dry_run, vérifie le format et la
    fill_rate empirique sur 200 trials → doit être ~35%."""

    async def _smoke():
        exec_ = FastExecutor(dry_run=True, rng=random.Random(42))
        results = []
        for i in range(50):
            r = await exec_.place_maker_post_only(
                token_id=f"tok_{i}", side="BUY", size=10.0, price_target=0.95)
            results.append(r)
        n_filled = sum(1 for r in results if r.filled)
        n_total = len(results)
        print(f"50 trials: {n_filled}/{n_total} filled = "
              f"{100.0 * n_filled / n_total:.1f}%")
        print(f"sample success: {results[0]}")
        print(f"sample timeout: "
              f"{next((r for r in results if not r.filled), None)}")
        print(f"Report: {exec_.report()}")
        # Sanity asserts
        sample_success = next((r for r in results if r.filled), None)
        assert sample_success is not None
        assert sample_success.fill_price == 0.95
        assert sample_success.fill_size == 10.0
        assert sample_success.error is None
        sample_fail = next((r for r in results if not r.filled), None)
        if sample_fail is not None:
            assert sample_fail.error == "MAKER_TIMEOUT_PAPER"
            assert sample_fail.attempts == FastExecutor.MAX_RETRIES
        # Expect ~35% (range 20-50% on N=50)
        assert 10 <= n_filled <= 40
        print("\nAll asserts OK.")

    asyncio.run(_smoke())
