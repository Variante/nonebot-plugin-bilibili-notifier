import datetime
import json
import re
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from bilibili_api import Credential, request_settings
from bilibili_api.dynamic import Dynamic, get_live_users
from bilibili_api.live import LiveRoom
from bilibili_api.utils.network import Api
from bilibili_api.utils.utils import get_api
from nonebot.log import logger
from nonebot_plugin_localstore import get_cache_file
from nonebot_plugin_saa import (
    AggregatedMessageFactory,
    Image,
    MessageFactory,
    TargetQQGroup,
    TargetQQPrivate,
    Text,
)

from .config import Config
from .models import ParsedDynamic
from .parser import parse_dynamic

BILIBILI_DYNAMIC_API = get_api("dynamic")
ROOM_URL_PATTERN = re.compile(r"https?://live.bilibili.com/(\d+)")
COOKIE_KEYS = ("sessdata", "bili_jct", "buvid3", "dedeuserid")


def _as_str(value: Any) -> str:
    if value is None:
        return ""
    return str(value)


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _coerce_sequence(value: Any) -> List[Any]:
    if value is None:
        return []
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return list(value)
    return [value]


def clean_url(url: str) -> str:
    return _as_str(url).split("?", 1)[0]


def _normalize_up_to_group_mapping(raw_mapping: Any) -> Dict[str, List[str]]:
    normalized: Dict[str, List[str]] = {}
    if not isinstance(raw_mapping, Mapping):
        return normalized

    for raw_up_mid, raw_group_ids in raw_mapping.items():
        up_mid = _as_str(raw_up_mid).strip()
        if not up_mid:
            continue

        group_list = normalized.setdefault(up_mid, [])
        for raw_group_id in _coerce_sequence(raw_group_ids):
            group_id = _as_str(raw_group_id).strip()
            if group_id and group_id not in group_list:
                group_list.append(group_id)

    return normalized


def _merge_group_to_up_mapping(base_mapping: Dict[str, List[str]], group_mapping: Any) -> Dict[str, List[str]]:
    merged = {up_mid: list(group_ids) for up_mid, group_ids in base_mapping.items()}
    if not isinstance(group_mapping, Mapping):
        return merged

    for raw_group_id, raw_up_mids in group_mapping.items():
        group_id = _as_str(raw_group_id).strip()
        if not group_id:
            continue

        for raw_up_mid in _coerce_sequence(raw_up_mids):
            up_mid = _as_str(raw_up_mid).strip()
            if not up_mid:
                continue

            group_list = merged.setdefault(up_mid, [])
            if group_id not in group_list:
                group_list.append(group_id)

    return merged


def _normalize_type_blacklist(raw_blacklist: Any) -> Dict[str, Set[str]]:
    normalized: Dict[str, Set[str]] = {}
    if not isinstance(raw_blacklist, Mapping):
        return normalized

    for raw_target, raw_types in raw_blacklist.items():
        target = _as_str(raw_target).strip()
        if not target:
            continue

        dynamic_types = {
            _as_str(dynamic_type).strip()
            for dynamic_type in _coerce_sequence(raw_types)
            if _as_str(dynamic_type).strip()
        }
        if dynamic_types:
            normalized[target] = dynamic_types

    return normalized


def _apply_cookie_items(raw_cookie_items: Any, cookie_data: Dict[str, Optional[str]]) -> None:
    for cookie_item in _coerce_sequence(raw_cookie_items):
        if not isinstance(cookie_item, Mapping):
            continue

        cookie_name = _as_str(cookie_item.get("name")).lower().strip()
        if cookie_name not in cookie_data:
            continue

        cookie_value = _as_str(cookie_item.get("value")).strip()
        if cookie_value:
            cookie_data[cookie_name] = cookie_value


