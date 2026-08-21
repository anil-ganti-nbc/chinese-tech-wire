import pytest


@pytest.fixture(autouse=True)
def _explicit_test_mutation_profile(monkeypatch):
    monkeypatch.setenv("CTW_TEST_ALLOW_UNAUTH_MUTATIONS", "1")
