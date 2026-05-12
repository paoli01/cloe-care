"""Routes principales : création de ticket, chat élicitation, soumission, listing."""
import json
import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from auth import (
    JWTPayload,
    validate_ticket_id,
    verify_jwt,
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
    validate_ticket_id(ticket_id)
    ticket = _load_ticket_or_404(ticket_id)
    verify_tenant(ticket["client_id"], payload)
    return {
        "ticket_id": ticket_id,
        "status": ticket["status"],
        "public_status_label": ticket.get("public_status_label"),
        "public_message": ticket.get("public_message"),
        "updated_at": ticket["updated_at"],
        "created_at": ticket["created_at"],
        "resolved_at": ticket.get("resolved_at"),
    }


@router.get("")
async def list_tickets(payload: JWTPayload = Depends(verify_jwt)):
    conn = get_db()
    try:
        rows = conn.execute(
            """SELECT id, status, public_status_label, public_message,
                      created_at, updated_at, resolved_at
                 FROM tickets
                WHERE client_id = ?
                ORDER BY created_at DESC
                LIMIT 50""",
            (payload.client_id,),
        ).fetchall()
        return {"tickets": [dict(r) for r in rows]}
    finally:
        conn.close()
