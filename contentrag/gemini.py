"""Describe footage with Gemini: it watches a small 360p copy of each clip, audio included.

For each request window, ffmpeg makes a low-bitrate proxy (360p, 2 fps, mono audio) in a temp
folder. Small proxies go inline in the request; bigger ones use the Files API and are deleted
afterwards. The original footage never leaves the Mac. Results land in the same tables as
the Claude backend, so search and the vault work the same way.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from .config import Config
from .describe import PHOTO_SCHEMA, VIDEO_SCHEMA, plan_requests, store_result
from .util import fmt_ts, source_path

INLINE_LIMIT = 8 * 1024 * 1024  # bigger proxies go through the Files API (more reliable for long clips)
TOKENS_PER_SECOND = {"low": 100, "medium": 300, "high": 300}
# $/MTok (input, output) at standard rates.
PRICES = {
    "gemini-3.5-flash": (0.75, 4.50),
    "gemini-3.5-flash-lite": (0.30, 2.50),
}

GEMINI_SYSTEM = """You catalogue a content creator's personal footage archive so clips can be found \
later for Instagram reels and so an AI assistant can learn the story of their life. The creator is \
based in India (currently Bangalore); speech is mostly Hindi or Hinglish.

You receive a low-resolution copy of a video excerpt with its audio (or a few photos), plus file \
metadata. For video, split the excerpt into moments: contiguous segments that together cover the \
whole excerpt, breaking where the shot, place or action changes. Typical moments are 3-30 seconds; \
a long static talk or single continuous action may be one longer moment. start/end are seconds \
from the start of THIS EXCERPT, which begins at 0.

Be concrete and searchable: name objects, places, activities, time of day, weather, clothing, \
vehicles, screens (what app or content is visible), text on signs, sounds and music. Do not guess \
anyone's name or identity; describe people by appearance and role ("a young man in a black \
hoodie", "a group of friends"). Listen to the audio: put an English gist of the speech in \
speech_en and, when asked, a transcript in the original language (Hindi in Devanagari, English \
words as spoken). Judge B-roll quality honestly."""

TRANSCRIPT_PROP = {
    "type": "array",
    "items": {
        "type": "object",
        "properties": {
            "start": {"type": "number"},
            "end": {"type": "number"},
            "text": {"type": "string"},
        },
        "required": ["start", "end", "text"],
        "additionalProperties": False,
    },
    "description": "Speech transcript in the original language; empty if nobody speaks.",
}


def api_key(cfg: Config) -> str:
    key = os.environ.get(cfg.gemini_api_key_env)
    if not key:
        raise RuntimeError(
            f"Set the {cfg.gemini_api_key_env} environment variable to your Gemini API key "
            f"(export {cfg.gemini_api_key_env}=... in the terminal before starting crag)."
        )
    return key


def client(cfg: Config):
    from google import genai

    return genai.Client(api_key=api_key(cfg))


def list_models(cfg: Config) -> list[str]:
    # Keep a reference to the client while paging: genai.Client closes its HTTP connection
    # when garbage-collected, which breaks the lazy pager mid-iteration.
    gclient = client(cfg)
    models = list(gclient.models.list())
    return sorted(
        m.name.removeprefix("models/")
        for m in models
        if "generateContent" in (getattr(m, "supported_actions", None) or ["generateContent"])
    )


# ------------------------------------------------------------------ proxies

def make_proxy(src: Path, start: float, duration: float, out: Path, has_audio: bool) -> Path:
    cmd = ["ffmpeg", "-nostdin", "-y", "-v", "error", "-ss", f"{start:.2f}", "-t", f"{duration:.2f}",
           "-i", str(src),
           "-vf", "scale='if(gt(iw,ih),640,-2)':'if(gt(iw,ih),-2,640)',fps=2",
           "-c:v", "libx264", "-preset", "veryfast", "-crf", "32", "-pix_fmt", "yuv420p"]
    cmd += ["-c:a", "aac", "-b:a", "40k", "-ac", "1"] if has_audio else ["-an"]
    r = subprocess.run(cmd + ["-movflags", "+faststart", str(out)], capture_output=True, text=True)
    if r.returncode != 0 or not out.exists():
        raise RuntimeError(f"ffmpeg proxy failed: {r.stderr.strip()[-300:]}")
    return out


