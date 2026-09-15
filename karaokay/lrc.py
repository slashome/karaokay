"""LRC parsing, lookup and write-path resolution."""

import os
import re
import codecs
from pathlib import Path
from dataclasses import dataclass
from typing import Optional


# ── Structures ────────────────────────────────────────────────────────────────

@dataclass
class LyricLine:
    time: float      # seconds
    text: str


# ── LRC parser ────────────────────────────────────────────────────────────────

TIME_RE = re.compile(r"\[(\d+):(\d+)(?:[.:](\d+))?\]")


def parse_lrc_text(text: str) -> list[LyricLine]:
    """Parse an in-memory LRC string into timed lines (unsynced text yields [])."""
    lines: list[LyricLine] = []
    for raw in text.splitlines():
        raw = raw.rstrip()
        for m in TIME_RE.finditer(raw):
            mins, secs, frac = int(m.group(1)), int(m.group(2)), m.group(3)
            # The fraction is centiseconds in most files, milliseconds in some,
            # and absent in the plainest ones: read it by its digit count.
            t = float(mins * 60 + secs)
            if frac:
                t += int(frac) / 10 ** len(frac)
            body = TIME_RE.sub("", raw).strip()
            if body:
                lines.append(LyricLine(time=t, text=body))
    return sorted(lines, key=lambda l: l.time)


# A line made only of an LRC metadata tag, e.g. "[ar:Oranssi Pazuzu]".
TAG_ONLY_RE = re.compile(r"^\s*\[[a-zA-Z#]+:[^\]]*\]\s*$")


def plain_lines(text: str) -> list[str]:
    """Untimed lyric lines, for text carrying no timestamps.

    Metadata tags are dropped and the leading/trailing blank lines trimmed;
    blank lines inside the text are kept, they are the verse separators.
    """
    out = [raw.rstrip() for raw in text.splitlines()
           if not TAG_ONLY_RE.match(raw)]
    while out and not out[0].strip():
        out.pop(0)
    while out and not out[-1].strip():
        out.pop()
    return out


@dataclass
class LyricEntry:
    """One editable line: its text and, once set, its timestamp.

    Unlike `LyricLine` — which only ever describes a *timed* line ready to be
    displayed — an entry may carry no time at all. That is what makes editing
    possible: plain text imported from a .txt starts out entirely untimed, and
    a half-synced file keeps its untimed lines instead of dropping them.
    """
    text: str
    time: Optional[float] = None


def parse_entries(text: str) -> list[LyricEntry]:
    """Parse lyrics into editable entries, preserving the file's line order.

    Timed lines keep their first timestamp; untimed ones come through with
    `time=None`. Metadata tags and blank lines are dropped: neither survives a
    round-trip through an LRC file as anything meaningful.
    """
    entries: list[LyricEntry] = []
    for raw in text.splitlines():
        if TAG_ONLY_RE.match(raw):
            continue
        stamps = TIME_RE.findall(raw)
        body   = TIME_RE.sub("", raw).strip()
        if not body:
            continue
        if stamps:
            mins, secs, frac = stamps[0]
            t = float(int(mins) * 60 + int(secs))
            if frac:
                t += int(frac) / 10 ** len(frac)
            entries.append(LyricEntry(text=body, time=t))
        else:
            entries.append(LyricEntry(text=body))
    return entries


def format_lrc(entries: list[LyricEntry]) -> str:
    """Serialize entries back to LRC text, in their current order.

    Untimed entries are written bare: they are kept so a long track can be
    synced over several sittings, and the parser simply ignores them until
    they get a timestamp.
    """
    out = []
    for e in entries:
        out.append(f"[{fmt_stamp(e.time)}]{e.text}" if e.time is not None
                   else e.text)
    return "\n".join(out) + "\n"


def fmt_stamp(t: float) -> str:
    """Seconds as an LRC timestamp: `mm:ss.cc`."""
    t  = max(0.0, t)
    cs = int(round(t * 100))
    return f"{cs // 6000:02d}:{cs // 100 % 60:02d}.{cs % 100:02d}"


# ── Reading lyrics files ──────────────────────────────────────────────────────

