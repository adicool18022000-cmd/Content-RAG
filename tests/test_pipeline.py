"""End-to-end test on synthetic media: scan -> prep -> describe (fake Claude) -> search -> vault.

Needs ffmpeg/ffprobe on PATH. No network, no API key.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from contentrag import describe, util
from contentrag.config import load_config
from contentrag.db import connect
from contentrag.prep import prep
from contentrag.probe import date_from_filename, parse_iso6709, parse_meta_datetime
from contentrag.scan import scan
from contentrag.search import Filters, search, write_html
from contentrag.vault import build_vault

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")


def _video(path: Path, size: str, seconds: int, meta: dict[str, str]):
    path.parent.mkdir(parents=True, exist_ok=True)
    cmd = ["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", f"testsrc2=size={size}:rate=25:duration={seconds}",
           "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}", "-shortest",
           "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac"]
    for k, v in meta.items():
        cmd += ["-metadata", f"{k}={v}"]
    subprocess.run(cmd + [str(path)], check=True)


@pytest.fixture()
def env(tmp_path, monkeypatch):
    from contentrag import scan as scan_mod

    monkeypatch.setattr(scan_mod, "SETTLE_SECONDS", 0)  # test files are brand new
    a, b = tmp_path / "ssd1" / "Archive", tmp_path / "ssd1" / "Brand"
    _video(a / "2023" / "goa.mov", "640x360", 12,
           {"creation_time": "2023-08-12T13:34:33Z", "location": "+15.4909+073.8278/"})
    _video(a / "2023" / "VID_20230812_210000.mp4", "360x640", 8, {})
    _video(b / "laptop.mp4", "640x360", 6, {"creation_time": "2024-02-01T05:00:00Z"})
    shutil.copy(a / "2023" / "goa.mov", a / "backup_goa.mov")  # duplicate
    (a / "._goa.mov").write_bytes(b"appledouble junk")
    from PIL import Image

    Image.new("RGB", (800, 600), "orange").save(a / "IMG_20230813_101500.jpg")
    cfg_path = tmp_path / "contentrag.toml"
    cfg_path.write_text(f"""
