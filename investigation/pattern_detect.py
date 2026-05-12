"""Détection de patterns récurrents pour décider de proposer un fix global."""
import hashlib
import re

from db import get_db


def fingerprint(root_cause: str, category: str) -> str:
    """Normalise le root_cause + catégorie en signature stable.

    Le client_id et les dates sont remplacés par des placeholders pour que
    deux tickets touchant deux clients avec la même cause racine produisent
    la même signature.
    """
    normalized = re.sub(r"\bclient_[a-z0-9_-]+\b", "CLIENT", (root_cause or "").lower())
    normalized = re.sub(r"\b\d{4}-\d{2}-\d{2}\b", "DATE", normalized)
    normalized = re.sub(r"\s+", " ", normalized).strip()

    h = hashlib.sha256(f"{category}|{normalized}".encode()).hexdigest()[:16]
    return f"fp_{h}"


def record_pattern(ticket_id: str, fingerprint_val: str) -> int:
    """Incrémente le compteur d'occurrences et retourne la nouvelle valeur."""
    conn = get_db()
    try:
        existing = conn.execute(
            "SELECT id, occurrences FROM pattern_fingerprints WHERE fingerprint = ?",
            (fingerprint_val,),
        ).fetchone()

        if existing:
            new_count = existing["occurrences"] + 1
            conn.execute(
                """UPDATE pattern_fingerprints
                      SET occurrences = ?, last_seen_at = datetime('now')
                    WHERE id = ?""",
                (new_count, existing["id"]),
            )
            conn.commit()
            return new_count

        conn.execute(
            """INSERT INTO pattern_fingerprints
                  (fingerprint, sample_ticket_id, occurrences)
               VALUES (?, ?, 1)""",
            (fingerprint_val, ticket_id),
        )
        conn.commit()
        return 1
    finally:
        conn.close()


GLOBAL_FIX_THRESHOLD = 3


def should_propose_global_fix(occurrences: int, llm_implication: str) -> bool:
    """Décide s'il faut ouvrir une global fix proposal sur ce pattern."""
    if llm_implication == "likely_others_affected":
        return True
    if occurrences >= GLOBAL_FIX_THRESHOLD:
        return True
    return False
