"""租约只记本机。云端机器用哈希名，不把用户 ID 写进标签。"""

from __future__ import annotations

import json
import hashlib
from typing import TypedDict
from pathlib import Path


class Lease(TypedDict):
    tenant: str
    backend: str
    machine_id: str
    name: str


class LeaseBook:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._rows: dict[str, Lease] = {}

    def load(self) -> None:
        self._rows = {}
        if not self._path.is_file():
            return
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return
        if not isinstance(raw, dict):
            return
        rows = raw["tenants"] if "tenants" in raw else None
        if not isinstance(rows, dict):
            return
        for tenant, item in rows.items():
            if not isinstance(tenant, str) or not isinstance(item, dict):
                continue
            fields: dict[str, str] = {}
            for key in ("backend", "machine_id", "name"):
                if key not in item:
                    continue
                value = item[key]
                if isinstance(value, str):
                    fields[key] = value
            parsed = _lease(tenant, fields)
            if parsed is not None:
                self._rows[tenant] = parsed

    def get(self, tenant: str) -> Lease | None:
        if tenant in self._rows:
            return self._rows[tenant]
        return None

    def put(self, row: Lease) -> None:
        # 先读盘再写，另一个用户刚记下的租约不会被这次覆盖。
        self.load()
        self._rows[row["tenant"]] = row
        self._path.parent.mkdir(parents=True, exist_ok=True)
        body = {"tenants": self._rows}
        self._path.write_text(json.dumps(body, ensure_ascii=False, indent=2), encoding="utf-8")


def machine_name(tenant: str) -> str:
    digest = hashlib.sha256(tenant.encode("utf-8")).hexdigest()[:12]
    return f"gscu-{digest}"


def _lease(tenant: str, item: dict[str, str]) -> Lease | None:
    if "backend" not in item or "machine_id" not in item or "name" not in item:
        return None
    backend = item["backend"]
    machine_id = item["machine_id"]
    name = item["name"]
    if not backend or not machine_id or not name:
        return None
    return {"tenant": tenant, "backend": backend, "machine_id": machine_id, "name": name}
