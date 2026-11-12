"""拟人化行为层。

为什么单独一个文件：这七条机制都是「在消息链路上加卡口」，和业务功能无关。
塞进 main.py 会让本来就 6000 行的文件更难维护，所以集中放这里，
main.py 只负责在合适的位置调用。

七条对应关系：
  1. InputDebounce   连续输入防抖 —— 别在你打字时抢答
  2. ClosingSilence  收尾沉默     —— 互道晚安不死循环
  3. sanitize_reply  发送前自检   —— 拦「你又熬夜」这类无依据指责
  4. FollowUp        未完话头     —— 别反复追问同一个问题
  5. AirGuard        气氛收口     —— 连续插话后强制旁听
  6. split_protected 保护引文分段 —— 引号里的话不切两半
  7. post_seed       说说由头     —— 「为什么现在发」而不是到点发

设计原则：
- 每个类都自带上限和过期清理，长期跑不会攒垃圾
- 全部只依赖标准库，不引入新包（手机端装依赖很麻烦）
- 出问题一律往「放行」方向兜底，宁可多说一句也不要卡住消息
"""

from __future__ import annotations

import re
import time
from collections import deque

# ============================================================
# 1. 连续输入防抖
# ============================================================


class InputDebounce:
    """把用户连发的几条纯文本合并成一轮请求。

    场景：用户打「我今天」「去医院了」「排队两小时」，拆成三条发。
    没有防抖时，三条各触发一次模型调用，bot 回三条，像抢答。
    有防抖时，前两条被静默拦下，只有最后一条带着完整文本进模型。

    窗口是自适应的：
    - 结尾有句末标点 → 说明话说完了，等 1.5 秒就走
    - 结尾没有标点  → 大概率还没打完，等 4 秒
    - 无论如何，从第一条算起不超过 8 秒（防止用户一直发导致永不出手）
    """

    def __init__(self, cfg):
        self._cfg = cfg
        self._buf: dict[str, list] = {}     # key -> [(ts, text)]
        self._seq: dict[str, int] = {}      # key -> 递增序号

    def _num(self, key: str, default: float) -> float:
        try:
            return float(self._cfg("debounce", key, default=default) or default)
        except Exception:  # noqa: BLE001
            return default

    def _prune(self) -> None:
        """清掉三分钟没动静的 key，避免长跑攒字典。"""
        now = time.monotonic()
        for k in list(self._buf):
            items = self._buf.get(k) or []
            if items and now - items[0][0] > 180:
                self._buf.pop(k, None)
                self._seq.pop(k, None)

    async def collect(self, key: str, text: str):
        """等一个静默窗口。

        返回合并后的文本 = 自己是这一串里的最后一条，该由我来回；
        返回 None        = 后面还有更新的消息，这一条应该被静默拦掉。
        """
        import asyncio

        text = (text or "").strip()
        if not text:
            return None

        self._prune()

        buf = self._buf.setdefault(key, [])
        buf.append((time.monotonic(), text))
        self._seq[key] = self._seq.get(key, 0) + 1
        my_seq = self._seq[key]

        # 有句末标点 = 说完了，少等；没标点 = 还在打，多等
        if re.search(r"[。！？!?…~\.]$", text):
            delay = self._num("min_seconds", 1.5)
        else:
            delay = self._num("max_seconds", 4.0)

        # 总量保护：从这一串的第一条算起
        first_ts = buf[0][0]
        remain = self._num("total_seconds", 8.0) - (time.monotonic() - first_ts)
        if remain <= 0:
            delay = 0.0
        else:
            delay = min(delay, remain)

        if delay > 0:
            await asyncio.sleep(delay)

        if self._seq.get(key) != my_seq:
            # 我不是最后一条，静默让位
            return None

        # 我是最后一条：把这一串全合并
        merged = ""
        for _ts, t in buf:
            if not t:
                continue
            # 中文之间不补空格，英文/数字之间补一个
            if merged and re.match(r"[A-Za-z0-9]", t[0]) and re.search(r"[A-Za-z0-9]$", merged):
                merged += " " + t
            else:
                merged += t
        self._buf[key] = []
        return merged.strip()

    def drop(self, key: str) -> None:
        self._buf.pop(key, None)
        self._seq.pop(key, None)


# ============================================================
# 2. 收尾沉默
# ============================================================

