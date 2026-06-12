"""Max Edge — D Phase 2 : QUI gagne sur crypto up/down 5/15min récent,
et COMMENT (copiable ou non). Via API Polymarket.

Robuste : le CircuitBreaker de PolymarketHttpClient se VERROUILLE après
5 échecs (aucun reset) -> on recrée le client à chaque échec.
"""
import re
import sys
import time
from collections import defaultdict

sys.path.insert(0, "/opt/app/polyoracle/backend")
from app.services.polymarket.gamma_client import GammaClient
from app.services.polymarket.data_client import DataClient
from app.services.market_resolution_scanner import MarketResolutionScanner

MAX_MARKETS = 3000          # bornes runtime (~12 min au rate-limit 4/s)
COHORT_FILE = "/opt/app/cohort_running_3574.txt"
OUT = "/opt/app/d2_recent_winners_report.txt"

CRYPTO = re.compile(
    r"\b(btc|bitcoin|eth|ethereum|sol|solana|xrp|ripple|bnb|doge|dogecoin|"
    r"hype|hyperliquid|ada|cardano|link|avax|trx|tron|sui|ltc|near|apt)\b", re.I)
UPDOWN = re.compile(r"up or down|haut ou bas|vers le haut", re.I)


def bk(q):
    if re.search(r"15\s*m", q, re.I):
        return "15min"
    if re.search(r"\b5\s*m", q, re.I):
        return "5min"
    if re.search(r"hourly|\bhour", q, re.I):
        return "hourly"
    return "other"


def norm(s):
    return str(s or "").strip().lower()


def log(*a):
    print(*a, flush=True)


class Resilient:
    """Recrée le client sous-jacent à chaque échec (le CircuitBreaker latch)."""

    def __init__(self, factory):
        self._factory = factory
        self._c = factory()

    def __getattr__(self, name):
        def wrapped(*a, **kw):
            for attempt in range(4):
                try:
                    return getattr(self._c, name)(*a, **kw)
                except Exception:
                    self._c = self._factory()
                    time.sleep(0.4 * (attempt + 1))
            return None
        return wrapped


