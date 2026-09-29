"""Step 3: speech-to-text with Whisper on Apple Silicon (mlx-whisper), Hindi by default."""

from __future__ import annotations

from .config import Config

# Whisper invents these on silence / music; drop segments that are only this.
_HALLUCINATIONS = (
    "subtitles by", "thanks for watching", "thank you for watching", "please subscribe",
    "like and subscribe", "सब्सक्राइब", "धन्यवाद",
)


def keep_segment(seg: dict) -> bool:
    text = (seg.get("text") or "").strip()
    if not text:
        return False
    if seg.get("no_speech_prob", 0) > 0.6 and seg.get("avg_logprob", 0) < -0.7:
        return False
    if seg.get("compression_ratio", 0) > 2.6:  # repetition loops
        return False
    low = text.lower()
    likely_silence = seg.get("no_speech_prob", 0) > 0.3
    return not (likely_silence and len(low) < 40 and any(h in low for h in _HALLUCINATIONS))


def transcribe(cfg: Config, conn, limit: int | None = None, log=print) -> dict:
    try:
        import mlx_whisper  # type: ignore
    except ImportError:
        raise SystemExit("mlx-whisper is not installed. On your Mac run: pip install -e '.[mac]'")

    rows = conn.execute(
        "SELECT id, relpath, duration FROM media WHERE kind='video' AND prepped=1 AND transcribed=0 ORDER BY taken_at"
    ).fetchall()
    if limit:
        rows = rows[:limit]
    stats = {"done": 0, "no_audio": 0, "failed": 0}
    for i, row in enumerate(rows, 1):
        wav = cfg.audio_dir / f"{row['id']}.wav"
        if not wav.exists():
            stats["no_audio"] += 1
            conn.execute("UPDATE media SET transcribed=1 WHERE id=?", (row["id"],))
            continue
        try:
            result = mlx_whisper.transcribe(
                str(wav),
                path_or_hf_repo=cfg.whisper_model,
                language=cfg.language or None,
                condition_on_previous_text=False,
                verbose=None,
            )
        except Exception as e:
            stats["failed"] += 1
            log(f"[transcribe] failed {row['relpath']}: {e}")
            continue
        segs = [s for s in result.get("segments", []) if keep_segment(s)]
        conn.execute("DELETE FROM transcript WHERE media_id=?", (row["id"],))
        conn.executemany(
            "INSERT INTO transcript VALUES (?,?,?,?)",
            [(row["id"], float(s["start"]), float(s["end"]), s["text"].strip()) for s in segs],
        )
        conn.execute("UPDATE media SET transcribed=1 WHERE id=?", (row["id"],))
        conn.commit()
        wav.unlink(missing_ok=True)
        stats["done"] += 1
        if i % 10 == 0:
            log(f"[transcribe] {i}/{len(rows)}")
    conn.commit()
    return stats
