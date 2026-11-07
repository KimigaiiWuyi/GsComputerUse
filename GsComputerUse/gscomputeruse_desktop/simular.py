"""Simular box。云端 Ubuntu 桌面，本机只发 MCP 请求，不开虚拟机。"""

from __future__ import annotations

import json
import asyncio
from pathlib import Path
from dataclasses import dataclass

from .mcp import Session, ToolReply
from .lease import Lease, LeaseBook
from .errors import ComputerNotReady
from .snapshot import Mark, MarkBook, clip_text, scroll_direction
from ..gscomputeruse_config.host import HostBudget, shot_byte_cap

_PNG = b"\x89PNG\r\n\x1a\n"
_MARK_CAP = 24
_DESK_PASSIVE = frozenset(
    {
        "text",
        "alert",
        "static",
        "image",
        "label",
        "toolbar",
        "separator",
        "filler",
        "panel",
        "frame",
        "section",
        "paragraph",
        "list",
        "table",
        "row",
        "cell",
        "column header",
        "row header",
        "canvas",
        "generic",
        "scroll bar",
        "status bar",
        "title bar",
        "window",
        "application",
        "heading",
    }
)
_PAGE_PASSIVE = frozenset({"text", "heading", "image", "img", "article", "paragraph", "list", "table"})


@dataclass(frozen=True)
class BoxRef:
    box_id: str
    name: str
    state: str


class SimularClient:
    def __init__(self, session: Session, book: LeaseBook) -> None:
        self._session = session
        self._book = book
        self._locks: dict[str, asyncio.Lock] = {}

    async def ensure(self, tenant: str) -> BoxRef:
        lock = self._locks.setdefault(tenant, asyncio.Lock())
        async with lock:
            return await self._ensure(tenant)

    async def tool(self, name: str, arguments: dict[str, object]) -> ToolReply:
        reply = await self._session.tool(name, arguments)
        if reply.failed:
            raise ComputerNotReady(clip_text(reply.text.strip() or "Simular 工具调用失败。", 400))
        return reply

    async def suspend(self, box: BoxRef) -> str:
        reply = await self._session.tool("box_hibernate", {"box": box.box_id})
        if reply.failed:
            return clip_text(reply.text.strip(), 200) or "Simular 云端桌面会按账号的空闲时间休眠。"
        return ""

    async def _ensure(self, tenant: str) -> BoxRef:
        boxes = await self._boxes()
        current = self._book.get(tenant)
        if current is not None and current["backend"] == "simular":
            found = _by_id(boxes, current["machine_id"])
            if found is not None:
                return await self._wake(tenant, found)
        if not boxes:
            # 空 box_id 交给服务端：选第一台，账号一台都没有就创建一台。
            reply = await self._session.tool("box_wake", {"box": ""})
            if not reply.failed:
                boxes = await self._boxes()
        if not boxes:
            raise ComputerNotReady("这个 Simular 账号还没有云端桌面。去 box.simular.ai 开一台。")
        return await self._wake(tenant, boxes[0])

    async def _wake(self, tenant: str, box: BoxRef) -> BoxRef:
        if box.state != "running":
            await self.tool("box_wake", {"box": box.box_id})
        row: Lease = {"tenant": tenant, "backend": "simular", "machine_id": box.box_id, "name": box.name}
        self._book.put(row)
        return box

    async def _boxes(self) -> list[BoxRef]:
        reply = await self.tool("box_list", {})
        return parse_boxes(reply.text)