# ------------------------------------------------------------------ one request

def _config(cfg: Config, schema: dict):
    from google.genai import types

    return types.GenerateContentConfig(
        system_instruction=GEMINI_SYSTEM,
        response_mime_type="application/json",
        response_json_schema=schema,
        media_resolution=f"MEDIA_RESOLUTION_{cfg.gemini_resolution.upper()}",
        thinking_config=types.ThinkingConfig(thinking_level=cfg.gemini_thinking.upper()),
        max_output_tokens=65536,
    )


class GeminiError(RuntimeError):
    def __init__(self, message: str, code: int | None = None):
        super().__init__(message)
        self.code = code


def _explain(e) -> GeminiError:
    code = getattr(e, "code", None)
    status = getattr(e, "status", "") or ""
    msg = (getattr(e, "message", None) or str(e)).strip().replace("\n", " ")[:300]
    hint = ""
    if code == 429:
        hint = (" — Gemini quota / rate limit. If the key's Google project has no billing enabled it is on the"
                " free tier, which allows only a few requests per day: enable billing in AI Studio, or wait.")
    elif code in (500, 503, 504):
        hint = " — Gemini was overloaded or timed out; usually works when retried later."
    elif code in (401, 403):
        hint = " — the API key was rejected or lacks access to this model."
    elif code == 404:
        hint = " — model not found; use 'Check key & models' and set [gemini] model in contentrag.toml."
    return GeminiError(f"Gemini {code} {status}: {msg}{hint}", code)


def _call(gclient, cfg: Config, contents, schema: dict) -> tuple[dict | None, str, dict]:
    """Returns (data, status, usage); usage["error"] explains a non-'done' status.
    Retries rate limits and server errors with backoff, then raises GeminiError."""
    from google.genai import errors

    delay = 5.0
    for attempt in range(5):
        try:
            resp = gclient.models.generate_content(model=cfg.gemini_model, contents=contents,
                                                   config=_config(cfg, schema))
            break
        except errors.APIError as e:
            code = getattr(e, "code", None)
            if code in (429, 500, 502, 503, 504) and attempt < 4:
                time.sleep(delay * (3 if code == 429 else 1))
                delay *= 2
                continue
            raise _explain(e) from e
    um = resp.usage_metadata
    usage = {
        "in": getattr(um, "prompt_token_count", 0) or 0,
        "out": (getattr(um, "candidates_token_count", 0) or 0) + (getattr(um, "thoughts_token_count", 0) or 0),
    }
    block = getattr(resp.prompt_feedback, "block_reason", None) if resp.prompt_feedback is not None else None
    if block:
        return None, "refused", {**usage, "error": f"blocked by Gemini: {getattr(block, 'name', block)}"}
    cand = resp.candidates[0] if resp.candidates else None
    reason = getattr(getattr(cand, "finish_reason", None), "name", "") if cand else "NO_CANDIDATE"
    if reason in ("SAFETY", "PROHIBITED_CONTENT", "BLOCKLIST", "SPII", "IMAGE_SAFETY", "IMAGE_PROHIBITED_CONTENT"):
        return None, "refused", {**usage, "error": f"blocked by Gemini: {reason}"}
    if reason == "MAX_TOKENS":
        return None, "error", {**usage, "error": "answer too long (MAX_TOKENS)", "max_tokens": True}
    try:
        return json.loads(resp.text or ""), "done", usage
    except json.JSONDecodeError:
        return None, "error", {**usage, "error": f"invalid JSON in answer (finish reason {reason or 'unknown'})"}


def _upload(gclient, proxy: Path):
    uploaded = gclient.files.upload(file=str(proxy), config={"mime_type": "video/mp4"})
    waited = 0
    while uploaded.state.name == "PROCESSING" and waited < 600:
        time.sleep(5)
        waited += 5
        uploaded = gclient.files.get(name=uploaded.name)
    if uploaded.state.name != "ACTIVE":
        raise GeminiError(f"Gemini could not process the uploaded video (state {uploaded.state.name})")
    return uploaded


