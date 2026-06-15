"""Preflight health check au boot du service polyoracle-v2-paper.

Valide les pré-requis critiques avant de connecter le RTDS listener :
1. DB SQLite reachable + cohort ELITE chargeable (≥ 50 wallets)
2. Polymarket Gamma API reachable (markets data)
3. Polymarket CLOB data API reachable (trades stream)
4. Orderbook WS port reachable (TCP connect uniquement, pas de subscribe)
5. RTDS WS port reachable (TCP connect)
6. py-clob-client lib importable (version v2 préférée)
7. ENV secrets (PK CLOB) présents si execution prévue

Si fail critique → raise PreflightError, le launcher abort avant tout WS.
Si fail non-critique → log warning, continue.

Pattern hérité de hyperdex/app/services/paper/preflight.py.
"""
from __future__ import annotations

import json
import os
import socket
import sqlite3
import urllib.request
from pathlib import Path
from typing import Optional


class PreflightError(RuntimeError):
    """Erreur critique au preflight — bot ne doit pas démarrer."""


def _check(label: str, ok: bool, detail: str = "", critical: bool = True) -> bool:
    marker = "✓" if ok else ("✗" if critical else "⚠")
    print(f"[PREFLIGHT-V2] {marker} {label}: {detail}", flush=True)
    return ok


def _tcp_reachable(host: str, port: int, timeout_s: float = 5.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout_s):
            return True
    except (socket.timeout, OSError):
        return False


def _http_get(url: str, timeout_s: float = 5.0) -> Optional[bytes]:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "PolyV2-Preflight/1.0"})
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            return resp.read()
    except Exception:
        return None


def run_preflight(
    db_path: Path,
    cohort_status: str = "ELITE",
    min_cohort_size: int = 50,
    require_clob_pk: bool = False,
) -> bool:
    """Returns True si tout OK, sinon raise PreflightError sur critique."""
    print("[PREFLIGHT-V2] === Polymarket V2 boot health check ===", flush=True)
    failures: list[str] = []

    # 1. DB + cohort
    if not db_path.exists():
        _check("DB SQLite", False, f"NOT FOUND at {db_path}")
        failures.append(f"db_missing: {db_path}")
    else:
        try:
            conn = sqlite3.connect(str(db_path))
            cur = conn.execute(
                "SELECT COUNT(*) FROM marketfirstwalletrecord WHERE candidate_status=?",
                (cohort_status,),
            )
            n_cohort = cur.fetchone()[0]
            conn.close()
            ok = n_cohort >= min_cohort_size
            _check(f"Cohort {cohort_status}", ok,
                   f"{n_cohort} wallets (min={min_cohort_size})")
            if not ok:
                failures.append(f"cohort_too_small: {n_cohort} < {min_cohort_size}")
        except Exception as e:
            _check("Cohort query", False, f"{type(e).__name__}: {str(e)[:80]}")
            failures.append(f"cohort_query_fail: {type(e).__name__}")

    # 2. Gamma API
    body = _http_get("https://gamma-api.polymarket.com/markets?limit=1")
    if body is None:
        _check("Gamma API", False, "unreachable")
        failures.append("gamma_api_unreachable")
    else:
        try:
            data = json.loads(body)
            ok = isinstance(data, list) and len(data) > 0
            _check("Gamma API", ok, f"got {len(data) if isinstance(data,list) else 'non-list'} market")
            if not ok:
                failures.append("gamma_api_unexpected_response")
        except Exception as e:
            _check("Gamma API parse", False, f"{type(e).__name__}")
            failures.append(f"gamma_parse_fail: {type(e).__name__}")

    # 3. CLOB data API (trades)
    body = _http_get("https://data-api.polymarket.com/trades?limit=1")
    if body is None:
        _check("CLOB data-api /trades", False, "unreachable")
        failures.append("data_api_unreachable")
    else:
        _check("CLOB data-api /trades", True, f"{len(body)}B response")

    # 4. Orderbook WS TCP connect (pas de WS handshake, juste port open)
    if _tcp_reachable("ws-subscriptions-clob.polymarket.com", 443):
        _check("Orderbook WS port (TCP 443)", True, "reachable")
    else:
        _check("Orderbook WS port (TCP 443)", False, "TCP timeout")
        failures.append("orderbook_ws_unreachable")

    # 5. RTDS WS port (sub-second wallet activity stream)
    # Endpoint confirmé via docs : wss://ws-live-data.polymarket.com
    if _tcp_reachable("ws-live-data.polymarket.com", 443):
        _check("RTDS WS port (TCP 443)", True, "reachable")
    else:
        _check("RTDS WS port (TCP 443)", False, "TCP timeout — fallback à data-api polling")
        # Non-critique : si RTDS pas accessible, fallback existe (REST polling 2-5s)
        # mais on logue pour visibilité.

    # 6. py-clob-client importable
    try:
        # On préfère v2 si dispo
        try:
            import py_clob_client_v2  # noqa: F401
            v = getattr(py_clob_client_v2, "__version__", "v2")
            _check("py-clob-client", True, f"v2 ({v})")
        except ImportError:
            import py_clob_client  # noqa: F401
            v = getattr(py_clob_client, "__version__", "v1")
            _check("py-clob-client", True, f"v1 fallback ({v}) — install v2 pour POLY_1271",
                   critical=False)
    except ImportError as e:
        _check("py-clob-client", False, f"NOT INSTALLED: {e}")
        failures.append("py_clob_client_missing")

    # 7. ENV PK (uniquement si execution prévue)
    if require_clob_pk:
        pk = os.getenv("POLYMARKET_PRIVATE_KEY") or os.getenv("CLOB_PRIVATE_KEY")
        if pk and len(pk) >= 32:
            _check("ENV CLOB private key", True, f"{len(pk)} chars present")
        else:
            _check("ENV CLOB private key", False, "missing / too short")
            failures.append("clob_pk_missing")

    # Verdict
    print("[PREFLIGHT-V2] " + "=" * 50, flush=True)
    if not failures:
        print("[PREFLIGHT-V2] ✅ ALL CRITICAL CHECKS PASSED — boot proceeds",
              flush=True)
        return True
    print(f"[PREFLIGHT-V2] ❌ CRITICAL FAILURES ({len(failures)}):", flush=True)
    for f in failures:
        print(f"[PREFLIGHT-V2]   - {f}", flush=True)
    raise PreflightError(f"Preflight V2 failed: {failures}")


if __name__ == "__main__":
    # CLI smoke test
    db = Path(__file__).resolve().parents[4] / "data" / "polyoracle.db"
    run_preflight(db_path=db, cohort_status="ELITE", min_cohort_size=50,
                  require_clob_pk=False)
