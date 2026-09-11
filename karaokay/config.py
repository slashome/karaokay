"""The YAML config file: ``~/.config/karaokay/config.yaml``.

It carries the defaults for the command-line options, so the flags stay the
override path: whatever is passed on the command line wins over the file.

Keys mirror the long flag names with the leading dashes dropped; ``-`` and
``_`` are interchangeable (``cover-protocol`` == ``cover_protocol``):

    host: localhost
    port: 6600
    music-dir: ~/Music
    lyrics-dir:
      - ~/.lyrics
    offset: 0
    playlist-width: 50
    no-cover: false
    cover-protocol: kitty

PyYAML is used when installed; otherwise the flat subset above is read by a
small built-in parser, so the config never depends on an optional module.
"""

import os
import re
from pathlib import Path
from typing import Any, Optional

try:
    import yaml
    YAML_AVAILABLE = True
except ImportError:
    YAML_AVAILABLE = False


# Recognized keys → the type they are coerced to. Anything else is reported as
# unknown rather than silently ignored, since a typo in a config file is
# otherwise invisible.
SCHEMA: dict[str, type] = {
    "host":            str,
    "port":            int,
    "password":        str,
    "music_dir":       str,
    "lyrics_dir":      list,
    "offset":          int,
    "no_autofetch":    bool,
    "no_cover":        bool,
    "cover_protocol":  str,
    "playlist_width":  int,
}

PATH_KEYS = ("music_dir", "lyrics_dir")   # values get ~ expanded


def config_path() -> Path:
    """The config file location, honouring $XDG_CONFIG_HOME."""
    base = os.getenv("XDG_CONFIG_HOME") or "~/.config"
    return Path(os.path.expanduser(base)) / "karaokay" / "config.yaml"


def load(path: Optional[Path] = None) -> tuple[dict[str, Any], list[str]]:
    """Read the config file into ``(values, warnings)``.

    A missing file is not an error: it yields empty values. Anything wrong
    inside it — unparsable YAML, an unknown key, a value of the wrong type —
    is reported as a warning and skipped, so a bad line never keeps karaokay
    from starting.
    """
    path = path or config_path()
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}, []
    except OSError as e:
        return {}, [f"{path}: {e.strerror or e}"]

    try:
        raw = yaml.safe_load(text) if YAML_AVAILABLE else _mini_yaml(text)
    except Exception as e:
        return {}, [f"{path}: not valid YAML ({e})"]
    if raw is None:
        return {}, []
    if not isinstance(raw, dict):
        return {}, [f"{path}: expected a mapping of options, got {type(raw).__name__}"]

    values:   dict[str, Any] = {}
    warnings: list[str]      = []
    for key, value in raw.items():
        name = str(key).strip().replace("-", "_")
        want = SCHEMA.get(name)
        if want is None:
            warnings.append(f"{path}: unknown option '{key}' (ignored)")
            continue
        if value is None:
            continue
        try:
            values[name] = _coerce(name, value, want)
        except (TypeError, ValueError):
            warnings.append(
                f"{path}: '{key}' expects {want.__name__}, got {value!r} (ignored)")
    return values, warnings


# ── Coercion ─────────────────────────────────────────────────────────────────

def _coerce(name: str, value: Any, want: type) -> Any:
    if want is list:
        items = value if isinstance(value, list) else [value]
        out = [str(v) for v in items]
        return [os.path.expanduser(v) for v in out] if name in PATH_KEYS else out
    if want is bool:
        if isinstance(value, bool):
            return value
        if str(value).strip().lower() in ("true", "yes", "on", "1"):
            return True
        if str(value).strip().lower() in ("false", "no", "off", "0"):
            return False
        raise ValueError(value)
    if want is int:
        return int(str(value).strip())          # rejects "abc", accepts "50"
    text = str(value)
    return os.path.expanduser(text) if name in PATH_KEYS else text


# ── Fallback parser (used when PyYAML is not installed) ───────────────────────

_KV_RE   = re.compile(r"^(?P<key>[A-Za-z0-9_-]+)\s*:\s*(?P<val>.*)$")
_ITEM_RE = re.compile(r"^-\s+(?P<val>.+)$")


def _mini_yaml(text: str) -> dict[str, Any]:
    """Parse the flat `key: value` / `- item` subset the config file needs.

    Deliberately narrow: one level of keys, scalars and block sequences. A line
    it cannot make sense of raises, which `load` turns into a warning.
    """
    out: dict[str, Any] = {}
    pending_list_key: Optional[str] = None
    for lineno, raw in enumerate(text.splitlines(), 1):
        line = raw.split(" #", 1)[0].rstrip() if " #" in raw else raw.rstrip()
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        item = _ITEM_RE.match(line.strip())
        if item:
            if pending_list_key is None:
                raise ValueError(f"line {lineno}: list item outside of a key")
            out[pending_list_key].append(_scalar(item.group("val")))
            continue
        kv = _KV_RE.match(line.strip())
        if not kv:
            raise ValueError(f"line {lineno}: cannot parse {raw.strip()!r}")
        key, val = kv.group("key"), kv.group("val").strip()
        if val:
            out[key] = _scalar(val)
            pending_list_key = None
        else:
            out[key] = []                 # a key with no value opens a list
            pending_list_key = key
    return out


def _scalar(text: str) -> Any:
    text = text.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        return text[1:-1]
    low = text.lower()
    if low in ("true", "yes", "on"):
        return True
    if low in ("false", "no", "off"):
        return False
    if low in ("null", "~", ""):
        return None
    try:
        return int(text)
    except ValueError:
        return text