def _merge_usage(a: dict, b: dict) -> dict:
    return {**b, "in": a.get("in", 0) + b.get("in", 0), "out": a.get("out", 0) + b.get("out", 0)}


def _video_job(gclient, cfg: Config, spec: dict, tmp: Path) -> tuple[dict | None, str, dict]:
    from google.genai import types

    proxy = make_proxy(spec["src"], spec["start"], spec["end"] - spec["start"],
                       tmp / f"{spec['custom_id']}.mp4", spec["has_audio"])
    uploaded = None
    try:
        vmeta = types.VideoMetadata(fps=cfg.gemini_fps)

        def file_part():
            nonlocal uploaded
            if uploaded is None:
                uploaded = _upload(gclient, proxy)
            return types.Part(file_data=types.FileData(file_uri=uploaded.uri, mime_type="video/mp4"),
                              video_metadata=vmeta)

        def schema_for(transcript: bool) -> dict:
            if not transcript:
                return VIDEO_SCHEMA
            schema = json.loads(json.dumps(VIDEO_SCHEMA))
            schema["properties"]["transcript"] = TRANSCRIPT_PROP
            schema["required"] = [*schema["required"], "transcript"]
            return schema

        want_tr = spec["want_transcript"]
        header = spec["header"]
        if proxy.stat().st_size <= INLINE_LIMIT:
            part = types.Part(inline_data=types.Blob(data=proxy.read_bytes(), mime_type="video/mp4"),
                              video_metadata=vmeta)
            try:
                data, status, usage = _call(gclient, cfg, [part, header], schema_for(want_tr))
            except GeminiError as e:
                if e.code in (429, 401, 403, 404):
                    raise
                # inline video rejected or timed out: try the Files API route once
                part = file_part()
                data, status, usage = _call(gclient, cfg, [part, header], schema_for(want_tr))
        else:
            part = file_part()
            data, status, usage = _call(gclient, cfg, [part, header], schema_for(want_tr))
        if usage.get("max_tokens") and want_tr:
            # long talky clip: ask again without the verbatim transcript (speech_en gists remain)
            header = header.replace("Also return `transcript`.", "")
            data, status, usage2 = _call(gclient, cfg, [part, header], schema_for(False))
            usage = _merge_usage(usage, usage2)
        return data, status, usage
    finally:
        proxy.unlink(missing_ok=True)
        if uploaded is not None:
            try:
                gclient.files.delete(name=uploaded.name)
            except Exception:
                pass


def _photo_job(gclient, cfg: Config, spec: dict) -> tuple[dict | None, str, dict]:
    from google.genai import types

    contents: list = []
    for i, p in enumerate(spec["photos"], 1):
        contents.append(f"Photo {i}: {p['label']}")
        contents.append(types.Part.from_bytes(data=p["path"].read_bytes(), mime_type="image/jpeg"))
    contents.append(f"Describe each of the {len(spec['photos'])} photos.")
    return _call(gclient, cfg, contents, PHOTO_SCHEMA)


# ------------------------------------------------------------------ planning (main thread)

