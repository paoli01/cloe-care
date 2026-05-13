"""Collecte read-only du contexte technique nécessaire à l'investigation.

Tout est strictement lecture seule :
- container logs via socket-proxy-readonly (LOGS=1, EXEC=0, POST=0)
- fichiers clients montés en :ro depuis /opt/cloe/clients/<id>
- registry.json monté en :ro depuis cloe-api
- cloe_proxy.db monté en :ro (file:?mode=ro)
"""
import json
import logging
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import httpx

logger = logging.getLogger("cloe-care.context")


def _clients_ro() -> Path:
    return Path(os.getenv("CLOE_CLIENTS_RO", "/opt/cloe/clients"))


def _registry_path() -> str:
    return os.getenv("REGISTRY_PATH", "/data/cloe-api/registry.json")


def _proxy_db_path() -> str:
    return os.getenv("CLOE_PROXY_DB_PATH", "/opt/cloe/proxy/cloe_proxy.db")


def _docker_host() -> str:
    return os.getenv("DOCKER_HOST", "tcp://socket-proxy-readonly:2375")


LOG_TAIL = 2000
LOG_LOOKBACK_HOURS = 2
LOG_TRUNCATE = 80000
FILE_READ_LIMIT = 50000


def read_client_file(client_id: str, relative_path: str) -> Optional[str]:
    """Lecture limitée d'un fichier client. None si absent ou illisible."""
    path = _clients_ro() / client_id / relative_path
    if not path.exists() or not path.is_file():
        return None
    try:
        return path.read_text(encoding="utf-8", errors="replace")[:FILE_READ_LIMIT]
    except OSError:
        return None


def read_registry_entry(client_id: str) -> Optional[dict]:
    try:
        with open(_registry_path()) as f:
            registry = json.load(f)
        return registry.get(client_id)
    except (OSError, json.JSONDecodeError):
        return None


async def read_container_logs(container_name: str) -> str:
    """Lit les logs Docker via socket-proxy (mode read-only).

    Le proxy expose l'API Docker en HTTP simple ; on remplace `tcp://` par
    `http://` pour httpx.
    """
    base = _docker_host().replace("tcp://", "http://")
    since = int(
        (datetime.now(timezone.utc) - timedelta(hours=LOG_LOOKBACK_HOURS)).timestamp()
    )

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                f"{base}/containers/{container_name}/logs",
                params={
                    "stdout": 1,
                    "stderr": 1,
                    "tail": LOG_TAIL,
                    "since": since,
                    "timestamps": 1,
                },
            )
            resp.raise_for_status()
    except httpx.HTTPError as e:
        logger.warning(
            "read_container_logs_failed container=%s error=%s",
            container_name,
            type(e).__name__,
        )
        return ""

    return _decode_docker_log_stream(resp.content)[:LOG_TRUNCATE]


def _decode_docker_log_stream(raw: bytes) -> str:
    """Décode le format multiplexé stdout/stderr Docker (header 8B + payload)."""
    lines: list[str] = []
    i = 0
    while i + 8 <= len(raw):
        size = int.from_bytes(raw[i + 4:i + 8], "big")
        i += 8
        if size <= 0 or i + size > len(raw):
            break
        try:
            lines.append(raw[i:i + size].decode("utf-8", errors="replace"))
        except Exception:
            pass
        i += size

    if not lines:
        try:
            return raw.decode("utf-8", errors="replace")
        except Exception:
            return ""
    return "".join(lines)


def read_session(client_id: str, session_id: Optional[str]) -> Optional[dict]:
    if not session_id:
        return None
    # Garde-fou path traversal côté ID de session
    if "/" in session_id or ".." in session_id:
        return None
    path = _clients_ro() / client_id / "sessions" / f"{session_id}.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def read_recent_acu_events(client_id: str, hours: int = 24) -> list[dict]:
    """Lit les events ACU récents en mode read-only sur cloe_proxy.db."""
    db_path = _proxy_db_path()
    if not os.path.exists(db_path):
        return []

    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        rows = conn.execute(
            """SELECT model, cost_eur, tokens_in, tokens_out, created_at
                 FROM cost_events
                WHERE client_id = ? AND created_at > ?
                ORDER BY created_at DESC
                LIMIT 30""",
            (client_id, since),
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except sqlite3.Error as e:
        logger.debug("read_acu_events_failed client=%s error=%s", client_id, e)
        return []


async def gather_context(client_id: str, session_id: Optional[str]) -> dict:
    """Assemble le contexte read-only nécessaire à l'investigation."""
    registry_entry = read_registry_entry(client_id) or {}
    container_name = registry_entry.get("container_name", f"hermes_{client_id}")

    logs = await read_container_logs(container_name)
    config = read_client_file(client_id, "config.yaml")
    soul = read_client_file(client_id, "SOUL.md")
    overrides = read_client_file(client_id, "client_overrides.json")
    session = read_session(client_id, session_id)
    acu_events = read_recent_acu_events(client_id)

    return {
        "client_id": client_id,
        "plan": registry_entry.get("plan", "unknown"),
        "subscription_status": registry_entry.get("subscription_status"),
        "container_name": container_name,
        "container_logs": logs,
        "config_yaml": config,
        "soul_md": soul[:5000] if soul else None,
        "client_overrides_json": overrides,
        "session": session,
        "recent_acu_events": acu_events,
    }
