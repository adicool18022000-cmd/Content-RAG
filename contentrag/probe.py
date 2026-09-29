"""Read technical metadata, capture date and GPS from videos and photos."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".avi", ".mkv", ".mts", ".m2ts", ".3gp", ".webm", ".wmv", ".mpg", ".mpeg"}
PHOTO_EXTS = {".jpg", ".jpeg", ".png", ".heic", ".heif", ".webp", ".dng", ".tif", ".tiff"}

_CHUNK = 256 * 1024


def media_kind(path: Path) -> str | None:
    ext = path.suffix.lower()
    if ext in VIDEO_EXTS:
        return "video"
    if ext in PHOTO_EXTS:
        return "photo"
    return None


def fingerprint(path: Path) -> str:
    """Fast content id: size plus three 256 KB samples. Survives renames and moves."""
    size = path.stat().st_size
    h = hashlib.sha1(str(size).encode())
    with open(path, "rb") as f:
        for offset in (0, max(0, size // 2 - _CHUNK // 2), max(0, size - _CHUNK)):
            f.seek(offset)
            h.update(f.read(_CHUNK))
    return h.hexdigest()[:20]


@dataclass
class Meta:
    duration: float | None = None
    width: int | None = None
    height: int | None = None
    fps: float | None = None
    has_audio: bool = False
    taken_at: str | None = None
    date_source: str | None = None
    lat: float | None = None
    lon: float | None = None
    camera: str | None = None

    @property
    def orientation(self) -> str | None:
        if not self.width or not self.height:
            return None
        if abs(self.width - self.height) <= 0.05 * max(self.width, self.height):
            return "square"
        return "vertical" if self.height > self.width else "horizontal"


# ---------------------------------------------------------------- dates

_FILENAME_DT = re.compile(
    r"(?<!\d)(20\d{2}|19\d{2})[-_.]?(0[1-9]|1[0-2])[-_.]?(0[1-9]|[12]\d|3[01])"
    r"(?:[ _T-]*(?:at[ _])?([01]\d|2[0-3])[.:_-]?([0-5]\d)[.:_-]?([0-5]\d))?(?!\d)"
)
_ISO6709 = re.compile(r"([+-]\d{1,3}(?:\.\d+)?)([+-]\d{1,3}(?:\.\d+)?)")


def _valid(dt: datetime) -> bool:
    return 1995 <= dt.year <= datetime.now().year + 1


def _to_local(dt: datetime, tz: str) -> datetime:
    if dt.tzinfo is None:
        return dt
    return dt.astimezone(ZoneInfo(tz)).replace(tzinfo=None)


def parse_meta_datetime(value: str, tz: str) -> datetime | None:
    value = value.strip()
    candidates = [value, value.replace("Z", "+00:00")]
    # "+0530" -> "+05:30" for fromisoformat on older inputs
    candidates.append(re.sub(r"([+-]\d{2})(\d{2})$", r"\1:\2", value))
    # EXIF style "2023:08:12 19:04:33"
    candidates.append(re.sub(r"^(\d{4}):(\d{2}):(\d{2})", r"\1-\2-\3", value))
    for c in candidates:
        try:
            dt = datetime.fromisoformat(c)
        except ValueError:
            continue
        if _valid(dt):
            return _to_local(dt, tz)
    return None


def date_from_filename(name: str) -> datetime | None:
    for m in _FILENAME_DT.finditer(name):
        y, mo, d, hh, mm, ss = m.groups()
        try:
            dt = datetime(int(y), int(mo), int(d), int(hh or 12), int(mm or 0), int(ss or 0))
        except ValueError:
            continue
        if _valid(dt):
            if name.upper().startswith("PXL_") and hh:  # Pixel names are UTC
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
    return None


def parse_iso6709(value: str) -> tuple[float, float] | None:
    m = _ISO6709.search(value or "")
    if not m:
        return None
    lat, lon = float(m.group(1)), float(m.group(2))
    if lat == 0 and lon == 0:
        return None
    return lat, lon


def _finish_date(meta: Meta, path: Path, tz: str, dt: datetime | None) -> None:
    source = "metadata"
    if dt is None:
        dt = date_from_filename(path.name)
        source = "filename"
        if dt is not None:
            dt = _to_local(dt, tz)
    if dt is None:
        dt = datetime.fromtimestamp(path.stat().st_mtime)
        source = "mtime"
    meta.taken_at = dt.replace(microsecond=0).isoformat()
    meta.date_source = source


# ---------------------------------------------------------------- video

def ffprobe(path: Path) -> dict:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)],
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(out.stdout)


def _rate(value: str | None) -> float | None:
    if not value or value in ("0/0", "0"):
        return None
    if "/" in value:
        num, den = value.split("/", 1)
        return float(num) / float(den) if float(den) else None
    return float(value)


def _rotation(stream: dict) -> int:
    for sd in stream.get("side_data_list", []) or []:
        if "rotation" in sd:
            return int(float(sd["rotation"]))
    rot = (stream.get("tags") or {}).get("rotate")
    return int(rot) if rot else 0


def probe_video(path: Path, tz: str) -> Meta:
    info = ffprobe(path)
    fmt = info.get("format", {})
    tags = {k.lower(): v for k, v in (fmt.get("tags") or {}).items()}
    streams = info.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    meta = Meta()
    meta.has_audio = any(s.get("codec_type") == "audio" for s in streams)
    if fmt.get("duration"):
        meta.duration = float(fmt["duration"])
    if video:
        w, h = video.get("width"), video.get("height")
        if abs(_rotation(video)) % 180 == 90:
            w, h = h, w
        meta.width, meta.height = w, h
        meta.fps = _rate(video.get("avg_frame_rate")) or _rate(video.get("r_frame_rate"))
        if meta.duration is None and video.get("duration"):
            meta.duration = float(video["duration"])
        vtags = {k.lower(): v for k, v in (video.get("tags") or {}).items()}
        tags = {**vtags, **tags}

    dt = None
    for key in ("com.apple.quicktime.creationdate", "creation_time", "date"):
        if key in tags:
            dt = parse_meta_datetime(tags[key], tz)
            if dt:
                break
    _finish_date(meta, path, tz, dt)

    for key in ("com.apple.quicktime.location.iso6709", "location", "location-eng"):
        if key in tags and (gps := parse_iso6709(tags[key])):
            meta.lat, meta.lon = gps
            break
    make = tags.get("com.apple.quicktime.make") or tags.get("make")
    model = tags.get("com.apple.quicktime.model") or tags.get("model")
    meta.camera = " ".join(x for x in (make, model) if x) or None
    return meta


# ---------------------------------------------------------------- photo

def _register_heif() -> None:
    try:
        import pillow_heif  # type: ignore

        pillow_heif.register_heif_opener()
    except ImportError:
        pass


_register_heif()


def _dms(value) -> float:
    d, m, s = (float(x) for x in value)
    return d + m / 60 + s / 3600


def probe_photo(path: Path, tz: str) -> Meta:
    from PIL import Image

    meta = Meta()
    dt = None
    try:
        with Image.open(path) as im:
            exif = im.getexif()
            w, h = im.size
            if exif.get(0x0112) in (5, 6, 7, 8):  # rotated 90/270
                w, h = h, w
            meta.width, meta.height = w, h
            ifd = exif.get_ifd(0x8769)
            raw = ifd.get(36867) or exif.get(306)  # DateTimeOriginal / DateTime
            if raw:
                offset = ifd.get(36881)  # OffsetTimeOriginal e.g. "+05:30"
                dt = parse_meta_datetime(f"{raw}{offset or ''}", tz)
            gps = exif.get_ifd(0x8825)
            if gps and 2 in gps and 4 in gps:
                lat, lon = _dms(gps[2]), _dms(gps[4])
                if gps.get(1) == "S":
                    lat = -lat
                if gps.get(3) == "W":
                    lon = -lon
                if lat or lon:
                    meta.lat, meta.lon = lat, lon
            make, model = exif.get(271), exif.get(272)
            meta.camera = " ".join(str(x).strip() for x in (make, model) if x) or None
    except Exception:  # unreadable or unsupported (e.g. HEIC without pillow-heif)
        pass
    _finish_date(meta, path, tz, dt)
    return meta


def probe(path: Path, kind: str, tz: str) -> Meta:
    return probe_video(path, tz) if kind == "video" else probe_photo(path, tz)
