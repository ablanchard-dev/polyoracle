"""Poly Sports / 60min-24h — validation par cohorte wallet-centric.

Pour chaque wallet de la cohorte (MFWR ELITE+STRONG, ~4400 wallets), fetch
son /activity Polymarket. Filtre les trades Sports (regex slug/title).
Résout chaque marché Sports via Gamma. Calcule per-wallet PnL Sports avec
train/holdout temporel (train -90j à -30j / holdout -30j à 0). Bonferroni
sur N. Test agrégé top vs bottom quartile.

Tourne sur le VPS (API Polymarket fiable depuis là).
"""
from __future__ import annotations

import json
import math
import re
import sqlite3
import statistics
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, "/opt/app/polyoracle/backend")
from app.services.market_resolution_scanner import MarketResolutionScanner
from app.services.polymarket.data_client import DataClient
from app.services.polymarket.gamma_client import GammaClient

DB = "/opt/app/polyoracle/data/polyoracle.db"
OUT_DIR = Path("/opt/app/polyoracle/data/poly_sports_validation")
OUT_DIR.mkdir(parents=True, exist_ok=True)
ACT_CACHE = OUT_DIR / "activity.json"
RES_CACHE = OUT_DIR / "resolutions.json"
VERDICT_MD = OUT_DIR / "verdict.md"

WINDOW_DAYS = 90
HOLDOUT_DAYS = 30
NOW = datetime.now(timezone.utc)
WINDOW_START_MS = int((NOW - timedelta(days=WINDOW_DAYS)).timestamp() * 1000)
HOLDOUT_CUTOFF_MS = int((NOW - timedelta(days=HOLDOUT_DAYS)).timestamp() * 1000)
HOLDOUT_CUTOFF_S = HOLDOUT_CUTOFF_MS // 1000

SPORTS_RE = re.compile(
    r"\b(sport|nfl|nba|mlb|nhl|epl|premier league|fifa|wcup|world cup|ucl|"
    r"champions|tennis|atp|wta|golf|pga|f1|formula 1|formula1|ufc|mma|"
    r"boxing|cricket|nascar|olympic|hockey|baseball|football|soccer|"
    r"basketball|cfb|college|bundesliga|la liga|serie a|ligue 1|nrl|afl|"
    r"chiefs|cowboys|celtics|lakers|warriors|yankees|dodgers|barcelona|"
    r"madrid|liverpool|arsenal|manchester|psg|bayern|juventus)\b", re.I)


def log(*a):
    print(*a, flush=True)


def is_sports(slug, title):
    s = (slug or "") + " " + (title or "")
    return bool(SPORTS_RE.search(s))


def ts_ms(a):
    """Récupère un timestamp ms à partir d'une activity entry (formats variables)."""
    for k in ("timestamp", "ts", "time"):
        v = a.get(k)
        if v is None:
            continue
        try:
            n = int(v)
            return n * 1000 if n < 10**12 else n
        except Exception:
            pass
    return 0


