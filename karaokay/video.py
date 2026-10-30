"""Music video lookup, download and decoding.

The cover view can swap the artwork for the track's music video (the `v` key).
`VideoSession` resolves the clip in a background thread, cheapest source
first:

  local   — a video file next to the audio file (same name, or `video.*`).
  cache   — a clip downloaded earlier, in $XDG_CACHE_HOME/karaokay/videos/.
  youtube — a `yt-dlp` search, downloaded without its audio track in the
            smallest format available: the sound always comes from MPD. Of
            the results, the closest in length to the track wins, still
            covers (square pictures) excepted.

`VideoFrames` then decodes the clip with `ffmpeg` into small raw frames, so the
frame matching MPD's elapsed time is a list lookup.

`yt-dlp` and `ffmpeg` are optional binaries looked up on $PATH, never Python
dependencies: without them the session reports `unavailable` and the cover
stays on screen, the same way a missing Pillow skips the cover.
"""

import os
import json
import shutil
import hashlib
import threading
import subprocess
from pathlib import Path
from typing import Optional


# ── Video lookup ──────────────────────────────────────────────────────────────

VIDEO_EXTS = (".mp4", ".webm", ".mkv")

YTDLP_RESULTS = 5          # search results compared on duration
# Video-only, smallest. A clip is wider than tall; a square picture is a still
# album cover laid over the audio, which tends to match the album track's
# length exactly — so it is refused and the next result tried.
YTDLP_FORMAT  = "worstvideo[height>=144][aspect_ratio>1.1]"
YTDLP_NO_FORMAT = "Requested format is not available"
YTDLP_TIMEOUT = 300        # seconds, for the search and the download each


def find_video(song: dict, music_dir: str) -> Optional[str]:
    """
    Looks for a video next to the audio file: `<track>.{mp4,webm,mkv}` (same
    name as the audio file), then `video.{mp4,webm,mkv}` in the album folder.
    """
    if not (music_dir and song.get("file")):
        return None
    audio     = Path(music_dir) / song["file"]
    album_dir = audio.parent

    # Fast path: exact names.
    for stem in (audio.stem, "video"):
        for ext in VIDEO_EXTS:
            p = album_dir / f"{stem}{ext}"
            if p.is_file():
                return str(p)

    # Case-insensitive fallback (e.g. Video.MP4), same order of preference.
    try:
        entries = [e for e in album_dir.iterdir()
                   if e.is_file() and e.suffix.lower() in VIDEO_EXTS]
    except OSError:
        return None
    for stem in (audio.stem.lower(), "video"):
        for entry in entries:
            if entry.stem.lower() == stem:
                return str(entry)
    return None


def cache_dir() -> Path:
    """The downloaded clips' folder, honouring $XDG_CACHE_HOME."""
    base = os.getenv("XDG_CACHE_HOME") or "~/.cache"
    return Path(os.path.expanduser(base)) / "karaokay" / "videos"


def cache_key(song: dict) -> str:
    """A file-name-safe key derived from the track's MPD path."""
    return hashlib.sha1(song.get("file", "").encode("utf-8")).hexdigest()


def cached_video(song: dict) -> Optional[str]:
    """A clip already downloaded for `song`, or None. Partial downloads
    (`.part`, `.ytdl`) don't count."""
    folder = cache_dir()
    key    = cache_key(song)
    for ext in VIDEO_EXTS:
        p = folder / f"{key}{ext}"
        if p.is_file() and p.stat().st_size > 0:
            return str(p)
    return None


def search_query(song: dict) -> str:
    artist = (song.get("artist") or "").strip()
    title  = (song.get("title") or "").strip()
    return f"{artist} {title} official video".strip()


def song_duration(song: dict) -> Optional[float]:
    """The track's length in seconds, as MPD reports it."""
    for key in ("duration", "time"):
        try:
            return float(song[key])
        except (KeyError, TypeError, ValueError):
            continue
    return None


