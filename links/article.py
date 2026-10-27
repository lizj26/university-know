#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""文章页：抓取 + 解析（本包唯一联网的入口之一）。

一篇文章页（3.4~3.8 MB）能提供四样东西：

    标题      var msg_title
    发布者    var nickname            ← 报告里「发布者是谁」就靠它
    发布时间  var ct（秒级时间戳）
    永久链接  var msg_link（带 chksm） ← 存这个，不存搜狗的签名链
    关联链接  正文/文末里的同号文章（带 chksm 的才能继续抓）

⚠ 关于「无 chksm 抓不到」这条旧结论（2026-10-14 的对照实验，2026-10-27 复核仍成立）：

    短链          /s/XXXXXX                           ✅
    签名链        /s?src=11&timestamp=..&signature=..   ✅（搜狗给的，分钟级时效）
    长链带 chksm  /s?__biz=..&mid=..&sn=..&chksm=..     ✅
    长链缺 chksm  /s?__biz=..&mid=..&sn=..             ❌ 17 KB 验证码墙

所以文末「往期回顾」里没有 chksm 的链接**抓不动**，只能拿来显示跳转；
而搜狗签名链虽然也不带 chksm，却是另一种可用形态——两者别混为一谈。
"""

from __future__ import annotations

import datetime
import gzip
import json
import re
import ssl
import time
import urllib.error
import urllib.request

CST = datetime.timezone(datetime.timedelta(hours=8))

# 桌面 UA 抓文章页反而更容易触发风控，移动端 UA 拿到的页面更"干净"且体积稳定
UA_MOBILE = ("Mozilla/5.0 (iPhone; CPU iPhone OS 15_0 like Mac OS X) "
             "AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148")

WALL_LIMIT = 60_000        # 低于 60 KB 的「文章页」一律当作验证码墙
WALL_MARKERS = ("TCaptcha", "环境异常", "请输入验证码", "访问过于频繁")


class WallError(RuntimeError):
    """撞上验证码墙。常见于缺 chksm 的长链——重试无用，只能换链接。"""


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #
_CTX = None


def _ctx(verified: bool = True):
    global _CTX
    if _CTX is None:
        _CTX = ssl.create_default_context() if verified else ssl._create_unverified_context()
    return _CTX


def http_get(url: str, *, timeout: float = 25, retries: int = 1,
             backoff: float = 1.5) -> str:
    """抓一个公开页面。撞墙抛 WallError，其它失败抛 RuntimeError。"""
    last: Exception | None = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": UA_MOBILE,
                "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
                "Accept-Language": "zh-CN,zh;q=0.9",
                "Accept-Encoding": "gzip",
            })
            with urllib.request.urlopen(req, timeout=timeout, context=_ctx()) as fh:
                raw = fh.read()
                if fh.headers.get("Content-Encoding") == "gzip":
                    raw = gzip.decompress(raw)
            text = raw.decode("utf-8", "replace")
            if len(raw) < WALL_LIMIT or any(m in text for m in WALL_MARKERS):
                raise WallError(f"验证码墙（{len(raw) // 1024} KB）")
            return text
        except WallError:
            raise
        except ssl.SSLCertVerificationError as exc:
            global _CTX
            _CTX = ssl._create_unverified_context()
            last = exc
        except Exception as exc:  # noqa: BLE001
            last = exc
        if attempt < retries:
            time.sleep(backoff * (attempt + 1))
    raise RuntimeError(f"{type(last).__name__}: {last}")


# --------------------------------------------------------------------------- #
# 解析
# --------------------------------------------------------------------------- #
def unescape_js(text: str) -> str:
    """把 JS 字符串里的转义还原成真字符。

    文章页会把正文嵌在一段 JS 字符串里，于是链接长这样：
        \\x3ca href=\\x22http://mp.weixin.qq.com/s?__biz=..\\x26amp;mid=..\\x22\\x3e
    不还原就只能匹配到 `__biz=..` 就被反斜杠截断——**这是最容易误判「页内没有关联
    链接」的坑**（一开始就踩了：误以为 0 条带 chksm）。另外还有模板占位符
    `${window.biz}` 这种假链接，必须显式排除。
    """
    out = re.sub(r"\\x([0-9a-fA-F]{2})", lambda m: chr(int(m.group(1), 16)), text or "")
    return (out.replace("\\/", "/").replace('\\"', '"')
               .replace("&amp;", "&").replace("&quot;", '"'))


def wechat_var(html: str, name: str) -> str:
    m = (re.search(r'var\s+%s\s*=\s*(?:htmlDecode\()?"([^"]*)"' % re.escape(name), html or "")
         or re.search(r"var\s+%s\s*=\s*(?:htmlDecode\()?'([^']*)'" % re.escape(name), html or ""))
    if not m:
        return ""
    return unescape_js(m.group(1)).strip()


def ts_to_iso(value) -> str:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return ""
    if n <= 0:
        return ""
    try:
        return datetime.datetime.fromtimestamp(n, CST).strftime("%Y-%m-%dT%H:%M:%S+08:00")
    except (OverflowError, OSError, ValueError):
        return ""


_URL_PIECE = re.compile(r"mp\.weixin\.qq\.com/s\?([^\"'<>\s]{20,600})")
_BIZ = re.compile(r"__biz=([A-Za-z0-9=_%+/]{4,})")
_MID = re.compile(r"mid=(\d{6,})")
_IDX = re.compile(r"idx=(\d+)")


def article_ref(url: str) -> dict | None:
    """从一个文章 URL 取出身份 (biz, mid, idx)。取不到就返回 None。

    `biz:mid:idx` 是本包的去重键——比 URL 稳：同一篇可能以 http/https、
    带不带 #rd、带不带 chksm 等多种形态出现。
    """
    if not url or "mp.weixin.qq.com/s?" not in url:
        return None
    biz, mid, idx = _BIZ.search(url), _MID.search(url), _IDX.search(url)
    if not (biz and mid):
        return None
    return {"biz": biz.group(1), "mid": mid.group(1),
            "idx": idx.group(1) if idx else "1"}


def ref_key(ref: dict) -> str:
    return f"{ref['biz']}:{ref['mid']}:{ref['idx']}"


def canonical_url(url: str) -> str:
    """规范化：统一 https、去掉 `#rd` 之类的锚点。"""
    url = (url or "").strip().replace("http://", "https://", 1)
    return url.split("#")[0]


def extract_links(html: str) -> list[dict]:
    """页面里所有真实文章链接（含 chksm 标记），按 biz:mid:idx 去重。"""
    out, seen = [], {}
    for piece in _URL_PIECE.findall(html or ""):
        piece = unescape_js(piece).split("#")[0]
        if "$" in piece:                      # ${window.biz} 这类模板占位
            continue
        ref = article_ref("https://mp.weixin.qq.com/s?" + piece)
        if not ref:
            continue
        key = ref_key(ref)
        if key in seen:
            continue
        url = "https://mp.weixin.qq.com/s?" + piece
        seen[key] = {"key": key, **ref, "url": url,
                     "permanent": is_permanent(url)}
        out.append(seen[key])
    return out


def parse_article(html: str, url: str = "") -> dict:
    """一篇文章页 → 结构化结果。撞墙由 http_get 抛 WallError，这里不处理。"""
    title = wechat_var(html, "msg_title") or wechat_var(html, "title")
    if not title:
        m = re.search(r'property="og:title"\s+content="([^"]+)"', html or "")
        title = unescape_js(m.group(1)) if m else ""

    link = wechat_var(html, "msg_link")
    ref = article_ref(link) or article_ref(url)
    if not ref:
        # 短链（/s/XXXX）的 msg_link 就是短链本身，里面没有 biz/mid；
        # 但页面另外把身份放在独立的变量里，拼起来即可（否则短链会全部入不了库）。
        biz = wechat_var(html, "biz")
        mid = wechat_var(html, "mid")
        idx = wechat_var(html, "idx") or "1"
        if biz and re.fullmatch(r"\d{6,}", mid or ""):
            ref = {"biz": biz, "mid": mid, "idx": idx}

    canonical = canonical_url(link or url)
    links = extract_links(html)
    self_key = ref_key(ref) if ref else ""
    related = [x for x in links if x["key"] != self_key]

    return {
        "key": self_key,
        "biz": (ref or {}).get("biz", ""),
        "mid": (ref or {}).get("mid", ""),
        "idx": (ref or {}).get("idx", ""),
        "url": canonical,
        "permanent": is_permanent(canonical),
        "title": title,
        "account": wechat_var(html, "nickname") or wechat_var(html, "user_name"),
        "published": ts_to_iso(wechat_var(html, "ct")),
        "related": related,
        "related_fetchable": sum(1 for x in related if x["permanent"]),
    }


def is_permanent(url: str) -> bool:
    """这个链接能不能长期用。

    能：短链 `/s/XXXXXX`、带 chksm 的长链。
    不能：缺 chksm 的长链（17 KB 验证墙）、搜狗签名链（分钟级失效）。
    """
    if not url:
        return False
    if "/s/" in url:
        return True
    return "chksm=" in url


def now_iso() -> str:
    return datetime.datetime.now(CST).strftime("%Y-%m-%dT%H:%M:%S+08:00")
