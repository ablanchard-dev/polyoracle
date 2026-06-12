"""Forward-looking validator pour Phase B Polymarket V2.

Mesure si les signaux OBI + Bayesian sont prédictifs de la direction du prix
15 minutes après l'event.

Méthodologie (Walk-Forward Validation, source arXiv 2512.12924 et standard
quant) :
  1. Pour chaque event PASS (allow=True), capture (token_id, side, price_t, ts_t)
  2. Fetch prices-history Polymarket via CLOB data API :
     GET https://clob.polymarket.com/prices-history?market={token_id}&interval=1d&fidelity=15
     → list[{t: sec, p: 0-1}]
  3. À ts_t + 15min, interpole price_t+15
  4. Compute actual direction : up si price_t+15 > price_t * 1.005 (spread buffer 0.5%)
     down si < price_t * 0.995, sinon flat (exclu)
  5. Compute predicted direction par signal :
     - OBI bullish (>0.65) → predicts UP
     - OBI bearish (<-0.65) → predicts DOWN
     - Bayes mispricing > +0.02 (model says undervalued YES) → predicts UP
     - Bayes mispricing < -0.02 (model says overvalued YES) → predicts DOWN
  6. Accuracy = matches / total_directional par bucket
  7. Gate Phase B : ≥55% accuracy sur N≥100 events par signal

R1 (2026-05-27) — Split par catégorie + price_bucket :
  - Fetch Gamma /markets?condition_ids={cid} pour récupérer slug + question
    (note : Gamma ne retourne PAS de champ `category` direct — inference via
    `category_inference.infer_category()` sur slug + question)
  - Bucket par price : [0,0.3), [0.3,0.5), [0.5,0.7), [0.7,0.9), [0.9,1.0]
  - Stats globales + par catégorie + par price_bucket + couples (cat × bucket)
  - Verdict PASS/FAIL par sous-pop N≥100 avec CI95 Wilson

Lance offline (pas dans le service runtime). Usage :
  ./.venv/bin/python -m app.services.polymarket_v2.forward_validator

Sources méthodo :
  - Walk-Forward Validation arXiv 2512.12924
  - Brier Score (Cultivate Labs)
  - Phase B gate du plan polymarket-v2 (la-partie-1-et-floofy-volcano.md)
  - R1 split : Favorite-Longshot Bias [NBER w15923]
    https://www.nber.org/system/files/working_papers/w15923/w15923.pdf
  - Polymarket fee formula [Coinmonks Apr 2026]
    https://medium.com/coinmonks/polymarket-just-changed-its-fees-heres-what-bot-traders-need-to-know-c11132e55d5c
"""
from __future__ import annotations

import json
import sys
import time
import urllib.request
from collections import defaultdict
from pathlib import Path
from typing import Optional

# ----- R1 : import category_inference depuis polyoracle backend -----
# Path : backend/app/services/category_inference.py
_THIS_DIR = Path(__file__).resolve().parent
_BACKEND_ROOT = _THIS_DIR.parent.parent.parent  # backend/
if str(_BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(_BACKEND_ROOT))
try:
    from app.services.category_inference import infer_category  # type: ignore
except Exception:  # pragma: no cover
    def infer_category(*, gamma_category=None, slug=None, question=None):
        return "Unknown"

EVENTS_PATH = Path("/opt/app/polyoracle/data/v2_paper/events.jsonl")
PRICE_HISTORY_URL = ("https://clob.polymarket.com/prices-history?"
                     "market={token_id}&interval=1d&fidelity=15")
# R1 : 1 condition_id par appel (Gamma ne supporte pas batch multi-condition_ids).
# IMPORTANT : `closed=true&archived=true` requis sinon Gamma retourne [] pour les markets
# résolus (5-min crypto Up/Down représentent la majorité du flow). Bug découvert 2026-05-27.
GAMMA_MARKET_URL = ("https://gamma-api.polymarket.com/markets?condition_ids={cid}"
                    "&closed=true&archived=true&limit=10")
