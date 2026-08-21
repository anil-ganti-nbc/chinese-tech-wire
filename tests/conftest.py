import pytest


@pytest.fixture(autouse=True)
def _explicit_test_mutation_profile():
    from web.app import app

    app.state.mutation_authorizer = lambda _value: True
    yield
    app.state.mutation_authorizer = None
