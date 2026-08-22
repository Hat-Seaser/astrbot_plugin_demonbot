"""
插图获取模块（Pixiv 排行榜/官方搜索 + 公开索引站 + 真人图）

图源分三类：

1. pixiv（本版新增，默认排第一）
   直接打 Pixiv 自己的两个接口，这是「画师水平高」的唯一可靠来源：
   - 排行榜  https://www.pixiv.net/ranking.php?mode=monthly&content=illust&format=json&p=N
     每页 50 条，p=1、p=2 就是「本月前 100 张」。返回里带 rating_count（收藏/评分数）、
     view_count（浏览数）、illust_page_count（多 p 张数）、tags、原图宽高。
     排行榜本身就是全站按热度排出来的，能进月榜前 100 的基本都是头部画师，
     这比「随机抽 + 按分辨率过滤」在画质上高一个数量级。
     全年龄榜不需要登录；带上 Cookie 只会更稳（不会被风控）。
   - 标签搜索 https://www.pixiv.net/ajax/search/artworks/<tag>
     月榜里凑不出某个冷门标签时用它兜底。Pixiv 的「按热门排序」是会员功能，
     免费号拿不到，所以这里用另一条路：Pixiv 会给作品自动打
     「1000users入り」「5000users入り」「10000users入り」这类收藏量里程碑标签，
     搜索时把它和你的标签一起搜（空格=AND），等价于「收藏数≥N 的作品」。
     这条路需要你的 Pixiv Cookie（PHPSESSID），没配就自动跳过。

2. lolicon / anosu（老图源，保留做兜底）
   公开索引站，无需登录，但返回字段里没有任何热度信号，只能靠分辨率粗筛。

3. real（真人随机图）
   公开随机图接口，多为 302 跳转。

多 p 作品：Pixiv 的多页作品每一页地址是有规律的
  https://i.pximg.net/img-master/img/<年/月/日/时/分/秒>/<pid>_p<页码>_master1200.jpg
所以只要拿到第 0 页地址和 illust_page_count，就能把整套图的地址全推出来，
本模块统一放在结果的 page_urls 字段里，由 main.py 决定是否整套发出。

i.pximg.net 有防盗链且国内不通，所有图片直链都会换成反代域名（默认 i.pixiv.re）。

r18：0=全年龄/擦边；1=成人分区（lolicon/anosu；pixiv 直连排行榜仅全年龄）。
"""

from __future__ import annotations

import json
import random
import re
import time
import urllib.parse
from datetime import date

from .websearch import UA, _request

# ---------------------------------------------------------------- 画质档位

# 画质档位 -> 优先尝试的规格顺序。
# 说明：original 是 Pixiv 原图，一张动辄 5~20MB，发到 QQ 又慢又费流量，
# 而且群里被压缩后跟 regular（约 1200px）几乎看不出差别，所以默认不再用 original。
QUALITY_ORDER = {
    "original": ["original", "regular", "small", "thumb", "mini"],
    "regular":  ["regular", "original", "small", "thumb", "mini"],
    "small":    ["small", "regular", "thumb", "mini", "original"],
    "thumb":    ["thumb", "mini", "small", "regular", "original"],
}

# lolicon 接口 size 参数认的规格名（mini 它不认，用 thumb 兜底）
_LOLICON_SIZES = {"original", "regular", "small", "thumb"}

# anosu 接口 size 参数认的规格名
_ANOSU_SIZE = {
    "original": "original",
    "regular": "regular",
    "small": "regular",
    "thumb": "regular",
}


def normalize_quality(q: str) -> str:
    """把中文档位/别名统一成内部规格名。"""
    q = str(q or "").strip().lower()
    alias = {
        "原图": "original", "原画": "original", "超清": "original", "original": "original",
        "高": "regular", "高清": "regular", "普通": "regular", "标准": "regular",
        "regular": "regular", "high": "regular",
        "中": "small", "中等": "small", "small": "small", "medium": "small", "mid": "small",
        "低": "thumb", "低清": "thumb", "省流": "thumb", "缩略": "thumb",
        "thumb": "thumb", "low": "thumb",
    }
    return alias.get(q, "regular")


def quality_label(q: str) -> str:
    return {"original": "原图", "regular": "高", "small": "中", "thumb": "低"}.get(
        normalize_quality(q), "高"
    )


def _pick_url(urls: dict, quality: str = "regular") -> tuple:
    """按画质档位从 urls 字典里挑一个可用地址。返回 (url, 实际规格)。"""
    urls = urls or {}
    for size in QUALITY_ORDER.get(normalize_quality(quality), QUALITY_ORDER["regular"]):
        u = urls.get(size)
        if u:
            return u, size
    return "", ""


# ---------------------------------------------------------------- Pixiv 直连