class SimularDesktop:
    def __init__(self, client: SimularClient, box: BoxRef, budget: HostBudget) -> None:
        self._client = client
        self._box = box
        self._budget = budget
        self._marks = MarkBook()
        self._size = (0, 0)

    async def look(self) -> str:
        listed = await self._client.tool("desk", {"op": "windows"})
        snap = await self._client.tool("desk", {"op": "snapshot", "text": True})
        return await self._render(snap.text, listed.text)

    async def click(self, target: str, *, foreground: bool) -> str:
        del foreground
        found = self._marks.resolve(target)
        if isinstance(found, str):
            return found
        if found.token.startswith("ref:"):
            reply = await self._client.tool("browser_click", {"ref": found.token[4:]})
            return self._render_page(reply.text)
        if found.token.startswith("desk:"):
            await self._client.tool("desk", {"op": "press", "id": found.token[5:]})
        elif found.token.startswith("win:"):
            await self._client.tool("desk", {"op": "focus", "id": found.token[4:]})
        else:
            await self._client.tool("desk", {"op": "click", "x": found.x, "y": found.y})
        return await self.look()

    async def type_text(self, text: str) -> str:
        await self._client.tool("desk", {"op": "type", "text": text})
        return await self.look()

    async def press(self, key: str, *, foreground: bool) -> str:
        del foreground
        keys = desk_keys(key)
        if not keys:
            return "不认识这个按键。"
        await self._client.tool("desk", {"op": "key", "keys": keys})
        return await self.look()

    async def scroll(self, direction: str) -> str:
        folded = scroll_direction(direction)
        if not folded:
            return "方向用 up、down、left、right。"
        await self._client.tool("computer_act", {"action": "scroll", "direction": folded, "amount": 3})
        return await self.look()

    async def open_url(self, url: str) -> str:
        reply = await self._client.tool("browser_navigate", {"url": url, "front": True})
        return self._render_page(reply.text)

    async def launch(self, app: str) -> str:
        if not _app_ok(app):
            return "云端桌面请填程序名，例如 mousepad，不要带空格。"
        await self._client.tool("desk", {"op": "launch", "app": app})
        return await self.look()

    async def shot(self, path: str) -> str:
        cap = shot_byte_cap(self._budget)
        reply = await self._client.tool("desk", {"op": "shot"})
        raw = _png(reply.images, cap)
        if raw is None:
            return f"截图超过 {cap // 1024} KiB 或不是 PNG，已丢弃。"
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(raw)
        return str(target)

    async def release(self) -> str:
        note = await self._client.suspend(self._box)
        if note:
            return note
        return "云端桌面已休眠。"

    async def _screen(self) -> tuple[int, int]:
        if self._size[0]:
            return self._size
        reply = await self._client.tool("desk", {"op": "screen"})
        self._size = parse_screen(reply.text)
        return self._size

    async def _render(self, text: str, listed: str) -> str:
        snap = parse_desk(text)
        width, height = await self._screen()
        marks = [Mark(kind=row.kind, name=row.name, token=f"desk:{row.token}", x=0, y=0) for row in snap.elements]
        others = parse_windows_list(listed, skip=snap.title)
        marks.extend(Mark(kind="window", name=row.name, token=f"win:{row.token}", x=0, y=0) for row in others)
        body = snap.body or "桌面上没有可见控件。"
        if others:
            body = f"其他窗口：{'、'.join(row.name for row in others)}。\n{body}"
        title = f"{snap.title}（云端桌面 {width}×{height}，坐标按屏幕像素）"
        return self._marks.replace(marks[:_MARK_CAP], title=title, body=body)

    def _render_page(self, text: str) -> str:
        page = parse_page(text)
        marks = [Mark(kind=row.kind, name=row.name, token=f"ref:{row.token}", x=0, y=0) for row in page.elements]
        return self._marks.replace(marks, title=page.title, body=page.body)


@dataclass(frozen=True)
class DeskRow:
    kind: str
    name: str
    token: str


@dataclass(frozen=True)
class DeskSnap:
    title: str
    body: str
    elements: list[DeskRow]


@dataclass(frozen=True)
class PageSnap:
    title: str
    body: str
    elements: list[DeskRow]


def parse_boxes(text: str) -> list[BoxRef]:
    try:
        raw = json.loads(text)
    except json.JSONDecodeError:
        return []
    if isinstance(raw, list):
        rows = raw
    else:
        rows = raw["boxes"] if isinstance(raw, dict) and "boxes" in raw and isinstance(raw["boxes"], list) else []
    found: list[BoxRef] = []
    for row in rows:
        box = _box(row)
        if box is not None:
            found.append(box)
    return found


def parse_desk(text: str) -> DeskSnap:
    try:
        raw = json.loads(text)
    except json.JSONDecodeError:
        return DeskSnap(title="云端桌面", body=clip_text(text.strip(), 3500), elements=[])
    if not isinstance(raw, dict):
        return DeskSnap(title="云端桌面", body="", elements=[])
    tree = raw["text"] if "text" in raw and isinstance(raw["text"], str) else ""
    window = raw["window"] if "window" in raw and isinstance(raw["window"], dict) else {}
    title = window["title"] if "title" in window and isinstance(window["title"], str) else ""
    elements = [
        row for row in (_tagged_row(line, "[id=", _DESK_PASSIVE) for line in tree.splitlines()) if row is not None
    ]
    return DeskSnap(title=title or "云端桌面", body=tree.strip(), elements=elements)