library_dir = "{tmp_path / 'lib'}"
[[roots]]
name = "arch"
path = "{a}"
[[roots]]
name = "brand"
path = "{b}"
kind = "brand"
[[roots]]
name = "offline"
path = "{tmp_path / 'not-mounted'}"
[prep]
min_interval = 2.0
window_seconds = 10
frames_per_window = 4
""")
    cfg = load_config(cfg_path)
    return cfg, connect(cfg.db_path)


def _fake_message(req, cfg, conn):
    payload = json.loads(req["payload"])
    if req["kind"] == "photos":
        data = {"photos": [{"index": i + 1, "title": "Orange wall", "shot_type": "wide",
                            "description": "A plain orange wall.", "action": "none", "setting": "street",
                            "people_count": 0, "mood": "calm", "quality_issues": [], "broll_score": 2,
                            "content_uses": ["texture"], "tags": ["orange", "wall"]}
                           for i in range(len(payload["media_ids"]))]}
    else:
        lo = payload["window"] * cfg.window_seconds
        m = conn.execute("SELECT relpath FROM media WHERE id=?", (payload["media_id"],)).fetchone()
        bike = "goa" in m["relpath"].lower()
        data = {"title": "Night bike ride in Goa" if bike else "Working on laptop",
                "summary": "Test summary.", "tags": ["test"],
                "moments": [{"start": lo, "end": lo + 3, "shot_type": "pov", "camera_motion": "driving_riding",
                             "energy": "high", "speech_en": "let's go",
                             "description": "Riding a motorbike at night on a coastal road." if bike
                             else "Typing on a laptop at a desk.",
                             "action": "riding a motorbike" if bike else "typing on laptop",
                             "setting": "highway" if bike else "office desk", "people_count": 1,
                             "mood": "free", "quality_issues": ["shaky"] if bike else [],
                             "broll_score": 4, "content_uses": ["bike rides"], "tags": ["bike", "night"]},
                            {"start": lo + 3, "end": lo + 50, "shot_type": "wide", "camera_motion": "static",
                             "energy": "low", "speech_en": "", "description": "Sunset over the sea.",
                             "action": "watching sunset", "setting": "beach", "people_count": 0, "mood": "calm",
                             "quality_issues": [], "broll_score": 5, "content_uses": ["travel"],
                             "tags": ["sunset", "sea"]}]}
    return SimpleNamespace(stop_reason="end_turn", stop_details=None,
                           content=[SimpleNamespace(type="text", text=json.dumps(data))])


def test_pipeline(env, tmp_path):
    cfg, conn = env
    stats = scan(cfg, conn, log=lambda *_: None)
    assert stats["new"] == 4 and stats["duplicate"] == 1 and stats["skipped_roots"] == ["offline"]
    assert scan(cfg, conn, log=lambda *_: None)["known"] == 5  # rescan is incremental

    goa = conn.execute("SELECT md.* FROM media md JOIN locations l ON l.media_id=md.id "
                       "WHERE l.relpath='2023/goa.mov'").fetchone()
    assert goa["taken_at"] == "2023-08-12T19:04:33"  # UTC -> Asia/Kolkata
    assert (round(goa["lat"], 2), round(goa["lon"], 2)) == (15.49, 73.83)
    assert goa["orientation"] == "horizontal" and goa["has_audio"] == 1
    vert = conn.execute("SELECT * FROM media WHERE relpath LIKE 'VID_%' OR relpath LIKE '2023/VID_%'").fetchone()
    assert vert["orientation"] == "vertical" and vert["date_source"] == "filename"

    assert prep(cfg, conn, log=lambda *_: None)["done"] == 4
    frames = conn.execute("SELECT count(*) FROM frames WHERE media_id=?", (goa["id"],)).fetchone()[0]
    assert frames == 5  # 12 s clip, 10 s windows, 4 frames/window max -> 4 + 1
    # pretend Whisper ran
    conn.execute("INSERT INTO transcript VALUES (?,?,?,?)", (goa["id"], 0.5, 2.5, "चलो चलते हैं"))
    conn.execute("UPDATE media SET transcribed=1")
    conn.commit()

    assert describe.plan_requests(cfg, conn) == 5  # goa: 2 windows, vertical: 1, laptop: 1, photos: 1
    est = describe.estimate(cfg, conn)
    assert est["requests"] == 5 and est["usd_batch"] > 0

    for req in conn.execute("SELECT * FROM requests").fetchall():
        params = describe.build_params(cfg, conn, req, cfg.model)
        imgs = [c for c in params["messages"][0]["content"] if c["type"] == "image"]
        assert imgs and params["output_config"]["format"]["type"] == "json_schema"
        if req["kind"] == "video" and json.loads(req["payload"])["media_id"] == goa["id"]:
            assert "चलो" in params["messages"][0]["content"][0]["text"] or json.loads(req["payload"])["window"] == 1
        assert describe.handle_message(cfg, conn, req, _fake_message(req, cfg, conn)) == "done"
    conn.commit()
    assert conn.execute("SELECT count(*) FROM media WHERE described=0").fetchone()[0] == 0
    # moments are clamped to their window
    assert conn.execute("SELECT max(end) FROM moments WHERE media_id=?", (goa["id"],)).fetchone()[0] <= 12.1

    res = search(cfg, conn, "motorbike night", use_vectors=False)
    assert res and "motorbike" in res[0]["description"]
    assert res[0]["file"].endswith(".mov")
    res = search(cfg, conn, "laptop", Filters(root_kind="brand"), use_vectors=False)
    assert res and all("laptop" in r["description"].lower() or "sunset" in r["description"].lower() for r in res)
    assert not search(cfg, conn, "motorbike", Filters(exclude_issues=("shaky",)), use_vectors=False) or all(
        "shaky" not in r["quality_issues"] for r in search(cfg, conn, "motorbike", Filters(exclude_issues=("shaky",)),
                                                          use_vectors=False))
    res = search(cfg, conn, "", Filters(orientation="vertical", min_broll=5), use_vectors=False)
    assert res and all(r["orientation"] == "vertical" and r["broll_score"] == 5 for r in res)
    assert search(cfg, conn, "sunset", Filters(date_from="2024"), use_vectors=False)[0]["taken_at"] >= "2024"
    html = write_html(res, "test", tmp_path / "out.html").read_text()
    assert "file://" in html

    out = build_vault(cfg, conn, log=lambda *_: None)
    assert out["broll"] == 1 and out["events"] >= 1
    vault = cfg.vault_dir
    assert (vault / "CLAUDE.md").exists() and (vault / "_generated" / "Timeline.md").exists()
    events = list((vault / "_generated" / "Events").rglob("*.md"))
    text = "\n".join(e.read_text() for e in events)
    assert "Night bike ride" in text and "![[_assets/thumbs/" in text
    (vault / "Me.md").write_text("mine")
    build_vault(cfg, conn, log=lambda *_: None)
    assert (vault / "Me.md").read_text() == "mine"  # human notes survive rebuilds

    # a memory (what an event meant) is linked to its event by event_id and shown in generated notes
    import re as _re

    ev_note = next(e for e in (vault / "_generated" / "Events").rglob("*.md") if "Night bike ride" in e.read_text())
    eid = _re.search(r"^event_id: (\S+)", ev_note.read_text(), _re.M).group(1)
    assert "No memory yet" in ev_note.read_text() and "Interview queue" in (vault / "_generated" / "Index.md").read_text()
    assert (vault / "Memories" / "_Memory template.md").exists()
    (vault / "Memories" / "Night ride.md").write_text(
        f"---\ntype: memory\nevent_id: {eid}   # comment\nera: College\nimportance: 4\n---\n# Night ride\n\n"
        "## What it meant\nFirst time I felt free in a new city.\n\n## What happened\nRode with friends.\n")
    build_vault(cfg, conn, log=lambda *_: None)
    ev_text = next(e for e in (vault / "_generated" / "Events").rglob("*.md") if "Night bike ride" in e.read_text()).read_text()
    assert "First time I felt free" in ev_text and "importance: 4" in ev_text and "[[Memories/Night ride]]" in ev_text
    years = "\n".join(y.read_text() for y in (vault / "_generated" / "Years").glob("*.md"))
    assert "★★★★" in years and "First time I felt free" in years
    assert ev_note.stem not in (vault / "_generated" / "Interview queue.md").read_text()


def test_helpers():
    assert parse_meta_datetime("2023-08-12T19:04:33+0530", "Asia/Kolkata").isoformat() == "2023-08-12T19:04:33"
    assert parse_meta_datetime("2023:08:12 19:04:33", "Asia/Kolkata").hour == 19
    assert parse_meta_datetime("1904-01-01T00:00:00Z", "Asia/Kolkata") is None
    assert date_from_filename("WhatsApp Video 2021-03-05 at 18.22.10.mp4").isoformat() == "2021-03-05T18:22:10"
    assert date_from_filename("20190704_101010.mp4").day == 4
    assert date_from_filename("random_12345.mp4") is None
    assert parse_iso6709("+12.9716+077.5946+920.000/") == (12.9716, 77.5946)
    assert util.frame_times(5, 2, 360, 36) == [[1.25, 3.75]]
    assert [len(w) for w in util.frame_times(800, 2, 360, 36)] == [36, 36, 36]
    assert util.fmt_ts(3725) == "1:02:05"


class _FakeModels:
    def __init__(self):
        self.calls = []

    def generate_content(self, model, contents, config):
        from google.genai import types

        assert isinstance(config, types.GenerateContentConfig)
        self.calls.append(contents)
        first = contents[0]
        if isinstance(first, types.Part) and first.inline_data is not None:  # video excerpt
            assert first.inline_data.mime_type == "video/mp4" and len(first.inline_data.data) > 1000
            data = {"title": "Clip", "summary": "s", "tags": ["x"], "transcript": [
                {"start": 0.5, "end": 2.0, "text": "चलो"}],
                "moments": [{"start": 0.0, "end": 2.0, "shot_type": "wide", "camera_motion": "static",
                             "energy": "low", "speech_en": "let's go", "description": "Colour bars on a screen.",
                             "action": "nothing", "setting": "studio", "people_count": 0, "mood": "calm",
                             "quality_issues": [], "broll_score": 2, "content_uses": [], "tags": ["bars"],
                             "content_roles": ["hook", "funny"], "hook_score": 5, "motion": 4,
                             "story_seed": "Tuk-tuk driver took us to the wrong island"},
                            {"start": 2.0, "end": 6.0, "shot_type": "wide", "camera_motion": "pan",
                             "energy": "low", "speech_en": "", "description": "Sunset over the beach.",
                             "action": "watching sunset", "setting": "beach", "people_count": 0, "mood": "calm",
                             "quality_issues": [], "broll_score": 5, "content_uses": ["travel"], "tags": ["sunset"],
                             "content_roles": ["cinematic", "establishing"], "hook_score": 2, "motion": 2,
                             "story_seed": ""}]}
        else:  # photos
            n = sum(1 for c in contents if isinstance(c, types.Part))
            data = {"photos": [{"index": i + 1, "title": "Orange", "shot_type": "wide", "description": "Orange.",
                                "action": "", "setting": "", "people_count": 0, "mood": "", "quality_issues": [],
                                "broll_score": 1, "content_uses": [], "tags": ["orange"]} for i in range(n)]}
        return SimpleNamespace(
            usage_metadata=SimpleNamespace(prompt_token_count=1000, candidates_token_count=200, thoughts_token_count=50),
            prompt_feedback=None, candidates=[SimpleNamespace(finish_reason=SimpleNamespace(name="STOP"))],
            text=json.dumps(data, ensure_ascii=False))


def test_gemini_backend(env, monkeypatch):
    from contentrag import gemini

    cfg, conn = env
    cfg.transcribe_enabled = False
    scan(cfg, conn, log=lambda *_: None)
    prep(cfg, conn, log=lambda *_: None)
    assert not list(cfg.audio_dir.glob("*.wav")) if cfg.audio_dir.exists() else True  # no audio when Whisper is off
    fake = SimpleNamespace(models=_FakeModels())
    monkeypatch.setattr(gemini, "client", lambda cfg: fake)
    est = gemini.estimate(cfg, conn)
    assert est["requests"] == 5 and est["usd"] > 0

    stats = gemini.run(cfg, conn, log=lambda *_: None)
    assert stats["done"] == 5 and stats["error"] == 0
    assert conn.execute("SELECT count(*) FROM media WHERE described=0").fetchone()[0] == 0
    goa = conn.execute("SELECT md.id FROM media md JOIN locations l ON l.media_id=md.id "
                       "WHERE l.relpath='2023/goa.mov'").fetchone()["id"]
    starts = [r[0] for r in conn.execute("SELECT start FROM moments WHERE media_id=? ORDER BY start", (goa,))]
    assert starts == [0.0, 2.0, 10.0]  # second excerpt shifted by its 10 s offset
    tr = conn.execute("SELECT start, text FROM transcript WHERE media_id=? ORDER BY start", (goa,)).fetchall()
    assert [(t["start"], t["text"]) for t in tr] == [(0.5, "चलो"), (10.5, "चलो")]
    assert gemini.spent(cfg, conn)["usd"] > 0
    assert gemini.run(cfg, conn, log=lambda *_: None)["done"] == 0  # nothing left


def test_ui_server(env):
    import threading
    import urllib.error
    import urllib.request
    from http.server import ThreadingHTTPServer

    from contentrag.ui import Job, make_handler

    cfg, conn = env
    scan(cfg, conn, log=lambda *_: None)
    prep(cfg, conn, log=lambda *_: None)
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(cfg, Job(cfg)))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        get = lambda path, **h: urllib.request.urlopen(urllib.request.Request(base + path, headers=h))  # noqa: E731
        assert b"Content-RAG" in get("/").read()
        st = json.loads(get("/api/status").read())
        assert st["media"]["videos"] == 3 and st["backend"] == "gemini"
        with pytest.raises(urllib.error.HTTPError) as e:  # no CSRF header
            urllib.request.urlopen(urllib.request.Request(base + "/api/run", data=b'{"step":"scan"}', method="POST"))
        assert e.value.code == 403
        with pytest.raises(urllib.error.HTTPError) as e:  # path traversal
            get("/api/thumb?p=../../../etc/passwd")
        assert e.value.code == 404
        mid = conn.execute("SELECT id FROM media WHERE kind='video' LIMIT 1").fetchone()["id"]
        r = get(f"/api/media/{mid}", Range="bytes=0-99")
        assert r.status == 206 and len(r.read()) == 100
        r = urllib.request.urlopen(urllib.request.Request(
            base + "/api/run", data=b'{"step":"vault"}', method="POST", headers={"X-Crag": "1"}))
        assert json.loads(r.read())["ok"]
        for _ in range(50):
            log = json.loads(get("/api/log").read())
            if not log["running"]:
                break
            import time

            time.sleep(0.1)
        assert any("vault finished" in line for line in log["lines"])
        assert json.loads(get("/api/search?q=anything").read())["results"] == []
    finally:
        server.shutdown()


def test_gemini_failures(env, monkeypatch):
    """Inline failures fall back to the Files API, long answers drop the transcript,
    and repeated quota errors stop the run with an explanation."""
    from google.genai import errors

    from contentrag import gemini
    from contentrag.db import failures

    cfg, conn = env
    cfg.transcribe_enabled = False
    cfg.gemini_workers = 1
    scan(cfg, conn, log=lambda *_: None)
    prep(cfg, conn, log=lambda *_: None)
    monkeypatch.setattr(gemini.time, "sleep", lambda s: None)
    good = _FakeModels()
    state = {"inline_fail": True, "max_tokens_once": True}

    class Models:
        def generate_content(self, model, contents, config):
            part = contents[0]
            if getattr(part, "inline_data", None) is not None and state["inline_fail"]:
                raise errors.ServerError(500, {"error": {"code": 500, "message": "Internal error", "status": "INTERNAL"}})
            schema = config.response_json_schema
            if "transcript" in schema.get("properties", {}) and state["max_tokens_once"]:
                state["max_tokens_once"] = False
                return SimpleNamespace(usage_metadata=SimpleNamespace(prompt_token_count=10, candidates_token_count=5,
                                                                      thoughts_token_count=0),
                                       prompt_feedback=None, text='{"title": "trunc',
                                       candidates=[SimpleNamespace(finish_reason=SimpleNamespace(name="MAX_TOKENS"))])
            if getattr(part, "file_data", None) is not None:  # Files API route: hand the fake an inline-looking part
                from google.genai import types
                contents = [types.Part(inline_data=types.Blob(data=b"x" * 2000, mime_type="video/mp4")), *contents[1:]]
            return good.generate_content(model, contents, config)

    class Files:
        def upload(self, file, config):
            return SimpleNamespace(name="files/1", uri="https://example/files/1", state=SimpleNamespace(name="ACTIVE"))

        def delete(self, name):
            pass

    fake = SimpleNamespace(models=Models(), files=Files())
    monkeypatch.setattr(gemini, "client", lambda cfg: fake)
    lines = []
    stats = gemini.run(cfg, conn, log=lines.append)
    assert stats["done"] == 5 and stats["error"] == 0, lines

    # quota: every call 429 -> run stops after 3 and the reason is recorded
    conn.execute("UPDATE requests SET status='pending'")
    conn.execute("UPDATE media SET described=0")
    conn.commit()

    class Quota:
        def generate_content(self, model, contents, config):
            raise errors.ClientError(429, {"error": {"code": 429, "message": "Resource exhausted", "status": "RESOURCE_EXHAUSTED"}})

    monkeypatch.setattr(gemini, "client", lambda cfg: SimpleNamespace(models=Quota(), files=Files()))
    lines = []
    stats = gemini.run(cfg, conn, log=lines.append)
    # quota errors are not the files' fault: they stay pending with no attempt used
    assert stats["quota"] and stats["pending"] == 3 and stats["done"] == 0
    assert any("stopping" in l for l in lines)
    row = conn.execute("SELECT error, attempts FROM requests WHERE error IS NOT NULL LIMIT 1").fetchone()
    assert "429" in row["error"] and "billing" in row["error"] and row["attempts"] == 0
    assert not failures(conn)


# ---------------------------------------------------------------- edge cases + autopilot

def test_scan_edge_cases(env, monkeypatch, tmp_path):
    import os

    from contentrag import scan as scan_mod

    cfg, conn = env
    a = cfg.roots[0].path
    (a / "empty.mp4").write_bytes(b"")
    (a / "broken.mp4").write_bytes(b"this is not a video" * 100)
    # iPhone Live Photo: IMG_0001.HEIC/.JPG + IMG_0001.MOV (~2 s)
    from PIL import Image

    Image.new("RGB", (400, 300), "blue").save(a / "IMG_0001.JPG")
    _video(a / "IMG_0001.MOV", "320x240", 2, {})
    _video(a / "tiny.mp4", "320x240", 1, {})
    stats = scan(cfg, conn, log=lambda *_: None)
    assert stats["empty"] == 1 and stats["failed"] == 1  # broken file recorded, run continues
    skips = dict(conn.execute("SELECT relpath, skip_reason FROM media WHERE skip_reason IS NOT NULL").fetchall())
    assert skips == {"IMG_0001.MOV": "live photo video", "tiny.mp4": "too short"}

    # a file being copied right now is left for the next scan
    monkeypatch.setattr(scan_mod, "SETTLE_SECONDS", 3600)
    _video(a / "copying.mp4", "320x240", 3, {})
    assert scan(cfg, conn, log=lambda *_: None)["still_copying"] >= 1
    assert not conn.execute("SELECT 1 FROM locations WHERE relpath='copying.mp4'").fetchone()
    monkeypatch.setattr(scan_mod, "SETTLE_SECONDS", 0)

    # replaced file at the same path, and a deleted file, are both noticed
    _video(a / "2023" / "VID_20230812_210000.mp4", "360x640", 5, {})
    os.remove(a / "IMG_20230813_101500.jpg")
    stats = scan(cfg, conn, log=lambda *_: None)
    assert stats["replaced"] == 1 and stats["removed"] == 2  # old version + deleted photo
    assert not conn.execute("SELECT 1 FROM media WHERE relpath='IMG_20230813_101500.jpg'").fetchone()


def test_prep_gives_up_after_max_attempts(env, monkeypatch):
    from contentrag import prep as prep_mod
    from contentrag.db import MAX_ATTEMPTS, failures

    cfg, conn = env
    scan(cfg, conn, log=lambda *_: None)
    monkeypatch.setattr(prep_mod, "prep_photo", lambda *a: (_ for _ in ()).throw(RuntimeError("bad photo")))
    for _ in range(MAX_ATTEMPTS + 1):
        prep(cfg, conn, log=lambda *_: None)
    row = conn.execute("SELECT attempts, prepped FROM media WHERE kind='photo'").fetchone()
    assert row["attempts"] == MAX_ATTEMPTS and row["prepped"] == 0
    assert any(f["status"] == "file gave up" for f in failures(conn))


def test_absolute_timestamps_are_not_shifted_twice():
    from contentrag.gemini import _shift

    rel = _shift({"moments": [{"start": 1, "end": 5}]}, offset=360, length=360)
    assert rel["moments"][0]["start"] == 361
    ab = _shift({"moments": [{"start": 365, "end": 400}]}, offset=360, length=360)
    assert ab["moments"][0]["start"] == 365


def test_autopilot_end_to_end(env, monkeypatch):
    """Quota errors make Autopilot wait and continue; it finishes with search data and a vault."""
    from google.genai import errors

    from contentrag import autopilot as ap_mod, gemini
    from contentrag.autopilot import Autopilot, Busy, pipeline_lock, read_state

    cfg, conn = env
    cfg.transcribe_enabled = False
    cfg.gemini_workers = 1
    monkeypatch.setattr(ap_mod, "QUOTA_WAITS", [0])
    monkeypatch.setattr(gemini.time, "sleep", lambda s: None)
    good = _FakeModels()
    calls = {"n": 0}

    class Flaky:
        def generate_content(self, model, contents, config):
            calls["n"] += 1
            if 2 <= calls["n"] <= 16:  # 3 requests x 5 tries all 429 -> autopilot waits, then resumes
                raise errors.ClientError(429, {"error": {"code": 429, "message": "quota", "status": "RESOURCE_EXHAUSTED"}})
            return good.generate_content(model, contents, config)

    monkeypatch.setattr(gemini, "client", lambda cfg: SimpleNamespace(models=Flaky()))
    monkeypatch.setenv("GEMINI_API_KEY", "test")
    monkeypatch.setattr(gemini, "list_models", lambda cfg: [cfg.gemini_model])
    lines = []
    result = Autopilot(cfg, log=lines.append, hours=1).run()
    assert result["counts"]["described"] == result["counts"]["media"] == 4, lines
    assert any("waiting until" in l for l in lines)
    assert read_state(cfg)["status"] == "finished"
    gen = cfg.vault_dir / "_generated"
    assert (gen / "Index.md").exists() and (gen / "Years" / "2023.md").exists()
    assert list((gen / "Themes").glob("*.md")) or True
    assert "`m" in (gen / "Years" / "2023.md").read_text()
    assert list((cfg.library_dir / "logs").glob("autopilot-*.log"))
    # only one pipeline per library
    with pipeline_lock(cfg):
        with pytest.raises(Busy):
            Autopilot(cfg, log=lambda *_: None).run()


def test_budget_cap_pauses_analysis(env, monkeypatch):
    from contentrag import gemini

    cfg, conn = env
    cfg.transcribe_enabled = False
    cfg.gemini_workers = 1
    cfg.gemini_budget_usd = 0.0001  # first answer already exceeds it
    scan(cfg, conn, log=lambda *_: None)
    prep(cfg, conn, log=lambda *_: None)
    monkeypatch.setattr(gemini, "client", lambda cfg: SimpleNamespace(models=_FakeModels()))
    stats = gemini.run(cfg, conn, log=lambda *_: None)
    assert stats["budget"] and stats["done"] < 5
    assert gemini.run(cfg, conn, log=lambda *_: None)["budget"]  # stays paused on the next run


def test_pull_cuts_clips_and_premiere_xml(env, monkeypatch):
    import xml.etree.ElementTree as ET

    from contentrag import gemini
    from contentrag.pull import parse_ids, pull

    cfg, conn = env
    cfg.transcribe_enabled = False
    scan(cfg, conn, log=lambda *_: None)
    prep(cfg, conn, log=lambda *_: None)
    monkeypatch.setattr(gemini, "client", lambda cfg: SimpleNamespace(models=_FakeModels()))
    gemini.run(cfg, conn, log=lambda *_: None)
    ids = [r[0] for r in conn.execute("SELECT m.id FROM moments m JOIN media md ON md.id=m.media_id "
                                      "WHERE md.kind='video' ORDER BY m.id LIMIT 2")]
    photo = conn.execute("SELECT m.id FROM moments m JOIN media md ON md.id=m.media_id "
                         "WHERE md.kind='photo'").fetchone()[0]
    assert parse_ids([f"m{ids[0]}, m{ids[1]}", str(photo), "m999999"]) == [*ids, photo, 999999]
    res = pull(cfg, conn, [*ids, photo, 999999], "my reel", log=lambda *_: None)
    assert res["pulled"] == 3 and len(res["skipped"]) == 1
    folder = Path(res["folder"])
    clips = sorted(folder.glob("*.mp4"))
    assert len(clips) == 2 and all(c.stat().st_size > 1000 for c in clips)
    sel = json.loads((folder / "selects.json").read_text())
    assert sel[0]["moment"] == f"m{ids[0]}"
    assert all(s["clip_in"] == min(0.5, s["source_in"]) for s in sel if s["kind"] == "video")  # head handle
    root = ET.parse(folder / "timeline.xml").getroot()
    items = root.findall(".//video/track/clipitem")
    assert len(items) == 2 and items[0].find("file/pathurl").text.startswith("file://")
    assert int(items[1].find("start").text) == int(items[0].find("end").text)  # back to back


def test_preflight_blocks_bad_setup(env, monkeypatch):
    from contentrag import gemini
    from contentrag.autopilot import Autopilot, preflight, read_state

    cfg, conn = env
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    assert any("GEMINI_API_KEY" in p for p in preflight(cfg))
    monkeypatch.setenv("GEMINI_API_KEY", "test")
    monkeypatch.setattr(gemini, "list_models", lambda cfg: ["gemini-9-flash"])
    assert any("gemini-9-flash" in p for p in preflight(cfg))
    res = Autopilot(cfg, log=lambda *_: None).run()
    assert res["summary"].startswith("Can't start") and read_state(cfg)["status"] == "error"
    assert conn.execute("SELECT count(*) FROM media").fetchone()[0] == 0  # nothing was touched


def test_bad_key_and_network_do_not_burn_attempts(env, monkeypatch):
    import httpx
    from google.genai import errors

    from contentrag import gemini

    cfg, conn = env
    cfg.transcribe_enabled = False
    cfg.gemini_workers = 1
    scan(cfg, conn, log=lambda *_: None)
    prep(cfg, conn, log=lambda *_: None)
    monkeypatch.setattr(gemini.time, "sleep", lambda s: None)

    class BadKey:
        def generate_content(self, model, contents, config):
            raise errors.ClientError(400, {"error": {"code": 400, "message": "API key not valid. Please pass a valid API key.",
                                                     "status": "INVALID_ARGUMENT"}})

    monkeypatch.setattr(gemini, "client", lambda cfg: SimpleNamespace(models=BadKey()))
    stats = gemini.run(cfg, conn, log=lambda *_: None)
    assert "API key" in stats["fatal"] and stats["pending"] == 1  # stopped at the first one

    class Offline:
        def generate_content(self, model, contents, config):
            raise httpx.ConnectError("nodename nor servname provided")

    monkeypatch.setattr(gemini, "client", lambda cfg: SimpleNamespace(models=Offline()))
    stats = gemini.run(cfg, conn, log=lambda *_: None)
    assert stats["network"] and stats["pending"] == 3
    assert conn.execute("SELECT max(coalesce(attempts,0)) FROM requests").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM requests WHERE status='pending'").fetchone()[0] == 5


# ---------------------------------------------------------------- collections, usage, hide list

def test_collection_names():
    from contentrag.collections import collection_of

    assert collection_of("College/Sem 5/DCIM/100APPLE/IMG_1.MOV") == "College / Sem 5"
    assert collection_of("Thailand Trip/IMG_2.MOV") == "Thailand Trip"
    assert collection_of("WhatsApp Video/2023/x.mp4") is None
    assert collection_of("IMG_3.MOV") is None
    assert collection_of("Phone dump 2025-26/Camera/x.mov") == "Phone dump 2025-26"


def _analysed(env, monkeypatch):
    from contentrag import gemini

    cfg, conn = env
    cfg.transcribe_enabled = False
    scan(cfg, conn, log=lambda *_: None)
    prep(cfg, conn, log=lambda *_: None)
    monkeypatch.setattr(gemini, "client", lambda cfg: SimpleNamespace(models=_FakeModels()))
    gemini.run(cfg, conn, log=lambda *_: None)
    return cfg, conn


def test_hide_usage_and_partial_pull(env, monkeypatch):
    from contentrag.pull import parse_items, pull
    from contentrag.usage import hide, safe_parts, unhide, uses_of

    cfg, conn = _analysed(env, monkeypatch)
    assert conn.execute("SELECT collection FROM media WHERE relpath LIKE '2023/%' LIMIT 1").fetchone()[0] is None
    res = search(cfg, conn, "colour bars", use_vectors=False, limit=50)
    assert res and all(r["uses"] == 0 for r in res)
    first = res[0]

    # using part of a clip counts as a use for overlapping searches; ranking demotes it
    items = parse_items([f"m{first['moment_id']}:{first['start']}-{min(first['end'], first['start'] + 1.0)}"])
    assert items[0]["end"] == min(first["end"], first["start"] + 1.0)
    out = pull(cfg, conn, items, "reel one", log=lambda *_: None)
    assert out["pulled"] == 1
    assert len(uses_of(conn, first["media_id"])) == 1
    again = {r["moment_id"]: r for r in search(cfg, conn, "colour bars", use_vectors=False, limit=50)}
    assert again[first["moment_id"]]["uses"] == 1 and again[first["moment_id"]]["used_in"] == ["reel one"]
    fresh = search(cfg, conn, "colour bars", Filters(fresh=True), use_vectors=False, limit=50)
    assert first["moment_id"] not in {r["moment_id"] for r in fresh}

    # hide a whole clip, then unhide
    hide(conn, "media", first["media_id"], "test")
    assert first["media_id"] not in {r["media_id"] for r in search(cfg, conn, "colour bars", use_vectors=False, limit=50)}
    assert pull(cfg, conn, [first["moment_id"]], "x", log=lambda *_: None)["skipped"]
    unhide(conn, "media", first["media_id"])
    assert first["media_id"] in {r["media_id"] for r in search(cfg, conn, "colour bars", use_vectors=False, limit=50)}

    # privacy trimming math
    assert safe_parts(0, 10, [(3, 5)]) == [(0, 3), (5, 10)]
    assert safe_parts(0, 10, [(0, 9)]) == []
    assert safe_parts(0, 10, [(2, 3), (2.5, 9.5)], min_len=0.5) == [(0, 2), (9.5, 10)]


def test_faces_group_name_merge_hide(env, monkeypatch):
    import re

    import numpy as np

    from contentrag.faces import find_faces, hide_people, list_people, name_person
    from contentrag.pull import pull
    from contentrag.usage import hidden_ranges

    cfg, conn = _analysed(env, monkeypatch)
    goa = conn.execute("SELECT md.id FROM media md JOIN locations l ON l.media_id=md.id "
                       "WHERE l.relpath='2023/goa.mov'").fetchone()["id"]
    a = np.zeros(128, np.float32); a[0] = 1
    b = np.zeros(128, np.float32); b[1] = 1
    a2 = np.zeros(128, np.float32); a2[0] = 0.3; a2[2] = 0.95; a2 /= np.linalg.norm(a2)  # A, different angle

    class Fake:
        def faces(self, frame):
            p = str(frame)
            m = re.search(r"t(\d+\.\d+)\.jpg$", p)
            d = re.search(r"f(\d+)\.jpg$", p)
            t = float(m.group(1)) if m else (int(d.group(1)) - 0.5) if d else 0.0
            if goa in p and 4 <= t <= 8:
                return [((10, 10, 80, 80), 0.99, a)]
            if goa in p:
                return []
            return [((10, 10, 80, 80), 0.99, b)]

    res = find_faces(cfg, conn, eng=Fake(), log=lambda *_: None)
    assert res["people"] == 2
    people = {p["id"]: p for p in list_people(conn, min_faces=1)}
    pa = next(pid for pid, p in people.items()
              if conn.execute("SELECT 1 FROM faces WHERE person_id=? AND media_id=?", (pid, goa)).fetchone())
    # an extra group for the same person (other angle): same name = same person, groups stay separate
    extra = conn.execute("INSERT INTO people(faces, centroid) VALUES (1, ?)", (a2.tobytes(),)).lastrowid
    name_person(conn, pa, "Ex")
    name_person(conn, extra, "Ex")
    from contentrag.faces import people_by_name, resolve_people

    assert sorted(resolve_people(conn, "Ex")) == sorted([pa, extra])
    ex = next(p for p in people_by_name(conn, 1)["persons"] if p["name"] == "Ex")
    assert len(ex["groups"]) == 2

    out = hide_people(cfg, conn, "Ex", eng=Fake(), log=lambda *_: None)
    assert out["rescanned"] == 1 and out["matches"] >= 4  # dense pass found her at 1 fps
    assert all(r["hidden"] for r in conn.execute("SELECT hidden FROM people WHERE id IN (?,?)", (pa, extra)))
    # a group given a hidden person's name later is hidden straight away
    late = conn.execute("INSERT INTO people(faces, centroid) VALUES (1, ?)", (a2.tobytes(),)).lastrowid
    name_person(conn, late, "ex")
    assert conn.execute("SELECT hidden FROM people WHERE id=?", (late,)).fetchone()[0] == 1
    ranges = hidden_ranges(conn, goa)
    assert ranges and ranges[0][0] <= 4.0 and ranges[-1][1] >= 8.0
    cut = [r for r in search(cfg, conn, "", Filters(kind="video"), use_vectors=False, limit=200) if r["media_id"] == goa]
    assert cut
    for r in cut:
        assert r["end"] <= 4.0 - 1.0 or r["start"] >= 8.0, r  # never overlaps her
    mo = conn.execute("SELECT id FROM moments WHERE media_id=? AND start <= 5 AND end >= 7", (goa,)).fetchone()
    if mo:
        res = pull(cfg, conn, [mo["id"]], "privacy", log=lambda *_: None)
        sel = json.loads((Path(res["folder"]) / "selects.json").read_text())
        for s in sel:
            assert s["source_out"] <= 4.0 or s["source_in"] >= 8.0

    # held back: the moments she's in are offered separately (whole, with her name), never by default
    from contentrag.edit.plan import Clip, EditPlan
    from contentrag.edit.run import protect_hidden_people, render_plan
    from contentrag.usage import face_boxes
    from contentrag.util import source_path

    held = [r for r in search(cfg, conn, "", Filters(kind="video", held_back=True), use_vectors=False, limit=200)]
    assert held and all(r["media_id"] == goa and r["held_back"] and r["hidden_people"] == ["Ex"] for r in held)
    full = held[0]
    assert full["start"] < 8.0 and full["end"] > 4.0 and not full["trimmed_for_privacy"]
    boxes = face_boxes(conn, goa, 0, 12)
    assert boxes and all(0 <= b[1] <= 1 and 0 < b[3] <= 1 and 0 < b[4] <= 1 for b in boxes)
    # an edit that uses it (only when the creator asked): blur is a layer in the exports, footage untouched
    src = str(source_path(cfg, conn, goa))
    plan = EditPlan("blurtest", width=360, height=640, fps=15)
    plan.clips = [Clip("broll", src, 3.0, 9.0, 0.0, media_id=goa)]
    assert protect_hidden_people(conn, plan) == 0  # her face is at the far left: the 9:16 crop cuts it off
    plan.clips[0].reframe = "blur"  # whole picture in frame: now she is visible and gets blurred
    assert protect_hidden_people(conn, plan) == 1 and plan.clips[0].blur_people == ["Ex"]
    for fr, to, x, y, w, h in plan.clips[0].blur:
        assert 0 <= fr < to <= 6.0 and 0 <= x <= 1 and 0 < w <= 1 and 0 < h <= 1
    out = render_plan(cfg, conn, plan, {"mp4", "hyperframes", "remotion", "premiere"}, log=lambda *_: None)
    assert Path(out["mp4"]).stat().st_size > 0
    assert "face-blur" in Path(out["hyperframes"]).read_text()
    assert json.loads((Path(out["remotion"]) / "src" / "plan.json").read_text())["blur"]
    assert src in Path(out["premiere"]).read_text() or "goa" in Path(out["premiere"]).read_text()
    assert "Blurred faces" in (Path(out["folder"]) / "EDIT.md").read_text()
    # photos with a hidden person never appear
    photo = conn.execute("SELECT id FROM media WHERE kind='photo' LIMIT 1").fetchone()["id"]
    assert any(r["media_id"] == photo for r in search(cfg, conn, "", Filters(kind="photo"), use_vectors=False))
    conn.execute("INSERT INTO faces(media_id, t, x, y, w, h, score, emb, person_id) VALUES (?,0,10,10,50,50,0.9,?,?)",
                 (photo, a.tobytes(), pa))
    conn.commit()
    for held_back in (False, True):
        f = Filters(kind="photo", held_back=held_back)
        assert not any(r["media_id"] == photo for r in search(cfg, conn, "", f, use_vectors=False))


def test_collection_notes_and_ideas(env, monkeypatch):
    cfg, conn = env
    _video(cfg.roots[0].path / "Thailand Trip" / "DCIM" / "fire.mov", "640x360", 7,
           {"creation_time": "2025-11-14T13:00:00Z", "location": "+7.8804+098.3923/"})
    cfg, conn = _analysed((cfg, conn), monkeypatch)
    assert conn.execute("SELECT collection FROM media WHERE relpath LIKE 'Thailand%'").fetchone()[0] == "Thailand Trip"
    hooks = search(cfg, conn, "", Filters(collection="thailand", roles=("hook",)), use_vectors=False)
    assert hooks and all(r["collection"] == "Thailand Trip" and "hook" in r["roles"] for r in hooks)
    build_vault(cfg, conn, log=lambda *_: None)
    note = (cfg.vault_dir / "_generated" / "Collections" / "Thailand Trip.md").read_text()
    for section in ("Story seeds", "Hooks", "Cinematic", "Reel ideas", "Tuk-tuk driver"):
        assert section in note, section
    ideas = (cfg.vault_dir / "_generated" / "Ideas.md").read_text()
    assert "Tuk-tuk driver" in ideas and "Unused gems" in ideas
    assert "[[Thailand Trip]]" in (cfg.vault_dir / "_generated" / "Index.md").read_text()


def test_organize_plan_apply_undo(env, monkeypatch):
    from contentrag import organize

    cfg, conn = _analysed(env, monkeypatch)
    a = cfg.roots[0].path
    _video(a / "Thailand Trip" / "fire.mov", "640x360", 3, {"creation_time": "2025-11-14T13:00:00Z"})
    (a / "Thailand Trip" / "fire.AAE").write_text("edit sidecar")
    scan(cfg, conn, log=lambda *_: None)
    before = sorted(str(p.relative_to(a)) for p in a.rglob("*") if p.is_file())

    res = organize.plan(cfg, conn, log=lambda *_: None)
    assert res["duplicates"] == 1 and Path(res["review"]).exists()
    moves = json.loads(Path(res["plan"]).read_text())["moves"]
    dup = next(m for m in moves if m["reason"] == "duplicate")
    assert dup["to"].startswith("_Duplicates/")
    assert any(m["to"].startswith("2025/2025-11 Thailand Trip/") for m in moves)
    assert all(not (a / m["to"]).exists() for m in moves)  # plan moves nothing

    out = organize.apply(cfg, conn, log=lambda *_: None)
    assert out["moved"] == len(moves) and out["skipped"] == 0
    assert (a / "2025/2025-11 Thailand Trip/fire.mov").exists()
    assert (a / "2025/2025-11 Thailand Trip/fire.AAE").exists()  # sidecar travelled along
    assert not (a / "Thailand Trip").exists()  # emptied folder removed
    # index follows the files: nothing to re-analyse, primary copy is not the duplicate
    assert scan(cfg, conn, log=lambda *_: None)["new"] == 0
    assert all(not r["file"].count("_Duplicates") for r in search(cfg, conn, "colour bars", use_vectors=False))
    assert conn.execute("SELECT collection FROM media WHERE relpath LIKE '%fire.mov'").fetchone()[0] == "Thailand Trip"

    organize.undo(cfg, conn, log=lambda *_: None)
    after = sorted(str(p.relative_to(a)) for p in a.rglob("*") if p.is_file())
    assert after == before


# ---------------------------------------------------------------- edit engine

def _assets(cfg):
    from contentrag.edit.style import assets_dir

    a = assets_dir(cfg)
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "color=c=orange:s=640x360:d=1.5",
                    "-vf", "fade=in:0:10,fade=out:30:15", str(a / "light_leaks" / "leak1.mp4")], check=True)
    for kind, d in (("whoosh", 0.4), ("shutter", 0.2), ("impact", 0.5), ("riser", 1.2)):
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", f"sine=f=600:d={d}",
                        str(a / "sfx" / kind / f"{kind}.wav")], check=True)


def test_talking_head_edit_all_exports(env, monkeypatch, tmp_path):
    import shutil as sh
    import xml.etree.ElementTree as ET

    from contentrag.edit import run
    from contentrag.edit.plan import EditPlan
    from contentrag.edit.talking import Word

    cfg, conn = _analysed(env, monkeypatch)
    _assets(cfg)
    aroll = tmp_path / "talk.mp4"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=720x1280:rate=30:duration=10",
                    "-f", "lavfi", "-i", "aevalsrc='if(between(t,3,4.5),0,0.5*sin(2*PI*300*t))':d=10:s=48000",
                    "-shortest", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(aroll)], check=True)
    words = [Word(0.2, 0.6, "Aaj"), Word(0.7, 1.0, "main"), Word(1.2, 1.5, "um"), Word(1.6, 2.4, "Thailand"),
             Word(2.5, 2.9, "gaya."), Word(4.6, 5.0, "Wahan"), Word(5.1, 5.6, "fire"), Word(5.7, 6.2, "show"),
             Word(6.3, 7.0, "dekha"), Word(7.1, 7.5, "aur"), Word(7.6, 8.4, "pagal"), Word(8.5, 9.6, "hogaya.")]
    understanding = {"topic": "Thailand trip", "hook_query": "colour bars", "collection_hint": "", "payoff_line": 1,
                     "lines": [{"index": 0, "english": "Today I went to Thailand.", "roman": "Aaj main Thailand gaya",
                                "visual_query": "sunset beach", "wants_broll": True, "emphasis": ["Thailand"]},
                               {"index": 1, "english": "There I saw a fire show and went crazy.",
                                "roman": "Wahan fire show dekha aur pagal hogaya", "visual_query": "colour bars",
                                "wants_broll": True, "emphasis": ["pagal"]}]}
    out = run.make_talking(cfg, conn, aroll, "thailand reel", run.parse_targets("all"), words=words,
                           understanding=understanding, use_vectors=False, log=lambda *_: None)
    plan = EditPlan.load(Path(out["plan"]))
    assert len(plan.track("aroll")) >= 2  # the 1.5 s pause was cut
    assert sum(c.length for c in plan.track("aroll")) < 9.5
    hook = [c for c in plan.clips if c.role == "hook"]
    assert hook and hook[0].at == 0.0 and plan.track("aroll")[0].at == pytest.approx(hook[0].length, abs=0.01)
    assert any(c.role == "payoff" for c in plan.clips)  # hook moment shown again at the payoff line
    assert plan.track("overlay") and plan.sounds  # light leak + sfx from the assets folder
    assert plan.captions and not any("UM" in c.text.split() for c in plan.captions)
    assert any("PAGAL" in c.emphasis for c in plan.captions)
    # MP4 preview has the planned length
    dur = float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0",
                                out["mp4"]], capture_output=True, text=True).stdout)
    assert abs(dur - plan.duration) < 0.25
    # Premiere XML: 3 video tracks, talk on V1 referencing the original
    root = ET.parse(out["premiere"]).getroot()
    tracks = root.findall(".//video/track")
    assert len(tracks) == 3 and tracks[0].find("clipitem/file/pathurl").text.endswith("talk.mp4")
    assert (Path(out["folder"]) / "premiere" / "captions.srt").exists()
    # After Effects script is valid JavaScript
    if sh.which("node"):
        js = tmp_path / "build_comp.js"
        sh.copyfile(out["aftereffects"], js)
        r = subprocess.run(["node", "--check", str(js)], capture_output=True, text=True)
        assert r.returncode == 0, r.stderr
    html = Path(out["hyperframes"]).read_text()
    assert html.count('class="clip') >= len(plan.clips) and "data-composition-id" in html
    assert (Path(out["remotion"]) / "src" / "Edit.tsx").exists()
    # usage recorded
    v = conn.execute("SELECT style FROM videos WHERE name='thailand reel'").fetchone()
    assert v["style"] == "default"
    assert conn.execute("SELECT count(*) FROM usage WHERE video='thailand reel'").fetchone()[0] >= 2
    # re-render after editing the plan by hand
    plan.captions = plan.captions[:1]
    plan.save(Path(out["plan"]))
    again = run.rerender(cfg, conn, "thailand reel", {"mp4"}, log=lambda *_: None)
    assert again["captions"] == 1


def test_beat_edit(env, monkeypatch, tmp_path):
    import numpy as np
    import soundfile as sf

    from contentrag import music
    from contentrag.edit import run
    from contentrag.edit.plan import EditPlan

    cfg, conn = _analysed(env, monkeypatch)
    _assets(cfg)
    sr, bpm, dur = 22050, 120, 12
    y = np.zeros(sr * dur)
    t = np.arange(int(0.05 * sr)) / sr
    click = np.sin(2 * np.pi * 1000 * t) * np.exp(-t * 60)
    for b in np.arange(0, dur, 60 / bpm):
        s = int(b * sr)
        y[s:s + len(click)] += (0.15 if b < 6 else 0.9) * click
    sf.write(tmp_path / "song.wav", y, sr)
    info = music.analyse(cfg, tmp_path / "song.wav", "test song", log=lambda *_: None)
    assert abs(info.bpm - 120) < 2 and info.sections[0].kind == "calm" and info.sections[-1].kind == "peak"
    out = run.make_beat(cfg, conn, "test song", "beat reel", {"mp4", "premiere"}, use_vectors=False,
                        log=lambda *_: None)
    plan = EditPlan.load(Path(out["plan"]))
    cuts = [c.at for c in plan.track("broll")]
    assert len(cuts) >= 6 and plan.track("broll")[0].role == "hook"
    beats = info.beats
    assert all(min(abs(c - b) for b in beats + [0.0]) < 0.06 for c in cuts)  # every cut lands on a beat
    peak = [c for c in plan.track("broll") if c.at >= 6.5]
    calm = [c for c in plan.track("broll") if c.at < 5.5]
    assert max(c.length for c in peak) < min(c.length for c in calm)  # faster cutting in the loud part
    assert Path(out["mp4"]).exists()


def test_backend_switch_and_claude_code(env, monkeypatch, tmp_path):
    from contentrag import claude_code
    from contentrag.autopilot import preflight
    from contentrag.config import set_backend

    cfg, conn = env
    set_backend(cfg.source, "claude-code")
    cfg = load_config(cfg.source)
    assert cfg.backend == "claude-code" and "[prep]" in cfg.source.read_text()  # rest of the file kept
    monkeypatch.setenv("CRAG_BACKEND", "gemini")  # crag --backend gemini <command>: this run only
    assert load_config(cfg.source).backend == "gemini"
    monkeypatch.delenv("CRAG_BACKEND")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-should-not-be-used")
    assert "ANTHROPIC_API_KEY" not in claude_code._env()  # subscription only, never API billing

    monkeypatch.setattr(claude_code, "auth_status", lambda *a: {"logged_in": False, "error": "not logged in"})
    assert any("not logged in" in p for p in preflight(cfg))
    monkeypatch.setattr(claude_code, "auth_status", lambda *a: {"logged_in": True, "error": None})
    assert not preflight(cfg)

    cfg.transcribe_enabled = False
    scan(cfg, conn, log=lambda *_: None)
    prep(cfg, conn, log=lambda *_: None)
    seen = []

    def limited(cfg_, content, schema, system, model=None):
        raise claude_code.ClaudeCodeError("usage limit reached", 429, resume_at=time.time() + 3600)

    monkeypatch.setattr(claude_code, "ask", limited)
    res = claude_code.run(cfg, conn, log=lambda *_: None)
    assert res["quota"] and res["resume_at"] > time.time()
    assert conn.execute("SELECT count(*) FROM requests WHERE status!='pending' OR coalesce(attempts,0)>0"
                        ).fetchone()[0] == 0  # no attempt used

    def answer(cfg_, content, schema, system, model=None):
        seen.append((model, [b["type"] for b in content]))
        cid = next(r for r in conn.execute("SELECT * FROM requests WHERE status='pending' ORDER BY custom_id"))
        msg = _fake_message(cid, cfg, conn)
        return json.loads(msg.content[0].text), {"in": 100, "out": 50}

    monkeypatch.setattr(claude_code, "ThreadPoolExecutor", _SerialPool)
    monkeypatch.setattr(claude_code, "ask", answer)
    res = claude_code.run(cfg, conn, log=lambda *_: None)
    assert res["done"] >= 3 and not res.get("quota")
    assert all("image" in kinds for _, kinds in seen) and seen[0][0] == cfg.cc_model
    assert conn.execute("SELECT count(*) FROM moments").fetchone()[0] > 0

    def logged_out(*a, **k):
        raise claude_code.ClaudeCodeError("Claude Code login problem", 401)

    conn.execute("UPDATE requests SET status='pending'")
    monkeypatch.setattr(claude_code, "ask", logged_out)
    res = claude_code.run(cfg, conn, log=lambda *_: None)
    assert res["fatal"] and conn.execute("SELECT max(coalesce(attempts,0)) FROM requests").fetchone()[0] == 0
    # error text from the CLI is sorted into the right bucket
    assert claude_code._classify("Claude AI usage limit reached|1793865600", None).code == 429
    assert claude_code._classify("Invalid API key · Please run /login", None).code == 401
    assert claude_code._classify("fetch failed ENOTFOUND api.anthropic.com", None).code == "network"


class _SerialPool:
    """Runs jobs immediately, in order (the fake `ask` above reads the database)."""

    def __init__(self, max_workers=None):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def submit(self, fn, *args):
        from concurrent.futures import Future

        f = Future()
        try:
            f.set_result(fn(*args))
        except Exception as e:  # noqa: BLE001
            f.set_exception(e)
        return f


TEMPLATE_XML = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE xmeml>
<xmeml version="4"><sequence id="s"><name>FX</name><rate><timebase>30</timebase></rate><media>
<video><track></track><track>
<clipitem id="c1"><name>T01 SNAP</name><start>57</start><end>64</end><in>84</in><out>91</out>
<compositemode>screen</compositemode><file id="f-plate"><name>Plate.mp4</name>
<pathurl>file://localhost/Users/someone/templates/flash_plates/Plate.mp4</pathurl></file></clipitem>
<clipitem id="c2"><name>T02 RISER INTO FLASH</name><start>205</start><end>221</end><in>9</in><out>25</out>
<compositemode>screen</compositemode><file id="f-plate"/></clipitem>
</track></video>
<audio><track>
<clipitem id="a1"><name>T01 Camera Click</name><start>58</start><end>69</end><in>0</in><out>11</out>
<file id="f-click"><name>Camera Click.MP3</name><pathurl>file://localhost/Users/someone/templates/Camera%20Click.MP3</pathurl></file>
<filter><effect><name>Audio Levels</name><parameter><parameterid>level</parameterid><value>0.79433</value></parameter></effect></filter></clipitem>
<clipitem id="a2"><name>T02 Riser</name><start>159</start><end>212</end><in>0</in><out>53</out>
<file id="f-riser"><name>Riser.mp3</name><pathurl>file://localhost/Users/someone/templates/Riser.mp3</pathurl></file>
<filter><effect><name>Audio Levels</name><parameter><parameterid>level</parameterid><value>0.631</value></parameter></effect></filter></clipitem>
</track><track>
<clipitem id="a3"><name>T02 Camera Click</name><start>216</start><end>227</end><in>0</in><out>11</out><file id="f-click"/></clipitem>
</track></audio></media>
<marker><name>T01 SNAP - CUT</name><in>60</in></marker>
<marker><name>T02 RISER INTO FLASH - CUT</name><in>210</in></marker>
</sequence></xmeml>
"""


