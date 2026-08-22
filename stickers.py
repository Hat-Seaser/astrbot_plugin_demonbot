"""
表情包模块（本地 GIF/PNG 图包 + 情绪匹配）

设计目标：一次只发一张、发得像人、而且不花任何 token。

工作方式：
1. 扫描一个本地文件夹（默认 data/plugin_data/demonbot/stickers/），
   把里面的图片全部读进索引。文件名会被自动清洗成「表情名」：
     蓝色大肥鱼_被击中(拖鞋)_2026-08-18-14-13-52.gif  ->  被击中(拖鞋)
   规则是：去掉扩展名，去掉结尾的日期时间戳，再去掉最前面的作者/系列前缀。
   所以你把整包 DeepSeek 表情原样丢进去就行，不用改名。
2. 每种「情绪」对应一串表情名关键词（见 EMOTION_RULES），
   匹配是子串匹配，所以「哭 1」「哭 2」「哭 3」会一起被「哭」命中，随机挑一张。
3. 情绪判定全是本地正则，不请求模型：
   - 先看用户说了什么（骂你 / 夸你 / 说晚安 / 问问题…）
   - 再看 bot 自己回了什么（说困了 / 说对不起 / 笑了…）
   两边都命中时以「用户的情绪」优先，因为表情是回应对方的。

没有图包也不会报错：索引为空时所有取图函数都返回 None，主流程照常走。
"""

from __future__ import annotations

import random
import re
import time
from pathlib import Path

# 支持的图片后缀。QQ 对 gif 支持最好，webp 部分协议端发不出去，放最后。
SUPPORTED_EXT = (".gif", ".png", ".jpg", ".jpeg", ".webp")

# 文件名结尾的时间戳：_2026-08-18-14-13-52
_TS_SUFFIX_RE = re.compile(r"[_\-\s]*\d{4}-\d{2}-\d{2}[-_]\d{2}[-_]\d{2}[-_]\d{2}\s*$")
# 也兼容 _20260818141352 这种连写
_TS_SUFFIX_RE2 = re.compile(r"[_\-\s]*\d{14}\s*$")


def clean_name(filename: str) -> str:
    """蓝色大肥鱼_被击中(拖鞋)_2026-08-18-14-13-52.gif -> 被击中(拖鞋)"""
    stem = Path(filename).stem
    stem = _TS_SUFFIX_RE.sub("", stem)
    stem = _TS_SUFFIX_RE2.sub("", stem)
    # 去掉最前面的作者/系列前缀（第一个下划线之前的部分），
    # 但只在确实还剩内容时才去，避免把「Bug」这种单段名字削没了
    if "_" in stem:
        head, rest = stem.split("_", 1)
        if rest.strip():
            stem = rest
    return stem.strip().replace("_", " ")


# ---------------------------------------------------------------- 情绪规则
#
# 每条 = 情绪键: (表情名关键词, 用户说这些话时触发, bot 自己说这些话时触发)
# 关键词是**表情文件名**里的子串；后两项是聊天文本里的正则。
# 顺序有意义：越靠前优先级越高，第一个命中的就用。

