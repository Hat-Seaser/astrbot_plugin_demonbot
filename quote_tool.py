"""文案工具：让模型自己决定什么时候来一句「含金量高」的话。

设计上刻意做得很克制——只有在群里确实在聊情绪、需要一句能戳到人的句子时，
模型才应该调它。返回的句子是给模型参考的素材，
它可以原样用，也可以按自己的语气改写一遍再说出口。
"""

from dataclasses import dataclass, field

from astrbot.api import FunctionTool
from astrbot.api.event import AstrMessageEvent


@dataclass
class QuoteTool(FunctionTool):
    """来一句情话 / 伤感文案 / 温柔语录 / 毒鸡汤。"""

    name: str = "get_quote"
    description: str = (
        "取一句现成的高质量短文案。适用于：有人在群里 emo、失恋、熬夜睡不着、"
        "让你说句好听的、让你安慰人、或者要你「来句骚话」的时候。"
        "拿到句子后可以直接用，也可以按自己的语气改一改再说，但别说这是查来的。"
    )
    parameters: dict = field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "kind": {
                    "type": "string",
                    "description": (
                        "文案类型。可选：情话（撩人/土味）、伤感（emo/失恋）、"
                        "温柔（治愈/安慰）、毒鸡汤（丧但好笑）、舔狗、晚安、一言（通用）"
                    ),
                    "enum": ["情话", "伤感", "温柔", "毒鸡汤", "舔狗", "晚安", "一言"],
                },
            },
            "required": ["kind"],
        }
    )
    plugin: object = None

    async def run(self, event: AstrMessageEvent, kind: str = "一言"):
        if self.plugin is None:
            return "文案功能未正确初始化。"
        try:
            from .. import quotes
        except Exception:  # noqa: BLE001
            return "文案模块没加载。"
        if quotes is None:
            return "文案模块没加载。"
        try:
            text, src = await quotes.fetch_one(
                kind,
                timeout=self.plugin._cfg("quotes", "timeout_seconds", default=8),
                max_chars=self.plugin._cfg("quotes", "max_chars", default=60),
            )
        except Exception as e:  # noqa: BLE001
            return f"取文案失败：{type(e).__name__}"
        return f"[{quotes.normalize_kind(kind)}素材，可直接用或改写]{text}"