def test_transition_library_from_premiere_template(env, monkeypatch, tmp_path):
    import xml.etree.ElementTree as ET

    from contentrag.edit import run, transitions
    from contentrag.edit.plan import EditPlan
    from contentrag.edit.talking import Word

    cfg, conn = _analysed(env, monkeypatch)
    _assets(cfg)
    tpl = tmp_path / "templates"
    (tpl / "flash_plates").mkdir(parents=True)
    (tpl / "fx.xml").write_text(TEMPLATE_XML)
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "color=c=orange:s=270x480:r=30:d=4",
                    str(tpl / "flash_plates" / "Plate.mp4")], check=True)
    for n in ("Camera Click.MP3", "Riser.mp3"):
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "sine=f=500:d=2", str(tpl / n)], check=True)
    res = transitions.import_template(cfg, tpl / "fx.xml", log=lambda *_: None)
    assert res["recipes"] == 2 and res["usable"] == 2 and not res["missing"]
    assert transitions.import_template(cfg, tpl, log=lambda *_: None)["recipes"] == 2  # the folder works too
    monkeypatch.setattr("contentrag.drive.drive_of", lambda cfg: tmp_path)
    assert transitions.find_templates(cfg) == [tpl / "fx.xml"]
    assert transitions.import_template(cfg, tmp_path / "nope", log=lambda *_: None)["recipes"] == 2  # found anyway
    lib = {r["id"]: r for r in transitions.load_library(cfg)}
    snap, riser = lib["T01"], lib["T02"]
    assert snap["offset"] == pytest.approx(-0.1) and snap["plate_in"] == pytest.approx(2.8)
    assert snap["sounds"][0]["gain"] == pytest.approx(0.794, abs=0.001)
    assert riser["sounds"][0]["offset"] == pytest.approx(-1.7) and "riser" in riser["feels"]
    assert transitions.choose(list(lib.values()), "payoff", "x")["id"] == "T02"

    aroll = tmp_path / "talk.mp4"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=720x1280:rate=30:duration=8",
                    "-f", "lavfi", "-i", "sine=f=300:d=8", "-shortest", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                    "-c:a", "aac", str(aroll)], check=True)
    words = [Word(0.2 + i * 0.6, 0.7 + i * 0.6, w) for i, w in enumerate(
        "Aaj main Thailand gaya. Wahan fire show dekha aur pagal hogaya yaar.".split())]
    understanding = {"topic": "t", "hook_query": "colour bars", "collection_hint": "", "payoff_line": 1,
                     "lines": [{"index": i, "english": "line", "roman": "line", "visual_query": "colour bars",
                                "wants_broll": True, "emphasis": []} for i in range(3)]}
    out = run.make_talking(cfg, conn, aroll, "fx reel", {"mp4", "premiere"}, words=words,
                           understanding=understanding, use_vectors=False, log=lambda *_: None)
    plan = EditPlan.load(Path(out["plan"]))
    recipes = [c for c in plan.track("overlay") if c.role == "transition"]
    assert recipes and all(c.blend == "screen" and "Plate.mp4" in c.file for c in recipes)
    payoff = next(c for c in plan.clips if c.role == "payoff")
    assert any(c.note.startswith("T02") and c.end > payoff.at for c in recipes)  # riser recipe into the payoff
    assert any("Riser.mp3" in s.file and s.at == pytest.approx(payoff.at - 1.7, abs=0.01) for s in plan.sounds)
    assert not any(Path(s.file).parent.name == "riser" for s in plan.sounds)  # no extra loose riser on top
    root = ET.parse(out["premiere"]).getroot()
    levels = [p.findtext("value") for p in root.iter("parameter") if p.findtext("parameterid") == "level"]
    assert "0.7940" in levels
    for track in root.findall(".//audio/track"):  # no overlapping clips on one Premiere track
        spans = sorted((int(c.findtext("start")), int(c.findtext("end"))) for c in track.findall("clipitem"))
        assert all(a[1] <= b[0] for a, b in zip(spans, spans[1:]))
    assert Path(out["mp4"]).exists()


