"""GET /tickets/{id}/status (snapshot) et /status-stream (SSE)."""
import logging

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse

from auth import JWTPayload, validate_ticket_id, verify_jwt, verify_tenant
from db import get_db
from notification.stream import STATUS_STREAM

logger = logging.getLogger("cloe-care.status")

router = APIRouter(prefix="/tickets/{ticket_id}", tags=["status"])


def _load_and_verify(ticket_id: str, payload: JWTPayload) -> dict:
    validate_ticket_id(ticket_id)
    conn = get_db()
    try:
        row = conn.execute(
            """SELECT client_id, status, public_status_label, public_message,
                      created_at, updated_at, resolved_at
                 FROM tickets WHERE id = ?""",
            (ticket_id,),
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="ticket_not_found")
        verify_tenant(row["client_id"], payload)
        return dict(row)
    finally:
        conn.close()


@router.get("/status")
async def get_status(ticket_id: str, payload: JWTPayload = Depends(verify_jwt)):
    ticket = _load_and_verify(ticket_id, payload)
    return {
        "ticket_id": ticket_id,
        "status": ticket["status"],
        "public_status_label": ticket["public_status_label"],
        "public_message": ticket["public_message"],
        "updated_at": ticket["updated_at"],
        "resolved_at": ticket["resolved_at"],
    }


@router.get("/status-stream")
async def stream_status(ticket_id: str, payload: JWTPayload = Depends(verify_jwt)):
    _load_and_verify(ticket_id, payload)

    async def generate():
        async for chunk in STATUS_STREAM.stream(ticket_id):
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
