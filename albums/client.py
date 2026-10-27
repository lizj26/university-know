#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""合集接口的 HTTP 层：只做「请求 → 解析成结构化对象」。

本文件是整个 albums 包里**唯一联网**的地方。它不认识校区、不落盘、不做增量，
所以可以被单元测试完全离线地覆盖（把 `fetch=` 换成一个假函数即可）。

接口事实（2026-10 实测）
------------------------
    GET https://mp.weixin.qq.com/mp/appmsgalbum
        ?action=getalbum&__biz=<biz>&album_id=<id>&f=json
        &count=20&begin_msgid=<msgid>&begin_itemidx=<itemidx>

  * 返回 200 + JSON，**不需要任何登录态**（这是本包最大的价值）；
  * 参数错时返回 `{"base_resp":{"ret":10004}}`，说明接口本身是开放的；
  * `getalbum_resp.base_info` 里带 `title`（合集名）/ `nickname`（公众号名）/
    `article_count`（总数）——**公众号名是接口给的，不用猜**，
    这一点比从文章页正则里抠 nickname 可靠得多；
  * `continue_flag=1` 表示还有下一页，用上一页最后一行的 `msgid`/`itemidx` 续取；
  * `article_list[].url` 带 `chksm`，因此正文可抓（对照实验：去掉 chksm 就吃验证码墙）。
