from nonebot import require
require("nonebot_plugin_apscheduler")
from nonebot_plugin_apscheduler import scheduler
require("nonebot_plugin_localstore")
from nonebot_plugin_localstore import get_cache_file
require("nonebot_plugin_saa")
from nonebot_plugin_saa import TargetQQGroup, TargetQQPrivate, MessageFactory, enable_auto_select_bot, Image, Text, AggregatedMessageFactory
from nonebot import  get_plugin_config, get_bot
from nonebot.log import logger
from .config import Config
import json
import time
import datetime
import re

from bilibili_api import Credential
from bilibili_api.dynamic import get_live_users, Dynamic
from bilibili_api.live import LiveRoom
from bilibili_api.utils.utils import get_api
from bilibili_api.utils.network import Api
from bilibili_api import settings

BILIBILI_API = get_api("dynamic")
enable_auto_select_bot()

async def get_dynamic_page_info(
    credential: Credential,
    features: str = "itemOpusStyle",
    page_number: int = 1,
):
    api = BILIBILI_API["info"]["dynamic_page_info"]
    params = {
        "timezone_offset": -480,
        "features": features,
        "page": page_number,
    }
    dynamic_data = (
        await Api(**api, credential=credential).update_params(**params).result
    )
    return dynamic_data["items"]

def parse_rich_text(rich_text_nodes: dict):
    message_segments = []
    for node in rich_text_nodes:
        if node['type'] == 'RICH_TEXT_NODE_TYPE_EMOJI':
            emoji_data = node['emoji']
            if 'gif_url' in emoji_data:
                emoji_url = emoji_data['gif_url']
            else:
                emoji_url = emoji_data.get('icon_url', None)
            if emoji_url is not None:
                message_segments.append(Image(emoji_url))
        elif node['type'] in ['RICH_TEXT_NODE_TYPE_TEXT', 'RICH_TEXT_NODE_TYPE_AT']:
            message_segments.append(Text(node['text']))
        else:
            continue
    return message_segments

def parse_dynamic(dynamic_data: dict):
    author_info = dynamic_data['modules']['module_author']
    dynamic_type = dynamic_data['type']
    parsed_message = None
    original_dynamic_result = None
    
    if dynamic_type == 'DYNAMIC_TYPE_AV':
        # Posted a video
        video_content = dynamic_data['modules']['module_dynamic']['major']['archive']
        parsed_text = author_info['pub_action'] + '：\n' + video_content['title']
        dynamic_url = video_content['jump_url']
        parsed_message = [Text(parsed_text), Image(video_content['cover'])]
        
    elif dynamic_type in ['DYNAMIC_TYPE_DRAW', 'DYNAMIC_TYPE_WORD']:
        # Posted image/text dynamic
        dynamic_url = dynamic_data['basic']['jump_url']
        parsed_text = '发布了动态：\n'
        opus_content = dynamic_data['modules']['module_dynamic']['major']['opus']
        
        # Add title if exists
        if opus_content['title'] is not None:
            parsed_text += opus_content['title'] + '\n'
            
        # Build message segments
        parsed_message = [Text(parsed_text)]
        parsed_text += opus_content['summary']['text']
        parsed_message.extend(parse_rich_text(opus_content['summary']['rich_text_nodes']))
        
        # Handle images separately
        if dynamic_type == 'DYNAMIC_TYPE_DRAW':
            for picture in opus_content['pics']:
                parsed_message.append(Image(picture['url']))
                
    elif dynamic_type == 'DYNAMIC_TYPE_ARTICLE':
        # Posted article
        dynamic_url = dynamic_data['basic']['jump_url']
        parsed_text = author_info['pub_action'] + '：\n' + dynamic_data['modules']['module_dynamic']['major']['opus']['title']
        
    elif dynamic_type == 'DYNAMIC_TYPE_FORWARD':
        # Forwarded dynamic
        dynamic_url = '//t.bilibili.com/' + dynamic_data['basic']['comment_id_str']
        parsed_text = '转发了：\n'
        parsed_message = [Text(parsed_text)]
        # 纯文本动态
        parsed_text += dynamic_data['modules']['module_dynamic']['desc']['text']
        # 多媒体动态
        parsed_message.extend(parse_rich_text(dynamic_data['modules']['module_dynamic']['desc']['rich_text_nodes']))
        
        # Check the original dynamic information
        original_dynamic_result = parse_dynamic(dynamic_data['orig'])
        
    elif dynamic_type in ['DYNAMIC_TYPE_LIVE_RCMD', 'DYNAMIC_TYPE_UGC_SEASON']:
        # Live notification - skip these
        return None
    else:
        return None
    """
    else:
        unknown_dynamic_file = get_cache_file('bilibili-notifier', 'unknown_dynamic.json')
        with open(unknown_dynamic_file, 'a') as f:
            json.dump(dynamic_data, f, indent=2, ensure_ascii=False)
        return None
    """
    return {
        'mid': author_info['mid'],
        'name': author_info['name'],
        'type': dynamic_type,
        'time': int(author_info['pub_ts']),
        'text': parsed_text, # 纯文本动态
        'msg': parsed_message, # 多媒体动态
        'url': ('https:' + dynamic_url) if len(dynamic_url) else '',
        'orig': original_dynamic_result, # 转发的原始动态
    }

