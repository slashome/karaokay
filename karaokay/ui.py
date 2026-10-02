"""The curses interface: now-playing view, lyrics, and the fetch panel."""

import os
import re
import time
import curses
import textwrap
import threading
from pathlib import Path
from typing import Optional

from . import __version__
from .mpd import (make_client, safe_status, safe_currentsong,
                  safe_playlist, MPDClient)
from .lrc import (LyricLine, load_lyrics, plain_lines, fmt_stamp,
                  find_lyrics_file, read_lyrics_file, resolve_write_path)
from .edit import EditSession, session_for
from .cover import CoverRenderer, find_cover
from .fetch import (FetchSession, LyricCandidate, simple_search,
                    SYNCEDLYRICS_AVAILABLE, REQUESTS_AVAILABLE)
from .util import fmt_time, fit, printable


class KaraokeUI:
    # Ratio of lines displayed before the active line (the rest goes after).
    # 0.33 = the active line sits at ~1/3 from the top of the lyrics area.
    BEFORE_RATIO = 0.33
    # Cover view: the artwork grows as large as it can on the left, as long as
    # the lyrics keep at least this many columns on the right.
    COVER_VIEW_MIN_LYRICS_W = 40

    def __init__(self, stdscr, args):
        self.stdscr   = stdscr
        self.args     = args
        self.offset   = args.offset  # ms
        self.music_dir: str            = args.music_dir or ""
        self.lyrics:   list[LyricLine] = []
        # Untimed lyrics (a "plain" candidate saved from the picker): shown as
        # a static, scrollable page since there is nothing to follow along.
        self.plain:    list[str]       = []
        self.plain_scroll              = 0
        self.lyrics_path: Optional[str]   = None
        self.last_song: Optional[str]  = None
        self.client:   Optional[MPDClient] = None

        self.show_header = True   # status bar (MPD/LRC/offset)
        self.show_player = True   # now-playing block (title/artist/progress)
        self.show_cover  = not getattr(args, "no_cover", False)  # album art

        self.cover       = CoverRenderer(getattr(args, "cover_protocol", "auto"))
        self.cover_path: Optional[str] = None
        self._cover_dirty = False   # force a cover redraw on next frame
        self.cover_fullscreen = False   # cover view: big artwork left, lyrics right

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
        # Timing editor (the `e` key) and the full-screen help (`h`).
        self.edit: Optional[EditSession] = None
        self.edit_confirm = False                  # leaving with unsaved edits
        self._edit_rows: dict[int, int] = {}       # screen row → entry index
        self.help_overlay = False
        self.song: dict = {}                       # last song seen by the loop

        # Playlist pane (the `l` key): the MPD queue on the right-hand side.
        self.playlist_pane = False
        self.playlist_pct  = getattr(args, "playlist_width", 50)
        self.queue: list[dict] = []
        self.queue_version: Optional[str] = None   # MPD's `playlist` counter
        self.queue_cur     = -1                    # position of the playing song
        self.queue_sel     = 0
        self.queue_scroll  = 0
        self._queue_follow = False                 # jump to the playing song
        self._queue_rows: dict[int, int] = {}      # screen row → queue index
        self._queue_x0: Optional[int] = None       # pane's left column

        self.panel_msg: Optional[str] = None      # transient status after a save
        self.msg: Optional[str] = None            # toast shown in the main view
        self.msg_until = 0.0                      # ... until this timestamp
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

            # Overlays and modes capture keys in order of focus; anything
            # they do not consume (playback +/-/p, resize) falls through to
            # the globals below.
            if self.help_overlay and key != -1:
                self._toggle_help()          # any key closes the cheat sheet
            elif self.fetch_panel and self._handle_panel_key(key):
                pass
            elif self.edit is not None and self._handle_edit_key(key):
                pass
            elif self.playlist_pane and self._handle_playlist_key(key):
                pass
            elif key in (ord("q"), ord("Q"), 27):
                break
            elif key == ord("+"):
                self.offset += 50
            elif key == ord("-"):
                self.offset -= 50
            elif key in (curses.KEY_UP, ord("k")):
                self._scroll_plain(-1)
            elif key in (curses.KEY_DOWN, ord("j")):
                self._scroll_plain(1)
            elif key == curses.KEY_PPAGE:
                self._scroll_plain(-10)
            elif key == curses.KEY_NPAGE:
                self._scroll_plain(10)
            elif key == ord("l"):
                self._toggle_playlist()
            elif key in (ord("s"), ord("S")):
                # Forced resync: reload lyrics
                self.last_song = None
            elif key in (ord("p"), ord("P")):
                self._toggle_pause()
            elif key in (ord("h"), ord("?")):
                self._toggle_help()
            elif key == ord("H"):
                self.show_header = not self.show_header
                self.stdscr.clear()
                self._reset_cover()
            elif key in (ord("e"), ord("E")):
                self._toggle_edit()
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

            self.song = song
            self._maybe_reload_lyrics(song)
            self._refresh_queue(status)
            self._draw(status, song)

    def _scroll_plain(self, delta: int):
        """Scroll untimed lyrics; a no-op when the view follows timings."""
        if self.lyrics or not self.plain:
            return
        self.plain_scroll = max(0, self.plain_scroll + delta)

    def _toast(self, msg: str, seconds: float = 4.0):
        """Show `msg` in place of the help line for a few seconds."""
        self.msg = msg
        self.msg_until = time.time() + seconds

    def _reset_cover(self):
        """After a full screen clear (resize/toggle) the cover must be wiped
        and redrawn from scratch on the next frame."""
        self.cover.hide()
        self._cover_dirty = True

    def _toggle_cover_fullscreen(self):
        """Switch between the normal layout and the cover view. The layout
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
        # Cover view: a click on the artwork returns to the normal layout;
        # anywhere else falls through (lyrics and playlist stay clickable).
        if self.cover_fullscreen and self._cover_rect:
            cx, cy, cw, ch = self._cover_rect
            if cx <= mx < cx + cw and cy <= my < cy + ch:
                self._toggle_cover_fullscreen()
                return
        # Playlist pane: a click selects a row, a click on the selected row
        # plays it (the pane is tested first — it overlays the lyrics rows).
        if self.playlist_pane and self._queue_x0 is not None and mx >= self._queue_x0:
            idx = self._queue_rows.get(my)
            if idx is not None:
                if idx == self.queue_sel:
                    self._play_selected()
                else:
                    self.queue_sel = idx
            return
        # Editor: a click stamps the clicked line with the current position.
        if self.edit is not None:
            if not self.edit_confirm:
                idx = self._edit_rows.get(my)
                if idx is not None:
                    self.edit.sel = idx
                    self._stamp_selected(advance=False)
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

    # ── Timing editor ─────────────────────────────────────────────────────────

    def _playback_time(self) -> float:
        """Playback position right now, in lyric time (display offset applied).

        Asked of MPD at the moment of the keystroke rather than reused from the
        last frame: a stamp taken up to 100 ms late is a stamp worth redoing.
        """
        if not self.client:
            return 0.0
        try:
            elapsed = float(safe_status(self.client).get("elapsed", 0))
        except (TypeError, ValueError):
            return 0.0
        return max(0.0, elapsed + self.offset / 1000.0)

    def _lyrics_source(self) -> Optional[str]:
        """Raw content of the current lyrics file, .lrc or .txt alike."""
        if not self.lyrics_path:
            return None
        return read_lyrics_file(self.lyrics_path)

    def _toggle_edit(self):
        if self.edit is not None:
            # Leaving: unsaved work gets a confirmation, an untouched session
            # just closes.
            if self.edit.changes:
                self.edit_confirm = True
            else:
                self._close_edit()
            return

        session = session_for(self._lyrics_source())
        if session is None:
            self._toast("nothing to edit — press f to fetch lyrics, "
                        "or drop a .txt next to the audio file")
            return
        # Start on the line playing right now, the one most likely to be wrong.
        session.sel = max(0, session.active_index(self._playback_time()))
        self.edit = session
        self.edit_confirm = False
        if self.fetch_panel:
            self._close_fetch_panel()
        self.stdscr.clear()
        self._reset_cover()

    def _close_edit(self):
        self.edit = None
        self.edit_confirm = False
        self._edit_rows = {}
        self.stdscr.clear()
        self._reset_cover()

    def _stamp_selected(self, advance: bool):
        """Give the selected line the current playback position."""
        if not self.edit:
            return
        self.edit.set_time(self.edit.sel, self._playback_time())
        if advance:
            self.edit.move(1)

    def _save_edit(self):
        if not self.edit:
            return
        # An .lrc being retimed is written back where it was found; plain text
        # (or nothing) falls back to the usual destination, so a .txt is never
        # overwritten by its own synced version.
        if self.lyrics_path and self.lyrics_path.lower().endswith(".lrc"):
            path = self.lyrics_path
        else:
            path = resolve_write_path(self.song, self.music_dir,
                                      self.args.lyrics_dir)
        if not path:
            self._toast("no writable location for the .lrc")
            return
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(self.edit.to_lrc())
        except OSError as e:
            self._toast(f"write failed: {e}")
            return
        count = self.edit.changes
        self.edit.mark_saved()
        self._toast(f"saved {count} timing change(s) → {os.path.basename(path)}")
        # Pick the file back up so the main view shows what was just written.
        self.last_song = None
        with self.fetch_lock:
            self.fetch_state = None
        self._close_edit()

    def _handle_edit_key(self, key) -> bool:
        """Return True if the key was consumed by the editor."""
        session = self.edit
        if session is None:
            return False

        # Confirmation prompt: only enter and escape answer it, so a stray key
        # can neither discard the work nor write the file.
        if self.edit_confirm:
            if key in (curses.KEY_ENTER, 10, 13):
                self._save_edit()
            elif key == 27:
                self._toast("edits discarded")
                self._close_edit()
            return True

        if key in (curses.KEY_UP, ord("k")):
            session.move(-1)
            return True
        if key in (curses.KEY_DOWN, ord("j")):
            session.move(1)
            return True
        if key == curses.KEY_PPAGE:
            session.move(-10)
            return True
        if key == curses.KEY_NPAGE:
            session.move(10)
            return True
        if key in (curses.KEY_HOME, ord("g")):
            session.sel = 0
            return True
        if key in (curses.KEY_END, ord("G")):
            session.sel = max(0, len(session.entries) - 1)
            return True
        if key == ord(" "):
            self._stamp_selected(advance=True)
            return True
        if key in (curses.KEY_ENTER, 10, 13):
            # Listen back: jump playback to the selected line.
            entry = session.entries[session.sel] if session.entries else None
            if entry and entry.time is not None:
                self._seek_to_time(max(0.0, entry.time - self.offset / 1000.0))
            return True
        if key in (curses.KEY_LEFT, ord("<"), ord(",")):
            session.nudge(session.sel, -1)
            return True
        if key in (curses.KEY_RIGHT, ord(">"), ord(".")):
            session.nudge(session.sel, 1)
            return True
        if key in (ord("x"), ord("X"), curses.KEY_DC):
            session.clear_time(session.sel)
            return True
        if key in (ord("e"), ord("E"), 27):
            self._toggle_edit()
            return True
        if key in (ord("f"), ord("F")):
            # Fetching would overwrite the very file being retimed.
            self._toast("close the editor first (e) to fetch other lyrics")
            return True
        if key == curses.KEY_MOUSE:
            self._handle_mouse()
            return True
        # p (pause), +/- (offset) and resize stay global.
        return False

    # ── Playlist pane ────────────────────────────────────────────────────────

    def _toggle_playlist(self):
        self.playlist_pane = not self.playlist_pane
        if self.playlist_pane:
            # Open on the song being played rather than at the top. The
            # position is only known once the queue has been read, so the jump
            # is deferred to the refresh below.
            self.queue_version = None            # force a refresh this frame
            self._queue_follow = True
        self.stdscr.clear()
        self._reset_cover()

    def _refresh_queue(self, status: dict):
        """Re-read the queue when MPD says it changed, and only then.

        MPD bumps `playlist` (a version counter) on every queue edit, so the
        pane can stay in sync without polling `playlistinfo` every frame.
        """
        if not self.playlist_pane or not self.client:
            return
        try:
            self.queue_cur = int(status.get("song", -1))
        except (TypeError, ValueError):
            self.queue_cur = -1
        version = status.get("playlist")
        if version is not None and version == self.queue_version:
            return
        self.queue = safe_playlist(self.client)
        self.queue_version = version
        if self._queue_follow and self.queue_cur >= 0:
            self.queue_sel = self.queue_cur
            self._queue_follow = False
        if self.queue_sel >= len(self.queue):
            self.queue_sel = max(0, len(self.queue) - 1)

    def _move_queue(self, delta: int):
        if not self.queue:
            return
        self.queue_sel = max(0, min(len(self.queue) - 1, self.queue_sel + delta))

    def _play_selected(self):
        """Start the highlighted queue entry."""
        if not (self.client and 0 <= self.queue_sel < len(self.queue)):
            return
        entry = self.queue[self.queue_sel]
        try:
            self.client.play(int(entry.get("pos", self.queue_sel)))
        except Exception:
            self.client = None
            return
        # Jump to the new track without waiting for the next poll.
        self.last_song = None

    def _handle_playlist_key(self, key) -> bool:
        """Return True if the key was consumed by the playlist pane."""
        if key in (curses.KEY_UP, ord("k")):
            self._move_queue(-1)
            return True
        if key in (curses.KEY_DOWN, ord("j")):
            self._move_queue(1)
            return True
        if key == curses.KEY_PPAGE:
            self._move_queue(-10)
            return True
        if key == curses.KEY_NPAGE:
            self._move_queue(10)
            return True
        if key in (curses.KEY_HOME, ord("g")):
            self.queue_sel = 0
            return True
        if key in (curses.KEY_END, ord("G")):
            self.queue_sel = max(0, len(self.queue) - 1)
            return True
        if key in (curses.KEY_ENTER, 10, 13):
            self._play_selected()
            return True
        if key == 27:                 # Esc closes the pane instead of quitting
            self._toggle_playlist()
            return True
        return False

    # ── Interactive fetch panel ─────────────────────────────────────────────

    def _open_fetch_panel(self):
        """Open the panel and kick off a multi-source search for the current
        track."""
        song = safe_currentsong(self.client) if self.client else {}
        title  = (song.get("title") or "").strip() or Path(song.get("file", "")).stem
        artist = (song.get("artist") or "").strip()
        query  = f"{title} {artist}".strip()
        if not query:
            self._toast("no track metadata to search with")
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
        kind = f"{len(cand.lines)} synced lines" if cand.synced else "plain text"
        self._toast(f"saved [{cand.provider}] {kind} → {os.path.basename(path)}")
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

    def _load_lrc_for(self, song: dict):
        """Locate and read the track's lyrics file into `lyrics` / `plain`."""
        self.lyrics_path = find_lyrics_file(song, self.args.lyrics_dir, self.music_dir)
        if self.lyrics_path:
            self.lyrics, self.plain = load_lyrics(self.lyrics_path)
        else:
            self.lyrics, self.plain = [], []
        self.plain_scroll = 0

    def _maybe_reload_lyrics(self, song: dict):
        song_id = song.get("id") or song.get("file")
        if song_id == self.last_song:
            # Has the autofetch thread dropped a file?
            with self.fetch_lock:
                ready = (self.fetch_state == "found"
                         and not self.lyrics and not self.plain)
                if ready:
                    self.fetch_state = None
            if ready:
                self._load_lrc_for(song)
            return
        self.last_song = song_id
        self._load_lrc_for(song)
        self.cover_path = find_cover(song, self.music_dir)
        self._cover_dirty = True
        if not self.lyrics_path:
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
        self._edit_rows = {}

        # The cheat sheet takes over the whole screen until a key is pressed.
        if self.help_overlay:
            self._draw_help(h, w)
            self.stdscr.refresh()
            return

        # The cover view only frames the lyrics: the editor and the fetch
        # panel need the normal layout, and so does a track without artwork.
        if self.cover_fullscreen and not (
                self.show_cover and self.cover.available() and self.cover_path
                and self.edit is None and not self.fetch_panel):
            self.cover_fullscreen = False
            self.stdscr.clear()
            self.cover.hide()

        # The playlist pane owns the right-hand columns; everything below
        # draws inside the remaining width, so `w` is narrowed here and the
        # full width kept aside for the pane itself.
        full_w = w
        pane_w = self._pane_width(w) if self.playlist_pane else 0
        w      = full_w - pane_w

        if self.cover_fullscreen:
            self._draw_cover_view(status, h, w, pane_w)
            return

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
            if self.lyrics_path:
                # Plain text is a legitimate source now, so the label follows
                # the file rather than always claiming "LRC".
                tag  = "TXT" if self.lyrics_path.lower().endswith(".txt") else "LRC"
                kind = "  (plain)" if (self.plain and not self.lyrics) else ""
                lrc_info = f"  {tag}: {os.path.basename(self.lyrics_path)}{kind}"
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

        # — Body: the editor, the fetch panel, or the lyrics ——————————
        if self.edit is not None:
            self._draw_edit(row, h, w, elapsed_adj)
            help_str = self._edit_help()
        elif self.fetch_panel:
            self._draw_panel(row, h, w, elapsed_adj)
            help_str = (" ↑↓/jk:move  enter:preview  w:save  +/-:offset "
                        " f/esc:close ")
        else:
            self._draw_lyrics(row, h, w, elapsed_adj)
            if self.playlist_pane:
                # The pane carries its own key hints, so the line is trimmed to
                # what still fits beside it.
                help_str = " q:quit  p:play/pause  f:fetch  e:edit  h:help "
            else:
                help_str = (" q:quit  p:play/pause  +/-:offset  s:resync "
                            " f:fetch  e:edit  l:playlist  h:help ")
                if self.plain and not self.lyrics:
                    help_str = " ↑↓/jk:scroll " + help_str

        # — Help at the bottom, or the transient toast that replaces it ————
        if self.msg and time.time() < self.msg_until:
            self._addstr(h - 1, 0, f" {self.msg} "[:w],
                         curses.color_pair(4) | curses.A_BOLD)
        else:
            self.msg = None
            self._addstr(h - 1, 0, help_str[:w], curses.color_pair(3))

        # — Playlist pane on the right ————————————————————————
        if pane_w:
            self._draw_playlist(w, h, pane_w)

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
        return ("No lyrics — press f to fetch, or drop a .lrc/.txt "
                "next to the audio file")

    def _draw_lyrics(self, row: int, h: int, w: int, elapsed_adj: float,
                     x0: int = 0):
        """Lyrics between columns `x0` and `w` (x0 > 0 in the cover view)."""
        if not self.lyrics:
            if self.plain:
                self._draw_plain_lyrics(row, h, w, x0)
            else:
                self._addstr(row, x0 + 2, self._no_lyrics_msg()[:w - x0 - 2],
                             curses.color_pair(5))
            return
        avail = max(1, h - row - 2)  # available lines (- help at the bottom)
        self._draw_lyric_lines(self.lyrics, row, avail, h, w, elapsed_adj,
                               clickable=True, x0=x0)

    def _draw_plain_lyrics(self, row: int, h: int, w: int, x0: int = 0):
        """Render untimed lyrics as a static, scrollable page.

        Nothing can be followed along here, so the whole text is shown plainly
        with a one-line banner making the lack of timings explicit.
        """
        inner = w - x0 - 2
        self._addstr(row, x0 + 2,
                     "Unsynchronized lyrics — ↑↓/jk to scroll, e to time them"[:inner],
                     curses.color_pair(5))
        row += 1
        avail = max(1, h - row - 2)
        self.plain_scroll = max(0, min(self.plain_scroll,
                                       max(0, len(self.plain) - avail)))
        visible = self.plain[self.plain_scroll:self.plain_scroll + avail]
        for i, text in enumerate(visible):
            self._addstr(row + i, x0 + 2, text[:inner], curses.color_pair(2))
        # Scroll hint when the text runs past the bottom of the screen.
        if len(self.plain) > avail:
            pos = f" {self.plain_scroll + 1}-{self.plain_scroll + len(visible)}/{len(self.plain)} "
            self._addstr(row - 1, max(x0, w - len(pos)), pos[:w - x0],
                         curses.color_pair(3))

    def _draw_lyric_lines(self, lines: list[LyricLine], top_row: int, avail: int,
                          h: int, w: int, elapsed_adj: float, clickable: bool,
                          x0: int = 0):
        """Render timed lyrics in a scrolling window centered on the active line.

        Shared by the main lyrics view, the cover view (drawn from column
        `x0`) and the fetch-panel preview.
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
            body_w   = max(1, w - x0 - 4 - len(time_tag))
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
                    self._addstr(lrow, x0, "▶ ", curses.color_pair(4) | curses.A_BOLD)
            elif abs_i < active:
                attr = curses.color_pair(2)  # already sung
            else:
                attr = curses.color_pair(3)  # upcoming
            indent_x = x0 + (4 if abs_i == active else 2)
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
                # Right-align the meta chip when there is room, keeping one
                # column of margin (the playlist pane may sit right there).
                pad  = max(1, w - 3 - len(text) - len(meta))
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
                body = plain_lines(cand.text)
                for i, txt in enumerate(body[:preview_h]):
                    if prow + i >= h - 1:
                        break
                    self._addstr(prow + i, 4, txt[:w - 4], curses.color_pair(3))

    # ── Timing editor area ────────────────────────────────────────────────────

    NO_STAMP = "--:--.--"

    def _draw_edit(self, row: int, h: int, w: int, elapsed_adj: float):
        """Render the editor: every line with its timestamp, cursor included."""
        session = self.edit
        name = os.path.basename(self.lyrics_path) if self.lyrics_path else "(new)"
        self._addstr(row, 2, f"Edit timings: {name}"[:w - 2],
                     curses.color_pair(4) | curses.A_BOLD)
        row += 1
        sub = f"{session.timed}/{len(session.entries)} lines timed"
        if session.changes:
            sub += f"  ·  {session.changes} unsaved"
        self._addstr(row, 2, sub[:w - 2], curses.color_pair(3))
        row += 1
        self._hline(row, w); row += 1

        avail = max(1, h - row - 2)
        if session.sel < session.scroll:
            session.scroll = session.sel
        elif session.sel >= session.scroll + avail:
            session.scroll = session.sel - avail + 1
        session.scroll = max(0, min(session.scroll,
                                    max(0, len(session.entries) - avail)))

        active  = session.active_index(elapsed_adj)
        visible = session.entries[session.scroll:session.scroll + avail]
        for i, entry in enumerate(visible):
            idx = session.scroll + i
            y   = row + i
            self._edit_rows[y] = idx
            stamp = fmt_stamp(entry.time) if entry.time is not None else self.NO_STAMP
            if idx == active:
                attr = curses.color_pair(1) | curses.A_BOLD   # playing now
            elif entry.time is None:
                attr = curses.color_pair(5)                   # still untimed
            else:
                attr = curses.color_pair(2)
            if idx == session.sel:
                attr |= curses.A_REVERSE
            line = f" [{stamp}] {entry.text}"
            self._addstr(y, 1, line.ljust(w - 2)[:w - 2], attr)

    def _edit_help(self) -> str:
        if self.edit_confirm:
            n = self.edit.changes if self.edit else 0
            return f" Save {n} change(s)?   enter:save   esc:discard "
        return (" space:stamp  ↑↓:line  ←→:nudge ±0.1s  x:clear  "
                "enter:listen  p:pause  e:done ")

    # ── Full-screen help ──────────────────────────────────────────────────────

    HELP_SECTIONS = (
        ("Playback", (
            ("p",          "play / pause"),
            ("+ / -",      "shift lyrics 50 ms later / earlier"),
            ("s",          "resync — reload the lyrics file"),
        )),
        ("Display", (
            ("H",          "status header"),
            ("i",          "now-playing block"),
            ("c",          "album cover"),
            ("h / ?",      "this help"),
            ("q / Esc",    "quit"),
        )),
        ("Lyrics", (
            ("f",          "fetch: pick lyrics from several sites"),
            ("e",          "edit the timings of the current lyrics"),
            ("↑↓ / jk",    "scroll unsynchronized lyrics"),
        )),
        ("Playlist (l)", (
            ("l",          "show / hide the queue"),
            ("↑↓ / jk",    "move the selection"),
            ("g / G",      "first / last entry"),
            ("enter",      "play the selected track"),
        )),
        ("Editor (e)", (
            ("space",      "stamp the line with the current position"),
            ("←→ / <>",    "nudge the line by ±0.1 s"),
            ("x",          "clear the line's timestamp"),
            ("enter",      "listen: seek to the selected line"),
            ("e / Esc",    "leave (asks before dropping edits)"),
        )),
        ("Mouse", (
            ("click lyric","seek to that line"),
            ("click bar",  "seek in the track"),
            ("click cover","cover view: artwork + lyrics"),
            ("in editor",  "stamp the clicked line"),
        )),
    )

    def _toggle_help(self):
        self.help_overlay = not self.help_overlay
        self.stdscr.clear()
        self._reset_cover()

    def _draw_help(self, h: int, w: int):
        """The cheat sheet, in two columns when the terminal is wide enough."""
        self.cover.hide()
        title = f"karaokay {__version__} — keyboard shortcuts"
        self._addstr(0, max(0, (w - len(title)) // 2), title[:w],
                     curses.color_pair(4) | curses.A_BOLD)

        blocks: list[list[tuple[str, str, int]]] = []
        for name, keys in self.HELP_SECTIONS:
            block: list[tuple[str, str, int]] = [(name, "", 4)]
            block += [(k, desc, 0) for k, desc in keys]
            block.append(("", "", 0))
            blocks.append(block)

        two_col = w >= 76
        if two_col:
            half     = (len(blocks) + 1) // 2
            columns  = [sum(blocks[:half], []), sum(blocks[half:], [])]
            col_w    = w // 2
        else:
            columns, col_w = [sum(blocks, [])], w
        # Drop the trailing blank of each column: on a short terminal that one
        # line is the difference between fitting and being cut off.
        columns = [col[:-1] if col and not col[-1][0] else col for col in columns]

        for ci, column in enumerate(columns):
            x = 2 + ci * col_w
            for i, (key, desc, kind) in enumerate(column):
                y = 2 + i
                if y >= h - 1:
                    break
                if kind == 4:                      # section title
                    self._addstr(y, x, key[:col_w - 2],
                                 curses.color_pair(4) | curses.A_BOLD)
                elif key:
                    self._addstr(y, x, key.rjust(11)[:col_w - 2],
                                 curses.color_pair(1) | curses.A_BOLD)
                    self._addstr(y, x + 13, desc[:max(0, col_w - 15)],
                                 curses.color_pair(3))

        hint = "press any key to close"
        self._addstr(h - 1, max(0, (w - len(hint)) // 2), hint[:w],
                     curses.color_pair(3))

    # ── Playlist pane area ────────────────────────────────────────────────────

    # Fixed-width columns; the rest of the room goes to the title.
    TRACK_W = 2
    TIME_W  = 5

    def _pane_width(self, w: int) -> int:
        """Pane width in columns, from the configured percentage.

        Kept wide enough to show something useful and narrow enough to leave
        the lyrics readable; on a very narrow terminal the pane wins, since it
        is only shown on demand.
        """
        want = int(w * self.playlist_pct / 100)
        return max(0, min(w - 8, max(18, want)))

    def _queue_columns(self, inner: int) -> tuple[int, int, int]:
        """(artist, title, album) widths for `inner` usable columns.

        Artist and album are dropped as the pane narrows, so the title — the
        one column that always matters — keeps its room.
        """
        rest = inner - self.TRACK_W - 1 - self.TIME_W - 1
        if rest >= 46:
            # Artist and album scale with the pane, the title keeps the rest.
            artist = min(20, max(12, int(rest * 0.20)))
            album  = min(20, max(10, int(rest * 0.18)))
            return artist, rest - artist - album - 2, album
        if rest >= 30:
            artist = 10
            return artist, rest - artist - 1, 0
        return 0, max(1, rest), 0

    def _queue_row(self, entry: dict, widths: tuple[int, int, int]) -> str:
        """Format one queue entry as a column-aligned row."""
        artist_w, title_w, album_w = widths
        title = (entry.get("title") or "").strip() or Path(entry.get("file", "?")).stem
        # MPD reports `track` as "7" or "7/12" depending on the tagger.
        track = (entry.get("track") or "").split("/")[0].strip()
        track = track.zfill(self.TRACK_W) if track.isdigit() else "  "
        secs  = entry.get("duration") or entry.get("time") or 0
        try:
            dur = fmt_time(float(secs))
        except (TypeError, ValueError):
            dur = ""
        cells = []
        if artist_w:
            cells.append(fit((entry.get("artist") or "").strip(), artist_w))
        cells.append(track[:self.TRACK_W])
        cells.append(fit(title, title_w))
        if album_w:
            cells.append(fit((entry.get("album") or "").strip(), album_w))
        cells.append(dur.rjust(self.TIME_W))
        return " ".join(cells)

    def _queue_header(self, widths: tuple[int, int, int]) -> str:
        artist_w, title_w, album_w = widths
        cells = []
        if artist_w:
            cells.append(fit("Artist", artist_w))
        cells.append("#".ljust(self.TRACK_W))
        cells.append(fit("Title", title_w))
        if album_w:
            cells.append(fit("Album", album_w))
        cells.append("Time".rjust(self.TIME_W))
        return " ".join(cells)

    def _draw_playlist(self, x0: int, h: int, pane_w: int):
        """Render the MPD queue in the right-hand pane.

        Column headers on top, a footer with the position, and — between the
        two — the queue scrolled to keep the selection visible. The playing
        track is marked, the selected one highlighted.
        """
        self._queue_rows = {}
        self._queue_x0   = x0
        # Vertical rule separating the pane from the lyrics.
        for y in range(h):
            self._addstr(y, x0, "│", curses.color_pair(3))
        left   = x0 + 2                  # first content column
        inner  = max(1, pane_w - 3)
        widths = self._queue_columns(inner)

        self._addstr(0, left, self._queue_header(widths)[:inner],
                     curses.color_pair(4) | curses.A_BOLD)

        avail = max(1, h - 2)            # minus the header and the footer
        if not self.queue:
            self._addstr(1, left, "(queue empty)"[:inner], curses.color_pair(5))
            return

        # Keep the selection inside the visible window.
        if self.queue_sel < self.queue_scroll:
            self.queue_scroll = self.queue_sel
        elif self.queue_sel >= self.queue_scroll + avail:
            self.queue_scroll = self.queue_sel - avail + 1
        self.queue_scroll = max(0, min(self.queue_scroll,
                                       max(0, len(self.queue) - avail)))

        visible = self.queue[self.queue_scroll:self.queue_scroll + avail]
        for i, entry in enumerate(visible):
            idx  = self.queue_scroll + i
            y    = 1 + i
            self._queue_rows[y] = idx
            playing  = idx == self.queue_cur
            selected = idx == self.queue_sel
            if playing:
                attr = curses.color_pair(1) | curses.A_BOLD
            else:
                attr = curses.color_pair(2)
            if selected:
                attr |= curses.A_REVERSE
            text = self._queue_row(entry, widths)
            # The row spans the pane so a selected one reads as a full bar.
            span = max(1, pane_w - 1)
            self._addstr(y, x0 + 1, (" " + text).ljust(span)[:span], attr)
            if playing:
                self._addstr(y, x0 + 1, "▶", attr)

        footer = f" {self.queue_sel + 1}/{len(self.queue)}  enter:play  l:close "
        self._addstr(h - 1, left, footer[:inner], curses.color_pair(3))

    def _draw_cover_view(self, status: dict, h: int, w: int, pane_w: int):
        """The cover view: the artwork as large as it fits on the left, the
        lyrics in the remaining columns on the right. The header and the
        now-playing block are hidden. Clicking the artwork returns.

        `w` is the width left of the playlist pane (if any). When the terminal
        is too narrow to keep the lyrics readable, the artwork takes the
        whole width, centered.
        """
        # Square in cells: width ≈ 2×height (cells are roughly 1:2). The cover
        # spans the full height when the width allows it, otherwise it shrinks
        # so the lyrics keep their minimum width.
        cv_rows = min(h, max(0, (w - 2 - self.COVER_VIEW_MIN_LYRICS_W) // 2))
        split   = cv_rows >= 4
        if split:
            cv_cols = cv_rows * 2
            cv_x    = 0
            cv_y    = max(0, (h - cv_rows) // 2)
            x0      = cv_cols + 1          # lyrics start one column past a gap

            elapsed_adj = (float(status.get("elapsed", 0))
                           + self.offset / 1000.0)
            self._draw_lyrics(0, h, w, elapsed_adj, x0=x0)
            help_str = " click cover:back  p:play/pause  h:help "
            if self.msg and time.time() < self.msg_until:
                self._addstr(h - 1, x0, f" {self.msg} "[:w - x0],
                             curses.color_pair(4) | curses.A_BOLD)
            else:
                self.msg = None
                self._addstr(h - 1, x0, help_str[:w - x0], curses.color_pair(3))
        else:
            # Too narrow for a side-by-side layout: the artwork alone, with a
            # one-line hint underneath.
            cv_rows = min(h - 1, max(1, w // 2))
            cv_cols = cv_rows * 2
            cv_x    = max(0, (w - cv_cols) // 2)
            cv_y    = max(0, (h - 1 - cv_rows) // 2)
            hint = "click the cover to return"
            self._addstr(h - 1, max(0, (w - len(hint)) // 2), hint[:w],
                         curses.color_pair(3))

        if pane_w:
            self._draw_playlist(w, h, pane_w)
        self.stdscr.refresh()

        self.cover.show(self.cover_path, cv_x, cv_y, cv_cols, cv_rows,
                        force=self._cover_dirty)
        self._cover_dirty = False
        self._cover_rect = (cv_x, cv_y, cv_cols, cv_rows)

    def _draw_error(self, msg: str):
        h, w = self.stdscr.getmaxyx()
        self.stdscr.erase()
        self._addstr(h // 2, max(0, (w - len(msg)) // 2), msg[:w], curses.color_pair(6) | curses.A_BOLD)
        self.stdscr.refresh()

    def _addstr(self, y, x, text, attr=0):
        try:
            self.stdscr.addstr(y, x, printable(text), attr)
        except (curses.error, ValueError):
            pass

    def _hline(self, y, w, x0=0):
        try:
            self.stdscr.addstr(y, x0, "─" * max(0, w - x0))
        except curses.error:
            pass