# 用户用来「收尾」的话。必须很短才算——「晚安，今天谢谢你听我说这么多」
# 这种带内容的要回，不能一刀切。
_USER_CLOSING = (
    "晚安", "早安", "睡了", "睡啦", "去睡", "我睡了", "困了睡了",
    "拜拜", "再见", "886", "先这样", "下了", "走了", "我下了",
    "谢谢", "谢啦", "多谢", "好的谢谢",
    "嗯嗯", "好的", "好嘞", "嗯好", "行", "收到",
)

# bot 说过这些，说明这一轮对话已经收口了
_BOT_CLOSING = (
    "晚安", "睡吧", "早点睡", "快去睡", "好梦", "做个好梦",
    "拜拜", "明天见", "回见", "去吧", "去忙吧",
)


class ClosingSilence:
    """判断这一轮该不该闭嘴。

    核心逻辑：用户说了句纯收尾的话，而且 bot 上一轮已经在道别了 → 不回。
    这样「晚安」「晚安呀」「好梦」「嗯你也早点睡」这种来回能停下来。

    只在私聊生效。群里别人说晚安不关 bot 的事。
    """

    def __init__(self, cfg):
        self._cfg = cfg
        self._bot_closed_at: dict[str, float] = {}

    def _on(self) -> bool:
        try:
            return bool(self._cfg("closing_silence", "enabled", default=True))
        except Exception:  # noqa: BLE001
            return True

    def _window(self) -> float:
        try:
            return float(self._cfg("closing_silence", "window_seconds", default=600) or 600)
        except Exception:  # noqa: BLE001
            return 600.0

    def note_bot_reply(self, key: str, text: str) -> None:
        """bot 发出回复后调一次，记下「这一轮我道过别了」。"""
        t = (text or "").strip()
        if not t or len(t) > 60:
            return
        if any(w in t for w in _BOT_CLOSING):
            self._bot_closed_at[key] = time.time()
        else:
            # 说了别的内容，说明对话还在继续，清掉标记
            self._bot_closed_at.pop(key, None)

    def should_stay_silent(self, key: str, text: str) -> bool:
        """该不该不回。"""
        if not self._on():
            return False
        t = (text or "").strip()
        if not t or len(t) > 12:
            return False
        if not any(w in t for w in _USER_CLOSING):
            return False
        last = self._bot_closed_at.get(key) or 0
        if not last:
            return False
        return (time.time() - last) <= self._window()

    def clear(self, key: str) -> None:
        self._bot_closed_at.pop(key, None)


# ============================================================
# 3. 发送前自检：拦「无依据的习惯指责」
# ============================================================

# 模型很爱写这种句子：它从人格设定里知道「用户是熬夜党」这个模糊印象，
# 就会在每次对话里把「你又熬夜」当关心说出来。但用户当下根本没提，
# 这就成了凭空指责——比不关心更像机器人。
#
# 只匹配「指责句式」，不匹配普通关心：
#   命中：「你又熬夜」「你总是熬夜」「你怎么老不吃饭」
#   放过：「你还没睡？」「今天累不累」「熬夜伤身体」
_HABIT_SCOLD = re.compile(
    r"[你您]\s*(?:又|总是|老是|怎么老|天天|成天|整天|老是这么|怎么又)\s*"
    r"(?:熬夜|晚睡|不睡|失眠|不吃饭|不吃早饭|不吃晚餐|节食|减肥|喝酒|加班|通宵(?:打游戏)?|打游戏)"
    # 后面挂的语气词和逗号要一起吃进去，否则会留下「还没睡？了吧」这种残句
    r"[了吧啊呀呢嘛的哦啦]*[，,、]?",
)

# 命中后换成的软问句。不删整句，因为句子里往往还有别的信息。
_SOFT_SWAP = {
    "熬夜": "还没睡？",
    "晚睡": "今天睡得晚吗",
    "不睡": "还不困？",
    "失眠": "最近睡得怎么样",
    "不吃饭": "饭吃了吗",
    "不吃早饭": "早饭吃了吗",
    "不吃晚餐": "晚饭吃了吗",
    "节食": "最近吃得还好吗",
    "减肥": "最近吃得还好吗",
    "喝酒": "最近还好吗",
    "加班": "最近忙不忙",
    "通宵": "昨晚睡够了吗",
    "打游戏": "最近在玩什么",
}


