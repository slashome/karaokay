#!/usr/bin/env python3
"""
mpd-karaoke — Synchronized lyrics display for MPD
Dependencies: python-mpd2, windows-curses (Windows only)
  pip install python-mpd2
Optional (autofetch of synchronized lyrics):
  pip install syncedlyrics
Optional (album cover display, incl. WebP/JPEG decoding):
  pip install pillow
"""

import curses
import time
import re
import os
import sys
import io
import base64
import socket
import argparse
import logging
import textwrap
import threading
from pathlib import Path
from dataclasses import dataclass
from typing import Optional

__version__ = "0.4.0"

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

try:
    from PIL import Image
    PIL_AVAILABLE = True
except ImportError:
    PIL_AVAILABLE = False


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


# ── Album cover lookup ────────────────────────────────────────────────────────

COVER_EXTS = (".png", ".jpg", ".jpeg", ".webp")


def find_cover(song: dict, music_dir: str) -> Optional[str]:
    """
    Looks for cover.{png,jpg,jpeg,webp} in the album folder, i.e. the
    directory that holds the currently playing audio file.
    """
    if not (music_dir and song.get("file")):
        return None
    album_dir = (Path(music_dir) / song["file"]).parent

    # Fast path: conventional lower-case names.
    for ext in COVER_EXTS:
        p = album_dir / f"cover{ext}"
        if p.is_file():
            return str(p)

    # Case-insensitive fallback (e.g. Cover.JPG).
    try:
        for entry in album_dir.iterdir():
            if (entry.is_file()
                    and entry.stem.lower() == "cover"
                    and entry.suffix.lower() in COVER_EXTS):
                return str(entry)
    except OSError:
        pass
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


# ── Album cover rendering ─────────────────────────────────────────────────────
#
# curses cannot draw images, so the cover is emitted with raw terminal escape
# sequences in a rectangle that curses is told to leave untouched. A best-effort
# cascade picks the nicest backend the terminal supports:
#
#   kitty   — Kitty graphics protocol (Kitty, Ghostty, WezTerm). Overlay image,
#             transmitted once per track.
#   iterm2  — iTerm2 inline images (iTerm2, WezTerm). Cell-anchored, drawn once
#             per track and kept alive by curses' blank-cell persistence.
#   blocks  — truecolor half-block glyphs (▀). Works in any 24-bit terminal;
#             repainted every frame so it survives curses redraws.
#   blocks256 — same half-block trick quantized to the xterm-256 palette, for
#             terminals without 24-bit color (e.g. Apple Terminal).
#
# All backends require Pillow (decoding + resizing, mandatory for WebP/JPEG).

def detect_cover_backend(override: str) -> Optional[str]:
    if override and override != "auto":
        return None if override == "none" else override

    term      = os.environ.get("TERM", "")
    term_prog = os.environ.get("TERM_PROGRAM", "")

    if (os.environ.get("KITTY_WINDOW_ID")
            or "kitty" in term or "ghostty" in term
            or term_prog == "ghostty"):
        return "kitty"
    if (term_prog in ("iTerm.app", "WezTerm")
            or os.environ.get("LC_TERMINAL") == "iTerm2"):
        return "iterm2"
    # Half-blocks: truecolor when advertised, otherwise the 256-color palette.
    if os.environ.get("COLORTERM") in ("truecolor", "24bit"):
        return "blocks"
    return "blocks256"


