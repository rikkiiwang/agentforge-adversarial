from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

import asyncpg

from agentforge_adversarial.config import Config

_pool: asyncpg.Pool | None = None


async def get_pool(cfg: Config) -> asyncpg.Pool:
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(cfg.database_url, min_size=1, max_size=4)
    return _pool


@asynccontextmanager
async def connection(cfg: Config):
    pool = await get_pool(cfg)
    async with pool.acquire() as conn:
        yield conn


async def init_schema(cfg: Config) -> None:
    sql = Path("migrations/001_initial.sql").read_text()
    async with connection(cfg) as conn:
        await conn.execute(sql)