def load_cookie_data(cookie_file: str) -> Dict[str, Optional[str]]:
    cookie_data: Dict[str, Optional[str]] = {key: None for key in COOKIE_KEYS}

    cookie_path = Path(cookie_file)
    with cookie_path.open("r", encoding="utf-8") as file:
        raw_content = json.load(file)

    if isinstance(raw_content, Mapping):
        lowered_mapping = {
            _as_str(key).lower().strip(): _as_str(value).strip()
            for key, value in raw_content.items()
            if _as_str(key).strip()
        }
        for cookie_key in COOKIE_KEYS:
            cookie_value = lowered_mapping.get(cookie_key, "")
            if cookie_value:
                cookie_data[cookie_key] = cookie_value

        if "cookies" in raw_content:
            _apply_cookie_items(raw_content.get("cookies"), cookie_data)

    elif isinstance(raw_content, list):
        _apply_cookie_items(raw_content, cookie_data)

    if not cookie_data.get("sessdata"):
        logger.warning("未在 cookies 文件中找到 SESSDATA，可能无法访问完整动态信息")

    return cookie_data


def build_credential(cookie_file: str) -> Credential:
    return Credential(**load_cookie_data(cookie_file))


class BilibiliNotifierService:
    def __init__(self, config: Config):
        self.config = config
        self.credential = build_credential(config.bnotifier_cookies)

        request_settings.timeout = _as_float(config.bnotifier_api_timeout, default=20.0)

        self.update_targets = _merge_group_to_up_mapping(
            _normalize_up_to_group_mapping(config.bnotifier_push_updates),
            config.bnotifier_push_updates_by_group,
        )
        self.live_targets = _merge_group_to_up_mapping(
            _normalize_up_to_group_mapping(config.bnotifier_push_lives),
            config.bnotifier_push_lives_by_group,
        )
        self.type_blacklist = _normalize_type_blacklist(config.bnotifier_push_type_blacklist)

        self.like_targets = {
            _as_str(target).strip()
            for target in config.bnotifier_like
            if _as_str(target).strip()
        }
        self.debug_users = [
            _as_str(user_id).strip()
            for user_id in config.bnotifier_debug_user
            if _as_str(user_id).strip()
        ]

        state_filename = _as_str(config.bnotifier_state_file).strip() or "last_update.json"
        self.state_file = get_cache_file("bilibili-notifier", state_filename)

        self.last_update_timestamp = self._resolve_start_timestamp()
        self.dynamic_like_blacklist: Set[int] = set()
        self.last_live_users: Optional[Set[str]] = None

        self._load_state()

    def _resolve_start_timestamp(self) -> int:
        if self.config.bnotifier_ignore_old_dynamic_on_start:
            return int(time.time())
        return 0

    def _load_state(self) -> None:
        if not self.config.bnotifier_persist_state:
            logger.info("状态持久化已关闭，运行时不会读写动态状态缓存")
            return

        try:
            with open(self.state_file, "r", encoding="utf-8") as file:
                state_data = json.load(file)

            saved_timestamp = _as_int(state_data.get("last_update"), default=self.last_update_timestamp)
            raw_blacklist = state_data.get("dyna_blacklist", [])
            parsed_blacklist = {
                _as_int(dynamic_id, default=-1)
                for dynamic_id in _coerce_sequence(raw_blacklist)
            }

            self.last_update_timestamp = saved_timestamp
            self.dynamic_like_blacklist = {
                dynamic_id for dynamic_id in parsed_blacklist if dynamic_id > 0
            }

            last_update_text = datetime.datetime.fromtimestamp(self.last_update_timestamp).strftime("%Y-%m-%d %H:%M:%S")
            logger.info(
                f"加载上次更新时间：{last_update_text}（{self.last_update_timestamp}）"
            )
        except FileNotFoundError:
            logger.warning("未找到上次更新时间缓存，使用启动策略初始化时间戳")
        except Exception as error:
            logger.warning(f"读取状态缓存失败，使用启动策略初始化时间戳：{error}")

    def _save_state(self) -> None:
        if not self.config.bnotifier_persist_state:
            return

        payload = {
            "last_update": self.last_update_timestamp,
            "dyna_blacklist": sorted(self.dynamic_like_blacklist),
        }
        with open(self.state_file, "w", encoding="utf-8") as file:
            json.dump(payload, file)

    def _is_type_blocked(self, target: str, dynamic_type: str) -> bool:
        if dynamic_type in self.type_blacklist.get(target, set()):
            logger.info(f"屏蔽了 {target} 的 {dynamic_type}，不推送")
            return True
        return False

    def _parse_qq_id(self, raw_id: str, id_name: str) -> Optional[int]:
        try:
            return int(raw_id)
        except (TypeError, ValueError):
            logger.warning(f"{id_name} 配置无效：{raw_id}")
            return None

    async def _get_dynamic_page_items(self, page_number: int) -> List[Dict[str, Any]]:
        api = BILIBILI_DYNAMIC_API["info"]["dynamic_page_info"]
        params = {
            "timezone_offset": self.config.bnotifier_timezone_offset,
            "features": self.config.bnotifier_dynamic_features,
            "page": page_number,
        }
        response = await Api(**api, credential=self.credential).update_params(**params).result

        items = response.get("items", [])
        if isinstance(items, list):
            return items
        return []

    async def _collect_dynamic_items(self) -> List[Dict[str, Any]]:
        pages = max(1, _as_int(self.config.bnotifier_dynamic_pages, default=1))
        all_items: List[Dict[str, Any]] = []
        seen_dynamic_ids: Set[str] = set()

        for page_number in range(1, pages + 1):
            page_items = await self._get_dynamic_page_items(page_number)
            if not page_items:
                break

            for item in page_items:
                dynamic_id = _as_str(item.get("id_str")).strip()
                if dynamic_id:
                    if dynamic_id in seen_dynamic_ids:
                        continue
                    seen_dynamic_ids.add(dynamic_id)
                all_items.append(item)

        return all_items

    async def _get_dynamic_item_by_id(self, dynamic_id: int) -> Optional[Dict[str, Any]]:
        dynamic_data = await Dynamic(dynamic_id, credential=self.credential).get_info()
        if not isinstance(dynamic_data, dict):
            return None

        item = dynamic_data.get("item")
        if isinstance(item, dict):
            return item

        if isinstance(dynamic_data.get("modules"), dict):
            return dynamic_data
        return None

    def _should_auto_like(self, parsed_dynamic: ParsedDynamic) -> bool:
        return (
            parsed_dynamic.mid in self.like_targets
            or parsed_dynamic.name in self.like_targets
        )

    async def _try_auto_like(
        self,
        dynamic_item: Dict[str, Any],
        parsed_dynamic: ParsedDynamic,
        next_blacklist: Set[int],
    ) -> None:
        if not self._should_auto_like(parsed_dynamic):
            return

        dynamic_id = _as_int(dynamic_item.get("id_str"), default=0)
        if dynamic_id <= 0:
            return

        if dynamic_id in self.dynamic_like_blacklist:
            next_blacklist.add(dynamic_id)
            return

        modules = dynamic_item.get("modules", {})
        module_stat = modules.get("module_stat", {}) if isinstance(modules, dict) else {}
        like_info = module_stat.get("like", {}) if isinstance(module_stat, dict) else {}
        has_liked = bool(like_info.get("status")) if isinstance(like_info, dict) else False

        if has_liked:
            return

        try:
            response = await Dynamic(dynamic_id, credential=self.credential).set_like(True)
        except Exception as error:
            logger.warning(f"给 {parsed_dynamic.name} 的 {dynamic_id} 点赞失败：{error}")
            next_blacklist.add(dynamic_id)
            return

        if isinstance(response, dict) and response.get("code", 1) == 0:
            logger.info(f"给 {parsed_dynamic.name} 的 {dynamic_id} 点赞了")
            return

        logger.warning(f"给 {parsed_dynamic.name} 的 {dynamic_id} 点赞失败")
        next_blacklist.add(dynamic_id)

    def _build_dynamic_message_segments(self, dynamic: ParsedDynamic) -> List[Any]:
        return [Text(f"{dynamic.name} ")] + list(dynamic.message)

    def _build_dynamic_notification(self, dynamic: ParsedDynamic) -> AggregatedMessageFactory:
        messages = [
            MessageFactory(
                self._build_dynamic_message_segments(dynamic)
            )
        ]

        if dynamic.origin and self.config.bnotifier_forward_message_mode != "none":
            messages.append(MessageFactory("被转发的动态:"))
            messages.append(
                MessageFactory(
                    self._build_dynamic_message_segments(dynamic.origin)
                )
            )

        dynamic_url = clean_url(dynamic.url)
        if dynamic_url:
            messages.append(MessageFactory(f"动态链接：{dynamic_url}"))

        return AggregatedMessageFactory(messages)

    async def _send_update_notification(self, dynamic: ParsedDynamic) -> None:
        message = self._build_dynamic_notification(dynamic)

        for debug_user_id in self.debug_users:
            user_id = self._parse_qq_id(debug_user_id, "调试用户")
            if user_id is None:
                continue
            logger.info(f"将 {dynamic.name} 的更新消息推送到用户 {user_id}")
            await message.send_to(TargetQQPrivate(user_id=user_id))

        for group_id in self.update_targets.get(dynamic.mid, []):
            if self._is_type_blocked(group_id, dynamic.dynamic_type):
                continue

            qq_group_id = self._parse_qq_id(group_id, "QQ群")
            if qq_group_id is None:
                continue

            logger.info(f"将 {dynamic.name} 的更新消息推送到群 {qq_group_id}")
            await message.send_to(TargetQQGroup(group_id=qq_group_id))

    async def fetch_bilibili_updates(self) -> None:
        if not self.update_targets and not self.like_targets:
            return

        try:
            dynamic_items = await self._collect_dynamic_items()
        except Exception as error:
            logger.warning(f"获取动态列表失败：{error}")
            return

        if not dynamic_items:
            return

        dynamic_timestamps: List[int] = []
        next_like_blacklist: Set[int] = set()

        for dynamic_item in dynamic_items:
            try:
                parsed_dynamic = parse_dynamic(dynamic_item)
                if parsed_dynamic is None:
                    continue

                await self._try_auto_like(dynamic_item, parsed_dynamic, next_like_blacklist)

                if parsed_dynamic.timestamp > 0:
                    dynamic_timestamps.append(parsed_dynamic.timestamp)

                if parsed_dynamic.mid not in self.update_targets:
                    continue
                if parsed_dynamic.timestamp <= self.last_update_timestamp:
                    continue
                if self._is_type_blocked(parsed_dynamic.mid, parsed_dynamic.dynamic_type):
                    continue
                if (
                    parsed_dynamic.origin
                    and self.config.bnotifier_skip_lottery_forward
                    and "中奖" in parsed_dynamic.text
                ):
                    logger.info(f"跳过 {parsed_dynamic.name} 的中奖动态：{parsed_dynamic.text}")
                    continue

                await self._send_update_notification(parsed_dynamic)
            except Exception as error:
                logger.warning(f"获取推送动态失败：{error}")

        self.dynamic_like_blacklist = next_like_blacklist

        if dynamic_timestamps:
            latest_timestamp = max(dynamic_timestamps)
            if latest_timestamp > self.last_update_timestamp:
                self.last_update_timestamp = latest_timestamp
                self._save_state()
                logger.debug(f"刷新动态更新时间为 {latest_timestamp}")

    def _extract_room_id(self, room_url: str) -> Optional[int]:
        matched = ROOM_URL_PATTERN.match(room_url)
        if not matched:
            return None
        return _as_int(matched.group(1), default=0) or None

    async def _build_live_notification_message(self, up_name: str, live_user: Dict[str, Any]) -> List[Any]:
        room_url = clean_url(_as_str(live_user.get("link")))
        segments: List[Any] = [Text(f"{up_name} 开始直播了：{room_url}")]

        if not self.config.bnotifier_live_include_title and not self.config.bnotifier_live_include_cover:
            return segments

        room_id = self._extract_room_id(room_url)
        if room_id is None:
            return segments

        try:
            room = LiveRoom(room_id, credential=self.credential)
            room_info = await room.get_room_info()
        except Exception as error:
            logger.debug(f"获取直播详情失败：{error}")
            return segments

        room_detail = room_info.get("room_info", {})
        if not isinstance(room_detail, dict):
            return segments

        if self.config.bnotifier_live_include_title:
            title = _as_str(room_detail.get("title")).strip()
            if title:
                segments.append(Text(f"\n标题：{title}"))

        if self.config.bnotifier_live_include_cover:
            cover = _as_str(room_detail.get("cover")).strip()
            if cover:
                segments.append(Image(cover))

        return segments

    async def _send_live_notification(self, up_mid: str, up_name: str, message_segments: List[Any]) -> None:
        message = MessageFactory(message_segments)

        for debug_user_id in self.debug_users:
            user_id = self._parse_qq_id(debug_user_id, "调试用户")
            if user_id is None:
                continue
            logger.info(f"将 {up_name} 的开播消息推送到用户 {user_id}")
            await message.send_to(TargetQQPrivate(user_id=user_id))

        for group_id in self.live_targets.get(up_mid, []):
            qq_group_id = self._parse_qq_id(group_id, "QQ群")
            if qq_group_id is None:
                continue
            logger.info(f"将 {up_name} 的开播消息推送到群 {qq_group_id}")
            await message.send_to(TargetQQGroup(group_id=qq_group_id))

    async def fetch_bilibili_live_info(self) -> None:
        if not self.live_targets:
            return

        size = max(1, _as_int(self.config.bnotifier_live_fetch_size, default=50))
        try:
            live_info = await get_live_users(credential=self.credential, size=size)
        except Exception as error:
            logger.warning(f"获取直播列表失败：{error}")
            return

        raw_items = live_info.get("items", []) if isinstance(live_info, dict) else []
        if not isinstance(raw_items, list):
            return

        current_live_users: Set[str] = set()
        current_live_names: List[str] = []

        for live_user in raw_items:
            if not isinstance(live_user, dict):
                continue

            try:
                up_mid = _as_str(live_user.get("uid")).strip()
                if not up_mid:
                    continue

                up_name = _as_str(live_user.get("uname")).strip() or up_mid
                current_live_users.add(up_mid)
                current_live_names.append(up_name)

                if up_mid not in self.live_targets:
                    continue
                if self.last_live_users is None:
                    continue
                if up_mid in self.last_live_users:
                    continue

                live_message = await self._build_live_notification_message(up_name, live_user)
                await self._send_live_notification(up_mid, up_name, live_message)
            except Exception as error:
                logger.error(f"获取推送直播信息失败：{error}")

        self.last_live_users = current_live_users
        if current_live_names:
            logger.debug(
                f"{len(current_live_names)} 个用户正在直播：{', '.join(current_live_names)}"
            )

    async def push_dynamic_to_user(self, dynamic_id: int, user_id: str) -> Tuple[bool, str]:
        qq_user_id = self._parse_qq_id(user_id, "调试用户")
        if qq_user_id is None:
            return False, "发送失败：用户ID无效"

        try:
            dynamic_item = await self._get_dynamic_item_by_id(dynamic_id)
        except Exception as error:
            logger.warning(f"按ID获取动态失败（{dynamic_id}）：{error}")
            return False, f"获取动态失败：{error}"

        if dynamic_item is None:
            return False, "获取动态失败：返回结构异常"

        parsed_dynamic = parse_dynamic(dynamic_item)
        if parsed_dynamic is None:
            return False, "该动态类型暂不支持推送"

        if (
            parsed_dynamic.origin
            and self.config.bnotifier_skip_lottery_forward
            and "中奖" in parsed_dynamic.text
        ):
            return False, "该动态命中中奖转发过滤，已跳过"

        try:
            await self._build_dynamic_notification(parsed_dynamic).send_to(
                TargetQQPrivate(user_id=qq_user_id)
            )
        except Exception as error:
            logger.warning(f"按ID推送动态失败（{dynamic_id} -> {qq_user_id}）：{error}")
            return False, f"推送失败：{error}"

        logger.info(f"按ID推送动态成功（{dynamic_id} -> {qq_user_id}）")
        return True, "ok"
