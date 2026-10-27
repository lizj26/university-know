#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""微信公众号文章爬虫 —— pkucli / info-spider 方案的 Python 复刻。

来源
----
    https://github.com/pkuinfo/pkucli
      docs/wechat-mp-flow.md          抓包流程说明书（本文件的协议依据）
      crates/info-spider/src/*.rs      Rust 实现（login / search / articles / scrape）

它解决什么问题
--------------
`fetch_wechat.py` 走的是 wxmp 库 + 手工从浏览器复制的 cookies；本文件走的是
pkucli 那条路：**自己扫码登录 mp.weixin.qq.com 后台**，把 token / cookie 落盘，
之后按「首页 → 新建图文 → 超链接面板」的人类点击顺序调 searchbiz / appmsgpublish。
好处是不用再手工复制 cookies；代价是仍然需要**有公众平台权限的微信号**（测试号即可）。

协议要点（照抄 pkucli，改动会直接导致 ret=200003）
--------------------------------------------------
  1. GET  /                                          拿初始 cookie
  2. POST /cgi-bin/bizlogin?action=startlogin        开扫码会话
  3. GET  /cgi-bin/scanloginqrcode?action=getqrcode  取二维码（微信返回 JPEG）
  4. 手机扫码 → 轮询 ask，status: 0 等扫描 / 4 待确认 / 1 成功 / 5,6 过期
  5. POST /cgi-bin/bizlogin?action=login             redirect_url 里带 token
  6. GET  /cgi-bin/home?...token=                    坐实 session
  7. GET  /cgi-bin/appmsg?t=media/appmsg_edit_v2...  **只为拿 Referer**
  8. GET  /cgi-bin/searchbiz?action=search_biz       公众号 → fakeid
  9. GET  /cgi-bin/appmsgpublish?sub=list...         文章列表（三层嵌套 JSON）

第 7 步是整个方案的关键：searchbiz / appmsgpublish 只认「编辑器页面」当 Referer，
直接拿 BASE 当 Referer 会被判权限不足。这是 pkucli 相对其它公众号爬虫的核心差异。

用法
----
    python tools/wxmp_spider.py login                 # 扫码登录，会话存 ~/.config/info-spider
    python tools/wxmp_spider.py login --qr-only       # 只走到二维码，不轮询（自检协议可达性）
    python tools/wxmp_spider.py status
    python tools/wxmp_spider.py search "西安交通大学" --count 3
    python tools/wxmp_spider.py articles --name "西安交通大学" --limit 10 --format json
    python tools/wxmp_spider.py articles --fakeid MjM5NDgxNTQwNQ== --limit 5
    python tools/wxmp_spider.py scrape https://mp.weixin.qq.com/s/xxxx -o _work/wechat/a.md
    python tools/wxmp_spider.py import-legacy         # 复用 _work/wechat 里已有的 cookie+token

    python tools/wxmp_spider.py --home _work/wxmp-spider status     # 换会话目录

    python tools/wxmp_spider.py sogou "浙江大学" --body      # 零凭据公开索引（见下）
    python tools/wxmp_spider.py history --name 浙江大学 -n 20  # 某号全部新发布（要 wap_sid2）
    python tools/wxmp_spider.py probe                        # 端点存活矩阵（零凭据）

抓 wap_sid2（history 的唯一前置条件）
------------------------------------
    mitmdump -s tools/wap_sid2_capture.py --mode wireguard   # 安卓副号（WireGuard VPN）
    mitmdump -s tools/wap_sid2_capture.py --mode local       # PC 微信（本机免代理）
插件把 Cookie / uin / key / pass_ticket / appmsg_token 落到 `_work/wechat/`，
`history` 会自动拾取。可行性与风险见 docs/A路线-wap_sid2抓包可行性.md。

风控
----
    * 每次翻页、每次打开页面之间都有 0.5~1.5 倍抖动延迟（--delay-ms 调基准）
    * ret=200013 是账号级限流，等待无效，只能换号
    * ret=200003 是 session 失效，重新 login
    * **2026-07-30/31 起，第 9 步（跨公众号文章列表）被平台收紧**：新会话第一页就
      返回 200013，等多久都不恢复，且与账号无关（多个独立项目同期复现）。
      第 8 步 searchbiz 仍可用。详见 docs/wxmp公众号接入.md 10.13。

公开索引兜底（sogou 子命令）
---------------------------
`cgi-bin/appmsg` / `appmsgpublish` 这两个 CGI **没有被删除**（不带凭据仍返回
`{"base_resp":{"ret":200003}}`，编造的路径才是 404），关掉的是"跨公众号拉列表"这个**能力**。所以零凭据路线仍然是有效的补充：

    weixin.sogou.com 搜索 → /link?url=... 跳转链（需拼 url += '...' 片段）
    → mp.weixin.qq.com/s?src=11&... 签名短链 → 正文 #js_content

三个必须知道的限制：① 搜狗只收录部分号，按相关度而非时间排序，**不能当全量历史**；
② 跳转链带 timestamp 签名，**几分钟级时效**，拿到就得立刻用，不能存档；
③ 连续请求会触发 antispider 验证码，一批 10~30 条就要停手。
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import parse_qs, quote, unquote, urlparse

try:                                    # 从 tools/ 目录直接跑
    from wxmp_pool import (CredentialPool, PoolError as PoolUnavailable, RET_OTHER,
                           RET_WALL, read_cookie_file)
    from wxmp_pool import render_status as render_pool_status
except ImportError:                     # 以包形式导入（tests/ 里）
    from tools.wxmp_pool import (CredentialPool, PoolError as PoolUnavailable, RET_OTHER,
                                 RET_WALL, read_cookie_file)
    from tools.wxmp_pool import render_status as render_pool_status

BASE = "https://mp.weixin.qq.com"

# 桌面 Chrome：登录 / 后台页面 / xhr
UA_DESKTOP = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/146.0.0.0 Safari/537.36"
)
# 微信内置浏览器：抓文章正文页
UA_WECHAT = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Mobile/15E148 MicroMessenger/8.0.40(0x18002830) "
    "NetType/WIFI Language/zh_CN"
)

RET_OK = 0
RET_SESSION_EXPIRED = 200003
RET_RATE_LIMITED = 200013


# --------------------------------------------------------------------------- #
# 异常
# --------------------------------------------------------------------------- #
class WxmpError(RuntimeError):
    """带微信 ret 码的业务异常。"""

    def __init__(self, message: str, code: int = -1, err_msg: str = ""):
        super().__init__(message)
        self.code = code
        self.err_msg = err_msg


# --------------------------------------------------------------------------- #
# 会话存储（对应 pkucli 的 session.rs）
# --------------------------------------------------------------------------- #
DEFAULT_HOME = Path(os.environ.get("WXMP_SPIDER_HOME") or Path.home() / ".config" / "info-spider")


class Store:
    """会话目录：session.json（token/fingerprint/bizuin）+ cookies.json。"""

    def __init__(self, root: Path | str | None = None):
        self.root = Path(root) if root else DEFAULT_HOME
        self.root.mkdir(parents=True, exist_ok=True)

    @property
    def session_path(self) -> Path:
        return self.root / "session.json"

    @property
    def cookies_path(self) -> Path:
        return self.root / "cookies.json"

    def load_session(self) -> dict[str, Any] | None:
        if not self.session_path.exists():
            return None
        try:
            return json.loads(self.session_path.read_text("utf-8"))
        except (OSError, ValueError):
            return None

    def save_session(self, token: str, fingerprint: str, bizuin: str | None = None) -> dict[str, Any]:
        data = {
            "token": token,
            "fingerprint": fingerprint,
            "bizuin": bizuin,
            "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        }
        self.session_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), "utf-8")
        return data

    def load_cookies(self) -> list[dict[str, Any]]:
        if not self.cookies_path.exists():
            return []
        try:
            raw = json.loads(self.cookies_path.read_text("utf-8"))
        except (OSError, ValueError):
            return []
        if isinstance(raw, dict):  # 兼容 _work/wechat/wxmp-cookies.json 的扁平写法
            return [{"name": k, "value": str(v), "domain": ".qq.com"} for k, v in raw.items()]
        return raw if isinstance(raw, list) else []

    def save_cookies(self, session) -> None:
        items = [
            {
                "name": c.name,
                "value": c.value,
                "domain": c.domain,
                "path": c.path,
                "secure": bool(c.secure),
                "expires": c.expires,
            }
            for c in session.cookies
        ]
        self.cookies_path.write_text(json.dumps(items, ensure_ascii=False, indent=2), "utf-8")

    def clear(self) -> None:
        for p in (self.session_path, self.cookies_path):
            if p.exists():
                p.unlink()


def new_fingerprint() -> str:
    """32 位小写 hex，仿浏览器 canvas 指纹；同一 session 内必须保持不变。"""
    return "".join(random.choice("0123456789abcdef") for _ in range(32))


def jitter_sleep(base_ms: int) -> None:
    """pkucli 的节奏：base * U(0.5, 1.5)。"""
    time.sleep(base_ms * random.uniform(0.5, 1.5) / 1000.0)


# --------------------------------------------------------------------------- #
# HTTP 客户端
# --------------------------------------------------------------------------- #
def build_session(store: Store, ua: str = UA_DESKTOP):
    import requests

    sess = requests.Session()
    sess.headers.update(
        {
            "User-Agent": ua,
            "Accept-Language": "zh,zh-CN;q=0.9",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        }
    )
    for c in store.load_cookies():
        sess.cookies.set(
            c.get("name", ""),
            c.get("value", ""),
            domain=c.get("domain") or ".qq.com",
            path=c.get("path") or "/",
        )
    return sess


def xhr_headers(referer: str) -> dict[str, str]:
    return {
        "X-Requested-With": "XMLHttpRequest",
        "Referer": referer,
        "Accept": "*/*",
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-origin",
    }


@dataclass
class Ctx:
    """已「进入编辑器」的上下文：后续所有 cgi-bin 调用都靠它。"""

    store: Store
    session: Any
    token: str
    fingerprint: str
    referer: str


def _ret_of(resp: dict[str, Any]) -> tuple[int, str]:
    base = resp.get("base_resp") or {}
    return int(base.get("ret", -1)), str(base.get("err_msg") or "")


def check_ret(resp: dict[str, Any], what: str) -> None:
    ret, msg = _ret_of(resp)
    if ret == RET_OK:
        return
    if ret == RET_SESSION_EXPIRED:
        raise WxmpError(f"{what}: session 已失效（ret={ret}），请重新 login", ret, msg)
    if ret == RET_RATE_LIMITED:
        raise WxmpError(f"{what}: 触发账号级限流（ret={ret}），等待无效，请换号", ret, msg)
    raise WxmpError(f"{what}: ret={ret} err_msg={msg}", ret, msg)


