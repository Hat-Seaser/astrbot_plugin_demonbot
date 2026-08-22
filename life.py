"""
动态生活状态模块（合并自 astrbot_plugin_dynamic_life_state，并做了三处关键改造）

原插件做的事：给 bot 生成一天的生活时间线，每次对话都把「当前时间段在干什么」
注入提示词。思路很好，但直接用会有一个很出戏的毛病——

**模型每次都会把状态念出来。**
你问「在吗」，它答「在的，我正在吃午饭」；你问「这题怎么做」，它答
「我刚下班回来，这题嘛……」。同一个时间段里，它会把同一句「我在吃午饭」
重复十几遍，比不加状态还假。

所以这里做了三处改造：

1. **状态默认只影响语气，不出现在字面上。**
   只有下面三种情况才允许把活动说出来：
   a) 对方直接问了（在干嘛 / 睡了没 / 忙不忙）
   b) 刚跨过一个时间段，且这个活动今天还没主动提过（每个活动一天只主动提一次）
   c) 深夜/睡眠时段被戳，这时候说一句「困死了」是合理的
   其余时候注入的是「你现在有点困，语气放慢」这种指令，不给具体活动名。

2. **时间线是每天重新掷骰子的，不是写死的时刻表。**
   工作日/周末两套模板，起床时间、午休长度、晚上干什么都带随机偏移，
   还会按概率插入当天的随机事件（加班、朋友约饭、感冒、快递到了）。
   所以「今天」和「昨天」的作息不一样，这才像个活人。

3. **状态自带 busy（忙碌度）和 sleep（是否在睡）两个信号**，
   主程序可以用它们来决定：要不要主动插话、回得快还是慢、
   要不要顺手发一张睡觉表情包。

这个模块不请求 LLM，不联网，纯本地计算，一分钱不花。
"""

from __future__ import annotations

import hashlib
import random
import time

# ---------------------------------------------------------------- 时间线模板
#
# 每个片段：(标签, 活动描述, 时长分钟数的范围, 心情基线 -1~1, 忙碌度 0~1, 是否在睡)
# 时间线是从「起床时间」开始按顺序累加出来的，所以调整起床时间会整体平移，
# 不用手改每一个时刻。

WEEKDAY_BLOCKS = [
    ("清晨", "刚起床，洗漱、找衣服，人还没醒透", (25, 45), -0.15, 0.4, False),
    ("早高峰", "在赶去上班的路上，挤地铁", (30, 55), -0.25, 0.6, False),
    ("上午", "在工位上处理事情，有点忙", (150, 200), -0.05, 0.8, False),
    ("中午", "吃午饭，顺便刷会儿手机", (40, 60), 0.30, 0.1, False),
    ("午休", "趴桌上眯一会儿", (20, 40), 0.10, 0.2, True),
    ("下午", "下午的活，犯困但还得干", (170, 220), -0.15, 0.75, False),
    ("下班路上", "刚下班，在回家路上", (30, 50), 0.20, 0.4, False),
    ("晚饭", "在吃晚饭", (35, 55), 0.35, 0.1, False),
    ("晚上", None, (150, 210), 0.35, 0.1, False),          # None = 从晚间活动池里抽
    ("睡前", "躺床上刷手机，准备睡了", (40, 70), 0.05, 0.1, False),
    ("深夜", "睡着了", (300, 420), -0.30, 0.0, True),
]

WEEKEND_BLOCKS = [
    ("睡懒觉", "还在赖床，不想起", (60, 120), 0.10, 0.1, True),
    ("上午", "起来了，慢悠悠地吃早午饭", (60, 110), 0.30, 0.1, False),
    ("下午", None, (180, 260), 0.35, 0.2, False),
    ("傍晚", "在外面晃，或者刚到家", (60, 100), 0.30, 0.2, False),
    ("晚饭", "在吃饭", (40, 60), 0.35, 0.1, False),
    ("晚上", None, (180, 240), 0.40, 0.1, False),
    ("睡前", "躺着刷手机，舍不得睡", (50, 90), 0.10, 0.1, False),
    ("深夜", "睡了", (300, 420), -0.30, 0.0, True),
]

