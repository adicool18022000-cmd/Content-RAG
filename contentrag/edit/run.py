"""Build -> render -> export -> record, for talking-head and beat edits."""

from __future__ import annotations

from pathlib import Path

from ..config import Config
from ..pull import _slug
from ..usage import record_video
from .plan import EditPlan

ALL_TARGETS = {"mp4", "premiere", "aftereffects", "hyperframes", "remotion"}


def out_dir(cfg: Config, name: str) -> Path:
    return cfg.library_dir / "exports" / _slug(name)


ALIASES = {"ae": "aftereffects", "after-effects": "aftereffects", "pr": "premiere", "pp": "premiere",
           "hf": "hyperframes", "video": "mp4"}


def parse_targets(value: str | None) -> set[str]:
    if not value or value == "all":
        return set(ALL_TARGETS)
    t = {ALIASES.get(x.strip().lower(), x.strip().lower()) for x in value.split(",") if x.strip()}
    bad = t - ALL_TARGETS
    if bad:
        raise SystemExit(f"Unknown export target(s): {', '.join(bad)}. Choose from {', '.join(sorted(ALL_TARGETS))}.")
    return t


def render_plan(cfg: Config, conn, plan: EditPlan, targets: set[str], log=print) -> dict:
    from .export import export_all
    from .render import materialize, render_mp4, summary, write_srt

    d = out_dir(cfg, plan.name)
    blurred = protect_hidden_people(conn, plan)
    if blurred:
        log(f"[edit] {blurred} clip(s) show a hidden person: their faces get a blur layer in HyperFrames, "
            "Remotion and the preview (Premiere/After Effects show the original - see EDIT.md)")
    plan_path = plan.save(d / "plan.json")
    fonts = cfg.library_dir / "assets" / "fonts"
    if fonts.is_dir() and any(fonts.iterdir()) and not (d / "fonts").exists():
        import shutil

        shutil.copytree(fonts, d / "fonts")
    pieces = materialize(plan, d, log)
    out = {"folder": str(d), "plan": str(plan_path), **summary(plan)}
    if plan.captions:
        write_srt(plan.captions, d / "captions.srt")
    if "mp4" in targets:
        out["mp4"] = str(render_mp4(plan, d, pieces, log=log))
    out.update(export_all(plan, d, pieces, targets))
    record_video(conn, plan.name, page=plan.page, style=plan.style, uses=plan.uses())
    _write_readme(plan, d, out)
    return out


def protect_hidden_people(conn, plan: EditPlan) -> int:
    """Every library clip in the plan gets the faces of hidden people that are on screen during its range,
    as blur boxes on the output timeline (Clip.blur). Search and the planners already leave those parts
    out; this covers moments the creator explicitly asked to use anyway, and face-check gaps. The blur
    is a layer in the exports - the footage and the cut pieces stay untouched. Returns how many clips."""
    from ..usage import BLUR_PAD_SECONDS, face_boxes, frame_box, hidden_people_in, hidden_sets

    people = hidden_sets(conn)["person"]
    n = 0
    for c in plan.clips:
        c.blur, c.blur_people = [], []
        if not (c.media_id and people):
            continue
        m = conn.execute("SELECT width, height FROM media WHERE id=?", (c.media_id,)).fetchone()
        sw, sh = (m["width"], m["height"]) if m else (plan.width, plan.height)
        mode = c.reframe if c.track == "broll" and c.reframe else "crop"
        for t, *box in face_boxes(conn, c.media_id, c.src_in, c.src_out, people):
            a = max(c.at, c.at + (t - c.src_in) - BLUR_PAD_SECONDS)
            b = min(c.end, c.at + (t - c.src_in) + BLUR_PAD_SECONDS)
            fb = frame_box(tuple(box), sw, sh, plan.width, plan.height, mode)
            if fb and b - a > 0.02:
                c.blur.append([round(a, 3), round(b, 3), *fb])
        if c.blur:
            c.blur_people = hidden_people_in(conn, c.media_id, c.src_in, c.src_out, people)
            n += 1
    return n


