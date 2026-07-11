"""Qdrant: векторное хранилище эмбеддингов bge-m3 (1024-dim, Cosine)."""

from __future__ import annotations

import structlog
from qdrant_client import AsyncQdrantClient, models

log = structlog.get_logger(__name__)

VECTOR_SIZE = 1024


class QdrantStore:
    COLLECTION = "news_items"
    TASTE_COLLECTION = "taste"  # singleton point id=1 — «вектор вкуса» (центроид лайков)

    def __init__(self, url: str) -> None:
        self._client = AsyncQdrantClient(url=url)

    async def ensure_collection(self) -> None:
        if not await self._client.collection_exists(self.COLLECTION):
            await self._client.create_collection(
                self.COLLECTION,
                vectors_config=models.VectorParams(
                    size=VECTOR_SIZE, distance=models.Distance.COSINE
                ),
            )
            log.info("qdrant_collection_created", collection=self.COLLECTION)

    async def ensure_taste_collection(self) -> None:
        if not await self._client.collection_exists(self.TASTE_COLLECTION):
            await self._client.create_collection(
                self.TASTE_COLLECTION,
                vectors_config=models.VectorParams(
                    size=VECTOR_SIZE, distance=models.Distance.COSINE
                ),
            )
            log.info("qdrant_collection_created", collection=self.TASTE_COLLECTION)

    async def get_taste(self) -> list[float] | None:
        try:
            pts = await self._client.retrieve(
                self.TASTE_COLLECTION, ids=[1], with_vectors=True
            )
        except Exception:
            return None
        if not pts or pts[0].vector is None:
            return None
        return list(pts[0].vector)

    async def set_taste(self, vector: list[float]) -> None:
        await self._client.upsert(
            self.TASTE_COLLECTION,
            points=[models.PointStruct(id=1, vector=vector, payload={})],
        )

    async def upsert_vector(
        self, news_item_id: int, vector: list[float], payload: dict
    ) -> None:
        await self._client.upsert(
            self.COLLECTION,
            points=[models.PointStruct(id=news_item_id, vector=vector, payload=payload)],
        )

    async def batch_upsert(self, points: list[tuple[int, list[float], dict]]) -> None:
        await self._client.upsert(
            self.COLLECTION,
            points=[
                models.PointStruct(id=i, vector=v, payload=p) for i, v, p in points
            ],
        )

    async def search_similar(
        self,
        vector: list[float],
        limit: int = 5,
        score_threshold: float = 0.88,
        exclude_id: int | None = None,
    ) -> list[tuple[int, float, dict]]:
        res = await self._client.query_points(
            self.COLLECTION,
            query=vector,
            limit=limit + (1 if exclude_id is not None else 0),
            score_threshold=score_threshold,
            with_payload=True,
        )
        out: list[tuple[int, float, dict]] = []
        for p in res.points:
            if exclude_id is not None and int(p.id) == exclude_id:
                continue
            out.append((int(p.id), float(p.score), p.payload or {}))
        return out[:limit]

    async def delete_vector(self, news_item_id: int) -> None:
        await self._client.delete(
            self.COLLECTION,
            points_selector=models.PointIdsList(points=[news_item_id]),
        )

    async def count(self) -> int:
        res = await self._client.count(self.COLLECTION)
        return res.count

    async def close(self) -> None:
        await self._client.close()
