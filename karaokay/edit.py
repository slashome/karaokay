"""The timing editor behind the `e` key.

Two jobs, one mechanism:

  * fixing a mistimed line in an existing .lrc, and
  * turning plain text (a .txt dropped next to the audio, or a `plain`
    candidate saved from the `f` picker) into a synced .lrc.

Both come down to giving lines a timestamp while the track plays, so the
editor holds a list of `LyricEntry` and stamps the one under the cursor with
the current playback position.
"""

from dataclasses import dataclass
from typing import Optional

from .lrc import LyricEntry, format_lrc, parse_entries


# How far one nudge moves a timestamp, in seconds.
NUDGE = 0.1


@dataclass
class EditSession:
    """A lyrics file being retimed, plus the cursor into it."""

    entries: list[LyricEntry]
    sel:     int = 0
    scroll:  int = 0

    def __post_init__(self):
        # Snapshot used to tell whether anything actually changed, so leaving
        # an untouched session never asks for a confirmation.
        self._initial = [(e.time, e.text) for e in self.entries]

    @classmethod
    def from_text(cls, text: str) -> "EditSession":
        """Start a session from .lrc or plain-text content."""
        return cls(entries=parse_entries(text))

    # ── State ────────────────────────────────────────────────────────────────

    @property
    def changes(self) -> int:
        """How many lines differ from the file as it was opened."""
        current = [(e.time, e.text) for e in self.entries]
        return sum(1 for a, b in zip(self._initial, current) if a != b)

    @property
    def timed(self) -> int:
        return sum(1 for e in self.entries if e.time is not None)

    def active_index(self, elapsed: float) -> int:
        """Index of the line playing at `elapsed`, or -1 before the first one.

        Timestamps are compared in file order rather than sorted: a line being
        retimed may sit out of order for a moment, and the cursor should not
        jump around while that is being fixed.
        """
        best, best_t = -1, None
        for i, e in enumerate(self.entries):
            if e.time is not None and e.time <= elapsed:
                if best_t is None or e.time >= best_t:
                    best, best_t = i, e.time
        return best

    # ── Edits ────────────────────────────────────────────────────────────────

    def set_time(self, index: int, t: float):
        if 0 <= index < len(self.entries):
            self.entries[index].time = max(0.0, t)

    def clear_time(self, index: int):
        if 0 <= index < len(self.entries):
            self.entries[index].time = None

    def nudge(self, index: int, steps: int):
        """Shift a line's timestamp by `steps` × NUDGE seconds."""
        if not (0 <= index < len(self.entries)):
            return
        entry = self.entries[index]
        if entry.time is not None:
            entry.time = max(0.0, entry.time + steps * NUDGE)

    def move(self, delta: int):
        if self.entries:
            self.sel = max(0, min(len(self.entries) - 1, self.sel + delta))

    # ── Output ───────────────────────────────────────────────────────────────

    def to_lrc(self) -> str:
        return format_lrc(self.entries)

    def mark_saved(self):
        """Treat the current state as the reference, after a successful write."""
        self._initial = [(e.time, e.text) for e in self.entries]


def session_for(lyrics_text: Optional[str]) -> Optional[EditSession]:
    """Build a session from file content, or None when there is nothing to edit."""
    if not lyrics_text or not lyrics_text.strip():
        return None
    session = EditSession.from_text(lyrics_text)
    return session if session.entries else None
