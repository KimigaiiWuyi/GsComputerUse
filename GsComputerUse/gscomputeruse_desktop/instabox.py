"""InstaBox 云端桌面。本机只发 HTTP，不在 2 核 2 GiB 上开虚拟机。"""

from __future__ import annotations

import json
import time
import asyncio
from typing import TypedDict
from pathlib import Path
from dataclasses import dataclass
from urllib.parse import quote, urlencode

from .wire import (
    WINDOW_SCRIPT,
    WindowRow,
    key_parts,
    decode_png,
    app_name_ok,
    key_command,
    shell_quote,
    open_command,
    type_command,
    with_display,
    click_command,
    parse_windows,
    launch_command,
    scroll_command,
)
from .lease import Lease, LeaseBook, machine_name
from .errors import ComputerNotReady
from .snapshot import Mark, MarkBook, clip_text, scroll_direction
from .transport import ApiReply, Transport
from ..gscomputeruse_config.host import HostBudget, screen_size, shot_byte_cap

_JSON_CAP = 65_536
_SHOT_CAP = 1_500_000


class MachineMeta(TypedDict):
    gs_plugin: str
    gs_tenant: str


class CreatePayload(TypedDict, total=False):
    name: str
    templateId: str
    metadata: MachineMeta
    vault: bool
    vcpus: int
    memMiB: int
    autoSuspendAfterSeconds: int


class ExecPayload(TypedDict, total=False):
    cmd: str
    stdin: str
    timeoutSeconds: int


@dataclass(frozen=True)
class MachineRef:
    machine_id: str
    name: str
    state: str
    tenant: str


@dataclass(frozen=True)
class ExecResult:
    ok: bool
    stdout: str
    stderr: str


@dataclass(frozen=True)
class ApiError:
    code: str
    message: str


@dataclass(frozen=True)
class DesktopStatus:
    installed: bool
    running: bool
    installing: bool
    failed: bool
    log: str


@dataclass(frozen=True)
class OperationRef:
    op_id: str
    machine_id: str
    state: str


def create_payload(name: str, *, small: bool) -> CreatePayload:
    payload: CreatePayload = {
        "name": name,
        "templateId": "agent-browser",
        "metadata": {"gs_plugin": "GsComputerUse", "gs_tenant": name},
        "vault": False,
    }
    if small:
        payload["vcpus"] = 2
        payload["memMiB"] = 4096
        payload["autoSuspendAfterSeconds"] = 300
    return payload


def should_soften(status: int, code: str, *, small: bool) -> bool:
    if not small:
        return False
    if code == "spec_not_allowed":
        return True
    return status == 403


