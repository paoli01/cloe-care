"""Routes principales : création de ticket, chat élicitation, soumission, listing."""
import json
import logging
import uuid
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from auth import (
    JWTPayload,
    validate_client_id,
    validate_ticket_id,
    verify_jwt,
    verify_service_key,
    verify_tenant,
)
from db import get_db
from intake.anti_abuse import llm_triage
from intake.chat import (
    build_user_summary,
    create_ticket,
    get_messages,
    stream_assistant_reply,
)

logger = logging.getLogger("cloe-care.tickets")

router = APIRouter(prefix="/tickets", tags=["tickets"])


class CreateTicketResponse(BaseModel):
    ticket_id: str
    status: str


class MessageIn(BaseModel):
    content: str = Field(..., min_length=1, max_length=4000)


class SubmitResponse(BaseModel):
    ticket_id: str
    status: str
    public_message: str


def _load_ticket_or_404(ticket_id: str) -> dict:
    conn = get_db()
    try:
        row = conn.execute("SELECT * FROM tickets WHERE id = ?", (ticket_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="ticket_not_found")
        return dict(row)
    finally:
        conn.close()


@router.post("", response_model=CreateTicketResponse)
async def create(payload: JWTPayload = Depends(verify_jwt)) -> CreateTicketResponse:
    ticket_id = create_ticket(payload.client_id)
    logger.info("ticket_created ticket_id=%s client_id=%s", ticket_id, payload.client_id)
    return CreateTicketResponse(ticket_id=ticket_id, status="draft")


@router.post("/{ticket_id}/messages")
async def post_message(
    ticket_id: str,
    body: MessageIn,
    payload: JWTPayload = Depends(verify_jwt),
):
    validate_ticket_id(ticket_id)
    ticket = _load_ticket_or_404(ticket_id)
    verify_tenant(ticket["client_id"], payload)

    if ticket["status"] != "draft":
        raise HTTPException(status_code=400, detail="ticket_not_in_draft")

    async def generate():
        async for chunk in stream_assistant_reply(ticket_id, body.content):
            yield chunk

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "X-Accel-Buffering": "no",
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
        },
    )


@router.get("/{ticket_id}/messages")
async def list_messages(
    ticket_id: str,
    payload: JWTPayload = Depends(verify_jwt),
):
    validate_ticket_id(ticket_id)
    ticket = _load_ticket_or_404(ticket_id)
    verify_tenant(ticket["client_id"], payload)
    return {"messages": get_messages(ticket_id)}


@router.post("/{ticket_id}/preview-summary")
async def preview_summary(
    ticket_id: str,
    payload: JWTPayload = Depends(verify_jwt),
):
    """Génère et cache le récap structuré 5 catégories pour la pop-up de
    confirmation. Si déjà généré, on retourne le cache (évite un 2e appel
    Haiku au moment du clic Soumettre).
    """
    validate_ticket_id(ticket_id)
    ticket = _load_ticket_or_404(ticket_id)
    verify_tenant(ticket["client_id"], payload)

    if ticket["status"] != "draft":
        raise HTTPException(status_code=400, detail="already_submitted")

    cached = ticket.get("user_summary")
    if cached:
        try:
            data = json.loads(cached)
            if all(k in data for k in ("context", "intent", "expected", "observed", "additional")):
                return {"summary": data, "cached": True}
        except (TypeError, ValueError):
            pass

    summary = await build_user_summary(ticket_id)
    conn = get_db()
    try:
        conn.execute(
            "UPDATE tickets SET user_summary = ?, updated_at = datetime('now') WHERE id = ?",
            (json.dumps(summary, ensure_ascii=False), ticket_id),
        )
        conn.commit()
    finally:
        conn.close()

    return {"summary": summary, "cached": False}


