"""Tests notifications : sanitizer anti-jargon, labels, stream SSE."""
import asyncio

import pytest

from notification.labels import (
    EMAIL_STATES,
    PUBLIC_LABELS,
    TERMINAL_STATES,
    is_terminal,
    label_for,
    should_email,
)
from notification.public_message import (
    FORBIDDEN_TERMS,
    _fallback_for,
    _FALLBACK_MESSAGES,
    _sanitize,
)
from notification.stream import StatusStreamManager


# ─── public_message sanitizer ────────────────────────────────────────────────


def test_sanitize_blocks_jargon():
    for term in ["container", "JWT", "Docker", "Hermes", "Prefect"]:
        result = _sanitize(f"Le {term} a redémarré.")
        assert result == "", f"sanitizer failed to block: {term}"


def test_sanitize_allows_clean_french():
    msg = "C'est corrigé, vous pouvez réessayer."
    assert _sanitize(msg) == msg


def test_sanitize_handles_empty():
    assert _sanitize("") == ""
    assert _sanitize(None) == ""


def test_all_states_have_fallback():
    for status in PUBLIC_LABELS:
        msg = _fallback_for(status)
        assert msg, f"no fallback for {status}"


def test_fallback_messages_have_no_jargon():
    """Le sanitizer ne doit jamais bloquer un fallback."""
    for status, msg in _FALLBACK_MESSAGES.items():
        assert _sanitize(msg), f"sanitizer blocked fallback[{status}]: {msg}"


def test_public_labels_have_no_jargon():
    """Idem pour les labels publics."""
    for status, label in PUBLIC_LABELS.items():
        assert _sanitize(label), f"sanitizer blocked label[{status}]: {label}"


# ─── labels ──────────────────────────────────────────────────────────────────


def test_label_for_unknown_status_falls_back():
    assert label_for("not_a_real_status") == "En cours"


def test_is_terminal_recognizes_terminal_states():
    for status in TERMINAL_STATES:
        assert is_terminal(status)


def test_is_terminal_negative_for_inflight():
    for status in ("investigating", "analyzed", "fixing", "draft"):
        assert not is_terminal(status)


def test_should_email_only_for_email_states():
    for status in EMAIL_STATES:
        assert should_email(status)
    for status in ("investigating", "fixing", "draft"):
        assert not should_email(status)


# ─── stream ──────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_publish_to_subscriber():
    mgr = StatusStreamManager()
    queue = mgr.subscribe("ticket_abcdef012345")

    await mgr.publish("ticket_abcdef012345", {"type": "status", "status": "received"})

    event = await asyncio.wait_for(queue.get(), timeout=1)
    assert event["status"] == "received"


@pytest.mark.asyncio
async def test_publish_no_subscribers_does_not_raise():
    mgr = StatusStreamManager()
    await mgr.publish("ticket_abcdef012345", {"type": "status"})


@pytest.mark.asyncio
async def test_unsubscribe_cleans_up():
    mgr = StatusStreamManager()
    queue = mgr.subscribe("ticket_abcdef012345")
    mgr.unsubscribe("ticket_abcdef012345", queue)
    assert "ticket_abcdef012345" not in mgr._subscribers


@pytest.mark.asyncio
async def test_multiple_subscribers_each_receive():
    mgr = StatusStreamManager()
    q1 = mgr.subscribe("ticket_abcdef012345")
    q2 = mgr.subscribe("ticket_abcdef012345")

    await mgr.publish("ticket_abcdef012345", {"type": "status", "status": "fixing"})

    e1 = await asyncio.wait_for(q1.get(), timeout=1)
    e2 = await asyncio.wait_for(q2.get(), timeout=1)
    assert e1 == e2 == {"type": "status", "status": "fixing"}