class VideoSession:
    """Finds (and downloads, if need be) the current track's music video in a
    background thread.

    Thread-safe: the UI polls `snapshot()` every frame. States:

      fetching    — looking up / downloading
      found       — `path` is a playable video file
      not_found   — no local file, no cache, no search result
      error       — yt-dlp failed (network, extraction…)
      unavailable — a required binary is missing (`missing` names it)
    """

    def __init__(self, song: dict, music_dir: str):
        self.song      = dict(song)
        self.music_dir = music_dir
        self.lock      = threading.Lock()
        self.state     = "fetching"
        self.path:    Optional[str] = None
        self.source:  Optional[str] = None     # "local" | "cache" | "youtube"
        self.missing: Optional[str] = None     # binary name when unavailable
        self._cancelled = False
        self._proc:   Optional[subprocess.Popen] = None
        self._thread: Optional[threading.Thread] = None

    # ── Public API ───────────────────────────────────────────────────────────

    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def snapshot(self) -> tuple[str, Optional[str], Optional[str]]:
        """(state, path, missing binary)."""
        with self.lock:
            return self.state, self.path, self.missing

    def cancel(self):
        """Abandon the lookup: a running yt-dlp is killed and its result, if
        any, ignored."""
        with self.lock:
            self._cancelled = True
            proc = self._proc
        if proc and proc.poll() is None:
            try:
                proc.kill()
            except OSError:
                pass

    def wait(self, timeout: Optional[float] = None):
        if self._thread:
            self._thread.join(timeout)

    # ── Worker ────────────────────────────────────────────────────────────────

    def _set(self, state: str, path: Optional[str] = None,
             source: Optional[str] = None, missing: Optional[str] = None):
        with self.lock:
            if self._cancelled:
                return
            self.state, self.path = state, path
            self.source, self.missing = source, missing

    def _run(self):
        try:
            self._resolve()
        except Exception:
            # Whatever happens in the worker must surface as a state, never as
            # an exception in a thread nobody joins.
            self._set("error")

    def _resolve(self):
        # Without ffmpeg nothing can be decoded, whatever the source.
        if not shutil.which("ffmpeg"):
            self._set("unavailable", missing="ffmpeg")
            return

        path = find_video(self.song, self.music_dir)
        if path:
            self._set("found", path, "local")
            return
        path = cached_video(self.song)
        if path:
            self._set("found", path, "cache")
            return

        ytdlp = shutil.which("yt-dlp")
        if not ytdlp:
            self._set("unavailable", missing="yt-dlp")
            return
        if not (self.song.get("title") or "").strip():
            self._set("not_found")
            return

        video_ids = self._search(ytdlp)
        if video_ids is None:
            return                      # state already set
        self._download(ytdlp, video_ids)

    def _call(self, cmd: list[str]) -> Optional[tuple[int, str, str]]:
        """Run `cmd`, killable by cancel(). None when cancelled or timed out."""
        with self.lock:
            if self._cancelled:
                return None
            self._proc = subprocess.Popen(
                cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True)
            proc = self._proc
        try:
            out, err = proc.communicate(timeout=YTDLP_TIMEOUT)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()
            self._set("error")
            return None
        with self.lock:
            self._proc = None
            if self._cancelled:
                return None
        return proc.returncode, out, err

    def _search(self, ytdlp: str) -> Optional[list[str]]:
        """The search results' ids, closest in length to the track first."""
        query = f"ytsearch{YTDLP_RESULTS}:{search_query(self.song)}"
        res = self._call([ytdlp, "--flat-playlist", "--dump-json",
                          "--no-warnings", "--ignore-config", query])
        if res is None:
            return None
        code, out, _ = res
        entries = []
        for line in out.splitlines():
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if entry.get("id"):
                entries.append(entry)
        if not entries:
            self._set("not_found" if code == 0 else "error")
            return None

        # A live take or a cover rarely runs as long as the album track, so the
        # closest duration wins; results without one rank last, in search order.
        target = song_duration(self.song)
        if target:
            def distance(e):
                d = e.get("duration")
                return abs(float(d) - target) if d else float("inf")
            entries.sort(key=distance)   # stable: ties keep the search order
        return [e["id"] for e in entries]

    def _download(self, ytdlp: str, video_ids: list[str]):
        """Download the first result that has a proper video stream."""
        folder = cache_dir()
        try:
            folder.mkdir(parents=True, exist_ok=True)
        except OSError:
            self._set("error")
            return
        template = str(folder / f"{cache_key(self.song)}.%(ext)s")
        failed = False                   # a failure other than "no such format"
        for video_id in video_ids:
            res = self._call([
                ytdlp, "--ignore-config", "--no-playlist", "--no-warnings",
                "--quiet", "--no-progress",
                "-f", YTDLP_FORMAT,
                "-o", template,
                "--", video_id,
            ])
            if res is None:
                return
            path = cached_video(self.song)
            if res[0] == 0 and path:
                self._set("found", path, "youtube")
                return
            if YTDLP_NO_FORMAT not in res[2]:
                failed = True
        self._set("error" if failed else "not_found")


