#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""搜狗微信搜索：零凭据的「找新号」入口。

为什么需要它
------------
合集和文章页只能覆盖**我们已经知道的号**。想发现新号（比如某学院的号从来没人
给过链接），公开渠道只剩搜索。搜狗微信搜索不需要登录，能给到
**标题 + 公众号名 + 日期**，这就够做覆盖率盘点了；再还原出真实文章链接就能抓正文。

三条实测出来的规矩（2026-10 复核）
----------------------------------
1. **必须先访问首页拿 SUID**。不预热直接请求 `/link`，正常跳转页会变成 antispider
   页；带预热则一次成功。搜狗盯的是"有没有正常会话起点"。
2. **搜狗给的是签名链，分钟级失效**：`/s?src=11&timestamp=..&signature=..`。
   所以拿到就要立刻抓，抓完把页面里的 `var msg_link`（带 chksm 的永久链）存下来；
   千万别把签名链存进库。
3. **签名链虽然不带 chksm，却是能抓的**——它和「文末往期回顾那种缺 chksm 的长链」
   是两种东西，后者才是 17 KB 验证墙。这一点最容易搞混。

依赖：只用标准库（`urllib` + `http.cookiejar`）。账号池那套 `requests` 与本包无关。
"""

from __future__ import annotations

import html as htmllib
import http.cookiejar
import random
import re
import time
import urllib.parse
import urllib.request

from .article import UA_MOBILE

SOGOU_HOME = "https://weixin.sogou.com/"
SOGOU_SEARCH = "https://weixin.sogou.com/weixin"

_RE_ITEM = re.compile(r'<li id="sogou_vr_[^"]*_box_\d+"', re.S)
_RE_HREF = re.compile(r'href="(/link\?url=[^"]+)"')
_RE_TITLE = re.compile(r'uigs="article_title_\d+"[^>]*>(.*?)</a>', re.S)
_RE_ACCOUNT = re.compile(r'class="all-time-y\d?">(.*?)</span>', re.S)
_RE_SUMMARY = re.compile(r'class="txt-info"[^>]*>(.*?)</p>', re.S)
_RE_TS = re.compile(r"timeConvert\('(\d+)'\)")
_RE_FRAG = re.compile(r"url \+= '([^']*)'")

GUARD_MARKERS = ("antispider", "请输入验证码", "访问过于频繁")


class SogouBlocked(RuntimeError):
    """搜狗反爬触发。不是封号，是本机 IP 短时间请求太多：停 10~30 分钟再来。"""


def _text(fragment: str) -> str:
    text = re.sub(r"<!--.*?-->", "", fragment or "", flags=re.S)
    text = re.sub(r"<[^>]+>", "", text)
    return (htmllib.unescape(text).replace("\u200b", "")
            .replace("\xa0", " ").strip())


class Sogou:
    """一个带 cookie 的搜狗会话。复用同一个实例比每次新建更不容易被拦。"""

    def __init__(self, timeout: float = 25, pace: float = 3.0):
        self.timeout = timeout
        self.pace = pace
        self._jar = http.cookiejar.CookieJar()
        self._opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self._jar))
        self._warmed = False
        self._last = 0.0

    # -- 底层 ------------------------------------------------------------- #
    def _get(self, url: str, referer: str = SOGOU_HOME) -> str:
        # 搜狗的 href 里会带原样的空格（query 直接拼进去的），urllib 会拒绝这种 URL
        url = url.replace(" ", "%20")
        wait = self.pace - (time.time() - self._last)
        if wait > 0:
            time.sleep(wait + random.uniform(0, 0.4))       # 固定间隔本身就是特征
        req = urllib.request.Request(url, headers={
            "User-Agent": UA_MOBILE.replace("iPhone", "Windows NT 10.0; Win64"),
            "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Referer": referer,
        })
        try:
            with self._opener.open(req, timeout=self.timeout) as fh:
                page = fh.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            raise SogouBlocked(f"搜狗返回 HTTP {exc.code}") from exc
        finally:
            self._last = time.time()
        if any(m in page for m in GUARD_MARKERS):
            raise SogouBlocked("搜狗触发反爬验证（antispider）：停 10~30 分钟再跑，"
                               "或把查询数/翻页数调小")
        return page

    def warm(self) -> None:
        if self._warmed:
            return
        try:
            self._get(SOGOU_HOME, referer="")
        except SogouBlocked:
            raise
        except Exception:                    # 首页失败不致命，搜索时还会再试
            pass
        self._warmed = True

    # -- 搜索 ------------------------------------------------------------- #
    def search(self, query: str, pages: int = 1) -> list[dict]:
        """搜文章（type=2）。返回 [{title, account, published, sogou_link}]。"""
        self.warm()
        out, seen = [], set()
        for page_no in range(1, max(1, pages) + 1):
            url = f"{SOGOU_SEARCH}?" + urllib.parse.urlencode(
                {"type": 2, "query": query, "ie": "utf8", "page": page_no})
            for item in parse_page(self._get(url)):
                if item["sogou_link"] in seen:
                    continue
                seen.add(item["sogou_link"])
                item["query"] = query
                out.append(item)
        return out

    def resolve(self, sogou_link: str, attempts: int = 3) -> str:
        """签名跳转链 → 真实文章链接。搜狗用 `url += '...'` 分段拼，还夹 `@` 占位符。"""
        self.warm()
        for _ in range(attempts):
            page = self._get(sogou_link)
            frags = _RE_FRAG.findall(page)
            if frags:
                return "".join(frags).replace("@", "")
            time.sleep(1.0)
        return ""


# --------------------------------------------------------------------------- #
# 纯函数（离线可测）
# --------------------------------------------------------------------------- #
def parse_page(page: str) -> list[dict]:
    """搜狗结果页 → 条目列表。"""
    items = []
    cuts = [m.start() for m in _RE_ITEM.finditer(page or "")]
    for i, start in enumerate(cuts):
        block = page[start: cuts[i + 1] if i + 1 < len(cuts) else len(page)]
        href, title = _RE_HREF.search(block), _RE_TITLE.search(block)
        if not (href and title):
            continue
        account = _RE_ACCOUNT.search(block)
        ts = _RE_TS.search(block)
        items.append({
            "title": _text(title.group(1)),
            "account": _text(account.group(1)) if account else "",
            "published": _iso(int(ts.group(1))) if ts else "",
            "sogou_link": ("https://weixin.sogou.com"
                           + href.group(1).replace("&amp;", "&").replace(" ", "%20")),
            "query": "",
        })
    return items


def _iso(ts: int) -> str:
    import datetime
    if not ts:
        return ""
    return datetime.datetime.fromtimestamp(
        ts, datetime.timezone(datetime.timedelta(hours=8))
    ).strftime("%Y-%m-%dT%H:%M:%S+08:00")


def guess_refs(real_url: str) -> dict | None:
    """把还原出来的真实链接转成身份（biz/mid/idx）。"""
    from .article import article_ref
    return article_ref(real_url)
