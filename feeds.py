"""各平台热点采集：给 bot 攒「今天刷到什么」的谈资。

为什么需要这个：bot 聊天时的素材只有群里正在聊的 + 人格设定，
所以永远是同一套说法。真人会刷手机，会拿刷到的东西当话题——
「我昨天刷到一个视频，说…」这种话，没有内容源就永远说不出来。

设计原则：
1. 只读，不评论不点赞（写操作风控重，另议）
2. 每个平台单独限流，失败静默跳过，不影响聊天
3. 抓到的内容压成一句话摘要存池子，聊天时按相关度捞
4. 需要 cookie 的平台，cookie 从配置读，过期就跳过并记日志
"""
from __future__ import annotations

import asyncio
import time
from typing import Any

import aiohttp

UA_MOBILE = (
    "Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Mobile Safari/537.36"
)
UA_DESKTOP = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


async def _get_json(url: str, *, params: dict | None = None, headers: dict | None = None,
                    timeout: float = 15.0) -> Any:
    try:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=timeout)
        ) as sess:
            async with sess.get(url, params=params, headers=headers or {}) as resp:
                if resp.status != 200:
                    return None
                return await resp.json(content_type=None)
    except Exception:  # noqa: BLE001
        return None


# ---------------- B站 ----------------

async def bili_hot(limit: int = 12, timeout: float = 15.0) -> list[dict]:
    """B站综合热门。要 Referer，不然会吃 -352 风控。"""
    data = await _get_json(
        "https://api.bilibili.com/x/web-interface/popular",
        params={"ps": max(1, min(limit, 50)), "pn": 1},
        headers={
            "User-Agent": UA_DESKTOP,
            "Referer": "https://www.bilibili.com/",
            "Accept": "application/json, text/plain, */*",
        },
        timeout=timeout,
    )
    if not data or data.get("code") != 0:
        return []
    out = []
    for v in (data.get("data") or {}).get("list") or []:
        title = str(v.get("title") or "").strip()
        if not title:
            continue
        owner = (v.get("owner") or {}).get("name") or ""
        stat = v.get("stat") or {}
        desc = str(v.get("desc") or "").replace("\n", " ").strip()
        out.append({
            "platform": "b站",
            "title": title[:60],
            "author": str(owner)[:20],
            # desc 是 UP 自己写的简介，最能说明「这条讲了什么」。
            # 播放量保留在字段里但注入时不用——真人聊天不提数据。
            "desc": desc[:90],
            "tname": str(v.get("tname") or "").strip(),
            "hot": _fmt_count(stat.get("view")),
            "url": f"https://www.bilibili.com/video/{v.get('bvid')}" if v.get("bvid") else "",
            "ts": int(time.time()),
        })
    return out


async def bili_profile(cookie: str, timeout: float = 15.0) -> dict:
    """用你自己的 B 站 cookie 读出「平时刷什么」，给 bot 当聊天常识。

    只调两个只读接口：nav（拿 uid 昵称）、history（最近看过什么）。
    不点赞、不评论、不投币，全程不改你账号任何状态。

    为什么值得做：bot 刷热门榜拿到的是「全网都在看的」，
    而你真正爱看的东西只在你的历史里。知道这些之后，
    它挑话题才可能挑到你感兴趣的，而不是每天推同一个游戏区视频。
    """
    cookie = (cookie or "").strip()
    if not cookie:
        return {}
    hdr = {
        "User-Agent": UA_DESKTOP,
        "Referer": "https://www.bilibili.com/",
        "Cookie": cookie,
        "Accept": "application/json, text/plain, */*",
    }

    nav = await _get_json(
        "https://api.bilibili.com/x/web-interface/nav", headers=hdr, timeout=timeout
    )
    if not nav or nav.get("code") != 0:
        return {"error": "cookie 被拒（接口没认）"}
    nd = nav.get("data") or {}
    if not nd.get("isLogin"):
        return {"error": "cookie 里没有登录态，多半缺 SESSDATA"}

    mid = str(nd.get("mid") or "")
    uname = str(nd.get("uname") or "")

    hist = await _get_json(
        "https://api.bilibili.com/x/web-interface/history/cursor",
        params={"ps": 30, "type": "archive"},
        headers=hdr,
        timeout=timeout,
    )
    rows = []
    if hist and hist.get("code") == 0:
        rows = (hist.get("data") or {}).get("list") or []
    if not isinstance(rows, list):
        rows = []

    tags: dict[str, int] = {}
    ups: dict[str, int] = {}
    recent: list[str] = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        title = str(r.get("title") or r.get("show_title") or "").strip()
        author = str(r.get("author_name") or "").strip()
        tag = str(r.get("tag_name") or "").strip()
        if tag:
            tags[tag] = tags.get(tag, 0) + 1
        if author:
            ups[author] = ups.get(author, 0) + 1
        if title and len(recent) < 12 and title not in recent:
            recent.append(title[:44])

    top = lambda d, n: [k for k, _ in sorted(d.items(), key=lambda x: x[1], reverse=True)[:n]]
    return {
        "mid": mid,
        "uname": uname,
        "tags": top(tags, 6),
        "ups": top(ups, 6),
        "recent": recent,
        "count": len(rows),
        "ts": int(time.time()),
    }


