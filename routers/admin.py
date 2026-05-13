"""Routes admin : liste/détail/validation/refus des tickets en review."""
import asyncio
import json
import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from auth import (
    JWTPayload,
    _lookup_email_by_client_id,
    validate_ticket_id,
    verify_admin,
    verify_jwt,
)
from db import get_db
from investigation.pattern_detect import fingerprint
from notification.labels import PUBLIC_LABELS
from notification.transition import transition_async
from resolution.global_fix import open_issue_for_code_transverse
from workers.resolution_pipeline import execute_fix_after_admin_approval

logger = logging.getLogger("cloe-care.admin")

router = APIRouter(prefix="/admin", tags=["admin"])

VALID_STATUSES = set(PUBLIC_LABELS.keys())
VALID_CATEGORIES = {
    "config_client",
    "data_client",
    "code_transverse",
    "ux",
    "out_of_scope",
}


@router.get("/tickets")
async def list_tickets(
    status: Optional[str] = None,
    statuses: Optional[str] = Query(None, description="CSV de statuts"),
    category: Optional[str] = None,
    client_id: Optional[str] = None,
    awaiting_review_only: bool = False,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    admin: JWTPayload = Depends(verify_admin),
):
    where: list[str] = ["1=1"]
    params: list = []

    if awaiting_review_only:
        where.append("status = ?")
        params.append("awaiting_admin_review")
    elif statuses:
        wanted = [s.strip() for s in statuses.split(",") if s.strip() in VALID_STATUSES]
        if wanted:
            placeholders = ",".join("?" * len(wanted))
            where.append(f"status IN ({placeholders})")
            params.extend(wanted)
    elif status and status in VALID_STATUSES:
        where.append("status = ?")
        params.append(status)

    if category and category in VALID_CATEGORIES:
        where.append("category = ?")
        params.append(category)

    if client_id:
        where.append("client_id LIKE ?")
        params.append(f"%{client_id}%")

    if date_from:
        where.append("created_at >= ?")
        params.append(date_from)

    if date_to:
        where.append("created_at <= ?")
        params.append(date_to)

    where_sql = " AND ".join(where)
    conn = get_db()
    try:
        rows = conn.execute(
            f"""SELECT id, client_id, status, category, public_status_label,
                       public_message, user_summary, severity,
                       investigation_acu_cost, attachments_analyzed,
                       visibility, priority, source,
                       created_at, updated_at, resolved_at
                  FROM tickets
                 WHERE {where_sql}
                 ORDER BY
                   CASE status WHEN 'awaiting_admin_review' THEN 0 ELSE 1 END,
                   updated_at DESC
                 LIMIT ? OFFSET ?""",
            (*params, limit, offset),
        ).fetchall()
        total = conn.execute(
            f"SELECT COUNT(*) AS c FROM tickets WHERE {where_sql}",
            params,
        ).fetchone()["c"]
    finally:
        conn.close()

    tickets_out: list[dict] = []
    for r in rows:
        d = dict(r)
        d["topic"] = _admin_extract_topic(d.pop("user_summary", None))
        tickets_out.append(d)

    return {
        "total": total,
        "limit": limit,
        "offset": offset,
        "tickets": tickets_out,
    }


