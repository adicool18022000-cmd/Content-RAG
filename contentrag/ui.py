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

from .config import Config
from .db import connect
from .util import source_path

HTML = Path(__file__).with_name("ui.html")
STEPS = ("scan", "prep", "transcribe", "describe", "retry", "embed", "vault", "all")


class Job:
    """At most one pipeline job runs at a time, in a background thread."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.lock = threading.Lock()
        self.lines: list[str] = []
        self.name: str | None = None
        self.running = False
        self.stop = threading.Event()

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
        conn = connect(self.cfg.db_path)
        try:
            steps = ["scan", "prep", "transcribe", "describe", "embed", "vault"] if step == "all" else [step]
            for s in steps:
                if self.stop.is_set():
                    self.log("[ui] stopped")
                    break
                self.log(f"[ui] ── {s} ──")
                result = self._step(s, conn)
                self.log(f"[ui] {s} finished: {json.dumps(result, ensure_ascii=False)}")
        except SystemExit as e:  # friendly errors from the pipeline (missing packages etc.)
            self.log(f"[ui] {e}")
        except Exception as e:
            self.log(f"[ui] ERROR: {e}")
            self.log(traceback.format_exc(limit=3))
        finally:
            conn.close()
            with self.lock:
                self.running = False

    def _step(self, s: str, conn):
        cfg, log = self.cfg, self.log
        if s == "scan":
            from .scan import scan

            return scan(cfg, conn, log=log)
        if s == "prep":
            from .prep import prep

            return prep(cfg, conn, log=log)
        if s == "transcribe":
            if not cfg.transcribe_enabled:
                return "skipped (transcribe.enabled = false)"
            from .transcribe import transcribe

            return transcribe(cfg, conn, log=log)
        if s in ("describe", "retry"):
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
        raise ValueError(s)


def status(cfg: Config, job: Job) -> dict:
    conn = connect(cfg.db_path)
    try:
        q = lambda sql, *a: conn.execute(sql, a).fetchone()[0]  # noqa: E731
        out = {
            "library": str(cfg.library_dir),
            "backend": cfg.backend,
            "model": cfg.gemini_model if cfg.backend == "gemini" else cfg.model,
            "key_env": cfg.gemini_api_key_env if cfg.backend == "gemini" else "ANTHROPIC_API_KEY",
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
        }
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
                        if cfg.backend == "gemini":
                            from . import gemini

                            return self._json(gemini.estimate(cfg, conn))
                        from . import describe

                        return self._json(describe.estimate(cfg, conn))
                    finally:
                        conn.close()
                if url.path == "/api/models":
                    from . import gemini

                    return self._json({"models": gemini.list_models(cfg), "configured": cfg.gemini_model})
                if url.path == "/api/search":
                    return self._search(qs)
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
                ok, msg = job.start(step)
                return self._json({"ok": ok, "message": msg}, 200 if ok else 409)
            if url.path == "/api/stop":
                job.stop.set()
                job.log("[ui] stop requested — the current request batch finishes first")
                return self._json({"ok": True})
            return self._json({"error": "not found"}, 404)

        def _search(self, qs):
            from .search import Filters, search

            f = Filters(
                orientation=qs.get("orientation") or None,
                min_broll=int(qs["min_broll"]) if qs.get("min_broll") else None,
                date_from=qs.get("from") or None, date_to=qs.get("to") or None,
                place=qs.get("place") or None, root_kind=qs.get("root") or None, kind=qs.get("kind") or None,
                exclude_issues=tuple(x for x in (qs.get("exclude") or "").split(",") if x),
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
    server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(cfg, job))
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