async def bili_like(cookie: str, bvid: str, timeout: float = 15.0) -> bool:
    """给某个视频点赞。需要 cookie 且要 csrf（bili_jct）。"""
    cookie = (cookie or "").strip()
    bvid = (bvid or "").strip()
    if not cookie or not bvid:
        return False
    import re as _re
    m = _re.search(r"bili_jct=([0-9a-fA-F]+)", cookie)
    if not m:
        return False
    hdr = {
        "User-Agent": UA_DESKTOP,
        "Referer": f"https://www.bilibili.com/video/{bvid}",
        "Cookie": cookie,
        "Content-Type": "application/x-www-form-urlencoded",
    }
    try:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=timeout)
        ) as sess:
            async with sess.post(
                "https://api.bilibili.com/x/web-interface/archive/like",
                data={"bvid": bvid, "like": "1", "csrf": m.group(1)},
                headers=hdr,
            ) as resp:
                if resp.status != 200:
                    return False
                data = await resp.json(content_type=None)
                return bool(data and data.get("code") == 0)
    except Exception:  # noqa: BLE001
        return False


def _fmt_count(n: Any) -> str:
    try:
        n = int(n or 0)
    except Exception:  # noqa: BLE001
        return ""
    if n >= 100000000:
        return f"{n / 100000000:.1f}亿"
    if n >= 10000:
        return f"{n / 10000:.1f}万"
    return str(n)


# ---------------- 贴吧 ----------------

async def tieba_hot(limit: int = 10, timeout: float = 15.0, cookie: str = "") -> list[dict]:
    """贴吧首页热门话题。

    注意：百度对非浏览器请求风控很硬，实测经常直接回「百度安全验证」页
    或者 302 跳到 error 页。给 cookie 也不一定好使——尤其贴吧登录态需要
    BDUSS（在 .baidu.com 域下），只有 STOKEN 是不完整的。
    抓不到就返回空，不影响别的平台。
    """
    # 贴吧的 json 接口时好时坏，先走移动版页面抓标题
    import re

    hdr = {"User-Agent": UA_MOBILE, "Referer": "https://tieba.baidu.com/"}
    if cookie:
        hdr["Cookie"] = cookie.strip()

    try:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=timeout)
        ) as sess:
            async with sess.get(
                "https://tieba.baidu.com/hot/getHotForumList",
                headers=hdr,
            ) as resp:
                if resp.status != 200:
                    return []
                text = await resp.text(errors="ignore")
    except Exception:  # noqa: BLE001
        return []

    # 优先按 json 解析
    try:
        import json as _json
        data = _json.loads(text)
        rows = (data.get("data") or {}).get("hot_forum_list") or []
        out = []
        for r in rows[:limit]:
            name = str(r.get("forum_name") or "").strip()
            if not name:
                continue
            out.append({
                "platform": "贴吧",
                "title": f"「{name}」吧在热议：{str(r.get('content') or '')[:40]}".strip(),
                "author": "",
                "hot": "",
                "url": f"https://tieba.baidu.com/f?kw={name}",
                "ts": int(time.time()),
            })
        return out
    except Exception:  # noqa: BLE001
        pass

    # 退化：从 html 里抠 a 标签标题
    hits = re.findall(r'<a[^>]+href="/f\?kw=([^"]+)"[^>]*>([^<]{2,30})</a>', text)
    out, seen = [], set()
    for kw, name in hits:
        name = name.strip()
        if not name or name in seen:
            continue
        seen.add(name)
        out.append({
            "platform": "贴吧",
            "title": f"「{name}」吧",
            "author": "",
            "hot": "",
            "url": f"https://tieba.baidu.com/f?kw={kw}",
            "ts": int(time.time()),
        })
        if len(out) >= limit:
            break
    return out


# ---------------- 微博（需要 cookie）----------------

