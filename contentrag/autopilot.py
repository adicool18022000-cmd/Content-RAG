"""Autopilot: run the whole pipeline unattended (overnight) until everything is done.

    scan -> prep -> (transcribe) -> AI analysis -> retries -> search index -> vault

- Waits out Gemini quota / rate limits (15 min, doubling to 1 h) instead of stopping.
- Retries failed items up to MAX_ATTEMPTS times, then lists them as problems.
- Respects the spending cap ([gemini] budget_usd).
- Keeps the Mac awake (`caffeinate`) while it runs.
- Writes its state to library_dir/autopilot.json and a log to library_dir/logs/, so a crash,
  a closed laptop or a power cut can be resumed: everything already done is kept.
- Only one pipeline can run per library at a time (a lock file in library_dir).
"""

from __future__ import annotations

import fcntl
import json
import os
import platform
import shutil
import subprocess
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path

from .config import Config
from .db import MAX_ATTEMPTS, connect, failures

QUOTA_WAITS = [15 * 60, 30 * 60, 60 * 60]  # then every hour
NETWORK_WAIT = 5 * 60


def stop_file(cfg: Config) -> Path:
    """Creating this file stops an Autopilot running in any process (dashboard or terminal)."""
    return cfg.library_dir / "STOP"


def preflight(cfg: Config, log=print) -> list[str]:
    """Problems that would make every file fail. Checked before an unattended run starts,
    so a typo can't burn through all retry attempts overnight."""
    problems = []
    for tool in ("ffmpeg", "ffprobe"):
        if not shutil.which(tool):
            problems.append(f"{tool} is not installed (brew install ffmpeg)")
    if not any(r.mounted for r in cfg.roots):
        problems.append("none of the footage folders in contentrag.toml are available (drives plugged in?)")
    problems += backend_problems(cfg, log)
    return problems


def backend_problems(cfg: Config, log=print) -> list[str]:
    """Is the chosen AI backend usable right now (key set / logged in / model available)?"""
    problems = []
    if cfg.backend == "gemini":
        if not os.environ.get(cfg.gemini_api_key_env):
            problems.append(f"{cfg.gemini_api_key_env} is not set in this terminal")
        else:
            from . import gemini

            try:
                models = gemini.list_models(cfg)
                if cfg.gemini_model not in models:
                    flash = [m for m in models if "flash" in m][:8]
                    problems.append(f"Gemini model '{cfg.gemini_model}' isn't available to this key; "
                                    f"set [gemini] model to one of: {', '.join(flash)}")
            except Exception as e:  # offline right now: not fatal, the run waits for the network
                if "network" in str(e).lower() or "connect" in str(e).lower():
                    log(f"[autopilot] couldn't reach Gemini yet ({e}); will keep trying")
                else:
                    problems.append(f"Gemini key check failed: {e}")
    elif cfg.backend == "claude-code":
        from . import claude_code

        st = claude_code.auth_status()
        if not st["logged_in"]:
            problems.append(st["error"])
    elif cfg.backend == "claude":
        if not os.environ.get("ANTHROPIC_API_KEY"):
            problems.append("ANTHROPIC_API_KEY is not set in this terminal (the Claude API backend needs API "
                            "credit; to use a Pro/Max subscription choose the claude-code backend)")
    return problems


class Busy(RuntimeError):
    pass


@contextmanager
def pipeline_lock(cfg: Config):
    """Exclusive per-library lock. Released automatically if the process dies."""
    cfg.library_dir.mkdir(parents=True, exist_ok=True)
    f = open(cfg.library_dir / ".crag.lock", "w")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        f.close()
        raise Busy("Another crag run is already working on this library (dashboard or terminal). "
                   "Wait for it to finish or stop it first.")
    try:
        f.write(str(os.getpid()))
        f.flush()
        yield
    finally:
        fcntl.flock(f, fcntl.LOCK_UN)
        f.close()


def lock_held(cfg: Config) -> bool:
    try:
        with pipeline_lock(cfg):
            return False
    except Busy:
        return True


