"""Launcher Polymarket V2 paper — Phase A minimal wiring.

Pipeline Phase A :
  preflight → load cohort → RTDSListener → LeanGates → log only
  (PAS d'execution, PAS de Bayesian, PAS de OBI — c'est Phase B+C)

Objectif Phase A : valider en prod
  1. Détection trades cohort < 100ms (latency_arrival_ms p50)
  2. LeanGates ne bloquent que les vrais cas (pas de over-rejection)
  3. Stabilité 6-12h (pas de crash, pas de fuite mémoire)

Service systemd recommandé : polyoracle-v2-paper.service
"""
from __future__ import annotations

import asyncio
import json
import os
import signal
import sqlite3
import sys
import time
import urllib.request
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from app.services.polymarket_v2.bayesian_price_model import BayesianPriceModel
from app.services.polymarket_v2.fast_executor import FastExecutor
from app.services.polymarket_v2.lean_gates import CopySignal, LeanGates
from app.services.polymarket_v2.obi_signal import OBISignalEngine
from app.services.polymarket_v2.preflight import PreflightError, run_preflight
from app.services.polymarket_v2.rtds_listener import RtdsEvent, RtdsListener
from app.services.polymarket_v2.scope_gate import scope_check


# ─── R1 V2 audit flags (2026-05-27) ──────────────────────────────────
# Audit R1 V2 verdict :
#   - Bayes FAIL_neg massif sur p_70+ (35-43%) — désactivé temporairement
#   - OBI directional 65.5% accuracy sur crypto (CI lo 59.1%, N=229)
#   - Maker rebate 20% rend break-even crypto possible (51.7% maker
#     vs 72.7% taker) — exécution maker-only obligatoire
BAYES_ENABLED = os.getenv("BAYES_ENABLED", "false").lower() == "true"
SCOPE_FILTER_ENABLED = os.getenv("SCOPE_FILTER_ENABLED", "true").lower() == "true"
FAST_EXECUTOR_ENABLED = os.getenv("FAST_EXECUTOR_ENABLED", "true").lower() == "true"
# Default dry_run=true : MVP paper-mode, pas de live clob_client encore wired.
FAST_EXECUTOR_DRY_RUN = os.getenv("FAST_EXECUTOR_DRY_RUN", "true").lower() == "true"


_REPO_ROOT = Path(__file__).resolve().parents[4]
DB_PATH = _REPO_ROOT / "data" / "polyoracle.db"
PAPER_DIR = _REPO_ROOT / "data" / "v2_paper"
PAPER_DIR.mkdir(parents=True, exist_ok=True)
EVENTS_LOG = PAPER_DIR / "events.jsonl"
LAUNCHER_LOG = PAPER_DIR / "launcher.log"
KILL_SWITCH = Path("/tmp/polyoracle_v2_kill")
# Phase B state
BAYES_STATE_PATH = PAPER_DIR / "bayesian_state.json"
# Sleeve FAVORIS taker (2026-05-28, audit Opus 4.8) — Route B.
# Remplace l'ancien sleeve longshot (cohort_noncrypto_validated.json, réfuté
# forward 1/63) par les 71 wallets favorite-edge (train+holdout+, px 0.60-0.90).
NONCRYPTO_COHORT_PATH = PAPER_DIR / "cohort_favorite_taker.json"

COHORT_STATUS = "ELITE"
MIN_COHORT_SIZE = 50

# Phase A : capital NANO $100, exposure max 80%
MAX_CONCURRENT = 8
MAX_NOTIONAL_PER_TRADE = 25.0
MAX_TOTAL_EXPOSURE = 80.0

# R-based sizing (operator spec, spec.md — remplace le bug $20 fixe).
# R = paper_capital × RISK_PER_TRADE ; target = R × current_r_multiplier
# (state machine 2R win / 1R loss, lue depuis BotState), floor MIN_STAKE,
# cap 2R. À $100 NANO : R=$1 → $1 (1R losing) ou $2 (2R winning).
RISK_PER_TRADE = 0.01
MIN_STAKE_USD = 1.0
R_WIN_CAP_MULT = 2.0  # jamais > 2R (spec)

