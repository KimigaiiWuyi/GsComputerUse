"""启动时同步检查按钮。退出时暂停还开着的桌面。"""

from __future__ import annotations

from gsuid_core.server import on_core_start, on_core_shutdown
from gsuid_core.utils.plugins_config.models import ButtonState, GsButtonConfig

from ..gscomputeruse_probe.probe import probe_fields
from ..gscomputeruse_config.config import CONFIG
from ..gscomputeruse_desktop.service import shutdown_pool


def sync_probe_button() -> None:
    item = CONFIG.get_config("probe")
    if not isinstance(item, GsButtonConfig):
        return
    fields = probe_fields()
    if item.data.phase == "running":
        CONFIG.set_button_state(
            "probe",
            ButtonState(phase="error", progress=item.data.progress, message="任务已中断", fields=fields),
        )
        return
    if item.data.phase in {"success", "error"}:
        return
    message = item.data.message or "还没有检查。"
    if item.data.phase == "idle" and item.data.message == message and dict(item.data.fields) == fields:
        return
    CONFIG.set_button_state("probe", ButtonState(phase="idle", progress=0, message=message, fields=fields))


@on_core_start
async def _sync_computer_probe() -> None:
    sync_probe_button()


@on_core_shutdown
async def _stop_computer() -> None:
    await shutdown_pool()
