"""Tests for the Git-revision provenance fields in runtime_bridge.

Covers CTW_SOURCE_REVISION handling only -- this is a provenance-only change,
mirroring the pattern already proven and tested on OEM Radar.
"""

from __future__ import annotations

import pytest

import runtime_bridge


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CTW_SOURCE_REVISION", raising=False)


def test_source_revision_defaults_to_unknown_without_env_var() -> None:
    assert runtime_bridge._source_revision() == "unknown"
    assert runtime_bridge._source_revision_short() == "unknown"


def test_source_revision_reflects_full_sha_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    full_sha = "52fdd72876ab178c1cecf8b9b16bedc22e804729"
    monkeypatch.setenv("CTW_SOURCE_REVISION", full_sha)
    assert runtime_bridge._source_revision() == full_sha
    assert runtime_bridge._source_revision_short() == "52fdd72876ab"


def test_get_version_info_includes_source_revision(monkeypatch: pytest.MonkeyPatch) -> None:
    full_sha = "52fdd72876ab178c1cecf8b9b16bedc22e804729"
    monkeypatch.setenv("CTW_SOURCE_REVISION", full_sha)
    info = runtime_bridge.get_version_info()
    assert info["source_revision"] == full_sha
    assert info["source_revision_short"] == "52fdd72876ab"


def test_get_identity_includes_source_revision(monkeypatch: pytest.MonkeyPatch) -> None:
    full_sha = "52fdd72876ab178c1cecf8b9b16bedc22e804729"
    monkeypatch.setenv("CTW_SOURCE_REVISION", full_sha)
    identity = runtime_bridge.get_identity()
    assert identity["source_revision"] == full_sha
    assert identity["source_revision_short"] == "52fdd72876ab"


def test_get_identity_reports_unknown_without_env_var() -> None:
    identity = runtime_bridge.get_identity()
    assert identity["source_revision"] == "unknown"
    assert identity["source_revision_short"] == "unknown"
