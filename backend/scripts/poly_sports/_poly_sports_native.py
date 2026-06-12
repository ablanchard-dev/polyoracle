"""Polymarket Sports-NATIVES — discovery + validation.

Différent de _poly_sports_validation.py (qui partait de la cohorte crypto).
Ici : market-centric Sports → identifie les wallets qui tradent massivement
Sports → valide en holdout temporel.

Stages :
  1. Sports markets résolus last 90j depuis resolvedmarketrecord local.
  2. fetch_market_trades par marché → agg wallets participation.
  3. Top N Sports-natives par # distinct Sports markets tradés.
  4. fetch_wallet_trades (/activity) per native.
  5. Résout les marchés non-locaux via Gamma.
  6. Holdout temporel + Bonferroni + agg.
"""
from __future__ import annotations

import json
import math
import re
import sqlite3
import statistics
import sys
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, "/opt/app/polyoracle/backend")
from app.services.market_resolution_scanner import MarketResolutionScanner
from app.services.polymarket.data_client import DataClient
from app.services.polymarket.gamma_client import GammaClient

DB = "/opt/app/polyoracle/data/polyoracle.db"
OUT = Path("/opt/app/polyoracle/data/poly_sports_native")
OUT.mkdir(parents=True, exist_ok=True)
CACHE_MT = OUT / "market_trades.json"
CACHE_WA = OUT / "wallet_activity.json"
CACHE_RES = OUT / "resolutions.json"
VERDICT = OUT / "verdict.md"

NOW = datetime.now(timezone.utc)
WINDOW_DAYS = 90
HOLDOUT_DAYS = 30
WINDOW_START = NOW - timedelta(days=WINDOW_DAYS)
HOLDOUT_CUTOFF_MS = int((NOW - timedelta(days=HOLDOUT_DAYS)).timestamp() * 1000)

MAX_SPORTS_MARKETS = 1500
TOP_NATIVES = 500
MIN_SPORTS_PARTICIPATION = 5
MIN_FILLS_FOR_TEST = 20
MIN_HOLD_N = 10


def log(*a):
    print(*a, flush=True)


SPORTS_RE = re.compile(
    r"\b(sport|nfl|nba|mlb|nhl|epl|premier league|fifa|wcup|world cup|ucl|"
    r"champions|tennis|atp|wta|golf|pga|f1|formula|ufc|mma|"
    r"boxing|cricket|nascar|olympic|hockey|baseball|football|soccer|"
    r"basketball|cfb|college|bundesliga|la liga|serie a|ligue 1)\b", re.I)


def is_sports(slug, title):
    return bool(SPORTS_RE.search((slug or "") + " " + (title or "")))


