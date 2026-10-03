"""Tidy the footage folders: duplicates aside, everything else into Year / Event-or-Collection folders.

Always three steps, and nothing is ever deleted:
    crag organize plan     -> library/organize/plan-<time>.json + .csv + .html to review
    crag organize apply    -> moves files exactly as planned (same drive only), records an undo log
    crag organize undo     -> puts every file back where it was

Target layout inside each footage folder (brand B-roll folders are left alone):
    2023/2023-05 Summer Stay Hostel/...        your own folder names are kept (date prefix added)
    2025/2025-11-14 Phuket - Fire show night/  big mixed folders (phone dumps) are split by trip/day
    _Duplicates/<old path>                     extra copies of files that exist elsewhere
The index follows the moves, so nothing has to be analysed again.
"""

from __future__ import annotations

import csv
import html
import json
import os
import re
from collections import Counter
from datetime import datetime
from pathlib import Path

from .config import Config
from .vault import _city, _dt, cluster_events

DUP_DIR = "_Duplicates"
SIDECARS = (".aae", ".xmp", ".thm", ".srt", ".lrf")
DUMP_DAYS = 45          # a "collection" spanning longer than this, in several cities, is split by trip
_BAD = re.compile(r'[\\/:*?"<>|#^\[\]]+')
_BACKUPISH = re.compile(r"(backup|copy|copies|duplicate|old|temp|whatsapp|download|export)", re.I)


def _clean(name: str, n: int = 70) -> str:
    return re.sub(r"\s+", " ", _BAD.sub(" ", name or "")).strip()[:n].strip() or "Untitled"


def _keeper(locs: list, collection_by_loc) -> dict:
    """Which copy of a duplicated file stays: the one in the most meaningful folder."""
    def score(loc):
        coll = collection_by_loc(loc["relpath"]) or ""
        return (bool(coll), -bool(_BACKUPISH.search(loc["relpath"])), len(coll), -len(loc["relpath"]))
    return max(locs, key=score)


def _is_dump(items: list) -> bool:
    dts = [d for d in (_dt(m["taken_at"]) for m in items) if d]
    if len(dts) < 2:
        return False
    cities = {_city(m["place"]) for m in items if m["place"]}
    return (max(dts) - min(dts)).days > DUMP_DAYS and len(cities) > 1


def _event_folder(ev: list) -> str:
    first = ev[0]
    places = Counter(_city(m["place"]) for m in ev if m["place"])
    city = places.most_common(1)[0][0] if places else ""
    titles = [m["title"] for m in ev if m["title"]]
    label = " - ".join(x for x in (city, titles[0] if titles else "") if x)
    return _clean(f"{first['taken_at'][:10]} {label}".strip())


