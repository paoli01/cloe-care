"""Mapping états techniques → labels publics. Source unique de vérité côté client.

Aucun jargon ne doit apparaître dans les valeurs. Cf. test_public_message.
"""

PUBLIC_LABELS: dict[str, str] = {
    "draft": "Brouillon",
    "received": "Reçu",
    "investigating": "Cloé analyse votre problème",
    "analyzed": "Cloé a identifié le problème",
    "fixing": "Correction en cours",
    "fix_applied": "Corrigé sur votre espace",
    "fix_rolled_back": "Correction annulée, équipe en charge",
    "escalated": "Transmis à notre équipe humaine",
    "proposing_global": "Corrigé, amélioration générale en préparation",
    "no_action": "Pas d'action nécessaire",
    "resolved": "Résolu",
    "rejected_review": "Non recevable",
    "awaiting_admin_review": "Vérification finale en cours",
    "refused_by_admin": "Examiné par notre équipe",
}

TERMINAL_STATES: frozenset[str] = frozenset(
    {
        "resolved",
        "escalated",
        "fix_rolled_back",
        "rejected_review",
        "no_action",
        "refused_by_admin",
    }
)

EMAIL_STATES: frozenset[str] = frozenset(
    {
        "resolved",
        "escalated",
        "fix_rolled_back",
        "rejected_review",
        "refused_by_admin",
    }
)


def label_for(status: str) -> str:
    return PUBLIC_LABELS.get(status, "En cours")


def is_terminal(status: str) -> bool:
    return status in TERMINAL_STATES


def should_email(status: str) -> bool:
    return status in EMAIL_STATES
