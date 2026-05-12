"""Génération du message public via Haiku. Sanitizer anti-jargon strict en sortie."""
import json
import logging
import os
import re
from typing import Optional

import httpx

logger = logging.getLogger("cloe-care.public_message")

CLOE_PROXY_URL = os.getenv("CLOE_PROXY_URL", "http://cloe-proxy:8000")
OPERATOR_KEY = os.getenv("OPERATOR_OPENROUTER_KEY", "")
PUBLIC_MSG_MODEL = os.getenv("CARE_PUBLIC_MSG_MODEL", "anthropic/claude-3-haiku")

FORBIDDEN_TERMS: tuple[str, ...] = (
    "container",
    "docker",
    "kubernetes",
    "k8s",
    "jwt",
    "token",
    "api key",
    "session id",
    "hermes",
    "prefect",
    "workflow",
    "stage 2",
    "stage 1",
    "llm",
    "openrouter",
    "anthropic",
    "stream",
    "sse",
    "websocket",
    "stdout",
    "stderr",
    "traceback",
    "stack trace",
    "exception",
    "null pointer",
    "segfault",
    "acu",
)


SYSTEM_PROMPT = """Tu écris des messages très courts (2 phrases max) pour informer un utilisateur final de l'état de son ticket de support.

Règles absolues :
- Français exclusivement.
- Vouvoiement par défaut.
- Jamais de jargon technique : ne dis JAMAIS "container", "Docker", "JWT", "session", "Hermes", "Prefect", "workflow", "stream", "API", "stack trace", "null pointer", "stage 2", "LLM", "ACU".
- Chaleureux et direct. Pas de "Cordialement", pas de "Bien à vous".
- Pas de promesse de délai sauf si l'état est terminal.
- Si c'est résolu, sois positif sans en faire trop.
- Si c'est escaladé, rassure sans minimiser.

Format : juste le message brut, sans guillemets, sans préambule. Maximum 2 phrases."""


_FORBIDDEN_PATTERN = re.compile(
    r"\b(?:" + "|".join(re.escape(t) for t in FORBIDDEN_TERMS) + r")\b",
    flags=re.IGNORECASE,
)


def _sanitize(text: str) -> str:
    """Filet de sécurité contre le jargon qui passerait quand même.

    On bloque entièrement si un terme interdit apparaît comme un mot entier
    (les substrings comme "sse" dans "passe" sont ignorés). Le caller
    utilise alors le fallback statique.
    """
    if not text:
        return ""
    if _FORBIDDEN_PATTERN.search(text):
        return ""
    return text.strip()


def _truncate_to_sentences(text: str, max_sentences: int) -> str:
    sentences = re.split(r"(?<=[.!?])\s+", text)
    return " ".join(sentences[:max_sentences]).strip()


_FALLBACK_MESSAGES: dict[str, str] = {
    "received": "C'est bien reçu, je m'occupe de votre demande.",
    "investigating": "Je regarde ce qui s'est passé.",
    "analyzed": "J'ai identifié l'origine du souci, je passe à l'action.",
    "fixing": "Je corrige ça pour vous.",
    "fix_applied": "C'est corrigé, vous pouvez réessayer.",
    "fix_rolled_back": "La correction n'a pas tenu, l'équipe prend le relais.",
    "escalated": "C'est transmis à l'équipe humaine, on revient vers vous au plus vite.",
    "proposing_global": "C'est corrigé chez vous, et une amélioration plus large est en préparation.",
    "no_action": "Tout fonctionne comme prévu, pas d'action nécessaire.",
    "resolved": "C'est résolu, merci de votre patience.",
    "rejected_review": "Je n'arrive pas à traiter cette demande, contactez-nous directement par email.",
    "awaiting_admin_review": "J'ai presque fini, je fais une dernière vérification avant d'agir.",
    "refused_by_admin": "Notre équipe a regardé votre demande de près. Elle revient vers vous directement.",
}


def _fallback_for(status: str) -> str:
    return _FALLBACK_MESSAGES.get(status, "Votre demande progresse.")


async def generate_public_message(
    new_status: str,
    label: str,
    user_summary: dict,
    analysis: Optional[dict] = None,
    extra_context: Optional[str] = None,
) -> str:
    """Génère le message public pour une transition. Fallback safe si LLM down."""
    context_parts = [
        f"État nouveau : {label}",
        f"Récit utilisateur : {str(user_summary.get('observed', 'N/A'))[:200]}",
    ]
    if analysis and analysis.get("category"):
        context_parts.append(f"Catégorie interne : {analysis['category']}")
    if extra_context:
        context_parts.append(extra_context[:200])

    user_prompt = (
        "Génère un court message pour informer l'utilisateur de cet état :\n\n"
        + "\n".join(context_parts)
    )

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                f"{CLOE_PROXY_URL}/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {OPERATOR_KEY}",
                    "X-Client-ID": os.getenv("CARE_LLM_BILLING_CLIENT_ID", "operator"),
                    "X-Operator-Bill": "true",
                },
                json={
                    "model": PUBLIC_MSG_MODEL,
                    "messages": [
                        {"role": "system", "content": SYSTEM_PROMPT},
                        {"role": "user", "content": user_prompt},
                    ],
                    "temperature": 0.3,
                    "max_tokens": 150,
                },
            )
        resp.raise_for_status()
        raw = resp.json()["choices"][0]["message"]["content"]
        clean = _sanitize(raw)
        if clean:
            return _truncate_to_sentences(clean, 2)
    except Exception:
        logger.exception("public_message_llm_failed status=%s", new_status)

    return _fallback_for(new_status)