def state_path(cfg: Config) -> Path:
    return cfg.library_dir / "autopilot.json"


def read_state(cfg: Config) -> dict:
    try:
        return json.loads(state_path(cfg).read_text())
    except (OSError, ValueError):
        return {}


def counts(conn) -> dict:
    q = lambda sql, *a: conn.execute(sql, a).fetchone()[0]  # noqa: E731
    return {
        "media": q("SELECT count(*) FROM media"),
        "prepped": q("SELECT count(*) FROM media WHERE prepped=1"),
        "prep_left": q("SELECT count(*) FROM media WHERE prepped=0 AND coalesce(attempts,0) < ?", MAX_ATTEMPTS),
        "described": q("SELECT count(*) FROM media WHERE described=1"),
        "requests": q("SELECT count(*) FROM requests"),
        "requests_done": q("SELECT count(*) FROM requests WHERE status='done'"),
        "requests_left": q("SELECT count(*) FROM requests WHERE status='pending'"),
        "retryable": q("SELECT count(*) FROM requests WHERE status IN ('error','refused') "
                       "AND coalesce(attempts,0) < ?", MAX_ATTEMPTS),
        "gave_up": q("SELECT count(*) FROM requests WHERE status IN ('error','refused') "
                     "AND coalesce(attempts,0) >= ?", MAX_ATTEMPTS)
                   + q("SELECT count(*) FROM media WHERE prepped=0 AND coalesce(attempts,0) >= ?", MAX_ATTEMPTS),
    }