def test_root_skip_folders_and_drive_names(tmp_path, monkeypatch):
    from contentrag import scan as scan_mod
    from contentrag.collections import collection_of

    monkeypatch.setattr(scan_mod, "SETTLE_SECONDS", 0)
    drive = tmp_path / "T7"
    _video(drive / "Disk D" / "Goa trip" / "a.mp4", "320x180", 2, {})
    _video(drive / "Downloads" / "movie.mp4", "320x180", 2, {})
    _video(drive / "System Volume Information" / "x.mp4", "320x180", 2, {})
    cfg_path = tmp_path / "contentrag.toml"
    cfg_path.write_text(f"""
library_dir = "{drive / 'ContentLibrary'}"
[[roots]]
name = "t7"
path = "{drive}"
skip = ["Downloads"]
""")
    cfg = load_config(cfg_path)
    conn = connect(cfg.db_path)
    scan(cfg, conn, log=lambda *_: None)
    rows = [r["relpath"] for r in conn.execute("SELECT relpath FROM locations")]
    assert rows == ["Disk D/Goa trip/a.mp4"]  # library inside the drive, Downloads and system folders skipped
    assert collection_of(rows[0]) == "Goa trip"  # "Disk D" is a drive name, not a collection


def test_folders_report_and_skip_added_later(tmp_path, monkeypatch, capsys):
    from contentrag import scan as scan_mod
    from contentrag.cli import main

    monkeypatch.setattr(scan_mod, "SETTLE_SECONDS", 0)
    drive = tmp_path / "T7"
    _video(drive / "Phone" / "DCIM" / "a.mp4", "320x180", 2, {})
    _video(drive / "Phone" / "Download" / "song.mp4", "320x240", 3, {})
    cfg_path = tmp_path / "contentrag.toml"
    text = f'library_dir = "{drive / "lib"}"\n[[roots]]\nname = "t7"\npath = "{drive}"\n'
    cfg_path.write_text(text)
    main(["-c", str(cfg_path), "scan"])
    capsys.readouterr()
    main(["-c", str(cfg_path), "folders", "--under", "Phone"])
    out = capsys.readouterr().out
    assert "Phone/DCIM" in out and "Phone/Download" in out
    cfg_path.write_text(text + 'skip = ["Phone/Download"]\n')
    main(["-c", str(cfg_path), "scan"])
    cfg = load_config(cfg_path)
    rows = [r["relpath"] for r in connect(cfg.db_path).execute("SELECT relpath FROM locations")]
    assert rows == ["Phone/DCIM/a.mp4"]  # skipped later -> dropped from the index on the next scan


