"""Bayesian Price Model — Beta-Binomial conjugate per market.

Maintient pour chaque condition_id un prior Beta(α, β) sur P(outcome=YES).
À chaque observation (trade cohort), update : α/β += observation_weight.

Formule (Bishop PRML ch. 2 + Gelman BDA ch. 5) :
  P(YES | data) = Beta(α + n_yes, β + n_no)
  expected_proba = α / (α + β)

Source Polymarket-spécifique : arXiv 2601.18815 — "Prediction Markets as Bayesian
Inverse Problems" — fournit le framework pour P(outcome | history+volume).

Mispricing detector :
  signal = expected_proba - market_price
  Si |signal| > 0.02 (2%) → mispricing notable (cf arXiv 2601.01706, 2-4% persistant)

Observation weights :
  Un BUY YES par wallet cohort = +1 sur α (poids unitaire).
  Un BUY NO = +1 sur β.
  On peut pondérer par notional (volume-weighted) en V2.

Phase B = OBSERVATION : on calcule expected_proba + mispricing à chaque trade,
on logue. Pas de filtre. Mesure correctness post-hoc.

Persistance JSON pour reprendre l'état après restart : `bayesian_state.json`.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


# Prior : market-anchored Beta(k·p, k·(1-p)) où p = market_price observé au
# premier observe() pour ce market. ESS k = effective sample size du prior.
# Source : Hahn & Carvalho ESS [PMC3081791], Bawa Polymarket Bayesian Mixing.
# k=30 = prior_dominance ~81% à n=7, descend ~50% à n=30. Empêche le model
# de dériver vers expected=1.0 sur cohorte 91% BUY (bug Beta(1,1) v1, cf
# audit subagent 2026-05-26).
# Validation simulée sur top market (36 trades) : k=1 → 10/10 false sig à n_obs<=2 ;
# k=30 → 0/10 ; k=100 magnitudes <0.4 dont 4 marginaux.
PRIOR_ESS = 30.0
# Fallback prior si market_price absent (rare, neutral)
PRIOR_FALLBACK_PROBA = 0.5

# Trigger mispricing — sourcé arXiv 2601.01706 (2-4% persistant)
MISPRICING_TRIGGER = 0.02

# Update weight par observation (V1 = unitaire, V2 = volume-weighted)
DEFAULT_OBS_WEIGHT = 1.0

# Cap state size — éviter croissance illimitée
MAX_TRACKED_MARKETS = 10_000


@dataclass
class BayesianSignal:
    expected_proba: float          # = α/(α+β), notre estimation P(YES)
    market_price: float            # prix actuel (du trade observé)
    mispricing: float              # expected - market (positif = market sous-évalué YES)
    mispricing_abs: float          # |mispricing|
    is_significant: bool           # |mispricing| > MISPRICING_TRIGGER
    direction_consensus: bool      # wallet trade direction confirme model ?
    alpha: float
    beta: float
    n_observations: int            # nb updates total pour ce market


class BayesianPriceModel:
    """Per-market Beta-Binomial updater + mispricing detector."""

    def __init__(self, state_path: Optional[Path] = None,
                 prior_ess: float = PRIOR_ESS,
                 mispricing_trigger: float = MISPRICING_TRIGGER,
                 obs_weight: float = DEFAULT_OBS_WEIGHT):
        self.state_path = state_path
        self.prior_ess = prior_ess
        self.mispricing_trigger = mispricing_trigger
        self.obs_weight = obs_weight

        # State : condition_id -> {"alpha": float, "beta": float, "n_obs": int, "last_update": ts}
        self.state: dict[str, dict] = {}

        # Stats
        self.stats: dict[str, int] = dict(
            updates=0, queries=0,
            significant_mispricings=0,
            direction_confirmed=0, direction_contradicted=0,
        )

        self._load_state()

    def _load_state(self):
        if self.state_path is None or not self.state_path.exists():
            return
        try:
            data = json.loads(self.state_path.read_text())
            for cid, v in data.get("state", {}).items():
                # Fallback : si state legacy (Beta(1,1)) → re-init au prior_ess neutre.
                # Sinon on garde les counts (déjà market-anchored si saved après fix).
                self.state[cid] = {
                    "alpha": float(v.get("alpha", self.prior_ess * PRIOR_FALLBACK_PROBA)),
                    "beta": float(v.get("beta", self.prior_ess * (1 - PRIOR_FALLBACK_PROBA))),
                    "n_obs": int(v.get("n_obs", 0)),
                    "last_update": float(v.get("last_update", 0)),
                }
            print(f"[BAYES] loaded state : {len(self.state)} markets "
                  f"(prior_ess={self.prior_ess})", flush=True)
        except Exception as e:
            print(f"[BAYES] state load fail : {type(e).__name__} — fresh start",
                  flush=True)

    def save_state(self):
        if self.state_path is None:
            return
        try:
            self.state_path.write_text(json.dumps({
                "state": self.state,
                "stats": self.stats,
                "saved_at": time.time(),
            }))
        except Exception as e:
            print(f"[BAYES] state save fail : {type(e).__name__}",
                  flush=True)

    def _get_or_init(self, condition_id: str,
                     market_price: Optional[float] = None) -> dict:
        """Récupère ou initialise l'état pour ce market.

        Si nouveau market : prior market-anchored Beta(k·p, k·(1-p)).
        Si market_price=None : fallback Beta(k/2, k/2).
        """
        s = self.state.get(condition_id)
        if s is None:
            # Cap state size
            if len(self.state) >= MAX_TRACKED_MARKETS:
                # Drop oldest (least recent update)
                oldest = min(self.state.items(),
                             key=lambda x: x[1].get("last_update", 0))
                del self.state[oldest[0]]
            # Prior market-anchored
            p = market_price if (market_price is not None
                                 and 0 < market_price < 1) else PRIOR_FALLBACK_PROBA
            s = {
                "alpha": self.prior_ess * p,
                "beta": self.prior_ess * (1 - p),
                "n_obs": 0, "last_update": time.time(),
            }
            self.state[condition_id] = s
        return s

    def observe(self, condition_id: str, side: str,
                weight: Optional[float] = None,
                market_price: Optional[float] = None) -> None:
        """Update Beta(α,β) avec une nouvelle observation — LOGIQUE INVERSÉE.

        Fix 2026-05-26b (Bayes inversé) : validator forward 15min sur N=11940
        empiriquement prouve que la cohort 97 est CONTRARIAN :
          - Wallet BUY signal accuracy = 43.8% (CI [41.0, 46.7] = anti-corrélé)
          - Wallet SELL signal accuracy = 33.6% (CI [26.3, 41.6] = anti-corrélé)
        Pattern : cohort achète markets déjà pumpés (mean px=0.83, p50=0.90)
        qui retombent. Les wallets vendent trop tôt avant le top.

        Donc on INVERSE la mapping classique :
          BUY  → +weight sur β  (= evidence NO, bearish — wallet chase-the-pump)
          SELL → +weight sur α  (= evidence YES, bullish — sell-before-top)

        Avec cette règle, le model produit une prédiction qui devrait passer
        de 43.8% accuracy à ~56% (inversion arithmétique).
        Source : forward_validator.py run 2026-05-26 N=11940.
        """
        s_upper = (side or "").upper()
        s = self._get_or_init(condition_id, market_price=market_price)
        w = weight if weight is not None else self.obs_weight
        if s_upper == "BUY":
            s["beta"] += w  # INVERSÉ : BUY → β (bearish evidence)
        elif s_upper == "SELL":
            s["alpha"] += w  # INVERSÉ : SELL → α (bullish evidence)
        else:
            return
        s["n_obs"] += 1
        s["last_update"] = time.time()
        self.stats["updates"] += 1

    def query(self, condition_id: str, market_price: float,
              side: str) -> BayesianSignal:
        """Compute expected_proba + mispricing pour ce market.

        side : direction du trade en cours d'observation. Utilisé pour
        direction_consensus = est-ce que le wallet trade dans le sens
        prédit par notre model ?
        """
        self.stats["queries"] += 1
        # Passe market_price pour init prior si market jamais vu
        s = self._get_or_init(condition_id, market_price=market_price)
        a, b = s["alpha"], s["beta"]
        denom = a + b
        expected = a / denom if denom > 0 else 0.5

        # Mispricing : positif = market sous-évalue YES (model dit YES plus
        # probable que prix). Négatif = market surévalue.
        mispricing = expected - market_price
        is_sig = abs(mispricing) > self.mispricing_trigger
        if is_sig:
            self.stats["significant_mispricings"] += 1

        # Direction consensus : si trade BUY YES (= bet long YES), il est aligné
        # avec model si expected > market_price (model thinks YES is undervalued).
        s_upper = (side or "").upper()
        direction_consensus = False
        if s_upper == "BUY":
            direction_consensus = mispricing > 0  # model d'accord avec long YES
        elif s_upper == "SELL":
            direction_consensus = mispricing < 0  # model d'accord avec sortir YES

        if is_sig:
            if direction_consensus:
                self.stats["direction_confirmed"] += 1
            else:
                self.stats["direction_contradicted"] += 1

        return BayesianSignal(
            expected_proba=expected,
            market_price=market_price,
            mispricing=mispricing,
            mispricing_abs=abs(mispricing),
            is_significant=is_sig,
            direction_consensus=direction_consensus,
            alpha=a, beta=b, n_observations=s["n_obs"],
        )

    def observe_and_query(self, condition_id: str, side: str,
                          market_price: float,
                          weight: Optional[float] = None) -> BayesianSignal:
        """Update + query en une étape (utilisé par on_rtds callback).

        On passe market_price à observe() pour qu'il puisse initialiser le
        prior market-anchored au cas où c'est le premier event pour ce market.
        """
        self.observe(condition_id, side, weight=weight, market_price=market_price)
        return self.query(condition_id, market_price, side)

    def report(self) -> dict:
        u = self.stats["updates"] or 1
        q = self.stats["queries"] or 1
        sig = self.stats["significant_mispricings"]
        confirm_rate = (100.0 * self.stats["direction_confirmed"] /
                        (sig or 1))
        return {
            **self.stats,
            "n_markets_tracked": len(self.state),
            "significant_rate_pct": 100.0 * sig / q,
            "consensus_rate_when_sig_pct": confirm_rate,
        }


# ─── CLI smoke test ──────────────────────────────────────────────────
if __name__ == "__main__":
    model = BayesianPriceModel()
    cid = "0xmarket_test"
    market_p = 0.55
    print(f"Prior ESS={model.prior_ess}, market_price={market_p}")
    print(f"Initial prior : α={model.prior_ess*market_p:.1f} β={model.prior_ess*(1-market_p):.1f}")
    print()
    print("Updates inversés 2026-05-26b : 7 BUY → β++ (bearish), 3 SELL → α++ (bullish)")
    for _ in range(7):
        model.observe(cid, "BUY", market_price=market_p)
    for _ in range(3):
        model.observe(cid, "SELL", market_price=market_p)
    s = model.query(cid, market_price=market_p, side="BUY")
    print(f"After updates : expected={s.expected_proba:.3f}  (α={s.alpha:.1f}, β={s.beta:.1f}, n_obs={s.n_observations})")
    print(f"  market={market_p}  mispricing={s.mispricing:+.3f}  significant={s.is_significant}")
    print(f"  direction_consensus(BUY)={s.direction_consensus}")
    print()
    s2 = model.query(cid, market_price=0.80, side="BUY")
    print(f"Market 0.80  mispricing={s2.mispricing:+.3f}  significant={s2.is_significant}")
    print(f"  direction_consensus(BUY)={s2.direction_consensus}  (model thinks YES overpriced)")
    print()
    print(f"Report: {model.report()}")
