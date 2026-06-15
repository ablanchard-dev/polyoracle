"""MIGRATION MFWR — refonte cohorte (2026-05-21).
Injecte les wallets validés copy-test (robustes 5/15min, plancher WR>=55%)
comme candidate_status='ELITE', copyability_score=EV normalisée, bande validée
dans best_category. Re-tag les vieux ELITE WR-global non validés -> LONG_SLEEVE.

DRY-RUN par défaut. --apply pour écrire.
"""
import sqlite3, csv, sys, time
from pathlib import Path

DB = str(Path(__file__).resolve().parents[1] / "data" / "polyoracle.db")
DRY = "--apply" not in sys.argv

def load(f):
    try:
        return list(csv.DictReader(open(f)))
    except Exception as e:
        print(f"ERR {f}: {e}"); return []

# --- assembler la cohorte validée ---
v5 = {r["wallet"]: (float(r["ev_copy60"]), int(r["n"]))
      for r in load("/tmp/m_valid5.csv") if r.get("robuste") == "1"}
v15 = {r["wallet"]: (float(r["ev_copy60"]), int(r["n"]))
       for r in load("/tmp/m_valid15.csv") if r.get("robuste") == "1"}
wr15 = {r["wallet"]: float(r["wr_pct"]) for r in load("/tmp/m_band15.csv") if r.get("wr_pct")}
wrd = {r["wallet"]: float(r["wr_pct"]) for r in load("/tmp/m_disco.csv") if r.get("wr_pct")}
wrx = {r["wallet"]: float(r["wr_pct"]) for r in load("/tmp/m_wrcheck.csv") if r.get("wr_pct")}

cohort = []
for w in set(v5) | set(v15):
    in5, in15 = w in v5, w in v15
    band = "CRYPTO_5M_15M" if (in5 and in15) else ("CRYPTO_5M" if in5 else "CRYPTO_15M")
    ev = max([x[0] for x in (v5.get(w), v15.get(w)) if x])
    n = sum([x[1] for x in (v5.get(w), v15.get(w)) if x])
    wr = next((x for x in (wr15.get(w), wrd.get(w), wrx.get(w)) if x is not None), None)  # %
    cohort.append({"w": w, "band": band, "ev": ev, "n": n, "wr": wr})

floor_ok = [c for c in cohort if c["wr"] is not None and c["wr"] >= 55.0]
excl_lowwr = [c for c in cohort if c["wr"] is not None and c["wr"] < 55.0]
excl_nowr = [c for c in cohort if c["wr"] is None]

def copyscore(ev):
    # map EV_copy -> [0.955, 0.999] pour bucketer SILVER/GOLD (gate >=0.95)
    return round(0.95 + min(max(ev, 0.0), 0.30) * 0.1633, 5)

# --- DB ---
con = sqlite3.connect(DB)
cur = con.cursor()
existing = {a for (a,) in cur.execute("SELECT address FROM marketfirstwalletrecord")}
old_elite = {a for (a,) in cur.execute(
    "SELECT address FROM marketfirstwalletrecord WHERE candidate_status='ELITE'")}
cohort_addrs = {c["w"] for c in floor_ok}
now = time.strftime("%Y-%m-%d %H:%M:%S")

ins = upd = 0
for c in floor_ok:
    w, band, ev, n, wr = c["w"], c["band"], c["ev"], c["n"], c["wr"] / 100.0
    cs = copyscore(ev)
    win = round(n * wr); los = n - win
    reasons = f"REFONTE_2026-05-21 band={band} ev_copy={ev:.4f} wr={c['wr']:.0f}"
    if w in existing:
        upd += 1
        if not DRY:
            cur.execute("""UPDATE marketfirstwalletrecord SET candidate_status='ELITE',
                copyability_score=?, best_category=?, resolved_market_win_rate=?,
                reasons=?, audit_at=? WHERE address=?""",
                (cs, band, wr, reasons, now, w))
    else:
        ins += 1
        if not DRY:
            cur.execute("""INSERT INTO marketfirstwalletrecord
                (address, market_first_score, composite_score, tier, status,
                 resolved_market_win_rate, win_rate_confidence, resolved_markets_traded,
                 resolved_winning_markets, resolved_losing_markets, recent_activity_score,
                 copyability_score, total_resolved_notional, average_position_size,
                 median_position_size, best_category, reasons, data_source, audit_at,
                 candidate_status)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (w, cs, cs, "ELITE", "STRONG_AND_ACTIVE", wr, "VALIDATED", n, win, los,
                 75.0, cs, 0.0, 2.0, 2.0, band, reasons, "copy_test_refonte", now, "ELITE"))

demote = [a for a in old_elite if a not in cohort_addrs]
if not DRY:
    for a in demote:
        cur.execute("UPDATE marketfirstwalletrecord SET candidate_status='LONG_SLEEVE', "
                    "reasons='REFONTE_2026-05-21 demoted from ELITE (WR-global, gardé sleeve long)' "
                    "WHERE address=?", (a,))
    con.commit()
con.close()

mode = "=== APPLIQUÉ ===" if not DRY else "=== DRY-RUN (rien écrit) ==="
print(mode)
print(f"cohorte validée totale : {len(cohort)}")
print(f"  plancher WR>=55% OK  : {len(floor_ok)}  <- injectés ELITE")
print(f"  exclus WR<55%        : {len(excl_lowwr)}")
print(f"  exclus WR inconnu    : {len(excl_nowr)}")
print(f"MFWR — INSERT (nouveaux wallets) : {ins}")
print(f"MFWR — UPDATE (déjà présents)    : {upd}")
print(f"MFWR — DEMOTE vieux ELITE -> LONG_SLEEVE : {len(demote)}")
print(f"  (anciens ELITE total: {len(old_elite)}, dont {len(old_elite & cohort_addrs)} re-validés gardés ELITE)")
b5 = sum(1 for c in floor_ok if c["band"] == "CRYPTO_5M")
b15 = sum(1 for c in floor_ok if c["band"] == "CRYPTO_15M")
bb = sum(1 for c in floor_ok if c["band"] == "CRYPTO_5M_15M")
print(f"bandes injectées : 5min={b5}  15min={b15}  5+15={bb}")
