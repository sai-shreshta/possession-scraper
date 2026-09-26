"""Search engines (free HTML + optional APIs) with rotation/back-off, and a polite page fetcher."""
from __future__ import annotations

import asyncio
import base64
import logging
import math
import os
import random
import re
import time
from collections import defaultdict
from dataclasses import dataclass
from urllib.parse import parse_qs, unquote, urlparse

import httpx
from bs4 import BeautifulSoup

log = logging.getLogger("scraper")

UAS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/139.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36 Edg/138.0.0.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_6) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.5 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:141.0) Gecko/20100101 Firefox/141.0",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/139.0.0.0 Safari/537.36",
]

SKIP_DOMAINS = ("youtube.com", "facebook.com", "instagram.com", "twitter.com", "x.com", "linkedin.com",
                "pinterest.", "google.", "bing.com", "duckduckgo.com", "yahoo.com", "mojeek.com",
                "wikipedia.org", "amazon.", "flipkart.")
SKIP_EXT = (".pdf", ".jpg", ".jpeg", ".png", ".gif", ".doc", ".docx", ".xls", ".xlsx", ".zip", ".mp4")


def headers(referer: str | None = None) -> dict:
    h = {
        "User-Agent": random.choice(UAS),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-IN,en-GB;q=0.9,en;q=0.8",
    }
    if referer:
        h["Referer"] = referer
    return h


@dataclass
class SearchResult:
    title: str
    url: str
    snippet: str


class Blocked(Exception):
    pass


class Disabled(Exception):
    pass


class Backend:
    name = "base"
    min_interval = 2.0
    is_api = False

    def __init__(self):
        self.lock = asyncio.Lock()
        self.last = 0.0
        self.cooldown_until = 0.0
        self.strikes = 0
        self.ok = 0
        self.blocks = 0

    def available(self) -> bool:
        return time.monotonic() >= self.cooldown_until

    def next_free(self) -> float:
        return max(self.cooldown_until, self.last + self.min_interval)

    async def search(self, client: httpx.AsyncClient, q: str) -> list[SearchResult]:
        async with self.lock:
            wait = self.last + self.min_interval * random.uniform(1.0, 1.6) - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            self.last = time.monotonic()
        try:
            res = await self._search(client, q)
        except Blocked:
            self.blocks += 1
            pause = min(1800, 60 * 2 ** min(self.strikes, 5))
            self.strikes += 1
            self.cooldown_until = time.monotonic() + pause
            log.warning("%s blocked/rate-limited, pausing it for %ds", self.name, pause)
            raise
        except Disabled as e:
            self.cooldown_until = math.inf
            log.error("%s disabled: %s", self.name, e)
            raise Blocked() from e
        self.strikes = 0
        self.ok += 1
        return res

    async def _search(self, client, q):
        raise NotImplementedError


def _soup(text: str) -> BeautifulSoup:
    return BeautifulSoup(text, "lxml")


def _txt(el) -> str:
    return el.get_text(" ", strip=True) if el else ""


class DuckDuckGo(Backend):
    name, min_interval = "duckduckgo", 2.5

    async def _search(self, client, q):
        r = await client.post("https://html.duckduckgo.com/html/", data={"q": q, "kl": "in-en"},
                              headers=headers("https://html.duckduckgo.com/"))
        if r.status_code in (202, 403, 418, 429) or "anomaly-modal" in r.text or "bots use DuckDuckGo" in r.text:
            raise Blocked()
        r.raise_for_status()
        out = []
        for div in _soup(r.text).select("div.result"):
            if "result--ad" in (div.get("class") or []):
                continue
            a = div.select_one("a.result__a")
            if not a:
                continue
            href = a.get("href", "")
            if "uddg=" in href:
                href = parse_qs(urlparse(href).query).get("uddg", [href])[0]
            elif href.startswith("//"):
                href = "https:" + href
            out.append(SearchResult(_txt(a), href, _txt(div.select_one(".result__snippet"))))
        return out


def _bing_url(href: str) -> str:
    if "bing.com/ck/a" in href:
        u = parse_qs(urlparse(href).query).get("u", [""])[0]
        if u.startswith("a1"):
            b = u[2:] + "=" * (-len(u[2:]) % 4)
            try:
                return base64.urlsafe_b64decode(b).decode("utf-8", "ignore")
            except ValueError:
                pass
    return href