# 晚间/周末白天的活动池，每天随机抽
EVENING_POOL = [
    "在打游戏", "在追剧", "在看直播", "在听歌发呆", "在刷短视频",
    "在画画", "在收拾房间", "在跟朋友语音", "在看书", "在健身",
]
WEEKEND_DAY_POOL = [
    "出门逛街", "在家躺着什么也不想干", "跟朋友出去吃饭", "在补觉",
    "在打游戏", "去看了场电影", "在咖啡店坐着", "在收拾屋子",
]

# 当天可能发生的随机事件：(描述, 概率, 心情修正, 忙碌度修正)
RANDOM_EVENTS = [
    ("今天要加班，心情不太好", 0.12, -0.30, 0.25),
    ("今天有点感冒，没什么精神", 0.08, -0.25, 0.0),
    ("今天发工资了，心情不错", 0.06, 0.35, 0.0),
    ("快递到了，是等了很久的东西", 0.08, 0.25, 0.0),
    ("昨晚没睡好，一整天都困", 0.12, -0.20, 0.0),
    ("今天天气很好，心情挺松快", 0.10, 0.20, 0.0),
    ("今天摸鱼一整天，很爽", 0.08, 0.30, -0.25),
    ("虽然今天是周末，但临时被喊去加班", 0.08, -0.25, 0.35),
    ("临时多了一趟外快/兼职，晚上会忙一点", 0.06, 0.10, 0.30),
    ("朋友突然约饭，原来的计划被打乱了", 0.08, 0.22, 0.10),
]


def _hm(minutes: int) -> str:
    minutes %= 1440
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


class LifeState:
    """
    一天的生活时间线。

    用法：
        life = LifeState(seed_salt="焦糖")
        st = life.current(now_minutes)      # 当前状态字典
        st["activity"] / st["label"] / st["mood"] / st["busy"] / st["sleeping"]
    """

    def __init__(self, seed_salt: str = "", wake_hour: int = 7, weekend_wake_hour: int = 10):
        self.seed_salt = str(seed_salt or "")
        self.wake_hour = int(wake_hour)
        self.weekend_wake_hour = int(weekend_wake_hour)
        self.day_key = ""
        self.timeline: list = []
        self.event: str = ""
        self._event_mood = 0.0
        self._event_busy = 0.0
        # 「今天已经主动交代过的活动」，保证同一件事一天只说一次
        self._told: set = set()

    # ---------- 生成 ----------

    def _rng(self, day_key: str) -> random.Random:
        """按「日期+盐」定种：同一天内无论重启多少次，作息表都是同一份。"""
        h = hashlib.md5(f"{day_key}|{self.seed_salt}".encode("utf-8")).hexdigest()
        return random.Random(int(h[:12], 16))

    def build(self, day_key: str, is_weekend: bool) -> None:
        rng = self._rng(day_key)
        blocks = WEEKEND_BLOCKS if is_weekend else WEEKDAY_BLOCKS
        start_hour = self.weekend_wake_hour if is_weekend else self.wake_hour
        cursor = start_hour * 60 + rng.randint(-25, 35)

        self.timeline = []
        for label, activity, (lo, hi), mood, busy, sleeping in blocks:
            dur = rng.randint(lo, hi)
            if activity is None:
                pool = WEEKEND_DAY_POOL if (is_weekend and label != "晚上") else EVENING_POOL
                activity = rng.choice(pool)
            self.timeline.append({
                "start": cursor % 1440,
                "end": (cursor + dur) % 1440,
                "label": label,
                "activity": activity,
                "mood": mood,
                "busy": busy,
                "sleeping": sleeping,
            })
            cursor += dur

        # 当天随机事件
        self.event, self._event_mood, self._event_busy = "", 0.0, 0.0
        for desc, prob, dm, db in RANDOM_EVENTS:
            if rng.random() < prob:
                self.event, self._event_mood, self._event_busy = desc, dm, db
                break

        if is_weekend and self.event == "虽然今天是周末，但临时被喊去加班":
            candidates = [b for b in self.timeline if b["label"] in ("下午", "傍晚", "晚上")]
            if candidates:
                blk = rng.choice(candidates)
                blk["activity"] = rng.choice([
                    "临时去单位加班，手上的活没收尾",
                    "周末被抓来加班，电脑一开就是半天",
                    "本来想休息，结果临时多了点工作",
                ])
                blk["mood"] = min(blk["mood"], -0.15)
                blk["busy"] = max(blk["busy"], 0.8)

        self.day_key = day_key
        self._told = set()

    def ensure(self, day_key: str, is_weekend: bool) -> None:
        if self.day_key != day_key or not self.timeline:
            self.build(day_key, is_weekend)

    # ---------- 查询 ----------

    def current(self, now_minutes: int) -> dict:
        """返回当前时间段的状态。时间线没盖住的缝隙按「深夜/睡着」处理。"""
        if not self.timeline:
            return {
                "label": "深夜", "activity": "睡着了", "mood": -0.3,
                "busy": 0.0, "sleeping": True, "start": 0, "end": 0,
                "event": self.event, "just_changed": False,
            }
        now = int(now_minutes) % 1440
        found = None
        for blk in self.timeline:
            s, e = blk["start"], blk["end"]
            if s <= e:
                if s <= now < e:
                    found = blk
                    break
            elif now >= s or now < e:      # 跨零点
                found = blk
                break
        if found is None:
            found = self.timeline[-1]      # 缝隙 -> 归到深夜块
        out = dict(found)
        out["mood"] = max(-1.0, min(1.0, out["mood"] + self._event_mood))
        out["busy"] = max(0.0, min(1.0, out["busy"] + self._event_busy))
        out["event"] = self.event
        # 刚进入这个时间段 20 分钟内算「刚换状态」，这时候提一嘴最自然
        s = out["start"]
        delta = (now - s) % 1440
        out["just_changed"] = delta <= 20
        out["key"] = f"{out['label']}|{out['activity']}"
        return out

    def mark_told(self, key: str) -> None:
        self._told.add(key)

    def already_told(self, key: str) -> bool:
        return key in self._told

    def schedule_text(self) -> str:
        """/作息 用的可读时间线。"""
        if not self.timeline:
            return "（还没生成今天的作息）"
        lines = []
        for blk in self.timeline:
            lines.append(
                f"{_hm(blk['start'])}-{_hm(blk['end'])} {blk['label']}：{blk['activity']}"
            )
        if self.event:
            lines.append(f"今天的小插曲：{self.event}")
        return "\n".join(lines)


