"""Étape résolution post-investigation : guard → apply → audit → escalade.

Importé via `_maybe_handle_resolution` dans `investigate_worker`. Le découplage
permet à `feature/investigation` de fonctionner sans ce module ; quand
`feature/apply-fix` est mergé, il devient disponible et `_maybe_handle_resolution`
le détecte automatiquement.
"""
import hashlib
import json
import logging

from db import get_db
from investigation.pattern_detect import should_propose_global_fix
from notification.transition import transition_async
from resolution.apply_client import ApplyResult, apply_patch
from resolution.apply_guard import evaluate
from resolution.global_fix import (
    open_issue_for_code_transverse,
    open_pr_for_global_pattern,
)

logger = logging.getLogger("cloe-care.resolution")


def _log_apply_audit(
    ticket_id: str,
    client_id: str,
    fix_proposal: dict,
    result: ApplyResult,
) -> None:
    patch_hash = hashlib.sha256(
        json.dumps(fix_proposal, sort_keys=True).encode()
    ).hexdigest()[:16]
    conn = get_db()
    try:
        conn.execute(
            """INSERT INTO apply_patch_audit
                  (ticket_id, client_id, patch_type, target_path, patch_hash,
                   response_status, response_body, rolled_back)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                ticket_id,
                client_id,
                fix_proposal.get("type"),
                fix_proposal.get("target_path"),
                patch_hash,
                result.response_status,
                (result.detail or "")[:1000],
                1 if result.rolled_back else 0,
            ),
        )
        conn.commit()
    finally:
        conn.close()


async def handle_resolution(
    ticket_id: str,
    ticket: dict,
    analysis: dict,
    fingerprint_val: str,
    occurrences: int,
) -> None:
    category = analysis.get("category")

    if category in ("ux", "out_of_scope"):
        await transition_async(ticket_id, "no_action", {"reason": category})
        await transition_async(ticket_id, "resolved")
        return

    if category == "code_transverse":
        issue_url = await open_issue_for_code_transverse(ticket_id, analysis)
        await transition_async(ticket_id, "escalated", {"issue_url": issue_url})
        return

    fix_proposal = analysis.get("fix_proposal") or {}
    decision = evaluate(fix_proposal, category or "")

    if not decision.allowed:
        await transition_async(ticket_id, "escalated", {"reason": decision.reason})
        await open_issue_for_code_transverse(ticket_id, analysis)
        return

    await transition_async(ticket_id, "fixing")
    result = await apply_patch(ticket_id, ticket["client_id"], fix_proposal)
    _log_apply_audit(ticket_id, ticket["client_id"], fix_proposal, result)

    if not result.success:
        if result.rolled_back:
            await transition_async(
                ticket_id,
                "fix_rolled_back",
                {"detail": result.detail, "healthcheck": result.healthcheck_status},
            )
            await transition_async(
                ticket_id,
                "escalated",
                {"reason": "rollback_after_fix_failed"},
            )
        else:
            await transition_async(
                ticket_id,
                "escalated",
                {"detail": result.detail, "healthcheck": result.healthcheck_status},
            )
            await open_issue_for_code_transverse(ticket_id, analysis)
        return

    await transition_async(
        ticket_id, "fix_applied", {"healthcheck": result.healthcheck_status}
    )

    if should_propose_global_fix(
        occurrences, analysis.get("global_implication", "unknown")
    ):
        pr_url = await open_pr_for_global_pattern(
            ticket_id, analysis, fingerprint_val
        )
        await transition_async(ticket_id, "proposing_global", {"pr_url": pr_url})

    await transition_async(ticket_id, "resolved")
