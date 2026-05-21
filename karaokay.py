#!/usr/bin/env python3
"""
mpd-karaoke — Affichage synchronisé des paroles pour MPD
Dépendances : python-mpd2, windows-curses (Windows uniquement)
  pip install python-mpd2
Optionnel (autofetch des paroles synchronisées) :
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

# Coupe les logs bruyants des providers de syncedlyrics (ex: Musixmatch 401)
logging.getLogger("syncedlyrics").setLevel(logging.CRITICAL)
logging.getLogger("root").setLevel(logging.CRITICAL)
logging.basicConfig(level=logging.CRITICAL)

try:
    from mpd import MPDClient, ConnectionError as MPDConnectionError
except ImportError:
    print("Erreur : python-mpd2 requis.  pip install python-mpd2")
    sys.exit(1)

try:
    import syncedlyrics
    SYNCEDLYRICS_AVAILABLE = True
except ImportError:
    SYNCEDLYRICS_AVAILABLE = False


# ── Structures ────────────────────────────────────────────────────────────────

@dataclass
class LyricLine:
    time: float      # secondes
    text: str


# ── Parseur LRC ───────────────────────────────────────────────────────────────

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


# ── Recherche de fichier LRC ───────────────────────────────────────────────────

def find_lrc(song: dict, lyrics_dirs: list[str], music_dir: str) -> Optional[str]:
    """
    Cherche <titre>.lrc dans :
      1. Le dossier du fichier audio (si music_dir est défini)
      2. Les dossiers --lyrics-dir passés en argument
    """
    candidates: list[str] = []

    # 1. Même dossier que le fichier audio
    if music_dir and song.get("file"):
        audio_path = Path(music_dir) / song["file"]
        candidates.append(str(audio_path.with_suffix(".lrc")))

    # 2. Dossiers de paroles dédiés
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


# ── Connexion MPD avec reconnexion automatique ────────────────────────────────

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


# ── Formatage du temps ────────────────────────────────────────────────────────

def fmt_time(secs: float) -> str:
    secs = max(0, int(secs))
    return f"{secs // 60}:{secs % 60:02d}"


# ── Interface curses ──────────────────────────────────────────────────────────

class KaraokeUI:
    # Ratio de lignes affichées avant la ligne active (le reste va après).
    # 0.33 = la ligne active se positionne à ~1/3 du haut de la zone paroles.
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

    # ── Couleurs ─────────────────────────────────────────────────────────────

    def _init_colors(self):
        curses.start_color()
        curses.use_default_colors()
        # (foreground, background)
        curses.init_pair(1, curses.COLOR_WHITE,   -1)  # actif
        curses.init_pair(2, curses.COLOR_CYAN,    -1)  # passé
        curses.init_pair(3, 8,                    -1)  # futur (gris)
        curses.init_pair(4, curses.COLOR_GREEN,   -1)  # info
        curses.init_pair(5, curses.COLOR_YELLOW,  -1)  # avertissement
        curses.init_pair(6, curses.COLOR_RED,     -1)  # erreur
        curses.init_pair(7, curses.COLOR_BLUE,    -1)  # barre de progression

    # ── Connexion ─────────────────────────────────────────────────────────────

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
        """Récupère music_directory soit via la commande `config` de MPD
        (socket UNIX local uniquement), soit en parsant les fichiers de
        config MPD standards."""
        # 1. Commande `config` (socket UNIX local + droits admin)
        if self.client:
            try:
                cfg = self.client.config()
                if isinstance(cfg, dict):
                    path = cfg.get("music_directory", "")
                    if path:
                        return os.path.expanduser(path)
            except Exception:
                pass

        # 2. Parsing des fichiers de config MPD courants
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

    # ── Boucle principale ─────────────────────────────────────────────────────

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
                # Resync forcée : recharge les paroles
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
                    self._draw_error("MPD inaccessible — nouvelle tentative dans 3 s…")
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

    # ── Contrôle lecture ──────────────────────────────────────────────────────

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

    # ── Auto-fetch des paroles ────────────────────────────────────────────────

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
                # garde l'état précédent (not_found / error) pour rester informatif
                return
            if self.fetch_thread and self.fetch_thread.is_alive():
                return
            self.fetch_attempted.add(song_id)
            self.fetch_state = "fetching"

        safe = lambda s: re.sub(r'[<>:"/\\|?*]', "_", s).strip()

        # 1. Préférence : à côté du fichier audio si on connaît music_dir
        #    et que le dossier est accessible en écriture.
        target_path: Optional[str] = None
        if self.music_dir and song.get("file"):
            audio_path = Path(self.music_dir) / song["file"]
            audio_dir  = audio_path.parent
            if audio_dir.is_dir() and os.access(audio_dir, os.W_OK):
                target_path = str(audio_path.with_suffix(".lrc"))

        # 2. Fallback : dossier de paroles dédié.
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

    # ── Rechargement des paroles ──────────────────────────────────────────────

    def _maybe_reload_lyrics(self, song: dict):
        song_id = song.get("id") or song.get("file")
        if song_id == self.last_song:
            # Le thread d'autofetch a-t-il déposé un fichier ?
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

    # ── Rendu ─────────────────────────────────────────────────────────────────

    def _draw(self, status: dict, song: dict):
        h, w = self.stdscr.getmaxyx()
        self.stdscr.erase()

        row = 0

        # — Barre de statut ——————————————————————————————————————
        state = status.get("state", "stop")
        state_icon = {"play": "▶", "pause": "⏸", "stop": "■"}.get(state, "?")
        connected  = f"{state_icon} MPD {self.args.host}:{self.args.port}"
        if self.lrc_path:
            lrc_info = f"  LRC: {os.path.basename(self.lrc_path)}"
        else:
            with self.fetch_lock:
                fs = self.fetch_state
            if fs == "fetching":
                lrc_info = "  LRC: recherche en ligne…"
            elif fs == "not_found":
                lrc_info = "  LRC: aucune version synchronisée trouvée"
            elif fs == "error":
                lrc_info = "  LRC: erreur de fetch"
            elif fs == "no_module":
                lrc_info = "  LRC: module 'syncedlyrics' absent (pip install syncedlyrics)"
            elif fs == "disabled":
                lrc_info = "  LRC: autofetch désactivé (--no-autofetch)"
            elif fs == "no_meta":
                lrc_info = "  LRC: métadonnées insuffisantes (pas de titre)"
            else:
                lrc_info = "  LRC: introuvable"
        off_str    = f"  offset: {'+' if self.offset >= 0 else ''}{self.offset}ms"
        status_line = connected + lrc_info + off_str
        self._addstr(row, 0, status_line[:w], curses.color_pair(4) | curses.A_BOLD)
        row += 1

        # — Séparateur ——————————————————————————————————————————
        self._hline(row, w); row += 1

        # — Titre / Artiste ————————————————————————————————————
        title  = song.get("title")  or Path(song.get("file", "?")).stem
        artist = song.get("artist") or "Artiste inconnu"
        album  = song.get("album",  "")
        self._addstr(row, 0, title[:w],  curses.color_pair(1) | curses.A_BOLD); row += 1
        info = f"{artist}"
        if album:
            info += f" — {album}"
        self._addstr(row, 0, info[:w], curses.color_pair(3)); row += 1

        # — Barre de progression ———————————————————————————————
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

        # — Séparateur ——————————————————————————————————————————
        self._hline(row, w); row += 1

        # — Paroles ————————————————————————————————————————————
        elapsed_adj = elapsed_raw + self.offset / 1000.0

        if not self.lyrics:
            msg = "Aucune parole (déposez un .lrc à côté du fichier audio)"
            self._addstr(row, 2, msg[:w-2], curses.color_pair(5))
        else:
            active = -1
            for i in range(len(self.lyrics) - 1, -1, -1):
                if elapsed_adj >= self.lyrics[i].time:
                    active = i
                    break

            avail = max(1, h - row - 2)  # lignes disponibles (- aide en bas)

            # Déploie chaque ligne en autant de lignes visuelles que nécessaire
            # pour gérer le wrap des phrases plus larges que le terminal.
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

        # — Aide en bas ————————————————————————————————————————
        help_row = h - 1
        help_str = " q:quitter  p:pause/lecture  +:offset+50ms  -:offset-50ms  s:resync  f:refetch "
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


# ── Point d'entrée ────────────────────────────────────────────────────────────

def main():
    p = argparse.ArgumentParser(
        description="Karaoké CLI synchronisé avec MPD"
    )
    p.add_argument("--host",       default=os.getenv("MPD_HOST", "localhost"),
                   help="Hôte MPD (défaut: localhost ou $MPD_HOST)")
    p.add_argument("--port",       type=int, default=int(os.getenv("MPD_PORT", 6600)),
                   help="Port MPD (défaut: 6600 ou $MPD_PORT)")
    p.add_argument("--password",   default=os.getenv("MPD_PASSWORD"),
                   help="Mot de passe MPD")
    p.add_argument("--music-dir",  default=os.getenv("MPD_MUSIC_DIR", ""),
                   help="Répertoire racine de la musique (pour trouver les .lrc côte à côte)")
    p.add_argument("--lyrics-dir", action="append", default=[],
                   help="Dossier(s) contenant les fichiers .lrc (répétable)")
    p.add_argument("--offset",     type=int, default=0,
                   help="Décalage initial en ms (positif = avancer les paroles)")
    p.add_argument("--no-autofetch", action="store_true",
                   help="Désactive la récupération automatique des paroles via syncedlyrics")
    args = p.parse_args()

    # Dossier par défaut
    if not args.lyrics_dir:
        args.lyrics_dir = [os.path.expanduser("~/.lyrics")]

    curses.wrapper(lambda s: KaraokeUI(s, args).run())


if __name__ == "__main__":
    main()