class CoverRenderer:
    KITTY_CHUNK = 4096   # base64 chars per kitty transmission chunk

    def __init__(self, override: str = "auto"):
        self.backend  = detect_cover_backend(override)
        self._sig      = None   # (path, x, y, cols, rows) currently displayed
        self._shown    = False
        self._blocks   = None   # cached escape string for the blocks backend

    def available(self) -> bool:
        return self.backend is not None and PIL_AVAILABLE

    # ── Low-level terminal I/O ──────────────────────────────────────────────

    @staticmethod
    def _emit(s: str):
        try:
            sys.stdout.write(s)
            sys.stdout.flush()
        except (OSError, ValueError):
            pass

    @staticmethod
    def _cup(x: int, y: int) -> str:
        # Absolute cursor positioning (1-based), x = column, y = row.
        return f"\x1b[{y + 1};{x + 1}H"

    # ── Public API ──────────────────────────────────────────────────────────

    def show(self, path: str, x: int, y: int, cols: int, rows: int,
             force: bool = False):
        """Display `path` at cell (x, y) spanning cols×rows. Must be called
        AFTER curses' refresh() so the escapes land on a settled screen."""
        if not self.available():
            return
        sig     = (path, x, y, cols, rows)
        changed = force or sig != self._sig
        try:
            if self.backend in ("blocks", "blocks256"):
                if changed or self._blocks is None:
                    self._blocks = self._render_blocks(path, x, y, cols, rows)
                    self._sig    = sig
                if self._blocks:
                    self._emit(self._blocks)   # repaint every frame
                    self._shown = True
            elif self.backend == "kitty":
                if changed:
                    self._clear_kitty()
                    self._emit_kitty(path, x, y, cols, rows)
                    self._sig, self._shown = sig, True
            elif self.backend == "iterm2":
                if changed:
                    self._emit_iterm2(path, x, y, cols, rows)
                    self._sig, self._shown = sig, True
        except Exception:
            # A broken/unreadable image must never crash the UI.
            self._sig = sig   # don't retry this geometry every frame
            self._blocks = ""

    def hide(self):
        """Physically erase whatever cover is on screen (idempotent)."""
        if not self._shown:
            return
        if self._sig:
            _, x, y, cols, rows = self._sig
            if self.backend == "kitty":
                self._clear_kitty()
            else:
                self._clear_region(x, y, cols, rows)
        self._shown = False
        self._sig    = None
        self._blocks = None

    def invalidate(self):
        """Force a full redraw on the next show() (after resize / toggle)."""
        self._sig    = None
        self._blocks = None

    # ── Backends ────────────────────────────────────────────────────────────

    @staticmethod
    def _rgb_to_256(r: int, g: int, b: int) -> int:
        # Map an RGB triple onto the xterm-256 palette: the 6×6×6 color cube
        # (16–231) or the grayscale ramp (232–255).
        if r == g == b:
            if r < 8:
                return 16
            if r > 248:
                return 231
            return 232 + (r - 8) * 24 // 247
        ri = r * 5 // 255
        gi = g * 5 // 255
        bi = b * 5 // 255
        return 16 + 36 * ri + 6 * gi + bi

    def _render_blocks(self, path: str, x: int, y: int,
                       cols: int, rows: int) -> str:
        # One character cell shows two vertical pixels via the upper half block:
        # foreground = top pixel, background = bottom pixel.
        img    = Image.open(path).convert("RGB").resize((cols, rows * 2))
        px     = img.load()
        is_256 = self.backend == "blocks256"
        out    = []
        for r in range(rows):
            line = [self._cup(x, y + r)]
            for c in range(cols):
                top = px[c, r * 2]
                bot = px[c, r * 2 + 1]
                if is_256:
                    line.append(
                        f"\x1b[38;5;{self._rgb_to_256(*top)}m"
                        f"\x1b[48;5;{self._rgb_to_256(*bot)}m▀")
                else:
                    line.append(
                        f"\x1b[38;2;{top[0]};{top[1]};{top[2]};"
                        f"48;2;{bot[0]};{bot[1]};{bot[2]}m▀")
            line.append("\x1b[0m")
            out.append("".join(line))
        return "".join(out)

    def _png_bytes(self, path: str) -> bytes:
        img = Image.open(path).convert("RGBA")
        img.thumbnail((512, 512))   # cap payload size
        buf = io.BytesIO()
        img.save(buf, "PNG")
        return buf.getvalue()

    def _emit_kitty(self, path: str, x: int, y: int, cols: int, rows: int):
        b64 = base64.b64encode(self._png_bytes(path)).decode("ascii")
        self._emit(self._cup(x, y))
        chunks = [b64[i:i + self.KITTY_CHUNK]
                  for i in range(0, len(b64), self.KITTY_CHUNK)] or [""]
        for i, chunk in enumerate(chunks):
            last = i == len(chunks) - 1
            if i == 0:
                ctrl = f"a=T,f=100,c={cols},r={rows},m={0 if last else 1}"
            else:
                ctrl = f"m={0 if last else 1}"
            self._emit(f"\x1b_G{ctrl};{chunk}\x1b\\")

    def _clear_kitty(self):
        self._emit("\x1b_Ga=d\x1b\\")   # delete all images

    def _emit_iterm2(self, path: str, x: int, y: int, cols: int, rows: int):
        b64 = base64.b64encode(self._png_bytes(path)).decode("ascii")
        self._emit(self._cup(x, y))
        self._emit(
            f"\x1b]1337;File=inline=1;width={cols};height={rows};"
            f"preserveAspectRatio=1:{b64}\x07")

    def _clear_region(self, x: int, y: int, cols: int, rows: int):
        blank = " " * cols
        for r in range(rows):
            self._emit(f"{self._cup(x, y + r)}\x1b[0m{blank}")


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

        self.show_header = True   # status bar (MPD/LRC/offset)
        self.show_player = True   # now-playing block (title/artist/progress)
        self.show_cover  = not getattr(args, "no_cover", False)  # album art

        self.cover       = CoverRenderer(getattr(args, "cover_protocol", "auto"))
        self.cover_path: Optional[str] = None
        self._cover_dirty = False   # force a cover redraw on next frame
        self.cover_fullscreen = False   # cover blown up to fill the terminal

        self.fetch_lock = threading.Lock()
        self.fetch_state: Optional[str] = None   # "fetching" | "found" | "not_found" | "error"
        self.fetch_attempted: set[str] = set()
        self.fetch_thread: Optional[threading.Thread] = None

        curses.curs_set(0)
        stdscr.nodelay(True)
        stdscr.timeout(100)
        # Mouse: click a lyric line (or the progress bar) to seek playback.
        curses.mousemask(curses.BUTTON1_PRESSED | curses.BUTTON1_CLICKED)
        # Rebuilt every frame so a click can be mapped back to a seek target:
        self._lyric_rows: dict[int, float] = {}          # screen row → lyric time (s)
        self._prog_bar: Optional[tuple] = None           # (row, x0, width, duration)
        self._cover_rect: Optional[tuple] = None         # (x, y, cols, rows) on screen
        self._init_colors()

    # ── Colors ───────────────────────────────────────────────────────────────

    def _init_colors(self):
        curses.start_color()
        curses.use_default_colors()
        # Lyrics use a monochrome, faintly warm (beige) white gradient rather
        # than hues. When the terminal can redefine palette entries we set exact
        # warm-white RGB triples (r,g,b on a 0..1000 scale; G≈0.95·R, B≈0.80·R
        # gives the beige tint while keeping luminosity ≈ R). Otherwise we fall
        # back to the neutral xterm grayscale ramp, then the base palette.
        LYRIC_ACTIVE, LYRIC_PAST, LYRIC_UPCOMING = 16, 17, 18  # custom RGB slots
        if curses.can_change_color() and curses.COLORS > LYRIC_UPCOMING:
            curses.init_color(LYRIC_ACTIVE,   1000, 950, 800)  # current  — warm white
            curses.init_color(LYRIC_PAST,      933, 886, 746)  # sung     — near-white beige
            curses.init_color(LYRIC_UPCOMING,  737, 700, 590)  # upcoming — light warm gray
            active_fg, past_fg, upcoming_fg = LYRIC_ACTIVE, LYRIC_PAST, LYRIC_UPCOMING
        elif curses.COLORS >= 256:
            active_fg, past_fg, upcoming_fg = 231, 255, 250  # neutral grayscale fallback
        else:
            active_fg, past_fg, upcoming_fg = curses.COLOR_WHITE, curses.COLOR_WHITE, 7
        # (foreground, background)
        curses.init_pair(1, active_fg,            -1)  # active   — brightest, pure white
        curses.init_pair(2, past_fg,              -1)  # past     — already sung, near-white
        curses.init_pair(3, upcoming_fg,          -1)  # upcoming — light gray
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
            elif key in (ord("h"), ord("H")):
                self.show_header = not self.show_header
                self.stdscr.clear()
                self._reset_cover()
            elif key in (ord("i"), ord("I")):
                self.show_player = not self.show_player
                self.stdscr.clear()
                self._reset_cover()
            elif key in (ord("c"), ord("C")):
                self.show_cover = not self.show_cover
                self.stdscr.clear()
                self._reset_cover()
            elif key in (ord("f"), ord("F")):
                with self.fetch_lock:
                    self.fetch_attempted.clear()
                    self.fetch_state = None
                self.last_song = None
            elif key == curses.KEY_MOUSE:
                self._handle_mouse()
            elif key == curses.KEY_RESIZE:
                self.stdscr.clear()
                self._reset_cover()

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

    def _reset_cover(self):
        """After a full screen clear (resize/toggle) the cover must be wiped
        and redrawn from scratch on the next frame."""
        self.cover.hide()
        self._cover_dirty = True

    def _toggle_cover_fullscreen(self):
        """Switch between the normal layout and the blown-up cover. The layout
        changes wholesale, so wipe the screen and force a fresh cover redraw."""
        self.cover_fullscreen = not self.cover_fullscreen
        self.stdscr.clear()
        self._reset_cover()

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

    def _handle_mouse(self):
        try:
            _id, mx, my, _z, bstate = curses.getmouse()
        except curses.error:
            return
        if not (bstate & (curses.BUTTON1_PRESSED | curses.BUTTON1_CLICKED)):
            return
        # Fullscreen cover: any click returns to the normal layout.
        if self.cover_fullscreen:
            self._toggle_cover_fullscreen()
            return
        # Click on the album cover → blow it up to fill the terminal.
        if self._cover_rect:
            cx, cy, cw, ch = self._cover_rect
            if cx <= mx < cx + cw and cy <= my < cy + ch:
                self._toggle_cover_fullscreen()
                return
        # Progress bar → seek to the clicked fraction of the track.
        if self._prog_bar:
            brow, x0, width, dur = self._prog_bar
            if my == brow and x0 <= mx < x0 + width and dur > 0:
                frac = (mx - x0 + 0.5) / width
                self._seek_to_time(max(0.0, min(1.0, frac)) * dur)
                return
        # Lyric line → seek so that line becomes the current one. Undo the
        # display offset, since the active line is picked on elapsed + offset.
        t = self._lyric_rows.get(my)
        if t is not None:
            self._seek_to_time(max(0.0, t - self.offset / 1000.0))

    def _seek_to_time(self, seconds: float):
        if not self.client:
            return
        try:
            self.client.seekcur(seconds)
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
        self.cover_path = find_cover(song, self.music_dir)
        self._cover_dirty = True
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
        # Rebuilt this frame; consumed by _handle_mouse on the next click.
        self._lyric_rows = {}
        self._prog_bar = None
        self._cover_rect = None

        # Fullscreen cover takes over the whole screen (click to return).
        if self.cover_fullscreen:
            if self.show_cover and self.cover.available() and self.cover_path:
                self._draw_cover_fullscreen(h, w)
                return
            # The track has no usable artwork — drop back to the normal layout.
            self.cover_fullscreen = False
            self.stdscr.clear()
            self.cover.hide()

        elapsed_raw = float(status.get("elapsed", 0))
        duration    = float(status.get("duration") or song.get("time") or 0) or 1

        # — Album cover geometry ————————————————————————————————
        # The cover sits to the LEFT of the now-playing block, so the top
        # block (header + player) is shifted right by `left` columns.
        cover_on = (self.show_cover and self.show_player
                    and self.cover.available() and bool(self.cover_path))
        cover_rows = cover_cols = 0
        if cover_on:
            cover_rows = min(8, max(4, h // 3))
            cover_cols = cover_rows * 2          # square (cells are ~1:2)
            if w < cover_cols + 24 or h < cover_rows + 4:
                cover_on = False
                cover_rows = cover_cols = 0
        left = cover_cols + 2 if cover_on else 0

        # — Status bar ——————————————————————————————————————————
        if self.show_header:
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
            self._addstr(row, left, status_line[:w - left], curses.color_pair(4) | curses.A_BOLD)
            row += 1

            # — Separator ———————————————————————————————————————————
            self._hline(row, w, left); row += 1

        # — Player: Title / Artist / Progress bar ————————————————
        if self.show_player:
            avail_w = w - left
            title  = song.get("title")  or Path(song.get("file", "?")).stem
            artist = song.get("artist") or "Unknown artist"
            album  = song.get("album",  "")
            self._addstr(row, left, title[:avail_w],  curses.color_pair(1) | curses.A_BOLD); row += 1
            info = f"{artist}"
            if album:
                info += f" — {album}"
            self._addstr(row, left, info[:avail_w], curses.color_pair(3)); row += 1

            row += 1
            elapsed_str = fmt_time(elapsed_raw)
            pct         = min(1.0, elapsed_raw / duration)
            bar_w       = max(1, avail_w - 18)
            filled      = int(bar_w * pct)
            bar = "█" * filled + "░" * (bar_w - filled)
            prog_line   = f" {elapsed_str} [{bar}] {fmt_time(duration)}"
            self._addstr(row, left, prog_line[:avail_w], curses.color_pair(7))
            # Remember the bar's on-screen span so a click can seek into it.
            # prog_line = " <elapsed> [<bar>] …" → bar starts len(elapsed)+3 in.
            self._prog_bar = (row, left + len(elapsed_str) + 3, bar_w, duration)
            row += 1

            # — Separator (full width, below the cover if any) ——————————
            row = max(row, cover_rows)
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
                self._lyric_rows[lrow] = self.lyrics[abs_i].time
                if abs_i == active:
                    attr = curses.color_pair(1) | curses.A_BOLD
                    if is_first:
                        self._addstr(lrow, 0, "▶ ", curses.color_pair(4) | curses.A_BOLD)
                elif abs_i < active:
                    attr = curses.color_pair(2)  # already sung → cyan
                else:
                    attr = curses.color_pair(3)  # upcoming → light gray
                indent_x = 4 if abs_i == active else 2
                self._addstr(lrow, indent_x, text[: w - indent_x], attr)

        # — Help at the bottom —————————————————————————————————
        help_row = h - 1
        help_str = " q:quit  p:play/pause  +/-:offset  s:resync  f:refetch  h:header  i:player  c:cover "
        self._addstr(help_row, 0, help_str[:w], curses.color_pair(3))

        self.stdscr.refresh()

        # — Album cover (raw escapes, on top of the settled screen) ————————
        if cover_on:
            self.cover.show(self.cover_path, 0, 0, cover_cols, cover_rows,
                            force=self._cover_dirty)
            self._cover_dirty = False
            self._cover_rect = (0, 0, cover_cols, cover_rows)  # clickable to zoom
        else:
            self.cover.hide()

    def _draw_cover_fullscreen(self, h: int, w: int):
        """Render the cover as a large centered square filling the terminal,
        with a one-line hint at the bottom. A click anywhere returns."""
        # Square in cells: width ≈ 2×height (cells are roughly 1:2). Leave the
        # last row for the hint.
        fs_rows = min(h - 1, max(1, w // 2))
        fs_cols = fs_rows * 2
        fs_x    = max(0, (w - fs_cols) // 2)
        fs_y    = max(0, (h - 1 - fs_rows) // 2)

        hint = "click anywhere to return"
        self._addstr(h - 1, max(0, (w - len(hint)) // 2), hint[:w], curses.color_pair(3))
        self.stdscr.refresh()

        self.cover.show(self.cover_path, fs_x, fs_y, fs_cols, fs_rows,
                        force=self._cover_dirty)
        self._cover_dirty = False
        self._cover_rect = (fs_x, fs_y, fs_cols, fs_rows)

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

    def _hline(self, y, w, x0=0):
        try:
            self.stdscr.addstr(y, x0, "─" * max(0, w - x0))
        except curses.error:
            pass


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    p = argparse.ArgumentParser(
        prog="karaokay",
        description=f"{BANNER}\nKaraoke CLI synchronized with MPD",
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
    args = p.parse_args()

    # Default folder
    if not args.lyrics_dir:
        args.lyrics_dir = [os.path.expanduser("~/.lyrics")]

    try:
        curses.wrapper(lambda s: KaraokeUI(s, args).run())
    except KeyboardInterrupt:
        pass  # Ctrl+C: curses.wrapper has already restored the terminal


if __name__ == "__main__":
    main()
