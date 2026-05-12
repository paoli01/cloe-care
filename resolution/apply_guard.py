"""Apply guard : règles déterministes qui valident un fix proposé.

Cette fonction est appelée AVANT tout appel à cloe-api. Elle protège contre
les fix LLM hallucinés ou malicieux (path traversal, clés secrètes, diff
massif, type de patch hors allowlist).
"""
import json
import os
import re
from dataclasses import dataclass
from typing import Optional

import yaml


FORBIDDEN_FILES = {
    "auth.json",
    "credentials.json",
    ".env",
}

FORBIDDEN_KEYS = {
    "api_key",
    "openrouter_key",
    "openrouter_api_key",
    "password",
    "secret",
    "token",
    "private_key",
}

ALLOWED_PATCH_TYPES = {
    "yaml_merge",
    "json_merge",
    "file_replace",
    "session_delete",
    "workflow_cancel",
    "loop_detector_reset",
    "container_restart",
}

ALLOWED_FILES_FOR_REPLACE = {
    "config.yaml",
    "client_overrides.json",
    "SOUL.md",
}

MAX_DIFF_LINES = 20
MAX_FILE_SIZE = 100_000


@dataclass
class GuardDecision:
    allowed: bool
    reason: str
    sanitized_patch: Optional[dict] = None


def _contains_forbidden_keys(data) -> Optional[str]:
    if isinstance(data, dict):
        for key, value in data.items():
            if isinstance(key, str) and key.lower() in FORBIDDEN_KEYS:
                return f"forbidden_key:{key}"
            r = _contains_forbidden_keys(value)
            if r:
                return r
    elif isinstance(data, list):
        for item in data:
            r = _contains_forbidden_keys(item)
            if r:
                return r
    return None


def _validate_yaml(content: str) -> tuple[bool, Optional[str]]:
    try:
        data = yaml.safe_load(content)
    except yaml.YAMLError as e:
        return False, f"yaml_invalid: {e}"
    if data is not None:
        forbidden = _contains_forbidden_keys(data)
        if forbidden:
            return False, forbidden
    return True, None


def _validate_json(content: str) -> tuple[bool, Optional[str]]:
    try:
        data = json.loads(content)
    except json.JSONDecodeError as e:
        return False, f"json_invalid: {e}"
    forbidden = _contains_forbidden_keys(data)
    if forbidden:
        return False, forbidden
    return True, None


_BASENAME_RE = re.compile(r"^[A-Za-z0-9_.-]+$")


def _basename_safe(path: str) -> Optional[str]:
    """Empêche tout path traversal. Retourne le nom de fichier final ou None."""
    if not path or ".." in path or path.startswith("/") or "\\" in path:
        return None
    parts = path.split("/")
    if len(parts) > 2:
        return None
    name = parts[-1]
    if not _BASENAME_RE.match(name):
        return None
    return name


def _safe_mode_blocks(patch_type: str) -> bool:
    """Garde-fou production : 7 premiers jours on désactive file_replace."""
    if os.getenv("CARE_SAFE_MODE", "true").lower() != "true":
        return False
    return patch_type == "file_replace"


def evaluate(fix_proposal: dict, category: str) -> GuardDecision:
    """Décide si le fix proposé peut être auto-appliqué."""
    if not fix_proposal or fix_proposal.get("type") == "none":
        return GuardDecision(allowed=False, reason="no_fix_proposed")

    patch_type = fix_proposal.get("type")
    if patch_type not in ALLOWED_PATCH_TYPES:
        return GuardDecision(allowed=False, reason=f"unknown_patch_type:{patch_type}")

    if category not in ("config_client", "data_client"):
        return GuardDecision(
            allowed=False,
            reason=f"category_not_auto_fixable:{category}",
        )

    if _safe_mode_blocks(patch_type):
        return GuardDecision(allowed=False, reason="safe_mode_file_replace_disabled")

    # Opérations qui ne touchent pas à un fichier client
    if patch_type in (
        "session_delete",
        "workflow_cancel",
        "loop_detector_reset",
        "container_restart",
    ):
        return GuardDecision(allowed=True, reason="ok", sanitized_patch=fix_proposal)

    target = fix_proposal.get("target_path") or ""
    filename = _basename_safe(target)
    if not filename:
        return GuardDecision(allowed=False, reason=f"invalid_target_path:{target}")

    if filename in FORBIDDEN_FILES:
        return GuardDecision(allowed=False, reason=f"forbidden_file:{filename}")

    if patch_type == "file_replace" and filename not in ALLOWED_FILES_FOR_REPLACE:
        return GuardDecision(
            allowed=False,
            reason=f"replace_not_allowed_for:{filename}",
        )

    content = fix_proposal.get("new_content") or ""
    if len(content) > MAX_FILE_SIZE:
        return GuardDecision(allowed=False, reason="content_too_large")

    line_count = content.count("\n") + 1
    if line_count > MAX_DIFF_LINES * 2:
        return GuardDecision(allowed=False, reason="diff_too_large")

    if patch_type == "yaml_merge" or (
        patch_type == "file_replace" and filename.endswith(".yaml")
    ):
        ok, err = _validate_yaml(content)
        if not ok:
            return GuardDecision(allowed=False, reason=err or "yaml_validation_failed")

    if patch_type == "json_merge" or (
        patch_type == "file_replace" and filename.endswith(".json")
    ):
        ok, err = _validate_json(content)
        if not ok:
            return GuardDecision(allowed=False, reason=err or "json_validation_failed")

    return GuardDecision(allowed=True, reason="ok", sanitized_patch=fix_proposal)
