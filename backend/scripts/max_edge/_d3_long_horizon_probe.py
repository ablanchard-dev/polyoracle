"""Max Edge — D3 v2 : a-t-on de vrais wallets EV-positifs sur les marchés
à horizon long (Sports ~2-4h = la bande 60min-24h ; Politics/events >24h) ?

Classement par CATÉGORIE (resolvedmarketrecord.category) — la durée directe
est trop peu peuplée. 100% local. Join sur condition_id.
"""
import re
import sqlite3
from collections import defaultdict
from pathlib import Path

DB = str(Path(__file__).resolve().parents[3] / "data" / "polyoracle.db")
SHORT_CRYPTO = re.compile(r"up or down|haut ou bas|vers le haut", re.I)


def norm(s):
    return str(s or "").strip().lower()


def klass(cat, q):
    c = norm(cat)
    if "sport" in c:
        return "Sports(~2-4h)"
    if "crypto" in c or re.search(
            r"\b(btc|bitcoin|eth|ethereum|sol|solana|xrp|bnb|doge)\b", q or "", re.I):
        return "Crypto-5/15min" if SHORT_CRYPTO.search(q or "") else "Crypto-autre"
    if "politic" in c or "election" in c:
        return "Politics(>24h)"
    return "Autre/Events"


def main():
    c = sqlite3.connect("file:%s?mode=ro" % DB, uri=True, timeout=60)

    mkt = {}  # cid -> (winner_lc, klass, end_date)
    kc = defaultdict(int)
    for cid, q, cat, win, ed in c.execute(
            "SELECT condition_id,question,category,winning_outcome_name,end_date "
            "FROM resolvedmarketrecord WHERE winning_outcome_name IS NOT NULL "
            "AND condition_id IS NOT NULL"):
        k = klass(cat, q)
        mkt[cid] = (norm(win), k, ed or "")
        kc[k] += 1
    print("=== D3 v2 — %d marchés résolus, par classe ===" % len(mkt))
    for k, n in sorted(kc.items(), key=lambda x: -x[1]):
        print("  %-18s %d" % (k, n))

    # trades BUY par classe + par wallet
    bytk = defaultdict(lambda: defaultdict(list))  # klass -> wallet -> [(won,price,date)]
    ntot = defaultdict(int)
    for wal, cid, out, price, side, ts in c.execute(
            "SELECT wallet_address,market_id,outcome,price,side,traded_at "
            "FROM publictrade WHERE UPPER(side)='BUY'"):
        v = mkt.get(cid)
        if not v or price is None or not (0.02 < price < 0.98):
            continue
        bytk[v[1]][norm(wal)].append((norm(out) == v[0], float(price), ts or ""))
        ntot[v[1]] += 1
    print("\n=== couverture publictrade par classe ===")
    for k in sorted(ntot, key=lambda x: -ntot[x]):
        print("  %-18s %d trades BUY" % (k, ntot[k]))

    elite = set(r[0].lower() for r in c.execute(
        "SELECT address FROM marketfirstwalletrecord WHERE candidate_status='ELITE'")
        if r[0])
    c.close()

    def ev(tr):
        n = len(tr)
        w = sum(1 for won, _, _ in tr if won)
        pnl = sum((1.0 / p - 1.0) if won else -1.0 for won, p, _ in tr)
        return n, w, pnl, sum(p for _, p, _ in tr) / n

    # ré-audit détaillé sur Sports + Crypto-autre (la vraie bande 60min-24h)
    for K in ("Sports(~2-4h)", "Crypto-autre", "Politics(>24h)"):
        byw = bytk.get(K, {})
        rows = []
        for wal, tr in byw.items():
            if len(tr) < 20:
                continue
            n, w, pnl, px = ev(tr)
            # holdout : tri temporel, 1ère moitié vs 2e
            srt = sorted(tr, key=lambda x: x[2])
            h = len(srt) // 2
            _, _, p1, _ = ev(srt[:h]) if h else (0, 0, 0, 0)
            _, _, p2, _ = ev(srt[h:]) if h else (0, 0, 0, 0)
            rows.append(dict(wal=wal, n=n, wr=100.0 * w / n, pnl=pnl, px=px,
                             evpt=pnl / n, stable=(p1 > 0 and p2 > 0),
                             elite=(wal in elite)))
        rows.sort(key=lambda r: -r["pnl"])
        print("\n=== %s — wallets n>=20 : %d ===" % (K, len(rows)))
        if not rows:
            print("  (pas assez de données)")
            continue
        pos = [r for r in rows if r["pnl"] > 0]
        stable = [r for r in rows if r["stable"] and r["pnl"] > 0]
        print("  positifs : %d (%.0f%%)  | positifs ET stables (2 moitiés>0) : %d"
              % (len(pos), 100.0 * len(pos) / len(rows), len(stable)))
        print("  EV/trade moyen population : %+.4f" % (sum(r["evpt"] for r in rows) / len(rows)))
        elr = [r for r in rows if r["elite"]]
        if elr:
            print("  MFWR ELITE actifs ici : %d, dont %d positifs, EV/tr moyen %+.4f"
                  % (len(elr), sum(1 for r in elr if r["pnl"] > 0),
                     sum(r["evpt"] for r in elr) / len(elr)))
        print("  --- top 12 (PnL réel) ---")
        print("  wallet         n     WR    prix   EV/tr   PnL    stable elite")
        for r in rows[:12]:
            print("  %-14s %-5d %3.0f%%  %.3f  %+.3f %+7.1f   %s    %s"
                  % (r["wal"][:14], r["n"], r["wr"], r["px"], r["evpt"], r["pnl"],
                     "OUI" if r["stable"] else "non", "OUI" if r["elite"] else ""))


if __name__ == "__main__":
    main()