def _write_readme(plan: EditPlan, d: Path, out: dict) -> None:
    lines = [f"# {plan.name}", "", f"Style: {plan.style or 'default'} · page: {plan.page or '-'} · "
             f"{plan.duration:.1f} s", ""]
    lines += [f"- {n}" for n in plan.notes]
    lines += ["", "## Timeline"]
    for c in sorted(plan.clips, key=lambda c: c.at):
        if c.track != "overlay":
            lines.append(f"- {c.at:6.2f}s  {c.track:<6} {Path(c.file).name} {c.src_in:.2f}-{c.src_out:.2f}"
                         + (f"  m{c.moment_id}" if c.moment_id else "") + (f"  ({c.note})" if c.note else ""))
    blurred = [c for c in sorted(plan.clips, key=lambda c: c.at) if c.blur]
    if blurred:
        lines += ["", "## Blurred faces (hidden people)",
                  "Blurred as a layer in HyperFrames, Remotion and preview.mp4 only. The footage is untouched, so "
                  "Premiere / After Effects show these faces: post from HyperFrames or Remotion, or blur them there."]
        lines += [f"- {c.at:6.2f}s  {', '.join(c.blur_people) or 'hidden person'} in {Path(c.file).name}"
                  + (f" m{c.moment_id}" if c.moment_id else "")
                  + f": {min(b[0] for b in c.blur):.2f}-{max(b[1] for b in c.blur):.2f}s" for c in blurred]
    if plan.shoot_list:
        lines += ["", "## Shoot list (no good footage yet)", *[f"- {s}" for s in plan.shoot_list]]
    lines += ["", "## Files", *[f"- {k}: {v}" for k, v in out.items() if k in ALL_TARGETS or k == "plan"],
              "", "Change anything by editing plan.json, then: crag edit render \"" + plan.name + "\""]
    (d / "EDIT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def make_talking(cfg: Config, conn, aroll: Path, name: str, targets: set[str], log=print, **kw) -> dict:
    from .talking import plan_talking

    plan = plan_talking(cfg, conn, aroll, name, log=log, **kw)
    return render_plan(cfg, conn, plan, targets, log)


def make_beat(cfg: Config, conn, song: str, name: str, targets: set[str], style_name: str | None = None,
              page: str | None = None, log=print, **kw) -> dict:
    from ..music import load
    from .beat import plan_beat_edit
    from .style import load_style
    from .talking import _sfx, _transition

    style = load_style(cfg, style_name)
    plan = plan_beat_edit(cfg, conn, song, name, reframe=style.get("reframe", "crop"), **kw)
    plan.style, plan.page, plan.grade = style["name"], page or style.get("page"), style["grade"]
    plan.sounds[0].volume = style["music"]["volume_alone"]
    info = load(cfg, song)
    start = kw.get("start", 0.0) or 0.0
    for sec in info.sections[1:]:  # a transition where the energy changes
        if start < sec.start < start + plan.duration:
            if not any(abs(sec.start - d) < 1.0 for d in info.drops):
                _transition(cfg, plan, style, sec.start - start, f"{name}-{sec.start}", "section")
    for drop in info.drops:  # riser into each drop (a riser recipe from the library, if imported)
        if start < drop < start + plan.duration:
            if not _transition(cfg, plan, style, drop - start, f"{name}-drop-{drop}", "drop"):
                _sfx(cfg, plan, style["sfx"].get("before_payoff"), drop - start, style["sfx"]["volume"], name,
                     end_at=True)
    return render_plan(cfg, conn, plan, targets, log)


def rerender(cfg: Config, conn, name_or_path: str, targets: set[str], log=print) -> dict:
    p = Path(name_or_path)
    path = p if p.suffix == ".json" and p.exists() else out_dir(cfg, name_or_path) / "plan.json"
    if not path.exists():
        raise SystemExit(f"No edit plan at {path}")
    plan = EditPlan.load(path)
    media = path.parent / "media"
    if media.exists():  # pieces are cut again from the (possibly changed) plan
        import shutil

        shutil.rmtree(media)
    return render_plan(cfg, conn, plan, targets, log)
