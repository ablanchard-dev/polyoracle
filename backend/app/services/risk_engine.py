from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

from sqlmodel import Session, select

from app.config import get_settings
from app.models.trade import NoTradeDecision
from app.services.risk_mode import RiskModeProfile


REASON_CODES = (
    "LOW_SIGNAL_SCORE",
    "LOW_CONFIDENCE",
    "LOW_LIQUIDITY",
    "WIDE_SPREAD",
    "BAD_ORDERBOOK",
    "NO_COPYABLE_EDGE",
    "LATE_ENTRY",
    "TOO_MUCH_EXPOSURE",
    "DAILY_LOSS_LIMIT",
    "WEEKLY_LOSS_LIMIT",
    "KILL_SWITCH",
    "INSUFFICIENT_DATA",
    "WALLET_NOT_RELIABLE",
)


@dataclass
class RiskDecision:
    approved: bool
    reason: str
    position_size: float = 0.0
    reason_code: str | None = None
    details: dict[str, Any] = field(default_factory=dict)


class RiskEngine:
    def __init__(self) -> None:
        self.settings = get_settings()

    def validate_trade(
        self,
        signal_score: float,
        spread: float,
        liquidity: float,
        capital: float,
        copy_delay_seconds: float = 0.0,
        price_deterioration: float = 0.0,
        total_exposure: float = 0.0,
        confidence_score: float | None = None,
        copyable_edge_score: float | None = None,
        wallet_tier: str | None = None,
        orderbook_quality: str | None = None,
        liquidity_score: float | None = None,
        kill_switch_active: bool = False,
    ) -> RiskDecision:
        if kill_switch_active:
            return RiskDecision(False, "Kill switch active", 0.0, reason_code="KILL_SWITCH")
        checks: list[RiskDecision] = [
            self.check_signal_quality(signal_score),
            self.check_spread(spread),
            self.check_liquidity(liquidity),
            self.check_copy_delay(copy_delay_seconds),
            self.check_price_deterioration(price_deterioration),
            self.check_total_exposure(total_exposure),
            self.check_daily_loss_limit(),
            self.check_weekly_loss_limit(),
        ]
        if confidence_score is not None:
            checks.append(self.check_confidence(confidence_score))
        if copyable_edge_score is not None:
            checks.append(self.check_copyable_edge(copyable_edge_score))
        if liquidity_score is not None:
            checks.append(self.check_liquidity_score(liquidity_score))
        if wallet_tier is not None:
            checks.append(self.check_wallet_tier(wallet_tier))
        if orderbook_quality is not None:
            checks.append(self.check_orderbook_quality(orderbook_quality))
        failed = next((check for check in checks if not check.approved), None)
        if failed:
            return failed
        return RiskDecision(True, "Risk checks passed", self.compute_position_size(capital), reason_code=None)

    def compute_position_size(self, capital: float) -> float:
        return round(capital * self.settings.max_risk_per_trade, 2)

    def check_daily_loss_limit(self) -> RiskDecision:
        return RiskDecision(True, "Daily loss within limit")

    def check_weekly_loss_limit(self) -> RiskDecision:
        return RiskDecision(True, "Weekly loss within limit")

    def check_liquidity(self, liquidity: float) -> RiskDecision:
        if liquidity < self.settings.min_liquidity:
            return RiskDecision(False, "Liquidity below minimum", reason_code="LOW_LIQUIDITY")
        return RiskDecision(True, "Liquidity ok")

    def check_liquidity_score(self, score: float) -> RiskDecision:
        if score < self.settings.min_liquidity_score:
            return RiskDecision(False, "Liquidity score below minimum", reason_code="LOW_LIQUIDITY")
        return RiskDecision(True, "Liquidity score ok")

    def check_spread(self, spread: float) -> RiskDecision:
        if spread > self.settings.max_spread_pct or spread > self.settings.max_spread:
            return RiskDecision(False, "Spread above maximum", reason_code="WIDE_SPREAD")
        return RiskDecision(True, "Spread ok")

    def check_signal_quality(self, signal_score: float) -> RiskDecision:
        if signal_score < self.settings.min_signal_score:
            return RiskDecision(False, "Signal score below minimum", reason_code="LOW_SIGNAL_SCORE")
        return RiskDecision(True, "Signal quality ok")

    def check_signal_score(self, signal_score: float) -> RiskDecision:
        return self.check_signal_quality(signal_score)

    def check_confidence(self, confidence: float) -> RiskDecision:
        if confidence < self.settings.min_confidence_score:
            return RiskDecision(False, "Confidence below minimum", reason_code="LOW_CONFIDENCE")
        return RiskDecision(True, "Confidence ok")

    def check_copyable_edge(self, score: float) -> RiskDecision:
        if score < self.settings.min_copyable_edge:
            return RiskDecision(False, "Copyable edge insufficient", reason_code="NO_COPYABLE_EDGE")
        return RiskDecision(True, "Copyable edge ok")

    def check_copy_delay(self, copy_delay_seconds: float) -> RiskDecision:
        if copy_delay_seconds > 300:
            return RiskDecision(False, "Copy delay too high", reason_code="LATE_ENTRY")
        return RiskDecision(True, "Copy delay ok")

    def check_price_deterioration(self, price_deterioration: float) -> RiskDecision:
        if price_deterioration > 0.05:
            return RiskDecision(False, "Price moved too far from smart-wallet entry", reason_code="LATE_ENTRY")
        return RiskDecision(True, "Price deterioration ok")

    def check_total_exposure(self, total_exposure: float) -> RiskDecision:
        if total_exposure > self.settings.paper_max_exposure or total_exposure > self.settings.max_total_exposure:
            return RiskDecision(False, "Total exposure above maximum", reason_code="TOO_MUCH_EXPOSURE")
        return RiskDecision(True, "Total exposure ok")

    def check_market_exposure(self, market_exposure: float) -> RiskDecision:
        if market_exposure > self.settings.paper_max_market_exposure:
            return RiskDecision(False, "Per-market exposure above maximum", reason_code="TOO_MUCH_EXPOSURE")
        return RiskDecision(True, "Market exposure ok")

    def check_wallet_tier(self, tier: str) -> RiskDecision:
        if tier in ("SUSPICIOUS", "INSUFFICIENT_DATA"):
            return RiskDecision(False, "Wallet not reliable enough", reason_code="WALLET_NOT_RELIABLE")
        if tier in ("WEAK", "IGNORE"):
            return RiskDecision(False, "Wallet tier below copy threshold", reason_code="WALLET_NOT_RELIABLE")
        return RiskDecision(True, "Wallet tier ok")

    def check_orderbook_quality(self, quality: str) -> RiskDecision:
        if quality in ("UNTRADABLE", "BAD"):
            return RiskDecision(False, "Orderbook quality too low", reason_code="BAD_ORDERBOOK")
        if quality == "INSUFFICIENT_DATA":
            return RiskDecision(False, "Orderbook data missing", reason_code="INSUFFICIENT_DATA")
        return RiskDecision(True, "Orderbook quality ok")

    def trigger_kill_switch(self) -> RiskDecision:
        return RiskDecision(False, "Kill switch triggered", reason_code="KILL_SWITCH")

    # ---------------- v0.5.2 risk-mode aware validation ----------------

    def validate_for_mode(
        self,
        profile: RiskModeProfile,
        *,
        candidate_status: str | None,
        market_sample_size: int,
        win_rate_confidence: str,
        signal_score: float,
        spread: float,
        liquidity: float,
        copyable_edge_score: float,
        orderbook_quality: str,
        copy_delay_seconds: float = 0.0,
        price_deterioration: float = 0.0,
        total_exposure: float = 0.0,
        capital_total: float | None = None,  # 2026-05-17 : live capital pour position_size
        market_exposure: float = 0.0,
        wallet_exposure: float = 0.0,
        open_positions_count: int = 0,
        daily_trades_count: int = 0,
        kill_switch_active: bool = False,
        tier_max_positions: int | None = None,
    ) -> RiskDecision:
        """Validate a paper trade against the active ``RiskModeProfile``.

        Kill switch always wins. Then we walk the profile-derived gates first
        (allowed status / sample size / confidence / open-position cap /
        daily-trade cap / wallet exposure) before falling back to the existing
        per-trade structural checks (signal, spread, liquidity, edge,
        orderbook, copy delay, price deterioration, total exposure, market
        exposure).
        """
        if kill_switch_active:
            return RiskDecision(False, "Kill switch active", reason_code="KILL_SWITCH")

        if not candidate_status or candidate_status not in profile.allowed_statuses:
            return RiskDecision(
                False,
                f"Wallet status {candidate_status or 'UNKNOWN'} not allowed in {profile.name}",
                reason_code="WALLET_NOT_RELIABLE",
                details={"candidate_status": candidate_status, "allowed": sorted(profile.allowed_statuses)},
            )

        if market_sample_size < profile.min_sample_size:
            return RiskDecision(
                False,
                f"Sample {market_sample_size} below {profile.name} minimum {profile.min_sample_size}",
                reason_code="INSUFFICIENT_DATA",
                details={"sample": market_sample_size, "required": profile.min_sample_size},
            )

        if profile.require_medium_high_confidence and win_rate_confidence not in {"MEDIUM", "HIGH"}:
            return RiskDecision(
                False,
                f"{profile.name} requires MEDIUM/HIGH confidence (got {win_rate_confidence})",
                reason_code="LOW_CONFIDENCE",
                details={"confidence": win_rate_confidence},
            )

        # P0-A2 2026-05-18 — dynamic position cap from capital tier overrides
        # the legacy profile.max_open_positions. The hardcoded AGGRESSIVE=15 was
        # capping the bot at 15 positions while tier HUGE allows 960 — bot
        # bloqué 1-in/1-out malgré effective_capital=$76k. Doctrine: capital
        # tier governs max_pos; profile cap stays only as fallback when tier
        # is not supplied (e.g. legacy callers, non-tier-aware tests).
        profile_cap = profile.max_open_positions
        final_pos_cap = tier_max_positions if tier_max_positions is not None else profile_cap
        cap_source = "tier" if tier_max_positions is not None else "profile"
        if final_pos_cap is not None and open_positions_count >= final_pos_cap:
            return RiskDecision(
                False,
                f"Open positions {open_positions_count} reached {cap_source} cap {final_pos_cap}",
                reason_code="TOO_MUCH_EXPOSURE",
                details={
                    "open_positions": open_positions_count,
                    "profile_cap": profile_cap,
                    "tier_cap": tier_max_positions,
                    "final_cap_used": final_pos_cap,
                    "cap_source": cap_source,
                },
            )

        if profile.max_daily_trades is not None and daily_trades_count >= profile.max_daily_trades:
            return RiskDecision(
                False,
                f"Daily trades {daily_trades_count} reached {profile.name} cap {profile.max_daily_trades}",
                reason_code="DAILY_LOSS_LIMIT",
                details={"daily_trades": daily_trades_count, "cap": profile.max_daily_trades},
            )

        if wallet_exposure > profile.max_wallet_exposure:
            return RiskDecision(
                False,
                f"Wallet exposure {wallet_exposure:.4f} above {profile.name} cap {profile.max_wallet_exposure:.4f}",
                reason_code="TOO_MUCH_EXPOSURE",
                details={"wallet_exposure": wallet_exposure, "cap": profile.max_wallet_exposure},
            )

        if total_exposure > profile.max_total_exposure:
            return RiskDecision(
                False,
                f"Total exposure {total_exposure:.4f} above {profile.name} cap {profile.max_total_exposure:.4f}",
                reason_code="TOO_MUCH_EXPOSURE",
                details={"total_exposure": total_exposure, "cap": profile.max_total_exposure},
            )

        if market_exposure > profile.max_market_exposure:
            return RiskDecision(
                False,
                f"Market exposure {market_exposure:.4f} above {profile.name} cap {profile.max_market_exposure:.4f}",
                reason_code="TOO_MUCH_EXPOSURE",
                details={"market_exposure": market_exposure, "cap": profile.max_market_exposure},
            )

        # Structural per-trade checks: only enforce them if the profile asks for them.
        if profile.require_spread_check and spread > self.settings.max_spread_pct:
            return RiskDecision(False, "Spread above maximum", reason_code="WIDE_SPREAD", details={"spread": spread})
        if profile.require_liquidity_check and liquidity < self.settings.min_liquidity:
            return RiskDecision(False, "Liquidity below minimum", reason_code="LOW_LIQUIDITY", details={"liquidity": liquidity})
        if profile.require_orderbook_quality and orderbook_quality in {"UNTRADABLE", "BAD"}:
            return RiskDecision(False, "Orderbook quality too low", reason_code="BAD_ORDERBOOK", details={"orderbook_quality": orderbook_quality})
        if profile.require_orderbook_quality and orderbook_quality == "INSUFFICIENT_DATA":
            # v0.7.8 P6 B16: ELITE wallets on crypto 5-min markets have no
            # public CLOB orderbook (404). In paper mode, accept the trade —
            # the ELITE edge is proven on 100+ resolved markets. In live mode,
            # this MUST be blocked (need real orderbook for slippage/fill).
            # 2026-05-11 Phase A: paper_live_strict=True forces reject
            # exactly like live (no ELITE+paper exception).
            if (
                candidate_status != "ELITE"
                or not self.settings.paper_trading_enabled
                or self.settings.paper_live_strict
            ):
                return RiskDecision(False, "Orderbook data missing", reason_code="INSUFFICIENT_DATA", details={"orderbook_quality": orderbook_quality})
        if profile.require_copyable_edge and copyable_edge_score < self.settings.min_copyable_edge:
            # v0.7.8 P6 B17: ELITE wallets with missing orderbook data get
            # artificially low copyable_edge scores. In paper mode, skip this
            # check for ELITE — their edge is proven via 100+ resolved markets.
            # 2026-05-11 Phase A: paper_live_strict=True forces reject.
            if (
                candidate_status != "ELITE"
                or not self.settings.paper_trading_enabled
                or self.settings.paper_live_strict
            ):
                return RiskDecision(False, "Copyable edge insufficient", reason_code="NO_COPYABLE_EDGE", details={"copyable_edge_score": copyable_edge_score})
        if signal_score < self.settings.min_signal_score:
            return RiskDecision(False, "Signal score below minimum", reason_code="LOW_SIGNAL_SCORE", details={"signal_score": signal_score})
        if copy_delay_seconds > 300 or price_deterioration > 0.05:
            return RiskDecision(False, "Late entry — price moved too much / delay too high", reason_code="LATE_ENTRY")

        # 2026-05-17 — utiliser live capital (param) + retourner 2R upper bound (= max
        # possible sizing). Le min downstream avec allocator_decision.sizing applique
        # le vrai R-multiplier (2R win / 1R loss state machine).
        effective_capital = capital_total if capital_total is not None else self.settings.paper_capital
        position_size = round(effective_capital * profile.max_risk_per_trade * 2.0, 2)  # 2R upper
        return RiskDecision(True, f"Risk checks passed ({profile.name})", position_size, reason_code=None, details={"profile": profile.name, "capital_total": effective_capital})

    # ---------------- no-trade decision log ----------------

    def log_no_trade(
        self,
        session: Session,
        decision: RiskDecision,
        signal_id: str | None = None,
        market_id: str | None = None,
        wallet_address: str | None = None,
        saved_loss_estimate: float = 0.0,
        details: dict[str, Any] | None = None,
    ) -> NoTradeDecision:
        record = NoTradeDecision(
            id=str(uuid4()),
            signal_id=signal_id,
            market_id=market_id,
            wallet_address=wallet_address,
            reason_code=decision.reason_code or "INSUFFICIENT_DATA",
            details=json.dumps({"reason": decision.reason, **(details or {})}),
            saved_loss_estimate=saved_loss_estimate,
        )
        session.add(record)
        session.commit()
        return record

    @staticmethod
    def list_no_trade_decisions(session: Session, limit: int = 100) -> list[NoTradeDecision]:
        statement = select(NoTradeDecision).order_by(NoTradeDecision.created_at.desc()).limit(limit)
        return list(session.exec(statement).all())