async def weibo_hot(cookie: str = "", limit: int = 12, timeout: float = 15.0) -> list[dict]:
    """微博热搜。没有 cookie 基本拿不到，直接返回空。"""
    if not cookie:
        return []
    data = await _get_json(
        "https://weibo.com/ajax/side/hotSearch",
        headers={
            "User-Agent": UA_DESKTOP,
            "Referer": "https://weibo.com/",
            "Cookie": cookie,
        },
        timeout=timeout,
    )
    if not data:
        return []
    rows = (data.get("data") or {}).get("realtime") or []
    out = []
    for r in rows[:limit]:
        word = str(r.get("word") or "").strip()
        if not word or r.get("is_ad"):
            continue
        out.append({
            "platform": "微博",
            "title": f"热搜：{word}",
            "author": "",
            "hot": _fmt_count(r.get("num")),
            "url": f"https://s.weibo.com/weibo?q={word}",
            "ts": int(time.time()),
        })
    return out


# ---------------- 小黑盒（需要 cookie）----------------

# ---- 小黑盒签名（hkey） ----
#
# 小黑盒的接口全部要 hkey 签名，不签就返回「非法请求」。这不是随便加个
# header 能糊过去的，算法是：把 path / 时间戳 / nonce 用一张 34 字符的
# 自定义表映射，三段交错取前 20 位，md5 之后前缀再映射一次、后缀走一遍
# 位运算求校验和。下面这份实现对着真实请求验证过，能通。
#
# 风险声明：这是逆向出来的私有协议，小黑盒随时可能改。改了之后表现为
# 「非法请求」，那时这段就得重写。抓不到就静默跳过，不影响其他平台。

_XHH_CHARSET = "AB45STUVWZEFGJ6CH01D237IXYPQRKLMN89"


def _xhh_vm(e: int) -> int:
    return (255 & ((e << 1) ^ 27)) if e & 128 else e << 1


def _xhh_qm(e: int) -> int:
    return _xhh_vm(e) ^ e


def _xhh_dm(e: int) -> int:
    return _xhh_qm(_xhh_vm(e))


def _xhh_ym(e: int) -> int:
    return _xhh_dm(_xhh_qm(_xhh_vm(e)))


def _xhh_gm(e: int) -> int:
    return _xhh_ym(e) ^ _xhh_dm(e) ^ _xhh_qm(e)


def _xhh_km(e_arr: list) -> list:
    e = list(e_arr)
    t = [
        _xhh_gm(e[0]) ^ _xhh_ym(e[1]) ^ _xhh_dm(e[2]) ^ _xhh_qm(e[3]),
        _xhh_qm(e[0]) ^ _xhh_gm(e[1]) ^ _xhh_ym(e[2]) ^ _xhh_dm(e[3]),
        _xhh_dm(e[0]) ^ _xhh_qm(e[1]) ^ _xhh_gm(e[2]) ^ _xhh_ym(e[3]),
        _xhh_ym(e[0]) ^ _xhh_dm(e[1]) ^ _xhh_qm(e[2]) ^ _xhh_gm(e[3]),
    ]
    e[0], e[1], e[2], e[3] = t[0], t[1], t[2], t[3]
    return e


def _xhh_av(src: str, table: str, cut: int) -> str:
    sub = table[:cut]
    return "".join(sub[ord(c) % len(sub)] for c in src)


def _xhh_sv(src: str, table: str) -> str:
    return "".join(table[ord(c) % len(table)] for c in src)


def xhh_hkey(path: str, ts: int, nonce: str) -> str:
    """算小黑盒请求的 hkey（7 位）。"""
    import hashlib

    parts = [x for x in path.split("/") if x]
    norm = "/" + "/".join(parts) + "/"
    c1 = _xhh_av(str(ts), _XHH_CHARSET, -2)
    c2 = _xhh_sv(norm, _XHH_CHARSET)
    c3 = _xhh_sv(nonce, _XHH_CHARSET)
    comps = [c1, c2, c3]
    inter = ""
    for k in range(max(len(c) for c in comps)):
        for c in comps:
            if k < len(c):
                inter += c[k]
    md5 = hashlib.md5(inter[:20].encode()).hexdigest()
    head = _xhh_av(md5[:5], _XHH_CHARSET, -4)
    tail = sum(_xhh_km([ord(c) for c in md5[-6:]])) % 100
    return head + f"{tail:02d}"


