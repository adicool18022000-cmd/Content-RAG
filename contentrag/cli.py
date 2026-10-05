"""`crag` command line."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys

from pathlib import Path

from . import describe as describe_mod
from .config import load_config
from .db import connect


def _open(args):
    cfg = load_config(args.config)
    return cfg, connect(cfg.db_path)


def cmd_status(args):
    cfg, conn = _open(args)
    q = lambda sql: conn.execute(sql).fetchone()[0]  # noqa: E731
    print(f"Library: {cfg.library_dir}")
    for r in cfg.roots:
        print(f"  root {r.name:<16} {'mounted' if r.mounted else 'NOT MOUNTED':<12} {r.kind:<8} {r.path}")
    videos = q("SELECT count(*) FROM media WHERE kind='video'")
    photos = q("SELECT count(*) FROM media WHERE kind='photo'")
    hours = q("SELECT round(coalesce(sum(duration),0)/3600.0,1) FROM media")
    dupes = q("SELECT count(*) FROM locations") - q("SELECT count(*) FROM media")
    print(f"Media: {videos} videos, {photos} photos, {hours} h of video, {dupes} duplicate copies")
    print(f"  prepped {q('SELECT count(*) FROM media WHERE prepped=1')}, "
          f"transcribed {q('SELECT count(*) FROM media WHERE transcribed=1')}, "
          f"described {q('SELECT count(*) FROM media WHERE described=1')}, "
          f"errors {q('SELECT count(*) FROM media WHERE error IS NOT NULL')}")
    print(f"Moments: {q('SELECT count(*) FROM moments')}")
    for row in conn.execute("SELECT status, count(*) n FROM requests GROUP BY status"):
        print(f"  AI requests {row['status']}: {row['n']}")
    from .db import failures

    fails = failures(conn)
    if fails:
        print("Problems:")
        for f in fails:
            extra = f" ({f['duration_s']} s, {f['size_mb']} MB)" if f["duration_s"] is not None else ""
            print(f"  [{f['status']}] {f['file']}{extra}\n      {f['error']}")


def cmd_scan(args):
    from .scan import scan

    cfg, conn = _open(args)
    print(json.dumps(scan(cfg, conn, only=getattr(args, "only", None)), indent=2))


def cmd_prep(args):
    from .prep import prep

    cfg, conn = _open(args)
    print(json.dumps(prep(cfg, conn, limit=args.limit), indent=2))


def cmd_transcribe(args):
    from .transcribe import transcribe

    cfg, conn = _open(args)
    print(json.dumps(transcribe(cfg, conn, limit=args.limit), indent=2))


def cmd_describe(args):
    cfg, conn = _open(args)
    if cfg.backend == "claude-code" and args.action in ("estimate", "run", "retry", "sync"):
        from . import claude_code

        if args.action == "estimate":
            print(json.dumps(claude_code.estimate(cfg, conn), indent=2))
        else:
            limit = args.limit or (3 if args.action == "sync" else None)
            print(json.dumps(claude_code.run(cfg, conn, limit=limit, retry=args.action == "retry"), indent=2))
        return
    if cfg.backend == "gemini" and args.action in ("estimate", "run", "retry"):
        from . import gemini

        if args.action == "estimate":
            print(json.dumps(gemini.estimate(cfg, conn), indent=2))
        else:
            print(json.dumps(gemini.run(cfg, conn, limit=args.limit, retry=args.action == "retry"), indent=2))
        return
    if args.action == "run":
        raise SystemExit("'describe run' is for the gemini / claude-code backends; with the Claude API use "
                         "submit/collect.")
    if args.action == "estimate":
        print(json.dumps(describe_mod.estimate(cfg, conn, args.model), indent=2))
    elif args.action == "submit":
        print(describe_mod.submit(cfg, conn, limit=args.limit))
    elif args.action == "collect":
        print(json.dumps(describe_mod.collect(cfg, conn), indent=2))
    elif args.action == "sync":
        print(json.dumps(describe_mod.run_sync(cfg, conn, limit=args.limit or 3, model=args.model), indent=2))
    elif args.action == "retry":
        stats = describe_mod.run_sync(cfg, conn, limit=args.limit or 1000, model=args.model or cfg.retry_model,
                                      statuses=("refused", "error"))
        print(json.dumps(stats, indent=2))


def cmd_embed(args):
    from .embed import embed

    cfg, conn = _open(args)
    print(json.dumps(embed(cfg, conn), indent=2))


def cmd_search(args):
    from .search import Filters, search, to_json, write_html

    cfg, conn = _open(args)
    f = Filters(
        orientation=args.orientation, min_broll=args.min_broll, date_from=args.date_from, date_to=args.date_to,
        place=args.place, root_kind=args.root, kind=args.kind, min_seconds=args.min_seconds,
        exclude_issues=tuple(args.exclude or ()), collection=args.collection, roles=tuple(args.role or ()),
        min_hook=args.min_hook, fresh=args.fresh, include_hidden=args.include_hidden, held_back=args.held_back,
    )
    results = search(cfg, conn, args.query or "", f, limit=args.limit, use_vectors=not args.no_vectors)
    if args.json:
        print(to_json(results))
    else:
        for i, r in enumerate(results, 1):
            print(f"{i:>2}. m{r['moment_id']} [{r['in_out']}] B{r['broll_score']} H{r['hook_score'] or '-'} "
                  f"used×{r['uses']} {','.join(r['roles'])} {r['orientation'] or ''} "
                  f"{(r['taken_at'] or '')[:10]} {r['place'] or ''}"
                  + (f"  [with {', '.join(r['hidden_people']) or 'a hidden person'}: blur only if you ask]"
                     if r.get("held_back") else "")
                  + f"\n    {r['description']}\n    {r['file']}")
    if args.html is not None:
        slug = re.sub(r"\W+", "-", args.query or "browse").strip("-")[:50] or "browse"
        out = args.html or str(cfg.library_dir / "searches" / f"{slug}.html")
        print(f"Contact sheet: {write_html(results, args.query or 'browse', __import__('pathlib').Path(out))}",
              file=sys.stderr)


def cmd_vault(args):
    from .vault import build_vault

    cfg, conn = _open(args)
    print(json.dumps(build_vault(cfg, conn), indent=2))


def cmd_pull(args):
    from .pull import parse_items, pull

    cfg, conn = _open(args)
    print(json.dumps(pull(cfg, conn, parse_items(args.moments), args.name, handles=args.handles,
                          reframe=args.reframe, page=args.page, style=args.style), indent=2))


def cmd_faces(args):
    from .faces import find_faces, refine_hidden

    cfg, conn = _open(args)
    print(json.dumps({**find_faces(cfg, conn, limit=args.limit), **refine_hidden(cfg, conn)}, indent=2))


def cmd_people(args):
    from .faces import hide_people, list_people, name_person, resolve_people, split_person
    from .usage import unhide

    cfg, conn = _open(args)
    names = " ".join(args.name) if isinstance(args.name, list) else (args.name or "")
    if args.action == "name":
        name_person(conn, int(args.ref.lstrip("#")), names)
        print(f"group {args.ref} is now '{names or '(unnamed)'}'")
    elif args.action == "split":
        ids = split_person(cfg, conn, int(args.ref.lstrip("#")))
        print(f"group {args.ref} -> {len(ids)} groups: {', '.join('#' + str(i) for i in ids)} (same name for now)")
    elif args.action in ("hide", "unhide"):
        refs = [args.ref, *(args.name or [])]  # several people at once: crag people hide "Richa (ex)" Rinki
        missing = []
        for ref in refs:
            try:
                ids = resolve_people(conn, ref)
            except SystemExit:
                missing.append(ref)
                continue
            if args.action == "hide":
                hide_people(cfg, conn, ref, dense=False)
            else:
                for pid in ids:
                    unhide(conn, "person", str(pid))
            print(f"{args.action}: {ref} ({len(ids)} face group{'s' if len(ids) != 1 else ''})")
        if args.action == "hide" and len(missing) < len(refs):
            from .faces import refine_hidden

            print("checking their clips second by second for exact on-screen times (can take a while)...")
            print(json.dumps(refine_hidden(cfg, conn), indent=2))
        if missing:
            print("not found (check the spelling in the dashboard's People tab): " + ", ".join(missing))
    else:
        for p in list_people(conn, args.min_faces):
            print(f"#{p['id']:<5} {p['name'] or '(unnamed)':<20} faces={p['faces']:<5} clips={p['clips']:<5} "
                  f"{'HIDDEN ' if p['hidden'] else ''}{cfg.library_dir / p['sample'] if p['sample'] else ''}")


def cmd_organize(args):
    from . import organize

    cfg, conn = _open(args)
    if args.action == "plan":
        print(json.dumps(organize.plan(cfg, conn), indent=2))
    elif args.action == "apply":
        p = organize.latest_plan(cfg)
        n = len(json.loads(p.read_text())["moves"])
        if not args.yes and input(f"Move {n} files as planned in {p.name}? Nothing is deleted. [y/N] ").lower() != "y":
            return
        print(json.dumps(organize.apply(cfg, conn, p), indent=2))
    else:
        print(json.dumps(organize.undo(cfg, conn), indent=2))


def cmd_edit(args):
    from pathlib import Path as _P

    from .edit import run
    from .search import Filters

    cfg, conn = _open(args)
    targets = run.parse_targets(args.export)
    args.name = args.name or _P(args.source).stem
    if args.mode == "talking":
        out = run.make_talking(cfg, conn, _P(args.source).expanduser(), args.name, targets, style_name=args.style,
                               page=args.page, music=args.music, collection=args.collection, notes=args.notes or "",
                               use_vectors=not args.no_vectors)
    elif args.mode == "beat":
        f = Filters(kind="video", collection=args.collection, roles=tuple(args.role or ()), fresh=args.fresh)
        out = run.make_beat(cfg, conn, args.source, args.name, targets, style_name=args.style, page=args.page,
                            query=args.query or "", filters=f, start=args.start or 0.0, end=args.end,
                            use_vectors=not args.no_vectors)
    else:
        out = run.rerender(cfg, conn, args.source, targets)
    print(json.dumps(out, ensure_ascii=False, indent=2))


def cmd_music(args):
    from pathlib import Path as _P

    from . import music

    cfg = load_config(args.config)
    if args.action == "analyse":
        music.analyse(cfg, _P(args.file).expanduser(), args.name)
    else:
        for s in music.songs(cfg):
            print(f"{s['name']:<30} {s['bpm']:>6} BPM  {s['duration']:6.1f}s  {s['file']}")


def cmd_style(args):
    from pathlib import Path as _P

    from .edit import style

    cfg = load_config(args.config)
    if args.action == "learn":
        if not args.references:
            raise SystemExit("Give one or more example reels: crag style learn <name> a.mp4 b.mp4")
        s = style.learn_style(cfg, args.name, [_P(p).expanduser() for p in args.references], page=args.page)
        print(json.dumps({k: s[k] for k in ("name", "page", "description")}, ensure_ascii=False, indent=2))
    elif args.action == "show":
        print(json.dumps(style.load_style(cfg, args.name), ensure_ascii=False, indent=2))
    elif args.action == "new":
        s = style.load_style(cfg, None)
        s.update(name=args.name, page=args.page, description=f"Copy of the default style for {args.page or args.name}")
        print(f"created {style.save_style(cfg, s)} - edit it, or ask Claude to")
    else:
        for s in style.list_styles(cfg):
            print(f"{s['name']:<24} page={s['page'] or '-':<14} {s['description'][:80]}")


def cmd_assets(args):
    from .edit import transitions
    from .edit.style import asset_report

    cfg = load_config(args.config)
    if args.action == "import":
        xml = Path(args.xml) if args.xml else None  # nothing given: found on the drive
        print(json.dumps(transitions.import_template(cfg, xml, args.media and Path(args.media)), indent=2))
        return
    print(json.dumps(asset_report(cfg) | {"transitions": len(transitions.load_library(cfg))}, indent=2))
    if args.action == "transitions":
        print("\n".join(transitions.summary(cfg)) or "no transition library yet: crag assets import")


def cmd_folders(args):
    """What is in each folder (videos, photos, hours), to decide what to skip before paying for analysis."""
    from collections import defaultdict

    cfg, conn = _open(args)
    agg = defaultdict(lambda: {"videos": 0, "photos": 0, "hours": 0.0})
    seen = set()
    for r in conn.execute("SELECT l.root, l.relpath, m.id, m.kind, coalesce(m.duration, 0) d FROM locations l "
                          "JOIN media m ON m.id = l.media_id ORDER BY l.root, l.relpath"):
        if r["id"] in seen:  # duplicates counted once, at their first copy
            continue
        seen.add(r["id"])
        parts = r["relpath"].split("/")[:-1]
        key = "/".join(parts[:args.depth]) or "(top level)"
        if args.under:
            if not r["relpath"].startswith(args.under.rstrip("/") + "/"):
                continue
            key = "/".join(parts[:len(args.under.strip("/").split("/")) + 1]) or args.under
        a = agg[(r["root"], key)]
        a["videos" if r["kind"] == "video" else "photos"] += 1
        a["hours"] += r["d"] / 3600
    rows = sorted(agg.items(), key=lambda kv: -(kv[1]["hours"] + kv[1]["photos"] / 3000))
    print(f"{'hours':>7} {'videos':>7} {'photos':>7}  folder")
    for (root, key), a in rows[:args.limit]:
        print(f"{a['hours']:7.1f} {a['videos']:7} {a['photos']:7}  {key}" + (f"   [{root}]" if len(cfg.roots) > 1 else ""))
    if len(rows) > args.limit:
        print(f"... {len(rows) - args.limit} more (--limit {len(rows)})")


def cmd_backend(args):
    """Show or switch the AI analysis backend (writes [describe] backend in contentrag.toml)."""
    from .config import BACKENDS, DEFAULT_CONFIG, set_backend

    if args.name:
        path = Path(args.config or os.environ.get("CONTENTRAG_CONFIG", DEFAULT_CONFIG)).expanduser()
        load_config(path)  # fails early on a broken config
        set_backend(path, args.name)
        print(f"analysis backend is now {args.name}: {BACKENDS[args.name]}")
    cfg = load_config(args.config)
    for name, what in BACKENDS.items():
        print(f"{'*' if name == cfg.backend else ' '} {name:<12} {what}")
    from .autopilot import backend_problems

    problems = backend_problems(cfg)
    print("ready" if not problems else "not ready: " + " · ".join(problems))
    if cfg.backend in ("claude", "claude-code") and not cfg.transcribe_enabled:
        print("tip: Claude sees frames only; set [transcribe] enabled = true so it also knows what was said")


def cmd_hide(args):
    from .usage import hide, unhide

    cfg, conn = _open(args)
    (unhide if args.undo else hide)(conn, args.kind, args.ref, *([] if args.undo else [args.reason]))
    print(f"{'unhidden' if args.undo else 'hidden'}: {args.kind} {args.ref}")


def cmd_hidden(args):
    cfg, conn = _open(args)
    for r in conn.execute("SELECT * FROM hidden ORDER BY kind, ref"):
        print(f"{r['kind']:<10} {r['ref']:<30} {r['reason'] or ''}")


def cmd_collections(args):
    from .collections import all_collections

    cfg, conn = _open(args)
    for c in all_collections(conn):
        print(f"{c['items']:>6}  {(c['first'] or '')[:10]} → {(c['last'] or '')[:10]}  {c['name']}")


def cmd_videos(args):
    from .usage import by_style, set_posted, videos

    cfg, conn = _open(args)
    if args.by_style:
        for r in by_style(conn):
            print("  ".join(f"{k}={v}" for k, v in r.items()))
        return
    if args.posted:
        metrics = {k: v for k, v in (("views", args.views), ("likes", args.likes), ("saves", args.saves),
                                     ("shares", args.shares), ("comments", args.comments),
                                     ("avg_watch_pct", args.retention)) if v is not None}
        set_posted(conn, args.posted, metrics, args.notes)
        print(f"marked posted: {args.posted}")
        return
    for v in videos(conn):
        print(f"{v['status']:<7} {v['created_at'][:10]}  {v['name']:<30} page={v['page'] or '-'} "
              f"style={v['style'] or '-'} clips={v['clips']} {v['metrics'] or ''}")


def cmd_autopilot(args):
    from .autopilot import Autopilot, Busy

    cfg = load_config(args.config)
    if args.only:
        from .scan import only_root

        only_root(cfg, args.only)  # clear message now if the folder isn't on a footage drive
    try:
        result = Autopilot(cfg, hours=args.hours, only=args.only).run()
    except Busy as e:
        raise SystemExit(str(e))
    except KeyboardInterrupt:
        raise SystemExit("\nStopped. Run `crag autopilot` again to continue where it left off.")
    print(result["summary"])
    for p in result["problems"][:20]:
        print(f"  [{p['status']}] {p['file']}: {p['error']}")


def cmd_drive(args):
    from .drive import setup

    print(json.dumps(setup(load_config(args.config), args.drive), indent=2))


def cmd_ui(args):
    from .ui import serve

    serve(load_config(args.config), port=args.port, open_browser=not args.no_browser)


def cmd_run(args):
    """scan -> prep -> transcribe -> describe."""
    cmd_scan(args)
    cmd_prep(args)
    cfg, conn = _open(args)
    if cfg.transcribe_enabled:
        cmd_transcribe(args)
    if cfg.backend == "claude-code":
        from . import claude_code

        print(json.dumps(claude_code.estimate(cfg, conn), indent=2))
        if args.yes or input("Analyse these with your Claude subscription? [y/N] ").strip().lower() == "y":
            print(json.dumps(claude_code.run(cfg, conn), indent=2))
        return
    if cfg.backend == "gemini":
        from . import gemini

        print(json.dumps(gemini.estimate(cfg, conn), indent=2))
        if args.yes or input("Send these to Gemini? [y/N] ").strip().lower() == "y":
            print(json.dumps(gemini.run(cfg, conn), indent=2))
        return
    print(json.dumps(describe_mod.estimate(cfg, conn), indent=2))
    if args.yes or input("Submit these to Claude? [y/N] ").strip().lower() == "y":
        print(describe_mod.submit(cfg, conn))


def main(argv=None):
    p = argparse.ArgumentParser(prog="crag", description="Personal footage index + Obsidian life vault")
    p.add_argument("-c", "--config", help="path to contentrag.toml (default: ./contentrag.toml)")
    p.add_argument("--backend", choices=["gemini", "claude-code", "claude"],
                   help="use this AI backend for this command only (crag backend <name> switches for good)")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("status").set_defaults(fn=cmd_status)
    sp = sub.add_parser("scan", help="find files, dedupe, read dates/GPS")
    sp.add_argument("--only", metavar="FOLDER", help="scan just this folder (inside a footage drive)")
    sp.set_defaults(fn=cmd_scan)
    for name, fn, help_ in (("prep", cmd_prep, "sample frames, scene cuts, audio"),
                            ("transcribe", cmd_transcribe, "Whisper speech-to-text")):
        sp = sub.add_parser(name, help=help_)
        sp.add_argument("--limit", type=int)
        sp.set_defaults(fn=fn)

    sp = sub.add_parser("describe", help="Claude descriptions of moments")
    sp.add_argument("action", choices=["estimate", "run", "submit", "collect", "sync", "retry"],
                    help="gemini: estimate | run | retry.  claude: estimate | submit | collect | sync | retry")
    sp.add_argument("--limit", type=int)
    sp.add_argument("--model")
    sp.set_defaults(fn=cmd_describe)

    sub.add_parser("embed", help="local text embeddings for fuzzy search").set_defaults(fn=cmd_embed)

    sp = sub.add_parser("search", help="find moments")
    sp.add_argument("query", nargs="?")
    sp.add_argument("--vertical", dest="orientation", action="store_const", const="vertical")
    sp.add_argument("--horizontal", dest="orientation", action="store_const", const="horizontal")
    sp.add_argument("--min-broll", type=int)
    sp.add_argument("--from", dest="date_from", help="YYYY, YYYY-MM or YYYY-MM-DD")
    sp.add_argument("--to", dest="date_to")
    sp.add_argument("--place")
    sp.add_argument("--root", choices=["archive", "brand"])
    sp.add_argument("--kind", choices=["video", "photo"])
    sp.add_argument("--min-seconds", type=float)
    sp.add_argument("--exclude", action="append", help="quality issue to exclude, e.g. shaky (repeatable)")
    sp.add_argument("--collection", help="folder label, partial match (e.g. thailand)")
    sp.add_argument("--role", action="append", help="hook, cinematic, spectacle, story_to_camera, funny, ...")
    sp.add_argument("--min-hook", type=int)
    sp.add_argument("--fresh", action="store_true", help="only moments never used in a video")
    sp.add_argument("--include-hidden", action="store_true")
    sp.add_argument("--held-back", action="store_true",
                    help="only the moments left out because a hidden person is in them (to ask before using, blurred)")
    sp.add_argument("--limit", type=int, default=20)
    sp.add_argument("--json", action="store_true")
    sp.add_argument("--html", nargs="?", const="", help="write an HTML contact sheet (optional path)")
    sp.add_argument("--no-vectors", action="store_true")
    sp.set_defaults(fn=cmd_search)

    sub.add_parser("vault", help="rebuild the Obsidian vault's generated notes").set_defaults(fn=cmd_vault)

    sp = sub.add_parser("pull", help="cut moments (m12 m48 ...) from the originals + Premiere timeline")
    sp.add_argument("moments", nargs="+", help="moment ids from search/vault, e.g. m12 m48 or 12,48")
    sp.add_argument("--name", default="selects", help="export folder name (library_dir/exports/<name>)")
    sp.add_argument("--handles", type=float, default=0.5, help="extra seconds before/after each moment")
    sp.add_argument("--reframe", choices=["crop", "blur"], help="make horizontal clips 9:16 (crop or blurred fill)")
    sp.add_argument("--page", help="which Instagram page this video is for")
    sp.add_argument("--style", help="style profile used")
    sp.set_defaults(fn=cmd_pull)

    sp = sub.add_parser("faces", help="find and group faces (local) so people can be named / hidden")
    sp.add_argument("--limit", type=int)
    sp.set_defaults(fn=cmd_faces)

    sp = sub.add_parser("people", help="list face groups, name them, hide someone everywhere")
    sp.add_argument("action", nargs="?", default="list", choices=["list", "name", "split", "hide", "unhide"])
    sp.add_argument("ref", nargs="?", help="group id (#7) or name")
    sp.add_argument("name", nargs="*", help="for 'name': the person's name (groups with the same name = one person; "
                                            "empty = unnamed). 'hide'/'unhide' take several names. "
                                            "'split <id>' separates a group's faces again")
    sp.add_argument("--min-faces", type=int, default=3)
    sp.set_defaults(fn=cmd_people)

    sp = sub.add_parser("edit", help="make an edit: talking head + B-roll, or a beat-synced montage")
    sp.add_argument("mode", choices=["talking", "beat", "render"],
                    help="talking <video> | beat <song name> | render <edit name> (after changing plan.json)")
    sp.add_argument("source", help="talking-head file, analysed song name, or edit name")
    sp.add_argument("--name", help="name of the reel (export folder)")
    sp.add_argument("--style", help="style profile (crag style list)")
    sp.add_argument("--page", help="Instagram page")
    sp.add_argument("--music", help="analysed song to put under the voice")
    sp.add_argument("--collection", help="prefer footage from this collection (e.g. thailand)")
    sp.add_argument("--notes", help="extra context for the AI (what the video is about)")
    sp.add_argument("--query", help="beat: what footage to use (search words)")
    sp.add_argument("--role", action="append", help="beat: only these roles (cinematic, action, ...)")
    sp.add_argument("--fresh", action="store_true", help="beat: only never-used footage")
    sp.add_argument("--start", type=float, help="beat: start of the song part to use (s)")
    sp.add_argument("--end", type=float, help="beat: end of the song part to use (s)")
    sp.add_argument("--export", default="all", help="mp4,premiere,aftereffects,hyperframes,remotion (default all)")
    sp.add_argument("--no-vectors", action="store_true")
    sp.set_defaults(fn=cmd_edit)

    sp = sub.add_parser("music", help="analyse a song (tempo, beats, sections) / list songs")
    sp.add_argument("action", choices=["analyse", "list"])
    sp.add_argument("file", nargs="?")
    sp.add_argument("--name")
    sp.set_defaults(fn=cmd_music)

    sp = sub.add_parser("style", help="editing styles: learn from example reels, list, show, new")
    sp.add_argument("action", choices=["learn", "list", "show", "new"])
    sp.add_argument("name", nargs="?")
    sp.add_argument("references", nargs="*", help="example reels (for learn)")
    sp.add_argument("--page")
    sp.set_defaults(fn=cmd_style)

    sp = sub.add_parser("assets", help="assets folder (light leaks, SFX, LUTs, fonts) + transition library")
    sp.add_argument("action", nargs="?", choices=["show", "import", "transitions"], default="show",
                    help="import [template.xml or its folder]: flash/leak + SFX recipes from a Premiere template "
                         "(without a path it is found on the drive)")
    sp.add_argument("xml", nargs="?")
    sp.add_argument("--media", help="folder with the template's plates and SFX (default: next to the XML)")
    sp.set_defaults(fn=cmd_assets)
    sp = sub.add_parser("folders", help="videos/photos/hours per folder, to choose what to skip")
    sp.add_argument("--depth", type=int, default=2, help="folder levels to group by (default 2)")
    sp.add_argument("--under", help="only this folder, one level deeper, e.g. 'Disk D/mobile files'")
    sp.add_argument("--limit", type=int, default=60)
    sp.set_defaults(fn=cmd_folders)
    sp = sub.add_parser("backend", help="show or switch the AI analysis backend: gemini | claude-code | claude")
    sp.add_argument("name", nargs="?", choices=["gemini", "claude-code", "claude"])
    sp.set_defaults(fn=cmd_backend)

    sp = sub.add_parser("organize", help="tidy footage folders: plan -> review -> apply (undo possible)")
    sp.add_argument("action", choices=["plan", "apply", "undo"])
    sp.add_argument("-y", "--yes", action="store_true")
    sp.set_defaults(fn=cmd_organize)

    sp = sub.add_parser("hide", help="never suggest a moment / clip / collection / person (privacy)")
    sp.add_argument("kind", choices=["moment", "media", "collection", "person"])
    sp.add_argument("ref", help="m123 | media id | collection name | person id")
    sp.add_argument("--reason")
    sp.add_argument("--undo", action="store_true", help="remove from the hide list")
    sp.set_defaults(fn=cmd_hide)
    sub.add_parser("hidden", help="show the hide list").set_defaults(fn=cmd_hidden)
    sub.add_parser("collections", help="list collections (your folder labels)").set_defaults(fn=cmd_collections)

    sp = sub.add_parser("videos", help="videos made from the library; mark one posted with its numbers")
    sp.add_argument("--posted", metavar="NAME", help="mark this video as posted")
    for flag in ("views", "likes", "saves", "shares", "comments"):
        sp.add_argument(f"--{flag}", type=int)
    sp.add_argument("--retention", type=float, help="average watch percentage")
    sp.add_argument("--notes")
    sp.add_argument("--by-style", action="store_true", help="average results per editing style")
    sp.set_defaults(fn=cmd_videos)

    sp = sub.add_parser("autopilot", help="run everything unattended until done (overnight)")
    sp.add_argument("--hours", type=float, default=24, help="give up waiting after this many hours (default 24)")
    sp.add_argument("--only", metavar="FOLDER",
                    help="new footage in just this folder: scans only it, then analyses what's new")
    sp.set_defaults(fn=cmd_autopilot)

    sp = sub.add_parser("drive", help="make the footage drive self-contained: plug in anywhere, open Claude on it")
    sp.add_argument("action", choices=["setup"], help="setup: copy the code, config, skills and launcher onto it")
    sp.add_argument("--drive", help="drive folder (default: the drive the library is on)")
    sp.set_defaults(fn=cmd_drive)

    sp = sub.add_parser("ui", help="local dashboard in your browser (http://127.0.0.1:8765)")
    sp.add_argument("--port", type=int, default=8765)
    sp.add_argument("--no-browser", action="store_true")
    sp.set_defaults(fn=cmd_ui)

    sp = sub.add_parser("run", help="scan + prep + transcribe + describe")
    sp.add_argument("--limit", type=int)
    sp.add_argument("-y", "--yes", action="store_true")
    sp.set_defaults(fn=cmd_run)

    args = p.parse_args(argv)
    if args.backend:
        os.environ["CRAG_BACKEND"] = args.backend
    args.fn(args)


if __name__ == "__main__":
    main()
