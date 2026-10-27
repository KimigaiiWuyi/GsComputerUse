"""InstaBox HTTP。密钥只放在请求头里，跳转不再发出。"""

from __future__ import annotations

import uuid
from typing import Protocol
from dataclasses import dataclass

import httpx


@dataclass(frozen=True)
class ApiReply:
    status: int
    text: str
    too_big: bool = False


class Transport(Protocol):
    async def send(self, method: str, path: str, payload: str, *, idempotent: bool, cap: int) -> ApiReply: ...


class HttpxTransport:
    def __init__(self, base: str, key: str, client: httpx.AsyncClient | None = None) -> None:
        self._base = base.rstrip("/")
        self._key = key
        self._owns = client is None
        # 不读取进程环境。密钥和地址只来自插件配置。
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(30.0, read=60.0),
            follow_redirects=False,
            trust_env=False,
        )

    async def aclose(self) -> None:
        if self._owns:
            await self._client.aclose()

    async def send(self, method: str, path: str, payload: str, *, idempotent: bool, cap: int) -> ApiReply:
        headers = {
            "X-API-Key": self._key,
            "Accept": "application/json",
            "User-Agent": "gsuid-gs-computer-use",
        }
        if idempotent:
            headers["Idempotency-Key"] = uuid.uuid4().hex
        content: bytes | None = None
        if payload:
            headers["Content-Type"] = "application/json"
            content = payload.encode("utf-8")
        try:
            async with self._client.stream(method, self._base + path, headers=headers, content=content) as response:
                return await _read(response, cap)
        except httpx.HTTPError as exc:
            # 网络失败留给调用方，不在这里换规格或重试创建。
            return ApiReply(0, str(exc), too_big=False)


async def _read(response: httpx.Response, cap: int) -> ApiReply:
    chunks: list[bytes] = []
    size = 0
    async for chunk in response.aiter_bytes():
        size += len(chunk)
        if size > cap:
            return ApiReply(response.status_code, "", too_big=True)
        chunks.append(chunk)
    text = b"".join(chunks).decode("utf-8", errors="replace")
    return ApiReply(response.status_code, text, too_big=False)
