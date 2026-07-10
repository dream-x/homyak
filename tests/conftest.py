"""Общие фикстуры. DB-тесты работают против отдельной базы homyak_test (создай заранее)."""

from __future__ import annotations

import os

import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from homyak.core.models import Base

TEST_URL = os.getenv(
    "HOMYAK_TEST_DATABASE_URL",
    "postgresql+asyncpg://homyak:homyak@localhost:5432/homyak_test",
)


@pytest_asyncio.fixture
async def session_factory():
    engine = create_async_engine(TEST_URL)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.execute(
            text("TRUNCATE news_items, clusters, ingest_state RESTART IDENTITY CASCADE")
        )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()
