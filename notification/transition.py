"""Transition unifiée : update BDD + génère public_message + publish SSE + email."""
import asyncio
import json
import logging
from typing import Optional

from db import get_db
from notification.email import get_client_email, send_terminal_email
from notification.labels import is_terminal, label_for, should_email
from notification.public_message import generate_public_message
from notification.stream import STATUS_STREAM

logger = logging.getLogger("cloe-care.transition")


async def transition_async(
    ticket_id: str,
    status: str,
    extra: Optional[dict] = None,
) -> None:
    """Single source of truth pour faire transiter un ticket.

    Effets :
    1. Génère le `public_message` via Haiku (avec fallback).
    2. Met à jour `tickets` et trace `ticket_events`.
    3. Publie l'event sur le SSE `STATUS_STREAM`.
    4. Envoie un email transactionnel sur les états terminaux email-éligibles.
    """
    label = label_for(status)

    conn = get_db()
    try:
        row = conn.execute("SELECT * FROM tickets WHERE id = ?", (ticket_id,)).fetchone()
        if not row:
            logger.warning("transition_async_ticket_missing ticket=%s", ticket_id)
            return
        ticket = dict(row)
    finally:
        conn.close()

    user_summary = _safe_json(ticket.get("user_summary"))
    analysis = _safe_json(ticket.get("investigation_report"))

    extra_context = None
    if extra and isinstance(extra, dict):
        if extra.get("reason"):
            extra_context = f"raison interne : {extra['reason']}"
        elif extra.get("detail"):
            extra_context = str(extra["detail"])[:200]

    public_message = await generate_public_message(
        new_status=status,
        label=label,
        user_summary=user_summary or {},
        analysis=analysis,
        extra_context=extra_context,
    )

    conn = get_db()
    try:
        conn.execute(
            """UPDATE tickets
                  SET status = ?,
                      public_status_label = ?,
                      public_message = ?,
                      updated_at = datetime('now'),
                      resolved_at = CASE
                          WHEN ? IN ('resolved', 'no_action') THEN datetime('now')
                          ELSE resolved_at
                      END
                WHERE id = ?""",
            (status, label, public_message, status, ticket_id),
        )
        conn.execute(
            "INSERT INTO ticket_events (ticket_id, event_type, actor, payload) "
            "VALUES (?, ?, 'system', ?)",
            (ticket_id, f"transition_{status}", json.dumps(extra or {})),
        )
        conn.commit()
    finally:
        conn.close()

    await STATUS_STREAM.publish(
        ticket_id,
        {
            "type": "status",
            "status": status,
            "public_status_label": label,
            "public_message": public_message,
            "is_terminal": is_terminal(status),
        },
    )

    if should_email(status):
        client_email = get_client_email(ticket["client_id"])
        if client_email:
            # Fire-and-forget : un échec d'envoi ne doit pas casser la transition
            asyncio.create_task(
                send_terminal_email(
                    ticket_id, client_email, status, label, public_message
                )
            )

    logger.info("transition ticket=%s status=%s", ticket_id, status)


def _safe_json(raw):
    if not raw:
        return None
    if isinstance(raw, (dict, list)):
        return raw
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return None
