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
    migrations_dir = Path("migrations")
    sql_files = sorted(migrations_dir.glob("*.sql"))
    async with connection(cfg) as conn:
        for sql_file in sql_files:
            await conn.execute(sql_file.read_text())
            print(f"applied {sql_file.name}")