def get_json(ctx: Ctx, url: str) -> dict[str, Any]:
    r = ctx.session.get(url, headers=xhr_headers(ctx.referer), timeout=30)
    r.raise_for_status()
    try:
        return r.json()
    except ValueError as e:  # 非 JSON：多半被风控拦成 HTML
        raise WxmpError(f"响应不是 JSON（{url[:80]}…），可能被风控拦截") from e


# --------------------------------------------------------------------------- #
# 1. 扫码登录（对应 login.rs）
# --------------------------------------------------------------------------- #
def extract_token(redirect_url: str) -> str:
    """redirect_url 形如 /cgi-bin/home?t=home/index&lang=zh_CN&token=362631694。"""
    if not redirect_url:
        raise WxmpError("redirect_url 为空，登录未成功")
    full = redirect_url if redirect_url.startswith("http") else BASE + redirect_url
    qs = parse_qs(urlparse(full).query)
    token = (qs.get("token") or [""])[0]
    if not token:
        raise WxmpError(f"redirect_url 中未找到 token: {redirect_url}")
    return token


def render_qr_terminal(jpg_path: Path, width: int = 40) -> bool:
    """用 PIL 把二维码缩成终端半块字符（▀/▄/█），失败就返回 False。"""
    try:
        from PIL import Image
    except ImportError:
        return False
    try:
        with Image.open(jpg_path) as raw:
            img = raw.convert("L")
    except Exception:
        return False
    w = max(20, min(width, 80))
    h = max(10, int(img.height / img.width * w))
    small = img.resize((w, h))
    px = small.load()
    lines = []
    for y in range(0, h - 1, 2):
        row = []
        for x in range(w):
            top = px[x, y] < 128      # 深色模块
            bot = px[x, y + 1] < 128
            row.append("█" if top and bot else "▀" if top else "▄" if bot else " ")
        lines.append("".join(row))
    print("\n".join(lines))
    return True


def login(store: Store, qr_only: bool = False, max_polls: int | None = None) -> dict[str, Any] | None:
    """走完整扫码链路。qr_only=True 时只到二维码（用于无手机时的协议自检）。"""
    old = store.load_session()
    if old:
        print(f"[info] 已有会话 token={old.get('token')} created_at={old.get('created_at')}，继续将覆盖")

    fp = new_fingerprint()
    if store.cookies_path.exists():
        store.cookies_path.unlink()          # 清旧 cookie，避免 uin 冲突
    sess = build_session(store)

    print("[1/7] 访问公众平台首页…")
    sess.get(BASE + "/", timeout=30).raise_for_status()
    jitter_sleep(400)

    print("[2/7] 启动扫码会话…")
    sid = f"177{int(time.time() * 1000)}{random.randint(10, 99)}"
    form = {
        "userlang": "zh_CN",
        "redirect_url": "",
        "login_type": "3",
        "sessionid": sid,
        "fingerprint": fp,
        "token": "",
        "lang": "zh_CN",
        "f": "json",
        "ajax": "1",
    }
    r = sess.post(
        f"{BASE}/cgi-bin/bizlogin?action=startlogin",
        data=form,
        headers=xhr_headers(BASE + "/"),
        timeout=30,
    )
    r.raise_for_status()
    check_ret(r.json(), "startlogin")

    print("[3/7] 下载二维码…")
    qr = sess.get(
        f"{BASE}/cgi-bin/scanloginqrcode?action=getqrcode&random={random.randint(0, 2**32 - 1)}",
        headers={"Referer": BASE + "/"},
        timeout=30,
    )
    qr.raise_for_status()
    qr_path = store.root / "login-qrcode.jpg"   # 微信实际返回 JPEG
    qr_path.write_bytes(qr.content)
    print(f"      二维码已保存：{qr_path}（{len(qr.content)} bytes, {qr.headers.get('Content-Type')}）")

    if qr_only:
        print("[stop] --qr-only：跳过轮询与登录握手")
        return None

    print("[4/7] 请用微信扫描上方二维码：")
    if not render_qr_terminal(qr_path):
        print(f"      终端渲染失败，请手动打开：{qr_path}")
    print()

    print("[5/7] 等待扫码确认…")
    last = -1
    polls = 0
    token = None
    while True:
        resp = sess.get(
            f"{BASE}/cgi-bin/scanloginqrcode?action=ask&fingerprint={fp}"
            "&token=&lang=zh_CN&f=json&ajax=1",
            headers=xhr_headers(BASE + "/"),
            timeout=30,
        )
        status = int(resp.json().get("status", -1))
        if status != last:
            print(
                {
                    0: "      等待扫描…",
                    4: "      ✓ 已扫码，请在手机上点确认",
                    1: "      ✓ 手机端已确认",
                }.get(status, f"      status={status}")
            )
            last = status
        if status == 1:
            break
        if status in (5, 6):
            raise WxmpError(f"二维码已失效（status={status}），请重新 login")
        polls += 1
        if max_polls is not None and polls >= max_polls:
            print(f"[stop] 已轮询 {polls} 次仍未确认（--max-polls），退出")
            return None
        time.sleep(random.uniform(1.0, 1.5))      # 贴近浏览器：1s + 抖动
    jitter_sleep(500)

    print("[6/7] 完成登录握手，提取 token…")
    r = sess.post(
        f"{BASE}/cgi-bin/bizlogin?action=login",
        data={
            "userlang": "zh_CN",
            "redirect_url": "",
            "cookie_forbidden": "0",
            "cookie_cleaned": "0",
            "plugin_used": "0",
            "login_type": "3",
            "fingerprint": fp,
            "token": "",
            "lang": "zh_CN",
            "f": "json",
            "ajax": "1",
        },
        headers=xhr_headers(BASE + "/"),
        timeout=30,
    )
    r.raise_for_status()
    body = r.json()
    check_ret(body, "bizlogin action=login")
    token = extract_token(str(body.get("redirect_url") or ""))
    bizuin = sess.cookies.get("bizuin")

    print("[7/7] 跟随跳转访问首页…")
    sess.get(BASE + str(body["redirect_url"]), headers={"Referer": BASE + "/"}, timeout=30)

    store.save_session(token, fp, bizuin)
    store.save_cookies(sess)
    print(f"\n[done] 登录成功  token={token}  bizuin={bizuin}  会话目录={store.root}")
    return store.load_session()


# --------------------------------------------------------------------------- #
# 2. warmup：拿到编辑器 Referer（对应 search.rs::warmup）
# --------------------------------------------------------------------------- #
def warmup(store: Store, quiet: bool = False) -> Ctx:
    info = store.load_session()
    if not info or not info.get("token"):
        raise WxmpError("尚未登录，请先执行 `login`（或 `import-legacy` 复用旧凭据）")

    token, fp = info["token"], info.get("fingerprint") or new_fingerprint()
    sess = build_session(store)

    home = f"{BASE}/cgi-bin/home?t=home/index&lang=zh_CN&token={token}"
    sess.get(home, headers={"Referer": BASE + "/"}, timeout=30).raise_for_status()
    jitter_sleep(600)

    editor = (
        f"{BASE}/cgi-bin/appmsg?t=media/appmsg_edit_v2&action=edit&isNew=1"
        f"&type=77&createType=0&token={token}&lang=zh_CN&timestamp={int(time.time() * 1000)}"
    )
    sess.get(editor, headers={"Referer": home}, timeout=30).raise_for_status()
    jitter_sleep(800)

    if not quiet:
        print(f"[warmup] 已进入编辑器，Referer 就绪（token={token}）", file=sys.stderr)
    return Ctx(store=store, session=sess, token=token, fingerprint=fp, referer=editor)


# --------------------------------------------------------------------------- #
# 3. 搜索公众号
# --------------------------------------------------------------------------- #
def search_biz(ctx: Ctx, query: str, count: int = 5) -> list[dict[str, Any]]:
    url = (
        f"{BASE}/cgi-bin/searchbiz?action=search_biz&begin=0&count={count}"
        f"&query={quote(query)}&fingerprint={ctx.fingerprint}&token={ctx.token}"
        "&lang=zh_CN&f=json&ajax=1"
    )
    resp = get_json(ctx, url)
    check_ret(resp, "searchbiz")
    out = []
    for it in resp.get("list") or []:
        out.append(
            {
                "fakeid": it.get("fakeid", ""),
                "nickname": it.get("nickname", ""),
                "alias": it.get("alias", ""),
                "signature": it.get("signature", ""),
                "service_type": it.get("service_type", 0),
                "verify_status": it.get("verify_status", 0),
            }
        )
    return out


# --------------------------------------------------------------------------- #
# 4. 文章列表（三层嵌套 JSON）
# --------------------------------------------------------------------------- #
def parse_publish_page(resp: dict[str, Any]) -> tuple[list[dict[str, Any]], int]:
    """外层 {publish_page: "<str>"} → 逐层 json.loads → appmsgex[] 摊平。"""
    raw = resp.get("publish_page")
    if not isinstance(raw, str):
        return [], 0
    try:
        page = json.loads(raw)
    except ValueError:
        return [], 0

    total = int(page.get("total_count") or 0)
    articles: list[dict[str, Any]] = []
    for item in page.get("publish_list") or []:
        info = item.get("publish_info")
        if isinstance(info, str):
            try:
                info = json.loads(info)
            except ValueError:
                continue
        if not isinstance(info, dict):
            continue
        for a in info.get("appmsgex") or []:
            if not isinstance(a, dict):
                continue
            articles.append(
                {
                    "aid": a.get("aid", ""),
                    "appmsgid": a.get("appmsgid", 0),
                    "itemidx": a.get("itemidx", 0),
                    "title": (a.get("title") or "").strip(),
                    "link": a.get("link", ""),
                    "cover": a.get("cover", ""),
                    "digest": a.get("digest", ""),
                    "author_name": a.get("author_name", ""),
                    "update_time": a.get("update_time", 0) or 0,
                    "create_time": a.get("create_time", 0) or 0,
                }
            )
    return articles, total


def fetch_page(ctx: Ctx, fakeid: str, begin: int = 0, count: int = 5) -> tuple[list[dict[str, Any]], int]:
    """fakeid 里的 == 必须编码成 %3D%3D，quote() 默认会做。"""
    url = (
        f"{BASE}/cgi-bin/appmsgpublish?sub=list&search_field=null&begin={begin}"
        f"&count={count}&query=&fakeid={quote(fakeid, safe='')}&type=101_1"
        f"&free_publish_type=1&sub_action=list_ex&fingerprint={ctx.fingerprint}"
        f"&token={ctx.token}&lang=zh_CN&f=json&ajax=1"
    )
    return parse_publish_page(get_json(ctx, url))