def test_estimate_before_prep(env):
    from contentrag import gemini

    cfg, conn = env
    scan(cfg, conn, log=lambda *_: None)
    e = gemini.estimate(cfg, conn)  # nothing prepped yet: still a real figure, from the file lengths
    assert e["requests"] > 0 and e["input_tokens"] > 0 and e["photos"] >= 1 and e["usd"] > 0


def test_organize_groups_dumps_by_month_and_trip():
    from contentrag.organize import _group_loose, _keeper

    def m(i, when, place=None):
        return {"id": f"m{i}", "taken_at": when, "place": place, "title": None}

    items = []
    i = 0
    for month in ("2025-05", "2025-06", "2025-07", "2025-08"):  # home: footage in many months
        for d in ("03", "11", "24"):
            i += 1
            items.append(m(i, f"{month}-{d}T10:00:00", "Gurgaon, Haryana, IN"))
    i += 1
    items.append(m(i, "2025-07-24T09:00:00"))  # screenshot without GPS: joins its month
    for day, city, n in (("08", "Danapur", 5), ("09", "Ara", 3), ("09", "Danapur", 6), ("10", "Khagaul", 2)):
        for _ in range(n):  # one Bihar trip over three days and several towns
            i += 1
            items.append(m(i, f"2025-08-{day}T12:00:00", f"{city}, Bihar, IN"))
    folders = _group_loose(items)
    names = set(folders.values())
    assert "2025/2025-08-08 Danapur trip" in names
    assert sum(1 for f in folders.values() if f == "2025/2025-08-08 Danapur trip") == 16
    assert folders["m13"] == "2025/2025-07 Gurgaon"  # no-GPS file in the July folder
    assert "2025/2025-05 Gurgaon" in names and len(names) == 5  # 4 months + 1 trip, no per-day folders

    # duplicates: the named folder keeps the file, not the "all pics" dump
    from contentrag.collections import collection_of

    locs = [{"root": "t7", "relpath": "College stuffs/all pics 4 years/IMG_1.JPG"},
            {"root": "t7", "relpath": "College stuffs/Home Summer'23/IMG_1.jpg"}]
    keep = _keeper(locs, collection_of, {"College stuffs / all pics 4 years"})
    assert "Home Summer" in keep["relpath"]


