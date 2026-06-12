"""Polymarket ALL CATEGORIES (≤24h) — NATIVES discovery + validation.

Unifié : Sports, Crypto-long, Politics court, Météo, Events, autres.
Hold ≤ 24h max (constraint opérateur). Stages identiques aux scripts natives :
market-centric discovery → /activity → résolution → holdout temporel.
"""
from __future__ import annotations

import json
import math
import re
import sqlite3
import statistics
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, "/opt/app/polyoracle/backend")
from app.services.market_resolution_scanner import MarketResolutionScanner
from app.services.polymarket.data_client import DataClient
from app.services.polymarket.gamma_client import GammaClient

DB = "/opt/app/polyoracle/data/polyoracle.db"
OUT = Path("/opt/app/polyoracle/data/poly_all_native_24h")
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

MAX_MARKETS = 3000  # stratifié par catégorie
TOP_NATIVES = 800
MIN_PARTICIPATION = 5
MIN_FILLS_FOR_TEST = 20
MIN_HOLD_N = 10
HOLD_MAX_MIN = 24 * 60  # 24h max — opérateur

# Exclusions : marchés clairement >24h
LONG_DUR = re.compile(
    r"\bby (year|end|december|\d{4})\b|\bin 20\d{2}\b|\b20\d{2} (election|champion|winner|finals|playoff)\b|"
    r"\bseason\b|\bnext (week|month|year|quarter)\b|\bin \d+ (week|month|year)|"
    r"\bever\b|\bbefore 20\d{2}\b", re.I)

# Catégorisation grossière par question/slug
CAT_RE = {
    "crypto-5_15min": re.compile(r"\b(btc|eth|sol|xrp|bnb|doge|hype)\b.*\b(5\s*m|15\s*m|5min|15min)\b|\b(5\s*m|15\s*m|5min|15min)\b.*\b(btc|eth|sol|xrp|bnb|doge|hype)\b", re.I),
    "crypto-1h_4h": re.compile(r"\b(btc|bitcoin|eth|ethereum|sol|solana|xrp|bnb|doge|hype)\b.*(hourly|\b1\s*h\b|\b4\s*h\b)", re.I),
    "crypto-strike": re.compile(r"\b(bitcoin|ethereum|btc|eth|sol)\b.*\babove\s+\$", re.I),
    "sport": re.compile(r"\b(sport|nfl|nba|mlb|nhl|epl|premier league|fifa|wcup|world cup|ucl|champions|tennis|atp|wta|golf|pga|f1|formula|ufc|mma|boxing|cricket|nascar|olympic|hockey|baseball|football|soccer|basketball|cfb|college|bundesliga|la liga|serie a|ligue 1)\b", re.I),
    "politics": re.compile(r"\b(election|president|senate|congress|vote|primary|trump|biden|harris|election day)\b", re.I),
    "weather": re.compile(r"\b(rain|snow|temperature|weather|hurricane|storm|tornado|degree|forecast)\b", re.I),
    "tech-ai": re.compile(r"\b(gpt|openai|chatgpt|claude|gemini|llm|ai model)\b", re.I),
    "other": re.compile(r"."),  # catch-all
}


def categorize(question: str) -> str:
    for cat, rx in CAT_RE.items():
        if cat == "other":
            continue
        if rx.search(question or ""):
            return cat
    return "other"


def looks_long(question: str) -> bool:
    return bool(LONG_DUR.search(question or ""))


def log(*a):
    print(*a, flush=True)


