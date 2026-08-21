"""
轻量联网检索模块（不依赖任何付费 API，可选配 key 提升成功率）

设计原则：
- 只用 aiohttp（AstrBot 自带依赖），不引入新的 pip 包，适合手机 Termux 环境
- 每个后端都是"能用就用、挂了就跳过"，任何异常都不会向上抛，最多返回空列表
- 所有网络请求都有超时，绝不阻塞主消息流程

后端优先级由插件配置 knowledge.backends 决定，默认顺序：
bing_rss -> baidu_baike -> jikipedia -> duckduckgo
如果你在国内手机上发现前几个都超时，去 WebUI 配一个 bocha_api_key（博查）
或 tavily_api_key，把对应后端名放到 backends 列表最前面，成功率最高。
"""

from __future__ import annotations

import json
import re
import urllib.parse

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")
_CDATA_RE = re.compile(r"<!\[CDATA\[(.*?)\]\]>", re.S)

UA = (
    "Mozilla/5.0 (Linux; Android 13; Pixel 6) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36"
)

_ENTITIES = {
    "&nbsp;": " ", "&amp;": "&", "&quot;": '"', "&lt;": "<",
    "&gt;": ">", "&#39;": "'", "&apos;": "'", "&ldquo;": "「", "&rdquo;": "」",
}


def clean_text(text: str) -> str:
    """去 HTML 标签、去实体、压缩空白。"""
    if not text:
        return ""
    text = _CDATA_RE.sub(r"\1", text)
    text = _TAG_RE.sub(" ", text)
    for k, v in _ENTITIES.items():
        text = text.replace(k, v)
    return _WS_RE.sub(" ", text).strip()


async def _request(
    url: str,
    *,
    method: str = "GET",
    timeout: float = 8.0,
    headers: dict | None = None,
    json_body: dict | None = None,
) -> str:
    import aiohttp

    h = {"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"}
    if headers:
        h.update(headers)
    client_timeout = aiohttp.ClientTimeout(total=timeout)
    async with aiohttp.ClientSession(timeout=client_timeout) as sess:
        if method == "POST":
            async with sess.post(url, headers=h, json=json_body) as resp:
                resp.raise_for_status()
                return await resp.text()
        async with sess.get(url, headers=h) as resp:
            resp.raise_for_status()
            return await resp.text()


# ==================== 各个搜索后端 ====================


async def bing_rss(query: str, limit: int = 4, timeout: float = 8.0) -> list[dict]:
    """必应搜索的 RSS 输出，返回的是干净 XML，比爬 HTML 稳得多。"""
    return await _bing_rss_host("https://cn.bing.com", query, limit, timeout)


async def bing_rss_intl(query: str, limit: int = 4, timeout: float = 8.0) -> list[dict]:
    """必应国际站，cn.bing.com 被劫持或超时时的备胎。"""
    return await _bing_rss_host("https://www.bing.com", query, limit, timeout)


async def _bing_rss_host(host: str, query: str, limit: int, timeout: float) -> list[dict]:
    url = f"{host}/search?q=" + urllib.parse.quote(query) + "&format=rss"
    raw = await _request(url, timeout=timeout)
    out = []
    for item in re.findall(r"<item>(.*?)</item>", raw, re.S)[:limit]:
        title = clean_text((re.search(r"<title>(.*?)</title>", item, re.S) or [None, ""])[1])
        desc = clean_text((re.search(r"<description>(.*?)</description>", item, re.S) or [None, ""])[1])
        link = clean_text((re.search(r"<link>(.*?)</link>", item, re.S) or [None, ""])[1])
        if title or desc:
            out.append({"title": title, "snippet": desc, "url": link, "source": "bing"})
    return out


async def baidu_baike_api(query: str, limit: int = 1, timeout: float = 8.0) -> list[dict]:
    """百度百科的开放摘要接口，返回 JSON，比爬词条页面稳。"""
    url = (
        "https://baike.baidu.com/api/openapi/BaikeLemmaCardApi"
        "?scope=103&format=json&appid=379020&bk_length=600&bk_key="
        + urllib.parse.quote(query)
    )
    raw = await _request(url, timeout=timeout)
    data = json.loads(raw)
    abstract = clean_text(data.get("abstract") or "")
    if not abstract:
        return []
    return [
        {
            "title": f"{data.get('key') or query}（百度百科）",
            "snippet": abstract[:400],
            "url": data.get("url", ""),
            "source": "baike_api",
        }
    ]


