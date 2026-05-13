"""Helper paresseux pour charger les attachments pendant l'investigation.

Stage 1 voit uniquement les métadonnées (filename, mime, page_count). Stage 2
appelle `load_attachment_content` pour récupérer le contenu binaire ou le texte
extrait avant l'appel au modèle vision.
"""
import base64
from pathlib import Path
from typing import Optional

from db import get_db


def get_attachments_metadata(ticket_id: str) -> list[dict]:
    """Métadonnées seules (pas de contenu binaire ni de texte intégral)."""
    conn = get_db()
    try:
        rows = conn.execute(
            """SELECT id, original_filename, mime_type, size_compressed,
                      page_count, extracted_text IS NOT NULL AS has_text
                 FROM attachments
                WHERE ticket_id = ?""",
            (ticket_id,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def load_attachment_content(attachment_id: str) -> Optional[dict]:
    """Charge le contenu d'un attachment pour analyse LLM.

    Image → base64 + mime. PDF → texte extrait.
    """
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT mime_type, storage_path, extracted_text FROM attachments WHERE id = ?",
            (attachment_id,),
        ).fetchone()
        if not row:
            return None
    finally:
        conn.close()

    if row["mime_type"].startswith("image/"):
        path = Path(row["storage_path"])
        if not path.exists():
            return None
        b64 = base64.b64encode(path.read_bytes()).decode()
        return {"type": "image", "base64": b64, "mime": row["mime_type"]}

    if row["mime_type"] == "application/pdf":
        return {"type": "pdf_text", "text": row["extracted_text"] or ""}

    return None


def mark_analyzed(ticket_id: str) -> None:
    """Marque le ticket comme ayant déclenché le stage 2 vision (audit ACU)."""
    conn = get_db()
    try:
        conn.execute(
            "UPDATE tickets SET attachments_analyzed = 1 WHERE id = ?",
            (ticket_id,),
        )
        conn.execute(
            "UPDATE attachments SET analyzed_at = datetime('now') "
            "WHERE ticket_id = ? AND analyzed_at IS NULL",
            (ticket_id,),
        )
        conn.commit()
    finally:
        conn.close()
