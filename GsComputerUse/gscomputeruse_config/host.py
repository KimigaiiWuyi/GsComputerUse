"""本机系统、内存和 2 核 2 GiB 限额。"""

from __future__ import annotations

import os
import sys
import ctypes
import platform
from ctypes import wintypes
from pathlib import Path
from dataclasses import dataclass


@dataclass(frozen=True)
class HostBudget:
    system: str
    cpu: str
    cores: int
    ram_mib: int

    @property
    def tight(self) -> bool:
        return self.cores <= 2 or self.ram_mib <= 2560


class _MemoryStatus(ctypes.Structure):
    _fields_ = (
        ("dwLength", wintypes.DWORD),
        ("dwMemoryLoad", wintypes.DWORD),
        ("ullTotalPhys", ctypes.c_ulonglong),
        ("ullAvailPhys", ctypes.c_ulonglong),
        ("ullTotalPageFile", ctypes.c_ulonglong),
        ("ullAvailPageFile", ctypes.c_ulonglong),
        ("ullTotalVirtual", ctypes.c_ulonglong),
        ("ullAvailVirtual", ctypes.c_ulonglong),
        ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
    )


def system_token(system: str) -> str:
    folded = system.lower()
    if folded.startswith("win"):
        return "windows"
    if folded in {"darwin", "macos"}:
        return "macos"
    if folded == "linux":
        return "linux"
    raise RuntimeError(f"不支持的系统：{system}")


def machine_token(machine: str) -> str:
    folded = machine.lower()
    if folded in {"amd64", "x86_64", "x64"}:
        return "x86_64"
    if folded in {"arm64", "aarch64"}:
        return "aarch64"
    raise RuntimeError(f"不支持的 CPU：{machine}")


def platform_label(system: str, machine: str) -> str:
    return f"{system_token(system)}-{machine_token(machine)}"


def host_label() -> str:
    return platform_label(sys.platform, platform.machine())


def budget_for(system: str, cpu: str, cores: int, ram_mib: int) -> HostBudget:
    counted = cores if cores > 0 else 1
    memory = ram_mib if ram_mib > 0 else 0
    return HostBudget(system=system_token(system), cpu=machine_token(cpu), cores=counted, ram_mib=memory)


def host_budget() -> HostBudget:
    cores = os.cpu_count() or 1
    return budget_for(sys.platform, platform.machine(), cores, _ram_mib())


def screen_size(budget: HostBudget) -> tuple[int, int]:
    if budget.tight:
        return 1024, 600
    return 1280, 800


def shot_byte_cap(budget: HostBudget) -> int:
    if budget.tight:
        return 512 * 1024
    return 1536 * 1024


def seat_limit(budget: HostBudget, backend: str, asked: int) -> int:
    if backend == "cua" or budget.tight:
        return 1
    if asked < 1:
        return 1
    if asked > 3:
        return 3
    return asked


def display_note(system: str) -> str:
    if system == "linux":
        return "Linux 上的 CUA Driver 需要图形会话。没有图形会话时它看不到桌面。InstaBox 不需要。"
    if system == "macos":
        return "macOS 上的 CUA Driver 需要辅助功能和屏幕录制权限。"
    if system == "windows":
        return "Windows 上的 CUA Driver 需要已登录的桌面会话。"
    return ""


def budget_report(
    budget: HostBudget,
    *,
    backend: str,
    seats: int,
    key_state: str,
    display: str,
    lease: str,
    active: int,
) -> str:
    width, height = screen_size(budget)
    cap_kib = shot_byte_cap(budget) // 1024
    if budget.tight:
        limit = "本机是 2 核或内存不超过 2560 MiB，同时只开 1 个桌面。配置写再多也按 1 个。"
    else:
        limit = "本机高于 2 核 2 GiB。InstaBox 最多同时开 3 个桌面。CUA Driver 仍是 1 个。"
    if backend == "cua":
        where = "CUA Driver 操作本机已有桌面，不另开虚拟机。同一时间只有一个用户。"
    else:
        where = "InstaBox 没有 2 GiB 的规格。插件先申请 2 核 4 GiB，账号不允许就用该账号的默认规格。桌面在云端。"
    lines = [
        f"本机 {budget.system}-{budget.cpu}，{budget.cores} 核，{budget.ram_mib} MiB。",
        limit,
        f"后端是 {backend}。同时桌面 {seats} 个，当前占用 {active} 个。屏幕 {width}×{height}，截图上限 {cap_kib} KiB。",
        where,
        f"InstaBox API Key：{key_state}。",
        f"你的桌面：{lease}。",
    ]
    if display:
        lines.append(display)
    return "\n".join(lines)


def _ram_mib() -> int:
    if sys.platform == "win32":
        return _windows_ram_mib()
    if sys.platform == "darwin":
        return _darwin_ram_mib()
    return _linux_ram_mib()


def _windows_ram_mib() -> int:
    # 读不到就按小机器限额，避免在 2 GiB 上同时开多个桌面。
    status = _MemoryStatus()
    status.dwLength = ctypes.sizeof(status)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    found = kernel.GlobalMemoryStatusEx(ctypes.byref(status))
    if found == 0:
        return 0
    return int(status.ullTotalPhys // (1024 * 1024))


def _darwin_ram_mib() -> int:
    page = os.sysconf("SC_PAGE_SIZE")
    pages = os.sysconf("SC_PHYS_PAGES")
    if not isinstance(page, int) or not isinstance(pages, int):
        return 0
    if page <= 0 or pages <= 0:
        return 0
    return (page * pages) // (1024 * 1024)


def _linux_ram_mib() -> int:
    path = Path("/proc/meminfo")
    if not path.is_file():
        return 0
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.startswith("MemTotal:"):
            continue
        parts = line.split()
        if len(parts) >= 2 and parts[1].isdigit():
            return int(parts[1]) // 1024
        return 0
    return 0
