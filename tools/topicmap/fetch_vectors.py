"""Забрать векторы bge-m3 для выборки из Qdrant на VM (batch retrieve по id) → vectors.npy + ids.npy."""
import csv, sys, time
import numpy as np
from qdrant_client import QdrantClient

ids = [int(r["id"]) for r in csv.DictReader(open("sample.csv", newline=""))]
print("sample ids", len(ids), flush=True)
c = QdrantClient(url="http://192.168.140.20:6333", timeout=120)
info = c.get_collection("news_items")
print("collection points", info.points_count, flush=True)
vecs, got = [], []
t0 = time.time()
for i in range(0, len(ids), 500):
    chunk = ids[i : i + 500]
    pts = c.retrieve("news_items", ids=chunk, with_vectors=True, with_payload=False)
    for p in pts:
        v = p.vector if not isinstance(p.vector, dict) else next(iter(p.vector.values()))
        vecs.append(np.asarray(v, dtype=np.float32)); got.append(int(p.id))
    if i % 5000 == 0:
        print(f"  {i}/{len(ids)} got {len(got)} {time.time()-t0:.0f}s", flush=True)
V = np.stack(vecs); I = np.asarray(got)
np.save("vectors.npy", V); np.save("ids.npy", I)
print("done", V.shape, f"{time.time()-t0:.0f}s", "missing", len(ids) - len(got))
