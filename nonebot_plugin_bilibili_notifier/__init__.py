import re
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


debug_dynamic = on_command("dd", rule=Rule(is_sender_debug_user), block=True)


@debug_dynamic.handle()
async def handle_debug_dynamic(event: Event, args: Message = CommandArg()) -> None:
    dynamic_id = _extract_dynamic_id(args.extract_plain_text().strip())
    if dynamic_id is None:
        await debug_dynamic.finish("用法: /dd <动态ID>")

    ok, reason = await service.push_dynamic_to_user(dynamic_id, event.get_user_id())
    if not ok:
        await debug_dynamic.finish(reason)
