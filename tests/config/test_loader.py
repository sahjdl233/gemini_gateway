"""Config loader tests: env placeholder substitution, no secrets in files."""

from __future__ import annotations

from config.loader import _resolve, default_config, load_config

NL = chr(10)


def _placeholder(name: str) -> str:
    return chr(36) + chr(123) + name + chr(125)


def test_default_config_has_fake_only():
    cfg = default_config()
    assert "fake" in cfg["providers"]
    assert "firebase" not in cfg["providers"]


def test_env_substitution(monkeypatch):
    monkeypatch.setenv("FIREBASE_PROJECT_01", "project-secret-123")
    resolved = _resolve("project_id: " + _placeholder("FIREBASE_PROJECT_01"))
    assert resolved == "project_id: project-secret-123"


def test_load_config_file(tmp_path, monkeypatch):
    monkeypatch.setenv("FIREBASE_API_KEY_01", "abc-123")
    p = tmp_path / "c.yaml"
    p.write_text(
        NL.join(
            [
                "firebase:",
                "  project_id: " + _placeholder("FIREBASE_PROJECT_01"),
                "  api_key: " + _placeholder("FIREBASE_API_KEY_01"),
            ]
        ),
        encoding="utf-8",
    )
    cfg = load_config(p)
    assert cfg["firebase"]["project_id"] == ""
    assert cfg["firebase"]["api_key"] == "abc-123"


def test_missing_env_becomes_empty(monkeypatch):
    monkeypatch.delenv("FIREBASE_API_KEY_01", raising=False)
    resolved = _resolve("k=" + _placeholder("FIREBASE_API_KEY_01"))
    assert resolved == "k="
