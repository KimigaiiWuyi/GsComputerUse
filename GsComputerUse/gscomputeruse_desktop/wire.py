"""InstaBox 桌面命令。用户文字只走 stdin 或引号，不拼进 shell。"""

from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass

_PNG = b"\x89PNG\r\n\x1a\n"
_WINDOW_CAP = 12
_KEYS = {
    "enter": "Return",
    "return": "Return",
    "esc": "Escape",
    "escape": "Escape",
    "tab": "Tab",
    "space": "space",
    "backspace": "BackSpace",
    "delete": "Delete",
    "del": "Delete",
    "insert": "Insert",
    "home": "Home",
    "end": "End",
    "pageup": "Prior",
    "pagedown": "Next",
    "up": "Up",
    "down": "Down",
    "left": "Left",
    "right": "Right",
    "ctrl": "ctrl",
    "control": "ctrl",
    "alt": "alt",
    "shift": "shift",
    "meta": "super",
    "cmd": "super",
    "command": "super",
    "super": "super",
    "win": "super",
}
_MODS = {"ctrl", "control", "alt", "shift", "super", "cmd", "command", "win", "meta"}
_NAMED = set(_KEYS) - _MODS
_WHEEL = {"up": 4, "down": 5, "left": 6, "right": 7}
_BUTTONS = {"left": 1, "right": 3, "middle": 2}

WINDOW_SCRIPT = """export DISPLAY=:1
xdotool search --onlyvisible --name . | while read -r id; do
  [ -n "$id" ] || continue
  geo=$(xdotool getwindowgeometry --shell "$id" 2>/dev/null) || continue
  X=$(printf '%s\\n' "$geo" | sed -n 's/^X=//p' | head -n 1)
  Y=$(printf '%s\\n' "$geo" | sed -n 's/^Y=//p' | head -n 1)
  W=$(printf '%s\\n' "$geo" | sed -n 's/^WIDTH=//p' | head -n 1)
  H=$(printf '%s\\n' "$geo" | sed -n 's/^HEIGHT=//p' | head -n 1)
  name=$(xdotool getwindowname "$id" 2>/dev/null | tr -s '[:space:]' ' ')
  printf '%s\\t%s\\t%s\\t%s\\t%s\\t%s\\n' "$id" "$name" "$X" "$Y" "$W" "$H"
done
"""


@dataclass(frozen=True)
class WindowRow:
    name: str
    x: int
    y: int


def shell_quote(text: str) -> str:
    return "'" + text.replace("'", "'\\''") + "'"


def key_parts(key: str) -> list[str] | None:
    parts = [part.strip() for part in key.split("+") if part.strip()]
    if not parts or len(parts) > 4:
        return None
    for part in parts:
        if not _known_key(part):
            return None
    return parts


def key_command(parts: list[str]) -> str:
    symbols = [keysym(part) for part in parts]
    return f"xdotool key -- {shell_quote('+'.join(symbols))}"


def click_command(x: int, y: int, *, button: str, repeat: int) -> str:
    code = _BUTTONS[button] if button in _BUTTONS else 1
    repeat_flag = f" --repeat {repeat} --delay 80" if repeat > 1 else ""
    return f"xdotool mousemove {x} {y} click{repeat_flag} {code}"


def scroll_command(direction: str) -> str:
    wheel = _WHEEL[direction] if direction in _WHEEL else 5
    return f"xdotool click --repeat 3 --delay 30 {wheel}"


def type_command() -> str:
    return "xdotool type --delay 0 --file -"


def open_command(url: str) -> str:
    quoted = shell_quote(url)
    return (
        "b=$(command -v chromium || command -v chromium-browser || command -v google-chrome) || "
        "{ echo 'no browser in the sandbox' >&2; exit 127; }; "
        f'setsid nohup "$b" --no-first-run --no-default-browser-check --password-store=basic {quoted} '
        ">/dev/null 2>&1 </dev/null &"
    )


def launch_command(app: str) -> str:
    quoted = shell_quote(app)
    return f"gtk-launch {quoted} >/dev/null 2>&1 || xdg-open {quoted} >/dev/null 2>&1"


def with_display(command: str) -> str:
    return f"export DISPLAY=:1; {command}"


def parse_windows(text: str) -> list[WindowRow]:
    rows: list[WindowRow] = []
    for line in text.splitlines():
        if len(rows) >= _WINDOW_CAP:
            break
        parts = line.split("\t")
        if len(parts) != 6:
            continue
        if not parts[2].isdigit() or not parts[3].isdigit() or not parts[4].isdigit() or not parts[5].isdigit():
            continue
        left = int(parts[2])
        top = int(parts[3])
        width = int(parts[4])
        height = int(parts[5])
        rows.append(WindowRow(name=parts[1].strip() or parts[0], x=left + width // 2, y=top + height // 2))
    return rows


def decode_png(text: str, cap: int) -> bytes | None:
    compact = "".join(text.split())
    if not compact or len(compact) > cap * 2:
        return None
    try:
        raw = base64.b64decode(compact, validate=True)
    except binascii.Error:
        return None
    if len(raw) > cap or not raw.startswith(_PNG):
        return None
    return raw


def trim_shots(paths: list[str], keep: int) -> list[str]:
    ordered = sorted(paths)
    extra = len(ordered) - keep
    if extra <= 0:
        return []
    return ordered[:extra]


def app_name_ok(app: str, *, allow_space: bool) -> bool:
    if not app or len(app) > 64:
        return False
    for char in app:
        if char.isascii() and (char.isalnum() or char in "._-"):
            continue
        if allow_space and char == " ":
            continue
        return False
    return app[0].isalnum()


def keysym(part: str) -> str:
    lower = part.lower()
    if lower in _KEYS:
        return _KEYS[lower]
    if len(lower) >= 2 and lower[0] == "f" and lower[1:].isdigit():
        return lower.upper()
    return part


def _known_key(part: str) -> bool:
    lower = part.lower()
    if lower in _MODS or lower in _NAMED:
        return True
    if len(part) == 1 and part.isascii() and part.isalnum():
        return True
    if len(lower) >= 2 and lower[0] == "f" and lower[1:].isdigit() and 1 <= int(lower[1:]) <= 12:
        return True
    return False
