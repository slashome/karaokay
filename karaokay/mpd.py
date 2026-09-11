"""MPD connection helpers with automatic reconnection."""

from typing import Optional

try:
    from mpd import MPDClient, ConnectionError as MPDConnectionError  # noqa: F401
    MPD_AVAILABLE = True
except ImportError:
    MPDClient = None            # type: ignore[assignment]
    MPDConnectionError = Exception
    MPD_AVAILABLE = False


def make_client(host: str, port: int, password: Optional[str]) -> "MPDClient":
    client = MPDClient()
    client.connect(host, port)
    if password:
        client.password(password)
    return client


def safe_status(client: "MPDClient") -> dict:
    try:
        return client.status()
    except Exception:
        return {}


def safe_currentsong(client: "MPDClient") -> dict:
    try:
        return client.currentsong()
    except Exception:
        return {}


def safe_playlist(client: "MPDClient") -> list[dict]:
    """The current queue, as MPD reports it (empty on any failure)."""
    try:
        return client.playlistinfo()
    except Exception:
        return []
