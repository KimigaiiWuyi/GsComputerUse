"""租户、限额、InstaBox 和 CUA Driver。"""

from __future__ import annotations

import json
import asyncio
from pathlib import Path

import httpx
import pytest
from fastapi.routing import APIRoute

from gsuid_core.models import Event
from gsuid_core.webconsole.app_app import app
from gsuid_core.webconsole.web_api import require_admin
from gsuid_core.utils.plugins_config.models import GsStrConfig, GsBoolConfig
from GsComputerUse.gscomputeruse_agent.tools import allow_computer
from GsComputerUse.gscomputeruse_config.host import (
    budget_for,
    seat_limit,
    host_budget,
    screen_size,
    display_note,
    system_token,
    budget_report,
    machine_token,
    shot_byte_cap,
)
from GsComputerUse.gscomputeruse_desktop.cua import (
    CuaBackend,
    CuaDesktop,
    ProcResult,
    cua_argv,
    daemon_ready,
    session_label,
)
from GsComputerUse.gscomputeruse_desktop.mcp import ToolReply, McpSession
from GsComputerUse.gscomputeruse_desktop.who import user_key
from GsComputerUse.gscomputeruse_probe.probe import spec_note
from GsComputerUse.gscomputeruse_desktop.pool import SessionPool
from GsComputerUse.gscomputeruse_desktop.wire import (
    decode_png,
    trim_shots,
    shell_quote,
    open_command,
    parse_windows,
)
from GsComputerUse.gscomputeruse_config.config import (
    CONFIG,
    PROBE_ENDPOINT,
    key_state,
    clean_base,
    key_problem,
    instabox_key,
)
from GsComputerUse.gscomputeruse_desktop.lease import Lease, LeaseBook, machine_name
from GsComputerUse.gscomputeruse_desktop.errors import ComputerNotReady
from GsComputerUse.gscomputeruse_desktop.actions import release, open_url, type_text
from GsComputerUse.gscomputeruse_desktop.simular import (
    BoxRef,
    SimularClient,
    SimularDesktop,
    desk_keys,
    parse_desk,
    parse_page,
    parse_boxes,
    parse_windows_list,
)
from GsComputerUse.gscomputeruse_desktop.instabox import (
    MachineRef,
    InstaboxClient,
    InstaboxDesktop,
)
from GsComputerUse.gscomputeruse_desktop.snapshot import MarkBook
from GsComputerUse.gscomputeruse_desktop.transport import ApiReply, HttpxTransport

_PNG = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="


class _Desk:
    def __init__(self) -> None:
        self.typed: list[str] = []
        self.urls: list[str] = []
        self.releases = 0

    async def look(self) -> str:
        return "本机桌面\n--- 可操作 ---"

    async def click(self, target: str, *, foreground: bool) -> str:
        return await self.look()

    async def type_text(self, text: str) -> str:
        self.typed.append(text)
        return await self.look()

    async def press(self, key: str, *, foreground: bool) -> str:
        return await self.look()

    async def scroll(self, direction: str) -> str:
        return await self.look()

    async def open_url(self, url: str) -> str:
        self.urls.append(url)
        return await self.look()

    async def launch(self, app: str) -> str:
        return await self.look()

    async def shot(self, path: str) -> str:
        return path

    async def release(self) -> str:
        self.releases += 1
        return "已释放"


class _Backend:
    def __init__(self, desk: _Desk) -> None:
        self.desk = desk
        self.opens = 0
        self.stopped = False
        self.block = ""
        self.entered = asyncio.Event()
        self.gate = asyncio.Event()
        self.gate.set()

    async def open_desktop(self, tenant: str) -> _Desk:
        self.opens += 1
        if tenant == self.block:
            self.entered.set()
            await self.gate.wait()
        return self.desk

    async def stop(self) -> None:
        self.stopped = True


class _Script:
    def __init__(self, replies: list[ApiReply]) -> None:
        self.replies = list(replies)
        self.calls: list[tuple[str, str, str, bool]] = []

    async def send(self, method: str, path: str, payload: str, *, idempotent: bool, cap: int) -> ApiReply:
        self.calls.append((method, path, payload, idempotent))
        if not self.replies:
            return ApiReply(500, '{"error":{"code":"empty","message":"no reply"}}')
        return self.replies.pop(0)


