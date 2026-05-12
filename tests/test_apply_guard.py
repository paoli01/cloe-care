"""Tests apply_guard : règles déterministes, défense en profondeur."""
import importlib
import os

import pytest


def _reload_guard_without_safe_mode():
    """Désactive le safe_mode pour pouvoir tester file_replace en local."""
    os.environ["CARE_SAFE_MODE"] = "false"
    import resolution.apply_guard as guard
    importlib.reload(guard)
    return guard


def _reload_guard_with_safe_mode():
    os.environ["CARE_SAFE_MODE"] = "true"
    import resolution.apply_guard as guard
    importlib.reload(guard)
    return guard


def test_blocks_when_no_fix():
    guard = _reload_guard_without_safe_mode()
    decision = guard.evaluate({"type": "none"}, "config_client")
    assert not decision.allowed
    assert decision.reason == "no_fix_proposed"


def test_blocks_code_transverse():
    guard = _reload_guard_without_safe_mode()
    decision = guard.evaluate(
        {
            "type": "file_replace",
            "target_path": "config.yaml",
            "new_content": "model:\n  default: x",
        },
        "code_transverse",
    )
    assert not decision.allowed
    assert "category_not_auto_fixable" in decision.reason


def test_blocks_forbidden_file():
    guard = _reload_guard_without_safe_mode()
    decision = guard.evaluate(
        {"type": "file_replace", "target_path": "auth.json", "new_content": "{}"},
        "config_client",
    )
    assert not decision.allowed
    assert "forbidden_file" in decision.reason


def test_blocks_path_traversal_dotdot():
    guard = _reload_guard_without_safe_mode()
    decision = guard.evaluate(
        {
            "type": "file_replace",
            "target_path": "../../../etc/passwd",
            "new_content": "x",
        },
        "config_client",
    )
    assert not decision.allowed
    assert "invalid_target_path" in decision.reason


def test_blocks_absolute_path():
    guard = _reload_guard_without_safe_mode()
    decision = guard.evaluate(
        {"type": "file_replace", "target_path": "/etc/passwd", "new_content": "x"},
        "config_client",
    )
    assert not decision.allowed


def test_blocks_forbidden_key_in_yaml():
    guard = _reload_guard_without_safe_mode()
    yaml_content = "model:\n  default: x\napi_key: SECRET_VALUE"
    decision = guard.evaluate(
        {
            "type": "file_replace",
            "target_path": "config.yaml",
            "new_content": yaml_content,
        },
        "config_client",
    )
    assert not decision.allowed
    assert "forbidden_key" in decision.reason


def test_blocks_forbidden_key_in_json():
    guard = _reload_guard_without_safe_mode()
    json_content = '{"openrouter_key": "sk-or-v1-leaked"}'
    decision = guard.evaluate(
        {
            "type": "file_replace",
            "target_path": "client_overrides.json",
            "new_content": json_content,
        },
        "config_client",
    )
    assert not decision.allowed
    assert "forbidden_key" in decision.reason


def test_allows_clean_config_replace():
    guard = _reload_guard_without_safe_mode()
    yaml_content = "model:\n  default: google/gemini-2.0-flash-001\nagent:\n  max_turns: 20"
    decision = guard.evaluate(
        {
            "type": "file_replace",
            "target_path": "config.yaml",
            "new_content": yaml_content,
        },
        "config_client",
    )
    assert decision.allowed


def test_blocks_diff_too_large():
    guard = _reload_guard_without_safe_mode()
    content = "\n".join(f"line {i}: value" for i in range(60))
    decision = guard.evaluate(
        {"type": "file_replace", "target_path": "SOUL.md", "new_content": content},
        "config_client",
    )
    assert not decision.allowed
    assert decision.reason == "diff_too_large"


def test_allows_session_delete():
    guard = _reload_guard_without_safe_mode()
    decision = guard.evaluate(
        {"type": "session_delete", "target_path": "sess_abc123"},
        "data_client",
    )
    assert decision.allowed


def test_allows_container_restart_without_target():
    guard = _reload_guard_without_safe_mode()
    decision = guard.evaluate(
        {"type": "container_restart"},
        "data_client",
    )
    assert decision.allowed


def test_safe_mode_blocks_file_replace():
    guard = _reload_guard_with_safe_mode()
    decision = guard.evaluate(
        {
            "type": "file_replace",
            "target_path": "config.yaml",
            "new_content": "model:\n  default: x",
        },
        "config_client",
    )
    assert not decision.allowed
    assert "safe_mode" in decision.reason


def test_safe_mode_still_allows_container_restart():
    guard = _reload_guard_with_safe_mode()
    decision = guard.evaluate(
        {"type": "container_restart"},
        "data_client",
    )
    assert decision.allowed


def test_blocks_invalid_yaml():
    guard = _reload_guard_without_safe_mode()
    decision = guard.evaluate(
        {
            "type": "file_replace",
            "target_path": "config.yaml",
            "new_content": "model:\n  default: [unclosed",
        },
        "config_client",
    )
    assert not decision.allowed
    assert "yaml_invalid" in decision.reason


def test_blocks_unknown_patch_type():
    guard = _reload_guard_without_safe_mode()
    decision = guard.evaluate(
        {"type": "rm_rf_slash", "target_path": "x"},
        "config_client",
    )
    assert not decision.allowed
    assert "unknown_patch_type" in decision.reason
