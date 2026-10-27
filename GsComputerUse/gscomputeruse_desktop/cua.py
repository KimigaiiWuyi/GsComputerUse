"""CUA Driver 命令行。插件只调用已经在跑的守护进程，不安装、不内嵌。"""

from __future__ import annotations

import json
import asyncio
import hashlib
from typing import Protocol
from pathlib import Path
from dataclasses import dataclass
from collections.abc import Mapping

from .errors import ComputerNotReady
from .snapshot import Mark, MarkBook, clip_text, scroll_direction
from ..gscomputeruse_config.host import HostBudget, shot_byte_cap

_STDOUT_CAP = 64 * 1024
_ELEMENT_CAP = 24


@dataclass(frozen=True)
class ProcResult:
    code: int
    out: str
    err: str


class Runner(Protocol):
    async def run(self, argv: list[str], *, timeout: float) -> ProcResult: ...


@dataclass(frozen=True)
class CuaWindow:
    window_id: int
    title: str
    z_index: int | None


@dataclass(frozen=True)
class CuaElement:
    token: str
    kind: str
    name: str


def cua_argv(binary: str, tool: str, payload: str = "", shot: str = "") -> list[str]:
    argv = [binary, "call", tool]
    if payload:
        argv.append(payload)
    if shot:
        argv.extend(["--screenshot-out-file", shot])
    return argv


def daemon_ready(code: int, out: str) -> bool:
    if code != 0:
        return False
    folded = out.lower()
    if "not running" in folded:
        return False
    return "running" in folded


def session_label(tenant: str) -> str:
    return "gs" + hashlib.sha256(tenant.encode("utf-8")).hexdigest()[:16]


def install_hint(system: str) -> str:
    if system == "macos":
        return "macOS 需要已安装的 CUA Driver，并打开辅助功能和屏幕录制。"
    if system == "windows":
        return "Windows 需要在已登录的桌面会话里启动 CUA Driver。"
    if system == "linux":
        return "Linux 需要在图形会话里启动 CUA Driver。没有图形会话时它看不到桌面。"
    return "需要已安装并正在运行的 CUA Driver。"


def browser_name(system: str) -> str:
    if system == "windows":
        return "Microsoft Edge"
    if system == "macos":
        return "Safari"
    return "firefox"


class SubprocessRunner:
    async def run(self, argv: list[str], *, timeout: float) -> ProcResult:
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except (FileNotFoundError, OSError):
            # 找不到程序时把缺失告诉用户，不改用 shell。
            raise ComputerNotReady("找不到 cua-driver。请先安装 CUA Driver。") from None
        try:
            out, err = await asyncio.wait_for(_collect(proc), timeout)
        except TimeoutError:
            proc.kill()
            await proc.wait()
            raise ComputerNotReady("CUA Driver 超时。") from None
        code = proc.returncode if proc.returncode is not None else 1
        return ProcResult(code=code, out=out, err=err)


