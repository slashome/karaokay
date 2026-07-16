"""Small shared helpers."""


def fmt_time(secs: float) -> str:
    secs = max(0, int(secs))
    return f"{secs // 60}:{secs % 60:02d}"
