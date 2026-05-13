"""Pipeline LLM single-pass : stage 1 texte, stage 2 vision conditionnel.

Le coût est imputé sur le compte opérateur (X-Operator-Bill: true). Le client
n'est jamais facturé pour l'investigation de son propre ticket.
"""
import json
import logging
import os
import re
from typing import Optional

import httpx

from investigation.attachment_loader import (
    get_attachments_metadata,
    load_attachment_content,
    mark_analyzed,
)

logger = logging.getLogger("cloe-care.investigate")

CARE_LLM_BASE_URL = os.getenv("CARE_LLM_BASE_URL", "https://openrouter.ai/api/v1")
OPERATOR_KEY = os.getenv("OPERATOR_OPENROUTER_KEY", "")
INVESTIGATION_MODEL = os.getenv("CARE_INVESTIGATION_MODEL", "anthropic/claude-sonnet-4")
TIMEOUT_S = 60
MAX_OUTPUT_TOKENS = 4000


SYSTEM_PROMPT = """Tu es un ingénieur SRE qui analyse un ticket de support pour la plateforme Cloe (SaaS multi-tenant Python/FastAPI + Hermes agent dans des containers Docker par client).

Ton rôle : déterminer la cause racine du problème signalé en croisant le récit du client avec les éléments techniques fournis (logs container, config, session, événements ACU récents).

Tu classes le problème dans une catégorie et tu proposes éventuellement un correctif.

Catégories possibles :
- "config_client" : la config dans /opt/cloe/clients/{id}/ a un problème (config.yaml, client_overrides.json, SOUL.md). Fix possible automatiquement.
- "data_client" : une session est corrompue, un workflow est bloqué, le LoopDetector a faussement bloqué. Fix possible automatiquement.
- "code_transverse" : bug dans le code Python partagé. Pas de fix automatique — escalade humaine.
- "ux" : confusion utilisateur, fonctionnalité mal documentée. Pas de fix code nécessaire.
- "out_of_scope" : LLM provider down, problème réseau externe, problème navigateur. Pas d'action.

Si tu as un correctif config_client ou data_client à proposer, fournis-le sous forme structurée.

Pour décider si tu as besoin de voir les pièces jointes :
- Si le client mentionne une capture d'écran ou un document et que sans ça tu ne peux pas confirmer la cause racine → demande l'analyse stage 2.
- Si tu as déjà assez d'information avec les logs et le texte → ne demande pas le stage 2 (économie de coût).

Réponds UNIQUEMENT en JSON strict avec ce schéma :
{
  "root_cause": "description courte de la cause racine",
  "evidence": ["élément 1", "élément 2"],
  "category": "config_client",
  "confidence": 0.0,
  "needs_attachment_analysis": false,
  "attachment_reason": "pourquoi tu veux voir les attachments (si needs=true)",
  "fix_proposal": {
    "type": "yaml_merge",
    "target_path": "chemin relatif sous /opt/cloe/clients/{id}/ ou null",
    "new_content": "contenu yaml/json/text ou null",
    "rationale": "pourquoi ce fix"
  },
  "global_implication": "isolated"
}

Le champ `type` du fix_proposal doit valoir : yaml_merge | json_merge | file_replace | session_delete | workflow_cancel | loop_detector_reset | container_restart | none.
Le champ `global_implication` doit valoir : isolated | likely_others_affected | unknown.
"""


def _build_text_only_context(ticket: dict, gathered: dict, attachments_meta: list) -> str:
    user_summary = json.loads(ticket.get("user_summary") or "{}")

    payload = {
        "ticket_summary": user_summary,
        "client_plan": gathered.get("plan"),
        "subscription_status": gathered.get("subscription_status"),
        "container_logs_tail": (gathered.get("container_logs") or "")[:30000],
        "config_yaml": gathered.get("config_yaml"),
        "client_overrides_json": gathered.get("client_overrides_json"),
        "soul_md_extract": gathered.get("soul_md"),
        "failing_session": gathered.get("session"),
        "recent_acu_events": (gathered.get("recent_acu_events") or [])[:10],
        "attachments_available": [
            {
                "id": a["id"],
                "mime": a["mime_type"],
                "filename": a["original_filename"],
                "page_count": a["page_count"],
                "has_extracted_text": bool(a["has_text"]),
            }
            for a in attachments_meta
        ],
    }
    return json.dumps(payload, ensure_ascii=False)[:60000]


