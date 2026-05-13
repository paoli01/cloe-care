"""Contexte léger envoyé à Haiku au moment de l'intake.

L'investigation Sonnet aura accès au contexte complet (logs container,
config, session) plus tard via investigation/context_gather. Ici on donne
juste à Cloé Support le profil du client pour qu'elle pose des questions
pertinentes dès le premier tour, sans relancer Docker ni lire les logs.

Coût : 0 LLM call, juste lectures BDD/JSON locales.
"""
import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional


def _registry_path() -> str:
    return os.getenv("REGISTRY_PATH", "/data/cloe-api/registry.json")


def _clients_ro() -> Path:
    return Path(os.getenv("CLOE_CLIENTS_RO", "/opt/cloe/clients"))


def _proxy_db_path() -> str:
    return os.getenv("CLOE_PROXY_DB_PATH", "/opt/cloe/proxy/cloe_proxy.db")


def _registry_entry(client_id: str) -> dict:
    try:
        with open(_registry_path()) as f:
            registry = json.load(f)
        return registry.get(client_id) or {}
    except (OSError, json.JSONDecodeError):
        return {}


def _recent_sessions_count(client_id: str, hours: int = 48) -> int:
    """Nombre de sessions modifiées dans les dernières 48h (proxy d'activité)."""
    sessions_dir = _clients_ro() / client_id / "sessions"
    if not sessions_dir.exists():
        return 0
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    cutoff_ts = cutoff.timestamp()
    count = 0
    try:
        for p in sessions_dir.glob("*.json"):
            if p.stat().st_mtime > cutoff_ts:
                count += 1
    except OSError:
        pass
    return count


def _last_session_date(client_id: str) -> Optional[str]:
    sessions_dir = _clients_ro() / client_id / "sessions"
    if not sessions_dir.exists():
        return None
    try:
        latest = max(
            (p.stat().st_mtime for p in sessions_dir.glob("*.json")),
            default=None,
        )
    except OSError:
        return None
    if not latest:
        return None
    return datetime.fromtimestamp(latest, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _last_error_in_proxy(client_id: str, hours: int = 24) -> Optional[str]:
    """Cherche un model fallback / quota event récent (signal de souci)."""
    db_path = _proxy_db_path()
    if not os.path.exists(db_path):
        return None
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        row = conn.execute(
            """SELECT model, cost_eur, created_at
                 FROM cost_events
                WHERE client_id = ? AND created_at > ?
                ORDER BY created_at DESC LIMIT 1""",
            (client_id, since),
        ).fetchone()
        conn.close()
        if row:
            return f"dernier appel modèle: {row['model']} le {row['created_at'][:16]}"
    except sqlite3.Error:
        pass
    return None


def build_client_context(client_id: str) -> str:
    """Texte court (≤300 tokens) à coller dans le system prompt de Cloé Support.

    Inclut uniquement les infos qui aident Cloé à formuler des questions
    pertinentes — pas de PII inutile, pas d'identifiants techniques exposés
    au client.
    """
    entry = _registry_entry(client_id)
    plan = entry.get("plan", "inconnu")
    subscription = entry.get("subscription_status", "inconnu")
    use_hermes = entry.get("use_hermes_http", False)

    sessions_48h = _recent_sessions_count(client_id, 48)
    last_session = _last_session_date(client_id)
    last_llm = _last_error_in_proxy(client_id)

    lines = [
        "Contexte interne (à utiliser pour comprendre, JAMAIS à exposer au client) :",
        f"- Plan : {plan}",
        f"- Abonnement : {subscription}",
        f"- Sessions actives ces 48h : {sessions_48h}",
    ]
    if last_session:
        lines.append(f"- Dernière session : {last_session}")
    if last_llm:
        lines.append(f"- {last_llm}")
    if use_hermes:
        lines.append("- Mode Hermes HTTP actif")

    return "\n".join(lines)
