"""Shared fixtures for tests/test_api/ package.

Re-exports the fixtures defined in tests/test_api.py (which is not a conftest)
so that the test_api/ sub-package can use them without import tricks that
confuse ruff.
"""

# pytest discovers conftest fixtures automatically — no explicit re-export is
# needed as long as we import them at module level so pytest registers them.
from tests.test_api import (  # noqa: F401
    api_db_conn,
    api_factory,
    client,
    mock_embeddings,
    mock_vector_store,
    repo,
    services,
)
