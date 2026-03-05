<div align="center">
  <a href="https://v2.nonebot.dev/store"><img src="https://github.com/A-kirami/nonebot-plugin-template/blob/resources/nbp_logo.png" width="180" height="180" alt="NoneBotPluginLogo"></a>
  <br>
  <p><img src="https://github.com/A-kirami/nonebot-plugin-template/blob/resources/NoneBotPlugin.svg" width="240" alt="NoneBotPluginText"></p>
</div>

<div align="center">

# nonebot-plugin-bilibili-notifier

_✨ B站UP更新与开播通知插件 ✨_

<a href="./LICENSE">
    <img src="https://img.shields.io/github/license/owner/nonebot-plugin-bilibili-notifier.svg" alt="license">
</a>
<a href="https://pypi.python.org/pypi/nonebot-plugin-bilibili-notifier">
    <img src="https://img.shields.io/pypi/v/nonebot-plugin-bilibili-notifier.svg" alt="pypi">
</a>
<img src="https://img.shields.io/badge/python-3.8+-blue.svg" alt="python">

</div>

## 📖 介绍

插件会定时拉取B站动态和开播信息，并将匹配UP主的消息推送到指定QQ群。支持：

- 动态/视频更新推送
- 开播推送
- 自动点赞（按UP mid 或昵称）
- 推送目标支持按 UP mid 或昵称配置
- 转发动态原文控制（全文/关闭）
- 动态多页抓取

默认使用 [SAA](https://github.com/MountainDash/nonebot-plugin-send-anything-anywhere) 发送消息（理论上支持 SAA 支持的所有平台）。也可以通过 `bnotifier_use_saa = false` 关闭 SAA 依赖，直接使用 OneBot v11 原生 API 发送，此时无需安装 SAA。

## 💿 安装

<details open>
<summary>使用 nb-cli 安装</summary>

在 nonebot2 项目根目录执行：

```bash
nb plugin install nonebot-plugin-bilibili-notifier
```

</details>

<details>
<summary>使用包管理器安装</summary>

```bash
pip install nonebot-plugin-bilibili-notifier
# 或 pdm add nonebot-plugin-bilibili-notifier
# 或 poetry add nonebot-plugin-bilibili-notifier
```

然后在 `pyproject.toml` 的 `[tool.nonebot]` 中加入：

```toml
plugins = ["nonebot_plugin_bilibili_notifier"]
```

</details>

## ⚙️ 配置

在 NoneBot 项目的 `.env` 中配置以下字段。

### 必填项

| 配置项 | 默认值 | 说明 |
| --- | --- | --- |
| `bnotifier_cookies` | 无 | cookies JSON 文件路径 |

### 推送目标

| 配置项 | 默认值 | 说明 |
| --- | --- | --- |
| `bnotifier_push_updates` | `{}` | 更新推送，格式 `{UP标识: [group_id, ...]}`，UP标识可为 mid 或昵称 |
| `bnotifier_push_lives` | `{}` | 开播推送，格式 `{UP标识: [group_id, ...]}`，UP标识可为 mid 或昵称 |
| `bnotifier_push_updates_by_group` | `{}` | 按群配置更新推送，格式 `{group_id: [UP标识, ...]}`，UP标识可为 mid 或昵称；当 `group_id` 为空字符串时，UP 仍会被记录，但只会发给 `bnotifier_debug_user`，不发群 |
| `bnotifier_push_lives_by_group` | `{}` | 按群配置开播推送，格式 `{group_id: [UP标识, ...]}`，UP标识可为 mid 或昵称；当 `group_id` 为空字符串时，UP 仍会被记录，但只会发给 `bnotifier_debug_user`，不发群 |
| `bnotifier_push_type_blacklist` | `{}` | 动态类型黑名单，格式 `{group_id或UP标识: [dynamic_type, ...]}`，UP标识可为 mid 或昵称 |
| `bnotifier_debug_user` | `[]` | 额外接收所有推送的QQ私聊用户ID列表 |

### 业务行为

| 配置项 | 默认值 | 说明 |
| --- | --- | --- |
| `bnotifier_like` | `[]` | 自动点赞目标（UP mid 或昵称） |
| `bnotifier_forward_message_mode` | `"full"` | 转发原文模式：`full`/`none` |
| `bnotifier_skip_lottery_forward` | `true` | 是否跳过包含“中奖”的转发动态 |

### 适配器

| 配置项 | 默认值 | 说明 |
| --- | --- | --- |
| `bnotifier_use_saa` | `true` | 是否使用 SAA 发送消息。`true` 时依赖 `nonebot-plugin-saa` 并支持多平台；`false` 时直接调用 OneBot v11 API，无需 SAA |

### 拉取与性能

| 配置项 | 默认值 | 说明 |
| --- | --- | --- |
| `bnotifier_dynamic_update_interval` | `120` | 动态轮询间隔（秒） |
| `bnotifier_live_update_interval` | `60` | 开播轮询间隔（秒） |
| `bnotifier_dynamic_pages` | `1` | 每轮动态抓取页数 |
| `bnotifier_dynamic_features` | `"itemOpusStyle"` | 动态接口 features 参数 |
| `bnotifier_timezone_offset` | `-480` | 动态接口 timezone_offset 参数 |
| `bnotifier_live_fetch_size` | `50` | 每轮直播列表抓取数量 |
| `bnotifier_api_timeout` | `20` | API 超时时间（秒） |

### 状态与启动行为

| 配置项 | 默认值 | 说明 |
| --- | --- | --- |
| `bnotifier_ignore_old_dynamic_on_start` | `true` | 无状态缓存时是否忽略历史动态 |
| `bnotifier_persist_state` | `true` | 是否持久化最后动态时间与点赞黑名单 |
| `bnotifier_state_file` | `"last_update.json"` | 状态文件名（缓存目录下） |

## 🛠️ 调试命令

- 命令：`/dd <动态ID>`
- 示例：`/dd 1175425797536612360`
- 权限：发送者必须在 `bnotifier_debug_user` 列表中
- 行为：按动态ID拉取并解析动态，然后将消息推送给发送者
- 命令：`/dr` 或 `/dr <unix时间戳>`
- 示例：`/dr`、`/dr 1732012345`
- 权限：发送者必须在 `bnotifier_debug_user` 列表中
- 行为：重置 `last_update_timestamp`（不带参数时重置为当前时间）

## 🍪 cookies 文件格式

支持以下两种格式。

对象格式：

```json
{
  "sessdata": "...",
  "bili_jct": "...",
  "buvid3": "...",
  "dedeuserid": "..."
}
```

列表格式（浏览器导出常见格式）：

```json
[
  {"name": "SESSDATA", "value": "..."},
  {"name": "bili_jct", "value": "..."},
  {"name": "buvid3", "value": "..."},
  {"name": "DedeUserID", "value": "..."}
]
```

## 🧪 配置示例

```env
bnotifier_cookies="/path/to/cookies.json"

# 按 mid 配置（数字UID）
bnotifier_push_updates={"823532":["123456"]}
bnotifier_push_lives={"823532":["123456"]}

# 也可以用昵称配置
# bnotifier_push_updates={"某UP主":["123456"]}

bnotifier_like=["823532"]
bnotifier_debug_user=["10001"]

# 直接使用 OneBot v11 发送（不需要安装 SAA）
bnotifier_use_saa=false
```

## 其它

有问题或需求欢迎提 issue。
