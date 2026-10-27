"""进程内的桌面池。空闲时暂停云端机器，HTTP 客户端留到进程退出。"""

from __future__ import annotations

from .pool import SessionPool
from .runtime import RoutingBackend
from ..gscomputeruse_config.host import seat_limit, host_budget
from ..gscomputeruse_config.config import idle_limit, configured_max, desired_backend

_pool: SessionPool | None = None


def live_limit() -> int:
    return seat_limit(host_budget(), desired_backend(), configured_max())


def get_pool() -> SessionPool:
    global _pool
    if _pool is None:
        backend = RoutingBackend(desired_backend(), host_budget())
        _pool = SessionPool(backend, live_limit, idle_limit)
    return _pool


def active_count() -> int:
    if _pool is None:
        return 0
    return _pool.count()


async def shutdown_pool() -> None:
    global _pool
    pool = _pool
    _pool = None
    if pool is not None:
        await pool.shutdown()