EMOTION_RULES = [
    # ---- 被骂 / 被批评：用户在发脾气，这是你点名要的那一档 ----
    ("挨骂", {
        "names": ["被击中", "害怕", "哭", "紧张", "自我安慰", "汗", "坐牢"],
        "user": r"(骂你|傻|蠢|笨|垃圾|废物|没用|没用的|不行|滚|闭嘴|烦人|烦死|讨厌你|"
                r"你有病|有病吧|智障|弱智|白痴|菜|真菜|好菜|难用|失望|批评|说你几句|"
                r"怎么回事|搞什么|什么玩意|服了你|无语了|气死我|认真点|改一改|"
                r"你怎么|能不能|太差了|离谱了你|气死|发火了|我真的无语)",
        "bot": r"(对不起|抱歉|我错了|是我不好|下次注意|我改|我会改)",
    }),
    # ---- 困 / 睡觉：配合作息状态 ----
    ("困了", {
        "names": ["睡觉", "工作(小睡)"],
        "user": r"(晚安|去睡|睡了|早点休息|该睡)",
        "bot": r"(困|睡了|眯一会|打瞌睡|想睡|去睡|晚安|熬不住|眼皮打架)",
    }),
    # ---- 报错 / 要充值：给 token 用完那一档专用 ----
    ("出bug", {
        "names": ["Bug", "叹号", "汗", "死亡", "停止工作", "小丑"],
        "not": ["被击中"],
        "user": r"(报错|出错|坏了|挂了|崩了|失败了|bug|BUG)",
        "bot": r"(出错|报错|失败|挂了|坏了|取不到|没取到|连不上|超时)",
    }),
    ("要钱", {
        "names": ["要米", "钱", "红包", "拿走我的钱", "刷卡"],
        "user": r"(充值|余额|没钱|token|额度)",
        "bot": r"(充值|余额|token|求求|赞助)",
    }),
    # ---- 情绪基本盘 ----
    ("难过", {
        "names": ["哭", "自我安慰", "垃圾桶", "汗"],
        "not": ["被击中"],
        "user": r"(难过|伤心|想哭|好惨|emo|破防|委屈|心疼|难受)",
        "bot": r"(难过|伤心|想哭|委屈|难受|可怜)",
    }),
    ("生气", {
        "names": ["生气", "打字(生气)", "工作(生气)", "拖鞋", "刀"],
        "not": ["被击中"],
        "user": r"(生气|气死|火大|发火|滚开)",
        "bot": r"(生气|气死|火大|别惹我|烦死)",
    }),
    ("害羞", {
        "names": ["害羞", "爱心", "玫瑰", "情书", "眨眼"],
        "not": ["被击中"],
        "user": r"(可爱|喜欢你|爱你|好乖|抱抱|亲亲|想你|摸摸|贴贴|老婆|宝贝)",
        "bot": r"(害羞|脸红|讨厌啦|别说了|羞)",
    }),
    ("开心", {
        "names": ["笑", "庆祝", "跳舞", "干杯", "点赞", "加油"],
        "not": ["反向"],
        "user": r"(哈哈|草|笑死|太好了|牛|厉害|绝了|赞|好耶|awsl|优秀|nb|NB|6666)",
        "bot": r"(哈哈|笑死|太好了|开心|爽|美滋滋|好耶)",
    }),
    ("惊讶", {
        "names": ["惊吓", "叹号", "呆", "问号"],
        "user": r"(卧槽|我去|离谱|震惊|不是吧|真的假的|什么情况|居然)",
        "bot": r"(卧槽|我去|离谱|震惊|不是吧|真的假的)",
    }),
    ("疑惑", {
        "names": ["问号", "呆", "正在思考"],
        "user": r"(\?{2,}|？{2,}|什么意思|啥意思|看不懂|听不懂)",
        "bot": r"(啥|什么意思|没懂|不明白)",
    }),
    ("思考", {
        "names": ["正在思考", "思考", "主意"],
        "user": r"(你觉得|怎么办|建议|分析一下|想想办法)",
        "bot": r"(我想想|让我想|应该是|大概是|可能吧)",
    }),
    ("无语", {
        "names": ["呆", "摇头", "汗", "静音"],
        "user": r"(无语|服了|绷不住|尬|离谱吧|又来了)",
        "bot": r"(无语|服了|绷|行吧|随便你)",
    }),
    ("打招呼", {
        "names": ["打招呼", "点头", "摇铃"],
        "user": r"(^在吗|^在不在|早上好|早安|中午好|下午好|晚上好|hi|hello|你好|来了)",
        "bot": r"(早|你好|来了|嗨)",
    }),
    ("吃饭", {
        "names": ["吃(", "馋(", "喝(", "蛋糕", "干杯"],
        "user": r"(饿了|吃饭|吃啥|吃什么|好吃|外卖|奶茶|夜宵)",
        "bot": r"(饿|吃|好吃|外卖|奶茶)",
    }),
    ("干活", {
        "names": ["打字(普通)", "工作(普通)", "工作(疲倦)", "停止工作", "带薪拉屎"],
        "user": r"(上班|上课|加班|作业|工作|摸鱼|下班)",
        "bot": r"(上班|上课|加班|作业|干活|摸鱼|下班)",
    }),
    ("玩", {
        "names": ["打游戏", "跳舞", "唱歌", "吉他"],
        "user": r"(打游戏|开黑|上号|玩什么|唱歌|听歌)",
        "bot": r"(打游戏|开黑|上号|唱歌|听歌)",
    }),
    ("夸人", {
        "names": ["点赞", "得分(10分)", "点头", "摸头"],
        "not": ["反向"],
        "user": r"(谢谢|多谢|辛苦了|感谢|你真好)",
        "bot": r"(不客气|应该的|小事|谢谢)",
    }),
    ("拒绝", {
        "names": ["反向点赞", "摇头", "得分(0分)", "静音"],
        "user": r"(不要|别|拒绝|不行吗|算了)",
        "bot": r"(不行|不要|拒绝|做不到|没门)",
    }),
    ("生日", {
        "names": ["庆祝", "蛋糕", "生日", "开心", "爱心", "干杯"],
        "user": r"(生日|礼物|送你|送给你|生日快乐|同一天生日)",
        "bot": r"(生日|礼物|生日快乐|后天就是)",
    }),
    ("惊喜", {
        "names": ["惊吓", "叹号", "呆", "庆祝", "开心", "爱心"],
        "user": r"(真的|认真的吗|真的吗|送你了|没错|居然|真的吗)",
        "bot": r"(真的|真的假的|认真的吗|欸|等等|没想到)",
    }),
    ("害羞", {
        "names": ["害羞", "爱心", "玫瑰", "情书", "眨眼"],
        "not": ["被击中"],
        "user": r"(礼物|想收到|送你|喜欢|心意|生日)",
        "bot": r"(不好意思|害羞|脸红|嘿嘿|被看穿|真说了)",
    }),
    ("期待", {
        "names": ["期待", "眨眼", "冒泡"],
        "user": r"(等你|快点|期待|什么时候|礼物|想收到)",
        "bot": r"(等|马上|快了|等我|期待)",
    }),
]

