from pydantic import BaseModel

class Config(BaseModel):  
    bnotifier_cookies: str
    """
    b站cookies地址
    """ 
    bnotifier_push_updates: dict = {}
    """
    推送视频/动态更新的up
    {UPs: [QQ群, ...]}
    """
    bnotifier_push_lives: dict = {}
    """
    推送直播更新的up
    {UPs: [QQ群, ...]}
    """
    bnotifier_push_updates_by_group: dict = {}
    """
    推送视频/动态更新的up (以QQ群为key)
    {QQ群: [UPs, ...]}
    """
    bnotifier_push_lives_by_group: dict = {}
    """
    推送直播更新的up (以QQ群为key)
    {QQ群: [UPs, ...]}
    """
    bnotifier_push_type_blacklist: dict = {}
    """
    不推送某个UP/某个群的某种动态信息
    
    {
        qq群：['DYNAMIC_TYPE_AV'],   不在群里推送视频投稿信息
        UP: ['DYNAMIC_TYPE_FORWARD'] 不推送这个up的转发信息
    }
    """
    bnotifier_like: list = []
    """
    刷到某人的动态的时候自动点赞，可mid或者昵称
    """
    bnotifier_api_timeout: float = 20
    """
    dev用，API访问超时设置，如果网络不稳定可以酌情加大
    """
    bnotifier_msg_truncate: int = 150
    """
    截断一条超长的动态
    """
    bnotifier_debug_user: list = []
    """
    所有原始消息都发给这个用户一份
    """
    bnotifier_dynamic_update_interval: int = 60
    """
    动态获取间隔，单位秒，默认60秒一次
    """
    bnotifier_live_update_interval: int = 29
    """
    直播获取间隔，单位秒，默认29秒一次
    """