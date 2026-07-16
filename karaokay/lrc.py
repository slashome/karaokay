"""LRC parsing, lookup and write-path resolution."""

import os
import re
from pathlib import Path
from dataclasses import dataclass
from typing import Optional


# ── Structures ────────────────────────────────────────────────────────────────

@dataclass
class LyricLine:
    time: float      # seconds
    text: str


# ── LRC parser ────────────────────────────────────────────────────────────────

TIME_RE = re.compile(r"\[(\d+):(\d+)\.(\d+)\]")


def parse_lrc_text(text: str) -> list[LyricLine]:
    """Parse an in-memory LRC string into timed lines (unsynced text yields [])."""
    lines: list[LyricLine] = []
    for raw in text.splitlines():
        raw = raw.rstrip()
        for m in TIME_RE.finditer(raw):
            mins, secs, cs = int(m.group(1)), int(m.group(2)), int(m.group(3))
            t = mins * 60 + secs + cs / 100
            body = TIME_RE.sub("", raw).strip()
            if body:
                lines.append(LyricLine(time=t, text=body))
    return sorted(lines, key=lambda l: l.time)


def parse_lrc(path: str) -> list[LyricLine]:
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return parse_lrc_text(f.read())
    except OSError:
        return []


# ── LRC file lookup ───────────────────────────────────────────────────────────

def find_lrc(song: dict, lyrics_dirs: list[str], music_dir: str) -> Optional[str]:
    """
    Looks for <title>.lrc in:
      1. The audio file's folder (if music_dir is set)
      2. The --lyrics-dir folders passed as arguments
    """
    candidates: list[str] = []

    # 1. Same folder as the audio file
    if music_dir and song.get("file"):
        audio_path = Path(music_dir) / song["file"]
        candidates.append(str(audio_path.with_suffix(".lrc")))

    # 2. Dedicated lyrics folders
    title  = song.get("title", "")
    artist = song.get("artist", "")
    for d in lyrics_dirs:
        if title and artist:
            candidates.append(os.path.join(d, f"{artist} - {title}.lrc"))
            candidates.append(os.path.join(d, f"{title}.lrc"))
        if song.get("file"):
            base = Path(song["file"]).stem
            candidates.append(os.path.join(d, f"{base}.lrc"))

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
