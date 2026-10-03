"""Step 4: Claude looks at contact sheets + transcript and writes structured moment descriptions.

Bulk runs go through the Message Batches API (50% cheaper, results within ~24h):
    crag describe estimate   -> cost preview, nothing is sent
    crag describe submit     -> build and submit batches
    crag describe collect    -> fetch finished batches into the index
    crag describe retry      -> resend refused/errored requests with retry_model
`crag describe sync --limit N` sends a few requests immediately (for testing prompts).
"""

from __future__ import annotations

import base64
import json
import math
from pathlib import Path

from .config import Config
from .db import reindex_fts
from .sheets import PER_SHEET, sheets_for, to_b64_jpeg
from .util import fmt_ts

PHOTOS_PER_REQUEST = 6
MAX_REQUESTS_PER_BATCH = 2000
MAX_BATCH_BYTES = 150 * 1024 * 1024

# $/MTok (input, output) at standard rates; Batches API is half.
PRICES = {
    "claude-opus-5-5": (4.0, 20.0),
    "claude-sonnet-5-5": (2.0, 10.0),
    "claude-haiku-4-5": (1.0, 5.0),
}

SHOT_TYPES = ["extreme_close_up", "close_up", "medium", "wide", "selfie", "pov", "over_shoulder",
              "top_down", "screen_recording", "other"]
CAMERA_MOTION = ["static", "handheld", "pan", "tilt", "tracking", "walking", "driving_riding", "zoom", "other"]
ROLES = ["hook", "cinematic", "spectacle", "story_to_camera", "funny", "emotional", "establishing", "transition",
         "food", "friends", "action", "calm", "work"]
QUALITY = ["shaky", "blurry", "dark", "overexposed", "noisy_audio", "obstructed", "low_resolution", "vertical_letterbox"]

_MOMENT_PROPS = {
    "description": {"type": "string", "description": "1-2 vivid, concrete sentences: who/what is visible, what happens, where."},
    "action": {"type": "string", "description": "Main action as a short verb phrase, e.g. 'typing on laptop', 'riding a motorbike at night'."},
    "setting": {"type": "string", "description": "Place type, e.g. 'college hostel room', 'cafe', 'highway', 'office desk'."},
    "people_count": {"type": "integer"},
    "mood": {"type": "string", "description": "One or two words, e.g. 'nostalgic', 'focused', 'chaotic fun'."},
    "quality_issues": {"type": "array", "items": {"type": "string", "enum": QUALITY}},
    "broll_score": {"type": "integer", "description": "1-5. 5 = stable, well lit, clear action, usable in a reel as-is. 1 = unusable."},
    "content_uses": {"type": "array", "items": {"type": "string"},
                     "description": "Themes this could illustrate, e.g. 'founder grind', 'college nostalgia', 'Bangalore life'."},
    "tags": {"type": "array", "items": {"type": "string"}, "description": "5-12 lowercase search keywords (objects, places, activities)."},
    "content_roles": {"type": "array", "items": {"type": "string", "enum": ROLES},
                      "description": "What this moment is good for in a reel (0-4 roles, see instructions)."},
    "hook_score": {"type": "integer", "description": "1-5: how strongly this would stop someone scrolling in the "
                                                    "first 2 seconds of a reel (funny, weird, shocking, spectacular)."},
    "story_seed": {"type": "string", "description": "If something story-worthy happens or is told here, one sentence "
                                                    "a creator could build a reel on; otherwise empty."},
    "motion": {"type": "integer", "description": "1-5 visual motion/pace: 1 still, 3 normal movement, 5 very fast "
                                                "(running, riding, dancing, whip pans). Used to match music tempo."},
}