def _admin_extract_topic(user_summary_raw: Optional[str]) -> Optional[str]:
    """Snippet ~100 chars dérivé du récap utilisateur pour identifier le
    ticket dans la liste admin. Préfère ``observed`` (le symptôme), fallback
    sur ``context``, ``intent``, ``what_user_did``.
    """
    if not user_summary_raw:
        return None
    try:
        data = json.loads(user_summary_raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    for key in ("observed", "context", "intent", "what_user_did"):
        text = (data.get(key) or "").strip()
        if text:
            if len(text) <= 100:
                return text
            return text[:100].rsplit(" ", 1)[0] + "…"
    return None


@router.get("/tickets/{ticket_id}")
async def ticket_detail(
    ticket_id: str,
    admin: JWTPayload = Depends(verify_admin),
):
    validate_ticket_id(ticket_id)
    conn = get_db()
    try:
        ticket_row = conn.execute(
            "SELECT * FROM tickets WHERE id = ?", (ticket_id,)
        ).fetchone()
        if not ticket_row:
            raise HTTPException(status_code=404, detail="ticket_not_found")
        ticket = dict(ticket_row)

        chat = [
            dict(r)
            for r in conn.execute(
                "SELECT role, content, created_at FROM chat_messages "
                "WHERE ticket_id = ? ORDER BY id",
                (ticket_id,),
            ).fetchall()
        ]
        attachments = [
            dict(r)
            for r in conn.execute(
                """SELECT id, original_filename, mime_type, size_original,
                          size_compressed, page_count, analyzed_at, created_at,
                          thumbnail_path IS NOT NULL AS has_thumbnail,
                          extracted_text IS NOT NULL AS has_extracted_text
                     FROM attachments WHERE ticket_id = ?""",
                (ticket_id,),
            ).fetchall()
        ]
        events = [
            dict(r)
            for r in conn.execute(
                """SELECT event_type, actor, payload, created_at
                     FROM ticket_events WHERE ticket_id = ? ORDER BY id""",
                (ticket_id,),
            ).fetchall()
        ]
        decisions = [
            dict(r)
            for r in conn.execute(
                """SELECT admin_email, decision, comment, decided_at
                     FROM admin_decisions WHERE ticket_id = ? ORDER BY id""",
                (ticket_id,),
            ).fetchall()
        ]
        apply_audits = [
            dict(r)
            for r in conn.execute(
                """SELECT patch_type, target_path, response_status,
                          response_body, rolled_back, applied_at
                     FROM apply_patch_audit WHERE ticket_id = ? ORDER BY id""",
                (ticket_id,),
            ).fetchall()
        ]
    finally:
        conn.close()

    # Résout l'email depuis le registry cloe-api (RO mount). Best-effort —
    # si le registry est inaccessible, on renvoie None et le front affiche
    # juste le client_id.
    client_email = _lookup_email_by_client_id(ticket["client_id"])

    return {
        "ticket": ticket,
        "client_email": client_email,
        "user_summary": _safe_json(ticket["user_summary"]) or {},
        "investigation_report": _safe_json(ticket["investigation_report"]),
        "proposed_fix": _safe_json(ticket["proposed_fix"]),
        "triage_result": _safe_json(ticket["triage_result"]),
        "chat_messages": chat,
        "attachments": attachments,
        "events": events,
        "admin_decisions": decisions,
        "apply_audits": apply_audits,
    }


class AcceptFixIn(BaseModel):
    comment: Optional[str] = Field(None, max_length=2000)


@router.post("/tickets/{ticket_id}/accept-fix")
async def accept_fix(
    ticket_id: str,
    body: AcceptFixIn,
    admin: JWTPayload = Depends(verify_admin),
):
    validate_ticket_id(ticket_id)
    conn = get_db()
    try:
        ticket_row = conn.execute(
            "SELECT * FROM tickets WHERE id = ?", (ticket_id,)
        ).fetchone()
        if not ticket_row:
            raise HTTPException(status_code=404, detail="ticket_not_found")
        ticket = dict(ticket_row)

        if ticket["status"] != "awaiting_admin_review":
            raise HTTPException(
                status_code=400,
                detail=f"invalid_status:{ticket['status']}",
            )

        conn.execute(
            """INSERT INTO admin_decisions (ticket_id, admin_email, decision, comment)
               VALUES (?, ?, 'accept', ?)""",
            (ticket_id, admin.email, body.comment),
        )
        conn.commit()
    finally:
        conn.close()

    analysis = _safe_json(ticket["investigation_report"]) or {}
    fp = fingerprint(analysis.get("root_cause", ""), analysis.get("category", ""))

    conn = get_db()
    try:
        row = conn.execute(
            "SELECT occurrences FROM pattern_fingerprints WHERE fingerprint = ?",
            (fp,),
        ).fetchone()
        occurrences = row["occurrences"] if row else 1
    finally:
        conn.close()

    # Background task : ne pas faire attendre le HTTP. Le fix continue même
    # si l'admin ferme l'onglet, car asyncio.create_task survit.
    asyncio.create_task(
        execute_fix_after_admin_approval(
            ticket_id, ticket, analysis, fp, occurrences
        )
    )

    logger.info(
        "admin_accept_fix ticket=%s admin=%s",
        ticket_id,
        admin.email,
    )

    return {"ticket_id": ticket_id, "status": "fixing_scheduled"}


class RefuseFixIn(BaseModel):
    reason: str = Field(..., min_length=10, max_length=2000)
    escalate_to_github: bool = True


@router.post("/tickets/{ticket_id}/refuse-fix")
async def refuse_fix(
    ticket_id: str,
    body: RefuseFixIn,
    admin: JWTPayload = Depends(verify_admin),
):
    validate_ticket_id(ticket_id)
    conn = get_db()
    try:
        ticket_row = conn.execute(
            "SELECT * FROM tickets WHERE id = ?", (ticket_id,)
        ).fetchone()
        if not ticket_row:
            raise HTTPException(status_code=404, detail="ticket_not_found")
        ticket = dict(ticket_row)

        if ticket["status"] != "awaiting_admin_review":
            raise HTTPException(
                status_code=400,
                detail=f"invalid_status:{ticket['status']}",
            )

        conn.execute(
            """INSERT INTO admin_decisions (ticket_id, admin_email, decision, comment)
               VALUES (?, ?, 'refuse', ?)""",
            (ticket_id, admin.email, body.reason),
        )
        conn.execute(
            "UPDATE tickets SET admin_refusal_reason = ? WHERE id = ?",
            (body.reason, ticket_id),
        )
        conn.commit()
    finally:
        conn.close()

    await transition_async(
        ticket_id,
        "refused_by_admin",
        {"admin_email": admin.email, "reason": body.reason[:200]},
    )

    if body.escalate_to_github:
        analysis = _safe_json(ticket["investigation_report"]) or {}
        analysis["_admin_refusal_reason"] = body.reason
        await open_issue_for_code_transverse(ticket_id, analysis)

    await transition_async(ticket_id, "escalated", {"reason": "refused_by_admin"})

    logger.info(
        "admin_refuse_fix ticket=%s admin=%s",
        ticket_id,
        admin.email,
    )

    return {"ticket_id": ticket_id, "status": "refused"}


class AdminNoteRequest(BaseModel):
    content: str = Field(..., min_length=1, max_length=4000)


@router.post("/tickets/{ticket_id}/note")
async def add_admin_note(
    ticket_id: str,
    body: AdminNoteRequest,
    admin: JWTPayload = Depends(verify_admin),
):
    """Ajoute une note admin libre sur un ticket.

    Persistée dans ``ticket_events`` avec ``event_type='admin_note'``,
    actor=email admin. Visible dans l'audit trail de la fiche détail.
    """
    validate_ticket_id(ticket_id)
    conn = get_db()
    try:
        row = conn.execute("SELECT id FROM tickets WHERE id = ?", (ticket_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="ticket_not_found")
        conn.execute(
            "INSERT INTO ticket_events (ticket_id, event_type, actor, payload) "
            "VALUES (?, 'admin_note', ?, ?)",
            (ticket_id, admin.email or "admin", json.dumps({"note": body.content})),
        )
        conn.commit()
    finally:
        conn.close()

    logger.info("admin_note ticket=%s admin=%s", ticket_id, admin.email)
    return {"ok": True}


class AdminPublicMessageRequest(BaseModel):
    content: str = Field(..., min_length=1, max_length=2000)


@router.post("/tickets/{ticket_id}/public-message")
async def set_public_message(
    ticket_id: str,
    body: AdminPublicMessageRequest,
    admin: JWTPayload = Depends(verify_admin),
):
    """Met à jour le message public visible par le client sur sa fiche ticket.

    Différent de ``/note`` qui est interne. Écrit aussi un event
    ``public_message_updated`` dans l'audit trail avec le contenu, pour
    garder l'historique des communications client.
    """
    validate_ticket_id(ticket_id)
    conn = get_db()
    try:
        row = conn.execute("SELECT id FROM tickets WHERE id = ?", (ticket_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="ticket_not_found")
        conn.execute(
            "UPDATE tickets SET public_message = ?, updated_at = datetime('now') "
            "WHERE id = ?",
            (body.content, ticket_id),
        )
        conn.execute(
            "INSERT INTO ticket_events (ticket_id, event_type, actor, payload) "
            "VALUES (?, 'public_message_updated', ?, ?)",
            (ticket_id, admin.email or "admin", json.dumps({"message": body.content})),
        )
        conn.commit()
    finally:
        conn.close()

    logger.info("admin_public_message ticket=%s admin=%s", ticket_id, admin.email)
    return {"ok": True}


class AdminStatusRequest(BaseModel):
    status: str
    reason: Optional[str] = Field(None, max_length=500)


@router.post("/tickets/{ticket_id}/status")
async def admin_change_status(
    ticket_id: str,
    body: AdminStatusRequest,
    admin: JWTPayload = Depends(verify_admin),
):
    """Change le statut d'un ticket à n'importe quel statut valide.

    Utilise ``transition_async`` pour notifier (push, webhook) et tenir
    le public_message à jour. La raison optionnelle est jointe à l'event.
    """
    validate_ticket_id(ticket_id)
    if body.status not in VALID_STATUSES:
        raise HTTPException(status_code=400, detail=f"invalid_status: {body.status}")

    conn = get_db()
    try:
        row = conn.execute("SELECT id FROM tickets WHERE id = ?", (ticket_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="ticket_not_found")
    finally:
        conn.close()

    payload: dict = {"by_admin": admin.email}
    if body.reason:
        payload["reason"] = body.reason
    await transition_async(ticket_id, body.status, payload)

    logger.info(
        "admin_status_change ticket=%s status=%s admin=%s",
        ticket_id, body.status, admin.email,
    )
    return {"ok": True, "status": body.status}


@router.get("/stats")
async def admin_stats(admin: JWTPayload = Depends(verify_admin)):
    """Compteurs pour le dashboard admin."""
    conn = get_db()
    try:
        counts_by_status = {
            row["status"]: row["c"]
            for row in conn.execute(
                "SELECT status, COUNT(*) AS c FROM tickets GROUP BY status"
            ).fetchall()
        }
        counts_by_category = {
            (row["category"] or "unknown"): row["c"]
            for row in conn.execute(
                "SELECT category, COUNT(*) AS c FROM tickets "
                "WHERE category IS NOT NULL GROUP BY category"
            ).fetchall()
        }
        total_acu = conn.execute(
            "SELECT COALESCE(SUM(investigation_acu_cost), 0) AS s FROM tickets"
        ).fetchone()["s"]
    finally:
        conn.close()

    return {
        "counts_by_status": counts_by_status,
        "counts_by_category": counts_by_category,
        "total_acu_consumed": float(total_acu),
        "awaiting_admin_review": counts_by_status.get("awaiting_admin_review", 0),
    }


# ─── /me/admin-check (auth-only, pas verify_admin) ──────────────────────────

me_router = APIRouter(prefix="/me", tags=["me"])


@me_router.get("/admin-check")
async def admin_check(payload: JWTPayload = Depends(verify_jwt)):
    """Permet au frontend de conditionner l'affichage du lien admin."""
    from auth import ADMIN_EMAILS, _lookup_email_by_client_id

    email = _lookup_email_by_client_id(payload.sub)
    is_admin = bool(email and email.lower() in ADMIN_EMAILS)
    return {"is_admin": is_admin}


def _safe_json(raw):
    if not raw:
        return None
    if isinstance(raw, (dict, list)):
        return raw
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return None
