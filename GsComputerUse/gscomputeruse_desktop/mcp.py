"""Simular box 的 MCP 会话。一条 Streamable HTTP 连接，密钥只放请求头。"""

from __future__ import annotations

import json
import base64
import asyncio
from typing import Protocol
from dataclasses import dataclass
from collections.abc import Mapping

import httpx

_MCP_VERSION = "2025-06-18"
_REPLY_CAP = 300_000


@dataclass(frozen=True)
class ToolReply:
    text: str
    images: list[bytes]
    failed: bool


@dataclass(frozen=True)
class _Reply:
    status: int
    text: str
    session: str
    too_big: bool = False


class Session(Protocol):
    async def tool(self, name: str, arguments: dict[str, object]) -> ToolReply: ...

    async def aclose(self) -> None: ...


class McpSession:
    def __init__(self, client: httpx.AsyncClient, url: str, key: str, *, owns: bool = True) -> None:
        self._client = client
        self._url = url
        self._key = key
        self._owns = owns
        self._session = ""
        self._ready = False
        self._next = 0
        self._lock = asyncio.Lock()

    async def aclose(self) -> None:
        if self._owns:
            await self._client.aclose()

    async def tool(self, name: str, arguments: dict[str, object]) -> ToolReply:
        async with self._lock:
            problem = await self._ensure()
            if problem:
                return ToolReply(problem, [], True)
            reply = await self._call(name, arguments)
            if reply.status in {400, 404}:
                # 会话过期，重连一次再发同一个工具调用。
                self._ready = False
                self._session = ""
                problem = await self._ensure()
                if problem:
                    return ToolReply(problem, [], True)
                reply = await self._call(name, arguments)
            if reply.status != 200 or reply.too_big:
                return ToolReply(self._fail(reply), [], True)
            parsed = parse_tool(reply.text)
            if parsed is None:
                return ToolReply("Simular 返回了不认识的数据。", [], True)
            return parsed

    async def _ensure(self) -> str:
        if self._ready:
            return ""
        self._next += 1
        body = {
            "jsonrpc": "2.0",
            "id": self._next,
            "method": "initialize",
            "params": {
                "protocolVersion": _MCP_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "gsuid-gs-computer-use", "version": "0.1"},
            },
        }
        reply = await self._send(body)
        if reply.status != 200 or reply.too_big:
            return self._fail(reply)
        self._session = reply.session
        self._ready = True
        self._next += 1
        notice: dict[str, object] = {"jsonrpc": "2.0", "method": "notifications/initialized"}
        await self._send(notice)
        return ""

    async def _call(self, name: str, arguments: dict[str, object]) -> _Reply:
        self._next += 1
        body = {
            "jsonrpc": "2.0",
            "id": self._next,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        }
        return await self._send(body)

    async def _send(self, body: Mapping[str, object]) -> _Reply:
        headers = {
            "Authorization": f"Bearer {self._key}",
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
            "User-Agent": "gsuid-gs-computer-use",
        }
        if "method" in body and body["method"] != "initialize":
            headers["MCP-Protocol-Version"] = _MCP_VERSION
        if self._session:
            headers["Mcp-Session-Id"] = self._session
        payload = json.dumps(body).encode("utf-8")
        try:
            async with self._client.stream("POST", self._url, headers=headers, content=payload) as response:
                reply = await _read(response, _REPLY_CAP)
        except httpx.HTTPError as exc:
            return _Reply(0, str(exc), "")
        return reply

    def _fail(self, reply: _Reply) -> str:
        if reply.status == 0:
            return "连不上 Simular box。"
        if reply.too_big:
            return "Simular 返回的数据超过本机限额，已丢弃。"
        if reply.status in {401, 403}:
            return "Simular 拒绝了这把 Key。"
        message = rpc_error(reply.text)
        if message:
            return message
        return "Simular 请求失败。"


async def _read(response: httpx.Response, cap: int) -> _Reply:
    chunks: list[bytes] = []
    size = 0
    async for chunk in response.aiter_bytes():
        size += len(chunk)
        if size > cap:
            return _Reply(response.status_code, "", "", too_big=True)
        chunks.append(chunk)
    text = b"".join(chunks).decode("utf-8", errors="replace")
    session = response.headers["mcp-session-id"] if "mcp-session-id" in response.headers else ""
    return _Reply(response.status_code, text, session, too_big=False)


def rpc_node(text: str) -> dict[str, object] | None:
    raw = _load(text)
    if raw is None:
        raw = _sse_load(text)
    if not isinstance(raw, dict):
        return None
    return raw


def rpc_error(text: str) -> str:
    raw = rpc_node(text)
    if raw is None or "error" not in raw or not isinstance(raw["error"], dict):
        return ""
    node = raw["error"]
    if "message" in node and isinstance(node["message"], str):
        return node["message"]
    return ""


def parse_tool(text: str) -> ToolReply | None:
    raw = rpc_node(text)
    if raw is None:
        return None
    rpc_message = rpc_error(text)
    if rpc_message:
        return ToolReply(rpc_message, [], True)
    node = raw["result"] if "result" in raw and isinstance(raw["result"], dict) else raw
    if not isinstance(node, dict):
        return None
    failed = node["isError"] is True if "isError" in node else False
    texts: list[str] = []
    images: list[bytes] = []
    items = node["content"] if "content" in node and isinstance(node["content"], list) else []
    for item in items:
        if not isinstance(item, dict) or "type" not in item:
            continue
        if item["type"] == "text" and "text" in item and isinstance(item["text"], str):
            texts.append(item["text"])
        if item["type"] == "image" and "data" in item and isinstance(item["data"], str):
            decoded = _image(item["data"])
            if decoded is not None:
                images.append(decoded)
    return ToolReply("\n".join(texts), images, failed)


def _image(data: str) -> bytes | None:
    compact = "".join(data.split())
    if len(compact) > _REPLY_CAP * 2:
        return None
    try:
        return base64.b64decode(compact, validate=True)
    except ValueError:
        return None


def _load(text: str) -> object:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def _sse_load(text: str) -> object:
    # 一条 SSE 里可能夹着通知，只有带 id 的才是响应。
    chosen: dict[str, object] | None = None
    fallback = ""
    for line in text.splitlines():
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        fallback = payload
        node = _load(payload)
        if isinstance(node, dict) and "id" in node:
            chosen = node
    if chosen is not None:
        return chosen
    return _load(fallback) if fallback else None