def main():
    cohort = set()
    try:
        with open(COHORT_FILE) as fh:
            cohort = {l.strip().lower() for l in fh if l.strip().startswith("0x")}
    except FileNotFoundError:
        pass
    g = Resilient(lambda: GammaClient(timeout=20))
    d = Resilient(lambda: DataClient())
    sc = MarketResolutionScanner()

    # --- Step 1 : enumérer marchés crypto up/down clos (les plus récents) ---
    log("=== D2 — enumération marchés crypto up/down clos ===")
    markets = []
    offset, page, scanned, empty = 0, 100, 0, 0
    while len(markets) < MAX_MARKETS:
        batch = g.fetch_closed_markets(limit=page, offset=offset, order="endDate")
        if batch is None:
            log("  abandon enumération (API KO) à offset=%d" % offset)
            break
        if not batch:
            empty += 1
            if empty >= 2:
                break
            offset += page
            continue
        empty = 0
        scanned += len(batch)
        for m in batch:
            q = (m.get("question") or "") + " " + (m.get("slug") or "")
            if not CRYPTO.search(q) or not UPDOWN.search(q):
                continue
            cid = m.get("conditionId") or m.get("condition_id")
            idx, name = sc.extract_winning_outcome(m)
            if not cid or name is None:
                continue
            ed = m.get("endDate") or m.get("end_date") or ""
            markets.append((cid, norm(name), bk(q), ed))
        offset += page
        if scanned % 2000 == 0:
            log("  ...scanné %d clos, crypto up/down=%d" % (scanned, len(markets)))
    bc = defaultdict(int)
    for _, _, b, _ in markets:
        bc[b] += 1
    log("marchés crypto up/down retenus : %d  %s" % (len(markets), dict(bc)))
    if markets:
        eds = sorted(m[3] for m in markets if m[3])
        if eds:
            log("  fenêtre couverte : %s -> %s" % (eds[0][:16], eds[-1][:16]))
    if not markets:
        log("AUCUN marché — stop.")
        return

    # --- Step 2 : trades de chaque marché ---
    log("\n=== fetch trades (%d marchés) ===" % len(markets))
    wtr = defaultdict(list)          # wallet -> [(won, price, bucket)]
    wsides = defaultdict(set)        # (wallet,cid) -> {sides}
    all_n, t0, ko = 0, time.time(), 0
    for i, (cid, winner, bucket, ed) in enumerate(markets):
        trades = d.fetch_market_trades(cid, limit=500)
        if trades is None:
            ko += 1
            continue
        for t in trades:
            wal = norm(t.get("proxyWallet") or t.get("proxy_wallet") or t.get("wallet"))
            side = str(t.get("side") or "").upper()
            out = norm(t.get("outcome"))
            price = t.get("price")
            if not wal or price is None:
                continue
            try:
                price = float(price)
            except Exception:
                continue
            if not (0.02 < price < 0.98):
                continue
            wsides[(wal, cid)].add(side)
            if side == "BUY":
                wtr[wal].append((out == winner, price, bucket))
                all_n += 1
        if (i + 1) % 300 == 0:
            log("  ...%d/%d marchés, %d trades BUY, %d KO, %.0fs"
                % (i + 1, len(markets), all_n, ko, time.time() - t0))
    log("trades BUY collectés : %d  wallets distincts : %d  (marchés KO : %d)"
        % (all_n, len(wtr), ko))

    # --- Step 3 : PnL par wallet ---
    def stats(tr):
        n = len(tr)
        w = sum(1 for won, _, _ in tr if won)
        pnl = sum((1.0 / p - 1.0) if won else -1.0 for won, p, _ in tr)
        px = sum(p for _, p, _ in tr) / n
        return n, w, pnl, px

    rows = []
    for wal, tr in wtr.items():
        if len(tr) < 20:
            continue
        n, w, pnl, px = stats(tr)
        mkts = {cid for (ww, cid) in wsides if ww == wal}
        scalped = sum(1 for cid in mkts if "SELL" in wsides.get((wal, cid), set()))
        rows.append(dict(wal=wal, n=n, wr=100.0 * w / n, pnl=pnl, px=px,
                         scalp=100.0 * scalped / len(mkts) if mkts else 0.0,
                         coh=(wal in cohort)))
    rows.sort(key=lambda r: -r["pnl"])

    lines = []

    def out(s=""):
        lines.append(s)
        log(s)

    out("\n=== D2 VERDICT — gagnants crypto up/down (fenêtre récente) ===")
    out("wallets avec n>=20 trades : %d" % len(rows))
    if rows:
        pos = [r for r in rows if r["pnl"] > 0]
        out("  positifs : %d (%.0f%%)  | négatifs : %d"
            % (len(pos), 100.0 * len(pos) / len(rows), len(rows) - len(pos)))
        cohr = [r for r in rows if r["coh"]]
        if cohr:
            out("  dont cohorte (3574) : %d wallets, %d positifs, PnL cumulé %+.0f"
                % (len(cohr), sum(1 for r in cohr if r["pnl"] > 0),
                   sum(r["pnl"] for r in cohr)))

    def show(title, rs):
        out("\n--- %s ---" % title)
        out("  wallet         n     WR    prix   PnL/1$  scalp%  cohorte")
        for r in rs:
            out("  %-14s %-5d %3.0f%%  %.3f  %+7.1f  %4.0f%%   %s"
                % (r["wal"][:14], r["n"], r["wr"], r["px"], r["pnl"],
                   r["scalp"], "OUI" if r["coh"] else ""))
    show("TOP 25 GAGNANTS", rows[:25])
    show("BOTTOM 10", rows[-10:])

    top = rows[:50]
    if top:
        out("\n--- profil des 50 meilleurs ---")
        out("  scalp rate moyen : %.0f%%  (haut = gère activement = dur à copier)"
            % (sum(r["scalp"] for r in top) / len(top)))
        out("  prix entrée moyen : %.3f" % (sum(r["px"] for r in top) / len(top)))
        out("  WR moyen : %.0f%%" % (sum(r["wr"] for r in top) / len(top)))
        out("  dans la cohorte : %d / 50" % sum(1 for r in top if r["coh"]))

    with open(OUT, "w") as fh:
        fh.write("\n".join(lines))
    log("\n-> rapport : %s" % OUT)


if __name__ == "__main__":
    main()