PIXIV_HOST_DEFAULT = "https://www.pixiv.net"

# 默认的排行榜模式顺序：月榜优先（题目要求「本月前一百」），冷门标签在月榜里凑不齐时
# 再退到周榜、日榜——都是热度榜，画师水平一样有保证。
DEFAULT_RANK_MODES = ["monthly", "weekly", "daily"]

# 收藏量里程碑标签，从高到低试。10000users入り ≈ 万收藏，属于全站头部。
DEFAULT_USERS_GATE = [10000, 5000, 1000]

# 从缩略图地址里抠出 「日期路径」和「pid」，用来推导多 p 的每一页地址
_PXIMG_RE = re.compile(r"/img/(\d{4}/\d{2}/\d{2}/\d{2}/\d{2}/\d{2})/(\d+)_p\d+")

# 排行榜/搜索结果缓存：{key: (写入时间, 数据)}。
# 榜单一天才动一次，没必要每次发图都去打一遍 Pixiv，既慢又容易被风控。
_RANK_CACHE: dict = {}
_SEARCH_CACHE: dict = {}
_TAG_CACHE: dict = {}


def _cache_get(store: dict, key, ttl: float):
    hit = store.get(key)
    if hit and (time.time() - hit[0]) < ttl:
        return hit[1]
    return None


def _cache_put(store: dict, key, value):
    store[key] = (time.time(), value)
    # 简单收敛，避免长期运行内存里堆一堆过期榜单
    if len(store) > 64:
        for k in sorted(store, key=lambda x: store[x][0])[:32]:
            store.pop(k, None)


def clear_cache():
    _RANK_CACHE.clear()
    _SEARCH_CACHE.clear()
    _TAG_CACHE.clear()


def _pixiv_headers(cookie: str = "", host: str = PIXIV_HOST_DEFAULT) -> dict:
    h = {
        "User-Agent": UA,
        "Referer": f"{host}/",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9,ja;q=0.8,en;q=0.7",
        "x-requested-with": "XMLHttpRequest",
    }
    cookie = (cookie or "").strip()
    if cookie:
        # 只填 PHPSESSID 的值也认，自动补成完整 Cookie 头
        h["Cookie"] = cookie if "=" in cookie else f"PHPSESSID={cookie}"
    return h


def _swap_host(url: str, proxy: str) -> str:
    """把 i.pximg.net 换成反代域名，并去掉 /c/240x480/ 这类缩略图裁切段。"""
    if not url:
        return ""
    if not proxy:
        return url
    u = re.sub(r"^https?://[^/]+", f"https://{proxy}", url)
    u = re.sub(r"/c/[^/]+/", "/", u)
    u = u.replace("/custom-thumb/", "/img-master/")
    return u


def _build_page_urls(
    sample_url: str, pid: str, page_count: int, proxy: str, quality: str, ext: str = "jpg"
) -> list:
    """由第 0 页缩略图地址推导整套多 p 的地址列表。"""
    proxy = proxy or "i.pixiv.re"
    m = _PXIMG_RE.search(sample_url or "")
    if not m:
        # 地址格式不认识时只能用原地址（换个反代域名），多 p 就放弃了
        return [_swap_host(sample_url, proxy)] if sample_url else []
    datepath, real_pid = m.group(1), m.group(2)
    pid = str(pid or real_pid)
    n = max(1, min(int(page_count or 1), 200))
    if normalize_quality(quality) == "original":
        tmpl = f"https://{proxy}/img-original/img/{datepath}/{pid}_p{{i}}.{ext}"
    else:
        tmpl = f"https://{proxy}/img-master/img/{datepath}/{pid}_p{{i}}_master1200.jpg"
    return [tmpl.format(i=i) for i in range(n)]


def _master_urls(sample_url: str, pid: str, page_count: int, proxy: str) -> list:
    return _build_page_urls(sample_url, pid, page_count, proxy, "regular")


async def _head_ok(url: str, timeout: float = 8.0) -> bool:
    """HEAD 探一下地址在不在。原图后缀（jpg/png）只能靠探测确定。"""
    import aiohttp

    try:
        ct = aiohttp.ClientTimeout(total=timeout)
        async with aiohttp.ClientSession(timeout=ct) as sess:
            async with sess.head(
                url, headers={"User-Agent": UA, "Referer": f"{PIXIV_HOST_DEFAULT}/"},
                allow_redirects=True,
            ) as resp:
                return 200 <= resp.status < 300
    except Exception:  # noqa: BLE001
        return False