class _Runner:
    def __init__(self, replies: list[ProcResult] | None = None) -> None:
        self.replies = list(replies or [])
        self.argv: list[list[str]] = []
        self.by_tool: dict[str, ProcResult] = {}

    async def run(self, argv: list[str], *, timeout: float) -> ProcResult:
        self.argv.append(list(argv))
        if self.replies:
            return self.replies.pop(0)
        tool = argv[2] if len(argv) > 2 else argv[1]
        if tool in self.by_tool:
            return self.by_tool[tool]
        return ProcResult(0, "", "")


class _Sim:
    def __init__(self, replies: list[ToolReply]) -> None:
        self.replies = list(replies)
        self.calls: list[tuple[str, dict[str, object]]] = []

    async def tool(self, name: str, arguments: dict[str, object]) -> ToolReply:
        self.calls.append((name, arguments))
        if not self.replies:
            return ToolReply("no reply", [], True)
        return self.replies.pop(0)

    async def aclose(self) -> None:
        return None


def _pool(
    desk: _Desk, *, live: int = 2, idle: int = 30, now: dict[str, float] | None = None
) -> tuple[SessionPool, _Backend]:
    backend = _Backend(desk)
    clock = (lambda: now["t"]) if now is not None else None
    pool = SessionPool(backend, lambda: live, lambda: idle, clock)
    return pool, backend


def _client(script: _Script, path: Path) -> InstaboxClient:
    return InstaboxClient(script, LeaseBook(path), poll_seconds=0, op_seconds=2)


def test_tenant_key_isolates_users() -> None:
    group = Event(bot_id="bot", user_id="u1", user_type="group")
    private = Event(bot_id="bot", user_id="u1", user_type="direct")
    other = Event(bot_id="bot", user_id="u2", user_type="group")
    assert user_key(group) == user_key(private)
    assert user_key(group) != user_key(other)
    assert user_key(None) == "local"
    assert user_key(Event(user_id="")) == "local"


def test_master_switch() -> None:
    item = CONFIG.get_config("master_only")
    assert isinstance(item, GsBoolConfig)
    previous = item.data
    owner = Event(user_id="owner", user_pm=0)
    guest = Event(user_id="guest", user_pm=6)
    CONFIG.set_config("master_only", True)
    try:
        assert allow_computer(owner)[0] is True
        allowed, message = allow_computer(guest)
        assert allowed is False
        assert "主人" in message
        CONFIG.set_config("master_only", False)
        assert allow_computer(guest)[0] is True
    finally:
        CONFIG.set_config("master_only", previous)


def test_pool_lets_another_tenant_act_while_a_desktop_is_opening() -> None:
    async def _run() -> _Backend:
        desk = _Desk()
        pool, backend = _pool(desk, live=2)
        backend.block = "a"
        backend.gate.clear()
        try:
            await pool.acquire("b")
            opening = asyncio.create_task(pool.acquire("a"))
            await asyncio.wait_for(backend.entered.wait(), 1)
            await asyncio.wait_for(pool.acquire("b"), 1)
            backend.gate.set()
            await opening
            return backend
        finally:
            if not backend.gate.is_set():
                backend.gate.set()
            await pool.shutdown()

    backend = asyncio.run(_run())
    assert backend.stopped


def test_idle_sweep_releases_the_desktop() -> None:
    async def _run() -> tuple[int, bool]:
        now = {"t": 0.0}
        desk = _Desk()
        pool, backend = _pool(desk, idle=30, now=now)
        try:
            await pool.acquire("u")
            now["t"] = 30
            await pool.sweep()
            assert desk.releases == 1
            assert backend.stopped is False
        finally:
            await pool.shutdown()
        return desk.releases, backend.stopped

    releases, stopped = asyncio.run(_run())
    assert releases == 1
    assert stopped is True


def test_release_opens_a_fresh_desktop_next_time() -> None:
    async def _run() -> tuple[int, int]:
        desk = _Desk()
        pool, backend = _pool(desk)
        try:
            await pool.acquire("u")
            await release(pool, "u")
            await pool.acquire("u")
            return backend.opens, desk.releases
        finally:
            await pool.shutdown()

    opens, releases = asyncio.run(_run())
    assert opens == 2
    assert releases == 1


