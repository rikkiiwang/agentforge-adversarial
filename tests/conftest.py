from __future__ import annotations

import pytest

from agentforge_adversarial import db as db_module


@pytest.fixture(autouse=True)
async def _reset_pool():
    """Each test gets a fresh asyncpg pool because pytest-asyncio creates a new event loop per test."""
    yield
    if db_module._pool is not None:
        await db_module._pool.close()
        db_module._pool = None
