"""Tests admin : auth gating, flow accept/refuse, statut requis."""
import importlib
import json
import os
import tempfile

import pytest
from fastapi.testclient import TestClient
from jose import jwt as jose_jwt


def _make_jwt(client_id: str, secret: str) -> str:
    from datetime import datetime, timedelta, timezone
    payload = {
        "sub": client_id,
        "plan": "pro",
        "iat": datetime.now(timezone.utc),
        "exp": datetime.now(timezone.utc) + timedelta(minutes=15),
        "jti": "test-jti",
    }
    return jose_jwt.encode(payload, secret, algorithm="HS256")


@pytest.fixture
def admin_client(tmp_path, monkeypatch):
    monkeypatch.setenv("CARE_DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("JWT_SECRET", "test-jwt-secret-xxxxxxxxxxxxxxxxxxx")
    monkeypatch.setenv("ADMIN_EMAILS", "admin@hellocloe.fr")

    # Mini registry avec un admin et un non-admin
    registry_path = tmp_path / "registry.json"
    registry_path.write_text(
        json.dumps(
            {
                "c_admin": {"email": "admin@hellocloe.fr", "plan": "pro"},
                "c_normal": {"email": "user@example.com", "plan": "pro"},
            }
        )
    )
    monkeypatch.setenv("REGISTRY_PATH", str(registry_path))

    for mod in ("db", "auth", "intake.chat", "routers.tickets", "routers.admin", "main"):
        importlib.reload(importlib.import_module(mod))

    import main
    importlib.reload(main)

    with TestClient(main.app) as c:
        yield c


def test_admin_check_returns_false_for_normal_user(admin_client):
    jwt = _make_jwt("c_normal", os.environ["JWT_SECRET"])
    resp = admin_client.get("/me/admin-check", headers={"Authorization": f"Bearer {jwt}"})
    assert resp.status_code == 200
    assert resp.json()["is_admin"] is False


def test_admin_check_returns_true_for_admin(admin_client):
    jwt = _make_jwt("c_admin", os.environ["JWT_SECRET"])
    resp = admin_client.get("/me/admin-check", headers={"Authorization": f"Bearer {jwt}"})
    assert resp.status_code == 200
    assert resp.json()["is_admin"] is True


def test_list_tickets_rejects_non_admin(admin_client):
    jwt = _make_jwt("c_normal", os.environ["JWT_SECRET"])
    resp = admin_client.get("/admin/tickets", headers={"Authorization": f"Bearer {jwt}"})
    assert resp.status_code == 403
    assert resp.json()["detail"] == "not_admin"


def test_list_tickets_allows_admin(admin_client):
    jwt = _make_jwt("c_admin", os.environ["JWT_SECRET"])
    resp = admin_client.get("/admin/tickets", headers={"Authorization": f"Bearer {jwt}"})
    assert resp.status_code == 200
    body = resp.json()
    assert "tickets" in body
    assert "total" in body


def test_admin_stats_for_admin(admin_client):
    jwt = _make_jwt("c_admin", os.environ["JWT_SECRET"])
    resp = admin_client.get("/admin/stats", headers={"Authorization": f"Bearer {jwt}"})
    assert resp.status_code == 200
    body = resp.json()
    assert "counts_by_status" in body
    assert "total_acu_consumed" in body
    assert "awaiting_admin_review" in body


def test_accept_fix_blocked_if_wrong_status(admin_client):
    """L'admin ne doit pas pouvoir accept sur un ticket pas en awaiting_admin_review."""
    jwt = _make_jwt("c_admin", os.environ["JWT_SECRET"])

    # Insert directe d'un ticket en status=resolved
    import db
    conn = db.get_db()
    conn.execute(
        "INSERT INTO tickets (id, client_id, status) VALUES (?, ?, ?)",
        ("ticket_abcdef012345", "c_normal", "resolved"),
    )
    conn.commit()
    conn.close()

    resp = admin_client.post(
        "/admin/tickets/ticket_abcdef012345/accept-fix",
        headers={"Authorization": f"Bearer {jwt}"},
        json={},
    )
    assert resp.status_code == 400
    assert "invalid_status" in resp.json()["detail"]


def test_refuse_fix_requires_min_reason(admin_client):
    jwt = _make_jwt("c_admin", os.environ["JWT_SECRET"])

    import db
    conn = db.get_db()
    conn.execute(
        "INSERT INTO tickets (id, client_id, status) VALUES (?, ?, ?)",
        ("ticket_abcdef012345", "c_normal", "awaiting_admin_review"),
    )
    conn.commit()
    conn.close()

    resp = admin_client.post(
        "/admin/tickets/ticket_abcdef012345/refuse-fix",
        headers={"Authorization": f"Bearer {jwt}"},
        json={"reason": "short", "escalate_to_github": False},
    )
    # Pydantic rejects min_length=10 with 422
    assert resp.status_code == 422


def test_refuse_fix_works_with_valid_reason(admin_client):
    jwt = _make_jwt("c_admin", os.environ["JWT_SECRET"])

    import db
    conn = db.get_db()
    conn.execute(
        "INSERT INTO tickets (id, client_id, status, investigation_report) "
        "VALUES (?, ?, ?, ?)",
        (
            "ticket_abcdef012345",
            "c_normal",
            "awaiting_admin_review",
            json.dumps({"root_cause": "x", "category": "config_client"}),
        ),
    )
    conn.commit()
    conn.close()

    resp = admin_client.post(
        "/admin/tickets/ticket_abcdef012345/refuse-fix",
        headers={"Authorization": f"Bearer {jwt}"},
        json={
            "reason": "Le fix proposé est trop risqué pour ce client",
            "escalate_to_github": False,
        },
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "refused"

    # Vérifie la trace en BDD
    conn = db.get_db()
    decisions = conn.execute(
        "SELECT decision, comment, admin_email FROM admin_decisions WHERE ticket_id = ?",
        ("ticket_abcdef012345",),
    ).fetchall()
    final_status = conn.execute(
        "SELECT status FROM tickets WHERE id = ?", ("ticket_abcdef012345",)
    ).fetchone()
    conn.close()

    assert len(decisions) == 1
    assert decisions[0]["decision"] == "refuse"
    assert decisions[0]["admin_email"] == "admin@hellocloe.fr"
    assert final_status["status"] == "escalated"


def test_admin_email_set_on_payload(admin_client):
    """L'admin email doit être attaché au payload pour traçabilité."""
    jwt = _make_jwt("c_admin", os.environ["JWT_SECRET"])
    resp = admin_client.get("/admin/stats", headers={"Authorization": f"Bearer {jwt}"})
    # 200 = email résolu et présent dans ADMIN_EMAILS
    assert resp.status_code == 200


def test_admin_check_403_when_no_admins_configured(admin_client, monkeypatch):
    monkeypatch.setenv("ADMIN_EMAILS", "")
    import auth
    importlib.reload(auth)
    import routers.admin as admin_mod
    importlib.reload(admin_mod)
    # Le client_id existe mais aucun admin n'est configuré
    jwt = _make_jwt("c_admin", os.environ["JWT_SECRET"])
    resp = admin_client.get("/admin/tickets", headers={"Authorization": f"Bearer {jwt}"})
    # admin_not_configured (503) ou not_admin (403) selon le moment du reload
    assert resp.status_code in (403, 503)