def main():
    log(f"=== Poly Sports-NATIVE discovery + validation ===")
    log(f"Window {WINDOW_DAYS}d  Holdout {HOLDOUT_DAYS}d  cutoff_ms={HOLDOUT_CUTOFF_MS}")

    # Stage 1
    c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=30)
    rows = c.execute(
        "SELECT condition_id, question, end_date, winning_outcome_name "
        "FROM resolvedmarketrecord "
        "WHERE category='Sports' AND condition_id IS NOT NULL AND end_date IS NOT NULL "
        "ORDER BY end_date DESC"
    ).fetchall()
    c.close()
    sports_mkts = []
    for cid, q, ed, win in rows:
        try:
            edt = datetime.fromisoformat(ed.replace("Z", "+00:00"))
            if edt < WINDOW_START:
                continue
            sports_mkts.append((cid, q, ed, win.strip().lower() if win else None))
        except Exception:
            continue
    log(f"\n[1] Sports markets local last {WINDOW_DAYS}d: {len(sports_mkts)}")
    if len(sports_mkts) > MAX_SPORTS_MARKETS:
        sports_mkts = sports_mkts[:MAX_SPORTS_MARKETS]
        log(f"  cap appliqué -> {MAX_SPORTS_MARKETS}")

    # Stage 2
    dc = DataClient()
    if CACHE_MT.exists():
        mkt_trades = json.loads(CACHE_MT.read_text())
        log(f"\n[2] cache market_trades : {len(mkt_trades)} cids")
    else:
        mkt_trades = {}
        log(f"\n[2] fetch_market_trades pour {len(sports_mkts)} marchés...")
        t0 = time.time()
        for i, (cid, q, ed, win) in enumerate(sports_mkts):
            try:
                trades = dc.fetch_market_trades(cid, limit=500)
                mkt_trades[cid] = trades
            except Exception:
                mkt_trades[cid] = []
            if (i + 1) % 100 == 0:
                el = time.time() - t0
                eta = el / (i + 1) * (len(sports_mkts) - i - 1)
                log(f"  ...{i+1}/{len(sports_mkts)} elapsed={el:.0f}s ETA={eta:.0f}s")
                CACHE_MT.write_text(json.dumps(mkt_trades))
        CACHE_MT.write_text(json.dumps(mkt_trades))
    total_t = sum(len(v) for v in mkt_trades.values())
    log(f"  ~{total_t} trades collectés")

    # Stage 3 : aggregate per-wallet
    log("\n[3] agg participation Sports par wallet...")
    participation = Counter()
    volume = Counter()
    for cid, trades in mkt_trades.items():
        seen = set()
        for t in trades:
            wal = (t.get("proxyWallet") or t.get("proxy_wallet") or "").lower()
            if not wal:
                continue
            try:
                sz = float(t.get("size", 0) or 0)
            except Exception:
                sz = 0
            volume[wal] += sz
            seen.add(wal)
        for w in seen:
            participation[w] += 1
    log(f"  wallets uniques Sports : {len(participation)}")
    natives = [w for w, n in participation.items() if n >= MIN_SPORTS_PARTICIPATION]
    natives.sort(key=lambda w: -participation[w])
    natives = natives[:TOP_NATIVES]
    log(f"  Sports-natives top {len(natives)} (participation >= {MIN_SPORTS_PARTICIPATION})")

    # Stage 4 : fetch activity
    if CACHE_WA.exists():
        wallet_acts = json.loads(CACHE_WA.read_text())
        log(f"\n[4] cache activity : {len(wallet_acts)} wallets")
    else:
        wallet_acts = {}
        log(f"\n[4] fetch /activity pour {len(natives)} natives...")
        t0 = time.time()
        for i, wal in enumerate(natives):
            try:
                acts = dc.fetch_wallet_trades(wal)
                wallet_acts[wal] = acts if isinstance(acts, list) else []
            except Exception:
                wallet_acts[wal] = []
            if (i + 1) % 50 == 0:
                el = time.time() - t0
                eta = el / (i + 1) * (len(natives) - i - 1)
                log(f"  ...{i+1}/{len(natives)} elapsed={el:.0f}s ETA={eta:.0f}s")
                CACHE_WA.write_text(json.dumps(wallet_acts))
        CACHE_WA.write_text(json.dumps(wallet_acts))

    # Stage 5 : extract Sports + collect cids
    sports_by_wallet = {}
    cids_needed = set()
    for wal, acts in wallet_acts.items():
        sp = []
        for a in acts:
            if not isinstance(a, dict):
                continue
            if is_sports(a.get("slug", ""), a.get("title", "") or a.get("eventTitle", "")):
                sp.append(a)
                cid = a.get("conditionId") or a.get("condition_id") or a.get("market_id")
                if cid:
                    cids_needed.add(cid)
        if sp:
            sports_by_wallet[wal] = sp
    log(f"\n[5] natives avec Sports dans /activity : {len(sports_by_wallet)}")
    log(f"  marchés Sports distincts dans /activity : {len(cids_needed)}")

    # Stage 6 : resolutions
    res = json.loads(CACHE_RES.read_text()) if CACHE_RES.exists() else {}
    # populate from local DB
    c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=30)
    for cid in cids_needed:
        if cid in res:
            continue
        row = c.execute(
            "SELECT winning_outcome_name FROM resolvedmarketrecord WHERE condition_id=?",
            (cid,)).fetchone()
        if row and row[0]:
            res[cid] = row[0].strip().lower()
    c.close()
    to_resolve = [c for c in cids_needed if c not in res]
    log(f"  à résoudre via Gamma : {len(to_resolve)} (local : {len(cids_needed) - len(to_resolve)})")
    gc = GammaClient(timeout=20)
    sc = MarketResolutionScanner()
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
            log(f"  ...{i+1}/{len(to_resolve)} elapsed={time.time()-t0:.0f}s")
            CACHE_RES.write_text(json.dumps(res))
    CACHE_RES.write_text(json.dumps(res))
    resolved_n = sum(1 for v in res.values() if v)
    log(f"  résolus avec winner : {resolved_n}")

    # Stage 7 : per-wallet PnL + holdout
    metrics = []
    for wal, sp in sports_by_wallet.items():
        tr_p, ho_p = [], []
        for a in sp:
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
            ts_val = a.get("timestamp") or a.get("ts") or a.get("time")
            try:
                if not ts_val:
                    continue
                ti = int(ts_val)
                tms = ti * 1000 if ti < 10**12 else ti
            except Exception:
                continue
            if not (0.02 < price < 0.98):
                continue
            outcome = (a.get("outcome") or "").strip().lower()
            won = (outcome == winner)
            pnl = (1.0 / price - 1.0) if won else -1.0
            if tms < HOLDOUT_CUTOFF_MS:
                tr_p.append(pnl)
            else:
                ho_p.append(pnl)
        if len(tr_p) + len(ho_p) < MIN_FILLS_FOR_TEST:
            continue
        t_stat = None
        if len(ho_p) >= 5:
            m = statistics.mean(ho_p)
            s = statistics.stdev(ho_p) if len(ho_p) > 1 else 0
            if s > 0:
                t_stat = m / (s / math.sqrt(len(ho_p)))
        metrics.append(dict(
            addr=wal, n=len(tr_p) + len(ho_p),
            train_n=len(tr_p), train_pnl=sum(tr_p),
            hold_n=len(ho_p), hold_pnl=sum(ho_p),
            t_stat=t_stat,
        ))
    log(f"\n[7] metrics : {len(metrics)} natives avec n>=20 Sports BUY résolus")

    # Stage 8 : verdict
    copyables = [m for m in metrics
                 if m["train_pnl"] > 0 and m["hold_pnl"] > 0 and m["hold_n"] >= MIN_HOLD_N]
    N = len(metrics)
    if N > 0:
        alpha = 0.05 / N
        z_crit = statistics.NormalDist().inv_cdf(1 - alpha / 2)
    else:
        alpha, z_crit = 0, 0
    bonf = [m for m in copyables if m["t_stat"] is not None and m["t_stat"] > z_crit]

    eligible = [m for m in metrics if m["train_n"] >= 10 and m["hold_n"] >= 5]
    eligible.sort(key=lambda x: -x["train_pnl"])
    q = max(1, len(eligible) // 4)
    top_q = sum(m["hold_pnl"] for m in eligible[:q])
    bot_q = sum(m["hold_pnl"] for m in eligible[-q:])
    spread = top_q - bot_q

    log(f"\n--- VERDICT ---")
    log(f"copyables : {len(copyables)}")
    log(f"Bonferroni (z>{z_crit:.2f}) : {len(bonf)}")
    log(f"spread top-bot quartile : ${spread:+.1f}")

    bonf.sort(key=lambda x: -x["hold_pnl"])
    copyables.sort(key=lambda x: -x["hold_pnl"])

    lines = ["# Poly Sports-NATIVES — Verdict\n",
             f"_Run : {NOW.isoformat()}_\n",
             "## Méthodo",
             f"- Stage 1 : Sports markets last {WINDOW_DAYS}d (resolvedmarketrecord local)",
             f"- Stage 2 : fetch_market_trades par marché (cap {MAX_SPORTS_MARKETS})",
             f"- Stage 3 : top {TOP_NATIVES} natives (participation >= {MIN_SPORTS_PARTICIPATION})",
             f"- Stage 4-7 : /activity → filter Sports → resolve → holdout {HOLDOUT_DAYS}d\n",
             "## Volume",
             f"- Sports markets processés : {len(sports_mkts)}",
             f"- Wallets uniques Sports : {len(participation)}",
             f"- Natives top : {len(natives)}",
             f"- Avec Sports trades dans /activity : {len(sports_by_wallet)}",
             f"- Avec n>=20 BUY résolus : **{N}**\n",
             "## Tests",
             f"- Copyables (train+/holdout+/hold_n>=10) : **{len(copyables)}**",
             f"- Bonferroni (z>{z_crit:.2f}, alpha={alpha:.5f}) : **{len(bonf)}**",
             f"- Spread top-bot quartile train → holdout : **${spread:+,.1f}**\n",
             "## VERDICT"]
    if len(bonf) >= 5 and spread > 0:
        lines.append(f"### **OUI — edge Sports natives détecté.**")
        lines.append(f"{len(bonf)} traders survivent Bonferroni + aggregate positif. Cohorte Sports candidate.\n")
    elif len(copyables) >= 10 and spread > 0:
        lines.append(f"### **TIÈDE — {len(copyables)} candidats, 0 Bonferroni strict.** Aggregate positif. À discuter.\n")
    elif N >= 30:
        lines.append(f"### **NON — pas d'edge Sports natives détecté.**")
        lines.append(f"N={N}, copyables={len(copyables)}, Bonferroni=0, spread=${spread:+,.1f}.\n")
    else:
        lines.append(f"### **ÉCHANTILLON TROP MINCE — N={N}.** Verdict impossible.\n")

    if bonf:
        lines.append("## Survivants Bonferroni")
        lines.append("| wallet | n | train | holdout | hold_n | t_stat |")
        lines.append("|---|---|---|---|---|---|")
        for m in bonf[:15]:
            lines.append(f"| {m['addr'][:14]} | {m['n']} | ${m['train_pnl']:+.1f} | ${m['hold_pnl']:+.1f} | {m['hold_n']} | {m['t_stat']:.2f} |")

    if copyables:
        lines.append("\n## Candidats copiables (avant Bonferroni)")
        lines.append("| wallet | n | train | holdout | hold_n | t_stat |")
        lines.append("|---|---|---|---|---|---|")
        for m in copyables[:25]:
            ts = f"{m['t_stat']:.2f}" if m["t_stat"] is not None else "—"
            lines.append(f"| {m['addr'][:14]} | {m['n']} | ${m['train_pnl']:+.1f} | ${m['hold_pnl']:+.1f} | {m['hold_n']} | {ts} |")

    VERDICT.write_text("\n".join(lines))
    log(f"\n=> {VERDICT}")


if __name__ == "__main__":
    main()