def test_seat_screen_and_shot_follow_the_host() -> None:
    tight = budget_for("win32", "AMD64", 2, 8192)
    assert seat_limit(tight, "instabox", 3) == 1
    assert seat_limit(tight, "cua", 3) == 1
    assert screen_size(tight) == (1024, 600)
    assert shot_byte_cap(tight) == 512 * 1024
    small_ram = budget_for("linux", "x86_64", 4, 2048)
    assert small_ram.tight
    assert seat_limit(small_ram, "instabox", 3) == 1
    roomy = budget_for("linux", "aarch64", 8, 16384)
    assert seat_limit(roomy, "instabox", 9) == 3
    assert seat_limit(roomy, "instabox", 2) == 2
    assert seat_limit(roomy, "instabox", 0) == 1
    assert seat_limit(roomy, "cua", 3) == 1
    assert screen_size(roomy) == (1280, 800)
    assert shot_byte_cap(roomy) == 1536 * 1024


def test_this_machine_reports_a_budget() -> None:
    budget = host_budget()
    assert budget.cores >= 1
    assert budget.ram_mib > 0
    assert budget.system in {"windows", "macos", "linux"}


def test_unknown_cpu_is_refused() -> None:
    with pytest.raises(RuntimeError):
        machine_token("sparc")
    with pytest.raises(RuntimeError):
        system_token("solaris")
    assert machine_token("AMD64") == "x86_64"
    assert system_token("win32") == "windows"
    assert system_token("darwin") == "macos"


def test_group_chat_does_not_type_a_secret() -> None:
    async def _run() -> tuple[str, str, int, list[str]]:
        desk = _Desk()
        pool, backend = _pool(desk)
        try:
            group = await type_text(pool, "u", "abc12345", user_type="group")
            opened = backend.opens
            direct = await type_text(pool, "u", "abc12345", user_type="direct")
            return group, direct, opened, list(desk.typed)
        finally:
            await pool.shutdown()

    group, direct, opened, typed = asyncio.run(_run())
    assert "群聊" in group
    assert opened == 0
    assert typed == ["abc12345"]
    assert "可操作" in direct


def test_long_typing_is_refused() -> None:
    async def _run() -> int:
        desk = _Desk()
        pool, backend = _pool(desk)
        try:
            text = await type_text(pool, "u", "a" * 401, user_type="direct")
            assert "400" in text
            return backend.opens
        finally:
            await pool.shutdown()

    assert asyncio.run(_run()) == 0


def test_only_http_urls_open() -> None:
    async def _run() -> tuple[str, list[str], int]:
        desk = _Desk()
        pool, backend = _pool(desk)
        try:
            refused = await open_url(pool, "u", "file:///tmp/secret")
            opened = backend.opens
            await open_url(pool, "u", "https://example.test/a")
            return refused, list(desk.urls), opened
        finally:
            await pool.shutdown()

    refused, urls, opened = asyncio.run(_run())
    assert "http" in refused
    assert opened == 0
    assert urls == ["https://example.test/a"]


def test_shell_quote_wraps_metacharacters() -> None:
    assert shell_quote("a'b;c") == "'a'\\''b;c'"
    command = open_command("http://a/b;c")
    assert "'http://a/b;c'" in command


def test_instabox_create_drops_size_when_the_plan_refuses(tmp_path: Path) -> None:
    script = _Script(
        [
            ApiReply(200, '{"machines":[]}'),
            ApiReply(400, '{"error":{"code":"spec_not_allowed","message":"plan"}}'),
            ApiReply(202, '{"id":"op1","machineId":"m1","state":"queued"}'),
            ApiReply(200, '{"id":"op1","machineId":"m1","state":"succeeded"}'),
            ApiReply(200, '{"id":"m1","name":"gscu-box","state":"running","metadata":{"gs_tenant":"gscu-box"}}'),
        ]
    )

    async def _run() -> list[tuple[str, str, str, bool]]:
        await _client(script, tmp_path / "leases.json").ensure("bot:u")
        return script.calls

    calls = asyncio.run(_run())
    creates = [call for call in calls if call[0] == "POST" and call[1] == "/v1/machines"]
    assert len(creates) == 2
    first = _object(creates[0][2])
    second = _object(creates[1][2])
    assert first["vcpus"] == 2
    assert first["memMiB"] == 4096
    assert first["autoSuspendAfterSeconds"] == 300
    assert first["vault"] is False
    assert first["templateId"] == "agent-browser"
    meta = first["metadata"]
    assert isinstance(meta, dict)
    assert meta["gs_plugin"] == "GsComputerUse"
    assert "gs_tenant" in meta
    assert "vcpus" not in second
    assert "memMiB" not in second
    assert "autoSuspendAfterSeconds" not in second
    assert creates[0][3] is True
    assert creates[1][3] is True