# 给「主动指定情绪」用的快捷别名：/表情 哭
ALIASES = {
    "被骂": "挨骂", "挨批": "挨骂", "委屈": "挨骂",
    "睡": "困了", "睡觉": "困了", "晚安": "困了",
    "bug": "出bug", "报错": "出bug", "错误": "出bug",
    "充值": "要钱", "赞助": "要钱", "token": "要钱",
    "哭": "难过", "伤心": "难过",
    "怒": "生气", "火": "生气",
    "羞": "害羞", "爱": "害羞",
    "笑": "开心", "高兴": "开心", "乐": "开心",
    "惊": "惊讶", "震惊": "惊讶",
    "问号": "疑惑", "懵": "疑惑",
    "想": "思考",
    "招呼": "打招呼", "打招呼": "打招呼",
    "吃": "吃饭", "饿": "吃饭",
    "工作": "干活", "上班": "干活",
    "游戏": "玩",
    "赞": "夸人", "谢谢": "夸人",
    "不": "拒绝",
}


class StickerBox:
    """表情包索引。构造代价很小，重扫也只是一次目录遍历。"""

    def __init__(self, folder: str | Path, logger=None):
        self.folder = Path(folder)
        self.logger = logger
        self.items: list = []          # [(表情名, Path), ...]
        self._recent: list = []        # 最近发过的表情名，用来避免连着重复
        self.scan()

    # ---------- 索引 ----------

    def scan(self) -> int:
        self.items = []
        try:
            if not self.folder.exists():
                return 0
            for p in sorted(self.folder.rglob("*")):
                if p.is_file() and p.suffix.lower() in SUPPORTED_EXT:
                    self.items.append((clean_name(p.name), p))
        except Exception as e:  # noqa: BLE001
            if self.logger:
                self.logger.warning(f"[恶魔bot] 扫描表情包目录失败：{e}")
        return len(self.items)

    @property
    def count(self) -> int:
        return len(self.items)

    def names(self) -> list:
        return [n for n, _ in self.items]

    # ---------- 检索 ----------

    def find(self, keyword: str) -> list:
        """按表情名子串找，返回 [(名字, 路径), ...]。"""
        kw = str(keyword or "").strip().lower()
        if not kw:
            return []
        return [(n, p) for n, p in self.items if kw in n.lower()]

    def _pick_avoiding_repeat(self, pool: list):
        if not pool:
            return None
        fresh = [x for x in pool if x[0] not in self._recent]
        chosen = random.choice(fresh or pool)
        self._recent.append(chosen[0])
        if len(self._recent) > 8:
            self._recent.pop(0)
        return chosen

    def pick_by_emotion(self, emotion: str):
        """按情绪键取一张。返回 (表情名, Path) 或 None。"""
        emotion = ALIASES.get(str(emotion or "").strip().lower(),
                              ALIASES.get(str(emotion or "").strip(), emotion))
        for key, rule in EMOTION_RULES:
            if key != emotion:
                continue
            pool = []
            for kw in rule["names"]:
                pool.extend(self.find(kw))
            # 排除词：防止「点赞」把「反向点赞」也捞进来、
            # 「爱心」把「被击中(爱心)」也捞进来这类反向命中
            bans = [b.lower() for b in rule.get("not", [])]
            if bans:
                pool = [(n, pth) for n, pth in pool
                        if not any(b in n.lower() for b in bans)]
            # 去重（同一张图可能被两个关键词都命中）
            seen, uniq = set(), []
            for n, p in pool:
                if str(p) not in seen:
                    seen.add(str(p))
                    uniq.append((n, p))
            return self._pick_avoiding_repeat(uniq)
        # 不是已知情绪键，就当成表情名直接搜
        return self._pick_avoiding_repeat(self.find(emotion))

    def pick_random(self):
        return self._pick_avoiding_repeat(list(self.items))