# Load cookies from file
def fetch_cookies(cookie_file):
    cookie_data = {
        'sessdata': None,
        'bili_jct': None,
        'buvid3': None,
        'dedeuserid': None
    }
    with open(cookie_file, 'r') as file:
        json_data = json.load(file)
        for cookie_item in json_data:
            cookie_name = cookie_item['name'].lower()
            if cookie_name in cookie_data:
                cookie_data[cookie_name] = cookie_item['value']
    return cookie_data

# Get credential information
def get_credential(cookie_file: str):
    cookies = fetch_cookies(cookie_file)
    # Generate a Credential object
    return Credential(**cookies)

# Convert {qq: [mid]} format to {mid: [qq]} format
def convert_by_group(group_config: dict, normal_config: dict):
    for group_id, up_list in group_config.items():
        for up_mid in up_list:
            if up_mid in normal_config:
                normal_config[up_mid].append(group_id)
            else:
                normal_config[up_mid] = [group_id]
                
# Check if in blacklist
def is_in_blacklist(mid_or_group: str, dynamic_type: str):
    if dynamic_type in config.bnotifier_push_type_blacklist.get(mid_or_group, {}):
        logger.info(f'屏蔽了{mid_or_group}的{dynamic_type}，不推送')
        return True
    return False

config = get_plugin_config(Config)
logger.debug(config)
credential = get_credential(config.bnotifier_cookies)
bnotifier_like = set(config.bnotifier_like)

last_update_file = get_cache_file('bilibili-notifier', 'last_update.json')

try:
    with open(last_update_file, 'r') as file:
        last_update_timestamp = json.load(file)['last_update']
    last_update_datetime = datetime.datetime.fromtimestamp(last_update_timestamp)
    logger.info(f'加载上次更新时间{last_update_datetime}({last_update_timestamp})')
except:
    logger.warning('未找到上次更新时间，使用当前时间')
    last_update_timestamp = int(time.time())
    
last_live_users = None

# Set API timeout
settings.timeout = config.bnotifier_api_timeout

# Convert group-based config to standard config
convert_by_group(config.bnotifier_push_updates_by_group, config.bnotifier_push_updates)
convert_by_group(config.bnotifier_push_lives_by_group, config.bnotifier_push_lives)

def clean_url(url: str) -> str:
    # Bilibili now adds weird parameters to URLs
    return url.split('?')[0]

def shorten_plain_text(text: str) -> str:
    max_length = config.bnotifier_msg_truncate
    current_length = 0
    shortened_lines = []
    for line in text.split('\n'):
        if current_length + len(line) > max_length:
            return '\n'.join(shortened_lines) + '\n[点击下方链接查看全文]'
        shortened_lines.append(line)
        current_length += len(line)
    return text

def shorten_message_list(message_list: list) -> list:
    max_length = config.bnotifier_msg_truncate
    current_length = 0
    shortened_message = []
    for message_part in message_list:
        if not isinstance(message_part, Text):
            shortened_message.append(message_part)
            # only count text message
            continue
        shortened_lines = []
        for line in str(message_part).split('\n'):
            if current_length + len(line) > max_length:
                shortened_message.append(Text('\n'.join(shortened_lines) + '\n[点击下方链接查看全文]'))
                return shortened_message
            shortened_lines.append(line)
            current_length += len(line)
        shortened_message.append(message_part)
    return shortened_message

def prepare_message_list(dynamic_result: dict):
    cleaned_url = clean_url(dynamic_result['url'])
    if dynamic_result['msg'] is None:
        # 纯文本动态，现在基本不用了，发送的消息是一条文本
        short_message = f"{dynamic_result['name']} {shorten_plain_text(dynamic_result['text'])}\n{cleaned_url}"
        full_message = f"{dynamic_result['name']} {dynamic_result['text']}\n{cleaned_url}"
        short_message_segments = [Text(short_message)]
        full_message_segments = [Text(full_message)]
    else:
        # 多媒体动态，使用这个，发送的是带表情包的聊天记录
        short_message_segments = [Text(f"{dynamic_result['name']} ")] + shorten_message_list(dynamic_result['msg'])
        short_message_segments.append(Text(f"\n{cleaned_url}"))
        
        full_message_segments = [Text(f"{dynamic_result['name']} ")] + dynamic_result['msg']
    return short_message_segments, full_message_segments