async def _resolve_original(pic: dict, timeout: float = 8.0) -> None:
    """原图档位时确认后缀：先试 .jpg，不行换 .png，再不行退回 master1200。"""
    urls = pic.get("page_urls") or []
    if not urls or "img-original" not in urls[0]:
        return
    if await _head_ok(urls[0], timeout):
        return
    png = [u[:-4] + ".png" if u.endswith(".jpg") else u for u in urls]
    if png and await _head_ok(png[0], timeout):
        pic["page_urls"] = png
        pic["url"] = png[0]
        return
    fallback = pic.get("_master_urls") or []
    if fallback:
        pic["page_urls"] = fallback
        pic["url"] = fallback[0]
        pic["size_used"] = "regular"


def _norm_rank_item(item: dict, proxy: str, quality: str) -> dict:
    pid = str(item.get("illust_id") or "")
    thumb = item.get("url") or ""
    try:
        page_count = int(item.get("illust_page_count") or 1)
    except (TypeError, ValueError):
        page_count = 1
    pages = _build_page_urls(thumb, pid, page_count, proxy, quality)
    if not pages:
        return {}
    def _int(v):
        try:
            return int(v)
        except (TypeError, ValueError):
            return 0
    return {
        "url": pages[0],
        "page_urls": pages,
        "_master_urls": _master_urls(thumb, pid, page_count, proxy),
        "title": item.get("title", ""),
        "author": item.get("user_name", ""),
        "pid": pid,
        "tags": list(item.get("tags") or []),
        "width": _int(item.get("width")),
        "height": _int(item.get("height")),
        "page_count": page_count,
        "bookmarks": _int(item.get("rating_count")),
        "views": _int(item.get("view_count")),
        "rank": _int(item.get("rank")),
        "size_used": "original" if normalize_quality(quality) == "original" else "master1200",
        "source": "pixiv榜",
        "r18": False,
    }


async def pixiv_ranking(
    mode: str = "monthly",
    content: str = "illust",
    pages: int = 2,
    proxy: str = "i.pixiv.re",
    timeout: float = 12.0,
    cookie: str = "",
    host: str = PIXIV_HOST_DEFAULT,
    quality: str = "regular",
    cache_seconds: float = 21600,
    logger=None,
) -> list:
    """拉排行榜。pages=2 就是前 100 名（每页 50 条），按名次顺序返回。"""
    host = (host or PIXIV_HOST_DEFAULT).rstrip("/")
    pages = max(1, min(int(pages or 1), 10))
    key = (host, mode, content, pages, date.today().isoformat())
    raw_items = _cache_get(_RANK_CACHE, key, cache_seconds)

    # 缓存里存的是接口原样返回的条目，取出来照样要过一遍归一化，
    # 否则命中缓存那次拿到的会是没有 url / 宽高还是字符串的生数据。
    if raw_items is None:
        raw_items = []
        for p in range(1, pages + 1):
            url = f"{host}/ranking.php?mode={mode}&content={content}&format=json&p={p}"
            try:
                body = await _request(url, timeout=timeout, headers=_pixiv_headers(cookie, host))
                data = json.loads(body)
            except Exception as e:  # noqa: BLE001
                if logger:
                    logger.debug(f"[恶魔bot] Pixiv {mode} 榜第 {p} 页失败：{type(e).__name__}: {e}")
                break
            chunk = data.get("contents") or []
            if not chunk:
                break
            raw_items.extend(chunk)
        if not raw_items:
            return []
        _cache_put(_RANK_CACHE, key, raw_items)

    out = []
    for it in raw_items:
        n = _norm_rank_item(it, proxy, quality)
        if n:
            out.append(n)
    return out


def _search_month_range(month_only: bool) -> tuple:
    if not month_only:
        return "", ""
    today = date.today()
    return today.replace(day=1).isoformat(), today.isoformat()


def _contains_japanese(text: str) -> bool:
    return bool(re.search(r"[ぁ-ゟ゠-ヿ々〆ヵヶ]", str(text or "")))


def _flatten_tag_candidates(value) -> list:
    out = []
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, list):
        for x in value:
            out.extend(_flatten_tag_candidates(x))
        return out
    if not isinstance(value, dict):
        return out
    for key in ("tag", "name", "tagName", "word", "keyword"):
        v = value.get(key)
        if isinstance(v, str) and v.strip():
            out.append(v.strip())
    for key in ("tags", "data", "suggestions", "results", "items"):
        if key in value:
            out.extend(_flatten_tag_candidates(value.get(key)))
    return out


def _score_resolved_tag(tag: str, wanted: str, translation: str = "", freq: int = 0) -> int:
    tag = str(tag or "").strip()
    wanted = str(wanted or "").strip().lower()
    translation = str(translation or "").strip().lower()
    if not tag:
        return -999
    score = 0
    if translation == wanted:
        score += 120
    elif wanted and wanted in translation:
        score += 80
    elif translation and translation in wanted:
        score += 55
    if tag == wanted:
        score += 100
    if _contains_japanese(tag):
        score += 18
    score += min(int(freq or 0), 12) * 2
    return score


