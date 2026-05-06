import os

# Scrub any environment variables that could redirect SQLite to another project's DB.
# Tests build their own engines and shouldn't pick up shell-leaked DATABASE_URL or POLYORACLE_*.
os.environ.pop("DATABASE_URL", None)


# v0.7.2 — module-level SignalClusterEngine state leaks between tests.
# Reset it on each test to keep paper auto trades deterministic.
import pytest


@pytest.fixture(autouse=True)
def _reset_cluster_engine_each_test():
    try:
        from app.services.paper_trading_engine import reset_cluster_engine
    except Exception:  # pragma: no cover — module not importable in some unit tests
        yield
        return
    reset_cluster_engine()
    yield
    reset_cluster_engine()
