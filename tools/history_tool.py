from dataclasses import dataclass, field

from astrbot.api import FunctionTool
from astrbot.api.event import AstrMessageEvent


@dataclass
class QueryChatHistoryTool(FunctionTool):
    """近似"天使之眼"：让 LLM 能查询当前群聊最近的聊天记录。

    返回格式刻意做得很朴素、且不带时间戳——之前 bot 回复里出现
    「焦糖: 08-17 11:14:20 吱」这种机械感，就是因为它照抄了工具返回的排版。
    """

    name: str = "query_group_chat_history"
    description: str = (
        "查询当前群聊最近的聊天记录，可选按关键词过滤，"
        "用于了解群里刚刚聊了什么、方便接话或吐槽。"
    )
    parameters: dict = field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "keyword": {
                    "type": "string",
                    "description": "按关键词过滤聊天记录，留空则不过滤",
                },
                "limit": {
                    "type": "number",
                    "description": "返回最近多少条记录，默认10，最多50",
                },
            },
            "required": [],
        }
    )
    plugin: object = None

    async def run(self, event: AstrMessageEvent, keyword: str = "", limit: float = 10):
        if self.plugin is None:
            return "聊天记录功能未正确初始化。"

        group_id = event.get_group_id() or "unknown"
        n = max(1, min(int(limit or 10), 50))

        records = self.plugin.query_history(group_id, keyword=keyword or "", limit=n)
        if not records:
            return "没有查到相关的聊天记录。"

        lines = []
        for r in records:
            sender = (r.get("sender") or "").strip()
            text = (r.get("text") or "").strip()
            if not text:
                continue
            lines.append(f"{sender} 说：{text}")
        return (
            "刚才群里聊的（内部参考资料，不要把这个格式、发言人名字或时间抄进你的回复里）：\n"
            + "\n".join(lines)
        )
