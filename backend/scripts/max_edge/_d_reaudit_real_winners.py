"""Max Edge — D Phase 1 : réaudit de la cohorte sur les VRAIS gagnants.

100% local, 0 appel API. Source : publictrade (386k trades, jan-mai) +
resolvedmarketrecord (marchés résolus, vrai winning_outcome).

Mesure 3 choses sur les marchés crypto up/down 5/15min :
  A. Baseline « tout parier » — EV de copier littéralement chaque trade
     de tout le monde = le plancher zéro-skill (le vig).
  B. EV réelle par wallet — re-classe la cohorte qui tourne (3574 ELITE)
     sur la bonne métrique. Les wallets qu'on copie sont-ils les vrais
     gagnants ?
  C. EV consensus — par marché, vote majoritaire de la cohorte → le
     consensus bat-il le marché ? + variante haute-unanimité.
  D. Holdout temporel — calib (vieux 60%) vs holdout (récent 40%).
     Un edge réel survit au split. Pas d'in-sample.

PnL : 1$ misé par trade. won -> 1/price - 1 ; lost -> -1. EV = moyenne.
"""
import re
import sqlite3
import sys
from collections import defaultdict

DB = "/opt/app/polyoracle/data/polyoracle.db"
COHORT_FILE = "/opt/app/cohort_running_3574.txt"
FEE = 0.01  # frais ~1% du notional

CRYPTO = re.compile(
    r"\b(btc|bitcoin|eth|ethereum|sol|solana|xrp|ripple|bnb|doge|dogecoin|"
    r"hype|hyperliquid|ada|cardano|link|chainlink|avax|trx|tron|sui|"
    r"matic|polygon|pol|near|apt|aptos|ltc|litecoin)\b", re.I)
UPDOWN = re.compile(r"up or down|haut ou bas|vers le haut", re.I)
RE_5M = re.compile(r"\b5\s*m(in)?\b", re.I)
RE_15M = re.compile(r"\b15\s*m(in)?\b", re.I)
RE_HOUR = re.compile(r"hourly|\b1\s*h(our)?\b", re.I)


def bucket(q):
    if RE_5M.search(q):
        return "5min"
    if RE_15M.search(q):
        return "15min"
    if RE_HOUR.search(q):
        return "hourly"
    return "other"


def norm(s):
    return str(s or "").strip().lower()


def evstats(trades):
    """trades = list of (won:bool, price:float). -> dict de stats."""
    n = len(trades)
    if not n:
        return None
    w = sum(1 for won, p in trades if won)
    px = sum(p for _, p in trades) / n
    pnl = sum((1.0 / p - 1.0) if won else -1.0 for won, p in trades)
    pnl_net = pnl - FEE * n
    return dict(n=n, wr=100.0 * w / n, px=px,
                ev=pnl / n, ev_net=pnl_net / n, pnl_net=pnl_net)


def line(lbl, s):
    if not s:
        return "  %-22s n=0" % lbl
    return ("  %-22s n=%-6d WR=%4.1f%%  prix=%.3f  EV/1$=%+.4f  "
            "EV_net=%+.4f  PnL_net=%+.0f$"
            % (lbl, s["n"], s["wr"], s["px"], s["ev"], s["ev_net"], s["pnl_net"]))