async def resolve_pixiv_tags(
    word: str,
    *,
    cookie: str = "",
    host: str = PIXIV_HOST_DEFAULT,
    timeout: float = 10.0,
    max_tags: int = 4,
    cache_seconds: float = 86400.0,
    logger=None,
) -> list:
    """利用 Pixiv 自己的标签翻译/相关标签/作品标签，把中文词扩展成多个实际标签。"""
    word = str(word or "").strip()
    if not word:
        return []
    host = (host or PIXIV_HOST_DEFAULT).rstrip("/")
    key = (host, word, "zh")
    cached = _cache_get(_TAG_CACHE, key, cache_seconds)
    if cached is not None:
        return list(cached)

    scores, translations, freq = {}, {}, {}

    def add_candidate(tag: str, score_hint: int = 0, translation: str = ""):
        tag = str(tag or "").strip()
        if not tag or len(tag) > 80:
            return
        if re.fullmatch(r"[\u4e00-\u9fff\u3400-\u4dbf\s·・()（）]+", tag) and tag != word:
            return
        translations[tag] = translation or translations.get(tag, "")
        freq[tag] = freq.get(tag, 0) + 1
        scores[tag] = max(scores.get(tag, -999), score_hint)

    headers = _pixiv_headers(cookie, host)

    urls = [
        f"{host}/ajax/search/tags/{urllib.parse.quote(word)}?lang=zh",
        f"{host}/ajax/tags/suggest_by_word?word={urllib.parse.quote(word)}&content_types_to_count%5B%5D=illust&lang=zh",
    ]
    for url in urls:
        try:
            body = await _request(url, timeout=timeout, headers=headers)
            payload = json.loads(body)
            body_obj = payload.get("body") if isinstance(payload, dict) else payload
            for tag in _flatten_tag_candidates(body_obj):
                add_candidate(tag, 25)

            def walk_translation(obj):
                if isinstance(obj, dict):
                    t = obj.get("tag") or obj.get("name") or obj.get("tagName")
                    tr = obj.get("translation")
                    if isinstance(t, str) and isinstance(tr, dict):
                        zh = tr.get("zh") or tr.get("zh-cn") or ""
                        if zh:
                            add_candidate(t, 80, zh)
                    for v in obj.values():
                        walk_translation(v)
                elif isinstance(obj, list):
                    for v in obj:
                        walk_translation(v)
            walk_translation(body_obj)
        except Exception as e:
            if logger:
                logger.debug(f"[恶魔bot] Pixiv 标签建议「{word}」失败：{type(e).__name__}: {e}")

    try:
        q = {"word": word, "order": "date_d", "mode": "safe", "p": 1,
             "s_mode": "s_tag", "type": "all", "lang": "zh"}
        url = f"{host}/ajax/search/artworks/{urllib.parse.quote(word)}?{urllib.parse.urlencode(q)}"
        body = await _request(url, timeout=timeout, headers=headers)
        payload = json.loads(body)
        b = payload.get("body") or {}
        trans = b.get("tagTranslation") or {}
        if isinstance(trans, dict):
            for tag, tr in trans.items():
                if isinstance(tr, dict):
                    zh = tr.get("zh") or tr.get("zh-cn") or ""
                else:
                    zh = str(tr or "")
                add_candidate(tag, 100, zh)
        for tag in _flatten_tag_candidates(b.get("relatedTags")):
            add_candidate(tag, 45)
        data = ((b.get("illustManga") or {}).get("data")) or []
        for item in data[:50]:
            for tag in item.get("tags") or []:
                add_candidate(tag, 20)
    except Exception as e:
        if logger:
            logger.debug(f"[恶魔bot] Pixiv 反向标签搜索「{word}」失败：{type(e).__name__}: {e}")

    ranked = []
    for tag in scores:
        score = _score_resolved_tag(
            tag, word, translations.get(tag, ""), freq.get(tag, 0)
        ) + scores.get(tag, 0)
        ranked.append((score, tag))
    ranked.sort(key=lambda x: (-x[0], x[1]))
    result = [tag for score, tag in ranked if score >= 18][:max(1, min(int(max_tags or 4), 6))]
    if not result:
        result = [word]
    _cache_put(_TAG_CACHE, key, result)
    return result


