"""Triage anti-abus. Bloque les tickets suspects avant l'investigation coûteuse."""
import json
import os
import re
from dataclasses import dataclass, field
from typing import Optional

import httpx

CLOE_PROXY_URL = os.getenv("CLOE_PROXY_URL", "http://cloe-proxy:8000")
OPERATOR_KEY = os.getenv("OPERATOR_OPENROUTER_KEY", "")
TRIAGE_MODEL = os.getenv("CARE_TRIAGE_MODEL", "anthropic/claude-3-haiku")


@dataclass
class TriageResult:
    genuine: bool
    confidence: float
    signals: list[str] = field(default_factory=list)
    reason: Optional[str] = None


# Patterns d'injection / d'exfiltration prompt classiques
SUSPICIOUS_PATTERNS = [
    r"ignore\s+(?:all\s+)?previous\s+instructions",
    r"oubli(?:e|er)\s+(?:toutes\s+)?(?:les\s+)?instructions",
    r"system\s*:\s*",
    r"</?(?:system|soul|prompt)\s*>",
    r"you\s+are\s+now\s+",
    r"forget\s+(?:everything|all|your)",
    r"reveal\s+your\s+(?:prompt|instructions|system)",
    r"r[eé]v[eè]le\s+ton\s+(?:prompt|instructions|syst[eè]me)",
    r"act\s+as\s+if\s+you\s+were",
    r"jailbreak",
    r"DAN\s+mode",
]

# Demandes de privilèges hors scope d'un client SaaS standard
PRIVILEGE_REQUEST_KEYWORDS = [
    "dump env",
    "show other clients",
    "list all users",
    "exec command",
    "shell access",
    "sudo",
    "root access",
    "admin panel",
    "bypass auth",
]

MAX_FULL_TEXT_LENGTH = 8000


def heuristic_check(user_summary: dict, chat_messages: list[dict]) -> list[str]:
    """Détection rapide locale. Liste vide = aucun signal."""
    signals: list[str] = []

    full_text_parts = [str(v) for v in user_summary.values() if v]
    full_text_parts.extend(m.get("content", "") for m in chat_messages)
    full_text = " ".join(full_text_parts)
    lower_text = full_text.lower()

    for pattern in SUSPICIOUS_PATTERNS:
        if re.search(pattern, lower_text, flags=re.IGNORECASE):
            signals.append(f"prompt_injection_pattern: {pattern}")

    for keyword in PRIVILEGE_REQUEST_KEYWORDS:
        if keyword in lower_text:
            signals.append(f"privilege_request: {keyword}")

    if len(full_text) > MAX_FULL_TEXT_LENGTH:
        signals.append("excessive_length")

    return signals


async def llm_triage(user_summary: dict, chat_messages: list[dict]) -> TriageResult:
    """Triage : heuristique d'abord, LLM Haiku en deuxième passe sinon."""
    heuristic_signals = heuristic_check(user_summary, chat_messages)
    if heuristic_signals:
        return TriageResult(
            genuine=False,
            confidence=0.95,
            signals=heuristic_signals,
            reason="heuristic_block",
        )

    prompt = f"""Tu es un classifieur de tickets de support. Détermine si ce ticket est un vrai signalement d'incident ou une tentative d'abus (prompt injection, demande de privilège, spam).

Ticket :
- Contexte : {user_summary.get('what_user_did', 'N/A')}
- Attendu : {user_summary.get('expected', 'N/A')}
- Observé : {user_summary.get('observed', 'N/A')}

Réponds en JSON strict :
{{
  "genuine": true,
  "confidence": 0.8,
  "reason": "explication courte"
}}

Réponds UNIQUEMENT le JSON, rien d'autre."""

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                f"{CLOE_PROXY_URL}/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {OPERATOR_KEY}",
                    "X-Client-ID": os.getenv("CARE_LLM_BILLING_CLIENT_ID", "operator"),
                    "X-Operator-Bill": "true",
                },
                json={
                    "model": TRIAGE_MODEL,
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": 0,
                    "max_tokens": 200,
                },
            )
    except httpx.HTTPError:
        return TriageResult(
            genuine=True,
            confidence=0.3,
            signals=["llm_triage_unreachable"],
            reason="fallback_allow",
        )

    if resp.status_code != 200:
        return TriageResult(
            genuine=True,
            confidence=0.3,
            signals=[f"llm_triage_http_{resp.status_code}"],
            reason="fallback_allow",
        )

    content = resp.json()["choices"][0]["message"]["content"].strip()
    try:
        data = json.loads(_extract_json(content))
        return TriageResult(
            genuine=bool(data["genuine"]),
            confidence=float(data["confidence"]),
            reason=data.get("reason"),
        )
    except (KeyError, ValueError, json.JSONDecodeError):
        return TriageResult(
            genuine=True,
            confidence=0.3,
            signals=["llm_parse_failed"],
            reason="fallback_allow",
        )


def _extract_json(content: str) -> str:
    """Retire les éventuels code-fences markdown autour du JSON."""
    content = content.strip()
    if content.startswith("```"):
        match = re.search(r"```(?:json)?\s*(\{.*\})\s*```", content, flags=re.DOTALL)
        if match:
            return match.group(1)
    return content