"""

from __future__ import annotations

import datetime
import json
import re
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

ALBUM_API = "https://mp.weixin.qq.com/mp/appmsgalbum"

# 移动端 UA：合集接口对 iPhone UA 返回的数据最完整（桌面 UA 也通，但字段更少）
UA_MOBILE = ("Mozilla/5.0 (iPhone; CPU iPhone OS 15_0 like Mac OS X) "
             "AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148")

# 统一用北京时间。绝不能用 time.localtime / datetime.now()：
# 云端（UTC）机器会把 UTC 时间贴上 +08:00 标签，页面显示差 8 小时。
CST = datetime.timezone(datetime.timedelta(hours=8))

RET_OK = 0
RET_BAD_PARAM = 10004      # __biz / album_id 不对，或该合集已删除

PAGE_DEFAULT = 20          # 微信每页实际给多少条由它决定，这个只是上限
PAGE_HARD_CAP = 100        # 翻页硬上限，防接口异常导致死循环


class AlbumError(RuntimeError):
    """网络 / 解析失败。

    单独一个异常类型，是为了让上层能区分「这一个合集拿不到」和「整条链路挂了」：
    前者记进该合集的 health 里继续抓下一个，后者应该让整轮 fail。
    """


# --------------------------------------------------------------------------- #
# 时间
# --------------------------------------------------------------------------- #
def ts_to_iso(value) -> str:
    """微信的时间戳（字符串或整数，秒）→ 项目统一的 `%Y-%m-%dT%H:%M:%S+08:00`。"""
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


def now_iso() -> str:
    return datetime.datetime.now(CST).strftime("%Y-%m-%dT%H:%M:%S+08:00")


# --------------------------------------------------------------------------- #
# 数据结构
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class AlbumRef:
    """一个合集的身份：(公众号 biz, 合集 id)。"""

    biz: str
    album_id: str

    @property
    def key(self) -> str:
        return f"{self.biz}:{self.album_id}"

    def __str__(self) -> str:  # 便于日志
        return self.key


@dataclass
class AlbumMeta:
    """合集自身的元信息（全部来自接口，不是猜的）。"""

    title: str = ""
    account: str = ""
    article_count: int = 0
    username: str = ""        # gh_xxxx，公众号的稳定标识
    description: str = ""
    is_reverse: bool = False


@dataclass
class AlbumArticle:
    """合集里的一篇文章。"""

    key: str = ""             # "<mid>:<idx>"，去重键（比 url 稳，url 会带 #rd / http 差异）
    url: str = ""
    title: str = ""
    published: str = ""
    msgid: str = ""
    itemidx: str = ""

    def as_dict(self) -> dict:
        return {"key": self.key, "url": self.url, "title": self.title,
                "published": self.published, "msgid": self.msgid,
                "itemidx": self.itemidx}


@dataclass
class AlbumPage:
    """一页的解析结果。`error` 非空表示这一页失败（ret 不对 / 网络错）。"""

    ref: AlbumRef | None = None
    meta: AlbumMeta = field(default_factory=AlbumMeta)
    rows: list[AlbumArticle] = field(default_factory=list)
    continue_flag: bool = False
    ret: int = RET_OK
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #
_CTX_VERIFIED = None
_CTX_UNVERIFIED = None


def _ssl_ctx(verified: bool = True):
    global _CTX_VERIFIED, _CTX_UNVERIFIED
    if verified:
        if _CTX_VERIFIED is None:
            _CTX_VERIFIED = ssl.create_default_context()
        return _CTX_VERIFIED
    if _CTX_UNVERIFIED is None:
        _CTX_UNVERIFIED = ssl._create_unverified_context()
    return _CTX_UNVERIFIED


def _open(url: str, timeout: float, verified: bool) -> str:
    req = urllib.request.Request(url, headers={
        "User-Agent": UA_MOBILE,
        "Referer": "https://mp.weixin.qq.com/",
        "Accept": "application/json, text/plain, */*",
    })
    with urllib.request.urlopen(req, timeout=timeout,
                                context=_ssl_ctx(verified)) as fh:
        return fh.read().decode("utf-8", "replace")


def http_get(url: str, *, timeout: float = 15, retries: int = 2,
             backoff: float = 1.2) -> str:
    """GET 一个 URL 并返回文本。失败抛 AlbumError（已含重试）。

    先按「校验证书」走；只有证书本身验不过（某些机器缺根证书）才降级到不校验，
    而不是一上来就 `verify=False` —— 那等于默默放弃中间人防护。
    """
    last: Exception | None = None
    for attempt in range(retries + 1):
        try:
            return _open(url, timeout, verified=True)
        except ssl.SSLCertVerificationError:
            try:
                return _open(url, timeout, verified=False)
            except Exception as exc:  # noqa: BLE001
                last = exc
        except urllib.error.HTTPError as exc:
            last = exc
            # 4xx 是确定性失败，重试没有意义（408/429 除外）
            if 400 <= exc.code < 500 and exc.code not in (408, 429):
                break
        except Exception as exc:  # noqa: BLE001
            last = exc
        if attempt < retries:
            time.sleep(backoff * (attempt + 1))
    raise AlbumError(f"请求失败 {url[:120]}：{last}")


def http_get_json(url: str, **kw) -> dict:
    text = http_get(url, **kw)
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise AlbumError(f"返回的不是 JSON（可能被风控拦了）：{exc}") from exc
    if not isinstance(data, dict):
        raise AlbumError(f"返回结构异常：{type(data).__name__}")
    return data


# --------------------------------------------------------------------------- #
# 解析：接口 JSON
# --------------------------------------------------------------------------- #
def album_url(ref: AlbumRef, *, count: int = PAGE_DEFAULT,
              begin_msgid=0, begin_itemidx=0, newest_first: bool = True) -> str:
    """拼合集接口 URL。

    `is_reverse=1` **实测有效**（2026-10-27 复核）：它会强制返回「新的在前」。
    不加这个参数时，接口按合集自己的设置返回，有的合集是**正序**（旧的在前，
    base_info.is_reverse=False）。正序会带来两个坑：

      ① 新鲜度判定失效——第一页是 20 条最旧的，拿它判"过期"会误杀活跃合集；
      ② **增量失效**——水位比的是一页里的最大 msgid，正序第一页永远最旧，
         于是每次都在第一页就判定"整页旧文"并收工，此后新增的文章永远看不到。

    统一加 `is_reverse=1` 之后，两条链路都只按「新→旧」处理，不需要分支。
    """
    params = {
        "action": "getalbum",
        "__biz": ref.biz,
        "album_id": ref.album_id,
        "f": "json",
        "count": count,
        "begin_msgid": begin_msgid or 0,
        "begin_itemidx": begin_itemidx or 0,
    }
    if newest_first:
        params["is_reverse"] = 1
    return f"{ALBUM_API}?{urllib.parse.urlencode(params)}"


def _mid_idx(url: str) -> tuple[str, str]:
    """从文章 url 里取 (mid, idx)，作为跨页去重键。

    不用 url 本身做键：同一条文章可能以 http / https、带不带 `#rd` 两种形态出现。
    """
    if not url:
        return "", ""
    try:
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    except ValueError:
        return "", ""
    mid = (qs.get("mid") or [""])[0]
    idx = (qs.get("idx") or qs.get("itemidx") or [""])[0]
    return mid, idx


def parse_article(raw: dict) -> AlbumArticle | None:
    url = (raw.get("url") or "").strip()
    title = (raw.get("title") or "").strip()
    if not url or not title:
        return None
    mid, idx = _mid_idx(url)
    msgid = str(raw.get("msgid") or "")
    itemidx = str(raw.get("itemidx") or "")
    return AlbumArticle(
        key=f"{mid}:{idx}" if mid else (url or f"{msgid}:{itemidx}"),
        url=url,
        title=title,
        published=ts_to_iso(raw.get("create_time") or raw.get("update_time")),
        msgid=msgid,
        itemidx=itemidx,
    )


def parse_album_payload(data: dict, ref: AlbumRef | None = None) -> AlbumPage:
    """把 appmsgalbum 的 JSON 变成 AlbumPage。纯函数，不联网，可单测。"""
    base = data.get("base_resp") or {}
    ret = base.get("ret", RET_OK)
    try:
        ret = int(ret)
    except (TypeError, ValueError):
        ret = -1
    if ret != RET_OK:
        hint = "（__biz / album_id 不对，或合集已删除/设为私密）" if ret == RET_BAD_PARAM else ""
        return AlbumPage(ref=ref, ret=ret, error=f"接口 ret={ret}{hint}")

    body = data.get("getalbum_resp") or {}
    info = body.get("base_info") or {}
    if not isinstance(info, dict):
        info = {}
    try:
        count = int(info.get("article_count") or 0)
    except (TypeError, ValueError):
        count = 0

    meta = AlbumMeta(
        title=(info.get("title") or "").strip(),
        account=(info.get("nickname") or "").strip(),
        article_count=count,
        username=(info.get("username") or "").strip(),
        description=(info.get("description") or "").strip(),
        is_reverse=str(info.get("is_reverse") or "0") == "1",
    )

    rows = [a for a in (parse_article(r) for r in (body.get("article_list") or [])
                        if isinstance(r, dict)) if a]
    cont = str(body.get("continue_flag") or "0") in ("1", "true", "True")
    return AlbumPage(ref=ref, meta=meta, rows=rows, continue_flag=cont, ret=ret)


def fetch_album_page(ref: AlbumRef, *, begin_msgid=0, begin_itemidx=0,
                     count: int = PAGE_DEFAULT, timeout: float = 15,
                     retries: int = 2, newest_first: bool = True,
                     fetch=http_get_json) -> AlbumPage:
    """取合集的一页。网络失败也返回 AlbumPage(error=...)，不抛——便于逐合集容错。"""
    url = album_url(ref, count=count, begin_msgid=begin_msgid,
                    begin_itemidx=begin_itemidx, newest_first=newest_first)
    try:
        data = fetch(url, timeout=timeout, retries=retries)
    except AlbumError as exc:
        return AlbumPage(ref=ref, error=str(exc))
    except Exception as exc:  # noqa: BLE001
        return AlbumPage(ref=ref, error=f"{type(exc).__name__}: {exc}")
    return parse_album_payload(data, ref)


# --------------------------------------------------------------------------- #
# 解析：文章页 / 手工输入里的合集引用
# --------------------------------------------------------------------------- #
# 微信把合集的完整链接印在文章页里：
#     mp/appmsgalbum?__biz=<biz>&action=getalbum&album_id=<id>#wechat_redirect
# 匹配限定在「同一个字符串内」（字符类排除引号、空白、尖括号），
# 避免页面别处的 __biz 和别处的 album_id 被误配成一对。
_SEP = r"[^\"'\s<>\\]{0,400}?"
_RE_FWD = re.compile(r"__biz=([A-Za-z0-9=_%-]+)" + _SEP + r"album_id=(\d{6,})")
_RE_REV = re.compile(r"album_id=(\d{6,})" + _SEP + r"__biz=([A-Za-z0-9=_%-]+)")


def norm_biz(biz: str) -> str:
    """URL 里 biz 常被编码成 %3D%3D，统一还原成 `==` 结尾的原形。"""
    return (biz or "").replace("%3D%3D", "==").replace("%3d%3d", "==")


def discover_refs(html: str) -> list[AlbumRef]:
    """从文章页 HTML 里挖出「这篇属于哪些合集」。"""
    html = html or ""
    pairs = [(m.group(1), m.group(2)) for m in _RE_FWD.finditer(html)]
    pairs += [(m.group(2), m.group(1)) for m in _RE_REV.finditer(html)]
    out, seen = [], set()
    for biz, album_id in pairs:
        biz = norm_biz(biz)
        if not biz or not album_id:
            continue
        ref = AlbumRef(biz, album_id)
        if ref.key in seen:
            continue
        seen.add(ref.key)
        out.append(ref)
    return out


def parse_ref(text: str) -> AlbumRef | None:
    """接受三种写法：完整合集 URL / `biz:album_id` / `biz album_id`。"""
    text = (text or "").strip()
    if not text:
        return None
    refs = discover_refs(text)
    if refs:
        return refs[0]
    m = re.match(r"^([A-Za-z0-9=_%-]{4,})[:\s,]+(\d{6,})$", text)
    if m:
        return AlbumRef(norm_biz(m.group(1)), m.group(2))
    return None


def wechat_var(html: str, name: str) -> str:
    """取文章页里的 `var name = "..."`，兼容 `htmlDecode("...")` 包裹。"""
    m = (re.search(r'var\s+%s\s*=\s*(?:htmlDecode\()?"([^"]*)"' % re.escape(name), html or "")
         or re.search(r"var\s+%s\s*=\s*(?:htmlDecode\()?'([^']*)'" % re.escape(name), html or ""))
    if not m:
        return ""
    return (m.group(1).replace("&quot;", '"').replace("&#39;", "'").replace("&amp;", "&")
            .replace("&lt;", "<").replace("&gt;", ">").replace(".html(false)", "").strip())


def parse_account(html: str) -> str:
    """从文章页里取公众号昵称（gh_ 开头的是内部 id，跳过）。"""
    for var in ("nickname", "profile_nickname", "user_name", "author"):
        val = wechat_var(html, var)
        if val and not val.startswith("gh_"):
            return val
    return ""