class CuaDesktop:
    def __init__(
        self,
        runner: Runner,
        binary: str,
        session: str,
        budget: HostBudget,
    ) -> None:
        self._runner = runner
        self._binary = binary
        self._session = session
        self._budget = budget
        self._marks = MarkBook()
        self._last_token = ""

    async def look(self) -> str:
        listed = await self._call("list_windows", {"on_screen_only": True, "session": self._session})
        windows = parse_windows(listed.out)
        if not windows:
            note = listed.out.strip() or listed.err.strip() or "桌面上没有可见窗口。"
            return self._marks.replace([], title="本机桌面", body=clip_text(note, 500))
        front = front_window(windows)
        state = await self._call(
            "get_window_state",
            {
                "window_id": front.window_id,
                "include_screenshot": False,
                "include_accessibility_tree": True,
                "tree_format": "elements",
                "max_elements": _ELEMENT_CAP,
                "session": self._session,
            },
        )
        elements = parse_elements(state.out)
        if not elements and state.code != 0:
            note = state.out.strip() or state.err.strip() or "读不到窗口里的控件。"
            return self._marks.replace([], title=front.title or "本机桌面", body=clip_text(note, 500))
        marks = [Mark(kind=item.kind, name=item.name, token=item.token, x=0, y=0) for item in elements]
        body = f"窗口「{front.title}」。用 e 编号点击。" if front.title else "用 e 编号点击。"
        return self._marks.replace(marks, title="本机桌面", body=body)

    async def click(self, target: str, *, foreground: bool) -> str:
        found = self._marks.resolve(target)
        if isinstance(found, str):
            return found
        mode = "foreground" if foreground else "background"
        payload: dict[str, str | int | bool] = {
            "session": self._session,
            "delivery_mode": mode,
            "post_action_wait": "none",
            "button": "left",
        }
        if found.token:
            payload["element_token"] = found.token
            self._last_token = found.token
        else:
            payload["x"] = found.x
            payload["y"] = found.y
            payload["scope"] = "desktop"
        result = await self._call("click", payload)
        if "background_unavailable" in result.out or "background_unavailable" in result.err:
            return "后台点击不可用。需要时把 foreground 设为 true，这会占用你的鼠标。"
        return await self.look()

    async def type_text(self, text: str) -> str:
        payload: dict[str, str] = {"text": text, "session": self._session}
        if self._last_token:
            payload["element_token"] = self._last_token
        await self._call("type_text", payload)
        return await self.look()

    async def press(self, key: str, *, foreground: bool) -> str:
        parts = _cua_keys(key)
        if parts is None:
            return "不认识这个按键。"
        mode = "foreground" if foreground else "background"
        if len(parts) == 1:
            payload: dict[str, str | list[str]] = {
                "key": parts[0],
                "session": self._session,
                "scope": "desktop",
                "delivery_mode": mode,
                "post_action_wait": "none",
            }
            await self._call("press_key", payload)
        else:
            chord: dict[str, str | list[str]] = {
                "keys": parts,
                "session": self._session,
                "scope": "desktop",
                "delivery_mode": mode,
                "post_action_wait": "none",
            }
            await self._call("hotkey", chord)
        return await self.look()

    async def scroll(self, direction: str) -> str:
        folded = scroll_direction(direction)
        if not folded:
            return "方向用 up、down、left、right。"
        await self._call(
            "scroll",
            {
                "direction": folded,
                "amount": 3,
                "by": "line",
                "scope": "desktop",
                "session": self._session,
                "post_action_wait": "none",
            },
        )
        return await self.look()

    async def open_url(self, url: str) -> str:
        app = browser_name(self._budget.system)
        await self._call("launch_app", {"name": app, "session": self._session})
        looked = await self.look()
        return f"已打开 {app}。请在地址栏输入：{url}\n{looked}"

    async def launch(self, app: str) -> str:
        await self._call("launch_app", {"name": app, "session": self._session})
        return await self.look()

    async def shot(self, path: str) -> str:
        cap = shot_byte_cap(self._budget)
        edge = 1024 if self._budget.tight else 1280
        await self._call(
            "get_desktop_state",
            {"session": self._session, "max_image_dimension": edge},
            shot=path,
        )
        target = Path(path)
        if not target.is_file():
            return "没有得到截图。"
        if target.stat().st_size > cap:
            target.unlink()
            return f"截图超过 {cap // 1024} KiB，已丢弃。"
        return str(target)

    async def release(self) -> str:
        result = await self._call("end_session", {"session": self._session})
        if result.code != 0:
            return "本机桌面会话已放开。"
        return "本机桌面会话已结束。"

    async def _call(
        self,
        tool: str,
        payload: Mapping[str, str | int | bool | list[str]],
        shot: str = "",
    ) -> ProcResult:
        text = json.dumps(payload, ensure_ascii=False)
        argv = cua_argv(self._binary, tool, text, shot)
        return await self._runner.run(argv, timeout=20)


class CuaBackend:
    def __init__(self, binary: str, runner: Runner, budget: HostBudget) -> None:
        self._binary = binary
        self._runner = runner
        self._budget = budget

    async def open_desktop(self, tenant: str) -> CuaDesktop:
        result = await self._runner.run([self._binary, "status"], timeout=8)
        if not daemon_ready(result.code, result.out):
            detail = result.out.strip() or result.err.strip()
            hint = install_hint(self._budget.system)
            extra = f" {detail}" if detail else ""
            raise ComputerNotReady(f"CUA Driver 没有在运行。{hint}{extra}")
        return CuaDesktop(self._runner, self._binary, session_label(tenant), self._budget)

    async def stop(self) -> None:
        return None


def front_window(rows: list[CuaWindow]) -> CuaWindow:
    ranked = [row for row in rows if row.z_index is not None]
    if not ranked:
        return rows[0]
    return max(ranked, key=lambda row: row.z_index or 0)