def test_instabox_auth_failure_does_not_create_again(tmp_path: Path) -> None:
    script = _Script(
        [
            ApiReply(200, '{"machines":[]}'),
            ApiReply(401, '{"error":{"code":"unauthorized","message":"bad key"}}'),
        ]
    )

    async def _run() -> None:
        with pytest.raises(ComputerNotReady):
            await _client(script, tmp_path / "leases.json").ensure("bot:u")

    asyncio.run(_run())
    posts = [call for call in script.calls if call[0] == "POST"]
    assert len(posts) == 1


def test_instabox_types_through_stdin(tmp_path: Path) -> None:
    secret = "你好$(rm)"
    window = json.dumps({"stdout": "1\thello\t0\t0\t10\t10", "stderr": "", "exitCode": 0})
    script = _Script(
        [
            ApiReply(200, json.dumps({"stdout": "", "stderr": "", "exitCode": 0})),
            ApiReply(200, window),
        ]
    )

    async def _run() -> str:
        client = _client(script, tmp_path / "leases.json")
        desk = InstaboxDesktop(
            client, MachineRef("m1", "gscu-box", "running", "gscu-box"), budget_for("linux", "x86_64", 2, 2048)
        )
        return await desk.type_text(secret)

    result = asyncio.run(_run())
    payload = _object(script.calls[0][2])
    command = payload["cmd"]
    assert isinstance(command, str)
    assert "xdotool type" in command
    assert "DISPLAY=:1" in command
    assert secret not in command
    assert payload["stdin"] == secret
    assert script.calls[0][3] is False
    assert "e1" in result


def test_instabox_wakes_the_tenants_own_machine(tmp_path: Path) -> None:
    tenant = "bot:mine"
    name = machine_name(tenant)
    other = {"id": "m-other", "name": "gscu-ffffffffffff", "state": "running", "metadata": {"gs_tenant": "else"}}
    mine = {"id": "m-mine", "name": name, "state": "suspended", "metadata": {"gs_tenant": name}}
    running = {"id": "m-mine", "name": name, "state": "running", "metadata": {"gs_tenant": name}}
    script = _Script(
        [
            ApiReply(200, json.dumps({"machines": [other, mine]})),
            ApiReply(202, json.dumps({"id": "op9", "machineId": "m-mine", "state": "queued"})),
            ApiReply(200, json.dumps({"id": "op9", "machineId": "m-mine", "state": "succeeded"})),
            ApiReply(200, json.dumps(running)),
        ]
    )

    async def _run() -> str:
        found = await _client(script, tmp_path / "leases.json").ensure(tenant)
        return found.machine_id

    assert asyncio.run(_run()) == "m-mine"
    created = [call for call in script.calls if call[0] == "POST" and call[1] == "/v1/machines"]
    assert created == []
    assert any(call[1].endswith("/resume") for call in script.calls)
    assert all("m-other" not in call[1] for call in script.calls)


def test_cloud_app_name_rejects_spaces(tmp_path: Path) -> None:
    script = _Script([])

    async def _run() -> tuple[str, int]:
        client = _client(script, tmp_path / "leases.json")
        budget = budget_for("linux", "x86_64", 2, 2048)
        desk = InstaboxDesktop(client, MachineRef("m1", "gscu-box", "running", "gscu-box"), budget)
        return await desk.launch("Visual Studio"), len(script.calls)

    text, calls = asyncio.run(_run())
    assert "空格" in text
    assert calls == 0


def test_shot_files_keep_only_eight() -> None:
    names = [f"{index:02d}.png" for index in range(10)]
    assert trim_shots(names, 8) == ["00.png", "01.png"]
    assert trim_shots(names[:8], 8) == []


def test_decode_png_drops_oversized_bytes() -> None:
    raw = decode_png(_PNG, 1000)
    assert raw is not None
    assert raw.startswith(b"\x89PNG\r\n\x1a\n")
    assert decode_png(_PNG, 8) is None
    assert decode_png("@@@", 1000) is None


