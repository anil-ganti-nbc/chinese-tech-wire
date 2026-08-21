from __future__ import annotations

import importlib.util
import os
from pathlib import Path


LAUNCHER = Path(__file__).resolve().parents[1] / "native" / "macos" / "launcher.py"


def _load_launcher():
    spec = importlib.util.spec_from_file_location("ctw_macos_launcher", LAUNCHER)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_field_test_runtime_is_isolated(tmp_path, monkeypatch):
    launcher = _load_launcher()
    state = tmp_path / "Chinese Tech Wire"
    monkeypatch.setenv("CTW_DATA_DIR", str(state))
    monkeypatch.setenv("CTW_CONFIG_DIR", "placeholder")
    monkeypatch.setenv("CTW_ENV_FILE", "placeholder")
    monkeypatch.delenv("CTW_DISABLE_COLLECTOR_LAUNCH", raising=False)
    monkeypatch.setenv("DATABASE_URL", "postgresql://production.invalid/ctw")
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "secret")
    monkeypatch.setenv("TRANSLATION_API_KEY", "secret")
    monkeypatch.setenv("GEMINI_API_KEY", "secret")

    resolved, resources = launcher.configure_field_test_runtime()

    assert resolved == state.resolve()
    assert os.environ["DATABASE_URL"] == f"sqlite:///{resolved / 'ctw.db'}"
    assert os.environ["CTW_ENV_FILE"] == str(resolved / "field-test.env")
    assert os.environ["CTW_CONFIG_DIR"] == str(resources / "config")
    # Field test allows real local collection: the launcher must not force
    # the collector-launch kill switch on.
    assert "CTW_DISABLE_COLLECTOR_LAUNCH" not in os.environ
    assert "DISCORD_WEBHOOK_URL" not in os.environ
    assert "TRANSLATION_API_KEY" not in os.environ
    assert "GEMINI_API_KEY" not in os.environ
    assert (resolved / "logs").is_dir()


def test_default_state_path_has_no_hard_coded_username(monkeypatch):
    launcher = _load_launcher()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: Path("/Users/example")))
    assert launcher.default_state_root() == Path(
        "/Users/example/Library/Application Support/Chinese Tech Wire"
    )
