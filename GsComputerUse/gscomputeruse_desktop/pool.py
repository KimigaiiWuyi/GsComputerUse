"""按用户租桌面。打开云端桌面可能要几十秒，不能占着锁挡住别的租户。"""

from __future__ import annotations

import time
import asyncio
from dataclasses import dataclass
from collections.abc import Callable

from .driver import DesktopDriver, ComputerBackend
from .errors import ComputerBusy, ComputerNotReady


@dataclass
class LiveSession:
    key: str
    driver: DesktopDriver | None
    lock: asyncio.Lock
    touched: float
    ready: asyncio.Event
    fail: str


class SessionPool:
    def __init__(
        self,
        backend: ComputerBackend,
        max_live: Callable[[], int],
        idle_seconds: Callable[[], int],
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._backend = backend
        self._max_live = max_live
        self._idle_seconds = idle_seconds
        self._now = clock or time.monotonic
        self._sessions: dict[str, LiveSession] = {}
        self._lock = asyncio.Lock()
        self._sweeper: asyncio.Task[None] | None = None
        self._closed = False

    def count(self) -> int:
        return len(self._sessions)

    async def acquire(self, key: str) -> LiveSession:
        self._ensure_sweeper()
        plan: tuple[LiveSession, bool] | None = None
        while plan is None:
            doomed: LiveSession | None = None
            async with self._lock:
                current = self._sessions.get(key)
                if current is not None:
                    plan = (current, False)
                elif len(self._sessions) >= self._max_live():
                    doomed = self._pop_oldest_free()
                    if doomed is None:
                        raise ComputerBusy("电脑正忙，请稍后再试。")
                else:
                    plan = (self._reserve(key), True)
            if doomed is not None:
                await self._release(doomed)
        session, create = plan
        if not create:
            await session.ready.wait()
            if session.driver is None:
                raise ComputerNotReady(session.fail or "桌面没有打开。")
            session.touched = self._now()
            return session
        try:
            driver = await self._backend.open_desktop(key)
        except ComputerNotReady as exc:
            session.fail = exc.message
            await self._discard(key, session)
            raise
        except ComputerBusy as exc:
            session.fail = exc.message
            await self._discard(key, session)
            raise
        except BaseException:
            # 取消也要清掉占位，否则名额会一直被空会话占着。
            await self._discard(key, session)
            raise
        session.driver = driver
        session.ready.set()
        return session

    def mark(self, session: LiveSession) -> None:
        session.touched = self._now()

    async def forget(self, key: str) -> None:
        async with self._lock:
            self._sessions.pop(key, None)

    async def sweep(self) -> None:
        stale: list[LiveSession] = []
        limit = self._idle_seconds()
        async with self._lock:
            now = self._now()
            for key, session in list(self._sessions.items()):
                if not _idle(session, now, limit):
                    continue
                self._sessions.pop(key, None)
                stale.append(session)
        for session in stale:
            await self._release(session)

    async def shutdown(self) -> None:
        self._closed = True
        sweeper = self._sweeper
        self._sweeper = None
        if sweeper is not None:
            sweeper.cancel()
            await asyncio.gather(sweeper, return_exceptions=True)
        await self._close_all()
        await self._backend.stop()

    def _ensure_sweeper(self) -> None:
        if self._closed or self._sweeper is not None:
            return
        self._sweeper = asyncio.create_task(self._sweep_loop())

    async def _sweep_loop(self) -> None:
        while not self._closed:
            await asyncio.sleep(30)
            await self.sweep()

    def _reserve(self, key: str) -> LiveSession:
        session = LiveSession(
            key=key,
            driver=None,
            lock=asyncio.Lock(),
            touched=self._now(),
            ready=asyncio.Event(),
            fail="",
        )
        self._sessions[key] = session
        return session

    def _pop_oldest_free(self) -> LiveSession | None:
        oldest: LiveSession | None = None
        for session in self._sessions.values():
            if not _free(session):
                continue
            if oldest is None or session.touched < oldest.touched:
                oldest = session
        if oldest is None:
            return None
        self._sessions.pop(oldest.key, None)
        return oldest

    async def _discard(self, key: str, session: LiveSession) -> None:
        async with self._lock:
            current = self._sessions.get(key)
            if current is session:
                self._sessions.pop(key, None)
        session.ready.set()

    async def _release(self, session: LiveSession) -> None:
        if not session.ready.is_set():
            return
        driver = session.driver
        if driver is None:
            return
        async with session.lock:
            await driver.release()

    async def _close_all(self) -> None:
        async with self._lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
        for session in sessions:
            await session.ready.wait()
            await self._release(session)


def _free(session: LiveSession) -> bool:
    return session.ready.is_set() and session.driver is not None and not session.lock.locked()


def _idle(session: LiveSession, now: float, limit: int) -> bool:
    return _free(session) and now - session.touched >= limit
