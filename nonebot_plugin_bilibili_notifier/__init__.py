from nonebot import get_plugin_config, require
from nonebot.log import logger

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
