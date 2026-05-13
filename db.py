"""Schéma SQLite et helpers de connexion pour cloe-care."""
import os
import sqlite3
from pathlib import Path


def _db_path() -> str:
    return os.getenv("CARE_DB_PATH", "/data/care/care.db")


def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(_db_path())
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def init_db() -> None:
    Path(_db_path()).parent.mkdir(parents=True, exist_ok=True)
    conn = get_db()
    try:
        conn.executescript(SCHEMA)
        _migrate(conn)
        conn.commit()
    finally:
        conn.close()


def _migrate(conn: sqlite3.Connection) -> None:
    """Migrations douces (idempotentes) pour les colonnes ajoutées après v0."""
    _add_column_if_missing(conn, "tickets", "admin_refusal_reason", "TEXT")
    # Pivot Cloé Aide (Expert Hermes) : un ticket peut désormais être créé par
    # cloe-api via /tickets/internal avec un visibility et une priority dédiés.
    # `visibility=internal` masque le ticket dans la vue client (feedback produit).
    _add_column_if_missing(conn, "tickets", "visibility", "TEXT NOT NULL DEFAULT 'client'")
    _add_column_if_missing(conn, "tickets", "priority", "TEXT NOT NULL DEFAULT 'normal'")
    _add_column_if_missing(conn, "tickets", "source", "TEXT NOT NULL DEFAULT 'intake'")


def _add_column_if_missing(conn: sqlite3.Connection, table: str, column: str, type_: str) -> None:
    cols = [r["name"] for r in conn.execute(f"PRAGMA table_info({table})")]
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {type_}")


SCHEMA = """
-- Tickets
CREATE TABLE IF NOT EXISTS tickets (
    id TEXT PRIMARY KEY,
    client_id TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'draft',
    category TEXT,
    severity TEXT DEFAULT 'p2',
    user_summary TEXT,
    chat_session_id TEXT,
    triage_result TEXT,
    investigation_report TEXT,
    proposed_fix TEXT,
    applied_fix_at TEXT,
    applied_by TEXT,
    rolled_back INTEGER DEFAULT 0,
    outcome TEXT,
    public_message TEXT,
    public_status_label TEXT,
    investigation_acu_cost REAL DEFAULT 0,
    matched_known_incident TEXT,
    similar_to_ticket TEXT,
    attachments_analyzed INTEGER DEFAULT 0,
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now')),
    resolved_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_tickets_client ON tickets(client_id, status);
CREATE INDEX IF NOT EXISTS idx_tickets_status ON tickets(status, created_at);

-- Chat messages (élicitation)
CREATE TABLE IF NOT EXISTS chat_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id TEXT NOT NULL REFERENCES tickets(id) ON DELETE CASCADE,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_chat_ticket ON chat_messages(ticket_id, created_at);

-- Audit trail append-only
CREATE TABLE IF NOT EXISTS ticket_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id TEXT NOT NULL REFERENCES tickets(id) ON DELETE CASCADE,
    event_type TEXT NOT NULL,
    actor TEXT NOT NULL,
    payload TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_events_ticket ON ticket_events(ticket_id, created_at);

-- Notifications
CREATE TABLE IF NOT EXISTS notifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id TEXT REFERENCES tickets(id) ON DELETE CASCADE,
    channel TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'queued',
    payload TEXT,
    sent_at TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);

-- Pièces jointes (métadonnées seules, contenu binaire sur disque)
CREATE TABLE IF NOT EXISTS attachments (
    id TEXT PRIMARY KEY,
    ticket_id TEXT NOT NULL REFERENCES tickets(id) ON DELETE CASCADE,
    original_filename TEXT NOT NULL,
    mime_type TEXT NOT NULL,
    size_original INTEGER NOT NULL,
    size_compressed INTEGER NOT NULL,
    storage_path TEXT NOT NULL,
    thumbnail_path TEXT,
    content_hash TEXT,
    extracted_text TEXT,
    page_count INTEGER,
    analyzed_at TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_attachments_ticket ON attachments(ticket_id);

-- Audit des fix appliqués via cloe-api
CREATE TABLE IF NOT EXISTS apply_patch_audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id TEXT REFERENCES tickets(id),
    client_id TEXT NOT NULL,
    patch_type TEXT NOT NULL,
    target_path TEXT,
    patch_hash TEXT,
    response_status INTEGER,
    response_body TEXT,
    rolled_back INTEGER DEFAULT 0,
    applied_at TEXT DEFAULT (datetime('now'))
);

-- Proposals globales (PR / Issue GitHub)
CREATE TABLE IF NOT EXISTS global_fix_proposals (
    id TEXT PRIMARY KEY,
    source_ticket_id TEXT REFERENCES tickets(id),
    pattern_signature TEXT NOT NULL,
    affected_clients_count INTEGER DEFAULT 1,
    target_repo TEXT NOT NULL,
    proposed_change TEXT,
    github_pr_url TEXT,
    github_issue_url TEXT,
    status TEXT NOT NULL DEFAULT 'pending_review',
    created_at TEXT DEFAULT (datetime('now')),
    reviewed_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_proposals_pattern ON global_fix_proposals(pattern_signature);

-- Incidents connus (status page)
CREATE TABLE IF NOT EXISTS known_incidents (
    id TEXT PRIMARY KEY,
    fingerprint TEXT UNIQUE,
    label TEXT NOT NULL,
    affects_plans TEXT DEFAULT '*',
    detected_at TEXT DEFAULT (datetime('now')),
    eta_resolution TEXT,
    public_message TEXT,
    auto_close_tickets INTEGER DEFAULT 0,
    resolved_at TEXT
);

-- Patterns pour déduplication / global fix proposal
CREATE TABLE IF NOT EXISTS pattern_fingerprints (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    fingerprint TEXT UNIQUE NOT NULL,
    sample_ticket_id TEXT REFERENCES tickets(id),
    last_fix TEXT,
    occurrences INTEGER DEFAULT 1,
    last_seen_at TEXT DEFAULT (datetime('now'))
);

-- Décisions admin (validation/refus des fixes)
CREATE TABLE IF NOT EXISTS admin_decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id TEXT NOT NULL REFERENCES tickets(id) ON DELETE CASCADE,
    admin_email TEXT NOT NULL,
    decision TEXT NOT NULL,
    comment TEXT,
    decided_at TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_admin_decisions_ticket ON admin_decisions(ticket_id);
"""