@router.post("/{ticket_id}/submit", response_model=SubmitResponse)
async def submit_ticket(
    ticket_id: str,
    payload: JWTPayload = Depends(verify_jwt),
):
    validate_ticket_id(ticket_id)
    ticket = _load_ticket_or_404(ticket_id)
    verify_tenant(ticket["client_id"], payload)

    if ticket["status"] != "draft":
        raise HTTPException(status_code=400, detail="already_submitted")

    # Réutilise le summary généré par /preview-summary si dispo, sinon le
    # génère maintenant. Évite le double appel Haiku.
    cached = ticket.get("user_summary")
    user_summary: dict | None = None
    if cached:
        try:
            user_summary = json.loads(cached)
            if not all(k in user_summary for k in ("context", "intent", "expected", "observed", "additional")):
                user_summary = None
        except (TypeError, ValueError):
            user_summary = None
    if user_summary is None:
        user_summary = await build_user_summary(ticket_id)
    chat_messages = get_messages(ticket_id)
    triage = await llm_triage(user_summary, chat_messages)

    new_status = "received" if triage.genuine else "rejected_review"
    public_message = (
        "C'est noté, je regarde ce qui s'est passé."
        if triage.genuine
        else "Désolée, je n'arrive pas à traiter cette demande. Contactez-nous directement par email."
    )

    conn = get_db()
    try:
        conn.execute(
            """UPDATE tickets
                  SET user_summary = ?,
                      triage_result = ?,
                      status = ?,
                      public_status_label = ?,
                      public_message = ?,
                      updated_at = datetime('now')
                WHERE id = ?""",
            (
                json.dumps(user_summary, ensure_ascii=False),
                json.dumps(
                    {
                        "genuine": triage.genuine,
                        "confidence": triage.confidence,
                        "signals": triage.signals,
                        "reason": triage.reason,
                    }
                ),
                new_status,
                "Reçu" if triage.genuine else "Non recevable",
                public_message,
                ticket_id,
            ),
        )
        conn.execute(
            "INSERT INTO ticket_events (ticket_id, event_type, actor, payload) "
            "VALUES (?, 'submitted', 'client', ?)",
            (
                ticket_id,
                json.dumps(
                    {
                        "genuine": triage.genuine,
                        "confidence": triage.confidence,
                        "signals": triage.signals,
                    }
                ),
            ),
        )
        conn.commit()
    finally:
        conn.close()

    if new_status == "received":
        _enqueue_for_investigation(ticket_id)

    logger.info(
        "ticket_submitted ticket_id=%s status=%s genuine=%s",
        ticket_id,
        new_status,
        triage.genuine,
    )

    return SubmitResponse(
        ticket_id=ticket_id,
        status=new_status,
        public_message=public_message,
    )


def _enqueue_for_investigation(ticket_id: str) -> None:
    """Hook vers le worker d'investigation (créé en 04). No-op si pas encore branché."""
    try:
        from workers.investigate_worker import enqueue_ticket  # noqa: WPS433

        enqueue_ticket(ticket_id)
    except (ImportError, AttributeError):
        # Worker pas encore disponible (avant 04_INVESTIGATION) — c'est normal.
        logger.debug("investigation_worker_not_available ticket_id=%s", ticket_id)


@router.get("/{ticket_id}")
async def get_ticket(
    ticket_id: str,
    payload: JWTPayload = Depends(verify_jwt),
):
    """Détails d'un ticket pour la page de suivi.

    Inclut le ``user_summary`` parsé (5 catégories validées par le client
    dans la modal de confirmation Cloé Aide) et ``source_session_id`` pour
    permettre au front de proposer un lien vers la conversation Cloé Aide
    d'origine (où vivent les pièces jointes et l'historique de chat).
    """
    validate_ticket_id(ticket_id)
    ticket = _load_ticket_or_404(ticket_id)
    verify_tenant(ticket["client_id"], payload)

    user_summary: Optional[dict] = None
    raw_summary = ticket.get("user_summary")
    if raw_summary:
        try:
            parsed = json.loads(raw_summary)
            if isinstance(parsed, dict):
                user_summary = parsed
        except (TypeError, ValueError):
            pass

    return {
        "ticket_id": ticket_id,
        "status": ticket["status"],
        "public_status_label": ticket.get("public_status_label"),
        "public_message": ticket.get("public_message"),
        "user_summary": user_summary,
        "category": ticket.get("category"),
        "priority": ticket.get("priority"),
        "source": ticket.get("source"),
        "source_session_id": ticket.get("chat_session_id"),
        "updated_at": ticket["updated_at"],
        "created_at": ticket["created_at"],
        "resolved_at": ticket.get("resolved_at"),
    }


@router.get("")
async def list_tickets(payload: JWTPayload = Depends(verify_jwt)):
    """Liste des tickets visibles côté client.

    Les tickets ``visibility='internal'`` (feedback produit créé en silence
    par Cloé Aide lors d'un refus) sont filtrés — invisibles par design.

    Inclut un snippet ``topic`` dérivé du ``user_summary`` (mots du client)
    pour que la carte ticket soit identifiable même quand le
    ``public_message`` est générique ("Transmis à notre équipe humaine").
    """
    conn = get_db()
    try:
        rows = conn.execute(
            """SELECT id, status, public_status_label, public_message,
                      user_summary, created_at, updated_at, resolved_at
                 FROM tickets
                WHERE client_id = ?
                  AND visibility = 'client'
                ORDER BY created_at DESC
                LIMIT 50""",
            (payload.client_id,),
        ).fetchall()
        out: list[dict] = []
        for r in rows:
            d = dict(r)
            d["topic"] = _extract_topic(d.pop("user_summary", None))
            out.append(d)
        return {"tickets": out}
    finally:
        conn.close()