def main():
    log("=== Poly Sports validation (wallet-centric, holdout OOS) ===")
    log(f"window={WINDOW_DAYS}d  holdout={HOLDOUT_DAYS}d  cutoff_ms={HOLDOUT_CUTOFF_MS}")

    # Step 1 — cohorte
    c = sqlite3.connect("file:%s?mode=ro" % DB, uri=True, timeout=30)
    cohort = [r[0].lower() for r in c.execute(
        "SELECT address FROM marketfirstwalletrecord "
        "WHERE candidate_status IN ('ELITE','STRONG') AND address IS NOT NULL"
    ) if r[0]]
    c.close()
    log(f"\n[1] cohorte ELITE+STRONG : {len(cohort)} wallets")

    # Step 2 — fetch /activity per wallet
    dc = DataClient()
    if ACT_CACHE.exists():
        wallet_acts = json.loads(ACT_CACHE.read_text())
        log(f"\n[2] cache activity : {len(wallet_acts)} wallets chargés")
    else:
        wallet_acts = {}
        log(f"\n[2] fetch /activity pour {len(cohort)} wallets...")
        t0 = time.time()
        errs = 0
        for i, addr in enumerate(cohort):
            try:
                acts = dc.fetch_wallet_trades(addr)
                wallet_acts[addr] = acts if isinstance(acts, list) else []
            except Exception:
                wallet_acts[addr] = []
                errs += 1
            if (i + 1) % 200 == 0:
                el = time.time() - t0
                eta = el / (i + 1) * (len(cohort) - i - 1)
                log(f"  ...{i+1}/{len(cohort)}  elapsed={el:.0f}s  ETA={eta:.0f}s  errs={errs}")
                ACT_CACHE.write_text(json.dumps(wallet_acts))
        ACT_CACHE.write_text(json.dumps(wallet_acts))
        log(f"  cached. total trades: ~{sum(len(v) for v in wallet_acts.values())}  errs={errs}")

    # Step 3 — extraire les trades Sports + collecter market_ids
    log("\n[3] filtre Sports...")
    sports_by_wallet = {}
    sport_cids = set()
    for addr, acts in wallet_acts.items():
        sp = []
        for a in acts:
            if not isinstance(a, dict):
                continue
            slug = a.get("slug", "")
            title = a.get("title", "") or a.get("eventTitle", "")
            if not is_sports(slug, title):
                continue
            sp.append(a)
            cid = a.get("conditionId") or a.get("condition_id") or a.get("market_id")
            if cid:
                sport_cids.add(cid)
        if sp:
            sports_by_wallet[addr] = sp
    log(f"  wallets avec Sports trades : {len(sports_by_wallet)}")
    log(f"  marchés Sports distincts : {len(sport_cids)}")

    if not sports_by_wallet:
        log("\nAUCUN trade Sports dans la cohorte. → projet Sports avec cette "
            "cohorte non viable. (Cohorte sélectionnée sur crypto.)")
        VERDICT_MD.write_text(
            "# Poly Sports — VERDICT\n\n"
            "**0 trade Sports** dans la cohorte ELITE+STRONG (4396 wallets).\n\n"
            "Cohorte sélectionnée sur crypto-5/15min → ne touche pas Sports. "
            "Pour évaluer Sports il faudrait une découverte indépendante "
            "(non-cohorte) — hors scope de ce run.\n")
        return

    # Step 4 — résoudre les marchés
    log(f"\n[4] résolution des {len(sport_cids)} marchés Sports...")
    if RES_CACHE.exists():
        res = json.loads(RES_CACHE.read_text())
        log(f"  cache res : {len(res)} chargés")
    else:
        res = {}
    gc = GammaClient(timeout=20)
    sc = MarketResolutionScanner()
    to_resolve = [c for c in sport_cids if c not in res]
    log(f"  à résoudre : {len(to_resolve)}")
    t0 = time.time()
    for i, cid in enumerate(to_resolve):
        try:
            raw = gc.fetch_market_by_condition(cid)
            if raw:
                _, name = sc.extract_winning_outcome(raw)
                res[cid] = name.strip().lower() if name else None
            else:
                res[cid] = None
        except Exception:
            res[cid] = None
        if (i + 1) % 100 == 0:
            el = time.time() - t0
            log(f"  ...{i+1}/{len(to_resolve)} elapsed={el:.0f}s")
            RES_CACHE.write_text(json.dumps(res))
    RES_CACHE.write_text(json.dumps(res))
    resolved_n = sum(1 for v in res.values() if v)
    log(f"  résolus avec winner : {resolved_n}/{len(sport_cids)}")

    # Step 5 — per-wallet EV avec holdout
    log("\n[5] métriques par wallet (Sports BUY)...")
    metrics = []
    for addr, sp_trades in sports_by_wallet.items():
        tr_p, ho_p = [], []
        for a in sp_trades:
            cid = a.get("conditionId") or a.get("condition_id") or a.get("market_id")
            winner = res.get(cid)
            if not winner:
                continue
            side = (a.get("side") or "").upper()
            if side != "BUY":
                continue
            try:
                price = float(a.get("price", 0))
            except Exception:
                continue
            if not (0.02 < price < 0.98):
                continue
            outcome = (a.get("outcome") or "").strip().lower()
            won = (outcome == winner)
            pnl = (1.0 / price - 1.0) if won else -1.0
            tms = ts_ms(a)
            if tms < HOLDOUT_CUTOFF_MS:
                tr_p.append(pnl)
            else:
                ho_p.append(pnl)
        n_total = len(tr_p) + len(ho_p)
        if n_total < 20:
            continue
        t_stat = None
        if len(ho_p) >= 5:
            m = statistics.mean(ho_p)
            s = statistics.stdev(ho_p) if len(ho_p) > 1 else 0
            if s > 0:
                t_stat = m / (s / math.sqrt(len(ho_p)))
        metrics.append(dict(
            addr=addr, n=n_total,
            train_n=len(tr_p), train_pnl=sum(tr_p),
            hold_n=len(ho_p), hold_pnl=sum(ho_p),
            t_stat=t_stat,
        ))
    log(f"  metrics : {len(metrics)} wallets avec n>=20 Sports BUY trades résolus")

    # Step 6 — filtre copiable + Bonferroni + aggregate
    copyables = [m for m in metrics
                 if m["train_pnl"] > 0 and m["hold_pnl"] > 0 and m["hold_n"] >= 10]
    N = len(metrics)
    if N > 0:
        alpha = 0.05 / N
        z_crit = statistics.NormalDist().inv_cdf(1 - alpha / 2)
    else:
        alpha, z_crit = 0, 0
    bonf = [m for m in copyables if m["t_stat"] is not None and m["t_stat"] > z_crit]
    log(f"  copyables (train+, holdout+, hold_n>=10) : {len(copyables)}")
    log(f"  Bonferroni (alpha={alpha:.4f}, z>{z_crit:.2f}) : {len(bonf)}")

    eligible = [m for m in metrics if m["train_n"] >= 10 and m["hold_n"] >= 5]
    eligible.sort(key=lambda x: -x["train_pnl"])
    q = max(1, len(eligible) // 4)
    top_q_hold = sum(m["hold_pnl"] for m in eligible[:q])
    bot_q_hold = sum(m["hold_pnl"] for m in eligible[-q:])
    spread = top_q_hold - bot_q_hold
    log(f"  aggregate: top quartile hold=${top_q_hold:+.1f}  bot=${bot_q_hold:+.1f}  spread=${spread:+.1f}")

    # Step 7 — verdict
    bonf.sort(key=lambda x: -x["hold_pnl"])
    copyables.sort(key=lambda x: -x["hold_pnl"])

    lines = []
    lines.append("# Poly Sports — VERDICT\n")
    lines.append(f"_Run : {NOW.isoformat()}_\n")
    lines.append("## Méthodo")
    lines.append(f"- Cohorte : MFWR ELITE+STRONG = {len(cohort)} wallets")
    lines.append(f"- Fenêtre : {WINDOW_DAYS}j  /  Holdout OOS : {HOLDOUT_DAYS}j")
    lines.append(f"- Source : /activity Polymarket par wallet, filtre Sports par regex slug/title")
    lines.append(f"- PnL : per $1 mis BUY, won={1}/price-1, lost=-1\n")
    lines.append("## Volume")
    lines.append(f"- Wallets avec trades Sports : **{len(sports_by_wallet)}** / {len(cohort)}")
    lines.append(f"- Marchés Sports distincts : {len(sport_cids)}  (résolus : {resolved_n})")
    lines.append(f"- Wallets avec n>=20 trades Sports résolus : **{N}**\n")

    lines.append("## Test")
    lines.append(f"- Candidats copiables (train+, holdout+, hold_n>=10) : **{len(copyables)}**")
    lines.append(f"- Survivants Bonferroni (z>{z_crit:.2f}, alpha={alpha:.4f}) : **{len(bonf)}**")
    lines.append(f"- Agrégé top vs bot quartile train → holdout : ${top_q_hold:+,.1f} vs ${bot_q_hold:+,.1f}  spread=${spread:+,.1f}\n")

    lines.append("## VERDICT")
    if len(bonf) >= 5 and spread > 0:
        lines.append("### **OUI — edge Sports copiable détecté.**\n")
    elif len(copyables) >= 10 and spread > 0:
        lines.append(f"### **TIÈDE — {len(copyables)} candidats, mais 0 passe Bonferroni strict. Spread agrégé positif.**\n")
    elif N < 30:
        lines.append(f"### **ÉCHANTILLON TROP MINCE — N={N} wallets seulement. Pas de conclusion possible.**")
        lines.append("La cohorte (sélectionnée sur crypto) trade très peu Sports. Une discovery Sports-native est nécessaire.\n")
    else:
        lines.append("### **NON — pas d'edge Sports copiable détecté dans cette cohorte.**\n")

    if bonf:
        lines.append("## Top survivants Bonferroni")
        lines.append("| wallet | n_total | train_pnl | holdout_pnl | hold_n | t_stat |")
        lines.append("|---|---|---|---|---|---|")
        for m in bonf[:15]:
            lines.append(f"| {m['addr'][:14]} | {m['n']} | ${m['train_pnl']:+.1f} | ${m['hold_pnl']:+.1f} | {m['hold_n']} | {m['t_stat']:.2f} |")

    if copyables:
        lines.append("\n## Top candidats copiables (avant Bonferroni)")
        lines.append("| wallet | n | train_pnl | holdout_pnl | hold_n | t_stat |")
        lines.append("|---|---|---|---|---|---|")
        for m in copyables[:20]:
            ts = f"{m['t_stat']:.2f}" if m["t_stat"] is not None else "—"
            lines.append(f"| {m['addr'][:14]} | {m['n']} | ${m['train_pnl']:+.1f} | ${m['hold_pnl']:+.1f} | {m['hold_n']} | {ts} |")

    txt = "\n".join(lines)
    VERDICT_MD.write_text(txt)
    log(f"\n=> verdict -> {VERDICT_MD}\n")
    log(txt)


if __name__ == "__main__":
    main()