def _extract_json(content: str) -> str:
    content = content.strip()
    if content.startswith("```"):
        match = re.search(r"```(?:json)?\s*(\{.*\})\s*```", content, flags=re.DOTALL)
        if match:
            return match.group(1)
    return content


async def _call_llm(messages: list) -> dict:
    async with httpx.AsyncClient(timeout=TIMEOUT_S) as client:
        resp = await client.post(
            f"{CARE_LLM_BASE_URL}/chat/completions",
            headers={
                "Authorization": f"Bearer {OPERATOR_KEY}",
            },
            json={
                "model": INVESTIGATION_MODEL,
                "messages": messages,
                "temperature": 0,
                "max_tokens": MAX_OUTPUT_TOKENS,
            },
        )
    resp.raise_for_status()
    data = resp.json()
    content = data["choices"][0]["message"]["content"]
    usage = data.get("usage", {})
    return {
        "analysis": json.loads(_extract_json(content), strict=False),
        "usage": usage,
    }


async def run_stage1(ticket: dict, gathered: dict, ticket_id: str) -> dict:
    attachments_meta = get_attachments_metadata(ticket_id)
    user_content = _build_text_only_context(ticket, gathered, attachments_meta)

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_content},
    ]
    return await _call_llm(messages)


async def run_stage2(
    ticket: dict,
    gathered: dict,
    ticket_id: str,
    stage1_analysis: dict,
) -> dict:
    """Stage 2 : recharge le contexte + attachments. Déclenché si stage 1 le demande."""
    attachments_meta = get_attachments_metadata(ticket_id)
    user_text = _build_text_only_context(ticket, gathered, attachments_meta)

    multimodal_content: list = [{"type": "text", "text": user_text}]

    for att in attachments_meta:
        loaded = load_attachment_content(att["id"])
        if not loaded:
            continue
        if loaded["type"] == "image":
            multimodal_content.append(
                {
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:{loaded['mime']};base64,{loaded['base64']}"
                    },
                }
            )
        elif loaded["type"] == "pdf_text":
            multimodal_content.append(
                {
                    "type": "text",
                    "text": (
                        f"--- Contenu PDF {att['original_filename']} ---\n"
                        f"{(loaded['text'] or '')[:20000]}"
                    ),
                }
            )

    multimodal_content.append(
        {
            "type": "text",
            "text": (
                f"\nStage 1 analysis was:\n{json.dumps(stage1_analysis, ensure_ascii=False)}\n"
                "Maintenant que tu as les attachments, confirme ou affine ton diagnostic. "
                "Réponds avec le même schéma JSON strict, et mets "
                "needs_attachment_analysis=false."
            ),
        }
    )

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": multimodal_content},
    ]
    return await _call_llm(messages)


async def investigate(ticket: dict, gathered: dict, ticket_id: str) -> dict:
    """Pipeline complet stage 1 + éventuellement stage 2."""
    result1 = await run_stage1(ticket, gathered, ticket_id)
    analysis = result1["analysis"]
    usage_total = dict(result1.get("usage") or {})

    if analysis.get("needs_attachment_analysis") and get_attachments_metadata(ticket_id):
        try:
            result2 = await run_stage2(ticket, gathered, ticket_id, analysis)
            analysis = result2["analysis"]
            u2 = result2.get("usage") or {}
            for k in ("prompt_tokens", "completion_tokens", "total_tokens"):
                usage_total[k] = usage_total.get(k, 0) + u2.get(k, 0)
            mark_analyzed(ticket_id)
        except Exception:
            logger.exception("stage2_failed ticket_id=%s — keeping stage1 analysis", ticket_id)

    return {"analysis": analysis, "usage": usage_total}
