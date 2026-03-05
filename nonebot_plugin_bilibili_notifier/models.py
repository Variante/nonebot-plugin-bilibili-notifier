from dataclasses import dataclass
from typing import Any, List, Optional

from nonebot.adapters.onebot.v11 import MessageSegment


@dataclass
class ParsedDynamic:
    mid: str
    name: str
    dynamic_type: str
    timestamp: int
    text: str
    message: List[MessageSegment]
    url: str
    action: str = ""
    origin: Optional["ParsedDynamic"] = None
