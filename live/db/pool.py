from __future__ import annotations

import os

import asyncpg

_pool: asyncpg.Pool | None = None


async def init_pool() -> asyncpg.Pool:
    global _pool
    db_url = os.environ["DB_URL"]
    _pool = await asyncpg.create_pool(db_url, min_size=2, max_size=10)
    return _pool


def get_pool() -> asyncpg.Pool:
    if _pool is None:
        raise RuntimeError("DB pool not initialised — call init_pool() first")
    return _pool


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None