def main():
    cohort = set()
    try:
        with open(COHORT_FILE) as fh:
            cohort = {l.strip().lower() for l in fh if l.strip().startswith("0x")}
    except FileNotFoundError:
        print("WARN cohort file absent — analyse universe-only", file=sys.stderr)
    print("=== D PHASE 1 — RÉAUDIT SUR VRAIS GAGNANTS ===")
    print("cohorte qui tourne : %d wallets\n" % len(cohort))

    c = sqlite3.connect("file:%s?mode=ro" % DB, uri=True, timeout=60)

    # --- Step 1 : marchés crypto up/down résolus ---
    mkt = {}  # condition_id -> (winner_lc, bucket)
    for cid, q, win in c.execute(
            "SELECT condition_id,question,winning_outcome_name "
            "FROM resolvedmarketrecord "
            "WHERE winning_outcome_name IS NOT NULL AND condition_id IS NOT NULL"):
        if not q or not CRYPTO.search(q) or not UPDOWN.search(q):
            continue
        mkt[cid] = (norm(win), bucket(q))
    bc = defaultdict(int)
    for _, b in mkt.values():
        bc[b] += 1
    print("marchés crypto up/down résolus : %d" % len(mkt))
    print("  par durée : " + "  ".join("%s=%d" % (k, bc[k]) for k in sorted(bc)))
    if not mkt:
        print("AUCUN marché — stop.")
        return

    # --- Step 2 : trades BUY sur ces marchés ---
    # all_tr : (wallet, cid, outcome_lc, price, traded_at, won, bucket)
    all_tr = []
    dates = []
    for wal, cid, out, price, ts in c.execute(
            "SELECT wallet_address,market_id,outcome,price,traded_at "
            "FROM publictrade WHERE UPPER(side)='BUY'"):
        m = mkt.get(cid)
        if not m or price is None or not (0.02 < price < 0.98):
            continue
        winner, bk = m
        won = norm(out) == winner
        all_tr.append((norm(wal), cid, norm(out), float(price), ts or "", won, bk))
        if ts:
            dates.append(ts)
    c.close()
    print("trades BUY sur marchés crypto up/down : %d" % len(all_tr))
    if not all_tr:
        print("AUCUN trade — stop.")
        return
    dates.sort()
    cutoff = dates[int(len(dates) * 0.60)]
    print("split holdout : calib <= %s  /  holdout > %s\n" % (cutoff[:10], cutoff[:10]))

    # ============ A. BASELINE « tout parier » ============
    print("## A. BASELINE — copier littéralement chaque trade (zéro-skill)")
    print(line("TOUT LE MONDE", evstats([(t[5], t[3]) for t in all_tr])))
    in_co = [t for t in all_tr if t[0] in cohort]
    out_co = [t for t in all_tr if t[0] not in cohort]
    print(line("cohorte 3574", evstats([(t[5], t[3]) for t in in_co])))
    print(line("hors cohorte", evstats([(t[5], t[3]) for t in out_co])))
    for b in ("5min", "15min", "hourly"):
        sub = [(t[5], t[3]) for t in all_tr if t[6] == b]
        if sub:
            print(line("  tous / " + b, evstats(sub)))

    # ============ B. EV RÉELLE PAR WALLET ============
    print("\n## B. EV RÉELLE PAR WALLET (re-classement sur vrais gagnants)")
    byw = defaultdict(list)
    for t in all_tr:
        byw[t[0]].append((t[5], t[3]))
    wstats = {}
    for wal, tr in byw.items():
        if len(tr) >= 30:
            wstats[wal] = evstats(tr)
    co_w = {w: s for w, s in wstats.items() if w in cohort}
    un_w = {w: s for w, s in wstats.items() if w not in cohort}
    print("wallets avec n>=30 : %d  (dont cohorte : %d)" % (len(wstats), len(co_w)))

    def split_pos(d):
        pos = sum(1 for s in d.values() if s["ev_net"] > 0)
        return pos, len(d) - pos
    cp, cn = split_pos(co_w)
    print("  cohorte 3574 : %d EV+ / %d EV-  | PnL_net cumulé %+.0f$"
          % (cp, cn, sum(s["pnl_net"] for s in co_w.values())))
    if un_w:
        up, un = split_pos(un_w)
        print("  hors cohorte : %d EV+ / %d EV-  | PnL_net cumulé %+.0f$"
              % (up, un, sum(s["pnl_net"] for s in un_w.values())))
    # le top universe : la cohorte capte-t-elle les vrais meilleurs ?
    ranked = sorted(wstats.items(), key=lambda x: -x[1]["ev_net"])
    top100 = ranked[:100]
    inco = sum(1 for w, _ in top100 if w in cohort)
    print("  top-100 wallets (EV_net) : %d sont dans la cohorte / 100" % inco)
    print("  --- top-10 wallets univers (EV_net réelle) ---")
    for w, s in ranked[:10]:
        print("    %s n=%-4d WR=%4.1f%% EV_net=%+.4f %s"
              % (w[:14], s["n"], s["wr"], s["ev_net"],
                 "[COHORTE]" if w in cohort else ""))

    # ============ C. CONSENSUS ============
    print("\n## C. CONSENSUS — vote majoritaire de la cohorte par marché")
    # par marché : votes cohorte par outcome (1 wallet = 1 voix/outcome)
    votes = defaultdict(lambda: defaultdict(set))   # cid -> outcome -> {wallets}
    px_co = defaultdict(lambda: defaultdict(list))  # cid -> outcome -> [prices]
    for wal, cid, out, price, ts, won, bk in all_tr:
        if wal in cohort:
            votes[cid][out].add(wal)
            px_co[cid][out].append(price)
    cons_all, cons_strong = [], []
    for cid, ov in votes.items():
        winner, bk = mkt[cid]
        tally = sorted(ov.items(), key=lambda x: -len(x[1]))
        if not tally:
            continue
        top_out, top_v = tally[0][0], len(tally[0][1])
        total_v = sum(len(s) for s in ov.values())
        prices = px_co[cid][top_out]
        if not prices:
            continue
        p = sum(prices) / len(prices)
        won = (top_out == winner)
        cons_all.append((won, p))
        if total_v >= 3 and top_v / total_v >= 0.70:
            cons_strong.append((won, p))
    print(line("consensus (tout)", evstats(cons_all)))
    print(line("consensus unanime>=70%", evstats(cons_strong)))

    # ============ D. HOLDOUT ============
    print("\n## D. HOLDOUT TEMPOREL — un edge réel survit au split")
    calib = [t for t in all_tr if t[4] <= cutoff]
    hold = [t for t in all_tr if t[4] > cutoff]

    # D1 : la cohorte, calib vs holdout
    print(line("cohorte / calib", evstats([(t[5], t[3]) for t in calib if t[0] in cohort])))
    print(line("cohorte / holdout", evstats([(t[5], t[3]) for t in hold if t[0] in cohort])))

    # D2 : wallets EV+ en calib -> tiennent-ils en holdout ?
    cw = defaultdict(list)
    for t in calib:
        cw[t[0]].append((t[5], t[3]))
    calib_winners = {w for w, tr in cw.items()
                     if len(tr) >= 20 and evstats(tr)["ev_net"] > 0}
    hold_tr = [(t[5], t[3]) for t in hold if t[0] in calib_winners]
    print("  wallets EV+ en calib (n>=20) : %d" % len(calib_winners))
    print(line("  leurs trades holdout", evstats(hold_tr)))

    # D3 : consensus calib vs holdout
    def cons_for(trades):
        v = defaultdict(lambda: defaultdict(set))
        px = defaultdict(lambda: defaultdict(list))
        for wal, cid, out, price, ts, won, bk in trades:
            if wal in cohort:
                v[cid][out].add(wal)
                px[cid][out].append(price)
        res = []
        for cid, ov in v.items():
            winner, bk = mkt[cid]
            tally = sorted(ov.items(), key=lambda x: -len(x[1]))
            if not tally or not px[cid][tally[0][0]]:
                continue
            pr = px[cid][tally[0][0]]
            res.append((tally[0][0] == winner, sum(pr) / len(pr)))
        return res
    print(line("consensus / calib", evstats(cons_for(calib))))
    print(line("consensus / holdout", evstats(cons_for(hold))))

    print("\n=== LECTURE ===")
    print("Si A(cohorte) ~ A(tout le monde) -> les wallets n'ajoutent rien.")
    print("Si C > A ET C/holdout reste > 0 -> signal consensus reel.")
    print("Si D2/holdout <= 0 -> le classement par wallet ne persiste pas (overfit).")


if __name__ == "__main__":
    main()