def test_window_list_caps_at_twelve() -> None:
    lines = [f"{index}\twin {index}\t10\t20\t30\t40" for index in range(13)]
    lines.append("junk")
    rows = parse_windows("\n".join(lines))
    assert len(rows) == 12
    assert rows[0].x == 25
    assert rows[0].y == 40
    book = MarkBook()
    book.replace([], title="桌面", body="空")
    missing = book.resolve("e3")
    assert isinstance(missing, str)
    assert "e3" in missing


def test_cua_look_uses_element_tokens() -> None:
    windows = json.dumps({"windows": [{"window_id": 3, "title": "记事本", "z_index": 2}]})
    elements = json.dumps({"elements": [{"element_token": "tok-1", "role": "button", "label": "加"}]})
    runner = _Runner()
    runner.by_tool["list_windows"] = ProcResult(0, windows, "")
    runner.by_tool["get_window_state"] = ProcResult(0, elements, "")
    budget = budget_for("linux", "x86_64", 2, 2048)

    async def _run() -> list[list[str]]:
        desk = CuaDesktop(runner, "cua-driver", session_label("bot:u"), budget)
        looked = await desk.look()
        assert "e1" in looked
        assert "加" in looked
        await desk.click("e1", foreground=False)
        await desk.click("e1", foreground=True)
        await desk.type_text("你好")
        return runner.argv

    argv = asyncio.run(_run())
    assert argv[0][:2] == ["cua-driver", "call"]
    assert cua_argv("cua-driver", "click", "{}") == ["cua-driver", "call", "click", "{}"]
    click = _object(argv[2][3])
    assert click["element_token"] == "tok-1"
    assert click["delivery_mode"] == "background"
    again = _object(argv[5][3])
    assert again["delivery_mode"] == "foreground"
    assert len(argv[8]) == 4
    assert "你好" in argv[8][3]


def test_cua_refuses_when_the_daemon_is_down() -> None:
    runner = _Runner([ProcResult(1, "not running", "")])
    backend = CuaBackend("cua-driver", runner, budget_for("linux", "x86_64", 2, 2048))

    async def _run() -> None:
        with pytest.raises(ComputerNotReady):
            await backend.open_desktop("bot:u")

    asyncio.run(_run())
    assert runner.argv == [["cua-driver", "status"]]
    assert daemon_ready(0, "Cua Driver daemon is running")
    assert daemon_ready(0, "daemon is not running") is False


def test_leases_do_not_cross_tenants(tmp_path: Path) -> None:
    path = tmp_path / "leases.json"
    book = LeaseBook(path)
    first: Lease = {"tenant": "a", "backend": "instabox", "machine_id": "m-a", "name": "gscu-a"}
    second: Lease = {"tenant": "b", "backend": "instabox", "machine_id": "m-b", "name": "gscu-b"}
    book.put(first)
    book.put(second)
    loaded = LeaseBook(path)
    loaded.load()
    left = loaded.get("a")
    right = loaded.get("b")
    assert left is not None and right is not None
    assert left["machine_id"] == "m-a"
    assert right["machine_id"] == "m-b"


def test_corrupt_lease_file_starts_empty(tmp_path: Path) -> None:
    path = tmp_path / "leases.json"
    path.write_text("{", encoding="utf-8")
    book = LeaseBook(path)
    book.load()
    assert book.get("a") is None


def test_machine_name_hides_the_user_id() -> None:
    left = machine_name("bot:alice-secret")
    right = machine_name("bot:bob")
    assert left.startswith("gscu-")
    assert left == machine_name("bot:alice-secret")
    assert "alice" not in left
    assert "secret" not in left
    assert left != right
    assert all(ch.islower() or ch.isdigit() or ch == "-" for ch in left)


def test_key_state_hides_the_secret() -> None:
    assert key_state("ibk_supersecret") == "已填写"
    assert "supersecret" not in key_state("ibk_supersecret")
    assert key_state("") != "已填写"
    assert key_state("not-a-key") != "已填写"
    assert clean_base("http://evil.example") == ""
    assert clean_base("https://api.instabox.sh") == "https://api.instabox.sh"
    assert clean_base("http://127.0.0.1:9") == "http://127.0.0.1:9"


def test_api_key_comes_from_plugin_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("INSTABOX_API_KEY", "ibk_from_env")
    item = CONFIG.get_config("instabox_api_key")
    assert isinstance(item, GsStrConfig)
    previous = item.data
    try:
        CONFIG.set_config("instabox_api_key", "ibk_from_config")
        assert instabox_key() == "ibk_from_config"
        CONFIG.set_config("instabox_api_key", "")
        assert instabox_key() == ""
    finally:
        CONFIG.set_config("instabox_api_key", previous)


