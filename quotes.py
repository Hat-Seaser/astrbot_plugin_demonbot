"""
文案模块（伤感文案 / 随机情话 / 温柔语录 / 毒鸡汤 / 一言）

用的是一批公开的免费文案接口。这类站点有两个共同特点：
- 随时可能挂、限速、或者换域名；
- 返回格式五花八门，有的是纯文本，有的是 JSON，JSON 里字段名还各不相同。

所以这里不绑死任何一家：每个分类下挂一串候选接口，逐个试，
第一个能返回像样文本的就用，全挂就返回空（由主程序决定说什么）。

**你的日志里有一条很关键的报错**：
    Cannot connect to host api.lolimi.cn:443 ssl:default [Name or service not known]
「Name or service not known」是 DNS 根本解析不出来，不是接口挂了——
说明你那台 Termux 上的 DNS 拿不到这个域名。所以：
1. 我没有把 lolimi 作为唯一来源，每个分类都配了别家接口兜底；
2. 提供 /文案测试，一次性告诉你哪几家在你的网络下真的能通；
3. 全部接口都挂时，还有一份本地兜底句库（LOCAL_FALLBACK），
   保证「/文案 情话」永远有东西发得出来，不会空手而归。

返回的文本会做长度截断和换行清理，避免把一大段排版塞进群里。
"""

from __future__ import annotations

import json
import random
import re

from .websearch import _request

# 分类 -> [(来源名, url, 取值方式), ...]
# 取值方式：
#   "text"        整个响应体就是文案
#   "json:a.b"    JSON，按点号路径取字段
BACKENDS = {
    "情话": [
        ("uomg", "https://api.uomg.com/api/rand.qinghua?format=json", "json:content"),
        ("lolimi", "https://api.lolimi.cn/API/qinghua/api.php?type=json", "json:qinghua"),
        ("oick", "https://api.oick.cn/api/qinghua", "text"),
    ],
    "伤感": [
        ("hitokoto", "https://v1.hitokoto.cn/?c=d&encode=json", "json:hitokoto"),
        ("lolimi", "https://api.lolimi.cn/API/wenan-shanggan/api.php?type=json", "json:data"),
        ("oick", "https://api.oick.cn/api/yulu", "text"),
    ],
    "温柔": [
        ("hitokoto", "https://v1.hitokoto.cn/?c=i&encode=json", "json:hitokoto"),
        ("lolimi", "https://api.lolimi.cn/API/wenan-wy/api.php?type=json", "json:data"),
        ("hitokoto2", "https://international.v1.hitokoto.cn/?c=k&encode=json", "json:hitokoto"),
    ],
    "毒鸡汤": [
        ("lolimi", "https://api.lolimi.cn/API/dujitang/api.php?type=json", "json:data"),
        ("oick", "https://api.oick.cn/api/dutang", "text"),
        ("hitokoto", "https://v1.hitokoto.cn/?c=h&encode=json", "json:hitokoto"),
    ],
    "一言": [
        ("hitokoto", "https://v1.hitokoto.cn/?encode=json", "json:hitokoto"),
        ("hitokoto2", "https://international.v1.hitokoto.cn/?encode=json", "json:hitokoto"),
        ("lolimi", "https://api.lolimi.cn/API/yiyan/api.php", "text"),
    ],
    "舔狗": [
        ("lolimi", "https://api.lolimi.cn/API/tiangou/api.php?type=json", "json:data"),
        ("oick", "https://api.oick.cn/api/dog", "text"),
        ("uomg", "https://api.uomg.com/api/rand.qinghua?format=json", "json:content"),
    ],
    "晚安": [
        ("hitokoto", "https://v1.hitokoto.cn/?c=k&encode=json", "json:hitokoto"),
        ("lolimi", "https://api.lolimi.cn/API/wenan-wy/api.php?type=json", "json:data"),
    ],
}

ALIASES = {
    "情话": "情话", "土味情话": "情话", "撩": "情话", "撩人": "情话",
    "伤感": "伤感", "伤感文案": "伤感", "emo": "伤感", "丧": "伤感",
    "温柔": "温柔", "温柔语录": "温柔", "治愈": "温柔",
    "毒鸡汤": "毒鸡汤", "鸡汤": "毒鸡汤", "毒": "毒鸡汤",
    "一言": "一言", "语录": "一言", "名句": "一言",
    "舔狗": "舔狗", "舔": "舔狗", "舔狗日记": "舔狗",
    "晚安": "晚安", "睡前": "晚安",
}

