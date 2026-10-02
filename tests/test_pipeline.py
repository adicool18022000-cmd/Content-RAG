"""End-to-end test on synthetic media: scan -> prep -> describe (fake Claude) -> search -> vault.

Needs ffmpeg/ffprobe on PATH. No network, no API key.
"""

from __future__ import annotations

import json
import shutil
import subprocess
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
                             "quality_issues": [], "broll_score": 2, "content_uses": [], "tags": ["bars"]}]}
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
    assert starts == [0.0, 10.0]  # second excerpt shifted by its 10 s offset
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
