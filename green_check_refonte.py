"""GREEN CHECK refonte POLYORACLE — 2026-05-21.

Vérifie l'état critique post-refonte :
  - DB : cohorte EV-based (ELITE refonte, copyability_score, best_category)
  - code déployé (markers REFONTE, band gate, WS wiring, fichier WS)
  - env : mode paper (LIVE_ENABLED off), flags reclass / WS / band gate
  - runtime : service, /health, /bot/status, polling, WS activity, logs

Usage : python3 green_check_refonte.py [--section a|b|all] [--since "6 min ago"]
  a   = DB + code + env   (OK même bot arrêté)
  b   = runtime           (bot doit tourner)
  all = les deux (défaut)
Exit 0 si aucun [FAIL], 1 sinon.
"""
import sqlite3, http.client, json, subprocess, sys, os

DB = "/opt/app/polyoracle/data/polyoracle.db"
BACKEND = "/opt/app/polyoracle/backend"
ENV_FILE = f"{BACKEND}/.env"
HOST, PORT = "127.0.0.1", 8000

results = []  # (level, label, detail)


def add(level, label, detail=""):
    results.append((level, label, detail))


def http_get(path, timeout=8):
    try:
        c = http.client.HTTPConnection(HOST, PORT, timeout=timeout)
        c.request("GET", path)
        r = c.getresponse()
        body = r.read().decode("utf-8", "ignore")
        c.close()
        return r.status, body
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"


def sh(cmd):
    try:
        return subprocess.run(cmd, shell=True, capture_output=True,
                              text=True, timeout=30).stdout
    except Exception as e:
        return f"ERR {e}"


def env_flags():
    out = {}
    try:
        for line in open(ENV_FILE, encoding="utf-8"):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip()
    except Exception:
        pass
    return out


def section_a():
    try:
        con = sqlite3.connect(DB)
        cur = con.cursor()
        qc = cur.execute("PRAGMA quick_check").fetchone()[0]
        add("GREEN" if qc == "ok" else "RED", "DB quick_check", qc)
        n_elite, n_bad = cur.execute(
            "SELECT COUNT(*), SUM(CASE WHEN copyability_score IS NULL "
            "OR copyability_score < 0.95 THEN 1 ELSE 0 END) "
            "FROM marketfirstwalletrecord WHERE candidate_status='ELITE'"
        ).fetchone()
        n_bad = n_bad or 0
        add("GREEN" if n_elite >= 3000 and n_bad == 0 else "RED",
            "Cohorte ELITE refonte", f"n={n_elite} copyscore_invalides={n_bad}")
        cats = cur.execute(
            "SELECT best_category, COUNT(*) FROM marketfirstwalletrecord "
            "WHERE candidate_status='ELITE' GROUP BY best_category").fetchall()
        bad = [c for c, _ in cats
               if c not in ("CRYPTO_5M", "CRYPTO_15M", "CRYPTO_5M_15M")]
        add("GREEN" if not bad else "RED", "best_category ELITE",
            " ".join(f"{c}={n}" for c, n in cats))
        con.close()
    except Exception as e:
        add("RED", "DB checks", f"{type(e).__name__}: {e}")

    code_checks = [
        ("code: load_cohort REFONTE",
         f"{BACKEND}/app/services/wallet_polling_engine.py", "REFONTE 2026-05-21"),
        ("code: band gate",
         f"{BACKEND}/app/services/wallet_polling_engine.py", "_band_aware_reject"),
        ("code: reclass flag-gated",
         f"{BACKEND}/app/main.py", "WEEKLY_RECLASS_ENABLED"),
        ("code: WS activity wired",
         f"{BACKEND}/app/main.py", "init_ws_activity_service"),
        ("code: _decide edge-gate (copyable_edge maître)",
         f"{BACKEND}/app/services/trade_audit_engine.py", "COPYABLE_EDGE_PAPER_FLOOR"),
    ]
    for label, path, marker in code_checks:
        try:
            found = marker in open(path, encoding="utf-8").read()
            add("GREEN" if found else "RED", label,
                "présent" if found else "ABSENT")
        except Exception as e:
            add("RED", label, str(e))
    ws_file = f"{BACKEND}/app/services/polymarket_ws_activity.py"
    add("GREEN" if os.path.exists(ws_file) else "RED",
        "code: fichier polymarket_ws_activity.py",
        "présent" if os.path.exists(ws_file) else "ABSENT")

    f = env_flags()
    live = f.get("LIVE_ENABLED", "0").lower()
    add("GREEN" if live in ("0", "false", "no", "") else "RED",
        "env: mode paper (LIVE_ENABLED)", f"LIVE_ENABLED={f.get('LIVE_ENABLED')}")
    rc = f.get("WEEKLY_RECLASS_ENABLED", "").lower()
    add("RED" if rc in ("1", "true", "yes", "on") else "GREEN",
        "env: reclass désactivé",
        f"WEEKLY_RECLASS_ENABLED={f.get('WEEKLY_RECLASS_ENABLED') or '(absent → off)'}")
    add("GREEN", "env: WS activity flag",
        f"POLYMARKET_WS_ACTIVITY_ENABLED={f.get('POLYMARKET_WS_ACTIVITY_ENABLED') or '(absent → off)'}")
    bg = f.get("BAND_AWARE_GATE_ENABLED", "").lower()
    add("WARN" if bg in ("0", "false", "no", "off") else "GREEN",
        "env: band gate",
        f"BAND_AWARE_GATE_ENABLED={f.get('BAND_AWARE_GATE_ENABLED') or '(absent → on)'}")
    wp = f.get("WALLET_POLLING_ENABLED", "").lower()
    add("GREEN" if wp in ("0", "false", "no", "off") else "WARN",
        "env: polling per-wallet coupé (mode WS-only)",
        f"WALLET_POLLING_ENABLED={f.get('WALLET_POLLING_ENABLED') or '(absent → ON: polling actif)'}")


