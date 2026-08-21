"""群友档案相关的 LLM 工具：让模型能分清群里谁是谁。"""

from dataclasses import dataclass, field

from astrbot.api import FunctionTool
from astrbot.api.event import AstrMessageEvent


@dataclass
class LookupMemberTool(FunctionTool):
    name: str = "lookup_member"
    description: str = (
        "查本群某个群友的档案：他的编号、用过的昵称、发言量、以及之前记下的关于他的事。"
        "适用于有人提到某个名字但你拿不准是谁的时候。"
    )
    parameters: dict = field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "who": {
                    "type": "string",
                    "description": "昵称、QQ号或编号（比如 M03）之一",
                },
            },
            "required": ["who"],
        }
    )
    plugin: object = None

    async def run(self, event: AstrMessageEvent, who: str = ""):
        if self.plugin is None:
            return "群友档案未正确初始化。"
        group_id = event.get_group_id() or "unknown"
        rec = self.plugin.find_member(group_id, who)
        if not rec:
            return f"群里没找到「{who}」，可能是新人或者改过名。"
        names = "、".join(rec.get("names", [])) or "（没记到昵称）"
        notes = "；".join(rec.get("notes", [])) or "（暂无）"
        return (
            f"编号 {rec.get('code')}，用过的昵称：{names}，"
            f"发言 {rec.get('count', 0)} 条，"
            f"{'是群主人' if rec.get('is_admin') else '普通群友'}，"
            f"记下的事：{notes}。"
            f"（内部资料，回复里不要说出编号和QQ号）"
        )


@dataclass
class NoteMemberTool(FunctionTool):
    name: str = "note_member"
    description: str = (
        "把关于某个群友的、值得长期记住的事记下来（爱好、身份、口头禅、雷点等）。"
        "聊天中自然得知就顺手记一条，不用告诉对方你记了。"
    )
    parameters: dict = field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "who": {"type": "string", "description": "昵称、QQ号或编号"},
                "note": {"type": "string", "description": "要记住的事，一句话，20字以内最好"},
            },
            "required": ["who", "note"],
        }
    )
    plugin: object = None

    async def run(self, event: AstrMessageEvent, who: str = "", note: str = ""):
        if self.plugin is None:
            return "群友档案未正确初始化。"
        group_id = event.get_group_id() or "unknown"
        if not (who or "").strip() or not (note or "").strip():
            return "人或内容是空的，没记。"
        return self.plugin.note_member(group_id, who.strip(), note.strip())