class InstaboxClient:
    def __init__(
        self,
        transport: Transport,
        book: LeaseBook,
        *,
        poll_seconds: float = 0.4,
        op_seconds: float = 90,
    ) -> None:
        self._transport = transport
        self._book = book
        self._poll = poll_seconds
        self._op_seconds = op_seconds
        self._locks: dict[str, asyncio.Lock] = {}

    async def ensure(self, tenant: str) -> MachineRef:
        lock = self._locks.setdefault(tenant, asyncio.Lock())
        async with lock:
            return await self._ensure(tenant)

    async def start_desktop(self, machine_id: str, width: int, height: int) -> None:
        status = await self._desktop("start", machine_id, width, height)
        if not status.installed:
            if not status.installing:
                await self._desktop("install", machine_id, width, height)
            await self._until_installed(machine_id)
            status = await self._desktop("start", machine_id, width, height)
        if status.failed:
            raise ComputerNotReady(status.log or "桌面没有装好。")
        if not status.running:
            raise ComputerNotReady("InstaBox 桌面没有启动。")

    async def exec(self, machine_id: str, command: str, *, stdin: str = "", cap: int = _JSON_CAP) -> ExecResult:
        body: ExecPayload = {"cmd": command, "timeoutSeconds": 25}
        if stdin:
            body["stdin"] = stdin
        reply = await self._send(
            "POST",
            f"/v1/machines/{quote(machine_id, safe='')}/exec",
            json.dumps(body),
            idempotent=False,
            cap=cap,
        )
        self._raise_transport(reply)
        if reply.status >= 400:
            raise ComputerNotReady(self._fail(reply))
        parsed = parse_exec(reply.text)
        if parsed is None:
            raise ComputerNotReady("InstaBox 命令没有返回结果。")
        if not parsed.ok:
            detail = parsed.stderr.strip() or parsed.stdout.strip() or "InstaBox 命令失败。"
            raise ComputerNotReady(clip_text(detail, 400))
        return parsed

    async def suspend(self, machine_id: str) -> str:
        reply = await self._send(
            "POST",
            f"/v1/machines/{quote(machine_id, safe='')}/suspend",
            "{}",
            idempotent=True,
            cap=_JSON_CAP,
        )
        if reply.status in {200, 202, 204, 404, 409}:
            return ""
        if reply.status == 0:
            return "连不上 InstaBox，云端机器会按账号的空闲时间暂停。"
        return self._fail(reply)

    async def _ensure(self, tenant: str) -> MachineRef:
        name = machine_name(tenant)
        current = self._book.get(tenant)
        if current is not None and current["backend"] == "instabox":
            loaded = await self._get(current["machine_id"])
            if loaded is not None:
                woken = await self._wake(loaded)
                self._remember(tenant, woken)
                return woken
        found = await self._find(name)
        if found is not None:
            woken = await self._wake(found)
            self._remember(tenant, woken)
            return woken
        created = await self._create(name)
        woken = await self._wake(created)
        self._remember(tenant, woken)
        return woken

    def _remember(self, tenant: str, machine: MachineRef) -> None:
        row: Lease = {
            "tenant": tenant,
            "backend": "instabox",
            "machine_id": machine.machine_id,
            "name": machine.name,
        }
        self._book.put(row)

    async def _create(self, name: str) -> MachineRef:
        small = True
        last = ApiReply(0, "")
        for _ in range(2):
            payload = create_payload(name, small=small)
            reply = await self._send("POST", "/v1/machines", json.dumps(payload), idempotent=True, cap=_JSON_CAP)
            last = reply
            if reply.status == 409:
                found = await self._get(name)
                if found is not None:
                    return found
            code = api_error(reply.text).code
            if reply.status < 400 and not reply.too_big and reply.status != 0:
                return await self._finish_create(reply.text, name)
            if should_soften(reply.status, code, small=small):
                small = False
                continue
            break
        raise ComputerNotReady(self._fail(last))

    async def _finish_create(self, text: str, name: str) -> MachineRef:
        operation = parse_operation(text)
        if operation is None or not operation.machine_id:
            raise ComputerNotReady("InstaBox 没有返回机器编号。")
        if operation.state not in {"succeeded", "success"}:
            await self._wait(operation.op_id)
        loaded = await self._get(operation.machine_id)
        if loaded is None:
            loaded = await self._get(name)
        if loaded is None:
            raise ComputerNotReady("InstaBox 创建后找不到机器。")
        return loaded

    async def _wake(self, machine: MachineRef) -> MachineRef:
        current = machine
        if current.state in {"suspended", "stopped"}:
            reply = await self._send(
                "POST",
                f"/v1/machines/{quote(current.machine_id, safe='')}/resume",
                "{}",
                idempotent=True,
                cap=_JSON_CAP,
            )
            self._raise_transport(reply)
            if reply.status >= 400:
                raise ComputerNotReady(self._fail(reply))
            operation = parse_operation(reply.text)
            if operation is not None and operation.state not in {"succeeded", "success"}:
                await self._wait(operation.op_id)
            loaded = await self._get(current.machine_id)
            if loaded is None:
                raise ComputerNotReady("InstaBox 机器不在了。")
            current = loaded
        if current.state != "running":
            current = await self._until_running(current.machine_id)
        return current

    async def _until_running(self, machine_id: str) -> MachineRef:
        deadline = time.monotonic() + self._op_seconds
        while True:
            loaded = await self._get(machine_id)
            if loaded is not None and loaded.state == "running":
                return loaded
            if loaded is not None and loaded.state == "error":
                raise ComputerNotReady("InstaBox 机器进入错误状态。")
            if time.monotonic() >= deadline:
                raise ComputerNotReady("InstaBox 机器没有在限定时间内启动。")
            await asyncio.sleep(self._poll)

    async def _until_installed(self, machine_id: str) -> None:
        deadline = time.monotonic() + self._op_seconds
        while True:
            status = await self._desktop("status", machine_id, 0, 0)
            if status.failed:
                raise ComputerNotReady(status.log or "InstaBox 桌面安装失败。")
            if status.installed:
                return
            if time.monotonic() >= deadline:
                raise ComputerNotReady("InstaBox 桌面没有在限定时间内装好。")
            await asyncio.sleep(self._poll)

    async def _desktop(self, action: str, machine_id: str, width: int, height: int) -> DesktopStatus:
        body: dict[str, str | int] = {"action": action}
        if width and height:
            body["width"] = width
            body["height"] = height
        reply = await self._send(
            "POST",
            f"/v1/machines/{quote(machine_id, safe='')}/desktop",
            json.dumps(body),
            idempotent=True,
            cap=_JSON_CAP,
        )
        self._raise_transport(reply)
        if reply.status >= 400:
            raise ComputerNotReady(self._fail(reply))
        status = parse_desktop(reply.text)
        if status is None:
            raise ComputerNotReady("InstaBox 没有返回桌面状态。")
        return status

    async def _find(self, name: str) -> MachineRef | None:
        query = urlencode([("metadata", f"gs_tenant:{name}"), ("limit", "20")])
        reply = await self._send("GET", f"/v1/machines?{query}", "", idempotent=False, cap=_JSON_CAP)
        if reply.status >= 400 or reply.status == 0 or reply.too_big:
            return None
        for machine in parse_machines(reply.text):
            if machine.name == name and machine.tenant in {"", name}:
                return machine
        return None

    async def _get(self, machine_id: str) -> MachineRef | None:
        reply = await self._send(
            "GET",
            f"/v1/machines/{quote(machine_id, safe='')}",
            "",
            idempotent=False,
            cap=_JSON_CAP,
        )
        if reply.status == 404:
            return None
        if reply.status >= 400 or reply.status == 0 or reply.too_big:
            raise ComputerNotReady(self._fail(reply))
        return parse_machine(reply.text)

    async def _wait(self, op_id: str) -> None:
        if not op_id:
            return
        deadline = time.monotonic() + self._op_seconds
        delay = self._poll
        while True:
            reply = await self._send(
                "GET",
                f"/v1/operations/{quote(op_id, safe='')}",
                "",
                idempotent=False,
                cap=_JSON_CAP,
            )
            self._raise_transport(reply)
            operation = parse_operation(reply.text)
            if operation is not None and operation.state in {"succeeded", "success"}:
                return
            if operation is not None and operation.state == "failed":
                raise ComputerNotReady(api_error(reply.text).message or "InstaBox 操作失败。")
            if time.monotonic() >= deadline:
                raise ComputerNotReady("InstaBox 操作超时。")
            await asyncio.sleep(delay)
            delay = min(max(delay * 1.5, 0.05), 1.0)

    async def _send(self, method: str, path: str, payload: str, *, idempotent: bool, cap: int) -> ApiReply:
        return await self._transport.send(method, path, payload, idempotent=idempotent, cap=cap)

    def _raise_transport(self, reply: ApiReply) -> None:
        if reply.too_big:
            raise ComputerNotReady("InstaBox 返回的数据超过本机限额，已丢弃。")
        if reply.status == 0:
            raise ComputerNotReady("连不上 InstaBox。")

    def _fail(self, reply: ApiReply) -> str:
        if reply.status == 0:
            return "连不上 InstaBox。"
        if reply.too_big:
            return "InstaBox 返回的数据超过本机限额，已丢弃。"
        message = api_error(reply.text).message
        if message:
            return message
        return "InstaBox 请求失败。"