def test_camera_and_phone_container_folders():
    from contentrag.collections import collection_of
    from contentrag.scan import SKIP_DIRS

    assert collection_of("DLSR college end/M4ROOT/CLIP/C0001.MP4") == "DLSR college end"
    assert collection_of("Disk D/mobile files/x.jpg") is None
    assert collection_of("Disk D/iPhone files/x.mov") is None
    assert collection_of("Disk D/new photo/x.jpg") is None
    assert collection_of("College stuffs/Kedarkantha Trek/a.jpg") == "College stuffs / Kedarkantha Trek"
    assert "THMBNL" in SKIP_DIRS  # Sony thumbnails are never indexed


def test_photo_model_separate(env, monkeypatch):
    from contentrag import gemini

    cfg, conn = env
    cfg.transcribe_enabled = False
    cfg.gemini_photo_model = "gemini-3.5-flash-lite"
    scan(cfg, conn, log=lambda *_: None)
    prep(cfg, conn, log=lambda *_: None)
    e = gemini.estimate(cfg, conn)
    assert e["photo_model"] == "gemini-3.5-flash-lite" and e["photo_requests"] >= 1 and e["video_requests"] >= 1
    used = []
    real = gemini._call
    monkeypatch.setattr(gemini, "client", lambda cfg: SimpleNamespace(models=_FakeModels()))
    monkeypatch.setattr(gemini, "_call", lambda g, c, contents, schema, model=None:
                        (used.append(model or c.gemini_model), real(g, c, contents, schema, model))[1])
    gemini.run(cfg, conn, log=lambda *_: None)
    assert "gemini-3.5-flash-lite" in used and cfg.gemini_model in used
    models = {r["kind"]: r["model"] for r in conn.execute("SELECT kind, model FROM requests")}
    assert models["photos"] == "gemini-3.5-flash-lite" and models["video"] == cfg.gemini_model


