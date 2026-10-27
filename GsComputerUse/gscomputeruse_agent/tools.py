"""电脑工具只挂在 computer_agent 上。主人格通过委派使用。"""

from __future__ import annotations

from pathlib import Path
from datetime import datetime
from collections.abc import Awaitable

from pydantic_ai import RunContext

from gsuid_core.models import Event
from gsuid_core.ai_core.models import ToolContext
from gsuid_core.ai_core.register import ai_tools

from ..gscomputeruse_desktop.who import user_key, user_place
from ..gscomputeruse_desktop.wire import trim_shots
from ..gscomputeruse_config.config import ROOT, flag_bool
from ..gscomputeruse_desktop.errors import ComputerBusy, ComputerNotReady
from ..gscomputeruse_desktop.actions import (
    look,
    shot,
    click,
    press,
    launch,
    scroll,
    release,
    open_url,
    type_text,
)
from ..gscomputeruse_desktop.service import get_pool


def allow_computer(ev: Event | None) -> tuple[bool, str]:
    if flag_bool("master_only", True) and (ev is None or ev.user_pm != 0):
        return False, "电脑只对主人开放。主人可以在插件配置里关掉「仅主人可用」。"
    return True, ""


async def _guard(work: Awaitable[str]) -> str:
    try:
        return await work
    except ComputerNotReady as exc:
        return exc.message
    except ComputerBusy as exc:
        return exc.message


def _shot_path(key: str) -> str:
    safe = "".join(ch if ch.isalnum() else "_" for ch in key)[:40]
    stamp = datetime.now().strftime("%Y%m%d%H%M%S")
    return str(ROOT / "shots" / f"{stamp}-{safe}.png")


@ai_tools(
    category="default",
    check_func=allow_computer,
    capability_domain="电脑",
    covers=["查看电脑桌面上的窗口和可操作控件"],
    aliases=["电脑·查看屏幕"],
    brief="读取当前桌面的窗口和可操作控件。",
    timeout=180,
    code_callable=False,
)
async def computer_look(ctx: RunContext[ToolContext]) -> str:
    """读取当前桌面。e 编号在分隔线下面。

    Args:
        ctx: 工具上下文
    """
    return await _guard(look(get_pool(), user_key(ctx.deps.ev)))


@ai_tools(
    category="default",
    check_func=allow_computer,
    capability_domain="电脑",
    covers=["点击电脑桌面上的控件或坐标"],
    aliases=["电脑·点击"],
    brief="点击 e 编号或坐标，并返回点击之后的屏幕。",
    timeout=90,
    code_callable=False,
)
async def computer_click(ctx: RunContext[ToolContext], target: str, foreground: bool = False) -> str:
    """点击快照里的 e 编号，或 x,y 坐标。

    foreground 为 true 时会占用使用者的鼠标。默认在后台点击。

    Args:
        ctx: 工具上下文
        target: e 编号，例如 e2；或 x,y，例如 120,80
        foreground: 是否切到前台。默认 false
    """
    return await _guard(click(get_pool(), user_key(ctx.deps.ev), target, foreground=foreground))


@ai_tools(
    category="default",
    check_func=allow_computer,
    capability_domain="电脑",
    covers=["向电脑桌面输入文字"],
    aliases=["电脑·输入文字"],
    brief="输入一段文字，并返回输入之后的屏幕。",
    timeout=90,
    code_callable=False,
)
async def computer_type(ctx: RunContext[ToolContext], text: str) -> str:
    """把文字输入到当前桌面。一次最多 400 个字符。

    群聊里看起来像密码或密钥的文字会被拒绝。

    Args:
        ctx: 工具上下文
        text: 要输入的文字
    """
    return await _guard(type_text(get_pool(), user_key(ctx.deps.ev), text, user_type=user_place(ctx.deps.ev)))