def sanitize_reply(text: str, enabled: bool = True) -> tuple[str, list]:
    """把无依据的习惯指责换成中性问句。

    返回 (处理后的文本, 命中记录)。命中记录只用于日志。
    """
    if not enabled or not text:
        return text, []

    hits = []

    def _sub(m: re.Match) -> str:
        whole = m.group(0)
        hits.append(whole)
        soft = "最近还好吗"
        for key in sorted(_SOFT_SWAP, key=len, reverse=True):
            if key in whole:
                soft = _SOFT_SWAP[key]
                break
        # 正则把后面的逗号也吃掉了，如果原句那儿本该有标点，补回去
        tail = m.string[m.end():m.end() + 1] if m.end() < len(m.string) else ""
        if tail and tail not in "，,。.！!？?、；;：: \n":
            # 换出来的是问句就不补逗号了，「还没睡？早点睡」比「还没睡？，早点睡」顺
            if soft.endswith(("？", "?", "！", "!")):
                return soft
            return soft + "，"
        return soft

    out = _HABIT_SCOLD.sub(_sub, text)
    return out, hits


# ============================================================
# 4. 未完话头
# ============================================================

# 用户回这些，说明只是在应声，没在回答问题
_ACK_ONLY = (
    "嗯", "哦", "噢", "好", "好的", "行", "是", "对", "哈哈", "笑死",
    "没事", "没什么", "不知道", "算了", "随便",
)


class FollowUp:
    """记着 bot 问过什么，别反复问。

    真人问一句「今天怎么样」，你没答，他可能会再问一次；
    但如果他已经问了三次还在问同一句，那就是机器人。

    做法很朴素：bot 的回复里提取问句存起来；用户下一条回复如果
    有实质内容（长度 > 4 且不是纯应声），就把这些问题标成已答；
    没答的作为提示注入，让模型别再问同样的。
    """

    def __init__(self, cfg):
        self._cfg = cfg
        self._asked: dict[str, deque] = {}

    def _limit(self) -> int:
        try:
            return int(self._cfg("followup", "max_pending", default=2) or 2)
        except Exception:  # noqa: BLE001
            return 2

    def _ttl(self) -> float:
        try:
            return float(self._cfg("followup", "ttl_hours", default=6) or 6) * 3600
        except Exception:  # noqa: BLE001
            return 6 * 3600

    @staticmethod
    def _extract_questions(text: str) -> list:
        """从 bot 的回复里抠出问句，最多两条。"""
        out = []
        for seg in re.split(r"(?<=[。！？!?…\n])", text or ""):
            seg = seg.strip()
            if not seg or len(seg) > 40:
                continue
            if seg.endswith(("？", "?")) or "吗" in seg or "呢" in seg or "怎么" in seg:
                out.append(seg.rstrip("？?"))
            if len(out) >= 2:
                break
        return out

    def note_bot_reply(self, key: str, text: str) -> None:
        qs = self._extract_questions(text)
        if not qs:
            return
        dq = self._asked.setdefault(key, deque(maxlen=8))
        now = time.time()
        for q in qs:
            dq.append((q, now, "open", ""))

    def _semantic(self) -> bool:
        try:
            return bool(self._cfg("followup", "semantic_review", default=True))
        except Exception:  # noqa: BLE001
            return True

    def note_user_reply(self, key: str, text: str) -> None:
        t = (text or "").strip()
        if len(t) <= 4:
            return
        if t in _ACK_ONLY or any(t == a for a in _ACK_ONLY):
            return
        dq = self._asked.get(key)
        if not dq:
            return
        # 语义复核：用户回了「实质内容」不代表真的回答了那个问题。
        # 你问「吃了吗」，他回「今天好累」——长度够、不是应声，但压根没答。
        # 所以这里不直接判已答，而是标「待复核」，注入时把「问什么 + 回什么」
        # 一起交给模型自己判断。关掉 semantic_review 就退回「有字即已答」。
        if self._semantic():
            self._asked[key] = deque(
                [(q, ts, ("review" if st != "answered" else st), t[:40])
                 for (q, ts, st, _r) in dq],
                maxlen=8,
            )
        else:
            self._asked[key] = deque(
                [(q, ts, "answered", t[:40]) for (q, ts, st, _r) in dq], maxlen=8
            )

    def pending_hint(self, key: str) -> str:
        """还没答完的问题，压成提示（含语义复核）。"""
        dq = self._asked.get(key)
        if not dq:
            return ""
        now = time.time()
        ttl = self._ttl()
        alive = [(q, ts, st, r) for (q, ts, st, r) in dq if now - ts <= ttl]
        self._asked[key] = deque(alive, maxlen=8)
        open_q = [q for (q, _ts, st, _r) in alive if st == "open"]
        review = [(q, r) for (q, _ts, st, r) in alive if st == "review"]
        if not open_q and not review:
            return ""
        lines = []
        if open_q:
            todo = open_q[-self._limit():]
            # 同一句问过两次以上，明确警告
            dup = len(todo) != len(set(todo))
            body = "；".join(f"「{q}」" for q in todo)
            tail = "（这个你问过了，别再问第二遍）" if dup else "（他没正面答，别追问）"
            lines.append(f"[你刚问过但对方没答的] {body}{tail}")
        if review:
            parts = []
            for q, r in review[-self._limit():]:
                parts.append(f"你问「{q}」，对方回「{r}」")
            lines.append(
                "[判断一下] " + "；".join(parts)
                + "——对方的回复要是已经回答了你的问题，就别再追问同一件事；没答完才接着问。"
            )
        return "\n\n" + "\n".join(lines)

    def clear(self, key: str) -> None:
        self._asked.pop(key, None)


