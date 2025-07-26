<div align="center">
  <a href="https://v2.nonebot.dev/store"><img src="https://github.com/A-kirami/nonebot-plugin-template/blob/resources/nbp_logo.png" width="180" height="180" alt="NoneBotPluginLogo"></a>
  <br>
  <p><img src="https://github.com/A-kirami/nonebot-plugin-template/blob/resources/NoneBotPlugin.svg" width="240" alt="NoneBotPluginText"></p>
</div>

<div align="center">

# nonebot-plugin-bilibili-notifier

_✨ B站UP的更新与开播通知机 ✨_


<a href="./LICENSE">
    <img src="https://img.shields.io/github/license/owner/nonebot-plugin-bilibili-notifier.svg" alt="license">
</a>
<a href="https://pypi.python.org/pypi/nonebot-plugin-bilibili-notifier">
    <img src="https://img.shields.io/pypi/v/nonebot-plugin-bilibili-notifier.svg" alt="pypi">
</a>
<img src="https://img.shields.io/badge/python-3.8+-blue.svg" alt="python">

</div>

## 📖 介绍

B站UP的更新与开播通知机，可以在指定的间隔爬取一次B站账号关注的动态与关注的正在直播的UP列表，自动给UP点赞，并将设置好的特定UP的更新或开播信息推送到QQ群里。

理论上适配[SAA](https://github.com/MountainDash/nonebot-plugin-send-anything-anywhere)支持的各种协议，实测了Onebot v11协议成功。

## 💿 安装

<details open>
<summary>使用 nb-cli 安装</summary>
在 nonebot2 项目的根目录下打开命令行, 输入以下指令即可安装

    nb plugin install nonebot-plugin-bilibili-notifier

</details>

<details>
<summary>使用包管理器安装</summary>
在 nonebot2 项目的插件目录下, 打开命令行, 根据你使用的包管理器, 输入相应的安装命令

<details>
<summary>pip</summary>

    pip install nonebot-plugin-bilibili-notifier
</details>
<details>
<summary>pdm</summary>

    pdm add nonebot-plugin-bilibili-notifier
</details>
<details>
<summary>poetry</summary>

    poetry add nonebot-plugin-bilibili-notifier
</details>
<details>
<summary>conda</summary>

    conda install nonebot-plugin-bilibili-notifier
</details>

打开 nonebot2 项目根目录下的 `pyproject.toml` 文件, 在 `[tool.nonebot]` 部分追加写入

    plugins = ["nonebot_plugin_bilibili_notifier"]

</details>

## ⚙️ 配置

首先用你的B站账号关注这些你想要追踪的UP主，并记住TA们的B站数字ID（以下简称BID）：（点击主页后地址中显示的space.bilibili.com/\[`中的这个号码`\]）。

然后导出B站cookies，建议使用Firefox的Export cookie JSON file for Puppeteer插件直接生成JSON文件，如果想手动生成，请确保以下字段有效：
```
{
  "sessdata": "",
  "bili_jct": "",
  "buvid3": "",
  "dedeuserid": "" 
}
```
建议导出cookies时使用和常用的浏览器不同的浏览器（或匿名模式），不然B站会定时要求刷新你的cookies。

在 nonebot2 项目的`.env`文件中添加下表中的必填配置

| 配置项 | 必填 | 默认值 | 说明 |
|:-----:|:----:|:----:|:----:|
| bnotifier_cookies | 是 | 无 | 上面导出的B站cookies的文件路径 |
| bnotifier_dynamic_update_interval | 是 | 60 | 动态更新获取间隔，单位秒，默认60秒一次 | 
| bnotifier_live_update_interval | 是 | 29 | 直播状态获取间隔，单位秒，默认29秒一次 | 
| bnotifier_push_updates | 否 | {} | 设置追踪更新的UP主的ID）以及要推送的QQ群号，格式为{<BID1>: \[QQ群号1， QQ群号2, ...\], ...}。注意全部号码都应为字符串，一个例子：`{"823532": ["xxxxx", "yyyyy"]}`|
| bnotifier_push_updates_by_group | 否 | {} | 作用同bnotifier_push_updates但格式为{QQ群: \[BID1, BID2, \]}，选择一个更适合你的方式即可 |
| bnotifier_push_lives | 否 | {} | 同bnotifier_push_updates但这个变量控制的是开播通知 |
| bnotifier_push_lives_by_group | 否 | {} | 格式同bnotifier_push_updates_by_group但这个变量控制的是开播通知 |
| bnotifier_push_type_blacklist | 否 | {} | 屏蔽推送某种动态信息，格式：`{QQ群号或BID: ['DYNAMIC_TYPE_AV', 'DYNAMIC_TYPE_FORWARD', ...]}`这样会屏蔽特定群/特定up的某种动态信息。关于动态种类请参考[这个连接](https://github.com/SocialSisterYi/bilibili-API-collect/blob/master/docs/dynamic/dynamic_enum.md) |
| bnotifier_msg_truncate | 否 | 500 | 将一条非常长的动态截短到设定的长度 |
| bnotifier_like | 否 | [] | BID或UP昵称列表，bot会给这个列表中的UP发的所有内容点赞 |
| bnotifier_debug_user | 否 | [] | 给列表中的*QQ用户*发送所有B站消息 |


其它配置为开发调试时使用，正常使用无需调整。

### 效果图
![发送动态效果](demos/dyna-notification-demo.png)
![开播推送效果](demos/live-notification-demo.png)


## 其它
有问题或新功能需求，请提issue。