LOOKAHEAD_MIN = 15  # minutes
SPREAD_BUFFER = 0.005  # 0.5% pour neutraliser microstructure noise
GATE_MIN_N = 100
GATE_ACCURACY_PCT = 55.0

# Trigger thresholds (doivent matcher obi_signal.py + bayesian_price_model.py)
OBI_BULL = 0.65
OBI_BEAR = -0.65
BAYES_TRIGGER = 0.02

# R1 : Price buckets — 5 buckets pour distinguer extremes (favorite-longshot bias)
PRICE_BUCKETS = [
    ("p_0_30", 0.0, 0.30),
    ("p_30_50", 0.30, 0.50),
    ("p_50_70", 0.50, 0.70),
    ("p_70_90", 0.70, 0.90),
    ("p_90_100", 0.90, 1.01),  # 1.01 pour inclure 1.0
]

# R1 : Catégories canoniques (lowercase) — wrappers HL category_inference legacy
CATEGORY_LABELS = [
    "crypto", "sports", "politics", "geopolitics", "economics",
    "finance", "tech", "culture", "weather", "mentions",
    "other", "unknown",
]

# Cache disk pour catégories (évite re-fetch entre runs)
CATEGORY_CACHE_PATH = Path("/opt/app/polyoracle/data/v2_paper/category_cache.json")
PRICE_HISTORY_CACHE_PATH = Path("/opt/app/polyoracle/data/v2_paper/price_history_cache.json")


def fetch_price_history(token_id: str, timeout_s: float = 5.0) -> list[dict]:
    """Fetch prices-history pour un token. Retourne list[{t, p}] ou []."""
    url = PRICE_HISTORY_URL.format(token_id=token_id)
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "PolyV2-Validator/1.0",
            "Accept": "application/json",
        })
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            data = json.loads(resp.read())
        return data.get("history") or []
    except Exception as e:
        print(f"[FETCH-ERR] token={token_id[:14]}: {type(e).__name__}: {str(e)[:60]}",
              flush=True)
        return []


def fetch_gamma_market(condition_id: str, timeout_s: float = 5.0) -> Optional[dict]:
    """Fetch un seul market via condition_id. Gamma ne supporte pas batch.

    Returns {slug, question, gamma_category, event_title} ou None.
    """
    url = GAMMA_MARKET_URL.format(cid=condition_id)
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "PolyV2-Validator/1.0",
            "Accept": "application/json",
        })
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            data = json.loads(resp.read())
        if not isinstance(data, list) or not data:
            return None
        m = data[0]
        ev_list = m.get("events") or []
        event_title = ev_list[0].get("title") if ev_list else None
        return {
            "slug": m.get("slug"),
            "question": m.get("question"),
            "gamma_category": m.get("category"),  # peut être None
            "event_title": event_title,
        }
    except Exception as e:
        print(f"[GAMMA-ERR] cid={condition_id[:14]}: {type(e).__name__}: {str(e)[:60]}",
              flush=True)
        return None


def categorize(condition_id: str, cache: dict, fetched_counter: list[int]) -> str:
    """Retourne la category canonique pour un condition_id.

    Cache disk-backed pour persister entre runs.
    """
    if condition_id in cache:
        return cache[condition_id]
    meta = fetch_gamma_market(condition_id)
    fetched_counter[0] += 1
    if meta is None:
        cat = "unknown"
    else:
        # Use slug + question + event_title for inference
        haystack_q = " ".join(filter(None, [meta.get("question"), meta.get("event_title")]))
        cat_raw = infer_category(
            gamma_category=meta.get("gamma_category"),
            slug=meta.get("slug"),
            question=haystack_q,
        )
        cat = cat_raw.lower() if cat_raw else "unknown"
    cache[condition_id] = cat
    return cat


def bucket_of(price: float) -> str:
    """Retourne le bucket de price pour un prix dans [0,1]."""
    for name, lo, hi in PRICE_BUCKETS:
        if lo <= price < hi:
            return name
    return "p_oob"


