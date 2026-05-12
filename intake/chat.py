"""Chat d'élicitation et génération du résumé structuré."""
import asyncio
import json
import logging
import os
import re
import uuid
from typing import AsyncIterator

import httpx

from db import get_db
from intake.persona import CLOE_SUPPORT_SYSTEM_PROMPT, build_recap_request

logger = logging.getLogger("cloe-care.intake")

CARE_LLM_BASE_URL = os.getenv("CARE_LLM_BASE_URL", "https://openrouter.ai/api/v1")
OPERATOR_KEY = os.getenv("OPERATOR_OPENROUTER_KEY", "")
ELICITATION_MODEL = os.getenv("CARE_ELICITATION_MODEL", "anthropic/claude-3-haiku")
MAX_TURNS = int(os.getenv("CARE_MAX_INTAKE_TURNS", "10"))

FALLBACK_ASSISTANT_OPENING = (
    "Bonjour, je suis Cloé Support. Pouvez-vous me raconter ce qui s'est passé ?"
)

# Message d'accueil envoyé automatiquement à la création du ticket. Affiche
# au client le cadre qu'il doit remplir, sans appeler de LLM (zéro coût,
# zéro latence). Sert aussi de contexte pour Haiku au tour suivant et
# apparaît dans l'admin view.
WELCOME_MESSAGE = (
    "Bonjour, je suis Cloé Support. Pour bien comprendre votre souci, "
    "dites-moi en quelques mots :\n"
    "• Ce que vous essayiez de faire\n"
    "• Ce que vous attendiez comme résultat\n"
    "• Ce qui s'est passé à la place\n\n"
    "Plus c'est précis, plus je règle ça vite. Vous pouvez aussi joindre "
    "une capture ou un PDF en bas si ça aide."
)


def create_ticket(client_id: str) -> str:
    """Crée un ticket en draft, trace l'event, seed le message d'accueil."""
    ticket_id = f"ticket_{uuid.uuid4().hex[:12]}"
    conn = get_db()
    try:
        conn.execute(
            "INSERT INTO tickets (id, client_id, status) VALUES (?, ?, 'draft')",
            (ticket_id, client_id),
        )
        conn.execute(
            "INSERT INTO ticket_events (ticket_id, event_type, actor, payload) "
            "VALUES (?, 'created', 'client', ?)",
            (ticket_id, json.dumps({"client_id": client_id})),
        )
        conn.execute(
            "INSERT INTO chat_messages (ticket_id, role, content) VALUES (?, 'assistant', ?)",
            (ticket_id, WELCOME_MESSAGE),
        )
        conn.commit()
        return ticket_id
    finally:
        conn.close()


def append_message(ticket_id: str, role: str, content: str) -> None:
    conn = get_db()
    try:
        conn.execute(
            "INSERT INTO chat_messages (ticket_id, role, content) VALUES (?, ?, ?)",
            (ticket_id, role, content),
        )
        conn.commit()
    finally:
        conn.close()


def get_messages(ticket_id: str) -> list[dict]:
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT role, content FROM chat_messages WHERE ticket_id = ? ORDER BY id",
            (ticket_id,),
        ).fetchall()
        return [{"role": r["role"], "content": r["content"]} for r in rows]
    finally:
        conn.close()


def _extract_json(content: str) -> str:
    content = content.strip()
    if content.startswith("```"):
        match = re.search(r"```(?:json)?\s*(\{.*\})\s*```", content, flags=re.DOTALL)
        if match:
            return match.group(1)
    return content


async def call_llm(messages: list[dict]) -> dict:
    """Appel Haiku via cloe-proxy. Retourne le JSON parsé {message, elicitation_complete}."""
    payload_messages = [
        {"role": "system", "content": CLOE_SUPPORT_SYSTEM_PROMPT}
    ] + messages

    async with httpx.AsyncClient(timeout=45) as client:
        resp = await client.post(
            f"{CARE_LLM_BASE_URL}/chat/completions",
            headers={
                "Authorization": f"Bearer {OPERATOR_KEY}",
            },
            json={
                "model": ELICITATION_MODEL,
                "messages": payload_messages,
                "temperature": 0.3,
                "max_tokens": 500,
            },
        )

    resp.raise_for_status()
    raw = resp.json()["choices"][0]["message"]["content"]
    return json.loads(_extract_json(raw))