def list_articles(
    store: Store,
    name: str | None = None,
    fakeid: str | None = None,
    begin: int = 0,
    count: int = 5,
    limit: int | None = None,
    delay_ms: int = 1500,
) -> tuple[list[dict[str, Any]], int, str | None]:
    """按 pkucli 的翻页逻辑：够 limit 或某页不足一页就停，页间带抖动。"""
    ctx = warmup(store)

    nick = None
    if not fakeid:
        if not name:
            raise WxmpError("必须指定 --name 或 --fakeid")
        hits = search_biz(ctx, name, 5)
        if not hits:
            raise WxmpError(f"未找到匹配公众号: {name}")
        fakeid, nick = hits[0]["fakeid"], hits[0]["nickname"]
        print(f"[info] 命中公众号: {nick}（{hits[0]['alias']}）fakeid={fakeid}", file=sys.stderr)
        jitter_sleep(900)

    target = limit or count
    collected: list[dict[str, Any]] = []
    total = 0
    cur = begin
    while len(collected) < target:
        page, total = fetch_page(ctx, fakeid, cur, min(count, 20))
        if not page:
            break
        need = target - len(collected)
        collected.extend(page[:need])
        if len(page) < count:
            break
        cur += count
        jitter_sleep(delay_ms)

    store.save_cookies(ctx.session)   # 回写刷新后的 cookie
    return collected, total, nick


# --------------------------------------------------------------------------- #
# 5. 单篇 → Markdown（对应 scrape.rs）
# --------------------------------------------------------------------------- #
def _md_escape(text: str) -> str:
    return re.sub(r"[ \t\u00a0]+", " ", text)


def html_to_markdown(html: str) -> str:
    """抽取 #js_content 正文并转 Markdown（零第三方 md 库，只用 bs4）。"""
    from bs4 import BeautifulSoup, NavigableString, Tag

    soup = BeautifulSoup(html, "html.parser")
    for bad in soup.find_all(["script", "style", "svg", "noscript"]):
        bad.decompose()
    root = soup.find(id="js_content") or soup.body or soup

    def inline(node, depth: int = 0) -> str:
        parts: list[str] = []
        for child in node.children:
            if isinstance(child, NavigableString):
                parts.append(_md_escape(str(child)))
                continue
            if not isinstance(child, Tag):
                continue
            name = child.name.lower()
            if name == "br":
                parts.append("\n")
            elif name in ("strong", "b"):
                parts.append(f"**{inline(child, depth).strip()}**")
            elif name in ("em", "i"):
                parts.append(f"*{inline(child, depth).strip()}*")
            elif name == "code":
                parts.append(f"`{child.get_text()} `".replace(" `", "`"))
            elif name == "img":
                src = child.get("data-src") or child.get("src") or ""
                if src:
                    parts.append(f"![{child.get('alt') or ''}]({src})")
            elif name == "a":
                text = inline(child, depth).strip()
                href = child.get("href") or ""
                parts.append(f"[{text}]({href})" if href and text else text)
            elif name in ("h1", "h2", "h3", "h4", "h5", "h6"):
                parts.append(f"\n\n{'#' * int(name[1])} {inline(child, depth).strip()}\n\n")
            elif name == "li":
                parts.append(f"\n- {inline(child, depth).strip()}")
            elif name == "blockquote":
                body = inline(child, depth + 1).strip()
                parts.append("\n" + "\n".join("> " + ln for ln in body.splitlines()) + "\n")
            elif name == "pre":
                parts.append(f"\n```\n{child.get_text().strip()}\n```\n")
            elif name == "hr":
                parts.append("\n\n---\n\n")
            elif name in ("p", "div", "section", "article", "table", "tr", "ul", "ol", "figure", "span"):
                body = inline(child, depth)
                parts.append(body if name == "span" else f"\n\n{body.strip()}\n\n")
            else:
                parts.append(inline(child, depth))
        return "".join(parts)

    md = inline(root)
    md = re.sub(r"[\u200b\u200c\u200d\ufeff]", "", md)
    md = re.sub(r"[ \t]+\n", "\n", md)
    md = re.sub(r"\n{3,}", "\n\n", md)
    return md.strip()


def fetch_article_markdown(url: str) -> str:
    import requests

    r = requests.get(
        url,
        headers={
            "User-Agent": UA_WECHAT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9",
        },
        timeout=30,
    )
    r.raise_for_status()
    return html_to_markdown(r.text)


# --------------------------------------------------------------------------- #
# 5.5 公开索引兜底：搜狗微信（零凭据）
# --------------------------------------------------------------------------- #
# 为什么需要它：2026-07-30/31 起，后台「跨公众号文章列表」被平台收紧，
# 新会话第一页即 200013，与账号无关。此时要发现「某号最近发了什么」，
# 只剩两条零凭据的路：公开合集（mp/appmsgalbum）和搜狗微信。
# 前者只覆盖自己建了合集的号，后者覆盖更广但只到"搜索结果"级别。
SOGOU_SEARCH = "https://weixin.sogou.com/weixin"
SOGOU_HOME = "https://weixin.sogou.com/"

# 结果页每条在 <li id="sogou_vr_11002601_box_0" d="..."> 里；用 li 边界切块最稳，
# 直接对整个文档跑贪婪正则会把 10 条粘成 1 条。
_SOGOU_ITEM_RE = re.compile(r'<li id="sogou_vr_[^"]*_box_\d+"', re.S)
_SOGOU_TITLE_RE = re.compile(r'uigs="article_title_\d+"[^>]*>(.*?)</a>', re.S)
_SOGOU_HREF_RE = re.compile(r'href="(/link\?url=[^"]+)"')
_SOGOU_ACCOUNT_RE = re.compile(r'class="all-time-y\d?">(.*?)</span>', re.S)
_SOGOU_SUMMARY_RE = re.compile(r'class="txt-info"[^>]*>(.*?)</p>', re.S)
_SOGOU_TS_RE = re.compile(r"timeConvert\('(\d+)'\)")
_SOGOU_FRAG_RE = re.compile(r"url \+= '([^']*)'")


def _sogou_text(fragment: str) -> str:
    """去标签 + 去搜狗高亮注释 + 反转义。"""
    import html as _html

    text = re.sub(r"<!--.*?-->", "", fragment or "", flags=re.S)
    text = re.sub(r"<[^>]+>", "", text)
    return _html.unescape(text).replace("\u200b", "").strip()


def _sogou_session():
    import requests

    s = requests.Session()
    s.headers.update({
        "User-Agent": UA_DESKTOP,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9",
    })
    return s


def _sogou_warm(sess) -> None:
    """先访问首页拿 SUID/ABTEST。

    这一步不是客套：**不先访问首页直接去 /link，会把 822 字节的正常跳转页
    换成 antispider 页**。实测同样是首条链接，带首页预热 11 个 url 片段一次成功，
    不带则四条全挂。搜狗的风控盯的是"有没有正常会话起点"。
    """
    if getattr(sess, "_sogou_warmed", False):
        return
    try:
        sess.get(SOGOU_HOME, timeout=20, verify=False)
    except Exception:                                     # noqa: BLE001
        pass
    sess._sogou_warmed = True                          # type: ignore[attr-defined]


def _sogou_guard(page: str) -> None:
    if "antispider" in page or "请输入验证码" in page or "用户您好，您的访问过于频繁" in page:
        raise WxmpError(
            "搜狗触发了反爬验证码（antispider）。\n"
            "  这不是被封，是本机 IP 短时间内请求太多：停 10~30 分钟再跑，"
            "或把 --pages / --limit 调小。\n"
            "  浏览器里手动过一次验证码也能缓解（同一出口 IP 才有效）。"
        )


def parse_sogou_page(page: str) -> list[dict[str, Any]]:
    """搜狗结果页 HTML → 条目列表（纯函数，便于离线测试）。"""
    items: list[dict[str, Any]] = []
    cuts = [m.start() for m in _SOGOU_ITEM_RE.finditer(page)]
    for idx, start in enumerate(cuts):
        block = page[start: cuts[idx + 1] if idx + 1 < len(cuts) else len(page)]
        href = _SOGOU_HREF_RE.search(block)
        title = _SOGOU_TITLE_RE.search(block)
        if not href or not title:
            continue
        account = _SOGOU_ACCOUNT_RE.search(block)
        summary = _SOGOU_SUMMARY_RE.search(block)
        ts = _SOGOU_TS_RE.search(block)
        items.append({
            "title": _sogou_text(title.group(1)),
            "account": _sogou_text(account.group(1)) if account else "",
            "summary": _sogou_text(summary.group(1))[:120] if summary else "",
            "timestamp": int(ts.group(1)) if ts else 0,
            "sogou_link": "https://weixin.sogou.com" + href.group(1).replace("&amp;", "&"),
            "link": "",
        })
    return items


def parse_sogou_jump(page: str) -> str:
    """跳转页 HTML → 真实链接。搜狗用 `url += '...'` 分段拼，还夹 `@` 占位符。"""
    frags = _SOGOU_FRAG_RE.findall(page)
    return "".join(frags).replace("@", "") if frags else ""


def sogou_search(query: str, pages: int = 1, session=None) -> list[dict[str, Any]]:
    """搜狗微信搜索 → 条目列表（含跳转链，未解析成真实链接）。"""
    sess = session or _sogou_session()
    _sogou_warm(sess)
    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    for page in range(1, max(1, pages) + 1):
        r = sess.get(SOGOU_SEARCH,
                     params={"type": 2, "query": query, "ie": "utf8", "page": page},
                     headers={"Referer": SOGOU_HOME}, timeout=25, verify=False)
        if r.status_code != 200:
            raise WxmpError(f"搜狗搜索返回 HTTP {r.status_code}")
        _sogou_guard(r.text)

        for it in parse_sogou_page(r.text):
            if it["sogou_link"] in seen:
                continue
            seen.add(it["sogou_link"])
            items.append(it)
        if page < pages:
            jitter_sleep(1500)
    return items


def sogou_resolve(link: str, session=None) -> str:
    """跳转链 → 真实 mp.weixin.qq.com 链接。

    搜狗不直接给 URL，而是在落地页里用 JS 把 `url += '...'` 一段段拼起来，
    还夹着 `@` 占位符要删。想省事可以直接请求跟随跳转，但那条路会落在
    「环境异常」页，所以必须走拼接。
    """
    sess = session or _sogou_session()
    _sogou_warm(sess)
    last = ""
    for attempt in range(3):
        r = sess.get(link, headers={"Referer": SOGOU_HOME}, timeout=25, verify=False)
        if r.status_code != 200:
            raise WxmpError(f"搜狗跳转页返回 HTTP {r.status_code}")
        frags = _SOGOU_FRAG_RE.findall(r.text)
        if frags:
            return parse_sogou_jump(r.text)
        last = r.text
        _sogou_guard(r.text)          # 明确是验证码就直接抛，不浪费时间重试
        if attempt < 2:
            time.sleep(8 * (attempt + 1))     # 换来的多是"页面太短"，等一下就好
    raise WxmpError(
        "跳转页里没找到 url += 片段（重试 3 次）。\n"
        f"  最后一次响应 {len(last)} 字节，开头：{re.sub(chr(10), ' ', last)[:120]}\n"
        "  搜狗可能改版；先手动在浏览器打开一条 /link?url=... 看还有没有 url += 拼接。"
    )


