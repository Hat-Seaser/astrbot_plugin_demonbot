"""
恶魔bot (astrbot_plugin_demonbot)

自维护、自包含的群聊拟人插件，不依赖任何其他插件。

本版新增/修复：
1. knowledge  联网学梗：检测到没见过的新词（676767 / xswl 这类）自动上网查，
               查完写进本地词库，并把结论注入这次回复的 system prompt；
               另外定时抓热搜，让 bot 知道最近大家在聊什么。
2. members    群友编号库：每个群每个人自动编号（M01、M02…），记昵称/别名/
               发言量/你给的备注，注入 prompt 让 bot 分得清谁是谁。
3. style      风格自学习：定时用 LLM 归纳群里（尤其是管理员你）的真实说话习惯，
               存成"风格速记"注入 prompt，代替死板的人设描述。
4. reply_style 回复清洗 + 长度硬控：去掉 markdown、去掉"昵称: 08-17 11:14:20"
               这种把聊天记录格式念出来的机械前缀、砍到一句话。
5. segment    分段乱序修复：改用 after_message_sent 钩子按顺序补发后续段落，
               不再用 asyncio.create_task 和框架抢发送顺序（默认直接关掉分段）。
6. end_talk   收到"嗯嗯/好的/哦哦"这类结束语时闭麦，并短时间不再主动插话。
7. mute       本地闭嘴 + QQ平台禁言(retcode 1200)自愈：管理员任何时候都能解，
               被平台禁言时自动退避，不再反复撞墙浪费 token。
"""

import asyncio
import json
import random
import re
import time
import zipfile
from pathlib import Path

import astrbot.api.message_components as Comp
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star
from astrbot.core.utils.astrbot_path import get_astrbot_data_path

# ---- 子模块导入做成"可降级"的 ----
# 手动合并代码时最常见的事故是漏传某个文件（比如只更新了 main.py 没传 websearch.py），
# 而顶层 import 一旦抛异常，整个插件会直接加载失败、所有功能连同指令一起消失，
# 表现就是"压根无法运行"。这里把每个子模块单独包起来：
# 缺哪个就停哪个功能，插件本身照常起来，/自检 会告诉你缺了什么。
_BOOT_ERRORS = []

try:
    from . import websearch
except Exception as _e:  # noqa: BLE001
    websearch = None
    _BOOT_ERRORS.append(f"websearch.py 加载失败：{type(_e).__name__}: {_e}")

try:
    from . import images
except Exception as _e:  # noqa: BLE001
    images = None
    _BOOT_ERRORS.append(f"images.py 加载失败：{type(_e).__name__}: {_e}")

try:
    from . import life
except Exception as _e:  # noqa: BLE001
    life = None
    _BOOT_ERRORS.append(f"life.py 加载失败：{type(_e).__name__}: {_e}")

try:
    from . import stickers
except Exception as _e:  # noqa: BLE001
    stickers = None
    _BOOT_ERRORS.append(f"stickers.py 加载失败：{type(_e).__name__}: {_e}")

try:
    from . import quotes
except Exception as _e:  # noqa: BLE001
    quotes = None
    _BOOT_ERRORS.append(f"quotes.py 加载失败：{type(_e).__name__}: {_e}")

try:
    from . import responses
except Exception as _e:  # noqa: BLE001
    responses = None
    _BOOT_ERRORS.append(f"responses.py 加载失败：{type(_e).__name__}: {_e}")

try:
    from . import poetry
except Exception as _e:
    poetry = None
    _BOOT_ERRORS.append(f"poetry.py 加载失败：{type(_e).__name__}: {_e}")

try:
    from . import music
except Exception as _e:
    music = None
    _BOOT_ERRORS.append(f"music.py 加载失败：{type(_e).__name__}: {_e}")

try:
    from .tools.history_tool import QueryChatHistoryTool
    from .tools.knowledge_tool import LookupSlangTool, SearchWebTool, TeachSlangTool
    from .tools.member_tool import LookupMemberTool, NoteMemberTool
    from .tools.memory_tool import RecallMemoryTool, RememberTool
    try:
        from .tools.quote_tool import QuoteTool
    except Exception:
        QuoteTool = None
        _BOOT_ERRORS.append("tools/quote_tool.py 缺失：文案工具降级，不影响核心聊天")
    _TOOLS_IMPORTED = True
except Exception as _e:  # noqa: BLE001
    _TOOLS_IMPORTED = False
    QuoteTool = None
    _BOOT_ERRORS.append(f"tools/ 加载失败：{type(_e).__name__}: {_e}")

PLUGIN_DATA_DIRNAME = "demonbot"

# 复读比对用：去掉标点/空白，方便"哈哈哈"和"哈哈哈哈"被识别成同一轮复读
_NORMALIZE_RE = re.compile(r"[\s,.!?，。！？~～、\-_]+")

# 分段发送用：按这些标点切分句子，切完之后标点本身会被丢弃
_SPLIT_RE = re.compile(r"[，,。.！!？?；;、\n]+")

# 昵称/正文里的不可见控制字符（QQ 昵称里经常夹这些，会污染 LLM 理解）
_CTRL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f\u200b-\u200f\u202a-\u202e\ufeff]")

# "08-17 11:14:20" / "2026-08-17 11:14" 这类时间戳
_TS_RE = re.compile(r"(?:\d{4}[-/])?\d{1,2}[-/]\d{1,2}\s*\d{1,2}:\d{2}(?::\d{2})?")

# markdown 痕迹
_MD_BOLD_RE = re.compile(r"(\*\*|__|`+)")
_MD_HEAD_RE = re.compile(r"^\s{0,3}#{1,6}\s*", re.M)
_MD_LIST_RE = re.compile(r"^\s{0,3}(?:[-*+]|\d{1,2}[.、)])\s+", re.M)

# "焦糖:" / "【焦糖】" / "焦糖：" 这类自称前缀
_NAME_PREFIX_RE = re.compile(r"^\s*[\[【(（]?([^\s:：\]】)）]{1,14})[\]】)）]?\s*[:：]\s*")

# 候选新词：数字梗 / 字母缩写
_NUM_TERM_RE = re.compile(r"(?<![0-9])([0-9]{3,12})(?![0-9])")
_ALPHA_TERM_RE = re.compile(r"(?<![A-Za-z])([A-Za-z]{2,8})(?![A-Za-z])")

# 字母缩写里明显是普通英文/常识词的，不要拿去搜
_ALPHA_STOPWORDS = {
    "ok", "okay", "no", "yes", "yeah", "hi", "hello", "hey", "bye", "lol", "the", "and",
    "you", "me", "my", "im", "is", "are", "am", "qq", "ai", "bot", "gpt", "api", "url",
    "http", "https", "www", "com", "cn", "png", "jpg", "gif", "mp4", "pdf", "id", "vs",
    "pc", "ios", "app", "usb", "cpu", "gpu", "ram", "wifi", "tv", "cd", "dvd", "ktv",
}

# 客服式收尾，出现就砍掉
_SERVICE_TAILS = [
    "有什么我可以帮", "有什么可以帮", "还有什么可以帮", "还有什么需要", "还有其他问题",
    "希望能帮到你", "请问还有", "如果有需要", "有需要随时", "有什么想聊",
    "祝你", "很抱歉", "作为一个", "需要我", "要不要我",
]

# 心情时间表：按小时判断"现在大概在做什么"，baseline 是这个时间段的心情基线（-1~1）
DEFAULT_MOOD_SCHEDULE = [
    {"start": 0, "end": 6, "activity": "熬夜/睡觉", "label": "深夜", "baseline": -0.35},
    {"start": 6, "end": 9, "activity": "起床、洗漱、赶着出门", "label": "清晨", "baseline": -0.1},
    {"start": 9, "end": 12, "activity": "上班/上课，忙自己的事", "label": "上午", "baseline": -0.05},
    {"start": 12, "end": 14, "activity": "吃午饭、摸鱼休息", "label": "中午", "baseline": 0.25},
    {"start": 14, "end": 18, "activity": "下午上班/上课，有点犯困", "label": "下午", "baseline": -0.15},
    {"start": 18, "end": 20, "activity": "下班/放学、吃晚饭", "label": "傍晚", "baseline": 0.3},
    {"start": 20, "end": 23, "activity": "自由活动，打游戏/出去玩/追剧", "label": "晚上", "baseline": 0.35},
    {"start": 23, "end": 24, "activity": "准备睡觉，有点犯困", "label": "深夜前", "baseline": -0.05},
]

# 有人直接问"在干嘛"时才允许把活动说出来
_ASK_ACTIVITY_RE = re.compile(r"(在干|干嘛|干什么|在忙|在做什么|睡了没|起了没|吃了没|在不在)")

# ---- 高峰时段（API 官方双倍计费）默认窗口，按北京时间 ----
DEFAULT_PEAK_WINDOWS = ["09:00-12:00", "14:00-18:00"]

# 「（迷迷糊糊翻了个身）」这类旁白式动作描写：真人打字不会这样，纯属多烧 token
_ACTION_PAREN_RE = re.compile(r"[（(][^（()）]{0,30}?[）)]")

# 没人问就主动播报行踪的句子。只匹配明确的作息/活动词，避免误伤「我在想」这类正常表达
_ACTIVITY_WORDS = (
    "睡|补觉|躺|床上|被窝|起床|洗漱|出门|上班|上课|下班|放学|加班|摸鱼|"
    "吃饭|吃早饭|吃午饭|吃晚饭|点外卖|外卖|画画|打游戏|追剧|看剧|写作业|赶路|忙着"
)
_SELF_REPORT_RE = re.compile(
    rf"[，,。！!？?；;、\s]*我(?:现在|这会儿|刚|还|正|才)*(?:在|要去|准备|刚)"
    rf"(?:{_ACTIVITY_WORDS})[^，,。！!？?；;\n]{{0,8}}"
)

DEFAULT_END_PHRASES = [
    "嗯嗯", "嗯", "恩恩", "哦哦", "哦", "噢", "好的", "好", "好吧", "行", "行吧",
    "知道了", "收到", "了解", "懂了", "ok", "okk", "okay", "去吧", "没事了", "算了",
    "就这样", "溜了", "拜拜", "886", "88", "👌", "好嘞", "得嘞",
]


class DemonBotPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config or {}

        # ---------- 数据目录：放在 data/plugin_data 下，插件更新/重装不会丢 ----------
        self.data_dir = Path(get_astrbot_data_path()) / "plugin_data" / PLUGIN_DATA_DIRNAME
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.history_file = self.data_dir / "chat_history.json"
        self.memory_file = self.data_dir / "memory.json"
        self.mood_file = self.data_dir / "mood.json"
        self.members_file = self.data_dir / "members.json"
        self.knowledge_file = self.data_dir / "knowledge.json"
        self.style_file = self.data_dir / "style.json"
        self.r18_file = self.data_dir / "r18_users.json"
        # /记住标签 存的自定义「中文词 -> Pixiv 标签」映射，优先级高于 WebUI 里的 keyword_map
        self.imgtag_file = self.data_dir / "image_tags.json"
        self.auto_imgtag_file = self.data_dir / "image_tags_auto.json"
        self.persona_file = self.data_dir / "persona.md"
        self.token_usage_file = self.data_dir / "token_usage.json"
        self.like_usage_file = self.data_dir / "like_usage.json"
        self.like_grants_file = self.data_dir / "like_grants.json"

        self._history: dict = self._load_json(self.history_file, {})
        self._memory: dict = self._load_json(self.memory_file, {})
        self._mood: dict = self._load_json(self.mood_file, {"score": 0.0, "last_update": 0})
        self._members: dict = self._load_json(self.members_file, {})
        self._knowledge: dict = self._load_json(
            self.knowledge_file, {"slang": {}, "hot": {"time": 0, "items": []}}
        )
        self._style: dict = self._load_json(self.style_file, {"profile": "", "updated": 0})
        self._persona = self._load_or_create_persona()
        self._token_usage = self._load_json(self.token_usage_file, {
            "day": "", "total": 0, "input": 0, "output": 0, "requests": 0,
            "aux_total": 0, "aux_requests": 0, "estimated": 0, "by_source": {}
        })
        self._like_usage = self._load_json(self.like_usage_file, {"day": "", "sent": {}})
        self._like_grants = self._load_json(self.like_grants_file, {"users": {}})
        self._last_poke_at: dict = {}
        self._known_groups: set[str] = set()
        self._last_owner_message_at: float = 0.0
        self._last_poetry_push_at: float = 0.0
        self._like_daily_task = None
        self._last_bot = None
        self._friend_request_seen: set = set()
        # 已开启 R18 分区的用户 QQ 号集合（仅私聊 /age>18 可写）
        _r18_raw = self._load_json(self.r18_file, {"users": []})
        self._r18_users: set = {str(x) for x in (_r18_raw.get("users") or [])}
        # {"校服": ["制服"], ...}
        _imgtag_raw = self._load_json(self.imgtag_file, {})
        self._img_tags: dict = {
            str(k): [str(x) for x in (v if isinstance(v, list) else [v]) if str(x).strip()]
            for k, v in (_imgtag_raw or {}).items()
            if str(k).strip()
        }
        _auto_imgtag_raw = self._load_json(self.auto_imgtag_file, {})
        self._auto_img_tags: dict = {
            str(k): [str(x) for x in (v if isinstance(v, list) else [v]) if str(x).strip()]
            for k, v in (_auto_imgtag_raw or {}).items()
            if str(k).strip()
        }

        self._last_reply_at: dict = {}
        self._last_repeat_at: dict = {}
        # 记录每个群"当前这条复读链"已经跟过的文本，跟过就不再跟第二次，
        # 直到链条被打断（有人发了不一样的内容）才清空，允许跟下一条新链
        self._repeat_joined: dict = {}
        self._muted_until: dict = {}
        self._muted_by: dict = {}
        # QQ 平台把 bot 禁言时（retcode 1200）的退避时间，避免反复撞墙浪费 token
        self._platform_muted_until: dict = {}
        # 收到"嗯嗯/好的"这类结束语后的沉默期
        self._quiet_until: dict = {}
        # 高频群滑动窗口内 bot 主动插话的时间戳
        self._auto_reply_times: dict = {}
        # 发图冷却：按「群」记一份，按「人」再记一份，管理员可豁免
        self._last_image_at: dict = {}
        self._last_image_user_at: dict = {}
        # 后台辅助 LLM 调用的当日预算（学风格/查梗/插话判断这些用户看不见的请求）
        self._aux_budget: dict = {"day": "", "count": 0}
        # 本次进程里省下的请求数，供 /省钱 展示
        self._saver_stats: dict = {"peak_skipped": 0, "aux_blocked": 0, "ctx_trimmed": 0}
        # 分段发送：待补发的尾段，key 为会话，由 after_message_sent 按顺序取走
        self._pending_tail: dict = {}
        self._pending_bot_segments: dict = {}
        self._tail_sending: set = set()
        # 本轮请求给 LLM 注入过的新词，用于日志排查
        self._style_lock = asyncio.Lock()
        self._hot_lock = asyncio.Lock()
        self._slang_locks: dict = {}

        # ---------- 管理员/群白名单 ----------
        self.admin_ids = {str(x) for x in self._cfg("reply_gate", "admin_ids", default=[])}
        self.group_whitelist = {str(x) for x in self._cfg("reply_gate", "group_ids", default=[])}

        # ---------- 主人身份：跨群识别 ----------
        # QQ 号是硬凭据，昵称/ID 是软凭据（换群改昵称也能认出来）。
        # 主人自动进管理员集合，不用在两个地方各填一遍。
        self.owner_ids = {str(x).strip() for x in (self._cfg("owner", "qq", default=[]) or []) if str(x).strip()}
        self.owner_names = {
            self._clean_nick(str(x)).lower()
            for x in (self._cfg("owner", "names", default=[]) or []) if str(x).strip()
        }
        # 默认主人：遂意（QQ 2677518198）。
        # 仍然以 owner.qq / owner.names 为正式配置；这里仅在配置为空时提供默认身份，
        # 防止升级后旧配置没有 owner 字段导致主人在群里被当成普通群友。
        if not self.owner_ids and not self.owner_names:
            self.owner_ids.add("2677518198")
            self.owner_names.add("遂意")
            self.config.setdefault("owner", {})["qq"] = sorted(self.owner_ids)
            self.config["owner"]["names"] = sorted(self.owner_names)
            self.config["owner"].setdefault("title", "主人")
            self._persist_config()
        self.admin_ids |= self.owner_ids
        self._last_sponsor_at = 0.0

        self._migrate_config()
        try:
            loop = asyncio.get_running_loop()
            self._like_daily_task = loop.create_task(self._daily_like_loop())
            self._poetry_idle_task = loop.create_task(self._poetry_idle_loop())
        except RuntimeError:
            self._like_daily_task = None
            self._poetry_idle_task = None
        self._last_bot = None

        # ---------- 生活状态 / 表情包 ----------
        self._life = None
        if life is not None:
            try:
                self._life = life.LifeState(
                    seed_salt=self._bot_self_name(),
                    wake_hour=self._cfg("life", "wake_hour", default=7),
                    weekend_wake_hour=self._cfg("life", "weekend_wake_hour", default=10),
                )
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[恶魔bot] 生活状态初始化失败：{e}")
        # 同一个活动一天只主动交代一次，靠这个集合去重
        self._life_last_key = ""
        self._sticker_box = None
        self._sticker_gate = stickers.StickerGate() if stickers is not None else None
        self._pending_sticker: dict = {}
        self._reload_stickers()

        # ---------- 注册函数工具 ----------
        # 同样包起来：不同 AstrBot 版本的 FunctionTool 接口有差异，
        # 注册失败不该拖垮整个插件——工具没了顶多少几个能力，指令和聊天要能照跑。
        self._boot_errors = list(_BOOT_ERRORS)
        self._tools_ready = False
        if _TOOLS_IMPORTED:
            try:
                self.context.add_llm_tools(
                    QueryChatHistoryTool(plugin=self),
                    RememberTool(plugin=self),
                    RecallMemoryTool(plugin=self),
                    SearchWebTool(plugin=self),
                    LookupSlangTool(plugin=self),
                    TeachSlangTool(plugin=self),
                    LookupMemberTool(plugin=self),
                    NoteMemberTool(plugin=self),
                    QuoteTool(plugin=self),
                )
                self._tools_ready = True
            except Exception as e:  # noqa: BLE001
                self._boot_errors.append(f"LLM工具注册失败：{type(e).__name__}: {e}")

        for _err in self._boot_errors:
            logger.error(f"[恶魔bot] 启动告警：{_err}")

        logger.info(
            "[恶魔bot] 插件已加载，管理员=%d 群白名单=%d 词库=%d 群友档案=%d",
            len(self.admin_ids),
            len(self.group_whitelist),
            len(self._knowledge.get("slang", {})),
            sum(len(g.get("members", {})) for g in self._members.values()),
        )

    # ==================== 通用小工具 ====================

    def _migrate_config(self):
        """一次性迁移：把几个「旧配置值会静默压过新默认值」的开关强制拨正。

        AstrBot 会把插件配置存成独立的 json，schema 里改了 default 对**已经存过配置**
        的用户是不生效的——旧值原样保留。分段发送就是典型：新版默认关，
        但老用户的配置里存着 true，升级后仍然一次发四条，看起来像代码没生效。
        """
        try:
            version = int(self.config.get("_schema_version") or 0)

            # v8：即使老配置已经是旧版 schema，也必须把新默认值写进去。
            # AstrBot 会持久化旧配置，单纯修改 schema default 对老用户不会生效。
            sleep_cfg = self.config.setdefault("sleep_mode", {})
            sleep_cfg.setdefault("enabled", True)
            sleep_cfg.setdefault("start", "23:59")
            sleep_cfg.setdefault("end", "07:30")

            persona_cfg = self.config.setdefault("persona", {})
            persona_cfg.setdefault("enabled", True)
            persona_cfg.setdefault("file", "persona.md")
            persona_cfg.setdefault("template", "persona_template.md")
            persona_cfg.setdefault("max_inject_chars", 900)

            poke_cfg = self.config.setdefault("poke", {})
            poke_cfg.setdefault("enabled", True)
            poke_cfg.setdefault("cooldown_seconds", 20)
            poke_cfg.setdefault("auto_profile", True)

            friend_cfg = self.config.setdefault("friend_request", {})
            friend_cfg.setdefault("auto_accept", True)
            friend_cfg.setdefault("log_requests", True)

            like_cfg = self.config.setdefault("like", {})
            like_cfg.setdefault("daily_owner", True)
            like_cfg.setdefault("auto_run_interval_seconds", 600)
            like_cfg.setdefault("max_times_per_user", 10)

            poke_cfg.setdefault("mention_id_mode", "none")

            poetry_cfg = self.config.setdefault("poetry", {})
            poetry_cfg.setdefault("enabled", True)
            poetry_cfg.setdefault("idle_enabled", True)
            poetry_cfg.setdefault("idle_threshold_seconds", 7200)
            poetry_cfg.setdefault("idle_check_interval_seconds", 900)
            poetry_cfg.setdefault("idle_chance", 0.18)
            poetry_cfg.setdefault("idle_min_gap_seconds", 43200)
            poetry_cfg.setdefault("reply_replace_chance", 0.10)

            music_cfg = self.config.setdefault("music", {})
            music_cfg.setdefault("enabled", True)
            music_cfg.setdefault("local_clue_file", "music_clues.json")
            music_cfg.setdefault("min_text_length", 6)

            stickers_cfg = self.config.setdefault("stickers", {})
            stickers_cfg.setdefault("enabled", True)
            stickers_cfg.setdefault("chance", 0.75)
            stickers_cfg.setdefault("cooldown_seconds", 60)
            stickers_cfg.setdefault("daily_limit", 80)
            stickers_cfg.setdefault("always_after_reply", True)
            stickers_cfg.setdefault("always_on_scold", True)

            token_cfg = self.config.setdefault("token_saver", {})
            token_cfg.setdefault("enabled", True)
            token_cfg["max_context_messages"] = min(max(int(token_cfg.get("max_context_messages") or 4), 1), 4)
            token_cfg["max_context_chars_per_message"] = min(max(int(token_cfg.get("max_context_chars_per_message") or 90), 40), 90)
            token_cfg["max_system_prompt_chars"] = min(max(int(token_cfg.get("max_system_prompt_chars") or 900), 300), 900)
            token_cfg["max_persona_chars"] = min(max(int(token_cfg.get("max_persona_chars") or 900), 300), 900)
            token_cfg.setdefault("daily_aux_calls", 30)

            if version >= 5:
                self.config["_schema_version"] = max(version, 8)
                if hasattr(self.config, "save_config"):
                    self.config.save_config()
                return
            if version >= 4:
                # ---- v5：表情包 / 生活状态 / 主人 / 求赞助 / 文案 / 多模态 ----
                self.config.setdefault("owner", {}).setdefault("title", "主人")
                self.config["owner"].setdefault("qq", sorted(self.admin_ids))
                self.config["owner"].setdefault("names", [])
                for sec, defaults in (
                    ("life", {"enabled": True, "wake_hour": 7, "weekend_wake_hour": 10,
                              "volunteer_on_change": True, "volunteer_chance": 0.35}),
                    ("stickers", {"enabled": True, "folder": "", "chance": 0.9,
                                  "cooldown_seconds": 180, "daily_limit": 40,
                                  "always_on_scold": True}),
                    ("sponsor", {"enabled": True, "sticker_emotion": "出bug",
                                 "cooldown_seconds": 600, "show_reason": False,
                                 "on_image_fail": False}),
                    ("quotes", {"enabled": True, "timeout_seconds": 8, "max_chars": 60}),
                    ("vision", {"enabled": True, "timeout_seconds": 40, "ignore_budget": False}),
                    ("voice", {"enabled": True, "max_chars": 80, "timeout_seconds": 30}),
                ):
                    node = self.config.setdefault(sec, {})
                    for k, v in defaults.items():
                        node.setdefault(k, v)
                imgs5 = self.config.setdefault("images", {})
                imgs5.setdefault("mature_group_whitelist", [])
                imgs5.setdefault("notify_on_empty", True)
                # 主人没单独配时，默认沿用管理员名单
                self.owner_ids |= {str(x) for x in (self.config["owner"].get("qq") or [])}
                self.admin_ids |= self.owner_ids
                self.config["_schema_version"] = 5
                if hasattr(self.config, "save_config"):
                    self.config.save_config()
                logger.info("[恶魔bot] 配置已迁移到 v5（表情包/生活状态/主人/求赞助）")
                return
            if version >= 3:
                # ---- v4：只补 Pixiv 直连相关的新键，别的照旧 ----
                imgs = self.config.setdefault("images", {})
                backends = list(imgs.get("backends") or [])
                if backends and "pixiv" not in backends:
                    imgs["backends"] = ["pixiv"] + backends
                    logger.info("[恶魔bot] 迁移：图源顺序前面加上了 pixiv（官方排行榜）")
                imgs.setdefault("prefer_ranking", True)
                imgs.setdefault("rank_modes", ["monthly", "weekly", "daily"])
                imgs.setdefault("rank_pages", 2)
                imgs.setdefault("rank_tag_pages", 22)
                imgs.setdefault("rank_cache_seconds", 21600)
                imgs.setdefault("pixiv_cookie", "")
                imgs.setdefault("pixiv_host", "https://www.pixiv.net")
                imgs.setdefault("users_gate", [10000, 5000, 1000])
                imgs.setdefault("month_only", True)
                imgs.setdefault("search_pages", 2)
                imgs.setdefault("min_bookmarks", 0)
                imgs.setdefault("send_all_pages", True)
                imgs.setdefault("forward_multi_page", True)
                imgs.setdefault("max_pages_per_illust", 20)
                self.config["_schema_version"] = 5
                if hasattr(self.config, "save_config"):
                    self.config.save_config()
                logger.info("[恶魔bot] 配置已迁移到 v4（Pixiv 排行榜取图已启用）")
                return
            seg = self.config.setdefault("segment_reply", {})
            if seg.get("enabled"):
                seg["enabled"] = False
                logger.warning("[恶魔bot] 迁移：检测到旧配置开着分段发送，已自动关闭（想要可在WebUI重开）")
            mood = self.config.setdefault("mood", {})
            mood.setdefault("mention_activity_when_asked", True)
            # ---- v3：省钱相关的默认值 ----
            # 老用户的配置文件里存着旧值，schema 改默认对他们不生效，
            # 所以这几项要主动拨正，否则升级完账单照样爆。
            imgs = self.config.setdefault("images", {})
            if imgs.pop("original", None):
                logger.warning("[恶魔bot] 迁移：检测到旧配置在发原图，已改成「高」画质")
            imgs.setdefault("quality", "regular")
            imgs.setdefault("per_user_cooldown_seconds", 300)
            imgs.setdefault("admin_bypass_cooldown", True)
            imgs.setdefault("real_enabled", True)
            imgs.setdefault("notify_on_cooldown", True)

            style_cfg = self.config.setdefault("reply_style", {})
            if int(style_cfg.get("max_chars") or 0) > 40:
                style_cfg["max_chars"] = 26
                logger.warning("[恶魔bot] 迁移：回复字数上限过大，已收到 26 字")
            style_cfg.setdefault("strip_action_text", True)
            style_cfg.setdefault("strip_self_report", True)

            gate = self.config.setdefault("reply_gate", {})
            gate.setdefault("speak", True)
            gate.setdefault("reply_to_members", True)
            gate.setdefault("reply_to_admin", True)
            if gate.get("mode") == "llm":
                gate["mode"] = "rule"
                logger.warning("[恶魔bot] 迁移：插话判断从 llm 改回 rule，llm 模式每条消息都要多花一次请求")

            mem = self.config.setdefault("members", {})
            if int(mem.get("roster_limit") or 0) > 8:
                mem["roster_limit"] = 8
            if int(mem.get("recent_context_lines") or 0) > 4:
                mem["recent_context_lines"] = 4

            kn = self.config.setdefault("knowledge", {})
            kn.setdefault("inject_hot_topics", False)

            self.config.setdefault("peak_hours", {}).setdefault("enabled", True)
            self.config.setdefault("token_saver", {}).setdefault("enabled", True)

            # ---- v4：Pixiv 直连相关的新键 ----
            backends = list(imgs.get("backends") or [])
            if backends and "pixiv" not in backends:
                imgs["backends"] = ["pixiv"] + backends
            imgs.setdefault("prefer_ranking", True)
            imgs.setdefault("rank_modes", ["monthly", "weekly", "daily"])
            imgs.setdefault("rank_pages", 2)
            imgs.setdefault("rank_tag_pages", 22)
            imgs.setdefault("rank_cache_seconds", 21600)
            imgs.setdefault("pixiv_cookie", "")
            imgs.setdefault("pixiv_host", "https://www.pixiv.net")
            imgs.setdefault("users_gate", [10000, 5000, 1000])
            imgs.setdefault("month_only", True)
            imgs.setdefault("search_pages", 2)
            imgs.setdefault("min_bookmarks", 0)
            imgs.setdefault("send_all_pages", True)
            imgs.setdefault("forward_multi_page", True)
            imgs.setdefault("max_pages_per_illust", 20)

            self.config["_schema_version"] = 5
            if hasattr(self.config, "save_config"):
                self.config.save_config()
            logger.info("[恶魔bot] 配置已迁移到 v4（省钱默认值 + Pixiv 排行榜取图）")
        except Exception as e:
            logger.warning(f"[恶魔bot] 配置迁移失败（不影响运行）：{e}")

    def _load_json(self, path: Path, default):
        if path.exists():
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except Exception as e:
                logger.warning(f"[恶魔bot] 读取 {path} 失败，使用默认值：{e}")
        return default

    def _save_json(self, path: Path, data):
        try:
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(path)
        except Exception as e:
            logger.warning(f"[恶魔bot] 写入 {path} 失败：{e}")

    def _usage_day_key(self) -> str:
        offset = self._cfg("peak_hours", "utc_offset_hours", default=8)
        return time.strftime("%Y-%m-%d", time.gmtime(time.time() + offset * 3600))

    def _usage_reset_if_needed(self) -> dict:
        day = self._usage_day_key()
        if self._token_usage.get("day") != day:
            self._token_usage = {"day": day, "total": 0, "input": 0, "output": 0,
                                 "requests": 0, "aux_total": 0, "aux_requests": 0,
                                 "estimated": 0, "by_source": {}}
            self._save_json(self.token_usage_file, self._token_usage)
        return self._token_usage

    @staticmethod
    def _usage_int(obj, key: str) -> int:
        try:
            v = getattr(obj, key, None)
            if v is None and isinstance(obj, dict):
                v = obj.get(key)
            return max(0, int(v or 0))
        except Exception:
            return 0

    def _record_usage(self, usage=None, source: str = "chat"):
        data = self._usage_reset_if_needed()
        if usage is None:
            return
        inp = self._usage_int(usage, "prompt_tokens") or self._usage_int(usage, "input_tokens") or self._usage_int(usage, "input")
        out = self._usage_int(usage, "completion_tokens") or self._usage_int(usage, "output_tokens") or self._usage_int(usage, "output")
        total = self._usage_int(usage, "total_tokens") or self._usage_int(usage, "total") or inp + out
        if total <= 0:
            return
        data["input"] += inp; data["output"] += out; data["total"] += total; data["requests"] += 1
        data.setdefault("by_source", {})[source] = data.setdefault("by_source", {}).get(source, 0) + total
        if source.startswith("aux"):
            data["aux_total"] += total; data["aux_requests"] += 1
        self._save_json(self.token_usage_file, data)

    def _load_or_create_persona(self) -> str:
        if self.persona_file.exists():
            try:
                content = self.persona_file.read_text(encoding="utf-8").strip()
                # 修正旧模板曾经出现过的日期笔误，不覆盖主人后来写入的其它人格信息。
                content = content.replace("生日：8月124日", "生日：8月24日")
                return content
            except Exception as e:
                logger.warning(f"[恶魔bot] 读取人格档案失败：{e}")
        template = Path(__file__).resolve().parent / "persona_template.md"
        if template.exists():
            try:
                content = template.read_text(encoding="utf-8").strip()
                self.persona_file.write_text(content + "\n", encoding="utf-8")
                return content
            except Exception as e:
                logger.warning(f"[恶魔bot] 创建人格档案失败：{e}")
        return "# 焦糖｜长期自我档案\n\n## 身份\n- 名字：焦糖\n- 身份：QQ群聊天机器人\n"

    def _save_persona(self, content: str):
        content = (content or "").strip() + "\n"
        tmp = self.persona_file.with_suffix(".md.tmp")
        try:
            tmp.write_text(content, encoding="utf-8"); tmp.replace(self.persona_file); self._persona=content.strip()
        except Exception as e:
            logger.warning(f"[恶魔bot] 保存人格档案失败：{e}")

    def _persona_fragment(self, text: str = "") -> str:
        """把长期人格档案以“相关优先”的方式注入当前请求。

        关键点：
        1. persona.md 是全局文件，不区分群聊/私聊。
        2. 普通聊天只注入一张很短的“身份卡”，减少 token。
        3. 如果用户问到生日/身高/MBTI/主人等人格字段，则优先抽取对应行，
           保证刚刚通过 /记住信息 写入的内容能被下一条消息立即看到。
        """
        if not self._cfg("persona", "inject", default=True):
            return ""

        raw = (self._persona or "").strip()
        if not raw:
            return ""

        cap = int(self._cfg("persona", "max_inject_chars", default=700) or 700)
        cap = max(250, min(cap, 900))
        text_l = (text or "").lower()

        # 常见的“自我查询”触发词；命中时允许注入更多档案字段。
        detail_words = (
            "生日", "几岁", "年龄", "身高", "体重", "三围", "鞋码", "血型",
            "星座", "mbti", "性别", "住哪", "城市", "职业", "名字", "叫什么",
            "是谁", "你是谁", "你叫什么", "你的设定", "人格", "自我介绍",
            "主人", "生日是哪天", "哪天生日"
        )
        detailed = any(w in text_l for w in detail_words)

        lines = [x.strip() for x in raw.splitlines() if x.strip()]
        # 优先保留最近新增的主人补充内容；然后寻找与当前问题相关的字段。
        supplement = []
        relevant = []
        identity = []

        for line in lines:
            norm = line.lstrip("-*• ").strip()
            low = norm.lower()
            if "主人补充的长期信息" in low:
                continue
            if "主人补充" in low:
                continue

            if any(k in low for k in ("名字：", "名字:", "角色：", "角色:", "主人：", "主人:",
                                      "主人 qq", "主人qq", "设定年龄", "生日：", "生日:",
                                      "性别设定", "职业设定")):
                identity.append(norm)

            if any(k.lower() in low for k in detail_words):
                relevant.append(norm)

        # 只抽取最近一段“主人补充”内容，避免旧聊天日志/大段人格正文挤占输入。
        for i, line in enumerate(lines):
            if "主人补充的长期信息" in line:
                for x in lines[i + 1:]:
                    if x.startswith("## ") and x != line:
                        break
                    if x.startswith("-"):
                        supplement.append(x.lstrip("- ").strip())

        # 去重并保持顺序。
        def uniq(seq):
            out = []
            seen = set()
            for x in seq:
                if x and x not in seen:
                    seen.add(x)
                    out.append(x)
            return out

        identity = uniq(identity)
        relevant = uniq(relevant)
        supplement = uniq(supplement)

        selected = []
        if detailed:
            selected.extend(relevant)
            selected.extend(identity)
            selected.extend(supplement[-8:])
        else:
            # 普通聊天只给最少量的身份连续性信息。
            selected.extend(identity[:6])
            selected.extend(supplement[-3:])

        # 如果字段提取为空，至少让模型知道自己叫焦糖。
        if not selected:
            selected = ["名字：焦糖", "角色：主人的私人小助手"]

        selected = uniq(selected)

        out = []
        n = 0
        for line in selected:
            piece = f"- {line}"
            if n + len(piece) + 1 > cap:
                break
            out.append(piece)
            n += len(piece) + 1

        if not out:
            return ""

        mode = "相关人格档案" if detailed else "长期身份卡"
        return f"\n\n[焦糖{mode}]\n" + "\n".join(out) + \
            "\n[上述内容是焦糖自己的长期资料；与普通聊天历史无关，优先相信它。]"

    def _is_sleep_window(self) -> bool:
        if not self._cfg("sleep_mode", "enabled", default=True):
            return False
        def mins(s, default):
            try:
                h,m=str(s).split(":",1); return int(h)*60+int(m)
            except Exception: return default
        start=mins(self._cfg("sleep_mode","start",default="23:59"),1439)
        end=mins(self._cfg("sleep_mode","end",default="07:30"),450)
        now=self._bj_minutes()
        return now >= start or now < end if start > end else start <= now < end

    def _sleep_remaining(self) -> str:
        if not self._is_sleep_window(): return "未休眠"
        end_s=self._cfg("sleep_mode","end",default="07:30")
        try:
            h,m=map(int,str(end_s).split(":",1)); end=h*60+m
        except Exception: end=450
        now=self._bj_minutes(); left=(end-now)%1440
        return f"{left//60}小时{left%60}分"

    def _is_recharge_notice(self, text: str) -> bool:
        t=(text or "").lower().replace(" ","")
        return bool(re.search(r"(充值token|充token|token充好了|token到账|补token|续token|已经充值token)", t))

    def _owner_recharge_reply(self) -> str:
        lines=getattr(responses,"OWNER_RECHARGE_THANKS",[]) if responses else []
        return random.choice(lines) if lines else "收到啦主人，token续上了，谢谢主人🥹"

    async def _recall_replied_message(self,event:AstrMessageEvent)->str:
        target=None; sender=None
        for seg in list(getattr(event.message_obj,"message",[]) or []):
            if isinstance(seg,Comp.Reply):
                target=getattr(seg,"message_id",None) or getattr(seg,"id",None); sender=getattr(seg,"sender_id",None); break
        if not target: return "没有检测到你引用的消息"
        self_id=str(getattr(event.message_obj,"self_id","") or "")
        if sender is not None and self_id and str(sender)!=self_id: return "你引用的不是我的消息"
        try:
            await event.bot.api.call_action("delete_msg", message_id=int(target)); return "撤回好了"
        except Exception as e:
            logger.warning(f"[恶魔bot] 撤回消息失败：{e}")
            return "撤回失败，可能已经过了 QQ 的可撤回时间"

    def _poke_event_field(self, event: AstrMessageEvent, name: str, default=None):
        msg = getattr(event, "message_obj", None)
        if msg is None:
            return default
        value = getattr(msg, name, None)
        if value is not None:
            return value
        if isinstance(msg, dict):
            return msg.get(name, default)
        raw = getattr(msg, "raw_message", None)
        if isinstance(raw, dict):
            return raw.get(name, default)
        return default

    def _poke_target_id(self, event: AstrMessageEvent) -> str:
        target = self._poke_event_field(event, "target_id", None)
        if target is not None:
            return str(target)
        # 某些适配器把 notice 数据塞在 raw_event/data 里。
        for obj in (getattr(event, "raw_event", None), getattr(event, "message_obj", None)):
            if isinstance(obj, dict):
                for container in (obj, obj.get("data", {})):
                    if isinstance(container, dict) and container.get("target_id") is not None:
                        return str(container.get("target_id"))
        return ""

    def _poke_self_id(self, event: AstrMessageEvent) -> str:
        sid = self._poke_event_field(event, "self_id", None)
        if sid is None:
            sid = getattr(event, "self_id", None)
        if sid is None:
            raw = getattr(event, "raw_event", None)
            if isinstance(raw, dict):
                sid = raw.get("self_id")
        if sid is None:
            try:
                sid = event.bot.self_id
            except Exception:
                sid = ""
        return str(sid or "")

    def _poke_text(self,event:AstrMessageEvent,group_id:str)->str:
        nick=self._clean_nick(event.get_sender_name()) or "你"
        if self._event_is_owner(event):
            lines=getattr(responses,"POKE_OWNER",[]) if responses else []
            return random.choice(lines) if lines else "主人戳我干嘛，我在这儿呢。"
        rec=((self._members.get(group_id) or {}).get("members") or {}).get(str(event.get_sender_id()),{})
        count=int(rec.get("count",0))
        if count>=100: profile="你都戳我这么多次了，手不累吗"
        elif count>=20: profile="你今天已经来找我很多次啦"
        elif count<=2: profile="刚认识就来戳我呀"
        else: profile="你又来戳我啦"
        lines=getattr(responses,"POKE_LINES",[]) if responses else []
        text = random.choice(lines).format(nick=nick,profile=profile,count=count) if lines else f"{profile}。"
        # 不再在群里显示 QQ 号/内部编号，直接用昵称即可。
        return text.replace("{id}", "").replace("QQ", "")

    def _cfg(self, *keys, default=None):
        """从插件配置里按路径取值，取不到就返回 default。"""
        node = self.config
        for k in keys:
            if not isinstance(node, dict) or k not in node:
                return default
            node = node[k]
        return node

    @staticmethod
    def _normalize_text(text: str) -> str:
        if not text:
            return ""
        return _NORMALIZE_RE.sub("", text).strip().lower()

    @staticmethod
    def _clean_nick(name: str) -> str:
        """洗掉 QQ 昵称里的控制字符和多余空白，否则会污染 prompt 和聊天记录。"""
        if not name:
            return ""
        name = _CTRL_RE.sub("", name)
        return re.sub(r"\s+", " ", name).strip()

    def _group_allowed(self, group_id: str) -> bool:
        if not self.group_whitelist:
            return True
        return group_id in self.group_whitelist

    # ==================== 主人身份（跨群识别） ====================
    #
    # 「管理员」是权限概念，「主人」是关系概念。这里把两者绑在一起：
    # 配置里填过的 QQ 号自动获得管理员权限，同时在提示词里被标成「你主人」。
    # 除了 QQ 号，还支持按昵称/ID 认人——你在不同群里改了群名片，
    # 只要昵称里含有配置的关键词（比如「遂意」），照样认得出来。

    def _bot_self_name(self) -> str:
        names = self._cfg("reply_style", "self_names", default=None) or []
        for n in names:
            if str(n).strip():
                return str(n).strip()
        return "恶魔bot"

    def _is_owner(self, sender_id: str = "", nickname: str = "") -> bool:
        if str(sender_id) and str(sender_id) in self.owner_ids:
            return True
        if not self.owner_names:
            return False
        nick = self._clean_nick(nickname or "").lower()
        if not nick:
            return False
        return any(name and name in nick for name in self.owner_names)

    def _event_is_owner(self, event: AstrMessageEvent) -> bool:
        try:
            return self._is_owner(str(event.get_sender_id()), event.get_sender_name() or "")
        except Exception:  # noqa: BLE001
            return False

    def _owner_title(self) -> str:
        return str(self._cfg("owner", "title", default="主人") or "主人")

    def _primary_owner_qq(self) -> str:
        for q in sorted(self.owner_ids):
            return q
        for q in sorted(self.admin_ids):
            return q
        return ""

    # ==================== 生活状态（合并自 dynamic_life_state） ====================

    def _life_day_key(self) -> str:
        offset = self._cfg("peak_hours", "utc_offset_hours", default=8)
        return time.strftime("%Y-%m-%d", time.gmtime(time.time() + offset * 3600))

    def _life_is_weekend(self) -> bool:
        offset = self._cfg("peak_hours", "utc_offset_hours", default=8)
        return time.gmtime(time.time() + offset * 3600).tm_wday >= 5

    def _life_now(self) -> dict | None:
        """当前生活状态。模块没加载或功能关掉时返回 None。"""
        if self._life is None or not self._cfg("life", "enabled", default=True):
            return None
        try:
            self._life.ensure(self._life_day_key(), self._life_is_weekend())
            return self._life.current(self._bj_minutes())
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[恶魔bot] 取生活状态失败：{e}")
            return None

    def _life_is_sleeping(self) -> bool:
        st = self._life_now()
        return bool(st and st.get("sleeping"))

    # ==================== 表情包 ====================

    def _sticker_dir(self) -> Path:
        """表情包最终存放/扫描的目录（zip 也是解压到这里之后再扫描）。"""
        raw = str(self._cfg("stickers", "folder", default="") or "").strip()
        if raw:
            return Path(raw).expanduser()
        return self.data_dir / "stickers"

    def _sticker_zip_path(self) -> Path | None:
        """
        表情包压缩包的位置。
        优先用配置里手动指定的 stickers.zip_path；
        没配的话，默认去插件代码目录下找一个叫 stickers.zip 的文件
        ——这样 git 仓库里只要提交这一个 zip 文件，不用传几百个散图，
        也不会撞到 GitHub 网页端一次最多 100 个文件的限制。
        """
        raw = str(self._cfg("stickers", "zip_path", default="") or "").strip()
        if raw:
            p = Path(raw).expanduser()
        else:
            p = Path(__file__).resolve().parent / "stickers.zip"
        return p if p.is_file() else None

    def _extract_sticker_zip(self, zip_path: Path, target_dir: Path) -> int:
        """
        把 zip 里的图片解压进 target_dir。只解压支持的图片格式，
        跳过 zip 里的其它杂项文件（README、.DS_Store 之类）；
        对每个成员路径做越界检查，防止畸形 zip 塞 ../../ 跑出目标目录。
        不会删除 target_dir 里已经存在、zip 里没有的文件，
        所以手动往解压目录里加几张图也不会被清掉。
        """
        n = 0
        with zipfile.ZipFile(zip_path) as zf:
            for info in zf.infolist():
                if info.is_dir():
                    continue
                name = info.filename
                if Path(name).suffix.lower() not in stickers.SUPPORTED_EXT:
                    continue
                dest = (target_dir / name).resolve()
                if target_dir.resolve() not in dest.parents and dest != target_dir.resolve():
                    logger.warning(f"[恶魔bot] 跳过 zip 里的可疑路径：{name}")
                    continue
                dest.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(info) as src, open(dest, "wb") as out:
                    out.write(src.read())
                n += 1
        return n

    def _reload_stickers(self) -> int:
        if stickers is None:
            return 0
        try:
            folder = self._sticker_dir()
            folder.mkdir(parents=True, exist_ok=True)

            zip_path = self._sticker_zip_path()
            if zip_path is not None:
                try:
                    extracted = self._extract_sticker_zip(zip_path, folder)
                    logger.info(f"[恶魔bot] 从 {zip_path.name} 解压了 {extracted} 张表情到 {folder}")
                except Exception as e:  # noqa: BLE001
                    logger.warning(f"[恶魔bot] 解压表情包 {zip_path} 失败：{e}")

            self._sticker_box = stickers.StickerBox(folder, logger=logger)
            n = self._sticker_box.count
            logger.info(f"[恶魔bot] 表情包目录 {folder}，索引到 {n} 张")
            return n
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[恶魔bot] 加载表情包失败：{e}")
            return 0

    def _sticker_chain(self, emotion: str):
        """按情绪取一张表情，返回消息链或 None。"""
        if not self._sticker_box or not self._sticker_box.count:
            return None
        hit = self._sticker_box.pick_by_emotion(emotion)
        if not hit:
            return None
        name, path = hit
        try:
            return [Comp.Image.fromFileSystem(str(path))], name
        except Exception as e:  # noqa: BLE001
            logger.debug(f"[恶魔bot] 构造表情消息失败：{e}")
            return None

    async def _send_sticker(
        self, event: AstrMessageEvent, group_id: str, emotion: str, force: bool = False
    ) -> bool:
        """发一张表情包（一次只发一张）。受概率/冷却/每日上限控制。"""
        if stickers is None or not self._cfg("stickers", "enabled", default=True):
            return False
        if not emotion:
            return False
        key = f"{group_id}:{event.get_sender_id()}"
        if self._sticker_gate and not self._sticker_gate.allow(
            key,
            chance=self._cfg("stickers", "chance", default=0.9),
            cooldown=self._cfg("stickers", "cooldown_seconds", default=180),
            daily_limit=self._cfg("stickers", "daily_limit", default=40),
            force=force,
        ):
            return False
        made = self._sticker_chain(emotion)
        if not made:
            return False
        chain, name = made
        ok = await self._safe_send(event, event.chain_result(chain), group_id)
        if ok:
            logger.info(f"[恶魔bot] 发表情包：情绪={emotion} 表情={name}")
        return ok

    # ==================== 出错求赞助 ====================

    _ERROR_PATTERNS = re.compile(
        r"(LLM\s*响应错误|All chat models failed|Insufficient Balance|余额不足|"
        r"invalid_request_error|APIConnectionError|Connection error|"
        r"请求失败|调用失败|指令执行出错|rate.?limit|402|401)",
        re.I,
    )

    def _looks_like_error(self, text: str) -> bool:
        return bool(text) and bool(self._ERROR_PATTERNS.search(text))

    async def _sponsor_alert(
        self, event: AstrMessageEvent, group_id: str, reason: str = "", quiet: bool = False
    ) -> bool:
        """
        出问题时 @主人 讨赞助。

        触发源有三个：LLM 报错（余额不足是最常见的）、指令抛异常、取图彻底失败。
        带冷却，避免接口一直挂的时候把群刷成复读机。
        """
        if not self._cfg("sponsor", "enabled", default=True):
            return False
        now = time.time()
        cd = self._cfg("sponsor", "cooldown_seconds", default=600)
        if now - self._last_sponsor_at < cd:
            return False
        self._last_sponsor_at = now

        templates = self._cfg("sponsor", "lines", default=None) or [
            "出bug了，给我主人充值点token求求啦",
            "我又崩了，主人快来充点token吧求求了",
            "脑子转不动了……主人，充值链接就在你手机里",
        ]
        line = random.choice([str(x) for x in templates if str(x).strip()])
        chain = []
        owner_qq = self._primary_owner_qq()
        if owner_qq and event.get_group_id():
            try:
                chain.append(Comp.At(qq=int(owner_qq) if owner_qq.isdigit() else owner_qq))
                chain.append(Comp.Plain(" "))
            except Exception:  # noqa: BLE001
                pass
        chain.append(Comp.Plain(line))
        if reason and self._cfg("sponsor", "show_reason", default=False):
            chain.append(Comp.Plain(f"（{reason[:40]}）"))
        sent = await self._safe_send(event, event.chain_result(chain), group_id)
        if sent and not quiet:
            emo = self._cfg("sponsor", "sticker_emotion", default="要钱") or "要钱"
            await self._send_sticker(event, group_id, emo, force=True)
        logger.warning(f"[恶魔bot] 触发求赞助提醒，原因：{reason[:80]}")
        return sent

    def _is_admin(self, sender_id: str) -> bool:
        return str(sender_id) in self.admin_ids

    async def _safe_send(self, event: AstrMessageEvent, text_or_chain, group_id: str = ""):
        """统一发送出口：捕获 QQ 平台禁言(1200)，进入退避而不是反复撞墙。"""
        if isinstance(text_or_chain, str):
            payload = event.chain_result([Comp.Plain(text_or_chain)])
        else:
            payload = text_or_chain
        try:
            await event.send(payload)
            return True
        except Exception as e:
            msg = str(e)
            if "1200" in msg or "禁言" in msg:
                minutes = self._cfg("mute", "platform_mute_backoff_minutes", default=30)
                gid = group_id or (event.get_group_id() or "unknown")
                self._platform_muted_until[gid] = time.time() + minutes * 60
                logger.warning(
                    f"[恶魔bot] 群 {gid} 里 bot 被QQ平台禁言，暂停发言 {minutes} 分钟"
                    f"（管理员可用 /解禁 强制清除）"
                )
            else:
                logger.warning(f"[恶魔bot] 发送失败：{e}")
            return False

    # ==================== LLM 调用小助手（供联网/风格模块用） ====================

    def _pick_provider(self, event: AstrMessageEvent | None = None, prefer_gate: bool = True):
        provider_id = self._cfg("reply_gate", "gate_provider_id", default="") if prefer_gate else ""
        try:
            if provider_id:
                prov = self.context.get_provider_by_id(provider_id)
                if prov:
                    return prov
            if event is not None:
                return self.context.get_using_provider(umo=event.unified_msg_origin)
            return self.context.get_using_provider()
        except Exception as e:
            logger.debug(f"[恶魔bot] 取 provider 失败：{e}")
            return None

    async def _llm_text(
        self,
        prompt: str,
        system_prompt: str = "",
        event: AstrMessageEvent | None = None,
        timeout: float = 25.0,
        force: bool = False,
    ) -> str:
        """直接调 provider，不走事件管线，所以不会触发人格注入、也不会递归。

        force=False 的都是「后台自娱自乐」的请求（学风格、查梗、判断要不要插话）。
        这类请求用户根本看不见，却在账单里占了很大一块，
        所以高峰时段和当日预算耗尽时一律不发出去；
        用户手打 /梗、/学风格 这种主动触发的才 force=True。
        """
        if not force and not self._aux_llm_allowed():
            self._saver_stats["aux_blocked"] += 1
            return ""
        prov = self._pick_provider(event)
        if not prov:
            return ""
        self._count_aux_call()
        try:
            resp = await asyncio.wait_for(
                prov.text_chat(prompt=prompt, system_prompt=system_prompt), timeout=timeout
            )
            self._record_usage(getattr(resp, "usage", None), source="aux")
            return (resp.completion_text or "").strip()
        except Exception as e:
            logger.debug(f"[恶魔bot] 辅助LLM调用失败：{type(e).__name__}: {e}")
            return ""

    # ==================== 群友编号库 ====================

    def _group_book(self, group_id: str) -> dict:
        book = self._members.setdefault(group_id, {"seq": 0, "members": {}})
        book.setdefault("seq", 0)
        book.setdefault("members", {})
        return book

    def register_member(self, group_id: str, sender_id: str, nickname: str) -> dict:
        """每条消息都过一遍：新人自动编号，老人更新昵称/发言量/最后出现时间。"""
        if not self._cfg("members", "enabled", default=True):
            return {}
        book = self._group_book(group_id)
        members = book["members"]
        sid = str(sender_id)
        nick = self._clean_nick(nickname)

        rec = members.get(sid)
        if rec is None:
            book["seq"] += 1
            rec = {
                "no": book["seq"],
                "code": f"M{book['seq']:02d}",
                "qq": sid,
                "names": [nick] if nick else [],
                "first_seen": int(time.time()),
                "last_seen": int(time.time()),
                "count": 0,
                "notes": [],
            }
            members[sid] = rec
            logger.info(f"[恶魔bot] 群 {group_id} 新登记群友 {rec['code']} {nick}({sid})")
        else:
            rec.setdefault("names", [])
            rec.setdefault("notes", [])
            if nick and nick not in rec["names"]:
                rec["names"].append(nick)
                max_alias = self._cfg("members", "max_alias_per_member", default=5)
                if len(rec["names"]) > max_alias:
                    del rec["names"][: len(rec["names"]) - max_alias]

        rec["last_seen"] = int(time.time())
        rec["count"] = int(rec.get("count", 0)) + 1
        rec["is_admin"] = self._is_admin(sid)
        return rec

    def member_display(self, group_id: str, rec: dict) -> str:
        names = "/".join(rec.get("names", [])[-2:]) or rec.get("qq", "")
        tag = "，群主人（就是你的老熟人，说话可以随意点）" if rec.get("is_admin") else ""
        notes = rec.get("notes", [])
        note_txt = f"，备注：{'；'.join(notes[-2:])}" if notes else ""
        return f"{rec.get('code','M??')} {names}(QQ{rec.get('qq','')}){tag}{note_txt}"

    def member_roster(self, group_id: str, limit: int = 12) -> str:
        book = self._members.get(group_id) or {}
        members = list((book.get("members") or {}).values())
        if not members:
            return ""
        members.sort(key=lambda r: r.get("last_seen", 0), reverse=True)
        lines = [self.member_display(group_id, r) for r in members[:limit]]
        return "\n".join(lines)

    def find_member(self, group_id: str, keyword: str) -> dict | None:
        book = self._members.get(group_id) or {}
        kw = (keyword or "").strip().lower()
        if not kw:
            return None
        for rec in (book.get("members") or {}).values():
            if kw == str(rec.get("qq", "")).lower() or kw == str(rec.get("code", "")).lower():
                return rec
        for rec in (book.get("members") or {}).values():
            if any(kw in (n or "").lower() for n in rec.get("names", [])):
                return rec
        return None

    def note_member(self, group_id: str, keyword: str, note: str) -> str:
        rec = self.find_member(group_id, keyword)
        if not rec:
            return f"群里没找到「{keyword}」这个人。"
        rec.setdefault("notes", []).append(self._clean_nick(note)[:60])
        max_notes = self._cfg("members", "max_notes_per_member", default=6)
        if len(rec["notes"]) > max_notes:
            del rec["notes"][: len(rec["notes"]) - max_notes]
        self._save_json(self.members_file, self._members)
        return f"已经记住了关于 {rec.get('code')} 的事。"

    # ==================== 联网学梗 / 词库 ====================

    def _slang_cache(self) -> dict:
        return self._knowledge.setdefault("slang", {})

    def slang_get(self, term: str) -> dict | None:
        return self._slang_cache().get((term or "").strip().lower())

    def slang_put(self, term: str, desc: str, source: str = "web"):
        key = (term or "").strip().lower()
        if not key or not desc:
            return
        cache = self._slang_cache()
        cache[key] = {
            "term": term.strip(),
            "desc": desc.strip()[:120],
            "source": source,
            "time": int(time.time()),
            "hits": int((cache.get(key) or {}).get("hits", 0)) + 1,
        }
        max_n = self._cfg("knowledge", "max_slang_entries", default=800)
        if len(cache) > max_n:
            oldest = sorted(cache.items(), key=lambda kv: kv[1].get("time", 0))
            for k, _ in oldest[: len(cache) - max_n]:
                cache.pop(k, None)
        self._save_json(self.knowledge_file, self._knowledge)

    def _candidate_terms(self, text: str) -> list[str]:
        """从消息里挑出"可能是新梗"的词：数字梗、字母缩写、被引号圈起来的词。

        重点是宁缺毋滥——每个候选词都要花一次联网+一次LLM归纳，
        误抓「2026」「200块」这种普通数字纯属浪费时间和额度。
        """
        if not text:
            return []
        out: list[str] = []
        stripped = text.strip()

        for m in _NUM_TERM_RE.finditer(text):
            num = m.group(1)
            after = text[m.end(): m.end() + 1]
            before = text[max(0, m.start() - 1): m.start()]
            # 后面跟单位/量词的是正常数字，不是梗
            if after and after in "年月日号点分秒元块钱岁次条个人天位%℃kgmKMG时刻半":
                continue
            if before and before in "¥$第转共有满约":
                continue
            # 四位数的年份
            if len(num) == 4 and 1900 <= int(num) <= 2100:
                continue
            # 梗的特征：够长且字符种类少（676767、7777），或者整条消息就是这串数字（114514）
            if (len(num) >= 4 and len(set(num)) <= 3) or (stripped == num and len(num) >= 3):
                out.append(num)

        for m in _ALPHA_TERM_RE.finditer(text):
            word = m.group(1).lower()
            if word not in _ALPHA_STOPWORDS:
                out.append(word)

        # 被引号圈起来的词，通常就是发言人自己都觉得需要解释的新词
        for m in re.finditer(r"[「『\"\u201c]([^」』\"\u201d]{2,12})[」』\"\u201d]", text):
            out.append(m.group(1).strip())

        # 去重，并丢掉是别人子串的短候选（city 之于 city不city）
        seen, uniq = set(), []
        for t in out:
            if t in seen:
                continue
            seen.add(t)
            uniq.append(t)
        uniq = [t for t in uniq if not any(t != o and t in o for o in uniq)]
        return uniq[: self._cfg("knowledge", "max_lookups_per_message", default=2)]

    async def lookup_slang(
        self, term: str, event: AstrMessageEvent | None = None, force: bool = False
    ) -> str:
        """查一个词的意思：先查本地词库，没有就上网搜 + LLM 归纳一句话，然后入库。"""
        term = (term or "").strip()
        if not term:
            return ""
        if websearch is None or not self._cfg("knowledge", "enabled", default=True):
            return ""

        def _fresh(entry) -> bool:
            """命中缓存算不算还新鲜。"查不到"这种失败结果用短得多的重试间隔，
            否则一次网络抖动会让这个词 60 天都不再查。"""
            if not entry:
                return False
            age = time.time() - entry.get("time", 0)
            if entry.get("source") == "miss":
                return age < self._cfg("knowledge", "miss_retry_hours", default=12) * 3600
            if entry.get("source") in ("manual", "group"):
                return True  # 你或群友亲口教的，永久有效，不要被联网结果覆盖
            return age < self._cfg("knowledge", "refresh_days", default=60) * 86400

        cached = self.slang_get(term)
        if _fresh(cached):
            return cached.get("desc", "") if cached.get("source") != "miss" else ""

        lock = self._slang_locks.setdefault(term.lower(), asyncio.Lock())
        async with lock:
            cached = self.slang_get(term)
            if _fresh(cached):
                return cached.get("desc", "") if cached.get("source") != "miss" else ""

            query_tpl = self._cfg(
                "knowledge", "slang_query_template", default="{term} 是什么意思 网络用语 梗"
            )
            results = await websearch.search(
                query_tpl.format(term=term),
                backends=self._cfg(
                    "knowledge", "backends",
                    default=["bing_rss", "baidu_baike", "jikipedia", "duckduckgo"],
                ),
                limit=self._cfg("knowledge", "results_per_query", default=4),
                timeout=self._cfg("knowledge", "http_timeout_seconds", default=8),
                api_keys={
                    "bocha": self._cfg("knowledge", "bocha_api_key", default=""),
                    "tavily": self._cfg("knowledge", "tavily_api_key", default=""),
                },
                logger=logger,
            )
            if not results:
                # 记一条"查过但没查到"，避免同一个词每次都触发联网
                self.slang_put(term, "查不到确切说法", source="miss")
                return ""

            snippet_text = "\n".join(
                f"- {r.get('title','')}：{r.get('snippet','')}" for r in results
            )[:2000]
            desc = await self._llm_text(
                force=force,
                prompt=(
                    f"搜索结果：\n{snippet_text}\n\n"
                    f"请根据上面的内容，用一句不超过35字的中文，解释「{term}」在网络聊天里的含义或用法。"
                    f"不要解释你怎么得出的，不要写多余的话。"
                    f"如果搜索结果里看不出这个词有特殊含义，只输出：无特殊含义"
                ),
                system_prompt="你是网络流行语速查助手，只输出一句极简的释义，不要任何前缀和标点堆砌。",
                event=event,
                timeout=self._cfg("knowledge", "summarize_timeout_seconds", default=20),
            )
            desc = self._strip_markdown(desc).strip()
            if not desc:
                return ""
            if "无特殊含义" in desc:
                self.slang_put(term, "无特殊含义", source="web")
                return ""
            self.slang_put(term, desc, source="web")
            logger.info(f"[恶魔bot] 学到新词：{term} = {desc}")
            return desc

    async def search_web_raw(self, query: str) -> list[dict]:
        if websearch is None:
            return []
        return await websearch.search(
            query,
            backends=self._cfg(
                "knowledge", "backends",
                default=["bing_rss", "baidu_baike", "jikipedia", "duckduckgo"],
            ),
            limit=self._cfg("knowledge", "results_per_query", default=4),
            timeout=self._cfg("knowledge", "http_timeout_seconds", default=8),
            api_keys={
                "bocha": self._cfg("knowledge", "bocha_api_key", default=""),
                "tavily": self._cfg("knowledge", "tavily_api_key", default=""),
            },
            logger=logger,
        )

    async def refresh_hot_topics(self, force: bool = False) -> list[str]:
        if websearch is None or not self._cfg("knowledge", "hot_topics_enabled", default=True):
            return []
        hours = self._cfg("knowledge", "hot_refresh_hours", default=6)
        hot = self._knowledge.setdefault("hot", {"time": 0, "items": []})
        if not force and time.time() - hot.get("time", 0) < hours * 3600:
            return hot.get("items", [])
        if not force and self.in_peak_hours():
            return hot.get("items", [])
        if self._hot_lock.locked():
            return hot.get("items", [])
        async with self._hot_lock:
            items = await websearch.hot_topics(
                limit=self._cfg("knowledge", "hot_topics_count", default=8),
                timeout=self._cfg("knowledge", "http_timeout_seconds", default=8),
                logger=logger,
            )
            if items:
                hot["items"] = items
                hot["time"] = int(time.time())
                self._save_json(self.knowledge_file, self._knowledge)
                logger.info(f"[恶魔bot] 热点已更新，{len(items)} 条")
            return hot.get("items", [])

    # ==================== 说话风格自学习 ====================

    def _style_samples(self, group_id: str) -> tuple[str, str]:
        """取管理员样本和全群样本，样本就是真实聊天记录本身。"""
        n = self._cfg("style", "sample_size", default=60)
        bucket = self._history.get(group_id, [])[-400:]
        admin_names = set()
        book = self._members.get(group_id) or {}
        for rec in (book.get("members") or {}).values():
            if rec.get("is_admin"):
                admin_names.update(rec.get("names", []))

        admin_lines, other_lines = [], []
        for m in bucket:
            text = (m.get("text") or "").strip()
            if not text or text.startswith("[") or len(text) > 80:
                continue
            if m.get("sender") in admin_names:
                admin_lines.append(text)
            else:
                other_lines.append(f"{m.get('sender','')}: {text}")
        return "\n".join(admin_lines[-n:]), "\n".join(other_lines[-n:])

    async def update_style(
        self, group_id: str, event: AstrMessageEvent | None = None, force: bool = False
    ) -> str:
        if not self._cfg("style", "enabled", default=True):
            return ""
        if self._style_lock.locked():
            return self._style.get("profile", "")
        async with self._style_lock:
            admin_txt, other_txt = self._style_samples(group_id)
            if len((admin_txt + other_txt).strip()) < self._cfg("style", "min_chars", default=120):
                return self._style.get("profile", "")

            profile = await self._llm_text(
                force=force,
                prompt=(
                    f"【群主人的发言样本】\n{admin_txt or '（暂无）'}\n\n"
                    f"【其他群友的发言样本】\n{other_txt or '（暂无）'}\n\n"
                    "请归纳出这个群真实的打字习惯，重点参考群主人。输出一份不超过180字的"
                    "「说话风格速记」，必须包含：常出现的口头禅或语气词、平均句子长短、"
                    "标点使用习惯（爱不爱打句号、爱不爱用问号叹号）、常用的梗或称呼、"
                    "以及回话时惯常的态度（比如爱怼人还是爱附和）。"
                    "直接输出速记正文，不要标题、不要列表符号、不要解释。"
                ),
                system_prompt="你是语言风格分析师，只输出简洁的风格速记正文。",
                event=event,
                timeout=self._cfg("style", "timeout_seconds", default=30),
            )
            profile = self._strip_markdown(profile).strip()
            if not profile:
                return self._style.get("profile", "")
            self._style = {
                "profile": profile[:400],
                "updated": int(time.time()),
                "group": group_id,
            }
            self._save_json(self.style_file, self._style)
            logger.info(f"[恶魔bot] 说话风格已更新：{profile[:40]}...")
            return self._style["profile"]

    def _style_due(self) -> bool:
        if not self._cfg("style", "enabled", default=True):
            return False
        # 学风格是一次几千字样本的大请求，高峰时段/预算耗尽时直接推迟
        if not self._aux_llm_allowed():
            return False
        hours = self._cfg("style", "update_interval_hours", default=12)
        return time.time() - self._style.get("updated", 0) > hours * 3600

    # ==================== 闭嘴 / 平台禁言 ====================

    def _check_mute_trigger(self, text: str) -> bool:
        if not self._cfg("mute", "enabled", default=True):
            return False
        keywords = self._cfg("mute", "trigger_keywords", default=["闭嘴", "shut up", "安静"])
        return any(kw and kw in text for kw in keywords)

    def _check_unmute_trigger(self, text: str) -> bool:
        keywords = self._cfg(
            "mute", "unmute_keywords", default=["说话", "复活", "解除闭嘴", "解禁", "出来"]
        )
        return any(kw and kw in text for kw in keywords)

    def _is_muted(self, group_id: str) -> bool:
        return time.time() < self._muted_until.get(group_id, 0)

    def _is_platform_muted(self, group_id: str) -> bool:
        return time.time() < self._platform_muted_until.get(group_id, 0)

    def force_unmute(self, group_id: str):
        """管理员专用：本地闭嘴 + 平台禁言退避 + 沉默期，一次全清。"""
        self._muted_until[group_id] = 0
        self._muted_by.pop(group_id, None)
        self._platform_muted_until[group_id] = 0
        self._quiet_until.pop(group_id, None)

    # ==================== 结束语识别 ====================

    def _is_end_phrase(self, text: str) -> bool:
        if not self._cfg("end_talk", "enabled", default=True):
            return False
        # 带问号说明对方还在问，不是收尾（"嗯？" 该回，"嗯嗯" 不该回）
        if any(ch in text for ch in "?？"):
            return False
        norm = self._normalize_text(text)
        if not norm:
            return False
        if len(norm) > self._cfg("end_talk", "max_length", default=5):
            return False
        # 配置留空时回落到内置列表（WebUI 里 list 默认值是空数组）
        phrases = self._cfg("end_talk", "phrases", default=None) or DEFAULT_END_PHRASES
        norm_phrases = {self._normalize_text(p) for p in phrases}
        if norm in norm_phrases:
            return True
        # "嗯嗯嗯嗯"、"哦哦哦"这类单字重复
        if len(set(norm)) == 1 and norm[0] in "嗯恩哦噢额好行":
            return True
        return False

    def _in_quiet_period(self, group_id: str) -> bool:
        return time.time() < self._quiet_until.get(group_id, 0)

    # ==================== 群聊发言频率检测 ====================

    def _group_msg_rate_per_minute(self, group_id: str) -> float:
        window = self._cfg("reply_gate", "rate_window_seconds", default=60)
        if window <= 0:
            return 0.0
        now = time.time()
        bucket = self._history.get(group_id, [])
        count = 0
        for m in reversed(bucket):
            if now - m.get("time", 0) > window:
                break
            count += 1
        return count * (60.0 / window)

    def _is_busy_group(self, group_id: str) -> bool:
        threshold = self._cfg("reply_gate", "busy_messages_per_minute", default=15)
        return self._group_msg_rate_per_minute(group_id) >= threshold

    def _busy_window_allows_reply(self, group_id: str) -> bool:
        window = self._cfg("reply_gate", "busy_window_seconds", default=300)
        max_replies = self._cfg("reply_gate", "busy_max_replies", default=2)
        now = time.time()
        times = self._auto_reply_times.setdefault(group_id, [])
        times[:] = [t for t in times if now - t <= window]
        return len(times) < max_replies

    def _record_auto_reply(self, group_id: str):
        self._auto_reply_times.setdefault(group_id, []).append(time.time())

    # ==================== 聊天记录 ====================

    def record_message(self, group_id: str, sender: str, text: str):
        if not self._cfg("chat_history", "enabled", default=True):
            return
        max_n = self._cfg("chat_history", "max_records_per_group", default=200)
        bucket = self._history.setdefault(group_id, [])
        bucket.append(
            {"time": int(time.time()), "sender": self._clean_nick(sender), "text": text}
        )
        if len(bucket) > max_n:
            del bucket[: len(bucket) - max_n]
        # 每条都落盘在手机上有点费 IO，改成按条数节流
        if len(bucket) % self._cfg("chat_history", "save_every_n", default=5) == 0:
            self._save_json(self.history_file, self._history)

    def _record_bot_message(self, group_id: str, text: str):
        """把 Bot 实际发出的每一段写入插件自己的聊天历史。"""
        if not text or not text.strip():
            return
        self.record_message(group_id, "焦糖", text.strip())

    def _like_today_state(self):
        day = time.strftime("%Y-%m-%d", time.localtime())
        d = self._like_usage if isinstance(self._like_usage, dict) else {}
        if d.get("day") != day or not isinstance(d.get("sent"), dict):
            self._like_usage = {"day": day, "sent": {}}
            self._save_json(self.like_usage_file, self._like_usage)
        return self._like_usage

    async def _is_friend(self, user_id: str, event: AstrMessageEvent | None = None) -> bool:
        bot = getattr(event, "bot", None) if event is not None else None
        bot = bot or self._last_bot
        try:
            if bot is None:
                return False
            api = getattr(bot, "api", bot)
            ret = await api.call_action("get_friend_list")
            data = ret.get("data", ret) if isinstance(ret, dict) else ret
            if not isinstance(data, list):
                return False
            target = str(user_id)
            return any(str(x.get("user_id")) == target for x in data if isinstance(x, dict))
        except Exception as e:
            logger.warning(f"[恶魔bot] 检查 QQ 好友失败：{type(e).__name__}: {e}")
            return False

    async def _send_like(self, event: AstrMessageEvent | None, user_id: str, times: int) -> bool:
        times = max(1, min(int(times), 10))
        try:
            bot = getattr(event, "bot", None) if event is not None else None
            if bot is None:
                # 尝试从当前上下文拿连接上的 bot；AstrBot 版本不同，这里只做兼容探测。
                bot = self._last_bot or getattr(self.context, "bot", None)
            if bot is None:
                return False
            api = getattr(bot, "api", bot)
            ret = await api.call_action("send_like", user_id=int(user_id), times=times)
            return not (isinstance(ret, dict) and ret.get("status") == "failed")
        except Exception as e:
            logger.warning(f"[恶魔bot] 给 QQ={user_id} 点赞失败：{type(e).__name__}: {e}")
            return False

    def _like_grant_items(self):
        users = self._like_grants.get("users", {}) if isinstance(self._like_grants, dict) else {}
        return users if isinstance(users, dict) else {}

    async def _cmd_like(self, event: AstrMessageEvent, arg: str) -> str:
        """
        /点赞                    主人：立即给自己 10 赞；并开启每日自动 10 赞。
        /点赞 同意5 QQ号       主人：允许某位 QQ 好友每日获得 5 赞。
        /点赞 同意10 QQ号      同上，10 赞。
        /点赞 撤销 QQ号        撤销自动点赞授权。
        /点赞 列表              查看当前授权。
        """
        if event.get_group_id():
            return "这个要私聊我发啦"
        sender_id = str(event.get_sender_id())
        if not self._event_is_owner(event):
            return "这个是主人的专属按钮"

        parts = (arg or "").strip().split()
        if not parts:
            target = sender_id
            ok = await self._send_like(event, target, 10)
            if not ok:
                return "刚刚点赞接口没接住，今天的赞还没发出去"
            state = self._like_today_state()
            state["sent"][target] = 10
            self._save_json(self.like_usage_file, state)
            return "好啦，今天的 10 个赞给你安排上了 👍"

        action = parts[0]
        if action.startswith("同意"):
            m = re.fullmatch(r"同意(\d{1,2})", action)
            if not m:
                return "用法：/点赞 同意5 2677518198"
            times = min(max(int(m.group(1)), 1), 10)
            if len(parts) < 2 or not parts[1].isdigit():
                return "还差一个 QQ 号，比如：/点赞 同意5 2677518198"
            target = str(int(parts[1]))
            if not await self._is_friend(target, event):
                return f"QQ {target} 目前不是我的好友，先加我好友再开这个授权"
            users = self._like_grant_items()
            users[target] = {"times": times, "granted_by": sender_id, "updated": int(time.time())}
            self._like_grants["users"] = users
            self._save_json(self.like_grants_file, self._like_grants)
            return f"好啦，QQ {target} 以后每天可以收到 {times} 个赞"

        if action == "撤销":
            if len(parts) < 2 or not parts[1].isdigit():
                return "用法：/点赞 撤销 2677518198"
            target = str(int(parts[1]))
            users = self._like_grant_items()
            if target not in users:
                return "这个 QQ 不在自动点赞名单里"
            users.pop(target, None)
            self._like_grants["users"] = users
            self._save_json(self.like_grants_file, self._like_grants)
            return f"已取消 QQ {target} 的每日点赞"

        if action == "列表":
            users = self._like_grant_items()
            if not users:
                return "目前没有额外的每日点赞授权"
            lines = ["每日点赞授权："]
            for uid, info in users.items():
                lines.append(f"QQ {uid}：每天 {int(info.get('times', 0))} 个")
            return "\n".join(lines)

        return "用法：/点赞 ｜ /点赞 同意5 2677518198 ｜ /点赞 撤销 2677518198 ｜ /点赞 列表"

    async def _daily_like_loop(self):
        """每天自动给主人和已授权好友点赞；每个用户每天最多 10 次。"""
        await asyncio.sleep(15)
        while True:
            try:
                await self._run_daily_likes_once()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(f"[恶魔bot] 自动点赞任务异常：{type(e).__name__}: {e}")
            await asyncio.sleep(max(60, int(self._cfg("like", "auto_run_interval_seconds", default=600))))

    async def _run_daily_likes_once(self):
        if not self._cfg("like", "daily_owner", default=True):
            return
        owner = self._primary_owner_qq()
        if not owner:
            return
        state = self._like_today_state()
        sent = state.setdefault("sent", {})
        targets = {owner: 10}
        for uid, info in self._like_grant_items().items():
            times = min(max(int((info or {}).get("times", 0)), 0), 10)
            if times:
                targets[str(uid)] = times
        for uid, times in targets.items():
            if int(sent.get(uid, 0)) >= times:
                continue
            # 授权用户必须是 bot 好友；主人自己也不强制检查好友状态，避免客户端配置异常时完全漏赞。
            if uid != owner and not await self._is_friend(uid):
                continue
            remaining = times - int(sent.get(uid, 0))
            if await self._send_like(None, uid, remaining):
                sent[uid] = times
                logger.info(f"[恶魔bot] 每日自动点赞成功：QQ={uid}，本日 {times} 个")
        self._save_json(self.like_usage_file, state)

    def query_history(self, group_id: str, keyword: str = "", limit: int = 10):
        bucket = self._history.get(group_id, [])
        if keyword:
            bucket = [m for m in bucket if keyword in m["text"]]
        return bucket[-limit:]

    # ==================== 长期记忆 ====================

    def remember(self, scope: str, content: str, tags: str = ""):
        max_n = self._cfg("memory", "max_memory_per_scope", default=300)
        bucket = self._memory.setdefault(scope, [])
        bucket.append({"time": int(time.time()), "content": content, "tags": tags})
        if len(bucket) > max_n:
            del bucket[: len(bucket) - max_n]
        self._save_json(self.memory_file, self._memory)

    def recall(self, scope: str, query: str = "", limit: int = 5):
        bucket = self._memory.get(scope, [])
        if query:
            bucket = [m for m in bucket if query in m["content"] or query in m.get("tags", "")]
        return bucket[-limit:]

    # ==================== 复读检测 ====================

    def _repeat_streak(self, group_id: str, current_text: str) -> int:
        norm = self._normalize_text(current_text)
        if not norm:
            return 0
        bucket = self._history.get(group_id, [])
        streak = 0
        for m in reversed(bucket[:-1]):
            if self._normalize_text(m.get("text", "")) == norm:
                streak += 1
            else:
                break
        return streak

    async def _try_join_repeat(self, event: AstrMessageEvent, group_id: str, text: str) -> bool:
        if not self._cfg("repeat", "enabled", default=True):
            return False
        if not self._group_allowed(group_id):
            return False

        norm = self._normalize_text(text)
        streak = self._repeat_streak(group_id, text)

        if streak == 0:
            # 这条消息没接上上一条，说明原来那条复读链（如果有的话）已经断了，
            # 清掉"已经跟过"的记录，允许之后新开的复读链重新跟一次
            self._repeat_joined.pop(group_id, None)
            return False

        min_streak = self._cfg("repeat", "min_streak", default=2)
        if streak < min_streak:
            return False

        # 同一条复读链只跟一次：这个规范化文本已经跟过了就不再跟，
        # 哪怕后面链条继续变长（比如 d 又发了一条一样的）
        if self._repeat_joined.get(group_id) == norm:
            return False

        now = time.time()
        cooldown = self._cfg("repeat", "cooldown_seconds", default=8)
        if now - self._last_repeat_at.get(group_id, 0) < cooldown:
            return False

        if random.random() >= self._cfg("repeat", "follow_chance", default=0.7):
            return False

        if not await self._safe_send(event, text, group_id):
            return False

        self._repeat_joined[group_id] = norm
        self._last_repeat_at[group_id] = now
        self._last_reply_at[group_id] = now
        return True

    # ==================== 核心：群消息拦截 ====================

    # ==================== 好友申请 ====================

    @filter.event_message_type(filter.EventMessageType.ALL, priority=1100)
    async def on_friend_request(self, event: AstrMessageEvent):
        """自动同意 OneBot v11 好友申请，只处理好友申请，不处理加群申请。"""
        if not self._cfg("friend_request", "auto_accept", default=True):
            return

        msg = getattr(event, "message_obj", None)
        if msg is None:
            return

        def field(name, default=None):
            value = getattr(msg, name, None)
            if value is not None:
                return value
            if isinstance(msg, dict):
                return msg.get(name, default)
            raw = getattr(msg, "raw_message", None)
            if isinstance(raw, dict):
                return raw.get(name, default)
            return default

        post_type = str(field("post_type", "") or "").lower()
        request_type = str(field("request_type", "") or "").lower()
        flag = field("flag")
        user_id = field("user_id")
        comment = str(field("comment", "") or "").strip()

        # OneBot v11 好友申请：post_type=request + request_type=friend。
        if post_type != "request" or request_type != "friend" or not flag or not user_id:
            return

        flag = str(flag)
        if flag in self._friend_request_seen:
            return
        self._friend_request_seen.add(flag)
        if len(self._friend_request_seen) > 500:
            self._friend_request_seen = set(list(self._friend_request_seen)[-250:])

        try:
            await event.bot.api.call_action(
                "set_friend_add_request",
                flag=flag,
                approve=True,
                remark="焦糖",
            )
            if self._cfg("friend_request", "log_requests", default=True):
                logger.info(
                    f"[恶魔bot] 自动同意好友申请：QQ={user_id}，验证信息={comment[:80] if comment else '（无）'}"
                )
        except Exception as e:
            logger.warning(
                f"[恶魔bot] 自动同意好友申请失败：QQ={user_id}，{type(e).__name__}: {e}"
            )

    # ==================== 戳一戳 ====================

    @filter.event_message_type(filter.EventMessageType.ALL, priority=1000)
    async def on_poke(self,event:AstrMessageEvent):
        # 这个处理器挂在 ALL 上时，绝对不能拦截普通消息。
        # 只有确认这是 notice/poke 且 target_id=机器人自己，才消费事件。
        raw_candidates = [
            getattr(event, "raw_event", None),
            getattr(getattr(event, "message_obj", None), "raw_message", None),
            getattr(event, "message_obj", None),
        ]
        is_poke = False
        for obj in raw_candidates:
            if isinstance(obj, dict):
                post_type = str(obj.get("post_type", "") or "").lower()
                notice_type = str(obj.get("notice_type", "") or "").lower()
                sub_type = str(obj.get("sub_type", "") or "").lower()
                if notice_type == "notify" and sub_type == "poke":
                    is_poke = True
                    break
                if post_type == "notice" and notice_type == "notify" and obj.get("target_id") is not None:
                    is_poke = True
                    break
                data = obj.get("data") if isinstance(obj.get("data"), dict) else {}
                if str(data.get("notice_type", "") or "").lower() == "notify" and str(data.get("sub_type", "") or "").lower() == "poke":
                    is_poke = True
                    break
        if not is_poke:
            # 不是戳一戳：严禁 stop_event，否则会吞掉所有普通聊天。
            return

        target_id = self._poke_target_id(event)
        self_id = self._poke_self_id(event)
        if not target_id or (self_id and target_id != self_id):
            # 是 poke，但戳的不是 Bot：只消费这条 poke 通知。
            event.stop_event()
            return
        if not self._cfg("poke","enabled",default=True) or self._is_sleep_window():
            event.stop_event(); return
        key=f"{event.get_group_id() or 'private'}:{event.get_sender_id()}"
        now=time.time(); cd=int(self._cfg("poke","cooldown_seconds",default=20))
        if now-self._last_poke_at.get(key,0)<cd:
            event.stop_event(); return
        self._last_poke_at[key]=now
        await self._safe_send(event,self._poke_text(event,event.get_group_id() or "private"),event.get_group_id() or "unknown")
        event.stop_event()

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE, priority=999)
    async def on_group_message(self, event: AstrMessageEvent):
        self._last_bot = getattr(event, "bot", None) or self._last_bot
        group_id = event.get_group_id() or "unknown"
        if group_id != "unknown":
            self._known_groups.add(str(group_id))
        sender_id = str(event.get_sender_id())
        if self._is_owner(sender_id, event.get_sender_name() or ""):
            self._last_owner_message_at = time.time()
        sender = self._clean_nick(event.get_sender_name()) or sender_id
        text = _CTRL_RE.sub("", event.message_str or "")
        if self._is_sleep_window():
            return
        music_hit = self._music_match(text)
        if music_hit and not text.startswith(("/", "==")):
            # 仅识别歌曲，不自动续写受版权保护的歌词；如用户自己提供了歌词数据库，music.py 可按其数据回填。
            yield event.plain_result(music_hit)
            event.stop_event()
            return

        self_id = getattr(event.message_obj, "self_id", None)
        is_at = any(
            isinstance(seg, Comp.At) and str(getattr(seg, "qq", "")) == str(self_id)
            for seg in event.message_obj.message
        )
        is_reply_to_bot = any(
            isinstance(seg, Comp.Reply) and str(getattr(seg, "sender_id", "")) == str(self_id)
            for seg in event.message_obj.message
        )

        # 固定深夜休眠：轻量指令可用，普通聊天/@/学习全部停止。
        if self._is_sleep_window():
            cmd0, _, _ = self._parse_command(event)
            wake_cmds={"帮助","状态","自检","版本","token","撤回"}
            if cmd0 not in wake_cmds:
                event.stop_event(); return

        self.register_member(group_id, sender_id, sender)
        self.record_message(group_id, sender, text if text else "[非文本消息]")

        if self._event_is_owner(event) and self._is_recharge_notice(text):
            yield event.plain_result(self._owner_recharge_reply())
            event.stop_event(); return

        # ---------- 第一优先级：指令。命中就自己回、自己截断，绝不进聊天流程 ----------
        cmd_name, cmd_arg, unknown_cmd = self._parse_command(event)
        if cmd_name is not None:
            if unknown_cmd:
                if self._cfg("commands", "strict", default=True):
                    prefix = (self._cfg("commands", "prefixes", default=None) or ["/"])[0]
                    yield event.plain_result(f"没有「{cmd_arg[:12]}」这个指令，发 {prefix}帮助 看指令表")
                    event.stop_event()
                    return
            else:
                logger.info(f"[恶魔bot] 指令命中：{cmd_name} 参数={cmd_arg!r}")
                try:
                    result = await self._dispatch_command(event, cmd_name, cmd_arg)
                except Exception as e:
                    logger.error(f"[恶魔bot] 指令 {cmd_name} 执行失败：{e}")
                    result = f"指令执行出错了：{type(e).__name__}"
                    asyncio.create_task(
                        self._sponsor_alert(event, group_id, f"{cmd_name}: {type(e).__name__}")
                    )
                event._demonbot_is_command_reply = True
                if isinstance(result, str) and result:
                    yield event.plain_result(result)
                elif isinstance(result, list) and result:
                    yield event.chain_result(result)
                event.stop_event()
                return

        # ---------- 被@着要图：直接发图，不请求 LLM ----------
        if (is_at or is_reply_to_bot) and self._wants_image(text):
            if await self._try_send_image(event, group_id, text):
                event.stop_event()
                return

        # ---------- 被@着发图：看图接话（需要视觉模型） ----------
        if (is_at or is_reply_to_bot) and not self._is_muted(group_id):
            if any(isinstance(seg, Comp.Image) for seg in event.message_obj.message):
                if await self._try_vision_reply(event, group_id, text):
                    event.stop_event()
                    return

        # ---------- 管理员解禁：优先级最高，任何状态下都生效 ----------
        if self._is_admin(sender_id) and (is_at or is_reply_to_bot) and self._check_unmute_trigger(text):
            was_muted = self._is_muted(group_id) or self._is_platform_muted(group_id)
            self.force_unmute(group_id)
            logger.info(f"[恶魔bot] 管理员 {sender_id} 强制解除群 {group_id} 的所有静音状态")
            if was_muted:
                await self._safe_send(event, "行，我回来了", group_id)
                event.stop_event()
                return
            # 本来就没被禁言，那就当普通对话继续走后面的流程

        # ---------- QQ平台禁言退避：连发都发不出去，就别浪费 token 了 ----------
        if self._is_platform_muted(group_id):
            event.stop_event()
            return

        # ---------- 闭嘴 / 复活指令（必须@或回复bot才触发）----------
        if is_at or is_reply_to_bot:
            if self._check_mute_trigger(text):
                minutes = self._cfg("mute", "duration_minutes", default=10)
                self._muted_until[group_id] = time.time() + minutes * 60
                self._muted_by[group_id] = sender_id
                await self._safe_send(event, f"好，闭嘴{minutes}分钟", group_id)
                event.stop_event()
                return

            if self._is_muted(group_id) and self._check_unmute_trigger(text):
                allow_anyone = self._cfg("mute", "anyone_can_unmute", default=True)
                if allow_anyone or self._is_admin(sender_id) or self._muted_by.get(group_id) == sender_id:
                    self.force_unmute(group_id)
                    await self._safe_send(event, "嗯？行吧，那我说两句", group_id)
                    event.stop_event()
                    return

        # ---------- 闭嘴期内，除上面的指令一律不吭声 ----------
        if self._is_muted(group_id):
            event.stop_event()
            return

        # ---------- 结束语：对话该收尾了，别再接话 ----------
        if text and self._is_end_phrase(text):
            quiet = self._cfg("end_talk", "quiet_seconds", default=180)
            self._quiet_until[group_id] = time.time() + quiet
            logger.debug(f"[恶魔bot] 群 {group_id} 收到结束语「{text}」，静默 {quiet} 秒")
            if is_at or is_reply_to_bot:
                if self._cfg("end_talk", "silent_even_if_at", default=True):
                    event.stop_event()
                    return
            else:
                event.stop_event()
                return

        # ---------- 复读检测 ----------
        if text and await self._try_join_repeat(event, group_id, text):
            event.stop_event()
            return

        # ---------- 图片 / 视频直接跳过 ----------
        if self._cfg("media_skip", "enabled", default=True):
            has_media = any(
                isinstance(seg, (Comp.Image, Comp.Video)) for seg in event.message_obj.message
            )
            if has_media:
                if self._cfg("media_skip", "react_enabled", default=False):
                    if random.random() < self._cfg("media_skip", "react_chance", default=0.15):
                        face_ids = self._cfg(
                            "media_skip", "react_face_ids", default=[178, 179, 182, 76]
                        )
                        try:
                            yield event.chain_result([Comp.Face(id=random.choice(face_ids))])
                        except Exception as e:
                            logger.warning(f"[恶魔bot] 发送表情反应失败：{e}")
                event.stop_event()
                return

        # ---------- 发言总开关：关掉之后除了指令一个字都不说 ----------
        if not self._cfg("reply_gate", "speak", default=True):
            event.stop_event()
            return

        # ---------- 回复对象开关：能不能搭理群友 / 能不能搭理管理员 ----------
        if self._is_admin(sender_id):
            if not self._cfg("reply_gate", "reply_to_admin", default=True):
                event.stop_event()
                return
        elif not self._cfg("reply_gate", "reply_to_members", default=True):
            event.stop_event()
            return

        # ---------- 高峰时段：双倍计费，能不花的钱一律不花 ----------
        if self.in_peak_hours():
            if is_at or is_reply_to_bot:
                # 被人直接叫到还是要理，但加一道冷却，
                # 免得高峰期有人连环 @ 把一天的额度刷完
                gap = self._cfg("peak_hours", "at_reply_cooldown_seconds", default=90)
                if not self._is_admin(sender_id) and (
                    time.time() - self._last_reply_at.get(group_id, 0) < gap
                ):
                    self._saver_stats["peak_skipped"] += 1
                    logger.debug(f"[恶魔bot] 高峰时段 @ 冷却中，跳过（群 {group_id}）")
                    event.stop_event()
                    return
                self._last_reply_at[group_id] = time.time()
                return
            # 没被叫到 —— 高峰期一律不主动开口
            self._saver_stats["peak_skipped"] += 1
            logger.debug(
                f"[恶魔bot] 高峰时段不主动插话（还有 {self._peak_ends_in()} 分钟结束）"
            )
            event.stop_event()
            return

        # ---------- 要不要插话 ----------
        if not self._cfg("reply_gate", "enabled", default=True):
            return

        if (is_at or is_reply_to_bot) and self._cfg("reply_gate", "always_reply_on_at", default=True):
            return  # 有人直接找你说话，放行

        if not self._group_allowed(group_id):
            event.stop_event()
            return

        # 刚被"嗯嗯/好的"收尾过，短时间内不主动插话
        if self._in_quiet_period(group_id):
            event.stop_event()
            return

        now = time.time()
        cooldown = self._cfg("reply_gate", "min_reply_interval_seconds", default=45)
        if now - self._last_reply_at.get(group_id, 0) < cooldown:
            event.stop_event()
            return

        busy = self._is_busy_group(group_id)
        if busy and not self._busy_window_allows_reply(group_id):
            event.stop_event()
            return

        mode = self._cfg("reply_gate", "mode", default="rule")
        # llm 模式为了"要不要开口"这一个是非题，要额外烧一次完整请求。
        # 后台预算不允许时自动退回本地规则判断，省下的正是这部分看不见的钱。
        if mode == "llm" and not self._aux_llm_allowed():
            mode = "rule"
        if mode == "llm":
            should_reply = await self._llm_judge(event, group_id, text, busy=busy)
        else:
            should_reply = self._rule_judge(group_id, sender_id, text, busy=busy)

        if should_reply:
            self._last_reply_at[group_id] = now
            if busy:
                self._record_auto_reply(group_id)
            return
        event.stop_event()
        return

    # ==================== 私聊：只处理指令，聊天交给框架原有流程 ====================

    @filter.event_message_type(filter.EventMessageType.PRIVATE_MESSAGE, priority=999)
    async def on_private_message(self, event: AstrMessageEvent):
        """私聊里同样能用全部指令。

        私聊不做插话判断、不做静音、不做复读——那些都是群聊场景的东西。
        这里只拦指令，其余消息原样放行给正常的聊天流程。
        """
        group_id = event.get_group_id() or "unknown"
        if group_id != "unknown":
            self._known_groups.add(str(group_id))
        sender_id = str(event.get_sender_id())
        if self._is_owner(sender_id, event.get_sender_name() or ""):
            self._last_owner_message_at = time.time()
        text = _CTRL_RE.sub("", event.message_str or "")

        if self._event_is_owner(event) and self._is_recharge_notice(text):
            yield event.plain_result(self._owner_recharge_reply())
            event.stop_event()
            return

        cmd_name, cmd_arg, unknown_cmd = self._parse_command(event)
        if cmd_name is None:
            return
        if unknown_cmd:
            if self._cfg("commands", "strict", default=True):
                prefix = (self._cfg("commands", "prefixes", default=None) or ["/"])[0]
                yield event.plain_result(f"没有「{cmd_arg[:12]}」这个指令，发 {prefix}帮助 看指令表")
                event.stop_event()
            return

        logger.info(f"[恶魔bot] 私聊指令：{cmd_name} 来自 {sender}({sender_id})")
        try:
            result = await self._dispatch_command(event, cmd_name, cmd_arg)
        except Exception as e:
            logger.error(f"[恶魔bot] 私聊指令 {cmd_name} 执行失败：{e}")
            result = f"指令执行出错了：{type(e).__name__}"
        event._demonbot_is_command_reply = True
        if isinstance(result, str) and result:
            yield event.plain_result(result)
        elif isinstance(result, list) and result:
            yield event.chain_result(result)
        event.stop_event()

    def _music_match(self, text: str):
        if music is None or not self._cfg("music","enabled",default=True):
            return None
        try:
            return music.match_lyric_clue(text, self.data_dir / str(self._cfg("music","local_clue_file",default="music_clues.json")))
        except Exception as e:
            logger.debug(f"[恶魔bot] 歌词识别跳过：{e}")
            return None

    # ==================== 插话判断：rule 模式 ====================

    def _rule_judge(self, group_id: str, sender_id: str, text: str, busy: bool = False) -> bool:
        damp = self._cfg("reply_gate", "busy_chance_multiplier", default=0.3) if busy else 1.0

        keywords = self._cfg("reply_gate", "keywords", default=[])
        if any(kw and kw in text for kw in keywords):
            return True

        emotion_keywords = self._cfg("reply_gate", "emotion_keywords", default=[])
        emotion_chance = self._cfg("reply_gate", "emotion_reply_chance", default=0.35) * damp
        if emotion_keywords and any(kw and kw in text for kw in emotion_keywords):
            if random.random() < emotion_chance:
                return True

        silence_threshold = self._cfg("reply_gate", "new_topic_silence_seconds", default=300)
        new_topic_chance = self._cfg("reply_gate", "new_topic_reply_chance", default=0.2) * damp
        bucket = self._history.get(group_id, [])
        if len(bucket) >= 2:
            if time.time() - bucket[-2]["time"] > silence_threshold and random.random() < new_topic_chance:
                return True

        if sender_id in self.admin_ids:
            base_chance = self._cfg("reply_gate", "admin_random_reply_chance", default=0.5)
        else:
            base_chance = self._cfg("reply_gate", "random_reply_chance", default=0.05)
        return random.random() < base_chance * damp

    # ==================== 插话判断：llm 模式 ====================

    async def _llm_judge(
        self, event: AstrMessageEvent, group_id: str, text: str, busy: bool = False
    ) -> bool:
        context_n = self._cfg("reply_gate", "llm_context_messages", default=6)
        recent = self._history.get(group_id, [])[-(context_n + 1):-1]
        context_lines = "\n".join(f"{m['sender']}: {m['text']}" for m in recent) or "（无更早记录）"

        busy_hint = (
            "\n注意：这个群现在消息刷得很快，一个真实的人此时更倾向于潜水，"
            "只有强相关或被明显需要时才会说话，不确定就选不回复。"
            if busy
            else ""
        )
        prompt = (
            f"最近的群聊记录：\n{context_lines}\n\n"
            f"最新一条消息：{text}\n{busy_hint}\n\n"
            "如果有一个正在潜水看聊天、不是每条都回的真实群友，现在会不会想接一句？"
            "只回答「是」或「否」。"
        )
        resp = await self._llm_text(
            prompt,
            system_prompt="你是判断群聊插话时机的助手，只输出「是」或「否」。",
            event=event,
            timeout=self._cfg("reply_gate", "llm_judge_timeout_seconds", default=12),
        )
        return "是" in resp

    # ==================== 高峰时段 / 省钱模式 ====================
    #
    # DeepSeek 这类接口在高峰时段（北京时间 9:00-12:00、14:00-18:00）按双倍计费。
    # 也就是说同一句话，中午说和早上说，价钱差一倍。
    # 这一段的全部目的就是：高峰期尽量什么都别干，等便宜的时候再干。

    def _bj_minutes(self) -> int:
        """当前北京时间的「时*60+分」。用 UTC 换算，不依赖容器的系统时区。"""
        offset = self._cfg("peak_hours", "utc_offset_hours", default=8)
        t = time.gmtime(time.time() + offset * 3600)
        return t.tm_hour * 60 + t.tm_min

    def _peak_windows(self) -> list:
        out = []
        raw = self._cfg("peak_hours", "windows", default=None) or DEFAULT_PEAK_WINDOWS
        for item in raw:
            try:
                a, b = str(item).split("-", 1)
                ah, am = (int(x) for x in a.strip().split(":"))
                bh, bm = (int(x) for x in b.strip().split(":"))
                out.append((ah * 60 + am, bh * 60 + bm))
            except Exception:
                logger.warning(f"[恶魔bot] 高峰时段「{item}」格式不对，应该写成 09:00-12:00")
        return out or [(540, 720), (840, 1080)]

    def in_peak_hours(self) -> bool:
        if not self._cfg("peak_hours", "enabled", default=True):
            return False
        now = self._bj_minutes()
        for start, end in self._peak_windows():
            if start <= end:
                if start <= now < end:
                    return True
            elif now >= start or now < end:      # 跨零点的窗口
                return True
        return False

    def _peak_ends_in(self) -> int:
        """距当前高峰窗口结束还有几分钟；不在高峰返回 0。"""
        now = self._bj_minutes()
        for start, end in self._peak_windows():
            if start <= end and start <= now < end:
                return end - now
            if start > end and (now >= start or now < end):
                return (end + 1440 - now) % 1440
        return 0

    def _aux_day_key(self) -> str:
        offset = self._cfg("peak_hours", "utc_offset_hours", default=8)
        return time.strftime("%Y-%m-%d", time.gmtime(time.time() + offset * 3600))

    def _aux_used_today(self) -> int:
        if self._aux_budget.get("day") != self._aux_day_key():
            self._aux_budget = {"day": self._aux_day_key(), "count": 0}
        return int(self._aux_budget.get("count", 0))

    def _aux_llm_allowed(self) -> bool:
        """后台辅助请求现在能不能发。"""
        if self._is_sleep_window():
            return False
        if self.in_peak_hours() and self._cfg("peak_hours", "block_background_llm", default=True):
            return False
        limit = self._cfg("token_saver", "daily_aux_call_limit", default=60)
        if limit <= 0:
            return True
        return self._aux_used_today() < limit

    def _count_aux_call(self):
        self._aux_used_today()          # 顺带跨天重置
        self._aux_budget["count"] = self._aux_budget.get("count", 0) + 1

    # ==================== 心情值系统 ====================

    def _mood_schedule(self):
        return self._cfg("mood", "schedule", default=None) or DEFAULT_MOOD_SCHEDULE

    def _mood_baseline_for_hour(self, hour: int) -> dict:
        for item in self._mood_schedule():
            start, end = item["start"], item["end"]
            if start <= end:
                if start <= hour < end:
                    return item
            elif hour >= start or hour < end:
                return item
        return self._mood_schedule()[0]

    def _update_mood(self) -> float:
        info = self._mood_baseline_for_hour(time.localtime().tm_hour)
        score = float(self._mood.get("score", 0.0))
        drift_weight = self._cfg("mood", "drift_weight", default=0.06)
        jitter = self._cfg("mood", "jitter", default=0.05)
        score += (info["baseline"] - score) * drift_weight
        score += random.uniform(-jitter, jitter)
        score = max(-1.0, min(1.0, score))
        self._mood["score"] = score
        self._mood["last_update"] = int(time.time())
        self._save_json(self.mood_file, self._mood)
        return score

    @staticmethod
    def _mood_feel_label(score: float) -> str:
        if score > 0.5:
            return "心情很好，兴致挺高"
        if score > 0.15:
            return "心情不错，还挺乐呵"
        if score > -0.15:
            return "心情比较平淡，中规中矩"
        if score > -0.5:
            return "有点烦躁或没精神，不太想多说话"
        return "心情很差，很累或者很丧"

    def _mood_prompt_fragment(self, asked_activity: bool, is_owner: bool = False) -> str:
        """
        状态注入。这里是「让 bot 别每次都报告自己在干嘛」的总闸门。

        允许把活动说出口，只有三种情况：
          1. 对方直接问了（在干嘛/睡了没/忙不忙）
          2. 刚跨进一个新时段（20 分钟内），且这个活动今天还没主动提过
          3. 深夜被戳，说一句「困死了」是合理的
        其余一律只给语气，不给活动名——模型看不见「我在吃午饭」这几个字，
        自然就不会把它念出来。这是原插件最大的体验问题。
        """
        score = self._update_mood()
        state = self._life_now()

        # 生活状态模块不可用时，退回原来的时段表逻辑，功能不丢
        if state is None or life is None:
            info = self._mood_baseline_for_hour(time.localtime().tm_hour)
            feel = self._mood_feel_label(score)
            if asked_activity and self._cfg("mood", "mention_activity_when_asked", default=True):
                return (
                    f"\n\n[状态]现在{info['label']}，{feel}。"
                    f"被问起就说在{info['activity']}，一句带过。"
                )
            return f"\n\n[状态]现在{info['label']}，{feel}。只让语气带上这种状态，别说出来。"

        # 生活状态里的心情基线也参与进来，让「今天加班」这类事件真的影响语气
        blended = max(-1.0, min(1.0, score * 0.5 + float(state.get("mood", 0.0)) * 0.5))

        tell = False
        if asked_activity and self._cfg("mood", "mention_activity_when_asked", default=True):
            tell = True
        elif (
            self._cfg("life", "volunteer_on_change", default=True)
            and state.get("just_changed")
            and not self._life.already_told(state.get("key", ""))
            and random.random() < self._cfg("life", "volunteer_chance", default=0.35)
        ):
            tell = True
        if tell and self._life is not None:
            self._life.mark_told(state.get("key", ""))
        return life.build_fragment(state, blended, tell_activity=tell, is_owner=is_owner)

    # ==================== LLM 请求前：注入知识 / 群友 / 风格 / 约束 ====================

    @filter.on_llm_request()
    async def on_llm_request(self, event: AstrMessageEvent, req):
        group_id = event.get_group_id() or "unknown"
        text = _CTRL_RE.sub("", event.message_str or "")
        if self._is_sleep_window():
            return

        # ===== 省钱第一刀，也是最重要的一刀：把带给模型的历史对话砍短 =====
        # 账单里最大的一块从来不是"bot 回了多长"，而是每次请求重新上传的上下文。
        # 会话越久，单次输入 token 越大，而且每来一条消息就要重付一遍。
        self._trim_request_context(req)

        saver = self._cfg("token_saver", "enabled", default=True)
        smart = saver and self._cfg("token_saver", "smart_inject", default=True)
        fragments = []

        # 长期人格是全局共享档案，不依赖当前群聊/私聊会话。
        # 必须在每次 LLM 请求前重新从 self._persona 构造片段，否则
        # /记住信息 刚写入的内容下一条消息仍然无法被模型看到。
        persona_fragment = self._persona_fragment(text)
        if persona_fragment:
            fragments.append(persona_fragment)

        # 1) 当前是谁在说话——这条必须排在最前面，也是「我是谁」被答错的根因：
        #    模型原本只看得到一句孤零零的消息文本，看不到发送者，
        #    于是默认按名单里最显眼的那个人（管理员）来猜。
        if self._cfg("members", "inject_speaker", default=True):
            fragments.append(self._speaker_fragment(event, group_id))

        # 2) 心情 + 生活状态（默认不含"我在干什么"，见 _mood_prompt_fragment 里的说明）
        if self._cfg("mood", "enabled", default=True):
            fragments.append(
                self._mood_prompt_fragment(
                    bool(_ASK_ACTIVITY_RE.search(text)),
                    is_owner=self._event_is_owner(event),
                )
            )

        # 3) 群友编号表。smart_inject 打开时，只有真的在聊"谁是谁"才带上这张表——
        #    它是所有注入片段里最长的一段，每条消息都带等于白烧钱。
        if self._cfg("members", "inject_roster", default=True) and (
            not smart or self._mentions_person(text)
        ):
            roster = self.member_roster(group_id, self._cfg("members", "roster_limit", default=8))
            if roster:
                fragments.append(
                    "\n\n[群里都有谁，编号只是你自己内部区分用的，"
                    "回复里绝对不要说出编号、QQ号或「备注」二字]\n" + roster
                )

        # 4) 最近几句话分别是谁说的，供「刚才那个人是谁」这类问题定位
        if self._cfg("members", "inject_recent_context", default=True) and (not smart or self._mentions_person(text) or self._event_is_owner(event)):
            ctx = self._recent_context(group_id, self._cfg("members", "recent_context_lines", default=3))
            if ctx:
                fragments.append("\n\n[刚才几句话分别是谁说的，注意区分不同的人]\n" + ctx)

        # 5) 说话风格速记
        if self._cfg("style", "enabled", default=True):
            profile = self._style.get("profile", "")
            if profile:
                fragments.append(
                    "\n\n[群里真实的打字风格，请自然靠近这种感觉，但不要照抄原句]\n" + profile
                )
            if self._style_due():
                asyncio.create_task(self.update_style(group_id, event))

        # 6) 新梗联网学习：查这条消息里没见过的词
        if self._cfg("knowledge", "enabled", default=True):
            notes = await self._collect_slang_notes(text, event)
            if notes:
                fragments.append(
                    "\n\n[刚查到的梗/新词含义，直接当成你本来就知道的事用，"
                    "不要说「我查了一下」或提到搜索]\n" + notes
                )

            hot = self._knowledge.get("hot", {}).get("items", [])
            if hot and self._cfg("knowledge", "inject_hot_topics", default=False):
                n = self._cfg("knowledge", "hot_inject_count", default=3)
                fragments.append(
                    "\n\n[最近大家在聊的热搜话题，只在相关时顺嘴提一句，不要主动播报新闻]\n"
                    + "、".join(hot[:n])
                )
            asyncio.create_task(self.refresh_hot_topics())

        # 7) 输出格式硬约束（DeepSeek 这类模型对显式禁令最敏感，放最后印象最深）
        if self._cfg("reply_style", "inject_constraints", default=True):
            fragments.append(self._style_constraint_fragment())

        if not fragments:
            return
        fragment = "".join(fragments)
        try:
            if hasattr(req, "system_prompt"):
                req.system_prompt = (req.system_prompt or "") + fragment
            elif hasattr(req, "prompt"):
                req.prompt = fragment + "\n\n" + (req.prompt or "")
        except Exception as e:
            logger.warning(f"[恶魔bot] 注入 prompt 失败：{e}")
        # 注入完成后再做一次总长度保护，避免人格片段被后面的处理阶段撑爆。
        try:
            cap = self._cfg("token_saver", "max_system_prompt_chars", default=900)
            if hasattr(req, "system_prompt") and 0 < cap < len(req.system_prompt or ""):
                # 人格片段已经放在最前面，因此裁剪时优先保留人格与发言人信息。
                req.system_prompt = (req.system_prompt or "")[:cap]
        except Exception as e:
            logger.debug(f"[恶魔bot] 最终 system prompt 裁剪失败：{e}")

    def _trim_request_context(self, req) -> None:
        """裁剪这次 LLM 请求要上传的历史对话和人格提示词。

        为什么这是省钱的关键：框架每次请求都会把整段会话历史重新发一遍。
        聊到第 100 条时，你发一句"在吗"，实际上传的是那 100 条的全文。
        用量表上"平均每次请求输入 token"从 1.7k 涨到 33k，涨的就是这一块。
        """
        if not self._cfg("token_saver", "enabled", default=True):
            return
        max_msgs = self._cfg("token_saver", "max_context_messages", default=4)
        max_chars = self._cfg("token_saver", "max_context_chars_per_message", default=90)
        try:
            ctx = getattr(req, "contexts", None)
            if isinstance(ctx, list) and ctx:
                before = len(ctx)
                kept = ctx
                if max_msgs > 0 and before > max_msgs:
                    # 开头如果是 system 消息（人格设定）要留住，其余只取最近几条
                    head = [
                        m for m in ctx[:1]
                        if isinstance(m, dict) and m.get("role") == "system"
                    ]
                    kept = head + ctx[-max_msgs:]
                # 复制一份再改，绝不动框架里存着的原始会话记录
                new_ctx = []
                for m in kept:
                    if isinstance(m, dict):
                        m = dict(m)
                        c = m.get("content")
                        if isinstance(c, str) and 0 < max_chars < len(c):
                            m["content"] = c[:max_chars] + "…"
                    new_ctx.append(m)
                req.contexts = new_ctx
                if before != len(new_ctx):
                    self._saver_stats["ctx_trimmed"] += before - len(new_ctx)
                    logger.debug(f"[恶魔bot] 上下文裁剪：{before} → {len(new_ctx)} 条")

            # 人格提示词本身也可能是几千字的长文，同样每次都要重付
            cap = self._cfg("token_saver", "max_system_prompt_chars", default=900)
            sp = getattr(req, "system_prompt", "") or ""
            if 0 < cap < len(sp):
                req.system_prompt = sp[:cap]
                logger.debug(f"[恶魔bot] 人格提示词 {len(sp)} 字过长，已截到 {cap} 字")
        except Exception as e:
            logger.warning(f"[恶魔bot] 裁剪上下文失败（不影响回复）：{e}")

    @staticmethod
    def _mentions_person(text: str) -> bool:
        """这句话是不是在聊「谁」。用来决定要不要把群友名单塞进提示词。"""
        return bool(re.search(r"(谁|他|她|你们|大家|群友|刚才|上面|楼上|@)", text or ""))

    def _style_constraint_fragment(self) -> str:
        """输出约束。

        原来这段写了 9 条、四百多字，每次请求都要原样上传一遍——
        约束本身就成了账单大头。现在压到 5 条，
        剩下的用本地正则在 _sanitize_reply 里兜底，
        本地能做的事就别花钱让模型做。
        """
        max_chars = self._cfg("reply_style", "max_chars", default=26)
        return (
            "\n\n[硬性要求]\n"
            f"1.只回一句话，不超过{max_chars}字，说完就停。\n"
            "2.不要markdown、不要写自己的名字或时间。\n"
            "3.不要主动交代自己在干嘛、在哪、吃没吃睡没睡，除非对方直接问。\n"
            "4.不要括号里的动作描写，不要客服式收尾。\n"
            "5.问「我是谁」时按上面标明的当前发言人回答。"
        )

    async def _collect_slang_notes(self, text: str, event: AstrMessageEvent) -> str:
        terms = self._candidate_terms(text)
        if not terms:
            return ""
        # 查梗要先搜网页、再让模型总结，是一次完整的额外请求。
        # 词库里已经有的会走缓存不花钱，没有的在高峰期就先欠着。
        if not self._aux_llm_allowed() and not all(self.slang_get(t) for t in terms):
            return ""
        timeout = self._cfg("knowledge", "total_lookup_timeout_seconds", default=12)
        notes = []
        try:
            results = await asyncio.wait_for(
                asyncio.gather(*[self.lookup_slang(t, event) for t in terms], return_exceptions=True),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            logger.debug("[恶魔bot] 新词联网查询超时，本次先不注入")
            return ""
        for term, desc in zip(terms, results):
            if isinstance(desc, str) and desc and desc != "无特殊含义":
                notes.append(f"{term} = {desc}")
        return "\n".join(notes)

    def _speaker_fragment(self, event: AstrMessageEvent, group_id: str) -> str:
        sender_id = str(event.get_sender_id())
        nick = self._clean_nick(event.get_sender_name()) or sender_id
        book = self._members.get(group_id) or {}
        rec = (book.get("members") or {}).get(sender_id) or {}
        code = rec.get("code", "")
        is_owner = self._is_owner(sender_id, nick)
        is_admin = is_owner or self._is_admin(sender_id)
        who = f"{nick}" + (f"（{code}）" if code else "")
        if is_owner:
            title = self._owner_title()
            role = (
                f"他是你{title}，就是把你创造出来、平时管着你的那个人，"
                f"不管在哪个群，只要是他，你都认得。对他可以更亲近、更听话一点，"
                f"但别每句话都喊{title}，偶尔喊一次就够了"
            )
        elif is_admin:
            role = "他是管理员，跟你比较熟"
        else:
            role = "他不是你主人，是另一个群友"
        return (
            f"\n\n[当前发言人]{who}。{role}。"
            "他问「我是谁」时答案就是这个人，别猜成别人。"
        )

    def _recent_context(self, group_id: str, limit: int = 6) -> str:
        bucket = self._history.get(group_id, [])[-(limit + 1):-1]
        lines = []
        for m in bucket:
            text = (m.get("text") or "").strip()
            if text:
                lines.append(f"{m.get('sender', '')}：{text[:40]}")
        return "\n".join(lines)

    # ==================== 回复清洗 + 长度控制 + 分段 ====================

    def _strip_markdown(self, text: str) -> str:
        text = _MD_HEAD_RE.sub("", text or "")
        text = _MD_LIST_RE.sub("", text)
        return _MD_BOLD_RE.sub("", text)

    def _sanitize_reply(self, text: str, group_id: str, asked_activity: bool = False) -> str:
        """把模型输出里"念聊天记录格式"的痕迹全部洗掉。

        asked_activity=True 表示对方确实问了"在干嘛"，
        这时才允许保留"我还在补觉呢"这类交代行踪的话。
        """
        if not text:
            return ""
        text = _CTRL_RE.sub("", text)
        text = self._strip_markdown(text)
        text = _TS_RE.sub(" ", text)

        known_names = set()
        book = self._members.get(group_id) or {}
        for rec in (book.get("members") or {}).values():
            for n in rec.get("names", []):
                if n:
                    known_names.add(n)
        for extra in self._cfg("reply_style", "self_names", default=[]):
            if extra:
                known_names.add(str(extra))

        # 反复剥掉开头的「昵称:」前缀（模型经常连着写好几层）
        for _ in range(4):
            m = _NAME_PREFIX_RE.match(text)
            if not m:
                break
            name = m.group(1).strip()
            if name in known_names or len(name) <= 8:
                text = text[m.end():]
            else:
                break

        # 同一个「某某:」在一句话里出现两次以上，那必然是模型在念聊天记录格式，
        # 不用查群友档案也能断定。这条兜住了 bot 刚进群、档案还是空的时候。
        repeated = [
            n for n, c in
            __import__("collections").Counter(
                re.findall(r"(?:^|[\s])([^\s:：]{1,10})\s*[:：]", text)
            ).items() if c >= 2
        ]
        for n in repeated:
            text = re.sub(r"(?:^|\s)%s\s*[:：]\s*" % re.escape(n), " ", text)

        # 中间夹着的 "昵称: " 也一并压平（分段被模型自己拼回来的情况）
        if known_names:
            pattern = r"\s*(?:%s)\s*[:：]\s*" % "|".join(re.escape(n) for n in known_names)
            text = re.sub(pattern, " ", text)

        # 长的先匹配：否则「有什么可以帮」会先命中，把「还」孤零零地剩在句尾
        for tail in sorted(_SERVICE_TAILS, key=len, reverse=True):
            idx = text.find(tail)
            if idx > 0:
                text = text[:idx]
        # 切完可能留下「，还」「，但」这种半截连词
        text = re.sub(r"[，,、]?\s*(?:还|但|不过|另外|所以)\s*$", "", text)

        # 「（迷迷糊糊翻了个身）」这类旁白：模型演上瘾了，本地直接删干净
        if self._cfg("reply_style", "strip_action_text", default=True):
            text = _ACTION_PAREN_RE.sub("", text)

        # 没人问就主动播报"我还在补觉呢"——这是最招人烦的一种 AI 味。
        # 提示词里写了模型也常忘，所以本地再兜一道；
        # 删完如果剩不下几个字，就说明整句话就是在讲这个，那还是留着原文。
        if self._cfg("reply_style", "strip_self_report", default=True) and not asked_activity:
            stripped = _SELF_REPORT_RE.sub("", text)
            # 删掉半句话之后常留下孤零零的标点（「呀...，别吵我」），一并收拾干净
            stripped = re.sub(r"([。！!？?…]+|\.{2,})\s*[，,、；;]+", r"\1", stripped)
            stripped = re.sub(r"[，,、；;]{2,}", "，", stripped)
            stripped = re.sub(r"^[，,。、！!？?；;\s]+", "", stripped).strip()
            if len(stripped) >= self._cfg("reply_style", "min_chars", default=4):
                text = stripped

        text = re.sub(r"[ \t]+", " ", text)
        text = re.sub(r"\n{2,}", "\n", text).strip()
        return text.rstrip("，,、 ")

    def _shorten(self, text: str) -> str:
        """砍到一句话、一个字数上限之内。

        断句要把「…」和「...」也算成句末——群聊里省略号用得比句号还多，
        不认它的话，截断就会卡在半个词上（「你们玩你」这种）。
        """
        max_chars = self._cfg("reply_style", "max_chars", default=26)
        if self._cfg("reply_style", "single_sentence", default=True):
            parts = [p for p in re.split(r"(?<=[。！？!?\n])|(?<=\.{3})|(?<=…)", text) if p.strip()]
            if parts:
                # 取第一句之后，只要还没超字数预算就继续往后带，
                # 免得「谁是小鼻嘎呀...」这种把真正想说的半句给丢了
                merged = parts[0].strip()
                for nxt in parts[1:]:
                    nxt = nxt.strip()
                    if not nxt:
                        continue
                    if len(merged) + len(nxt) <= max_chars:
                        merged = (merged + nxt).strip()
                    else:
                        break
                text = merged
        if len(text) > max_chars:
            cut = text[:max_chars]
            # 宁可短一点，也要断在标点上，别把词切两半
            m = list(re.finditer(r"[，,。！？!?、…\. ]", cut))
            if m and m[-1].start() >= max_chars * 0.4:
                cut = cut[: m[-1].start()]
            text = cut.rstrip("，,、。. ")
        return text.strip()

    def _split_by_punct(self, text: str) -> list:
        min_len = self._cfg("segment_reply", "trigger_min_length", default=30)
        if len(text) < min_len:
            return [text]
        parts = [p.strip() for p in _SPLIT_RE.split(text)]
        parts = [p for p in parts if p]
        return parts if parts else [text]

    @filter.on_decorating_result()
    async def on_decorating_result(self, event: AstrMessageEvent):
        result = event.get_result()
        if result is None:
            return
        # 指令回复（/状态 /发送日志 /帮助 等）不走聊天语气清洗/截断，
        # 否则多行的状态报告、日志文件说明会被砍成一句话。
        if getattr(event, "_demonbot_is_command_reply", False):
            return
        chain = result.chain
        group_id = event.get_group_id() or "unknown"
        try:
            usage = getattr(result, "usage", None)
            if usage is None and getattr(result, "raw_completion", None) is not None:
                usage = getattr(result.raw_completion, "usage", None)
            if usage is not None and not getattr(event, "_demonbot_usage_recorded", False):
                self._record_usage(usage, source="chat")
                event._demonbot_usage_recorded = True
        except Exception as e:
            logger.debug(f"[恶魔bot] 记录 token 用量失败：{e}")

        full_text = "".join(seg.text for seg in chain if isinstance(seg, Comp.Plain))
        if not full_text.strip():
            return

        user_text = _CTRL_RE.sub("", event.message_str or "")

        # ---------- 报错拦截：把「LLM 响应错误: All chat models failed」换成人话 ----------
        # 你日志里最常见的那条是 402 Insufficient Balance——余额没了。
        # 直接把框架的英文报错甩进群里既难看又没用，不如让它 @主人 要钱。
        if self._looks_like_error(full_text):
            group_id2 = event.get_group_id() or "unknown"
            logger.warning(f"[恶魔bot] 拦下一条报错回复：{full_text[:60]}")
            line = random.choice(
                self._cfg("sponsor", "lines", default=None)
                or ["出bug了，给我主人充值点token求求啦"]
            )
            new_chain = []
            owner_qq = self._primary_owner_qq()
            if owner_qq and event.get_group_id():
                try:
                    new_chain.append(Comp.At(qq=int(owner_qq) if owner_qq.isdigit() else owner_qq))
                    new_chain.append(Comp.Plain(" "))
                except Exception:  # noqa: BLE001
                    pass
            new_chain.append(Comp.Plain(str(line)))
            result.chain = new_chain
            self._pending_tail.pop(event.unified_msg_origin, None)
            self._pending_sticker[event.unified_msg_origin] = (
                self._cfg("sponsor", "sticker_emotion", default="出bug") or "出bug", True
            )
            self._last_sponsor_at = time.time()
            return

        asked_activity = bool(_ASK_ACTIVITY_RE.search(user_text))

        cleaned = full_text
        if self._cfg("reply_style", "enabled", default=True):
            cleaned = self._sanitize_reply(full_text, group_id, asked_activity)
            cleaned = self._shorten(cleaned)
        if not cleaned.strip():
            cleaned = self._sanitize_reply(full_text, group_id, True)[:20] or full_text[:20]

        # 偶尔把适合诗意表达的普通回复改写成古诗文，但只在本地诗句库中选择，
        # 不调用 LLM，也不会把每句话都诗化。
        if (poetry is not None and self._cfg("poetry","enabled",default=True) and
            random.random() < float(self._cfg("poetry","reply_replace_chance",default=0.10)) and
            poetry.is_poetry_worthy(cleaned)):
            cleaned = poetry.pick_poem()

        non_plain = [seg for seg in chain if not isinstance(seg, Comp.Plain)]

        segments = [cleaned]
        if self._cfg("segment_reply", "enabled", default=False):
            segments = self._split_by_punct(cleaned)
            max_segments = self._cfg("segment_reply", "max_segments", default=2)
            segments = segments[:max_segments]

        result.chain = non_plain + [Comp.Plain(segments[0])]

        # 顺序修复的关键：尾段不再用 create_task 抢跑，
        # 而是登记下来，等 after_message_sent 确认第一段真的发出去之后再按顺序补发
        tail = [s for s in segments[1:] if s.strip()]
        key = event.unified_msg_origin
        self._pending_bot_segments[key] = list(segments)
        if tail:
            self._pending_tail[key] = tail
        else:
            self._pending_tail.pop(key, None)

        # ---------- 表情包：先判情绪，等这条消息真发出去之后再补一张 ----------
        # 判定完全靠本地正则，不额外请求模型。
        # 「困了」这一档会跟生活状态联动：作息表里正在睡觉时优先发睡觉表情。
        if stickers is not None and self._cfg("stickers", "enabled", default=True):
            emotion = stickers.detect_emotion(user_text, cleaned)
            if not emotion and self._life_is_sleeping():
                emotion = "困了"
            # 没命中具体情绪时：若开启「每句都跟表情」，用中性表情兜底
            if not emotion and self._cfg("stickers", "always_after_reply", default=True):
                emotion = "无语"  # 池子：呆/摇头/汗/静音，最不抢戏
            if emotion:
                force = emotion in ("挨骂", "困了") and self._cfg(
                    "stickers", "always_on_scold", default=True
                )
                # 每句都跟时，中性兜底不强制（仍受 chance 控制）；挨骂/困了强制
                if emotion == "无语" and self._cfg("stickers", "always_after_reply", default=True):
                    force = False
                self._pending_sticker[key] = (emotion, force)
            else:
                self._pending_sticker.pop(key, None)

        # 模拟"先想再打"：按长度给一点思考+打字延迟，也顺带避免秒回的机器感
        if self._cfg("reply_style", "think_delay_enabled", default=True):
            base = self._cfg("reply_style", "think_delay_base_seconds", default=0.8)
            per_char = self._cfg("reply_style", "think_delay_per_char", default=0.06)
            cap = self._cfg("reply_style", "think_delay_max_seconds", default=4.0)
            delay = min(cap, base + per_char * len(segments[0]))
            await asyncio.sleep(max(0.0, delay + random.uniform(-0.2, 0.3)))

    @filter.after_message_sent()
    async def after_message_sent(self, event: AstrMessageEvent):
        """第一段确认发出后，补发尾段，并把每一段都记录到插件历史/日志。"""
        key = event.unified_msg_origin
        tail = self._pending_tail.pop(key, None)
        all_segments = self._pending_bot_segments.pop(key, None)
        sticker = self._pending_sticker.pop(key, None)
        if key in self._tail_sending:
            return
        if not tail and not sticker:
            # 即使只有一段，也把这一段记录下来
            if all_segments:
                gid = event.get_group_id() or "unknown"
                self._record_bot_message(gid, all_segments[0])
                logger.info(f"[恶魔bot] 分段发送[1/1]：{all_segments[0][:120]}")
            return

        group_id = event.get_group_id() or "unknown"
        self._tail_sending.add(key)
        try:
            interval_min = self._cfg("segment_reply", "interval_min", default=1.2)
            interval_max = self._cfg("segment_reply", "interval_max", default=2.5)

            # 第一段由 AstrBot 已经确认发送成功
            if all_segments:
                self._record_bot_message(group_id, all_segments[0])
                logger.info(
                    f"[恶魔bot] 分段发送[1/{len(all_segments)}]：{all_segments[0][:120]}"
                )

            total = len(all_segments) if all_segments else (1 + len(tail or []))
            sent_index = 1
            for seg in tail or []:
                await asyncio.sleep(random.uniform(interval_min, interval_max))
                if not await self._safe_send(event, seg, group_id):
                    logger.warning(f"[恶魔bot] 分段发送失败[{sent_index+1}/{total}]：{seg[:120]}")
                    break
                sent_index += 1
                self._record_bot_message(group_id, seg)
                logger.info(f"[恶魔bot] 分段发送[{sent_index}/{total}]：{seg[:120]}")

            if sticker:
                emotion, force = sticker
                await asyncio.sleep(random.uniform(0.6, 1.6))
                ok = await self._send_sticker(event, group_id, emotion, force=force)
                if ok:
                    logger.info(f"[恶魔bot] 表情发送：{emotion}")
        finally:
            self._tail_sending.discard(key)

    # ==================== 自建指令分发（不走框架的 command 装饰器） ====================
    #
    # 为什么不用 @filter.command：框架的指令匹配依赖唤醒前缀被正确剥离，
    # 一旦群里配置了唤醒词、或插件加载顺序有变，指令就会被当成普通聊天喂给 LLM。
    # 这里在最高优先级的消息监听里自己解析，命中就发结果 + stop_event，
    # 聊天流程根本没有机会介入。群聊和私聊共用同一套分发。

    # 指令表：(名字, 别名, 参数示例, 说明, 分类, 是否仅管理员)
    COMMAND_TABLE = [
        ("帮助",     ["help", "菜单", "指令"], "",            "看这张指令表",                     "基础", False),
        ("状态",     ["恶魔状态"],            "",            "运行状态总览",                     "基础", False),
        ("自检",     ["诊断"],                "",            "逐项体检，插件出问题先发这个",       "基础", False),
        ("版本",     ["ver"],                 "",            "看插件版本和已加载的模块",          "基础", False),
        ("token",    ["用量", "tokens"],      "",            "看今天消耗的 token",               "基础", False),
        ("发送日志", ["日志", "运行日志"],     "500/全部",     "私聊把运行日志发给你（仅管理员，仅私聊）", "基础", True),
        ("撤回",     ["删除刚才"],            "",            "引用 bot 消息并发送 /撤回 自动撤回", "其他", False),
        ("诗",       ["诗句"],                  "",            "来一句古诗文；由本地诗句库生成",       "其他", False),
        ("点赞",     ["赞我"],                 "| 同意5 QQ号 | 撤销 QQ号 | 列表", "主人可立即点赞；可授权好友每日 1~10 赞", "其他", False),

        ("梗",       ["查梗"],                "676767",      "联网查一个梗，查完入库",            "词库", False),
        ("教梗",     [],                      "词=解释",      "手动录入，优先级高于联网结果",       "词库", False),
        ("忘梗",     [],                      "词",           "从词库删掉一个词",                 "词库", False),

        ("搜",       ["搜索"],                "关键词",       "直接联网搜一次并给出摘要",          "联网", False),
        ("联网测试", ["网络测试"],            "",            "逐个体检搜索后端，报告哪个能通",     "联网", False),
        ("热点",     ["热搜"],                "",            "刷新并查看当前热搜",               "联网", False),

        # 一级菜单：这些名字本身不执行具体功能，无参数时只展开二级菜单。
        ("插图",     ["图片菜单", "图菜单"], "",            "展开插图相关指令",                  "菜单", False),
        ("表情",     ["发表情", "表情包"],     "",            "展开表情相关指令；带参数仍可直接发图", "菜单", False),
        ("词库",     ["词库菜单"],            "",            "展开词库相关指令；带参数仍可查询", "菜单", False),
        ("联网",     ["联网菜单", "网络"],    "",            "展开联网相关指令",                  "菜单", False),
        ("群友",     ["成员"],                "",            "展开群友相关指令；带参数仍可查人", "菜单", False),
        ("生活",     ["生活菜单"],            "",            "展开生活相关指令",                  "菜单", False),
        ("文案",     ["来句", "语录"],        "",            "展开文案相关指令；带参数仍可直接取文案", "菜单", False),
        ("多模态",   ["多模态菜单"],          "",            "展开识图、语音相关指令",              "菜单", False),
        ("人格",     ["人格菜单"],            "",            "展开人格、风格、心情相关指令",        "菜单", False),
        ("记忆",     ["记忆菜单"],            "",            "展开记忆相关指令；带参数仍可直接使用", "菜单", False),
        ("控制",     ["控制菜单"],            "",            "展开控制、开关、省钱相关指令",        "菜单", False),
        ("其他指令", ["其它指令", "其他", "更多"], "",       "展开低频、诊断和管理员指令",          "菜单", False),

        ("图",       ["来图", "图片", "看看"], "腿/真腿/好腿", "按标签取图（真=真人，好=R18且需私聊解锁）", "插图", False),
        ("图源测试", [],                      "",            "体检图源，并显示取到图的规格尺寸",   "插图", False),
        ("标签",     [],                      "腿",           "查某个词会被映射成哪些 Pixiv 标签", "插图", False),
        ("记住标签", ["记标签", "学标签"],     "校服=制服",     "自定义中文词→Pixiv标签，立刻生效并永久保存", "插图", False),
        ("忘记标签", ["删标签", "忘标签"],     "校服",         "删掉一条自定义标签映射",            "插图", False),
        ("标签表",   ["映射表", "标签列表"],   "",            "看所有自定义标签映射",              "插图", False),
        ("榜单",     ["月榜", "排行"],        "",            "看当前 Pixiv 榜单状态和取图链路",    "插图", False),
        ("上一张",   ["图信息"],              "",            "看上一张图的作者、pid、尺寸",       "插图", False),
        ("age>18",   ["开启R18", "开启成人"], "",            "私聊开启本账号 R18 分区（仅私聊）", "插图", False),
        ("age<18",   ["关闭R18", "关闭成人"], "",            "关闭本账号 R18 分区",               "插图", False),

        ("查人",     [],                      "笙歌",         "查某个群友的档案",                 "群友", False),
        ("备注",     [],                      "昵称=内容",    "给某个群友加备注",                 "群友", False),
        ("忘了他",   [],                      "昵称",         "清空某人的备注",                   "群友", True),

        ("表情测试", [],                      "",            "看表情库装了多少张、情绪对得上吗",   "表情", False),

        ("作息",     ["日程", "时间线"],       "",            "看它今天的作息时间线",              "生活", False),
        ("在干嘛",   ["状态查询"],            "",            "问它此刻在做什么",                 "生活", False),
        ("重排作息", [],                      "",            "重新掷一次今天的作息（管理员）",     "生活", True),
        ("主人",     [],                      "查看/加 QQ号", "看/设置谁是主人",                  "生活", False),

        ("文案测试", [],                      "",            "体检文案接口，看你的网络能通哪几家", "文案", False),

        ("识图",     ["看图", "这是什么"],     "（回复或附带图片）", "让它看懂你发的图",             "多模态", False),
        ("说",       ["语音", "念"],          "文本",         "把一段话用语音发出来",              "多模态", False),

        ("学风格",   [],                      "",            "立刻重学一遍群里的说话风格",         "人格", False),
        ("风格",     [],                      "",            "看当前学到的风格速记",              "人格", False),
        ("重置风格", [],                      "",            "清掉风格速记，下次重新学",           "人格", True),
        ("心情",     [],                      "",            "看当前心情值和时段状态",            "人格", False),
        ("发送人格", ["人格文件", "看人格"],     "",            "私聊发送当前长期自我档案",            "人格", False),
        ("添加人格", [],                       "内容",         "追加长期自我信息",                  "人格", True),
        ("替换人格", [],                       "完整内容",      "替换整份长期自我档案",              "人格", True),
        ("记住信息", ["记住自己"],             "内容",         "写入长期自我档案",                  "人格", True),

        ("记住",     [],                      "内容",         "让它记住一件事",                   "记忆", False),
        ("回忆",     [],                      "关键词",       "翻出记住过的事",                   "记忆", False),

        ("闭嘴",     ["安静"],                "30",           "让它安静几分钟",                   "控制", False),
        ("解禁",     ["复活"],                "",            "清除所有静音状态",                 "控制", True),
        ("冷却",     [],                      "60",           "改主动插话的最小间隔（秒）",        "控制", True),
        ("概率",     [],                      "0.1",          "改普通群友发言后的插话概率",        "控制", True),
        ("开关",     ["设置"],                "分辨率 中",     "分级开关：发言/插图/画质/冷却/省钱等", "控制", True),
        ("省钱",     ["用量", "省流"],        "",            "看高峰时段、省流状态和已省下的请求", "控制", False),
    ]

    PLUGIN_VERSION = "v2.9.3"

    def _command_names(self) -> list:
        names = []
        for name, aliases, *_ in self.COMMAND_TABLE:
            names.append(name)
            names.extend(aliases)
        return names

    def _canonical_command(self, word: str) -> str:
        for name, aliases, *_ in self.COMMAND_TABLE:
            if word == name or word in aliases:
                return name
        return ""

    def _raw_text(self, event: AstrMessageEvent) -> str:
        """直接从消息段里取原始文本。

        不能只信 event.message_str——框架在唤醒检查阶段可能已经把 / 前缀吃掉了，
        那样 startswith("/") 永远为假，指令就永远匹配不上。
        """
        try:
            parts = [
                seg.text for seg in event.message_obj.message if isinstance(seg, Comp.Plain)
            ]
        except Exception:
            parts = []
        return _CTRL_RE.sub("", "".join(parts)).strip()

    def _parse_command(self, event: AstrMessageEvent):
        """返回 (指令名, 参数, 是否命中前缀但指令名不认识)。"""
        prefixes = self._cfg("commands", "prefixes", default=None) or ["/", "=="]
        candidates = [self._raw_text(event), _CTRL_RE.sub("", event.message_str or "").strip()]
        for text in candidates:
            if not text:
                continue
            for prefix in sorted([p for p in prefixes if p], key=len, reverse=True):
                if not text.startswith(prefix):
                    continue
                rest = text[len(prefix):].strip()
                if not rest:
                    continue
                for word in sorted(self._command_names(), key=len, reverse=True):
                    if rest.startswith(word):
                        arg = rest[len(word):].strip().lstrip(":：= 　")
                        return self._canonical_command(word), arg, False
                return "", rest, True
        return None, "", False

    def _command_is_admin_only(self, name: str) -> bool:
        for cmd, _a, _p, _d, _c, admin_only in self.COMMAND_TABLE:
            if cmd == name:
                return admin_only
        return False

    async def _dispatch_command(self, event: AstrMessageEvent, name: str, arg: str):
        """返回要发送的内容（字符串或消息段列表），None 表示不发。"""
        group_id = event.get_group_id() or "private"
        is_admin = self._is_admin(event.get_sender_id())

        if self._command_is_admin_only(name) and self.admin_ids and not is_admin:
            return "这个只有管理员能用"

        # ---------- 基础 ----------
        if name == "帮助":
            return self._cmd_help()
        # 一级分类菜单：无参数时展开二级菜单；有参数时交给原业务指令继续处理。
        category_menu_names = {
            "插图": "插图", "表情": "表情", "词库": "词库", "联网": "联网",
            "群友": "群友", "生活": "生活", "文案": "文案", "多模态": "多模态",
            "人格": "人格", "记忆": "记忆", "控制": "控制", "其他指令": "其他指令",
        }
        if name in category_menu_names and not arg.strip():
            return self._cmd_category_menu(category_menu_names[name])
        if name == "状态":
            return self._cmd_status(group_id)
        if name == "自检":
            return await self._cmd_selfcheck(event, group_id)
        if name == "token":
            return self._cmd_token_usage()
        if name == "点赞":
            return await self._cmd_like(event, arg)
        if name == "撤回":
            return await self._recall_replied_message(event)
        if name == "诗":
            return self._poetry_pick_text()
        if name == "版本":
            mods = [
                f"联网模块 {'已加载' if websearch else '缺失'}",
                f"插图模块 {'已加载' if images else '缺失'}",
                f"生活状态 {'已加载' if life else '缺失'}",
                f"表情模块 {'已加载' if stickers else '缺失'}（{self._sticker_box.count if self._sticker_box else 0} 张）",
                f"文案模块 {'已加载' if quotes else '缺失'}",
                f"LLM工具 {'已注册' if self._tools_ready else '未注册'}",
            ]
            return f"恶魔bot {self.PLUGIN_VERSION}\n" + "\n".join(mods) + (
                f"\n启动告警：{len(self._boot_errors)} 条（发 /自检 看详情）" if self._boot_errors else ""
            )
        if name == "发送日志":
            if event.get_group_id():
                return "这个指令只能私聊里用，群里问了也不给"
            return await self._cmd_send_log(event, arg)
        # ---------- 词库 ----------
        if name == "梗":
            if not arg:
                return "用法：/梗 676767"
            cached = self.slang_get(arg)
            desc = await self.lookup_slang(arg, event, force=True)
            if desc:
                return f"{arg}（{'词库' if cached else '刚查的'}）：{desc}"
            return f"「{arg}」没查到。可以 /教梗 {arg}=你的解释，或先发 /联网测试 看看网通不通"

        if name == "教梗":
            sep = "=" if "=" in arg else ("＝" if "＝" in arg else "")
            if not sep:
                return "用法：/教梗 词=解释"
            term, desc = arg.split(sep, 1)
            if not term.strip() or not desc.strip():
                return "词或解释是空的"
            self.slang_put(term.strip(), desc.strip(), source="manual")
            return f"记住了：{term.strip()} = {desc.strip()[:60]}"

        if name == "忘梗":
            if not arg:
                return "用法：/忘梗 词"
            if self._slang_cache().pop(arg.strip().lower(), None) is None:
                return f"词库里没有「{arg}」"
            self._save_json(self.knowledge_file, self._knowledge)
            return f"已从词库删掉「{arg}」"

        if name == "词库":
            if not arg.strip():
                return self._cmd_category_menu("词库")
            if arg.strip().lower() in ("列表", "list"):
                arg = ""
            cache = self._slang_cache()
            if not cache:
                return "词库还是空的"
            items = sorted(cache.values(), key=lambda v: v.get("time", 0), reverse=True)[:12]
            return f"词库共 {len(cache)} 条，最近的：\n" + "\n".join(
                f"{v.get('term')}：{v.get('desc')}" for v in items
            )

        # ---------- 联网 ----------
        if name == "搜":
            if not arg:
                return "用法：/搜 关键词"
            results = await self.search_web_raw(arg)
            if not results:
                return "一条都没搜到，发 /联网测试 看看是哪个环节断了"
            return "搜到了：\n" + "\n\n".join(
                f"[{r.get('source','')}] {r.get('title','')[:30]}\n{r.get('snippet','')[:80]}"
                for r in results[:3]
            )

        if name == "联网测试":
            if websearch is None:
                return "联网模块没加载（websearch.py 缺失或报错），发 /自检 看详情"
            report = await websearch.diagnose(
                query=arg or "梗",
                backends=self._cfg("knowledge", "backends", default=None) or websearch.DEFAULT_BACKENDS,
                timeout=self._cfg("knowledge", "http_timeout_seconds", default=8),
                api_keys={
                    "bocha": self._cfg("knowledge", "bocha_api_key", default=""),
                    "tavily": self._cfg("knowledge", "tavily_api_key", default=""),
                },
            )
            lines = [f"{'可用' if ok else '不通'} {n}：{msg}" for n, ok, msg in report]
            usable = [n for n, ok, _ in report if ok]
            tail = (
                f"\n\n可用的：{'、'.join(usable)}，建议把它们排到 backends 最前面。"
                if usable
                else "\n\n全都不通，说明手机网络访问不了这些站。去 bochaai.com 申请 key 填进 "
                     "knowledge.bocha_api_key，再把 bocha 放到 backends 第一位。"
            )
            return "搜索后端体检：\n" + "\n".join(lines) + tail

        if name == "热点":
            items = await self.refresh_hot_topics(force=True)
            if not items:
                return "热搜抓不到，发 /联网测试 看看网络"
            return "现在的热搜：\n" + "\n".join(f"{i+1}. {w}" for i, w in enumerate(items))

        # ---------- 插图 ----------
        if name == "图":
            if images is None:
                return "插图模块没加载（images.py 缺失或报错），发 /自检 看详情"
            sender_id = str(event.get_sender_id())
            is_private = not bool(event.get_group_id())
            is_real, mature_mode, keyword = self._parse_image_intent(arg or "")
            chain = await self.fetch_image_chain(
                arg or keyword,
                is_real=is_real,
                mature_mode=mature_mode,
                user_id=sender_id,
                is_private=is_private,
                event=event,
                group_hint=event.get_group_id() or "",
            )
            if chain == "MATURE_PRIVATE_ONLY":
                return "R18 分区只能私聊获取（或把群号加进 mature_group_whitelist）。"
            if chain == "NEED_R18_UNLOCK":
                return "R18 未开启。请先私聊我发送 /age>18。"
            if not chain:
                return "没取到图。发 /图源测试 看是网络问题还是这个标签真没图"
            # 多 p 走合并转发，需要自己发（转发消息不能靠 chain_result 直接 yield 兜底）
            if await self._send_illust(event, group_id, chain):
                now = time.time()
                self._last_image_at[group_id] = now
                self._last_image_user_at[f"{group_id}:{sender_id}"] = now
                return ""
            return "图取到了但发不出去，可能被平台拦了，看看日志"

        if name == "age>18":
            # 仅私聊生效，避免在群里误开
            try:
                gid = event.get_group_id()
            except Exception:
                gid = None
            if gid and str(gid) not in ("", "0", "None"):
                return "请私聊我发送 /age>18 开启成人分区（群聊里无效，防止误触）"
            self._set_r18_unlocked(event.get_sender_id(), True)
            return (
                "已为你开启 R18 分区。\n"
                "私聊可用「好」请求成人图，例如：\n"
                "  /看看好腿  → Pixiv R18\n"
                "  /看看好真腿 → 真人 R18\n"
                "不含「好」仍是全年龄/擦边。关闭请发 /age<18"
            )

        if name == "age<18":
            self._set_r18_unlocked(event.get_sender_id(), False)
            return "已关闭你的 R18 分区"

        if name == "图源测试":
            if images is None:
                return "插图模块没加载（images.py 缺失或报错），发 /自检 看详情"
            return await self._cmd_image_test(arg)

        if name == "标签":
            if not arg:
                return "用法：/标签 腿"
            word = arg.strip()
            tags = await self._resolve_image_tags(word)
            if word in self._img_tags:
                note = "（自定义映射，/记住标签 存的）"
            elif word in self._auto_img_tags:
                note = "（Pixiv 自动发现，已缓存）"
            elif self._cfg("images", "keyword_map", default=None) and self._image_keyword_map().get(word):
                note = "（内置映射）"
            else:
                note = "（Pixiv 自动发现）"
            return f"「{word}」{note}\n实际搜索标签：{'、'.join(tags) if tags else '（空，会随机发图）'}"


        if name == "记住标签":
            sep = "=" if "=" in arg else ("＝" if "＝" in arg else "")
            if not sep:
                return "用法：/记住标签 校服=制服\n多个同义标签用 | 分隔，例：/记住标签 校服=制服|スクール水着"
            word, value = arg.split(sep, 1)
            word, value = word.strip(), value.strip()
            if not word or not value:
                return "词或标签是空的，格式：/记住标签 校服=制服"
            tags = [x.strip() for x in re.split(r"[|｜、,，\s]+", value) if x.strip()][:4]
            if not tags:
                return "没解析出有效标签，格式：/记住标签 校服=制服"
            self._img_tags[word] = tags
            self._save_json(self.imgtag_file, self._img_tags)
            return (
                f"记住了：{word} → {'、'.join(tags)}\n"
                f"以后发「/看看{word}」就按这个标签去 Pixiv 榜单里找图"
            )

        if name == "忘记标签":
            word = (arg or "").strip()
            if not word:
                return "用法：/忘记标签 校服"
            if self._img_tags.pop(word, None) is None:
                return f"自定义映射里没有「{word}」（内置映射删不掉，可以用 /记住标签 覆盖它）"
            self._save_json(self.imgtag_file, self._img_tags)
            return f"已删掉自定义映射「{word}」"

        if name == "标签表":
            if not self._img_tags:
                return "还没存过自定义标签。用法：/记住标签 校服=制服"
            lines = [f"{k} → {'、'.join(v)}" for k, v in list(self._img_tags.items())[:30]]
            return f"自定义标签映射（共 {len(self._img_tags)} 条）：\n" + "\n".join(lines)

        if name == "榜单":
            if images is None:
                return "插图模块没加载（images.py 缺失或报错），发 /自检 看详情"
            return await self._cmd_rank_status(arg)

        if name == "上一张":
            pic = getattr(self, "_last_image_info", None)
            if not pic:
                return "还没发过图"
            extra = ""
            if pic.get("rank"):
                extra += f"｜榜单第 {pic.get('rank')} 名"
            if pic.get("bookmarks"):
                extra += f"｜{pic.get('bookmarks')} 收藏"
            out = [
                f"来源：{pic.get('source')}｜规格：{pic.get('size_used')}{extra}",
                f"尺寸：{pic.get('width')}x{pic.get('height')}｜{pic.get('page_count', 1)} p",
                f"标题：{pic.get('title','')[:30]}｜作者：{pic.get('author','')[:20]}",
                f"pid：{pic.get('pid')}",
            ]
            if pic.get("pid"):
                out.append(f"原作品：https://www.pixiv.net/artworks/{pic.get('pid')}")
            return "\n".join(out)

        # ---------- 表情包 ----------
        if name == "表情":
            if stickers is None:
                return "表情模块没加载（stickers.py 缺失或报错），发 /自检 看详情"
            arg = (arg or "").strip()
            if arg in ("重载", "刷新", "reload"):
                n = self._reload_stickers()
                return f"重新扫了一遍，现在有 {n} 张表情。目录：{self._sticker_dir()}"
            if arg == "":
                return self._cmd_category_menu("表情")
            if arg in ("列表", "list"):
                if not self._sticker_box or not self._sticker_box.count:
                    return (
                        f"表情库是空的。把 GIF 丢进这个目录再发 /表情 重载：\n{self._sticker_dir()}\n"
                        f"文件名不用改，「蓝色大肥鱼_哭 1_2026-08-18-14-07-00.gif」这种直接放就行"
                    )
                names = self._sticker_box.names()
                sample = "、".join(names[:18])
                return (
                    f"表情库共 {len(names)} 张，例如：{sample}…\n"
                    f"可用情绪：{'、'.join(stickers.emotion_keys())}\n"
                    f"用法：/表情 哭    /表情 挨骂    /表情 重载"
                )
            made = self._sticker_chain(arg)
            if not made:
                return f"没找到跟「{arg}」对得上的表情，发 /表情 列表 看看都有啥"
            chain, sname = made
            if await self._safe_send(event, event.chain_result(chain), group_id):
                logger.info(f"[恶魔bot] 手动发表情：{sname}")
                return ""
            return "表情发不出去，可能被平台拦了"

        if name == "表情测试":
            if stickers is None:
                return "表情模块没加载"
            if not self._sticker_box:
                return "表情库没初始化"
            lines = [
                f"目录：{self._sticker_dir()}",
                f"已索引：{self._sticker_box.count} 张",
                f"发送概率：{self._cfg('stickers', 'chance', default=0.9)}｜"
                f"冷却 {self._cfg('stickers', 'cooldown_seconds', default=180)} 秒｜"
                f"每天上限 {self._cfg('stickers', 'daily_limit', default=40)} 张",
                "",
                "各情绪能不能取到图：",
            ]
            for key in stickers.emotion_keys():
                hit = self._sticker_box.pick_by_emotion(key)
                lines.append(f"  {key}：{('✓ ' + hit[0]) if hit else '✗ 库里没有对应的图'}")
            return "\n".join(lines)

        # ---------- 生活状态 ----------
        if name == "作息":
            if self._life is None:
                return "生活状态模块没加载（life.py 缺失或报错），发 /自检 看详情"
            self._life_now()          # 确保今天的时间线已生成
            weekend = "周末" if self._life_is_weekend() else "工作日"
            return f"今天（{self._life_day_key()}，{weekend}）的作息：\n" + self._life.schedule_text()

        if name == "在干嘛":
            st = self._life_now()
            if not st:
                return "生活状态没开，去 WebUI 里把 life.enabled 打开"
            score = float(self._mood.get("score", 0.0))
            extra = f"\n今天{st['event']}" if st.get("event") else ""
            return (
                f"现在{st['label']}：{st['activity']}\n"
                f"{life.mood_label(score) if life else ''}"
                f"｜忙碌度 {st['busy']:.0%}｜{'在睡' if st['sleeping'] else '醒着'}{extra}"
            )

        if name == "重排作息":
            if self._life is None:
                return "生活状态模块没加载"
            self._life.build(self._life_day_key(), self._life_is_weekend())
            return "今天的作息重掷了一遍：\n" + self._life.schedule_text()

        if name == "主人":
            parts = (arg or "").split()
            if parts and parts[0] in ("加", "add", "设为") and len(parts) > 1:
                if self.admin_ids and not is_admin:
                    return "只有管理员能改主人名单"
                target = parts[1].strip()
                self.owner_ids.add(target)
                self.admin_ids.add(target)
                self.config.setdefault("owner", {})["qq"] = sorted(self.owner_ids)
                self._persist_config()
                return f"好，{target} 也是我{self._owner_title()}了"
            title = self._owner_title()
            qqs = "、".join(sorted(self.owner_ids)) or "（没配）"
            names = "、".join(sorted(self.owner_names)) or "（没配）"
            me = "，而且就是你" if self._event_is_owner(event) else ""
            return (
                f"我的{title}：\nQQ：{qqs}\n昵称关键词：{names}{me}\n"
                f"（改名单：/主人 加 123456，或去 WebUI 的 owner 分组）"
            )

        # ---------- 文案 ----------
        if name == "文案":
            if not arg.strip():
                return self._cmd_category_menu("文案")
            if quotes is None:
                return "文案模块没加载（quotes.py 缺失或报错），发 /自检 看详情"
            kind = quotes.normalize_kind(arg or "一言")
            text, src = await quotes.fetch_one(
                kind,
                timeout=self._cfg("quotes", "timeout_seconds", default=8),
                max_chars=self._cfg("quotes", "max_chars", default=60),
                logger=logger,
            )
            tail = "" if src != "本地" else "（接口都没通，这条是本地库的）"
            return f"{text}{tail}"

        if name == "文案测试":
            if quotes is None:
                return "文案模块没加载"
            report = await quotes.diagnose(
                timeout=self._cfg("quotes", "timeout_seconds", default=8)
            )
            ok = [f"{k}/{n}" for k, n, good, _ in report if good]
            bad = [f"{k}/{n}：{msg}" for k, n, good, msg in report if not good]
            lines = ["文案接口体检："]
            lines.append("可用：" + ("、".join(ok) if ok else "一个都没通"))
            if bad:
                lines.append("不通：")
                lines.extend(f"  {x}" for x in bad[:10])
            if not ok:
                lines.append(
                    "\n全都不通的话，多半是这台机器的 DNS 出不去（你日志里就有一条"
                    "「Name or service not known」）。可以给 Termux 换个 DNS，"
                    "或者就这么用——没网时会自动走本地句库。"
                )
            return "\n".join(lines)

        # ---------- 多模态 ----------
        if name == "识图":
            urls = self._collect_image_urls(event)
            if not urls:
                return "没看到图。把图和 /识图 一起发，或者回复那张图再发 /识图"
            answer = await self._vision_describe(event, urls, arg or "")
            if answer:
                return answer
            lines = getattr(responses, "VISION_NOT_READY", []) if responses else []
            return random.choice(lines) if lines else "这张图我现在还看不懂，给当前会话换个支持视觉的模型，我再认真看。"

        if name == "说":
            if not arg:
                return "用法：/说 今天天气不错"
            ok = await self._send_voice(event, group_id, arg.strip())
            if ok:
                return ""
            lines = getattr(responses, "TTS_NOT_READY", []) if responses else []
            return random.choice(lines) if lines else "我也想给你开口说话，可惜嗓子还没接上，主人有空给我配个 TTS。"

        # ---------- 群友 ----------
        if name == "群友":
            if not arg.strip():
                return self._cmd_category_menu("群友")
            roster = self.member_roster(group_id, 20)
            return ("本群群友档案：\n" + roster) if roster else "还没记录到人"

        if name == "查人":
            if not arg:
                return "用法：/查人 笙歌"
            rec = self.find_member(group_id, arg)
            if not rec:
                return f"没找到「{arg}」"
            names = "、".join(rec.get("names", [])) or "（无）"
            notes = "；".join(rec.get("notes", [])) or "（无）"
            return (
                f"{rec.get('code')} {names}\nQQ：{rec.get('qq')}｜发言 {rec.get('count', 0)} 条\n"
                f"{'群主人' if rec.get('is_admin') else '普通群友'}\n备注：{notes}"
            )

        if name == "备注":
            sep = "=" if "=" in arg else ("＝" if "＝" in arg else "")
            if not sep:
                return "用法：/备注 昵称=要记住的事"
            who, note = arg.split(sep, 1)
            return self.note_member(group_id, who.strip(), note.strip())

        if name == "忘了他":
            rec = self.find_member(group_id, arg)
            if not rec:
                return f"没找到「{arg}」"
            rec["notes"] = []
            self._save_json(self.members_file, self._members)
            return f"已清空 {rec.get('code')} 的备注"

        # ---------- 人格 ----------
        if name == "学风格":
            profile = await self.update_style(group_id, event, force=True)
            return ("学完了：\n" + profile) if profile else "样本太少，多聊几句再来"

        if name == "风格":
            profile = self._style.get("profile", "")
            if not profile:
                return "还没学过，发 /学风格 学一次"
            ts = time.strftime("%m-%d %H:%M", time.localtime(self._style.get("updated", 0)))
            return f"（{ts} 更新）\n{profile}"

        if name == "重置风格":
            self._style = {"profile": "", "updated": 0}
            self._save_json(self.style_file, self._style)
            return "风格速记已清空，下次回复时会重新学"

        if name == "发送人格":
            if event.get_group_id():
                return "人格档案只在私聊发"
            self._ensure_persona_file()
            try:
                if await self._send_local_file(event, self.persona_file, "jiao_tang_persona.md"):
                    return "人格档案发给你了"
            except Exception:
                pass
            return [Comp.File(file=str(self.persona_file), name="jiao_tang_persona.md")]

        if name in ("添加人格", "记住信息"):
            if not is_admin:
                return "这个只让主人改"
            if not arg.strip():
                return f"用法：/{name} 内容"
            if name == "记住信息":
                content = self._persona.rstrip() + "\n\n## 主人补充的长期信息\n- " + arg.strip()
            else:
                content = self._persona.rstrip() + "\n- " + arg.strip()
            self._save_persona(content)
            return "已经写进长期自我档案了"

        if name == "替换人格":
            if not is_admin:
                return "这个只让主人改"
            if not arg.strip():
                return "用法：/替换人格 完整人格内容"
            self._save_persona(arg.strip())
            return "旧人格已经替换成新的了"

        if name == "心情":
            score = float(self._mood.get("score", 0.0))
            info = self._mood_baseline_for_hour(time.localtime().tm_hour)
            return (
                f"心情值：{score:+.2f}（{self._mood_feel_label(score)}）\n"
                f"当前时段：{info['label']}，基线 {info['baseline']:+.2f}\n"
                f"设定活动：{info['activity']}（只有被问起才会说出口）"
            )

        # ---------- 记忆 ----------
        if name == "记住":
            if not arg:
                return "用法：/记住 内容"
            scope = f"group:{group_id}" if event.get_group_id() else f"user:{event.get_sender_id()}"
            self.remember(scope, arg.strip())
            return "记住了"

        if name == "回忆":
            scope = f"group:{group_id}" if event.get_group_id() else f"user:{event.get_sender_id()}"
            records = self.recall(scope, query=arg.strip(), limit=8)
            if not records:
                return "没找到相关的记忆"
            return "记得这些：\n" + "\n".join(f"- {m['content'][:50]}" for m in records)

        # ---------- 控制 ----------
        if name == "闭嘴":
            m = re.search(r"\d+", arg or "")
            minutes = int(m.group()) if m else self._cfg("mute", "duration_minutes", default=10)
            self._muted_until[group_id] = time.time() + minutes * 60
            self._muted_by[group_id] = str(event.get_sender_id())
            return f"好，闭嘴{minutes}分钟"

        if name == "解禁":
            self.force_unmute(group_id)
            return "已清除所有静音状态"

        if name == "冷却":
            m = re.search(r"\d+", arg or "")
            if not m:
                cur = self._cfg("reply_gate", "min_reply_interval_seconds", default=45)
                return f"当前插话冷却 {cur} 秒。用法：/冷却 60"
            seconds = int(m.group())
            self.config.setdefault("reply_gate", {})["min_reply_interval_seconds"] = seconds
            self._persist_config()
            return f"插话冷却改成 {seconds} 秒"

        if name == "概率":
            m = re.search(r"[\d.]+", arg or "")
            if not m:
                cur = self._cfg("reply_gate", "random_reply_chance", default=0.05)
                return f"当前插话概率 {cur}。用法：/概率 0.1"
            try:
                value = max(0.0, min(1.0, float(m.group())))
            except ValueError:
                return "数字看不懂，比如 /概率 0.1"
            self.config.setdefault("reply_gate", {})["random_reply_chance"] = value
            self._persist_config()
            return f"普通群友的插话概率改成 {value}"

        if name == "开关":
            return self._cmd_toggle(arg)

        if name == "省钱":
            return self._cmd_saver()

        return None

    # 分级开关表：(分类, 名字, 别名, 类型, 配置路径, 说明, 取值)
    # 类型 bool=开/关，enum=固定几档，int=数字，float=小数
    TOGGLE_TABLE = [
        ("发言", "发言",   ["说话", "总开关"], "bool",  ("reply_gate", "speak"),
         "总开关，关了除指令外一个字都不说", None),
        ("发言", "插话",   [],                 "bool",  ("reply_gate", "enabled"),
         "没被@时要不要主动接话", None),
        ("发言", "回复群友", ["群友"],         "bool",  ("reply_gate", "reply_to_members"),
         "搭不搭理普通群友", None),
        ("发言", "回复管理员", ["管理员", "回复管理"], "bool", ("reply_gate", "reply_to_admin"),
         "搭不搭理管理员", None),
        ("发言", "字数",   ["回复字数"],       "int",   ("reply_style", "max_chars"),
         "单条回复最多几个字", (8, 200)),
        ("发言", "冷却",   ["插话冷却"],       "int",   ("reply_gate", "min_reply_interval_seconds"),
         "两次主动插话的最小间隔（秒）", (0, 3600)),

        ("插图", "发插图", ["插图", "图"],     "bool",  ("images", "enabled"),
         "能不能发插图", None),
        ("插图", "分辨率", ["画质", "品质"],   "enum",  ("images", "quality"),
         "插图画质档位", ["原图", "高", "中", "低"]),
        ("插图", "真人图", ["真人"],           "bool",  ("images", "real_enabled"),
         "能不能发真人图", None),
        ("插图", "图冷却", ["发图冷却"],       "int",   ("images", "per_user_cooldown_seconds"),
         "普通用户两张图之间要等几秒", (0, 86400)),
        ("插图", "群图冷却", [],               "int",   ("images", "cooldown_seconds"),
         "整个群两张图之间要等几秒", (0, 86400)),
        ("插图", "管理员免冷却", ["免冷却"],   "bool",  ("images", "admin_bypass_cooldown"),
         "管理员要图是否不受冷却限制", None),
        ("插图", "走榜单", ["排行榜", "月榜"], "bool",  ("images", "prefer_ranking"),
         "优先从 Pixiv 排行榜取图（画师水平最有保证）", None),
        ("插图", "榜单页数", ["榜单范围"],     "int",   ("images", "rank_pages"),
         "不带标签时从榜单前几页里抽（1页=50名，2=前100）", (1, 10)),
        ("插图", "多p全发", ["多p", "整套"],   "bool",  ("images", "send_all_pages"),
         "多 p 作品是不是整套发出来", None),
        ("插图", "合并转发", ["转发"],         "bool",  ("images", "forward_multi_page"),
         "多 p 用「聊天记录」形式发，避免刷屏", None),
        ("插图", "多p上限", [],                "int",   ("images", "max_pages_per_illust"),
         "一套图最多发几张", (1, 60)),

        ("省钱", "高峰省钱", ["高峰", "省钱"], "bool",  ("peak_hours", "enabled"),
         "高峰时段（双倍计费）不主动开口", None),
        ("省钱", "高峰冷却", [],               "int",   ("peak_hours", "at_reply_cooldown_seconds"),
         "高峰期被@后要等几秒才再理人", (0, 3600)),
        ("省钱", "省流",   ["瘦身", "省token"], "bool", ("token_saver", "enabled"),
         "裁剪上下文，省 token 的主力开关", None),
        ("省钱", "上下文", ["记忆条数"],       "int",   ("token_saver", "max_context_messages"),
         "每次带给模型的历史消息条数", (0, 50)),
        ("省钱", "每日额度", ["额度"],         "int",   ("token_saver", "daily_aux_call_limit"),
         "后台请求每天最多几次（0=不限）", (0, 2000)),

        ("功能", "联网",   ["学梗"],           "bool",  ("knowledge", "enabled"),
         "自动上网查不认识的新梗", None),
        ("功能", "热搜",   [],                 "bool",  ("knowledge", "inject_hot_topics"),
         "把热搜塞进提示词（很费token）", None),
        ("功能", "学风格", ["风格"],           "bool",  ("style", "enabled"),
         "定时归纳群里的说话风格", None),
        ("功能", "群友表", ["名单"],           "bool",  ("members", "inject_roster"),
         "把群友编号表塞进提示词", None),
        ("功能", "心情",   [],                 "bool",  ("mood", "enabled"),
         "心情/时段状态", None),
        ("功能", "复读",   [],                 "bool",  ("repeat", "enabled"),
         "跟着群里一起复读", None),
        ("功能", "分段",   [],                 "bool",  ("segment_reply", "enabled"),
         "长回复拆成多条发（建议关）", None),
        ("功能", "结束语", [],                 "bool",  ("end_talk", "enabled"),
         "收到「嗯嗯」「好的」就闭麦", None),
        ("功能", "跳过图片", [],               "bool",  ("media_skip", "enabled"),
         "别人发图时不接话", None),
    ]

    _ON_WORDS = {"开", "on", "1", "true", "启用", "打开", "开启", "是"}
    _OFF_WORDS = {"关", "off", "0", "false", "禁用", "关闭", "否"}

    def _find_toggle(self, word: str):
        for row in self.TOGGLE_TABLE:
            _cat, name, aliases, *_ = row
            if word == name or word in aliases:
                return row
        return None

    def _toggle_display(self, row) -> str:
        """把某一项的当前值显示成人话。"""
        _cat, name, _al, kind, (sec, key), _desc, spec = row
        if kind == "bool":
            default = False if key in ("inject_hot_topics",) else True
            return "开" if self._cfg(sec, key, default=default) else "关"
        if kind == "enum":
            raw = self._cfg(sec, key, default="regular")
            if sec == "images" and key == "quality" and images is not None:
                return images.quality_label(raw)
            return str(raw)
        return str(self._cfg(sec, key, default="?"))

    def _cmd_toggle(self, arg: str) -> str:
        prefix = (self._cfg("commands", "prefixes", default=None) or ["/"])[0]
        parts = (arg or "").split()

        # ---------- /开关：列出全部分级指令 ----------
        if not parts:
            by_cat = {}
            for row in self.TOGGLE_TABLE:
                cat, name, _al, kind, _path, desc, spec = row
                cur = self._toggle_display(row)
                if kind == "bool":
                    hint = "开/关"
                elif kind == "enum":
                    hint = "/".join(spec or [])
                else:
                    hint = f"{spec[0]}~{spec[1]}" if spec else "数字"
                by_cat.setdefault(cat, []).append(
                    f"{prefix}开关 {name} [{hint}]  现在：{cur}\n    └ {desc}"
                )
            out = [f"恶魔bot 开关总表（用法：{prefix}开关 <项目> <值>）"]
            for cat, lines in by_cat.items():
                out.append(f"\n【{cat}】\n" + "\n".join(lines))
            out.append(
                f"\n例：{prefix}开关 分辨率 中    {prefix}开关 图冷却 300    {prefix}开关 真人图 关"
            )
            return "\n".join(out)

        row = self._find_toggle(parts[0])
        if row is None:
            return f"没有「{parts[0]}」这一项，发 {prefix}开关 看全部分级指令"

        _cat, name, _al, kind, (sec, key), desc, spec = row

        # ---------- /开关 某项：只看不改 ----------
        if len(parts) < 2:
            if kind == "bool":
                hint = "开/关"
            elif kind == "enum":
                hint = "/".join(spec or [])
            else:
                hint = f"{spec[0]}~{spec[1]}" if spec else "数字"
            return f"{name}：{self._toggle_display(row)}（{desc}）\n改：{prefix}开关 {name} {hint}"

        raw = parts[1]

        # ---------- 改值 ----------
        if kind == "bool":
            if raw in self._ON_WORDS:
                value = True
            elif raw in self._OFF_WORDS:
                value = False
            else:
                return f"{name} 只能填 开 或 关"
        elif kind == "enum":
            if images is not None and sec == "images" and key == "quality":
                value = images.normalize_quality(raw)
                if raw not in (spec or []) and images.quality_label(value) != raw:
                    return f"{name} 只能填：{'/'.join(spec or [])}"
            else:
                if raw not in (spec or []):
                    return f"{name} 只能填：{'/'.join(spec or [])}"
                value = raw
        else:
            try:
                value = int(float(raw))
            except ValueError:
                return f"{name} 要填数字，比如 {prefix}开关 {name} 300"
            if spec:
                lo, hi = spec
                if not (lo <= value <= hi):
                    return f"{name} 只能填 {lo}~{hi} 之间的数字"

        self.config.setdefault(sec, {})[key] = value
        self._persist_config()
        shown = self._toggle_display(row)
        extra = ""
        if kind == "int" and key == "per_user_cooldown_seconds":
            extra = f"（普通用户每 {self._fmt_seconds(value)} 才能要一张图，管理员不受限）"
        return f"{name} 已改成：{shown}{extra}"

    def _cmd_saver(self) -> str:
        """/省钱：一眼看清现在为什么不说话、省了多少。"""
        peak = self.in_peak_hours()
        windows = "、".join(
            self._cfg("peak_hours", "windows", default=None) or DEFAULT_PEAK_WINDOWS
        )
        limit = self._cfg("token_saver", "daily_aux_call_limit", default=60)
        minutes = self._bj_minutes()
        now_bj = f"{minutes // 60:02d}:{minutes % 60:02d}"
        lines = [
            f"省钱状态｜北京时间 {now_bj}",
            f"高峰时段：{windows}（这段时间接口双倍计费）",
            (
                f"当前：高峰期，只回被@的，还剩 {self._peak_ends_in()} 分钟"
                if peak else "当前：空闲时段，正常说话"
            ),
            f"上下文裁剪：{'开' if self._cfg('token_saver', 'enabled', default=True) else '关'}，"
            f"每次最多带 {self._cfg('token_saver', 'max_context_messages', default=6)} 条历史",
            f"后台请求：今天已用 {self._aux_used_today()}/{limit if limit > 0 else '不限'} 次",
            f"回复字数上限：{self._cfg('reply_style', 'max_chars', default=26)} 字",
            f"插图画质：{self._toggle_display(self._find_toggle('分辨率'))}｜"
            f"普通用户发图冷却 "
            f"{self._fmt_seconds(self._cfg('images', 'per_user_cooldown_seconds', default=300))}",
            f"本次启动以来：跳过主动发言 {self._saver_stats['peak_skipped']} 次，"
            f"拦下后台请求 {self._saver_stats['aux_blocked']} 次，"
            f"少传历史 {self._saver_stats['ctx_trimmed']} 条",
        ]
        return "\n".join(lines)

    def _persist_config(self):
        try:
            if hasattr(self.config, "save_config"):
                self.config.save_config()
        except Exception as e:
            logger.warning(f"[恶魔bot] 配置保存失败：{e}")

    # ==================== 两级菜单 ====================
    # 一级 /help 只保留最重要的 3 个诊断指令；其余功能按分类二级展开。
    MENU_GROUPS = {
        "插图": [
            "/图 关键词  按关键词取图",
            "/标签 关键词  查看 Pixiv 实际搜索标签",
            "/记住标签 词=标签  手动固定 Pixiv 标签",
            "/上一张  查看上一张图的信息",
            "/age>18  私聊开启本账号 R18 分区",
            "/age<18  关闭本账号 R18 分区",
        ],
        "表情": [
            "/表情 哭  发送指定情绪的表情包",
            "/表情 列表  查看表情库",
            "/表情 重载  重新扫描/解压表情包",
            "/表情测试  检查表情库和情绪匹配",
        ],
        "词库": [
            "/梗 词  联网查询并学习一个梗",
            "/教梗 词=解释  手动录入梗",
            "/忘梗 词  删除一个梗",
            "/词库 列表  查看最近学到的梗",
        ],
        "联网": [
            "/搜 关键词  联网搜索并摘要",
            "/联网测试  检查搜索后端",
            "/热点  查看当前热搜",
        ],
        "群友": [
            "/群友  查看本群群友编号",
            "/查人 昵称  查询群友档案",
            "/备注 昵称=内容  添加群友备注",
            "/忘了他 昵称  清空备注（管理员）",
        ],
        "生活": [
            "/作息  查看今天的作息",
            "/在干嘛  查看当前生活状态",
            "/主人  查看当前主人身份",
            "/主人 加 QQ号  增加主人（管理员）",
            "/重排作息  重新生成今天作息（管理员）",
        ],
        "文案": [
            "/文案 情话/伤感/温柔  获取一句文案",
            "/文案测试  检查文案接口",
        ],
        "多模态": [
            "/识图  让 Bot 看懂图片",
            "/说 文本  把文字转成语音",
        ],
        "人格": [
            "/学风格  重新学习群聊说话风格",
            "/风格  查看当前风格速记",
            "/心情  查看当前心情",
            "/发送人格  私聊发送长期自我档案",
            "/记住信息 内容  写入长期自我档案",
            "/添加人格 内容  追加长期人格信息",
            "/替换人格 内容  替换整份人格档案",
            "/重置风格  清除风格速记（管理员）",
        ],
        "记忆": [
            "/记住 内容  记住一件事",
            "/回忆 关键词  找回记忆",
        ],
        "控制": [
            "/闭嘴 30  让 Bot 安静几分钟",
            "/解禁  清除静音状态（管理员）",
            "/开关  查看分级开关",
            "/开关 分辨率 中  修改插图画质（管理员）",
            "/省钱  查看省钱/省流状态",
            "/冷却 60  修改主动插话冷却（管理员）",
            "/概率 0.1  修改普通群友插话概率（管理员）",
        ],
        "其他指令": [
            "/token  查看今天消耗的 token",
            "/撤回  引用 bot 消息后撤回",
            "/发送日志 500/全部  私聊发送运行日志（管理员）",
            "/图源测试  检查图源和图片规格",
            "/标签表  查看全部自定义 Pixiv 标签映射",
            "/忘记标签 词  删除自定义标签映射",
            "/榜单  查看 Pixiv 榜单状态",
            "/文案测试  检查文案接口",
            "/表情测试  检查表情库",
        ],
    }

    def _menu_prefix(self) -> str:
        return (self._cfg("commands", "prefixes", default=None) or ["/"])[0]

    def _cmd_category_menu(self, category: str) -> str:
        try:
            from . import command_menu
            lines = list(command_menu.CATEGORY_MENUS.get(category, []))
            if lines:
                p = self._menu_prefix()
                rendered = [((p + line[1:]) if line.startswith("/") else line) for line in lines]
                return f"恶魔bot｜{category}指令\n" + "\n".join(rendered) + "\n\n如需新增功能请联系主人。"
        except Exception:
            pass
        lines = self.MENU_GROUPS.get(category)
        if not lines:
            return "没有这个分类，发 /help 看目录"
        return f"恶魔bot｜{category}指令\n" + "\n".join(lines) + "\n\n如需新增功能请联系主人。"

    def _cmd_help(self) -> str:
        p = self._menu_prefix()
        try:
            from . import command_menu
            return command_menu.render_main(self.PLUGIN_VERSION, p)
        except Exception:
            lines = [
                f"恶魔bot {self.PLUGIN_VERSION}",
                f"常用指令（前缀 {p} 或 == 都行）",
                "",
                "【核心】",
                f"{p}状态  运行状态总览",
                f"{p}自检  插件出问题先发这个",
                f"{p}版本  查看版本和已加载的模块",
                "",
                "【功能目录】",
                f"{p}插图  图片/Pixiv/R18",
                f"{p}表情  表情包",
                f"{p}词库  梗词学习",
                f"{p}联网  搜索/热搜",
                f"{p}群友  群友档案",
                f"{p}生活  作息/主人",
                f"{p}文案  文案",
                f"{p}多模态  识图/语音",
                f"{p}人格  风格/心情",
                f"{p}记忆  记住/回忆",
                f"{p}控制  开关/冷却/省钱",
                f"{p}其他指令  低频/诊断/管理员功能",
            ]
            return "\n".join(lines) + "\n\n如需新增功能请联系主人。"

    def _ensure_persona_file(self):
        if not self.persona_file.exists():
            self._load_or_create_persona()

    def _poetry_pick_text(self) -> str:
        if poetry is None:
            return "今夜且听风，也别把自己想得太累。"
        return poetry.pick_poem()

    async def _poetry_idle_loop(self):
        await asyncio.sleep(30)
        while True:
            try:
                if (self._cfg("poetry","enabled",default=True) and
                    self._cfg("poetry","idle_enabled",default=True) and
                    self._last_owner_message_at and
                    time.time() - self._last_owner_message_at >= int(self._cfg("poetry","idle_threshold_seconds",default=7200)) and
                    time.time() - self._last_poetry_push_at >= int(self._cfg("poetry","idle_min_gap_seconds",default=43200)) and
                    not self._is_sleep_window() and self._known_groups and random.random() < float(self._cfg("poetry","idle_chance",default=0.18))):
                    gid = random.choice(sorted(self._known_groups))
                    bot = self._last_bot or getattr(self.context, "bot", None)
                    if bot is not None:
                        text = poetry.pick_for_context("idle_owner") if poetry is not None else self._poetry_pick_text()
                        await bot.api.call_action("send_group_msg", group_id=int(gid), message=text)
                        self._last_poetry_push_at = time.time()
                        logger.info(f"[恶魔bot] 许久没见主人发言，向群 {gid} 发了一句诗。")
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.debug(f"[恶魔bot] 主动诗句任务跳过：{e}")
            await asyncio.sleep(max(60, int(self._cfg("poetry","idle_check_interval_seconds",default=900))))

    def _cmd_token_usage(self) -> str:
        d=self._usage_reset_if_needed()
        return (f"今天 token：{int(d.get('total',0)):,}\n"
                f"输入：{int(d.get('input',0)):,}｜输出：{int(d.get('output',0)):,}\n"
                f"请求：{int(d.get('requests',0))} 次｜后台辅助：{int(d.get('aux_total',0)):,} token\n"
                f"省钱模式：{'开' if self._cfg('token_saver','enabled',default=True) else '关'}")

    def _cmd_status(self, group_id: str) -> str:
        now = time.time()

        def left(ts):
            return f"{int(max(0, ts - now))}秒" if ts > now else "无"

        book = self._members.get(group_id) or {}
        seg_on = self._cfg("segment_reply", "enabled", default=False)
        sleep_line = (f"是，剩 {self._sleep_remaining()}" if self._is_sleep_window() else "否（23:59-07:30）")
        token_today = int(self._usage_reset_if_needed().get("total", 0))
        return "\n".join(
            [
                f"恶魔bot {self.PLUGIN_VERSION}｜会话：{group_id}",
                (
                    f"高峰时段：是（双倍计费，只回被@的，{self._peak_ends_in()}分钟后恢复）"
                    if self.in_peak_hours() else "高峰时段：否（正常说话）"
                ),
                f"本地闭嘴剩余：{left(self._muted_until.get(group_id, 0))}",
                f"平台禁言退避剩余：{left(self._platform_muted_until.get(group_id, 0))}",
                f"结束语静默剩余：{left(self._quiet_until.get(group_id, 0))}",
                f"当前语速：{self._group_msg_rate_per_minute(group_id):.1f} 条/分"
                f"（高频线 {self._cfg('reply_gate', 'busy_messages_per_minute', default=15)}）",
                f"插话冷却：{self._cfg('reply_gate', 'min_reply_interval_seconds', default=45)}秒，"
                f"上次插话："
                + (f"{int(now - self._last_reply_at[group_id])}秒前" if self._last_reply_at.get(group_id) else "从未"),
                f"分段发送：{'开（会发多条，建议关）' if seg_on else '关'}，"
                f"单条上限 {self._cfg('reply_style', 'max_chars', default=26)} 字",
                f"群友档案：{len(book.get('members', {}))} 人｜词库：{len(self._slang_cache())} 条",
                f"风格速记：{'有' if self._style.get('profile') else '无'}｜"
                f"热搜缓存：{len(self._knowledge.get('hot', {}).get('items', []))} 条",
                f"启动告警：{len(self._boot_errors)} 条" + ("（发 /自检 看详情）" if self._boot_errors else ""),
                f"夜间休眠：{sleep_line}｜今日 token：{token_today:,}",
            ]
        )
    
    async def _send_local_file(self, event: AstrMessageEvent, file_path: Path, file_name: str) -> bool:
        """把本地文件发给触发指令的私聊对象，尽量走平台原生的文件上传接口。

        QQ(aiocqhttp/NapCat) 不支持在普通消息链里塞一个本地路径的 File 段来发私聊文件
        （框架会当成消息发出去但对方收不到任何附件，只会看到一串路径文字，
        这正是之前遇到的问题）。QQ 这边必须调用协议端扩展 API upload_private_file。

        返回 True 表示已经用平台原生接口发出去了；返回 False 表示当前平台不支持这条路，
        调用方应该退回到普通的 Comp.File 消息段（对 Telegram/Discord 等原生支持文件的平台仍然有效）。
        """
        platform_name = event.get_platform_name()
        if platform_name == "aiocqhttp":
            try:
                from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
                    AiocqhttpMessageEvent,
                )
            except Exception as e:
                logger.warning(f"[恶魔bot] 导入 aiocqhttp 事件类失败，回退普通消息段：{type(e).__name__}: {e}")
                return False
            if not isinstance(event, AiocqhttpMessageEvent):
                return False
            try:
                client = event.bot
                await client.api.call_action(
                    "upload_private_file",
                    user_id=int(event.get_sender_id()),
                    file=str(file_path.resolve()),
                    name=file_name,
                )
                return True
            except Exception as e:
                logger.warning(f"[恶魔bot] upload_private_file 调用失败，回退普通消息段：{type(e).__name__}: {e}")
                return False
        return False

    def _find_main_log(self) -> Path | None:
        """自动寻找 AstrBot 主运行日志。

        优先级：
        1. AstrBot 标准主日志 data/logs/astrbot.log
        2. 可能存在的轮转主日志 astrbot.log.*
        3. 我们自己通过 stdout/stderr 保存的 astrbot-runtime.log

        不会把 event_loop_watchdog.log、插件 daemon.log 等其他日志
        冒充成 AstrBot 主运行日志。
        """
        data_dir = Path(get_astrbot_data_path())
        logs_dir = data_dir / "logs"

        candidates = [
            logs_dir / "astrbot.log",
            logs_dir / "astrbot-runtime.log",
        ]

        # 支持 astrbot.log.1 / astrbot.log.2 等轮转文件，按修改时间倒序。
        if logs_dir.is_dir():
            rotated = [
                p for p in logs_dir.glob("astrbot.log.*")
                if p.is_file()
            ]
            rotated.sort(
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
            candidates.extend(rotated)

        for path in candidates:
            try:
                if path.is_file() and path.stat().st_size >= 0:
                    return path
            except OSError:
                continue

        return None

    async def _cmd_send_log(self, event: AstrMessageEvent, arg: str):
        """把 AstrBot 运行日志发给私聊里的管理员。
        默认发最近 N 行（防止文件太大发不出去/太长没法看），
        发 /发送日志 全部 则发完整原始文件。

        日志路径不再写死，优先自动探测 AstrBot 主日志；
        如果你用 `astrbot run 2>&1 | tee -a data/logs/astrbot-runtime.log`
        保存终端日志，也会自动识别该文件。
        """
        log_path = self._find_main_log()
        if log_path is None:
            data_dir = Path(get_astrbot_data_path())
            logs_dir = data_dir / "logs"
            return (
                "当前没有找到 AstrBot 主运行日志文件。\n"
                f"检查目录：{logs_dir}\n"
                "如果你是通过终端直接运行 AstrBot，主日志可能只输出在终端 stdout/stderr，"
                "没有自动写入 astrbot.log。\n"
                "可用 `astrbot run 2>&1 | tee -a data/logs/astrbot-runtime.log` "
                "启动，让终端日志同时保存到文件。"
            )

        arg = (arg or "").strip()
        try:
            if arg in ("全部", "all", "完整", "全文"):
                send_path = log_path
                label = "完整日志"
            else:
                try:
                    n = int(arg) if arg else 1000
                except ValueError:
                    n = 1000
                n = max(50, min(n, 20000))
                with log_path.open("r", encoding="utf-8", errors="ignore") as f:
                    lines = f.readlines()
                tail = lines[-n:]
                send_path = self.data_dir / "_log_tail.log"
                send_path.write_text("".join(tail), encoding="utf-8")
                label = f"最近 {len(tail)} 行"
        except Exception as e:
            logger.warning(f"[恶魔bot] 准备日志文件失败：{type(e).__name__}: {e}")
            return f"读日志失败：{type(e).__name__}: {e}"

        file_name = f"astrbot_log_{int(time.time())}.log"
        logger.info(f"[恶魔bot] 管理员 {event.get_sender_id()} 请求发送日志（{label}）")
        try:
            sent_natively = await self._send_local_file(event, send_path, file_name)
        except Exception as e:
            logger.warning(f"[恶魔bot] 发送日志文件失败：{type(e).__name__}: {e}")
            return f"发送日志文件失败：{type(e).__name__}: {e}"

        if sent_natively:
            return f"日志文件已发送（{label}），文件名 {file_name}，去看看聊天窗口的文件消息"

        # 非 QQ/aiocqhttp 平台，或者上面的原生接口不可用：退回普通消息段（对支持文件段的平台仍然有效）
        try:
            return [
                Comp.Plain(f"日志来了（{label}）：\n"),
                Comp.File(file=str(send_path), name=file_name),
            ]
        except Exception as e:
            logger.warning(f"[恶魔bot] 构造日志文件消息失败：{e}")
            return f"构造消息失败：{type(e).__name__}: {e}"

    async def _cmd_selfcheck(self, event: AstrMessageEvent, group_id: str) -> str:
        """插件出问题时第一个该发的指令：逐项报告哪里断了。"""
        lines = [f"恶魔bot {self.PLUGIN_VERSION} 自检"]

        lines.append(f"1. 指令通道：正常（你能看到这条就说明通了）")

        ok_dir = False
        try:
            probe = self.data_dir / ".probe"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
            ok_dir = True
        except Exception as e:
            lines.append(f"2. 数据目录：写不了！{type(e).__name__}: {e}")
        if ok_dir:
            lines.append(f"2. 数据目录：可写 {self.data_dir}")

        lines.append(f"3. 子模块：联网{'✓' if websearch else '✗'} 插图{'✓' if images else '✗'} "
                     f"LLM工具{'✓' if self._tools_ready else '✗'}")

        prov = self._pick_provider(event)
        lines.append(f"4. 模型 provider：{'拿到了' if prov else '拿不到！查 AstrBot 的模型配置'}")

        lines.append(f"5. 管理员：{len(self.admin_ids)} 人"
                     + ("（没配！/解禁 这类指令会失效）" if not self.admin_ids else ""))
        lines.append(f"6. 群白名单：{len(self.group_whitelist) or '不限制'}")
        lines.append(f"7. 分段发送：{'开（建议关）' if self._cfg('segment_reply','enabled',default=False) else '关'}")

        if self._boot_errors:
            lines.append("8. 启动告警：")
            lines.extend(f"   - {e}" for e in self._boot_errors[:5])
        else:
            lines.append("8. 启动告警：无")

        lines.append("\n还是不正常的话，去 AstrBot 日志里搜 demonbot，把红色那几行发我。")
        return "\n".join(lines)

    async def _cmd_image_test(self, arg: str) -> str:
        tags = self._extract_image_tags(arg) if arg else []
        lines = [f"标签：{tags or '（空，随机取图）'}"]
        backends = self._image_backends()
        for backend in backends + ["real"]:
            try:
                results = await images.fetch(
                    tags,
                    backends=[backend],
                    limit=1,
                    proxy=self._cfg("images", "proxy_domain", default="i.pixiv.re"),
                    timeout=self._cfg("images", "timeout_seconds", default=12),
                    logger=logger,
                    pool=self._cfg("images", "pool_size", default=8),
                    min_width=self._cfg("images", "min_width", default=900),
                    min_height=self._cfg("images", "min_height", default=900),
                    exclude_ai=self._cfg("images", "exclude_ai", default=True),
                    strict=True,
                    real=(backend == "real"),
                    **self._pixiv_args(),
                )
                if results:
                    r = results[0]
                    extra = ""
                    if r.get("rank"):
                        extra += f" 榜{r.get('rank')}名"
                    if r.get("bookmarks"):
                        extra += f" {r.get('bookmarks')}收藏"
                    if (r.get("page_count") or 1) > 1:
                        extra += f" {r.get('page_count')}p"
                    lines.append(
                        f"可用 {backend}：{r.get('size_used')} "
                        f"{r.get('width')}x{r.get('height')} pid={r.get('pid')}{extra}"
                    )
                else:
                    lines.append(f"不通 {backend}：连上了但这个标签没结果")
            except Exception as e:
                lines.append(f"不通 {backend}：{type(e).__name__}")
        lines.append(
            f"反代域名：{self._cfg('images', 'proxy_domain', default='i.pixiv.re')}"
            "（图发出来是裂图就换这个）"
        )
        if "pixiv" in backends and not self._cfg("images", "pixiv_cookie", default=""):
            lines.append("没配 Pixiv Cookie：榜单能用，冷门标签的官方搜索会被拒（发 /榜单 看说明）")
        lines.append("R18 需私聊 /age>18 开启；指令含「好」走成人区，「真」走真人图")
        return "\n".join(lines)

    async def _cmd_rank_status(self, arg: str) -> str:
        """/榜单：看 Pixiv 直连链路是否通、当前月榜长什么样。"""
        args = self._pixiv_args()
        mode = (arg or "").strip() or (args["rank_modes"][0] if args["rank_modes"] else "monthly")
        alias = {"月榜": "monthly", "月": "monthly", "周榜": "weekly", "周": "weekly",
                 "日榜": "daily", "日": "daily", "新人": "rookie", "原创": "original"}
        mode = alias.get(mode, mode)
        lines = [
            f"图源顺序：{'、'.join(self._image_backends())}",
            f"榜单模式：{mode}｜取前 {args['rank_pages'] * 50} 名"
            f"（带标签时翻到前 {args['rank_tag_pages'] * 50} 名里筛）",
            f"Pixiv Cookie：{'已配置' if args['pixiv_cookie'] else '未配置（冷门标签会退回 lolicon/anosu）'}",
            f"多 p 整套发送：{'开' if self._cfg('images', 'send_all_pages', default=True) else '关'}"
            f"（最多 {self._cfg('images', 'max_pages_per_illust', default=20)} p，"
            f"{'合并转发' if self._cfg('images', 'forward_multi_page', default=True) else '普通多图'}）",
        ]
        try:
            items = await images.pixiv_ranking(
                mode=mode,
                pages=args["rank_pages"],
                proxy=self._cfg("images", "proxy_domain", default="i.pixiv.re"),
                timeout=self._cfg("images", "timeout_seconds", default=12),
                cookie=args["pixiv_cookie"],
                host=args["pixiv_host"],
                quality=self._cfg("images", "quality", default="regular"),
                cache_seconds=args["rank_cache_seconds"],
                logger=logger,
            )
        except Exception as e:
            return "\n".join(lines) + f"\n榜单拉取失败：{type(e).__name__}（多半是连不上 pixiv.net）"
        if not items:
            return "\n".join(lines) + "\n榜单拉取失败：没返回数据，检查网络能不能直连 pixiv.net"
        multi = sum(1 for x in items if (x.get("page_count") or 1) > 1)
        lines.append(f"榜单已拉到 {len(items)} 张，其中多 p 作品 {multi} 张")
        for x in items[:3]:
            lines.append(
                f"  第{x.get('rank')}名 {str(x.get('title'))[:16]}"
                f"｜{x.get('author')}｜{x.get('bookmarks')}收藏｜{x.get('page_count')}p"
            )
        return "\n".join(lines)


    def _image_keyword_map(self) -> dict:
        """中文触发词 -> Pixiv 实际标签。

        画质差的头号原因就是拿中文当 tag 搜——Pixiv 的标签体系是日文，
        「腿」几乎搜不到东西，能搜到的多半是标题里凑巧带这个字的杂图。
        映射到 日文标签 之后，命中的才是真正被打过该标签的作品。

        三份来源合并：WebUI 内置映射 + Pixiv 自动发现缓存 + /记住标签 自定义映射，
        优先级为「自定义 > 自动发现 > 内置」。
        """
        out = {}
        for item in self._cfg("images", "keyword_map", default=None) or []:
            if "=" in str(item):
                k, v = str(item).split("=", 1)
                if k.strip() and v.strip():
                    # 值支持用 | 写多个同义标签
                    out[k.strip()] = [x.strip() for x in v.split("|") if x.strip()]
        for k, v in (self._auto_img_tags or {}).items():
            if k and v:
                out[k] = list(v)
        for k, v in (self._img_tags or {}).items():
            if k and v:
                out[k] = list(v)
        return out

    def _parse_image_intent(self, text: str) -> tuple:
        """解析看图意图，返回 (is_real, mature_mode, cleaned_keyword)。

        规则：
          - 只有出现在实际关键词之前的「真」才表示真人模式。
          - 只有出现在实际关键词之前的「好」才表示 R18 成人分区（需 /age>18）。
          - 第一个实际关键词确定后，后面的内容不再作为新的模式或搜索词。
        """
        t = (text or "").strip()
        t = re.sub(r"@\S+", " ", t)
        for n in self._cfg("reply_style", "self_names", default=[]) or []:
            if n:
                t = t.replace(str(n), " ")
        for p in (self._cfg("commands", "prefixes", default=None) or ["/", "=="]):
            if t.startswith(str(p)):
                t = t[len(str(p)):].lstrip()
                break
        for w in self._cfg("images", "trigger_words", default=None) or []:
            if w:
                t = t.replace(str(w), " ")
        t = re.sub(r"(图片|图|照片|壁纸)", " ", t).strip()

        is_real = False
        mature_mode = False
        while t:
            before = t
            if t.startswith("真人"):
                is_real = True
                t = t[2:].lstrip()
                continue
            if t.startswith("真"):
                is_real = True
                t = t[1:].lstrip()
                continue
            if t.startswith("好"):
                mature_mode = True
                t = t[1:].lstrip()
                continue
            if t == before:
                break

        t = re.sub(r"^[的啊吧呀呢吗嘛了个张点儿~!！?？。，,\s]+", "", t)
        t = re.sub(r"[的啊吧呀呢吗嘛了个张点儿~!！?？。，,\s]+$", "", t)
        keyword = (t.split() or [""])[0]
        return is_real, mature_mode, keyword

    def _extract_image_tags(self, text: str) -> list:
        """只提取第一个实际关键词，并映射成 Pixiv/图片源标签。"""
        _, _, keyword = self._parse_image_intent(text)
        if not keyword:
            return []
        mapping = self._image_keyword_map()
        mapped = mapping.get(keyword)
        tags = mapped[:3] if mapped else [keyword]
        quality = self._cfg("images", "quality_tag", default="")
        if quality and tags:
            tags.append(quality)
        return [x for x in tags if x][:4]

    async def _resolve_image_tags(self, word: str) -> list:
        """自动发现 Pixiv 实际标签；手工 /记住标签 永远优先。"""
        word = (word or "").strip()
        if not word:
            return []
        mapping = self._image_keyword_map()
        if word in mapping and mapping[word]:
            return list(mapping[word])[:6]
        if images is None:
            return self._extract_image_tags(word)
        try:
            tags = await images.resolve_pixiv_tags(
                word,
                cookie=str(self._cfg("images", "pixiv_cookie", default="") or "").strip(),
                host=str(self._cfg("images", "pixiv_host", default="https://www.pixiv.net") or "https://www.pixiv.net").strip(),
                timeout=min(float(self._cfg("images", "timeout_seconds", default=12) or 12), 10.0),
                max_tags=4,
                cache_seconds=86400,
                logger=logger,
            )
        except Exception as e:
            logger.debug(f"[恶魔bot] 自动解析 Pixiv 标签失败「{word}」：{type(e).__name__}: {e}")
            return self._extract_image_tags(word)
        if tags:
            self._auto_img_tags[word] = list(tags[:6])
            self._save_json(self.auto_imgtag_file, self._auto_img_tags)
            return list(tags[:6])
        return self._extract_image_tags(word)

    def _user_r18_unlocked(self, user_id: str) -> bool:
        return str(user_id) in self._r18_users

    def _set_r18_unlocked(self, user_id: str, unlocked: bool) -> None:
        uid = str(user_id)
        if unlocked:
            self._r18_users.add(uid)
        else:
            self._r18_users.discard(uid)
        self._save_json(self.r18_file, {"users": sorted(self._r18_users)})
        self._save_json(self.like_usage_file, self._like_usage)
        self._save_json(self.like_grants_file, self._like_grants)
        if self._like_daily_task:
            self._like_daily_task.cancel()
        if self._poetry_idle_task:
            self._poetry_idle_task.cancel()

    async def fetch_image_chain(
        self,
        keyword: str = "",
        strict: bool = False,
        *,
        is_real: bool = False,
        mature_mode: bool = False,
        want_r18: bool = False,
        user_id: str = "",
        is_private: bool = True,
        quality: str = "",
        event: AstrMessageEvent | None = None,
        group_hint: str = "",
    ):
        """按关键词取一张图。

        mature_mode=True（指令含「好」）表示 R18 成人分区：
        - 默认仅私聊允许（测试群可填 images.mature_group_whitelist）
        - 必须先私聊 /age>18 解锁
        - 解锁后向 lolicon/anosu 请求 r18=1 真正的成人图
        """
        if images is None or not self._cfg("images", "enabled", default=True):
            return None
        use_r18 = bool(mature_mode or want_r18)
        if use_r18:
            allowed_groups = {
                str(x) for x in (self._cfg("images", "mature_group_whitelist", default=[]) or [])
            }
            if not is_private and str(group_hint or "") not in allowed_groups:
                return "MATURE_PRIVATE_ONLY"
            if not self._user_r18_unlocked(user_id):
                return "NEED_R18_UNLOCK"
        tags = await self._resolve_image_tags(keyword) if keyword else []
        # 画质档位：默认不再要原图。原图动辄十几 MB，发到 QQ 又慢又费流量，
        # 而群聊本来就会二次压缩，跟 regular（约1200px）几乎看不出差别。
        quality = images.normalize_quality(
            quality or self._cfg("images", "quality", default="regular")
        )
        # R18 时优先走支持成人分区的 lolicon/anosu，跳过全年龄排行榜
        backends = self._image_backends()
        if use_r18 and not is_real:
            backends = [b for b in backends if b != "pixiv"] or ["lolicon", "anosu"]
            if "lolicon" not in backends:
                backends = ["lolicon"] + backends
        try:
            results = await images.fetch(
                tags,
                backends=backends,
                limit=1,
                proxy=self._cfg("images", "proxy_domain", default="i.pixiv.re"),
                timeout=self._cfg("images", "timeout_seconds", default=12),
                logger=logger,
                pool=self._cfg("images", "pool_size", default=8),
                min_width=self._cfg("images", "min_width", default=900),
                min_height=self._cfg("images", "min_height", default=900),
                exclude_ai=self._cfg("images", "exclude_ai", default=True),
                strict=strict,
                r18=1 if use_r18 else 0,
                real=is_real,
                mature=use_r18,
                quality=quality,
                **(self._pixiv_args() if not use_r18 else {}),
            )
        except Exception as e:
            logger.warning(f"[恶魔bot] 取图失败：{type(e).__name__}: {e}")
            return None
        if not results:
            return None
        pic = results[0]
        self._last_image_info = pic
        kind = "真人" if is_real else "Pixiv"
        zone = "R18" if use_r18 else "全年龄/擦边"
        logger.info(
            f"[恶魔bot] 取图成功 类型={kind} 分区={zone} 标签={tags} "
            f"来源={pic.get('source')} 名次={pic.get('rank') or '-'} "
            f"收藏={pic.get('bookmarks') or '-'} 多p={pic.get('page_count') or 1} "
            f"档位={images.quality_label(quality)} 规格={pic.get('size_used')} "
            f"尺寸={pic.get('width')}x{pic.get('height')} pid={pic.get('pid')}"
        )
        try:
            return self._build_image_chain(pic, event=event)
        except Exception as e:
            logger.warning(f"[恶魔bot] 构造图片消息失败：{e}")
            return None

    # ==================== Pixiv 参数 / 多 p 消息构造 ====================

    def _image_backends(self) -> list:
        """图源顺序。老配置里没有 pixiv 时自动把它补到最前面。"""
        backends = list(self._cfg("images", "backends", default=None) or [])
        if not backends:
            return ["pixiv", "lolicon", "anosu"]
        if self._cfg("images", "prefer_ranking", default=True) and "pixiv" not in backends:
            backends = ["pixiv"] + backends
        return backends

    def _pixiv_args(self) -> dict:
        """喂给 images.fetch 的 Pixiv 直连参数，全部来自 WebUI 配置。"""
        gate_raw = self._cfg("images", "users_gate", default=None) or [10000, 5000, 1000]
        gate = []
        for g in gate_raw:
            try:
                gate.append(int(str(g).strip()))
            except (TypeError, ValueError):
                continue
        return dict(
            pixiv_cookie=str(self._cfg("images", "pixiv_cookie", default="") or "").strip(),
            pixiv_host=str(
                self._cfg("images", "pixiv_host", default="https://www.pixiv.net") or ""
            ).strip() or "https://www.pixiv.net",
            rank_modes=self._cfg("images", "rank_modes", default=None) or ["monthly", "weekly", "daily"],
            rank_pages=int(self._cfg("images", "rank_pages", default=2) or 2),
            rank_tag_pages=int(self._cfg("images", "rank_tag_pages", default=22) or 22),
            rank_cache_seconds=int(self._cfg("images", "rank_cache_seconds", default=21600) or 21600),
            min_bookmarks=int(self._cfg("images", "min_bookmarks", default=0) or 0),
            users_gate=gate or [10000, 5000, 1000],
            month_only=bool(self._cfg("images", "month_only", default=True)),
            search_pages=int(self._cfg("images", "search_pages", default=2) or 2),
        )

    def _bot_display_name(self) -> str:
        names = self._cfg("reply_style", "self_names", default=None) or []
        for n in names:
            if str(n).strip():
                return str(n).strip()
        return "恶魔bot"

    def _build_image_chain(self, pic: dict, event: AstrMessageEvent | None = None):
        """把一张（可能是多 p 的）插图变成消息链。

        单图 -> 直接一张 Image。
        多 p -> 优先做成「合并转发的聊天记录」，一页一条，刷屏感最低；
                平台不支持 Node 时退回成一条消息里塞多张图。
        """
        urls = [u for u in (pic.get("page_urls") or []) if u] or [pic.get("url")]
        urls = [u for u in urls if u]
        if not urls:
            return None
        max_pages = int(self._cfg("images", "max_pages_per_illust", default=20) or 20)
        if not self._cfg("images", "send_all_pages", default=True):
            urls = urls[:1]
        truncated = len(urls) > max_pages
        urls = urls[:max_pages]

        if len(urls) == 1:
            return [Comp.Image.fromURL(urls[0])]

        caption = self._image_caption(pic, len(urls), truncated)
        if self._cfg("images", "forward_multi_page", default=True) and hasattr(Comp, "Node"):
            try:
                uin = str(getattr(event.message_obj, "self_id", "") or "10000") if event else "10000"
                nick = self._bot_display_name()
                nodes = [Comp.Node(uin=int(uin) if str(uin).isdigit() else 10000,
                                   name=nick, content=[Comp.Plain(caption)])]
                for u in urls:
                    nodes.append(
                        Comp.Node(
                            uin=int(uin) if str(uin).isdigit() else 10000,
                            name=nick,
                            content=[Comp.Image.fromURL(u)],
                        )
                    )
                return nodes
            except Exception as e:  # noqa: BLE001
                logger.debug(f"[恶魔bot] 构造合并转发失败，退回普通多图：{e}")
        chain = [Comp.Plain(caption)]
        chain.extend(Comp.Image.fromURL(u) for u in urls)
        return chain

    def _image_caption(self, pic: dict, count: int, truncated: bool = False) -> str:
        bits = [f"{pic.get('title', '') or '无题'}"]
        if pic.get("author"):
            bits.append(f"by {pic.get('author')}")
        if pic.get("rank"):
            bits.append(f"月榜第{pic.get('rank')}")
        if pic.get("bookmarks"):
            bits.append(f"{pic.get('bookmarks')}收藏")
        if pic.get("pid"):
            bits.append(f"pid {pic.get('pid')}")
        tail = f"｜共 {count} p" + ("（超出上限已截断）" if truncated else "")
        return "｜".join(bits)[:80] + tail

    async def _send_illust(self, event: AstrMessageEvent, group_id: str, chain) -> bool:
        """发图专用出口：合并转发发不出去时自动退回逐张发。"""
        if not chain:
            return False
        is_forward = hasattr(Comp, "Node") and any(isinstance(c, Comp.Node) for c in chain)
        if await self._safe_send(event, event.chain_result(chain), group_id):
            return True
        if not is_forward:
            return False
        logger.info("[恶魔bot] 合并转发失败，改成逐张发送")
        ok = False
        for node in chain:
            for seg in getattr(node, "content", []) or []:
                if isinstance(seg, Comp.Image):
                    if await self._safe_send(event, event.chain_result([seg]), group_id):
                        ok = True
                    await asyncio.sleep(0.6)
        return ok

    # ==================== 图片识别（多模态） ====================

    def _collect_image_urls(self, event: AstrMessageEvent) -> list:
        """从这条消息（含引用的那条）里把图片地址抠出来。"""
        urls = []
        try:
            segs = list(event.message_obj.message or [])
        except Exception:  # noqa: BLE001
            segs = []
        # 引用消息里的图也算，方便「回复那张图 + /识图」
        for seg in list(segs):
            if isinstance(seg, Comp.Reply):
                inner = getattr(seg, "chain", None) or []
                segs.extend(inner)
        for seg in segs:
            if isinstance(seg, Comp.Image):
                u = getattr(seg, "url", "") or getattr(seg, "file", "") or ""
                u = str(u)
                if u.startswith("base64://"):
                    continue
                if u:
                    urls.append(u)
        return urls[:3]

    async def _vision_describe(
        self, event: AstrMessageEvent, urls: list, question: str = ""
    ) -> str:
        """
        调多模态模型看图。走的是当前会话正在用的 provider，
        所以你得给它配一个支持视觉的模型（比如带 -vl / -vision 的），
        纯文本模型会直接报不支持，这里会如实返回空字符串。
        """
        provider = self._pick_provider(event, prefer_gate=False)
        if provider is None:
            return ""
        prompt = question.strip() or "看看这张图，用一句口语化的中文说说图里是什么、有什么好笑或值得说的地方。"
        prompt += "\n只回一句话，别超过30字，别用markdown。"
        try:
            resp = await asyncio.wait_for(
                provider.text_chat(
                    prompt=prompt,
                    image_urls=urls,
                    contexts=[],
                ),
                timeout=self._cfg("vision", "timeout_seconds", default=40),
            )
            self._record_usage(getattr(resp, "usage", None), source="vision")
            text = (getattr(resp, "completion_text", "") or "").strip()
            return self._shorten(self._strip_markdown(text))
        except asyncio.TimeoutError:
            logger.warning("[恶魔bot] 识图超时")
            return ""
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[恶魔bot] 识图失败：{type(e).__name__}: {e}")
            return ""

    async def _try_vision_reply(
        self, event: AstrMessageEvent, group_id: str, text: str
    ) -> bool:
        """被@着发图时自动看图接话。没配视觉模型就静默放弃，不打扰。"""
        if not self._cfg("vision", "enabled", default=True):
            return False
        if not self._aux_llm_allowed() and not self._cfg("vision", "ignore_budget", default=False):
            return False
        urls = self._collect_image_urls(event)
        if not urls:
            return False
        answer = await self._vision_describe(event, urls, text)
        if not answer:
            return False
        sent = await self._safe_send(event, answer, group_id)
        if sent and stickers is not None:
            emo = stickers.detect_emotion(text, answer)
            if emo:
                await asyncio.sleep(random.uniform(0.6, 1.4))
                await self._send_sticker(event, group_id, emo)
        return sent

    # ==================== 语音（TTS） ====================

    async def _send_voice(self, event: AstrMessageEvent, group_id: str, text: str) -> bool:
        """
        把一句话转成语音发出去。

        用 AstrBot 自己配的 TTS 服务商——插件不自带发音引擎，
        也不去薅第三方 TTS 接口（那些接口大多不稳，还容易把文本泄露出去）。
        没配 TTS 时返回 False，由调用方给出提示。
        """
        if not self._cfg("voice", "enabled", default=True):
            return False
        text = (text or "").strip()[: self._cfg("voice", "max_chars", default=80)]
        if not text:
            return False
        provider = None
        for getter in ("get_using_tts_provider", "get_using_tts", "get_tts_provider"):
            try:
                fn = getattr(self.context, getter, None)
                if fn:
                    provider = fn()
                    if provider:
                        break
            except Exception:  # noqa: BLE001
                continue
        if provider is None:
            logger.info("[恶魔bot] 没有可用的 TTS 服务商，语音发送跳过")
            return False
        try:
            path = await asyncio.wait_for(
                provider.get_audio(text),
                timeout=self._cfg("voice", "timeout_seconds", default=30),
            )
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[恶魔bot] TTS 合成失败：{type(e).__name__}: {e}")
            return False
        if not path:
            return False
        try:
            chain = [Comp.Record(file=str(path))]
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[恶魔bot] 构造语音消息失败：{e}")
            return False
        return await self._safe_send(event, event.chain_result(chain), group_id)

    def _wants_image(self, text: str) -> bool:
        if images is None or not self._cfg("images", "enabled", default=True) or not text:
            return False
        words = self._cfg("images", "trigger_words", default=None) or []
        return any(str(w) in text for w in words if w)

    def image_cooldown_left(self, group_id: str, sender_id: str) -> int:
        """还要等几秒才能再要图。0 表示现在就能发。

        两道冷却取最严的一道：
          - 按人：普通用户默认 5 分钟一张（WebUI 里 images.per_user_cooldown_seconds 可调）
          - 按群：防止一群人轮流刷图，把群刷成图站
        管理员默认完全不受限。
        """
        if self._is_admin(sender_id) and self._cfg("images", "admin_bypass_cooldown", default=True):
            return 0
        now = time.time()
        per_user = self._cfg("images", "per_user_cooldown_seconds", default=300)
        if self.in_peak_hours():
            per_user = int(
                per_user * self._cfg("peak_hours", "image_cooldown_multiplier", default=2.0)
            )
        left = int(per_user - (now - self._last_image_user_at.get(f"{group_id}:{sender_id}", 0)))
        group_cd = self._cfg("images", "cooldown_seconds", default=20)
        left = max(left, int(group_cd - (now - self._last_image_at.get(group_id, 0))))
        return max(0, left)

    @staticmethod
    def _fmt_seconds(sec: int) -> str:
        sec = max(0, int(sec))
        if sec < 60:
            return f"{sec}秒"
        return f"{sec // 60}分{sec % 60}秒" if sec % 60 else f"{sec // 60}分钟"

    async def _try_send_image(self, event: AstrMessageEvent, group_id: str, text: str) -> bool:
        sender_id = str(event.get_sender_id())
        is_private = not bool(event.get_group_id())
        is_real, mature_mode, keyword = self._parse_image_intent(text)

        if is_real and not self._cfg("images", "real_enabled", default=True):
            await self._safe_send(event, "真人图关了，管理员可以用 /开关 真人图 开", group_id)
            return True

        left = self.image_cooldown_left(group_id, sender_id)
        if left > 0:
            if self._cfg("images", "notify_on_cooldown", default=True):
                await self._safe_send(
                    event, f"歇会儿，{self._fmt_seconds(left)}后再来要图", group_id
                )
            return True

        chain = await self.fetch_image_chain(
            text if not keyword else keyword,
            is_real=is_real,
            mature_mode=mature_mode,
            user_id=sender_id,
            is_private=is_private,
            event=event,
            group_hint=event.get_group_id() or "",
        )
        if chain == "MATURE_PRIVATE_ONLY":
            return True
        if chain == "NEED_R18_UNLOCK":
            if is_private:
                await self._safe_send(
                    event,
                    "R18 未开启。请先私聊我发送 /age>18。",
                    group_id,
                )
            return True
        if chain == "MATURE_PRIVATE_ONLY":
            return True
        if not chain:
            # 取不到图别装死：给一句人话 + 一张表情，必要时喊主人
            if self._cfg("images", "notify_on_empty", default=True):
                await self._safe_send(event, "没找着这个标签的图，换个词试试", group_id)
                await self._send_sticker(event, group_id, "出bug", force=True)
                if self._cfg("sponsor", "on_image_fail", default=False):
                    await self._sponsor_alert(event, group_id, "取图失败", quiet=True)
                return True
            return False
        sent = await self._send_illust(event, group_id, chain)
        if sent:
            # 只有真发出去才计冷却，取图失败不该占用户的额度
            now = time.time()
            self._last_image_at[group_id] = now
            self._last_image_user_at[f"{group_id}:{sender_id}"] = now
        return sent

    async def terminate(self):
        self._save_json(self.history_file, self._history)
        self._save_json(self.memory_file, self._memory)
        self._save_json(self.mood_file, self._mood)
        self._save_json(self.members_file, self._members)
        self._save_json(self.knowledge_file, self._knowledge)
        self._save_json(self.style_file, self._style)
        self._save_json(self.r18_file, {"users": sorted(self._r18_users)})
        self._save_json(self.like_usage_file, self._like_usage)
        self._save_json(self.like_grants_file, self._like_grants)
        if self._like_daily_task:
            self._like_daily_task.cancel()
        if self._poetry_idle_task:
            self._poetry_idle_task.cancel()
        self._save_json(self.imgtag_file, self._img_tags)
        logger.info("[恶魔bot] 插件已卸载/停用，数据已保存")
