"""
插图获取模块（Pixiv 排行榜/官方搜索 + 公开索引站 + 真人图）

图源分三类：

1. pixiv（本版新增，默认排第一）
   直接打 Pixiv 自己的两个接口，这是「画师水平高」的唯一可靠来源：
   - 排行榜  https://www.pixiv.net/ranking.php?mode=monthly&content=illust&format=json&p=N
     每页 50 条，p=1 就是「本月前 50 张」。返回里带 rating_count、view_count、
     illust_page_count（多 p）、tags。月榜是全站按热度排的官方榜，
     不需要会员、不需要登录即可 JSON 拉取；这是免费账号能拿到的最高质量来源。
   - 重要限制：标签页的「按热门度排序」是 Pixiv 会员功能，bot 和免费号都拿不到。
     因此带关键词时，策略是「在月榜前 50 里按标签过滤」，而不是伪造标签热门榜。
   - R18 榜 mode=weekly_r18 / daily_r18 需要登录 Cookie；没配 Cookie 时回退 lolicon。
   - 标签搜索（users入り）仅作可选补强，默认关闭，避免质量被搜索结果拉低。

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

# 全年龄默认只走月榜（官方热度，无需会员）。
DEFAULT_RANK_MODES = ["monthly"]
# R18 官方榜（需要 Cookie）
DEFAULT_R18_RANK_MODES = ["weekly_r18", "daily_r18"]

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


def _norm_rank_item(item: dict, proxy: str, quality: str, *, r18: bool = False, mode: str = "") -> dict:
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
    is_r18 = bool(r18) or ("r18" in str(mode or "").lower())
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
        "source": f"pixiv榜/{mode or 'monthly'}",
        "r18": is_r18,
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
    """拉排行榜。pages=1 就是前 50 名（每页 50 条），按名次顺序返回。"""
    host = (host or PIXIV_HOST_DEFAULT).rstrip("/")
    pages = max(1, min(int(pages or 1), 3))
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
    is_r18 = "r18" in str(mode or "").lower()
    for it in raw_items:
        n = _norm_rank_item(it, proxy, quality, r18=is_r18, mode=mode)
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
    rank_pages: int = 1,
    rank_tag_pages: int = 1,
    rank_cache_seconds: float = 21600,
    min_bookmarks: int = 0,
    users_gate: list = None,
    month_only: bool = True,
    search_pages: int = 0,
    allow_search_fallback: bool = False,
    r18: int = 0,
    logger=None,
) -> list:
    """
    Pixiv 直连取图 —— 严格走官方排行榜前 50（每页 50，默认只取第 1 页）。

    设计原则（针对「官网月榜极高质量」）：
      - 全年龄：mode=monthly，前 50 名随机。无需登录。
      - R18：mode=weekly_r18（需 Cookie），前 50 名随机；失败再试 daily_r18。
      - 带关键词时：只在上述榜单前 50 里按标签过滤。
        Pixiv 标签页的「按热门排序」是会员功能，免费接口拿不到标签热门榜，
        因此不会假装能做「某 tag 的月榜」。
      - 标签在月榜前 50 无命中：退回整份月榜前 50（保证画质），不再用搜索结果掺水。
      - allow_search_fallback=True 时才启用 users入り 搜索（默认关）。

    多 p：结果带 page_urls，由 main 以合并转发发出。
    """
    flat = []
    for t in tags or []:
        flat.extend(t if isinstance(t, list) else [t])
    flat = [str(x).strip() for x in flat if str(x).strip()]

    if r18:
        rank_modes = list(rank_modes or DEFAULT_R18_RANK_MODES)
    else:
        rank_modes = list(rank_modes or DEFAULT_RANK_MODES)

    # 强制只取 1 页 = 前 50；即使配置写大了也截断，避免质量稀释
    pages = 1
    top_n = 50

    ranked_pool: list = []
    for mode in rank_modes:
        try:
            items = await pixiv_ranking(
                mode=mode, pages=pages, proxy=proxy, timeout=timeout,
                cookie=cookie, host=host, quality=quality,
                cache_seconds=rank_cache_seconds, logger=logger,
            )
        except Exception as e:
            if logger:
                logger.debug(f"[恶魔bot] {mode} 榜异常：{type(e).__name__}: {e}")
            items = []
        if items:
            ranked_pool = items[:top_n]
            if logger:
                logger.info(
                    f"[恶魔bot] 已取 {mode} 榜前 {len(ranked_pool)} 名"
                    f"{'（含多p）' if any((x.get('page_count') or 1) > 1 for x in ranked_pool) else ''}"
                )
            break

    if not ranked_pool:
        return []

    pool: list = []
    if flat:
        for x in ranked_pool:
            score = _tag_score(x.get("tags"), flat)
            if score > 0:
                x = dict(x)
                x["_tag_match_score"] = score
                pool.append(x)
        if pool:
            if logger:
                logger.info(
                    f"[恶魔bot] 标签 {flat} 在榜单前 {top_n} 命中 {len(pool)} 张"
                )
        else:
            # 标签页没有公开热度榜：宁可发月榜高质量，也不用搜索灌水
            if logger:
                logger.info(
                    f"[恶魔bot] 标签 {flat} 未出现在榜单前 {top_n}，回退整榜随机（保画质）"
                )
            pool = [dict(x, _tag_match_score=0) for x in ranked_pool]
    else:
        pool = [dict(x, _tag_match_score=0) for x in ranked_pool]

    # 可选：搜索兜底（默认关，避免低质图混入）
    if allow_search_fallback and cookie and flat and not any(
        int(x.get("_tag_match_score") or 0) > 0 for x in pool
    ):
        gates = list(users_gate or DEFAULT_USERS_GATE)
        for tag in flat[:3]:
            for g in gates[:2]:
                try:
                    found = await pixiv_search(
                        f"{tag} {int(g)}users入り",
                        pages=max(1, int(search_pages or 1)),
                        proxy=proxy, timeout=timeout, cookie=cookie, host=host,
                        quality=quality, month_only=month_only, logger=logger,
                    )
                except Exception:
                    found = []
                for x in found:
                    x = dict(x)
                    x["_tag_match_score"] = _tag_score(x.get("tags"), [tag])
                    pool.append(x)
                if len(pool) >= 10:
                    break
            if len(pool) >= 10:
                break

    if min_bookmarks and pool:
        strong = [
            x for x in pool
            if (x.get("bookmarks") or 0) >= min_bookmarks or not x.get("bookmarks")
        ]
        pool = strong or pool

    pool = _dedupe(pool)
    # 有标签命中的优先，再按榜单名次（rank 越小越好）
    pool.sort(
        key=lambda x: (
            -int(x.get("_tag_match_score") or 0),
            int(x.get("rank") or 9999),
        )
    )
    return pool


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
    rank_pages: int = 1,
    rank_tag_pages: int = 1,
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
    r18=1 时优先官方 weekly_r18 榜前 50（需 Cookie），失败再走 lolicon/anosu。
    """
    quality = normalize_quality(quality)
    if real:
        backends = ["real"]
    else:
        backends = list(backends or ["pixiv", "lolicon", "anosu"])
    pool = max(pool, limit)

    candidates = []

    r18 = 1 if r18 else 0
    # R18 也优先试官方周榜（需 Cookie）；失败再由后面的 lolicon/anosu 兜底
    if not real and "pixiv" in backends:
        try:
            candidates = await pixiv_direct(
                tags or [], limit=limit, proxy=proxy, timeout=timeout, quality=quality,
                cookie=pixiv_cookie, host=pixiv_host,
                rank_modes=(rank_modes or (DEFAULT_R18_RANK_MODES if r18 else DEFAULT_RANK_MODES)),
                rank_pages=1, rank_tag_pages=1,
                rank_cache_seconds=rank_cache_seconds, min_bookmarks=min_bookmarks,
                users_gate=users_gate, month_only=month_only,
                search_pages=search_pages, allow_search_fallback=False,
                r18=r18, logger=logger,
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
