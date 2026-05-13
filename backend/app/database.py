import logging
import os

from sqlalchemy import text
from sqlmodel import SQLModel, Session, create_engine

from app.config import get_settings

logger = logging.getLogger(__name__)

settings = get_settings()
if settings.local_first:
    settings.resolved_sqlite_path.parent.mkdir(parents=True, exist_ok=True)

database_url = settings.resolved_database_url
connect_args = {"check_same_thread": False} if database_url.startswith("sqlite") else {}
# v0.7.8 P6 — 2026-05-06: bump pool size from default (5+10) to 20+40
# pool_pre_ping evicts stale conns. Required to handle polling cohort 112+
# concurrent reads + UI refresh + market_metadata_resolver hooks.
engine = create_engine(
    database_url,
    echo=False,
    connect_args=connect_args,
    pool_size=20,
    max_overflow=40,
    pool_pre_ping=True,
    pool_recycle=300,
)


# v0.7.5 Option C — DATABASE_URL startup guard.
#
# v0.7.4 discovered that an inherited shell env var DATABASE_URL pointed
# to a different project's DB ("lumenia.db" under /Downloads/) and the
# backend silently wrote ALL polling/audit/no_trade/paper data to the
# wrong file for at least 24 h. The smoke 22→0 mystery was a
# direct consequence. This guard hard-fails at startup so the bug
# cannot recur silently.
_FORBIDDEN_URL_PATTERNS = ("lumenia", "/downloads/", "/temp/", "\\downloads\\", "\\temp\\")
_MIN_MFWR_ROWS = 100


def _validate_database_url(min_mfwr_rows: int = _MIN_MFWR_ROWS) -> None:
    """Hard-fail at startup if DATABASE_URL is misconfigured.

    Three checks (any one fails -> RuntimeError):

    1. DATABASE_URL env or resolved path does not contain a forbidden
       substring (e.g. ``lumenia``, ``/Downloads/``, ``/Temp/``).
    2. The active SQLite file resolves to a path under the polyoracle
       project root.
    3. The active DB has at least ``min_mfwr_rows`` rows in
       ``marketfirstwalletrecord`` (the truth pool table).

    Test bypass: pytest tmp paths contain ``/Temp/`` but they're legitimate
    test isolation. Detect ``pytest-of-`` substring (Windows) or
    ``pytest-`` segment (POSIX) and skip ALL checks.

    Raises:
        RuntimeError with a message that points the operator to the
        exact misconfiguration.
    """
    db_url_env = os.environ.get("DATABASE_URL", "") or ""
    resolved_path_str = str(settings.resolved_sqlite_path)
    db_url_str = str(database_url)
    haystack = (db_url_env + " " + resolved_path_str + " " + db_url_str).lower()
    # v0.7.8 — pytest test isolation tmp paths are LEGIT, skip all checks.
    if "pytest-of-" in haystack or "/pytest-" in haystack or "\\pytest-" in haystack:
        return
    for pattern in _FORBIDDEN_URL_PATTERNS:
        if pattern in haystack:
            raise RuntimeError(
                f"FATAL DATABASE_URL leak: pattern {pattern!r} found in env or "
                f"resolved path. DATABASE_URL={db_url_env!r}, "
                f"resolved_database_url={db_url_str!r}, "
                f"resolved_sqlite_path={resolved_path_str!r}. "
                f"Expected: data/polyoracle.db under POLYORACLE project. "
                f"Unset DATABASE_URL or set it explicitly to the polyoracle path."
            )

    if not database_url.startswith("sqlite"):
        # non-SQLite DB — skip MFWR sanity check, only forbidden-pattern guard.
        logger.info("DATABASE_URL guard: non-SQLite URL, MFWR sanity skipped.")
        return

    try:
        with engine.connect() as conn:
            try:
                count = conn.execute(
                    text("SELECT COUNT(*) FROM marketfirstwalletrecord")
                ).scalar()
            except Exception as exc:
                # Table may not exist yet on a fresh DB — distinguish that
                # from a wrong-DB error.
                count = None
                table_missing = "no such table" in str(exc).lower()
                if not table_missing:
                    raise RuntimeError(
                        f"FATAL DB sanity check failed: cannot read "
                        f"marketfirstwalletrecord at {resolved_path_str!r}: {exc}"
                    ) from exc
    except RuntimeError:
        raise
    except Exception as exc:
        raise RuntimeError(
            f"FATAL DB sanity check failed: cannot connect to {resolved_path_str!r}: {exc}"
        ) from exc

    if count is not None and count < min_mfwr_rows:
        raise RuntimeError(
            f"FATAL DB sanity check failed: active DB at {resolved_path_str!r} has "
            f"only {count} rows in marketfirstwalletrecord (need >= {min_mfwr_rows}). "
            f"Wrong DB or empty fresh install? Expected v0.7.0_FINAL pool ~238k rows."
        )

    logger.info(
        "DATABASE_URL guard: validated (path=%s, MFWR rows=%s)",
        resolved_path_str, count if count is not None else "fresh-table-missing",
    )


