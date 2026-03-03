import re
from datetime import datetime
from typing import Optional

from nonebot import get_plugin_config, on_command, require
from nonebot.adapters import Event, Message
from nonebot.log import logger
from nonebot.params import CommandArg
from nonebot.rule import Rule

require("nonebot_plugin_apscheduler")
require("nonebot_plugin_localstore")
require("nonebot_plugin_saa")

from nonebot_plugin_apscheduler import scheduler
from nonebot_plugin_saa import enable_auto_select_bot

from .config import Config
from .runtime import BilibiliNotifierService

enable_auto_select_bot()

config = get_plugin_config(Config)
logger.debug(config)
service = BilibiliNotifierService(config)


@scheduler.scheduled_job("interval", seconds=config.bnotifier_dynamic_update_interval)
async def fetch_bilibili_updates() -> None:
    await service.fetch_bilibili_updates()


@scheduler.scheduled_job("interval", seconds=config.bnotifier_live_update_interval)
async def fetch_bilibili_live_info() -> None:
    await service.fetch_bilibili_live_info()


async def is_sender_debug_user(event: Event) -> bool:
    sender_user_id = event.get_user_id()
    if sender_user_id in service.debug_users:
        return True
    return False


def _extract_dynamic_id(arg_text: str) -> Optional[int]:
    for token in re.findall(r"\d+", arg_text):
        dynamic_id = int(token)
        if dynamic_id > 0:
            return dynamic_id
    return None


def _extract_timestamp(arg_text: str) -> Optional[int]:
    text = arg_text.strip()
    if not text:
        return None
    if not re.fullmatch(r"\d+", text):
        return None
    return int(text)


debug_dynamic = on_command("dd", rule=Rule(is_sender_debug_user), block=True)


@debug_dynamic.handle()
async def handle_debug_dynamic(event: Event, args: Message = CommandArg()) -> None:
    dynamic_id = _extract_dynamic_id(args.extract_plain_text().strip())
    if dynamic_id is None:
        await debug_dynamic.finish("用法: /dd <动态ID>")

    ok, reason = await service.push_dynamic_to_user(dynamic_id, event.get_user_id())
    if not ok:
        await debug_dynamic.finish(reason)


debug_reset_update = on_command("dr", rule=Rule(is_sender_debug_user), block=True)


@debug_reset_update.handle()
async def handle_debug_reset_update(args: Message = CommandArg()) -> None:
    raw_arg = args.extract_plain_text().strip()
    if raw_arg:
        target_timestamp = _extract_timestamp(raw_arg)
        if target_timestamp is None:
            await debug_reset_update.finish("用法: /dr 或 /dr <unix时间戳>")
    else:
        target_timestamp = None

    old_timestamp, new_timestamp = service.reset_last_update_timestamp(target_timestamp)

    old_text = datetime.fromtimestamp(old_timestamp).strftime("%Y-%m-%d %H:%M:%S")
    new_text = datetime.fromtimestamp(new_timestamp).strftime("%Y-%m-%d %H:%M:%S")
    await debug_reset_update.finish(
        f"已重置 last_update_timestamp\n旧值: {old_timestamp} ({old_text})\n新值: {new_timestamp} ({new_text})"
    )
