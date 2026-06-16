import asyncio
import json
import re
import struct
import time
import zlib
from collections import deque
from collections.abc import Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable

import aiohttp
import brotli
from bilibili_api import Credential, request_settings
from bilibili_api.dynamic import Dynamic
from bilibili_api.live import LiveRoom
from bilibili_api.user import User
from bilibili_api.utils.network import Api
from bilibili_api.utils.utils import get_api
from nonebot import get_bot, get_driver
from nonebot.adapters.onebot.v11 import Bot, Message, MessageSegment
from nonebot.log import logger
from nonebot_plugin_localstore import get_cache_file

from .config import Config
from .models import ParsedDynamic
from .parser import parse_dynamic
from .utils import _as_int, _as_str

BILIBILI_DYNAMIC_API = get_api("dynamic")
BILIBILI_USER_API = get_api("user")
ROOM_URL_PATTERN = re.compile(r"https?://live.bilibili.com/(\d+)")
COOKIE_KEYS = ("sessdata", "bili_jct", "buvid3", "dedeuserid")
MAX_AUTO_LIKE_ATTEMPT_CACHE_SIZE = 5000
IntMessageSender = Callable[[int], Awaitable[None]]

LIVE_WS_HEADERS = {
    "Accept-Language": "zh-CN,zh;q=0.9",
    "Cache-Control": "no-cache",
    "Connection": "Upgrade",
    "Origin": "https://live.bilibili.com",
    "Pragma": "no-cache",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/136.0.0.0 Safari/537.36"
    ),
}


@dataclass(frozen=True)
class LiveMonitorTarget:
    room_id: int
    up_mid: str
    up_name: str


@dataclass
class LiveMonitorStatus:
    target: LiveMonitorTarget
    state: str
    updated_at: float
    connected_at: float | None = None
    last_error: str | None = None
    reconnect_count: int = 0


@dataclass
class LiveSessionState:
    start_time: int = 0
    start_notified: bool = False
    suppressed: bool = False
    last_confirmed_end_time: int = 0


