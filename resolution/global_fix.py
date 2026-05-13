"""Création de GitHub Issue (code_transverse) ou PR (global fix proposal)."""
import json
import logging
import os
import uuid
from typing import Optional

import httpx

from db import get_db

logger = logging.getLogger("cloe-care.global_fix")


def _github_token() -> str:
    return os.getenv("GITHUB_TOKEN", "")


def _github_owner() -> str:
    return os.getenv("GITHUB_REPO_OWNER", "paoli01")


async def open_issue_for_code_transverse(
    ticket_id: str,
    analysis: dict,
) -> Optional[str]:
    """Crée une issue GitHub sur le repo qu'on devine depuis l'analyse."""
    token = _github_token()
    if not token:
        logger.info("github_token_missing — skip issue creation")
        return None

    repo = _guess_repo_from_analysis(analysis)
    title = f"[care #{ticket_id[:12]}] {(analysis.get('root_cause') or 'unknown')[:80]}"
    body = _format_issue_body(ticket_id, analysis)

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"https://api.github.com/repos/{_github_owner()}/{repo}/issues",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/vnd.github+json",
                },
                json={
                    "title": title,
                    "body": body,
                    "labels": ["auto-care", "investigation"],
                },
            )
    except httpx.HTTPError as e:
        logger.warning("github_issue_failed error=%s", type(e).__name__)
        return None

    if resp.status_code != 201:
        logger.warning("github_issue_status=%s body=%s", resp.status_code, resp.text[:300])
        return None

    issue_url = resp.json().get("html_url")

    conn = get_db()
    try:
        conn.execute(
            """INSERT INTO global_fix_proposals
                  (id, source_ticket_id, pattern_signature, target_repo,
                   proposed_change, github_issue_url, status)
               VALUES (?, ?, ?, ?, ?, ?, 'pending_review')""",
            (
                f"prop_{uuid.uuid4().hex[:12]}",
                ticket_id,
                (analysis.get("root_cause") or "")[:200],
                repo,
                json.dumps(analysis.get("fix_proposal", {})),
                issue_url,
            ),
        )
        conn.commit()
    finally:
        conn.close()

    return issue_url


async def open_pr_for_global_pattern(
    ticket_id: str,
    analysis: dict,
    fingerprint_val: str,
) -> Optional[str]:
    """MVP : issue labelisée `global-fix-proposal` (la vraie PR est une évolution).

    Voir 08_DEPLOYMENT §Évolutions identifiées.
    """
    token = _github_token()
    if not token:
        return None

    title = f"[care global-fix] {(analysis.get('root_cause') or 'unknown')[:80]}"
    body = (
        f"## Pattern récurrent détecté\n\n"
        f"Signature: `{fingerprint_val}`\n\n"
        f"Ticket source: `{ticket_id}`\n\n"
        f"## Diagnostic\n\n{analysis.get('root_cause', '')}\n\n"
        f"## Fix proposé (à valider et propager au template)\n\n"
        f"```json\n{json.dumps(analysis.get('fix_proposal', {}), indent=2)}\n```\n\n"
        f"## Implication\n\n{analysis.get('global_implication', 'unknown')}\n\n"
        f"---\n_Généré automatiquement par cloe-care. Review humain obligatoire avant merge._"
    )

    target_repo = "cloe"
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"https://api.github.com/repos/{_github_owner()}/{target_repo}/issues",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/vnd.github+json",
                },
                json={
                    "title": title,
                    "body": body,
                    "labels": ["auto-care", "global-fix-proposal", "needs-review"],
                },
            )
    except httpx.HTTPError as e:
        logger.warning("github_global_proposal_failed error=%s", type(e).__name__)
        return None

    if resp.status_code != 201:
        return None
    return resp.json().get("html_url")


_REPO_KEYWORDS: list[tuple[str, tuple[str, ...]]] = [
    ("cloe-flow", ("cloe-flow", "prefect", "workflow")),
    ("cloe-gui", ("cloe-gui", "frontend", "next.js")),
    ("cloe", ("cloe-proxy", "openrouter", "quota")),
]


def _guess_repo_from_analysis(analysis: dict) -> str:
    rc = (analysis.get("root_cause") or "").lower()
    evidence = " ".join(analysis.get("evidence") or []).lower()
    text = f"{rc} {evidence}"
    for repo, keywords in _REPO_KEYWORDS:
        if any(k in text for k in keywords):
            return repo
    return "cloe-api"


def _format_issue_body(ticket_id: str, analysis: dict) -> str:
    return (
        f"## Ticket source\n\n`{ticket_id}`\n\n"
        f"## Cause racine identifiée\n\n{analysis.get('root_cause', 'N/A')}\n\n"
        "## Evidence\n\n"
        + "\n".join(f"- {e}" for e in (analysis.get("evidence") or []))
        + f"\n\n## Catégorie\n\n`{analysis.get('category')}` "
        f"(confidence: {analysis.get('confidence', 0)})\n\n"
        f"## Fix proposé par le LLM\n\n```json\n"
        f"{json.dumps(analysis.get('fix_proposal', {}), indent=2)}\n```\n\n"
        f"---\n_Issue créée automatiquement par cloe-care. Investigation à valider._"
    )