# 全部接口都不通时的本地兜底。数量不多，但保证功能永远不是「哑」的。
LOCAL_FALLBACK = {
    "情话": [
        "今天的风很甜，大概是路过你身边的时候沾上的。",
        "我数过了，从这里到你那儿，一共要想你一整天。",
        "别人问我图你什么，我说图你恰好也在。",
    ],
    "伤感": [
        "有些话说出口就轻了，所以一直没说。",
        "后来才明白，不是所有等待都有回音。",
        "最难过的不是分开，是还记得当时觉得会一直在一起。",
    ],
    "温柔": [
        "慢一点也没关系，路又不会跑。",
        "今天做得已经够多了，剩下的交给明天。",
        "你不用一直发光，偶尔歇着也很好看。",
    ],
    "毒鸡汤": [
        "努力不一定成功，但不努力一定很舒服。",
        "你不是胖，你只是把未来的自己提前吃掉了。",
        "条条大路通罗马，但有人就出生在罗马。",
    ],
    "一言": [
        "所谓成长，就是把哭声调成静音的过程。",
        "把日子过好，就是最了不起的本事。",
    ],
    "舔狗": [
        "你说你困了，我就把整个夜晚调成静音。",
        "我知道你不会回，但我还是把话发出去了。",
    ],
    "晚安": [
        "今天到此为止吧，剩下的明天再烦。",
        "关灯，闭眼，世界跟你一样该歇了。",
    ],
}

_WS_RE = re.compile(r"\s+")


def normalize_kind(kind: str) -> str:
    k = str(kind or "").strip().lower()
    return ALIASES.get(k, ALIASES.get(str(kind or "").strip(), "一言"))


def kinds() -> list:
    return list(BACKENDS.keys())


def _dig(data, path: str):
    node = data
    for part in path.split("."):
        if isinstance(node, dict):
            node = node.get(part)
        elif isinstance(node, list) and node:
            node = node[0]
            if isinstance(node, dict):
                node = node.get(part)
        else:
            return None
    return node


def _clean(text: str, max_chars: int = 60) -> str:
    if not text:
        return ""
    text = _WS_RE.sub(" ", str(text)).strip().strip('"“”')
    # 有些接口会把整个 HTML 页面吐回来，那就不是文案
    if "<" in text and ">" in text and len(text) > 200:
        return ""
    if len(text) > max_chars:
        text = text[:max_chars].rstrip("，,、 ") + "…"
    return text


async def fetch_one(
    kind: str = "一言",
    timeout: float = 8.0,
    max_chars: int = 60,
    logger=None,
) -> tuple:
    """
    取一条文案。返回 (文本, 来源名)。全挂时用本地兜底，来源名为 "本地"。
    """
    kind = normalize_kind(kind)
    backends = list(BACKENDS.get(kind) or BACKENDS["一言"])
    if kind in ("伤感", "温柔", "一言", "晚安"):
        preferred = [b for b in backends if b[0].startswith("hitokoto")]
        others = [b for b in backends if not b[0].startswith("hitokoto")]
        backends = preferred + others
    for name, url, mode in backends:
        try:
            raw = await _request(url, timeout=timeout)
        except Exception as e:  # noqa: BLE001
            if logger:
                logger.debug(f"[恶魔bot] 文案源 {name} 不通：{type(e).__name__}: {e}")
            continue
        text = ""
        try:
            if mode.startswith("json:"):
                data = json.loads(raw)
                text = _dig(data, mode.split(":", 1)[1]) or ""
                if not text and isinstance(data, dict):
                    for k in ("content", "qinghua", "hitokoto", "data", "msg", "message", "text"):
                        if isinstance(data.get(k), str) and data.get(k).strip():
                            text = data[k]
                            break
            else:
                text = raw
        except Exception:  # noqa: BLE001
            # 声称是 JSON 但返回了纯文本，那就当纯文本用
            text = raw
        text = _clean(text, max_chars)
        if text:
            return text, name
    pool = LOCAL_FALLBACK.get(kind) or LOCAL_FALLBACK["一言"]
    return random.choice(pool), "本地"


async def diagnose(timeout: float = 6.0) -> list:
    """/文案测试 用：逐个体检，返回 [(分类, 来源, 是否可用, 说明), ...]。"""
    report = []
    for kind, backends in BACKENDS.items():
        for name, url, mode in backends:
            try:
                raw = await _request(url, timeout=timeout)
                text = raw
                if mode.startswith("json:"):
                    try:
                        text = _dig(json.loads(raw), mode.split(":", 1)[1]) or ""
                    except Exception:  # noqa: BLE001
                        pass
                text = _clean(text, 24)
                report.append((kind, name, bool(text), text or "连上了但没解析出文案"))
            except Exception as e:  # noqa: BLE001
                msg = str(e)
                if "Name or service not known" in msg or "getaddrinfo" in msg:
                    hint = "DNS 解析不了这个域名"
                elif "timeout" in msg.lower():
                    hint = "超时"
                else:
                    hint = type(e).__name__
                report.append((kind, name, False, hint))
    return report