def parse_windows_list(text: str, *, skip: str) -> list[DeskRow]:
    try:
        raw = json.loads(text)
    except json.JSONDecodeError:
        return []
    if not isinstance(raw, list):
        return []
    rows: list[DeskRow] = []
    for row in raw:
        if not isinstance(row, dict):
            continue
        title = row["title"] if "title" in row and isinstance(row["title"], str) else ""
        shell = row["shell"] is True if "shell" in row else False
        if shell or not title or title == skip:
            continue
        token = row["id"] if "id" in row and isinstance(row["id"], str) else ""
        if any(title == seen.name for seen in rows):
            continue
        rows.append(DeskRow(kind="window", name=title, token=token or title))
        if len(rows) >= 6:
            break
    return rows


def parse_page(text: str) -> PageSnap:
    title = "云端浏览器"
    body_lines: list[str] = []
    elements: list[DeskRow] = []
    for index, line in enumerate(text.splitlines()):
        if index == 0 and line.startswith("Page:"):
            title = line[5:].strip() or title
            continue
        row = _tagged_row(line, "[ref=", _PAGE_PASSIVE)
        if row is not None:
            elements.append(row)
            continue
        body_lines.append(line)
    body = "\n".join(body_lines).strip()
    return PageSnap(title=clip_text(title, 120), body=body, elements=elements)


def parse_screen(text: str) -> tuple[int, int]:
    try:
        raw = json.loads(text)
    except json.JSONDecodeError:
        return 1280, 800
    if not isinstance(raw, dict):
        return 1280, 800
    width = raw["width"] if "width" in raw and isinstance(raw["width"], int) else 1280
    height = raw["height"] if "height" in raw and isinstance(raw["height"], int) else 800
    return width, height


def desk_keys(key: str) -> str:
    parts = [part.strip() for part in key.split("+") if part.strip()]
    if not parts or len(parts) > 4:
        return ""
    names: list[str] = []
    for part in parts:
        name = _desk_key(part)
        if not name:
            return ""
        names.append(name)
    return "+".join(names)


def _app_ok(app: str) -> bool:
    if not app or len(app) > 64:
        return False
    for char in app:
        if char.isascii() and (char.isalnum() or char in "._-"):
            continue
        return False
    return app[0].isalnum()


def _tagged_row(line: str, marker: str, passive: frozenset[str]) -> DeskRow | None:
    start = line.find(marker)
    if start < 0:
        return None
    end = line.find("]", start)
    if end < 0:
        return None
    token = line[start + len(marker) : end]
    if not token:
        return None
    stripped = line[:start].strip().lstrip("- ").strip()
    kind, name = _kind_name(stripped)
    if not kind or kind.lower() in passive:
        return None
    return DeskRow(kind=kind, name=name or token, token=token)


def _kind_name(stripped: str) -> tuple[str, str]:
    if '"' not in stripped:
        return stripped.split()[0] if stripped else "", ""
    kind, _, rest = stripped.partition('"')
    name, _, _ = rest.rpartition('"')
    return kind.strip(), name.strip()


def _box(row: object) -> BoxRef | None:
    if not isinstance(row, dict):
        return None
    box_id = row["box_id"] if "box_id" in row and isinstance(row["box_id"], str) else ""
    if not box_id:
        box_id = row["id"] if "id" in row and isinstance(row["id"], str) else ""
    name = row["name"] if "name" in row and isinstance(row["name"], str) else ""
    state = row["state"] if "state" in row and isinstance(row["state"], str) else ""
    if not box_id:
        return None
    # 兼容 BOX_STATE_RUNNING 这类枚举名，统一成小写。
    return BoxRef(box_id=box_id, name=name or box_id, state=state.rsplit("_", 1)[-1].lower())


def _by_id(boxes: list[BoxRef], box_id: str) -> BoxRef | None:
    for box in boxes:
        if box.box_id == box_id:
            return box
    return None


def _png(images: list[bytes], cap: int) -> bytes | None:
    for raw in images:
        if len(raw) <= cap and raw.startswith(_PNG):
            return raw
    return None


_DESK_MODS = {
    "ctrl": "ctrl",
    "control": "ctrl",
    "alt": "alt",
    "shift": "shift",
    "cmd": "super",
    "command": "super",
    "win": "super",
    "meta": "super",
    "super": "super",
}
_DESK_NAMED = {
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
    "pageup": "Page_Up",
    "pagedown": "Page_Down",
    "up": "Up",
    "down": "Down",
    "left": "Left",
    "right": "Right",
}


def _desk_key(part: str) -> str:
    lower = part.lower()
    if lower in _DESK_MODS:
        return _DESK_MODS[lower]
    if lower in _DESK_NAMED:
        return _DESK_NAMED[lower]
    if len(part) == 1 and part.isascii() and part.isalnum():
        return part.lower()
    if len(lower) >= 2 and lower[0] == "f" and lower[1:].isdigit() and 1 <= int(lower[1:]) <= 12:
        return lower.upper()
    return ""