async def pixiv_search(
    word: str,
    pages: int = 2,
    proxy: str = "i.pixiv.re",
    timeout: float = 12.0,
    cookie: str = "",
    host: str = PIXIV_HOST_DEFAULT,
    quality: str = "regular",
    month_only: bool = True,
    order: str = "date_d",
    cache_seconds: float = 1800,
    logger=None,
) -> list:
    """Pixiv 官方标签搜索（safe 分区）。需要 Cookie，没有就基本拿不到结果。"""
    word = (word or "").strip()
    if not word:
        return []
    host = (host or PIXIV_HOST_DEFAULT).rstrip("/")
    scd, ecd = _search_month_range(month_only)
    key = (host, word, order, scd, ecd, int(pages))
    cached = _cache_get(_SEARCH_CACHE, key, cache_seconds)
    if cached is None:
        collected = []
        for p in range(1, max(1, min(int(pages or 1), 5)) + 1):
            q = {
                "word": word,
                "order": order,
                "mode": "safe",
                "p": p,
                "s_mode": "s_tag_full",   # 完全匹配标签，比部分匹配干净得多
                "type": "illust",
                "lang": "zh",
            }
            if scd:
                q["scd"], q["ecd"] = scd, ecd
            url = (
                f"{host}/ajax/search/artworks/{urllib.parse.quote(word)}"
                f"?{urllib.parse.urlencode(q)}"
            )
            try:
                body = await _request(url, timeout=timeout, headers=_pixiv_headers(cookie, host))
                data = json.loads(body)
            except Exception as e:  # noqa: BLE001
                if logger:
                    logger.debug(f"[恶魔bot] Pixiv 搜索「{word}」失败：{type(e).__name__}: {e}")
                break
            if data.get("error"):
                if logger:
                    logger.debug(f"[恶魔bot] Pixiv 搜索被拒（多半是没登录）：{data.get('message')}")
                break
            chunk = (((data.get("body") or {}).get("illustManga") or {}).get("data")) or []
            if not chunk:
                break
            collected.extend(chunk)
        cached = collected
        _cache_put(_SEARCH_CACHE, key, collected)

    out = []
    for item in cached:
        if item.get("isAdContainer"):
            continue
        pid = str(item.get("id") or "")
        thumb = item.get("url") or ""
        try:
            page_count = int(item.get("pageCount") or 1)
        except (TypeError, ValueError):
            page_count = 1
        pages_urls = _build_page_urls(thumb, pid, page_count, proxy, quality)
        if not pages_urls:
            continue
        tags = [str(t) for t in (item.get("tags") or [])]
        out.append(
            {
                "url": pages_urls[0],
                "page_urls": pages_urls,
                "_master_urls": _master_urls(thumb, pid, page_count, proxy),
                "title": item.get("title", ""),
                "author": item.get("userName", ""),
                "pid": pid,
                "tags": tags,
                "width": int(item.get("width") or 0),
                "height": int(item.get("height") or 0),
                "page_count": page_count,
                "bookmarks": 0,
                "views": 0,
                "rank": 0,
                "size_used": "original" if normalize_quality(quality) == "original" else "master1200",
                "source": "pixiv搜",
                "r18": bool(item.get("xRestrict")),
            }
        )
    return out


def _tag_score(item_tags: list, wanted: list) -> int:
    """算这张图命中了几个我们要的标签。0 = 完全没沾边。"""
    tl = [str(t).lower() for t in (item_tags or []) if t]
    score = 0
    for w in wanted or []:
        w = str(w).lower().strip()
        if not w:
            continue
        if any(w == t or w in t or t in w for t in tl):
            score += 1
    return score