class InstaboxDesktop:
    def __init__(self, client: InstaboxClient, machine: MachineRef, budget: HostBudget) -> None:
        self._client = client
        self._machine = machine
        self._budget = budget
        self._marks = MarkBook()

    async def look(self) -> str:
        result = await self._client.exec(self._machine.machine_id, WINDOW_SCRIPT)
        rows = parse_windows(result.stdout)
        width, height = screen_size(self._budget)
        marks = [_window_mark(row) for row in rows]
        body = "没有可见窗口。" if not rows else f"可见窗口 {len(rows)} 个。点击 e 编号会点在窗口中间。"
        return self._marks.replace(marks, title=f"云端桌面 {width}×{height}", body=body)

    async def click(self, target: str, *, foreground: bool) -> str:
        del foreground
        found = self._marks.resolve(target)
        if isinstance(found, str):
            return found
        command = click_command(found.x, found.y, button="left", repeat=1)
        await self._client.exec(self._machine.machine_id, with_display(command))
        return await self.look()

    async def type_text(self, text: str) -> str:
        await self._client.exec(self._machine.machine_id, with_display(type_command()), stdin=text)
        return await self.look()

    async def press(self, key: str, *, foreground: bool) -> str:
        del foreground
        parts = key_parts(key)
        if parts is None:
            return "不认识这个按键。"
        await self._client.exec(self._machine.machine_id, with_display(key_command(parts)))
        return await self.look()

    async def scroll(self, direction: str) -> str:
        folded = scroll_direction(direction)
        if not folded:
            return "方向用 up、down、left、right。"
        await self._client.exec(self._machine.machine_id, with_display(scroll_command(folded)))
        return await self.look()

    async def open_url(self, url: str) -> str:
        await self._client.exec(self._machine.machine_id, with_display(open_command(url)))
        return await self.look()

    async def launch(self, app: str) -> str:
        if not app_name_ok(app, allow_space=False):
            return "云端桌面请填桌面文件名，例如 firefox，不要带空格。"
        await self._client.exec(self._machine.machine_id, with_display(launch_command(app)))
        return await self.look()

    async def shot(self, path: str) -> str:
        remote = "/tmp/gscu-shot.png"
        quoted = shell_quote(remote)
        machine = self._machine.machine_id
        capture = (
            f"if command -v scrot >/dev/null; then scrot -o {quoted}; "
            f"elif command -v import >/dev/null; then import -window root png:{quoted}; "
            "else echo 'no screenshot tool' >&2; exit 127; fi"
        )
        cap = shot_byte_cap(self._budget)
        raw: bytes | None = None
        await self._client.exec(machine, with_display(capture))
        counted = await self._client.exec(machine, f"wc -c < {quoted}")
        size_text = counted.stdout.strip()
        if size_text.isdigit() and int(size_text) <= cap:
            encoded = await self._client.exec(machine, f"base64 -w 0 {quoted}", cap=_SHOT_CAP)
            raw = decode_png(encoded.stdout, cap)
        await self._client.exec(machine, f"rm -f {quoted}")
        if not size_text.isdigit() or int(size_text) > cap:
            return f"截图超过 {cap // 1024} KiB，已丢弃。"
        if raw is None:
            return "截图无法保存，已丢弃。"
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(raw)
        return str(target)

    async def release(self) -> str:
        note = await self._client.suspend(self._machine.machine_id)
        if note:
            return note
        return "云端桌面已暂停。"


