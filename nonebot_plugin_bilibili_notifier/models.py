from dataclasses import dataclass
from typing import Any, List, Optional


MessageSegment = Any


@dataclass
class ParsedDynamic:
    mid: str
    name: str
    dynamic_type: str
    timestamp: int
    text: str
    message: List[MessageSegment]
    url: str
    origin: Optional["ParsedDynamic"] = None
