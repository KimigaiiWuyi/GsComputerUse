"""检查按钮。只询问后端是否可用，不下载虚拟机，也不安装 CUA Driver。"""

from __future__ import annotations

import json

import httpx

from gsuid_core.utils.plugins_config.button_action import ButtonReport, register_config_button

from ..gscomputeruse_config.host import seat_limit, host_budget, screen_size, shot_byte_cap
from ..gscomputeruse_desktop.cua import SubprocessRunner, daemon_ready, install_hint
from ..gscomputeruse_desktop.mcp import McpSession
from ..gscomputeruse_config.config import (
    PROBE_ENDPOINT,
    cua_binary,
    key_problem,
    simular_key,
    simular_url,
    instabox_key,
    instabox_base,
    configured_max,
    desired_backend,
)
from ..gscomputeruse_desktop.errors import ComputerNotReady
from ..gscomputeruse_desktop.simular import parse_boxes
from ..gscomputeruse_desktop.transport import HttpxTransport


def probe_fields() -> dict[str, str]:
    budget = host_budget()
    backend = desired_backend()
    width, height = screen_size(budget)
    profile = "同时 1 个桌面" if budget.tight else "InstaBox 最多 3 个，Simular 和 CUA Driver 仍是 1 个"
    return {
        "host": f"{budget.system}-{budget.cpu}",
        "cores": str(budget.cores),
        "memory": str(budget.ram_mib),
        "profile": profile,
        "backend": backend,
        "screen": f"{width}×{height}",
        "shot": f"{shot_byte_cap(budget) // 1024} KiB",
        "seats": str(seat_limit(budget, backend, configured_max())),
        "ready": "",
    }


def spec_note(text: str) -> str:
    pairs = _spec_pairs(text)
    if pairs is None:
        return "没有读到规格列表。插件仍会先申请 2 核 4 GiB，账号不允许就改用默认规格。"
    if (2, 4096) in pairs:
        return "账号允许 2 核 4 GiB。插件会申请这个规格。"
    return "账号没有 2 核 4 GiB。插件会使用账号的默认规格。"


async def probe_computer(report: ButtonReport) -> None:
    fields = probe_fields()
    report.update(phase="running", progress=20, message="正在检查", fields=fields)
    backend = desired_backend()
    if backend == "cua":
        ready = await _probe_cua()
    elif backend == "simular":
        ready = await _probe_simular()
    else:
        ready = await _probe_instabox()
    fields["ready"] = ready
    phase = "error" if _failed(ready) else "success"
    report.update(phase=phase, progress=100, message=ready, fields=fields)


def _failed(text: str) -> bool:
    marks = ("没有在运行", "拒绝", "还没有", "不是", "连不上", "无效", "找不到")
    return any(mark in text for mark in marks)


async def _probe_instabox() -> str:
    key = instabox_key()
    problem = key_problem(key)
    if problem:
        return problem
    base = instabox_base()
    if not base:
        return "InstaBox API 地址无效。只接受 https，或本机的 http://127.0.0.1。"
    client = httpx.AsyncClient(timeout=httpx.Timeout(8.0), follow_redirects=False, trust_env=False)
    transport = HttpxTransport(base, key, client)
    try:
        reply = await transport.send("GET", "/v1/usage", "", idempotent=False, cap=65_536)
        if reply.status == 404:
            reply = await transport.send("GET", "/v1/orgs", "", idempotent=False, cap=65_536)
    finally:
        await client.aclose()
    if reply.status == 401:
        return "InstaBox 拒绝了这把 API Key。"
    if reply.status == 0:
        return "连不上 InstaBox。"
    if reply.status >= 400 or reply.too_big:
        return "InstaBox 没有返回账号信息。"
    return spec_note(reply.text)


async def _probe_simular() -> str:
    key = simular_key()
    problem = key_problem(key, backend="simular")
    if problem:
        return problem
    url = simular_url()
    if not url:
        return "Simular MCP 地址无效。只接受 https，或本机的 http://127.0.0.1。"
    client = httpx.AsyncClient(timeout=httpx.Timeout(15.0, read=60.0), follow_redirects=False, trust_env=False)
    session = McpSession(client, url, key)
    try:
        reply = await session.tool("box_list", {})
    finally:
        await client.aclose()
    if reply.failed:
        return reply.text
    boxes = parse_boxes(reply.text)
    if not boxes:
        return "连接成功，但账号下还没有云端桌面。去 box.simular.ai 开一台。"
    running = sum(1 for box in boxes if box.state == "running")
    return f"连接成功。账号下有 {len(boxes)} 台云端桌面，{running} 台在跑。同一时间所有用户共用一台。"


async def _probe_cua() -> str:
    binary = cua_binary()
    if not binary:
        return "CUA Driver 的程序路径无效。"
    try:
        result = await SubprocessRunner().run([binary, "status"], timeout=8)
    except ComputerNotReady as exc:
        return exc.message
    if daemon_ready(result.code, result.out):
        return "CUA Driver 正在运行。"
    detail = result.out.strip() or result.err.strip()
    hint = install_hint(host_budget().system)
    head = "CUA Driver 没有在运行。"
    if detail:
        return f"{head}{hint} {detail}"
    return f"{head}{hint}"


def _spec_pairs(text: str) -> list[tuple[int, int]] | None:
    try:
        raw = json.loads(text)
    except json.JSONDecodeError:
        return None
    rows = _spec_rows(raw)
    if rows is None:
        return None
    pairs: list[tuple[int, int]] = []
    for row in rows:
        pair = _pair(row)
        if pair is not None:
            pairs.append(pair)
    return pairs


def _spec_rows(raw: object) -> list[object] | None:
    if not isinstance(raw, dict):
        return None
    for key in ("allowedSpecs", "specs"):
        if key in raw and isinstance(raw[key], list):
            return [item for item in raw[key]]
    return None


def _pair(row: object) -> tuple[int, int] | None:
    if not isinstance(row, dict):
        return None
    cpus = row["vcpus"] if "vcpus" in row and isinstance(row["vcpus"], int) else None
    memory = None
    if "memMiB" in row and isinstance(row["memMiB"], int):
        memory = row["memMiB"]
    elif "memoryMiB" in row and isinstance(row["memoryMiB"], int):
        memory = row["memoryMiB"]
    if cpus is None or memory is None:
        return None
    return cpus, memory


register_config_button(PROBE_ENDPOINT, probe_computer)
