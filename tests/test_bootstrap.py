"""Smoke tests pour le bootstrap : schéma BDD + endpoint /health."""
import importlib
import sqlite3


def test_init_db_creates_all_tables(tmp_path, monkeypatch):
    db_file = tmp_path / "test.db"
    monkeypatch.setenv("CARE_DB_PATH", str(db_file))

    import db
    importlib.reload(db)

    db.init_db()

    conn = sqlite3.connect(str(db_file))
    conn.row_factory = sqlite3.Row
    tables = {
        row["name"]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    conn.close()

    expected = {
        "tickets",
        "chat_messages",
        "ticket_events",
        "notifications",
        "attachments",
        "apply_patch_audit",
        "global_fix_proposals",
        "known_incidents",
        "pattern_fingerprints",
        "admin_decisions",
    }
    assert expected.issubset(tables), f"missing tables: {expected - tables}"


def test_tickets_has_admin_refusal_reason(tmp_path, monkeypatch):
    """La migration douce doit ajouter admin_refusal_reason même sur une base existante."""
    db_file = tmp_path / "test.db"
    monkeypatch.setenv("CARE_DB_PATH", str(db_file))

    import db
    importlib.reload(db)
    db.init_db()

    conn = sqlite3.connect(str(db_file))
    conn.row_factory = sqlite3.Row
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(tickets)")}
    conn.close()

    assert "admin_refusal_reason" in cols


def test_health_endpoint(tmp_path, monkeypatch):
    monkeypatch.setenv("CARE_DB_PATH", str(tmp_path / "test.db"))

    import db
    importlib.reload(db)
    import main
    importlib.reload(main)

    from fastapi.testclient import TestClient

    with TestClient(main.app) as client:
        response = client.get("/health")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["service"] == "cloe-care"
    assert body["version"] == "0.1.0"
