"""电脑代理。工具白名单只有桌面操作。"""

from __future__ import annotations

from gsuid_core.ai_core.agent_node import AgentNode, register_agent_node

_PROMPT = """你操作一台电脑桌面。每次只调用一个工具，然后根据新的屏幕文本决定下一步。

屏幕文本里，「--- 可操作 ---」下面每行是 e编号、种类、名称。
点击、输入都优先用这些 e 编号。编号过期就重新看屏幕。不要编造屏幕上没有的控件。

滚动发生在当前指针位置。先点击，再滚动。
foreground 默认 false。只有后台点击失败时才把它设为 true，那会占用使用者的鼠标。

群聊里不要输入密码或密钥。
InstaBox 和 Simular box 是云端 Linux 桌面。Simular 上打开网址或点网页元素后，e 编号是网页元素；看屏幕看到的是桌面控件。
CUA Driver 是这台机器已经打开的桌面，同一时间只有一个用户。
做完调用释放。用户没要截图就不要截图。
"""

_BOUNDARY = """【交付】
把最后一屏的文字原样作为返回值。不要发消息给用户。若用户要了截图，只把文件路径写进返回值。
"""

_TOOLS = [
    "computer_look",
    "computer_click",
    "computer_type",
    "computer_press",
    "computer_scroll",
    "computer_open",
    "computer_app",
    "computer_shot",
    "computer_release",
]


def register_computer_agent() -> None:
    register_agent_node(
        AgentNode(
            node_id="computer_agent",
            display_name="电脑代理",
            prompt=_PROMPT,
            prompt_style="plain",
            when_to_use="用户要操作一台电脑桌面：打开程序、点击、输入、滚动或截图。只打开网页并阅读时不要用这个代理。",
            match_keywords=["打开电脑", "操作桌面", "电脑截图"],
            tool_packs=[],
            tool_names=list(_TOOLS),
            boundary_override=_BOUNDARY,
            master_only=False,
            source="plugin",
            plugin="GsComputerUse",
        )
    )


register_computer_agent()