def sogou_fetch(
    query: str,
    limit: int = 10,
    pages: int = 1,
    with_body: bool = False,
    out_dir: Path | None = None,
) -> list[dict[str, Any]]:
    """搜索 → 逐条解析真实链接（可选抓正文）。链接是签名短链，必须当场用。"""
    sess = _sogou_session()          # 搜索与解析必须共用同一个会话（SUID 就靠它带着）
    items = sogou_search(query, pages=pages, session=sess)
    items = items[:limit] if limit else items
    for i, it in enumerate(items):
        if i:
            jitter_sleep(1200)
        try:
            it["link"] = sogou_resolve(it["sogou_link"], session=sess)
        except WxmpError as exc:
            it["error"] = str(exc).splitlines()[0]
            continue
        if with_body:
            try:
                md = fetch_article_markdown(it["link"])
                it["chars"] = len(md)
                it["images"] = len(re.findall(r"!\[[^\]]*\]\(", md))
                if out_dir:
                    out_dir.mkdir(parents=True, exist_ok=True)
                    safe = re.sub(r'[\\/:*?"<>|\s]+', "-", it["title"])[:40]
                    path = out_dir / f"{safe}.md"
                    path.write_text(f"# {it['title']}\n\n> {it['account']}\n> {it['link']}\n\n{md}\n",
                                    encoding="utf-8")
                    it["md_path"] = str(path)
            except Exception as exc:                      # noqa: BLE001
                it["error"] = f"{type(exc).__name__}: {exc}"
    return items


# --------------------------------------------------------------------------- #
# 5.6 端点能力探测：一条命令分辨「接口被删 / 要凭据 / 被限流 / 能力被收紧」
# --------------------------------------------------------------------------- #
# 起因是网上流传「appmsg 接口已废弃」。实测发现「删了路由」和「关了能力」
# 在客户端体验上都是"点了没反应"，但处置完全相反，所以值得做成可复现的探针。
PROBE_TARGETS: tuple[tuple[str, str, str], ...] = (
    ("appmsg", "/cgi-bin/appmsg", "json"),
    ("appmsgpublish", "/cgi-bin/appmsgpublish", "json"),
    ("searchbiz", "/cgi-bin/searchbiz", "json"),
    ("profile_ext.getmsg", "/mp/profile_ext", "json"),
    ("profile_ext.home", "/mp/profile_ext", "html"),
    ("homepage", "/mp/homepage", "html"),
    ("appmsgalbum", "/mp/appmsgalbum", "json"),
)


def _probe_params(name: str, biz: str) -> dict[str, Any]:
    if name == "appmsg":
        return {"action": "list_ex", "begin": 0, "count": 5, "fakeid": biz, "type": 9,
                "query": "", "token": "", "lang": "zh_CN", "f": "json", "ajax": 1}
    if name == "appmsgpublish":
        return {"sub": "list", "search_field": "null", "begin": 0, "count": 5, "query": "",
                "fakeid": biz, "type": "101_1", "free_publish_type": 1,
                "sub_action": "list_ex", "token": "", "lang": "zh_CN", "f": "json", "ajax": 1}
    if name == "searchbiz":
        return {"action": "search_biz", "begin": 0, "count": 5, "query": "浙江大学",
                "token": "", "lang": "zh_CN", "f": "json", "ajax": 1}
    if name == "profile_ext.getmsg":
        return {"action": "getmsg", "__biz": biz, "f": "json", "offset": 0, "count": 10,
                "is_ok": 1, "scene": 124}
    if name == "profile_ext.home":
        return {"action": "home", "__biz": biz, "scene": 124, "is_ok": 1}
    if name == "homepage":
        return {"__biz": biz, "hid": "1", "cid": "1", "scene": 126}
    return {"action": "getalbum", "__biz": biz, "album_id": "", "f": "json"}


def probe_verdict(row: dict[str, Any]) -> tuple[str, str]:
    """单条探测结果 → (状态, 含义)。纯函数，便于离线测试。"""
    if row.get("http") == 404:
        return "路由不存在", "接口被删（真·废弃）"
    if row.get("json"):
        ret = row.get("ret")
        if ret == 200003:
            return "要凭据", "接口在，只是没 session（不是废弃）"
        if ret == -3:
            extra = ""
            if row.get("cookie_count") is not None:
                extra = f"（服务端数到 {row['cookie_count']} 个 cookie）"
            return "要 session", f"要 wap_sid2 一类客户端凭据{extra}——这正是 Hook 的目标"
        if ret == 200013:
            return "被限/收紧", "权限或频控问题，不是接口下线"
        if ret in (0, 10004):
            return "存活", "正常返回业务数据" if ret == 0 else "活着但参数不全"
        return f"业务错误 ret={ret}", "接口在"
    title = (row.get("title") or "").strip()
    if title == "验证":
        return "环境校验墙", "要微信内环境 / 客户端 cookie 才放行"
    if title == "错误":
        return "参数错误页", "路由在，缺 hid/cid 或参数不对"
    if row.get("http") == 200:
        return "存活", f"返回 {row.get('size', 0)} 字节 HTML"
    return f"HTTP {row.get('http')}", ""


def probe_capabilities(biz: str = "MjM5NDgxNTQwNQ==", timeout: int = 20) -> list[dict[str, Any]]:
    """不带任何凭据逐个打端点，看谁还活着、缺什么。只读，不改任何状态。"""
    import requests

    sess = requests.Session()
    sess.headers.update({"User-Agent": UA_WECHAT, "Accept-Language": "zh-CN,zh;q=0.9"})
    rows: list[dict[str, Any]] = []
    for name, path, kind in PROBE_TARGETS:
        row: dict[str, Any] = {"name": name, "path": path}
        try:
            r = sess.get(BASE + path, params=_probe_params(name, biz), timeout=timeout,
                         verify=False, allow_redirects=False)
            row["http"] = r.status_code
            row["size"] = len(r.content)
            if kind == "json":
                try:
                    data = r.json()
                    row["json"] = True
                    br = data.get("base_resp") or {}
                    row["ret"] = br.get("ret", data.get("ret"))
                    row["errmsg"] = br.get("err_msg") or br.get("errmsg") or data.get("errmsg") or ""
                    if "cookie_count" in data:
                        row["cookie_count"] = data["cookie_count"]
                except Exception:                            # noqa: BLE001
                    row["json"] = False
            if not row.get("json"):
                m = re.search(r"<title>(.*?)</title>", r.text, re.S)
                row["title"] = (m.group(1) if m else "").strip()
        except Exception as exc:                             # noqa: BLE001
            row["http"] = 0
            row["errmsg"] = f"{type(exc).__name__}: {exc}"
        row["verdict"], row["meaning"] = probe_verdict(row)
        rows.append(row)
        time.sleep(0.4)
    return rows


# --------------------------------------------------------------------------- #
# 5.7 全量历史：mp/profile_ext（要手机微信的 wap_sid2，零逆向）
# --------------------------------------------------------------------------- #
# 这是「拿到某号所有已发布文章链接」的唯一零逆向入口。
# 2026-10-24 实测：不带 cookie 它回 {"ret":-3,"errmsg":"no session","cookie_count":0}；
# 塞一个假 wap_sid2 → cookie_count 变 1，仍 no session。
# 也就是说它不校验签名、不需要 token，**只缺一个真的客户端 cookie**。
PROFILE_EXT = f"{BASE}/mp/profile_ext"

# 抓包插件（tools/wap_sid2_capture.py）的落盘位置；history 默认去这里找参数
WAP_COOKIES_PATH = Path("_work/wechat/wxmp-wap-cookies.txt")
WAP_PARAMS_PATH = Path("_work/wechat/wxmp-wap-params.json")
WAP_PARAM_KEYS = ("uin", "key", "pass_ticket", "appmsg_token", "__biz")


def parse_history(resp: dict[str, Any]) -> tuple[list[dict[str, Any]], bool]:
    """`action=getmsg` 响应 → (文章列表, 是否还能继续翻页)。

    两个坑：
      ① `general_msg_list` 是**字符串化的 JSON**，要再 loads 一次；
      ② 一条群发可能带多条图文（`multi_app_msg_item_list`），要摊平。
    """
    try:
        outer = json.loads(resp.get("general_msg_list") or "{}")
    except (TypeError, ValueError):
        outer = {}
    items: list[dict[str, Any]] = []
    for msg in outer.get("list") or []:
        info = msg.get("comm_msg_info") or {}
        if info.get("type") != 49:            # 49 = 图文消息，其它类型（文本/语音）跳过
            continue
        ts = int(info.get("datetime") or 0)
        ext = msg.get("app_msg_ext_info") or {}
        subs = [ext] + list(ext.get("multi_app_msg_item_list") or [])
        for idx, sub in enumerate(subs):
            if not sub or not sub.get("content_url"):
                continue
            items.append({
                "title": sub.get("title") or "",
                "link": (sub.get("content_url") or "").replace("#rd", "").replace("&amp;", "&"),
                "digest": sub.get("digest") or "",
                "author": sub.get("author") or "",
                "cover": sub.get("cover") or "",
                "datetime": ts,
                "idx": idx + 1,
                "msgid": info.get("id"),
            })
    cont = bool(resp.get("can_msg_continue"))
    if resp.get("ret") not in (0, None):
        cont = False
    return items, cont


def fetch_history_page(
    biz: str,
    cookie_header: str,
    offset: int = 0,
    count: int = 10,
    uin: str = "",
    key: str = "",
    pass_ticket: str = "",
    appmsg_token: str = "",
    timeout: int = 25,
) -> dict[str, Any]:
    import requests

    headers = {
        "User-Agent": UA_WECHAT,
        "Accept": "application/json, text/plain, */*",
        "Referer": f"{BASE}/mp/profile_ext?action=home&__biz={biz}&scene=124",
        "Cookie": re.sub(r"(?i)^\s*cookie:\s*", "", cookie_header.strip()),
    }
    params = {
        "action": "getmsg", "__biz": biz, "f": "json", "offset": offset, "count": count,
        "is_ok": 1, "scene": 124, "uin": uin, "key": key, "pass_ticket": pass_ticket,
        "appmsg_token": appmsg_token, "wxtoken": "", "x5": 0,
    }
    r = requests.get(PROFILE_EXT, params=params, headers=headers, timeout=timeout, verify=False)
    try:
        return r.json()
    except ValueError:
        body = r.text[:4000]
        wall = r.status_code == 200 or any(
            k in body for k in ("环境异常", "访问过于频繁", "captcha", "verify", "滑块"))
        err = WxmpError(
            f"profile_ext 返回的不是 JSON（HTTP {r.status_code}，{len(r.content)} 字节）。\n"
            "  多半是撞到了『验证』墙：cookie 无效、出口 IP 不干净，或请求环境被判异常。\n"
            "  别急着换号 —— 这类信号是账号/出口级的，继续打只会牵连更多号。\n"
            "  处理顺序：先降频（--delay-ms 2000 起）→ 再换出口 → 最后才重抓 wap_sid2。"
        )
        err.ret = RET_WALL if wall else RET_OTHER   # 号池据此决定"单号冷却"还是"整池熔断"
        raise err


