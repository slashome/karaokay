"""Small shared helpers."""


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
