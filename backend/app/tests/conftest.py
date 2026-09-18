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

# ─────────────────────────────────────────────────────────────────────────────────────────
# « la suite n'a besoin ni du reseau ni d'une cle » — tenu ici, pas seulement ecrit.
#
# Mesure du 18/09/2026 : 937 tests passent avec TOUTE sortie reseau bloquee et les variables
# de cle retirees de l'environnement. C'etait vrai, et tenu par rien. Un test qui appellerait
# une API passerait au vert sur une machine connectee et casserait chez qui clone.
# # Le depot interroge Polymarket (Gamma, CLOB, Data API). Un test qui y toucherait rendrait la suite dependante d'un service tiers et de ses quotas.
#
# La boucle locale reste ouverte : la couverture et le debogueur s'en servent, et l'interdire
# testerait pytest plutot que le produit.
#
# La PREUVE que ce blocage mord vit dans `test_la_suite_ne_sort_pas_du_poste.py` — un fichier que pytest COLLECTE.
# Le meme controle ecrit ici ne tournerait jamais, et le compte de tests ne bougerait pas.
import socket as _socket

_CONNECT = _socket.socket.connect
_CONNECT_EX = _socket.socket.connect_ex


class SortieReseauInterdite(RuntimeError):
    """Un test a tente de sortir. La suite n'est pas censee en avoir besoin."""


def _est_local(adresse) -> bool:
    try:
        hote = adresse[0]
    except (TypeError, IndexError):
        return False
    return hote in {"127.0.0.1", "::1", "localhost", ""}


def _refuser(vrai):
    def _appel(self, adresse, *a, **kw):
        if not _est_local(adresse):
            raise SortieReseauInterdite(f"sortie reseau vers {adresse!r}")
        return vrai(self, adresse, *a, **kw)
    return _appel


_socket.socket.connect = _refuser(_CONNECT)
_socket.socket.connect_ex = _refuser(_CONNECT_EX)
