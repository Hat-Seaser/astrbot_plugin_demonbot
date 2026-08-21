from dataclasses import dataclass, field

from astrbot.api import FunctionTool
from astrbot.api.event import AstrMessageEvent


def _scope_key(event: AstrMessageEvent) -> str:
    """记忆的作用域：优先按群维度记忆，没有群则按用户维度记忆。"""
    group_id = event.get_group_id()
    if group_id:
        return f"group:{group_id}"
    return f"user:{event.get_sender_id()}"


@dataclass
class RememberTool(FunctionTool):
    """近似"天使之魂"：记住一条关于用户/群聊的重要信息。"""

    name: str = "demon_remember"
    description: str = (
        "记住一条关于当前用户或群聊的重要信息（偏好、约定、人设相关事实等），"
        "以便以后回忆和保持角色一致性。"
    )
    parameters: dict = field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "content": {
                    "type": "string",
                    "description": "要记住的内容，简洁明了的一句话",
                },
                "tags": {
                    "type": "string",
                    "description": "可选，逗号分隔的标签，方便之后按关键词检索",
                },
            },
            "required": ["content"],
        }
    )
    plugin: object = None

    async def run(self, event: AstrMessageEvent, content: str, tags: str = ""):
        if self.plugin is None:
            return "记忆功能未正确初始化。"
        scope = _scope_key(event)
        self.plugin.remember(scope, content, tags)
        return "已经记住了。"


@dataclass
class RecallMemoryTool(FunctionTool):
    """近似"天使之魂"：回忆之前记住的信息。"""

    name: str = "demon_recall"
    description: str = "回忆之前记住的、关于当前用户或群聊的信息，可选按关键词过滤。"
    parameters: dict = field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "按关键词过滤，留空则返回最近记住的内容",
                },
                "limit": {
                    "type": "number",
                    "description": "返回最多多少条，默认5",
                },
            },
            "required": [],
        }
    )
    plugin: object = None

    async def run(self, event: AstrMessageEvent, query: str = "", limit: float = 5):
        if self.plugin is None:
            return "记忆功能未正确初始化。"
        scope = _scope_key(event)
        n = max(1, min(int(limit or 5), 20))

        records = self.plugin.recall(scope, query=query or "", limit=n)
        if not records:
            return "没有找到相关的记忆。"

        lines = [m["content"] for m in records]
        return "回忆起的内容（内部参考，不要说自己查过记录）：\n" + "\n".join(
            f"- {line}" for line in lines
        )
