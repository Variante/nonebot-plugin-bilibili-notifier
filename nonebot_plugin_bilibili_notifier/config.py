from typing import Dict, List, Literal

from pydantic import BaseModel, Field


class Config(BaseModel):
    # Cookie file path (required)
    bnotifier_cookies: str

    # Push targets
    bnotifier_push_updates: Dict[str, List[str]] = Field(default_factory=dict)
    bnotifier_push_lives: Dict[str, List[str]] = Field(default_factory=dict)
    bnotifier_push_updates_by_group: Dict[str, List[str]] = Field(default_factory=dict)
    bnotifier_push_lives_by_group: Dict[str, List[str]] = Field(default_factory=dict)

    # Blacklist and auto-like
    bnotifier_push_type_blacklist: Dict[str, List[str]] = Field(default_factory=dict)
    bnotifier_like: List[str] = Field(default_factory=list)

    # Runtime tuning
    bnotifier_api_timeout: float = Field(default=20, gt=0)
    bnotifier_debug_user: List[str] = Field(default_factory=list)
    bnotifier_dynamic_update_interval: int = Field(default=120, ge=1)
    bnotifier_live_update_interval: int = Field(default=60, ge=1)

    # Dynamic fetch controls
    bnotifier_dynamic_pages: int = Field(default=1, ge=1)
    bnotifier_dynamic_features: str = "itemOpusStyle"
    bnotifier_timezone_offset: int = -480

    # Adapter
    bnotifier_use_saa: bool = True

    # Message behavior
    bnotifier_forward_message_mode: Literal["full", "none"] = "full"
    bnotifier_skip_lottery_forward: bool = True

    # Live message detail controls
    bnotifier_live_fetch_size: int = Field(default=50, ge=1)
    bnotifier_live_include_title: bool = True
    bnotifier_live_include_cover: bool = True

    # State behavior
    bnotifier_ignore_old_dynamic_on_start: bool = True
    bnotifier_persist_state: bool = True
    bnotifier_state_file: str = "last_update.json"
