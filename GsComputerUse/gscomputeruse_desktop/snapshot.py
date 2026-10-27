"""屏幕文本。Agent 只根据 e 编号操作，编号和名称在同一次快照里对齐。"""

from __future__ import annotations

from dataclasses import dataclass

SNAPSHOT_MARK = "--- 可操作 ---"
_BODY_LIMIT = 3500
_NAME_LIMIT = 80
_TEXT_LIMIT = 400
_MARK_LIMIT = 24

_SCROLL = {
    "up": "up",
    "down": "down",
    "left": "left",
    "right": "right",
    "上": "up",
    "下": "down",
    "左": "left",
    "右": "right",
}


@dataclass(frozen=True)
class Mark:
    kind: str
    name: str
    token: str
    x: int
    y: int


class MarkBook:
    def __init__(self) -> None:
        self.rows: list[Mark] = []

    def replace(self, rows: list[Mark], *, title: str, body: str) -> str:
        self.rows = rows[:_MARK_LIMIT]
        return format_desktop(title, body, self.rows)

    def resolve(self, target: str) -> Mark | str:
        point = parse_point(target)
        if point is not None:
            return Mark(kind="point", name=f"{point[0]},{point[1]}", token="", x=point[0], y=point[1])
        index = parse_index(target)
        if index is None:
            return "请填 e 编号，或 x,y 坐标。"
        if index < 0 or index >= len(self.rows):
            shown = target.strip()
            return f"屏幕上没有「{shown}」。请重新看屏幕。"
        return self.rows[index]


def format_desktop(title: str, body: str, rows: list[Mark]) -> str:
    clipped = body.strip()
    if len(clipped) > _BODY_LIMIT:
        clipped = clipped[:_BODY_LIMIT] + "…"
    lines = [title, "正文:", clipped, SNAPSHOT_MARK]
    for index, row in enumerate(rows[:_MARK_LIMIT], start=1):
        lines.append(f"e{index} {row.kind} {clip_name(row.name)}")
    return "\n".join(lines)


def clip_name(name: str) -> str:
    text = " ".join(name.split())
    if len(text) <= _NAME_LIMIT:
        return text
    return text[:_NAME_LIMIT] + "…"


def clip_text(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "…"


def parse_index(target: str) -> int | None:
    text = target.strip()
    if len(text) >= 2 and text[0] == "e" and text[1:].isdigit():
        return int(text[1:]) - 1
    return None


def parse_point(target: str) -> tuple[int, int] | None:
    text = target.strip()
    if "," not in text:
        return None
    left, right = text.split(",", 1)
    if not left.strip().isdigit() or not right.strip().isdigit():
        return None
    x = int(left.strip())
    y = int(right.strip())
    if x > 16384 or y > 16384:
        return None
    return x, y


def scroll_direction(text: str) -> str:
    folded = text.strip().lower()
    if folded in _SCROLL:
        return _SCROLL[folded]
    return ""


def secret_problem(user_type: str, text: str) -> str:
    if user_type == "direct" or not _looks_secret(text):
        return ""
    return "群聊里不能输入密码或密钥，请改走私聊。"


def length_problem(text: str) -> str:
    if len(text) > _TEXT_LIMIT:
        return f"一次最多输入 {_TEXT_LIMIT} 个字符。"
    if not text:
        return "没有要输入的文字。"
    return ""


def _looks_secret(text: str) -> bool:
    compact = text.strip()
    if any(ch.isspace() for ch in compact) or len(compact) < 8:
        return False
    has_alpha = any(ch.isalpha() for ch in compact)
    has_digit = any(ch.isdigit() for ch in compact)
    return has_alpha and has_digit
