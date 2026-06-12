from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from nonebot.adapters.onebot.v11 import MessageSegment


@dataclass
class ParsedDynamic:
    mid: str
    name: str
    dynamic_type: str
    timestamp: int
    text: str
    message: list[MessageSegment]
    url: str
    id_str: str = ""
    action: str = ""
    origin: ParsedDynamic | None = None
