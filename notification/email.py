"""Email transactionnel via Resend. Un seul email par ticket (état terminal)."""
import json
import logging
import os
from typing import Optional

import httpx

from db import get_db

logger = logging.getLogger("cloe-care.email")

RESEND_API_KEY = os.getenv("RESEND_API_KEY", "")
EMAIL_FROM = os.getenv("EMAIL_FROM", "cloe@hellocloe.fr")
EMAIL_REPLY_TO = os.getenv("EMAIL_REPLY_TO", "care@hellocloe.fr")

# Couleurs par état pour personnaliser légèrement le hero
_STATE_COLOR: dict[str, str] = {
    "resolved": "#00C9A7",
    "fix_rolled_back": "#FF9F0A",
    "escalated": "#0072FF",
    "rejected_review": "#8E8E93",
    "refused_by_admin": "#8E8E93",
}


def _render_email_html(ticket_id: str, label: str, message: str, status: str) -> str:
    cta_url = f"https://app.hellocloe.fr/support/{ticket_id}"
    color = _STATE_COLOR.get(status, "#0072FF")

    return (
        '<!DOCTYPE html>'
        '<html><body style="font-family: -apple-system, BlinkMacSystemFont, sans-serif; '
        'background:#070C18; color:#fff; padding:20px;">'
        '<div style="max-width:560px; margin:auto; background:#0F1729; '
        'border-radius:16px; padding:32px;">'
        f'<div style="font-size:14px; color:#8E8E93; margin-bottom:8px;">Cloé Support</div>'
        f'<h1 style="font-size:20px; margin:0 0 16px; color:{color};">{label}</h1>'
        f'<p style="font-size:16px; line-height:1.5; margin:0 0 24px;">{message}</p>'
        f'<a href="{cta_url}" style="display:inline-block; padding:12px 24px; '
        'background:linear-gradient(135deg, #00C9A7, #0072FF); color:#fff; '
        'text-decoration:none; border-radius:8px; font-weight:600;">Voir mon ticket</a>'
        f'<p style="font-size:12px; color:#8E8E93; margin-top:32px;">Référence : {ticket_id}</p>'
        '</div></body></html>'
    )


async def send_terminal_email(
    ticket_id: str,
    client_email: str,
    status: str,
    label: str,
    public_message: str,
) -> bool:
    if not RESEND_API_KEY or not client_email:
        logger.info("send_terminal_email_skipped api_key_or_email_missing")
        return False

    subject = f"[Cloe] Ticket {ticket_id[:12]} — {label}"
    html_body = _render_email_html(ticket_id, label, public_message, status)

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                "https://api.resend.com/emails",
                headers={"Authorization": f"Bearer {RESEND_API_KEY}"},
                json={
                    "from": EMAIL_FROM,
                    "to": [client_email],
                    "reply_to": EMAIL_REPLY_TO,
                    "subject": subject,
                    "html": html_body,
                },
            )
        success = resp.status_code in (200, 202)
    except httpx.HTTPError as e:
        logger.warning("resend_http_error error=%s", type(e).__name__)
        success = False

    conn = get_db()
    try:
        conn.execute(
            """INSERT INTO notifications (ticket_id, channel, state, payload, sent_at)
               VALUES (?, 'email', ?, ?, datetime('now'))""",
            (
                ticket_id,
                "sent" if success else "failed",
                json.dumps({"to": client_email, "subject": subject}),
            ),
        )
        conn.commit()
    finally:
        conn.close()

    return success


def get_client_email(client_id: str) -> Optional[str]:
    """Lit l'email depuis registry.json (RO mount cloe-api)."""
    path = os.getenv("REGISTRY_PATH", "/data/cloe-api/registry.json")
    try:
        with open(path) as f:
            registry = json.load(f)
        entry = registry.get(client_id, {})
        email = entry.get("email")
        return email.lower() if email else None
    except (OSError, json.JSONDecodeError):
        return None
