"""TASK-CONFIG-002: bootstrap observability (CONFIG-001 P2/P4).

Bootstrap conflicts and db_only records are the drift signal between the
YAML seed and the repository — they must be impossible to miss:

* conflicts / db_only are logged at WARNING with provider, resource_id
  and (for conflicts) a secret-free canonical per-field diff, stating
  that the repository value is kept;
* check mode against an empty repository warns that the runtime will
  start without resources.

The diff itself is owned by ``core.resource_bootstrap.canonical_diff``:
differing leaves flatten to dotted paths and values under secret-shaped
field names (credential_secret, refresh_token, api_key, ...) are
replaced with ``<redacted>`` — definitions are not supposed to carry
secrets at all, so this is reporting defence in depth, not validation.
"""

from __future__ import annotations

import logging

import pytest

from app.main import _log_bootstrap_outcome, create_app
from core.resource_bootstrap import (
    BootstrapConflict,
    BootstrapMode,
    BootstrapRecord,
    BootstrapResult,
    canonical_diff,
)
from core.runtime_snapshot import RuntimeSnapshot, utcnow


def _result(**overrides) -> BootstrapResult:
    kwargs = dict(
        mode=BootstrapMode.IMPORT,
        added=[],
        unchanged=[],
        conflicts=[],
        db_only=[],
    )
    kwargs.update(overrides)
    return BootstrapResult(**kwargs)


def _snapshot(source_count: int = 1) -> RuntimeSnapshot:
    return RuntimeSnapshot(
        resources=[],
        generated_at=utcnow(),
        source_count=source_count,
        resources_by_provider={},
    )


def _conflict(**definition_overrides) -> BootstrapConflict:
    existing = {
        "provider": "antigravity",
        "resource_id": "r1",
        "enabled": True,
        "credential_id": "cred-a",
        "definition": {"project_id": "p-old", **definition_overrides},
    }
    incoming = {
        "provider": "antigravity",
        "resource_id": "r1",
        "enabled": False,
        "credential_id": "cred-b",
        "definition": {"project_id": "p-new", **definition_overrides},
    }
    # Make the secret fields differ too, so a redaction bug would leak
    # them into the "changed" report.
    existing["definition"]["credential_secret"] = "existing-secret-value"
    incoming["definition"]["credential_secret"] = "incoming-secret-value"
    return BootstrapConflict(
        provider="antigravity",
        resource_id="r1",
        existing=existing,
        incoming=incoming,
    )


# -- Part A + B: conflict warning with canonical diff ------------------------------


def test_conflict_warning_names_key_and_policy(caplog):
    result = _result(conflicts=[_conflict()])
    with caplog.at_level(logging.WARNING, logger="app.main"):
        _log_bootstrap_outcome(result, _snapshot())

    warnings = [
        record for record in caplog.records
        if record.levelno == logging.WARNING
    ]
    assert len(warnings) == 1
    message = warnings[0].getMessage()
    assert "provider=antigravity" in message
    assert "resource_id=r1" in message
    assert "existing != incoming" in message
    assert "keeping repository value" in message


def test_conflict_diff_is_secret_free_and_per_field(caplog):
    """The diff reports non-secret definition fields by dotted path and
    replaces secret-shaped values with <redacted> — the raw secret must
    appear nowhere in the logs."""
    result = _result(conflicts=[_conflict()])
    with caplog.at_level(logging.WARNING, logger="app.main"):
        _log_bootstrap_outcome(result, _snapshot())

    log_text = caplog.text
    # Secret redaction: neither value may leak.
    assert "existing-secret-value" not in log_text
    assert "incoming-secret-value" not in log_text
    assert "<redacted>" in log_text
    # Non-secret per-field diff (canonical payload dotted paths).
    assert "credential_id" in log_text
    assert "cred-a" in log_text and "cred-b" in log_text
    assert "enabled" in log_text
    assert "definition.project_id" in log_text
    assert "p-old" in log_text and "p-new" in log_text


