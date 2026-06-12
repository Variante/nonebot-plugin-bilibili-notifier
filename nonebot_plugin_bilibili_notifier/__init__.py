import re

from nonebot import get_driver, get_plugin_config, on_command, require
from nonebot.adapters import Event, Message
from nonebot.adapters.onebot.v11 import Bot
from nonebot.log import logger
from nonebot.params import CommandArg
from nonebot.rule import Rule

require("nonebot_plugin_apscheduler")
require("nonebot_plugin_localstore")

from .config import Config

config = get_plugin_config(Config)
driver = get_driver()

if config.bnotifier_use_saa:
    require("nonebot_plugin_saa")
    from nonebot_plugin_saa import enable_auto_select_bot
    enable_auto_select_bot()

from nonebot_plugin_apscheduler import scheduler

from .runtime import BilibiliNotifierService
logger.debug(config)
service = BilibiliNotifierService(config)


@driver.on_bot_connect
async def check_bilibili_cookie_on_start(bot: Bot) -> None:
    await service.notify_admins_of_startup_cookie_failure(bot)


@driver.on_startup
async def start_bilibili_live_monitors() -> None:
    await service.start_live_monitors()


@driver.on_shutdown
async def stop_bilibili_live_monitors() -> None:
    await service.stop_live_monitors()


@scheduler.scheduled_job("interval", seconds=config.bnotifier_dynamic_update_interval)
async def fetch_bilibili_updates() -> None:
    await service.fetch_bilibili_updates()


async def is_sender_privileged(event: Event) -> bool:
    user_id = event.get_user_id()
    return user_id in service.debug_users or user_id in driver.config.superusers


def _extract_dynamic_id(arg_text: str) -> int | None:
    for token in re.findall(r"\d+", arg_text):
        dynamic_id = int(token)
        if dynamic_id > 0:
            return dynamic_id
    return None


bnotifier_parse = on_command("bnotifier_parse", rule=Rule(is_sender_privileged), block=True)
bnotifier_status = on_command(
    "bnotifier_status",
    rule=Rule(is_sender_privileged),
    block=True,
)


@bnotifier_parse.handle()
async def handle_bnotifier_parse(event: Event, args: Message = CommandArg()) -> None:
    dynamic_id = _extract_dynamic_id(args.extract_plain_text().strip())
    if dynamic_id is None:
        await bnotifier_parse.finish("用法: /bnotifier_parse <动态ID>")

    ok, reason = await service.push_dynamic_to_user(dynamic_id, event.get_user_id())
    if not ok:
        await bnotifier_parse.finish(reason)


@bnotifier_status.handle()
async def handle_bnotifier_status() -> None:
    await bnotifier_status.finish(service.get_live_monitor_status_message())
