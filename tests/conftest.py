import os

import pytest


@pytest.fixture(autouse=True)
def _explicit_test_mutation_profile():
    from web.app import app

    app.state.mutation_authorizer = lambda _value: True
    yield
    app.state.mutation_authorizer = None


_PAID_HOSTS = ("openrouter.ai", "generativelanguage.googleapis.com", "api.openai.com")


def _paid_host(url: str) -> bool:
    try:
        from urllib.parse import urlsplit

        return urlsplit(url).hostname or "" in _PAID_HOSTS or any(
            (urlsplit(url).hostname or "").endswith(h) for h in _PAID_HOSTS
        )
    except Exception:
        return False


@pytest.fixture(autouse=True)
def _no_paid_api_calls(request, monkeypatch, tmp_path):
    """COST-CONTAINMENT HARD WALL.

    Normal pytest execution must never be able to spend money on a paid
    inference API, even if a real OPENROUTER_API_KEY exists in the
    operator's shell:

      1. credential neutralisation — every paid-API key env var is removed
         for the duration of each test, so no translator can be BUILT with
         a real credential;
      2. transport wall — httpx and urllib requests aimed at any paid
         inference host raise immediately, so even a hand-built translator
         cannot reach the network;
      3. telemetry isolation — the translation spend store is redirected to
         the test's tmp dir, so tests neither read the operator's real
         daily spend (which would trip the breaker) nor write into it.

    The only exception is a test explicitly marked @pytest.mark.live_openrouter
    AND run with ALLOW_PAID_API_TESTS=1 in the environment. Both are
    required: possessing a key is not consent to spend. Marked tests without
    the env var skip; unmarked tests fail loudly at the wall.
    """
    for var in ("OPENROUTER_API_KEY", "GEMINI_API_KEY", "TRANSLATION_API_KEY", "EMBEDDING_API_KEY"):
        monkeypatch.delenv(var, raising=False)

    import pipeline.translation_cost as cost_mod
    from datetime import datetime as _dt, timezone as _tz

    monkeypatch.setattr(cost_mod, "DB_PATH", tmp_path / "translation_cost.db")
    monkeypatch.setattr(cost_mod, "_RUN_STARTED", _dt.now(_tz.utc).isoformat())
    monkeypatch.setattr(cost_mod, "_warned_budget", {"run": False, "day": False})

    live_requested = request.node.get_closest_marker("live_openrouter") is not None
    allowed = live_requested and os.environ.get("ALLOW_PAID_API_TESTS") == "1"

    if allowed:
        return  # explicit opt-in: the test owns its own live transport

    if live_requested:
        pytest.skip("live paid OpenRouter test requires ALLOW_PAID_API_TESTS=1")

    import httpx
    import urllib.request

    real_httpx_post = httpx.Client.post
    real_urlopen = urllib.request.urlopen

    def wall_post(self, url, *args, **kwargs):
        if _paid_host(str(url)):
            raise AssertionError(
                "TEST WALL: a paid inference API call was attempted in a normal "
                f"test ({url}). Normal pytest must cost exactly $0.00. Mark the "
                "test @pytest.mark.live_openrouter AND run with "
                "ALLOW_PAID_API_TESTS=1 to opt in."
            )
        return real_httpx_post(self, url, *args, **kwargs)

    def wall_urlopen(url, *args, **kwargs):
        target = getattr(url, "full_url", url)
        if _paid_host(str(target)):
            raise AssertionError(
                "TEST WALL: a paid inference API call was attempted in a normal "
                f"test ({target}). Normal pytest must cost exactly $0.00. Mark the "
                "test @pytest.mark.live_openrouter AND run with "
                "ALLOW_PAID_API_TESTS=1 to opt in."
            )
        return real_urlopen(url, *args, **kwargs)

    monkeypatch.setattr(httpx.Client, "post", wall_post)
    monkeypatch.setattr(urllib.request, "urlopen", wall_urlopen)
    yield


def pytest_configure(config):
    config.addinivalue_line(
        "markers", "live_openrouter: PAID live OpenRouter API test; requires "
        "ALLOW_PAID_API_TESTS=1 and is excluded from normal suite runs"
    )