def find_price_at_ts(history: list[dict], ts_target_sec: int) -> Optional[float]:
    """Cherche le price le plus proche de ts_target_sec dans history.

    history : list[{t: sec, p: 0-1}] (déjà trié ASC par convention API).
    Si ts_target_sec hors range, retourne None.
    Tolerance : ±20 min (= 2 buckets fidelity=15 = 30min).
    """
    if not history:
        return None
    # Limites
    min_t = int(history[0]["t"])
    max_t = int(history[-1]["t"])
    if ts_target_sec < min_t - 120 or ts_target_sec > max_t + 1200:
        return None
    # Linear scan (history bornée à ~96 entries par 1d/fidelity=15)
    best_idx = 0
    best_diff = abs(int(history[0]["t"]) - ts_target_sec)
    for i, h in enumerate(history):
        t = int(h["t"])
        d = abs(t - ts_target_sec)
        if d < best_diff:
            best_diff = d
            best_idx = i
    if best_diff > 1200:  # >20min de gap = data trop loin
        return None
    return float(history[best_idx]["p"])


def classify_actual(price_t: float, price_future: float) -> Optional[str]:
    """UP / DOWN / FLAT selon delta. FLAT exclu de l'accuracy."""
    if price_t <= 0 or price_future <= 0:
        return None
    delta = price_future - price_t
    if delta > SPREAD_BUFFER:
        return "UP"
    if delta < -SPREAD_BUFFER:
        return "DOWN"
    return None  # flat → exclude


def classify_obi(obi_value: float) -> Optional[str]:
    """OBI prédiction direction : UP si bullish, DOWN si bearish, None si neutral."""
    if obi_value > OBI_BULL:
        return "UP"
    if obi_value < OBI_BEAR:
        return "DOWN"
    return None


def classify_bayes(mispricing: float) -> Optional[str]:
    """Bayes prédiction : UP si market undervalues YES (model > market), DOWN si overvalues."""
    if mispricing > BAYES_TRIGGER:
        return "UP"
    if mispricing < -BAYES_TRIGGER:
        return "DOWN"
    return None


def wilson_ci(correct: int, total: int, z: float = 1.96) -> tuple[float, float, float]:
    """Wilson 95% binomial CI. Returns (acc_pct, lo_pct, hi_pct)."""
    from math import sqrt
    if total == 0:
        return 0.0, 0.0, 0.0
    p = correct / total
    denom = 1 + z*z/total
    center = (p + z*z/(2*total)) / denom
    margin = z * sqrt((p*(1-p) + z*z/(4*total))/total) / denom
    lo = max(0, (center - margin) * 100)
    hi = min(100, (center + margin) * 100)
    return p * 100, lo, hi


def verdict_for(correct: int, total: int) -> str:
    """Verdict gate Phase B avec Wilson CI95."""
    if total < GATE_MIN_N:
        return "low_N"
    acc, lo, hi = wilson_ci(correct, total)
    if lo >= GATE_ACCURACY_PCT:
        return "PASS_strict"
    if acc >= GATE_ACCURACY_PCT and lo > 50:
        return "PASS"
    if hi < 50:
        return "FAIL_neg"
    return "indeterminate"


def empty_bucket():
    return {"correct": 0, "total": 0, "ups": 0, "downs": 0}


def load_cache(path: Path) -> dict:
    if path.exists():
        try:
            with open(path) as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def save_cache(path: Path, cache: dict):
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w") as f:
            json.dump(cache, f)
    except Exception as e:
        print(f"[CACHE-WARN] cannot save {path}: {e}")


