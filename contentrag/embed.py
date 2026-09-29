"""Step 5: local multilingual text embeddings of every moment, for fuzzy search."""

from __future__ import annotations

import numpy as np

from .config import Config
from .db import jloads

_MODEL = {}


def load_model(name: str):
    if name not in _MODEL:
        try:
            from sentence_transformers import SentenceTransformer  # type: ignore
        except ImportError:
            return None
        _MODEL[name] = SentenceTransformer(name)
    return _MODEL[name]


def moment_text(r) -> str:
    parts = [
        r["title"] or "",
        r["description"] or "",
        f"Action: {r['action']}" if r["action"] else "",
        f"Setting: {r['setting']}" if r["setting"] else "",
        f"Place: {r['place']}" if r["place"] else "",
        f"Shot: {r['shot_type']}" if r["shot_type"] else "",
        f"Mood: {r['mood']}" if r["mood"] else "",
        "Tags: " + ", ".join(jloads(r["tags"])),
        "Themes: " + ", ".join(jloads(r["content_uses"])),
        f"Speech: {r['speech_en']}" if r["speech_en"] else "",
    ]
    return ". ".join(p for p in parts if p)


def load_vectors(cfg: Config) -> tuple[np.ndarray, np.ndarray]:
    if not cfg.vectors_path.exists():
        return np.zeros(0, dtype=np.int64), np.zeros((0, 0), dtype=np.float16)
    data = np.load(cfg.vectors_path)
    return data["ids"], data["vecs"]


def embed(cfg: Config, conn, log=print) -> dict:
    model = load_model(cfg.embed_model)
    if model is None:
        raise SystemExit("sentence-transformers is not installed. On your Mac run: pip install -e '.[mac]'")
    ids, vecs = load_vectors(cfg)
    have = set(ids.tolist())
    rows = conn.execute(
        "SELECT m.*, md.title, md.place FROM moments m JOIN media md ON md.id=m.media_id"
    ).fetchall()
    live = {r["id"] for r in rows}
    keep = np.array([i in live for i in ids.tolist()], dtype=bool)  # drop vectors of deleted moments
    ids, vecs = ids[keep], vecs[keep] if len(vecs) else vecs
    todo = [r for r in rows if r["id"] not in have]
    if todo:
        log(f"[embed] embedding {len(todo)} moments with {cfg.embed_model}")
        new = model.encode([moment_text(r) for r in todo], batch_size=32, normalize_embeddings=True,
                           show_progress_bar=True).astype(np.float16)
        new_ids = np.array([r["id"] for r in todo], dtype=np.int64)
        ids = np.concatenate([ids, new_ids]) if len(ids) else new_ids
        vecs = np.vstack([vecs, new]) if len(vecs) else new
    cfg.vectors_path.parent.mkdir(parents=True, exist_ok=True)
    np.savez(cfg.vectors_path, ids=ids, vecs=vecs)
    return {"embedded": len(todo), "total": int(len(ids))}


def vector_search(cfg: Config, query: str, top: int = 200) -> list[tuple[int, float]]:
    ids, vecs = load_vectors(cfg)
    if not len(ids):
        return []
    model = load_model(cfg.embed_model)
    if model is None:
        return []
    q = model.encode([query], normalize_embeddings=True)[0].astype(np.float32)
    scores = vecs.astype(np.float32) @ q
    order = np.argsort(-scores)[:top]
    return [(int(ids[i]), float(scores[i])) for i in order]