# ── Favorite-taker sleeve (2026-05-28, audit Opus 4.8) ──────────────────
# Verdict : edge "longshot Bonferroni" 4.7 = artefact (réfuté forward 1/63).
# Edge RÉEL = favorite-longshot bias (px 0.60-0.90) : 71 wallets train+holdout+,
# Spearman +0.27, stable 3 mois, +0.067 gross. MAKER copy = toxic-filled (~0
# capture, adverse selection) → TAKER. Net +0.043/tr in-cache (15511) / +0.114
# forward OOS. Gate : side BUY + px∈[0.60,0.90) + spread CLOB ≤ 3¢.
FAV_PRICE_LO = float(os.getenv("FAV_PRICE_LO", "0.60"))
FAV_PRICE_HI = float(os.getenv("FAV_PRICE_HI", "0.90"))
FAV_MAX_SPREAD = float(os.getenv("FAV_MAX_SPREAD", "0.03"))
FAV_FEE_RATE = float(os.getenv("FAV_FEE_RATE", "0.05"))  # weather/other dominant
_CLOB_BOOK_URL = "https://clob.polymarket.com/book?token_id={t}"


def fetch_bbo(token_id, timeout=3.0):
    """(best_bid, best_ask) depuis CLOB REST book, ou None. Bloquant→to_thread."""
    try:
        req = urllib.request.Request(
            _CLOB_BOOK_URL.format(t=token_id),
            headers={"User-Agent": "PolyV2-taker/1.0", "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            book = json.loads(r.read())
        bids = book.get("bids") or []
        asks = book.get("asks") or []
        if not bids or not asks:
            return None
        return (max(float(x["price"]) for x in bids),
                min(float(x["price"]) for x in asks))
    except Exception:
        return None


def log(msg: str):
    line = f"[{time.strftime('%Y-%m-%dT%H:%M:%S')}] {msg}"
    print(line, flush=True)
    try:
        with open(LAUNCHER_LOG, "a") as fh:
            fh.write(line + "\n")
    except Exception:
        pass


def load_cohort() -> set[str]:
    """Charge la cohorte ELITE depuis la DB (97 directional anti-MM expected)."""
    conn = sqlite3.connect(str(DB_PATH))
    rows = conn.execute(
        "SELECT address FROM marketfirstwalletrecord WHERE candidate_status=?",
        (COHORT_STATUS,),
    ).fetchall()
    conn.close()
    return {r[0].lower() for r in rows if r[0]}


def read_bot_state_sizing() -> tuple[float, float]:
    """(paper_capital, current_r_multiplier) depuis BotState id=1.

    BotState est la source of truth (spec.md P5-A). Fallback NANO $100 / 1R
    si la lecture échoue — jamais de crash sizing."""
    try:
        conn = sqlite3.connect(str(DB_PATH))
        row = conn.execute(
            "SELECT paper_capital, current_r_multiplier FROM botstate WHERE id=1"
        ).fetchone()
        conn.close()
        if row:
            cap = float(row[0]) if row[0] is not None else 100.0
            r = float(row[1]) if row[1] is not None else 1.0
            return cap, r
    except Exception:
        pass
    return 100.0, 1.0


def compute_target_notional(paper_capital: float, r_multiplier: float) -> float:
    """Target $ R-based (spec opérateur). R = capital × RISK_PER_TRADE ;
    target = clamp(MIN_STAKE, R × r_mult, 2R)."""
    R = max(0.0, float(paper_capital)) * RISK_PER_TRADE
    target = R * float(r_multiplier)
    return max(MIN_STAKE_USD, min(R_WIN_CAP_MULT * R, target))


def load_noncrypto_validated() -> set[str]:
    """Wallets du sleeve FAVORIS taker (Route B). 71 wallets favorite-edge
    (train+holdout+, px 0.60-0.90). Exécution taker + gate spread dans on_rtds.
    Set vide si le fichier n'existe pas (sleeve désactivé)."""
    try:
        data = json.loads(NONCRYPTO_COHORT_PATH.read_text())
        return {w["addr"].lower() for w in data.get("wallets", []) if w.get("addr")}
    except Exception:
        return set()


def jsonl_append(path: Path, obj: dict):
    try:
        with open(path, "a") as fh:
            fh.write(json.dumps(obj) + "\n")
    except Exception:
        pass


async def main():
    log("=" * 70)
    log("POLYMARKET V2 PAPER — Phase A (detection + lean gates, no execution)")
    log("=" * 70)

    # Preflight
    try:
        run_preflight(
            db_path=DB_PATH,
            cohort_status=COHORT_STATUS,
            min_cohort_size=MIN_COHORT_SIZE,
            require_clob_pk=False,  # Phase A : no execution
        )
    except PreflightError as e:
        log(f"PREFLIGHT FAILED — abort: {e}")
        return

    # Cohort
    cohort = load_cohort()
    log(f"Cohort loaded: {len(cohort)} {COHORT_STATUS} wallets")
    if len(cohort) < MIN_COHORT_SIZE:
        log(f"Cohort too small — abort")
        return

    # Route B — sleeve non-crypto validé (copy holdout-validé). On l'UNION dans
    # la cohorte pollée (dedup auto) ; le routage exec se fait dans on_rtds.
    noncrypto_validated = load_noncrypto_validated()
    if noncrypto_validated:
        before = len(cohort)
        cohort = cohort | noncrypto_validated
        log(f"Non-crypto validated sleeve: {len(noncrypto_validated)} wallets "
            f"(+{len(cohort) - before} new) -> total polled cohort {len(cohort)}")
    else:
        log("Non-crypto validated sleeve: 0 wallets (file absent, Route B off)")

    # State : simulé pour Phase A (pas de réelle open positions tracking)
    # + sizing R-based lu depuis BotState (remplace le $20 fixe).
    cap, r_mult = read_bot_state_sizing()
    target_notional = compute_target_notional(cap, r_mult)
    state = {
        "n_open": 0, "current_exposure_usd": 0.0,
        "paper_capital": cap, "current_r_multiplier": r_mult,
        "target_notional": target_notional,
    }
    log(f"Sizing R-based: capital=${cap:.2f} risk={RISK_PER_TRADE} "
        f"r_mult={r_mult} -> target=${target_notional:.2f} "
        f"(floor ${MIN_STAKE_USD}, cap 2R=${R_WIN_CAP_MULT * cap * RISK_PER_TRADE:.2f})")

    # Gates
    gates = LeanGates(
        kill_switch_path=KILL_SWITCH,
        max_concurrent=MAX_CONCURRENT,
        max_notional_per_trade=MAX_NOTIONAL_PER_TRADE,
        max_total_exposure=MAX_TOTAL_EXPOSURE,
    )
    log(f"Gates: max_conc={MAX_CONCURRENT} max_notional=${MAX_NOTIONAL_PER_TRADE} "
        f"max_exposure=${MAX_TOTAL_EXPOSURE} kill_switch={KILL_SWITCH}")

    # Phase B — Signal quant : OBI + (Bayesian disabled depuis R1 V2 fix).
    # Mode OBSERVATION + R4 scope_check + maker-only exec.
    obi_engine = OBISignalEngine()
    bayes_model = BayesianPriceModel(state_path=BAYES_STATE_PATH)
    log(f"Phase B signal layer: OBI engine + Bayesian model "
        f"(BAYES_ENABLED={BAYES_ENABLED} SCOPE_FILTER_ENABLED={SCOPE_FILTER_ENABLED} "
        f"FAST_EXECUTOR_ENABLED={FAST_EXECUTOR_ENABLED} "
        f"FAST_EXECUTOR_DRY_RUN={FAST_EXECUTOR_DRY_RUN})")

    # R1 V2 Change 4 — maker-only fast executor.
    # Paper-mode (`dry_run=True`) tant que clob_client live pas wired ici.
    fast_executor = FastExecutor(
        clob_client=None,
        dry_run=FAST_EXECUTOR_DRY_RUN,
    ) if FAST_EXECUTOR_ENABLED else None

    # PnL reconciler — résout les fills copiés contre la VRAIE résolution
    # (Gamma outcomePrices>=0.999 / resolvedmarketrecord). Settlement
    # déterministe = paper=live faithful. Positions persistées (survit restart).
    try:
        from app.services.polymarket.gamma_client import GammaClient
        from app.services.market_resolution_scanner import MarketResolutionScanner
        from app.services.polymarket_v2.reconciler import PnLReconciler
        reconciler = PnLReconciler(
            positions_path=PAPER_DIR / "positions.jsonl",
            gamma_client=GammaClient(timeout=15),
            scanner=MarketResolutionScanner(),
            db_path=str(DB_PATH),
        )
        log(f"PnL reconciler: {len(reconciler.positions)} positions chargées "
            f"(positions.jsonl)")
    except Exception as e:
        reconciler = None
        log(f"[INIT] PnL reconciler unavailable ({type(e).__name__}: {e})")

    # DB session pour resolve_category_with_fallback.
    # Lazy import — éviter cycles et payload startup. On utilise une
    # session par lookup (cheap, SQLite local) et on swallow toute erreur.
    try:
        from app.database import engine as _db_engine
        from sqlmodel import Session as _DBSession
        _db_available = True
    except Exception as e:
        log(f"[INIT] DB engine unavailable ({type(e).__name__}: {e})"
            " — category fallback will be 'unknown'")
        _db_engine = None
        _DBSession = None
        _db_available = False

    from app.services.category_resolver import resolve_category_with_fallback

    def _resolve_market_category(cond_id: str) -> str:
        if not _db_available or not cond_id:
            return "unknown"
        try:
            with _DBSession(_db_engine) as sess:
                cat, _src = resolve_category_with_fallback(
                    sess, cond_id, hint_category=None)
                return (cat or "unknown").lower()
        except Exception:
            return "unknown"

    # Callback : RTDS event → CopySignal → LeanGates → (Phase B) OBI + Bayes → log
    async def on_rtds(ev: RtdsEvent):
        trade = ev.trade
        signal = CopySignal(
            trader=ev.wallet,
            token_id=str(trade.get("asset", "")),
            condition_id=str(trade.get("conditionId", "") or trade.get("market", "")),
            side=str(trade.get("side", "")).upper(),
            notional_usd=float(trade.get("size", 0) or 0) * float(trade.get("price", 0) or 0),
            price=float(trade.get("price", 0) or 0),
            ts_ms=ev.ts_event_arrival_ms,
            source_ts_ms=ev.ts_trade_ms,
            market_title=str(trade.get("title", ""))[:80],
        )
        # Route B helper : détecter crypto-updown (univers mort) via slug/title,
        # sans dépendre du resolver de catégorie (fragile).
        _slug = str(trade.get("slug", "")).lower()
        _title_lc = str(trade.get("title", "")).lower()
        is_updown = ("updown" in _slug or "up-or-down" in _slug
                     or "up or down" in _title_lc or "-up-" in _slug)
        decision = gates.evaluate(
            signal,
            n_open=state["n_open"],
            current_exposure_usd=state["current_exposure_usd"],
            target_notional_usd=state["target_notional"],
        )

        # Phase B — calcul OBI (toujours pour observation, même sur REJECTs).
        # Bayesian : conditionnel BAYES_ENABLED (R1 V2 fix, Bayes FAIL_neg p_70+).
        obi_sig = None
        bayes_sig = None
        # OBI via CLOB REST — peut rate-limit, on swallow gracefully (run dans
        # un thread car urllib bloquant).
        try:
            obi_sig = await asyncio.to_thread(
                obi_engine.compute, signal.token_id, signal.side)
        except Exception as e:
            log(f"[OBI err] {type(e).__name__}: {str(e)[:80]}")
        # Bayesian désactivé par défaut depuis R1 V2 (FAIL_neg massif p_70+).
        # On laisse le flag BAYES_ENABLED=true permettre le re-run d'analyse.
        if BAYES_ENABLED:
            try:
                bayes_sig = bayes_model.observe_and_query(
                    condition_id=signal.condition_id,
                    side=signal.side,
                    market_price=signal.price,
                )
            except Exception as e:
                log(f"[BAYES err] {type(e).__name__}: {str(e)[:80]}")

        # R1 V2 Change 3 — scope gate (post-OBI, pre-execute).
        # On évalue le scope MÊME si lean_gates a rejeté (observation).
        # Mais on exécute uniquement si BOTH gates PASS.
        scope_passed = False
        scope_reject_reason: Optional[str] = None
        market_category = "unknown"
        if SCOPE_FILTER_ENABLED:
            obi_val_for_scope = (
                obi_sig.value if obi_sig is not None and not obi_sig.error
                else 0.0
            )
            market_category = _resolve_market_category(signal.condition_id)
            scope_passed, scope_reject_reason = scope_check(
                signal, obi_val_for_scope, market_category)
            if scope_passed:
                scope_reject_reason = None
        else:
            scope_passed = True  # by-pass when feature flag off

        # Log (concis sur stdout, full sur jsonl)
        verdict = "PASS" if decision.allow else "REJECT"
        scope_str = (
            f" SCOPE=OK cat={market_category}" if scope_passed
            else f" SCOPE=NO {scope_reject_reason}"
        ) if SCOPE_FILTER_ENABLED else ""
        obi_str = (f" OBI={obi_sig.value:+.2f}" if obi_sig and not obi_sig.error
                   else " OBI=ERR" if obi_sig else "")
        bay_str = (f" bays.mp={bayes_sig.mispricing:+.2f}"
                   f"{'*' if bayes_sig.is_significant else ''}"
                   if bayes_sig else "")
        log(f"[SIG] {verdict} {signal.trader[:14]} {signal.side} "
            f"px={signal.price:.4f} size_src=${signal.notional_usd:.2f} "
            f"latency={ev.latency_arrival_ms}ms{obi_str}{bay_str}{scope_str} "
            f"reason={decision.reason}")

        # Routage 2-voies (2026-05-28, audit Opus 4.8) :
        #   Route A (crypto) : scope OBI, exécution MAKER post-only (R1 65.5%).
        #   Route B (favoris) : wallet favorite-edge, side BUY, px 0.60-0.90,
        #     hors crypto-updown, exécution TAKER (maker copy = toxic-filled ~0
        #     capture) + spread gate ≤3¢. Edge favorite-longshot bias, net +0.043/tr.
        route: Optional[str] = None
        if decision.allow and fast_executor is not None:
            if scope_passed:
                route = "A_crypto_obi"
            elif (signal.trader.lower() in noncrypto_validated
                  and not is_updown
                  and signal.side == "BUY"
                  and FAV_PRICE_LO <= signal.price < FAV_PRICE_HI):
                route = "B_favorite_taker"

        executed = False
        exec_filled = False
        exec_attempts = 0
        exec_error: Optional[str] = None
        exec_elapsed_s = 0.0
        if route:
            try:
                if route == "B_favorite_taker":
                    # TAKER : fetch BBO réel, spread gate, fill au best_ask.
                    # (maker copy = toxic-filled ~0 capture — audit 2026-05-28.)
                    bbo = await asyncio.to_thread(fetch_bbo, signal.token_id)
                    executed = True
                    exec_attempts = 1
                    _fill_px = 0.0
                    if bbo is None:
                        exec_error = "TAKER_NOBOOK"
                    else:
                        best_bid, best_ask = bbo
                        spread = best_ask - best_bid
                        if spread > FAV_MAX_SPREAD:
                            exec_error = f"SPREAD_GATE {spread:.3f}"
                        else:
                            exec_filled = True
                            _fill_px = best_ask
                    _flag = "FILLED" if exec_filled else "SKIP"
                    log(f"[EXEC:{route}] {_flag} {signal.trader[:14]} "
                        f"src_px={signal.price:.4f} fill={_fill_px:.4f} "
                        f"err={exec_error or '-'}")
                    if exec_filled and reconciler is not None:
                        _notional = state["target_notional"]
                        _fee = _notional * FAV_FEE_RATE * (1.0 - _fill_px)
                        reconciler.record_fill(
                            route=route, wallet=signal.trader,
                            condition_id=signal.condition_id,
                            token_id=signal.token_id,
                            outcome=str(trade.get("outcome", "")),
                            side=signal.side,
                            entry_price=_fill_px,
                            size=_notional / max(_fill_px, 0.01),
                            notional=_notional,
                            fee=_fee,
                            ts=int(time.time() * 1000),
                        )
                else:
                    # Route A (crypto) : maker post-only inchangé (R1 OBI).
                    exec_result = await fast_executor.place_maker_post_only(
                        token_id=signal.token_id,
                        side=signal.side,
                        size=state["target_notional"] / max(signal.price, 0.01),
                        price_target=signal.price,
                    )
                    executed = True
                    exec_filled = exec_result.filled
                    exec_attempts = exec_result.attempts
                    exec_error = exec_result.error
                    exec_elapsed_s = exec_result.elapsed_s
                    _flag = "FILLED" if exec_filled else "MISS"
                    log(f"[EXEC:{route}] {_flag} {signal.trader[:14]} "
                        f"px={signal.price:.4f} attempts={exec_attempts} "
                        f"elapsed={exec_elapsed_s*1000:.0f}ms "
                        f"err={exec_error or '-'}")
                    if exec_filled and reconciler is not None:
                        reconciler.record_fill(
                            route=route, wallet=signal.trader,
                            condition_id=signal.condition_id,
                            token_id=signal.token_id,
                            outcome=str(trade.get("outcome", "")),
                            side=signal.side,
                            entry_price=exec_result.fill_price or signal.price,
                            size=(exec_result.fill_size
                                  or state["target_notional"] / max(signal.price, 0.01)),
                            notional=state["target_notional"],
                            ts=int(time.time() * 1000),
                        )
            except Exception as e:
                exec_error = f"{type(e).__name__}: {str(e)[:80]}"
                log(f"[EXEC err] {exec_error}")

        # JSONL audit trail — full structured pour post-hoc analyzer
        jsonl_append(EVENTS_LOG, {
            "ts": int(time.time() * 1000),
            "wallet": signal.trader, "side": signal.side,
            "price": signal.price, "notional_src": signal.notional_usd,
            "condition_id": signal.condition_id,
            "token_id": signal.token_id,
            "outcome": str(trade.get("outcome", "")),
            "latency_ms": ev.latency_arrival_ms,
            "allow": decision.allow, "reason": decision.reason,
            # Phase B
            "obi": (None if obi_sig is None or obi_sig.error else {
                "value": obi_sig.value,
                "side_aligned": obi_sig.side_aligned,
                "bullish": obi_sig.bullish,
                "bearish": obi_sig.bearish,
                "bid_qty_top5": obi_sig.bid_qty_sum,
                "ask_qty_top5": obi_sig.ask_qty_sum,
                "book_age_ms": obi_sig.raw_book_age_ms,
            }),
            "bayes": (None if bayes_sig is None else {
                "expected_proba": bayes_sig.expected_proba,
                "mispricing": bayes_sig.mispricing,
                "significant": bayes_sig.is_significant,
                "direction_consensus": bayes_sig.direction_consensus,
                "alpha": bayes_sig.alpha, "beta": bayes_sig.beta,
                "n_obs": bayes_sig.n_observations,
            }),
            # R1 V2 Change 5 — observability scope + maker exec.
            "scope_passed": scope_passed,
            "scope_reject_reason": scope_reject_reason,
            "scope_category": market_category,
            "route": route,
            "executed": executed,
            "maker_filled": exec_filled,
            "maker_fill_attempts": exec_attempts,
            "maker_elapsed_s": exec_elapsed_s,
            "maker_error": exec_error,
        })
        # Phase A : pas d'execution réelle, donc on simule juste
        # mark_cluster_traded uniquement si lean_gate PASS.
        if decision.allow:
            gates.mark_cluster_traded(signal.condition_id)

    # Listener
    listener = RtdsListener(
        cohort_wallets=cohort, on_event=on_rtds,
        verbose=True,
    )

    # Summary loop — 5min en service permanent (prod cadence).
    async def summary_loop():
        interval = 300
        while True:
            await asyncio.sleep(interval)
            # Refresh sizing R-based depuis BotState (capital/r_mult évoluent).
            cap, r_mult = read_bot_state_sizing()
            state["paper_capital"] = cap
            state["current_r_multiplier"] = r_mult
            state["target_notional"] = compute_target_notional(cap, r_mult)
            log(f"[SUMMARY] sizing: capital=${cap:.2f} r_mult={r_mult} "
                f"target=${state['target_notional']:.2f}")
            log(f"[SUMMARY] listener.stats={listener.stats}")
            log(f"[SUMMARY] gates.report={gates.report()}")
            # Phase B
            log(f"[SUMMARY] obi.report={obi_engine.report()}")
            if BAYES_ENABLED:
                log(f"[SUMMARY] bayes.report={bayes_model.report()}")
                # Persiste l'état Bayesian (cumulé sur tous les markets)
                bayes_model.save_state()
            # R1 V2 Change 4 — fast executor report
            if fast_executor is not None:
                log(f"[SUMMARY] exec.report={fast_executor.report()}")
            # PnL reconciler — résout les marchés des positions ouvertes
            # (Gamma blocking → thread) puis logue le PnL réel par route.
            if reconciler is not None:
                try:
                    rec = await asyncio.to_thread(reconciler.resolve_pending)
                    log(f"[SUMMARY] reconciler.resolve={rec}")
                    log(f"[SUMMARY] reconciler.pnl={reconciler.report()}")
                except Exception as e:
                    log(f"[reconciler err] {type(e).__name__}: {str(e)[:80]}")

    # Signal handlers
    stop_event = asyncio.Event()

    def _shutdown(*a):
        log("SIGNAL received — arrêt propre")
        stop_event.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            asyncio.get_event_loop().add_signal_handler(sig, _shutdown)
        except (NotImplementedError, RuntimeError):
            pass

    # Launch
    log("Starting RTDS listener + summary loop")
    listener_task = asyncio.create_task(listener.run())
    summary_task = asyncio.create_task(summary_loop())
    stop_task = asyncio.create_task(stop_event.wait())

    done, pending = await asyncio.wait(
        {listener_task, summary_task, stop_task},
        return_when=asyncio.FIRST_COMPLETED,
    )
    log("One task ended — shutdown")
    listener.stop()
    for t in pending:
        t.cancel()
    log("STOPPED")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log("KeyboardInterrupt — bye")