@ai_tools(
    category="default",
    check_func=allow_computer,
    capability_domain="电脑",
    covers=["在电脑桌面按下键盘按键"],
    aliases=["电脑·按键"],
    brief="按下键盘按键，并返回按键之后的屏幕。",
    timeout=90,
    code_callable=False,
)
async def computer_press(ctx: RunContext[ToolContext], key: str, foreground: bool = False) -> str:
    """按下按键。组合键用加号连接，例如 ctrl+l。

    Args:
        ctx: 工具上下文
        key: 按键名，例如 enter、ctrl+a
        foreground: 是否切到前台。默认 false
    """
    return await _guard(press(get_pool(), user_key(ctx.deps.ev), key, foreground=foreground))


@ai_tools(
    category="default",
    check_func=allow_computer,
    capability_domain="电脑",
    covers=["在电脑桌面滚动"],
    aliases=["电脑·滚动"],
    brief="按方向滚动，并返回滚动之后的屏幕。",
    timeout=90,
    code_callable=False,
)
async def computer_scroll(ctx: RunContext[ToolContext], direction: str) -> str:
    """滚动当前指针所在的位置。方向是 up、down、left、right。

    Args:
        ctx: 工具上下文
        direction: 滚动方向
    """
    return await _guard(scroll(get_pool(), user_key(ctx.deps.ev), direction))


@ai_tools(
    category="default",
    check_func=allow_computer,
    capability_domain="电脑",
    covers=["在电脑桌面打开一个网址"],
    aliases=["电脑·打开网址"],
    brief="打开一个 http 或 https 网址，并返回之后的屏幕。",
    timeout=180,
    code_callable=False,
)
async def computer_open(ctx: RunContext[ToolContext], url: str) -> str:
    """打开网址。只接受 http 或 https。

    Args:
        ctx: 工具上下文
        url: 以 http:// 或 https:// 开头的网址
    """
    return await _guard(open_url(get_pool(), user_key(ctx.deps.ev), url))


@ai_tools(
    category="default",
    check_func=allow_computer,
    capability_domain="电脑",
    covers=["在电脑桌面启动一个程序"],
    aliases=["电脑·启动程序"],
    brief="按程序名启动程序，并返回启动之后的屏幕。",
    timeout=180,
    code_callable=False,
)
async def computer_app(ctx: RunContext[ToolContext], name: str) -> str:
    """启动程序。云端桌面填桌面文件名，例如 firefox。

    Args:
        ctx: 工具上下文
        name: 程序名
    """
    return await _guard(launch(get_pool(), user_key(ctx.deps.ev), name))


@ai_tools(
    category="default",
    check_func=allow_computer,
    capability_domain="电脑",
    covers=["保存电脑桌面截图"],
    aliases=["电脑·截图"],
    brief="把当前桌面保存成图片，返回文件路径。",
    timeout=90,
    code_callable=False,
)
async def computer_shot(ctx: RunContext[ToolContext]) -> str:
    """保存一张截图。超过本机限额的图片会删掉。

    Args:
        ctx: 工具上下文
    """
    path = _shot_path(user_key(ctx.deps.ev))
    text = await _guard(shot(get_pool(), user_key(ctx.deps.ev), path))
    directory = Path(path).parent
    if directory.is_dir():
        names = [str(item) for item in directory.glob("*.png")]
        for old in trim_shots(names, 8):
            Path(old).unlink()
    return text


@ai_tools(
    category="default",
    check_func=allow_computer,
    capability_domain="电脑",
    covers=["释放当前用户的电脑桌面"],
    aliases=["电脑·释放桌面"],
    brief="暂停云端桌面，或放开本机桌面。",
    timeout=60,
    code_callable=False,
)
async def computer_release(ctx: RunContext[ToolContext]) -> str:
    """释放这个用户的桌面。云端机器会暂停，不会删除。

    Args:
        ctx: 工具上下文
    """
    return await _guard(release(get_pool(), user_key(ctx.deps.ev)))
