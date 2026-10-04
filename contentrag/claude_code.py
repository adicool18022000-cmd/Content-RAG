"""AI analysis through Claude Code (the `claude` command), billed to a Claude Pro/Max subscription.

[describe] backend = "claude-code". Same contact sheets + prompt + schema as the Claude API backend
(describe.py), but each request runs as one headless `claude -p` call instead of an API request, so
no API key or API credit is needed: it counts against the subscription's usage limits.

- Login once in a terminal: `claude` then /login (or `claude auth login`). `claude auth status`
  must say loggedIn. ANTHROPIC_API_KEY is removed from the environment of these calls so they
  never fall back to (paid) API billing by accident.
- Usage limit reached (5-hour / weekly window): requests stay pending, no attempt is used, and
  Autopilot sleeps until the window resets (time read from Claude Code's rate-limit events).
- Not logged in / unknown model: stops with a clear message instead of failing every file.
- Only the frames already extracted by `crag prep` are sent, so the footage drive is not needed
  for this step.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from .config import Config
from .db import MAX_ATTEMPTS
from .describe import build_params, plan_requests, store_result

TIMEOUT = 15 * 60


class ClaudeCodeError(RuntimeError):
    def __init__(self, msg: str, code=None, resume_at: float | None = None):
        super().__init__(msg)
        self.code = code
        self.resume_at = resume_at


def cli() -> str | None:
    return shutil.which("claude")


def _env() -> dict:
    env = dict(os.environ)
    for k in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):  # use the subscription login, never API billing
        env.pop(k, None)
    return env


_auth_cache: dict = {}


def auth_status(max_age: float = 30.0) -> dict:
    """{'installed', 'logged_in', 'method', 'error'} without sending any prompt (cached briefly: the
    dashboard asks every few seconds)."""
    hit = _auth_cache.get("v")
    if hit and time.time() - hit[0] < max_age:
        return hit[1]
    res = _auth_status()
    _auth_cache["v"] = (time.time(), res)
    return res


def _auth_status() -> dict:
    exe = cli()
    if not exe:
        return {"installed": False, "logged_in": False,
                "error": "Claude Code is not installed (npm install -g @anthropic-ai/claude-code)"}
    try:
        r = subprocess.run([exe, "auth", "status"], capture_output=True, text=True, timeout=60, env=_env())
        info = json.loads(r.stdout or "{}")
    except (subprocess.TimeoutExpired, ValueError, OSError) as e:
        return {"installed": True, "logged_in": False, "error": f"couldn't read login status: {e}"}
    ok = bool(info.get("loggedIn"))
    return {"installed": True, "logged_in": ok, "method": info.get("authMethod"),
            "error": None if ok else "Claude Code is not logged in: run `claude` and type /login"}


def _classify(text: str, resume_at: float | None) -> ClaudeCodeError:
    low = text.lower()
    if any(k in low for k in ("usage limit", "limit reached", "rate limit", "rate_limit", "overloaded",
                               "429", "too many requests", "hit your limit", "out of extra usage")):
        return ClaudeCodeError(f"Claude usage limit reached: {text[:200]}", 429, resume_at)
    if any(k in low for k in ("/login", "not logged in", "authentication", "invalid api key", "oauth",
                               "unauthorized", "401", "credit balance")):
        return ClaudeCodeError(f"Claude Code login problem: {text[:200]} (run `claude`, then /login)", 401)
    if ("model" in low and any(k in low for k in ("not found", "not available", "invalid", "does not exist"))) \
            or "404" in low:
        return ClaudeCodeError(f"Model problem: {text[:200]} (check [claude_code] model)", 404)
    if any(k in low for k in ("econnrefused", "enotfound", "etimedout", "econnreset", "fetch failed",
                               "network", "connection error", "socket hang up", "getaddrinfo")):
        return ClaudeCodeError(f"no connection to Claude: {text[:200]}", "network")
    return ClaudeCodeError(text[:400] or "Claude Code failed without a message")


def ask(cfg: Config, content: list[dict], schema: dict, system: str, model: str | None = None) -> tuple[dict, dict]:
    """One headless Claude Code call with images. Returns (json answer, usage). Raises ClaudeCodeError."""
    exe = cli()
    if not exe:
        raise ClaudeCodeError("Claude Code is not installed (npm install -g @anthropic-ai/claude-code)", 401)
    cmd = [exe, "-p", "--input-format", "stream-json", "--output-format", "stream-json", "--verbose",
           "--model", model or cfg.cc_model, "--tools", "", "--strict-mcp-config", "--no-session-persistence",
           "--system-prompt", system, "--json-schema", json.dumps(schema)]
    if cfg.cc_effort:
        cmd += ["--effort", cfg.cc_effort]
    msg = {"type": "user", "message": {"role": "user", "content": content}}
    with tempfile.TemporaryDirectory(prefix="crag-cc-") as cwd:  # empty folder: no project files or CLAUDE.md
        try:
            r = subprocess.run(cmd, input=json.dumps(msg) + "\n", capture_output=True, text=True,
                               timeout=TIMEOUT, cwd=cwd, env=_env())
        except subprocess.TimeoutExpired:
            raise ClaudeCodeError(f"Claude Code took longer than {TIMEOUT // 60} min", "network")
    result, resume_at = None, None
    for line in r.stdout.splitlines():
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if ev.get("type") == "rate_limit_event":
            info = ev.get("rate_limit_info") or {}
            if info.get("status") not in (None, "allowed", "allowed_warning"):
                resume_at = info.get("resetsAt") or resume_at
        elif ev.get("type") == "result":
            result = ev
    if result is None:
        raise _classify((r.stderr or r.stdout or "").strip()[-600:] or f"exit code {r.returncode}", resume_at)
    usage = result.get("usage") or {}
    used = {"in": int(usage.get("input_tokens") or 0) + int(usage.get("cache_read_input_tokens") or 0)
            + int(usage.get("cache_creation_input_tokens") or 0),
            "out": int(usage.get("output_tokens") or 0)}
    if result.get("is_error") or result.get("subtype") != "success":
        text = str(result.get("result") or result.get("subtype") or "")
        if result.get("stop_reason") == "refusal" or "refus" in text.lower():
            raise ClaudeCodeError(f"refused: {text[:200]}", "refusal")
        raise _classify(text, resume_at)
    data = result.get("structured_output")
    if data is None:  # older Claude Code: the JSON comes back as text
        text = (result.get("result") or "").strip()
        if text.startswith("```"):
            text = text.strip("`").removeprefix("json").strip()
        try:
            data = json.loads(text)
        except ValueError:
            raise ClaudeCodeError(f"answer was not valid JSON: {text[:200]}")
    return data, used


def run(cfg: Config, conn, limit: int | None = None, retry: bool = False, log=print,
        stop: threading.Event | None = None) -> dict:
    """Describe everything pending (retry=True: what failed before, with retry_model). Resumable."""
    plan_requests(cfg, conn)
    statuses = ("error", "refused") if retry else ("pending",)
    reqs = conn.execute(
        f"SELECT * FROM requests WHERE status IN ({','.join('?' * len(statuses))}) "
        "AND coalesce(attempts,0) < ? ORDER BY custom_id", (*statuses, MAX_ATTEMPTS)).fetchall()
    if limit:
        reqs = reqs[:limit]
    stats = {"done": 0, "refused": 0, "error": 0, "quota": False}
    if not reqs:
        return stats
    model = cfg.cc_retry_model if retry and cfg.cc_retry_model else cfg.cc_model
    log(f"[claude-code] {len(reqs)} requests with {model} on your Claude subscription, {cfg.cc_workers} at a time")
    jobs = iter(reqs)
    quota_hits = network_hits = 0
    fatal = None
    resume_at = None
    with ThreadPoolExecutor(max_workers=cfg.cc_workers) as pool:
        futures = {}

        def submit_next():
            while True:
                if (stop is not None and stop.is_set()) or quota_hits or network_hits >= 3 or fatal:
                    return
                req = next(jobs, None)
                if req is None:
                    return
                try:  # built here: the database connection stays on this thread
                    params = build_params(cfg, conn, req, model)
                except Exception as e:
                    conn.execute("UPDATE requests SET status='error', error=?, attempts=coalesce(attempts,0)+1 "
                                 "WHERE custom_id=?", (f"build: {e}", req["custom_id"]))
                    conn.commit()
                    stats["error"] += 1
                    continue
                schema = params["output_config"]["format"]["schema"]
                content = params["messages"][0]["content"]
                futures[pool.submit(ask, cfg, content, schema, params["system"], model)] = req
                return

        for _ in range(cfg.cc_workers):
            submit_next()
        done = 0
        while futures:
            fut = next(as_completed(list(futures)))
            req = futures.pop(fut)
            cid = req["custom_id"]
            err, usage, data = None, {"in": 0, "out": 0}, None
            try:
                data, usage = fut.result()
                status = "done"
            except ClaudeCodeError as e:
                err, status = str(e), "error"
                # Not the file's fault -> stays "pending", no attempt used, picked up again later.
                if e.code == 429:
                    quota_hits += 1
                    resume_at = e.resume_at or resume_at
                    status = "pending"
                elif e.code == "network":
                    network_hits += 1
                    status = "pending"
                elif e.code in (401, 404):
                    fatal, status = str(e), "pending"
                elif e.code == "refusal":
                    status = "refused"
            except Exception as e:  # noqa: BLE001
                err, status = str(e), "error"
            conn.execute("UPDATE requests SET model=?, in_tokens=coalesce(in_tokens,0)+?, "
                         "out_tokens=coalesce(out_tokens,0)+? WHERE custom_id=?",
                         (f"claude-code:{model}", usage["in"], usage["out"], cid))
            if status == "done":
                try:
                    store_result(cfg, conn, req, data)
                except Exception as e:
                    status, err = "error", f"could not save the answer: {e}"
            if status != "done":
                conn.execute("UPDATE requests SET status=?, error=?, attempts=coalesce(attempts,0)+? "
                             "WHERE custom_id=?", (status, err, int(status != "pending"), cid))
            conn.commit()
            stats[status] = stats.get(status, 0) + 1
            done += 1
            if status != "done":
                log(f"[claude-code] {done}/{len(reqs)} {cid}: {status} — {err}")
            else:
                network_hits = 0
                if done % 10 == 0 or done == len(reqs):
                    log(f"[claude-code] {done}/{len(reqs)} done")
            submit_next()
    if fatal:
        stats["fatal"] = fatal
        log(f"[claude-code] stopping: {fatal}")
    elif quota_hits:
        stats["quota"] = True
        if resume_at:
            stats["resume_at"] = float(resume_at)
        when = time.strftime("%H:%M", time.localtime(resume_at)) if resume_at else "later"
        log(f"[claude-code] subscription usage limit reached; continuing at {when}")
    elif network_hits >= 3:
        stats["network"] = True
        log("[claude-code] no connection to Claude; will continue when it's back")
    if stop is not None and stop.is_set():
        log("[claude-code] stopped; run again to continue where it left off")
    return stats


def estimate(cfg: Config, conn) -> dict:
    """Requests left. Subscription calls have no per-request price; the API estimate shows the size."""
    from .describe import estimate as api_estimate

    e = api_estimate(cfg, conn, model=cfg.model)
    return {"requests": e["requests"], "model": cfg.cc_model, "input_tokens": e["input_tokens"],
            "output_tokens": e["output_tokens"], "usd": 0.0,
            "note": "billed to your Claude subscription (usage limits), not per request"}
