"""The curses interface: now-playing view, lyrics, and the fetch panel."""

import os
import re
import time
import curses
import textwrap
import threading
from pathlib import Path
from typing import Optional

from .mpd import make_client, safe_status, safe_currentsong, MPDClient
from .lrc import LyricLine, parse_lrc, find_lrc, resolve_write_path
from .cover import CoverRenderer, find_cover
from .fetch import (FetchSession, LyricCandidate, simple_search,
                    SYNCEDLYRICS_AVAILABLE, REQUESTS_AVAILABLE)
from .util import fmt_time


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

        # Automatic autofetch (background, silent) — runs when no local .lrc.
        self.fetch_lock = threading.Lock()
        self.fetch_state: Optional[str] = None   # "fetching" | "found" | "not_found" | "error"
        self.fetch_attempted: set[str] = set()
        self.fetch_thread: Optional[threading.Thread] = None

        # Interactive fetch panel (the `f` key).
        self.fetch_panel = False
        self.fetch_session: Optional[FetchSession] = None
        self._panel_song: dict = {}
        self.panel_sel   = 0
        self.panel_scroll = 0
        self.preview_cand: Optional[LyricCandidate] = None
        self.panel_msg: Optional[str] = None      # transient status after a save
        self._panel_rows: dict[int, int] = {}     # screen row → candidate index

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

            # The fetch panel captures navigation keys; anything it does not
            # consume (playback +/-/p, resize) falls through to the globals.
            if self.fetch_panel and self._handle_panel_key(key):
                pass
            elif key in (ord("q"), ord("Q"), 27):
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
                self._open_fetch_panel()
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
        # Fetch panel: a click selects (and previews) the candidate row.
        if self.fetch_panel:
            idx = self._panel_rows.get(my)
            if idx is not None:
                self.panel_sel = idx
                self._preview_selected()
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

    # ── Interactive fetch panel ─────────────────────────────────────────────

    def _open_fetch_panel(self):
        """Open the panel and kick off a multi-source search for the current
        track."""
        song = safe_currentsong(self.client) if self.client else {}
        title  = (song.get("title") or "").strip() or Path(song.get("file", "")).stem
        artist = (song.get("artist") or "").strip()
        query  = f"{title} {artist}".strip()
        if not query:
            self.panel_msg = "no track metadata to search with"
            return
        self._panel_song   = song
        self.fetch_session = FetchSession(query)
        self.fetch_session.start()
        self.fetch_panel   = True
        self.panel_sel     = 0
        self.panel_scroll  = 0
        self.preview_cand  = None
        self.panel_msg     = None
        self.stdscr.clear()
        self._reset_cover()

    def _close_fetch_panel(self):
        self.fetch_panel  = False
        self.fetch_session = None
        self.preview_cand = None
        self._panel_rows  = {}
        self.stdscr.clear()
        self._reset_cover()

    def _panel_candidates(self) -> list[LyricCandidate]:
        if not self.fetch_session:
            return []
        cands, _status, _done = self.fetch_session.snapshot()
        return cands

    def _preview_selected(self):
        cands = self._panel_candidates()
        if 0 <= self.panel_sel < len(cands):
            self.preview_cand = cands[self.panel_sel]

    def _handle_panel_key(self, key) -> bool:
        """Return True if the key was consumed by the panel."""
        if key in (ord("f"), ord("F"), 27, ord("q"), ord("Q")):
            self._close_fetch_panel()
            return True
        if key in (curses.KEY_UP, ord("k")):
            self._move_panel(-1)
            return True
        if key in (curses.KEY_DOWN, ord("j")):
            self._move_panel(1)
            return True
        if key in (curses.KEY_ENTER, 10, 13, ord(" ")):
            self._preview_selected()
            return True
        if key in (ord("w"), ord("W")):
            self._save_selected()
            return True
        if key == curses.KEY_MOUSE:
            self._handle_mouse()
            return True
        # Let +/-/p (and resize) fall through to the global handler.
        return False

    def _move_panel(self, delta: int):
        n = len(self._panel_candidates())
        if n == 0:
            return
        self.panel_sel = max(0, min(n - 1, self.panel_sel + delta))

    def _save_selected(self):
        cands = self._panel_candidates()
        if not (0 <= self.panel_sel < len(cands)):
            return
        cand = cands[self.panel_sel]
        path = resolve_write_path(self._panel_song, self.music_dir,
                                  self.args.lyrics_dir)
        if not path:
            self.panel_msg = "no writable location for the .lrc"
            return
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(cand.text)
        except OSError as e:
            self.panel_msg = f"write failed: {e}"
            return
        self.panel_msg = f"saved [{cand.provider}] → {os.path.basename(path)}"
        # Force a reload so the freshly written file is picked up immediately,
        # and clear any earlier autofetch failure for this track.
        self.last_song = None
        with self.fetch_lock:
            self.fetch_state = None
        self._close_fetch_panel()

    # ── Lyrics auto-fetch (silent, background) ────────────────────────────────

    def _start_autofetch(self, song: dict):
        # Autofetch works through Lrclib (requests) and/or the syncedlyrics
        # providers; only bail if neither source is available.
        if not (REQUESTS_AVAILABLE or SYNCEDLYRICS_AVAILABLE):
            with self.fetch_lock:
                self.fetch_state = "no_module"
            return
        if self.args.no_autofetch:
            with self.fetch_lock:
                self.fetch_state = "disabled"
            return
        title   = (song.get("title") or "").strip()
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

        target_path = resolve_write_path(song, self.music_dir, self.args.lyrics_dir)
        if not target_path:
            with self.fetch_lock:
                self.fetch_state = "error"
            return

        artist = (song.get("artist") or "").strip()
        query  = f"{title} {artist}".strip()

        def worker():
            try:
                result = simple_search(query)
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
        self._panel_rows = {}

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

        elapsed_adj = elapsed_raw + self.offset / 1000.0

        # — Body: either the fetch panel or the lyrics —————————————
        if self.fetch_panel:
            self._draw_panel(row, h, w, elapsed_adj)
            help_str = (" ↑↓/jk:move  enter:preview  w:save  +/-:offset "
                        " f/esc:close ")
        else:
            self._draw_lyrics(row, h, w, elapsed_adj)
            help_str = (" q:quit  p:play/pause  +/-:offset  s:resync  f:fetch "
                        " h:header  i:player  c:cover ")

        # — Help at the bottom —————————————————————————————————
        self._addstr(h - 1, 0, help_str[:w], curses.color_pair(3))

        self.stdscr.refresh()

        # — Album cover (raw escapes, on top of the settled screen) ————————
        if cover_on:
            self.cover.show(self.cover_path, 0, 0, cover_cols, cover_rows,
                            force=self._cover_dirty)
            self._cover_dirty = False
            self._cover_rect = (0, 0, cover_cols, cover_rows)  # clickable to zoom
        else:
            self.cover.hide()

    # ── Lyrics area ───────────────────────────────────────────────────────────

    def _no_lyrics_msg(self) -> str:
        """The message shown in the lyrics area when the track has no .lrc.

        Reflects the automatic autofetch state: while it runs, once it fails,
        and — when it is disabled/unavailable — the plain "press f" hint.
        """
        with self.fetch_lock:
            fs = self.fetch_state
        if fs == "fetching":
            return "Searching lyrics online…  (press f to pick one yourself)"
        if fs == "not_found":
            return ("Autofetch found no synchronized lyrics — "
                    "press f to search for them yourself")
        if fs == "error":
            return "Autofetch failed — press f to search for lyrics yourself"
        # disabled / no_module / no_meta / none / cleared: the plain hint.
        return "No lyrics — press f to fetch, or drop a .lrc next to the audio file"

    def _draw_lyrics(self, row: int, h: int, w: int, elapsed_adj: float):
        if not self.lyrics:
            self._addstr(row, 2, self._no_lyrics_msg()[:w - 2],
                         curses.color_pair(5))
            return
        avail = max(1, h - row - 2)  # available lines (- help at the bottom)
        self._draw_lyric_lines(self.lyrics, row, avail, h, w, elapsed_adj,
                               clickable=True)

    def _draw_lyric_lines(self, lines: list[LyricLine], top_row: int, avail: int,
                          h: int, w: int, elapsed_adj: float, clickable: bool):
        """Render timed lyrics in a scrolling window centered on the active line.

        Shared by the main lyrics view and the fetch-panel preview.
        """
        active = -1
        for i in range(len(lines) - 1, -1, -1):
            if elapsed_adj >= lines[i].time:
                active = i
                break

        # Expand each line into as many visual rows as needed to handle
        # wrapping for phrases wider than the terminal.
        visual_rows: list[tuple[int, bool, str]] = []
        active_visual_start: Optional[int] = None
        for idx, lyr in enumerate(lines):
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
            lrow = top_row + i
            if lrow >= top_row + avail or lrow >= h - 1:
                break
            if clickable:
                self._lyric_rows[lrow] = lines[abs_i].time
            if abs_i == active:
                attr = curses.color_pair(1) | curses.A_BOLD
                if is_first:
                    self._addstr(lrow, 0, "▶ ", curses.color_pair(4) | curses.A_BOLD)
            elif abs_i < active:
                attr = curses.color_pair(2)  # already sung
            else:
                attr = curses.color_pair(3)  # upcoming
            indent_x = 4 if abs_i == active else 2
            self._addstr(lrow, indent_x, text[: w - indent_x], attr)

    # ── Fetch panel area ──────────────────────────────────────────────────────

    def _draw_panel(self, row: int, h: int, w: int, elapsed_adj: float):
        cands, status, done = (self.fetch_session.snapshot()
                               if self.fetch_session else ([], {}, True))
        if self.panel_sel >= len(cands):
            self.panel_sel = max(0, len(cands) - 1)

        # — Title line: query + per-provider progress ————————————
        prov_bits = []
        for name in ("Lrclib", "Musixmatch", "NetEase", "Megalobiz", "Genius"):
            st = status.get(name)
            if st == "searching":
                prov_bits.append(f"{name}…")
            elif st in ("error", "no_requests", "no_module"):
                prov_bits.append(f"{name}✗")
        head = f"Fetch lyrics: {self.fetch_session.query if self.fetch_session else ''}"
        self._addstr(row, 2, head[:w - 2], curses.color_pair(4) | curses.A_BOLD)
        row += 1
        sub = f"{len(cands)} candidate(s)"
        if not done:
            sub += "  · searching " + " ".join(prov_bits) if prov_bits else "  · searching…"
        elif self.panel_msg:
            sub = self.panel_msg
        self._addstr(row, 2, sub[:w - 2], curses.color_pair(3))
        row += 1
        self._hline(row, w); row += 1

        total = max(1, h - row - 2)
        # Split the area between the candidate list and the live preview.
        if self.preview_cand is not None and total >= 8:
            list_h    = max(3, total // 2)
            preview_h = total - list_h - 1
        else:
            list_h    = total
            preview_h = 0

        # — Candidate list (scrollable) —————————————————————————
        if not cands:
            note = "searching…" if not done else "no lyrics found on any provider"
            self._addstr(row, 4, note[:w - 4], curses.color_pair(5))
        else:
            # Keep the selection within the visible window.
            if self.panel_sel < self.panel_scroll:
                self.panel_scroll = self.panel_sel
            elif self.panel_sel >= self.panel_scroll + list_h:
                self.panel_scroll = self.panel_sel - list_h + 1
            self.panel_scroll = max(0, min(self.panel_scroll,
                                           max(0, len(cands) - list_h)))

            visible = cands[self.panel_scroll:self.panel_scroll + list_h]
            for i, cand in enumerate(visible):
                idx  = self.panel_scroll + i
                lrow = row + i
                self._panel_rows[lrow] = idx
                selected = idx == self.panel_sel
                marker   = "› " if selected else "  "
                kind     = f"synced {len(cand.lines)}" if cand.synced else "plain"
                dur      = f" {fmt_time(cand.duration)}" if cand.duration else ""
                text = f"{marker}[{cand.provider}] {cand.label()}"
                meta = f"{kind}{dur}"
                # Right-align the meta chip when there is room.
                pad  = max(1, w - 2 - len(text) - len(meta))
                line = (text + " " * pad + meta) if pad > 1 else text
                attr = (curses.color_pair(1) | curses.A_BOLD if selected
                        else curses.color_pair(3))
                self._addstr(lrow, 2, line[:w - 2], attr)

        # — Live preview of the selected candidate ————————————————
        if preview_h > 0:
            prow = row + list_h
            self._hline(prow, w); prow += 1
            cand = self.preview_cand
            if cand.synced and cand.lines:
                self._draw_lyric_lines(cand.lines, prow, preview_h, h, w,
                                       elapsed_adj, clickable=False)
            else:
                # Plain text: show the opening lines statically.
                body = cand.text.strip().splitlines()
                for i, txt in enumerate(body[:preview_h]):
                    if prow + i >= h - 1:
                        break
                    self._addstr(prow + i, 4, txt[:w - 4], curses.color_pair(3))

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
