"""联网相关的 LLM 工具：让模型自己决定什么时候上网。

注意：这三个工具的返回内容是给模型看的"内部资料"，
人格提示词里已经明令禁止把"我查了一下"这类痕迹说出来。
"""

from dataclasses import dataclass, field

from astrbot.api import FunctionTool
from astrbot.api.event import AstrMessageEvent


@dataclass
class SearchWebTool(FunctionTool):
    """通用联网搜索：新闻、事件、某个东西是什么。"""

    name: str = "search_web"
    description: str = (
        "上网搜索最新信息。适用于：群里提到你不知道的新闻/事件/人物/游戏/产品，"
        "或者需要确认某件事的最新情况。返回几条搜索结果摘要。"
    )
    parameters: dict = field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "搜索关键词，尽量短且具体，比如「星穹铁道 3.0 版本」",
                },
            },
            "required": ["query"],
        }
    )
    plugin: object = None

    async def run(self, event: AstrMessageEvent, query: str = ""):
        if self.plugin is None:
            return "联网功能未正确初始化。"
        query = (query or "").strip()
        if not query:
            return "没有给搜索词。"
        results = await self.plugin.search_web_raw(query)
        if not results:
            return "网上没搜到有用的结果，这次就按自己知道的说，别提搜索这件事。"
        lines = []
        for r in results[:4]:
            title = (r.get("title") or "").strip()
            snippet = (r.get("snippet") or "").strip()[:180]
            lines.append(f"- {title}：{snippet}")
        return "搜索到的资料（内部参考，不要在回复里提到搜索）：\n" + "\n".join(lines)


@dataclass
class LookupSlangTool(FunctionTool):
    """专门查网络梗/缩写/新词，查完会自动存进本地词库。"""

    name: str = "lookup_slang"
    description: str = (
        "查一个网络流行梗、缩写或新词的意思（比如 676767、xswl、city不city）。"
        "只要群里出现你没把握的词，就先用这个查，不要自己瞎猜谐音或编造来源。"
        "查到的结果会自动记进词库，以后不用再查。"
    )
    parameters: dict = field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "term": {"type": "string", "description": "要查的那个词本身，不要带多余的字"},
            },
            "required": ["term"],
        }
    )
    plugin: object = None

    async def run(self, event: AstrMessageEvent, term: str = ""):
        if self.plugin is None:
            return "词库功能未正确初始化。"
        term = (term or "").strip()
        if not term:
            return "没给要查的词。"
        desc = await self.plugin.lookup_slang(term, event)
        if not desc:
            return (
                f"「{term}」查不到确切说法。别硬编一个来源或谐音解释，"
                f"更自然的做法是直接问一句这是什么意思。"
            )
        return f"{term} 的意思：{desc}（当成你本来就知道的事来用）"


@dataclass
class TeachSlangTool(FunctionTool):
    """群友当场解释了某个梗时，把解释存下来。"""

    name: str = "remember_slang"
    description: str = (
        "当群里有人当场解释了某个梗、缩写或黑话的含义时，用这个把它记进词库，"
        "以后遇到同一个词就不用再问也不用再查。"
    )
    parameters: dict = field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "term": {"type": "string", "description": "那个词"},
                "meaning": {"type": "string", "description": "群友给出的解释，压缩成一句话"},
            },
            "required": ["term", "meaning"],
        }
    )
    plugin: object = None

    async def run(self, event: AstrMessageEvent, term: str = "", meaning: str = ""):
        if self.plugin is None:
            return "词库功能未正确初始化。"
        term, meaning = (term or "").strip(), (meaning or "").strip()
        if not term or not meaning:
            return "词或解释是空的，没存。"
        self.plugin.slang_put(term, meaning, source="group")
        return "已记进词库。"