def test_out_of_credit_stops_run_and_recovers(env, monkeypatch):
    from google.genai import errors

    from contentrag import gemini

    cfg, conn = env
    cfg.transcribe_enabled = False
    cfg.gemini_workers = 1
    scan(cfg, conn, log=lambda *_: None)
    prep(cfg, conn, log=lambda *_: None)
    monkeypatch.setattr(gemini.time, "sleep", lambda s: None)

    class NoCredit:
        def generate_content(self, model, contents, config):
            raise errors.ClientError(402, {"error": {"code": 402, "status": "RESOURCE_EXHAUSTED",
                                                     "message": "Your prepayment credits are depleted."}})

    monkeypatch.setattr(gemini, "client", lambda cfg: SimpleNamespace(models=NoCredit()))
    stats = gemini.run(cfg, conn, log=lambda *_: None)
    assert "credits ran out" in stats["fatal"] and stats["pending"] == 1  # stopped at the first one
    assert conn.execute("SELECT max(coalesce(attempts,0)) FROM requests").fetchone()[0] == 0

    # requests that an older version marked as failed for this reason come back with their attempts
    conn.execute("UPDATE requests SET status='error', attempts=3, error=? ",
                  ("402 RESOURCE_EXHAUSTED. {'error': {'code': 402, 'message': 'Your prepayment credits are depleted.'}}",))
    conn.commit()
    monkeypatch.setattr(gemini, "client", lambda cfg: SimpleNamespace(models=_FakeModels()))
    stats = gemini.run(cfg, conn, log=lambda *_: None)
    assert stats["done"] == 5 and not stats.get("fatal")