async def stream_assistant_reply(ticket_id: str, user_message: str) -> AsyncIterator[str]:
    """Yield des chunks SSE pour un tour de chat. Persiste les deux messages en BDD."""
    append_message(ticket_id, "user", user_message)
    messages = get_messages(ticket_id)

    # Émettre un keepalive SSE immédiat : le browser sait que la réponse a
    # démarré et n'avorte pas la connexion pendant le temps d'attente Haiku
    # (jusqu'à ~15s sur le hop bedrock).
    yield ": connected\n\n"

    if len(messages) > MAX_TURNS * 2:
        limit_msg = "On a déjà bien échangé, vous pouvez soumettre votre ticket dès maintenant."
        append_message(ticket_id, "assistant", limit_msg)
        yield "data: " + json.dumps({"type": "limit_reached", "message": limit_msg}) + "\n\n"
        yield "data: " + json.dumps({"type": "done", "elicitation_complete": True}) + "\n\n"
        return

    # Pendant l'appel LLM, on émet un keepalive toutes les 5s pour garder le
    # canal vivant (certains proxys et browsers coupent une connexion idle).
    llm_task = asyncio.create_task(call_llm(messages))
    while not llm_task.done():
        try:
            await asyncio.wait_for(asyncio.shield(llm_task), timeout=5)
        except asyncio.TimeoutError:
            yield ": keepalive\n\n"

    try:
        result = llm_task.result()
    except Exception:
        logger.exception("elicitation_llm_failed ticket=%s", ticket_id)
        fallback = "Petit souci de mon côté, pouvez-vous réessayer ?"
        append_message(ticket_id, "assistant", fallback)
        yield "data: " + json.dumps({"type": "error", "message": fallback}) + "\n\n"
        yield "data: " + json.dumps({"type": "done", "elicitation_complete": False}) + "\n\n"
        return

    assistant_msg = (result.get("message") or "").strip()
    if not assistant_msg:
        assistant_msg = FALLBACK_ASSISTANT_OPENING
    complete = bool(result.get("elicitation_complete", False))

    append_message(ticket_id, "assistant", assistant_msg)

    # Streaming token-by-token simulé pour respecter l'UX SSE
    for token in re.findall(r"\S+\s*", assistant_msg):
        yield "data: " + json.dumps({"type": "token", "content": token}) + "\n\n"
        await asyncio.sleep(0.02)

    yield "data: " + json.dumps({"type": "done", "elicitation_complete": complete}) + "\n\n"


async def build_user_summary(ticket_id: str) -> dict:
    """Génère le récapitulatif structuré à partir du chat complet."""
    messages = get_messages(ticket_id)
    if not messages:
        return {
            "what_user_did": None,
            "expected": None,
            "observed": None,
            "when": None,
            "additional_context": None,
        }

    prompt = build_recap_request(messages)

    try:
        async with httpx.AsyncClient(timeout=45) as client:
            resp = await client.post(
                f"{CARE_LLM_BASE_URL}/chat/completions",
                headers={
                    "Authorization": f"Bearer {OPERATOR_KEY}",
                },
                json={
                    "model": ELICITATION_MODEL,
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": 0,
                    "max_tokens": 600,
                },
            )
        resp.raise_for_status()
        return json.loads(_extract_json(resp.json()["choices"][0]["message"]["content"]))
    except Exception:
        logger.exception("recap_llm_failed ticket=%s", ticket_id)
        # Fallback minimal : on prend le dernier message user comme "observé"
        last_user = next(
            (m["content"] for m in reversed(messages) if m["role"] == "user"),
            None,
        )
        return {
            "what_user_did": None,
            "expected": None,
            "observed": last_user,
            "when": None,
            "additional_context": "recap_llm_failed",
        }