def test_probe_button_requires_admin() -> None:
    matched = [route for route in app.routes if isinstance(route, APIRoute) and route.path == PROBE_ENDPOINT]
    methods = {method for route in matched for method in route.methods}
    assert methods == {"GET", "POST"}

    def calls(route: APIRoute) -> list[object]:
        found: list[object] = []
        stack = list(route.dependant.dependencies)
        while stack:
            dep = stack.pop()
            found.append(dep.call)
            stack.extend(dep.dependencies)
        return found

    for route in matched:
        assert require_admin in calls(route)


def test_budget_report_states_the_limit() -> None:
    text = budget_report(
        budget_for("linux", "x86_64", 2, 2048),
        backend="instabox",
        seats=1,
        key_state="已填写",
        display=display_note("linux"),
        lease="还没有",
        active=0,
    )
    assert "2048" in text
    assert "2560" in text
    assert "1024" in text
    assert "512" in text
    assert "已填写" in text
    assert "ibk_" not in text
    assert "图形会话" in text
    assert "InstaBox" in display_note("linux")
    assert "桌面会话" in display_note("windows")
    assert "辅助功能" in display_note("macos")
    assert "屏幕录制" in display_note("macos")


def test_spec_note_reads_only_listed_sizes() -> None:
    assert "允许 2 核 4 GiB" in spec_note('{"allowedSpecs":[{"vcpus":2,"memMiB":4096}]}')
    assert "默认规格" in spec_note('{"allowedSpecs":[{"vcpus":4,"memMiB":8192}]}')
    assert "没有读到" in spec_note("{}")


def test_api_key_is_not_sent_to_a_redirect() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(302, headers={"location": "https://evil.example/steal"})

    async def _run() -> tuple[int, int]:
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False)
        transport = HttpxTransport("https://api.instabox.sh", "ibk_test", client)
        try:
            first = await transport.send("POST", "/v1/machines", "{}", idempotent=True, cap=1000)
            second = await transport.send("POST", "/v1/machines/m/exec", "{}", idempotent=False, cap=1000)
            return first.status, second.status
        finally:
            await client.aclose()

    first, second = asyncio.run(_run())
    assert first == 302
    assert second == 302
    assert len(seen) == 2
    assert seen[0].headers["x-api-key"] == "ibk_test"
    assert "ibk_test" not in str(seen[0].url)
    assert "idempotency-key" in seen[0].headers
    assert "idempotency-key" not in seen[1].headers


def _object(text: str) -> dict[str, object]:
    raw = json.loads(text)
    assert isinstance(raw, dict)
    return raw


def test_simular_ensure_wakes_the_leased_box(tmp_path: Path) -> None:
    boxes = json.dumps(
        {
            "boxes": [
                {"box_id": "cb-a", "name": "main", "state": "running"},
                {"box_id": "cb-b", "name": "second", "state": "hibernated"},
            ]
        }
    )
    sim = _Sim(
        [
            ToolReply(boxes, [], False),
            ToolReply('{"state":"running"}', [], False),
        ]
    )
    path = tmp_path / "leases.json"
    book = LeaseBook(path)
    book.put({"tenant": "bot:u", "backend": "simular", "machine_id": "cb-b", "name": "second"})

    async def _run() -> str:
        found = await SimularClient(sim, book).ensure("bot:u")
        return found.box_id

    assert asyncio.run(_run()) == "cb-b"
    assert sim.calls[1] == ("box_wake", {"box": "cb-b"})
    loaded = LeaseBook(path)
    loaded.load()
    row = loaded.get("bot:u")
    assert row is not None and row["backend"] == "simular"


def test_simular_ensure_uses_the_first_box(tmp_path: Path) -> None:
    boxes = json.dumps({"boxes": [{"box_id": "cb-a", "name": "main", "state": "running"}]})
    sim = _Sim([ToolReply(boxes, [], False)])

    async def _run() -> str:
        found = await SimularClient(sim, LeaseBook(tmp_path / "leases.json")).ensure("bot:u")
        return found.box_id

    assert asyncio.run(_run()) == "cb-a"
    assert sim.calls == [("box_list", {})]


