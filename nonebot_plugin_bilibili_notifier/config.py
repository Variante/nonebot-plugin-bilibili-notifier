from typing import Literal

from pydantic import BaseModel, Field


class Config(BaseModel):
    # Cookie file path (required)
    bnotifier_cookies: str

    # Push targets
    bnotifier_push_updates: dict[str, list[str]] = Field(default_factory=dict)
    bnotifier_push_lives: dict[str, list[str]] = Field(default_factory=dict)
    bnotifier_push_updates_by_group: dict[str, list[str]] = Field(default_factory=dict)
    bnotifier_push_lives_by_group: dict[str, list[str]] = Field(default_factory=dict)

    # Blacklist and auto-like
    bnotifier_push_type_blacklist: dict[str, list[str]] = Field(default_factory=dict)
    bnotifier_like: list[str] = Field(default_factory=list)

    # Runtime tuning
    bnotifier_api_timeout: float = Field(default=20, gt=0)
    bnotifier_debug_user: list[str] = Field(default_factory=list)
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
    bnotifier_live_include_title: bool = True
    bnotifier_live_include_cover: bool = True
    bnotifier_live_websocket_enabled: bool = True
    bnotifier_live_reconcile_interval: int = Field(default=60, ge=30)
    bnotifier_live_start_silence_seconds: int = Field(default=300, ge=0)
    bnotifier_live_push_stop: bool = False
    bnotifier_live_stop_grace_seconds: int = Field(default=90, ge=0)

    # State behavior
    bnotifier_ignore_old_dynamic_on_start: bool = True
    bnotifier_state_file: str = "last_update.json"
