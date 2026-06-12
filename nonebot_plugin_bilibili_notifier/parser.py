from typing import Any

from nonebot.adapters.onebot.v11 import MessageSegment

from .models import ParsedDynamic
from .utils import _as_int, _as_str


def Text(text: str) -> MessageSegment:
    return MessageSegment.text(text)


def Image(url: str) -> MessageSegment:
    return MessageSegment.image(url)

TEXT_NODE_TYPES = {
    "RICH_TEXT_NODE_TYPE_TEXT",
    "RICH_TEXT_NODE_TYPE_AT",
    "RICH_TEXT_NODE_TYPE_TOPIC",
    "RICH_TEXT_NODE_TYPE_LOTTERY",
}

SKIPPED_DYNAMIC_TYPES = {
    "DYNAMIC_TYPE_LIVE_RCMD",
    "DYNAMIC_TYPE_UGC_SEASON",
}


def _normalize_url(raw_url: str) -> str:
    if not raw_url:
        return ""
    if raw_url.startswith(("http://", "https://")):
        return raw_url
    if raw_url.startswith("//"):
        return f"https:{raw_url}"
    return f"https://{raw_url.lstrip('/')}"


def parse_rich_text(rich_text_nodes: Any) -> list[Any]:
    if not isinstance(rich_text_nodes, list):
        return []

    segments: list[Any] = []
    for node in rich_text_nodes:
        if not isinstance(node, dict):
            continue

        node_type = _as_str(node.get("type"))
        if node_type == "RICH_TEXT_NODE_TYPE_EMOJI":
            emoji_data = node.get("emoji", {})
            if not isinstance(emoji_data, dict):
                continue
            emoji_url = _as_str(emoji_data.get("gif_url") or emoji_data.get("icon_url"))
            if emoji_url:
                segments.append(Image(emoji_url))
            continue

        if node_type in TEXT_NODE_TYPES:
            text = _as_str(node.get("text"))
            if text:
                segments.append(Text(text))
            continue

        if node_type == "RICH_TEXT_NODE_TYPE_WEB":
            text = _as_str(node.get("orig_text") or node.get("text"))
            if text:
                segments.append(Text(text))

    return segments


def parse_dynamic(dynamic_data: dict[str, Any]) -> ParsedDynamic | None:
    if not isinstance(dynamic_data, dict):
        return None

    modules = dynamic_data.get("modules", {})
    if not isinstance(modules, dict):
        return None

    author_info = modules.get("module_author", {})
    if not isinstance(author_info, dict):
        return None

    id_str = _as_str(dynamic_data.get("id_str")).strip()

    dynamic_type = _as_str(dynamic_data.get("type"))
    if not dynamic_type or dynamic_type in SKIPPED_DYNAMIC_TYPES:
        return None

    mid = _as_str(author_info.get("mid")).strip()
    name = _as_str(author_info.get("name")).strip() or "未知UP"
    pub_action = _as_str(author_info.get("pub_action")).strip()
    timestamp = _as_int(author_info.get("pub_ts"), default=0)

    text = ""
    action = ""
    message: list[Any] = []
    raw_url = ""
    origin: ParsedDynamic | None = None

    module_dynamic = modules.get("module_dynamic", {})
    if not isinstance(module_dynamic, dict):
        module_dynamic = {}

    if dynamic_type == "DYNAMIC_TYPE_AV":
        major = module_dynamic.get("major", {})
        archive = major.get("archive", {}) if isinstance(major, dict) else {}
        if not isinstance(archive, dict):
            archive = {}

        title = _as_str(archive.get("title")).strip()
        action = pub_action or "发布视频"
        text = f"{action}：{title}".strip()
        message = []
        if title:
            message.append(Text(title))

        cover = _as_str(archive.get("cover")).strip()
        if cover:
            message.append(Image(cover))

        raw_url = _as_str(archive.get("jump_url")).strip()

    elif dynamic_type in {"DYNAMIC_TYPE_DRAW", "DYNAMIC_TYPE_WORD"}:
        basic = dynamic_data.get("basic", {})
        if not isinstance(basic, dict):
            basic = {}

        major = module_dynamic.get("major", {})
        opus = major.get("opus", {}) if isinstance(major, dict) else {}
        if not isinstance(opus, dict):
            opus = {}

        summary = opus.get("summary", {})
        if not isinstance(summary, dict):
            summary = {}

        title = _as_str(opus.get("title")).strip()
        summary_text = _as_str(summary.get("text"))

        action = pub_action or "发布动态"
        text = f"{action}：\n{title}\n{summary_text}".strip()
        message = []
        if title:
            message.append(Text(title + "\n"))

        rich_text_segments = parse_rich_text(summary.get("rich_text_nodes"))
        if rich_text_segments:
            message.extend(rich_text_segments)
        elif summary_text:
            message.append(Text(summary_text))

        if dynamic_type == "DYNAMIC_TYPE_DRAW":
            pics = opus.get("pics", [])
            if isinstance(pics, list):
                for picture in pics:
                    if not isinstance(picture, dict):
                        continue
                    pic_url = _as_str(picture.get("url")).strip()
                    if pic_url:
                        message.append(Image(pic_url))

        raw_url = _as_str(basic.get("jump_url")).strip()

    elif dynamic_type == "DYNAMIC_TYPE_ARTICLE":
        basic = dynamic_data.get("basic", {})
        if not isinstance(basic, dict):
            basic = {}

        major = module_dynamic.get("major", {})
        if not isinstance(major, dict):
            major = {}

        opus = major.get("opus")
        if not isinstance(opus, dict):
            opus = major.get("article") if isinstance(major.get("article"), dict) else {}

        title = _as_str(opus.get("title")).strip()
        action = pub_action or "发布专栏"
        text = f"{action}：{title}".strip()
        message = []
        if title:
            message.append(Text(title))

        raw_url = _as_str(basic.get("jump_url")).strip()

    elif dynamic_type == "DYNAMIC_TYPE_FORWARD":
        basic = dynamic_data.get("basic", {})
        if not isinstance(basic, dict):
            basic = {}

        desc = module_dynamic.get("desc", {})
        if not isinstance(desc, dict):
            desc = {}

        desc_text = _as_str(desc.get("text"))
        action = pub_action or "转发动态"
        text = f"{action}\n{desc_text}".rstrip()
        message = []

        rich_text_segments = parse_rich_text(desc.get("rich_text_nodes"))
        if rich_text_segments:
            message.extend(rich_text_segments)
        elif desc_text:
            message.append(Text(desc_text))

        origin_data = dynamic_data.get("orig")
        if isinstance(origin_data, dict):
            origin = parse_dynamic(origin_data)

        comment_id = _as_str(basic.get("comment_id_str")).strip()
        if comment_id:
            raw_url = f"//t.bilibili.com/{comment_id}"

    else:
        return None

    if not message and text:
        message = [Text(text)]

    return ParsedDynamic(
        mid=mid,
        name=name,
        dynamic_type=dynamic_type,
        timestamp=timestamp,
        text=text,
        message=message,
        url=_normalize_url(raw_url),
        id_str=id_str,
        action=f'{name} {action}',
        origin=origin,
    )
