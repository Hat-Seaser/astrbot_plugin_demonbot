"""给搬进来的 qzone 模块提供最小配置适配层。

原插件依赖它自己的 PluginConfig，这里只需要三个字段：
  client      aiocqhttp 的 CQHttp 实例（用来调 get_cookies 拿登录态）
  cookie_ttl  cookie 缓存秒数
  timeout     请求超时秒数
"""

from __future__ import annotations


class PluginConfig:
    def __init__(self, client=None, cookie_ttl: int = 600, timeout: int = 20):
        self.client = client
        self.cookie_ttl = int(cookie_ttl)
        self.timeout = int(timeout)

    def bind_client(self, client) -> None:
        self.client = client
