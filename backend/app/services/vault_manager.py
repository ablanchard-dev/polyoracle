"""HWM vault manager (Phase E E-VAULT — 2026-05-11).

Implémente la règle dure opérateur "le bot ne doit JAMAIS repasser sous le HWM"
via cash-out / lock progressive vers un wallet vault froid.

Mode initial = SIMULATION (pas de vraie tx Polygon). On track HWM + simule
les triggers de transfer dans la DB. Quand opérateur prêt pour live + EOA
configurée, swap simulator par real Polygon USDC.e tx.

Spec (T2 Round 2 review 2026-05-11) :
- 2 wallets logiques : BOT_ACTIVE (tradable) + VAULT_COLD (locked)
- Trigger transfer : total_equity ≥ last_vault_trigger × 1.20
- Quantum : 50% du gain depuis dernier trigger
- Cooldown : max 1 transfer / 12h
- R sizing : 2% de active_capital (pas total incl. vault)
- Vault-back : interdit auto (réinjection = validation opérateur explicite)
- Si active capital chute 10% sous HWM actif : R passe 1R fixe
- Si chute 20% : pause new opens 6h

Activation runtime (Phase E E8 first trade) :
- Add VaultState model (DB table) — id, hwm, active_capital, vault_balance,
  last_trigger_at, last_trigger_equity
- Hook vault_step() après chaque close (paper + live)
- Tier resolution lit active_capital (pas total)
- UI panel /vault/status

Aujourd'hui (Phase A) : skeleton + simulateur + tests. Pas activé runtime.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlmodel import Session, select

from app.models.bot import BotState
from app.models.trade import PaperTrade

# --------- Locked spec (T2 Round 2 review 2026-05-11) ---------

VAULT_TRIGGER_RATIO = 1.20  # transfer when equity ≥ last_trigger × 1.20
VAULT_QUANTUM_PCT = 0.50  # transfer 50% of gain since last trigger
VAULT_COOLDOWN_HOURS = 12.0
SIZING_DD_REDUCE_THRESHOLD = 0.10  # if -10% below active HWM, R → 1R fixed
SIZING_PAUSE_THRESHOLD = 0.20  # if -20% below active HWM, pause opens 6h
PAUSE_DURATION_HOURS = 6.0


@dataclass
class VaultState:
    """In-memory representation of vault state — no DB table yet (Phase A skeleton).

    Phase E will persist this in a VaultState SQLModel table and hydrate at boot.
    """
    hwm: float
    active_capital: float
    vault_balance: float
    last_trigger_at: datetime | None
    last_trigger_equity: float
    paused_until: datetime | None
    sizing_mode: str  # "FULL" | "REDUCED_1R" | "PAUSED"

    @property
    def total_equity(self) -> float:
        return self.active_capital + self.vault_balance

    @property
    def dd_below_hwm_pct(self) -> float:
        if self.hwm <= 0:
            return 0.0
        return max(0.0, (self.hwm - self.active_capital) / self.hwm)


@dataclass
class VaultStepDecision:
    """Result of one vault_step() — what the caller should do (transfer, pause, etc.)."""
    state_after: VaultState
    transfer_amount: float
    transfer_triggered: bool
    cooldown_blocked: bool
    sizing_mode_changed: bool
    new_paused_until: datetime | None
    reasoning: str


def vault_step(
    state: VaultState, *, current_pnl_realized_total: float, now: datetime | None = None,
) -> VaultStepDecision:
    """Pure function — given current vault state + current realized PnL,
    decide if transfer triggers + sizing mode changes.

    Caller responsibility :
    - Call after each close
    - Persist state_after if changes
    - If transfer_triggered=True : execute the actual transfer (Polygon tx in
      live mode, simulated journal entry in paper mode)
    """
    now = now or datetime.now(UTC)
    new_active = state.active_capital + (current_pnl_realized_total - 0.0)  # caller sums correctly
    new_total = new_active + state.vault_balance
    new_hwm = max(state.hwm, new_total)

    # Cooldown check
    cooldown_blocked = False
    if state.last_trigger_at is not None:
        last = state.last_trigger_at if state.last_trigger_at.tzinfo else state.last_trigger_at.replace(tzinfo=UTC)
        if (now - last).total_seconds() < VAULT_COOLDOWN_HOURS * 3600:
            cooldown_blocked = True

    # Trigger condition: total_equity ≥ last_trigger_equity × 1.20
    transfer_amount = 0.0
    transfer_triggered = False
    new_last_trigger_equity = state.last_trigger_equity
    new_last_trigger_at = state.last_trigger_at
    new_vault = state.vault_balance
    new_active_post_xfer = new_active

    threshold_equity = state.last_trigger_equity * VAULT_TRIGGER_RATIO if state.last_trigger_equity > 0 else state.hwm * VAULT_TRIGGER_RATIO
    if not cooldown_blocked and new_total >= threshold_equity:
        gain_since_last = new_total - state.last_trigger_equity
        transfer_amount = round(VAULT_QUANTUM_PCT * gain_since_last, 4)
        if transfer_amount > 0:
            transfer_triggered = True
            new_vault = state.vault_balance + transfer_amount
            new_active_post_xfer = new_active - transfer_amount
            new_last_trigger_equity = new_total
            new_last_trigger_at = now

    # Sizing mode based on DD vs HWM (use post-transfer active capital)
    dd_pct = max(0.0, (new_hwm - new_active_post_xfer - new_vault) / max(new_hwm, 1.0))
    new_paused_until = state.paused_until
    if dd_pct >= SIZING_PAUSE_THRESHOLD:
        new_sizing = "PAUSED"
        new_paused_until = now + timedelta(hours=PAUSE_DURATION_HOURS)
    elif dd_pct >= SIZING_DD_REDUCE_THRESHOLD:
        new_sizing = "REDUCED_1R"
    else:
        new_sizing = "FULL"

    # Clear pause if elapsed
    if new_sizing != "PAUSED" and state.paused_until and now >= state.paused_until:
        new_paused_until = None

    sizing_changed = new_sizing != state.sizing_mode

    new_state = VaultState(
        hwm=round(new_hwm, 4),
        active_capital=round(new_active_post_xfer, 4),
        vault_balance=round(new_vault, 4),
        last_trigger_at=new_last_trigger_at,
        last_trigger_equity=round(new_last_trigger_equity, 4),
        paused_until=new_paused_until,
        sizing_mode=new_sizing,
    )

    reasoning = []
    if transfer_triggered:
        reasoning.append(f"transfer {transfer_amount:.2f} (gain {gain_since_last:.2f} × {VAULT_QUANTUM_PCT})")
    elif cooldown_blocked:
        reasoning.append("cooldown_blocked")
    if sizing_changed:
        reasoning.append(f"sizing {state.sizing_mode}→{new_sizing} (dd={dd_pct*100:.1f}%)")
    if not reasoning:
        reasoning.append("no-op (no trigger, no sizing change)")

    return VaultStepDecision(
        state_after=new_state,
        transfer_amount=transfer_amount,
        transfer_triggered=transfer_triggered,
        cooldown_blocked=cooldown_blocked,
        sizing_mode_changed=sizing_changed,
        new_paused_until=new_paused_until,
        reasoning=" + ".join(reasoning),
    )


def initial_state(starting_capital: float = 100.0) -> VaultState:
    """Factory for a fresh vault state at the start of a session/baseline."""
    return VaultState(
        hwm=starting_capital,
        active_capital=starting_capital,
        vault_balance=0.0,
        last_trigger_at=None,
        last_trigger_equity=starting_capital,
        paused_until=None,
        sizing_mode="FULL",
    )


def simulate_trajectory(
    daily_returns_pct: list[float], starting_capital: float = 100.0,
) -> list[dict[str, Any]]:
    """Simulate vault trajectory given a list of daily returns (decimal, e.g. 0.10
    = +10%). Returns the daily snapshot list — capital_actif / vault / total /
    transfer / sizing_mode / dd_pct.

    Used for:
    - Phase A E-VAULT design validation (table simulation 30 jours dans plan)
    - Phase F scaling palier-by-palier sanity check
    - Operator preview avant flip vault runtime
    """
    state = initial_state(starting_capital)
    rows: list[dict[str, Any]] = []
    rows.append({
        "day": 0,
        "active": state.active_capital,
        "vault": state.vault_balance,
        "total": state.total_equity,
        "transfer": 0.0,
        "sizing_mode": state.sizing_mode,
        "dd_pct": 0.0,
    })
    now = datetime.now(UTC)
    for i, r in enumerate(daily_returns_pct, start=1):
        gain = state.active_capital * r
        decision = vault_step(
            state,
            current_pnl_realized_total=gain,
            now=now + timedelta(days=i),
        )
        state = decision.state_after
        rows.append({
            "day": i,
            "active": state.active_capital,
            "vault": state.vault_balance,
            "total": state.total_equity,
            "transfer": decision.transfer_amount,
            "sizing_mode": state.sizing_mode,
            "dd_pct": round(state.dd_below_hwm_pct * 100, 2),
        })
    return rows


def vault_status_payload(session: Session) -> dict[str, Any]:
    """Best-effort vault status for /vault/status endpoint.

    Phase A: no VaultState DB table yet → returns simulated current state
    based on current paper_capital + cumulative realized_pnl post-cutover.
    Operator sees the vault projection without needing the live infra.
    """
    state_row = session.get(BotState, 1)
    starting = float((state_row.paper_capital if state_row else 100.0) or 100.0)

    # Pull current realized PnL post-cutover (single SQL aggregate)
    from sqlalchemy import func
    cutover = getattr(state_row, "strict_cutover_at", None) if state_row else None
    if cutover is not None and cutover.tzinfo is None:
        cutover = cutover.replace(tzinfo=UTC)
    if cutover is not None:
        total = session.exec(
            select(func.sum(PaperTrade.realized_pnl))
            .where(PaperTrade.opened_at >= cutover)
        ).one()
    else:
        total = session.exec(select(func.sum(PaperTrade.realized_pnl))).one()
    realized = float(total or 0.0)

    state = initial_state(starting)
    decision = vault_step(state, current_pnl_realized_total=realized)
    return {
        "mode": "SIMULATION (Phase A — no live tx)",
        "starting_capital": starting,
        "realized_pnl_post_cutover": realized,
        "vault_state": asdict(decision.state_after),
        "transfer_amount_if_triggered": decision.transfer_amount,
        "transfer_triggered": decision.transfer_triggered,
        "cooldown_blocked": decision.cooldown_blocked,
        "sizing_mode": decision.state_after.sizing_mode,
        "dd_below_hwm_pct": round(decision.state_after.dd_below_hwm_pct * 100, 2),
        "reasoning": decision.reasoning,
        "spec_locked": {
            "trigger_ratio": VAULT_TRIGGER_RATIO,
            "quantum_pct": VAULT_QUANTUM_PCT,
            "cooldown_hours": VAULT_COOLDOWN_HOURS,
            "dd_reduce_threshold": SIZING_DD_REDUCE_THRESHOLD,
            "pause_threshold": SIZING_PAUSE_THRESHOLD,
            "pause_duration_hours": PAUSE_DURATION_HOURS,
        },
    }
