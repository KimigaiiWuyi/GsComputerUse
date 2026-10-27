"""按配置选择 InstaBox 或 CUA Driver。名额由本机规格决定，不由云端机器数量决定。"""

from __future__ import annotations

from .cua import CuaBackend, SubprocessRunner
from .lease import LeaseBook
from .driver import DesktopDriver, ComputerBackend
from .errors import ComputerNotReady
from .instabox import InstaboxClient, InstaboxDesktop
from .transport import HttpxTransport
from ..gscomputeruse_config.host import HostBudget, screen_size
from ..gscomputeruse_config.config import (
    ROOT,
    cua_binary,
    key_problem,
    instabox_key,
    instabox_base,
)


class RoutingBackend:
    def __init__(self, backend_name: str, budget: HostBudget) -> None:
        self._name = backend_name
        self._budget = budget
        self._transport: HttpxTransport | None = None

    async def open_desktop(self, tenant: str) -> DesktopDriver:
        if self._name == "cua":
            binary = cua_binary()
            if not binary:
                raise ComputerNotReady("CUA Driver 的程序路径无效。")
            backend = CuaBackend(binary, SubprocessRunner(), self._budget)
            return await backend.open_desktop(tenant)
        return await self._open_instabox(tenant)

    async def stop(self) -> None:
        transport = self._transport
        self._transport = None
        if transport is not None:
            await transport.aclose()

    async def _open_instabox(self, tenant: str) -> InstaboxDesktop:
        key = instabox_key()
        problem = key_problem(key)
        if problem:
            raise ComputerNotReady(problem)
        base = instabox_base()
        if not base:
            raise ComputerNotReady("InstaBox API 地址只接受 https，或本机的 http://127.0.0.1。")
        if self._transport is None:
            self._transport = HttpxTransport(base, key)
        book = LeaseBook(ROOT / "leases.json")
        book.load()
        client = InstaboxClient(self._transport, book)
        machine = await client.ensure(tenant)
        width, height = screen_size(self._budget)
        await client.start_desktop(machine.machine_id, width, height)
        return InstaboxDesktop(client, machine, self._budget)


def make_backend(name: str, budget: HostBudget) -> ComputerBackend:
    return RoutingBackend(name, budget)