def test_simular_ensure_creates_a_box_when_the_account_has_none(tmp_path: Path) -> None:
    boxes = json.dumps({"boxes": [{"box_id": "cb-a", "name": "main", "state": "starting"}]})
    sim = _Sim(
        [
            ToolReply('{"boxes":[]}', [], False),
            ToolReply('{"ok":true}', [], False),
            ToolReply(boxes, [], False),
            ToolReply('{"ok":true}', [], False),
        ]
    )

    async def _run() -> str:
        found = await SimularClient(sim, LeaseBook(tmp_path / "leases.json")).ensure("bot:u")
        return found.box_id

    assert asyncio.run(_run()) == "cb-a"
    assert sim.calls[0] == ("box_list", {})
    assert sim.calls[1] == ("box_wake", {"box": ""})
    assert sim.calls[3] == ("box_wake", {"box": "cb-a"})


def test_simular_release_hibernates_by_box_id(tmp_path: Path) -> None:
    sim = _Sim([ToolReply('{"ok":true}', [], False)])

    async def _run() -> str:
        client = SimularClient(sim, LeaseBook(tmp_path / "leases.json"))
        desk = SimularDesktop(client, BoxRef("cb-a", "main", "running"), budget_for("linux", "x86_64", 4, 8192))
        return await desk.release()

    assert asyncio.run(_run()) == "云端桌面已休眠。"
    assert sim.calls == [("box_hibernate", {"box": "cb-a"})]


def test_simular_desktop_marks_and_clicks(tmp_path: Path) -> None:
    windows = json.dumps(
        [
            {"id": "1.7/1", "title": "Desktop", "role": "window", "shell": True},
            {"id": "1.9/1", "title": "Notes", "role": "window"},
            {"id": "1.9/9", "title": "Pad", "role": "window"},
        ]
    )
    snap = json.dumps(
        {
            "window": {"title": "Notes"},
            "text": '- window "Notes" [id=1.9/1]\n  - button "Save" [id=1.9/8]\n  - alert [id=1.9/3]\n',
        }
    )
    screen = json.dumps({"width": 1280, "height": 800})
    page = 'Page: Example (https://example.test)\n- button "Go" [ref=e7]\n- text "hi"\n'
    sim = _Sim(
        [
            ToolReply(windows, [], False),
            ToolReply(snap, [], False),
            ToolReply(screen, [], False),
            ToolReply('{"ok":true}', [], False),
            ToolReply(windows, [], False),
            ToolReply(snap, [], False),
            ToolReply(page, [], False),
            ToolReply(page, [], False),
        ]
    )

    async def _run() -> tuple[str, str, str, str]:
        client = SimularClient(sim, LeaseBook(tmp_path / "leases.json"))
        desk = SimularDesktop(client, BoxRef("cb-a", "main", "running"), budget_for("linux", "x86_64", 4, 8192))
        looked = await desk.look()
        clicked = await desk.click("e1", foreground=False)
        opened = await desk.open_url("https://example.test")
        again = await desk.click("e1", foreground=False)
        return looked, clicked, opened, again

    looked, clicked, opened, again = asyncio.run(_run())
    assert "e1" in looked and "Save" in looked
    assert "e2" in looked and "Pad" in looked  # 其他窗口进 mark
    assert "e3" not in looked  # alert 不进 mark
    assert sim.calls[3] == ("desk", {"op": "press", "id": "1.9/8"})
    assert "Go" in opened
    assert sim.calls[7] == ("browser_click", {"ref": "e7"})
    assert "e1" in again


def test_simular_shot_writes_png(tmp_path: Path) -> None:
    raw = b"\x89PNG\r\n\x1a\n" + b"0" * 64
    sim = _Sim([ToolReply("", [raw], False)])

    async def _run() -> str:
        client = SimularClient(sim, LeaseBook(tmp_path / "leases.json"))
        desk = SimularDesktop(client, BoxRef("cb-a", "main", "running"), budget_for("linux", "x86_64", 4, 8192))
        return await desk.shot(str(tmp_path / "s.png"))

    path = asyncio.run(_run())
    assert Path(path).read_bytes() == raw


def test_simular_seat_key_and_desk_keys() -> None:
    roomy = budget_for("linux", "x86_64", 8, 16384)
    assert seat_limit(roomy, "simular", 3) == 1
    assert key_problem("cb_ok", backend="simular") == ""
    assert "Simular" in key_problem("", backend="simular")
    assert "Simular" in key_problem("ibk_x", backend="simular")
    assert desk_keys("ctrl+l") == "ctrl+l"
    assert desk_keys("enter") == "Return"
    assert desk_keys("F5") == "F5"
    assert desk_keys("not a key!") == ""


