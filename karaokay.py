#!/usr/bin/env python3
"""
mpd-karaoke — Synchronized lyrics display for MPD
Dependencies: python-mpd2, windows-curses (Windows only)
  pip install python-mpd2
Optional (autofetch of synchronized lyrics):
  pip install syncedlyrics
"""

import curses
import time
import re
import os
import sys
import socket
import argparse
import logging
import textwrap
import threading
from pathlib import Path
from dataclasses import dataclass
from typing import Optional

# Silence noisy logs from syncedlyrics providers (e.g. Musixmatch 401)
logging.getLogger("syncedlyrics").setLevel(logging.CRITICAL)
logging.getLogger("root").setLevel(logging.CRITICAL)
logging.basicConfig(level=logging.CRITICAL)

try:
    from mpd import MPDClient, ConnectionError as MPDConnectionError
except ImportError:
    print("Error: python-mpd2 required.  pip install python-mpd2")
    sys.exit(1)

try:
    import syncedlyrics
    SYNCEDLYRICS_AVAILABLE = True
except ImportError:
    SYNCEDLYRICS_AVAILABLE = False


# ── Structures ────────────────────────────────────────────────────────────────

@dataclass
class LyricLine:
    time: float      # seconds
    text: str


# ── LRC parser ────────────────────────────────────────────────────────────────

TIME_RE = re.compile(r"\[(\d+):(\d+)\.(\d+)\]")


def parse_lrc(path: str) -> list[LyricLine]:
    lines: list[LyricLine] = []
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for raw in f:
                raw = raw.rstrip()
                for m in TIME_RE.finditer(raw):
                    mins, secs, cs = int(m.group(1)), int(m.group(2)), int(m.group(3))
                    t = mins * 60 + secs + cs / 100
                    text = TIME_RE.sub("", raw).strip()
                    if text:
                        lines.append(LyricLine(time=t, text=text))
    except OSError:
        pass
    return sorted(lines, key=lambda l: l.time)


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


# ── MPD connection with automatic reconnection ────────────────────────────────

def make_client(host: str, port: int, password: Optional[str]) -> MPDClient:
    client = MPDClient()
    client.connect(host, port)
    if password:
        client.password(password)
    return client


def safe_status(client: MPDClient) -> dict:
    try:
        return client.status()
    except Exception:
        return {}


def safe_currentsong(client: MPDClient) -> dict:
    try:
        return client.currentsong()
    except Exception:
        return {}


# ── Time formatting ───────────────────────────────────────────────────────────

def fmt_time(secs: float) -> str:
    secs = max(0, int(secs))
    return f"{secs // 60}:{secs % 60:02d}"


# ── Curses interface ──────────────────────────────────────────────────────────

