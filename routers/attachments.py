"""Upload, listing, miniature et suppression des pièces jointes."""
import json
import logging
import os
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import FileResponse

from auth import (
    JWTPayload,
    validate_attachment_id,
    validate_ticket_id,
    verify_jwt,
    verify_service_key,
    verify_tenant,
)
from db import get_db
from intake.attachments import (
    AttachmentTooLarge,
    AttachmentTypeNotAllowed,
    process_attachment,
)

logger = logging.getLogger("cloe-care.attachments")

router = APIRouter(prefix="/tickets/{ticket_id}/attachments", tags=["attachments"])


def _max_per_ticket() -> int:
    return int(os.getenv("MAX_ATTACHMENTS_PER_TICKET", "5"))


def _load_ticket_and_verify(ticket_id: str, payload: JWTPayload) -> dict:
    validate_ticket_id(ticket_id)
    conn = get_db()
    try:
        row = conn.execute("SELECT * FROM tickets WHERE id = ?", (ticket_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="ticket_not_found")
        verify_tenant(row["client_id"], payload)
        return dict(row)
    finally:
        conn.close()


def _count_attachments(ticket_id: str) -> int:
    conn = get_db()
    try:
        return conn.execute(
            "SELECT COUNT(*) FROM attachments WHERE ticket_id = ?", (ticket_id,)
        ).fetchone()[0]
    finally:
        conn.close()


@router.post("")
async def upload_attachment(
    ticket_id: str,
    file: UploadFile = File(...),
    payload: JWTPayload = Depends(verify_jwt),
):
    ticket = _load_ticket_and_verify(ticket_id, payload)

    if ticket["status"] not in ("draft", "received"):
        raise HTTPException(
            status_code=400,
            detail="cannot_attach_after_investigation_started",
        )

    max_per_ticket = _max_per_ticket()
    if _count_attachments(ticket_id) >= max_per_ticket:
        raise HTTPException(
            status_code=400,
            detail=f"max_{max_per_ticket}_attachments",
        )

    raw = await file.read()

    try:
        result = process_attachment(
            ticket_id=ticket_id,
            raw=raw,
            filename=file.filename or "unknown",
            mime_type=file.content_type or "application/octet-stream",
        )
    except AttachmentTooLarge:
        raise HTTPException(status_code=413, detail="file_too_large")
    except AttachmentTypeNotAllowed as e:
        raise HTTPException(status_code=415, detail=f"unsupported_type: {e}")
    except Exception as e:
        logger.exception("attachment_processing_failed ticket=%s", ticket_id)
        raise HTTPException(
            status_code=500,
            detail=f"processing_failed: {type(e).__name__}",
        )

    conn = get_db()
    try:
        conn.execute(
            """INSERT INTO attachments
                  (id, ticket_id, original_filename, mime_type,
                   size_original, size_compressed, storage_path,
                   thumbnail_path, content_hash, extracted_text, page_count)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                result.id,
                ticket_id,
                result.original_filename,
                result.mime_type,
                result.size_original,
                result.size_compressed,
                result.storage_path,
                result.thumbnail_path,
                result.content_hash,
                result.extracted_text,
                result.page_count,
            ),
        )
        conn.execute(
            "INSERT INTO ticket_events (ticket_id, event_type, actor, payload) "
            "VALUES (?, 'attachment_added', 'client', ?)",
            (
                ticket_id,
                json.dumps(
                    {"attachment_id": result.id, "mime": result.mime_type}
                ),
            ),
        )
        conn.commit()
    finally:
        conn.close()

    return {
        "attachment_id": result.id,
        "mime_type": result.mime_type,
        "size_original": result.size_original,
        "size_compressed": result.size_compressed,
        "has_extracted_text": bool(result.extracted_text),
        "page_count": result.page_count,
    }


@router.post("/internal")
async def upload_attachment_internal(
    ticket_id: str,
    file: UploadFile = File(...),
    _service: None = Depends(verify_service_key),
):
    """Variante service-to-service du POST /attachments.

    Appelée par cloe-api après création d'un ticket Cloé Aide pour
    forwarder les pièces jointes depuis la session de chat vers le
    ticket. On skip le check ``status in ('draft', 'received')`` car le
    ticket Cloé Aide démarre à ``received`` puis peut passer rapidement
    en ``escalated`` via l'investigation worker — on veut quand même
    pouvoir attacher les fichiers à temps.
    """
    validate_ticket_id(ticket_id)
    conn = get_db()
    try:
        row = conn.execute("SELECT id FROM tickets WHERE id = ?", (ticket_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="ticket_not_found")
    finally:
        conn.close()

    max_per_ticket = _max_per_ticket()
    if _count_attachments(ticket_id) >= max_per_ticket:
        raise HTTPException(
            status_code=400,
            detail=f"max_{max_per_ticket}_attachments",
        )

    raw = await file.read()
    try:
        result = process_attachment(
            ticket_id=ticket_id,
            raw=raw,
            filename=file.filename or "unknown",
            mime_type=file.content_type or "application/octet-stream",
        )
    except AttachmentTooLarge:
        raise HTTPException(status_code=413, detail="file_too_large")
    except AttachmentTypeNotAllowed as e:
        raise HTTPException(status_code=415, detail=f"unsupported_type: {e}")
    except Exception as e:
        logger.exception("attachment_processing_failed_internal ticket=%s", ticket_id)
        raise HTTPException(
            status_code=500,
            detail=f"processing_failed: {type(e).__name__}",
        )

    conn = get_db()
    try:
        conn.execute(
            """INSERT INTO attachments
                  (id, ticket_id, original_filename, mime_type,
                   size_original, size_compressed, storage_path,
                   thumbnail_path, content_hash, extracted_text, page_count)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                result.id, ticket_id, result.original_filename, result.mime_type,
                result.size_original, result.size_compressed, result.storage_path,
                result.thumbnail_path, result.content_hash,
                result.extracted_text, result.page_count,
            ),
        )
        conn.execute(
            "INSERT INTO ticket_events (ticket_id, event_type, actor, payload) "
            "VALUES (?, 'attachment_added', 'cloe_aide', ?)",
            (ticket_id, json.dumps({"attachment_id": result.id, "mime": result.mime_type})),
        )
        conn.commit()
    finally:
        conn.close()

    return {
        "attachment_id": result.id,
        "mime_type": result.mime_type,
        "size_original": result.size_original,
    }