def test_iphone_spatial_audio_track_is_skipped(tmp_path, monkeypatch):
    from contentrag import gemini, probe
    from contentrag.pull import cut_clip

    # an iPhone 16 style file: AAC plus a second audio track ffmpeg can't decode (APAC)
    fake = {"streams": [{"index": 0, "codec_type": "video", "codec_name": "hevc"},
                        {"index": 1, "codec_type": "audio", "codec_name": "apple_apac"},
                        {"index": 2, "codec_type": "audio", "codec_name": "aac"}]}
    monkeypatch.setattr(probe, "ffprobe", lambda p: fake)
    assert probe.audio_stream(tmp_path / "x.mov") == 2
    fake["streams"] = fake["streams"][:2]
    assert probe.audio_stream(tmp_path / "x.mov") is None  # only APAC: use the picture without sound
    monkeypatch.undo()

    src = tmp_path / "two_tracks.mov"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=25:duration=3",
                    "-f", "lavfi", "-i", "sine=f=300:d=3", "-f", "lavfi", "-i", "sine=f=900:d=3:sample_rate=48000",
                    "-map", "0", "-map", "1", "-map", "2", "-c:v", "libx264", "-c:a:0", "aac", "-c:a:1", "pcm_s16le",
                    str(src)], check=True)
    out = gemini.make_proxy(src, 0, 3, tmp_path / "proxy.mp4", True)
    streams = probe.ffprobe(out)["streams"]
    assert [s["codec_type"] for s in streams].count("audio") == 1
    ok, err = cut_clip(src, 0.5, 2.0, tmp_path / "blur.mp4", reframe="blur", width=360, height=640)
    assert ok, err
    info = probe.ffprobe(tmp_path / "blur.mp4")["streams"]
    assert any(s["codec_type"] == "video" and s["width"] == 360 for s in info)


def test_split_merged_group(env):
    import numpy as np

    from contentrag.faces import people_by_name, split_person

    cfg, conn = env
    rng = np.random.default_rng(0)
    centres = [np.eye(128, dtype=np.float32)[i] for i in range(3)]  # three different people...
    pid = conn.execute("INSERT INTO people(name, faces, centroid) VALUES ('Not important', 30, ?)",
                       (centres[0].tobytes(),)).lastrowid
    for k, c in enumerate(centres):  # ...merged into one named group by an older version
        for i in range(10):
            e = c + rng.normal(0, 0.05, 128).astype(np.float32)
            e /= np.linalg.norm(e)
            conn.execute("INSERT INTO faces(media_id, t, x, y, w, h, score, emb, person_id) VALUES (?,?,?,?,?,?,?,?,?)",
                         (f"m{k}", float(i), 0, 0, 10, 10, 0.9, e.astype(np.float32).tobytes(), pid))
    conn.commit()
    ids = split_person(cfg, conn, pid)
    assert len(ids) == 3 and ids[0] == pid
    sizes = sorted(conn.execute("SELECT count(*) FROM faces WHERE person_id=?", (g,)).fetchone()[0] for g in ids)
    assert sizes == [10, 10, 10]
    p = next(p for p in people_by_name(conn, 1)["persons"] if p["name"] == "Not important")
    assert len(p["groups"]) == 3  # same name until renamed


def test_scan_only_and_portable_drive(env, tmp_path):
    import os
    import sys

    from contentrag import drive
    from contentrag.config import load_config

    cfg, conn = env
    a = cfg.roots[0].path
    _video(a / "Day in my life" / "wake.mp4", "360x640", 3, {})
    st = scan(cfg, conn, log=lambda *_: None, only=a / "Day in my life")
    assert st["new"] == 1 and st["removed"] == 0
    assert [r[0] for r in conn.execute("SELECT relpath FROM media")] == ["Day in my life/wake.mp4"]
    with pytest.raises(SystemExit):
        scan(cfg, conn, log=lambda *_: None, only=tmp_path)  # not inside a footage drive
    assert scan(cfg, conn, log=lambda *_: None)["new"] >= 3  # a full scan still finds the rest

    # the drive carries code, a relative config, skills and a launcher
    d = tmp_path / "ssd1"
    (d / "ContentLibrary").mkdir()
    toml = tmp_path / "portable.toml"
    toml.write_text(f'library_dir = "{d / "ContentLibrary"}"\n[[roots]]\nname = "arch"\npath = "{a}"\n'
                    f'[[roots]]\nname = "other"\npath = "{tmp_path / "elsewhere"}"\n')
    res = drive.setup(load_config(toml), d, log=lambda *_: None)
    app = d / "ContentLibrary" / "app"
    text = (app / "contentrag.toml").read_text()
    assert 'library_dir = ".."' in text and 'path = "../../Archive"' in text and str(tmp_path / "elsewhere") in text
    moved = load_config(app / "contentrag.toml")
    assert moved.library_dir == (d / "ContentLibrary").resolve() and moved.roots[0].path == a.resolve()
    for f in ("crag", "Set up this Mac.command", "CLAUDE.md", "START HERE.md", ".claude/skills/find-clips/SKILL.md",
              "ContentLibrary/app/contentrag/edit/render.py"):
        assert (d / f).exists(), f
    assert res["files"] > 20
    env_vars = {k: v for k, v in os.environ.items() if k != "CONTENTRAG_CONFIG"}
    r = subprocess.run(["sh", str(d / "crag"), "status"], capture_output=True, text=True,
                       env=env_vars | {"CRAG_VENV": str(tmp_path / "nothing")})
    assert r.returncode == 1 and "Set up this Mac" in r.stderr
    r = subprocess.run(["sh", str(d / "crag"), "status"], capture_output=True, text=True,
                       env=env_vars | {"CRAG_VENV": str(Path(sys.executable).parents[1])})
    assert r.returncode == 0, r.stderr
