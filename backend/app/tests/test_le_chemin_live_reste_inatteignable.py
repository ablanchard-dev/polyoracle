"""Le chemin live de Polymarket ne peut pas s'ouvrir par accident — et s'il s'ouvre, on le sait.

Contexte, mesuré le 18/09/2026. `test_p0_6_orderargs_semantics.py` porte **9 tests désactivés
en bloc**. Ils gardaient la classe de bug la plus chère du dépôt : `OrderArgs.size` compte des
**PARTS**, pas des dollars. Avant correction, un `size=5, price=0.10` plaçait **0,50 $ au lieu
de 5 $** — catastrophique au premier ordre réel.

Ces tests sont éteints parce que le refactor « live v2 » a remplacé le chemin qu'ils
interceptaient par `py_clob_client_v2.MarketOrderArgsV2` — **un SDK qui n'a jamais été livré**.

🔴 Et la raison affichée par `pytest -rs` disait *« guardrail preserved »*, trois lignes sous un
commentaire qui dit exactement l'inverse : *« live-v2 DROPPED this guardrail »*. Deux phrases
opposées dans le même fichier, et c'est la rassurante qui était visible. Corrigée.

Ce fichier-ci est la contrepartie active. Tant que le garde-fou est absent, la seule protection
réelle est que **le chemin live soit inatteignable**. C'est vrai aujourd'hui — et ce test le dit
tout haut au lieu de compter dessus en silence.

⚠️ Ce test n'est PAS un mur : il n'empêche personne d'installer le SDK. Il garantit qu'on ne
pourra pas le faire **sans que ce trou se rappelle à lui** — c'est-à-dire sans décider, en
connaissance de cause, de réactiver d'abord les 9 tests.
"""
from __future__ import annotations

import ast
import importlib.util
from pathlib import Path

RACINE = Path(__file__).resolve().parents[2]
EXECUTEUR = RACINE / "app" / "services" / "clob_executor.py"

#: Le SDK fantôme sur lequel repose le chemin live. Il n'existe pas.
SDK_FANTOME = "py_clob_client_v2"


def test_le_SDK_du_chemin_live_n_est_toujours_pas_installe():
    """S'il apparaît un jour, ce test tombe — et c'est le bon moment pour le dire.

    Le message nomme ce qu'il faut faire AVANT de s'en servir : rétablir les 9 tests de
    sémantique, sinon la protection contre le mauvais dimensionnement n'existe nulle part.
    """
    present = importlib.util.find_spec(SDK_FANTOME) is not None
    assert not present, (
        f"{SDK_FANTOME} est installe : le chemin live de Polymarket devient atteignable.\n"
        "AVANT de l'utiliser, reactiver `test_p0_6_orderargs_semantics.py` (9 tests) : il garde "
        "la seule verification que la taille d'un ordre compte des PARTS et non des dollars.\n"
        "Sans lui, un size=5 @ $0.10 envoie 0,50 $ au lieu de 5 $."
    )


def test_les_9_tests_de_semantique_sont_toujours_la_pas_supprimes():
    """Un garde-fou éteint qu'on supprime disparaît pour de bon.

    Ce fichier est, selon ses propres mots, « the standing record that live-v2 dropped this
    guardrail ». Le garder éteint est un choix ; le perdre serait un accident.
    """
    chemin = Path(__file__).with_name("test_p0_6_orderargs_semantics.py")
    assert chemin.exists(), "le garde-fou de dimensionnement a disparu du depot"

    arbre = ast.parse(chemin.read_text(encoding="utf-8-sig"), filename=str(chemin))
    tests = [n.name for n in ast.walk(arbre)
             if isinstance(n, ast.FunctionDef) and n.name.startswith("test_")]
    assert len(tests) >= 5, f"il ne reste que {len(tests)} test(s) de semantique : {tests}"


def test_la_raison_du_skip_ne_pretend_PAS_que_le_garde_fou_existe():
    """La phrase que `pytest -rs` imprime est la seule que quelqu'un lit.

    Elle disait « guardrail preserved » alors que le commentaire au-dessus dit « dropped ».
    Un lecteur pressé en concluait que la protection tenait encore.
    """
    texte = Path(__file__).with_name("test_p0_6_orderargs_semantics.py").read_text(
        encoding="utf-8-sig")
    debut = texte.index("pytestmark")
    raison = texte[debut:debut + 800].lower()
    assert "guardrail preserved" not in raison, (
        "la raison du skip affirme que le garde-fou est preserve ; il ne l'est pas, "
        "et c'est cette phrase que pytest imprime"
    )


def test_l_executeur_dit_toujours_qu_il_est_papier_seulement():
    """Le contrepoids documentaire : si cette phrase disparaît du code, la posture a changé."""
    source = EXECUTEUR.read_text(encoding="utf-8-sig")
    assert "paper-only" in source, (
        "`clob_executor.py` n'annonce plus la posture paper-only — verifier ce qui a change"
    )
