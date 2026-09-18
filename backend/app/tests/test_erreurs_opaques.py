"""Une réponse d'erreur ne doit rien apprendre à qui la reçoit.

CodeQL, 18/09/2026 : `py/stack-trace-exposure`, six fois. Les routes renvoyaient le message de
l'exception tel quel. Ces tests tiennent les deux moitiés de la règle : **rien ne fuite au
client**, et **tout reste dans le journal** — sinon on aurait échangé une fuite contre une
panne de diagnostic.
"""
from __future__ import annotations

import logging
import re

from app.api.erreurs import MESSAGE_GENERIQUE, erreur_opaque


class _JournalEspion(logging.Logger):
    def __init__(self):
        super().__init__("espion")
        self.lignes: list[str] = []

    def warning(self, msg, *args, **kwargs):  # type: ignore[override]
        self.lignes.append(msg % args if args else str(msg))


def _exception_bavarde() -> Exception:
    return ConnectionError(
        "[Errno 111] Connection refused to 10.0.3.14:5432 "
        "while running SELECT * FROM wallets WHERE api_key='sk-live-abc'"
    )


def test_la_reponse_ne_contient_ni_hote_ni_requete_ni_secret():
    journal = _JournalEspion()
    reponse = erreur_opaque(_exception_bavarde(), journal, "fetch orderbook")

    rendu = str(reponse)
    for fuite in ("10.0.3.14", "5432", "SELECT", "sk-live-abc", "Errno", "ConnectionError"):
        assert fuite not in rendu, f"« {fuite} » part chez le client : {rendu}"
    assert reponse["error"] == MESSAGE_GENERIQUE


def test_la_reponse_porte_une_reference_retrouvable_dans_le_journal():
    """L'autre moitié : sans elle, on a supprimé la fuite ET le diagnostic."""
    journal = _JournalEspion()
    reponse = erreur_opaque(_exception_bavarde(), journal, "fetch orderbook")

    ref = reponse["ref"]
    assert re.fullmatch(r"[0-9a-f]{8}", ref), ref
    assert any(f"ref={ref}" in l for l in journal.lignes), journal.lignes
    assert any("10.0.3.14" in l for l in journal.lignes), "la vraie cause doit rester journalisée"
    assert any("fetch orderbook" in l for l in journal.lignes), "l'operation doit etre nommee"


def test_deux_erreurs_ont_deux_references():
    journal = _JournalEspion()
    a = erreur_opaque(ValueError("a"), journal, "x")["ref"]
    b = erreur_opaque(ValueError("b"), journal, "x")["ref"]
    assert a != b


def test_le_message_ne_varie_pas_selon_la_cause():
    """Un message qui change selon l'erreur cartographie l'infrastructure tout seul."""
    journal = _JournalEspion()
    causes = [ConnectionError("db"), PermissionError("fichier"), TimeoutError("amont")]
    messages = {erreur_opaque(c, journal, "x")["error"] for c in causes}
    assert messages == {MESSAGE_GENERIQUE}


def test_les_valeurs_fournies_par_le_client_lui_sont_rendues():
    """Un `token_id` qu'il a envoyé ne lui apprend rien, et son absence casserait les appelants."""
    journal = _JournalEspion()
    reponse = erreur_opaque(ValueError("boom"), journal, "x", token_id="42", data_source="clob_error")
    assert reponse["token_id"] == "42"
    assert reponse["data_source"] == "clob_error"
