"""Lyrics fetching.

Two entry points:

  * `simple_search`  — one best synced match, used by the automatic autofetch
                        when a track has no local .lrc.
  * `FetchSession`   — the interactive multi-candidate search behind the `f`
                        panel: it gathers several alternatives (many from
                        Lrclib, one best match from each other provider) in a
                        background thread so several sites can be compared and
                        one picked by hand.
"""

import threading
from dataclasses import dataclass, field
from typing import Optional

from .lrc import LyricLine, parse_lrc_text

try:
    import syncedlyrics
    SYNCEDLYRICS_AVAILABLE = True
except ImportError:
    SYNCEDLYRICS_AVAILABLE = False

try:
    import requests
    REQUESTS_AVAILABLE = True
except ImportError:
    REQUESTS_AVAILABLE = False


# Lrclib is queried directly (clean public JSON API, no key) so it can return
# several candidates. The remaining providers go through syncedlyrics, which
# collapses each to a single best match.
LRCLIB_ENDPOINT   = "https://lrclib.net/api/search"
LRCLIB_MAX        = 10          # keep the list manageable
USER_AGENT        = "karaokay (https://github.com/slashome/karaokay)"
SYNCED_PROVIDERS  = ["Musixmatch", "NetEase", "Megalobiz", "Genius"]


def simple_search(query: str) -> Optional[str]:
    """One best synced-only match, or None. Used by the automatic autofetch."""
    if not SYNCEDLYRICS_AVAILABLE:
        return None
    return syncedlyrics.search(query, synced_only=True)


@dataclass
class LyricCandidate:
    provider: str
    title:    str
    artist:   str
    album:    str
    duration: Optional[float]
    synced:   bool
    text:     str                                   # raw LRC or plain text
    lines:    list[LyricLine] = field(default_factory=list)   # parsed (synced only)

    def label(self) -> str:
        """A one-line human description for the candidate list."""
        if self.artist or self.title:
            who = " - ".join(p for p in (self.artist, self.title) if p)
        else:
            # No metadata (syncedlyrics providers): fall back to the first line.
            who = (self.lines[0].text if self.lines
                   else self.text.strip().splitlines()[0] if self.text.strip()
                   else "(empty)")
        return who


class FetchSession:
    """Runs a multi-source lyrics search in a background thread.

    Thread-safe: the UI polls `snapshot()` every frame; the worker appends
    candidates and updates per-provider status as results arrive.
    """

    def __init__(self, query: str):
        self.query      = query
        self.lock       = threading.Lock()
        self.candidates: list[LyricCandidate] = []
        self.status:     dict[str, str] = {}    # provider -> state string
        self.done       = False
        self._seen:      set[str] = set()
        self._thread:    Optional[threading.Thread] = None

    # ── Public API ───────────────────────────────────────────────────────────

    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def snapshot(self) -> tuple[list[LyricCandidate], dict[str, str], bool]:
        with self.lock:
            return list(self.candidates), dict(self.status), self.done

    # ── Worker ────────────────────────────────────────────────────────────────

    def _set_status(self, provider: str, state: str):
        with self.lock:
            self.status[provider] = state

    def _add(self, cand: LyricCandidate) -> bool:
        """Append a candidate unless an identical text is already listed."""
        key = "".join(cand.text.split())
        if not key:
            return False
        with self.lock:
            if key in self._seen:
                return False
            self._seen.add(key)
            self.candidates.append(cand)
            return True

    def _run(self):
        self._fetch_lrclib()
        for name in SYNCED_PROVIDERS:
            self._fetch_synced_provider(name)
        with self.lock:
            self.done = True

    def _fetch_lrclib(self):
        if not REQUESTS_AVAILABLE:
            self._set_status("Lrclib", "no_requests")
            return
        self._set_status("Lrclib", "searching")
        try:
            r = requests.get(
                LRCLIB_ENDPOINT,
                params={"q": self.query},
                headers={"User-Agent": USER_AGENT},
                timeout=10,
            )
            r.raise_for_status()
            tracks = r.json()
        except Exception:
            self._set_status("Lrclib", "error")
            return

        added = 0
        for t in tracks:
            synced = (t.get("syncedLyrics") or "").strip()
            plain  = (t.get("plainLyrics") or "").strip()
            text   = synced or plain
            if not text:
                continue
            cand = LyricCandidate(
                provider="Lrclib",
                title=t.get("trackName") or "",
                artist=t.get("artistName") or "",
                album=t.get("albumName") or "",
                duration=t.get("duration"),
                synced=bool(synced),
                text=text,
                lines=parse_lrc_text(text) if synced else [],
            )
            if self._add(cand):
                added += 1
            if added >= LRCLIB_MAX:
                break
        self._set_status("Lrclib", "done" if added else "none")

    def _fetch_synced_provider(self, name: str):
        if not SYNCEDLYRICS_AVAILABLE:
            self._set_status(name, "no_module")
            return
        self._set_status(name, "searching")
        try:
            text = syncedlyrics.search(self.query, providers=[name])
        except Exception:
            self._set_status(name, "error")
            return
        if not text or not text.strip():
            self._set_status(name, "none")
            return
        text  = text.strip()
        lines = parse_lrc_text(text)
        cand  = LyricCandidate(
            provider=name, title="", artist="", album="", duration=None,
            synced=bool(lines), text=text, lines=lines,
        )
        added = self._add(cand)
        self._set_status(name, "done" if added else "dup")