def _list_history_once(
    biz: str,
    cookie_header: str,
    limit: int = 20,
    since: str | None = None,
    uin: str = "",
    key: str = "",
    pass_ticket: str = "",
    appmsg_token: str = "",
    delay_ms: int = 1500,
    max_pages: int = 20,
) -> list[dict[str, Any]]:
    """单份凭据翻页直到够 limit / 到 since / 没有更多（不换号、不重试）。"""
    cutoff = None
    if since:
        cutoff = datetime.strptime(since, "%Y-%m-%d").timestamp()   # 本地时区，与展示一致
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    offset = 0
    for page in range(max_pages):
        resp = fetch_history_page(biz, cookie_header, offset, 10, uin, key, pass_ticket,
                                  appmsg_token)
        ret = (resp.get("base_resp") or {}).get("ret", resp.get("ret"))
        if ret not in (0, None):
            hint = {
                -3: ("no session —— cookie 里没有真的 wap_sid2，或 uin/key/pass_ticket 不配套。"
                     "重跑一次抓包插件（tools/wap_sid2_capture.py）再试"),
                200013: "被限流/能力收紧，降低频率、换时间或换号",
            }.get(ret, "")
            err = WxmpError(f"getmsg 失败 ret={ret}（{hint or resp.get('errmsg', '')}）")
            err.ret = ret                       # 号池靠这个决定冷却多久
            raise err
        items, cont = parse_history(resp)
        if not items:
            break
        for it in items:
            if it["link"] in seen:
                continue
            seen.add(it["link"])
            if cutoff and it["datetime"] and it["datetime"] < cutoff:
                return out[:limit] if limit else out
            out.append(it)
            if limit and len(out) >= limit:
                return out
        if not cont:
            break
        offset += 10
        if page < max_pages - 1:
            jitter_sleep(delay_ms)
    return out


def list_history(
    biz: str,
    cookie_header: str = "",
    limit: int = 20,
    since: str | None = None,
    uin: str = "",
    key: str = "",
    pass_ticket: str = "",
    appmsg_token: str = "",
    delay_ms: int = 1500,
    max_pages: int = 20,
    pool: "CredentialPool | None" = None,
    max_swaps: int = 2,
) -> list[dict[str, Any]]:
    """翻页取文章；给了 `pool` 就自动选号、失败换号重试。

    换号策略：**一次任务固定用一个号跑完**（避免会话跳变），只在出错时换。
    换号后从 offset 0 重来 —— `_list_history_once` 内部按链接去重，
    重来不会产生重复条目，最坏只是多花几次请求。
    """
    if pool is None:
        return _list_history_once(biz, cookie_header, limit, since, uin, key,
                                  pass_ticket, appmsg_token, delay_ms, max_pages)

    last: WxmpError | None = None
    for attempt in range(max_swaps + 1):
        try:
            cred = pool.acquire()
        except PoolUnavailable as exc:
            raise WxmpError(f"号池暂时没有可用凭据：{exc}") from exc
        p = cred.params
        if attempt:
            print(f"[pool] 换号重试（第 {attempt + 1} 次）→ {cred.name}", file=sys.stderr)
        try:
            rows = _list_history_once(
                biz, cred.cookie, limit, since,
                p.get("uin", uin), p.get("key", key),
                p.get("pass_ticket", pass_ticket),
                p.get("appmsg_token", appmsg_token), delay_ms, max_pages,
            )
        except WxmpError as exc:
            last = exc
            ret = getattr(exc, "ret", RET_OTHER)
            cool = pool.report(cred.name, ret)
            print(f"[pool] {cred.name} 失败 ret={ret}，冷却 {cool}s", file=sys.stderr)
            if ret == RET_WALL:
                # 撞验证墙不换号：换号只会让更多号被牵连，直接停。
                raise WxmpError(
                    f"{exc}\n  （已停手，不再换号。号池熔断信号 "
                    f"{pool.breaker_status().strip()}）"
                ) from exc
            continue
        pool.report(cred.name, 0)
        return rows
    if last is not None:
        raise WxmpError(f"{last}\n  （已试 {max_swaps + 1} 个号；其余号状态见 `pool --status`）") from last
    raise WxmpError("号池里没有可用的号")