def section_b(since):
    active = sh("systemctl is-active polyoracle-backend.service").strip()
    add("GREEN" if active == "active" else "RED", "service systemd", active)
    if active != "active":
        return

    st, body = http_get("/health")
    add("GREEN" if st == 200 else "RED", "GET /health", f"HTTP {st} {body[:80]}")

    st, body = http_get("/bot/status")
    add("GREEN" if st == 200 else "RED", "GET /bot/status", f"HTTP {st} {body[:200]}")

    st, body = http_get("/bot/polling/status")
    add("GREEN" if st == 200 else "WARN", "GET /bot/polling/status",
        f"HTTP {st} {body[:240]}")

    st, body = http_get("/observability/ws-activity")
    if st == 200:
        try:
            j = json.loads(body)
            if j.get("instance") is None and not j.get("running"):
                add("WARN", "WS activity",
                    f"service non initialisé (flag={j.get('enabled_flag')})")
            else:
                run, conn = j.get("running"), j.get("connected")
                evs = j.get("events_received") or 0
                # WS-only : seule lane de détection → exigeant (events > 0).
                add("GREEN" if run and conn and evs > 0 else "RED",
                    "WS activity (lane de détection unique)",
                    f"running={run} connected={conn} "
                    f"events={j.get('events_received')} "
                    f"matched={j.get('trades_matched_cohort')} "
                    f"dispatched={j.get('trades_dispatched')} "
                    f"err={j.get('last_error')}")
        except Exception as e:
            add("RED", "WS activity", f"parse err {e}: {body[:120]}")
    else:
        add("RED", "WS activity endpoint", f"HTTP {st} {body[:80]}")

    logs = sh(f'journalctl -u polyoracle-backend.service --no-pager --since "{since}"')
    n_tb = logs.count("Traceback (most recent call last)")
    n_err = sum(1 for l in logs.splitlines() if ":ERROR:" in l or " ERROR " in l)
    add("GREEN" if n_tb == 0 else "RED", "logs : tracebacks depuis restart",
        f"{n_tb} traceback(s)")
    add("GREEN" if n_err == 0 else "WARN", "logs : lignes ERROR", f"{n_err}")
    if "auto-reclass daily: ENABLED" in logs:
        add("RED", "reclass runtime", "ENABLED détecté dans les logs !")
    elif "auto-reclass daily: DISABLED" in logs:
        add("GREEN", "reclass runtime", "log DISABLED confirmé")
    else:
        add("WARN", "reclass runtime", "aucun log reclass (since trop court ?)")

    if "wallet polling DÉSACTIVÉ" in logs:
        add("GREEN", "polling runtime", "scan per-wallet coupé — confirmé (WS-only)")
    elif "polling architecture = WORKERS" in logs or "P0-B workers: pool=" in logs:
        add("WARN", "polling runtime",
            "scan per-wallet ACTIF (WALLET_POLLING_ENABLED non coupé)")
    else:
        add("WARN", "polling runtime", "aucun log polling (since trop court ?)")


def main():
    section, since = "all", "6 min ago"
    args = sys.argv[1:]
    for i, a in enumerate(args):
        if a == "--section" and i + 1 < len(args):
            section = args[i + 1]
        if a == "--since" and i + 1 < len(args):
            since = args[i + 1]

    if section in ("a", "all"):
        section_a()
    if section in ("b", "all"):
        section_b(since)

    print("=" * 64)
    reds = 0
    for level, label, detail in results:
        mark = {"GREEN": "[ OK ]", "RED": "[FAIL]", "WARN": "[WARN]"}[level]
        print(f"{mark}  {label}: {detail}")
        if level == "RED":
            reds += 1
    print("=" * 64)
    if reds == 0:
        print("=== TOUT VERT ===")
        sys.exit(0)
    print(f"=== {reds} ECHEC(S) — voir [FAIL] ===")
    sys.exit(1)


if __name__ == "__main__":
    main()
