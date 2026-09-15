"""Small shared helpers."""

import re


def fmt_time(secs: float) -> str:
    secs = max(0, int(secs))
    return f"{secs // 60}:{secs % 60:02d}"


def fit(text: str, width: int) -> str:
    """`text` padded or truncated to exactly `width` columns."""
    if width <= 0:
        return ""
    if len(text) <= width:
        return text.ljust(width)
    return text[:width - 1] + "…" if width > 1 else text[:1]


# Control characters curses refuses or mangles: a NUL makes addstr() raise
# ValueError outright — it is not a curses.error, so it escapes the usual
# guard — and the rest scramble the line they land on. Lyrics files carry
# them more often than one would think (a .txt saved as UTF-16, a tag ripped
# straight from an ID3 frame).
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]")


def printable(text: str) -> str:
    """`text` stripped of the control characters curses cannot render."""
    return _CONTROL_RE.sub("", text)