# ============================================================
# 5. 气氛保护硬收口
# ============================================================


class AirGuard:
    """连续插上话之后强制安静。

    调高插话概率以后会出现另一种怪：群里聊得正热闹，bot 每轮都能插进去，
    连着四五句全是它，比刷屏还讨嫌。

    规则：连续插话次数达到阈值 → 后续「没点名」的消息一律旁听，
    直到有人 @ 它、引用它，或者安静超过冷却时间。
    """

    def __init__(self, cfg):
        self._cfg = cfg
        self._streak: dict[str, int] = {}
        self._last: dict[str, float] = {}

    def _limit(self) -> int:
        try:
            return int(self._cfg("air_guard", "max_streak", default=3) or 3)
        except Exception:  # noqa: BLE001
            return 3

    def _cooldown(self) -> float:
        try:
            return float(self._cfg("air_guard", "cooldown_seconds", default=180) or 180)
        except Exception:  # noqa: BLE001
            return 180.0

    def note_spoke(self, gid: str) -> None:
        """bot 主动插了一句（不是被 @ 的）。"""
        self._streak[gid] = self._streak.get(gid, 0) + 1
        self._last[gid] = time.time()

    def note_mention(self, gid: str) -> None:
        """被 @ 或引用了，不算插话，重置。"""
        self._streak[gid] = 0
        self._last[gid] = time.time()

    def should_hold(self, gid: str) -> bool:
        """现在是不是该闭嘴。"""
        streak = self._streak.get(gid, 0)
        if streak < self._limit():
            return False
        # 安静够久就放过
        if time.time() - (self._last.get(gid) or 0) > self._cooldown():
            self._streak[gid] = 0
            return False
        return True


# ============================================================
# 6. 保护引文分段
# ============================================================

# 这些标点会让「引号/括号内部」不参与断句
_OPENERS = "「『（(【[《"
_CLOSERS = "」』）)】]》"
_QUOTES = "\"“”‘’'"


def split_protected(text: str, min_part: int = 10) -> list:
    """按句末标点断句，但引号、括号内部不断。

    以前的实现是纯正则 `[^。！？]+[。！？]*`，于是：
        「他跟我说：『我不去了。你自己去吧。』」
    会在中间的句号处被切成两个气泡，引语断成两截，一眼假。

    这里改成手写扫描：维护一个「嵌套深度」，深度 > 0 时任何标点都不算句末。
    """
    text = text or ""
    if not text.strip():
        return [text]

    parts: list[str] = []
    cur: list[str] = []
    depth = 0
    quote_open = False

    def _flush() -> None:
        s = "".join(cur).strip()
        if s:
            parts.append(s)
        cur.clear()

    for ch in text:
        cur.append(ch)

        if ch in _OPENERS:
            depth += 1
            continue
        if ch in _CLOSERS:
            depth = max(0, depth - 1)
            continue
        if ch in _QUOTES:
            # 直引号没有方向，用奇偶判断
            quote_open = not quote_open
            continue

        if depth > 0 or quote_open:
            continue

        if ch in "。！？!?…~":
            # 连续的句末标点一起收（「真的吗？！」）
            _flush()

    _flush()
    if not parts:
        return [text]

    # 太短的段并进前一段，避免「哎」「好啊」各成一条
    merged: list = []
    for p in parts:
        if merged and len(p) < min_part:
            merged[-1] = merged[-1] + p
        else:
            merged.append(p)
    return merged or [text]