def _window_mark(row: WindowRow) -> Mark:
    return Mark(kind="window", name=row.name, token="", x=row.x, y=row.y)


def api_error(text: str) -> ApiError:
    try:
        raw = json.loads(text)
    except json.JSONDecodeError:
        return ApiError("", clip_text(text.strip(), 400))
    if not isinstance(raw, dict):
        return ApiError("", "")
    node = raw["error"] if "error" in raw and isinstance(raw["error"], dict) else raw
    if not isinstance(node, dict):
        return ApiError("", "")
    code = node["code"] if "code" in node and isinstance(node["code"], str) else ""
    message = node["message"] if "message" in node and isinstance(node["message"], str) else ""
    return ApiError(code, message)


def parse_exec(text: str) -> ExecResult | None:
    try:
        raw = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(raw, dict):
        return None
    stdout = raw["stdout"] if "stdout" in raw and isinstance(raw["stdout"], str) else ""
    stderr = raw["stderr"] if "stderr" in raw and isinstance(raw["stderr"], str) else ""
    code = raw["exitCode"] if "exitCode" in raw and isinstance(raw["exitCode"], int) else 0
    return ExecResult(ok=code == 0, stdout=stdout, stderr=stderr)


def parse_operation(text: str) -> OperationRef | None:
    try:
        raw = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(raw, dict):
        return None
    op_id = raw["id"] if "id" in raw and isinstance(raw["id"], str) else ""
    machine_id = raw["machineId"] if "machineId" in raw and isinstance(raw["machineId"], str) else ""
    state = raw["state"] if "state" in raw and isinstance(raw["state"], str) else ""
    if not op_id and not machine_id:
        return None
    return OperationRef(op_id=op_id, machine_id=machine_id, state=state)


def parse_machine(text: str) -> MachineRef | None:
    try:
        raw = json.loads(text)
    except json.JSONDecodeError:
        return None
    return _machine(raw)


def parse_machines(text: str) -> list[MachineRef]:
    try:
        raw = json.loads(text)
    except json.JSONDecodeError:
        return []
    if not isinstance(raw, dict) or "machines" not in raw or not isinstance(raw["machines"], list):
        return []
    found: list[MachineRef] = []
    for row in raw["machines"]:
        parsed = _machine(row)
        if parsed is not None:
            found.append(parsed)
    return found


def parse_desktop(text: str) -> DesktopStatus | None:
    try:
        raw = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(raw, dict):
        return None
    return DesktopStatus(
        installed=_flag(raw, "installed"),
        running=_flag(raw, "running"),
        installing=_flag(raw, "installing"),
        failed=_flag(raw, "failed"),
        log=_text(raw, "log"),
    )


def _machine(raw: object) -> MachineRef | None:
    if not isinstance(raw, dict):
        return None
    machine_id = raw["id"] if "id" in raw and isinstance(raw["id"], str) else ""
    name = raw["name"] if "name" in raw and isinstance(raw["name"], str) else ""
    state = raw["state"] if "state" in raw and isinstance(raw["state"], str) else ""
    tenant = ""
    if "metadata" in raw and isinstance(raw["metadata"], dict):
        meta = raw["metadata"]
        if "gs_tenant" in meta and isinstance(meta["gs_tenant"], str):
            tenant = meta["gs_tenant"]
    if not machine_id:
        return None
    return MachineRef(machine_id=machine_id, name=name, state=state, tenant=tenant)


def _flag(raw: object, key: str) -> bool:
    if not isinstance(raw, dict) or key not in raw:
        return False
    return raw[key] is True


def _text(raw: object, key: str) -> str:
    if not isinstance(raw, dict) or key not in raw:
        return ""
    value = raw[key]
    if isinstance(value, str):
        return value
    return ""