def parse_windows(text: str) -> list[CuaWindow]:
    rows = _rows(text, "windows")
    found: list[CuaWindow] = []
    for row in rows:
        window = _window(row)
        if window is not None:
            found.append(window)
    return found[:12]


def parse_elements(text: str) -> list[CuaElement]:
    rows = _rows(text, "elements")
    found: list[CuaElement] = []
    for row in rows:
        element = _element(row)
        if element is not None:
            found.append(element)
        if len(found) >= _ELEMENT_CAP:
            break
    return found


async def _collect(proc: asyncio.subprocess.Process) -> tuple[str, str]:
    out_pipe = proc.stdout
    err_pipe = proc.stderr
    out = await _pipe(out_pipe, proc)
    err = await _pipe(err_pipe, proc)
    await proc.wait()
    return out, err


async def _pipe(pipe: asyncio.StreamReader | None, proc: asyncio.subprocess.Process) -> str:
    if pipe is None:
        return ""
    chunks: list[bytes] = []
    size = 0
    while True:
        chunk = await pipe.read(4096)
        if not chunk:
            break
        size += len(chunk)
        if size > _STDOUT_CAP:
            proc.kill()
            break
        chunks.append(chunk)
    return b"".join(chunks).decode("utf-8", errors="replace")


def _rows(text: str, key: str) -> list[object]:
    try:
        raw = json.loads(text)
    except json.JSONDecodeError:
        return []
    if isinstance(raw, list):
        return [item for item in raw if isinstance(item, dict)]
    if not isinstance(raw, dict):
        return []
    if key in raw and isinstance(raw[key], list):
        return [item for item in raw[key] if isinstance(item, dict)]
    if "structuredContent" in raw and isinstance(raw["structuredContent"], dict):
        nested = raw["structuredContent"]
        if key in nested and isinstance(nested[key], list):
            return [item for item in nested[key] if isinstance(item, dict)]
    return []


def _window(row: object) -> CuaWindow | None:
    if not isinstance(row, dict):
        return None
    window_id = row["window_id"] if "window_id" in row else None
    if not isinstance(window_id, int):
        return None
    title = row["title"] if "title" in row and isinstance(row["title"], str) else ""
    z_index = row["z_index"] if "z_index" in row and isinstance(row["z_index"], int) else None
    return CuaWindow(window_id=window_id, title=title, z_index=z_index)


def _element(row: object) -> CuaElement | None:
    if not isinstance(row, dict):
        return None
    token = row["element_token"] if "element_token" in row and isinstance(row["element_token"], str) else ""
    if not token:
        return None
    kind = ""
    if "role" in row and isinstance(row["role"], str):
        kind = row["role"]
    elif "kind" in row and isinstance(row["kind"], str):
        kind = row["kind"]
    name = ""
    if "label" in row and isinstance(row["label"], str):
        name = row["label"]
    elif "name" in row and isinstance(row["name"], str):
        name = row["name"]
    return CuaElement(token=token, kind=kind or "control", name=name or token)


_CUA_MODS = {
    "ctrl": "ctrl",
    "control": "ctrl",
    "alt": "alt",
    "shift": "shift",
    "cmd": "cmd",
    "command": "cmd",
    "win": "win",
    "meta": "meta",
    "super": "super",
}
_CUA_NAMED = {
    "enter": "enter",
    "return": "enter",
    "tab": "tab",
    "esc": "escape",
    "escape": "escape",
    "space": "space",
    "backspace": "backspace",
    "delete": "delete",
    "del": "delete",
    "up": "up",
    "down": "down",
    "left": "left",
    "right": "right",
    "home": "home",
    "end": "end",
    "pageup": "pageup",
    "pagedown": "pagedown",
    "insert": "insert",
}


def _cua_keys(key: str) -> list[str] | None:
    parts = [part.strip() for part in key.split("+") if part.strip()]
    if not parts or len(parts) > 4:
        return None
    mapped: list[str] = []
    for part in parts:
        lower = part.lower()
        if lower in _CUA_MODS:
            mapped.append(_CUA_MODS[lower])
            continue
        if lower in _CUA_NAMED:
            mapped.append(_CUA_NAMED[lower])
            continue
        if len(part) == 1 and part.isascii() and part.isalnum():
            mapped.append(part.lower())
            continue
        if len(lower) >= 2 and lower[0] == "f" and lower[1:].isdigit() and 1 <= int(lower[1:]) <= 12:
            mapped.append(lower)
            continue
        return None
    return mapped
