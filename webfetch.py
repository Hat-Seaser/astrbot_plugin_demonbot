"""
webfetch.py —— 抓网页正文（给 /看网页 指令用）

设计约束：
- 只用 aiohttp（AstrBot 自带依赖），不引入 trafilatura/bs4，手机 Termux 上装包代价太大。
- 不做通用爬虫，只做「把一页正文抠出来交给模型」，所以用启发式而不是完整 DOM 解析。
- 挡内网地址：群里任何人都能发指令，不挡的话 /看网页 http://127.0.0.1:6099 就能探本机面板。
"""

from __future__ import annotations

import html as _htmllib
import ipaddress
import re
import socket
import urllib.parse

import aiohttp

MAX_BYTES = 2_000_000
DEFAULT_TIMEOUT = 20
UA = (
    "Mozilla/5.0 (Linux; Android 13; 2206123SC) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36"
)

_COMMENT_RE = re.compile(r"(?s)<!--.*?-->")
_DROP_RE = re.compile(r"(?is)<(script|style|noscript|svg|iframe|head)[^>]*>.*?</\1\s*>")
_TITLE_RE = re.compile(r"(?is)<title[^>]*>(.*?)</title\s*>")
_META_CHARSET_RE = re.compile(rb"""(?i)<meta[^>]+charset\s*=\s*["']?\s*([\w\-]+)""")
_BR_RE = re.compile(r"(?i)<\s*br\s*/?\s*>")
_BLOCK_END_RE = re.compile(
    r"(?i)</\s*(p|div|section|article|li|tr|h[1-6]|blockquote|pre|dd|dt)\s*>"
)
_TAG_RE = re.compile(r"(?s)<[^>]+>")
_MULTI_NL_RE = re.compile(r"\n{3,}")
_SPACE_RUN_RE = re.compile(r"[ \t\u00a0\u3000]{2,}")

# 正文容器：中文小说站、博客、新闻站最常见的几种命名
_CONTAINER_RE = re.compile(
    r"(?is)<(div|article|section|main|td)[^>]*(?:id|class)\s*=\s*[\"'][^\"']*"
    r"(?:content|article|chapter|read|novel|txt|mainbody|entry|post|正文|article-content)"
    r"[^\"']*[\"'][^>]*>"
)

_PRIVATE_NAMES = {"localhost", "localhost.localdomain", "ip6-localhost", "0.0.0.0", "::1"}


class FetchError(RuntimeError):
    """抓取失败，消息可直接回给用户。"""


def _assert_public(url: str) -> None:
    p = urllib.parse.urlparse(url)
    if p.scheme not in ("http", "https"):
        raise FetchError("只支持 http / https 网址")
    host = (p.hostname or "").strip().lower()
    if not host:
        raise FetchError("网址里没解析出域名")
    if host in _PRIVATE_NAMES or host.endswith(".local"):
        raise FetchError("不允许访问本机或内网地址")
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError as e:
        raise FetchError(f"域名解析失败：{e}") from e
    for info in infos:
        ip = str(info[4][0]).split("%")[0]
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            continue
        if (
            addr.is_private
            or addr.is_loopback
            or addr.is_link_local
            or addr.is_reserved
            or addr.is_multicast
        ):
            raise FetchError("不允许访问内网地址")


def _unescape(text: str) -> str:
    return _htmllib.unescape(text).replace("\u00a0", " ").replace("\u3000", " ")


def _collapse(text: str) -> str:
    lines = []
    for raw in text.splitlines():
        line = _SPACE_RUN_RE.sub(" ", raw).strip()
        if line:
            lines.append(line)
    return _MULTI_NL_RE.sub("\n\n", "\n".join(lines)).strip()


def _guess_charset(raw: bytes) -> str | None:
    m = _META_CHARSET_RE.search(raw[:4096])
    if not m:
        return None
    try:
        name = m.group(1).decode("ascii", "ignore").lower()
    except Exception:  # noqa: BLE001
        return None
    # 国内站点大量还在用 gb2312 声明，实际多为 gbk 超集
    if name in ("gb2312", "gbk", "gb-2312"):
        return "gb18030"
    return name or None


