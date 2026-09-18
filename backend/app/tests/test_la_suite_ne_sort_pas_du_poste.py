"""La preuve que le blocage reseau de `conftest.py` mord encore.

Dans un fichier COLLECTE, et c'est delibere : le meme controle ecrit dans `conftest.py` ne
tournerait jamais, et le compte de tests ne bougerait pas — c'est ainsi qu'une garde devient
decorative sans que personne ne s'en apercoive.
"""
import socket

import pytest


def test_le_blocage_reseau_MORD_vraiment():
    """🔴 Un bloqueur inoperant laisse passer exactement ce qu'il doit arreter, sans rien dire."""
    # On ne fait PAS `from conftest import ...` : selon la disposition du depot, le dossier du
    # conftest n'est pas sur `sys.path`, et le test echouerait sur son propre import.
    # Verifier le NOM ecarte aussi un faux positif : une panne DNS leve `gaierror`, pas ca.
    with pytest.raises(Exception) as leve:
        socket.create_connection(("example.com", 80), timeout=1)
    assert type(leve.value).__name__ == "SortieReseauInterdite", (
        f"la sortie a echoue pour une autre raison ({type(leve.value).__name__}) : "
        "le blocage ne mord peut-etre plus"
    )


def test_la_boucle_locale_reste_ouverte():
    """Le contrepoids : une garde insupportable finit par etre retiree."""
    serveur = socket.socket()
    serveur.bind(("127.0.0.1", 0))
    serveur.listen(1)
    try:
        with socket.create_connection(serveur.getsockname(), timeout=2):
            pass
    finally:
        serveur.close()
