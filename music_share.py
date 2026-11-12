"""Apple Music 歌单 → 随机推歌，给 QQ 空间的音乐分享用。

为什么自己抓：Apple Music 没有免鉴权的公开歌单接口，
但歌单网页里内嵌了 `serialized-server-data` 的 JSON，
里面有完整的曲目名、歌手、封面模板，够用了。
封面模板形如 https://.../{w}x{h}bb.{f}，把占位符换成尺寸就是真图。
"""
from __future__ import annotations

import json
import random
import re
import time
import urllib.parse
import urllib.request
from pathlib import Path

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15"
)
_DATA_RE = re.compile(
    r'<script[^>]*id="serialized-server-data"[^>]*>(.*?)</script>', re.S
)

# 主人的「喜爱歌曲」歌单。大的那个 228 首，是全集；小的 129 首，合一并去重。
DEFAULT_PLAYLISTS = (
    "https://music.apple.com/cn/playlist/pl.u-6mo4aP3I809P4xZ",
    "https://music.apple.com/cn/playlist/pl.u-ZmblxdoFVaXvZJz",
)


def _http_get(url: str, timeout: int = 25) -> bytes:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": UA,
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Accept": "text/html,application/xhtml+xml,image/*,*/*",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def fetch_tracks(url: str, timeout: int = 25) -> list[dict]:
    """抓一个歌单页，返回 [{title, artist, art}]。失败返回空列表。"""
    try:
        html = _http_get(url, timeout=timeout).decode("utf-8", "ignore")
    except Exception:
        return []
    m = _DATA_RE.search(html)
    if not m:
        return []
    try:
        data = json.loads(m.group(1))
    except Exception:
        return []

    out: list[dict] = []
    seen: set[tuple[str, str]] = set()

    def walk(node) -> None:
        if isinstance(node, dict):
            if "artistName" in node and "title" in node and "artwork" in node:
                art = (node.get("artwork") or {}).get("dictionary") or {}
                tpl = str(art.get("url") or "")
                if tpl:
                    key = (str(node["title"]), str(node["artistName"]))
                    if key not in seen:
                        seen.add(key)
                        out.append(
                            {
                                "title": key[0],
                                "artist": key[1],
                                "art": tpl.replace("{w}", "600")
                                .replace("{h}", "600")
                                .replace("{f}", "jpg"),
                            }
                        )
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(data)
    return out


def build_pool(playlists, cache_file: Path, ttl_hours: int = 24) -> list[dict]:
    """抓多个歌单合并去重，缓存到本地，超过 TTL 才重新抓。"""
    cache_file = Path(cache_file)
    now = time.time()
    try:
        if cache_file.is_file():
            cached = json.loads(cache_file.read_text(encoding="utf-8"))
            if now - float(cached.get("ts") or 0) < ttl_hours * 3600:
                tracks = cached.get("tracks") or []
                if tracks:
                    return tracks
    except Exception:
        pass

    pool: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for url in playlists or []:
        for t in fetch_tracks(str(url)):
            key = (t["title"], t["artist"])
            if key not in seen:
                seen.add(key)
                pool.append(t)

    if pool:
        try:
            cache_file.parent.mkdir(parents=True, exist_ok=True)
            cache_file.write_text(
                json.dumps(
                    {"ts": now, "tracks": pool}, ensure_ascii=False, indent=1
                ),
                encoding="utf-8",
            )
        except Exception:
            pass
        return pool

    # 抓失败就退回旧缓存，哪怕过期也比没有强
    try:
        cached = json.loads(cache_file.read_text(encoding="utf-8"))
        return cached.get("tracks") or []
    except Exception:
        return []


def pick_one(pool: list[dict], exclude: tuple[str, ...] = ()) -> dict | None:
    """随机挑一首，尽量避开最近发过的。"""
    if not pool:
        return None
    fresh = [t for t in pool if t.get("title") not in exclude]
    return random.choice(fresh or pool)


def download_cover(track: dict, timeout: int = 20) -> bytes | None:
    url = str(track.get("art") or "")
    if not url:
        return None
    try:
        blob = _http_get(url, timeout=timeout)
        return blob if blob and len(blob) > 1024 else None
    except Exception:
        return None


def apple_music_search_url(track: dict) -> str:
    """给没装 Apple Music 的人一个可点的链接（完整链接，不是短码）。"""
    q = f"{track.get('title', '')} {track.get('artist', '')}".strip()
    return "https://music.apple.com/cn/search?term=" + urllib.parse.quote(q)