async def xiaoheihe_hot(cookie: str = "", limit: int = 10, timeout: float = 15.0) -> list[dict]:
    """小黑盒社区推荐流。需要 cookie + hkey 签名。"""
    cookie = (cookie or "").strip()
    if not cookie:
        return []

    import re as _re
    import uuid

    m = _re.search(r"heybox_id=(\d+)", cookie)
    hid = m.group(1) if m else ""
    if not hid:
        return []

    path = "/bbs/app/feeds"
    ts = int(time.time())
    nonce = uuid.uuid4().hex.upper()
    hkey = xhh_hkey(path, ts, nonce)

    data = await _get_json(
        "https://api.xiaoheihe.cn" + path,
        params={
            "os_type": "web",
            "app": "heybox",
            "client_type": "web",
            "version": "999.0.4",
            "web_version": "2.5",
            "x_client_type": "web",
            "x_app": "heybox_website",
            "heybox_id": hid,
            "x_os_type": "Windows",
            "device_info": "Chrome",
            # 固定一个 device_id：每次换反而更像异常设备
            "device_id": "2c2fef8385ccef915e3b3caf94e3aa06",
            "offset": 0,
            "limit": max(1, min(limit, 30)),
            "pull": 0,
            "hkey": hkey,
            "_time": ts,
            "nonce": nonce,
        },
        headers={
            "User-Agent": UA_DESKTOP,
            "Referer": "https://www.xiaoheihe.cn/",
            "Cookie": cookie,
            "Accept": "application/json, text/plain, */*",
        },
        timeout=timeout,
    )
    if not data:
        return []
    if str(data.get("status") or "") != "ok" and not isinstance(data.get("result"), dict):
        return []

    result = data.get("result") or {}
    rows = result.get("links") or result.get("feed_list") or []
    if not isinstance(rows, list):
        return []

    # 小黑盒的内容里混着大量水贴和表情代码（[cube_喜欢] 这种），
    # 直接进话题池会把「谈资」拉成垃圾，所以在这里洗一遍再存。
    import re as _re2

    def _clean(x: str) -> str:
        x = _re2.sub(r"\[cube_[^\]]*\]", "", x or "")
        x = _re2.sub(r"\[[^\]]{1,8}\]", "", x)      # 其他方括号表情
        x = _re2.sub(r"\s+", " ", x)
        return x.strip()

    out = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        title = _clean(str(r.get("title") or ""))
        desc = _clean(str(r.get("description") or ""))
        # 太短的（「我的jiji」这种）没有复述价值，直接丢
        if len(title) < 6 and len(desc) < 18:
            continue
        if len(desc) < 10 and len(title) < 10:
            continue
        linkid = r.get("linkid") or r.get("link_id") or ""
        out.append({
            "platform": "小黑盒",
            "title": (title or desc)[:60],
            # description 是帖子正文开头，比标题更能说明「在聊什么」
            "desc": (desc or "")[:90],
            "author": "",
            "tname": "",
            "hot": _fmt_count(r.get("link_award_num")),
            "url": f"https://www.xiaoheihe.cn/bbs/app/link/web/view?link_id={linkid}" if linkid else "",
            "ts": int(time.time()),
        })
    return out


# ---------------- 汇总 ----------------

async def collect_all(cfg, timeout: float = 15.0) -> list[dict]:
    """按配置并发抓所有启用的平台，失败的静默跳过。"""
    jobs = []

    def on(name: str) -> bool:
        try:
            return bool(cfg("feeds", name, default=True))
        except Exception:  # noqa: BLE001
            return True

    if on("bilibili"):
        jobs.append(bili_hot(limit=int(cfg("feeds", "bilibili_limit", default=12) or 12), timeout=timeout))

    if on("tieba"):
        tb = str(cfg("feeds", "tieba_cookie", default="") or "").strip()
        jobs.append(
            tieba_hot(
                limit=int(cfg("feeds", "limit", default=10) or 10),
                timeout=timeout,
                cookie=tb,
            )
        )

    wb = str(cfg("feeds", "weibo_cookie", default="") or "").strip()
    if on("weibo") and wb:
        jobs.append(weibo_hot(cookie=wb, limit=int(cfg("feeds", "limit", default=10) or 10), timeout=timeout))

    xhh = str(cfg("feeds", "xiaoheihe_cookie", default="") or "").strip()
    if on("xiaoheihe") and xhh:
        jobs.append(xiaoheihe_hot(cookie=xhh, limit=int(cfg("feeds", "limit", default=10) or 10), timeout=timeout))

    if not jobs:
        return []

    results = await asyncio.gather(*jobs, return_exceptions=True)
    out = []
    for r in results:
        if isinstance(r, list):
            out.extend(r)
    return out
