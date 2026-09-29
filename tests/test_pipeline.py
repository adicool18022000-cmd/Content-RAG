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
def env(tmp_path):
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
    events = list((vault / "_generated" / "Events").glob("*.md"))
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