def plan(cfg: Config, conn, log=print) -> dict:
    from .collections import collection_of

    archive_roots = {r.name: r for r in cfg.roots if r.kind == "archive" and r.mounted}
    moves: list[dict] = []
    taken: set[tuple[str, str]] = set()

    def target(root: str, rel: str) -> str:
        base, ext = os.path.splitext(rel)
        cand, k = rel, 2
        while (root, cand) in taken or (archive_roots[root].path / cand).exists():
            cand, k = f"{base} ({k}){ext}", k + 1
        taken.add((root, cand))
        return cand

    media = {m["id"]: m for m in conn.execute("SELECT * FROM media ORDER BY taken_at")}
    locs_by_media: dict[str, list] = {}
    for loc in conn.execute("SELECT root, relpath, media_id FROM locations ORDER BY root, relpath"):
        locs_by_media.setdefault(loc["media_id"], []).append(dict(loc))

    # 1. duplicates: keep the best-placed copy; the others go to _Duplicates in their own drive
    keepers: dict[str, dict] = {}
    for mid, locs in locs_by_media.items():
        keep = _keeper(locs, collection_of) if len(locs) > 1 else locs[0]
        keepers[mid] = keep
        for loc in locs:
            if loc is keep or loc["root"] not in archive_roots or loc["relpath"].startswith(DUP_DIR + "/"):
                continue
            moves.append({"media_id": mid, "root": loc["root"], "from": loc["relpath"],
                          "to": target(loc["root"], f"{DUP_DIR}/{loc['relpath']}"), "reason": "duplicate",
                          "kept": f"{keep['root']}:{keep['relpath']}"})

    # 2. everything else: Year / (collection or trip) / file
    groups: dict[str, list] = {}
    loose = []
    for mid, keep in keepers.items():
        m = media.get(mid)
        if m is None or keep["root"] not in archive_roots or not m["taken_at"]:
            continue
        coll = collection_of(keep["relpath"])
        (groups.setdefault(coll, []) if coll else loose).append(m)
    folder_of: dict[str, str] = {}
    for coll, items in groups.items():
        items.sort(key=lambda m: m["taken_at"])
        if _is_dump(items):
            loose += items
            continue
        name = _clean(f"{items[0]['taken_at'][:7]} {coll.replace(' / ', ' - ')}")
        for m in items:
            folder_of[m["id"]] = f"{items[0]['taken_at'][:4]}/{name}"
    loose.sort(key=lambda m: m["taken_at"])
    for ev in cluster_events(loose):
        name = _event_folder(ev)
        for m in ev:
            folder_of[m["id"]] = f"{ev[0]['taken_at'][:4]}/{name}"
    for mid, folder in folder_of.items():
        keep = keepers[mid]
        new_rel = f"{folder}/{Path(keep['relpath']).name}"
        if new_rel == keep["relpath"]:
            continue
        moves.append({"media_id": mid, "root": keep["root"], "from": keep["relpath"],
                      "to": target(keep["root"], new_rel), "reason": "organize"})

    out_dir = cfg.library_dir / "organize"
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = out_dir / f"plan-{stamp}.json"
    path.write_text(json.dumps({"created": stamp, "moves": moves}, ensure_ascii=False, indent=1), encoding="utf-8")
    with open(path.with_suffix(".csv"), "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["reason", "drive", "from", "to"])
        w.writerows([m["reason"], m["root"], m["from"], m["to"]] for m in moves)
    _plan_html(path.with_suffix(".html"), moves)
    dup = sum(1 for m in moves if m["reason"] == "duplicate")
    log(f"[organize] plan: {len(moves) - dup} files to reorganise, {dup} duplicates -> {DUP_DIR}/. "
        f"Review {path.with_suffix('.html')} then run `crag organize apply`.")
    return {"plan": str(path), "review": str(path.with_suffix(".html")), "moves": len(moves), "duplicates": dup}


def _plan_html(path: Path, moves: list[dict]) -> None:
    by_folder: dict[str, list] = {}
    for m in moves:
        key = f"{m['root']}: {m['to'].rsplit('/', 1)[0]}"
        by_folder.setdefault(key, []).append(m)
    parts = []
    for folder in sorted(by_folder):
        items = by_folder[folder]
        rows = "".join(f"<li>{html.escape(m['from'])}{' <i>(copy of ' + html.escape(m['kept']) + ')</i>' if m.get('kept') else ''}</li>"
                       for m in items[:30])
        more = f"<li>… and {len(items) - 30} more</li>" if len(items) > 30 else ""
        parts.append(f"<details><summary><b>{html.escape(folder)}</b> — {len(items)} files</summary><ul>{rows}{more}</ul></details>")
    path.write_text(f"""<!doctype html><meta charset="utf-8"><title>Organize plan</title>
<style>body{{font:14px system-ui;margin:20px;max-width:1000px}}details{{margin:6px 0}}li{{font-size:12px;color:#555}}</style>
<h2>Organize plan — {len(moves)} moves</h2>
<p>Nothing has been moved yet. Folders below are the new locations; open one to see which files go there.
Duplicates go to <code>{DUP_DIR}/</code> (nothing is deleted). Run <code>crag organize apply</code> to do it,
<code>crag organize undo</code> to reverse it afterwards.</p>{''.join(parts)}""", encoding="utf-8")


def latest_plan(cfg: Config) -> Path:
    plans = sorted((cfg.library_dir / "organize").glob("plan-*.json"))
    if not plans:
        raise SystemExit("No plan yet. Run `crag organize plan` first.")
    return plans[-1]


def apply(cfg: Config, conn, plan_path: Path | None = None, log=print) -> dict:
    from .autopilot import pipeline_lock

    plan_path = plan_path or latest_plan(cfg)
    moves = json.loads(plan_path.read_text())["moves"]
    done, skipped = [], []
    with pipeline_lock(cfg):
        for mv in moves:
            root = cfg.root(mv["root"])
            if root is None or not root.mounted:
                skipped.append({**mv, "why": "drive not plugged in"})
                continue
            src, dst = root.path / mv["from"], root.path / mv["to"]
            if not src.exists():
                skipped.append({**mv, "why": "source no longer there"})
                continue
            if dst.exists():
                skipped.append({**mv, "why": "target already exists"})
                continue
            dst.parent.mkdir(parents=True, exist_ok=True)
            try:
                os.rename(src, dst)  # same drive: instant and safe; never copies+deletes
            except OSError as e:
                skipped.append({**mv, "why": str(e)})
                continue
            for ext in SIDECARS:  # iPhone edit / metadata companions travel with their file
                for side in (src.with_suffix(ext), src.with_suffix(ext.upper())):
                    if side.exists() and not dst.with_suffix(side.suffix).exists():
                        os.rename(side, dst.with_suffix(side.suffix))
            _relocate(conn, mv["root"], mv["from"], mv["to"])
            done.append(mv)
            if len(done) % 200 == 0:
                conn.commit()
        conn.commit()
        _remove_empty_dirs(cfg, {m["root"] for m in done}, {m["from"] for m in done})
    log_path = plan_path.with_name(plan_path.name.replace("plan-", "applied-"))
    log_path.write_text(json.dumps({"plan": plan_path.name, "done": done, "skipped": skipped},
                                   ensure_ascii=False, indent=1), encoding="utf-8")
    log(f"[organize] moved {len(done)} files, skipped {len(skipped)}. Undo with `crag organize undo`.")
    return {"moved": len(done), "skipped": len(skipped), "log": str(log_path)}


def undo(cfg: Config, conn, log=print) -> dict:
    from .autopilot import pipeline_lock

    logs = sorted((cfg.library_dir / "organize").glob("applied-*.json"))
    if not logs:
        raise SystemExit("Nothing to undo.")
    data = json.loads(logs[-1].read_text())
    back = 0
    with pipeline_lock(cfg):
        for mv in reversed(data["done"]):
            root = cfg.root(mv["root"])
            if root is None or not root.mounted:
                continue
            src, dst = root.path / mv["to"], root.path / mv["from"]
            if src.exists() and not dst.exists():
                dst.parent.mkdir(parents=True, exist_ok=True)
                os.rename(src, dst)
                for ext in SIDECARS:
                    for side in (src.with_suffix(ext), src.with_suffix(ext.upper())):
                        if side.exists():
                            os.rename(side, dst.with_suffix(side.suffix))
                _relocate(conn, mv["root"], mv["to"], mv["from"])
                back += 1
        conn.commit()
        _remove_empty_dirs(cfg, {m["root"] for m in data["done"]}, {m["to"] for m in data["done"]})
    logs[-1].rename(logs[-1].with_name(logs[-1].name.replace("applied-", "undone-")))
    log(f"[organize] moved {back} files back")
    return {"restored": back}


def _relocate(conn, root: str, old: str, new: str) -> None:
    conn.execute("UPDATE locations SET relpath=? WHERE root=? AND relpath=?", (new, root, old))
    conn.execute("UPDATE media SET relpath=? WHERE root=? AND relpath=?", (new, root, old))
    if new.startswith(DUP_DIR + "/"):  # the main copy is the one that stayed, not the duplicate
        row = conn.execute("SELECT media_id FROM locations WHERE root=? AND relpath=?", (root, new)).fetchone()
        if row:
            other = conn.execute("SELECT root, relpath FROM locations WHERE media_id=? AND relpath NOT LIKE ? "
                                 "LIMIT 1", (row["media_id"], DUP_DIR + "/%")).fetchone()
            if other:
                conn.execute("UPDATE media SET root=?, relpath=? WHERE id=?",
                             (other["root"], other["relpath"], row["media_id"]))


def _remove_empty_dirs(cfg: Config, roots: set[str], old_paths: set[str]) -> None:
    """Delete folders that became empty because their files moved (never anything with files left)."""
    for name in roots:
        root = cfg.root(name)
        if root is None:
            continue
        dirs = {str(Path(p).parent) for p in old_paths}
        for d in sorted(dirs, key=lambda x: -x.count("/")):
            path = root.path / d
            while path != root.path and path.is_dir():
                leftovers = [x for x in path.iterdir() if x.name not in (".DS_Store",)]
                if leftovers:
                    break
                for x in path.iterdir():
                    x.unlink()
                path.rmdir()
                path = path.parent
