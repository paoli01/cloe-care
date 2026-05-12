"""Client HTTP vers `cloe-api /internal/apply-patch`. Aucun écrit FS local."""
import hashlib
import json
import os
from dataclasses import dataclass
from typing import Optional

import httpx


def _cloe_api_url() -> str:
    return os.getenv("CLOE_API_URL", "http://cloe-api:8700")


def _cloe_api_key() -> str:
    return os.getenv("CLOE_API_KEY") or os.getenv("SERVICE_SECRET", "")


@dataclass
class ApplyResult:
    success: bool
    rolled_back: bool
    healthcheck_status: Optional[str]
    response_status: int
    detail: str


async def apply_patch(
    ticket_id: str,
    client_id: str,
    fix_proposal: dict,
) -> ApplyResult:
    """Appelle cloe-api /internal/apply-patch et interprète la réponse."""
    patch_hash = hashlib.sha256(
        json.dumps(fix_proposal, sort_keys=True).encode()
    ).hexdigest()[:16]

    payload = {
        "ticket_id": ticket_id,
        "client_id": client_id,
        "patch_type": fix_proposal["type"],
        "target_path": fix_proposal.get("target_path"),
        "new_content": fix_proposal.get("new_content"),
        "patch_hash": patch_hash,
    }

    try:
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(
                f"{_cloe_api_url()}/internal/apply-patch",
                headers={"X-API-Key": _cloe_api_key()},
                json=payload,
            )
    except httpx.HTTPError as e:
        return ApplyResult(
            success=False,
            rolled_back=False,
            healthcheck_status=None,
            response_status=0,
            detail=f"connection_error: {type(e).__name__}",
        )

    if resp.status_code != 200:
        body = resp.text[:500]
        return ApplyResult(
            success=False,
            rolled_back=False,
            healthcheck_status=None,
            response_status=resp.status_code,
            detail=body,
        )

    data = resp.json()
    return ApplyResult(
        success=bool(data.get("success", False)),
        rolled_back=bool(data.get("rolled_back", False)),
        healthcheck_status=data.get("healthcheck_status"),
        response_status=200,
        detail=data.get("detail", ""),
    )