# --------------------------------------------------------------------------- #
# 5.8 增量轮询：长期自动化的最小实现
# --------------------------------------------------------------------------- #
# 思路照抄「清华知道」那类站点：**只轮询第一页**，靠 10 条缓冲吸收漏掉的轮次。
# 每轮 1 次请求，40 个号按 2 小时轮转也只有 20 次/小时，不会触发 200013。
def diff_new(seen: set[str], items: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """只留下没见过的链接，保持输入顺序。"""
    out: list[dict[str, Any]] = []
    for it in items:
        link = it.get("link")
        if link and link not in seen:
            out.append(it)
    return out


def load_seen(path: Path) -> set[str]:
    """读取已见链接集合；文件坏了就当空的（下次会重复一遍，不会丢数据）。"""
    if not path.exists():
        return set()
    try:
        data = json.loads(path.read_text("utf-8"))
    except (OSError, ValueError):
        return set()
    if isinstance(data, dict):
        data = data.get("links") or []
    return {x for x in data if isinstance(x, str)}


def save_seen(path: Path, seen: set[str], keep: int = 8000) -> None:
    """落盘；只保留最近 keep 条，避免文件无限长。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    links = list(seen)[-keep:]
    path.write_text(json.dumps({"count": len(links), "links": links},
                               ensure_ascii=False), "utf-8")


def watch_history(
    biz: str,
    cookie_header: str,
    interval: int = 300,
    cycles: int = 0,
    state_path: Path | None = None,
    output: Path | None = None,
    limit: int = 50,
    since: str | None = None,
    uin: str = "",
    key: str = "",
    pass_ticket: str = "",
    appmsg_token: str = "",
    delay_ms: int = 800,
    pool: "CredentialPool | None" = None,
) -> int:
    """每 interval 秒拉一次第一页，只打印/落盘新增条目。返回累计新增数。

    `cycles=0` 表示不限轮数；测试或"只跑一轮"时给 1。
    `output` 是 JSONL（每行一条，带 `seen_at`），方便直接喂给下游。
    """
    state_path = state_path or Path(f"_work/wechat/history-seen-{biz[:12]}.json")
    seen = load_seen(state_path)
    total_new = 0
    round_no = 0
    while True:
        round_no += 1
        rows = list_history(biz, cookie_header, limit, since, uin, key, pass_ticket,
                            appmsg_token, delay_ms, 1, pool=pool)   # max_pages=1：只看第一页
        fresh = diff_new(seen, rows)
        stamp = datetime.now().strftime("%H:%M:%S")
        if fresh:
            print(f"[{stamp}] 第 {round_no} 轮：新增 {len(fresh)} 条")
            for it in fresh:
                when = (datetime.fromtimestamp(it["datetime"]).strftime("%Y-%m-%d %H:%M")
                        if it.get("datetime") else "?")
                print(f"    {when}  {it.get('title', '')}")
                print(f"              {it.get('link', '')}")
            if output:
                output.parent.mkdir(parents=True, exist_ok=True)
                with output.open("a", encoding="utf-8") as fh:
                    for it in fresh:
                        fh.write(json.dumps({**it, "seen_at": datetime.now().isoformat(
                            timespec="seconds")}, ensure_ascii=False) + "\n")
            seen.update(it["link"] for it in fresh)
            save_seen(state_path, seen)
        else:
            print(f"[{stamp}] 第 {round_no} 轮：无新增（首页 {len(rows)} 条都已见过）")
        total_new += len(fresh)
        if cycles and round_no >= cycles:
            return total_new
        jitter_sleep(interval * 1000)


def resolve_biz(name: str | None, biz: str | None, cache: Path | None = None) -> tuple[str, str]:
    """号名 → (biz, 昵称)。fakeid 与 __biz 同值，所以直接复用已有缓存。"""
    if biz:
        return biz, ""
    if not name:
        raise WxmpError("给 --biz 或 --name 其中之一")
    path = cache or Path("_work/wechat/wxmp-fakeid.json")
    if not path.exists():
        raise WxmpError(f"没有 fakeid 缓存 {path}（先跑一次 fetch_wechat.py --search-only）")
    data = json.loads(path.read_text("utf-8"))
    want = name.strip().lower()
    for key, val in data.items():
        if not isinstance(val, dict) or not val.get("fakeid"):
            continue
        if str(key).strip().lower() == want or str(val.get("nickname", "")).strip().lower() == want:
            return val["fakeid"], val.get("nickname", "")
    hits = [(k, v) for k, v in data.items()
            if isinstance(v, dict) and v.get("fakeid")
            and (want in str(k).lower() or want in str(v.get("nickname", "")).lower())]
    if len(hits) == 1:
        return hits[0][1]["fakeid"], hits[0][1].get("nickname", "")
    if len(hits) > 1:
        raise WxmpError("匹配到多个号，请用 --biz 指定：\n  "
                        + "\n  ".join(f"{k} → {v['fakeid']}" for k, v in hits[:8]))
    raise WxmpError(f"缓存里没有“{name}”，先用 fetch_wechat.py --search-only 搜一遍")


def load_capture_params(path: Path | None = None) -> dict[str, str]:
    """读抓包落下的参数文件（uin / key / pass_ticket / appmsg_token / __biz）。

    顺序：显式 `--params` → 默认 `_work/wechat/wxmp-wap-params.json`。
    文件不存在返回 {}（允许「只有 cookie」的裸跑）；只挑认识的键，防止脏字段混进请求。
    """
    target = path or WAP_PARAMS_PATH
    if not target.exists():
        return {}
    try:
        raw = json.loads(target.read_text("utf-8"))
    except (OSError, ValueError) as exc:                     # noqa: BLE001
        print(f"[!] 读参数文件 {target} 失败（{exc}），忽略", file=sys.stderr)
        return {}
    if not isinstance(raw, dict):
        return {}
    out: dict[str, str] = {}
    for k in WAP_PARAM_KEYS:
        v = raw.get(k)
        if isinstance(v, str) and v.strip():
            out[k] = unquote(v.strip())
    return out


def parse_curl(text: str) -> dict[str, str]:
    """从「Copy as cURL」里捞出 Cookie / uin / key / pass_ticket / __biz。

    抓包工具给的 cURL 有三种写法要都认：`-H 'Cookie: ...'`、`-b '...'`、
    `--header 'cookie: ...'`（大小写和引号都不统一）。
    """
    out: dict[str, str] = {}
    cookie = ""
    for pat in (r"-H\s+'Cookie:\s*([^']*)'", r'-H\s+"Cookie:\s*([^"]*)"',
                r"--header\s+'[Cc]ookie:\s*([^']*)'", r"-b\s+'([^']*)'", r'-b\s+"([^"]*)"'):
        m = re.search(pat, text)
        if m and m.group(1).strip():
            cookie = m.group(1).strip()
            break
    if cookie:
        out["cookie"] = cookie

    for key in ("uin", "key", "pass_ticket", "appmsg_token", "__biz"):
        m = re.search(r"[?&]" + re.escape(key) + r"=([^&'\"]+)", text)
        if m:
            out[key] = unquote(m.group(1))       # URL 里是 %3D%3D 这种，参数要还原
    return out


def mask_cookie(cookie: str, keep: int = 6) -> str:
    """展示用：只留前 keep 个字符，别把整串凭据打到终端/日志里。"""
    pairs = [p.strip() for p in cookie.split(";") if "=" in p]
    shown = []
    for p in pairs:
        k, _, v = p.partition("=")
        shown.append(f"{k}={v[:keep]}…" if len(v) > keep else f"{k}={v}")
    return "; ".join(shown)


# --------------------------------------------------------------------------- #
# 6. 复用旧凭据（_work/wechat 里手工复制的 cookie + token）
# --------------------------------------------------------------------------- #
def import_legacy(store: Store, cookie_file: Path, token_file: Path | None, fingerprint: str | None) -> None:
    if cookie_file.suffix.lower() == ".txt":       # Cookie: 头格式
        pairs = []
        for chunk in cookie_file.read_text("utf-8", "replace").replace("Cookie:", "").split(";"):
            if "=" in chunk:
                k, v = chunk.split("=", 1)
                pairs.append({"name": k.strip(), "value": v.strip(), "domain": ".qq.com"})
        store.cookies_path.write_text(json.dumps(pairs, ensure_ascii=False, indent=2), "utf-8")
    else:
        raw = json.loads(cookie_file.read_text("utf-8"))
        if isinstance(raw, dict):
            pairs = [{"name": k, "value": str(v), "domain": ".qq.com"} for k, v in raw.items()]
        else:
            pairs = raw
        store.cookies_path.write_text(json.dumps(pairs, ensure_ascii=False, indent=2), "utf-8")

    token = ""
    if token_file and token_file.exists():
        token = token_file.read_text("utf-8").strip()
    if not token:
        raise WxmpError("缺少 token（--token-file），appmsgpublish 无法调用")
    store.save_session(token, fingerprint or new_fingerprint(), None)
    print(f"[import] cookie={len(pairs)} 项 → {store.cookies_path}")
    print(f"[import] token={token} → {store.session_path}")


# --------------------------------------------------------------------------- #
# 6.4 反向导出：给 fetch_wechat.py 用的 Cookie 头文件
# --------------------------------------------------------------------------- #
def export_cookie_header(
    store: Store,
    target: Path,
    force: bool = False,
    write_token: bool = False,
) -> dict[str, Any]:
    """把扫码登录得到的会话导出成 `fetch_wechat.py` 认的「Cookie: k=v; k2=v2」整行。

    `import-legacy` 是 fetch_wechat → 本工具，这个是反方向：本工具 → 主链路。
    导出的文件按主链路约定命名（`wxmp-cookie-header.txt` / `-poolN.txt`），
    格式与浏览器 F12 复制出来的完全一致，所以主链路无需任何改动。
    """
    cookies = store.load_cookies()
    if not cookies:
        raise WxmpError("本地没有 cookie，先执行 `login`")

    pairs = [f"{c['name']}={c['value']}" for c in cookies if c.get("name") and c.get("value")]
    if not pairs:
        raise WxmpError("cookie 全部为空，无法导出")

    if target.exists() and not force:
        raise WxmpError(f"{target} 已存在。要覆盖请加 --force（会先备份成 .bak）")
    if target.exists() and force:
        target.with_suffix(target.suffix + ".bak").write_bytes(target.read_bytes())

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("; ".join(pairs) + "\n", "utf-8")

    token = (store.load_session() or {}).get("token") or ""
    token_path = None
    if write_token and token:
        token_path = target.parent / "wxmp-token.txt"
        if token_path.exists():
            token_path.with_suffix(token_path.suffix + ".bak").write_bytes(token_path.read_bytes())
        token_path.write_text(token + "\n", "utf-8")

    return {
        "target": str(target),
        "cookies": len(pairs),
        "names": [c["name"] for c in cookies if c.get("name")],
        "token": token,
        "token_file": str(token_path) if token_path else None,
    }


# --------------------------------------------------------------------------- #
# 6.5 限流诊断：账号级还是 fakeid 级？
# --------------------------------------------------------------------------- #
@dataclass
class Probe:
    """对单个公众号拉一页列表的探测结果。"""

    name: str
    fakeid: str
    code: int = -1          # base_resp.ret；0 = 成功
    articles: int = 0
    err: str = ""

    @property
    def blocked(self) -> bool:
        return self.code != RET_OK


VERDICTS: dict[str, tuple[str, str]] = {
    "credential_expired": (
        "凭据已失效（200003）",
        "不是限流。重新 login 或换一份 cookies 再诊断。",
    ),
    "account": (
        "全部号都 200013（账号级限流 或 接口级收紧）",
        "先换第二个登录账号复诊：换号后正常=账号级（加 pool2 轮换）；"
        "换号后仍全 200013=2026-07-30 起的接口级收紧（docs/wxmp公众号接入.md 10.13），"
        "别再换号或注销，改走合集源 + sogou 子命令。",
    ),
    "target": (
        "fakeid 级限流",
        "凭据本身可用，坏的只是个别公众号 → 不用换号，让脚本跳过这几个号继续抓其余的。",
    ),
    "ok": (
        "正常，没有限流",
        "拉列表全部成功，可以正常抓取。",
    ),
    "search_only": (
        "搜索被拒但列表正常",
        "少见：searchbiz 报错而 appmsgpublish 成功，多半是瞬时问题，重跑一次即可。",
    ),
    "empty": (
        "限流但拿不到判定依据",
        "所有号都返回 0 篇却没报 200013，可能是号本身没有文章或参数问题，看单条 ret 再定。",
    ),
}


def classify_verdict(search_ok: bool, search_code: int, probes: list[Probe]) -> tuple[str, str, str]:
    """把一串探测结果压成一个结论：返回 (key, 结论, 处置建议)。

    判定规则沿用 docs/wxmp公众号接入.md 10.2：
      * 连 searchbiz 都 200003 → 凭据过期，跟限流无关
      * 全部 200013           → 账号级限流 或 接口级收紧（单份凭据分不开，需换号复诊）
      * 部分成功、部分 200013 → fakeid 级（只跳过坏的）
    """
    if search_code == RET_SESSION_EXPIRED or any(p.code == RET_SESSION_EXPIRED for p in probes):
        return "credential_expired", *VERDICTS["credential_expired"]

    if not probes:
        return "empty", *VERDICTS["empty"]

    good = [p for p in probes if not p.blocked and p.articles > 0]
    limited = [p for p in probes if p.code == RET_RATE_LIMITED]

    if len(limited) == len(probes):
        return "account", *VERDICTS["account"]
    if good and limited:
        return "target", *VERDICTS["target"]
    if not limited and good and not search_ok:
        return "search_only", *VERDICTS["search_only"]
    if not limited and len(good) == len(probes):
        return "ok", *VERDICTS["ok"]
    return "empty", *VERDICTS["empty"]


def load_account_names(campus: str | None, root: Path | None = None) -> list[str]:
    """兼容 `wxmp-accounts[-<校区>].json` 的三种写法（见 fetch_wechat.py 文档）。"""
    root = root or Path("_work/wechat")
    names: list[str] = []
    files = [root / "wxmp-accounts.json"]
    if campus:
        files.insert(0, root / f"wxmp-accounts-{campus}.json")
    for f in files:
        if not f.exists():
            continue
        try:
            data = json.loads(f.read_text("utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, list):
            names += [str(x) for x in data]
        elif isinstance(data, dict):
            if isinstance(data.get("accounts"), list):
                for a in data["accounts"]:
                    if isinstance(a, dict) and a.get("name"):
                        names.append(str(a["name"]))
                    elif isinstance(a, str):
                        names.append(a)
            else:
                for k, v in data.items():
                    names.append(str(v["name"]) if isinstance(v, dict) and v.get("name") else str(k))
    # 去重保序
    seen, out = set(), []
    for n in names:
        if n and n not in seen:
            seen.add(n)
            out.append(n)
    return out


def diagnose(
    store: Store,
    names: list[str],
    count: int = 5,
    out_dir: Path | None = None,
    quiet: bool = False,
) -> dict[str, Any]:
    """一次跑完：warmup → searchbiz → 逐个号拉一页 → 给结论。

    只读不写：除了自己的诊断报告，不改动 fetch_wechat.py 的 wxmp-blocked.json
    （那是主链路的状态文件，避免诊断行为影响它的跳过逻辑）。
    """
    probes: list[Probe] = []
    search_ok, search_code, search_err = False, -1, ""
    ctx = None

    try:
        ctx = warmup(store, quiet=True)
        for name in names[:1]:
            hits = search_biz(ctx, name, 1)
            search_ok = True
            if not hits:
                search_err = f"searchbiz 成功但没搜到「{name}」"
    except WxmpError as e:
        search_code, search_err = e.code, str(e)

    if ctx and search_ok:
        for name in names:
            probe = Probe(name=name, fakeid="")
            try:
                hits = search_biz(ctx, name, 1)
                if not hits:
                    probe.err = "未搜到该号"
                else:
                    probe.fakeid = hits[0]["fakeid"]
                    arts, _total = fetch_page(ctx, probe.fakeid, 0, min(count, 20))
                    probe.code, probe.articles = RET_OK, len(arts)
                    if not arts:
                        probe.err = "返回 0 篇（可能确实没新文章）"
            except WxmpError as e:
                probe.code, probe.err = e.code, str(e)
            probes.append(probe)
            if not quiet:
                mark = "✓" if probe.code == RET_OK and probe.articles else "⚠"
                print(f"    {mark} {name:<20} ret={probe.code:<7} 取回 {probe.articles} 篇", file=sys.stderr)
            if name != names[-1]:
                jitter_sleep(1200)

    key, verdict, advice = classify_verdict(search_ok, search_code, probes)
    report = {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "session_created_at": (store.load_session() or {}).get("created_at"),
        "token": (store.load_session() or {}).get("token"),
        "search": {"ok": search_ok, "ret": search_code, "err": search_err},
        "probes": [p.__dict__ for p in probes],
        "verdict": key,
        "verdict_text": verdict,
        "advice": advice,
    }

    print()
    print(f"  结论：{verdict}")
    print(f"  处置：{advice}")
    if key == "target":
        bad = [p.name for p in probes if p.code == RET_RATE_LIMITED]
        print(f"  受影响的号（{len(bad)}）：{'、'.join(bad)}")

    if out_dir:
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / f"wxmp-diag-{datetime.now():%Y%m%d-%H%M%S}.json"
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2), "utf-8")
        print(f"  报告：{path}")
    return report


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _print_table(rows: Iterable[dict[str, Any]]) -> None:
    rows = list(rows)
    if not rows:
        print("（空）")
        return
    for i, a in enumerate(rows, 1):
        ts = a.get("update_time") or a.get("create_time") or 0
        date = datetime.fromtimestamp(ts).strftime("%Y-%m-%d") if ts else "-"
        print(f"{i:>2}. [{date}] {a.get('title', '')}")
        link = a.get("link") or a.get("fakeid") or ""
        if link:
            print(f"    {link}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="微信公众号文章爬虫（pkucli/info-spider 的 Python 复刻）")
    ap.add_argument("--home", help=f"会话目录，默认 {DEFAULT_HOME}")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("login", help="扫码登录并把会话落盘")
    p.add_argument("--qr-only", action="store_true", help="只跑到二维码，不轮询（协议自检）")
    p.add_argument("--max-polls", type=int, default=None, help="最多轮询几次（无手机时限制耗时）")

    sub.add_parser("status", help="查看本地会话")
    sub.add_parser("logout", help="清除本地会话")

    p = sub.add_parser("search", help="搜索公众号")
    p.add_argument("query")
    p.add_argument("-n", "--count", type=int, default=5)
    p.add_argument("--format", default="table", choices=["table", "json"])

    p = sub.add_parser("articles", help="列出公众号文章")
    p.add_argument("--name")
    p.add_argument("--fakeid")
    p.add_argument("--begin", type=int, default=0)
    p.add_argument("--count", type=int, default=5)
    p.add_argument("-l", "--limit", type=int, default=None)
    p.add_argument("--delay-ms", type=int, default=1500)
    p.add_argument("--format", default="table", choices=["table", "json", "jsonl"])

    p = sub.add_parser("scrape", help="单篇文章 → Markdown")
    p.add_argument("url")
    p.add_argument("-o", "--output")

    p = sub.add_parser("import-legacy", help="复用已有 cookie+token")
    p.add_argument("--cookie-file", default="_work/wechat/wxmp-cookies.json")
    p.add_argument("--token-file", default="_work/wechat/wxmp-token.txt")
    p.add_argument("--fingerprint", default=None)

    p = sub.add_parser("export-cookie", help="把会话导成 fetch_wechat.py 认的 Cookie 头文件")
    p.add_argument("--to", default="_work/wechat/wxmp-cookie-header-pool2.txt")
    p.add_argument("--force", action="store_true", help="覆盖已存在的目标（先备份 .bak）")
    p.add_argument("--write-token", action="store_true", help="同时写 _work/wechat/wxmp-token.txt")

    p = sub.add_parser("diag", help="诊断 200013 是账号级还是 fakeid 级")
    p.add_argument("--campus", help="从 _work/wechat/wxmp-accounts-<校区>.json 读名单")
    p.add_argument("--name", action="append", help="手工指定公众号名（可重复）")
    p.add_argument("--count", type=int, default=5, help="每页拉几篇（默认 5）")
    p.add_argument("--out", default="_work/wechat", help="诊断报告落盘目录，空字符串=不留档")

    p = sub.add_parser("sogou", help="搜狗微信公开索引（零凭据，后台列表被收紧时的兜底）")
    p.add_argument("query")
    p.add_argument("-n", "--limit", type=int, default=10, help="最多取几条（默认 10）")
    p.add_argument("--pages", type=int, default=1, help="翻几页搜索（每页 10 条）")
    p.add_argument("--body", action="store_true", help="顺带把正文转 Markdown")
    p.add_argument("--md-dir", default="", help="正文落盘目录（配合 --body）")
    p.add_argument("--format", default="table", choices=["table", "json"])

    p = sub.add_parser("probe", help="零凭据探测各端点：接口被删 / 要凭据 / 被限流")
    p.add_argument("--biz", default="MjM5NDgxNTQwNQ==", help="用来测 __biz 类接口的公众号 biz")
    p.add_argument("--format", default="table", choices=["table", "json"])

    p = sub.add_parser("pool", help="号池：查看/加入凭据（方案 A 的多号轮换）")
    p.add_argument("--status", action="store_true", help="打印每个号的配额、冷却、健康度")
    p.add_argument("--dir", default="_work/wechat/wxmp-pool", help="号池目录")
    p.add_argument("--add", metavar="NAME", help="把一份 cookie 加进池（配合 --cookie-file）")
    p.add_argument("--cookie-file", default="_work/wechat/wxmp-wap-cookies.txt",
                   help="要加入的整行 Cookie 文件")
    p.add_argument("--params", help="配套参数 JSON（uin/key/pass_ticket/appmsg_token）")
    p.add_argument("--quota", type=int, default=120, help="每号每小时请求上限（默认 120）")
    p.add_argument("--resume", action="store_true",
                   help="解除整池熔断（换出口/降频之后再开闸）")

    p = sub.add_parser("history", help="某号全部已发布文章链接（要手机微信的 wap_sid2）")
    p.add_argument("--name", help="公众号名（用 _work/wechat/wxmp-fakeid.json 里的 fakeid 当 biz）")
    p.add_argument("--biz", help="直接给 __biz（与 fakeid 同值）")
    p.add_argument("--cookie-file", default="_work/wechat/wxmp-wap-cookies.txt",
                   help="整行 Cookie: ...（含 wap_sid2），从手机微信抓")
    p.add_argument("--uin", default="", help="地址栏里的 uin（可空）")
    p.add_argument("--key", default="", help="地址栏里的 key（可空）")
    p.add_argument("--pass-ticket", default="", help="地址栏里的 pass_ticket（可空）")
    p.add_argument("--appmsg-token", default="", help="地址栏里的 appmsg_token（旧版接口常要，可空）")
    p.add_argument("--params", help="抓包参数 JSON（默认 _work/wechat/wxmp-wap-params.json）")
    p.add_argument("--pool-dir", help="用号池轮换凭据（给了就忽略 --cookie-file）")
    p.add_argument("--quota", type=int, default=120, help="号池里每号每小时请求上限")
    p.add_argument("--watch", action="store_true",
                   help="持续轮询第一页、只输出新增（长期自动化；配合 --interval）")
    p.add_argument("--interval", type=int, default=300, help="轮询间隔秒（默认 300）")
    p.add_argument("--cycles", type=int, default=0, help="轮询轮数，0=不限（自测用 1）")
    p.add_argument("--state", help="已见链接状态文件（默认 _work/wechat/history-seen-<biz>.json）")
    p.add_argument("-n", "--limit", type=int, default=20)
    p.add_argument("--since", help="只取这天之后的（YYYY-MM-DD）")
    p.add_argument("--max-pages", type=int, default=20)
    p.add_argument("--delay-ms", type=int, default=1500)
    p.add_argument("-o", "--output", help="落盘 JSON")
    p.add_argument("--diff-from", help="上一轮的 history JSON，只输出这次新增的链接")
    p.add_argument("--format", default="table", choices=["table", "json"])

    p = sub.add_parser("cookie-from-curl", help="从抓包工具「Copy as cURL」里提取 wap cookie")
    p.add_argument("-f", "--file", help="curl 文本文件；不给就从 stdin 读")
    p.add_argument("--to", default="_work/wechat/wxmp-wap-cookies.txt")
    p.add_argument("--force", action="store_true", help="覆盖已存在的文件")

    args = ap.parse_args(argv)
    store = Store(args.home)

    try:
        if args.cmd == "login":
            login(store, qr_only=args.qr_only, max_polls=args.max_polls)
        elif args.cmd == "status":
            s = store.load_session()
            if not s:
                print(f"[ ] 未登录。会话目录：{store.root}")
            else:
                print(f"[●] 已登录  token={s.get('token')}  fingerprint={s.get('fingerprint')}")
                print(f"    bizuin={s.get('bizuin')}  created_at={s.get('created_at')}")
                print(f"    cookies={len(store.load_cookies())} 项  dir={store.root}")
        elif args.cmd == "logout":
            store.clear()
            print("[✓] 已清除本地会话")
        elif args.cmd == "import-legacy":
            import_legacy(store, Path(args.cookie_file), Path(args.token_file) if args.token_file else None,
                          args.fingerprint)
        elif args.cmd == "search":
            ctx = warmup(store)
            hits = search_biz(ctx, args.query, args.count)
            store.save_cookies(ctx.session)
            if args.format == "json":
                print(json.dumps(hits, ensure_ascii=False, indent=2))
            else:
                for i, h in enumerate(hits, 1):
                    print(f"{i:>2}. {h['nickname']}（{h['alias']}）\n    fakeid: {h['fakeid']}")
                    if h["signature"]:
                        print(f"    签名: {h['signature']}")
        elif args.cmd == "articles":
            arts, total, nick = list_articles(
                store, args.name, args.fakeid, args.begin, args.count, args.limit, args.delay_ms
            )
            if args.format == "json":
                print(json.dumps(arts, ensure_ascii=False, indent=2))
            elif args.format == "jsonl":
                for a in arts:
                    print(json.dumps(a, ensure_ascii=False))
            else:
                if nick:
                    print(f"● {nick}（共 {total} 篇，取到 {len(arts)} 篇）")
                _print_table(arts)
        elif args.cmd == "export-cookie":
            info = export_cookie_header(store, Path(args.to), args.force, args.write_token)
            print(f"[✓] 已写出 {info['cookies']} 项 cookie → {info['target']}")
            print(f"    字段：{'、'.join(info['names'][:8])}{' …' if len(info['names']) > 8 else ''}")
            if info["token_file"]:
                print(f"    token={info['token']} → {info['token_file']}")
            print(f"    自检：python fetch_wechat.py --dry-run --campus zju")
        elif args.cmd == "diag":
            names = list(args.name or [])
            if not names and args.campus:
                names = load_account_names(args.campus)
                if not names:
                    print(f"[!] 没从 wxmp-accounts-{args.campus}.json 读到名单，改用 --name", file=sys.stderr)
            if not names:
                raise WxmpError("请用 --name 指定至少一个公众号，或 --campus 提供名单文件")
            print(f"[diag] 凭据 {(store.load_session() or {}).get('token')}，探测 {len(names)} 个号")
            diagnose(store, names, args.count, Path(args.out) if args.out else None)
        elif args.cmd == "cookie-from-curl":
            raw = Path(args.file).read_text("utf-8", "replace") if args.file else sys.stdin.read()
            got = parse_curl(raw)
            if not got.get("cookie"):
                raise WxmpError(
                    "没从这段文本里找到 Cookie。\n"
                    "  在抓包工具里对着 mp/profile_ext 那条请求右键 → Copy → Copy as cURL，"
                    "整段贴进来；\n"
                    "  或者直接用 Copy Request Headers，把 `Cookie:` 那一行存成文件。"
                )
            if "wap_sid2" not in got["cookie"]:
                print("[!] 这段 Cookie 里没有 wap_sid2 —— 很可能抓到的是“游客”请求，"
                      "拉不了历史列表。换「查看历史消息」触发的那条再抓一次。", file=sys.stderr)
            target = Path(args.to)
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists() and not args.force:
                raise WxmpError(f"{target} 已存在（避免覆盖有效凭据）。确认要换用 --force。")
            target.write_text("Cookie: " + got["cookie"] + "\n", "utf-8")
            print(f"[✓] 已写入 {target}")
            print(f"    Cookie: {mask_cookie(got['cookie'])}")
            for k in WAP_PARAM_KEYS:
                if got.get(k):
                    print(f"    {k}: {got[k][:16]}{'…' if len(got[k]) > 16 else ''}")
            # 顺手把参数落成 JSON，history 会自动拾取（省掉一长串 --uin --key ...）
            params_path = target.parent / WAP_PARAMS_PATH.name
            if any(got.get(k) for k in WAP_PARAM_KEYS):
                params_path.write_text(
                    json.dumps({k: got[k] for k in WAP_PARAM_KEYS if got.get(k)},
                               ensure_ascii=False, indent=2), "utf-8")
                print(f"[✓] 参数已写入 {params_path}")
            print("\n下一步：")
            print("  python tools/wxmp_spider.py history --name 浙江大学 -n 3")
        elif args.cmd == "pool":
            pdir = Path(args.dir)
            pobj = CredentialPool(pdir, quota=args.quota)
            if args.resume:
                pobj.resume()
                print("[✓] 已解除整池熔断（确保已经换过出口或降过频率）")
            if args.add:
                src = Path(args.cookie_file)
                if not src.exists():
                    raise WxmpError(f"缺少 {src}（要含 wap_sid2 的整行 Cookie）")
                cookie = read_cookie_file(src)
                if not cookie:
                    raise WxmpError(f"{src} 是空的")
                if "wap_sid2" not in cookie:
                    print("[!] 这份 Cookie 里没有 wap_sid2，拉不了历史列表", file=sys.stderr)
                name = re.sub(r"[^A-Za-z0-9_.-]", "_", args.add)
                pdir.mkdir(parents=True, exist_ok=True)
                (pdir / f"{name}.txt").write_text("Cookie: " + cookie + "\n", "utf-8")
                src_params = Path(args.params) if args.params else WAP_PARAMS_PATH
                if src_params.exists():
                    try:
                        raw = json.loads(src_params.read_text("utf-8"))
                    except (OSError, ValueError):
                        raw = {}
                    keep = {k: str(raw[k]) for k in WAP_PARAM_KEYS if raw.get(k)}
                    if keep:
                        (pdir / f"{name}.json").write_text(
                            json.dumps(keep, ensure_ascii=False, indent=2), "utf-8")
                        print(f"    参数一并写入 {name}.json：{', '.join(sorted(keep))}")
                print(f"[✓] 已加入号池：{pdir / (name + '.txt')}（{mask_cookie(cookie)}）")
            print(render_pool_status(pobj.status()))
            print(pobj.breaker_status())
            return 0
        elif args.cmd == "history":
            cap = load_capture_params(Path(args.params) if args.params else None)
            biz, nick = resolve_biz(args.name, args.biz or cap.get("__biz"))
            uin = args.uin or cap.get("uin", "")
            key = args.key or cap.get("key", "")
            ticket = args.pass_ticket or cap.get("pass_ticket", "")
            appmsg_token = args.appmsg_token or cap.get("appmsg_token", "")
            if cap:
                src = args.params or WAP_PARAMS_PATH
                print(f"[i] 参数取自 {src}：{', '.join(sorted(cap))}", file=sys.stderr)
            pool = None
            cookie = ""
            if args.pool_dir:
                pool = CredentialPool(args.pool_dir, quota=args.quota)
                n = len(pool.load())
                if not n:
                    raise WxmpError(
                        f"号池 {args.pool_dir} 是空的。加一个号：\n"
                        f"  python tools/wxmp_spider.py pool --add a1 --cookie-file {args.cookie_file}\n"
                        "  一份 .txt 放 `Cookie: ...`，同名的 .json 放 uin/key/pass_ticket 即为一套"
                    )
                print(f"[pool] {n} 个号，理论吞吐 {pool.capacity_per_hour()} 次/小时",
                      file=sys.stderr)
            else:
                cpath = Path(args.cookie_file)
                if not cpath.exists():
                    raise WxmpError(
                        f"缺少 {cpath}（要含 wap_sid2 的整行 Cookie）。怎么拿：\n"
                        "  1. 起抓包插件：mitmdump -s tools/wap_sid2_capture.py --mode wireguard\n"
                        "     （安卓副号走 WireGuard；PC 微信可用 --mode local），它会自动落盘；\n"
                        "  2. 或手机微信打开该号任意文章 → 右上「…」→「查看公众号」→「查看历史消息」，\n"
                        "     把那次 mp/profile_ext 请求「Copy as cURL」交给 cookie-from-curl；\n"
                        "  3. 手工也行：整行存成上面的文件（一行 `Cookie: ...` 即可）。\n"
                        "  详见 docs/A路线-wap_sid2抓包可行性.md"
                    )
                cookie = cpath.read_text("utf-8", "replace")
            if args.watch:
                state = Path(args.state) if args.state else Path(
                    f"_work/wechat/history-seen-{biz[:12]}.json")
                got = watch_history(
                    biz, cookie,
                    interval=args.interval, cycles=args.cycles, state_path=state,
                    output=Path(args.output) if args.output else None,
                    limit=max(args.limit, 50), since=args.since,
                    uin=uin, key=key, pass_ticket=ticket, appmsg_token=appmsg_token,
                    delay_ms=max(args.delay_ms, 500), pool=pool,
                )
                print(f"[watch] 结束，累计新增 {got} 条；状态文件 {state}")
                return 0
            rows = list_history(biz, cookie, args.limit, args.since,
                                uin, key, ticket, appmsg_token, args.delay_ms, args.max_pages,
                                pool=pool)
            if args.diff_from:
                prev = Path(args.diff_from)
                old_links: set[str] = set()
                if prev.exists():
                    try:
                        pdata = json.loads(prev.read_text("utf-8"))
                        old_links = {x.get("link") for x in (pdata.get("items") or []) if x.get("link")}
                    except Exception as exc:                 # noqa: BLE001
                        print(f"[!] 读上一轮 {prev} 失败（{exc}），按全量输出", file=sys.stderr)
                fresh = [r for r in rows if r["link"] not in old_links]
                print(f"[diff] 本轮 {len(rows)} 条，新增 {len(fresh)} 条", file=sys.stderr)
                rows = fresh
            if args.output:
                Path(args.output).parent.mkdir(parents=True, exist_ok=True)
                Path(args.output).write_text(
                    json.dumps({"biz": biz, "nickname": nick, "count": len(rows), "items": rows},
                               ensure_ascii=False, indent=2), "utf-8")
                print(f"[✓] {len(rows)} 条 → {args.output}", file=sys.stderr)
            if args.format == "json":
                print(json.dumps(rows, ensure_ascii=False, indent=2))
            else:
                print(f"[history] {nick or args.name or biz}（{biz}）共 {len(rows)} 条\n")
                for it in rows:
                    when = (datetime.fromtimestamp(it["datetime"]).strftime("%Y-%m-%d")
                            if it["datetime"] else "?")
                    print(f"{when}  {it['title']}")
                    print(f"          {it['link'][:110]}")
        elif args.cmd == "probe":
            rows = probe_capabilities(args.biz)
            if args.format == "json":
                print(json.dumps(rows, ensure_ascii=False, indent=2))
            else:
                print(f"[probe] 零凭据端点矩阵（biz={args.biz}）\n")
                print(f"{'端点':<20} {'HTTP':>4} {'ret':>8}  {'判定':<10} 含义")
                print("-" * 100)
                for r in rows:
                    ret = "" if r.get("ret") is None else str(r["ret"])
                    print(f"{r['name']:<20} {r.get('http', 0):>4} {ret:>8}  {r['verdict']:<10} {r['meaning']}")
                print("\n判读：404=真删了；200003/-3=接口在只缺凭据；200013=被限或能力收紧；"
                      "HTML『验证』=环境墙；HTML『错误』=缺 hid/cid。")
        elif args.cmd == "sogou":
            rows = sogou_fetch(args.query, args.limit, args.pages, args.body,
                               Path(args.md_dir) if args.md_dir else None)
            if args.format == "json":
                print(json.dumps(rows, ensure_ascii=False, indent=2))
            else:
                print(f"[搜狗] “{args.query}” {len(rows)} 条"
                      f"{'（含正文）' if args.body else ''}\n")
                for i, it in enumerate(rows, 1):
                    when = ""
                    if it.get("timestamp"):
                        import datetime
                        when = datetime.datetime.fromtimestamp(it["timestamp"]).strftime("%Y-%m-%d")
                    print(f"{i:>2}. {it['title']}")
                    print(f"    {it.get('account','?')}  {when}")
                    if it.get("link"):
                        print(f"    {it['link'][:96]}")
                    if it.get("chars"):
                        print(f"    正文 {it['chars']} 字符 / {it.get('images',0)} 图"
                              f"{'  → ' + it['md_path'] if it.get('md_path') else ''}")
                    if it.get("error"):
                        print(f"    ⚠ {it['error']}")
            if not args.body:
                print("\n提示：链接是搜狗签名短链（分钟级时效），要正文就加 --body --md-dir DIR 当场抓。")
        elif args.cmd == "scrape":
            md = fetch_article_markdown(args.url)
            if args.output:
                Path(args.output).parent.mkdir(parents=True, exist_ok=True)
                Path(args.output).write_text(md, "utf-8")
                print(f"[✓] 已写入 {args.output}（{len(md)} 字符）")
            else:
                print(md)
    except WxmpError as e:
        print(f"[✗] {e}", file=sys.stderr)
        return 2
    except Exception as e:  # noqa: BLE001
        print(f"[✗] {type(e).__name__}: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