def main():
    log("=== Poly ALL CATEGORIES (<=24h) NATIVES ===")
    log(f"Window {WINDOW_DAYS}d  Holdout {HOLDOUT_DAYS}d  hold_max {HOLD_MAX_MIN}min (24h)")

    # Stage 1 — pull tous les marchés résolus + classifier
    c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=30)
    rows = c.execute(
        "SELECT condition_id, question, end_date, winning_outcome_name, category "
        "FROM resolvedmarketrecord "
        "WHERE condition_id IS NOT NULL AND end_date IS NOT NULL AND question IS NOT NULL "
        "ORDER BY end_date DESC"
    ).fetchall()
    c.close()

    by_cat = defaultdict(list)
    excluded_long = 0
    for cid, q, ed, win, cat_db in rows:
        try:
            edt = datetime.fromisoformat(ed.replace("Z", "+00:00"))
            if edt < WINDOW_START:
                continue
        except Exception:
            continue
        if looks_long(q):
            excluded_long += 1
            continue
        cat = categorize(q)
        # exclure les 5/15min crypto (déjà testés négatifs)
        if cat == "crypto-5_15min":
            continue
        # use category DB hint if matches "Sports"/"Crypto"
        if (cat_db or "").lower() == "sports":
            cat = "sport"
        winner_lc = win.strip().lower() if win else None
        by_cat[cat].append((cid, q, ed, winner_lc))
    log(f"\n[1] Markets candidats (90d, hors clairement >24h, hors crypto-5/15min) :")
    log(f"  exclusions 'clairement long' : {excluded_long}")
    for k in sorted(by_cat, key=lambda x: -len(by_cat[x])):
        log(f"  {k:<20} {len(by_cat[k])}")

    # Stratifier : prendre ~max per_cat = MAX_MARKETS/n_cats
    n_cats = len(by_cat)
    per_cat_target = max(50, MAX_MARKETS // max(1, n_cats))
    selected = []
    for cat, mlist in by_cat.items():
        # déjà triés par end_date desc (du global query)
        take = mlist[:per_cat_target]
        selected.extend((cat, *m) for m in take)
    if len(selected) > MAX_MARKETS:
        # garder le mix mais cap
        selected = selected[:MAX_MARKETS]
    log(f"  selected stratifié (cap {MAX_MARKETS}, ~{per_cat_target}/cat) : {len(selected)}")

    # Stage 2 — fetch_market_trades
    dc = DataClient()
    if CACHE_MT.exists():
        mkt_trades = json.loads(CACHE_MT.read_text())
        log(f"\n[2] cache : {len(mkt_trades)}")
    else:
        mkt_trades = {}
        mkt_cat = {}
        log(f"\n[2] fetch_market_trades pour {len(selected)} marchés stratifiés...")
        t0 = time.time()
        for i, (cat, cid, q, ed, win) in enumerate(selected):
            try:
                mkt_trades[cid] = dc.fetch_market_trades(cid, limit=500)
            except Exception:
                mkt_trades[cid] = []
            mkt_cat[cid] = cat
            if (i + 1) % 100 == 0:
                el = time.time() - t0
                eta = el / (i + 1) * (len(selected) - i - 1)
                log(f"  ...{i+1}/{len(selected)} elapsed={el:.0f}s ETA={eta:.0f}s")
                CACHE_MT.write_text(json.dumps(mkt_trades))
        CACHE_MT.write_text(json.dumps(mkt_trades))
        (OUT / "market_cat.json").write_text(json.dumps(mkt_cat))
    log(f"  ~{sum(len(v) for v in mkt_trades.values())} trades collectés")
    mkt_cat = json.loads((OUT / "market_cat.json").read_text()) if (OUT / "market_cat.json").exists() else {}

    # Stage 3 — top natives par participation
    participation = Counter()
    wallet_cats = defaultdict(Counter)
    for cid, trades in mkt_trades.items():
        seen = set()
        for t in trades:
            wal = (t.get("proxyWallet") or t.get("proxy_wallet") or "").lower()
            if wal:
                seen.add(wal)
        for w in seen:
            participation[w] += 1
            wallet_cats[w][mkt_cat.get(cid, "other")] += 1
    log(f"\n[3] wallets uniques : {len(participation)}")
    natives = [w for w, n in participation.items() if n >= MIN_PARTICIPATION]
    natives.sort(key=lambda w: -participation[w])
    natives = natives[:TOP_NATIVES]
    log(f"  natives top {len(natives)}")

    # Stage 4 — /trades?user= paginé per native (PATCH: /activity inclut MERGE/CONV = pollution)
    import httpx
    WINDOW_START_S = int(WINDOW_START.timestamp())

    def fetch_trades_paginated(wal: str, max_pages: int = 25) -> list:
        all_trades = []
        with httpx.Client(timeout=20) as c:
            for page in range(max_pages):
                try:
                    r = c.get("https://data-api.polymarket.com/trades",
                              params={"user": wal, "limit": 100, "offset": page * 100})
                    d = r.json()
                    batch = d if isinstance(d, list) else d.get("trades", [])
                except Exception:
                    break
                if not batch:
                    break
                all_trades.extend(batch)
                # stop si dernière timestamp < window_start
                try:
                    last_ts = int(batch[-1].get("timestamp", 0))
                    if last_ts and last_ts < WINDOW_START_S:
                        break
                except Exception:
                    pass
                if len(batch) < 100:
                    break
                time.sleep(0.05)  # léger pacing pour pas saturer
        return all_trades

    if CACHE_WA.exists():
        wallet_acts = json.loads(CACHE_WA.read_text())
        log(f"\n[4] cache trades_paginated : {len(wallet_acts)}")
    else:
        wallet_acts = {}
        log(f"\n[4] fetch /trades?user= paginé {len(natives)} natives (window {WINDOW_DAYS}d)...")
        t0 = time.time()
        for i, wal in enumerate(natives):
            try:
                wallet_acts[wal] = fetch_trades_paginated(wal)
            except Exception:
                wallet_acts[wal] = []
            if (i + 1) % 50 == 0:
                el = time.time() - t0
                eta = el / (i + 1) * (len(natives) - i - 1)
                avg_trades = sum(len(v) for v in wallet_acts.values()) / len(wallet_acts)
                log(f"  ...{i+1}/{len(natives)} elapsed={el:.0f}s ETA={eta:.0f}s avg_trades/wallet={avg_trades:.0f}")
                CACHE_WA.write_text(json.dumps(wallet_acts))
        CACHE_WA.write_text(json.dumps(wallet_acts))

    # Stage 5 — extract trades + cids
    trades_by_wallet = {}
    cids_needed = set()
    for wal, acts in wallet_acts.items():
        sp = []
        for a in acts:
            if not isinstance(a, dict):
                continue
            q = a.get("title", "") or a.get("eventTitle", "")
            if looks_long(q):
                continue
            sp.append(a)
            cid = a.get("conditionId") or a.get("condition_id") or a.get("market_id")
            if cid:
                cids_needed.add(cid)
        if sp:
            trades_by_wallet[wal] = sp
    log(f"\n[5] natives avec trades non-long dans /activity : {len(trades_by_wallet)}")
    log(f"  cids distincts : {len(cids_needed)}")

    # Stage 6 — resolutions
    res = json.loads(CACHE_RES.read_text()) if CACHE_RES.exists() else {}
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
    to_resolve = [x for x in cids_needed if x not in res]
    log(f"  à résoudre via Gamma : {len(to_resolve)}")
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
    log(f"  résolus avec winner : {sum(1 for v in res.values() if v)}")

    # Stage 7 — per-wallet PnL avec hold-time analysis
    metrics = []
    for wal, sp in trades_by_wallet.items():
        # group by market+outcome to estimate hold time per market
        per_market = defaultdict(list)
        for a in sp:
            cid = a.get("conditionId") or a.get("condition_id") or a.get("market_id")
            try:
                ts_val = a.get("timestamp") or a.get("ts") or a.get("time")
                if not ts_val:
                    continue
                ti = int(ts_val)
                tms = ti * 1000 if ti < 10**12 else ti
                per_market[cid].append((tms, a))
            except Exception:
                continue
        # hold-time per market : end - start (heuristique)
        holds_minutes = []
        for cid, entries in per_market.items():
            if len(entries) < 2:
                continue
            ts_sorted = sorted(entries, key=lambda x: x[0])
            holds_minutes.append((ts_sorted[-1][0] - ts_sorted[0][0]) / 60000.0)
        hold_med = statistics.median(holds_minutes) if holds_minutes else 0.0
        # Cap 24h max
        if hold_med > HOLD_MAX_MIN:
            continue
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
        primary_cat = wallet_cats.get(wal, Counter()).most_common(1)[0][0] if wal in wallet_cats else "?"
        metrics.append(dict(
            addr=wal, n=len(tr_p) + len(ho_p),
            train_n=len(tr_p), train_pnl=sum(tr_p),
            hold_n=len(ho_p), hold_pnl=sum(ho_p),
            t_stat=t_stat, hold_med=hold_med, primary_cat=primary_cat,
        ))
    log(f"\n[7] metrics finales (hold<=24h) : {len(metrics)}")

    # Stage 8 — verdict
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
    log(f"spread : ${spread:+.1f}")

    bonf.sort(key=lambda x: -x["hold_pnl"])
    copyables.sort(key=lambda x: -x["hold_pnl"])

    lines = ["# Poly ALL CATEGORIES <=24h — NATIVES — Verdict\n",
             f"_Run : {NOW.isoformat()}_\n",
             "## Méthodo",
             f"- Univers : Polymarket TOUTES catégories sauf crypto-5/15min (déjà testé NON)",
             f"- Exclusions auto : marchés clairement >24h (election/season/etc.)",
             f"- Stratification : ~{per_cat_target} marchés/catégorie (cap {MAX_MARKETS})",
             f"- Per-wallet : hold médian ≤ {HOLD_MAX_MIN//60}h (24h max opérateur)",
             f"- Holdout temporel : {HOLDOUT_DAYS}j hors-échantillon",
             f"- Bonferroni : alpha=0.05/N, sub-window test optionnel\n",
             "## Volume par catégorie (univers Stage 1)"]
    for k in sorted(by_cat, key=lambda x: -len(by_cat[x])):
        lines.append(f"- {k} : {len(by_cat[k])}")
    lines.append(f"\n## Tests")
    lines.append(f"- Wallets retenus (n>=20, hold<=24h) : **{N}**")
    lines.append(f"- Copyables (train+/holdout+/hold_n>=10) : **{len(copyables)}**")
    lines.append(f"- Bonferroni (z>{z_crit:.2f}, alpha={alpha:.5f}) : **{len(bonf)}**")
    lines.append(f"- Spread top-bot quartile train → holdout : **${spread:+,.1f}**\n")

    lines.append("## VERDICT")
    if len(bonf) >= 5 and spread > 0:
        lines.append(f"### **OUI — edge copiable détecté toutes catégories <=24h.**\n")
    elif len(copyables) >= 10 and spread > 0:
        lines.append(f"### **TIÈDE — {len(copyables)} candidats, 0 Bonferroni strict, spread positif.**\n")
    elif N >= 30:
        lines.append(f"### **NON — pas d'edge copiable détecté sur les catégories <=24h.**\n")
    else:
        lines.append(f"### **ÉCHANTILLON TROP MINCE — N={N}.**\n")

    if bonf:
        lines.append("## Bonferroni survivants")
        lines.append("| wallet | primary_cat | hold(min) | n | train | holdout | t_stat |")
        lines.append("|---|---|---|---|---|---|---|")
        for m in bonf[:20]:
            lines.append(f"| {m['addr'][:14]} | {m['primary_cat']} | {m['hold_med']:.0f} | {m['n']} | ${m['train_pnl']:+.1f} | ${m['hold_pnl']:+.1f} | {m['t_stat']:.2f} |")
    if copyables:
        lines.append("\n## Candidats copiables (avant Bonferroni)")
        lines.append("| wallet | primary_cat | hold(min) | n | train | holdout | t_stat |")
        lines.append("|---|---|---|---|---|---|---|")
        for m in copyables[:30]:
            ts = f"{m['t_stat']:.2f}" if m["t_stat"] is not None else "—"
            lines.append(f"| {m['addr'][:14]} | {m['primary_cat']} | {m['hold_med']:.0f} | {m['n']} | ${m['train_pnl']:+.1f} | ${m['hold_pnl']:+.1f} | {ts} |")

    cat_dist = Counter(m["primary_cat"] for m in metrics)
    lines.append(f"\n## Distribution primary category des wallets retenus")
    for c, n in cat_dist.most_common():
        lines.append(f"- {c} : {n}")

    VERDICT.write_text("\n".join(lines))
    log(f"\n=> {VERDICT}")


if __name__ == "__main__":
    main()
