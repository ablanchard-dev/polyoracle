"""Ce qu'une route rend quand quelque chose casse : assez pour diagnostiquer, rien pour aider.

Trouvé le 18/09/2026 en activant CodeQL sur les dépôts publics : `py/stack-trace-exposure`,
six fois sur ce dépôt. Les routes renvoyaient le message de l'exception tel quel —
``{"error": "ConnectionError: [Errno 111] Connection refused to 10.0.3.14:5432"}``. Un message
d'exception Python contient régulièrement un chemin de fichier, un hôte interne, un port, un
extrait de requête SQL, parfois un fragment de secret ; c'est une carte gratuite de
l'infrastructure pour qui la lit.

Le réflexe inverse — renvoyer ``{"error": "internal error"}`` et rien d'autre — casse le
diagnostic : plus personne ne sait relier le 500 d'un utilisateur à une ligne de journal. D'où la
**référence** : un identifiant court, rendu au client, écrit dans le journal à côté de la trace
complète. Le client ne tient qu'un numéro de ticket ; l'opérateur retrouve tout avec.
"""
from __future__ import annotations

import logging
import uuid
from typing import Any

# Le message unique. Il ne dit ni ce qui a échoué ni où : un message qui varie selon la cause
# est déjà une fuite (« database » / « upstream » suffit à cartographier).
MESSAGE_GENERIQUE = "internal error"


def erreur_opaque(
    exc: BaseException,
    journal: logging.Logger,
    contexte: str,
    **extras: Any,
) -> dict[str, Any]:
    """Journalise la vraie cause, rend au client une erreur opaque et sa référence.

    `contexte` dit à l'opérateur *quelle opération* a échoué — il va dans le journal, jamais
    dans la réponse. `extras` sont les champs que l'appelant doit conserver dans sa réponse
    (un `token_id`, une `address`) : ce sont des valeurs que le client a lui-même fournies,
    donc il ne les apprend pas.
    """
    reference = uuid.uuid4().hex[:8]
    journal.warning("%s [ref=%s]: %s", contexte, reference, exc, exc_info=True)
    return {**extras, "error": MESSAGE_GENERIQUE, "ref": reference}