# ── Decoding ──────────────────────────────────────────────────────────────────

# The UI loop ticks every 100 ms, so 10 frames/s is the ceiling; 8 keeps a
# 4-minute clip at 80×80 px around 37 MB.
VIDEO_FPS = 8


class VideoFrames:
    """Decodes a clip into raw rgb24 frames of `width`×`height` pixels, at
    `fps` frames per second, in a background thread.

    `ffmpeg` scales the clip straight to the cover's size (letterboxed, black
    bars) and drops the audio, so the frames are ready to paint. They are kept
    in memory, frame n covering [n/fps, (n+1)/fps): the frame for a playback
    position is a list lookup. States: decoding / ready / error / unavailable.
    """

    def __init__(self, path: str, width: int, height: int, fps: int = VIDEO_FPS):
        self.path   = path
        self.width  = max(1, width)
        self.height = max(1, height)
        self.fps    = fps
        self.lock   = threading.Lock()
        self.frames: list[bytes] = []
        self.state  = "decoding"
        self._cancelled = False
        self._proc:   Optional[subprocess.Popen] = None
        self._thread: Optional[threading.Thread] = None

    @property
    def geometry(self) -> tuple[int, int]:
        return self.width, self.height

    # ── Public API ───────────────────────────────────────────────────────────

    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def snapshot(self) -> tuple[str, int]:
        """(state, number of frames decoded so far)."""
        with self.lock:
            return self.state, len(self.frames)

    def frame_at(self, elapsed: float) -> Optional[tuple[int, bytes]]:
        """(index, rgb24 bytes) of the frame shown `elapsed` seconds into the
        track, or None while that frame is not decoded yet. Past the end of a
        fully decoded clip, the last frame stays."""
        idx = int(max(0.0, elapsed) * self.fps)
        with self.lock:
            if idx < len(self.frames):
                return idx, self.frames[idx]
            if self.state == "ready" and self.frames:
                return len(self.frames) - 1, self.frames[-1]
        return None

    def cancel(self):
        """Stop decoding and drop the frames."""
        with self.lock:
            self._cancelled = True
            self.frames = []
            proc = self._proc
        if proc and proc.poll() is None:
            try:
                proc.kill()
            except OSError:
                pass

    def wait(self, timeout: Optional[float] = None):
        if self._thread:
            self._thread.join(timeout)

    # ── Worker ────────────────────────────────────────────────────────────────

    def _set_state(self, state: str):
        with self.lock:
            if not self._cancelled:
                self.state = state

    def _run(self):
        try:
            self._decode()
        except Exception:
            self._set_state("error")

    def _decode(self):
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            self._set_state("unavailable")
            return
        w, h = self.width, self.height
        vf = (f"fps={self.fps},"
              f"scale={w}:{h}:force_original_aspect_ratio=decrease,"
              f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2")
        with self.lock:
            if self._cancelled:
                return
            self._proc = subprocess.Popen(
                [ffmpeg, "-nostdin", "-v", "error", "-i", self.path,
                 "-an", "-sn", "-dn", "-vf", vf,
                 "-pix_fmt", "rgb24", "-f", "rawvideo", "pipe:1"],
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL)
            proc = self._proc

        size = w * h * 3
        while True:
            frame = proc.stdout.read(size)
            if len(frame) < size:
                break                    # end of stream (or a torn last frame)
            with self.lock:
                if self._cancelled:
                    break
                self.frames.append(frame)
        proc.stdout.close()
        code = proc.wait()
        with self.lock:
            if self._cancelled:
                return
            self.state = "ready" if code == 0 and self.frames else "error"
