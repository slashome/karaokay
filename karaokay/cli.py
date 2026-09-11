"""Command-line entry point: argument parsing and the curses bootstrap."""

import os
import sys
import curses
import logging
import argparse

from . import __version__, config
from .mpd import MPD_AVAILABLE
from .ui import KaraokeUI

# ASCII masthead shown at the top of --help (mirrors the README banner).
BANNER = """
  ▄█   ▄█▄    ▄████████    ▄████████    ▄████████  ▄██████▄     ▄█   ▄█▄    ▄████████ ▄██   ▄
  ███ ▄███▀   ███    ███   ███    ███   ███    ███ ███    ███   ███ ▄███▀   ███    ███ ███   ██▄
  ███▐██▀     ███    ███   ███    ███   ███    ███ ███    ███   ███▐██▀     ███    ███ ███▄▄▄███
 ▄█████▀      ███    ███  ▄███▄▄▄▄██▀   ███    ███ ███    ███  ▄█████▀      ███    ███ ▀▀▀▀▀▀███
▀▀█████▄    ▀███████████ ▀▀███▀▀▀▀▀   ▀███████████ ███    ███ ▀▀█████▄    ▀███████████ ▄██   ███
  ███▐██▄     ███    ███ ▀███████████   ███    ███ ███    ███   ███▐██▄     ███    ███ ███   ███
  ███ ▀███▄   ███    ███   ███    ███   ███    ███ ███    ███   ███ ▀███▄   ███    ███ ███   ███
  ███   ▀█▀   ███    █▀    ███    ███   ███    █▀   ▀██████▀    ███   ▀█▀   ███    █▀   ▀█████▀
  ▀                        ███    ███                           ▀
"""

# Playlist pane width, as a percentage of the terminal width.
DEFAULT_PLAYLIST_WIDTH = 50
PLAYLIST_WIDTH_MIN     = 20
PLAYLIST_WIDTH_MAX     = 80

# Silence noisy logs from syncedlyrics providers (e.g. Musixmatch 401)
logging.getLogger("syncedlyrics").setLevel(logging.CRITICAL)
logging.getLogger("root").setLevel(logging.CRITICAL)
logging.basicConfig(level=logging.CRITICAL)


def main():
    if not MPD_AVAILABLE:
        print("Error: python-mpd2 required.  pip install python-mpd2")
        sys.exit(1)

    p = argparse.ArgumentParser(
        prog="karaokay",
        description=f"{BANNER}\nKaraoke CLI synchronized with MPD",
        epilog=f"Defaults are read from {config.config_path()} "
               "(YAML); the flags above override it.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--version", action="version",
                   version=f"%(prog)s {__version__}")
    p.add_argument("--host",       default=os.getenv("MPD_HOST", "localhost"),
                   help="MPD host (default: localhost or $MPD_HOST)")
    p.add_argument("--port",       type=int, default=int(os.getenv("MPD_PORT", 6600)),
                   help="MPD port (default: 6600 or $MPD_PORT)")
    p.add_argument("--password",   default=os.getenv("MPD_PASSWORD"),
                   help="MPD password")
    p.add_argument("--music-dir",  default=os.getenv("MPD_MUSIC_DIR", ""),
                   help="Music root directory (to find .lrc files side-by-side)")
    p.add_argument("--lyrics-dir", action="append", default=[],
                   help="Folder(s) containing .lrc files (repeatable)")
    p.add_argument("--offset",     type=int, default=0,
                   help="Initial offset in ms (positive = move lyrics ahead)")
    p.add_argument("--no-autofetch", action="store_true",
                   help="Disable automatic lyrics retrieval via syncedlyrics")
    p.add_argument("--no-cover", action="store_true",
                   help="Disable album cover display (requires pillow)")
    p.add_argument("--cover-protocol", default="auto",
                   choices=["auto", "kitty", "iterm2", "blocks", "blocks256", "none"],
                   help="Cover rendering backend (default: auto-detect)")
    p.add_argument("--playlist-width", type=int, default=DEFAULT_PLAYLIST_WIDTH,
                   metavar="PCT",
                   help="Width of the `l` playlist pane, in %% of the screen "
                        f"({PLAYLIST_WIDTH_MIN}-{PLAYLIST_WIDTH_MAX}, default: "
                        f"{DEFAULT_PLAYLIST_WIDTH})")

    # The config file supplies the defaults; the flags above still win, since
    # argparse only falls back to a default when the flag is absent.
    cfg, warnings = config.load()
    cfg_lyrics_dir = cfg.pop("lyrics_dir", [])
    p.set_defaults(**cfg)
    args = p.parse_args()

    # `--lyrics-dir` appends, so it is merged by hand: the flag replaces the
    # configured folders rather than adding to them.
    if not args.lyrics_dir:
        args.lyrics_dir = cfg_lyrics_dir or [os.path.expanduser("~/.lyrics")]

    if not PLAYLIST_WIDTH_MIN <= args.playlist_width <= PLAYLIST_WIDTH_MAX:
        clamped = max(PLAYLIST_WIDTH_MIN, min(PLAYLIST_WIDTH_MAX, args.playlist_width))
        warnings.append(f"playlist-width {args.playlist_width}% out of range "
                        f"({PLAYLIST_WIDTH_MIN}-{PLAYLIST_WIDTH_MAX}) — using {clamped}%")
        args.playlist_width = clamped

    # Reported before curses takes over the screen.
    for line in warnings:
        print(f"karaokay: {line}", file=sys.stderr)

    try:
        curses.wrapper(lambda s: KaraokeUI(s, args).run())
    except KeyboardInterrupt:
        pass  # Ctrl+C: curses.wrapper has already restored the terminal