async def pixiv_direct(
    tags: list,
    limit: int = 1,
    proxy: str = "i.pixiv.re",
    timeout: float = 12.0,
    quality: str = "regular",
    cookie: str = "",
    host: str = PIXIV_HOST_DEFAULT,
    rank_modes: list = None,
    rank_pages: int = 2,
    rank_tag_pages: int = 22,
    rank_cache_seconds: float = 21600,
    min_bookmarks: int = 0,
    users_gate: list = None,
    month_only: bool = True,
    search_pages: int = 2,
    logger=None,
) -> list:
    """
    Pixiv 直连取图，返回一批候选（调用方自己随机抽）。

    没给标签  -> 直接返回榜单前 rank_pages*50 名（默认前 100）。
    给了标签  -> 先在榜单里按标签筛（这批是全站热度前列，画师水平最有保证），
                 筛出来太少再用官方搜索 + users入り 收藏量门槛补齐。
    """
    flat = []
    for t in tags or []:
        flat.extend(t if isinstance(t, list) else [t])
    flat = [str(x).strip() for x in flat if str(x).strip()]

    rank_modes = list(rank_modes or DEFAULT_RANK_MODES)
    pool: list = []

    # ---------- 1. 排行榜：多个标签按 OR 分开筛 ----------
    if flat:
        for mode in rank_modes:
            try:
                items = await pixiv_ranking(
                    mode=mode, pages=rank_tag_pages, proxy=proxy, timeout=timeout,
                    cookie=cookie, host=host, quality=quality,
                    cache_seconds=rank_cache_seconds, logger=logger,
                )
            except Exception as e:
                if logger:
                    logger.debug(f"[恶魔bot] {mode} 榜异常：{type(e).__name__}: {e}")
                items = []
            if not items:
                continue
            for x in items:
                score = _tag_score(x.get("tags"), flat)
                if score > 0:
                    x = dict(x)
                    x["_tag_match_score"] = score
                    pool.append(x)
            if len(pool) >= 80:
                break

        gates = list(users_gate or DEFAULT_USERS_GATE)
        per_tag_pool = []
        for tag in flat[:6]:
            tried = [f"{tag} {int(g)}users入り" for g in gates] + [tag]
            tag_found = []
            for w in tried:
                try:
                    found = await pixiv_search(
                        w, pages=search_pages, proxy=proxy, timeout=timeout,
                        cookie=cookie, host=host, quality=quality,
                        month_only=month_only, logger=logger,
                    )
                except Exception as e:
                    if logger:
                        logger.debug(f"[恶魔bot] 搜索「{w}」异常：{type(e).__name__}: {e}")
                    found = []
                if found:
                    for x in found:
                        x = dict(x)
                        x["_tag_match_score"] = max(
                            int(x.get("_tag_match_score") or 0),
                            _tag_score(x.get("tags"), [tag]),
                        )
                        tag_found.append(x)
                    if len(tag_found) >= 8:
                        break
            per_tag_pool.extend(tag_found)

        pool.extend(per_tag_pool)
        if not per_tag_pool and month_only:
            for tag in flat[:6]:
                try:
                    pool.extend(await pixiv_search(
                        tag, pages=search_pages, proxy=proxy, timeout=timeout,
                        cookie=cookie, host=host, quality=quality,
                        month_only=False, logger=logger,
                    ))
                except Exception:
                    pass
    else:
        for mode in rank_modes:
            try:
                items = await pixiv_ranking(
                    mode=mode, pages=rank_pages, proxy=proxy, timeout=timeout,
                    cookie=cookie, host=host, quality=quality,
                    cache_seconds=rank_cache_seconds, logger=logger,
                )
            except Exception as e:
                if logger:
                    logger.debug(f"[恶魔bot] {mode} 榜异常：{type(e).__name__}: {e}")
                items = []
            if items:
                pool = items[:max(1, rank_pages) * 50]
                break

    if min_bookmarks and pool:
        strong = [x for x in pool if (x.get("bookmarks") or 0) >= min_bookmarks or not x.get("bookmarks")]
        pool = strong or pool
    pool = sorted(
        enumerate(pool),
        key=lambda iv: (-int(iv[1].get("_tag_match_score") or 0), iv[0]),
    )
    return _dedupe([x for _, x in pool])


# ---------------------------------------------------------------- 老图源

async def lolicon(
    tags: list,
    limit: int = 1,
    proxy: str = "i.pixiv.re",
    timeout: float = 12.0,
    mode: str = "and",
    exclude_ai: bool = True,
    r18: int = 0,
    quality: str = "regular",
) -> list:
    quality = normalize_quality(quality)
    # 只向接口索要需要的规格：少要一档就少一份 CDN 回源，返回体也更小
    want_sizes = [s for s in QUALITY_ORDER[quality][:2] if s in _LOLICON_SIZES] or ["regular"]
    body = {
        "r18": 1 if r18 else 0,
        "num": max(1, min(limit, 20)),
        "size": want_sizes,
        "proxy": proxy,
        "excludeAI": bool(exclude_ai),
    }
    if tags:
        if isinstance(tags[0], list):
            body["tag"] = tags                    # 已是二维数组，直接透传
        elif mode == "and":
            body["tag"] = [[t] for t in tags]     # 每词一组 = 词之间「且」
        else:
            body["tag"] = [list(tags)]            # 同一组 = 词之间「或」
    raw = await _request(
        "https://api.lolicon.app/setu/v2",
        method="POST",
        timeout=timeout,
        headers={"Content-Type": "application/json"},
        json_body=body,
    )
    data = json.loads(raw)
    out = []
    for item in data.get("data") or []:
        url, size_used = _pick_url(item.get("urls") or {}, quality)
        if not url:
            continue
        # lolicon 也给 pageCount，多 p 一样能推导
        try:
            page_count = int(item.get("pageCount") or 1)
        except (TypeError, ValueError):
            page_count = 1
        pages = _build_page_urls(url, item.get("pid"), page_count, proxy, quality) if page_count > 1 else [url]
        out.append(
            {
                "url": url,
                "page_urls": pages or [url],
                "title": item.get("title", ""),
                "author": item.get("author", ""),
                "pid": item.get("pid", ""),
                "tags": item.get("tags", []),
                "width": item.get("width") or 0,
                "height": item.get("height") or 0,
                "page_count": page_count,
                "bookmarks": 0,
                "size_used": size_used,
                "source": "lolicon",
                "r18": bool(item.get("r18")),
            }
        )
    return out


