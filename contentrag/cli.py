"""`crag` command line."""

from __future__ import annotations

import argparse
import json
import re
import sys

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
    print(json.dumps(scan(cfg, conn), indent=2))


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
    if cfg.backend == "gemini" and args.action in ("estimate", "run", "retry"):
        from . import gemini

        if args.action == "estimate":
            print(json.dumps(gemini.estimate(cfg, conn), indent=2))
        else:
            print(json.dumps(gemini.run(cfg, conn, limit=args.limit, retry=args.action == "retry"), indent=2))
        return
    if args.action == "run":
        raise SystemExit("'describe run' is for backend = \"gemini\"; with Claude use submit/collect.")
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
        min_hook=args.min_hook, fresh=args.fresh, include_hidden=args.include_hidden,
    )
    results = search(cfg, conn, args.query or "", f, limit=args.limit, use_vectors=not args.no_vectors)
    if args.json:
        print(to_json(results))
    else:
        for i, r in enumerate(results, 1):
            print(f"{i:>2}. m{r['moment_id']} [{r['in_out']}] B{r['broll_score']} H{r['hook_score'] or '-'} "
                  f"used×{r['uses']} {','.join(r['roles'])} {r['orientation'] or ''} "
                  f"{(r['taken_at'] or '')[:10]} {r['place'] or ''}\n    {r['description']}\n    {r['file']}")
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
    from .faces import hide_people, list_people, name_person, resolve_people
    from .usage import unhide

    cfg, conn = _open(args)
    if args.action == "name":
        kept = name_person(conn, int(args.ref.lstrip("#")), args.name)
        print(f"group {args.ref} is now '{args.name}' (id {kept})")
    elif args.action == "hide":
        print(json.dumps(hide_people(cfg, conn, args.ref), indent=2))
    elif args.action == "unhide":
        for pid in resolve_people(conn, args.ref):
            unhide(conn, "person", str(pid))
        print(f"unhidden: {args.ref}")
    else:
        for p in list_people(conn, args.min_faces):
            print(f"#{p['id']:<5} {p['name'] or '(unnamed)':<20} faces={p['faces']:<5} clips={p['clips']:<5} "
                  f"{'HIDDEN ' if p['hidden'] else ''}{cfg.library_dir / p['sample'] if p['sample'] else ''}")


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
    from .usage import set_posted, videos

    cfg, conn = _open(args)
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
    try:
        result = Autopilot(cfg, hours=args.hours).run()
    except Busy as e:
        raise SystemExit(str(e))
    except KeyboardInterrupt:
        raise SystemExit("\nStopped. Run `crag autopilot` again to continue where it left off.")
    print(result["summary"])
    for p in result["problems"][:20]:
        print(f"  [{p['status']}] {p['file']}: {p['error']}")


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
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("status").set_defaults(fn=cmd_status)
    sub.add_parser("scan", help="find files, dedupe, read dates/GPS").set_defaults(fn=cmd_scan)
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
    sp.add_argument("action", nargs="?", default="list", choices=["list", "name", "hide", "unhide"])
    sp.add_argument("ref", nargs="?", help="group id (#7) or name")
    sp.add_argument("name", nargs="?", help="for 'name': the person's name (same name = same person)")
    sp.add_argument("--min-faces", type=int, default=3)
    sp.set_defaults(fn=cmd_people)

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
    sp.set_defaults(fn=cmd_videos)

    sp = sub.add_parser("autopilot", help="run everything unattended until done (overnight)")
    sp.add_argument("--hours", type=float, default=24, help="give up waiting after this many hours (default 24)")
    sp.set_defaults(fn=cmd_autopilot)

    sp = sub.add_parser("ui", help="local dashboard in your browser (http://127.0.0.1:8765)")
    sp.add_argument("--port", type=int, default=8765)
    sp.add_argument("--no-browser", action="store_true")
    sp.set_defaults(fn=cmd_ui)

    sp = sub.add_parser("run", help="scan + prep + transcribe + describe")
    sp.add_argument("--limit", type=int)
    sp.add_argument("-y", "--yes", action="store_true")
    sp.set_defaults(fn=cmd_run)

    args = p.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()