VIDEO_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string", "description": "3-8 word title for this footage."},
        "summary": {"type": "string", "description": "2-4 sentences: what this footage is about, as a memory."},
        "tags": {"type": "array", "items": {"type": "string"}},
        "moments": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "start": {"type": "number", "description": "seconds from the start of the file"},
                    "end": {"type": "number"},
                    "shot_type": {"type": "string", "enum": SHOT_TYPES},
                    "camera_motion": {"type": "string", "enum": CAMERA_MOTION},
                    "energy": {"type": "string", "enum": ["low", "medium", "high"]},
                    "speech_en": {"type": "string", "description": "English gist of what is said in this moment, empty if nothing."},
                    **_MOMENT_PROPS,
                },
                "required": ["start", "end", "shot_type", "camera_motion", "energy", "speech_en", *_MOMENT_PROPS],
                "additionalProperties": False,
            },
        },
    },
    "required": ["title", "summary", "tags", "moments"],
    "additionalProperties": False,
}

PHOTO_SCHEMA = {
    "type": "object",
    "properties": {
        "photos": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "index": {"type": "integer", "description": "Photo number as labelled, starting at 1."},
                    "title": {"type": "string"},
                    "shot_type": {"type": "string", "enum": SHOT_TYPES},
                    **_MOMENT_PROPS,
                },
                "required": ["index", "title", "shot_type", *_MOMENT_PROPS],
                "additionalProperties": False,
            },
        }
    },
    "required": ["photos"],
    "additionalProperties": False,
}

SYSTEM = """You catalogue a content creator's personal footage archive so clips can be found later \
for Instagram reels and so an AI assistant can learn the story of their life. The creator is \
based in India (currently Bangalore); speech is mostly Hindi or Hinglish.

You receive contact sheets (each tile is one sampled frame, its timestamp printed in yellow in the \
top-left), the Whisper transcript (may contain recognition errors), and file metadata.

For video, split the covered time range into moments: contiguous segments that together cover the \
whole range, breaking where the shot, place or action changes (detected scene cuts are listed as \
hints). Typical moments are 3-30 seconds; a long static talk or single continuous action may be one \
longer moment. Use timestamps from the tiles; start/end are seconds from the start of the file.

Be concrete and searchable: name objects, places, activities, time of day, weather, clothing, \
vehicles, screens (what app or content is visible), text on signs. Do not guess anyone's name or \
identity; describe people by appearance and role ("a young man in a black hoodie", "a group of \
friends"). Translate speech gists into English. Judge B-roll quality honestly.

Content roles (pick the ones that really fit, can be none):
- hook: funny, weird, shocking, surprising or crazy - would stop a scroll in the first 2 seconds
- cinematic: beautiful, stable, nice light or composition (sunsets, wide landscapes, slow walks)
- spectacle: something big or rare happening (fire show, festival, concert, cliff jump, storm)
- story_to_camera: the person filming talks to the camera / tells what happened (selfie vlog)
- funny, emotional, friends, food, action, calm, work (laptop/desk/notebook/meetings)
- establishing: shows where we are (arrival, city view, airport, signboard, hotel)
- transition: movement that bridges scenes (walking, plane window, vehicle POV, door, sky)
Story seeds: note anything a creator could tell a story about - an incident, a mishap, a scam,
a celebration, a conversation, a decision - especially when someone explains it to the camera."""


# ------------------------------------------------------------------ request building

def _transcript_text(conn, media_id: str, start: float, end: float) -> str:
    rows = conn.execute(
        "SELECT start, end, text FROM transcript WHERE media_id=? AND end>=? AND start<=? ORDER BY start",
        (media_id, start, end),
    ).fetchall()
    return "\n".join(f"[{fmt_ts(r['start'])}-{fmt_ts(r['end'])}] {r['text']}" for r in rows)


def _window_frames(cfg: Config, conn, media_id: str, window: int) -> list[tuple[float, Path]]:
    lo, hi = window * cfg.window_seconds, (window + 1) * cfg.window_seconds
    rows = conn.execute(
        "SELECT t, path FROM frames WHERE media_id=? AND t>=? AND t<? ORDER BY t", (media_id, lo, hi)
    ).fetchall()
    return [(r["t"], cfg.library_dir / r["path"]) for r in rows]


