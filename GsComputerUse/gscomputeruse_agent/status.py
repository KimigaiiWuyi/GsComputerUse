"""查看本机限额、后端和你自己的桌面。客人也可以看。"""

from __future__ import annotations

from gsuid_core.sv import SV
from gsuid_core.bot import Bot
from gsuid_core.models import Event

from ..gscomputeruse_config.host import seat_limit, host_budget, display_note, budget_report
from ..gscomputeruse_desktop.who import user_key
from ..gscomputeruse_config.config import (
    ROOT,
    key_state,
    simular_key,
    instabox_key,
    configured_max,
    desired_backend,
)
from ..gscomputeruse_desktop.lease import LeaseBook
from ..gscomputeruse_desktop.service import active_count

sv = SV("电脑状态")


def status_text(ev: Event | None) -> str:
    budget = host_budget()
    backend = desired_backend()
    book = LeaseBook(ROOT / "leases.json")
    book.load()
    row = book.get(user_key(ev))
    lease = row["name"] if row is not None else "还没有"
    if backend == "simular":
        label = "Simular Key"
        state = key_state(simular_key(), backend="simular")
    elif backend == "cua":
        label = "CUA Driver"
        state = "不需要密钥"
    else:
        label = "InstaBox API Key"
        state = key_state(instabox_key())
    return budget_report(
        budget,
        backend=backend,
        seats=seat_limit(budget, backend, configured_max()),
        key_state=state,
        key_label=label,
        display=display_note(budget.system),
        lease=lease,
        active=active_count(),
    )


@sv.on_command(
    "状态",
    block=True,
    to_ai="查看电脑操作是否就绪，包括后端、本机规格和名额。",
    covers=["查看电脑操作是否可用"],
    aliases=["电脑·查看状态"],
)
async def computer_status(bot: Bot, ev: Event) -> None:
    """回复本机限额和这个用户的桌面名。"""
    await bot.send(status_text(ev))
