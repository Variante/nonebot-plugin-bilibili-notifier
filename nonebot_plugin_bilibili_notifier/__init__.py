from nonebot import require
require("nonebot_plugin_apscheduler")
from nonebot_plugin_apscheduler import scheduler

from nonebot import  get_plugin_config, get_bot
from nonebot.log import logger
from .config import Config
import json
import time

from bilibili_api import Credential
from bilibili_api.dynamic import get_live_users
from bilibili_api.utils.utils import get_api
from bilibili_api.utils.network import Api
from bilibili_api import settings

API = get_api("dynamic")

async def get_dynamic_page_info(
    credential: Credential,
    features: str = "itemOpusStyle",
    pn: int = 1,
):
    api = API["info"]["dynamic_page_info"]
    params = {
        "timezone_offset": -480,
        "features": features,
        "page": pn,
    }
    dynmaic_data = (
        await Api(**api, credential=credential).update_params(**params).result
    )
    return dynmaic_data["items"]


def parse_dynamic(d: dict):
    info = d['modules']['module_author']
    dtype = d['type']
    
    if dtype == 'DYNAMIC_TYPE_AV':
        # 投稿了视频
        content = d['modules']['module_dynamic']['major']['archive']
        text = info['pub_action'] + ': ' + content['title']
        url = content['jump_url']
    elif dtype in ['DYNAMIC_TYPE_DRAW', 'DYNAMIC_TYPE_WORD']:
        # 发送了图文动态
        url = d['basic']['jump_url']
        text = '发布动态：' + d['modules']['module_dynamic']['major']['opus']['summary']['text']
    elif dtype == 'DYNAMIC_TYPE_FORWARD':
        # 转发
        url = '//t.bilibili.com/' + d['basic']['comment_id_str']
        text = '转发了：' + d['modules']['module_dynamic']['desc']['text']
    else:
        url = ''
        text = '未识别动态种类：' + dtype
    return {
        'mid': info['mid'],
        'name': info['name'],
        'type': dtype,
        'time': int(info['pub_ts']),
        'text': text,
        'url': ('https:' + url) if len(url) else ''
    }


# 加载cookies
def fetch_cookies(file):
    res = {
        'sessdata': None,
        'bili_jct': None,
        'buvid3': None,
        'dedeuserid': None
    }
    with open(file, 'r') as f:
        js = json.load(f)
        for i in js:
            j = i['name'].lower()
            if j in res:
                res[j] = i['value']
    return res

# 获得验证信息信息
def get_credential(file: str):
    cookies = fetch_cookies(file=file)
    # 生成一个 Credential 对象
    return Credential(**cookies)

# 获取当前时刻（只发布当前时刻之后更新的动态）
def get_last_update():
    return int(time.time()) - config.bnotifier_timeshift

# 把{qq: [mid]}的形式转化为{mid: [qq]}
def convert_by_group(by_group: dict, normal: dict):
    for k, v in by_group.items():
        for ups in v:
            if ups in normal:
                normal[ups].append(k)
            else:
                normal[ups] = [k]
                

config = get_plugin_config(Config)
logger.debug(config)
credential = get_credential(config.bnotifier_cookies)
last_update = int(config.bnotifier_push_after)
if last_update == 0:
    last_update = get_last_update()
last_live = None
# 设置延时
settings.timeout = config.bnotifier_api_timeout
# 将by group的配置转化为标准配置
convert_by_group(config.bnotifier_push_updates_by_group, config.bnotifier_push_updates)
convert_by_group(config.bnotifier_push_lives_by_group, config.bnotifier_push_lives)
logger.info(f'推送更新消息的用户：群：{config.bnotifier_push_updates}' )
logger.info(f'推送直播消息的用户：群：{config.bnotifier_push_lives}' )

@scheduler.scheduled_job('cron', second='0', misfire_grace_time=60) # = UTC+8 1445
async def fetch_bilibili_updates():
    global last_update
    if len(config.bnotifier_push_updates) == 0:
        return
    logger.debug('获取B站动态更新')
    dyna = await get_dynamic_page_info(credential)
    logger.debug(f'更新到{len(dyna)}条动态')
    bot = get_bot()
    for i, d in enumerate(dyna):
        logger.debug(f'处理第{i + 1}条动态')
        logger.debug(d)
        res = parse_dynamic(d)
        logger.debug('处理结果')
        logger.debug(res)
        
        if (key:=str(res['mid'])) in config.bnotifier_push_updates and res['time'] > last_update:
            msg = f"{res['name']} {res['text']}\n{res['url']}"
            for gid in config.bnotifier_push_updates[key]:
                logger.info(f'将{key}的更新推送到{gid}\n{msg}')
                await bot.send_group_msg(group_id=gid, message=msg)
    last_update = get_last_update()
    
    
@scheduler.scheduled_job('cron', second='0', misfire_grace_time=60) # = UTC+8 1445
async def fetch_bilibili_live_info():
    global last_live
    if len(config.bnotifier_push_lives) == 0:
        return
    logger.debug('获取直播状态')
    live = await get_live_users(credential=credential, size=50)
    if live['count'] == 0:
        last_live = set()
        return
    on_live = []
    bot = get_bot()
    for i, d in enumerate(live['items']):
        logger.debug(f'处理第{i + 1}个直播用户')
        key = str(d['uid'])
        on_live.append(key)
        if key in config.bnotifier_push_lives and last_live is not None:
            # 已经在直播就不通知了
            if key in last_live:
                continue
            msg = f"{d['uname']}开始直播了：{d['title']}\n地址：{d['link']}"
            for gid in config.bnotifier_push_lives[key]:
                logger.info(f'将{key}的直播消息推送到{gid}')
                await bot.send_group_msg(group_id=gid, message=msg)
    last_live = set(on_live)
    