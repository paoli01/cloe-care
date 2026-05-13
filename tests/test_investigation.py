"""Tests investigation : fingerprint, décodage logs Docker, decision propose global."""
import importlib

import pytest

from investigation.context_gather import _decode_docker_log_stream
from investigation.pattern_detect import (
    GLOBAL_FIX_THRESHOLD,
    fingerprint,
    record_pattern,
    should_propose_global_fix,
)


# ─── fingerprint ─────────────────────────────────────────────────────────────


def test_fingerprint_normalizes_client_id():
    fp1 = fingerprint(
        "Le client client_abc voit une erreur le 2026-04-18", "config_client"
    )
    fp2 = fingerprint(
        "Le client client_xyz voit une erreur le 2026-05-01", "config_client"
    )
    assert fp1 == fp2


def test_fingerprint_differs_by_category():
    fp1 = fingerprint("Cause identique", "config_client")
    fp2 = fingerprint("Cause identique", "data_client")
    assert fp1 != fp2


def test_fingerprint_stable_across_whitespace():
    fp1 = fingerprint("Erreur de quota", "code_transverse")
    fp2 = fingerprint("Erreur   de   quota  ", "code_transverse")
    assert fp1 == fp2


# ─── should_propose_global_fix ───────────────────────────────────────────────


def test_global_fix_triggered_by_llm():
    assert should_propose_global_fix(
        occurrences=1, llm_implication="likely_others_affected"
    )


def test_global_fix_triggered_by_recurrence():
    assert should_propose_global_fix(
        occurrences=GLOBAL_FIX_THRESHOLD, llm_implication="isolated"
    )


def test_global_fix_not_triggered():
    assert not should_propose_global_fix(occurrences=1, llm_implication="isolated")


# ─── Docker log multiplex decoder ────────────────────────────────────────────


def test_decode_docker_log_stream_multiplexed():
    msg = b"hello world\n"
    header = bytes([1, 0, 0, 0]) + len(msg).to_bytes(4, "big")
    raw = header + msg
    assert _decode_docker_log_stream(raw) == "hello world\n"


def test_decode_handles_empty():
    assert _decode_docker_log_stream(b"") == ""


def test_decode_concatenates_multiple_frames():
    chunks = [b"line 1\n", b"line 2\n", b"line 3\n"]
    raw = b""
    for c in chunks:
        raw += bytes([1, 0, 0, 0]) + len(c).to_bytes(4, "big") + c
    assert _decode_docker_log_stream(raw) == "line 1\nline 2\nline 3\n"


# ─── record_pattern persistence ──────────────────────────────────────────────


def test_record_pattern_increments(tmp_path, monkeypatch):
    monkeypatch.setenv("CARE_DB_PATH", str(tmp_path / "test.db"))
    import db as db_module
    importlib.reload(db_module)
    db_module.init_db()

    import investigation.pattern_detect as pd
    importlib.reload(pd)

    # FK constraint exige des tickets valides en référence
    conn = db_module.get_db()
    for tid in ("ticket_abc012345678", "ticket_abc012345679", "ticket_abc012345680"):
        conn.execute(
            "INSERT INTO tickets (id, client_id, status) VALUES (?, 'test_client', 'analyzed')",
            (tid,),
        )
    conn.commit()
    conn.close()

    fp = pd.fingerprint("Test cause", "config_client")
    assert pd.record_pattern("ticket_abc012345678", fp) == 1
    assert pd.record_pattern("ticket_abc012345679", fp) == 2
    assert pd.record_pattern("ticket_abc012345680", fp) == 3