def _extract_topic(user_summary_raw: Optional[str]) -> Optional[str]:
    """Snippet 80 chars dérivé du récap 5-catégories pour identifier un
    ticket dans la liste. Préfère ``observed`` (ce qui s'est passé), fall-
    back sur ``context`` (situation), puis ``intent``.
    """
    if not user_summary_raw:
        return None
    try:
        data = json.loads(user_summary_raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    for key in ("observed", "context", "intent"):
        text = (data.get(key) or "").strip()
        if text:
            if len(text) <= 80:
                return text
            return text[:80].rsplit(" ", 1)[0] + "…"
    return None


# ─── Création service-to-service (cloe-api → cloe-care) ──────────────────────


class InternalSummary(BaseModel):
    context: str = ""
    intent: str = ""
    expected: str = ""
    observed: str = ""
    additional: str = ""


class InternalIncidentRequest(BaseModel):
    """Payload émis par cloe-api quand l'Expert ``cloe_aide`` produit un
    marker ``[[create_incident: ...]]``."""
    client_id: str = Field(..., min_length=1, max_length=64)
    category: Literal[
        "technical_bug",
        "personalized_support",
        "config_beyond_self_service",
        "out_of_product_scope",
    ]
    visibility: Literal["client", "internal"] = "client"
    priority: Literal["ultra_low", "low", "normal", "high"] = "normal"
    summary: InternalSummary
    source_session_id: Optional[str] = Field(None, max_length=64)
    user_facing_message: Optional[str] = Field(None, max_length=500)


class InternalIncidentResponse(BaseModel):
    ticket_id: str
    status: str
    visibility: str
    priority: str


@router.post("/internal", response_model=InternalIncidentResponse)
async def create_internal_incident(
    body: InternalIncidentRequest,
    _service: None = Depends(verify_service_key),
):
    """Création directe d'un ticket par un service amont (cloe-api).

    Court-circuite le flow intake/draft : le récap 5-catégories arrive déjà
    construit par Cloé Aide via la conversation Hermes. On passe direct au
    statut final :
    - ``visibility='client'`` → status ``received`` (visible dans /support,
      le client voit la bannière dans le chat dashboard)
    - ``visibility='internal'`` → status ``internal_feedback`` (invisible
      côté client, feedback produit interne)
    """
    validate_client_id(body.client_id)

    ticket_id = f"ticket_{uuid.uuid4().hex[:12]}"
    status = "received" if body.visibility == "client" else "internal_feedback"
    summary_json = json.dumps(body.summary.model_dump(), ensure_ascii=False)
    public_status_label = "Reçu" if body.visibility == "client" else None
    public_message = (
        body.user_facing_message
        if body.visibility == "client" and body.user_facing_message
        else (
            "C'est noté, je regarde ce qui s'est passé."
            if body.visibility == "client"
            else None
        )
    )

    conn = get_db()
    try:
        conn.execute(
            """INSERT INTO tickets (
                    id, client_id, status, category, user_summary,
                    chat_session_id, visibility, priority, source,
                    public_status_label, public_message
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'cloe_aide', ?, ?)""",
            (
                ticket_id,
                body.client_id,
                status,
                body.category,
                summary_json,
                body.source_session_id,
                body.visibility,
                body.priority,
                public_status_label,
                public_message,
            ),
        )
        conn.execute(
            "INSERT INTO ticket_events (ticket_id, event_type, actor, payload) "
            "VALUES (?, 'created', 'cloe_aide', ?)",
            (
                ticket_id,
                json.dumps(
                    {
                        "category": body.category,
                        "visibility": body.visibility,
                        "priority": body.priority,
                        "source_session_id": body.source_session_id,
                    }
                ),
            ),
        )
        conn.commit()
    finally:
        conn.close()

    if body.visibility == "client":
        _enqueue_for_investigation(ticket_id)

    logger.info(
        "ticket_internal_created ticket_id=%s client_id=%s category=%s "
        "visibility=%s priority=%s",
        ticket_id, body.client_id, body.category, body.visibility, body.priority,
    )

    return InternalIncidentResponse(
        ticket_id=ticket_id,
        status=status,
        visibility=body.visibility,
        priority=body.priority,
    )
