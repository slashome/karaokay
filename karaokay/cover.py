"""Album cover lookup and terminal rendering.

curses cannot draw images, so the cover is emitted with raw terminal escape
sequences in a rectangle that curses is told to leave untouched. A best-effort
cascade picks the nicest backend the terminal supports:

  kitty   — Kitty graphics protocol (Kitty, Ghostty, WezTerm). Overlay image,
            transmitted once per track.
  iterm2  — iTerm2 inline images (iTerm2, WezTerm). Cell-anchored, drawn once
            per track and kept alive by curses' blank-cell persistence.
  blocks  — truecolor half-block glyphs (▀). Works in any 24-bit terminal;
            repainted every frame so it survives curses redraws.
  blocks256 — same half-block trick quantized to the xterm-256 palette, for
            terminals without 24-bit color (e.g. Apple Terminal).

All backends require Pillow (decoding + resizing, mandatory for WebP/JPEG).
"""

import io
import os
import sys
import base64
from pathlib import Path
from typing import Optional

try:
    from PIL import Image
    PIL_AVAILABLE = True
except ImportError:
    PIL_AVAILABLE = False


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


# ── Backend detection ─────────────────────────────────────────────────────────

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
