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
- Interactive lyrics picker (`f`): list candidates from several sites (LrcLib, Musixmatch, NetEase, Megalobiz, Genius), preview one against the running track, and save the chosen `.lrc` into the album folder
- Timing editor (`e`): retime a line by clicking it while the track plays, or turn a plain `.txt` into a synced `.lrc` line by line
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
- [`requests`](https://pypi.org/project/requests/) (used to query LrcLib directly for the `f` picker)
- *(Optional)* [`syncedlyrics`](https://pypi.org/project/syncedlyrics/) for online lyrics fetch
- *(Optional)* [`pillow`](https://pypi.org/project/pillow/) for album cover display (required for WebP/JPEG decoding)
- *(Optional)* [`pyyaml`](https://pypi.org/project/PyYAML/) for the config file — without it, the flat `key: value` form documented below is still read
- *(Windows only)* `windows-curses`

```bash
pip install python-mpd2 requests syncedlyrics pillow pyyaml
# Windows additionally:
pip install windows-curses
```

---

## Usage

```bash
karaokay [options]           # installed entry point
python -m karaokay [options] # equivalently, from a source checkout
```

### Common invocations

```bash
# Default — connects to localhost:6600, fetches lyrics on demand
karaokay

# Remote MPD with password
karaokay --host mpd.lan --port 6600 --password hunter2

# Pin a music directory and a dedicated lyrics folder
karaokay --music-dir ~/Music --lyrics-dir ~/.lyrics

# Disable online fetching entirely
karaokay --no-autofetch
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
| `--playlist-width` | `50` | Width of the `l` playlist pane, in % of the screen (20–80) |

---

## Configuration file

Every option above can be set once and for all in
**`~/.config/karaokay/config.yaml`** (or `$XDG_CONFIG_HOME/karaokay/config.yaml`).
Command-line flags always win over the file, so it only supplies the defaults.

Keys are the long flag names with the leading dashes dropped; `-` and `_` are
interchangeable:

```yaml
# ~/.config/karaokay/config.yaml
host: localhost
port: 6600
music-dir: ~/Music
lyrics-dir:
  - ~/.lyrics
  - ~/Music/lyrics
offset: 0               # ms
playlist-width: 40      # % of the screen taken by the `l` pane
cover-protocol: kitty
no-cover: false
no-autofetch: false
```

`~` is expanded in paths. The file is optional; an unknown key or a value of
the wrong type is reported on stderr at startup and ignored, so a typo never
keeps karaokay from running. `--lyrics-dir` on the command line *replaces* the
configured folders rather than adding to them.

---

## Keyboard shortcuts

| Key | Action |
|-----|--------|
| `q` / `Esc` | Quit |
| `p` | Play / pause toggle |
| `+` | Push lyrics 50 ms later |
| `-` | Pull lyrics 50 ms earlier |
| `s` | Resync — reload the `.lrc` for the current track |
| `↑` / `↓` (or `k` / `j`), `PgUp` / `PgDn` | Scroll unsynchronized lyrics |
| `f` | Open the lyrics picker (fetch candidates from several sites) |
| `e` | Open the timing editor for the current lyrics |
| `l` | Toggle the playlist pane (the MPD queue, on the right) |
| `h` / `?` | Show the full list of shortcuts |
| `H` | Toggle the status header |
| `i` | Toggle the now-playing block |
| `c` | Toggle the album cover |

---

## Mouse

| Action | Effect |
|--------|--------|
| Click a lyric line | Seek so that line becomes the current one |
| Click the progress bar | Seek to that point in the track |
| Click the album cover | Blow it up fullscreen; click anywhere to return |
| Click a playlist row | Select it; click the selected row again to play it |
| Click a line in the editor | Stamp it with the current playback position |

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

## Playlist pane (`l`)

Press **`l`** to split the screen and show the MPD queue on the right, the way
ncmpcpp does — artist, track number, title, album and duration, with the
playing track marked `▶`. It opens on the song being played.

| Key | Action |
|-----|--------|
| `↑` / `↓` (or `k` / `j`) | Move the selection |
| `PgUp` / `PgDn` | Move ten rows at a time |
| `g` / `G` (or `Home` / `End`) | Jump to the first / last entry |
| `Enter` | Play the selected track |
| `l` / `Esc` | Close the pane |

The pane takes `--playlist-width` percent of the screen (`playlist-width` in
the config file, 50% by default) and the lyrics keep the rest. Columns are
dropped as the pane narrows — album first, then artist — so the title always
stays readable. The queue is re-read only when MPD reports a change, so a long
queue costs nothing to keep open.

While the pane is open it takes the arrow keys, so scrolling unsynchronized
lyrics with `↑`/`↓` means closing it first.

---

## Picking better lyrics (`f`)

Auto-fetch grabs whatever the first provider returns, which is sometimes the
wrong take (a remix, a mistimed transcription…). Press **`f`** to open the
picker and choose by hand:

| Key | Action |
|-----|--------|
| `↑` / `↓` (or `k` / `j`) | Move through the candidates |
| `Enter` / `Space` | Preview the highlighted candidate, synced to the running track |
| `w` | Save the highlighted candidate as the track's `.lrc` |
| `+` / `-` | Adjust the offset (also affects the live preview) |
| `f` / `Esc` / `q` | Close the picker |

Candidates are gathered in the background from several sites:

- **LrcLib** is queried directly and can return **several alternatives** for the
  same song (different durations/takes), each shown with its metadata and line
  count.
- **Musixmatch, NetEase, Megalobiz, Genius** each contribute their single best
  match through `syncedlyrics`.

Each row is tagged `synced N` (timed, `N` lines) or `plain` (untimed text).
Selecting a synced candidate scrolls its lyrics along with playback so you can
judge the timing before committing. `w` writes the file **into the album
folder** (next to the audio file) when it is writable, otherwise into the first
`--lyrics-dir`, replacing any existing `.lrc`. The file is picked up right away
and the save is confirmed at the bottom of the screen.

### Unsynchronized lyrics

Some songs only exist as plain text — the `plain` candidates. Saving one is
still useful: karaokay shows the whole text as a static page you scroll with
`↑`/`↓` (or `k`/`j`, `PgUp`/`PgDn`), and the header tags the file `(plain)`.
There are no timings, so nothing scrolls along with playback — until you time
them yourself with [the `e` editor](#editing-timings-e).

---

## Editing timings (`e`)

Press **`e`** to retime the lyrics of the track being played. It covers the two
cases that leave you with lyrics you cannot sing along to:

- **a line that fires too early or too late** in an otherwise fine `.lrc`, and
- **plain text with no timings at all** — a `.txt` dropped next to the audio
  file, or a `plain` candidate saved from the `f` picker.

Every line is listed with its timestamp, `--:--.--` when it has none. The line
currently playing is highlighted, so you can see the drift as it happens.

| Key | Action |
|-----|--------|
| `Space` | Stamp the selected line with the current playback position, then move down |
| *click a line* | Stamp that line, staying where you are |
| `↑` / `↓` (or `k` / `j`), `PgUp` / `PgDn`, `g` / `G` | Move through the lines |
| `←` / `→` (or `<` / `>`) | Nudge the selected line by ±0.1 s |
| `x` | Clear the selected line's timestamp |
| `Enter` | Listen back: seek playback to the selected line |
| `p` | Play / pause — stamping works paused or playing |
| `e` / `Esc` | Leave the editor |

Leaving with unsaved work asks first: **`Enter` saves**, **`Esc` discards**.
Leaving an untouched session just closes, without a prompt.

To sync a whole song from scratch: drop the lyrics as a `.txt` next to the
audio file, press `e`, start playback, and tap `Space` as each line comes.

Where the result is written: an existing `.lrc` is updated in place, wherever
it was found. Anything else — a `.txt`, or lyrics saved from the picker — is
written as a `.lrc` in the album folder when it is writable, otherwise in the
first `--lyrics-dir`. Your `.txt` is never overwritten.

Lines you have not timed yet are kept in the saved file, untagged, so a long
track can be synced over several sittings — reopen it with `e` and carry on.
Timestamps are recorded in *displayed* time, i.e. with the current `+`/`-`
offset applied, so what you stamp is what you see.

---

## Where lyrics files are looked up

1. Same folder as the audio file (e.g. `~/Music/Album/Track.lrc`)
2. Each `--lyrics-dir`, checked in order, with these filename patterns:
   - `<Artist> - <Title>.lrc`
   - `<Title>.lrc`
   - `<original basename>.lrc`

`.lrc` files are looked for first, then `.txt` — plain lyrics with no timings,
shown as a static page and ready to be timed with the `e` editor. Saving from
the editor always writes a `.lrc`, so the `.txt` you started from is left as
it is.

When auto-fetch is enabled and no local file is found, `syncedlyrics` is queried in a background thread. The fetched file is saved next to the audio file when possible, otherwise in the first `--lyrics-dir`.

---

## Notes

- The Musixmatch provider in `syncedlyrics` occasionally returns HTTP 401 — those log lines are silenced by default; other providers (LrcLib, NetEase) keep working transparently. The `f` picker degrades the same way: providers that fail are marked with a small `✗` and the rest still populate the list.
- Lyrics rendering is colorized: cyan for already-sung lines, light gray for upcoming ones, and bold white for the active line, with a `▶` cursor in front. Exact hues come from your terminal's ANSI palette, so they follow your theme (Kitty recommended).

---

## License

MIT — do whatever you want with it.