@scheduler.scheduled_job('interval', seconds=config.bnotifier_dynamic_update_interval)
async def fetch_bilibili_updates():
    global last_update_timestamp
    
    if len(config.bnotifier_push_updates) == 0:
        return

    dynamic_list = await get_dynamic_page_info(credential)
    dynamic_timestamps = []
    
    for index, dynamic_item in enumerate(dynamic_list):
        logger.debug(f'处理第{index + 1}条动态')
        
        parsed_dynamic = parse_dynamic(dynamic_item)
        if parsed_dynamic is None:
            continue
        
        up_mid = str(parsed_dynamic['mid'])
        up_name = parsed_dynamic['name']
        
        # Handle auto-like feature
        if up_mid in bnotifier_like or up_name in bnotifier_like:
            dynamic_id = int(dynamic_item['id_str'])
            if dynamic_item['modules']['module_stat']['like']['status']:
                # already liked
                logger.debug(f'已经给 {up_name} 的 {dynamic_id} 点赞了')
            else:
                await Dynamic(dynamic_id, credential=credential).set_like(True)
                logger.info(f'给 {up_name} 的 {dynamic_id} 点赞了')
                
        # Handle push feature
        if up_mid in config.bnotifier_push_updates and parsed_dynamic['time'] > last_update_timestamp:
            dynamic_type = parsed_dynamic['type']
            if is_in_blacklist(up_mid, dynamic_type):
                continue
            
            """
            如果需要限制长度，使用short_msg和original_short_msg，现在默认为全文本
            """
            short_msg, full_msg = prepare_message_list(parsed_dynamic)
            messages_to_send = MessageFactory(full_msg) # 聊天记录中的第一条消息
            
            # Handle forwarded dynamics
            if parsed_dynamic['orig'] is not None:
                _, original_full_msg = prepare_message_list(parsed_dynamic['orig'])
                messages_to_send.append(MessageFactory('被转发的动态:')) # 聊天记录中的第二条消息，如果有转发消息的话
                messages_to_send.append(MessageFactory(original_full_msg))
            
            # 聊天记录中的最后一条消息，发送的动态的链接
            messages_to_send.append(MessageFactory('动态链接：' + clean_url(parsed_dynamic['url'])))
            
            # Send to groups
            for group_id in config.bnotifier_push_updates[up_mid]:
                if is_in_blacklist(group_id, dynamic_type):
                    continue
                logger.info(f'将 {up_name} 的更新消息推送到群{group_id}')
                await AggregatedMessageFactory(messages_to_send).send_to(TargetQQGroup(group_id=int(group_id)))
                
            # Send to debug users
            for debug_user_id in config.bnotifier_debug_user:
                logger.info(f'将 {up_name} 的更新消息推送到用户{debug_user_id}')
                await AggregatedMessageFactory(messages_to_send).send_to(TargetQQPrivate(user_id=int(debug_user_id)))
             
        dynamic_timestamps.append(parsed_dynamic["time"])
        
    # Update last update timestamp
    if (latest_timestamp := max(dynamic_timestamps)) > last_update_timestamp:
        last_update_timestamp = latest_timestamp
        with open(last_update_file, 'w') as file:
            json.dump({'last_update': latest_timestamp}, file)
        logger.debug(f'刷新动态更新时间为{latest_timestamp}({last_update_timestamp})')


# 每29秒更新一次
@scheduler.scheduled_job('interval', seconds=config.bnotifier_live_update_interval)
async def fetch_bilibili_live_info():
    global last_live_users
    
    if len(config.bnotifier_push_lives) == 0:
        return
    
    live_info = await get_live_users(credential=credential, size=50)
    
    if live_info['count'] == 0 or 'items' not in live_info:
        # Handle weird network errors
        return
        
    currently_live_users = []
    currently_live_usernames = []
    
    for index, live_user in enumerate(live_info['items']):
        up_mid = str(live_user['uid'])
        up_name = live_user['uname']
        
        currently_live_users.append(up_mid)
        currently_live_usernames.append(up_name)
        
        if up_mid in config.bnotifier_push_lives and last_live_users is not None:
            # Don't notify if already live
            if up_mid in last_live_users:
                continue
                
            # Title is now empty for some reason
            room_url = clean_url(live_user['link'])
            live_notification_msg = [Text(f"{up_name} 开始直播了：{room_url}")]
            try:
                room_cls = LiveRoom(int(re.match(r'https://live.bilibili.com/(\d+)', room_url).group(1)), credential=credential)
                room_info = await room_cls.get_room_info()
                live_notification_msg.append(Text('\n标题：' + room_info['room_info']['title']))
                live_notification_msg.append(Image(room_info['room_info']['cover']))
            except Exception as e:
                pass
            for group_id in config.bnotifier_push_lives[up_mid]:
                logger.info(f'将 {up_name} 的开播消息推送到群{group_id}')
                await MessageFactory(live_notification_msg).send_to(TargetQQGroup(group_id=int(group_id)))
            
            for debug_user_id in config.bnotifier_debug_user:
                logger.debug(f'将 {up_name} 的开播消息推送到用户{debug_user_id}')
                debug_target = TargetQQPrivate(user_id=int(debug_user_id))
                await MessageFactory(live_notification_msg).send_to(debug_target)
                
    last_live_users = set(currently_live_users)
    logger.debug(f'{live_info["count"]}个用户正在直播：{", ".join(currently_live_usernames)}')