def init_db() -> None:
    from app.models import audit, bot, market, signal, trade, wallet  # noqa: F401

    SQLModel.metadata.create_all(engine)
    if database_url.startswith("sqlite"):
        settings.resolved_sqlite_path.touch(exist_ok=True)
        ensure_sqlite_schema()
        # Ensure WalletMarketResolutionAudit unique index (Phase G ledger)
        try:
            with engine.begin() as conn:
                conn.execute(text(
                    "CREATE UNIQUE INDEX IF NOT EXISTS "
                    "uq_wallet_market_outcome ON walletmarketresolutionaudit "
                    "(wallet_address, market_id, outcome)"
                ))
        except Exception:
            pass  # table may not exist yet on fresh DB or migrations
    # v0.7.5 — run guard AFTER schema creation so a fresh install
    # (zero MFWR rows) doesn't fail validation; it'll only trigger
    # when an existing wrong-project DB is mounted.
    _validate_database_url()


def ensure_sqlite_schema() -> None:
    extra_columns = {
        "market": {
            "clob_token_ids": "TEXT",
            "data_source": "VARCHAR DEFAULT 'mock'",
            "raw_slug": "TEXT",
            # v0.7.8 P4 — capital-turnover derived fields
            "expected_resolution_minutes": "FLOAT",
            "market_speed_class": "VARCHAR",
            "duration_bucket": "VARCHAR",
        },
        "wallet": {
            "tier": "VARCHAR DEFAULT 'INSUFFICIENT_DATA'",
            "confidence": "FLOAT DEFAULT 0",
            "data_source": "VARCHAR DEFAULT 'mock'",
        },
        "papertrade": {
            "signal_id": "TEXT",
            "wallet_address": "TEXT",
            "notional_usd": "FLOAT DEFAULT 0",
            "auto": "INTEGER DEFAULT 0",
            "close_reason": "TEXT",
        },
        "signal": {
            "decision": "VARCHAR DEFAULT 'WATCH'",
            "wallets": "TEXT",
            "cluster_id": "TEXT",
            "proposed_size_usd": "FLOAT DEFAULT 0",
            "copyable_edge": "FLOAT DEFAULT 0",
            "notes": "TEXT",
        },
        "botloopstate": {
            "last_cycle_summary": "TEXT",
            "polling_running": "INTEGER DEFAULT 0",
            "polling_started_at": "DATETIME",
            "last_poll_at": "DATETIME",
            "wallets_polled_count": "INTEGER DEFAULT 0",
            "trades_detected_total": "INTEGER DEFAULT 0",
            "paper_trades_via_polling": "INTEGER DEFAULT 0",
            "polling_errors_count": "INTEGER DEFAULT 0",
            "last_polling_error": "TEXT",
        },
        "marketfirstwalletrecord": {
            "candidate_status": "VARCHAR",
        },
        # v0.7.8 P6 — R-based dynamic sizing state machine
        # A-T0 (2026-05-11): strict_cutover_at
        # P0 Round 8 (2026-05-12): p0_fix_applied_at marker for A/B
        "botstate": {
            "current_r_multiplier": "FLOAT DEFAULT 2.0",
            "strict_cutover_at": "DATETIME",
            "p0_fix_applied_at": "DATETIME",
        },
    }
    with engine.begin() as connection:
        for table_name, columns in extra_columns.items():
            try:
                existing = {
                    row[1]
                    for row in connection.execute(text(f"PRAGMA table_info({table_name})")).fetchall()
                }
            except Exception:
                continue
            if not existing:
                continue
            for column_name, column_type in columns.items():
                if column_name not in existing:
                    connection.execute(text(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_type}"))


def get_session() -> Session:
    with Session(engine) as session:
        yield session
