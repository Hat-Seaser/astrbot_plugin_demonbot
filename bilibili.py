"""B 站内容抓取（热门 / 分区排行）。

只走公开接口，不需要登录、不需要 wbi 签名——签名接口改动频繁，用不了几天就失效，
所以这里刻意只用稳定的两个端点。

拿到的是一条条结构化的视频信息（标题、UP 主、分区、时长、播放量、简介），
由主程序交给模型转成"人话"，避免模型凭空编造视频内容。
"""

from __future__ import annotations

import random
import time
from typing import Any

import aiohttp

__all__ = [
    "BiliError",
    "fetch_popular",
    "fetch_ranking",
    "pick_one",
    "format_for_prompt",
    "format_for_reply",
]

API_POPULAR = "https://api.bilibili.com/x/web-interface/popular"
API_RANKING = "https://api.bilibili.com/x/web-interface/ranking/v2"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Mobile Safari/537.36"
    ),
    "Referer": "https://www.bilibili.com/",
    "Accept": "application/json, text/plain, */*",
}

# 分区 id -> 分区名，只列常用的几个，用来让"刷视频"有点偏好
TID_NAMES = {
    0: "全站",
    1: "动画",
    3: "音乐",
    4: "游戏",
    5: "娱乐",
    36: "科技",
    119: "鬼畜",
    129: "舞蹈",
    155: "时尚",
    160: "生活",
    168: "国创相关",
    181: "影视",
    188: "数码",
    211: "美食",
    217: "动物圈",
}


class BiliError(Exception):
    """接口不可用或返回异常。"""


async def _get_json(url: str, params: dict, timeout: float = 15.0) -> dict:
    try:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=timeout), headers=_HEADERS
        ) as sess:
            async with sess.get(url, params=params) as resp:
                if resp.status != 200:
                    raise BiliError(f"HTTP {resp.status}")
                data = await resp.json(content_type=None)
    except aiohttp.ClientError as exc:
        raise BiliError(f"网络错误：{type(exc).__name__}") from exc
    except TimeoutError as exc:
        raise BiliError("请求超时") from exc

    code = data.get("code")
    if code != 0:
        raise BiliError(f"接口返回 code={code} {data.get('message') or ''}".strip())
    return data


def _fmt_count(n: Any) -> str:
    try:
        n = int(n)
    except (TypeError, ValueError):
        return "0"
    if n >= 100000000:
        return f"{n / 100000000:.1f}亿"
    if n >= 10000:
        return f"{n / 10000:.1f}万"
    return str(n)


def _fmt_duration(sec: Any) -> str:
    try:
        sec = int(sec)
    except (TypeError, ValueError):
        return ""
    if sec <= 0:
        return ""
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def _norm(item: dict) -> dict:
    """把接口返回压成统一结构，主程序只认这几个字段。"""
    stat = item.get("stat") or {}
    owner = item.get("owner") or {}
    bvid = item.get("bvid") or ""
    return {
        "bvid": bvid,
        "url": f"https://www.bilibili.com/video/{bvid}" if bvid else "",
        "title": (item.get("title") or "").strip(),
        "desc": (item.get("desc") or "").strip()[:160],
        "up": (owner.get("name") or "").strip(),
        "tid": item.get("tid"),
        "tname": (item.get("tname") or "").strip(),
        "duration": _fmt_duration(item.get("duration")),
        "view": _fmt_count(stat.get("view")),
        "like": _fmt_count(stat.get("like")),
        "danmaku": _fmt_count(stat.get("danmaku")),
        "pubdate": item.get("pubdate") or 0,
        "fetched_at": int(time.time()),
    }


async def fetch_popular(limit: int = 10, page: int = 1, timeout: float = 15.0) -> list:
    """热门视频。每页最多 20 条。"""
    limit = max(1, min(int(limit or 10), 20))
    data = await _get_json(API_POPULAR, {"ps": limit, "pn": max(1, int(page or 1))}, timeout)
    items = ((data.get("data") or {}).get("list")) or []
    return [_norm(v) for v in items if v.get("bvid")][:limit]


async def fetch_ranking(tid: int = 0, limit: int = 20, timeout: float = 15.0) -> list:
    """分区排行榜。tid=0 为全站。"""
    data = await _get_json(API_RANKING, {"rid": int(tid or 0), "type": "all"}, timeout)
    items = ((data.get("data") or {}).get("list")) or []
    out = [_norm(v) for v in items if v.get("bvid")]
    return out[: max(1, int(limit or 20))]


async def collect(pages: int = 3, per_page: int = 20, timeout: float = 15.0) -> list:
    """抓多页热门并去重，供随机挑选用。任何一页失败都不影响其它页。"""
    seen, out = set(), []
    for p in range(1, max(1, int(pages)) + 1):
        try:
            batch = await fetch_popular(limit=per_page, page=p, timeout=timeout)
        except BiliError:
            if p == 1:
                raise
            break
        for v in batch:
            if v["bvid"] and v["bvid"] not in seen:
                seen.add(v["bvid"])
                out.append(v)
    return out


def pick_one(videos: list, exclude: set | None = None) -> dict | None:
    """随机挑一条，排除最近已经聊过的。"""
    if not videos:
        return None
    pool = [v for v in videos if v["bvid"] not in (exclude or set())]
    if not pool:
        pool = videos
    return random.choice(pool)


def format_for_prompt(video: dict, include_stats: bool = False) -> str:
    """喂给模型的事实清单。明确告诉它这些是全部已知信息，别编。

    默认【不】把播放量/点赞/弹幕塞进去。原因很直接：模型手里有这个数字
    就一定会说出口——「我刷到一个 45 万播放的视频」这种话，真人不会讲，
    而且一说就把话题从「内容」拽到「这视频好火」，反而更假。
    只有明确要「播报热榜」时才传 include_stats=True。
    """
    lines = [
        f"标题：{video.get('title') or '（无）'}",
        f"UP主：{video.get('up') or '（未知）'}",
        f"分区：{video.get('tname') or '（未知）'}",
    ]
    if video.get("duration"):
        lines.append(f"时长：{video['duration']}")
    if video.get("desc"):
        lines.append(f"简介：{video['desc']}")
    if include_stats:
        lines.append(
            f"数据：播放 {video.get('view') or 0}，点赞 {video.get('like') or 0}，"
            f"弹幕 {video.get('danmaku') or 0}"
        )
    else:
        lines.append("（没有播放量、点赞、弹幕这些数据，也不许提这些数字）")
    return "\n".join(lines)


def format_for_reply(video: dict) -> str:
    """纯文本兜底（模型不可用时直接发这个）。"""
    parts = [video.get("title") or ""]
    if video.get("up"):
        parts.append(f"@{video['up']}")
    if video.get("tname"):
        parts.append(f"【{video['tname']}】")
    if video.get("duration"):
        parts.append(video["duration"])
    head = " ".join(x for x in parts if x)
    desc = str(video.get("desc") or "").strip().replace("\n", " ")
    tail = f"\n{desc[:60]}" if desc else ""
    return f"{head}{tail}\n{video.get('url') or ''}".strip()