class Bing(Backend):
    name, min_interval = "bing", 2.0

    async def _search(self, client, q):
        r = await client.get("https://www.bing.com/search",
                             params={"q": q, "setlang": "en", "cc": "IN", "count": "15"}, headers=headers())
        if r.status_code in (403, 429) or "/challenge" in str(r.url) or "captcha" in r.text[:8000].lower():
            raise Blocked()
        r.raise_for_status()
        soup = _soup(r.text)
        if not soup.select_one("#b_results"):
            raise Blocked()
        out = []
        for li in soup.select("li.b_algo"):
            a = li.select_one("h2 a")
            if not a:
                continue
            sn = li.select_one("div.b_caption p") or li.select_one("p")
            out.append(SearchResult(_txt(a), _bing_url(a.get("href", "")), _txt(sn)))
        return out


class Yahoo(Backend):
    name, min_interval = "yahoo", 2.0

    async def _search(self, client, q):
        r = await client.get("https://search.yahoo.com/search",
                             params={"p": q, "ei": "UTF-8", "n": "15"}, headers=headers())
        if r.status_code in (403, 429, 999):
            raise Blocked()
        r.raise_for_status()
        out = []
        for div in _soup(r.text).select("div.algo"):
            a = div.select_one("h3 a") or div.select_one("a")
            if not a:
                continue
            href = a.get("href", "")
            m = re.search(r"/RU=([^/]+)/R[KS]=", href)
            if m:
                href = unquote(m.group(1))
            title = a.get("aria-label") or _txt(div.select_one("h3")) or _txt(a)
            out.append(SearchResult(title, href, _txt(div.select_one(".compText"))))
        return out


class Mojeek(Backend):
    name, min_interval = "mojeek", 3.0

    async def _search(self, client, q):
        r = await client.get("https://www.mojeek.com/search", params={"q": q}, headers=headers())
        if r.status_code in (403, 429):
            raise Blocked()
        r.raise_for_status()
        out = []
        for li in _soup(r.text).select("ul.results-standard > li"):
            a = li.select_one("a.title") or li.select_one("h2 a")
            if a:
                out.append(SearchResult(_txt(a), a.get("href", ""), _txt(li.select_one("p.s"))))
        return out


class Serper(Backend):
    """Google results via serper.dev (SERPER_API_KEY)."""
    name, min_interval, is_api = "serper", 0.15, True

    def __init__(self, key):
        super().__init__()
        self.key = key

    async def _search(self, client, q):
        r = await client.post("https://google.serper.dev/search",
                              json={"q": q, "gl": "in", "hl": "en", "num": 10},
                              headers={"X-API-KEY": self.key, "Content-Type": "application/json"})
        if r.status_code == 429:
            raise Blocked()
        if r.status_code in (400, 401, 402, 403):
            raise Disabled(r.text[:200])
        r.raise_for_status()
        return [SearchResult(o.get("title", ""), o.get("link", ""), o.get("snippet", ""))
                for o in r.json().get("organic", [])]


class Brave(Backend):
    """Brave Search API (BRAVE_API_KEY)."""
    name, min_interval, is_api = "brave", 1.05, True

    def __init__(self, key):
        super().__init__()
        self.key = key

    async def _search(self, client, q):
        r = await client.get("https://api.search.brave.com/res/v1/web/search",
                             params={"q": q, "count": 20, "country": "IN"},
                             headers={"X-Subscription-Token": self.key, "Accept": "application/json"})
        if r.status_code == 429:
            raise Blocked()
        if r.status_code in (401, 402, 403):
            raise Disabled(r.text[:200])
        r.raise_for_status()
        return [SearchResult(o.get("title", ""), o.get("url", ""),
                             re.sub(r"<[^>]+>", "", o.get("description", "")))
                for o in r.json().get("web", {}).get("results", [])]


class GoogleCSE(Backend):
    """Google Programmable Search (GOOGLE_CSE_KEY + GOOGLE_CSE_CX)."""
    name, min_interval, is_api = "google_cse", 0.2, True

    def __init__(self, key, cx):
        super().__init__()
        self.key, self.cx = key, cx

    async def _search(self, client, q):
        r = await client.get("https://www.googleapis.com/customsearch/v1",
                             params={"key": self.key, "cx": self.cx, "q": q, "num": 10, "gl": "in"})
        if r.status_code == 429:
            raise Blocked()
        if r.status_code in (400, 401, 403):
            raise Disabled(r.text[:200])
        r.raise_for_status()
        return [SearchResult(o.get("title", ""), o.get("link", ""), o.get("snippet", ""))
                for o in r.json().get("items", [])]


FREE_BACKENDS = {"bing": Bing, "duckduckgo": DuckDuckGo, "yahoo": Yahoo, "mojeek": Mojeek}


