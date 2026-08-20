"""Les verrous du mode LIVE, verifies en les EXECUTANT (15/08).

Le README affirme « Live trading is locked by default -- the BotLoop refuses LIVE ».
Mesure : c'est vrai, et mieux que ca -- la protection ne repose PAS sur la
configuration. Trois faits etablis, dans l'ordre ou ils comptent :

1. `BotLoop.set_mode("LIVE")` leve sans condition.
2. `ExecutionEngine.switch_mode("LIVE")`, lui, N'EST gardee que par trois reglages
   (`live_enabled`, juridiction configuree, juridiction autorisee). Avec deux
   variables d'environnement, l'ETIQUETTE du mode passe a LIVE. Asymetrie reelle,
   que le README ne mentionne pas.
3. Mais aucun ordre ne peut partir : `submit_order_live` finit par un `raise`
   INCONDITIONNEL qu'aucun reglage n'ouvre. C'est ce verrou-la qui protege.

Ces tests existent pour que le point 3 ne disparaisse jamais en silence.
"""
import pytest
from sqlmodel import Session, SQLModel, create_engine

from app.services.execution_engine import ExecutionEngine


def _session():
    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    return Session(engine)


def test_par_defaut_la_porte_du_mode_est_fermee():
    with _session() as session:
        with pytest.raises(PermissionError) as exc:
            ExecutionEngine(session).switch_mode("LIVE")
    assert "LIVE_ENABLED" in str(exc.value)


def test_un_ordre_live_exige_une_confirmation_humaine():
    with _session() as session:
        with pytest.raises(PermissionError) as exc:
            ExecutionEngine(session).submit_order_live({"x": 1})
    assert "confirmation" in str(exc.value).lower()


def test_le_dernier_verrou_ne_depend_D_AUCUN_reglage():
    """LE test qui compte : meme confirme et meme conforme, l'envoi leve.

    On lit la source plutot que de dependre de la config : le `raise` final doit
    rester INCONDITIONNEL. Un jour ou quelqu'un le met derriere un `if`, ce test
    tombe -- et c'est exactement le rappel qu'il faut a ce moment-la."""
    import inspect

    src = inspect.getsource(ExecutionEngine.submit_order_live)
    lignes = [l.strip() for l in src.splitlines() if l.strip()]
    derniere = lignes[-1]
    assert derniere.startswith("raise PermissionError"), (
        f"la derniere instruction de submit_order_live n'est plus un refus sec : {derniere!r}")
    assert "intentionally disabled" in derniere


def test_les_deux_portes_du_mode_ont_des_serrures_differentes():
    """Acte l'asymetrie mesuree, pour qu'elle soit un choix et non une surprise."""
    import inspect

    from app.services.bot_loop import BotLoop
    from app.services.compliance_config import ComplianceConfig

    botloop = inspect.getsource(BotLoop.set_mode)
    assert 'raise PermissionError("LIVE mode is intentionally disabled' in botloop, \
        "le BotLoop ne refuse plus LIVE sans condition -- le README l'affirme pourtant"

    serrure = inspect.getsource(ComplianceConfig.live_blocked_reason)
    assert "block_if_missing_credentials" not in serrure, (
        "la verification des identifiants est entree dans la serrure : les deux portes "
        "ne sont plus asymetriques, mettre a jour ce test et le README")