def test_canonical_diff_shape_and_redaction():
    """Unit contract of the diff: flattened dotted paths, only differing
    leaves, secrets redacted."""
    existing = {
        "provider": "antigravity",
        "resource_id": "r1",
        "enabled": True,
        "credential_id": "cred-a",
        "definition": {
            "project_id": "p-old",
            "ide_type": "ANTIGRAVITY",  # unchanged leaf → absent
            "refresh_token": "token-old",
            "api_key": "key-old",
        },
    }
    incoming = {
        "provider": "antigravity",
        "resource_id": "r1",
        "enabled": False,
        "credential_id": "cred-b",
        "definition": {
            "project_id": "p-new",
            "ide_type": "ANTIGRAVITY",
            "refresh_token": "token-new",
            "api_key": "key-new",
        },
    }
    diff = canonical_diff(existing, incoming)

    assert diff == {
        "enabled": {"existing": True, "incoming": False},
        "credential_id": {"existing": "cred-a", "incoming": "cred-b"},
        "definition.project_id": {
            "existing": "p-old",
            "incoming": "p-new",
        },
        "definition.refresh_token": {
            "existing": "<redacted>",
            "incoming": "<redacted>",
        },
        "definition.api_key": {
            "existing": "<redacted>",
            "incoming": "<redacted>",
        },
    }


# -- Part C: db_only warning --------------------------------------------------------


def test_db_only_warning_names_resource_and_preservation(caplog):
    result = _result(
        db_only=[
            BootstrapRecord(
                provider="antigravity",
                resource_id="r-db-only",
                payload={},
            )
        ]
    )
    with caplog.at_level(logging.WARNING, logger="app.main"):
        _log_bootstrap_outcome(result, _snapshot())

    warnings = [
        record for record in caplog.records
        if record.levelno == logging.WARNING
    ]
    assert len(warnings) == 1
    message = warnings[0].getMessage()
    assert "provider=antigravity" in message
    assert "resource_id=r-db-only" in message
    assert "resources preserved" in message


# -- Part D: check mode against an empty repository ---------------------------------


def test_check_empty_repository_warns(caplog):
    """Fabricated outcome: check mode, empty repository (source_count=0),
    non-empty seed → the deployment guidance warning fires and the
    runtime snapshot is empty."""
    result = _result(
        mode=BootstrapMode.CHECK,
        added=[
            BootstrapRecord(
                provider="antigravity",
                resource_id="r1",
                payload={},
            )
        ],
    )
    snapshot = _snapshot(source_count=0)
    with caplog.at_level(logging.WARNING, logger="app.main"):
        _log_bootstrap_outcome(result, snapshot)

    assert snapshot.resources == []
    messages = [record.getMessage() for record in caplog.records]
    assert any(
        "check mode did not import resources" in message
        and "Use import mode for initial deployment" in message
        for message in messages
    )


def test_check_mode_memory_backend_warns_end_to_end(tmp_path, monkeypatch, caplog):
    """End to end over create_app: check mode + memory sink (empty on
    every start) + non-empty seed → guidance warning; runtime pools are
    empty."""
    monkeypatch.setenv("ADMIN_TOKEN", "test-token")
    with caplog.at_level(logging.WARNING, logger="app.main"):
        app = create_app(
            config={
                "providers": {
                    "antigravity": {
                        "enabled": True,
                        "resources": [{"id": "r1", "project_id": "p-seed"}],
                    }
                },
                "resource_bootstrap": {"enabled": True, "mode": "check"},
            },
            config_path=tmp_path / "config.yaml",
        )

    assert any(
        "check mode did not import resources" in record.getMessage()
        for record in caplog.records
    )
    assert list(app.state.scheduler.pools["antigravity"].resources) == []


def test_import_mode_and_nonempty_sink_do_not_warn(caplog):
    """No false positives: import mode with a satisfied sink, and check
    mode with a non-empty repository, stay INFO-only."""
    seed = [
        BootstrapRecord(
            provider="antigravity", resource_id="r1", payload={}
        )
    ]
    with caplog.at_level(logging.WARNING, logger="app.main"):
        # import mode, empty sink: added is fine — no warning.
        _log_bootstrap_outcome(
            _result(mode=BootstrapMode.IMPORT, added=seed), _snapshot()
        )
        # check mode but the repository has content: no warning.
        _log_bootstrap_outcome(
            _result(mode=BootstrapMode.CHECK, added=seed),
            _snapshot(source_count=2),
        )

    assert not [
        record for record in caplog.records
        if record.levelno == logging.WARNING
    ]
