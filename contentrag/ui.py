"""Local dashboard: `crag ui` serves http://127.0.0.1:8765 on this Mac only.

The page runs the pipeline steps, shows progress, cost and search results, and plays clips.
API keys stay in this Python process (read from environment variables); the browser never
sees them.
"""

from __future__ import annotations

import json
import mimetypes
import os
import re
import threading
import time
import traceback
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .config import BACKENDS, Config, set_backend, set_value
from .db import connect, failures
from .util import source_path

HTML = Path(__file__).with_name("ui.html")
STEPS = ("hide_person", "scan", "prep", "faces", "transcribe", "describe", "retry", "embed", "vault", "all", "autopilot", "pull")


class Job:
    """At most one pipeline job runs at a time, in a background thread."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.lock = threading.Lock()
        self.lines: list[str] = []
        self.name: str | None = None
        self.running = False
        self.stop = threading.Event()
        self.hours = 24.0
        self.params: dict = {}

    def log(self, *parts) -> None:
        line = f"{datetime.now():%H:%M:%S} " + " ".join(str(p) for p in parts)
        with self.lock:
            self.lines.append(line)
            if len(self.lines) > 5000:
                del self.lines[:1000]

    def start(self, step: str) -> tuple[bool, str]:
        with self.lock:
            if self.running:
                return False, f"'{self.name}' is still running"
            self.running, self.name = True, step
            self.stop.clear()
        threading.Thread(target=self._run, args=(step,), daemon=True).start()
        return True, "started"

    def _run(self, step: str) -> None:
        from .autopilot import Autopilot, Busy, pipeline_lock

        try:
            if step == "autopilot":
                Autopilot(self.cfg, log=self.log, stop=self.stop, hours=self.hours).run()
                return
            with pipeline_lock(self.cfg):
                conn = connect(self.cfg.db_path)
                try:
                    steps = ["scan", "prep", "transcribe", "describe", "embed", "vault"] if step == "all" else [step]
                    for s in steps:
                        if self.stop.is_set():
                            self.log("[ui] stopped")
                            break
                        self.log(f"[ui] ── {s} ──")
                        result = self._step(s, conn)
                        self.log(f"[ui] {s} finished: {json.dumps(result, ensure_ascii=False, default=str)}")
                finally:
                    conn.close()
        except Busy as e:
            self.log(f"[ui] {e}")
        except SystemExit as e:  # friendly errors from the pipeline (missing packages etc.)
            self.log(f"[ui] {e}")
        except Exception as e:
            self.log(f"[ui] ERROR: {e}")
            self.log(traceback.format_exc(limit=3))
        finally:
            with self.lock:
                self.running = False

    def _step(self, s: str, conn):
        cfg, log = self.cfg, self.log
        if s == "scan":
            from .scan import scan

            return scan(cfg, conn, log=log)
        if s == "prep":
            from .prep import prep

            return prep(cfg, conn, log=log, stop=self.stop)
        if s == "hide_person":
            from .faces import hide_people

            return hide_people(cfg, conn, self.params["person"], log=log)
        if s == "faces":
            from .faces import find_faces, refine_hidden

            return {**find_faces(cfg, conn, log=log, stop=self.stop), **refine_hidden(cfg, conn, log=log)}
        if s == "transcribe":
            if not cfg.transcribe_enabled:
                return "skipped (transcribe.enabled = false)"
            from .transcribe import transcribe

            return transcribe(cfg, conn, log=log)
        if s in ("describe", "retry"):
            if cfg.backend == "claude-code":
                from . import claude_code

                return claude_code.run(cfg, conn, retry=(s == "retry"), log=log, stop=self.stop)
            if cfg.backend == "gemini":
                from . import gemini

                return gemini.run(cfg, conn, retry=(s == "retry"), log=log, stop=self.stop)
            from . import describe

            if s == "retry":
                return describe.run_sync(cfg, conn, limit=1000, model=cfg.retry_model,
                                         statuses=("refused", "error"), log=log)
            ids = describe.submit(cfg, conn, log=log)
            return {"submitted_batches": ids, "note": "run 'describe' again later to collect"} if ids \
                else describe.collect(cfg, conn, log=log)
        if s == "embed":
            from .embed import embed, load_model

            if load_model(cfg.embed_model) is None:
                return "skipped (install sentence-transformers for search by meaning)"
            return embed(cfg, conn, log=log)
        if s == "vault":
            from .vault import build_vault

            return build_vault(cfg, conn, log=log)
        if s == "pull":
            from .pull import pull

            return pull(cfg, conn, self.params["ids"], self.params["name"], log=log)
        raise ValueError(s)


def status(cfg: Config, job: Job) -> dict:
    conn = connect(cfg.db_path)
    try:
        q = lambda sql, *a: conn.execute(sql, a).fetchone()[0]  # noqa: E731
        out = {
            "library": str(cfg.library_dir),
            "backend": cfg.backend,
            "backends": BACKENDS,
            "model": {"gemini": cfg.gemini_model, "claude-code": cfg.cc_model}.get(cfg.backend, cfg.model),
            "key_env": {"gemini": cfg.gemini_api_key_env, "claude": "ANTHROPIC_API_KEY"}.get(cfg.backend),
            "transcribe_enabled": cfg.transcribe_enabled,
            "roots": [{"name": r.name, "path": str(r.path), "kind": r.kind, "mounted": r.mounted} for r in cfg.roots],
            "media": {
                "videos": q("SELECT count(*) FROM media WHERE kind='video'"),
                "photos": q("SELECT count(*) FROM media WHERE kind='photo'"),
                "hours": q("SELECT round(coalesce(sum(duration),0)/3600.0,1) FROM media"),
                "duplicates": q("SELECT count(*) FROM locations") - q("SELECT count(*) FROM media"),
                "prepped": q("SELECT count(*) FROM media WHERE prepped=1"),
                "transcribed": q("SELECT count(*) FROM media WHERE transcribed=1 AND kind='video'"),
                "described": q("SELECT count(*) FROM media WHERE described=1"),
                "errors": q("SELECT count(*) FROM media WHERE error IS NOT NULL"),
            },
            "moments": q("SELECT count(*) FROM moments"),
            "requests": {r["status"]: r["n"] for r in conn.execute(
                "SELECT status, count(*) n FROM requests GROUP BY status")},
            "job": {"name": job.name, "running": job.running},
            "failures": failures(conn, 30),
        }
        from .autopilot import eta, lock_held, read_state

        ap = read_state(cfg)
        if ap.get("status") in ("running", "waiting") and not job.running and not lock_held(cfg):
            ap["status"] = "interrupted"  # the process died (crash, closed terminal, power cut)
        out["autopilot"] = {**{k: ap.get(k) for k in ("status", "phase", "round", "started_at", "updated_at",
                                                       "waiting_until", "message", "deadline")},
                            "progress": eta(cfg, conn, ap)}
        if cfg.backend == "claude-code":  # subscription login instead of a key
            from . import claude_code

            st = claude_code.auth_status()
            out["key_present"] = st["logged_in"]
            out["login"] = st
        else:
            out["key_present"] = bool(os.environ.get(out["key_env"]))
        if cfg.backend == "gemini":
            from . import gemini

            out["spent"] = gemini.spent(cfg, conn)
        return out
    finally:
        conn.close()


def make_handler(cfg: Config, job: Job):
    library = cfg.library_dir.resolve()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):  # keep the terminal quiet
            pass

        # -------------------------------------------------------------- helpers
        def _ok_host(self) -> bool:
            host = (self.headers.get("Host") or "").split(":")[0]
            return host in ("127.0.0.1", "localhost")

        def _json(self, data, code=200):
            body = json.dumps(data, ensure_ascii=False, default=str).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _file(self, path: Path, ctype: str | None = None):
            size = path.stat().st_size
            ctype = ctype or mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            if path.suffix.lower() in (".mov", ".m4v"):
                ctype = "video/mp4"  # lets Chrome try QuickTime files; Safari plays either
            start, end = 0, size - 1
            rng = re.match(r"bytes=(\d*)-(\d*)", self.headers.get("Range") or "")
            if rng:
                if rng.group(1):
                    start = int(rng.group(1))
                    end = int(rng.group(2)) if rng.group(2) else size - 1
                else:
                    start = max(0, size - int(rng.group(2)))
                end = min(end, size - 1)
                self.send_response(206)
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            else:
                self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(end - start + 1))
            self.end_headers()
            with open(path, "rb") as f:
                f.seek(start)
                left = end - start + 1
                while left > 0:
                    chunk = f.read(min(1 << 20, left))
                    if not chunk:
                        break
                    try:
                        self.wfile.write(chunk)
                    except (BrokenPipeError, ConnectionResetError):
                        return
                    left -= len(chunk)

        # -------------------------------------------------------------- routes
        def do_GET(self):
            if not self._ok_host():
                return self._json({"error": "forbidden"}, 403)
            url = urlparse(self.path)
            qs = {k: v[-1] for k, v in parse_qs(url.query).items()}
            try:
                if url.path == "/":
                    body = HTML.read_bytes()
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    return self.wfile.write(body)
                if url.path == "/api/status":
                    return self._json(status(cfg, job))
                if url.path == "/api/log":
                    since = int(qs.get("since", 0))
                    with job.lock:
                        lines = job.lines[since:]
                        total = len(job.lines)
                    return self._json({"lines": lines, "next": total, "running": job.running, "job": job.name})
                if url.path == "/api/estimate":
                    conn = connect(cfg.db_path)
                    try:
                        if cfg.backend == "claude-code":
                            from . import claude_code

                            return self._json(claude_code.estimate(cfg, conn))
                        if cfg.backend == "gemini":
                            from . import gemini

                            return self._json(gemini.estimate(cfg, conn))
                        from . import describe

                        return self._json(describe.estimate(cfg, conn))
                    finally:
                        conn.close()
                if url.path == "/api/models":
                    if cfg.backend == "claude-code":
                        return self._json({"models": ["opus", "sonnet", "haiku"], "configured": cfg.cc_model})
                    from . import gemini

                    return self._json({"models": gemini.list_models(cfg), "configured": cfg.gemini_model})
                if url.path == "/api/search":
                    return self._search(qs)
                if url.path == "/api/people":
                    from .faces import people_by_name

                    conn = connect(cfg.db_path)
                    try:
                        data = people_by_name(conn, int(qs.get("min", 3)))
                    finally:
                        conn.close()
                    thumb = lambda x: f"/api/thumb?p={x}" if x else None  # noqa: E731
                    for g in data["unnamed"] + [g for p in data["persons"] for g in p["groups"]]:
                        g["sample_url"] = thumb(g["sample"])
                    for p in data["persons"]:
                        p["sample_url"] = thumb(p["sample"])
                    return self._json(data)
                if url.path == "/api/memories":
                    from .memories import list_events

                    conn = connect(cfg.db_path)
                    try:
                        return self._json(list_events(cfg, conn))
                    finally:
                        conn.close()
                if url.path == "/api/memories/event":
                    from .memories import event_detail

                    conn = connect(cfg.db_path)
                    try:
                        ev = event_detail(cfg, conn, qs.get("id", ""), show_hidden_people=qs.get("hidden") == "1")
                    finally:
                        conn.close()
                    return self._json(ev) if ev else self._json({"error": "not found"}, 404)
                if url.path == "/api/story/sessions":
                    from . import story

                    return self._json({"sessions": story.sessions(cfg), "ai": story.ai_name(cfg)})
                if url.path == "/api/story/session":
                    from . import story

                    try:
                        return self._json(story.load(cfg, qs.get("id", "")))
                    except ValueError as e:
                        return self._json({"error": str(e)}, 404)
                if url.path == "/api/thumb":
                    p = (library / qs.get("p", "")).resolve()
                    if library not in p.parents or not p.is_file():
                        return self._json({"error": "not found"}, 404)
                    return self._file(p)
                if url.path.startswith("/api/media/"):
                    mid = url.path.rsplit("/", 1)[-1]
                    if not re.fullmatch(r"[0-9a-f]{20}", mid):
                        return self._json({"error": "bad id"}, 400)
                    conn = connect(cfg.db_path)
                    try:
                        src = source_path(cfg, conn, mid)
                    finally:
                        conn.close()
                    if src is None:
                        return self._json({"error": "drive not mounted"}, 404)
                    return self._file(src)
                return self._json({"error": "not found"}, 404)
            except Exception as e:
                return self._json({"error": str(e)}, 500)

        def do_POST(self):
            # Custom header: a random website can't send it to localhost without a CORS preflight.
            if not self._ok_host() or self.headers.get("X-Crag") != "1":
                return self._json({"error": "forbidden"}, 403)
            url = urlparse(self.path)
            if url.path == "/api/run":
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
                step = body.get("step")
                if step not in STEPS:
                    return self._json({"error": f"unknown step {step}"}, 400)
                if step == "autopilot":
                    job.hours = float(body.get("hours") or 24)
                if step == "pull":
                    from .pull import parse_items

                    try:
                        ids = parse_items([str(x) for x in body.get("ids") or []])
                    except ValueError:
                        return self._json({"error": "bad moment ids"}, 400)
                    if not ids:
                        return self._json({"error": "no moments selected"}, 400)
                    job.params = {"ids": ids, "name": str(body.get("name") or "selects")[:60]}
                ok, msg = job.start(step)
                return self._json({"ok": ok, "message": msg}, 200 if ok else 409)
            if url.path == "/api/backend":
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
                name = body.get("backend")
                if name not in BACKENDS:
                    return self._json({"error": "unknown backend"}, 400)
                from .autopilot import lock_held

                if job.running or lock_held(cfg):
                    return self._json({"error": "stop the running job before switching"}, 409)
                if cfg.source:
                    set_backend(cfg.source, name)  # remembered for the next start / terminal runs
                cfg.backend = name
                return self._json({"ok": True, "backend": name})
            if url.path == "/api/people":
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
                from .faces import name_person, resolve_people, split_person
                from .usage import unhide

                conn = connect(cfg.db_path)
                try:
                    if "split" in body:  # separate the faces of one group into groups again
                        ids = split_person(cfg, conn, int(body["split"]))
                        return self._json({"ok": True, "groups": ids})
                    if "rename" in body:  # rename a whole person (every group with that name)
                        new = str(body.get("name") or "").strip()[:60]
                        ids = [r["id"] for r in conn.execute("SELECT id FROM people WHERE lower(name)=lower(?)",
                                                             (str(body["rename"]),))]
                        for gid in ids:
                            name_person(conn, gid, new)
                        return self._json({"ok": True, "groups": ids})
                    if "name" in body and "id" in body:  # name / rename / un-name one group
                        return self._json({"ok": True, "id": name_person(conn, int(body["id"]),
                                                                         str(body["name"] or "")[:60])})
                    if "hide" in body:
                        ref = str(body["person"]) if body.get("person") else str(int(body.get("id", 0)))
                        if body["hide"]:
                            from .faces import hide_people

                            hide_people(cfg, conn, ref, dense=False)  # out of search/edits right away
                            job.params = {"person": ref}  # then the exact on-screen seconds, if free now
                            ok, _ = job.start("hide_person")
                            return self._json({"ok": True, "message": "Hidden." + (
                                "" if ok else " The exact on-screen seconds are refined on the next "
                                              "Autopilot run (another job is running now).")})
                        ids = resolve_people(conn, ref) if not ref.isdigit() else [int(ref)]
                        for gid in ids:
                            unhide(conn, "person", str(gid))
                        return self._json({"ok": True})
                except SystemExit as e:
                    return self._json({"error": str(e)}, 400)
                finally:
                    conn.close()
                return self._json({"error": "nothing to do"}, 400)
            if url.path == "/api/memories/save":
                from .memories import save_memory

                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
                conn = connect(cfg.db_path)
                try:
                    return self._json(save_memory(cfg, conn, str(body.get("id") or ""), body))
                except ValueError as e:
                    return self._json({"error": str(e)}, 400)
                finally:
                    conn.close()
            if url.path.startswith("/api/story/"):
                return self._story(url)
            if url.path == "/api/transcribe":
                from .memories import transcribe_audio

                length = int(self.headers.get("Content-Length") or 0)
                if not 0 < length <= 80 * 1024 * 1024:
                    return self._json({"error": "recording is empty or too long"}, 400)
                audio = self.rfile.read(length)
                ctype = (self.headers.get("Content-Type") or "").split(";")[0]
                suffix = {"audio/mp4": ".m4a", "audio/ogg": ".ogg", "audio/wav": ".wav"}.get(ctype, ".webm")
                mode = parse_qs(url.query).get("mode", ["auto"])[-1]
                try:
                    return self._json({"text": transcribe_audio(cfg, audio, suffix, mode)})
                except RuntimeError as e:
                    return self._json({"error": str(e)}, 501)
            if url.path == "/api/stop":
                from .autopilot import read_state, stop_file

                job.stop.set()
                if read_state(cfg).get("status") in ("running", "waiting"):
                    stop_file(cfg).touch()  # also stops an Autopilot started from the terminal
                job.log("[ui] stop requested — the current request batch finishes first")
                return self._json({"ok": True})
            return self._json({"error": "not found"}, 404)

        def _story(self, url):
            from . import story

            qs = {k: v[-1] for k, v in parse_qs(url.query).items()}
            length = int(self.headers.get("Content-Length") or 0)
            if length > 100 * 1024 * 1024:
                return self._json({"error": "too big"}, 400)
            raw = self.rfile.read(length) if length else b""
            try:
                if url.path == "/api/story/chunk":
                    return self._json(story.append_chunk(cfg, qs.get("id", ""), int(qs.get("seq", -1)), raw))
                body = json.loads(raw or b"{}")
                sid = str(body.get("id") or "")
                if url.path == "/api/story/start":
                    return self._json(story.start(cfg, str(body.get("mime") or "audio/webm")))
                if url.path == "/api/story/update":
                    story.update(cfg, sid, body.get("timeline") or [], body.get("marks") or {})
                    return self._json({"ok": True})
                if url.path == "/api/story/finish":
                    s = story.load(cfg, sid)
                    return self._json(story.finish(cfg, sid, body.get("timeline") or s["timeline"],
                                                   body.get("marks") or s["marks"], str(body.get("mode") or "auto")))
                if url.path == "/api/story/approve":
                    conn = connect(cfg.db_path)
                    try:
                        return self._json(story.approve(cfg, conn, sid, body.get("drafts") or []))
                    finally:
                        conn.close()
                if url.path == "/api/story/discard":
                    story.discard(cfg, sid)
                    return self._json({"ok": True})
            except ValueError as e:
                return self._json({"error": str(e)}, 400)
            return self._json({"error": "not found"}, 404)

        def _search(self, qs):
            from .search import Filters, search

            f = Filters(
                orientation=qs.get("orientation") or None,
                min_broll=int(qs["min_broll"]) if qs.get("min_broll") else None,
                date_from=qs.get("from") or None, date_to=qs.get("to") or None,
                place=qs.get("place") or None, root_kind=qs.get("root") or None, kind=qs.get("kind") or None,
                exclude_issues=tuple(x for x in (qs.get("exclude") or "").split(",") if x),
                collection=qs.get("collection") or None, roles=tuple(x for x in [qs.get("role")] if x),
                fresh=qs.get("fresh") == "1",
            )
            conn = connect(cfg.db_path)
            try:
                results = search(cfg, conn, qs.get("q", ""), f, limit=int(qs.get("limit", 40)))
            finally:
                conn.close()
            for r in results:
                if r["thumbnail"]:
                    r["thumb_url"] = "/api/thumb?p=" + Path(r["thumbnail"]).resolve().relative_to(library).as_posix()
                r["media_url"] = f"/api/media/{r['media_id']}"
            return self._json({"results": results})

    return Handler


def serve(cfg: Config, port: int = 8765, open_browser: bool = True) -> None:
    connect(cfg.db_path).close()  # create the database on first run
    job = Job(cfg)
    server = None
    for p in range(port, port + 10):
        try:
            server = ThreadingHTTPServer(("127.0.0.1", p), make_handler(cfg, job))
            break
        except OSError:  # address in use: usually a dashboard still running in another window
            if p == port:
                print(f"Port {port} is busy: another dashboard (crag ui) is probably still running in another "
                      f"Terminal window. Press Ctrl+C there to stop it (it may be an older version), or run\n"
                      f"  lsof -ti :{port} | xargs kill\nto stop it from here. Starting on the next free port instead.")
    if server is None:
        raise SystemExit(f"Ports {port}-{port + 9} are all busy; close other dashboards and try again.")
    port = server.server_address[1]
    url = f"http://127.0.0.1:{port}"
    print(f"Content-RAG dashboard: {url}  (Ctrl+C to quit)")
    if open_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        job.stop.set()
        print("\nStopping…")
        time.sleep(0.2)