class BilibiliLiveStatusWebsocket:
    HEADER_FORMAT = ">IHHII"
    HEADER_LENGTH = 16
    PROTOCOL_NORMAL = 0
    PROTOCOL_HEARTBEAT = 1
    PROTOCOL_ZLIB = 2
    PROTOCOL_BROTLI = 3
    OP_HEARTBEAT = 2
    OP_HEARTBEAT_REPLY = 3
    OP_MESSAGE = 5
    OP_AUTH = 7
    OP_AUTH_REPLY = 8

    def __init__(self, target: LiveMonitorTarget, credential: Credential) -> None:
        self.target = target
        self.credential = credential
        self.room_real_id = target.room_id

    async def connect(
        self,
        on_live: Callable[[dict[str, Any]], Awaitable[None]],
        on_preparing: Callable[[dict[str, Any]], Awaitable[None]],
        on_connected: Callable[[bool, int], Awaitable[None]],
    ) -> None:
        room = LiveRoom(self.target.room_id, credential=self.credential)
        room_play_info = await room.get_room_play_info()
        self.room_real_id = _as_int(room_play_info.get("room_id"), default=self.target.room_id)

        room_detail: dict[str, Any] = {}
        try:
            room_info = await room.get_room_info()
            raw_room_detail = room_info.get("room_info", {}) if isinstance(room_info, dict) else {}
            if isinstance(raw_room_detail, dict):
                room_detail = raw_room_detail
        except Exception as error:
            logger.debug(f"获取 {self.target.up_name} 初始直播详情失败：{error}")

        # live_status: 1 = live, 0 = preparing, 2 = round
        initial_live_status = _as_int(room_detail.get("live_status"), default=-1)
        if initial_live_status < 0:
            initial_live_status = _as_int(room_play_info.get("live_status"), default=0)
        initially_live = initial_live_status == 1
        initial_live_start_time = _extract_live_start_timestamp(room_detail)
        danmu_info = await room.get_danmu_info()

        host_list = danmu_info.get("host_list", [])
        if not isinstance(host_list, list) or not host_list:
            raise RuntimeError("Bilibili danmu host list is empty")

        last_error: Exception | None = None
        for host_info in host_list:
            try:
                await self._connect_host(
                    host_info,
                    _as_str(danmu_info.get("token")),
                    on_live,
                    on_preparing,
                    on_connected,
                    initially_live=initially_live,
                    initial_live_start_time=initial_live_start_time,
                )
                return
            except asyncio.CancelledError:
                raise
            except Exception as error:
                last_error = error
                logger.warning(f"{self.target.up_name} 直播 websocket 主机连接失败：{error}")

        if last_error is not None:
            raise last_error

    async def _connect_host(
        self,
        host_info: dict[str, Any],
        token: str,
        on_live: Callable[[dict[str, Any]], Awaitable[None]],
        on_preparing: Callable[[dict[str, Any]], Awaitable[None]],
        on_connected: Callable[[bool, int], Awaitable[None]],
        *,
        initially_live: bool = False,
        initial_live_start_time: int = 0,
    ) -> None:
        host = _as_str(host_info.get("host")).strip()
        port = _as_int(host_info.get("wss_port"), default=443)
        if not host:
            raise RuntimeError("Bilibili danmu host is empty")

        url = f"wss://{host}:{port}/sub"
        async with aiohttp.ClientSession() as session:
            async with session.ws_connect(url, headers=LIVE_WS_HEADERS, timeout=10) as ws:
                heartbeat_task: asyncio.Task[None] | None = None
                receive_task: asyncio.Task[aiohttp.WSMessage] | None = None
                try:
                    await ws.send_bytes(self._encode(self.OP_AUTH, self._build_auth_payload(token)))
                    auth_msg = await asyncio.wait_for(ws.receive(), timeout=10)
                    if auth_msg.type != aiohttp.WSMsgType.BINARY:
                        raise RuntimeError(f"WebSocket closed during auth: {auth_msg.type}")
                    self._check_auth_reply(auth_msg.data)
                    await on_connected(initially_live, initial_live_start_time)
                    heartbeat_task = asyncio.create_task(self._heartbeat(ws))
                    while True:
                        receive_task = asyncio.create_task(ws.receive())
                        done, pending = await asyncio.wait(
                            {receive_task, heartbeat_task},
                            timeout=65,
                            return_when=asyncio.FIRST_COMPLETED,
                        )
                        for task in pending:
                            if task is receive_task:
                                task.cancel()

                        if heartbeat_task in done:
                            if receive_task in pending:
                                with suppress(asyncio.CancelledError):
                                    await receive_task
                            heartbeat_task.result()

                        if receive_task not in done:
                            with suppress(asyncio.CancelledError):
                                await receive_task
                            logger.debug(f"{self.target.up_name} 直播 websocket 接收超时，继续等待")
                            continue

                        message = receive_task.result()

                        if message.type == aiohttp.WSMsgType.BINARY:
                            for payload in self._decode_messages(message.data):
                                command = _as_str(payload.get("cmd"))
                                if command == "LIVE":
                                    await on_live(payload)
                                elif command == "PREPARING":
                                    await on_preparing(payload)
                        elif message.type == aiohttp.WSMsgType.ERROR:
                            raise RuntimeError(f"websocket error: {ws.exception()}")
                        elif message.type in {
                            aiohttp.WSMsgType.CLOSE,
                            aiohttp.WSMsgType.CLOSED,
                            aiohttp.WSMsgType.CLOSING,
                        }:
                            raise RuntimeError(f"websocket closed: {message.type}")
                finally:
                    await _cancel_task_with_timeout(
                        receive_task,
                        timeout=1,
                        task_name=f"{self.target.up_name} websocket receive",
                    )
                    await _cancel_task_with_timeout(
                        heartbeat_task,
                        timeout=1,
                        task_name=f"{self.target.up_name} websocket heartbeat",
                    )

    def _build_auth_payload(self, token: str) -> str:
        payload = {
            "uid": _as_int(self.credential.dedeuserid, default=0),
            "roomid": self.room_real_id,
            "protover": self.PROTOCOL_BROTLI,
            "buvid": self.credential.buvid3 or "",
            "platform": "web",
            "type": 2,
            "key": token,
        }
        return json.dumps(payload, separators=(",", ":"))

    async def _heartbeat(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        heartbeat = self._encode(self.OP_HEARTBEAT, "")
        while True:
            await ws.send_bytes(heartbeat)
            await asyncio.sleep(30)

    def _encode(self, op: int, message: str) -> bytes:
        body = message.encode("utf-8")
        packet_length = self.HEADER_LENGTH + len(body)
        header = struct.pack(
            self.HEADER_FORMAT,
            packet_length,
            self.HEADER_LENGTH,
            self.PROTOCOL_HEARTBEAT,
            op,
            1,
        )
        return header + body

    def _check_auth_reply(self, data: bytes) -> None:
        if len(data) < self.HEADER_LENGTH:
            return
        _, header_length, _, op, _ = struct.unpack_from(self.HEADER_FORMAT, data, 0)
        if op != self.OP_AUTH_REPLY:
            logger.debug(f"直播 websocket auth 期间收到意外数据包：op={op}")
            return
        body = data[header_length:]
        try:
            reply = json.loads(body.decode("utf-8"))
            code = reply.get("code", 0)
            if code != 0:
                raise RuntimeError(f"Bilibili WebSocket 认证失败（可能 cookies 已过期）：{reply}")
        except json.JSONDecodeError:
            pass

    def _decode_messages(self, data: bytes) -> list[dict[str, Any]]:
        messages: list[dict[str, Any]] = []
        offset = 0
        while offset + self.HEADER_LENGTH <= len(data):
            packet_length, header_length, protocol, op, _ = struct.unpack_from(
                self.HEADER_FORMAT,
                data,
                offset,
            )
            if packet_length <= 0:
                break

            body = data[offset + header_length: offset + packet_length]
            offset += packet_length

            if op == self.OP_MESSAGE and protocol == self.PROTOCOL_NORMAL:
                try:
                    payload = json.loads(body.decode("utf-8"))
                except Exception as error:
                    logger.debug(f"解析直播 websocket 消息失败：{error}")
                    continue
                if isinstance(payload, dict):
                    messages.append(payload)
            elif op == self.OP_MESSAGE and protocol == self.PROTOCOL_BROTLI:
                try:
                    messages.extend(self._decode_messages(brotli.decompress(body)))
                except Exception as error:
                    logger.debug(f"解压直播 websocket brotli 消息失败：{error}")
            elif op == self.OP_MESSAGE and protocol == self.PROTOCOL_ZLIB:
                try:
                    messages.extend(self._decode_messages(zlib.decompress(body)))
                except Exception as error:
                    logger.debug(f"解压直播 websocket zlib 消息失败：{error}")
            elif op in {self.OP_AUTH_REPLY, self.OP_HEARTBEAT_REPLY}:
                continue
            else:
                logger.debug(f"跳过未处理的直播 websocket 数据包：op={op}, protocol={protocol}")

        return messages


def _consume_task_result(task: asyncio.Task[Any]) -> None:
    with suppress(asyncio.CancelledError, Exception):
        task.result()


async def _cancel_task_with_timeout(
    task: asyncio.Task[Any] | None,
    *,
    timeout: float,
    task_name: str,
) -> None:
    if task is None:
        return
    if task.done():
        _consume_task_result(task)
        return

    task.cancel()
    done, pending = await asyncio.wait({task}, timeout=timeout)
    for done_task in done:
        _consume_task_result(done_task)
    if pending:
        logger.warning(f"{task_name}停止超时：未在 {timeout:g} 秒内退出")


def _extract_live_start_timestamp(live_data: Mapping[str, Any]) -> int:
    live_start_time = _as_int(live_data.get("live_start_time"), default=0)
    if live_start_time > 0:
        return live_start_time

    live_time = _as_str(live_data.get("live_time")).strip()
    if not live_time or live_time == "0000-00-00 00:00:00":
        return 0

    try:
        return int(time.mktime(time.strptime(live_time, "%Y-%m-%d %H:%M:%S")))
    except (ValueError, OverflowError):
        return 0


def _get_live_start_timestamp(live_data: Mapping[str, Any], *, default_to_now: bool = False) -> int:
    live_start_time = _extract_live_start_timestamp(live_data)
    if live_start_time > 0:
        return live_start_time
    if default_to_now:
        return int(time.time())
    return 0


def _coerce_sequence(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return list(value)
    return [value]


def clean_url(url: str) -> str:
    return _as_str(url).split("?", 1)[0]


def _format_live_timestamp(timestamp: int | float | None) -> str:
    if not timestamp:
        return "-"
    try:
        timestamp_int = int(timestamp)
    except (TypeError, ValueError):
        return _as_str(timestamp)
    formatted_time = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(timestamp_int))
    return f"{formatted_time} ({timestamp_int})"


def _format_message_segments_for_log(message_segments: Sequence[MessageSegment]) -> str:
    formatted_segments: list[dict[str, str]] = []
    for segment in message_segments:
        if segment.type == "text":
            formatted_segments.append(
                {
                    "type": "text",
                    "text": _as_str(segment.data.get("text")),
                }
            )
        elif segment.type == "image":
            formatted_segments.append(
                {
                    "type": "image",
                    "file": _as_str(segment.data.get("file")),
                }
            )
        else:
            formatted_segments.append(
                {
                    "type": segment.type,
                    "data": json.dumps(segment.data, ensure_ascii=False, default=str),
                }
            )
    return json.dumps(formatted_segments, ensure_ascii=False)


def _seg_to_saa(seg: MessageSegment):
    from nonebot_plugin_saa import Text, Image
    if seg.type == "text":
        return Text(seg.data["text"])
    return Image(seg.data.get("file", ""))


def _add_group_id(group_ids: list[str], group_id: str) -> None:
    if group_id and group_id not in group_ids:
        group_ids.append(group_id)


def _build_targets(up_mapping: Any, group_mapping: Any) -> dict[str, list[str]]:
    targets: dict[str, list[str]] = {}

    if isinstance(up_mapping, Mapping):
        for raw_up_mid, raw_group_ids in up_mapping.items():
            up_mid = _as_str(raw_up_mid).strip()
            if not up_mid:
                continue

            group_ids = targets.setdefault(up_mid, [])
            for raw_group_id in _coerce_sequence(raw_group_ids):
                _add_group_id(group_ids, _as_str(raw_group_id).strip())

    if isinstance(group_mapping, Mapping):
        for raw_group_id, raw_up_mids in group_mapping.items():
            group_id = _as_str(raw_group_id).strip()

            for raw_up_mid in _coerce_sequence(raw_up_mids):
                up_mid = _as_str(raw_up_mid).strip()
                if not up_mid:
                    continue

                _add_group_id(targets.setdefault(up_mid, []), group_id)

    return targets


def _normalize_type_blacklist(raw_blacklist: Any) -> dict[str, set[str]]:
    normalized: dict[str, set[str]] = {}
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


def _apply_cookie_items(raw_cookie_items: Any, cookie_data: dict[str, str | None]) -> None:
    for cookie_item in _coerce_sequence(raw_cookie_items):
        if not isinstance(cookie_item, Mapping):
            continue

        cookie_name = _as_str(cookie_item.get("name")).lower().strip()
        if cookie_name not in cookie_data:
            continue

        cookie_value = _as_str(cookie_item.get("value")).strip()
        if cookie_value:
            cookie_data[cookie_name] = cookie_value


def load_cookie_data(cookie_file: str) -> dict[str, str | None]:
    cookie_data: dict[str, str | None] = {key: None for key in COOKIE_KEYS}

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
        self.cookie_load_error: str | None = None
        try:
            self.credential = build_credential(config.bnotifier_cookies)
        except Exception as error:
            self.cookie_load_error = f"读取 B 站 cookies 文件失败：{error}"
            logger.warning(self.cookie_load_error)
            self.credential = Credential()
        self._startup_cookie_checked = False

        request_settings.timeout = config.bnotifier_api_timeout

        self.update_targets = _build_targets(
            config.bnotifier_push_updates,
            config.bnotifier_push_updates_by_group,
        )
        self.live_targets = _build_targets(
            config.bnotifier_push_lives,
            config.bnotifier_push_lives_by_group,
        )
        self.type_blacklist = _normalize_type_blacklist(config.bnotifier_push_type_blacklist)

        self.like_targets = {
            target.strip()
            for target in config.bnotifier_like
            if target.strip()
        }
        self.debug_users = [
            user_id.strip()
            for user_id in config.bnotifier_debug_user
            if user_id.strip()
        ]

        state_filename = config.bnotifier_state_file.strip() or "last_update.json"
        self.state_file = get_cache_file("bilibili-notifier", state_filename)

        self.last_seen_dynamic_timestamp = 0
        self.last_auto_like_dynamic_timestamp = 0
        self.last_dynamic_check_timestamp = 0
        self._auto_like_attempted_keys: set[str] = set()
        self._auto_like_attempt_order: deque[str] = deque()
        self.live_sessions: dict[str, LiveSessionState] = {}
        self.notified_live_users: set[str] = set()
        self.current_live_users: set[str] = set()
        self.suppressed_live_users: set[str] = set()
        self.last_live_start_times: dict[str, float] = {}
        self.current_live_start_times: dict[str, int] = {}
        self._live_notification_lock = asyncio.Lock()
        self._pending_live_stop_tasks: dict[str, asyncio.Task[None]] = {}
        self._live_monitor_tasks: dict[int, asyncio.Task[None]] = {}
        self._live_reconcile_tasks: dict[int, asyncio.Task[None]] = {}
        self._live_monitor_status: dict[int, LiveMonitorStatus] = {}
        self._live_monitor_seen_initial_status: set[int] = set()
        self._live_monitors_started = False

        self._load_state()

    def _load_state(self) -> None:
        try:
            with open(self.state_file, "r", encoding="utf-8") as file:
                state_data = json.load(file)

            self.last_seen_dynamic_timestamp = self._parse_state_timestamp(
                state_data.get("last_seen_dynamic_timestamp")
            )
            self.last_auto_like_dynamic_timestamp = self._parse_state_timestamp(
                state_data.get("last_auto_like_dynamic_timestamp")
            )
            self.last_dynamic_check_timestamp = self._parse_state_timestamp(
                state_data.get("last_dynamic_check_timestamp")
            )

            self.live_sessions = self._load_live_sessions(state_data.get("live_sessions"))

            logger.info(
                f"上次检查动态：{self._format_beijing_timestamp(self.last_dynamic_check_timestamp)}"
            )
        except FileNotFoundError:
            logger.warning("未找到状态缓存文件，将在首次拉取时初始化")
        except Exception as error:
            logger.warning(f"读取状态缓存失败：{error}")

    def _save_state(self) -> None:
        payload = {
            "last_seen_dynamic_timestamp": self.last_seen_dynamic_timestamp,
            "last_auto_like_dynamic_timestamp": self.last_auto_like_dynamic_timestamp,
            "last_dynamic_check_timestamp": self.last_dynamic_check_timestamp,
            "live_sessions": self._dump_live_sessions(),
        }
        with open(self.state_file, "w", encoding="utf-8") as file:
            json.dump(payload, file)

    def _load_live_sessions(self, raw_sessions: Any) -> dict[str, LiveSessionState]:
        if not isinstance(raw_sessions, Mapping):
            return {}

        live_sessions: dict[str, LiveSessionState] = {}
        for raw_up_mid, raw_session in raw_sessions.items():
            up_mid = _as_str(raw_up_mid).strip()
            if not up_mid or not isinstance(raw_session, Mapping):
                continue

            session = LiveSessionState(
                start_time=max(0, _as_int(raw_session.get("start_time"), default=0)),
                start_notified=bool(raw_session.get("start_notified")),
                suppressed=bool(raw_session.get("suppressed")),
                last_confirmed_end_time=max(
                    0,
                    _as_int(raw_session.get("last_confirmed_end_time"), default=0),
                ),
            )
            if (
                session.start_time > 0
                or session.start_notified
                or session.suppressed
                or session.last_confirmed_end_time > 0
            ):
                live_sessions[up_mid] = session
        return live_sessions

    def _dump_live_sessions(self) -> dict[str, dict[str, int | bool]]:
        live_sessions: dict[str, dict[str, int | bool]] = {}
        for up_mid, session in self.live_sessions.items():
            if (
                session.start_time <= 0
                and not session.start_notified
                and not session.suppressed
                and session.last_confirmed_end_time <= 0
            ):
                continue

            live_sessions[up_mid] = {
                "start_time": session.start_time,
                "start_notified": session.start_notified,
                "suppressed": session.suppressed,
                "last_confirmed_end_time": session.last_confirmed_end_time,
            }
        return live_sessions

    def _parse_state_timestamp(self, value: Any) -> int:
        return max(0, _as_int(value, default=0))

    def _format_beijing_timestamp(self, timestamp: int) -> str:
        if timestamp <= 0:
            return "无记录"
        formatted_time = time.strftime(
            "%Y-%m-%d %H:%M:%S",
            time.gmtime(timestamp + 8 * 60 * 60),
        )
        return f"{formatted_time} 北京时间"

    def _get_dynamic_timestamp(self, dynamic: ParsedDynamic) -> int:
        return max(0, _as_int(dynamic.timestamp, default=0))

    def _get_auto_like_attempt_key(self, dynamic: ParsedDynamic, timestamp: int) -> str:
        target = dynamic.mid or dynamic.name
        return f"{target}:{timestamp}"

    def _remember_auto_like_attempt(self, attempt_key: str) -> None:
        if attempt_key in self._auto_like_attempted_keys:
            return

        self._auto_like_attempted_keys.add(attempt_key)
        self._auto_like_attempt_order.append(attempt_key)

        while len(self._auto_like_attempt_order) > MAX_AUTO_LIKE_ATTEMPT_CACHE_SIZE:
            expired_attempt_key = self._auto_like_attempt_order.popleft()
            self._auto_like_attempted_keys.discard(expired_attempt_key)

    def _is_type_blocked(self, target: str, dynamic_type: str) -> bool:
        if dynamic_type in self.type_blacklist.get(target, set()):
            logger.info(f"屏蔽了 {target} 的 {dynamic_type}，不推送")
            return True
        return False

    def _get_target_groups(self, targets: dict[str, list[str]], mid: str, name: str) -> list[str]:
        """Return merged group list for an UP, matching by both mid and name."""
        return list(dict.fromkeys(targets.get(mid, []) + targets.get(name, [])))

    def _is_live_target(self, up_mid: str, up_name: str) -> bool:
        return up_mid in self.live_targets or up_name in self.live_targets

    def _parse_qq_id(self, raw_id: str, id_name: str) -> int | None:
        try:
            return int(raw_id)
        except (TypeError, ValueError):
            logger.warning(f"{id_name} 配置无效：{raw_id}")
            return None

    def _iter_debug_user_ids(self) -> list[int]:
        user_ids: list[int] = []
        for debug_user_id in self.debug_users:
            user_id = self._parse_qq_id(debug_user_id, "调试用户")
            if user_id is not None:
                user_ids.append(user_id)
        return user_ids

    def _iter_admin_user_ids(self) -> list[int]:
        user_ids: list[int] = []
        for raw_user_id in get_driver().config.superusers:
            user_id = self._parse_qq_id(raw_user_id.strip(), "管理员用户")
            if user_id is not None:
                user_ids.append(user_id)
        return list(dict.fromkeys(user_ids))

    async def notify_admins_of_startup_cookie_failure(self, bot: Bot) -> None:
        if self._startup_cookie_checked:
            return

        self._startup_cookie_checked = True
        failure_reason = self.cookie_load_error
        if failure_reason is None:
            if not self.credential.has_sessdata():
                failure_reason = "B 站 cookies 缺少 SESSDATA，登录态不可用"
            else:
                try:
                    if not await self.credential.check_valid():
                        failure_reason = "B 站 cookies 已失效或未登录"
                except Exception as error:
                    failure_reason = f"B 站 cookies 校验失败：{error}"

        if failure_reason is None:
            return

        logger.warning(failure_reason)
        admin_user_ids = self._iter_admin_user_ids()
        if not admin_user_ids:
            logger.warning("B 站 cookies 失效，但未配置 superusers，无法通知管理员")
            return

        message = Message([MessageSegment.text(
            "Bilibili notifier 启动时检测到 cookies 不可用：\n"
            f"{failure_reason}\n"
            "请更新 bnotifier_cookies 指向的 cookies 文件。"
        )])
        for user_id in admin_user_ids:
            try:
                await bot.send_private_msg(user_id=user_id, message=message)
            except Exception as error:
                logger.warning(f"发送 B 站 cookies 失效通知到管理员 {user_id} 失败：{error}")

    def _iter_target_group_ids(self, targets: dict[str, list[str]], mid: str, name: str) -> list[int]:
        group_ids: list[int] = []
        for group_id in self._get_target_groups(targets, mid, name):
            qq_group_id = self._parse_qq_id(group_id, "QQ群")
            if qq_group_id is not None:
                group_ids.append(qq_group_id)
        return group_ids

    def _set_live_monitor_status(
        self,
        target: LiveMonitorTarget,
        state: str,
        *,
        connected_at: float | None = None,
        last_error: str | None = None,
        increment_reconnect: bool = False,
    ) -> None:
        now = time.monotonic()
        previous = self._live_monitor_status.get(target.room_id)
        reconnect_count = previous.reconnect_count if previous else 0
        if increment_reconnect:
            reconnect_count += 1

        self._live_monitor_status[target.room_id] = LiveMonitorStatus(
            target=target,
            state=state,
            updated_at=now,
            connected_at=connected_at if connected_at is not None else (previous.connected_at if previous else None),
            last_error=last_error,
            reconnect_count=reconnect_count,
        )

    def _format_duration(self, seconds: float | None) -> str:
        if seconds is None:
            return "-"
        seconds = max(0, int(seconds))
        minutes, secs = divmod(seconds, 60)
        hours, minutes = divmod(minutes, 60)
        if hours:
            return f"{hours}h{minutes}m"
        if minutes:
            return f"{minutes}m{secs}s"
        return f"{secs}s"

    def get_live_monitor_status_message(self) -> str:
        now = time.monotonic()
        lines = [
            "Bilibili 直播状态：",
            (
                "状态复核："
                f"每 {self.config.bnotifier_live_reconcile_interval}s"
            ),
            (
                "WebSocket："
                f"{'开启' if self.config.bnotifier_live_websocket_enabled else '关闭'}"
                f"，启动状态 {'已启动' if self._live_monitors_started else '未启动'}"
                f"，监听任务 {len(self._live_monitor_tasks)}"
            ),
        ]

        live_names = [
            status.target.up_name
            for status in self._live_monitor_status.values()
            if status.target.up_mid in self.current_live_users
        ]
        if live_names:
            lines.append(f"正在直播：{', '.join(live_names)}")
        else:
            lines.append("正在直播：（无）")

        if not self.config.bnotifier_live_websocket_enabled:
            return "\n".join(lines)

        if not self._live_monitor_status:
            lines.append("当前没有已解析的直播间监听。")
            return "\n".join(lines)

        for room_id, status in sorted(self._live_monitor_status.items()):
            task = self._live_monitor_tasks.get(room_id)
            state = status.state
            if task is not None and task.done():
                state = "exited"

            is_live = status.target.up_mid in self.current_live_users
            connected_for = (
                self._format_duration(now - status.connected_at)
                if status.connected_at is not None and state == "connected"
                else "-"
            )
            updated_ago = self._format_duration(now - status.updated_at)

            line = (
                f"- {'[直播中] ' if is_live else ''}{status.target.up_name} "
                f"(room {status.target.room_id}, uid {status.target.up_mid}): "
                f"{state}, connected={connected_for}, updated={updated_ago} ago, "
                f"reconnects={status.reconnect_count}"
            )
            if status.last_error:
                line += f", last_error={status.last_error}"
            lines.append(line)

        return "\n".join(lines)

    def _extract_live_room_id_from_info(self, live_info: dict[str, Any]) -> int | None:
        candidates = [live_info]
        for key in ("live_room", "room_info"):
            value = live_info.get(key)
            if isinstance(value, dict):
                candidates.append(value)

        for candidate in candidates:
            for key in ("roomid", "room_id", "roomId"):
                room_id = _as_int(candidate.get(key), default=0)
                if room_id > 0:
                    return room_id
        return None

    def _extract_room_id_from_live_user(self, live_user: Mapping[str, Any]) -> int | None:
        for key in ("roomid", "room_id", "roomId"):
            room_id = _as_int(live_user.get(key), default=0)
            if room_id > 0:
                return room_id

        room_url = clean_url(_as_str(live_user.get("link")))
        if room_url:
            return self._extract_room_id(room_url)
        return None

    def _get_monitor_target_by_up_mid(self, up_mid: str) -> LiveMonitorTarget | None:
        for status in self._live_monitor_status.values():
            if status.target.up_mid == up_mid:
                return status.target
        return None

    async def _resolve_room_id_for_live_status(
        self,
        up_mid: str,
        live_user: Mapping[str, Any],
    ) -> int | None:
        room_id = self._extract_room_id_from_live_user(live_user)
        if room_id is not None:
            return room_id

        monitor_target = self._get_monitor_target_by_up_mid(up_mid)
        if monitor_target is not None:
            return monitor_target.room_id

        parsed_mid = _as_int(up_mid, default=0)
        if parsed_mid <= 0:
            return None

        live_info = await User(parsed_mid, credential=self.credential).get_live_info()
        if isinstance(live_info, dict):
            return self._extract_live_room_id_from_info(live_info)
        return None

    async def _get_room_live_status(self, room_id: int) -> tuple[bool, int]:
        room = LiveRoom(room_id, credential=self.credential)
        room_detail: dict[str, Any] = {}
        last_error: Exception | None = None
        try:
            room_info = await room.get_room_info()
            raw_room_detail = room_info.get("room_info", {}) if isinstance(room_info, dict) else {}
            if isinstance(raw_room_detail, dict):
                room_detail = raw_room_detail
        except Exception as error:
            last_error = error
            logger.debug(f"获取直播间 {room_id} 详情失败：{error}")

        live_status = _as_int(room_detail.get("live_status"), default=-1)
        if live_status < 0:
            try:
                room_play_info = await room.get_room_play_info()
                live_status = _as_int(room_play_info.get("live_status"), default=-1)
            except Exception as error:
                last_error = error
                logger.debug(f"获取直播间 {room_id} 播放信息失败：{error}")

        if live_status < 0 and last_error is not None:
            raise last_error
        if live_status < 0:
            raise RuntimeError(f"直播间 {room_id} 状态响应缺少 live_status")

        return live_status == 1, _extract_live_start_timestamp(room_detail)

    async def _get_current_live_payload(
        self,
        up_mid: str,
        up_name: str,
        live_user: Mapping[str, Any],
    ) -> dict[str, Any] | None:
        room_id = await self._resolve_room_id_for_live_status(up_mid, live_user)
        if room_id is None:
            raise RuntimeError(f"未能解析 {up_name} ({up_mid}) 的直播间号")

        is_live, live_start_time = await self._get_room_live_status(room_id)
        if not is_live:
            return None

        return self._build_live_user_payload(
            LiveMonitorTarget(room_id=room_id, up_mid=up_mid, up_name=up_name),
            live_start_time=live_start_time,
        )

    async def _reconcile_live_target_status(
        self,
        target: LiveMonitorTarget,
        *,
        source: str,
    ) -> None:
        live_user = self._build_live_user_payload(target)
        current_live_payload = await self._get_current_live_payload(
            target.up_mid,
            target.up_name,
            live_user,
        )
        if current_live_payload is not None:
            await self._notify_live_start_once(
                target.up_mid,
                target.up_name,
                current_live_payload,
                source=source,
            )
            return

        await self._notify_live_stop_once(
            target.up_mid,
            target.up_name,
            live_user,
            source=source,
        )

    def _extract_uid_from_name_response(self, target: str, response: Any) -> int | None:
        if isinstance(response, Mapping):
            direct_uid = _as_int(response.get(target), default=0)
            if direct_uid > 0:
                return direct_uid

            candidate_name = (
                _as_str(response.get("name")).strip()
                or _as_str(response.get("uname")).strip()
            )
            candidate_uid = _as_int(
                response.get("uid", response.get("mid", response.get("id"))),
                default=0,
            )
            if candidate_uid > 0 and candidate_name == target:
                return candidate_uid

            for value in response.values():
                uid = self._extract_uid_from_name_response(target, value)
                if uid is not None:
                    return uid

        if isinstance(response, Sequence) and not isinstance(response, (str, bytes, bytearray)):
            for item in response:
                uid = self._extract_uid_from_name_response(target, item)
                if uid is not None:
                    return uid

        return None

    async def _resolve_live_target_uid(self, target: str) -> int | None:
        up_mid = _as_int(target, default=0)
        if up_mid > 0:
            return up_mid

        try:
            response = (
                await Api(**BILIBILI_USER_API["info"]["name_to_uid"], credential=self.credential)
                .update_params(names=target)
                .result
            )
        except Exception as error:
            logger.warning(f"按昵称解析 UP {target} 失败：{error}")
            return None

        up_mid = self._extract_uid_from_name_response(target, response)
        if up_mid is None:
            logger.warning(f"未能按昵称解析 UP {target} 的 UID")
            return None
        return up_mid

    async def _resolve_live_monitor_target(self, raw_target: str) -> LiveMonitorTarget | None:
        target = _as_str(raw_target).strip()
        if not target:
            return None

        up_mid = await self._resolve_live_target_uid(target)
        if up_mid is None:
            return None

        user = User(up_mid, credential=self.credential)
        try:
            live_info = await user.get_live_info()
        except Exception as error:
            logger.warning(f"解析 UP {target} 的直播间失败：{error}")
            return None

        room_id = self._extract_live_room_id_from_info(live_info)
        if room_id is None:
            logger.warning(f"未能从 UP {target} 的直播信息中解析直播间号")
            return None

        up_name = target
        try:
            user_info = await user.get_user_info()
            up_name = _as_str(user_info.get("name")).strip() or target
        except Exception as error:
            logger.debug(f"解析 UP {target} 的昵称失败：{error}")

        return LiveMonitorTarget(room_id=room_id, up_mid=str(up_mid), up_name=up_name)

    async def start_live_monitors(self) -> None:
        if self._live_monitors_started:
            return
        self._live_monitors_started = True

        if not self.config.bnotifier_live_websocket_enabled or not self.live_targets:
            return

        monitor_targets: dict[int, LiveMonitorTarget] = {}
        resolved_targets = await asyncio.gather(
            *(self._resolve_live_monitor_target(target) for target in self.live_targets),
            return_exceptions=True,
        )
        for target, resolved_target in zip(self.live_targets, resolved_targets):
            if isinstance(resolved_target, asyncio.CancelledError):
                raise resolved_target
            if isinstance(resolved_target, Exception):
                logger.warning(f"解析 UP {target} 的直播间失败：{resolved_target}")
                continue
            if resolved_target is None:
                continue
            monitor_targets.setdefault(resolved_target.room_id, resolved_target)

        if not monitor_targets:
            logger.error(
                f"B 站直播 websocket 监听未启动：所有 {len(self.live_targets)} 个目标解析失败，"
                "请检查 cookies 是否有效及网络是否正常"
            )
            return

        for room_id, target in monitor_targets.items():
            self._set_live_monitor_status(target, "starting")
            task = asyncio.create_task(self._run_live_monitor(target))
            self._live_monitor_tasks[room_id] = task
            reconcile_task = asyncio.create_task(self._run_live_status_reconciler(target))
            self._live_reconcile_tasks[room_id] = reconcile_task

        logger.info(f"已启动 {len(monitor_targets)} 个 B 站直播 websocket 监听")

    async def stop_live_monitors(self) -> None:
        tasks = list(self._live_monitor_tasks.values()) + list(self._live_reconcile_tasks.values())
        self._live_monitor_tasks.clear()
        self._live_reconcile_tasks.clear()
        self._live_monitors_started = False

        pending_stop_tasks = list(self._pending_live_stop_tasks.values())
        self._pending_live_stop_tasks.clear()
        await self._cancel_tasks_with_timeout(
            tasks,
            timeout=5,
            task_group_name="B 站直播 websocket 监听",
        )
        await self._cancel_tasks_with_timeout(
            pending_stop_tasks,
            timeout=2,
            task_group_name="B 站直播下播延迟推送",
        )

    async def _cancel_tasks_with_timeout(
        self,
        tasks: Sequence[asyncio.Task[Any]],
        *,
        timeout: float,
        task_group_name: str,
    ) -> None:
        if not tasks:
            return

        for task in tasks:
            task.cancel()

        done, pending = await asyncio.wait(tasks, timeout=timeout)
        for task in done:
            _consume_task_result(task)
        if pending:
            logger.warning(
                f"{task_group_name}停止超时：{len(pending)} 个任务未在 {timeout:g} 秒内退出"
            )

    async def _run_live_monitor(self, target: LiveMonitorTarget) -> None:
        retry_interval = self.config.bnotifier_live_update_interval

        while True:
            try:
                self._set_live_monitor_status(target, "connecting", connected_at=None)
                logger.info(f"开始监听 {target.up_name} 的直播间 {target.room_id}")
                live_client = BilibiliLiveStatusWebsocket(target, self.credential)
                await live_client.connect(
                    lambda event: self._handle_live_websocket_started(target, event),
                    lambda event: self._handle_live_websocket_stopped(target, event),
                    lambda initially_live, live_start_time: self._handle_live_websocket_connected(
                        target,
                        initially_live,
                        live_start_time,
                    ),
                )
            except asyncio.CancelledError:
                self._set_live_monitor_status(target, "stopped")
                raise
            except Exception as error:
                self._set_live_monitor_status(
                    target,
                    "retrying",
                    connected_at=None,
                    last_error=repr(error),
                    increment_reconnect=True,
                )
                logger.warning(f"{target.up_name} 的直播 websocket 监听异常：{error}")
            else:
                self._set_live_monitor_status(
                    target,
                    "retrying",
                    connected_at=None,
                    last_error="connection closed",
                    increment_reconnect=True,
                )

            await asyncio.sleep(retry_interval)

    async def _run_live_status_reconciler(self, target: LiveMonitorTarget) -> None:
        reconcile_interval = self.config.bnotifier_live_reconcile_interval

        while True:
            try:
                await asyncio.sleep(reconcile_interval)
                await self._reconcile_live_target_status(target, source="websocket-reconcile")
            except asyncio.CancelledError:
                raise
            except Exception as error:
                logger.warning(f"{target.up_name} 直播状态定期复核失败：{error}")

    def _build_live_user_payload(
        self,
        target: LiveMonitorTarget,
        *,
        live_start_time: int = 0,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "uid": target.up_mid,
            "uname": target.up_name,
            "link": f"https://live.bilibili.com/{target.room_id}",
        }
        if live_start_time > 0:
            payload["live_start_time"] = live_start_time
        return payload

    def _log_live_status_change(
        self,
        *,
        up_mid: str,
        up_name: str,
        status: str,
        source: str,
        live_user: Mapping[str, Any],
        reason: str = "",
    ) -> None:
        live_start_time = _get_live_start_timestamp(live_user)
        fields = [
            f"UP={up_name} ({up_mid})",
            f"status={status}",
            f"source={source}",
            f"live_start_time={_format_live_timestamp(live_start_time)}",
        ]
        room_url = clean_url(_as_str(live_user.get("link")))
        if room_url:
            fields.append(f"room={room_url}")
        if reason:
            fields.append(f"reason={reason}")
        logger.info("直播状态变化：" + "，".join(fields))

    def _is_recent_initial_live_snapshot(self, live_start_time: int) -> bool:
        if live_start_time <= 0:
            return False
        max_snapshot_age = max(
            60,
            self.config.bnotifier_live_reconcile_interval,
            self.config.bnotifier_live_start_silence_seconds,
        )
        return time.time() - live_start_time <= max_snapshot_age

    def _get_live_session(self, up_mid: str) -> LiveSessionState:
        return self.live_sessions.setdefault(up_mid, LiveSessionState())

    def _is_stale_live_start_time(self, up_mid: str, live_start_time: int) -> bool:
        if live_start_time <= 0:
            return False

        session = self._get_live_session(up_mid)
        if session.last_confirmed_end_time <= 0:
            return False

        tolerance = max(
            self.config.bnotifier_live_stop_grace_seconds,
            self.config.bnotifier_live_reconcile_interval,
            60,
        )
        return live_start_time <= session.last_confirmed_end_time + tolerance

    def _mark_live_session(
        self,
        up_mid: str,
        up_name: str,
        live_start_time: int,
        *,
        start_notified: bool,
        suppressed: bool,
    ) -> LiveSessionState:
        session = self._get_live_session(up_mid)
        session.start_time = live_start_time if live_start_time > 0 else int(time.time())
        session.start_notified = start_notified
        session.suppressed = suppressed

        self.current_live_users.add(up_mid)
        self.notified_live_users.add(up_mid)
        self.notified_live_users.add(up_name)
        self.current_live_start_times[up_mid] = session.start_time
        if suppressed:
            self.suppressed_live_users.add(up_mid)
            self.suppressed_live_users.add(up_name)
        else:
            self.suppressed_live_users.discard(up_mid)
            self.suppressed_live_users.discard(up_name)
        self._save_state()
        return session

    def _finish_live_session(
        self,
        up_mid: str,
        up_name: str,
        stopped_at: int,
    ) -> tuple[LiveSessionState, bool, bool]:
        session = self._get_live_session(up_mid)
        start_notified = session.start_notified
        suppressed = session.suppressed

        session.start_time = 0
        session.start_notified = False
        session.suppressed = False
        session.last_confirmed_end_time = max(session.last_confirmed_end_time, stopped_at)

        self.current_live_start_times.pop(up_mid, None)
        self.current_live_users.discard(up_mid)
        self.notified_live_users.discard(up_mid)
        self.notified_live_users.discard(up_name)
        self.suppressed_live_users.discard(up_mid)
        self.suppressed_live_users.discard(up_name)
        self._save_state()
        return session, start_notified, suppressed

    def _remember_initial_live_snapshot(
        self,
        target: LiveMonitorTarget,
        live_user: dict[str, Any],
        live_start_time: int,
        *,
        reason: str,
    ) -> None:
        session = self._get_live_session(target.up_mid)
        session_start_time = (
            session.start_time
            if (
                session.start_time > 0
                and self._is_stale_live_start_time(target.up_mid, live_start_time)
            )
            else live_start_time
        )
        self._mark_live_session(
            target.up_mid,
            target.up_name,
            session_start_time,
            start_notified=False,
            suppressed=True,
        )
        self._log_live_status_change(
            up_mid=target.up_mid,
            up_name=target.up_name,
            status="live",
            source="websocket-initial",
            live_user=live_user,
            reason=reason,
        )

    async def _handle_live_websocket_connected(
        self,
        target: LiveMonitorTarget,
        initially_live: bool = False,
        live_start_time: int = 0,
    ) -> None:
        self._set_live_monitor_status(
            target,
            "connected",
            connected_at=time.monotonic(),
        )
        observed_live_start_time = live_start_time
        if initially_live and observed_live_start_time <= 0:
            observed_live_start_time = int(time.time())
        live_user = self._build_live_user_payload(
            target,
            live_start_time=observed_live_start_time,
        )

        async with self._live_notification_lock:
            is_first_observation = target.room_id not in self._live_monitor_seen_initial_status
            self._live_monitor_seen_initial_status.add(target.room_id)
            session = self._get_live_session(target.up_mid)
            has_active_session = session.start_time > 0
            was_known_live = (
                target.up_mid in self.current_live_users
                or target.up_mid in self.notified_live_users
                or target.up_name in self.notified_live_users
                or has_active_session
            )

            if initially_live and was_known_live:
                if live_start_time > 0 and not self._is_stale_live_start_time(
                    target.up_mid,
                    live_start_time,
                ):
                    session_start_time = observed_live_start_time
                else:
                    session_start_time = session.start_time or observed_live_start_time
                self._mark_live_session(
                    target.up_mid,
                    target.up_name,
                    session_start_time,
                    start_notified=session.start_notified,
                    suppressed=session.suppressed or not session.start_notified,
                )
                if live_start_time > 0 and self._is_stale_live_start_time(
                    target.up_mid,
                    live_start_time,
                ):
                    self._log_live_status_change(
                        up_mid=target.up_mid,
                        up_name=target.up_name,
                        status="live",
                        source="websocket-initial",
                        live_user=live_user,
                        reason="忽略早于上次确认下播时间的直播开始时间",
                    )
                return

            if initially_live and (
                is_first_observation
                or live_start_time <= 0
                or self._is_stale_live_start_time(target.up_mid, live_start_time)
                or not self._is_recent_initial_live_snapshot(observed_live_start_time)
            ):
                self._remember_initial_live_snapshot(
                    target,
                    live_user,
                    observed_live_start_time,
                    reason=(
                        "bot 启动或 websocket 重连时已在直播中，"
                        "按快照记录，不推送通知"
                    ),
                )
                logger.info(f"{target.up_name} 连接时已在直播中，已跳过快照开播推送")
                return

        if initially_live:
            await self._notify_live_start_once(
                target.up_mid,
                target.up_name,
                live_user,
                source="websocket-initial",
            )
        elif was_known_live:
            await self._notify_live_stop_once(
                target.up_mid,
                target.up_name,
                live_user,
                source="websocket-initial",
            )

    async def _handle_live_websocket_started(
        self,
        target: LiveMonitorTarget,
        event: dict[str, Any],
    ) -> None:
        logger.debug(f"{target.up_name} 直播 websocket 收到开播事件：{event}")
        try:
            await self._reconcile_live_target_status(target, source="websocket-event")
        except Exception as error:
            logger.warning(f"{target.up_name} 开播事件复核失败，按 websocket 事件处理：{error}")
            live_user = self._build_live_user_payload(
                target,
                live_start_time=_get_live_start_timestamp(event),
            )
            await self._notify_live_start_once(
                target.up_mid,
                target.up_name,
                live_user,
                source="websocket-event",
            )

    async def _handle_live_websocket_stopped(
        self,
        target: LiveMonitorTarget,
        event: dict[str, Any],
    ) -> None:
        logger.debug(f"{target.up_name} 直播 websocket 收到下播事件：{event}")
        try:
            await self._reconcile_live_target_status(target, source="websocket-event")
        except Exception as error:
            logger.warning(f"{target.up_name} 下播事件复核失败，保留直播中状态：{error}")

    async def _get_dynamic_page_items(self, page_number: int) -> list[dict[str, Any]]:
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

    async def _collect_dynamic_items(self) -> list[dict[str, Any]]:
        pages = self.config.bnotifier_dynamic_pages
        all_items: list[dict[str, Any]] = []
        seen_dynamic_ids: set[str] = set()

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

    async def _get_dynamic_item_by_id(self, dynamic_id: int) -> dict[str, Any] | None:
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
        dynamic_item: dict[str, Any],
        parsed_dynamic: ParsedDynamic,
        previous_auto_like_timestamp: int,
    ) -> int | None:
        if not self._should_auto_like(parsed_dynamic):
            return None

        dynamic_timestamp = self._get_dynamic_timestamp(parsed_dynamic)
        if dynamic_timestamp <= 0 or dynamic_timestamp <= previous_auto_like_timestamp:
            return None

        dynamic_id = _as_int(dynamic_item.get("id_str"), default=0)
        if dynamic_id <= 0:
            return None

        attempt_key = self._get_auto_like_attempt_key(parsed_dynamic, dynamic_timestamp)
        if attempt_key in self._auto_like_attempted_keys:
            return dynamic_timestamp

        modules = dynamic_item.get("modules", {})
        module_stat = modules.get("module_stat", {}) if isinstance(modules, dict) else {}
        like_info = module_stat.get("like", {}) if isinstance(module_stat, dict) else {}
        has_liked = bool(like_info.get("status")) if isinstance(like_info, dict) else False

        if has_liked:
            self._remember_auto_like_attempt(attempt_key)
            return dynamic_timestamp

        # Some dynamics do not reflect like status changes; avoid retrying them every poll.
        self._remember_auto_like_attempt(attempt_key)
        try:
            await Dynamic(dynamic_id, credential=self.credential).set_like(True)
        except Exception as error:
            logger.warning(f"给 {parsed_dynamic.name} 的动态点赞失败：{error}")
            return dynamic_timestamp
        # bilibili-api returns an empty payload here, so failures are detected by exception.
        logger.info(f"给 {parsed_dynamic.name} 的动态点赞成功")
        return dynamic_timestamp

    def _build_dynamic_message_segments(self, dynamic: ParsedDynamic) -> list[MessageSegment]:
        return [MessageSegment.text(dynamic.action + '：\n')] + list(dynamic.message)

    def _build_ob11_dynamic_messages(self, dynamic: ParsedDynamic) -> list[Message]:
        messages = [Message(self._build_dynamic_message_segments(dynamic))]
        if dynamic.origin and self.config.bnotifier_forward_message_mode != "none":
            messages.append(Message([MessageSegment.text("被转发的动态:")]))
            messages.append(Message(self._build_dynamic_message_segments(dynamic.origin)))
        dynamic_url = clean_url(dynamic.url)
        if dynamic_url:
            messages.append(Message([MessageSegment.text(f"动态链接：{dynamic_url}")]))
        return messages

    def _build_saa_dynamic_notification(self, dynamic: ParsedDynamic):
        from nonebot_plugin_saa import AggregatedMessageFactory, MessageFactory, Text

        messages = [MessageFactory([_seg_to_saa(s) for s in self._build_dynamic_message_segments(dynamic)])]
        if dynamic.origin and self.config.bnotifier_forward_message_mode != "none":
            messages.append(MessageFactory([Text("被转发的动态:")]))
            messages.append(MessageFactory([_seg_to_saa(s) for s in self._build_dynamic_message_segments(dynamic.origin)]))
        dynamic_url = clean_url(dynamic.url)
        if dynamic_url:
            messages.append(MessageFactory([Text(f"动态链接：{dynamic_url}")]))
        return AggregatedMessageFactory(messages)

    def _is_lottery_forward(self, dynamic: ParsedDynamic) -> bool:
        return (
            dynamic.origin is not None
            and self.config.bnotifier_skip_lottery_forward
            and "中奖" in dynamic.text
        )

    def _build_dynamic_senders(self, dynamic: ParsedDynamic) -> tuple[IntMessageSender, IntMessageSender]:
        if self.config.bnotifier_use_saa:
            from nonebot_plugin_saa import TargetQQGroup, TargetQQPrivate

            message = self._build_saa_dynamic_notification(dynamic)

            async def send_private(user_id: int) -> None:
                await message.send_to(TargetQQPrivate(user_id=user_id))

            async def send_group(group_id: int) -> None:
                await message.send_to(TargetQQGroup(group_id=group_id))
        else:
            bot = get_bot()
            messages = self._build_ob11_dynamic_messages(dynamic)
            nodes = [{"type": "node", "data": {"uin": bot.self_id, "content": msg}} for msg in messages]

            async def send_private(user_id: int) -> None:
                await bot.send_private_forward_msg(user_id=user_id, messages=nodes, source=dynamic.action)

            async def send_group(group_id: int) -> None:
                await bot.send_group_forward_msg(group_id=group_id, messages=nodes, source=dynamic.action)

        return send_private, send_group

    async def _send_update_notification(self, dynamic: ParsedDynamic) -> None:
        send_private, send_group = self._build_dynamic_senders(dynamic)
        failures: list[str] = []

        for user_id in self._iter_debug_user_ids():
            logger.info(f"将 {dynamic.name} 的更新消息推送到用户 {user_id}")
            try:
                await send_private(user_id)
            except Exception as error:
                logger.warning(f"给用户 {user_id} 推送 {dynamic.name} 更新失败：{error}")
                failures.append(f"用户 {user_id}: {error}")

        for group_id in self._get_target_groups(self.update_targets, dynamic.mid, dynamic.name):
            if self._is_type_blocked(group_id, dynamic.dynamic_type):
                continue

            qq_group_id = self._parse_qq_id(group_id, "QQ群")
            if qq_group_id is None:
                continue

            logger.info(f"将 {dynamic.name} 的更新消息推送到群 {qq_group_id}")
            try:
                await send_group(qq_group_id)
            except Exception as error:
                logger.warning(f"给群 {qq_group_id} 推送 {dynamic.name} 更新失败：{error}")
                failures.append(f"群 {qq_group_id}: {error}")

        if failures:
            await self._notify_debug_users_of_failure(dynamic, failures)

    async def _notify_debug_users_of_failure(
        self, dynamic: ParsedDynamic, failures: list[str]
    ) -> None:
        dynamic_url = clean_url(dynamic.url)
        failure_summary = "\n".join(failures)
        text = f"推送失败通知：{dynamic.name}\n链接：{dynamic_url}\n失败详情：\n{failure_summary}"
        send_error, _ = self._build_segment_senders([MessageSegment.text(text)])

        for user_id in self._iter_debug_user_ids():
            try:
                await send_error(user_id)
            except Exception as error:
                logger.warning(f"发送失败通知到用户 {user_id} 也失败了：{error}")

    async def fetch_bilibili_updates(self) -> None:
        if not self.update_targets and not self.like_targets:
            return

        try:
            dynamic_items = await self._collect_dynamic_items()
        except Exception as error:
            logger.warning(f"获取动态列表失败：{error}")
            return

        self.last_dynamic_check_timestamp = int(time.time())

        if not dynamic_items:
            self._save_state()
            return

        previous_last_seen_timestamp = self.last_seen_dynamic_timestamp
        max_seen_timestamp = previous_last_seen_timestamp
        previous_last_auto_like_timestamp = self.last_auto_like_dynamic_timestamp
        max_auto_like_timestamp = previous_last_auto_like_timestamp
        is_first_run = (
            previous_last_seen_timestamp <= 0
            and self.config.bnotifier_ignore_old_dynamic_on_start
        )

        for dynamic_item in dynamic_items:
            try:
                parsed_dynamic = parse_dynamic(dynamic_item)
                if parsed_dynamic is None:
                    continue

                if not is_first_run:
                    auto_like_timestamp = await self._try_auto_like(
                        dynamic_item,
                        parsed_dynamic,
                        previous_last_auto_like_timestamp,
                    )
                    if auto_like_timestamp is not None:
                        max_auto_like_timestamp = max(
                            max_auto_like_timestamp,
                            auto_like_timestamp,
                        )

                dynamic_timestamp = self._get_dynamic_timestamp(parsed_dynamic)
                if dynamic_timestamp <= 0:
                    continue

                max_seen_timestamp = max(max_seen_timestamp, dynamic_timestamp)

                if dynamic_timestamp <= previous_last_seen_timestamp:
                    continue

                if is_first_run:
                    continue

                if parsed_dynamic.mid not in self.update_targets and parsed_dynamic.name not in self.update_targets:
                    continue
                if self._is_type_blocked(parsed_dynamic.mid, parsed_dynamic.dynamic_type) or self._is_type_blocked(parsed_dynamic.name, parsed_dynamic.dynamic_type):
                    continue
                if self._is_lottery_forward(parsed_dynamic):
                    logger.info(f"跳过 {parsed_dynamic.name} 的中奖动态：{parsed_dynamic.text}")
                    continue

                await self._send_update_notification(parsed_dynamic)
            except Exception as error:
                logger.warning(f"获取推送动态失败：{error}")

        if max_seen_timestamp > self.last_seen_dynamic_timestamp:
            self.last_seen_dynamic_timestamp = max_seen_timestamp
        if is_first_run:
            max_auto_like_timestamp = max(max_auto_like_timestamp, max_seen_timestamp)
        if max_auto_like_timestamp > self.last_auto_like_dynamic_timestamp:
            self.last_auto_like_dynamic_timestamp = max_auto_like_timestamp

        self._save_state()

    def _extract_room_id(self, room_url: str) -> int | None:
        matched = ROOM_URL_PATTERN.match(room_url)
        if not matched:
            return None
        return _as_int(matched.group(1), default=0) or None

    async def _build_live_notification_message(self, up_name: str, live_user: dict[str, Any]) -> list[MessageSegment]:
        room_url = clean_url(_as_str(live_user.get("link")))
        segments: list[MessageSegment] = [MessageSegment.text(f"{up_name} 开始直播了：{room_url}")]

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
                segments.append(MessageSegment.text(f"\n标题：{title}"))

        if self.config.bnotifier_live_include_cover:
            cover = _as_str(room_detail.get("cover")).strip()
            if cover:
                segments.append(MessageSegment.image(cover))

        return segments

    def _build_live_stop_notification_message(
        self,
        up_name: str,
        live_user: dict[str, Any],
        duration_seconds: float | None,
    ) -> list[MessageSegment]:
        if duration_seconds is None:
            duration_text = "未知"
        else:
            duration_text = self._format_duration(duration_seconds)
        return [MessageSegment.text(f"{up_name} 直播结束了，本场时长：{duration_text}")]

    def _build_segment_senders(
        self,
        message_segments: list[MessageSegment],
    ) -> tuple[IntMessageSender, IntMessageSender]:
        if self.config.bnotifier_use_saa:
            from nonebot_plugin_saa import MessageFactory, TargetQQGroup, TargetQQPrivate

            message = MessageFactory([_seg_to_saa(s) for s in message_segments])

            async def send_private(user_id: int) -> None:
                await message.send_to(TargetQQPrivate(user_id=user_id))

            async def send_group(group_id: int) -> None:
                await message.send_to(TargetQQGroup(group_id=group_id))
        else:
            bot = get_bot()
            message = Message(message_segments)

            async def send_private(user_id: int) -> None:
                await bot.send_private_msg(user_id=user_id, message=message)

            async def send_group(group_id: int) -> None:
                await bot.send_group_msg(group_id=group_id, message=message)

        return send_private, send_group

    async def _send_live_notification(
        self,
        up_mid: str,
        up_name: str,
        message_segments: list[MessageSegment],
        event_name: str = "开播",
    ) -> None:
        send_private, send_group = self._build_segment_senders(message_segments)
        logger.info(
            f"直播{event_name}推送消息内容：UP={up_name} ({up_mid})，"
            f"message={_format_message_segments_for_log(message_segments)}"
        )

        for user_id in self._iter_debug_user_ids():
            logger.info(f"将 {up_name} 的{event_name}消息推送到用户 {user_id}")
            try:
                await send_private(user_id)
            except Exception as error:
                logger.warning(f"给用户 {user_id} 推送 {up_name} {event_name}消息失败：{error}")

        for qq_group_id in self._iter_target_group_ids(self.live_targets, up_mid, up_name):
            logger.info(f"将 {up_name} 的{event_name}消息推送到群 {qq_group_id}")
            try:
                await send_group(qq_group_id)
            except Exception as error:
                logger.warning(f"给群 {qq_group_id} 推送 {up_name} {event_name}消息失败：{error}")

    async def _notify_live_start_once(
        self,
        up_mid: str,
        up_name: str,
        live_user: dict[str, Any],
        *,
        source: str = "unknown",
    ) -> None:
        now = time.monotonic()
        observed_live_start_time = _get_live_start_timestamp(live_user)
        async with self._live_notification_lock:
            session = self._get_live_session(up_mid)
            live_start_time = (
                observed_live_start_time
                if observed_live_start_time > 0
                and not self._is_stale_live_start_time(up_mid, observed_live_start_time)
                else int(time.time())
            )
            if observed_live_start_time > 0 and self._is_stale_live_start_time(
                up_mid,
                observed_live_start_time,
            ):
                self._mark_live_session(
                    up_mid,
                    up_name,
                    session.start_time or live_start_time,
                    start_notified=session.start_notified,
                    suppressed=session.suppressed or not session.start_notified,
                )
                self._log_live_status_change(
                    up_mid=up_mid,
                    up_name=up_name,
                    status="live",
                    source=source,
                    live_user=live_user,
                    reason="忽略早于上次确认下播时间的直播开始时间",
                )
                return

            pending_stop_task = self._pending_live_stop_tasks.pop(up_mid, None)
            if pending_stop_task is not None:
                pending_stop_task.cancel()
                self._mark_live_session(
                    up_mid,
                    up_name,
                    live_start_time,
                    start_notified=session.start_notified,
                    suppressed=session.suppressed or not session.start_notified,
                )
                self._log_live_status_change(
                    up_mid=up_mid,
                    up_name=up_name,
                    status="live",
                    source=source,
                    live_user=live_user,
                    reason="下播静默期内恢复直播，取消下播推送",
                )
                logger.info(f"{up_name} 在下播静默期内恢复直播，已取消下播推送")
                return

            if session.start_time > 0:
                self._mark_live_session(
                    up_mid,
                    up_name,
                    live_start_time if observed_live_start_time > 0 else session.start_time,
                    start_notified=session.start_notified,
                    suppressed=session.suppressed or not session.start_notified,
                )
                return

            if up_mid in self.notified_live_users or up_name in self.notified_live_users:
                return

            silence_seconds = self.config.bnotifier_live_start_silence_seconds
            last_start_time = self.last_live_start_times.get(up_mid)
            if last_start_time is None:
                last_start_time = self.last_live_start_times.get(up_name)
            self.last_live_start_times[up_mid] = now
            self.last_live_start_times[up_name] = now

            if (
                silence_seconds > 0
                and last_start_time is not None
                and now - last_start_time < silence_seconds
            ):
                self._mark_live_session(
                    up_mid,
                    up_name,
                    live_start_time,
                    start_notified=False,
                    suppressed=True,
                )
                self._log_live_status_change(
                    up_mid=up_mid,
                    up_name=up_name,
                    status="live",
                    source=source,
                    live_user=live_user,
                    reason=f"{silence_seconds} 秒静默期内跳过开播推送",
                )
                logger.info(
                    f"{up_name} 在 {silence_seconds} 秒静默期内再次开播，已跳过开播推送"
                )
                return

            self._mark_live_session(
                up_mid,
                up_name,
                live_start_time,
                start_notified=True,
                suppressed=False,
            )
            self._log_live_status_change(
                up_mid=up_mid,
                up_name=up_name,
                status="live",
                source=source,
                live_user=live_user,
            )

        live_message = await self._build_live_notification_message(up_name, live_user)
        await self._send_live_notification(up_mid, up_name, live_message)

    async def _notify_live_stop_once(
        self,
        up_mid: str,
        up_name: str,
        live_user: dict[str, Any],
        *,
        source: str = "unknown",
    ) -> None:
        stopped_at = int(time.time())
        async with self._live_notification_lock:
            session = self._get_live_session(up_mid)
            was_known_live = (
                up_mid in self.current_live_users
                or up_mid in self.notified_live_users
                or up_name in self.notified_live_users
                or session.start_time > 0
            )
            if not was_known_live:
                return

            if up_mid in self._pending_live_stop_tasks:
                return

            live_start_time = self.current_live_start_times.get(up_mid)
            if live_start_time is None and session.start_time > 0:
                live_start_time = session.start_time
            if live_start_time is None:
                candidate_start_time = _get_live_start_timestamp(live_user)
                if self._is_stale_live_start_time(up_mid, candidate_start_time):
                    live_start_time = None
                else:
                    live_start_time = candidate_start_time

        live_user_for_log = dict(live_user)
        if live_start_time and live_start_time > 0:
            live_user_for_log.setdefault("live_start_time", live_start_time)

        duration_seconds = (
            max(0, stopped_at - live_start_time)
            if live_start_time and live_start_time > 0
            else None
        )
        self._log_live_status_change(
            up_mid=up_mid,
            up_name=up_name,
            status="preparing",
            source=source,
            live_user=live_user_for_log,
            reason="等待确认下播状态",
        )

        grace_seconds = self.config.bnotifier_live_stop_grace_seconds
        if grace_seconds > 0:
            async with self._live_notification_lock:
                if up_mid in self._pending_live_stop_tasks:
                    return
                task = asyncio.create_task(
                    self._confirm_live_stop_after_grace(
                        up_mid,
                        up_name,
                        live_user,
                        grace_seconds,
                        stopped_at,
                        live_start_time,
                        duration_seconds,
                        source,
                    )
                )
                self._pending_live_stop_tasks[up_mid] = task
            logger.info(f"{up_name} 疑似下播，将在 {grace_seconds} 秒后确认并推送")
            return

        await self._confirm_live_stop_once(
            up_mid,
            up_name,
            live_user,
            stopped_at,
            live_start_time,
            duration_seconds,
            source,
        )

    async def _confirm_live_stop_after_grace(
        self,
        up_mid: str,
        up_name: str,
        live_user: dict[str, Any],
        grace_seconds: int,
        stopped_at: int,
        live_start_time: int | None,
        duration_seconds: float | None,
        source: str,
    ) -> None:
        try:
            await asyncio.sleep(grace_seconds)
            await self._confirm_live_stop_once(
                up_mid,
                up_name,
                live_user,
                stopped_at,
                live_start_time,
                duration_seconds,
                source,
            )
        except asyncio.CancelledError:
            raise
        except Exception as error:
            logger.warning(f"确认 {up_name} 下播状态失败：{error}")

    async def _confirm_live_stop_once(
        self,
        up_mid: str,
        up_name: str,
        live_user: dict[str, Any],
        stopped_at: int,
        live_start_time: int | None,
        duration_seconds: float | None,
        source: str,
    ) -> None:
        try:
            current_live_payload = await self._get_current_live_payload(up_mid, up_name, live_user)
        except Exception as error:
            logger.warning(f"确认 {up_name} 下播状态失败，保留直播中状态：{error}")
            async with self._live_notification_lock:
                self._pending_live_stop_tasks.pop(up_mid, None)
            return

        async with self._live_notification_lock:
            self._pending_live_stop_tasks.pop(up_mid, None)
            if current_live_payload is not None:
                live_start_time = _get_live_start_timestamp(
                    current_live_payload,
                    default_to_now=True,
                )
                session = self._get_live_session(up_mid)
                if self._is_stale_live_start_time(up_mid, live_start_time):
                    has_current_session = (
                        session.start_time > 0
                        and not self._is_stale_live_start_time(up_mid, session.start_time)
                    )
                    self._mark_live_session(
                        up_mid,
                        up_name,
                        session.start_time or live_start_time,
                        start_notified=session.start_notified if has_current_session else False,
                        suppressed=(
                            session.suppressed or not session.start_notified
                            if has_current_session
                            else True
                        ),
                    )
                    self._log_live_status_change(
                        up_mid=up_mid,
                        up_name=up_name,
                        status="live",
                        source=source,
                        live_user=current_live_payload,
                        reason="复核仍显示直播，但开始时间早于上次确认下播，按快照忽略",
                    )
                    return

                self._mark_live_session(
                    up_mid,
                    up_name,
                    live_start_time,
                    start_notified=session.start_notified,
                    suppressed=session.suppressed or not session.start_notified,
                )
                self._log_live_status_change(
                    up_mid=up_mid,
                    up_name=up_name,
                    status="live",
                    source=source,
                    live_user=current_live_payload,
                    reason="下播事件复核后仍在直播，忽略本次下播",
                )
                return

            session = self._get_live_session(up_mid)
            was_known_live = (
                up_mid in self.current_live_users
                or up_mid in self.notified_live_users
                or up_name in self.notified_live_users
                or session.start_time > 0
            )
            was_suppressed = (
                up_mid in self.suppressed_live_users
                or up_name in self.suppressed_live_users
                or session.suppressed
            )
            if live_start_time is None:
                live_start_time = self.current_live_start_times.pop(up_mid, None)
            else:
                self.current_live_start_times.pop(up_mid, None)
            if live_start_time is None and session.start_time > 0:
                live_start_time = session.start_time
            if live_start_time is None:
                candidate_start_time = _get_live_start_timestamp(live_user)
                if not self._is_stale_live_start_time(up_mid, candidate_start_time):
                    live_start_time = candidate_start_time
            _, start_notified, session_was_suppressed = self._finish_live_session(
                up_mid,
                up_name,
                stopped_at,
            )
            was_suppressed = was_suppressed or session_was_suppressed

        if not was_known_live:
            return

        live_user_for_log = dict(live_user)
        if live_start_time and live_start_time > 0:
            live_user_for_log.setdefault("live_start_time", live_start_time)
        duration_seconds = (
            max(0, stopped_at - live_start_time)
            if live_start_time and live_start_time > 0
            else duration_seconds
        )

        if not start_notified:
            reason = (
                "已确认下播，开播未推送，跳过下播推送"
                if not was_suppressed
                else "已确认下播，开播来自静默或快照记录，跳过下播推送"
            )
            self._log_live_status_change(
                up_mid=up_mid,
                up_name=up_name,
                status="preparing",
                source=source,
                live_user=live_user_for_log,
                reason=reason,
            )
            return

        if not self.config.bnotifier_live_push_stop:
            self._log_live_status_change(
                up_mid=up_mid,
                up_name=up_name,
                status="preparing",
                source=source,
                live_user=live_user_for_log,
                reason="已确认下播，下播推送未开启",
            )
            return

        self._log_live_status_change(
            up_mid=up_mid,
            up_name=up_name,
            status="preparing",
            source=source,
            live_user=live_user_for_log,
            reason="已确认下播",
        )
        await self._send_live_stop_notification(up_mid, up_name, live_user, duration_seconds)

    async def _send_live_stop_notification(
        self,
        up_mid: str,
        up_name: str,
        live_user: dict[str, Any],
        duration_seconds: float | None,
    ) -> None:
        stop_message = self._build_live_stop_notification_message(
            up_name,
            live_user,
            duration_seconds,
        )
        await self._send_live_notification(up_mid, up_name, stop_message, event_name="下播")

    async def push_dynamic_to_user(self, dynamic_id: int, user_id: str) -> tuple[bool, str]:
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

        if self._is_lottery_forward(parsed_dynamic):
            return False, "该动态命中中奖转发过滤，已跳过"

        try:
            send_private, _ = self._build_dynamic_senders(parsed_dynamic)
            await send_private(qq_user_id)
        except Exception as error:
            logger.warning(f"按ID推送动态失败（{dynamic_id} -> {qq_user_id}）：{error}")
            return False, f"推送失败：{error}"

        logger.info(f"按ID推送动态成功（{dynamic_id} -> {qq_user_id}）")
        return True, "ok"