def test_simular_parses_boxes_pages_and_desk() -> None:
    assert parse_boxes('{"boxes":[{"box_id":"cb-1","name":"main","state":"running"}]}') == [
        BoxRef("cb-1", "main", "running")
    ]
    assert parse_boxes('{"boxes":[{"box_id":"cb-2","name":"b","state":"BOX_STATE_RUNNING"}]}')[0].state == "running"
    assert parse_boxes('[{"id":"cb-3","name":"c","state":"hibernated"}]')[0].box_id == "cb-3"
    assert parse_boxes("junk") == []
    others = parse_windows_list(
        json.dumps(
            [
                {"id": "1.7/1", "title": "Desktop", "role": "window", "shell": True},
                {"id": "1.9/1", "title": "Notes", "role": "window"},
                {"id": "1.9/9", "title": "Pad", "role": "window"},
            ]
        ),
        skip="Notes",
    )
    assert [row.token for row in others] == ["1.9/9"]
    assert [row.name for row in others] == ["Pad"]
    page = parse_page('Page: T (https://x)\n- link "A" [ref=e3] href="/a"\n- text "hi"\n- button [ref=e9]\n')
    assert page.title == "T (https://x)"
    assert [row.token for row in page.elements] == ["e3", "e9"]
    snap = parse_desk(
        json.dumps(
            {
                "window": {"title": "W"},
                "text": '- window "W" [id=1/1]\n  - button "B" [id=1/2]\n  - text "x" [id=1/3]\n',
            }
        )
    )
    assert snap.title == "W"
    assert [row.token for row in snap.elements] == ["1/2"]
    textbox = parse_desk(json.dumps({"window": {"title": "W"}, "text": '- textbox "Addr" [id=1/7]: "youtube.com"\n'}))
    assert textbox.elements[0].token == "1/7"
    assert textbox.elements[0].name == "Addr"


def test_mcp_session_sends_bearer_and_reads_sse() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if b"initialize" in request.content:
            return httpx.Response(
                200,
                json={"jsonrpc": "2.0", "id": 1, "result": {"capabilities": {}}},
                headers={"mcp-session-id": "s1"},
            )
        if b"initialized" in request.content:
            return httpx.Response(202)
        body = (
            "event: message\n"
            'data: {"jsonrpc":"2.0","id":2,"result":{"content":[{"type":"text","text":"hi"}],"isError":false}}\n\n'
        )
        return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})

    async def _run() -> ToolReply:
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        session = McpSession(client, "https://mcp.test/mcp", "cb_test")
        try:
            return await session.tool("box_list", {})
        finally:
            await client.aclose()

    reply = asyncio.run(_run())
    assert reply.text == "hi" and reply.failed is False
    assert seen[0].headers["authorization"] == "Bearer cb_test"
    assert "cb_test" not in str(seen[0].url)
    assert seen[2].headers["mcp-session-id"] == "s1"


def test_mcp_sse_picks_the_reply_over_notifications() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if b"initialize" in request.content:
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": {}})
        if b"initialized" in request.content:
            return httpx.Response(202)
        body = (
            "event: message\n"
            'data: {"jsonrpc":"2.0","id":2,"result":{"content":[{"type":"text","text":"hi"}]}}\n\n'
            "event: message\n"
            'data: {"jsonrpc":"2.0","method":"notifications/progress","params":{"t":1}}\n\n'
        )
        return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})

    async def _run() -> ToolReply:
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        session = McpSession(client, "https://mcp.test/mcp", "cb_test")
        try:
            return await session.tool("box_list", {})
        finally:
            await client.aclose()

    reply = asyncio.run(_run())
    assert reply.text == "hi" and reply.failed is False


def test_mcp_tool_error_comes_back_as_failure() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if b"initialize" in request.content:
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": {}})
        if b"initialized" in request.content:
            return httpx.Response(202)
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": 2,
                "result": {"isError": True, "content": [{"type": "text", "text": "nope"}]},
            },
        )

    async def _run() -> ToolReply:
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        session = McpSession(client, "https://mcp.test/mcp", "cb_test")
        try:
            return await session.tool("desk", {"op": "windows"})
        finally:
            await client.aclose()

    reply = asyncio.run(_run())
    assert reply.failed and reply.text == "nope"