async def anosu(
    tags: list,
    limit: int = 1,
    proxy: str = "",
    timeout: float = 12.0,
    mode: str = "or",
    exclude_ai: bool = True,
    r18: int = 0,
    quality: str = "regular",
) -> list:
    """anosu 只支持关键词「或」匹配（| 分隔），没有 AND。"""
    quality = normalize_quality(quality)
    flat = []
    for t in tags or []:
        flat.extend(t if isinstance(t, list) else [t])
    keyword = urllib.parse.quote("|".join(flat)) if flat else ""
    url = (
        f"https://image.anosu.top/pixiv/json?num={max(1, min(limit, 20))}"
        f"&r18={1 if r18 else 0}&size={_ANOSU_SIZE.get(quality, 'regular')}"
    )
    if keyword:
        url += f"&keyword={keyword}"
    if proxy:
        url += f"&proxy={urllib.parse.quote(proxy)}"
    raw = await _request(url, timeout=timeout)
    data = json.loads(raw)
    if isinstance(data, dict):
        data = [data]
    out = []
    for item in data or []:
        link = item.get("url")
        size_used = _ANOSU_SIZE.get(quality, "regular")
        if not link and isinstance(item.get("urls"), dict):
            link, size_used = _pick_url(item["urls"], quality)
        if link:
            out.append(
                {
                    "url": link,
                    "page_urls": [link],
                    "title": item.get("title", ""),
                    "author": item.get("user", ""),
                    "pid": item.get("pid", ""),
                    "tags": item.get("tags", []),
                    "width": item.get("width") or 0,
                    "height": item.get("height") or 0,
                    "page_count": 1,
                    "bookmarks": 0,
                    "size_used": size_used,
                    "source": "anosu",
                    "r18": bool(r18),
                }
            )
    return out


# ---------- 真人图源 ----------
# 多数公开接口没有精确年龄分区，用不同端点区分「普通/擦边」与「更高尺度」。
# 接口多为 302 跳转，直接把端点 URL 交给消息组件，由平台跟随重定向下载。

_REAL_SFW_ENDPOINTS = [
    "https://api.yviii.com/img/suiji/",
    "https://api.yviii.com/img/baisi/",
    "https://api.yviii.com/img/heisi/",
]

_REAL_MATURE_ENDPOINTS = list(_REAL_SFW_ENDPOINTS)


async def real_person(
    tags: list = None,
    limit: int = 1,
    timeout: float = 12.0,
    r18: int = 0,
    **kwargs,
) -> list:
    """
    真人随机图。不解析 tags（多数接口不支持关键词），按 r18 选端点池随机抽。
    返回格式与 Pixiv 后端一致，方便统一处理。
    """
    endpoints = _REAL_MATURE_ENDPOINTS if kwargs.get("mature") else _REAL_SFW_ENDPOINTS
    pool = list(endpoints)
    random.shuffle(pool)
    out = []
    for ep in pool:
        if len(out) >= max(1, min(limit, 5)):
            break
        try:
            await _request(ep, timeout=min(timeout, 8.0))
            out.append(
                {
                    "url": ep,
                    "page_urls": [ep],
                    "title": "真人图",
                    "author": "",
                    "pid": "",
                    "tags": list(tags or []),
                    "width": 0,
                    "height": 0,
                    "page_count": 1,
                    "bookmarks": 0,
                    "size_used": "direct",
                    "source": "real",
                    "r18": bool(r18),
                }
            )
        except Exception:
            continue
    return out


BACKENDS = {
    "lolicon": lolicon,
    "anosu": anosu,
    "real": real_person,
}


def _quality_filter(results: list, min_width: int, min_height: int) -> list:
    """剔掉明显偏小的图。没给分辨率的一律放行，避免误杀。"""
    if not (min_width or min_height):
        return results
    kept = []
    for r in results:
        w, h = r.get("width") or 0, r.get("height") or 0
        if (w == 0 and h == 0) or (w >= min_width and h >= min_height):
            kept.append(r)
    return kept or results


def _dedupe(results: list) -> list:
    seen, out = set(), []
    for r in results:
        key = r.get("pid") or r.get("url")
        if key and key not in seen:
            seen.add(key)
            out.append(r)
    return out