def build_backends(only: list[str] | None = None) -> list[Backend]:
    bs: list[Backend] = []
    if os.getenv("SERPER_API_KEY"):
        bs.append(Serper(os.environ["SERPER_API_KEY"]))
    if os.getenv("BRAVE_API_KEY"):
        bs.append(Brave(os.environ["BRAVE_API_KEY"]))
    if os.getenv("GOOGLE_CSE_KEY") and os.getenv("GOOGLE_CSE_CX"):
        bs.append(GoogleCSE(os.environ["GOOGLE_CSE_KEY"], os.environ["GOOGLE_CSE_CX"]))
    bs += [cls() for cls in FREE_BACKENDS.values()]
    if only:
        bs = [b for b in bs if b.name in only]
    return bs


class AllBackendsDisabled(Exception):
    pass


class SearchManager:
    def __init__(self, backends: list[Backend], cache):
        self.backends = backends
        self.cache = cache

    def _pick(self, exclude: set) -> Backend | None:
        avail = [b for b in self.backends if b.available() and b.name not in exclude]
        if not avail:
            return None
        apis = [b for b in avail if b.is_api]
        return min(apis or avail, key=lambda b: b.next_free())

    async def search(self, client: httpx.AsyncClient, q: str) -> list[SearchResult]:
        cached = self.cache.get_search(q)
        if cached is not None:
            return cached
        errors, tried_empty = 0, set()
        while True:
            b = self._pick(tried_empty)
            if b is None:
                if tried_empty:
                    return []
                wake = min(x.cooldown_until for x in self.backends)
                if wake == math.inf:
                    raise AllBackendsDisabled("every search backend is disabled")
                await asyncio.sleep(max(1.0, min(wake - time.monotonic(), 60)))
                continue
            try:
                res = await b.search(client, q)
            except Blocked:
                continue
            except (httpx.HTTPError, ValueError) as e:
                errors += 1
                log.debug("%s error on %r: %s", b.name, q, e)
                if errors >= 6:
                    return []
                await asyncio.sleep(2)
                continue
            res = [r for r in res if r.url.startswith("http")]
            if not res and len(tried_empty) < 1:
                tried_empty.add(b.name)  # a silent soft-block looks like zero results; confirm elsewhere
                continue
            if res:
                self.cache.put_search(q, b.name, res)
            return res

    def stats(self) -> str:
        return ", ".join(f"{b.name}:{b.ok}ok/{b.blocks}blk" for b in self.backends)


class BrowserFetcher:
    """Headless Chromium for pages that block plain HTTP clients."""

    def __init__(self, concurrency: int = 2):
        self.sem = asyncio.Semaphore(concurrency)
        self.pw = self.browser = self.ctx = None

    async def start(self):
        from playwright.async_api import async_playwright
        self.pw = await async_playwright().start()
        self.browser = await self.pw.chromium.launch(headless=True)
        self.ctx = await self.browser.new_context(user_agent=UAS[0], locale="en-IN")

    async def fetch(self, url: str) -> str | None:
        async with self.sem:
            page = await self.ctx.new_page()
            try:
                await page.goto(url, timeout=30000, wait_until="domcontentloaded")
                await page.wait_for_timeout(1500)
                return await page.content()
            except Exception as e:
                log.debug("browser fetch failed %s: %s", url, e)
                return None
            finally:
                await page.close()

    async def close(self):
        if self.browser:
            await self.browser.close()
        if self.pw:
            await self.pw.stop()


class PageFetcher:
    def __init__(self, client: httpx.AsyncClient, browser: BrowserFetcher | None = None, per_domain_delay=1.5):
        self.client = client
        self.browser = browser
        self.delay = per_domain_delay
        self.locks = defaultdict(asyncio.Lock)
        self.last = defaultdict(float)
        self.blocked_hosts = defaultdict(int)

    @staticmethod
    def skippable(url: str) -> bool:
        u = url.lower()
        host = urlparse(u).netloc
        return (not u.startswith("http") or urlparse(u).path.endswith(SKIP_EXT)
                or any(d in host for d in SKIP_DOMAINS))

    async def fetch(self, url: str) -> str | None:
        host = urlparse(url).netloc.lower()
        html = None
        # Hosts that keep refusing plain HTTP go straight to the browser (or are skipped).
        if self.blocked_hosts[host] < 5:
            async with self.locks[host]:
                wait = self.last[host] + self.delay - time.monotonic()
                if wait > 0:
                    await asyncio.sleep(wait)
                self.last[host] = time.monotonic()
            try:
                r = await self.client.get(url, headers=headers())
                ctype = r.headers.get("content-type", "")
                if r.status_code == 200 and "html" in ctype:
                    html = r.text
                    self.blocked_hosts[host] = 0
                elif r.status_code in (401, 403, 429, 503):
                    self.blocked_hosts[host] += 1
            except httpx.HTTPError as e:
                log.debug("fetch failed %s: %s", url, e)
        if (html is None or len(html) < 2000) and self.browser:
            html = await self.browser.fetch(url) or html
        return html[:3_000_000] if html else None
