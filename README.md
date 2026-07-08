<pre>
  ▄█   ▄█▄    ▄████████    ▄████████    ▄████████  ▄██████▄     ▄█   ▄█▄    ▄████████ ▄██   ▄   
  ███ ▄███▀   ███    ███   ███    ███   ███    ███ ███    ███   ███ ▄███▀   ███    ███ ███   ██▄ 
  ███▐██▀     ███    ███   ███    ███   ███    ███ ███    ███   ███▐██▀     ███    ███ ███▄▄▄███ 
 ▄█████▀      ███    ███  ▄███▄▄▄▄██▀   ███    ███ ███    ███  ▄█████▀      ███    ███ ▀▀▀▀▀▀███ 
▀▀█████▄    ▀███████████ ▀▀███▀▀▀▀▀   ▀███████████ ███    ███ ▀▀█████▄    ▀███████████ ▄██   ███ 
  ███▐██▄     ███    ███ ▀███████████   ███    ███ ███    ███   ███▐██▄     ███    ███ ███   ███ 
  ███ ▀███▄   ███    ███   ███    ███   ███    ███ ███    ███   ███ ▀███▄   ███    ███ ███   ███ 
  ███   ▀█▀   ███    █▀    ███    ███   ███    █▀   ▀██████▀    ███   ▀█▀   ███    █▀   ▀█████▀  
  ▀                        ███    ███                           ▀                                
</pre>

> Synchronized lyrics in your terminal, powered by [MPD](https://www.musicpd.org/).

A minimalist curses-based karaoke client that follows whatever MPD is playing and renders timed lyrics (`.lrc`) line by line, with auto-fetch when a track has no local lyrics file.

![karaokay in action, with album cover, running in Kitty](docs/screenshot.png)

> **Tip:** for the best rendering — crisp album covers via the Kitty graphics protocol and vivid, theme-aware colors — run karaokay in [Kitty](https://sw.kovidgoyal.net/kitty/). Ghostty and WezTerm also support crisp covers; other terminals fall back gracefully (see [Album cover](#album-cover)).

---

## Features

- Live sync with MPD's elapsed time, with adjustable offset
- Auto-detects `music_directory` via MPD `config` command or by parsing standard `mpd.conf` locations
- Looks for `.lrc` files next to the audio file **and** in user-supplied lyrics directories
- Optional auto-fetch of synchronized lyrics through [`syncedlyrics`](https://github.com/moehmeni/syncedlyrics)
- Optional album cover next to the player — uses the best backend your terminal supports (Kitty graphics, iTerm2 inline images, or truecolor half-blocks everywhere else)
- Reconnects automatically if MPD goes away
- Clean color-coded UI (past / current / upcoming lines)

---

## Install

### Homebrew (macOS / Linux)

```bash
brew tap slashome/tap
brew install karaokay
```

### From source

Requirements:

- Python 3.10+
- An MPD instance you can reach
- [`python-mpd2`](https://pypi.org/project/python-mpd2/)
- *(Optional)* [`syncedlyrics`](https://pypi.org/project/syncedlyrics/) for online lyrics fetch
- *(Optional)* [`pillow`](https://pypi.org/project/pillow/) for album cover display (required for WebP/JPEG decoding)
- *(Windows only)* `windows-curses`

```bash
pip install python-mpd2 syncedlyrics pillow
# Windows additionally:
pip install windows-curses
```

---

## Usage

```bash
./karaokay.py [options]
```

### Common invocations

```bash
# Default — connects to localhost:6600, fetches lyrics on demand
./karaokay.py

# Remote MPD with password
./karaokay.py --host mpd.lan --port 6600 --password hunter2

# Pin a music directory and a dedicated lyrics folder
./karaokay.py --music-dir ~/Music --lyrics-dir ~/.lyrics

# Disable online fetching entirely
./karaokay.py --no-autofetch
```

### CLI flags

| Flag | Default | Description |
|------|---------|-------------|
| `--host` | `$MPD_HOST` or `localhost` | MPD host |
| `--port` | `$MPD_PORT` or `6600` | MPD port |
| `--password` | `$MPD_PASSWORD` | MPD password |
| `--music-dir` | `$MPD_MUSIC_DIR` | Audio root (used to find `.lrc` next to files) |
| `--lyrics-dir` | `~/.lyrics` | Lyrics folder (repeatable) |
| `--offset` | `0` | Initial sync offset in ms |
| `--no-autofetch` | off | Disables `syncedlyrics` online fetch |
| `--no-cover` | off | Disables album cover display |
| `--cover-protocol` | `auto` | Cover backend: `auto`, `kitty`, `iterm2`, `blocks`, `blocks256`, `none` |

---

## Keyboard shortcuts

| Key | Action |
|-----|--------|
| `q` / `Esc` | Quit |
| `p` | Play / pause toggle |
| `+` | Push lyrics 50 ms later |
| `-` | Pull lyrics 50 ms earlier |
| `s` | Resync — reload the `.lrc` for the current track |
| `f` | Force re-fetch lyrics for the current track |
| `h` | Toggle the status header |
| `i` | Toggle the now-playing block |
| `c` | Toggle the album cover |

---

## Mouse

| Action | Effect |
|--------|--------|
| Click a lyric line | Seek so that line becomes the current one |
| Click the progress bar | Seek to that point in the track |
| Click the album cover | Blow it up fullscreen; click anywhere to return |

---

## Album cover

The cover image must live **in the album folder** (next to the audio files) and be named `cover.png`, `cover.jpg`, `cover.jpeg`, or `cover.webp`. It is shown to the left of the now-playing block.

Rendering picks the best backend available, degrading gracefully:

1. **Kitty graphics protocol** — Kitty, Ghostty, WezTerm (crisp image)
2. **iTerm2 inline images** — iTerm2, WezTerm (crisp image)
3. **Truecolor half-blocks** — any 24-bit terminal (pixelated but universal)
4. **256-color half-blocks** — terminals without truecolor, e.g. Apple Terminal

`pillow` is required (and is what enables WebP/JPEG decoding); if it is missing the cover is silently skipped. Force a specific backend with `--cover-protocol`.

**Click the cover to enlarge it**: it expands to a centered square filling the terminal; click anywhere to restore the normal layout.

---

## Where lyrics files are looked up

1. Same folder as the audio file (e.g. `~/Music/Album/Track.lrc`)
2. Each `--lyrics-dir`, checked in order, with these filename patterns:
   - `<Artist> - <Title>.lrc`
   - `<Title>.lrc`
   - `<original basename>.lrc`

When auto-fetch is enabled and no local file is found, `syncedlyrics` is queried in a background thread. The fetched file is saved next to the audio file when possible, otherwise in the first `--lyrics-dir`.

---

## Notes

- The Musixmatch provider in `syncedlyrics` occasionally returns HTTP 401 — those log lines are silenced by default; other providers (LrcLib, NetEase) keep working transparently.
- Lyrics rendering is colorized: cyan for already-sung lines, light gray for upcoming ones, and bold white for the active line, with a `▶` cursor in front. Exact hues come from your terminal's ANSI palette, so they follow your theme (Kitty recommended).

---

## License

MIT — do whatever you want with it.