def main():
    # 1. Load events.jsonl
    if not EVENTS_PATH.exists():
        print(f"ERROR: events.jsonl not found at {EVENTS_PATH}")
        sys.exit(1)

    events_pass = []
    n_total = 0
    with open(EVENTS_PATH) as f:
        for line in f:
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            n_total += 1
            if ev.get("allow") and ev.get("token_id") and ev.get("ts"):
                events_pass.append(ev)
    print(f"[LOAD] events.jsonl total={n_total} PASS_with_token={len(events_pass)}")

    # R1 : load disk caches (persistance entre runs)
    category_cache = load_cache(CATEGORY_CACHE_PATH)
    price_history_cache = load_cache(PRICE_HISTORY_CACHE_PATH)
    print(f"[CACHE] category preloaded={len(category_cache)} prices preloaded={len(price_history_cache)}")

    # 2. Group by token_id pour minimiser fetches
    token_events: dict[str, list[dict]] = defaultdict(list)
    for ev in events_pass:
        token_events[ev["token_id"]].append(ev)

    n_tokens = len(token_events)
    print(f"[LOAD] unique tokens to fetch: {n_tokens}")

    # 3. Fetch prices-history par token (rate-limit 8 req/s)
    print(f"[FETCH] downloading price histories...")
    for i, (tid, evs) in enumerate(token_events.items()):
        if tid in price_history_cache:
            continue
        if i > 0 and i % 50 == 0:
            print(f"[FETCH] {i}/{n_tokens} tokens (price history)...")
        history = fetch_price_history(tid)
        price_history_cache[tid] = history
        time.sleep(0.12)  # ~8 req/s
    save_cache(PRICE_HISTORY_CACHE_PATH, price_history_cache)
    n_with_history = sum(1 for tid in token_events if price_history_cache.get(tid))
    print(f"[FETCH] done. {n_with_history}/{n_tokens} tokens have price history")

    # R1 : 3b. Fetch Gamma metadata par UNIQUE condition_id (= un par market)
    unique_cids = set(ev["condition_id"] for ev in events_pass)
    print(f"[GAMMA] unique condition_ids: {len(unique_cids)}")
    fetched_counter = [0]
    cids_to_fetch = [c for c in unique_cids if c not in category_cache]
    print(f"[GAMMA] need to fetch metadata for {len(cids_to_fetch)} new cids")
    for i, cid in enumerate(cids_to_fetch):
        if i > 0 and i % 100 == 0:
            print(f"[GAMMA] {i}/{len(cids_to_fetch)} fetched...")
            save_cache(CATEGORY_CACHE_PATH, category_cache)  # checkpoint
        categorize(cid, category_cache, fetched_counter)
        time.sleep(0.10)  # ~10 req/s sustain
    save_cache(CATEGORY_CACHE_PATH, category_cache)
    print(f"[GAMMA] done. fetched_this_run={fetched_counter[0]}")

    # 4. Pour chaque event, compute predicted vs actual + buckets
    # Bucket structures :
    #   buckets["global"] = dict signal_name → empty_bucket()
    #   buckets["category"][cat] = dict signal_name → empty_bucket()
    #   buckets["price_bucket"][pb] = dict signal_name → empty_bucket()
    #   buckets["cat_x_price"][(cat,pb)] = dict signal_name → empty_bucket()
    SIGNAL_NAMES = [
        "Baseline_all_PASS",  # wallet direction baseline
        "Wallet_BUY",
        "Wallet_SELL",
        "OBI_only_directional",
        "Bayes_only_directional",
        "Cross_aligned",
    ]

    def new_signal_dict():
        return {s: empty_bucket() for s in SIGNAL_NAMES}

    bucket_global = new_signal_dict()
    bucket_by_cat: dict[str, dict] = defaultdict(new_signal_dict)
    bucket_by_px: dict[str, dict] = defaultdict(new_signal_dict)
    bucket_by_cat_x_px: dict[tuple, dict] = defaultdict(new_signal_dict)

    n_evaluated = 0
    n_no_history = 0
    n_no_future = 0
    n_flat = 0

    for ev in events_pass:
        tid = ev["token_id"]
        history = price_history_cache.get(tid) or []
        if not history:
            n_no_history += 1
            continue

        ts_event_sec = int(ev["ts"]) // 1000
        ts_future_sec = ts_event_sec + LOOKAHEAD_MIN * 60
        price_future = find_price_at_ts(history, ts_future_sec)
        price_t = ev.get("price", 0)

        if price_future is None or price_t <= 0:
            n_no_future += 1
            continue

        actual = classify_actual(price_t, price_future)
        if actual is None:
            n_flat += 1
            continue

        n_evaluated += 1
        side = ev.get("side", "")
        cid = ev["condition_id"]
        cat = category_cache.get(cid, "unknown")
        pb = bucket_of(price_t)

        # Compute per-signal predicted direction
        wallet_pred = "UP" if side == "BUY" else ("DOWN" if side == "SELL" else None)
        obi = ev.get("obi") or {}
        obi_val = obi.get("value")
        obi_pred = classify_obi(obi_val) if obi_val is not None else None
        bay = ev.get("bayes") or {}
        bay_mp = bay.get("mispricing")
        bay_pred = classify_bayes(bay_mp) if bay_mp is not None else None
        cross_pred = obi_pred if (obi_pred and bay_pred and obi_pred == bay_pred) else None

        # Update all 4 bucket levels (global, cat, px, cat_x_px)
        def _update(b: dict, signal: str, pred: Optional[str]):
            if not pred:
                return
            entry = b[signal]
            entry["total"] += 1
            if pred == actual:
                entry["correct"] += 1
            if actual == "UP":
                entry["ups"] += 1
            else:
                entry["downs"] += 1

        for sig, pred in [
            ("Baseline_all_PASS", wallet_pred),
            ("OBI_only_directional", obi_pred),
            ("Bayes_only_directional", bay_pred),
            ("Cross_aligned", cross_pred),
        ]:
            _update(bucket_global, sig, pred)
            _update(bucket_by_cat[cat], sig, pred)
            _update(bucket_by_px[pb], sig, pred)
            _update(bucket_by_cat_x_px[(cat, pb)], sig, pred)

        # Wallet_BUY/SELL split (baseline diagnostic)
        side_sig = "Wallet_BUY" if side == "BUY" else ("Wallet_SELL" if side == "SELL" else None)
        if side_sig:
            _update(bucket_global, side_sig, wallet_pred)
            _update(bucket_by_cat[cat], side_sig, wallet_pred)
            _update(bucket_by_px[pb], side_sig, wallet_pred)
            _update(bucket_by_cat_x_px[(cat, pb)], side_sig, wallet_pred)

    # 5. Report
    print()
    print("═" * 76)
    print(f"FORWARD VALIDATOR R1 — Phase B Polymarket V2 (lookahead {LOOKAHEAD_MIN}min)")
    print("═" * 76)
    print(f"Total events.jsonl       : {n_total}")
    print(f"Events PASS w/ token_id  : {len(events_pass)}")
    print(f"Unique tokens fetched    : {n_tokens} ({n_with_history} avec history)")
    print(f"Unique condition_ids     : {len(unique_cids)}")
    print(f"Categories distribution  : {dict((c, sum(1 for k,v in category_cache.items() if v == c and k in unique_cids)) for c in CATEGORY_LABELS)}")
    print(f"Evaluated (UP/DOWN)      : {n_evaluated}")
    print(f"Excluded:")
    print(f"  no_history             : {n_no_history}")
    print(f"  no_future_price        : {n_no_future}")
    print(f"  flat (<{SPREAD_BUFFER*100:.1f}% move) : {n_flat}")

    # ----- 5a. Global -----
    print()
    print("█ GLOBAL")
    _print_signal_table(bucket_global)

    # ----- 5b. Par catégorie -----
    print()
    print("█ PAR CATÉGORIE")
    # Trier par N total dispo
    cat_order = sorted(
        bucket_by_cat.keys(),
        key=lambda c: -sum(bucket_by_cat[c][s]["total"] for s in SIGNAL_NAMES),
    )
    for cat in cat_order:
        total_in_cat = sum(bucket_by_cat[cat][s]["total"] for s in SIGNAL_NAMES if s in ("OBI_only_directional", "Bayes_only_directional"))
        baseline_n = bucket_by_cat[cat]["Baseline_all_PASS"]["total"]
        print(f"\n  Catégorie={cat:<14s} (baseline_n={baseline_n})")
        _print_signal_table(bucket_by_cat[cat], indent="    ")

    # ----- 5c. Par price_bucket -----
    print()
    print("█ PAR PRICE BUCKET")
    px_order = [name for name, _, _ in PRICE_BUCKETS] + ["p_oob"]
    for pb in px_order:
        if pb not in bucket_by_px:
            continue
        baseline_n = bucket_by_px[pb]["Baseline_all_PASS"]["total"]
        print(f"\n  Bucket={pb:<10s} (baseline_n={baseline_n})")
        _print_signal_table(bucket_by_px[pb], indent="    ")

    # ----- 5d. Par couple cat × price_bucket (top 25 par N) -----
    print()
    print("█ PAR COUPLE (CATÉGORIE × PRICE_BUCKET) — top 25 par N")
    couple_order = sorted(
        bucket_by_cat_x_px.keys(),
        key=lambda k: -bucket_by_cat_x_px[k]["Baseline_all_PASS"]["total"],
    )[:25]
    for (cat, pb) in couple_order:
        baseline_n = bucket_by_cat_x_px[(cat, pb)]["Baseline_all_PASS"]["total"]
        if baseline_n < 30:  # skip très petits buckets
            continue
        print(f"\n  {cat:<14s} × {pb:<10s} (baseline_n={baseline_n})")
        _print_signal_table(bucket_by_cat_x_px[(cat, pb)], indent="    ")

    # ----- 5e. Synthèse PASS findings (sous-pop ≥55% N≥100 ?) -----
    print()
    print("█ SYNTHÈSE R1 — sous-pops ≥55% accuracy avec N≥100")
    found_pass = []
    for level_name, level_buckets in [
        ("category", bucket_by_cat),
        ("price_bucket", bucket_by_px),
        ("cat_x_price", bucket_by_cat_x_px),
    ]:
        for key, signals in level_buckets.items():
            for sig_name, sig in signals.items():
                if sig_name in ("Wallet_BUY", "Wallet_SELL"):
                    continue  # diagnostic only
                if sig["total"] < GATE_MIN_N:
                    continue
                acc, lo, hi = wilson_ci(sig["correct"], sig["total"])
                if acc >= GATE_ACCURACY_PCT:
                    found_pass.append((level_name, key, sig_name, sig["total"], acc, lo, hi))
    if not found_pass:
        print("  AUCUNE sous-pop ne PASSE le gate (≥55% accuracy + N≥100).")
        print("  → R1 verdict: FAIL. Aucun bucket exploitable identifié.")
    else:
        print(f"  {len(found_pass)} sous-pop(s) trouvée(s):")
        for level, key, sig, n, acc, lo, hi in sorted(found_pass, key=lambda x: -x[4]):
            print(f"    {level}={key} | {sig:<24s} | n={n:>5d} | acc={acc:.1f}% [CI95 {lo:.1f},{hi:.1f}]")


def _print_signal_table(signals: dict, indent: str = ""):
    print(f"{indent}{'Signal':<28s} {'n':>5s} {'acc':>7s} {'CI95 lo':>8s} {'CI95 hi':>8s} {'ups/downs':>11s} {'verdict':>13s}")
    print(f"{indent}{'-'*76}")
    for name, b in signals.items():
        n = b["total"]
        if n == 0:
            print(f"{indent}{name:<28s} {0:>5d}")
            continue
        acc, lo, hi = wilson_ci(b["correct"], n)
        v = verdict_for(b["correct"], n)
        print(f"{indent}{name:<28s} {n:>5d} {acc:>6.1f}% {lo:>7.1f}% {hi:>7.1f}% {b['ups']}/{b['downs']:<6} {v:>13s}")


if __name__ == "__main__":
    main()
