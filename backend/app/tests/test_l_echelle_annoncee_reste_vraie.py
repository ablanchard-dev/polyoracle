"""« About 33k lines of Python » : l'ordre de grandeur publié doit rester celui du dépôt.

Mesuré le 18/09/2026 : **35 628** lignes hors tests, **54 179** avec. Le README annonçait
« about 33k » sans dire ce qu'il comptait — un écart de 8 % dans un sens, de 64 % dans l'autre
selon la lecture.

Ce n'est pas un défaut, c'est une dérive : le chiffre était juste quand il a été écrit, il ne
l'est plus tout à fait, et personne ne le saura. Un dépôt vivant grossit ; « about 33k » finira
par annoncer la moitié de la réalité.

🔑 **Une tolérance, pas un chiffre figé.** Épingler le compte exact ferait rougir la suite à
chaque commit — une garde insupportable se fait retirer, et on perd tout. Le mot « about »
autorise une marge : ce test la rend explicite et la tient. Il tombe quand le chiffre publié
cesse d'être un ordre de grandeur honnête, pas quand le dépôt respire.
"""
from __future__ import annotations

import re
from pathlib import Path

RACINE = Path(__file__).resolve().parents[3]
BACKEND = RACINE / "backend"

# « about » : jusqu'à un quart d'écart reste un ordre de grandeur. Au-delà, le lecteur est
# trompé sur la taille de ce qu'il regarde.
MARGE = 0.25

IGNORES = {".venv", "site-packages", "__pycache__", "node_modules", ".git"}


def lignes_python(avec_tests: bool) -> int:
    total = 0
    for chemin in BACKEND.rglob("*.py"):
        if any(p in IGNORES for p in chemin.parts):
            continue
        if not avec_tests and chemin.name.startswith("test_"):
            continue
        total += chemin.read_text(encoding="utf-8-sig", errors="replace").count("\n") + 1
    return total


def test_le_README_dit_CE_QU_IL_compte():
    """Sans ça, le chiffre est invérifiable : 33k de quoi ?"""
    texte = (RACINE / "README.md").read_text(encoding="utf-8")
    trouve = re.search(r"([\d.]+)k lines of Python \(([^)]*)\)", texte)
    assert trouve, (
        "le README n'annonce plus une taille en lignes de Python avec ce qu'elle couvre — "
        "cette garde ne vise plus rien"
    )
    assert "test" in trouve.group(2).lower(), (
        "le README ne dit pas si les tests sont comptés : le même dépôt fait 35k ou 54k "
        f"selon la lecture (lu : « {trouve.group(0)} »)"
    )


def test_l_ordre_de_grandeur_annonce_reste_celui_du_depot():
    texte = (RACINE / "README.md").read_text(encoding="utf-8")
    trouve = re.search(r"([\d.]+)k lines of Python", texte)
    assert trouve, "le README n'annonce plus de taille"
    annonce = float(trouve.group(1)) * 1000

    reel = lignes_python(avec_tests="tests included" in texte.lower()
                         or "with tests" in texte.lower())
    assert reel > 1000, f"le compteur ne trouve presque rien ({reel}) : il ne garde plus rien"

    ecart = abs(reel - annonce) / reel
    assert ecart <= MARGE, (
        f"le README annonce {annonce:,.0f} lignes, le dépôt en contient {reel:,.0f} "
        f"({ecart:.0%} d'écart, marge {MARGE:.0%}). « About » n'est plus honnête."
    )