def _specs(cfg: Config, conn, reqs) -> list[dict]:
    specs = []
    for req in reqs:
        payload = json.loads(req["payload"])
        if req["kind"] == "video":
            m = conn.execute("SELECT * FROM media WHERE id=?", (payload["media_id"],)).fetchone()
            src = source_path(cfg, conn, m["id"])
            if src is None:
                continue  # drive not mounted; stays pending
            start = payload["window"] * cfg.window_seconds
            end = min(m["duration"] or 0, start + cfg.window_seconds)
            if end - start < 0.2:
                end = m["duration"] or start + 1
            n = max(1, -(-int(m["duration"] or 0) // int(cfg.window_seconds)))
            has_tr = conn.execute("SELECT 1 FROM transcript WHERE media_id=? LIMIT 1", (m["id"],)).fetchone()
            header = "\n".join([
                f"File: {m['relpath']}",
                f"Folder type: {'purpose-shot brand B-roll' if m['root_kind'] == 'brand' else 'life archive'}",
                f"Recorded: {m['taken_at']} (from {m['date_source']})",
                f"Place from GPS: {m['place'] or 'unknown'}",
                f"Original: {m['width']}x{m['height']} {m['orientation'] or ''}, total length {fmt_ts(m['duration'])}",
                f"This excerpt is {fmt_ts(start)}-{fmt_ts(end)} of the file (part {payload['window'] + 1} of {n}); "
                "give start/end relative to the excerpt (it starts at 0).",
                "Also return `transcript`." if not has_tr and m["has_audio"] else "",
            ])
            specs.append({"custom_id": req["custom_id"], "req": req, "kind": "video", "src": src,
                          "start": start, "end": end, "offset": start, "has_audio": bool(m["has_audio"]),
                          "want_transcript": bool(m["has_audio"] and not has_tr), "header": header,
                          "label": f"{m['relpath']}" + (f" (part {payload['window'] + 1})" if n > 1 else ""),
                          "media_id": m["id"]})
        else:
            photos = []
            for mid in payload["media_ids"]:
                m = conn.execute("SELECT * FROM media WHERE id=?", (mid,)).fetchone()
                f = conn.execute("SELECT path FROM frames WHERE media_id=? LIMIT 1", (mid,)).fetchone()
                if f is None:
                    break
                photos.append({"path": cfg.library_dir / f["path"], "label": (
                    f"{m['relpath']} | taken {m['taken_at']} ({m['date_source']}) | place {m['place'] or 'unknown'} | "
                    f"{'brand B-roll' if m['root_kind'] == 'brand' else 'life archive'}")})
            else:
                specs.append({"custom_id": req["custom_id"], "req": req, "kind": "photos", "photos": photos})
    return specs


def _shift(data: dict, offset: float) -> dict:
    for key in ("moments", "transcript"):
        for item in data.get(key, []) or []:
            item["start"] = float(item.get("start", 0)) + offset
            item["end"] = float(item.get("end", item["start"])) + offset
    return data


def estimate(cfg: Config, conn) -> dict:
    plan_requests(cfg, conn)
    seconds = photos = n = 0
    for req in conn.execute("SELECT * FROM requests WHERE status IN ('pending','error')"):
        n += 1
        payload = json.loads(req["payload"])
        if req["kind"] == "video":
            m = conn.execute("SELECT duration FROM media WHERE id=?", (payload["media_id"],)).fetchone()
            seconds += max(0.0, min(cfg.window_seconds, (m["duration"] or 0) - payload["window"] * cfg.window_seconds))
        else:
            photos += len(payload["media_ids"])
    tps = TOKENS_PER_SECOND.get(cfg.gemini_resolution, 100) * cfg.gemini_fps + 32  # + audio
    in_tok = seconds * tps + n * 900 + photos * 260
    out_tok = seconds / 8 * 170 + seconds * 4 + n * 600 + photos * 200  # moments + transcript + thinking
    pin, pout = PRICES.get(cfg.gemini_model, PRICES["gemini-3.5-flash"])
    return {"requests": n, "model": cfg.gemini_model, "video_hours": round(seconds / 3600, 1), "photos": photos,
            "input_tokens": int(in_tok), "output_tokens": int(out_tok),
            "usd": round((in_tok * pin + out_tok * pout) / 1e6, 2)}


def spent(cfg: Config, conn) -> dict:
    row = conn.execute("SELECT coalesce(sum(in_tokens),0) i, coalesce(sum(out_tokens),0) o FROM requests "
                       "WHERE model=?", (cfg.gemini_model,)).fetchone()
    pin, pout = PRICES.get(cfg.gemini_model, PRICES["gemini-3.5-flash"])
    return {"input_tokens": row["i"], "output_tokens": row["o"], "usd": round((row["i"] * pin + row["o"] * pout) / 1e6, 2)}


# ------------------------------------------------------------------ run

def run(cfg: Config, conn, limit: int | None = None, retry: bool = False, log=print,
        stop: threading.Event | None = None) -> dict:
    """Describe everything pending (or, with retry=True, what failed before). Resumable."""
    plan_requests(cfg, conn)
    statuses = ("error", "refused") if retry else ("pending",)
    reqs = conn.execute(
        f"SELECT * FROM requests WHERE status IN ({','.join('?' * len(statuses))}) ORDER BY custom_id",
        statuses).fetchall()
    if limit:
        reqs = reqs[:limit]
    specs = _specs(cfg, conn, reqs)
    stats = {"done": 0, "refused": 0, "error": 0, "skipped_unmounted": len(reqs) - len(specs)}
    if not specs:
        return stats
    gclient = client(cfg)
    log(f"[gemini] {len(specs)} requests with {cfg.gemini_model}, {cfg.gemini_workers} at a time")
    with tempfile.TemporaryDirectory(prefix="crag-") as tmpdir, \
            ThreadPoolExecutor(max_workers=cfg.gemini_workers) as pool:
        tmp = Path(tmpdir)
        futures = {}
        it = iter(specs)
        quota_hits = 0

        def submit_next():
            if (stop is not None and stop.is_set()) or quota_hits >= 3:
                return
            spec = next(it, None)
            if spec is None:
                return
            fn = (lambda s=spec: _video_job(gclient, cfg, s, tmp)) if spec["kind"] == "video" \
                else (lambda s=spec: _photo_job(gclient, cfg, s))
            futures[pool.submit(fn)] = spec

        for _ in range(cfg.gemini_workers * 2):
            submit_next()
        done = 0
        while futures:
            fut = next(as_completed(list(futures)))
            spec = futures.pop(fut)
            cid = spec["custom_id"]
            label = spec.get("label", cid)
            err = None
            try:
                data, status, usage = fut.result()
                err = usage.get("error")
            except Exception as e:
                data, status, usage = None, "error", {"in": 0, "out": 0}
                err = str(e)
                if getattr(e, "code", None) == 429:
                    quota_hits += 1
            conn.execute("UPDATE requests SET model=?, in_tokens=coalesce(in_tokens,0)+?, "
                         "out_tokens=coalesce(out_tokens,0)+? WHERE custom_id=?",
                         (cfg.gemini_model, usage["in"], usage["out"], cid))
            if status == "done":
                try:
                    _store(cfg, conn, spec, data)
                except Exception as e:
                    status, err = "error", f"could not save the answer: {e}"
            if status != "done":
                conn.execute("UPDATE requests SET status=?, error=? WHERE custom_id=?", (status, err, cid))
            conn.commit()
            stats[status] += 1
            done += 1
            if status != "done":
                log(f"[gemini] {done}/{len(specs)} {label}: {status} — {err}")
            elif done % 10 == 0 or done == len(specs):
                log(f"[gemini] {done}/{len(specs)} done — spent so far ${spent(cfg, conn)['usd']}")
            if quota_hits >= 3:
                log("[gemini] stopping: Gemini keeps answering 'quota exceeded' (429). Enable billing on the "
                    "key's Google project (free tier = few requests/day) or wait, then click Retry failed.")
                for f in futures:
                    f.cancel()
                break
            submit_next()
    if stop is not None and stop.is_set():
        log("[gemini] stopped; run again to continue where it left off")
    return stats


def _store(cfg: Config, conn, spec: dict, data: dict) -> None:
    req = spec["req"]
    if spec["kind"] == "video":
        data = _shift(data, spec["offset"])
        segs = [s for s in data.get("transcript", []) or [] if (s.get("text") or "").strip()]
        if segs:
            conn.execute("DELETE FROM transcript WHERE media_id=? AND start>=? AND start<?",
                         (spec["media_id"], spec["start"], spec["end"]))
            conn.executemany("INSERT INTO transcript VALUES (?,?,?,?)",
                             [(spec["media_id"], s["start"], s["end"], s["text"].strip()) for s in segs])
    store_result(cfg, conn, req, data)
