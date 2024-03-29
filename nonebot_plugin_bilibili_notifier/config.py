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
    bnotifier_push_after: int = 0
    """
    dev用，只推送晚于这个时间的动态，默认0为程序启动时间
    """
    bnotifier_api_timeout: float = 20
    """
    API访问超时设置，不用管
    """
    bnotifier_timeshift: int = 45