# ---------------------------------------------------------------- 情绪判定

def detect_emotion(user_text: str = "", bot_text: str = "") -> str:
    """
    本地规则判情绪，不花 token。返回情绪键，判不出来返回 ""。

    用户说的话优先级高于 bot 自己说的话——表情是回应对方的，
    对方在骂你的时候，就算你嘴上说着「哈哈」，也该发挨骂的表情。
    """
    u = (user_text or "").strip()
    b = (bot_text or "").strip()
    for key, rule in EMOTION_RULES:
        if u and re.search(rule["user"], u):
            return key
    for key, rule in EMOTION_RULES:
        if b and re.search(rule["bot"], b):
            return key
    return ""


def emotion_keys() -> list:
    return [k for k, _ in EMOTION_RULES]


# ---------------------------------------------------------------- 频率控制

class StickerGate:
    """决定「这次要不要发表情」。目的是别刷屏，也别一整天不发。"""

    def __init__(self):
        self._last_at: dict = {}       # 会话 -> 上次发表情的时间
        self._count_today: dict = {}   # 会话 -> (日期, 条数)

    def allow(
        self,
        key: str,
        chance: float = 0.25,
        cooldown: int = 180,
        daily_limit: int = 40,
        force: bool = False,
    ) -> bool:
        now = time.time()
        day = time.strftime("%Y-%m-%d", time.localtime(now))
        d, n = self._count_today.get(key, (day, 0))
        if d != day:
            d, n = day, 0
        if daily_limit > 0 and n >= daily_limit:
            return False
        if not force:
            if now - self._last_at.get(key, 0) < cooldown:
                return False
            if random.random() > max(0.0, min(1.0, chance)):
                return False
        self._last_at[key] = now
        self._count_today[key] = (d, n + 1)
        return True