class KaraokeUI:
    # Ratio of lines displayed before the active line (the rest goes after).
    # 0.33 = the active line sits at ~1/3 from the top of the lyrics area.
    BEFORE_RATIO = 0.33

    def __init__(self, stdscr, args):
        self.stdscr   = stdscr
        self.args     = args
        self.offset   = args.offset  # ms
        self.music_dir: str            = args.music_dir or ""
        self.lyrics:   list[LyricLine] = []
        self.lrc_path: Optional[str]   = None
        self.last_song: Optional[str]  = None
        self.client:   Optional[MPDClient] = None

        self.fetch_lock = threading.Lock()
        self.fetch_state: Optional[str] = None   # "fetching" | "found" | "not_found" | "error"
        self.fetch_attempted: set[str] = set()
        self.fetch_thread: Optional[threading.Thread] = None

        curses.curs_set(0)
        stdscr.nodelay(True)
        stdscr.timeout(100)
        self._init_colors()

    # ── Colors ───────────────────────────────────────────────────────────────

    def _init_colors(self):
        curses.start_color()
        curses.use_default_colors()
        # (foreground, background)
        curses.init_pair(1, curses.COLOR_WHITE,   -1)  # active
        curses.init_pair(2, curses.COLOR_CYAN,    -1)  # past
        curses.init_pair(3, 8,                    -1)  # upcoming (gray)
        curses.init_pair(4, curses.COLOR_GREEN,   -1)  # info
        curses.init_pair(5, curses.COLOR_YELLOW,  -1)  # warning
        curses.init_pair(6, curses.COLOR_RED,     -1)  # error
        curses.init_pair(7, curses.COLOR_BLUE,    -1)  # progress bar

    # ── Connection ────────────────────────────────────────────────────────────

    def connect(self) -> bool:
        try:
            self.client = make_client(self.args.host, self.args.port, self.args.password)
        except Exception:
            self.client = None
            return False
        if not self.music_dir:
            self.music_dir = self._detect_music_dir()
        return True

    def _detect_music_dir(self) -> str:
        """Retrieve music_directory either via MPD's `config` command
        (local UNIX socket only), or by parsing standard MPD config
        files."""
        # 1. `config` command (local UNIX socket + admin rights)
        if self.client:
            try:
                cfg = self.client.config()
                if isinstance(cfg, dict):
                    path = cfg.get("music_directory", "")
                    if path:
                        return os.path.expanduser(path)
            except Exception:
                pass

        # 2. Parsing common MPD config files
        candidates = [
            os.path.expanduser("~/.config/mpd/mpd.conf"),
            os.path.expanduser("~/.mpdconf"),
            "/etc/mpd.conf",
            "/usr/local/etc/mpd.conf",
            "/opt/homebrew/etc/mpd.conf",
        ]
        conf_re = re.compile(r'^\s*music_directory\s+"([^"]+)"')
        for conf in candidates:
            try:
                with open(conf, encoding="utf-8", errors="replace") as f:
                    for line in f:
                        m = conf_re.match(line)
                        if m:
                            return os.path.expanduser(m.group(1))
            except OSError:
                continue
        return ""

    # ── Main loop ─────────────────────────────────────────────────────────────

    def run(self):
        while True:
            key = self.stdscr.getch()
            if key in (ord("q"), ord("Q"), 27):
                break
            elif key == ord("+"):
                self.offset += 50
            elif key == ord("-"):
                self.offset -= 50
            elif key in (ord("s"), ord("S")):
                # Forced resync: reload lyrics
                self.last_song = None
            elif key in (ord("p"), ord("P")):
                self._toggle_pause()
            elif key in (ord("f"), ord("F")):
                with self.fetch_lock:
                    self.fetch_attempted.clear()
                    self.fetch_state = None
                self.last_song = None
            elif key == curses.KEY_RESIZE:
                self.stdscr.clear()

            if not self.client:
                if not self.connect():
                    self._draw_error("MPD unreachable — retrying in 3s…")
                    time.sleep(3)
                    continue

            try:
                status = safe_status(self.client)
                song   = safe_currentsong(self.client)
            except Exception:
                self.client = None
                continue

            self._maybe_reload_lyrics(song)
            self._draw(status, song)

    # ── Playback control ──────────────────────────────────────────────────────

    def _toggle_pause(self):
        if not self.client:
            return
        try:
            state = safe_status(self.client).get("state", "stop")
            if state == "play":
                self.client.pause(1)
            elif state == "pause":
                self.client.pause(0)
            else:
                self.client.play()
        except Exception:
            self.client = None

    # ── Lyrics auto-fetch ─────────────────────────────────────────────────────

    def _start_autofetch(self, song: dict):
        if not SYNCEDLYRICS_AVAILABLE:
            with self.fetch_lock:
                self.fetch_state = "no_module"
            return
        if self.args.no_autofetch:
            with self.fetch_lock:
                self.fetch_state = "disabled"
            return
        title = (song.get("title") or "").strip()
        artist = (song.get("artist") or "").strip()
        song_id = song.get("id") or song.get("file")
        if not title or not song_id:
            with self.fetch_lock:
                self.fetch_state = "no_meta"
            return

        with self.fetch_lock:
            if song_id in self.fetch_attempted:
                # keep the previous state (not_found / error) to stay informative
                return
            if self.fetch_thread and self.fetch_thread.is_alive():
                return
            self.fetch_attempted.add(song_id)
            self.fetch_state = "fetching"

        safe = lambda s: re.sub(r'[<>:"/\\|?*]', "_", s).strip()

        # 1. Preferred: next to the audio file if music_dir is known
        #    and the folder is writable.
        target_path: Optional[str] = None
        if self.music_dir and song.get("file"):
            audio_path = Path(self.music_dir) / song["file"]
            audio_dir  = audio_path.parent
            if audio_dir.is_dir() and os.access(audio_dir, os.W_OK):
                target_path = str(audio_path.with_suffix(".lrc"))

        # 2. Fallback: dedicated lyrics folder.
        if target_path is None:
            target_dir = self.args.lyrics_dir[0] if self.args.lyrics_dir else os.path.expanduser("~/.lyrics")
            try:
                os.makedirs(target_dir, exist_ok=True)
            except OSError:
                with self.fetch_lock:
                    self.fetch_state = "error"
                return
            if artist:
                filename = f"{safe(artist)} - {safe(title)}.lrc"
            else:
                filename = f"{safe(title)}.lrc"
            target_path = os.path.join(target_dir, filename)

        query = f"{title} {artist}".strip()

        def worker():
            try:
                result = syncedlyrics.search(query, synced_only=True)
                if not result:
                    with self.fetch_lock:
                        self.fetch_state = "not_found"
                    return
                with open(target_path, "w", encoding="utf-8") as f:
                    f.write(result)
                with self.fetch_lock:
                    self.fetch_state = "found"
            except Exception:
                with self.fetch_lock:
                    self.fetch_state = "error"

        self.fetch_thread = threading.Thread(target=worker, daemon=True)
        self.fetch_thread.start()

    # ── Lyrics reload ─────────────────────────────────────────────────────────

    def _maybe_reload_lyrics(self, song: dict):
        song_id = song.get("id") or song.get("file")
        if song_id == self.last_song:
            # Has the autofetch thread dropped a file?
            with self.fetch_lock:
                ready = self.fetch_state == "found" and not self.lyrics
                if ready:
                    self.fetch_state = None
            if ready:
                self.lrc_path = find_lrc(song, self.args.lyrics_dir, self.music_dir)
                self.lyrics   = parse_lrc(self.lrc_path) if self.lrc_path else []
            return
        self.last_song = song_id
        self.lrc_path  = find_lrc(song, self.args.lyrics_dir, self.music_dir)
        self.lyrics    = parse_lrc(self.lrc_path) if self.lrc_path else []
        if not self.lrc_path:
            self._start_autofetch(song)
        else:
            with self.fetch_lock:
                self.fetch_state = None

    # ── Rendering ─────────────────────────────────────────────────────────────

    def _draw(self, status: dict, song: dict):
        h, w = self.stdscr.getmaxyx()
        self.stdscr.erase()

        row = 0

        # — Status bar ——————————————————————————————————————————
        state = status.get("state", "stop")
        state_icon = {"play": "▶", "pause": "⏸", "stop": "■"}.get(state, "?")
        connected  = f"{state_icon} MPD {self.args.host}:{self.args.port}"
        if self.lrc_path:
            lrc_info = f"  LRC: {os.path.basename(self.lrc_path)}"
        else:
            with self.fetch_lock:
                fs = self.fetch_state
            if fs == "fetching":
                lrc_info = "  LRC: searching online…"
            elif fs == "not_found":
                lrc_info = "  LRC: no synchronized version found"
            elif fs == "error":
                lrc_info = "  LRC: fetch error"
            elif fs == "no_module":
                lrc_info = "  LRC: 'syncedlyrics' module missing (pip install syncedlyrics)"
            elif fs == "disabled":
                lrc_info = "  LRC: autofetch disabled (--no-autofetch)"
            elif fs == "no_meta":
                lrc_info = "  LRC: insufficient metadata (no title)"
            else:
                lrc_info = "  LRC: not found"
        off_str    = f"  offset: {'+' if self.offset >= 0 else ''}{self.offset}ms"
        status_line = connected + lrc_info + off_str
        self._addstr(row, 0, status_line[:w], curses.color_pair(4) | curses.A_BOLD)
        row += 1

        # — Separator ———————————————————————————————————————————
        self._hline(row, w); row += 1

        # — Title / Artist ——————————————————————————————————————
        title  = song.get("title")  or Path(song.get("file", "?")).stem
        artist = song.get("artist") or "Unknown artist"
        album  = song.get("album",  "")
        self._addstr(row, 0, title[:w],  curses.color_pair(1) | curses.A_BOLD); row += 1
        info = f"{artist}"
        if album:
            info += f" — {album}"
        self._addstr(row, 0, info[:w], curses.color_pair(3)); row += 1

        # — Progress bar ————————————————————————————————————————
        row += 1
        elapsed_raw = float(status.get("elapsed", 0))
        duration    = float(status.get("duration") or song.get("time") or 0) or 1
        pct         = min(1.0, elapsed_raw / duration)
        bar_w       = w - 18
        filled      = int(bar_w * pct)
        bar = "█" * filled + "░" * (bar_w - filled)
        prog_line   = f" {fmt_time(elapsed_raw)} [{bar}] {fmt_time(duration)}"
        self._addstr(row, 0, prog_line[:w], curses.color_pair(7))
        row += 2

        # — Separator ———————————————————————————————————————————
        self._hline(row, w); row += 1

        # — Lyrics ——————————————————————————————————————————————
        elapsed_adj = elapsed_raw + self.offset / 1000.0

        if not self.lyrics:
            msg = "No lyrics (drop a .lrc next to the audio file)"
            self._addstr(row, 2, msg[:w-2], curses.color_pair(5))
        else:
            active = -1
            for i in range(len(self.lyrics) - 1, -1, -1):
                if elapsed_adj >= self.lyrics[i].time:
                    active = i
                    break

            avail = max(1, h - row - 2)  # available lines (- help at the bottom)

            # Expand each line into as many visual rows as needed to handle
            # wrapping for phrases wider than the terminal.
            visual_rows: list[tuple[int, bool, str]] = []
            active_visual_start: Optional[int] = None
            for idx, lyr in enumerate(self.lyrics):
                time_tag = f"[{fmt_time(lyr.time)}] "
                body_w   = max(1, w - 4 - len(time_tag))
                pieces   = textwrap.wrap(
                    lyr.text, width=body_w,
                    break_long_words=True,
                    break_on_hyphens=False,
                ) or [""]
                if idx == active:
                    active_visual_start = len(visual_rows)
                visual_rows.append((idx, True, time_tag + pieces[0]))
                cont = " " * len(time_tag)
                for piece in pieces[1:]:
                    visual_rows.append((idx, False, cont + piece))

            if active_visual_start is None:
                active_visual_start = 0

            target_before = int(avail * self.BEFORE_RATIO)
            start = max(0, active_visual_start - target_before)
            end   = min(len(visual_rows), start + avail)
            if end - start < avail:
                start = max(0, end - avail)

            for i, (abs_i, is_first, text) in enumerate(visual_rows[start:end]):
                lrow = row + i
                if lrow >= h - 1:
                    break
                if abs_i == active:
                    attr = curses.color_pair(1) | curses.A_BOLD
                    if is_first:
                        self._addstr(lrow, 0, "▶ ", curses.color_pair(4) | curses.A_BOLD)
                else:
                    attr = curses.color_pair(3)
                indent_x = 4 if abs_i == active else 2
                self._addstr(lrow, indent_x, text[: w - indent_x], attr)

        # — Help at the bottom —————————————————————————————————
        help_row = h - 1
        help_str = " q:quit  p:play/pause  +:offset+50ms  -:offset-50ms  s:resync  f:refetch "
        self._addstr(help_row, 0, help_str[:w], curses.color_pair(3))

        self.stdscr.refresh()

    def _draw_error(self, msg: str):
        h, w = self.stdscr.getmaxyx()
        self.stdscr.erase()
        self._addstr(h // 2, max(0, (w - len(msg)) // 2), msg[:w], curses.color_pair(6) | curses.A_BOLD)
        self.stdscr.refresh()

    def _addstr(self, y, x, text, attr=0):
        try:
            self.stdscr.addstr(y, x, text, attr)
        except curses.error:
            pass

    def _hline(self, y, w):
        try:
            self.stdscr.addstr(y, 0, "─" * w)
        except curses.error:
            pass


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    p = argparse.ArgumentParser(
        description="Karaoke CLI synchronized with MPD"
    )
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
    args = p.parse_args()

    # Default folder
    if not args.lyrics_dir:
        args.lyrics_dir = [os.path.expanduser("~/.lyrics")]

    curses.wrapper(lambda s: KaraokeUI(s, args).run())


if __name__ == "__main__":
    main()