async def moegirl(query: str, limit: int = 1, timeout: float = 8.0) -> list[dict]:
    """萌娘百科，ACG 圈的梗收录得比百度百科全得多。"""
    url = (
        "https://mzh.moegirl.org.cn/api.php?action=query&format=json&prop=extracts"
        "&exintro=1&explaintext=1&redirects=1&titles=" + urllib.parse.quote(query)
    )
    raw = await _request(url, timeout=timeout, headers={"Referer": "https://mzh.moegirl.org.cn/"})
    data = json.loads(raw)
    pages = ((data.get("query") or {}).get("pages")) or {}
    out = []
    for _, page in pages.items():
        extract = clean_text(page.get("extract") or "")
        if extract:
            out.append(
                {
                    "title": f"{page.get('title', query)}（萌娘百科）",
                    "snippet": extract[:400],
                    "url": "https://mzh.moegirl.org.cn/" + urllib.parse.quote(query),
                    "source": "moegirl",
                }
            )
    return out[:limit]


async def baidu_baike(query: str, limit: int = 1, timeout: float = 8.0) -> list[dict]:
    """百度百科词条页的 meta description，作为开放接口挂掉时的备胎。"""
    url = "https://baike.baidu.com/item/" + urllib.parse.quote(query)
    raw = await _request(url, timeout=timeout)
    m = re.search(r'<meta\s+name="description"\s+content="(.*?)"', raw, re.S | re.I)
    if not m:
        return []
    desc = clean_text(m.group(1))
    if not desc or "百度百科" in desc[:6] and len(desc) < 20:
        return []
    return [{"title": f"{query}（百度百科）", "snippet": desc[:400], "url": url, "source": "baike"}]


async def jikipedia(query: str, limit: int = 3, timeout: float = 8.0) -> list[dict]:
    """小鸡词典（新梗最全的地方）。接口不保证长期可用，失败就跳过。"""
    raw = await _request(
        "https://api.jikipedia.com/go/search_definitions",
        method="POST",
        timeout=timeout,
        headers={"Client": "web", "Client-Version": "2.7.2", "Content-Type": "application/json"},
        json_body={"phrase": query, "page": 1, "size": limit},
    )
    data = json.loads(raw)
    out = []
    for item in (data.get("data") or [])[:limit]:
        term = (item.get("term") or {}).get("title") or query
        content = clean_text(item.get("plaintext") or item.get("content") or "")
        if content:
            out.append(
                {
                    "title": f"{term}（小鸡词典）",
                    "snippet": content[:400],
                    "url": "https://jikipedia.com/search?phrase=" + urllib.parse.quote(query),
                    "source": "jiki",
                }
            )
    return out


async def duckduckgo(query: str, limit: int = 4, timeout: float = 8.0) -> list[dict]:
    """DuckDuckGo 的纯 HTML 版页面，国内可能连不上，作为兜底。"""
    url = "https://html.duckduckgo.com/html/?q=" + urllib.parse.quote(query)
    raw = await _request(url, timeout=timeout)
    out = []
    blocks = re.findall(r'class="result__body".*?</div>\s*</div>', raw, re.S)[:limit]
    for b in blocks:
        title = clean_text((re.search(r'class="result__a".*?>(.*?)</a>', b, re.S) or [None, ""])[1])
        snip = clean_text((re.search(r'class="result__snippet".*?>(.*?)</a>', b, re.S) or [None, ""])[1])
        if title or snip:
            out.append({"title": title, "snippet": snip, "url": "", "source": "ddg"})
    return out


async def bocha(query: str, limit: int = 4, timeout: float = 10.0, api_key: str = "") -> list[dict]:
    """博查搜索 API（国内可直连，需要自己去 bochaai.com 申请 key）。"""
    if not api_key:
        return []
    raw = await _request(
        "https://api.bochaai.com/v1/web-search",
        method="POST",
        timeout=timeout,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json_body={"query": query, "count": limit, "summary": True},
    )
    data = json.loads(raw)
    pages = (((data.get("data") or {}).get("webPages") or {}).get("value")) or []
    return [
        {
            "title": clean_text(p.get("name", "")),
            "snippet": clean_text(p.get("summary") or p.get("snippet") or "")[:400],
            "url": p.get("url", ""),
            "source": "bocha",
        }
        for p in pages[:limit]
    ]


