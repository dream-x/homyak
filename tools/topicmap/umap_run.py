"""UMAP(cosine) + HDBSCAN по 20k векторам выборки → topicmap.json для HTML-карты."""
import csv, json, time, collections
import numpy as np

t0 = time.time()
V = np.load("vectors.npy"); I = np.load("ids.npy")
rows = {int(r["id"]): r for r in csv.DictReader(open("sample.csv", newline=""))}
FB = {}
for iid, sig in csv.reader(open("feedback.csv", newline="")):
    FB.setdefault(int(iid), set()).add(sig)
print("feedback on sample", sum(1 for i in rows if i in FB), flush=True)
print("loaded", V.shape, f"{time.time()-t0:.0f}s", flush=True)

import umap, hdbscan
import os, sys
if os.path.exists("xy.npy"):
    xy = np.load("xy.npy"); print("umap cached", flush=True)
else:
    xy = umap.UMAP(n_neighbors=15, min_dist=0.1, metric="cosine", random_state=42, n_jobs=1).fit_transform(V)
    np.save("xy.npy", xy); print("umap", f"{time.time()-t0:.0f}s", flush=True)
MCS = int(sys.argv[1]) if len(sys.argv) > 1 else 40
MS = int(sys.argv[2]) if len(sys.argv) > 2 else 10
METHOD = sys.argv[3] if len(sys.argv) > 3 else "eom"
labels = hdbscan.HDBSCAN(min_cluster_size=MCS, min_samples=MS, cluster_selection_method=METHOD).fit_predict(xy)
n_cl = labels.max() + 1
print("hdbscan clusters", n_cl, "noise", int((labels < 0).sum()), f"{time.time()-t0:.0f}s", flush=True)

BROAD = {"business", "it", "medical", "ai", "news", "tech", "technology", "finance", "markets", "economy",
         "health", "medicine", "software", "programming", "science", "politics", "world", "russia", "usa",
         "startups", "healthcare", "investments", "stocks", "research"}
import math
STOP = set("""the and for with from that this your what will have into about after over more than
are not but you can how its new says said top best why when their they them what were been has had
who our out all one two 2026 2025 could would should just like also here there still some only many
amid как для что это или при его она они них все уже ещё еще был была были будет может после
через между также очень очень какой какие такой""".split())
TOK = __import__("re").compile(r"[a-zA-Zа-яёА-ЯЁ][a-zA-Zа-яёА-ЯЁ\-]{3,}")
def tag_terms(r):
    return {t for t in r["tags"].split("|") if t and t not in BROAD}
def word_terms(r):
    return {w for w in (x.lower() for x in TOK.findall(r["title"])) if w not in STOP and w not in BROAD}
def terms(r):
    return tag_terms(r) | word_terms(r)
DF = collections.Counter()
for r in rows.values():
    for t in terms(r): DF[t] += 1
N = len(rows)
VERTS = ["business", "it", "medical"]
xy = xy - xy.min(0); xy = xy / xy.max()  # 0..1
clusters = []
for c in range(n_cl):
    m = labels == c
    tags = collections.Counter(); words = collections.Counter(); verts = collections.Counter()
    for i in np.where(m)[0]:
        r = rows[int(I[i])]
        for t in tag_terms(r):
            if DF[t] >= 5: tags[t] += 1
        for t in word_terms(r):
            if DF[t] >= 5: words[t] += 1
        verts[r["vertical"]] += 1
    # лифт: тег, который в кластере встречается часто, а в выборке редко — и есть имя кластера;
    # теги первыми (словарь LLM чище), слова заголовков — только когда тегов не хватило
    floor = max(3, int(0.04 * m.sum()))
    lift = lambda cnt: sorted(((t, c * math.log(N / DF[t])) for t, c in cnt.items() if c >= floor), key=lambda kv: -kv[1])
    top = []
    for pool, want in ((lift(tags), 3), (lift(words), 2)):
        for term, _ in pool:
            if len(top) >= want: break
            if not any(term in x or x in term for x in top): top.append(term)  # crypto / cryptocurrency — не дважды
    clusters.append({"id": c, "n": int(m.sum()), "x": round(float(xy[m, 0].mean()), 4),
                     "y": round(float(xy[m, 1].mean()), 4), "label": " · ".join(top[:2]) or "—",
                     "tags": top, "verts": {k: int(v) for k, v in verts.items()},
                     "fb": int(sum(1 for i in np.where(m)[0] if int(I[i]) in FB and FB[int(I[i])] & {"up", "save"}))})
pts = []
for i in range(len(I)):
    r = rows[int(I[i])]
    pts.append([round(float(xy[i, 0]), 4), round(float(xy[i, 1]), 4), int(labels[i]),
                VERTS.index(r["vertical"]), round(float(r["score"]), 2), int(I[i]),
                r["title"][:120], r["url"], r["feed"], r["tags"], r["dt"],
                ("s" if "save" in FB.get(int(I[i]), ()) else "u" if "up" in FB.get(int(I[i]), ()) else "d" if "down" in FB.get(int(I[i]), ()) else "")])
json.dump({"verts": VERTS, "clusters": clusters, "points": pts,
           "meta": {"n": len(pts), "fb": sum(1 for p in pts if p[11]), "umap": "n_neighbors=15 min_dist=0.1 cosine", "hdbscan": f"min_cluster_size={MCS} min_samples={MS} {METHOD}",
                    "window_days": 90, "generated": time.strftime("%Y-%m-%d")}},
          open("topicmap.json", "w"), ensure_ascii=False, separators=(",", ":"))
import os
print("json", os.path.getsize("topicmap.json") // 1024, "KB", f"{time.time()-t0:.0f}s")
for c in sorted(clusters, key=lambda c: -c["n"])[:12]: print(" ", c["n"], c["label"], c["verts"])
