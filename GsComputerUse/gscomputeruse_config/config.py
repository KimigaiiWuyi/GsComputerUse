"""插件配置。按钮的 endpoint 必须和 register_config_button 用同一条路径。"""

from __future__ import annotations

from urllib.parse import urlsplit

from gsuid_core.data_store import get_res_path
from gsuid_core.utils.plugins_config.models import (
    GSC,
    ButtonState,
    GsIntConfig,
    GsStrConfig,
    GsBoolConfig,
    GsButtonConfig,
    ButtonStatusField,
)
from gsuid_core.utils.plugins_config.gs_config import StringConfig

from .host import host_label

ROOT = get_res_path("GsComputerUse")
PROBE_ENDPOINT = "/api/gs_computer_use/probe"
_HOST = host_label()
_BACKENDS = ("instabox", "cua")

CONFIG_DEFAULT: dict[str, GSC] = {
    "probe": GsButtonConfig(
        title="检查电脑后端",
        desc="查看本机规格，并检查 InstaBox API Key 或 CUA Driver。不会下载虚拟机。",
        data=ButtonState(),
        endpoint=PROBE_ENDPOINT,
        label="检查",
        level="primary",
        confirm=f"当前环境是 {_HOST}。2 核或内存不超过 2560 MiB 时同时只开 1 个桌面。",
        status_fields=[
            ButtonStatusField(key="host", label="当前环境"),
            ButtonStatusField(key="cores", label="核数"),
            ButtonStatusField(key="memory", label="内存 MiB"),
            ButtonStatusField(key="profile", label="限额"),
            ButtonStatusField(key="backend", label="后端"),
            ButtonStatusField(key="screen", label="屏幕"),
            ButtonStatusField(key="shot", label="截图上限"),
            ButtonStatusField(key="seats", label="同时桌面"),
            ButtonStatusField(key="ready", label="检查结果"),
        ],
        show_progress=True,
        poll_ms=800,
    ),
    "backend": GsStrConfig(
        title="电脑后端",
        desc="instabox 是云端 Linux 桌面，本机只发请求。cua 操作本机已安装的 CUA Driver。2 核 2 GiB 请选 instabox。",
        data="instabox",
        options=["instabox", "cua"],
    ),
    "instabox_api_key": GsStrConfig(
        title="InstaBox API Key",
        desc="以 ibk_ 开头。只填写在这项插件配置里。",
        data="",
        secret=True,
    ),
    "instabox_api_url": GsStrConfig(
        title="InstaBox API 地址",
        desc="默认 https://api.instabox.sh。只接受 https，或本机 http://127.0.0.1。",
        data="https://api.instabox.sh",
    ),
    "cua_bin": GsStrConfig(
        title="CUA Driver 程序",
        desc="可执行文件名或绝对路径，例如 cua-driver。插件不会替你安装。",
        data="cua-driver",
    ),
    "max_desktops": GsIntConfig(
        title="同时打开的桌面数",
        desc="每个用户一个桌面。2 核或内存不超过 2560 MiB 时固定为 1。CUA Driver 固定为 1。最多 3。",
        data=1,
        max_value=3,
    ),
    "idle_seconds": GsIntConfig(
        title="空闲暂停秒数",
        desc="这么久没人操作就暂停云端机器，或放开本机桌面。少于 60 秒时按 60 秒。",
        data=180,
        max_value=1800,
    ),
    "master_only": GsBoolConfig(
        title="仅主人可用",
        desc="打开时，只有主人能让代理操作电脑。查看状态不受这一项限制。",
        data=True,
    ),
}

CONFIG = StringConfig("GsComputerUse", ROOT / "config.json", CONFIG_DEFAULT)


def _int_flag(key: str, default: int, low: int, high: int) -> int:
    item = CONFIG.get_config(key)
    value = item.data if isinstance(item, GsIntConfig) else default
    if value < low:
        return low
    if value > high:
        return high
    return value


def flag_bool(key: str, default: bool) -> bool:
    item = CONFIG.get_config(key)
    if isinstance(item, GsBoolConfig):
        return item.data
    return default


def flag_text(key: str) -> str:
    item = CONFIG.get_config(key)
    if isinstance(item, GsStrConfig):
        return item.data.strip()
    return ""


def desired_backend() -> str:
    value = flag_text("backend")
    if value in _BACKENDS:
        return value
    return "instabox"


def configured_max() -> int:
    return _int_flag("max_desktops", 1, 1, 3)


def idle_limit() -> int:
    return _int_flag("idle_seconds", 180, 60, 1800)


def instabox_key() -> str:
    return flag_text("instabox_api_key")


def key_problem(key: str) -> str:
    if not key:
        return "还没有 InstaBox API Key。"
    if not key.startswith("ibk_"):
        return "这不是 InstaBox 的 API Key。"
    return ""


def key_state(key: str) -> str:
    problem = key_problem(key)
    if problem:
        return problem
    return "已填写"


def clean_base(raw: str) -> str:
    text = raw.strip().rstrip("/")
    parsed = urlsplit(text)
    host = parsed.hostname or ""
    if parsed.scheme == "https" and host:
        return text
    if parsed.scheme == "http" and host in {"localhost", "127.0.0.1"}:
        return text
    return ""


def instabox_base() -> str:
    return clean_base(flag_text("instabox_api_url") or "https://api.instabox.sh")


def cua_binary() -> str:
    text = flag_text("cua_bin") or "cua-driver"
    if "\n" in text or "\r" in text:
        return ""
    return text
