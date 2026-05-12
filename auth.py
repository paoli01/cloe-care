"""JWT auth partagée avec cloe-api. Implémentation complète en 02_AUTH_INTAKE."""
from fastapi import HTTPException, status
from fastapi.security import HTTPBearer

bearer = HTTPBearer(auto_error=False)


async def verify_jwt():  # pragma: no cover - placeholder remplacé en 02
    raise HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        detail="not_implemented",
    )