# ---------------------------------------------------------------- 提示词片段

def mood_label(score: float) -> str:
    if score > 0.5:
        return "心情很好，兴致挺高"
    if score > 0.15:
        return "心情不错，还挺乐呵"
    if score > -0.15:
        return "心情比较平淡，中规中矩"
    if score > -0.5:
        return "有点烦躁或没精神，不太想多说话"
    return "心情很差，很累或者很丧"


def build_fragment(state: dict, score: float, *, tell_activity: bool, is_owner: bool = False) -> str:
    """
    生成注入给模型的状态片段。

    tell_activity=False 时**绝不写出活动名**——这是解决「每次都报告在干嘛」的关键。
    模型看不到「我在吃午饭」这几个字，就不会把它念出来。
    """
    feel = mood_label(score)
    if state.get("sleeping") and not tell_activity:
        return (
            f"\n\n[状态]现在是{state.get('label')}，你本来已经睡了，被消息吵醒，"
            f"{feel}。回得短一点、带点没睡醒的迷糊感，但别说明自己在睡觉。"
        )
    if not tell_activity:
        busy = state.get("busy", 0.0)
        tempo = "手上有事，回得简短些" if busy >= 0.6 else "比较闲，语气可以松弛点"
        return (
            f"\n\n[状态]现在{state.get('label')}，{feel}，{tempo}。"
            f"只让语气带上这种状态，不要说出自己在做什么、在哪、吃没吃睡没睡。"
        )
    # 允许说出来的那次
    who = "对方是你主人" if is_owner else "对方"
    return (
        f"\n\n[状态]现在{state.get('label')}，你{state.get('activity')}，{feel}。"
        f"{who}问起了，就顺口提一句在干嘛，一句话带过，别展开描述，之后别再重复说。"
        + (f"（今天{state.get('event')}）" if state.get("event") else "")
    )