@router.get("")
async def list_attachments(
    ticket_id: str,
    payload: JWTPayload = Depends(verify_jwt),
):
    _load_ticket_and_verify(ticket_id, payload)

    conn = get_db()
    try:
        rows = conn.execute(
            """SELECT id, original_filename, mime_type, size_original,
                      size_compressed, page_count, created_at,
                      thumbnail_path IS NOT NULL AS has_thumbnail
                 FROM attachments
                WHERE ticket_id = ?
                ORDER BY created_at""",
            (ticket_id,),
        ).fetchall()
        return {"attachments": [dict(r) for r in rows]}
    finally:
        conn.close()


@router.get("/{attachment_id}/thumbnail")
async def get_thumbnail(
    ticket_id: str,
    attachment_id: str,
    payload: JWTPayload = Depends(verify_jwt),
):
    _load_ticket_and_verify(ticket_id, payload)
    validate_attachment_id(attachment_id)

    conn = get_db()
    try:
        row = conn.execute(
            "SELECT thumbnail_path FROM attachments "
            "WHERE id = ? AND ticket_id = ?",
            (attachment_id, ticket_id),
        ).fetchone()
    finally:
        conn.close()

    if not row or not row["thumbnail_path"]:
        raise HTTPException(status_code=404, detail="no_thumbnail")

    path = Path(row["thumbnail_path"])
    if not path.exists():
        raise HTTPException(status_code=404, detail="thumbnail_missing_on_disk")

    return FileResponse(str(path), media_type="image/jpeg")


@router.get("/{attachment_id}")
async def get_attachment(
    ticket_id: str,
    attachment_id: str,
    payload: JWTPayload = Depends(verify_jwt),
):
    """Sert le fichier complet (image originale, PDF, etc.).

    Auth identique au thumbnail : JWT du tenant propriétaire OU admin via
    le tenant guard de ``_load_ticket_and_verify``. Le mime-type est
    renvoyé depuis la BDD pour que le navigateur ouvre/affiche
    correctement (inline pour les images, download pour les PDF selon
    Content-Disposition).
    """
    _load_ticket_and_verify(ticket_id, payload)
    validate_attachment_id(attachment_id)

    conn = get_db()
    try:
        row = conn.execute(
            "SELECT storage_path, mime_type, original_filename "
            "FROM attachments WHERE id = ? AND ticket_id = ?",
            (attachment_id, ticket_id),
        ).fetchone()
    finally:
        conn.close()

    if not row or not row["storage_path"]:
        raise HTTPException(status_code=404, detail="attachment_not_found")

    path = Path(row["storage_path"])
    if not path.exists():
        raise HTTPException(status_code=404, detail="file_missing_on_disk")

    mime = row["mime_type"] or "application/octet-stream"
    filename = row["original_filename"] or attachment_id
    # Inline pour les types affichables (image, PDF, texte) ; attachment
    # pour le reste afin de déclencher un téléchargement plutôt qu'une
    # tentative d'affichage qui finirait en charabia.
    inline_types = ("image/", "application/pdf", "text/")
    disposition = "inline" if any(mime.startswith(p) for p in inline_types) else "attachment"
    headers = {
        "Content-Disposition": f'{disposition}; filename="{filename}"',
    }
    return FileResponse(str(path), media_type=mime, headers=headers)


@router.delete("/{attachment_id}")
async def delete_attachment(
    ticket_id: str,
    attachment_id: str,
    payload: JWTPayload = Depends(verify_jwt),
):
    ticket = _load_ticket_and_verify(ticket_id, payload)
    validate_attachment_id(attachment_id)

    if ticket["status"] not in ("draft", "received"):
        raise HTTPException(status_code=400, detail="immutable_after_investigation")

    conn = get_db()
    try:
        row = conn.execute(
            "SELECT storage_path, thumbnail_path FROM attachments "
            "WHERE id = ? AND ticket_id = ?",
            (attachment_id, ticket_id),
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="not_found")

        for path_key in ("storage_path", "thumbnail_path"):
            p = row[path_key]
            if p and os.path.exists(p):
                try:
                    os.remove(p)
                except OSError:
                    logger.warning("attachment_file_remove_failed path=%s", p)

        conn.execute(
            "DELETE FROM attachments WHERE id = ? AND ticket_id = ?",
            (attachment_id, ticket_id),
        )
        conn.commit()
    finally:
        conn.close()

    return {"deleted": True}
