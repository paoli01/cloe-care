"""Worker async d'investigation. Consomme une asyncio.Queue alimentée par submit.

État du ticket le long du flow :
  received → investigating → analyzed
                          → escalated (timeout / exception)

Les transitions suivantes (fixing/applied/rolled_back/resolved) sont gérées
en 05_APPLY_FIX et 06_NOTIFICATIONS qui étendent ce module.
"""
import asyncio
import json
import logging
from typing import Optional

from db import get_db
from investigation.context_gather import gather_context
from investigation.llm_analyze import investigate
from investigation.pattern_detect import fingerprint, record_pattern

logger = logging.getLogger("cloe-care.worker")

INVESTIGATE_QUEUE: asyncio.Queue = asyncio.Queue()
_WORKER_TASK: Optional[asyncio.Task] = None

INVESTIGATION_TIMEOUT_S = 90

# Sonnet pricing (USD per million tokens — source de vérité humaine, à
# affiner si on bascule sur un autre modèle).
SONNET_PRICING = {"input": 3.0, "output": 15.0}


def enqueue_ticket(ticket_id: str) -> None:
    """Appel synchrone safe depuis n'importe quelle route. Non bloquant."""
    INVESTIGATE_QUEUE.put_nowait(ticket_id)


def is_worker_alive() -> bool:
    return _WORKER_TASK is not None and not _WORKER_TASK.done()


def queue_size() -> int:
    return INVESTIGATE_QUEUE.qsize()


def _load_ticket(ticket_id: str) -> Optional[dict]:
    conn = get_db()
    try:
        row = conn.execute("SELECT * FROM tickets WHERE id = ?", (ticket_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def _transition(ticket_id: str, status: str, extra: Optional[dict] = None) -> None:
    """Transition synchrone basique. Remplacée par `transition_async` en 06."""
    conn = get_db()
    try:
        conn.execute(
            "UPDATE tickets SET status = ?, updated_at = datetime('now') WHERE id = ?",
            (status, ticket_id),
        )
        conn.execute(
            "INSERT INTO ticket_events (ticket_id, event_type, actor, payload) "
            "VALUES (?, ?, 'system', ?)",
            (ticket_id, f"transition_{status}", json.dumps(extra or {})),
        )
        conn.commit()
    finally:
        conn.close()


def _persist_analysis(ticket_id: str, result: dict) -> None:
    analysis = result["analysis"]
    usage = result.get("usage") or {}
    acu_cost = _estimate_acu(usage)

    conn = get_db()
    try:
        conn.execute(
            """UPDATE tickets
                  SET investigation_report = ?,
                      category = ?,
                      proposed_fix = ?,
                      investigation_acu_cost = ?,
                      updated_at = datetime('now')
                WHERE id = ?""",
            (
                json.dumps(analysis, ensure_ascii=False),
                analysis.get("category"),
                json.dumps(analysis.get("fix_proposal", {}), ensure_ascii=False),
                acu_cost,
                ticket_id,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _estimate_acu(usage: dict) -> float:
    """Coût USD approximatif. Stocké tel quel pour traçabilité ACU opérateur."""
    tokens_in = usage.get("prompt_tokens", 0) or 0
    tokens_out = usage.get("completion_tokens", 0) or 0
    cost_usd = (
        tokens_in * SONNET_PRICING["input"]
        + tokens_out * SONNET_PRICING["output"]
    ) / 1_000_000
    return round(cost_usd, 6)


async def _process_one(ticket_id: str) -> None:
    ticket = _load_ticket(ticket_id)
    if not ticket:
        logger.warning("ticket_not_found ticket_id=%s", ticket_id)
        return

    if ticket["status"] != "received":
        logger.info(
            "skip_non_received ticket_id=%s status=%s",
            ticket_id,
            ticket["status"],
        )
        return

    _transition(ticket_id, "investigating")

    try:
        session_id = (json.loads(ticket["user_summary"] or "{}")
                      .get("session_id"))
        gathered = await gather_context(ticket["client_id"], session_id)
        result = await asyncio.wait_for(
            investigate(ticket, gathered, ticket_id),
            timeout=INVESTIGATION_TIMEOUT_S,
        )
        _persist_analysis(ticket_id, result)

        analysis = result["analysis"]
        fp = fingerprint(analysis.get("root_cause", ""), analysis.get("category", ""))
        occurrences = record_pattern(ticket_id, fp)

        _transition(
            ticket_id,
            "analyzed",
            {
                "category": analysis.get("category"),
                "pattern_fingerprint": fp,
                "occurrences": occurrences,
            },
        )

        # 05_APPLY_FIX étend ici via _handle_resolution(...).
        await _maybe_handle_resolution(ticket_id, ticket, analysis, fp, occurrences)

    except asyncio.TimeoutError:
        logger.exception("investigation_timeout ticket_id=%s", ticket_id)
        _transition(ticket_id, "escalated", {"reason": "investigation_timeout"})
    except Exception as e:
        logger.exception("investigation_failed ticket_id=%s", ticket_id)
        _transition(
            ticket_id,
            "escalated",
            {"reason": f"investigation_error: {type(e).__name__}"},
        )


async def _maybe_handle_resolution(
    ticket_id: str,
    ticket: dict,
    analysis: dict,
    fingerprint_val: str,
    occurrences: int,
) -> None:
    """Hook étendu en 05/09. No-op tant que les modules resolution ne sont pas
    importables (cas de feature/investigation seul)."""
    try:
        from workers.resolution_pipeline import handle_resolution  # noqa: WPS433
    except ImportError:
        return

    await handle_resolution(ticket_id, ticket, analysis, fingerprint_val, occurrences)


async def _worker_loop() -> None:
    logger.info("investigate_worker_started")
    while True:
        try:
            ticket_id = await INVESTIGATE_QUEUE.get()
            try:
                await _process_one(ticket_id)
            finally:
                INVESTIGATE_QUEUE.task_done()
        except asyncio.CancelledError:
            logger.info("investigate_worker_cancelled")
            break
        except Exception:
            logger.exception("worker_loop_error")
            await asyncio.sleep(2)


def start_worker() -> None:
    global _WORKER_TASK
    if is_worker_alive():
        return
    _WORKER_TASK = asyncio.create_task(_worker_loop())


def stop_worker() -> None:
    global _WORKER_TASK
    if is_worker_alive():
        _WORKER_TASK.cancel()