def decode_lyrics(data: bytes) -> str:
    """Decode a lyrics file, UTF-8 by default but UTF-16 when it says so.

    Lyrics dropped next to a track come from everywhere — a browser, Notepad,
    a tag editor — and some of them are UTF-16. Read as UTF-8 such a file
    decodes into text riddled with NUL characters, which curses refuses to
    draw, so the encoding is settled here rather than papered over later.
    """
    for bom, enc in ((codecs.BOM_UTF8, "utf-8-sig"),
                     (codecs.BOM_UTF32_LE, "utf-32"),
                     (codecs.BOM_UTF32_BE, "utf-32"),
                     (codecs.BOM_UTF16_LE, "utf-16"),
                     (codecs.BOM_UTF16_BE, "utf-16")):
        if data.startswith(bom):
            try:
                return data.decode(enc)
            except (UnicodeDecodeError, LookupError):
                break
    # No BOM: UTF-16 still gives itself away with a NUL in every other byte —
    # on the odd bytes when it is little-endian, on the even ones when it is
    # big-endian. Whichever side carries more of them is tried first.
    if b"\x00" in data:
        little = data[1::2].count(0) >= data[0::2].count(0)
        for enc in ("utf-16-le", "utf-16-be") if little else ("utf-16-be", "utf-16-le"):
            try:
                text = data.decode(enc)
            except UnicodeDecodeError:
                continue
            if "\x00" not in text:
                return text
    # Anything left: UTF-8 with replacements, minus the NULs a truly binary
    # file would carry — they would travel into the editor and back out into
    # a saved .lrc.
    return data.decode("utf-8", errors="replace").replace("\x00", "")


def read_lyrics_file(path: str) -> Optional[str]:
    """File content as text, or None when it cannot be read."""
    try:
        with open(path, "rb") as f:
            return decode_lyrics(f.read())
    except OSError:
        return None


def load_lyrics(path: str) -> tuple[list[LyricLine], list[str]]:
    """Read a lyrics file as either timed lines or untimed text.

    Returns `(timed, [])` when the file carries timestamps, `([], untimed)`
    when it does not (a plain-text candidate saved from the `f` picker), and
    `([], [])` when it is unreadable or empty.
    """
    text = read_lyrics_file(path)
    if text is None:
        return [], []
    timed = parse_lrc_text(text)
    if timed:
        return timed, []
    return [], plain_lines(text)


# ── LRC file lookup ───────────────────────────────────────────────────────────

# Lyrics files, best first: a synced .lrc beats plain text, which is only a
# starting point for the `e` editor.
LYRICS_SUFFIXES = (".lrc", ".txt")


def find_lyrics_file(song: dict, lyrics_dirs: list[str],
                     music_dir: str) -> Optional[str]:
    """
    Looks for <title>.lrc — then <title>.txt — in:
      1. The audio file's folder (if music_dir is set)
      2. The --lyrics-dir folders passed as arguments
    """
    title  = song.get("title", "")
    artist = song.get("artist", "")

    for suffix in LYRICS_SUFFIXES:
        candidates: list[str] = []

        # 1. Same folder as the audio file
        if music_dir and song.get("file"):
            audio_path = Path(music_dir) / song["file"]
            candidates.append(str(audio_path.with_suffix(suffix)))

        # 2. Dedicated lyrics folders
        for d in lyrics_dirs:
            if title and artist:
                candidates.append(os.path.join(d, f"{artist} - {title}{suffix}"))
                candidates.append(os.path.join(d, f"{title}{suffix}"))
            if song.get("file"):
                base = Path(song["file"]).stem
                candidates.append(os.path.join(d, f"{base}{suffix}"))

        for c in candidates:
            if os.path.isfile(c):
                return c
    return None


# ── Write-path resolution ───────────────────────────────────────────────────
#
# Shared by the automatic autofetch and the interactive fetch panel so both
# drop the .lrc in exactly the same place.

_SAFE_RE = re.compile(r'[<>:"/\\|?*]')


def _safe(s: str) -> str:
    return _SAFE_RE.sub("_", s).strip()


def resolve_write_path(song: dict, music_dir: str,
                       lyrics_dirs: list[str]) -> Optional[str]:
    """Where a freshly fetched .lrc should be written.

    Preference: next to the audio file (the album folder) when music_dir is
    known and the folder is writable; otherwise the first --lyrics-dir (or
    ~/.lyrics), named "<artist> - <title>.lrc".
    """
    # 1. Preferred: alongside the audio file, i.e. inside the album folder.
    if music_dir and song.get("file"):
        audio_path = Path(music_dir) / song["file"]
        audio_dir  = audio_path.parent
        if audio_dir.is_dir() and os.access(audio_dir, os.W_OK):
            return str(audio_path.with_suffix(".lrc"))

    # 2. Fallback: dedicated lyrics folder.
    target_dir = lyrics_dirs[0] if lyrics_dirs else os.path.expanduser("~/.lyrics")
    try:
        os.makedirs(target_dir, exist_ok=True)
    except OSError:
        return None

    title  = (song.get("title") or "").strip()
    artist = (song.get("artist") or "").strip()
    if not title:
        title = Path(song.get("file", "lyrics")).stem
    if artist:
        filename = f"{_safe(artist)} - {_safe(title)}.lrc"
    else:
        filename = f"{_safe(title)}.lrc"
    return os.path.join(target_dir, filename)