# ============================================================
# 7. 说说由头
# ============================================================


def post_seed(hour: int, weekday: int, life_line: str = "", recent_topic: str = "") -> str:
    """给「现在发说说」找一个具体理由。

    原来的保底逻辑是「48 小时没发 → 发一条」，模型收到的是「你要发一条说说」，
    于是写出来的东西像交作业。

    给一个由头之后，它写的是「此刻发生的事」：
    - 现在是几点（凌晨和下午的心境完全不同）
    - 今天周几（周日的下午天然适合发懒）
    - 角色此刻在干什么（`life_line`）
    - 刚才群里聊到什么（`recent_topic`）
    """
    bits = []

    if 0 <= hour < 6:
        bits.append("现在是凌晨，你还没睡")
    elif 6 <= hour < 9:
        bits.append("早上，刚醒不久")
    elif 9 <= hour < 12:
        bits.append("上午")
    elif 12 <= hour < 14:
        bits.append("中午，正犯困")
    elif 14 <= hour < 18:
        bits.append("下午")
    elif 18 <= hour < 22:
        bits.append("晚上")
    else:
        bits.append("夜里了")

    if weekday >= 5:
        bits.append("周末")
    elif weekday == 0:
        bits.append("周一")

    if life_line:
        bits.append(f"你此刻：{life_line}")

    if recent_topic:
        bits.append(f"刚才有人聊到「{recent_topic[:24]}」")

    if not bits:
        return ""
    return "\n\n[此刻] " + "；".join(bits) + "\n（说说从这儿长出来，别硬凑）"


# ============================================================
# 8. 撤回沉默 + 撤回摘要
# ============================================================


class RecallSilence:
    """对方撤回消息后的两件事：别对着已撤回的话继续回 + 能答「撤回了什么」。

    真人不会对一条已经被撤回的消息长篇大论——话都收回了，还追着回，
    一眼假。这里维护两块内存态：

    1. 已撤回消息的标识集合（scope + message_id）：on_decorating_result 里
       查到「触发本轮回复的那条消息已被撤回」，就把整条回复拦掉。
    2. 最近撤回的有界摘要：管理员问「刚才撤回了什么」时注入最近几条。

    撤回是即时态，重启即清空，不做持久化。
    """

    def __init__(self, cfg):
        self._cfg = cfg
        self._recalled: dict[str, float] = {}   # "scope:message_id" -> 撤回时间
        self._log: dict[str, deque] = {}        # scope -> deque[(ts, sender, text)]

    def _on(self) -> bool:
        try:
            return bool(self._cfg("recall", "enabled", default=True))
        except Exception:  # noqa: BLE001
            return True

    def _window(self) -> float:
        try:
            return float(self._cfg("recall", "window_minutes", default=30) or 30) * 60
        except Exception:  # noqa: BLE001
            return 30 * 60

    def _prune(self) -> None:
        now = time.time()
        w = self._window()
        for k in list(self._recalled):
            if now - self._recalled[k] > w:
                self._recalled.pop(k, None)

    def mark(self, scope: str, message_id: str, sender: str, text: str) -> None:
        if not self._on():
            return
        mid = str(message_id or "").strip()
        if not mid:
            return
        self._prune()
        self._recalled[f"{scope}:{mid}"] = time.time()
        # 上限保护：超了删最老的一半，长跑不攒垃圾
        if len(self._recalled) > 600:
            items = sorted(self._recalled.items(), key=lambda x: x[1])
            for k, _v in items[:300]:
                self._recalled.pop(k, None)
        dq = self._log.setdefault(scope, deque(maxlen=8))
        dq.append((time.time(), (sender or "").strip(), (text or "").strip()[:60]))

    def is_recalled(self, scope: str, message_id: str) -> bool:
        if not self._on():
            return False
        mid = str(message_id or "").strip()
        if not mid:
            return False
        self._prune()
        return f"{scope}:{mid}" in self._recalled

    def recent(self, scope: str, limit: int = 5):
        now = time.time()
        w = self._window()
        dq = self._log.get(scope) or []
        return [(s, t) for (ts, s, t) in dq if now - ts <= w][-limit:]

    def summary_text(self, scope: str, limit: int = 5) -> str:
        items = self.recent(scope, limit)
        if not items:
            return ""
        lines = [f"{s or '有人'}撤回了：「{t}」" for (s, t) in items]
        return "\n".join(lines)
