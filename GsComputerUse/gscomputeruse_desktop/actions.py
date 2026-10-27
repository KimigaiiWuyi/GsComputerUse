"""工具真正调用的动作。不该发出去的内容在占用桌面之前就拒绝。"""

from __future__ import annotations

from collections.abc import Callable, Awaitable

from .pool import SessionPool
from .driver import DesktopDriver
from .errors import ComputerNotReady
from .snapshot import length_problem, secret_problem

_URL_OK = ("http://", "https://")


def url_problem(url: str) -> str:
    text = url.strip()
    if text.startswith(_URL_OK):
        return ""
    return "只打开 http 或 https 网址。"


async def look(pool: SessionPool, key: str) -> str:
    return await _run(pool, key, lambda driver: driver.look())


async def click(pool: SessionPool, key: str, target: str, *, foreground: bool) -> str:
    return await _run(pool, key, lambda driver: driver.click(target, foreground=foreground))


async def type_text(pool: SessionPool, key: str, text: str, *, user_type: str) -> str:
    problem = secret_problem(user_type, text)
    if problem:
        return problem
    problem = length_problem(text)
    if problem:
        return problem
    return await _run(pool, key, lambda driver: driver.type_text(text))


async def press(pool: SessionPool, key: str, hotkey: str, *, foreground: bool) -> str:
    return await _run(pool, key, lambda driver: driver.press(hotkey, foreground=foreground))


async def scroll(pool: SessionPool, key: str, direction: str) -> str:
    return await _run(pool, key, lambda driver: driver.scroll(direction))


async def open_url(pool: SessionPool, key: str, url: str) -> str:
    problem = url_problem(url)
    if problem:
        return problem
    return await _run(pool, key, lambda driver: driver.open_url(url.strip()))


async def launch(pool: SessionPool, key: str, app: str) -> str:
    return await _run(pool, key, lambda driver: driver.launch(app.strip()))


async def shot(pool: SessionPool, key: str, path: str) -> str:
    return await _run(pool, key, lambda driver: driver.shot(path))


async def release(pool: SessionPool, key: str) -> str:
    session = await pool.acquire(key)
    async with session.lock:
        pool.mark(session)
        driver = session.driver
        if driver is None:
            raise ComputerNotReady("桌面没有打开。")
        text = await driver.release()
    await pool.forget(key)
    return text or "桌面已释放。"


async def _run(pool: SessionPool, key: str, work: Callable[[DesktopDriver], Awaitable[str]]) -> str:
    session = await pool.acquire(key)
    async with session.lock:
        pool.mark(session)
        driver = session.driver
        if driver is None:
            raise ComputerNotReady("桌面没有打开。")
        return await work(driver)