class Autopilot:
    def __init__(self, cfg: Config, log=print, stop: threading.Event | None = None, hours: float = 24.0):
        self.cfg = cfg
        self.stop = stop or threading.Event()
        self.deadline = time.time() + hours * 3600
        self.state: dict = {}
        self._log = log
        self._logfile = None
        self._done = False
        self.fatal: str | None = None

    def _watch_stop_file(self) -> None:
        while not self._done:
            if stop_file(self.cfg).exists():
                self.log("[autopilot] stop requested")
                self.stop.set()
                return
            time.sleep(3)

    # ------------------------------------------------------------- plumbing
    def log(self, msg: str) -> None:
        self._log(msg)
        if self._logfile:
            self._logfile.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} {msg}\n")
            self._logfile.flush()

    def save(self, **kw) -> None:
        self.state.update(kw, updated_at=datetime.now().isoformat(timespec="seconds"))
        tmp = state_path(self.cfg).with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state, indent=2))
        tmp.replace(state_path(self.cfg))

    def phase(self, name: str, conn, done_key: str | None = None) -> None:
        c = counts(conn)
        self.save(phase=name, phase_started=time.time(), phase_done_at_start=c.get(done_key) if done_key else None,
                  phase_done_key=done_key, status="running", waiting_until=None)
        self.log(f"[autopilot] ── {name} ──")

    def wait(self, seconds: float, why: str) -> bool:
        """Sleep in small steps so Stop works. Returns False if stopped or out of time."""
        until = min(time.time() + seconds, self.deadline)
        self.save(status="waiting", waiting_until=datetime.fromtimestamp(until).isoformat(timespec="seconds"),
                  message=why)
        self.log(f"[autopilot] waiting until {datetime.fromtimestamp(until):%H:%M}: {why}")
        while time.time() < until:
            if self.stop.is_set():
                return False
            time.sleep(min(10, max(0.0, until - time.time())))
        self.save(status="running", waiting_until=None, message=None)
        return time.time() < self.deadline and not self.stop.is_set()

    def _caffeinate(self):
        if platform.system() == "Darwin" and shutil.which("caffeinate"):
            # -i idle sleep, -m disk sleep, -s system sleep on AC power; ends with this process
            return subprocess.Popen(["caffeinate", "-ims", "-w", str(os.getpid())])
        return None

    # ------------------------------------------------------------- main
    def run(self) -> dict:
        with pipeline_lock(self.cfg):
            logs = self.cfg.library_dir / "logs"
            logs.mkdir(parents=True, exist_ok=True)
            self._logfile = open(logs / f"autopilot-{datetime.now():%Y%m%d-%H%M%S}.log", "a")
            awake = self._caffeinate()
            stop_file(self.cfg).unlink(missing_ok=True)
            watcher = threading.Thread(target=self._watch_stop_file, daemon=True)
            watcher.start()
            self.state = {"started_at": datetime.now().isoformat(timespec="seconds"),
                          "deadline": datetime.fromtimestamp(self.deadline).isoformat(timespec="seconds")}
            self.save(status="running")
            conn = connect(self.cfg.db_path)
            try:
                problems = preflight(self.cfg, self.log)
                if problems:
                    msg = "Can't start: " + " · ".join(problems)
                    self.save(status="error", message=msg)
                    self.log(f"[autopilot] {msg}")
                    return {"summary": msg, "counts": counts(conn), "problems": failures(conn, 200)}
                result = self._run(conn)
                status = "stopped" if self.stop.is_set() else ("needs attention" if self.fatal else "finished")
                self.save(status=status, phase=None, result=result, message=result.get("summary"))
                self.log(f"[autopilot] {status}: {result.get('summary')}")
                return result
            except Exception as e:
                self.save(status="error", message=str(e))
                self.log(f"[autopilot] ERROR: {e}")
                raise
            finally:
                self._done = True
                stop_file(self.cfg).unlink(missing_ok=True)
                conn.close()
                if awake:
                    awake.terminate()
                self._logfile.close()
                self._logfile = None

    def _run(self, conn) -> dict:
        from .prep import prep
        from .scan import scan

        cfg = self.cfg
        quota_wait = 0
        rounds = 0
        while not self.stop.is_set() and time.time() < self.deadline:
            rounds += 1
            self.save(round=rounds)
            # 1. find new files (also picks up files that were still copying last time)
            self.phase("scan", conn)
            scan(cfg, conn, log=self.log)
            if self.stop.is_set():
                break
            # 2. thumbnails / cuts
            self.phase("prep", conn, "prepped")
            res = prep(cfg, conn, log=self.log, stop=self.stop)
            if res.get("disk_full"):
                self.log("[autopilot] library drive is nearly full; free space and run Autopilot again")
                break
            if self.stop.is_set():
                break
            # 2b. face grouping (local, free); never fatal - it only powers the people hide list
            if cfg.faces_enabled:
                self.phase("faces", conn)
                try:
                    from .faces import find_faces, refine_hidden

                    find_faces(cfg, conn, log=self.log, stop=self.stop)
                    refine_hidden(cfg, conn, log=self.log, stop=self.stop)
                except (Exception, SystemExit) as e:
                    self.log(f"[autopilot] face grouping skipped: {e}")
                if self.stop.is_set():
                    break
            # 3. optional local transcription
            if cfg.transcribe_enabled:
                from .transcribe import transcribe

                self.phase("transcribe", conn)
                transcribe(cfg, conn, log=self.log)
            # 4. AI analysis
            self.phase("analyse", conn, "requests_done")
            res = self._describe(conn, retry=False)
            if res.get("fatal"):
                self.fatal = res["fatal"]
                break
            if res.get("budget"):
                break
            if res.get("network"):
                if not self.wait(NETWORK_WAIT, f"no internet connection to {self.ai_name}"):
                    break
                continue
            if res.get("quota"):
                wait = QUOTA_WAITS[min(quota_wait, len(QUOTA_WAITS) - 1)]
                if res.get("resume_at"):  # Claude subscription: sleep until the usage window resets
                    wait = min(max(60.0, res["resume_at"] - time.time() + 60), 6 * 3600)
                quota_wait += 1
                if not self.wait(wait, f"{self.ai_name} quota / usage limit reached"):
                    break
                continue
            quota_wait = 0
            # 5. retry what failed (each item at most MAX_ATTEMPTS times in total)
            c = counts(conn)
            if c["retryable"]:
                self.phase("retry", conn, "requests_done")
                res = self._describe(conn, retry=True)
                if res.get("fatal"):
                    self.fatal = res["fatal"]
                    break
                if res.get("quota") or res.get("network"):
                    continue
                if res.get("budget"):
                    break
            c = counts(conn)
            if c["requests_left"] == 0 and c["retryable"] == 0 and c["prep_left"] == 0:
                break
            if not res.get("done") and rounds > 1:
                # nothing moved: whatever is left is on unplugged drives or keeps failing
                break
        return self._finish(conn)

    def _describe(self, conn, retry: bool) -> dict:
        if self.cfg.backend == "claude-code":
            from . import claude_code

            return claude_code.run(self.cfg, conn, retry=retry, log=self.log, stop=self.stop)
        if self.cfg.backend == "gemini":
            from . import gemini

            return gemini.run(self.cfg, conn, retry=retry, log=self.log, stop=self.stop)
        from . import describe

        if retry:
            return describe.run_sync(self.cfg, conn, limit=1000, model=self.cfg.retry_model,
                                     statuses=("refused", "error"), log=self.log)
        ids = describe.submit(self.cfg, conn, log=self.log)
        while ids and not self.stop.is_set():
            res = describe.collect(self.cfg, conn, log=self.log)
            if not res.get("still_running"):
                return res
            if not self.wait(600, "Claude batch still processing"):
                break
        return {}

    @property
    def ai_name(self) -> str:
        return {"gemini": "Gemini", "claude-code": "Claude (subscription)", "claude": "Claude API"}[self.cfg.backend]

    def _finish(self, conn) -> dict:
        cfg = self.cfg
        if not self.stop.is_set():
            from .embed import embed, load_model

            self.phase("search index", conn)
            try:
                if load_model(cfg.embed_model) is not None:
                    embed(cfg, conn, log=self.log)
                else:
                    self.log("[autopilot] search-by-meaning skipped (sentence-transformers not installed); "
                             "keyword search works")
            except Exception as e:  # never lose the vault because of the optional index
                self.log(f"[autopilot] search index failed: {e}")
            self.phase("vault", conn)
            from .vault import build_vault

            build_vault(cfg, conn, log=self.log)
        c = counts(conn)
        unmounted = [r.name for r in cfg.roots if not r.mounted]
        parts = [f"{c['described']}/{c['media']} items analysed"]
        if self.fatal:
            parts.insert(0, f"STOPPED EARLY: {self.fatal}")
        if cfg.backend == "gemini" and cfg.gemini_budget_usd:
            from . import gemini as _g

            if _g.spent(cfg, conn)["usd"] >= cfg.gemini_budget_usd:
                parts.append(f"spending cap ${cfg.gemini_budget_usd:g} reached (raise [gemini] budget_usd)")
        if c["requests_left"]:
            parts.append(f"{c['requests_left']} still waiting"
                         + (f" (drives not plugged in: {', '.join(unmounted)})" if unmounted else ""))
        if c["gave_up"]:
            parts.append(f"{c['gave_up']} failed {MAX_ATTEMPTS}× and were skipped (see Problems)")
        if self.cfg.backend == "gemini":
            from . import gemini

            parts.append(f"spent ${gemini.spent(cfg, conn)['usd']}")
        return {"summary": "; ".join(parts), "counts": c, "problems": failures(conn, 200)}


def eta(cfg: Config, conn, state: dict) -> dict | None:
    """Progress and a rough time-left estimate for the current phase."""
    key = state.get("phase_done_key")
    if not key or state.get("status") != "running":
        return None
    c = counts(conn)
    total = {"prepped": c["media"], "requests_done": c["requests"]}.get(key)
    done = c.get(key)
    if not total:
        return None
    start_done = state.get("phase_done_at_start") or 0
    elapsed = time.time() - (state.get("phase_started") or time.time())
    rate = (done - start_done) / elapsed if elapsed > 30 else 0
    left = (total - done) / rate if rate > 0 else None
    return {"done": done, "total": total,
            "eta": (datetime.now() + timedelta(seconds=left)).strftime("%H:%M") if left else None}
