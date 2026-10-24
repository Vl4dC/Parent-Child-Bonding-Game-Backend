"""Fixtures for the end-to-end edge-case suite.

The faked-model `client`, `lesson_id` and `repository` fixtures are the ones the learning tests use,
so every case here runs against the in-memory adapter and, with TEST_DATABASE_URL set, PostgreSQL.
Bugs found while writing the suite are kept as strict xfails whose reason starts with a BUG-nn id;
see docs/E2E_TEST_REPORT.md. When one is fixed its test XPASSes and strict mode asks for the marker
to be removed.
"""

import pytest

from tests.learning import conftest as learning

_no_visual_model = learning._no_visual_model
client = learning.client
lesson_id = learning.lesson_id


@pytest.fixture
def api(client):
    """`client` with the per-account request limit lifted, for tests that make many requests."""
    client.app.state.settings.api_requests_per_minute = 100_000
    return client
