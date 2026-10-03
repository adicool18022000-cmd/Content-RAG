"""Face grouping, on the Mac, with OpenCV's YuNet detector + SFace recogniser (MIT / Apache-2.0).

1. `crag faces` finds faces in the frames `prep` already sampled and groups them into people.
2. `crag people` lists the groups; you name them (`crag people name 7 Riya`; giving two groups
   the same name merges them) and hide anyone who must never appear (`crag people hide Riya`).
3. Hiding a person rescans every clip they appear in at 1 frame/second, so search, pull and
   edits can use the rest of those clips and skip exactly the parts where they're on screen.

Nothing leaves the Mac; the two model files (~40 MB) are downloaded once into library/models.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
import urllib.request
from pathlib import Path

import numpy as np

from .config import Config
from .util import source_path

MODELS = {
    "yunet": ("face_detection_yunet_2023mar.onnx",
              "https://github.com/opencv/opencv_zoo/raw/main/models/face_detection_yunet/"
              "face_detection_yunet_2023mar.onnx", 100_000),
    "sface": ("face_recognition_sface_2021dec.onnx",
              "https://github.com/opencv/opencv_zoo/raw/main/models/face_recognition_sface/"
              "face_recognition_sface_2021dec.onnx", 10_000_000),
}
MIN_FACE_PX = 36          # smaller faces (in 640 px frames) are too blurry to recognise
DETECT_SCORE = 0.8
SAME_PERSON = 0.40        # cosine similarity; SFace's published threshold is 0.363 - a bit stricter here
HIDDEN_MATCH = 0.36       # when looking for a hidden person, err on the side of catching them
DENSE_FPS = 1.0


class FaceEngine:
    """Thin wrapper so tests can swap in a fake."""

    def __init__(self, models_dir: Path):
        import cv2

        self.cv2 = cv2
        det, rec = (ensure_model(models_dir, k) for k in ("yunet", "sface"))
        self.detector = cv2.FaceDetectorYN.create(str(det), "", (320, 320), DETECT_SCORE, 0.3, 5000)
        self.recognizer = cv2.FaceRecognizerSF.create(str(rec), "")

    def faces(self, image_path: Path) -> list[tuple[tuple[int, int, int, int], float, np.ndarray]]:
        img = self.cv2.imread(str(image_path))
        if img is None:
            return []
        h, w = img.shape[:2]
        self.detector.setInputSize((w, h))
        _, found = self.detector.detect(img)
        out = []
        for f in (found if found is not None else []):
            x, y, fw, fh = (int(v) for v in f[:4])
            if min(fw, fh) < MIN_FACE_PX:
                continue
            emb = self.recognizer.feature(self.recognizer.alignCrop(img, f)).flatten().astype(np.float32)
            out.append(((x, y, fw, fh), float(f[14]), emb / (np.linalg.norm(emb) or 1.0)))
        return out


def ensure_model(models_dir: Path, key: str) -> Path:
    name, url, min_size = MODELS[key]
    path = models_dir / name
    if path.exists() and path.stat().st_size >= min_size:
        return path
    models_dir.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".part")
    try:
        urllib.request.urlretrieve(url, tmp)
    except Exception as e:
        raise RuntimeError(f"couldn't download the face model {name} ({e}); check the internet connection")
    if tmp.stat().st_size < min_size:
        tmp.unlink(missing_ok=True)
        raise RuntimeError(f"downloaded face model {name} looks wrong (too small)")
    tmp.replace(path)
    return path


def engine(cfg: Config) -> FaceEngine:
    try:
        import cv2  # noqa: F401
    except ImportError:
        raise SystemExit("opencv is not installed. Run: uv pip install opencv-python-headless")
    return FaceEngine(cfg.library_dir / "models")


# ---------------------------------------------------------------- people store

def _emb(blob) -> np.ndarray:
    return np.frombuffer(blob, dtype=np.float32)


def _people(conn) -> tuple[list[int], np.ndarray]:
    rows = conn.execute("SELECT id, centroid FROM people WHERE centroid IS NOT NULL").fetchall()
    if not rows:
        return [], np.zeros((0, 128), dtype=np.float32)
    return [r["id"] for r in rows], np.vstack([_emb(r["centroid"]) for r in rows])


def _save_sample(cfg: Config, frame: Path, box, person_id: int) -> str | None:
    try:
        from PIL import Image

        x, y, w, h = box
        pad = int(0.35 * max(w, h))
        with Image.open(frame) as im:
            crop = im.crop((max(0, x - pad), max(0, y - pad), x + w + pad, y + h + pad)).convert("RGB")
            crop.thumbnail((160, 160))
            out = cfg.library_dir / "people" / f"{person_id}.jpg"
            out.parent.mkdir(parents=True, exist_ok=True)
            crop.save(out, "JPEG", quality=85)
        return out.relative_to(cfg.library_dir).as_posix()
    except Exception:
        return None


def assign(conn, cfg: Config, emb: np.ndarray, frame: Path, box) -> int:
    """Put a face into the closest person group, or start a new group."""
    ids, cents = _people(conn)
    if len(ids):
        sims = cents @ emb
        best = int(np.argmax(sims))
        if sims[best] >= SAME_PERSON:
            pid = ids[best]
            row = conn.execute("SELECT faces, centroid FROM people WHERE id=?", (pid,)).fetchone()
            n = row["faces"] or 1
            c = (_emb(row["centroid"]) * n + emb) / (n + 1)
            c = (c / (np.linalg.norm(c) or 1.0)).astype(np.float32)
            conn.execute("UPDATE people SET faces=faces+1, centroid=? WHERE id=?", (c.tobytes(), pid))
            return pid
    pid = conn.execute("INSERT INTO people(faces, centroid) VALUES (1, ?)", (emb.astype(np.float32).tobytes(),)).lastrowid
    sample = _save_sample(cfg, frame, box, pid)
    if sample:
        conn.execute("UPDATE people SET sample=? WHERE id=?", (sample, pid))
    return pid


def find_faces(cfg: Config, conn, eng=None, limit: int | None = None, log=print, stop=None) -> dict:
    """Detect + group faces in every prepared item not scanned yet."""
    rows = conn.execute(
        "SELECT id, relpath FROM media WHERE prepped=1 AND skip_reason IS NULL AND coalesce(faces_done,0)=0 "
        "ORDER BY taken_at").fetchall()
    if limit:
        rows = rows[:limit]
    if not rows:
        return {"scanned": 0, "faces": 0}
    eng = eng or engine(cfg)
    found = 0
    for i, m in enumerate(rows, 1):
        for fr in conn.execute("SELECT t, path FROM frames WHERE media_id=? ORDER BY t", (m["id"],)).fetchall():
            frame = cfg.library_dir / fr["path"]
            for box, score, emb in eng.faces(frame):
                pid = assign(conn, cfg, emb, frame, box)
                conn.execute("INSERT INTO faces(media_id, t, x, y, w, h, score, emb, person_id) "
                             "VALUES (?,?,?,?,?,?,?,?,?)", (m["id"], fr["t"], *box, score, emb.tobytes(), pid))
                found += 1
        conn.execute("UPDATE media SET faces_done=1 WHERE id=?", (m["id"],))
        if i % 50 == 0:
            conn.commit()
            log(f"[faces] {i}/{len(rows)} clips, {found} faces")
            if stop is not None and stop.is_set():
                break
    conn.commit()
    return {"scanned": len(rows), "faces": found, "people": conn.execute("SELECT count(*) FROM people").fetchone()[0]}


# ---------------------------------------------------------------- naming, merging, hiding

def list_people(conn, min_faces: int = 3) -> list[dict]:
    return [dict(r) | {"clips": r["clips"]} for r in conn.execute(
        "SELECT p.id, p.name, p.hidden, p.faces, p.sample, "
        "(SELECT count(DISTINCT media_id) FROM faces f WHERE f.person_id=p.id) AS clips "
        "FROM people p WHERE p.faces >= ? OR p.hidden=1 OR p.name IS NOT NULL "
        "ORDER BY p.hidden DESC, (p.name IS NULL), p.faces DESC", (min_faces,))]


def resolve_people(conn, ref: str) -> list[int]:
    """A person id ('7') or a name ('Riya', all groups with that name)."""
    if ref.isdigit():
        return [int(ref)]
    ids = [r["id"] for r in conn.execute("SELECT id FROM people WHERE lower(name)=lower(?)", (ref,))]
    if not ids:
        raise SystemExit(f"No person called '{ref}'. Name a group first: crag people name <id> {ref}")
    return ids


def name_person(conn, pid: int, name: str) -> int:
    """Name a group. If another group already has this name, merge into it. Returns the kept id."""
    other = conn.execute("SELECT id, faces, centroid, hidden FROM people WHERE lower(name)=lower(?) AND id!=?",
                         (name, pid)).fetchone()
    if other is None:
        conn.execute("UPDATE people SET name=? WHERE id=?", (name, pid))
        conn.commit()
        return pid
    me = conn.execute("SELECT faces, centroid, hidden FROM people WHERE id=?", (pid,)).fetchone()
    a, b = me["faces"] or 1, other["faces"] or 1
    c = _emb(me["centroid"]) * a + _emb(other["centroid"]) * b
    c = (c / (np.linalg.norm(c) or 1.0)).astype(np.float32)
    conn.execute("UPDATE faces SET person_id=? WHERE person_id=?", (other["id"], pid))
    conn.execute("UPDATE people SET faces=?, centroid=?, hidden=? WHERE id=?",
                 (a + b, c.tobytes(), int(bool(me["hidden"] or other["hidden"])), other["id"]))
    conn.execute("UPDATE hidden SET ref=? WHERE kind='person' AND ref=?", (str(other["id"]), str(pid)))
    conn.execute("DELETE FROM people WHERE id=?", (pid,))
    conn.commit()
    return other["id"]


def hide_people(cfg: Config, conn, ref: str, eng=None, log=print, dense: bool = True) -> dict:
    from .usage import hide

    ids = resolve_people(conn, ref)
    for pid in ids:
        hide(conn, "person", str(pid), "hidden person")
    return refine_hidden(cfg, conn, eng=eng, log=log) if dense else {"rescanned": 0}


def refine_hidden(cfg: Config, conn, eng=None, log=print, stop=None) -> dict:
    """Rescan clips that show a hidden person at DENSE_FPS so their exact on-screen times are known."""
    hidden = [r["id"] for r in conn.execute("SELECT id FROM people WHERE hidden=1")]
    if not hidden:
        return {"rescanned": 0}
    marks = ",".join("?" * len(hidden))
    cents = np.vstack([_emb(r["centroid"]) for r in conn.execute(
        f"SELECT centroid FROM people WHERE id IN ({marks})", hidden)])
    clips = [r["media_id"] for r in conn.execute(
        f"SELECT DISTINCT f.media_id FROM faces f JOIN media m ON m.id=f.media_id WHERE f.person_id IN ({marks}) "
        "AND m.kind='video' AND coalesce(m.faces_dense,0)=0", hidden)]
    if not clips:
        return {"rescanned": 0}
    eng = eng or engine(cfg)
    log(f"[faces] checking {len(clips)} clips second by second for hidden people")
    added = 0
    with tempfile.TemporaryDirectory(prefix="crag-faces-") as tmp:
        for i, mid in enumerate(clips, 1):
            src = source_path(cfg, conn, mid)
            if src is None:
                continue  # drive unplugged: coarse ranges still apply, retried next time
            frames = _dense_frames(src, Path(tmp) / mid)
            for t, frame in frames:
                for box, score, emb in eng.faces(frame):
                    sims = cents @ emb
                    k = int(np.argmax(sims))
                    if sims[k] >= HIDDEN_MATCH:
                        conn.execute("INSERT INTO faces(media_id, t, x, y, w, h, score, emb, person_id) "
                                     "VALUES (?,?,?,?,?,?,?,?,?)",
                                     (mid, t, *box, score, emb.tobytes(), hidden[k]))
                        added += 1
            shutil.rmtree(Path(tmp) / mid, ignore_errors=True)
            conn.execute("UPDATE media SET faces_dense=1 WHERE id=?", (mid,))
            conn.commit()
            if i % 20 == 0:
                log(f"[faces] {i}/{len(clips)} clips rechecked")
            if stop is not None and stop.is_set():
                break
    return {"rescanned": len(clips), "matches": added}


def _dense_frames(src: Path, out_dir: Path) -> list[tuple[float, Path]]:
    out_dir.mkdir(parents=True, exist_ok=True)
    try:
        subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-i", str(src), "-vf",
                        f"fps={DENSE_FPS},scale='if(gt(iw,ih),640,-2)':'if(gt(iw,ih),-2,640)'",
                        "-q:v", "4", str(out_dir / "f%06d.jpg")], capture_output=True, timeout=3600)
    except subprocess.TimeoutExpired:
        pass
    frames = sorted(out_dir.glob("f*.jpg"))
    # frame n (1-based) of fps=F covers time (n-1)/F .. n/F; use its middle
    return [((int(p.stem[1:]) - 0.5) / DENSE_FPS, p) for p in frames]

