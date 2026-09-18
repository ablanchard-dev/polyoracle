"""Une route peut rendre l'exception au client — mais seulement si elle sait laquelle.

Trouvé le 18/09/2026 en balayant le dépôt plutôt qu'en relisant les six sites corrigés.
`erreur_opaque` est prouvé par cinq tests, et **rien ne balayait le reste** : un septième site,
ou une route écrite demain, fuiterait en silence.

Le balayage a rendu quatre `HTTPException(detail=str(exc))` dans les routes. Aucune n'est une
fuite, et c'est important de le dire : elles attrapent un type ÉTROIT —

    except PermissionError as exc:   # levé par le verrou du produit
        raise HTTPException(403, detail=str(exc))
    except ValueError as exc:        # levé par la validation de `set_active_mode`
        raise HTTPException(400, detail=str(exc))

— dont le message est écrit par le produit lui-même, pour l'utilisateur. Le rendre est le bon
comportement : c'est ce qui dit « ce mode n'existe pas » plutôt que « internal error ».

🔴 **Mais rien ne tient cette distinction.** Élargir `except PermissionError` en
`except Exception` transforme exactement la même ligne en `py/stack-trace-exposure` — un message
d'exception Python porte régulièrement un chemin, un hôte interne, un port, un fragment de
requête SQL. Aucun test ne bronchait.

La règle que ce fichier tient, et qui est celle que le code applique déjà correctement :

    un gestionnaire LARGE (`except Exception`, `except BaseException`, `except:`) n'a pas le
    droit de faire parvenir l'exception au client. Il passe par `erreur_opaque`, qui journalise
    la trace et ne rend qu'une référence.

    un gestionnaire ÉTROIT garde ce droit : il sait ce qu'il a attrapé.

C'est une règle de CAPACITÉ, pas d'orthographe : elle ne cherche pas `str(exc)`, elle demande
d'où vient l'exception qu'on rend. Et elle tombe le jour où quelqu'un élargit un `except`.
"""
from __future__ import annotations

import ast
import pathlib

import pytest

ROUTES = pathlib.Path(__file__).resolve().parents[1] / "api" / "routes"

# Ce qui part vers le client.
SORTIES_CLIENT = {"HTTPException", "JSONResponse"}

# La porte de sortie honnête : si l'exception y entre, elle n'en ressort pas.
PASSAGE_SUR = "erreur_opaque"

TYPES_LARGES = {"Exception", "BaseException"}


def _types_attrapes(handler: ast.ExceptHandler) -> set[str]:
    """Les noms de types attrapés. Vide = `except:` nu, le plus large de tous."""
    t = handler.type
    if t is None:
        return set()
    noeuds = t.elts if isinstance(t, ast.Tuple) else [t]
    return {getattr(n, "id", None) or getattr(n, "attr", "") for n in noeuds}


def _est_large(handler: ast.ExceptHandler) -> bool:
    attrapes = _types_attrapes(handler)
    return not attrapes or bool(attrapes & TYPES_LARGES)


def _cite(noeud: ast.AST, nom: str) -> bool:
    return any(isinstance(x, ast.Name) and x.id == nom for x in ast.walk(noeud))


def _passe_par_la_porte_sure(noeud: ast.AST, nom: str) -> bool:
    """L'exception n'apparaît que comme argument d'`erreur_opaque` ?"""
    for appel in ast.walk(noeud):
        if isinstance(appel, ast.Call):
            cible = getattr(appel.func, "id", None) or getattr(appel.func, "attr", None)
            if cible == PASSAGE_SUR and _cite(appel, nom):
                return True
    return False


def fuites(source: str, nom_fichier: str = "<test>") -> list[str]:
    """Les endroits où un gestionnaire LARGE fait parvenir son exception au client."""
    trouvees: list[str] = []
    arbre = ast.parse(source, filename=nom_fichier)
    for handler in (n for n in ast.walk(arbre) if isinstance(n, ast.ExceptHandler)):
        if not handler.name or not _est_large(handler):
            continue
        for n in ast.walk(handler):
            rendu = None
            if isinstance(n, ast.Return) and n.value is not None:
                rendu = n.value
            elif isinstance(n, ast.Call):
                cible = getattr(n.func, "id", None) or getattr(n.func, "attr", None)
                if cible in SORTIES_CLIENT:
                    rendu = n
            if rendu is None or not _cite(rendu, handler.name):
                continue
            if _passe_par_la_porte_sure(rendu, handler.name):
                continue
            trouvees.append(f"{nom_fichier}:{n.lineno} {ast.unparse(rendu)[:90]}")
    return trouvees


def test_aucune_route_large_ne_rend_son_exception_au_client():
    coupables: list[str] = []
    for chemin in sorted(ROUTES.rglob("*.py")):
        if "__pycache__" in chemin.parts:
            continue
        coupables += fuites(chemin.read_text(encoding="utf-8-sig"), chemin.name)
    assert not coupables, (
        "un `except Exception` fait parvenir l'exception au client — un message Python porte "
        "un chemin, un hote interne, un port, parfois un fragment de requete :\n  "
        + "\n  ".join(coupables)
    )


# ── Le detecteur prouve sa couverture, et sa retenue. ────────────────────────────────────────
# Sans le premier bloc, un detecteur devenu aveugle passerait au vert en ne comparant rien.
# Sans le second, on ecrirait une garde qui refuse tout — et qui se ferait retirer au premier
# message de validation casse.

FUITES_A_VOIR = [
    ("except Exception as e:\n    raise HTTPException(500, detail=str(e))", "detail=str(e)"),
    ("except Exception as e:\n    return {'error': str(e)}", "dict rendu"),
    ("except Exception as e:\n    return JSONResponse({'e': repr(e)}, 500)", "JSONResponse"),
    ("except BaseException as e:\n    raise HTTPException(500, detail=f'{e}')", "f-string"),
    ("except:\n    pass", "except nu sans nom — hors portee, doit rester silencieux"),
    ("except (ValueError, Exception) as e:\n    return {'x': str(e)}", "tuple qui contient large"),
    ("except Exception as e:\n    return {'a': 1, 'b': e.args}", "e.args"),
]


@pytest.mark.parametrize("code,quoi", [(c, q) for c, q in FUITES_A_VOIR if "hors portee" not in q])
def test_le_detecteur_voit_chaque_fuite_large(code, quoi):
    enveloppe = f"def r():\n  try:\n    pass\n  {code.replace(chr(10), chr(10) + '  ')}\n"
    assert fuites(enveloppe), f"aveugle sur : {quoi}"


@pytest.mark.parametrize(
    "code,pourquoi",
    [
        ("except ValueError as e:\n    raise HTTPException(400, detail=str(e))",
         "type etroit : le message vient de la validation du produit"),
        ("except PermissionError as e:\n    raise HTTPException(403, detail=str(e))",
         "type etroit : le message vient du verrou du produit"),
        ("except Exception as e:\n    return erreur_opaque(e, logger, 'ctx')",
         "la porte sure : journalise la trace, ne rend qu'une reference"),
        ("except Exception as e:\n    logger.warning('%s', e)\n    raise HTTPException(500, detail='internal error')",
         "journalisee, jamais rendue"),
    ],
)
def test_le_detecteur_n_accuse_pas_ce_qui_est_correct(code, pourquoi):
    enveloppe = f"def r():\n  try:\n    pass\n  {code.replace(chr(10), chr(10) + '  ')}\n"
    assert not fuites(enveloppe), f"faux positif ({pourquoi})"
