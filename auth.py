"""JWT auth partagée avec cloe-api (même JWT_SECRET, même algo HS256).

Le cookie httpOnly est posé par cloe-api/auth.login. Pour que ce cookie soit
lisible côté `care.hellocloe.fr`, il doit être posé avec `domain=.hellocloe.fr`
(voir 08_DEPLOYMENT). En attendant, le header `Authorization: Bearer …` est
accepté en fallback (utile pour les smoke tests et le mode dégradé direct).
"""
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import Cookie, Depends, Header, HTTPException, status
from jose import JWTError, jwt as jose_jwt

JWT_ALGORITHM = "HS256"


def _get_jwt_secret() -> str:
    secret = os.environ.get("JWT_SECRET") or os.environ.get("SERVICE_SECRET")
    if not secret:
        raise RuntimeError("JWT_SECRET environment variable is required")
    return secret


class JWTPayload:
    """Payload décodé. Aligné sur le format émis par cloe-api/jwt_auth."""

    def __init__(self, sub: str, plan: str, exp: int, jti: str):
        self.sub = sub
        self.client_id = sub
        self.plan = plan
        self.exp = exp
        self.jti = jti
        self.email: Optional[str] = None


def _decode_jwt(token: str) -> JWTPayload:
    try:
        payload = jose_jwt.decode(
            token,
            _get_jwt_secret(),
            algorithms=[JWT_ALGORITHM],
        )
    except JWTError as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid_token",
        ) from e

    exp = payload.get("exp", 0)
    if exp < datetime.now(timezone.utc).timestamp():
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="token_expired",
        )

    sub = payload.get("sub")
    if not sub:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid_token_no_sub",
        )

    return JWTPayload(
        sub=sub,
        plan=payload.get("plan", "starter"),
        exp=exp,
        jti=payload.get("jti", ""),
    )


async def verify_jwt(
    cloe_jwt: Optional[str] = Cookie(None),
    authorization: Optional[str] = Header(None),
) -> JWTPayload:
    """Auth via cookie httpOnly en priorité, header Bearer en fallback."""
    token = cloe_jwt
    if not token and authorization and authorization.startswith("Bearer "):
        token = authorization[7:]
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing_token",
        )
    return _decode_jwt(token)


def verify_tenant(client_id: str, payload: JWTPayload) -> None:
    """Garde-fou cross-tenant : le ticket appartient bien au sub du JWT."""
    if payload.sub != client_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="tenant_mismatch",
        )


CLIENT_ID_REGEX = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")
TICKET_ID_REGEX = re.compile(r"^ticket_[a-f0-9]{12}$")
ATTACHMENT_ID_REGEX = re.compile(r"^att_[a-f0-9]{16}$")


def validate_client_id(client_id: str) -> str:
    if not CLIENT_ID_REGEX.fullmatch(client_id):
        raise HTTPException(status_code=400, detail="invalid_client_id")
    return client_id


def validate_ticket_id(ticket_id: str) -> str:
    if not TICKET_ID_REGEX.fullmatch(ticket_id):
        raise HTTPException(status_code=400, detail="invalid_ticket_id")
    return ticket_id


def validate_attachment_id(attachment_id: str) -> str:
    if not ATTACHMENT_ID_REGEX.fullmatch(attachment_id):
        raise HTTPException(status_code=400, detail="invalid_attachment_id")
    return attachment_id


# ─── Auth admin (utilisée par 09_ADMIN_VIEW) ─────────────────────────────────

ADMIN_EMAILS_RAW = os.getenv("ADMIN_EMAILS", "")
ADMIN_EMAILS = {e.strip().lower() for e in ADMIN_EMAILS_RAW.split(",") if e.strip()}

REGISTRY_PATH = os.getenv("REGISTRY_PATH", "/data/cloe-api/registry.json")


def _lookup_email_by_client_id(client_id: str) -> Optional[str]:
    """Charge l'email depuis registry.json (RO mount). Pas de cache pour rester simple."""
    path = Path(REGISTRY_PATH)
    if not path.exists():
        return None
    try:
        with open(path) as f:
            registry = json.load(f)
        entry = registry.get(client_id) or {}
        email = entry.get("email")
        return email.lower() if email else None
    except (OSError, json.JSONDecodeError):
        return None


async def verify_admin(payload: JWTPayload = Depends(verify_jwt)) -> JWTPayload:
    """Auth admin : le sub du JWT doit avoir un email dans ADMIN_EMAILS."""
    if not ADMIN_EMAILS:
        raise HTTPException(status_code=503, detail="admin_not_configured")
    email = _lookup_email_by_client_id(payload.sub)
    if not email or email not in ADMIN_EMAILS:
        raise HTTPException(status_code=403, detail="not_admin")
    payload.email = email
    return payload


# ─── Auth service-to-service (cloe-api → cloe-care) ───────────────────────────


async def verify_service_key(
    x_service_key: Optional[str] = Header(None, alias="X-Service-Key"),
) -> None:
    """Garde-fou pour les endpoints appelés par cloe-api uniquement.

    Le secret ``CARE_SERVICE_KEY`` est partagé entre cloe-api et cloe-care
    (même valeur dans les deux ``.env``). Sans clé configurée, l'endpoint
    est désactivé (503) — meilleur "fail closed" qu'une porte ouverte.
    """
    expected = os.environ.get("CARE_SERVICE_KEY", "").strip()
    if not expected:
        raise HTTPException(status_code=503, detail="service_key_not_configured")
    if not x_service_key or x_service_key != expected:
        raise HTTPException(status_code=401, detail="invalid_service_key")