def _num_windows(cfg: Config, duration: float | None) -> int:
    return max(1, math.ceil((duration or 0) / cfg.window_seconds))


def video_params(cfg: Config, conn, media_id: str, window: int, model: str) -> dict:
    m = conn.execute("SELECT * FROM media WHERE id=?", (media_id,)).fetchone()
    frames = _window_frames(cfg, conn, media_id, window)
    if not frames:  # e.g. clip shorter than expected: use whatever frames exist
        frames = [(r["t"], cfg.library_dir / r["path"]) for r in
                  conn.execute("SELECT t, path FROM frames WHERE media_id=? ORDER BY t", (media_id,))]
    n = _num_windows(cfg, m["duration"])
    start = window * cfg.window_seconds
    end = min(m["duration"] or 0, start + cfg.window_seconds)
    cuts = [r["t"] for r in conn.execute(
        "SELECT t FROM cuts WHERE media_id=? AND t>=? AND t<? ORDER BY t", (media_id, start, end))]
    transcript = _transcript_text(conn, media_id, start, end)
    header = "\n".join([
        f"File: {m['relpath']}",
        f"Folder type: {'purpose-shot brand B-roll' if m['root_kind'] == 'brand' else 'life archive'}",
        f"Recorded: {m['taken_at']} (from {m['date_source']})",
        f"Place from GPS: {m['place'] or 'unknown'}",
        f"Duration: {fmt_ts(m['duration'])}, {m['width']}x{m['height']} {m['orientation'] or ''}",
        f"This request covers {start:.1f}s to {end:.1f}s (part {window + 1} of {n}).",
        f"Scene cuts detected at (s): {', '.join(f'{c:.1f}' for c in cuts[:60]) or 'none'}",
        "Transcript:",
        transcript or "(no speech detected)",
        f"\n{len(frames)} frames follow as {math.ceil(len(frames) / PER_SHEET)} contact sheet(s).",
    ])
    content: list[dict] = [{"type": "text", "text": header}]
    for sheet in sheets_for(frames):
        content.append({"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                                    "data": to_b64_jpeg(sheet)}})
    return _params(cfg, model, content, VIDEO_SCHEMA)


def photo_params(cfg: Config, conn, media_ids: list[str], model: str) -> dict:
    content: list[dict] = []
    for i, mid in enumerate(media_ids, 1):
        m = conn.execute("SELECT * FROM media WHERE id=?", (mid,)).fetchone()
        f = conn.execute("SELECT path FROM frames WHERE media_id=? LIMIT 1", (mid,)).fetchone()
        content.append({"type": "text", "text": (
            f"Photo {i}: {m['relpath']} | taken {m['taken_at']} ({m['date_source']}) | "
            f"place {m['place'] or 'unknown'} | {'brand B-roll' if m['root_kind'] == 'brand' else 'life archive'}")})
        data = (cfg.library_dir / f["path"]).read_bytes()
        content.append({"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                                    "data": base64.standard_b64encode(data).decode()}})
    content.append({"type": "text", "text": f"Describe each of the {len(media_ids)} photos."})
    return _params(cfg, model, content, PHOTO_SCHEMA)


def _params(cfg: Config, model: str, content: list[dict], schema: dict) -> dict:
    return {
        "model": model,
        "max_tokens": 16000,
        "system": SYSTEM,
        "messages": [{"role": "user", "content": content}],
        "output_config": {"effort": cfg.effort, "format": {"type": "json_schema", "schema": schema}},
    }


def plan_requests(cfg: Config, conn) -> int:
    """Create request rows for everything prepped + transcribed but not yet described."""
    created = 0
    queued = {r["custom_id"] for r in conn.execute("SELECT custom_id FROM requests")}
    for m in conn.execute(
        "SELECT id, duration FROM media WHERE kind='video' AND prepped=1 AND (transcribed=1 OR ?) AND described=0",
        (int(not cfg.transcribe_enabled),),
    ).fetchall():
        for w in range(_num_windows(cfg, m["duration"])):
            cid = f"v{m['id']}w{w}"
            if cid not in queued:
                conn.execute("INSERT INTO requests(custom_id, kind, payload) VALUES (?,?,?)",
                             (cid, "video", json.dumps({"media_id": m["id"], "window": w})))
                created += 1
    in_flight = set()
    for r in conn.execute("SELECT payload FROM requests WHERE kind='photos' AND status!='done'"):
        in_flight.update(json.loads(r["payload"])["media_ids"])
    photos = [r["id"] for r in conn.execute(
        "SELECT id FROM media WHERE kind='photo' AND prepped=1 AND described=0 ORDER BY taken_at")
        if r["id"] not in in_flight]
    for i in range(0, len(photos), PHOTOS_PER_REQUEST):
        group = photos[i:i + PHOTOS_PER_REQUEST]
        conn.execute("INSERT OR IGNORE INTO requests(custom_id, kind, payload) VALUES (?,?,?)",
                     (f"p{group[0]}n{len(group)}", "photos", json.dumps({"media_ids": group})))
        created += 1
    conn.commit()
    return created


def build_params(cfg: Config, conn, req, model: str) -> dict:
    payload = json.loads(req["payload"])
    if req["kind"] == "video":
        return video_params(cfg, conn, payload["media_id"], payload["window"], model)
    return photo_params(cfg, conn, payload["media_ids"], model)


# ------------------------------------------------------------------ results

def _clean_list(values) -> str:
    seen, out = set(), []
    for v in values or []:
        v = str(v).strip().lower()
        if v and v not in seen:
            seen.add(v)
            out.append(v)
    return json.dumps(out, ensure_ascii=False)


def store_result(cfg: Config, conn, req, data: dict) -> None:
    payload = json.loads(req["payload"])
    if req["kind"] == "video":
        mid, window = payload["media_id"], payload["window"]
        m = conn.execute("SELECT duration FROM media WHERE id=?", (mid,)).fetchone()
        lo = window * cfg.window_seconds
        hi = min(m["duration"] or lo + cfg.window_seconds, lo + cfg.window_seconds)
        conn.execute("DELETE FROM moments WHERE media_id=? AND start>=? AND start<?", (mid, lo, hi))
        for mo in sorted(data.get("moments", []), key=lambda x: x.get("start", 0)):
            start = max(lo, min(hi, float(mo.get("start", lo))))
            end = max(lo, min(hi, float(mo.get("end", start))))
            if end - start < 0.5:
                continue
            _insert_moment(conn, mid, start, end, mo)
        if window == 0:
            conn.execute("UPDATE media SET title=?, summary=?, tags=? WHERE id=?",
                         (data.get("title"), data.get("summary"), _clean_list(data.get("tags")), mid))
        elif data.get("summary"):
            conn.execute("UPDATE media SET summary=coalesce(summary,'') || ' ' || ? WHERE id=? "
                         "AND instr(coalesce(summary,''), ?) = 0", (data["summary"], mid, data["summary"]))
        conn.execute("UPDATE requests SET status='done', error=NULL WHERE custom_id=?", (req["custom_id"],))
        pending = conn.execute(
            "SELECT count(*) FROM requests WHERE kind='video' AND status!='done' AND payload LIKE ?",
            (f'%"{mid}"%',)).fetchone()[0]
        if pending == 0:
            conn.execute("UPDATE media SET described=1 WHERE id=?", (mid,))
        reindex_fts(conn, mid)
    else:
        ids = payload["media_ids"]
        for p in data.get("photos", []):
            idx = int(p.get("index", 0)) - 1
            if not 0 <= idx < len(ids):
                continue
            mid = ids[idx]
            conn.execute("DELETE FROM moments WHERE media_id=?", (mid,))
            _insert_moment(conn, mid, 0.0, 0.0, {**p, "camera_motion": "static", "energy": None, "speech_en": ""},
                           allow_zero=True)
            conn.execute("UPDATE media SET title=?, summary=?, tags=?, described=1 WHERE id=?",
                         (p.get("title"), p.get("description"), _clean_list(p.get("tags")), mid))
            reindex_fts(conn, mid)
        conn.execute("UPDATE requests SET status='done', error=NULL WHERE custom_id=?", (req["custom_id"],))


def _insert_moment(conn, mid, start, end, mo, allow_zero=False):
    def score(key, default=None):
        v = mo.get(key)
        try:
            return max(1, min(5, int(v))) if v is not None else default
        except (TypeError, ValueError):
            return default

    roles = [r for r in (mo.get("content_roles") or []) if r in ROLES]
    conn.execute(
        """INSERT INTO moments (media_id, start, end, description, action, setting, shot_type, camera_motion,
           people_count, mood, energy, quality_issues, broll_score, content_uses, tags, speech_en,
           roles, hook_score, story_seed, motion)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (mid, start, end, mo.get("description"), mo.get("action"), mo.get("setting"), mo.get("shot_type"),
         mo.get("camera_motion"), mo.get("people_count"), mo.get("mood"), mo.get("energy"),
         _clean_list(mo.get("quality_issues")), score("broll_score", 1),
         _clean_list(mo.get("content_uses")), _clean_list(mo.get("tags")), mo.get("speech_en") or "",
         _clean_list(roles), score("hook_score", 1), (mo.get("story_seed") or "").strip(), score("motion", 3)),
    )


def handle_message(cfg: Config, conn, req, message) -> str:
    """Store a Messages API response. Returns the request's new status."""
    if message.stop_reason == "refusal":
        conn.execute("UPDATE requests SET status='refused', error=? WHERE custom_id=?",
                     (getattr(message.stop_details, "category", None) or "refusal", req["custom_id"]))
        return "refused"
    if message.stop_reason == "max_tokens":
        conn.execute("UPDATE requests SET status='error', error='max_tokens' WHERE custom_id=?", (req["custom_id"],))
        return "error"
    text = next((b.text for b in message.content if b.type == "text"), "")
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        conn.execute("UPDATE requests SET status='error', error=? WHERE custom_id=?", (f"bad json: {e}", req["custom_id"]))
        return "error"
    store_result(cfg, conn, req, data)
    return "done"


# ------------------------------------------------------------------ commands

def _client():
    import anthropic

    return anthropic.Anthropic()


def estimate(cfg: Config, conn, model: str | None = None) -> dict:
    plan_requests(cfg, conn)
    model = model or cfg.model
    in_tok = out_tok = 0
    n = 0
    for req in conn.execute("SELECT * FROM requests WHERE status IN ('pending','error','refused')"):
        n += 1
        payload = json.loads(req["payload"])
        if req["kind"] == "video":
            frames = len(_window_frames(cfg, conn, payload["media_id"], payload["window"])) or 1
            tr = _transcript_text(conn, payload["media_id"], payload["window"] * cfg.window_seconds,
                                  (payload["window"] + 1) * cfg.window_seconds)
            m = conn.execute("SELECT duration FROM media WHERE id=?", (payload["media_id"],)).fetchone()
            span = min(cfg.window_seconds, max(1.0, (m["duration"] or 0) - payload["window"] * cfg.window_seconds))
            in_tok += 1100 + math.ceil(frames / PER_SHEET) * 1600 + len(tr) // 2
            out_tok += 900 + int(span / 8) * 170
        else:
            k = len(payload["media_ids"])
            in_tok += 1100 + k * 400
            out_tok += 700 + k * 200
    pin, pout = PRICES.get(model, PRICES["claude-opus-5-5"])
    usd_batch = (in_tok * pin + out_tok * pout) / 1e6 / 2
    return {"requests": n, "model": model, "input_tokens": in_tok, "output_tokens": out_tok,
            "usd_batch": round(usd_batch, 2), "usd_standard": round(usd_batch * 2, 2)}


def submit(cfg: Config, conn, limit: int | None = None, log=print) -> list[str]:
    from anthropic.types.message_create_params import MessageCreateParamsNonStreaming
    from anthropic.types.messages.batch_create_params import Request

    plan_requests(cfg, conn)
    reqs = conn.execute("SELECT * FROM requests WHERE status='pending' ORDER BY custom_id").fetchall()
    if limit:
        reqs = reqs[:limit]
    client = _client()
    batch_ids: list[str] = []
    chunk: list = []
    chunk_ids: list[str] = []
    size = 0

    def flush():
        nonlocal chunk, chunk_ids, size
        if not chunk:
            return
        batch = client.messages.batches.create(requests=chunk)
        conn.executemany("UPDATE requests SET status='submitted', batch_id=?, model=? WHERE custom_id=?",
                         [(batch.id, cfg.model, c) for c in chunk_ids])
        conn.commit()
        batch_ids.append(batch.id)
        log(f"[describe] submitted batch {batch.id} with {len(chunk)} requests")
        chunk, chunk_ids, size = [], [], 0

    for req in reqs:
        try:
            params = build_params(cfg, conn, req, cfg.model)
        except Exception as e:
            conn.execute("UPDATE requests SET status='error', error=? WHERE custom_id=?", (f"build: {e}", req["custom_id"]))
            continue
        est = len(json.dumps(params))
        if chunk and (len(chunk) >= MAX_REQUESTS_PER_BATCH or size + est > MAX_BATCH_BYTES):
            flush()
        chunk.append(Request(custom_id=req["custom_id"], params=MessageCreateParamsNonStreaming(**params)))
        chunk_ids.append(req["custom_id"])
        size += est
    flush()
    return batch_ids


def collect(cfg: Config, conn, log=print) -> dict:
    client = _client()
    stats = {"done": 0, "refused": 0, "error": 0, "still_running": 0}
    batch_ids = [r["batch_id"] for r in conn.execute(
        "SELECT DISTINCT batch_id FROM requests WHERE status='submitted' AND batch_id IS NOT NULL")]
    for bid in batch_ids:
        batch = client.messages.batches.retrieve(bid)
        if batch.processing_status != "ended":
            stats["still_running"] += 1
            log(f"[describe] {bid}: {batch.processing_status} ({batch.request_counts.processing} processing)")
            continue
        for result in client.messages.batches.results(bid):
            req = conn.execute("SELECT * FROM requests WHERE custom_id=?", (result.custom_id,)).fetchone()
            if req is None:
                continue
            if result.result.type == "succeeded":
                status = handle_message(cfg, conn, req, result.result.message)
            else:
                status = "error"
                err = result.result.type
                if result.result.type == "errored":
                    err = f"{result.result.error.type}"
                # expired / canceled / server errors go back to pending for the next submit
                conn.execute("UPDATE requests SET status=?, error=?, batch_id=NULL WHERE custom_id=?",
                             ("error" if "invalid" in err else "pending", err, result.custom_id))
            stats[status] = stats.get(status, 0) + 1
        conn.commit()
        log(f"[describe] collected {bid}")
    return stats


def run_sync(cfg: Config, conn, limit: int = 3, model: str | None = None, statuses=("pending",), log=print) -> dict:
    """Send requests one by one right now (standard price). For testing and for retries."""
    plan_requests(cfg, conn)
    model = model or cfg.model
    placeholders = ",".join("?" * len(statuses))
    reqs = conn.execute(
        f"SELECT * FROM requests WHERE status IN ({placeholders}) ORDER BY custom_id LIMIT ?", (*statuses, limit)
    ).fetchall()
    client = _client()
    stats: dict[str, int] = {}
    for req in reqs:
        params = build_params(cfg, conn, req, model)
        message = client.messages.create(**params)
        status = handle_message(cfg, conn, req, message)
        conn.execute("UPDATE requests SET model=? WHERE custom_id=?", (model, req["custom_id"]))
        conn.commit()
        stats[status] = stats.get(status, 0) + 1
        log(f"[describe] {req['custom_id']}: {status}")
    return stats