async def _try_backends(
    tags: list, backends: list, pool: int, proxy: str, timeout: float,
    mode: str, exclude_ai: bool, min_width: int, min_height: int,
    r18: int = 0, mature: bool = False, quality: str = "regular", logger=None,
) -> list:
    for name in backends:
        fn = BACKENDS.get(name)
        if not fn:
            continue
        if name == "anosu" and mode == "and" and len(tags) > 1:
            continue
        if name == "real":
            try:
                results = await fn(
                    tags=tags, limit=pool, timeout=timeout, r18=r18,
                    mature=mature,
                )
                results = _dedupe(results)
                if results:
                    return results
            except Exception as e:  # noqa: BLE001
                if logger:
                    logger.debug(f"[恶魔bot] 图源 {name} 失败：{type(e).__name__}: {e}")
            continue
        try:
            results = await fn(
                tags, limit=pool, proxy=proxy, timeout=timeout,
                mode=mode, exclude_ai=exclude_ai, r18=r18, quality=quality,
            )
            results = _quality_filter(_dedupe(results), min_width, min_height)
            if results:
                return results
        except Exception as e:  # noqa: BLE001
            if logger:
                logger.debug(f"[恶魔bot] 图源 {name} 失败：{type(e).__name__}: {e}")
    return []


async def fetch(
    tags: list,
    backends: list = None,
    limit: int = 1,
    proxy: str = "i.pixiv.re",
    timeout: float = 12.0,
    logger=None,
    pool: int = 8,
    min_width: int = 900,
    min_height: int = 900,
    exclude_ai: bool = True,
    strict: bool = False,
    r18: int = 0,
    real: bool = False,
    mature: bool = False,
    quality: str = "regular",
    # ---- 以下为 Pixiv 直连相关参数（全部可选，不传就用默认值）----
    pixiv_cookie: str = "",
    pixiv_host: str = PIXIV_HOST_DEFAULT,
    rank_modes: list = None,
    rank_pages: int = 2,
    rank_tag_pages: int = 22,
    rank_cache_seconds: float = 21600,
    min_bookmarks: int = 0,
    users_gate: list = None,
    month_only: bool = True,
    search_pages: int = 2,
) -> list:
    """
    取图主入口。

    backends 里含 "pixiv" 时优先走 Pixiv 排行榜/官方搜索（画质最高的一路），
    拿不到再依次退到 lolicon / anosu。real=True 时只走真人图源。
    r18=1 时请求成人分区（跳过全年龄排行榜，走 lolicon/anosu 或真人 R18 端点）。
    """
    quality = normalize_quality(quality)
    if real:
        backends = ["real"]
    else:
        backends = list(backends or ["pixiv", "lolicon", "anosu"])
    pool = max(pool, limit)

    candidates = []

    r18 = 1 if r18 else 0
    if not real and r18:
        # 官方排行榜默认 safe，R18 改走支持成人参数的第三方
        backends = [b for b in list(backends or []) if b != "pixiv"] or ["lolicon", "anosu"]
    if not real and not r18 and "pixiv" in backends:
        try:
            candidates = await pixiv_direct(
                tags or [], limit=limit, proxy=proxy, timeout=timeout, quality=quality,
                cookie=pixiv_cookie, host=pixiv_host, rank_modes=rank_modes,
                rank_pages=rank_pages, rank_tag_pages=rank_tag_pages,
                rank_cache_seconds=rank_cache_seconds, min_bookmarks=min_bookmarks,
                users_gate=users_gate, month_only=month_only,
                search_pages=search_pages, logger=logger,
            )
            candidates = _quality_filter(candidates, min_width, min_height)
        except Exception as e:  # noqa: BLE001
            if logger:
                logger.debug(f"[恶魔bot] Pixiv 直连失败：{type(e).__name__}: {e}")
            candidates = []

    rest = [b for b in backends if b != "pixiv"]
    if not candidates and (rest or real):
        args = dict(
            backends=(rest or ["real"]), pool=pool, proxy=proxy, timeout=timeout,
            exclude_ai=exclude_ai, min_width=min_width, min_height=min_height,
            r18=r18, mature=bool(mature or r18), quality=quality, logger=logger,
        )
        if real:
            candidates = await _try_backends(tags or [], mode="or", **args)
        else:
            if tags and len(tags) > 1:
                candidates = await _try_backends(tags, mode="and", **args)
            if not candidates and tags:
                candidates = await _try_backends(tags, mode="or", **args)
            if not candidates and tags and strict:
                return []
            if not candidates:
                candidates = await _try_backends([], mode="or", **args)

    if not candidates:
        return []

    random.shuffle(candidates)
    final = candidates[:limit]
    if quality == "original":
        for pic in final:
            try:
                await _resolve_original(pic, timeout=min(timeout, 8.0))
            except Exception:  # noqa: BLE001
                pass
    return final