async def tavily(query: str, limit: int = 4, timeout: float = 10.0, api_key: str = "") -> list[dict]:
    """Tavily 搜索 API（海外，需 key，质量高）。"""
    if not api_key:
        return []
    raw = await _request(
        "https://api.tavily.com/search",
        method="POST",
        timeout=timeout,
        headers={"Content-Type": "application/json"},
        json_body={
            "api_key": api_key,
            "query": query,
            "max_results": limit,
            "search_depth": "basic",
        },
    )
    data = json.loads(raw)
    return [
        {
            "title": clean_text(r.get("title", "")),
            "snippet": clean_text(r.get("content", ""))[:400],
            "url": r.get("url", ""),
            "source": "tavily",
        }
        for r in (data.get("results") or [])[:limit]
    ]


BACKENDS = {
    "bing_rss": bing_rss,
    "bing_rss_intl": bing_rss_intl,
    "baidu_baike_api": baidu_baike_api,
    "moegirl": moegirl,
    "baidu_baike": baidu_baike,
    "jikipedia": jikipedia,
    "duckduckgo": duckduckgo,
    "bocha": bocha,
    "tavily": tavily,
}

DEFAULT_BACKENDS = [
    "bing_rss",
    "baidu_baike_api",
    "moegirl",
    "jikipedia",
    "bing_rss_intl",
    "baidu_baike",
    "duckduckgo",
]


async def search(
    query: str,
    backends: list[str] | None = None,
    limit: int = 4,
    timeout: float = 8.0,
    api_keys: dict | None = None,
    logger=None,
) -> list[dict]:
    """按顺序试各个后端，第一个有结果的就返回。全挂则返回空列表。"""
    backends = backends or DEFAULT_BACKENDS
    api_keys = api_keys or {}
    for name in backends:
        fn = BACKENDS.get(name)
        if not fn:
            continue
        try:
            if name in ("bocha", "tavily"):
                results = await fn(query, limit=limit, timeout=timeout, api_key=api_keys.get(name, ""))
            else:
                results = await fn(query, limit=limit, timeout=timeout)
            if results:
                return results
        except Exception as e:  # noqa: BLE001 — 任何后端出错都只是跳过
            if logger:
                logger.debug(f"[恶魔bot] 搜索后端 {name} 失败：{type(e).__name__}: {e}")
    return []


async def diagnose(
    query: str = "梗",
    backends: list[str] | None = None,
    timeout: float = 8.0,
    api_keys: dict | None = None,
) -> list[tuple]:
    """逐个后端体检，返回 [(名字, 是否可用, 说明)]。

    这个函数存在的意义：手机上到底哪个搜索源能通，只有你自己的网络说了算，
    与其猜，不如让 bot 自己跑一遍报给你。
    """
    backends = backends or DEFAULT_BACKENDS
    api_keys = api_keys or {}
    report = []
    for name in backends:
        fn = BACKENDS.get(name)
        if not fn:
            report.append((name, False, "没有这个后端"))
            continue
        try:
            if name in ("bocha", "tavily"):
                key = api_keys.get(name, "")
                if not key:
                    report.append((name, False, "没配 key，跳过"))
                    continue
                results = await fn(query, limit=2, timeout=timeout, api_key=key)
            else:
                results = await fn(query, limit=2, timeout=timeout)
            if results:
                report.append((name, True, f"{len(results)}条：{results[0]['title'][:16]}"))
            else:
                report.append((name, False, "能连上但没结果"))
        except Exception as e:  # noqa: BLE001
            report.append((name, False, f"{type(e).__name__}"))
    return report


async def hot_topics(limit: int = 10, timeout: float = 8.0, logger=None) -> list[str]:
    """抓当下热搜词，用来让 bot 知道"最近大家在聊什么"。"""
    # 微博热搜（新梗和新闻的第一发源地）
    try:
        raw = await _request("https://weibo.com/ajax/side/hotSearch", timeout=timeout)
        data = json.loads(raw)
        items = ((data.get("data") or {}).get("realtime")) or []
        words = [clean_text(i.get("word") or i.get("note") or "") for i in items]
        words = [w for w in words if w][:limit]
        if words:
            return words
    except Exception as e:  # noqa: BLE001
        if logger:
            logger.debug(f"[恶魔bot] 微博热搜抓取失败：{type(e).__name__}: {e}")

    # 兜底：用必应 RSS 搜今日热点新闻标题
    try:
        results = await bing_rss("今日热点 新闻", limit=limit, timeout=timeout)
        return [r["title"] for r in results if r.get("title")][:limit]
    except Exception as e:  # noqa: BLE001
        if logger:
            logger.debug(f"[恶魔bot] 热点兜底抓取失败：{type(e).__name__}: {e}")
    return []