def html_to_text(page: str) -> tuple[str, str]:
    """返回 (标题, 正文)。启发式：优先切到正文容器，再整体去标签。"""
    page = _COMMENT_RE.sub(" ", page)

    # 标题必须在删 <head> 之前取，否则永远拿不到 <title>
    title = ""
    m = _TITLE_RE.search(page)
    if m:
        title = _collapse(_unescape(_TAG_RE.sub(" ", m.group(1)))).replace("\n", " ")[:120]

    page = _DROP_RE.sub(" ", page)

    body = page
    cm = _CONTAINER_RE.search(page)
    if cm:
        # 从正文容器开始截，丢掉前面的导航/侧栏；尾部残留让模型自己忽略
        body = page[cm.end():]

    body = _BR_RE.sub("\n", body)
    body = _BLOCK_END_RE.sub("\n", body)
    body = _TAG_RE.sub(" ", body)
    return title, _collapse(_unescape(body))


_A_RE = re.compile(r"""(?is)<a\s[^>]*href\s*=\s*["']([^"']+)["'][^>]*>(.*?)</a>""")
_NEXT_WORDS = (
    "下一章", "下一页", "下一篇", "下一节", "下一頁", "下一张",
    "下章", "继续阅读", "下一章&gt;", "next chapter", "next page",
)


def find_next_link(page_html: str, base_url: str) -> str:
    """从页面里找「下一章/下一页」的链接，找不到返回空串。

    只认文本标签里带关键字的 <a>，不猜 URL 规律——各家小说站的命名五花八门，
    靠猜 99% 会跑偏。javascript: 伪链接一律跳过。
    """
    for m in _A_RE.finditer(page_html):
        href = (m.group(1) or "").strip()
        if not href or href.lower().startswith(("javascript:", "#", "mailto:")):
            continue
        label = _unescape(_TAG_RE.sub("", m.group(2))).strip()
        if not label or len(label) > 20:
            continue
        low = label.lower()
        for word in _NEXT_WORDS:
            if word.lower() in low:
                return urllib.parse.urljoin(base_url, href)
    return ""


def build_body_text(page_html: str, max_chars: int = 4000) -> tuple[str, str, bool]:
    """返回 (标题, 正文, 是否被截断)。"""
    title, text = html_to_text(page_html)
    truncated = len(text) > max_chars
    if truncated:
        text = text[:max_chars]
    return title, text, truncated


async def fetch_page(
    url: str,
    timeout: float = DEFAULT_TIMEOUT,
    max_chars: int = 4000,
) -> dict:
    """抓一页，返回 {url, title, text, truncated, chars}。失败抛 FetchError。"""
    _assert_public(url)
    jar_timeout = aiohttp.ClientTimeout(total=timeout)
    headers = {
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml,text/plain;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.6",
    }
    try:
        async with aiohttp.ClientSession(timeout=jar_timeout, headers=headers) as sess:
            async with sess.get(url, allow_redirects=True, max_redirects=5) as resp:
                if resp.status >= 400:
                    raise FetchError(f"HTTP {resp.status}")
                ctype = (resp.headers.get("Content-Type") or "").lower()
                raw = await resp.content.read(MAX_BYTES)
                charset = resp.charset or _guess_charset(raw) or "utf-8"
    except FetchError:
        raise
    except aiohttp.ClientError as e:
        raise FetchError(f"网络请求失败：{type(e).__name__}") from e
    except Exception as e:  # noqa: BLE001
        raise FetchError(f"{type(e).__name__}: {e}") from e

    text_html = raw.decode(charset, errors="replace")

    if "html" not in ctype and "<html" not in text_html[:600].lower():
        body = _collapse(text_html)
        return {
            "url": url,
            "title": "",
            "text": body[:max_chars],
            "truncated": len(body) > max_chars,
            "chars": len(body),
            "next_url": "",
        }

    title, text, truncated = build_body_text(text_html, max_chars=max_chars)
    return {
        "url": url,
        "title": title,
        "text": text,
        "truncated": truncated,
        "chars": len(text),
        "next_url": find_next_link(text_html, url),
    }
