from nonebot import require
require("nonebot_plugin_apscheduler")
from nonebot_plugin_apscheduler import scheduler
require("nonebot_plugin_localstore")
from nonebot_plugin_localstore import get_cache_file
require("nonebot_plugin_saa")
from nonebot_plugin_saa import TargetQQGroup, MessageFactory, enable_auto_select_bot
from nonebot import  get_plugin_config
from nonebot.log import logger
from .config import Config
import json
import time
import datetime

from bilibili_api import Credential
from bilibili_api.dynamic import get_live_users
from bilibili_api.utils.utils import get_api
from bilibili_api.utils.network import Api
from bilibili_api import settings

API = get_api("dynamic")
enable_auto_select_bot()

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
    elif dtype == 'DYNAMIC_TYPE_ARTICLE':
        # 投稿专栏
        url = d['basic']['jump_url']
        text = info['pub_action'] + ': ' + d['modules']['module_dynamic']['major']['opus']['title']
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


# 把{qq: [mid]}的形式转化为{mid: [qq]}
def convert_by_group(by_group: dict, normal: dict):
    for k, v in by_group.items():
        for ups in v:
            if ups in normal:
                normal[ups].append(k)
            else:
                normal[ups] = [k]
                
# 查看是否黑名单
def is_in_blacklist(mid: str, dtype: str):
    if mid in config.bnotifier_push_type_blacklist:
        if dtype in config.bnotifier_push_type_blacklist[mid]:
            logger.info(f'屏蔽了{mid}的{dtype}，不推送')
            return True
    return False
                

config = get_plugin_config(Config)
logger.debug(config)
credential = get_credential(config.bnotifier_cookies)
tmp_save = get_cache_file('bilibili-notifier', 'last_update.json')
try:
    with open(tmp_save, 'r') as f:
        last_update = json.load(f)['last_update']
    dt = datetime.datetime.fromtimestamp(last_update)
    logger.info(f'加载上次更新时间{dt}({last_update})')
except:
    logger.warning('未找到上次更新时间，使用当前时间')
    last_update = int(time.time())
    
last_live = None
# 设置延时
settings.timeout = config.bnotifier_api_timeout
# 将by group的配置转化为标准配置
convert_by_group(config.bnotifier_push_updates_by_group, config.bnotifier_push_updates)
convert_by_group(config.bnotifier_push_lives_by_group, config.bnotifier_push_lives)
logger.info(f'推送更新消息的用户：群：{config.bnotifier_push_updates}' )
logger.info(f'推送直播消息的用户：群：{config.bnotifier_push_lives}' )
logger.info(f'屏蔽的消息/群：{config.bnotifier_push_type_blacklist}' )

def clean_url(u: str) -> str:
    # 叔叔现在开始往链接里塞奇怪的东西了
    return u.split('?')[0]

def shorten(u: str) -> str:
    l = config.bnotifier_msg_truncate
    if len(u) > l:
        return u[:l] + '\n...点击链接查看全文'
    return u

@scheduler.scheduled_job('cron', second='0', misfire_grace_time=20)
async def fetch_bilibili_updates():
    global last_update
    if len(config.bnotifier_push_updates) == 0:
        return
    logger.debug('获取B站动态更新')
    dyna = await get_dynamic_page_info(credential)
    logger.debug(f'更新到{len(dyna)}条动态')
    dyna_names = []
    dyna_times = []
    for i, d in enumerate(dyna):
        logger.debug(f'处理第{i + 1}条动态')
        # logger.debug(d)
        res = parse_dynamic(d)
        logger.debug(res)
        dyna_names.append(res['name'])
        logger.debug(f'Time: {datetime.datetime.fromtimestamp(res["time"])}({res["time"]}) vs {last_update}')
        if (key:=str(res['mid'])) in config.bnotifier_push_updates and res['time'] > last_update:
            dtype = res['mid']
            if is_in_blacklist(key, dtype):
                continue
            msg = f"{res['name']} {shorten(res['text'])}\n{clean_url(res['url'])}"
            for gid in config.bnotifier_push_updates[key]:
                if is_in_blacklist(gid, dtype):
                    continue
                logger.info(f'将{key}的更新推送到{gid}\n{msg}')
                await MessageFactory(msg).send_to(TargetQQGroup(group_id=int(gid)))
        dyna_times.append(res["time"])
    if (t:= max(dyna_times)) != last_update: # 使用最后一条动态的时间
        last_update = t
        with open(tmp_save, 'w') as f:
            json.dump({'last_update': t}, f)
        logger.debug(f'刷新动态更新时间为{t}({last_update})')
    logger.debug(f'成功刷新{len(dyna)}条动态：' + ', '.join(dyna_names))

    
@scheduler.scheduled_job('cron', second='0', misfire_grace_time=20)
async def fetch_bilibili_live_info():
    global last_live
    if len(config.bnotifier_push_lives) == 0:
        return
    logger.debug('获取直播状态')
    live = await get_live_users(credential=credential, size=50)
    if live['count'] == 0 or 'items' not in live:
        # 应对有时奇怪的网络错误
        return
    on_live = []
    on_live_names = []
    for i, d in enumerate(live['items']):
        # logger.debug(f'处理第{i + 1}个直播用户')
        key = str(d['uid'])
        on_live.append(key)
        on_live_names.append(d['uname'])
        if key in config.bnotifier_push_lives and last_live is not None:
            # 已经在直播就不通知了
            if key in last_live:
                continue
            # 现在title全都是空了，不知道为什么
            msg = f"{d['uname']}开始直播了：{d['title']}\n地址：{clean_url(d['link'])}"
            for gid in config.bnotifier_push_lives[key]:
                logger.info(f'将{key}的直播消息推送到{gid}')
                await MessageFactory(msg).send_to(TargetQQGroup(group_id=int(gid)))
    last_live = set(on_live)
    logger.debug(f'{live["count"]}用户正在直播：{", ".join(on_live_names)}')
    # if live['count'] >= 10:
    #     with open('live.log', '+a') as f:
    #        f.write(f'{datetime.datetime.now()} | {live["count"]}用户正在直播：{", ".join(on_live_names)}\n')
    